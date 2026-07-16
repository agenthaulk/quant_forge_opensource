"""HTTP routing and server composition for the local web adapter.

Workflow callables and ``DEFAULT_RD_CONFIG_PATH`` are resolved through
:mod:`quant_forge.apps.web.server` at request time so monkeypatches on the
server module namespace keep taking effect.

Static frontend assets (decision D8, CP6-1) are served from the whitelisted
``static/`` directory next to this module through :func:`_static_asset`,
which enforces resolve()+is_relative_to containment, serves plain files
only (no directory listing), and rejects everything else with a 404.
"""

from __future__ import annotations

import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from quant_forge.apps.web.api import (
    _active_llm,
    _control_token_for_bind,
    _docs_relpath_from_path,
    _factor_from_validation_payload,
    _int_parameter,
    _job_id_from_cancel_path,
    _job_id_from_path,
    _json_safe,
    _llm_provider_options,
    _optional_int,
    _optional_parameters_payload,
    _optional_parser_payload,
    _optional_standardization,
    _optional_str,
    _paths_payload,
    _query_parameter,
    _rd_status_payload,
    _registry_factor_id_from_path,
    _synthesis_block,
    _synthesis_factor_refs,
)
from quant_forge.apps.web.html import _index_html
from quant_forge.apps.web.jobs import LOGGER, RequestBodyTooLarge, _WebJobManager, _client_error_message
from quant_forge.apps.web.pipeline import (
    pipeline_report,
    PipelineStore,
    cancel_pipeline,
    confirm_pipeline,
    create_pipeline,
    create_pipeline_as_fallback,
    create_pipeline_from_edited_formula,
    create_rd_pipeline,
    fork_pipeline_from_failure,
    get_pipeline,
    list_active_pipelines,
    pre_validate_formula,
    retry_pipeline,
    update_pipeline_parameters,
)
from quant_forge.apps.web.narration import (
    ClarifyBlockedError,
    SidecarSessionStore,
    UnresolvedNarrationRefError,
    active_component_ids_for,
    assert_chat_not_sole_number_carrier,
    assert_clarify_unblocked,
    llm_readiness,
)
from quant_forge.apps.web.tools import (
    ACTION_TOOL_NAMES,
    SidecarJournal,
    TOOL_KINDS,
    ToolAuthorizationError,
    ToolBudgetError,
    ToolRegistry,
)
from quant_forge.specs.narration import NarrationNode
from quant_forge.apps.web.memory_review import memory_review_payload, review_promoted, review_rule
from quant_forge.config import QuantForgeConfig
from quant_forge.mcp.read_models import list_available_fields, list_available_operators
from quant_forge.research_loop.config import ResearchLoopConfig, load_research_loop_config
from quant_forge.research_loop.memory import ResearchMemoryStore
from quant_forge.research_loop.scheduler import (
    ResearchLoopScheduler,
    ResearchScheduleRequest,
)


MAX_REQUEST_BODY_BYTES = 1024 * 1024

STATIC_URL_PREFIX = "/static/"
STATIC_ROOT = Path(__file__).resolve().parent / "static"
STATIC_CONTENT_TYPES = {
    ".js": "text/javascript; charset=utf-8",
}


def _static_asset(url_path: str) -> tuple[bytes, str]:
    """Resolve a ``/static/`` URL path to ``(body, content_type)``.

    Containment contract (D8): only plain files with a whitelisted suffix
    directly inside the ``static/`` directory tree are served. Traversal
    segments (including percent-encoded ones), absolute paths, backslashes,
    directories (no listing), symlink escapes, and unknown suffixes all raise
    ``KeyError``, which the request handlers map to HTTP 404.
    """

    relative = unquote(url_path[len(STATIC_URL_PREFIX):])
    if not relative or "\x00" in relative or "\\" in relative:
        raise KeyError(f"unknown static asset: {url_path}")
    if relative.startswith("/") or ".." in relative.split("/"):
        raise KeyError(f"unknown static asset: {url_path}")
    root = STATIC_ROOT.resolve()
    candidate = (root / relative).resolve()
    if candidate == root or not candidate.is_relative_to(root):
        raise KeyError(f"unknown static asset: {url_path}")
    content_type = STATIC_CONTENT_TYPES.get(candidate.suffix)
    if content_type is None or not candidate.is_file():
        raise KeyError(f"unknown static asset: {url_path}")
    # Authorize-then-open guard (same pattern as _read_bench_artifact /
    # _docs_document_payload in api.py): O_NOFOLLOW fails the open (ELOOP ->
    # OSError) if the final path component was swapped for a symlink after the
    # resolve()-based containment check above, closing the TOCTOU that a plain
    # read_bytes() would leave open. Any OSError -- the symlink race, or the
    # file disappearing after the is_file() check -- maps to the same "unknown
    # static asset" KeyError (HTTP 404) the missing/invalid cases already use.
    try:
        fd = os.open(candidate, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        raise KeyError(f"unknown static asset: {url_path}") from None
    try:
        with os.fdopen(fd, "rb") as handle:
            body = handle.read()
    except OSError:
        raise KeyError(f"unknown static asset: {url_path}") from None
    return body, content_type


def _pipeline_id_from_path(path: str) -> str:
    parts = path.strip("/").split("/")
    if len(parts) != 3 or parts[:2] != ["api", "pipelines"] or not parts[2]:
        raise KeyError(f"unknown pipeline path: {path}")
    return parts[2]


def _pipeline_id_from_action_path(path: str, action: str) -> str:
    parts = path.strip("/").split("/")
    if len(parts) != 4 or parts[:2] != ["api", "pipelines"] or parts[3] != action or not parts[2]:
        raise KeyError(f"unknown pipeline path: {path}")
    return parts[2]


def _sidecar_pipeline_id_from_path(path: str, suffix: str) -> str:
    """``/api/sidecar/pipelines/<id>/<suffix>`` -> ``<id>`` (suffix fixed)."""

    parts = path.strip("/").split("/")
    if len(parts) != 5 or parts[:3] != ["api", "sidecar", "pipelines"] or parts[4] != suffix or not parts[3]:
        raise KeyError(f"unknown sidecar path: {path}")
    return parts[3]


def _sidecar_tool_from_path(path: str) -> tuple[str, str]:
    """``/api/sidecar/pipelines/<id>/tools/<name>`` -> ``(<id>, <name>)``."""

    parts = path.strip("/").split("/")
    if len(parts) != 6 or parts[:3] != ["api", "sidecar", "pipelines"] or parts[4] != "tools" or not parts[3] or not parts[5]:
        raise KeyError(f"unknown sidecar tool path: {path}")
    return parts[3], parts[5]


def _sidecar_session_payload(
    sessions: SidecarSessionStore, journal: SidecarJournal, pipeline_id: str
) -> dict[str, Any]:
    """Clarify session + tool journal for a pipeline, for the sidecar drawer +
    rejoin/replay. An absent session renders an empty (trivially unblocked)
    interview -- e.g. no-LLM degradation posed nothing (spec §10)."""

    session = sessions.load(pipeline_id)
    if session is None:
        clarify = {"pipeline_id": pipeline_id, "questions": [], "answers": [], "blocking_unanswered": [], "executable": True}
    else:
        clarify = {
            **session.to_dict(),
            "blocking_unanswered": session.blocking_unanswered(),
            "executable": session.is_executable(),
        }
    return {"clarify": clarify, "journal": journal.rows(pipeline_id)}


def _sidecar_clarify_answer(
    sessions: SidecarSessionStore, pipeline_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """Record one clarify answer (or a skip = accept default) and persist it.

    A later answer that replaces an earlier one keeps BOTH in provenance
    (spec §5.2). The session must already exist (the sidecar posed the
    questions); answering an un-posed pipeline is a 404.
    """

    session = sessions.load(pipeline_id)
    if session is None:
        raise KeyError(f"no clarify session for pipeline: {pipeline_id}")
    session.answer(
        str(payload.get("question_key", "")),
        _optional_str(payload.get("option_id")),
        skipped=bool(payload.get("skipped", False)),
    )
    sessions.save(session)
    return {
        "clarify": {
            **session.to_dict(),
            "blocking_unanswered": session.blocking_unanswered(),
            "executable": session.is_executable(),
        },
        "provenance": [entry.to_dict() for entry in session.provenance_entries()],
    }


def _sidecar_invoke_tool(
    registry: ToolRegistry,
    journal: SidecarJournal,
    sessions: SidecarSessionStore,
    *,
    pipeline_store: PipelineStore,
    job_manager: Any,
    config: QuantForgeConfig,
    pipeline_id: str,
    tool_name: str,
    arguments: dict[str, Any],
    objective: str,
    nav_target: str | None,
    supplied_capability: str,
) -> dict[str, Any]:
    """Authorize + invoke one allowlisted tool scoped to a pipeline, then
    journal the action + its narration so a replay reproduces the same cards.

    The per-run grant is created on first touch and reused (so budgets
    accumulate); an action tool needs the frontend to have called ``authorize``
    and to present the token as ``X-Sidecar-Capability`` -- otherwise
    :meth:`ToolRegistry.invoke` raises 401 (even on loopback, spec §5.7).
    """

    grant = registry.grant_for(pipeline_id) or registry.authorize(pipeline_id)
    # Blocking clarify questions gate the confirm action server-side too (the
    # second FE-L4 enforcement point, mirroring the direct /confirm route).
    if tool_name == "confirm_pipeline":
        assert_clarify_unblocked(sessions.load(pipeline_id))
    result = registry.invoke(tool_name, arguments, grant=grant, capability=supplied_capability or None)
    # One journaled sidecar action. Narration for a tool result is a labelled
    # STATUS node (no number ever inline -- assert_chat_not_sole_number_carrier
    # re-checks). Numbers reach the UI only when a later ref node points a
    # canonical renderer at one of `result.artifact_refs`.
    narration = [
        NarrationNode(kind="status", message_key=f"sidecar.tool.{tool_name}", args=[tool_name]).to_dict()
    ]
    assert_chat_not_sole_number_carrier(narration)
    input_refs = {
        key: arguments[key]
        for key in ("factor_id", "pipeline_id", "parse_job_id", "run_id")
        if key in arguments
    }
    journal.record(
        pipeline_id,
        tool=tool_name,
        objective=objective,
        input_refs=input_refs,
        request_hash=registry.request_hash(tool_name, arguments),
        artifact_refs=result.artifact_refs,
        nav_target=nav_target,
        narration=tuple(narration),
    )
    return {"result": result.to_dict(), "narration": narration}


def _optional_universe_filters(value: Any) -> tuple[str, ...]:
    """Shape a request ``universe_filters`` list into a tuple of strings.

    Strict (F8): a non-list, or a list with any non-string item, is a 400 --
    no ``str()`` coercion of a numeric/object item into a fake filter. An
    absent/empty value is an empty tuple. The ValidationGate remains the single
    authority on which filter FORMS are accepted (``specs/validation_gate.py``).
    """

    if value in (None, ""):
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("universe_filters must be a JSON array of strings")
    return tuple(value)


def _memory_review_str_field(payload: dict[str, Any], name: str) -> str:
    """One field from a ``POST /api/memory/review/*`` body, type-checked at
    the JSON boundary (review finding P4B-F1).

    A JSON value that is not a string -- ``null``, a number, an object, an
    array, a bool -- must never reach :func:`review_rule`/
    :func:`review_promoted` through a blind ``str()`` coercion:
    ``str(None) == "None"``, ``str(42) == "42"``, ``str({}) == "{}"`` are
    all non-empty after ``.strip()``, so they would silently pass the
    "actor is required" check downstream and fabricate a persisted actor
    identity ("None" as a reviewer!), or corrupt a signature-prefix lookup.
    An absent key reads as ``""`` (unchanged prior behavior for every
    optional field on this surface); a present key must be a string
    (``""`` included) or this raises ``ValueError``, which ``do_POST``'s
    existing handler maps to a clean 400 -- the action is never called.
    """

    if name not in payload:
        return ""
    value = payload[name]
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string, got {type(value).__name__}")
    return value


def create_local_web_server(
    *, host: str, port: int, config: QuantForgeConfig, rd_config: ResearchLoopConfig | None = None
) -> ThreadingHTTPServer:
    from quant_forge.apps.web import server as _server

    allowed_hosts = {"127.0.0.1", "localhost"}
    if config.web.allow_docker_bind:
        allowed_hosts.add("0.0.0.0")
    if host not in allowed_hosts:
        raise ValueError(
            "OpenSource web adapter is local-only; use 127.0.0.1 or localhost. "
            "Set web.allow_docker_bind only for Docker containers published to host loopback."
        )

    research_config = rd_config or load_research_loop_config(_server.DEFAULT_RD_CONFIG_PATH, config.research, config.simulation)
    scheduler = ResearchLoopScheduler(
        lambda seed_factor_id, objective, max_candidates, iterations: _server.run_research_once_workflow(
            config,
            seed_factor_id,
            objective=objective,
            max_candidates=max_candidates,
            iterations=iterations,
            rd_config=research_config,
        ),
        allowed_interval_days=research_config.allowed_interval_days,
    )
    job_manager = _WebJobManager()
    pipeline_store = PipelineStore(config.paths.artifact_root)
    # Sidecar surface (agent_sidecar_frontend.md §5.7): the in-process typed
    # tool adapter, its per-run journal, and clarify-session persistence. The
    # tool registry mints its OWN per-run capability token for action tools --
    # independent of `control_token` below, which is empty on a loopback bind
    # (the network bearer the general routes skip). Action tools stay gated
    # even then (spec §5.7).
    tool_registry = ToolRegistry(
        config=config, store=pipeline_store, job_manager=job_manager, rd_config=research_config
    )
    sidecar_journal = SidecarJournal(config.paths.artifact_root)
    sidecar_sessions = SidecarSessionStore(config.paths.artifact_root)
    control_token = _control_token_for_bind(host, config)
    control_token_required = bool(control_token)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed_url = urlparse(self.path)
            path = parsed_url.path
            try:
                if path == "/health":
                    self._json({"ok": True})
                elif path == "/catalog":
                    self._require_control_token()
                    self._json({"fields": list_available_fields(), "operators": list_available_operators()})
                elif path == "/api/research/history":
                    self._require_control_token()
                    # ValueError (limit validation) is reflected for the new
                    # read-only endpoints only; pre-existing GET routes keep
                    # the generic "request failed" mapping below unchanged.
                    try:
                        payload = _server._research_history_payload(
                            config,
                            limit=_query_parameter(parsed_url.query, "limit"),
                        )
                    except ValueError as exc:
                        self._json({"error": str(exc)}, status=400)
                    else:
                        self._json(payload)
                elif path == "/api/bench":
                    self._require_control_token()
                    try:
                        payload = _server._bench_runs_payload(
                            config,
                            limit=_query_parameter(parsed_url.query, "limit"),
                        )
                    except ValueError as exc:
                        self._json({"error": str(exc)}, status=400)
                    else:
                        self._json(payload)
                elif path == "/api/data/catalog":
                    self._require_control_token()
                    self._json(_server._data_catalog_payload(config))
                elif path == "/api/data/status":
                    # No per-route ValueError reflection here on purpose: this
                    # route takes no parameters, and validation-layer errors
                    # (for example an unreadable panel file) may carry local
                    # path detail, so they keep the generic mapping below.
                    self._require_control_token()
                    self._json(_server._data_status_payload(config))
                elif path == "/api/synthesis/methods":
                    self._require_control_token()
                    self._json(_server._synthesis_methods_payload(config))
                elif path == "/api/registry/factors":
                    self._require_control_token()
                    self._json(_server._registry_factors_payload(config))
                elif path.startswith("/api/registry/factors/"):
                    self._require_control_token()
                    try:
                        payload = _server._registry_factor_detail_payload(
                            config,
                            _registry_factor_id_from_path(path),
                            limit=_query_parameter(parsed_url.query, "limit"),
                            kind=_query_parameter(parsed_url.query, "kind"),
                        )
                    except ValueError as exc:
                        self._json({"error": str(exc)}, status=400)
                    else:
                        self._json(payload)
                elif path == "/api/docs":
                    self._require_control_token()
                    self._json(_server._docs_list_payload(config))
                elif path.startswith("/api/docs/"):
                    self._require_control_token()
                    self._json(_server._docs_document_payload(config, _docs_relpath_from_path(path)))
                elif path == "/api/extensions":
                    self._require_control_token()
                    self._json(_server._extensions_payload(config))
                elif path == "/api/memory/review":
                    # SE-P4b: rule governance + findings/failures + priors +
                    # optional read-only plugin pane (memory_review_payload's
                    # plugin_store parameter). No config/env hook resolves a
                    # plugin artifact root anywhere in this repo yet, so V1
                    # never constructs one here and the plugin pane always
                    # renders omitted (payload["plugin"] is None) until one
                    # is added deliberately -- see memory_review.py's
                    # docstring for the full plugin-pane contract.
                    self._require_control_token()
                    memory_store = ResearchMemoryStore(config.paths.artifact_root)
                    self._json(memory_review_payload(memory_store))
                elif path == "/api/status":
                    self._require_control_token()
                    active_llm = _active_llm(config)
                    self._json(
                        {
                            "name": "Quant Forge",
                            "paths": _paths_payload(config),
                            "llm": {
                                "provider": active_llm.provider,
                                "model": active_llm.model,
                                "api_key_env": active_llm.api_key_env,
                                "providers": _llm_provider_options(config),
                            },
                            "rd": _rd_status_payload(config, research_config),
                        }
                    )
                elif path == "/api/research/status":
                    self._require_control_token()
                    self._json(_json_safe(scheduler.status()))
                elif path.startswith("/api/jobs/"):
                    self._require_control_token()
                    self._json(job_manager.get(_job_id_from_path(path)))
                elif path == "/api/pipelines":
                    # Rejoin (spec §2.3): the frontend queries active
                    # pipelines on load and re-attaches; refresh and server
                    # restart never silently strand a running computation.
                    self._require_control_token()
                    self._json(
                        {
                            "pipelines": [
                                record.to_dict()
                                for record in list_active_pipelines(pipeline_store, job_manager=job_manager, config=config)
                            ]
                        }
                    )
                elif path.startswith("/api/pipelines/") and path.endswith("/report"):
                    # Restart-proof report retrieval (re-verify RV-F4): the
                    # live job result while the manager remembers it, else
                    # the durable completion artifact -- a recovered
                    # completed pipeline must still render a report.
                    self._require_control_token()
                    self._json(
                        pipeline_report(
                            pipeline_store,
                            _pipeline_id_from_action_path(path, "report"),
                            job_manager=job_manager,
                            config=config,
                        )
                    )
                elif path.startswith("/api/pipelines/"):
                    self._require_control_token()
                    record = get_pipeline(
                        pipeline_store, _pipeline_id_from_path(path), job_manager=job_manager, config=config
                    )
                    self._json(record.to_dict())
                elif path == "/api/sidecar/readiness":
                    # LLM readiness tri-state (spec §5.6/§10). The client's
                    # pre-fetch default is `unknown`; this authenticated read
                    # returns the real ready/unavailable state.
                    self._require_control_token()
                    self._json({"readiness": llm_readiness(config)})
                elif path == "/api/sidecar/tools":
                    # Model-facing tool catalog (MCP-shaped). Carries no token
                    # of any kind (bearer never in model context, spec §5.7).
                    self._require_control_token()
                    self._json({"tools": tool_registry.catalog()})
                elif path.startswith("/api/sidecar/pipelines/") and path.endswith("/session"):
                    self._require_control_token()
                    self._json(
                        _sidecar_session_payload(
                            sidecar_sessions, sidecar_journal, _sidecar_pipeline_id_from_path(path, "session")
                        )
                    )
                elif path.startswith(STATIC_URL_PREFIX):
                    # Static frontend modules are public like the index page
                    # itself; they contain no runtime values or secrets.
                    body, content_type = _static_asset(path)
                    self._bytes(body, content_type)
                elif path == "/api" or path.startswith("/api/"):
                    # Unmatched paths in the API namespace are contract
                    # errors: scripts and JSON consumers probing a typo'd
                    # endpoint need 404 JSON, never the HTML shell. The
                    # response is uniform for every unknown API path and
                    # carries no runtime state, so like the index page it
                    # does not require the control token.
                    self._json({"error": f"unknown API path: {path}"}, status=404)
                else:
                    # Deliberate single-page fallthrough: the frontend is a
                    # hash-routed single page, so every unknown non-API GET
                    # path serves the index shell (deep links and typos
                    # alike land in the app).
                    self._html(
                        _index_html(
                            config,
                            research_config,
                            control_token_required=control_token_required,
                            redact_runtime=control_token_required,
                        )
                    )
            except KeyError as exc:
                self._json({"error": str(exc)}, status=404)
            except PermissionError:
                self._json({"error": "unauthorized"}, status=401)
            except Exception:
                LOGGER.exception("web GET request failed")
                self._json({"error": "request failed"}, status=400)

        def do_POST(self) -> None:
            try:
                path = urlparse(self.path).path
                self._require_control_token()
                payload = self._read_json()
                if path == "/api/run-idea":
                    result = _server.run_idea_workflow(
                        config,
                        str(payload.get("text", "")),
                        parser_mode=str(payload.get("parser_mode", "llm")),
                        llm_provider=_optional_str(payload.get("llm_provider")),
                        rd_config=research_config,
                    )
                    self._json(result)
                    return
                if path == "/api/parse-idea":
                    result = _server.run_idea_parse_workflow(
                        config,
                        str(payload.get("text", "")),
                        parser_mode=str(payload.get("parser_mode", "llm")),
                        llm_provider=_optional_str(payload.get("llm_provider")),
                        rd_config=research_config,
                    )
                    self._json(result)
                    return
                if path == "/api/validate-idea":
                    result = _server.run_idea_validation_workflow(
                        config,
                        _factor_from_validation_payload(payload, config),
                        parser=_optional_parser_payload(payload.get("parser")),
                        parameters=_optional_parameters_payload(payload.get("parameters")),
                        rd_config=research_config,
                    )
                    self._json(result)
                    return
                if path == "/api/staggered-entry":
                    result = _server.run_staggered_entry_workflow(
                        config,
                        str(payload.get("factor_id", "")),
                        parameters=_optional_parameters_payload(payload.get("parameters")),
                        formation_trading_days=_optional_int(payload.get("formation_trading_days"), "formation_trading_days"),
                        rd_config=research_config,
                    )
                    self._json(result)
                    return
                if path == "/api/research/run-once":
                    result = _server.run_research_once_workflow(
                        config,
                        str(payload.get("seed_factor_id", "")),
                        objective=str(payload.get("objective", research_config.objective)),
                        max_candidates=_optional_int(payload.get("max_candidates"), "max_candidates"),
                        iterations=_optional_int(payload.get("iterations"), "iterations"),
                        rd_config=research_config,
                    )
                    self._json(result)
                    return
                if path == "/api/jobs/run-idea":
                    self._json(
                        job_manager.start(
                            "run_idea",
                            lambda cancel_event: _server.run_idea_workflow(
                                config,
                                str(payload.get("text", "")),
                                parser_mode=str(payload.get("parser_mode", "llm")),
                                llm_provider=_optional_str(payload.get("llm_provider")),
                                rd_config=research_config,
                                cancel_event=cancel_event,
                            ),
                        ),
                        status=202,
                    )
                    return
                if path == "/api/jobs/parse-idea":
                    parse_text = str(payload.get("text", ""))
                    parse_mode = str(payload.get("parser_mode", "llm"))
                    self._json(
                        job_manager.start(
                            "parse_idea",
                            lambda cancel_event: _server.run_idea_parse_workflow(
                                config,
                                parse_text,
                                parser_mode=parse_mode,
                                llm_provider=_optional_str(payload.get("llm_provider")),
                                rd_config=research_config,
                                cancel_event=cancel_event,
                            ),
                            # Echoed back through job_manager.get() only for
                            # apps/web/pipeline.py::create_pipeline's genuine
                            # per-field provenance derivation (phase-review
                            # F3) -- never re-parsed or trusted as a claim
                            # about the RESULT, just the recorded INPUT the
                            # parser itself was given.
                            request={"text": parse_text, "parser_mode": parse_mode},
                        ),
                        status=202,
                    )
                    return
                if path == "/api/jobs/validate-idea":
                    self._json(
                        job_manager.start(
                            "validate_idea",
                            lambda cancel_event: _server.run_idea_validation_workflow(
                                config,
                                _factor_from_validation_payload(payload, config),
                                parser=_optional_parser_payload(payload.get("parser")),
                                parameters=_optional_parameters_payload(payload.get("parameters")),
                                rd_config=research_config,
                                cancel_event=cancel_event,
                            ),
                        ),
                        status=202,
                    )
                    return
                if path == "/api/jobs/staggered-entry":
                    self._json(
                        job_manager.start(
                            "staggered_entry",
                            lambda cancel_event: _server.run_staggered_entry_workflow(
                                config,
                                str(payload.get("factor_id", "")),
                                parameters=_optional_parameters_payload(payload.get("parameters")),
                                formation_trading_days=_optional_int(
                                    payload.get("formation_trading_days"),
                                    "formation_trading_days",
                                ),
                                rd_config=research_config,
                                cancel_event=cancel_event,
                            ),
                        ),
                        status=202,
                    )
                    return
                if path == "/api/jobs/research-run-once":
                    self._json(
                        job_manager.start(
                            "research_run_once",
                            lambda cancel_event: _server.run_research_once_workflow(
                                config,
                                str(payload.get("seed_factor_id", "")),
                                objective=str(payload.get("objective", research_config.objective)),
                                max_candidates=_optional_int(payload.get("max_candidates"), "max_candidates"),
                                iterations=_optional_int(payload.get("iterations"), "iterations"),
                                rd_config=research_config,
                                cancel_event=cancel_event,
                            ),
                        ),
                        status=202,
                    )
                    return
                if path == "/api/jobs/multi-factor-backtest":
                    # Shape guards run eagerly (not inside the deferred job
                    # lambda) and the preflight re-asserts every §13 request
                    # rejection — including the data-dependent WINDOW_TOO_SHORT
                    # and UNIVERSE_MISMATCH — synchronously, so a bad request
                    # is a clean 400 here instead of a failed background job.
                    factor_refs = _synthesis_factor_refs(payload.get("factor_refs"))
                    synthesis = _synthesis_block(payload.get("synthesis"))
                    standardization = _optional_standardization(payload.get("standardization"))
                    run_parameters = _optional_parameters_payload(payload.get("parameters"))
                    _server.preflight_multi_factor_backtest(
                        config,
                        factor_refs=factor_refs,
                        synthesis=synthesis,
                        standardization=standardization,
                        parameters=run_parameters,
                        rd_config=research_config,
                    )
                    self._json(
                        job_manager.start(
                            "multi_factor_backtest",
                            lambda cancel_event: _server.run_multi_factor_backtest_workflow(
                                config,
                                factor_refs=factor_refs,
                                synthesis=synthesis,
                                standardization=standardization,
                                parameters=run_parameters,
                                rd_config=research_config,
                                cancel_event=cancel_event,
                            ),
                        ),
                        status=202,
                    )
                    return
                if path.startswith("/api/jobs/") and path.endswith("/cancel"):
                    self._json(job_manager.cancel(_job_id_from_cancel_path(path)))
                    return
                if path == "/api/pipelines/pre-validate":
                    # Editable-formula pre-validation (spec §5.3): canonicalize +
                    # ValidationGate, NO persist / eval / backtest. An unknown
                    # operator returns an operator_drafts review-packet ref and
                    # NEVER hot-executes. Distinct from /api/validate-idea, which
                    # runs the whole evaluation chain. Request types are STRICT
                    # (F8): the raw formula/horizon are passed through UNCOERCED
                    # so pre_validate_formula can 400 a non-string formula or a
                    # non-integer/invalid horizon instead of silently str()/
                    # int()-coercing or defaulting it.
                    self._json(
                        pre_validate_formula(
                            payload.get("formula"),
                            name=payload.get("name", ""),
                            horizon_days=payload.get("horizon_days", 5),
                            universe_filters=_optional_universe_filters(payload.get("universe_filters")),
                        )
                    )
                    return
                if path == "/api/pipelines":
                    kind = str(payload.get("kind", "factor_study"))
                    if kind == "rd_optimize":
                        # Pipeline B (spec §2.1): user-initiated, seeded from an
                        # explicit factor id (a completed report's factor or a
                        # registry factor). There is NO automatic A->B bridge --
                        # nothing here is reached by A completing. Rounds are
                        # validated server-side inside create_rd_pipeline.
                        record = create_rd_pipeline(
                            pipeline_store,
                            job_manager=job_manager,
                            config=config,
                            seed_factor_id=str(payload.get("seed_factor_id", "")),
                            rd_config=research_config,
                            rounds=_optional_int(payload.get("rounds"), "rounds"),
                            candidates_per_round=_optional_int(
                                payload.get("candidates_per_round"), "candidates_per_round"
                            ),
                            objective=_optional_str(payload.get("objective")),
                        )
                    else:
                        # create_pipeline takes a job id, never a client-supplied
                        # parser/factor payload (FE-L3): the parser/factor this
                        # pipeline stores comes from job_manager's OWN stored
                        # result for parse_job_id, not from this request body.
                        record = create_pipeline(
                            pipeline_store,
                            job_manager=job_manager,
                            parse_job_id=str(payload.get("parse_job_id", "")),
                            rd_config=research_config,
                            kind=kind,
                        )
                    self._json(record.to_dict(), status=201)
                    return
                if path.startswith("/api/pipelines/") and path.endswith("/confirm"):
                    confirm_pipeline_id = _pipeline_id_from_action_path(path, "confirm")
                    # Blocking clarify questions gate execution (spec §5.2,
                    # FE-L4). Enforced server-side at BOTH confirm entrypoints
                    # (this direct route and the confirm_pipeline tool), so the
                    # UI is never the only gate. A pipeline the sidecar never
                    # interviewed has no session -> trivially unblocked.
                    assert_clarify_unblocked(sidecar_sessions.load(confirm_pipeline_id))
                    record = confirm_pipeline(
                        config,
                        pipeline_store,
                        confirm_pipeline_id,
                        nonce=str(payload.get("nonce", "")),
                        version=_int_parameter(payload.get("version", 0), "version"),
                        job_manager=job_manager,
                        rd_config=research_config,
                        parameters=_optional_parameters_payload(payload.get("parameters")),
                    )
                    self._json(record.to_dict())
                    return
                if path.startswith("/api/pipelines/") and path.endswith("/cancel"):
                    record = cancel_pipeline(
                        pipeline_store, _pipeline_id_from_action_path(path, "cancel"), job_manager=job_manager, config=config
                    )
                    self._json(record.to_dict())
                    return
                if path.startswith("/api/pipelines/") and path.endswith("/retry"):
                    record = retry_pipeline(
                        pipeline_store, _pipeline_id_from_action_path(path, "retry"), job_manager=job_manager, config=config
                    )
                    self._json(record.to_dict())
                    return
                if path.startswith("/api/pipelines/") and path.endswith("/parameters"):
                    record = update_pipeline_parameters(
                        pipeline_store,
                        _pipeline_id_from_action_path(path, "parameters"),
                        dict(payload.get("parameters") or {}),
                        job_manager=job_manager,
                        config=config,
                    )
                    self._json(record.to_dict())
                    return
                if path.startswith("/api/pipelines/") and path.endswith("/fork"):
                    # phase-review F7 "edit" exit: forks the frozen inputs of
                    # a paused_failure pipeline into a brand-new draft
                    # (its own attempt lineage, parent_run_id set) and
                    # terminalizes the old one -- the failed attempt's own
                    # history stays intact under its own id. The payload's
                    # `parameters` carries the user's pending edits from the
                    # paused card (re-verify RV-F9); the server validates and
                    # applies them as human_override against the frozen
                    # baseline instead of silently discarding them.
                    record = fork_pipeline_from_failure(
                        pipeline_store,
                        _pipeline_id_from_action_path(path, "fork"),
                        job_manager=job_manager,
                        config=config,
                        rd_config=research_config,
                        parameters=dict(payload.get("parameters") or {}),
                    )
                    self._json(record.to_dict(), status=201)
                    return
                if path.startswith("/api/pipelines/") and path.endswith("/edit-formula"):
                    # F2d (compare loop, spec §5.3/§5.4): a validated formula
                    # edit the user RUNS branches a NEW immutable factor_study
                    # run from this (completed) pipeline. edited_by=human is
                    # derived SERVER-side by fingerprint comparison inside
                    # create_pipeline_from_edited_formula -- the request never
                    # asserts it. The edit must pass read-only pre-validation
                    # (ready) first; an unknown-operator edit is refused here.
                    record = create_pipeline_from_edited_formula(
                        pipeline_store,
                        job_manager=job_manager,
                        config=config,
                        rd_config=research_config,
                        parent_pipeline_id=_pipeline_id_from_action_path(path, "edit-formula"),
                        formula=payload.get("formula"),
                        universe_filters=(
                            _optional_universe_filters(payload["universe_filters"])
                            if "universe_filters" in payload
                            else None
                        ),
                        horizon_days=payload.get("horizon_days"),
                    )
                    self._json(record.to_dict(), status=201)
                    return
                if path.startswith("/api/pipelines/") and path.endswith("/fallback-rule-parse"):
                    # phase-review F7 "fall back to rule parse" exit, fully
                    # server-side (re-verify RV-F10): the rule parse runs
                    # inside create_pipeline_as_fallback against the failed
                    # pipeline's own PERSISTED idea text. No client-supplied
                    # parse job id is accepted anymore -- a crafted request
                    # can no longer substitute an unrelated parse, and
                    # job-manager pruning can no longer strand this exit.
                    record = create_pipeline_as_fallback(
                        pipeline_store,
                        job_manager=job_manager,
                        rd_config=research_config,
                        parent_pipeline_id=_pipeline_id_from_action_path(path, "fallback-rule-parse"),
                        config=config,
                    )
                    self._json(record.to_dict(), status=201)
                    return
                if path == "/api/research/schedule":
                    action = str(payload.get("action", "")).strip().lower()
                    if action == "start":
                        request = ResearchScheduleRequest(
                            seed_factor_id=str(payload.get("seed_factor_id", "")),
                            objective=str(payload.get("objective", research_config.objective)),
                            interval_days=_int_parameter(
                                payload.get("interval_days", research_config.default_interval_days),
                                "interval_days",
                            ),
                            max_candidates=_int_parameter(
                                payload.get("max_candidates", research_config.default_max_candidates),
                                "max_candidates",
                            ),
                            iterations=_int_parameter(payload.get("iterations", 1), "iterations"),
                        )
                        self._json(_json_safe(scheduler.start(request)))
                        return
                    if action == "stop":
                        self._json(_json_safe(scheduler.stop()))
                        return
                    self._json({"error": "action must be start or stop"}, status=400)
                    return
                if path.startswith("/api/sidecar/pipelines/") and path.endswith("/authorize"):
                    # Mint a per-run tool grant. The capability token crosses to
                    # the TRUSTED frontend (which holds it and presents it as
                    # X-Sidecar-Capability on action-tool calls) over the same
                    # already-authorized channel -- it is never handed to the
                    # model (spec §5.7: bearer never in model context).
                    grant = tool_registry.authorize(_sidecar_pipeline_id_from_path(path, "authorize"))
                    self._json(
                        {"pipeline_id": grant.pipeline_id, "created_at": grant.created_at, "capability": grant.capability},
                        status=201,
                    )
                    return
                if path.startswith("/api/sidecar/pipelines/") and path.endswith("/clarify"):
                    self._json(
                        _sidecar_clarify_answer(
                            sidecar_sessions,
                            _sidecar_pipeline_id_from_path(path, "clarify"),
                            payload,
                        )
                    )
                    return
                if path.startswith("/api/sidecar/pipelines/") and "/tools/" in path:
                    sidecar_pipeline_id, tool_name = _sidecar_tool_from_path(path)
                    self._json(
                        _sidecar_invoke_tool(
                            tool_registry,
                            sidecar_journal,
                            sidecar_sessions,
                            pipeline_store=pipeline_store,
                            job_manager=job_manager,
                            config=config,
                            pipeline_id=sidecar_pipeline_id,
                            tool_name=tool_name,
                            arguments=dict(payload.get("arguments") or {}),
                            objective=str(payload.get("objective", "")),
                            nav_target=_optional_str(payload.get("nav_target")),
                            supplied_capability=self.headers.get("X-Sidecar-Capability", ""),
                        )
                    )
                    return
                if path == "/api/memory/review/rule":
                    # SE-P4b: review_rule validates actor/action and appends
                    # exactly one event via the store's atomic
                    # resolve_validate_append; ValueError (empty actor,
                    # unknown action, ambiguous/absent signature prefix)
                    # falls through to the generic ValueError -> 400 mapping
                    # below. No plugin_store passed in V1, matching the GET
                    # route above. P4B-F1: every field is extracted through
                    # _memory_review_str_field, which rejects a non-string
                    # JSON value (null/number/object/array/bool) with a 400
                    # BEFORE review_rule is ever called -- a blind str()
                    # here would let {"actor": null} persist "None" as the
                    # reviewer identity.
                    memory_store = ResearchMemoryStore(config.paths.artifact_root)
                    self._json(
                        review_rule(
                            memory_store,
                            signature_prefix=_memory_review_str_field(payload, "signature_prefix"),
                            action=_memory_review_str_field(payload, "action"),
                            actor=_memory_review_str_field(payload, "actor"),
                            rationale=_memory_review_str_field(payload, "rationale"),
                            expected_entry_id=_memory_review_str_field(payload, "expected_entry_id"),
                        )
                    )
                    return
                if path == "/api/memory/review/promoted":
                    memory_store = ResearchMemoryStore(config.paths.artifact_root)
                    self._json(
                        review_promoted(
                            memory_store,
                            kind=_memory_review_str_field(payload, "kind"),
                            signature_prefix=_memory_review_str_field(payload, "signature_prefix"),
                            action=_memory_review_str_field(payload, "action"),
                            actor=_memory_review_str_field(payload, "actor"),
                            rationale=_memory_review_str_field(payload, "rationale"),
                            expected_entry_id=_memory_review_str_field(payload, "expected_entry_id"),
                        )
                    )
                    return
                self._json({"error": f"unknown endpoint: {path}"}, status=404)
            except ToolAuthorizationError:
                self._json({"error": "unauthorized"}, status=401)
            except ClarifyBlockedError as exc:
                self._json({"error": str(exc)}, status=409)
            except ToolBudgetError as exc:
                self._json({"error": str(exc)}, status=429)
            except UnresolvedNarrationRefError as exc:
                self._json({"error": str(exc)}, status=400)
            except KeyError as exc:
                self._json({"error": str(exc)}, status=404)
            except PermissionError:
                self._json({"error": "unauthorized"}, status=401)
            except RequestBodyTooLarge as exc:
                self._json({"error": str(exc)}, status=413)
            except ValueError as exc:
                self._json({"error": str(exc)}, status=400)
            except Exception as exc:
                LOGGER.exception("web POST request failed")
                self._json({"error": _client_error_message(exc, fallback="request failed")}, status=400)

        def log_message(self, format: str, *args: object) -> None:
            return

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0:
                return {}
            if length > MAX_REQUEST_BODY_BYTES:
                raise RequestBodyTooLarge(
                    f"request body exceeds {MAX_REQUEST_BODY_BYTES} bytes"
                )
            raw = self.rfile.read(length).decode("utf-8")
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("request body must be a JSON object")
            return payload

        def _json(self, payload: dict[str, Any], *, status: int = 200) -> None:
            body = json.dumps(_json_safe(payload), ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _html(self, body: str) -> None:
            encoded = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def _bytes(self, body: bytes, content_type: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _require_control_token(self) -> None:
            if not control_token_required:
                return
            supplied = self.headers.get("Authorization", "")
            expected = f"Bearer {control_token}"
            if not hmac.compare_digest(supplied, expected):
                raise PermissionError("unauthorized")

    return ThreadingHTTPServer((host, port), Handler)


def run_local_web(
    *, host: str, port: int, config: QuantForgeConfig, rd_config: ResearchLoopConfig | None = None
) -> None:
    server = create_local_web_server(host=host, port=port, config=config, rd_config=rd_config)
    actual_host, actual_port = server.server_address
    # flush=True: with stdout redirected to a file or pipe (docker logs,
    # shell redirection) the interpreter block-buffers, and serve_forever()
    # never returns, so an unflushed line would stay invisible while the
    # server is healthy.
    print(f"Quant Forge local web listening on http://{actual_host}:{actual_port}", flush=True)
    server.serve_forever()

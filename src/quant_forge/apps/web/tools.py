"""In-process typed tool adapter for the agent sidecar (agent_sidecar_frontend.md §5.7, D3-locked).

A server-side allowlisted tool registry over EXISTING workflows. Signatures are
MCP-schema-shaped so a later MCP export is a transport change, not a redesign;
there is NO MCP server in V1. The registry is the deterministic security
boundary between the LLM's prose and the kernel:

* **Closed catalog (FE-X1/FE-X3).** Exactly six read tools and five action
  tools. No shell/fs/provider transports; no ``promote``/``submit``; no auto
  multi-round RD. A name outside the catalog is an :class:`UnknownToolError` --
  a prompt-injected "call promote_factor" cannot resolve to anything.
* **Control-token on action tools EVEN on loopback (spec §5.7).** The web
  server skips the network bearer on a loopback bind
  (``apps/web/routing.py`` -> ``_control_token_for_bind`` returns ``""`` for
  any non-``0.0.0.0`` host, so ``_require_control_token`` early-returns). The
  agent surface must NOT inherit that: every action-class invocation requires
  a per-run capability token minted by :meth:`ToolRegistry.authorize`. The
  token is held by the deterministic adapter and is a SEPARATE argument to
  :meth:`invoke` -- it is never part of the tool catalog, tool arguments,
  narration, or the journal, so **the bearer never enters model context**.
* **Per-run authorization + budgets.** Tools are scoped to a pipeline via a
  :class:`ToolGrant`; each grant carries its own rate and concurrency budget.
* **Idea/factor text is untrusted DATA, never instructions.** Tool arguments
  are passed verbatim to the wrapped workflow and are never scanned for tool
  directives (RD prompts already consume catalog free text -- this registry is
  the choke point that keeps that text from escalating).

The sidecar journal (spec §11 ship gate #1) lives here too: every sidecar
action appends ``{tool, objective, input_refs, request_hash, artifact_refs,
nav_target, narration}`` under ``artifact_root/sidecar/`` so a replay
reproduces the same rendered cards. Numeric payloads never travel in the
``narration`` field (that invariant is enforced by
:mod:`quant_forge.specs.narration`); the journal stores narration as the plain
dicts that module produced.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

from quant_forge.specs.run_manifest import canonical_fingerprint

__all__ = [
    "READ_TOOL_NAMES",
    "ACTION_TOOL_NAMES",
    "TOOL_NAMES",
    "TOOL_KINDS",
    "TOOL_SPECS",
    "ToolError",
    "UnknownToolError",
    "ToolAuthorizationError",
    "ToolBudgetError",
    "ToolArgumentError",
    "ToolResult",
    "ToolGrant",
    "RateBudget",
    "ConcurrencyBudget",
    "ToolRegistry",
    "SidecarJournal",
    "assert_bearer_absent",
]

# ---------------------------------------------------------------------------
# Closed v1 catalog (spec §5.7). CLOSED: nothing here may be extended at
# runtime, and the two frozensets are the allowlist. Read tools are
# side-effect-free; action tools mutate research state and are token-gated.
# ---------------------------------------------------------------------------
READ_TOOL_NAMES: tuple[str, ...] = (
    "list_factors",
    "get_factor",
    "search_runs",
    "get_run",
    "get_data_summary",
    "search_docs",
)
ACTION_TOOL_NAMES: tuple[str, ...] = (
    "parse_idea",
    "validate_draft_formula",
    "create_pipeline",
    "confirm_pipeline",
    "cancel_pipeline",
)
TOOL_NAMES: tuple[str, ...] = READ_TOOL_NAMES + ACTION_TOOL_NAMES
TOOL_KINDS: dict[str, str] = {name: "read" for name in READ_TOOL_NAMES}
TOOL_KINDS.update({name: "action" for name in ACTION_TOOL_NAMES})


def _schema(properties: dict[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


_STRING = {"type": "string"}
_OPT_INT = {"type": "integer"}

# MCP-schema-shaped specs (name/description/inputSchema): a later MCP export is
# a transport change over exactly this catalog. Kept intentionally small.
TOOL_SPECS: dict[str, dict[str, Any]] = {
    "list_factors": {
        "name": "list_factors",
        "kind": "read",
        "description": "List registered factors (id, name, formula, status) from the local registry.",
        "inputSchema": _schema({"query": _STRING}),
    },
    "get_factor": {
        "name": "get_factor",
        "kind": "read",
        "description": "Get one factor's definition and recent evidence by factor_id.",
        "inputSchema": _schema({"factor_id": _STRING, "kind": _STRING}, required=("factor_id",)),
    },
    "search_runs": {
        "name": "search_runs",
        "kind": "read",
        "description": "List recent research-history runs (most recent first).",
        "inputSchema": _schema({"limit": _OPT_INT}),
    },
    "get_run": {
        "name": "get_run",
        "kind": "read",
        "description": "Get one run/job record by id (status, kind, artifact refs).",
        "inputSchema": _schema({"run_id": _STRING}, required=("run_id",)),
    },
    "get_data_summary": {
        "name": "get_data_summary",
        "kind": "read",
        "description": "Summarize the local panel data contract (fields, rows, date range).",
        "inputSchema": _schema({}),
    },
    "search_docs": {
        "name": "search_docs",
        "kind": "read",
        "description": "List/search local documentation entries.",
        "inputSchema": _schema({"query": _STRING}),
    },
    "parse_idea": {
        "name": "parse_idea",
        "kind": "action",
        "description": "Parse a natural-language factor idea into a draft factor (rule or llm).",
        "inputSchema": _schema(
            {"text": _STRING, "parser_mode": _STRING, "llm_provider": _STRING}, required=("text",)
        ),
    },
    "validate_draft_formula": {
        "name": "validate_draft_formula",
        "kind": "action",
        "description": (
            "Fail-closed spec validation of a draft formula (operators/fields/filters) "
            "WITHOUT persisting, evaluating, or backtesting."
        ),
        "inputSchema": _schema(
            {
                "factor_id": _STRING,
                "name": _STRING,
                "formula": _STRING,
                "universe_filters": {"type": "array", "items": _STRING},
                "horizon_days": _OPT_INT,
            },
            required=("formula",),
        ),
    },
    "create_pipeline": {
        "name": "create_pipeline",
        "kind": "action",
        "description": "Wrap a completed parse_idea job into a server-owned pipeline (awaiting_confirm).",
        "inputSchema": _schema({"parse_job_id": _STRING, "kind": _STRING}, required=("parse_job_id",)),
    },
    "confirm_pipeline": {
        "name": "confirm_pipeline",
        "kind": "action",
        "description": "Idempotently confirm a pipeline's assumptions and launch its compute stage.",
        "inputSchema": _schema(
            {"pipeline_id": _STRING, "nonce": _STRING, "version": _OPT_INT, "parameters": {"type": "object"}},
            required=("pipeline_id", "nonce", "version"),
        ),
    },
    "cancel_pipeline": {
        "name": "cancel_pipeline",
        "kind": "action",
        "description": "Cancel/abort a pipeline.",
        "inputSchema": _schema({"pipeline_id": _STRING}, required=("pipeline_id",)),
    },
}

assert set(TOOL_SPECS) == set(TOOL_NAMES), "TOOL_SPECS must cover exactly the closed catalog"


class ToolError(Exception):
    """Base class for tool-registry failures."""


class UnknownToolError(ToolError, KeyError):
    """A tool name outside the closed allowlist (spec §5.7).

    Subclasses ``KeyError`` so the web handlers map it to HTTP 404 like every
    other unknown-path error, and so a prompt-injected escalation ("call
    promote_factor") surfaces as "no such tool", never a silent no-op.
    """


class ToolAuthorizationError(ToolError, PermissionError):
    """An action-class tool was invoked without a valid per-run control token.

    Subclasses ``PermissionError`` so the web handlers map it to HTTP 401.
    Raised EVEN on a loopback bind -- the agent action surface never inherits
    the network-bearer skip (spec §5.7).
    """


class ToolBudgetError(ToolError, RuntimeError):
    """A per-run rate or concurrency budget was exceeded."""


class ToolArgumentError(ToolError, ValueError):
    """Tool arguments failed schema validation (missing required key / wrong type)."""


@dataclass(frozen=True)
class ToolResult:
    """A typed tool result: a payload plus artifact refs, never a numeric claim
    smuggled into prose. ``artifact_refs`` are what a narration ``ref`` node
    points at so a canonical renderer -- not the sidecar -- shows any number."""

    tool: str
    payload: dict[str, Any]
    artifact_refs: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {"tool": self.tool, "payload": self.payload, "artifact_refs": list(self.artifact_refs)}


# ---------------------------------------------------------------------------
# Budgets (per-grant). Small, directly-unit-testable classes.
# ---------------------------------------------------------------------------


class RateBudget:
    """Sliding-window call-rate budget: at most ``max_calls`` in ``window_seconds``."""

    def __init__(self, *, max_calls: int, window_seconds: float, clock: Callable[[], float] = time.monotonic) -> None:
        if max_calls < 1:
            raise ValueError("max_calls must be >= 1")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be > 0")
        self._max_calls = max_calls
        self._window = float(window_seconds)
        self._clock = clock
        self._events: deque[float] = deque()
        self._lock = threading.Lock()

    def reserve(self) -> None:
        now = self._clock()
        with self._lock:
            cutoff = now - self._window
            while self._events and self._events[0] <= cutoff:
                self._events.popleft()
            if len(self._events) >= self._max_calls:
                raise ToolBudgetError(
                    f"rate budget exceeded: at most {self._max_calls} tool calls per {self._window:g}s"
                )
            self._events.append(now)


class ConcurrencyBudget:
    """At most ``max_concurrency`` invocations in flight at once, per grant."""

    def __init__(self, *, max_concurrency: int) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be >= 1")
        self._max = max_concurrency
        self._active = 0
        self._lock = threading.Lock()

    @property
    def active(self) -> int:
        return self._active

    @contextmanager
    def slot(self) -> Iterator[None]:
        with self._lock:
            if self._active >= self._max:
                raise ToolBudgetError(f"concurrency budget exceeded: at most {self._max} concurrent tool calls")
            self._active += 1
        try:
            yield
        finally:
            with self._lock:
                self._active -= 1


@dataclass
class ToolGrant:
    """Per-run authorization (spec §5.7): tools scoped to one pipeline, with a
    capability token minted server-side and never revealed to the model.

    ``capability`` is the action-tool control token; it is compared with
    :func:`hmac.compare_digest` on every action-class invocation. It is created
    here, held by the deterministic adapter, and is never serialized into the
    catalog, arguments, narration, or journal.
    """

    pipeline_id: str
    capability: str
    rate: RateBudget
    concurrency: ConcurrencyBudget
    created_at: str = ""

    def public_view(self) -> dict[str, Any]:
        """The model-facing description of a grant: the pipeline scope only.
        The control token is deliberately ABSENT (bearer never in model
        context)."""

        return {"pipeline_id": self.pipeline_id, "created_at": self.created_at}


# ---------------------------------------------------------------------------
# Sidecar journal (spec §11 ship gate #1). Append-only JSONL under
# artifact_root/sidecar/, mirroring the pipeline journal's discipline.
# ---------------------------------------------------------------------------

_SIDECAR_DIR_NAME = "sidecar"
_PIPELINE_ID_RE = re.compile(r"^PL_[0-9a-f]{32}$")


def _utc_now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class SidecarJournal:
    """Durable, append-only record of sidecar actions under ``artifact_root/sidecar/``.

    Each row is one sidecar action: ``{ts, tool, objective, input_refs,
    request_hash, artifact_refs, nav_target, narration}``. ``narration`` holds
    the plain-dict NarrationNodes that were rendered for that action, so
    :func:`quant_forge.apps.web.narration.replay_rendered_cards` can reproduce
    the exact same cards from the journal alone (spec §11 replay gate). No
    control token is ever written (it is not part of any recorded field).
    """

    def __init__(self, artifact_root: Path) -> None:
        self._root = Path(artifact_root).expanduser() / _SIDECAR_DIR_NAME
        self._lock = threading.Lock()

    def _journal_path(self, pipeline_id: str) -> Path:
        if not _PIPELINE_ID_RE.match(pipeline_id):
            raise ToolArgumentError(f"invalid pipeline_id for sidecar journal: {pipeline_id!r}")
        return self._root / f"{pipeline_id}.journal.jsonl"

    def record(
        self,
        pipeline_id: str,
        *,
        tool: str,
        objective: str,
        input_refs: dict[str, Any] | None = None,
        request_hash: str,
        artifact_refs: tuple[dict[str, Any], ...] = (),
        nav_target: str | None = None,
        narration: tuple[dict[str, Any], ...] = (),
    ) -> dict[str, Any]:
        row = {
            "ts": _utc_now(),
            "pipeline_id": pipeline_id,
            "tool": tool,
            "objective": objective,
            "input_refs": dict(input_refs or {}),
            "request_hash": request_hash,
            "artifact_refs": [dict(ref) for ref in artifact_refs],
            "nav_target": nav_target,
            "narration": [dict(node) for node in narration],
        }
        path = self._journal_path(pipeline_id)
        with self._lock:
            self._root.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        return row

    def rows(self, pipeline_id: str) -> list[dict[str, Any]]:
        path = self._journal_path(pipeline_id)
        if not path.exists():
            return []
        rows: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped:
                rows.append(json.loads(stripped))
        return rows


def assert_bearer_absent(obj: Any, bearer: str) -> None:
    """Fail loud if a control token leaked into a model-facing structure.

    Belt-and-suspenders for "the bearer never enters model context" (spec
    §5.7): the ``bearer`` is a separate :meth:`ToolRegistry.invoke` argument and
    is never placed into the catalog / arguments / narration / journal, so this
    should always pass -- a test asserts it over each of those surfaces.
    """

    if bearer and bearer in json.dumps(obj, ensure_ascii=False, default=str):
        raise AssertionError("control token leaked into a model-facing structure")


def _validate_arguments(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Minimal fail-closed schema check: required keys present, declared types
    honored, no undeclared keys. Never inspects string CONTENT (idea/factor
    text is opaque data, never instructions)."""

    schema = TOOL_SPECS[name]["inputSchema"]
    properties: dict[str, Any] = schema["properties"]
    required: list[str] = schema.get("required", [])
    if not isinstance(arguments, dict):
        raise ToolArgumentError(f"{name} arguments must be an object")
    for key in required:
        if key not in arguments:
            raise ToolArgumentError(f"{name} is missing required argument: {key}")
    for key, value in arguments.items():
        if key not in properties:
            raise ToolArgumentError(f"{name} got an unexpected argument: {key}")
        expected = properties[key]["type"]
        if not _type_ok(expected, value):
            raise ToolArgumentError(f"{name} argument {key} must be of type {expected}")
    return dict(arguments)


def _type_ok(expected: str, value: Any) -> bool:
    if value is None:
        return True
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    return True  # pragma: no cover - no other declared types in the catalog


class ToolRegistry:
    """The deterministic tool adapter. Holds the server context needed to
    dispatch, mints per-run grants, enforces the closed allowlist + action
    token + budgets, and journals every action.

    Dispatch goes through the ``quant_forge.apps.web.server`` seam (like every
    other web workflow call) so monkeypatches keep taking effect.
    """

    def __init__(
        self,
        *,
        config: Any,
        store: Any,
        job_manager: Any,
        rd_config: Any,
        rate_max_calls: int = 30,
        rate_window_seconds: float = 10.0,
        max_concurrency: int = 4,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._store = store
        self._job_manager = job_manager
        self._rd_config = rd_config
        self._rate_max_calls = rate_max_calls
        self._rate_window_seconds = rate_window_seconds
        self._max_concurrency = max_concurrency
        self._clock = clock
        self._grants: dict[str, ToolGrant] = {}
        self._lock = threading.Lock()

    def apply_runtime_config(self, config: Any) -> None:
        """Swap the registry's config snapshot after a runtime settings update.

        ``POST /api/settings/llm`` atomically replaces the routing closure's
        frozen config; the registry captured its own reference at construction,
        so the route hands the replacement here too — otherwise sidecar tools
        would keep parsing with the pre-switch LLM provider.
        """

        self._config = config

    # -- authorization -----------------------------------------------------

    def authorize(self, pipeline_id: str) -> ToolGrant:
        """Mint -- or REUSE -- the per-run grant for a pipeline (P2-F1).

        Idempotent: a grant is scoped to a pipeline for the life of its sidecar
        session, so re-authorizing an already-authorized pipeline returns the
        SAME grant -- same capability, same rate/concurrency budget counters.
        Minting a fresh grant on every call would let a client reset its budget
        by re-authorizing before each read burst, defeating the rate/concurrency
        cap the §11 gate #4 security plane promises. The budgets therefore
        outlive any single authorize call and accumulate across the session.
        """

        with self._lock:
            existing = self._grants.get(pipeline_id)
            if existing is not None:
                return existing
            grant = ToolGrant(
                pipeline_id=pipeline_id,
                capability=secrets.token_urlsafe(32),
                rate=RateBudget(
                    max_calls=self._rate_max_calls, window_seconds=self._rate_window_seconds, clock=self._clock
                ),
                concurrency=ConcurrencyBudget(max_concurrency=self._max_concurrency),
                created_at=_utc_now(),
            )
            self._grants[pipeline_id] = grant
            return grant

    def grant_for(self, pipeline_id: str) -> ToolGrant | None:
        with self._lock:
            return self._grants.get(pipeline_id)

    def catalog(self) -> list[dict[str, Any]]:
        """Model-facing tool catalog (MCP-shaped). Never carries any token."""

        return [dict(TOOL_SPECS[name]) for name in TOOL_NAMES]

    # -- invocation --------------------------------------------------------

    def invoke(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        grant: ToolGrant,
        capability: str | None = None,
    ) -> ToolResult:
        """Authorize + budget + dispatch one tool call.

        ``capability`` is the caller-presented action-tool control token.
        Order (fail-closed): allowlist -> schema -> action-token -> rate ->
        concurrency -> dispatch. The action-token check happens BEFORE any
        budget is consumed and independently of any network-bearer state, so a
        loopback bind never weakens it.
        """

        if name not in TOOL_SPECS:
            raise UnknownToolError(f"unknown tool (not in closed allowlist): {name!r}")
        args = _validate_arguments(name, dict(arguments or {}))
        kind = TOOL_KINDS[name]
        # Pipeline-scope binding (fail-closed): a ToolGrant is scoped to ONE
        # pipeline (§5.7), so any tool argument naming a ``pipeline_id`` MUST
        # equal the grant's own pipeline. Without this a capability minted for
        # pipeline A could confirm/cancel pipeline B (a cross-pipeline confused
        # deputy — the clarify/blocking check runs against A while the action
        # lands on B). Checked BEFORE the action-token gate and before any
        # budget is consumed.
        arg_pipeline_id = args.get("pipeline_id")
        if arg_pipeline_id is not None and str(arg_pipeline_id) != grant.pipeline_id:
            raise ToolAuthorizationError(
                f"tool {name!r} argument pipeline_id does not match this grant's scope"
            )
        if kind == "action":
            expected = grant.capability
            if not capability or not expected or not hmac.compare_digest(str(capability), expected):
                raise ToolAuthorizationError(
                    f"action tool {name!r} requires the per-run control token (required even on loopback)"
                )
        grant.rate.reserve()
        with grant.concurrency.slot():
            payload, artifact_refs = self._dispatch(name, args, grant)
        return ToolResult(tool=name, payload=payload, artifact_refs=tuple(artifact_refs))

    def request_hash(self, name: str, arguments: dict[str, Any]) -> str:
        """Canonical fingerprint of a tool request (tool + arguments), for the
        journal. The control token is not part of ``arguments``, so it is never
        hashed into the record."""

        return canonical_fingerprint({"tool": name, "arguments": arguments})

    # -- dispatch (wraps EXISTING workflows via the server seam) -----------

    def _dispatch(
        self, name: str, args: dict[str, Any], grant: ToolGrant
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        # Read payloads + parse workflow go through the server monkeypatch seam
        # (like every other web call); the pipeline aggregate functions are
        # imported directly (they are not part of the workflow seam and server
        # does not re-export them).
        from quant_forge.apps.web import server as _server
        from quant_forge.apps.web.pipeline import (
            cancel_pipeline as _cancel_pipeline,
            confirm_pipeline as _confirm_pipeline,
            create_pipeline as _create_pipeline,
        )

        config = self._config
        if name == "list_factors":
            payload = _server._registry_factors_payload(config)
            return payload, [{"kind": "registry_factors"}]
        if name == "get_factor":
            payload = _server._registry_factor_detail_payload(
                config, str(args["factor_id"]), limit=None, kind=args.get("kind")
            )
            return payload, [{"kind": "factor", "factor_id": str(args["factor_id"])}]
        if name == "search_runs":
            payload = _server._research_history_payload(config, limit=args.get("limit"))
            return payload, [{"kind": "research_history"}]
        if name == "get_run":
            run_id = str(args["run_id"])
            try:
                payload = self._job_manager.get(run_id)
            except KeyError:
                return {"run_id": run_id, "status": "not_found"}, []
            return payload, [{"kind": "run", "job_id": run_id}]
        if name == "get_data_summary":
            payload = _server._data_status_payload(config)
            return payload, [{"kind": "data_summary"}]
        if name == "search_docs":
            payload = _server._docs_list_payload(config)
            query = str(args.get("query") or "").strip().lower()
            if query:
                docs = [doc for doc in payload.get("docs", []) if query in json.dumps(doc, ensure_ascii=False).lower()]
                payload = {**payload, "docs": docs, "query": query}
            return payload, [{"kind": "docs"}]
        if name == "parse_idea":
            # The idea text is untrusted DATA (prompt-injection posture): it is
            # handed verbatim to the parser and never inspected for directives.
            payload = _server.run_idea_parse_workflow(
                config,
                str(args["text"]),
                parser_mode=str(args.get("parser_mode") or "rule"),
                llm_provider=args.get("llm_provider"),
                rd_config=self._rd_config,
            )
            return payload, [{"kind": "parse"}]
        if name == "validate_draft_formula":
            return self._validate_draft_formula(args)
        if name == "create_pipeline":
            record = _create_pipeline(
                self._store,
                job_manager=self._job_manager,
                parse_job_id=str(args["parse_job_id"]),
                rd_config=self._rd_config,
                kind=str(args.get("kind") or "factor_study"),
            )
            return record.to_dict(), [{"kind": "pipeline", "pipeline_id": record.pipeline_id}]
        if name == "confirm_pipeline":
            record = _confirm_pipeline(
                config,
                self._store,
                str(args["pipeline_id"]),
                nonce=str(args["nonce"]),
                version=int(args["version"]),
                job_manager=self._job_manager,
                rd_config=self._rd_config,
                parameters=args.get("parameters"),
            )
            return record.to_dict(), [{"kind": "pipeline", "pipeline_id": record.pipeline_id}]
        if name == "cancel_pipeline":
            record = _cancel_pipeline(
                self._store, str(args["pipeline_id"]), job_manager=self._job_manager, config=config
            )
            return record.to_dict(), [{"kind": "pipeline", "pipeline_id": record.pipeline_id}]
        raise UnknownToolError(f"no dispatch for tool: {name!r}")  # pragma: no cover - guarded by allowlist above

    def _validate_draft_formula(self, args: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Fail-closed spec validation with NO persist/eval/backtest (spec §5.3
        P2 subset): canonicalize into a FactorSpec and run the read-only
        ValidationGate. The full P3 pre-validation endpoint additionally returns
        a canonical fingerprint or an unknown-operator draft-review packet ref;
        that extension lands in P3 (§12 open question / WORKORDER P3)."""

        from quant_forge.specs.factor_spec import FactorSpec
        from quant_forge.specs.validation_gate import validate_factor_spec

        try:
            spec = FactorSpec(
                factor_id=str(args.get("factor_id") or "FTR_DRAFT"),
                name=str(args.get("name") or "draft"),
                formula_dsl=str(args["formula"]),
                horizon_days=int(args.get("horizon_days") or 5),
                universe_filters=tuple(str(item) for item in (args.get("universe_filters") or ())),
            )
        except ValueError as exc:
            return {"status": "blocked", "blocking_reasons": [str(exc)], "unresolved_operators": [],
                    "unresolved_fields": []}, []
        result = validate_factor_spec(spec)
        payload = {
            "status": result.status,
            "unresolved_operators": list(result.unresolved_operators),
            "unresolved_fields": list(result.unresolved_fields),
            "blocking_reasons": list(result.blocking_reasons),
            "warnings": list(result.warnings),
            "unchecked": list(result.unchecked),
        }
        return payload, [{"kind": "validation_gate", "factor_id": spec.factor_id}]

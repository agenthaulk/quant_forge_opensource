"""HTTP routing and server composition for the local web adapter.

Workflow callables and ``DEFAULT_RD_CONFIG_PATH`` are resolved through
:mod:`quant_forge.apps.web.server` at request time so monkeypatches on the
server module namespace keep taking effect.
"""

from __future__ import annotations

import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from typing import Any
from urllib.parse import urlparse

from quant_forge.apps.web.api import (
    _active_llm,
    _control_token_for_bind,
    _factor_from_validation_payload,
    _int_parameter,
    _job_id_from_cancel_path,
    _job_id_from_path,
    _json_safe,
    _llm_provider_options,
    _optional_int,
    _optional_parameters_payload,
    _optional_parser_payload,
    _optional_str,
    _paths_payload,
    _rd_status_payload,
)
from quant_forge.apps.web.html import _index_html
from quant_forge.apps.web.jobs import LOGGER, RequestBodyTooLarge, _WebJobManager, _client_error_message
from quant_forge.config import QuantForgeConfig
from quant_forge.mcp.read_models import list_available_fields, list_available_operators
from quant_forge.research_loop.config import ResearchLoopConfig, load_research_loop_config
from quant_forge.research_loop.scheduler import (
    ResearchLoopScheduler,
    ResearchScheduleRequest,
)


MAX_REQUEST_BODY_BYTES = 1024 * 1024


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
    control_token = _control_token_for_bind(host, config)
    control_token_required = bool(control_token)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = urlparse(self.path).path
            try:
                if path == "/health":
                    self._json({"ok": True})
                elif path == "/catalog":
                    self._require_control_token()
                    self._json({"fields": list_available_fields(), "operators": list_available_operators()})
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
                else:
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
                    self._json(
                        job_manager.start(
                            "parse_idea",
                            lambda cancel_event: _server.run_idea_parse_workflow(
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
                if path.startswith("/api/jobs/") and path.endswith("/cancel"):
                    self._json(job_manager.cancel(_job_id_from_cancel_path(path)))
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
                self._json({"error": f"unknown endpoint: {path}"}, status=404)
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
    print(f"Quant Forge local web listening on http://{actual_host}:{actual_port}")
    server.serve_forever()

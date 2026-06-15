"""Minimal local-only web/API adapter."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import UTC, datetime
from html import escape
import json
import logging
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
import time
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from quant_forge.backtesting.service import run_factor_backtest
from quant_forge.config import QuantForgeConfig, validate_llm_runtime
from quant_forge.core.contracts import BacktestResult, EvaluationResult, FactorDefinition
from quant_forge.evaluation.service import evaluate_factor
from quant_forge.factor_library.catalog import FactorCatalog
from quant_forge.factor_library.repository import FactorRepository
from quant_forge.llm_factor_parser import ParsedFactor, parse_factor_idea
from quant_forge.mcp.read_models import list_available_fields, list_available_operators
from quant_forge.research_loop.scheduler import (
    ResearchLoopScheduler,
    ResearchScheduleRequest,
)
from quant_forge.research_loop.config import (
    DEFAULT_RD_CONFIG_PATH,
    ResearchLoopConfig,
    load_research_loop_config,
    weights_for_objective,
)
from quant_forge.research_loop.llm import LLMHypothesisGenerator, LLMResearchReviewGenerator
from quant_forge.research_loop.service import ResearchLoopResult, ResearchLoopService


LOGGER = logging.getLogger(__name__)
LONG_RUNNING_JOB_SECONDS = 10.0
_TERMINAL_JOB_STATUSES = {"completed", "failed", "cancelled"}
_WEB_PATH_KEYS = {
    "artifact_path",
    "factor_values_path",
    "factor_values_write_path",
    "trace_root",
    "report_path",
}


class _WebJobCancelled(RuntimeError):
    pass


@dataclass
class _WebJob:
    job_id: str
    kind: str
    status: str
    started_at: str
    updated_at: str
    finished_at: str | None = None
    result: dict[str, Any] | None = None
    error: str = ""
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    thread: threading.Thread | None = field(default=None, repr=False)
    started_monotonic: float = field(default_factory=time.monotonic, repr=False)
    finished_monotonic: float | None = None


class _WebJobManager:
    def __init__(self, *, slow_after_seconds: float = LONG_RUNNING_JOB_SECONDS, max_retained_jobs: int = 50) -> None:
        self._slow_after_seconds = slow_after_seconds
        self._max_retained_jobs = max(1, max_retained_jobs)
        self._lock = threading.Lock()
        self._jobs: dict[str, _WebJob] = {}

    def start(self, kind: str, runner: Any) -> dict[str, Any]:
        job_id = uuid4().hex[:12]
        now = _utc_now()
        job = _WebJob(job_id=job_id, kind=kind, status="running", started_at=now, updated_at=now)

        def run() -> None:
            try:
                result = runner(job.cancel_event)
            except _WebJobCancelled as exc:
                self._finish(job_id, status="cancelled", error=str(exc))
            except Exception as exc:
                if job.cancel_event.is_set():
                    self._finish(job_id, status="cancelled", error=_client_error_message(exc, fallback="run cancelled by user"))
                else:
                    LOGGER.exception("web job %s failed", job_id)
                    self._finish(job_id, status="failed", error=_client_error_message(exc, fallback="job failed"))
            else:
                if job.cancel_event.is_set():
                    self._finish(job_id, status="cancelled", error="run cancelled by user")
                else:
                    try:
                        public_result = _web_public_json(result)
                    except Exception as exc:
                        LOGGER.exception("web job %s result serialization failed", job_id)
                        self._finish(
                            job_id,
                            status="failed",
                            error=_client_error_message(exc, fallback="job result serialization failed"),
                        )
                    else:
                        self._finish(job_id, status="completed", result=public_result)

        thread = threading.Thread(target=run, name=f"qf-web-job-{job_id}", daemon=True)
        job.thread = thread
        with self._lock:
            if any(item.kind == kind and item.status not in _TERMINAL_JOB_STATUSES for item in self._jobs.values()):
                raise ValueError(f"{kind} job already running")
            self._jobs[job_id] = job
            self._prune_locked()
        thread.start()
        return self.get(job_id)

    def get(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._require(job_id)
            return self._payload(job)

    def cancel(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._require(job_id)
            if job.status not in _TERMINAL_JOB_STATUSES:
                job.cancel_event.set()
                job.status = "cancel_requested"
                job.error = "cancel requested by user"
                job.updated_at = _utc_now()
            return self._payload(job)

    def _finish(
        self,
        job_id: str,
        *,
        status: str,
        result: dict[str, Any] | None = None,
        error: str = "",
    ) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            if job.status in _TERMINAL_JOB_STATUSES:
                return
            job.status = status
            job.result = result
            job.error = error
            job.finished_at = _utc_now()
            job.finished_monotonic = time.monotonic()
            job.updated_at = job.finished_at
            self._prune_locked()

    def _require(self, job_id: str) -> _WebJob:
        job = self._jobs.get(job_id)
        if job is None:
            raise KeyError(f"unknown job: {job_id}")
        return job

    def _payload(self, job: _WebJob) -> dict[str, Any]:
        end_time = job.finished_monotonic if job.finished_monotonic is not None else time.monotonic()
        runtime_seconds = max(0.0, end_time - job.started_monotonic)
        running = job.status not in _TERMINAL_JOB_STATUSES
        slow = running and runtime_seconds >= self._slow_after_seconds
        message = "system is still running" if slow else ""
        return {
            "job_id": job.job_id,
            "kind": job.kind,
            "status": job.status,
            "started_at": job.started_at,
            "updated_at": job.updated_at,
            "finished_at": job.finished_at,
            "runtime_seconds": runtime_seconds,
            "slow": slow,
            "slow_after_seconds": self._slow_after_seconds,
            "message": message,
            "error": job.error,
            "result": job.result,
        }

    def _prune_locked(self) -> None:
        terminal = [
            job
            for job in self._jobs.values()
            if job.status in _TERMINAL_JOB_STATUSES
        ]
        overflow = len(self._jobs) - self._max_retained_jobs
        if overflow <= 0:
            return
        for job in sorted(terminal, key=lambda item: item.started_monotonic)[:overflow]:
            self._jobs.pop(job.job_id, None)


def run_idea_workflow(
    config: QuantForgeConfig,
    text: str,
    *,
    parser_mode: str = "llm",
    llm_provider: str | None = None,
    rd_config: ResearchLoopConfig | None = None,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    """Parse an idea, persist the draft, evaluate it, and run a backtest."""

    if not text.strip():
        raise ValueError("idea text is required")
    _raise_if_cancelled(cancel_event)
    research_config = rd_config or load_research_loop_config(DEFAULT_RD_CONFIG_PATH, config.research, config.simulation)
    llm_settings = config.llm.select_provider(llm_provider) if parser_mode == "llm" else config.llm
    if parser_mode == "llm":
        validate_llm_runtime(llm_settings)
    _raise_if_cancelled(cancel_event)
    parsed = parse_factor_idea(text, llm_settings, mode=parser_mode)
    _raise_if_cancelled(cancel_event)
    repo = FactorRepository(config.paths.factor_root)
    previous_factor = _existing_factor(repo, parsed.factor.factor_id)
    try:
        repo.save(parsed.factor)
        _raise_if_cancelled(cancel_event)
        evaluation = evaluate_factor(
            parsed.factor.factor_id,
            factor_root=config.paths.factor_root,
            data_root=config.paths.data_root,
            artifact_root=config.paths.artifact_root,
            horizon_days=parsed.factor.horizon_days,
            horizon_days_matrix=research_config.horizon_days_matrix,
            sample_splits=research_config.sample_splits,
            simulation_profile=research_config.simulation_profile,
            factor_values_root=config.paths.factor_values_root,
            factor_values_overlay_root=config.paths.factor_values_overlay_root,
            factor_values_manifest_root=config.paths.factor_values_manifest_root,
        )
        _raise_if_cancelled(cancel_event)
        backtest = run_factor_backtest(
            parsed.factor.factor_id,
            factor_root=config.paths.factor_root,
            data_root=config.paths.data_root,
            artifact_root=config.paths.artifact_root,
            simulation_profile=research_config.simulation_profile,
            holding_days=parsed.factor.horizon_days,
            transaction_costs=research_config.transaction_costs,
            sample_splits=research_config.sample_splits,
            factor_values_root=config.paths.factor_values_root,
            factor_values_overlay_root=config.paths.factor_values_overlay_root,
            factor_values_manifest_root=config.paths.factor_values_manifest_root,
        )
        _raise_if_cancelled(cancel_event)
    except _WebJobCancelled:
        if previous_factor is None:
            repo.delete(parsed.factor.factor_id)
        else:
            repo.save(previous_factor)
        raise
    return _workflow_payload(parsed, evaluation, backtest)


def run_research_once_workflow(
    config: QuantForgeConfig,
    seed_factor_id: str,
    *,
    objective: str | None = None,
    max_candidates: int | None = None,
    rd_config: ResearchLoopConfig | None = None,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    """Run one local research-development iteration and return JSON-safe data."""

    _raise_if_cancelled(cancel_event)
    research_config = rd_config or load_research_loop_config(DEFAULT_RD_CONFIG_PATH, config.research, config.simulation)
    result = _run_research_once(
        config,
        research_config,
        seed_factor_id,
        objective=objective or research_config.objective,
        max_candidates=max_candidates if max_candidates is not None else research_config.default_max_candidates,
        cancel_event=cancel_event,
    )
    _raise_if_cancelled(cancel_event)
    return _json_safe(result)


def create_local_web_server(
    *, host: str, port: int, config: QuantForgeConfig, rd_config: ResearchLoopConfig | None = None
) -> ThreadingHTTPServer:
    allowed_hosts = {"127.0.0.1", "localhost"}
    if config.web.allow_docker_bind:
        allowed_hosts.add("0.0.0.0")
    if host not in allowed_hosts:
        raise ValueError(
            "OpenSource web adapter is local-only; use 127.0.0.1 or localhost. "
            "Set web.allow_docker_bind only for Docker containers published to host loopback."
        )

    research_config = rd_config or load_research_loop_config(DEFAULT_RD_CONFIG_PATH, config.research, config.simulation)
    scheduler = ResearchLoopScheduler(
        lambda seed_factor_id, objective, max_candidates: _run_research_once(
            config,
            research_config,
            seed_factor_id,
            objective=objective,
            max_candidates=max_candidates,
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
                    result = run_idea_workflow(
                        config,
                        str(payload.get("text", "")),
                        parser_mode=str(payload.get("parser_mode", "llm")),
                        llm_provider=_optional_str(payload.get("llm_provider")),
                        rd_config=research_config,
                    )
                    self._json(result)
                    return
                if path == "/api/research/run-once":
                    result = run_research_once_workflow(
                        config,
                        str(payload.get("seed_factor_id", "")),
                        objective=str(payload.get("objective", research_config.objective)),
                        max_candidates=_optional_int(payload.get("max_candidates")),
                        rd_config=research_config,
                    )
                    self._json(result)
                    return
                if path == "/api/jobs/run-idea":
                    self._json(
                        job_manager.start(
                            "run_idea",
                            lambda cancel_event: run_idea_workflow(
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
                if path == "/api/jobs/research-run-once":
                    self._json(
                        job_manager.start(
                            "research_run_once",
                            lambda cancel_event: run_research_once_workflow(
                                config,
                                str(payload.get("seed_factor_id", "")),
                                objective=str(payload.get("objective", research_config.objective)),
                                max_candidates=_optional_int(payload.get("max_candidates")),
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
                            interval_days=int(payload.get("interval_days", research_config.default_interval_days)),
                            max_candidates=int(payload.get("max_candidates", research_config.default_max_candidates)),
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
            if supplied != f"Bearer {control_token}":
                raise PermissionError("unauthorized")

    return ThreadingHTTPServer((host, port), Handler)


def run_local_web(
    *, host: str, port: int, config: QuantForgeConfig, rd_config: ResearchLoopConfig | None = None
) -> None:
    server = create_local_web_server(host=host, port=port, config=config, rd_config=rd_config)
    actual_host, actual_port = server.server_address
    print(f"Quant Forge local web listening on http://{actual_host}:{actual_port}")
    server.serve_forever()


def _workflow_payload(parsed: ParsedFactor, evaluation: EvaluationResult, backtest: BacktestResult) -> dict[str, Any]:
    return {
        "parser": {
            "source": parsed.source,
            "provider": parsed.provider,
            "model": parsed.model,
        },
        "factor": _json_safe(parsed.factor),
        "evaluation": _json_safe(evaluation),
        "backtest": _json_safe(backtest),
    }


def _run_research_once(
    config: QuantForgeConfig,
    rd_config: ResearchLoopConfig,
    seed_factor_id: str,
    *,
    objective: str,
    max_candidates: int,
    cancel_event: threading.Event | None = None,
) -> ResearchLoopResult:
    if not seed_factor_id.strip():
        raise ValueError("seed_factor_id is required")
    _raise_if_cancelled(cancel_event)
    hypothesis_generator = None
    review_generator = None
    if _rd_generation_mode(rd_config.llm.hypothesis_mode) == "llm":
        hypothesis_generator = LLMHypothesisGenerator(_rd_llm_settings(config, feature="RD hypothesis generation"))
    if _rd_generation_mode(rd_config.llm.review_mode) == "llm":
        review_generator = LLMResearchReviewGenerator(_rd_llm_settings(config, feature="RD self-review"))
    service = ResearchLoopService(
        factor_root=config.paths.factor_root,
        data_root=config.paths.data_root,
        artifact_root=config.paths.artifact_root,
        factor_values_root=config.paths.factor_values_root,
        factor_values_overlay_root=config.paths.factor_values_overlay_root,
        factor_values_manifest_root=config.paths.factor_values_manifest_root,
        simulation_profile=rd_config.simulation_profile,
        simulation_profiles=rd_config.simulation_profiles,
        parameter_search_enabled=rd_config.parameter_search.enabled,
        parameter_search_method=rd_config.parameter_search.method,
        parameter_search_keep_ratio=rd_config.parameter_search.keep_ratio,
        parameter_search_min_survivors=rd_config.parameter_search.min_survivors,
        quick_horizon_days_matrix=rd_config.parameter_search.quick_horizon_days_matrix,
        quick_sample_splits=rd_config.parameter_search.quick_sample_splits,
        horizon_days_matrix=rd_config.horizon_days_matrix,
        sample_splits=rd_config.sample_splits,
        transaction_costs=rd_config.transaction_costs,
        deduplication=rd_config.deduplication,
        llm_formula_repair_attempts=rd_config.llm.max_formula_repair_attempts,
        hypothesis_generator=hypothesis_generator,
        review_generator=review_generator,
        cancel_event=cancel_event,
    )
    weights = weights_for_objective(rd_config, objective)
    _raise_if_cancelled(cancel_event)
    return service.run_once(
        seed_factor_id,
        objective=objective,
        max_candidates=max_candidates,
        weights=weights,
        gate=rd_config.gate,
    )


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _raise_if_cancelled(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise _WebJobCancelled("run cancelled by user")


def _existing_factor(repo: FactorRepository, factor_id: str) -> FactorDefinition | None:
    try:
        return repo.get(factor_id)
    except FileNotFoundError:
        return None


def _job_id_from_path(path: str) -> str:
    parts = path.strip("/").split("/")
    if len(parts) != 3 or parts[:2] != ["api", "jobs"] or not parts[2]:
        raise KeyError(f"unknown job path: {path}")
    return parts[2]


def _job_id_from_cancel_path(path: str) -> str:
    parts = path.strip("/").split("/")
    if len(parts) != 4 or parts[:2] != ["api", "jobs"] or parts[3] != "cancel" or not parts[2]:
        raise KeyError(f"unknown job path: {path}")
    return parts[2]


def _rd_llm_settings(config: QuantForgeConfig, *, feature: str) -> Any:
    selected = config.llm.select_provider()
    if selected.provider.lower() in {"rule", "deterministic"}:
        raise RuntimeError(f"{feature} requires a configured LLM provider; selected provider is local rule.")
    validate_llm_runtime(selected)
    return selected


def _rd_generation_mode(mode: str) -> str:
    normalized = mode.strip().lower()
    if normalized in {"deterministic", "rule", "local_rule"}:
        return "local"
    return normalized


def _control_token_for_bind(host: str, config: QuantForgeConfig) -> str:
    if host != "0.0.0.0":
        return ""
    token_env = config.web.control_token_env.strip()
    if not token_env:
        raise ValueError("web.control_token_env is required when binding the web adapter to 0.0.0.0")
    control_value = os.environ.get(token_env, "")
    if not control_value:
        raise ValueError(f"web control token environment variable is not set: {token_env}")
    return control_value


def _client_error_message(exc: Exception, *, fallback: str) -> str:
    if isinstance(exc, _WebJobCancelled):
        return str(exc) or "run cancelled by user"
    if isinstance(exc, PermissionError):
        return "unauthorized"
    if isinstance(exc, ValueError):
        return str(exc)
    text = str(exc)
    if "Missing API key" in text or "requires api_key_env" in text:
        return "LLM runtime is not ready"
    return fallback


def _web_public_json(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _web_public_json(asdict(value))
    if isinstance(value, Path):
        return _path_label(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, tuple):
        return [_web_public_json(item) for item in value]
    if isinstance(value, set):
        return [_web_public_json(item) for item in sorted(value, key=str)]
    if isinstance(value, list):
        return [_web_public_json(item) for item in value]
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            if name == "raw_response":
                continue
            if name in _WEB_PATH_KEYS and item:
                result[name] = _path_label(Path(item)) if isinstance(item, str | os.PathLike) else _web_public_json(item)
            else:
                result[name] = _web_public_json(item)
        return result
    if value is None or isinstance(value, bool | int | float | str):
        return value
    return str(value)


def _path_label(path: Path) -> str:
    return path.name or "path"


def _json_safe(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _json_safe(asdict(value))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, set):
        return [_json_safe(item) for item in sorted(value, key=str)]
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items() if str(key) != "raw_response"}
    if value is None or isinstance(value, bool | int | float | str):
        return value
    return str(value)


def _optional_int(value: Any) -> int | None:
    if value in {None, ""}:
        return None
    return int(value)


def _optional_str(value: Any) -> str | None:
    if value in {None, ""}:
        return None
    return str(value)


def _paths_payload(config: QuantForgeConfig) -> dict[str, str]:
    return {
        "data_root": str(config.paths.data_root),
        "factor_root": str(config.paths.factor_root),
        "factor_values_root": str(config.paths.factor_values_root or ""),
        "factor_values_overlay_root": str(config.paths.factor_values_overlay_root or ""),
        "factor_values_manifest_root": str(config.paths.factor_values_manifest_root or ""),
        "artifact_root": str(config.paths.artifact_root),
    }


def _llm_provider_options(config: QuantForgeConfig) -> tuple[dict[str, str], ...]:
    options: list[dict[str, str]] = []
    for option in config.llm.public_provider_options():
        runtime_ready, runtime_error = _llm_runtime_status(config, option["provider"])
        enriched = dict(option)
        enriched["runtime_ready"] = "true" if runtime_ready else "false"
        enriched["runtime_error"] = runtime_error
        options.append(enriched)
    return tuple(options)


def _llm_runtime_status(config: QuantForgeConfig, provider: str) -> tuple[bool, str]:
    try:
        validate_llm_runtime(config.llm, provider)
    except RuntimeError as exc:
        return False, str(exc)
    return True, ""


def _active_llm(config: QuantForgeConfig) -> Any:
    return config.llm.select_provider()


def _rd_status_payload(config: QuantForgeConfig, rd_config: ResearchLoopConfig) -> dict[str, str]:
    active_llm = _active_llm(config)
    return {
        "research_stage": "research",
        "hypothesis_mode": _rd_generation_mode(rd_config.llm.hypothesis_mode),
        "review_mode": _rd_generation_mode(rd_config.llm.review_mode),
        "provider": active_llm.provider,
        "model": active_llm.model,
    }


def _rd_optimizer_label(config: QuantForgeConfig, rd_config: ResearchLoopConfig) -> str:
    active_llm = _active_llm(config)
    modes = (
        _rd_generation_mode(rd_config.llm.hypothesis_mode),
        _rd_generation_mode(rd_config.llm.review_mode),
    )
    if "llm" not in modes:
        return "research local deterministic"
    if active_llm.provider in {"rule", "deterministic"}:
        return "research LLM required / provider not configured"
    return f"research LLM / {active_llm.provider} / {active_llm.model}"


def _default_seed_factor_id(config: QuantForgeConfig) -> str:
    factor_ids = _catalog_factor_ids(config)
    if "FTR_DEMO_SMALL_CAP" in factor_ids:
        return "FTR_DEMO_SMALL_CAP"
    return factor_ids[0] if factor_ids else ""


def _simulation_profile_period_text(profile: Any) -> str:
    start = getattr(profile, "test_period_start", None) or "full available data"
    end = getattr(profile, "test_period_end", None) or "latest available data"
    return f"{start} -> {end}"


def _catalog_factor_ids(config: QuantForgeConfig) -> list[str]:
    try:
        factors = FactorCatalog(
            config.paths.factor_root,
            factor_values_root=config.paths.factor_values_root,
            factor_values_manifest_root=config.paths.factor_values_manifest_root,
        ).list()
    except Exception:
        return []
    return [factor.factor_id for factor in factors]


def _selected_attr(selected: bool) -> str:
    return " selected" if selected else ""


def _provider_readiness_label(option: dict[str, str]) -> str:
    if option.get("runtime_ready") == "true":
        return " · env " + option["api_key_env"] if option["api_key_env"] else " · no auth"
    api_key_env = option.get("api_key_env", "")
    if api_key_env:
        return " · missing env " + api_key_env
    return " · not ready"


def _index_html(
    config: QuantForgeConfig,
    rd_config: ResearchLoopConfig | None = None,
    *,
    control_token_required: bool = False,
    redact_runtime: bool = False,
) -> str:
    research_config = rd_config or load_research_loop_config(DEFAULT_RD_CONFIG_PATH, config.research, config.simulation)
    paths = _paths_payload(config)
    provider_options = _llm_provider_options(config)
    active_llm = _active_llm(config)
    active_provider = active_llm.provider if active_llm.provider not in {"rule", "deterministic"} else ""
    provider = escape(active_llm.provider)
    model = escape(active_llm.model)
    parser_label = escape(active_provider or "未配置 LLM provider")
    rd_optimizer_label = escape(_rd_optimizer_label(config, research_config))
    seed_factor_id = escape(_default_seed_factor_id(config))
    if redact_runtime:
        paths = {
            "data_root": "protected",
            "factor_root": "protected",
            "factor_values_root": "protected",
            "factor_values_overlay_root": "protected",
            "factor_values_manifest_root": "protected",
            "artifact_root": "protected",
        }
        provider_options = ()
        active_provider = ""
        provider = "protected"
        model = "protected"
        parser_label = "需要控制令牌"
        rd_optimizer_label = "需要控制令牌"
        seed_factor_id = ""
    data_root = escape(paths["data_root"])
    factor_root = escape(paths["factor_root"])
    factor_values_root = escape(paths["factor_values_root"])
    factor_values_overlay_root = escape(paths["factor_values_overlay_root"])
    artifact_root = escape(paths["artifact_root"])
    interval_options = "\n".join(
        f'      <option value="{day}"{_selected_attr(day == research_config.default_interval_days)}>{day}天</option>'
        for day in research_config.allowed_interval_days
    )
    objective_options = "\n".join(
        f'      <option value="{value}"{_selected_attr(value == research_config.objective)}>{label}</option>'
        for value, label in (
            ("balanced", "IC / ICIR 优先"),
            ("rank_ic", "Rank IC"),
            ("rank_icir", "ICIR"),
            ("annualized_return", "回测收益"),
        )
    )
    llm_provider_options = "\n".join(
        (
            f'      <option value="{escape(option["provider"])}"'
            f'{_selected_attr(option["provider"] == active_provider)}>'
            f'{escape(option["provider"])} / {escape(option["model"])}'
            f'{escape(_provider_readiness_label(option))}</option>'
        )
        for option in provider_options
    )
    if not llm_provider_options:
        llm_provider_options = '      <option value="">需要控制令牌</option>' if redact_runtime else '      <option value="">未配置 LLM provider</option>'
    rd_seed_html = (
        f'<input id="rd-seed" value="{seed_factor_id}">'
        if seed_factor_id
        else '<input id="rd-seed" value="" placeholder="先创建或配置一个因子">'
    )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Quant Forge</title>
  <style>
    :root {{
      color-scheme: light;
      --ink: #17211d;
      --muted: #65736e;
      --faint: #87948e;
      --line: #d9e0dc;
      --line-strong: #b7c4be;
      --surface: #fbfcfa;
      --panel: #ffffff;
      --wash: #f2f6f1;
      --accent: #134b3c;
      --accent-2: #1f6f63;
      --blue: #265f8f;
      --bad: #9b2f31;
      --warn: #a36213;
      --mono: "SFMono-Regular", ui-monospace, Menlo, Consolas, monospace;
    }}
    * {{ box-sizing: border-box; }}
    html {{ min-width: 320px; }}
    body {{
      margin: 0;
      font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: var(--ink);
      background:
        linear-gradient(90deg, rgba(19,75,60,.045) 1px, transparent 1px),
        linear-gradient(180deg, rgba(38,95,143,.04) 1px, transparent 1px),
        var(--surface);
      background-size: 40px 40px;
    }}
    .app-shell {{
      min-height: 100vh;
      display: grid;
      grid-template-columns: minmax(300px, 388px) minmax(0, 1fr);
    }}
    .control-rail {{
      position: sticky;
      top: 0;
      height: 100vh;
      overflow: auto;
      padding: 22px;
      border-right: 1px solid var(--line);
      background: rgba(251, 252, 250, .94);
      backdrop-filter: blur(8px);
    }}
    .workbench {{
      min-width: 0;
      padding: 22px 28px 32px;
    }}
    h1, h2, h3, p {{ margin-top: 0; }}
    h1 {{
      margin-bottom: 4px;
      font-size: 26px;
      line-height: 1.08;
      letter-spacing: 0;
    }}
    h2 {{
      margin-bottom: 8px;
      font-size: 15px;
      color: var(--ink);
      letter-spacing: 0;
    }}
    h3 {{
      margin-bottom: 8px;
      color: var(--muted);
      font-size: 12px;
      font-weight: 800;
      letter-spacing: .08em;
      text-transform: uppercase;
    }}
    label {{
      display: block;
      margin: 14px 0 7px;
      font-size: 12px;
      font-weight: 800;
      color: var(--muted);
    }}
    textarea, select, input {{
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      color: var(--ink);
      font: inherit;
      outline: none;
      transition: border-color .15s ease, box-shadow .15s ease;
    }}
    textarea:focus, select:focus, input:focus {{
      border-color: var(--accent-2);
      box-shadow: 0 0 0 3px rgba(31, 111, 99, .12);
    }}
    textarea {{
      min-height: 126px;
      resize: vertical;
      padding: 12px;
    }}
    select {{ padding: 10px 12px; }}
    input {{ padding: 10px 12px; }}
    button {{
      width: 100%;
      margin-top: 14px;
      border: 1px solid var(--accent);
      border-radius: 6px;
      padding: 13px 16px;
      background: var(--accent);
      color: #fff;
      font-weight: 800;
      cursor: pointer;
      transition: transform .12s ease, background .12s ease;
    }}
    button:hover {{ background: #0f3f32; }}
    button:active {{ transform: translateY(1px); }}
    button.secondary {{
      border-color: var(--line-strong);
      background: #fff;
      color: var(--ink);
    }}
    button.danger {{
      border-color: var(--bad);
      background: #fff;
      color: var(--bad);
    }}
    button.danger:hover {{
      background: #fff6f6;
    }}
    button:disabled {{ opacity: .55; cursor: wait; }}
    code {{
      background: #eef5ef;
      border: 1px solid var(--line);
      border-radius: 5px;
      padding: 2px 6px;
    }}
    .brand {{
      padding-bottom: 18px;
      border-bottom: 1px solid var(--line);
    }}
    .brand-mark {{
      display: inline-grid;
      place-items: center;
      width: 36px;
      height: 36px;
      margin-bottom: 12px;
      border: 1px solid var(--line-strong);
      border-radius: 8px;
      background: #fff;
      color: var(--accent);
      font-family: var(--mono);
      font-weight: 900;
    }}
    .brand-subtitle {{
      margin-bottom: 0;
      color: var(--muted);
      font-size: 13px;
    }}
    .sr-only {{
      position: absolute;
      width: 1px;
      height: 1px;
      padding: 0;
      margin: -1px;
      overflow: hidden;
      clip: rect(0, 0, 0, 0);
      white-space: nowrap;
      border: 0;
    }}
    .runtime-strip {{
      display: grid;
      gap: 8px;
      margin: 18px 0;
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
    }}
    .runtime-row {{
      display: grid;
      grid-template-columns: 78px minmax(0, 1fr);
      gap: 10px;
      align-items: start;
      min-width: 0;
      font-size: 12px;
    }}
    .runtime-row span:first-child {{
      color: var(--faint);
      font-weight: 800;
    }}
    .meta {{
      color: var(--muted);
      font-size: 13px;
      word-break: break-word;
    }}
    .path-meta {{
      color: var(--muted);
      font-family: var(--mono);
      font-size: 11px;
      line-height: 1.45;
      word-break: break-all;
    }}
    .section-title {{
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 14px;
      margin: 0 0 14px;
    }}
    .section-title p {{
      margin: 0;
      color: var(--muted);
      font-size: 12px;
    }}
    .form-block {{
      margin: 18px 0;
      padding-bottom: 18px;
      border-bottom: 1px solid var(--line);
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(4, minmax(132px, 1fr));
      gap: 10px;
      margin: 14px 0 20px;
    }}
    .tile, .panel {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
    }}
    .tile {{
      min-height: 94px;
      padding: 14px;
      color: var(--muted);
      font-size: 12px;
      font-weight: 800;
    }}
    .tile b {{
      display: block;
      margin-top: 10px;
      color: var(--ink);
      font-family: var(--mono);
      font-size: clamp(20px, 2.3vw, 28px);
      line-height: 1.05;
      letter-spacing: 0;
    }}
    .panel {{
      margin-bottom: 14px;
      padding: 18px;
    }}
    .hero-panel {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 18px;
      align-items: start;
      border-top: 4px solid var(--accent);
    }}
    .hero-panel > p {{
      grid-column: 1 / -1;
      margin: 0;
    }}
    hr {{
      margin: 20px 0;
      border: 0;
      border-top: 1px solid var(--line);
    }}
    .button-row {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 8px;
    }}
    .button-row button {{
      padding: 11px 10px;
      font-size: 13px;
    }}
    .pill {{
      display: inline-block;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 4px 8px;
      margin: 2px 4px 2px 0;
      color: var(--muted);
      background: #fff;
      font-size: 11px;
      font-family: var(--mono);
    }}
    .ok {{ color: var(--accent-2); font-weight: 800; }}
    .warn {{ color: var(--warn); font-weight: 800; }}
    .err {{ color: var(--bad); font-weight: 800; white-space: pre-wrap; }}
    .formula {{
      max-width: 100%;
      overflow-wrap: anywhere;
      color: var(--accent);
      font-family: var(--mono);
      font-size: clamp(18px, 2vw, 24px);
      font-weight: 800;
      margin: 10px 0;
    }}
    .formula-badge {{
      justify-self: end;
      min-width: 104px;
      padding: 10px 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--wash);
      color: var(--muted);
      font-family: var(--mono);
      font-size: 11px;
      text-align: right;
    }}
    .evidence-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 14px;
    }}
    .empty-state {{
      min-height: 240px;
      display: grid;
      align-content: center;
      border-style: dashed;
      background: rgba(255, 255, 255, .72);
    }}
    .empty-state h3 {{
      color: var(--accent);
      font-size: 13px;
      letter-spacing: .08em;
    }}
    @media (max-width: 900px) {{
      .app-shell {{ grid-template-columns: 1fr; }}
      .control-rail {{
        position: static;
        height: auto;
        border-right: 0;
        border-bottom: 1px solid var(--line);
      }}
      .workbench {{ padding: 18px; }}
      .grid {{ grid-template-columns: repeat(2, minmax(140px, 1fr)); }}
      .evidence-grid {{ grid-template-columns: 1fr; }}
      .hero-panel {{ grid-template-columns: 1fr; }}
      .formula-badge {{ justify-self: start; text-align: left; }}
    }}
  </style>
</head>
<body>
<main class="app-shell">
  <aside class="control-rail">
    <div class="brand">
      <div class="brand-mark">QF</div>
      <h1>Quant Forge</h1>
      <p class="brand-subtitle">Factor research console</p>
    </div>
    <p class="sr-only">LLM parser: {provider} / {model}</p>
    <p class="sr-only">RD optimizer: {rd_optimizer_label}</p>
    <div class="runtime-strip">
      <div class="runtime-row"><span>LLM</span><strong>{provider} / {model}</strong></div>
      <div class="runtime-row"><span>RD</span><strong>{rd_optimizer_label}</strong></div>
      <div class="runtime-row"><span>data</span><div class="path-meta">{data_root}</div></div>
      <div class="runtime-row"><span>factors</span><div class="path-meta">{factor_root}</div></div>
      <div class="runtime-row"><span>values</span><div class="path-meta">{factor_values_root or '未配置'}</div></div>
      <div class="runtime-row"><span>overlay</span><div class="path-meta">{factor_values_overlay_root or '未配置'}</div></div>
      <div class="runtime-row"><span>artifacts</span><div class="path-meta">{artifact_root}</div></div>
    </div>
    <div class="form-block">
      <div class="section-title">
        <h2>01 Parse</h2>
        <p>idea → factor</p>
      </div>
      <label for="idea">因子观点</label>
      <textarea id="idea">非ST的小市值股票未来表现更好</textarea>
      <label for="parser">解析方式</label>
      <select id="parser">
        <option value="llm">LLM 语义解析: {parser_label}</option>
        <option value="rule">本地规则解析</option>
      </select>
      <label for="llm-provider">LLM Provider</label>
      <select id="llm-provider">
{llm_provider_options}
      </select>
      <button id="run">解析并验证</button>
      <button id="cancel-run" class="secondary danger" disabled>中断本次运行</button>
      <p id="status" class="meta"></p>
    </div>
    <div class="form-block">
      <div class="section-title">
        <h2>02 Research</h2>
        <p>seed → candidate</p>
      </div>
      <label for="rd-seed">Seed Factor</label>
      {rd_seed_html}
      <label for="rd-objective">目标优先级</label>
      <select id="rd-objective">
{objective_options}
      </select>
      <label for="rd-max">候选数量</label>
      <input id="rd-max" type="number" min="1" max="10" value="{research_config.default_max_candidates}">
      <label for="rd-interval">自动周期</label>
      <select id="rd-interval">
{interval_options}
      </select>
      <div class="button-row">
        <button id="rd-run">运行一次</button>
        <button id="rd-start" class="secondary">开启</button>
        <button id="rd-stop" class="secondary">停止</button>
      </div>
      <button id="rd-cancel" class="secondary danger" disabled>中断本次RD</button>
      <p id="rd-status" class="meta"></p>
    </div>
  </aside>
  <section class="workbench">
    <div class="section-title">
      <h2>Factor Tape</h2>
      <p>解析、评价、回测集中展示</p>
    </div>
    <div id="error" class="err"></div>
    <div id="result">
      <div class="panel empty-state">
        <h3>等待输入</h3>
        <p class="meta">输入因子观点后运行，公式、IC、回测收益、缓存路径会在这里展开。</p>
      </div>
    </div>
    <div class="section-title">
      <h2>RD Loop</h2>
      <p>候选因子与研究证据</p>
    </div>
    <div id="rd-result">
      <div class="panel empty-state">
        <h3>等待运行</h3>
        <p class="meta">RD 候选、gate、report path 和分段证据会展示在这里。</p>
      </div>
    </div>
  </section>
</main>
<script>
const button = document.getElementById('run');
const cancelButton = document.getElementById('cancel-run');
const statusEl = document.getElementById('status');
const errorEl = document.getElementById('error');
const resultEl = document.getElementById('result');
const rdRun = document.getElementById('rd-run');
const rdStart = document.getElementById('rd-start');
const rdStop = document.getElementById('rd-stop');
const rdCancel = document.getElementById('rd-cancel');
const rdStatusEl = document.getElementById('rd-status');
const rdResultEl = document.getElementById('rd-result');
let activeIdeaJobId = null;
let activeRdJobId = null;
const controlTokenRequired = {str(control_token_required).lower()};

function pct(value) {{
  return (Number(value) * 100).toFixed(2) + '%';
}}
function num(value, digits = 4) {{
  return Number(value).toFixed(digits);
}}
function esc(value) {{
  return String(value).replace(/[&<>"']/g, ch => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[ch]));
}}
function profilePeriodText(profile) {{
  const start = profile.test_period_start || 'full available data';
  const end = profile.test_period_end || 'latest available data';
  return `${{start}} -> ${{end}}`;
}}
function render(payload) {{
  const factor = payload.factor;
  const evaluation = payload.evaluation;
  const backtest = payload.backtest;
  const profile = backtest.simulation_profile || evaluation.simulation_profile || {{}};
  const splitRows = (evaluation.split_metrics || []).map(metric =>
    `<span class="pill">${{esc(metric.name)}} ICIR ${{num(metric.rank_icir, 2)}} · days ${{metric.ic_days}}</span>`
  ).join('');
  const horizonRows = (evaluation.horizon_metrics || []).map(metric =>
    `<span class="pill">${{metric.horizon_days}}日 IC ${{num(metric.rank_ic_mean)}} / ICIR ${{num(metric.rank_icir, 2)}}</span>`
  ).join('');
  const groupRows = (backtest.group_returns || []).map(metric =>
    `<span class="pill">${{esc(metric.group)}} ${{pct(metric.mean_return)}}</span>`
  ).join('');
  const segmentRows = (backtest.segment_metrics || []).map(metric =>
    `<span class="pill">${{esc(metric.name)}} net ${{pct(metric.net_annualized_return)}} · sharpe ${{num(metric.net_long_short_sharpe || 0, 2)}}</span>`
  ).join('');
  const warningRows = [...(evaluation.warnings || []), ...(backtest.warnings || [])].map(item =>
    `<span class="pill">${{esc(item)}}</span>`
  ).join('');
  const cacheRows = [
    `eval ${{evaluation.score_source || 'computed'}} · cached ${{evaluation.score_cached_rows || 0}} · computed ${{evaluation.score_computed_rows || 0}}`,
    evaluation.factor_values_path ? `eval path ${{evaluation.factor_values_path}}` : '',
    `backtest ${{backtest.score_source || 'computed'}} · cached ${{backtest.score_cached_rows || 0}} · computed ${{backtest.score_computed_rows || 0}}`,
    backtest.factor_values_path ? `backtest path ${{backtest.factor_values_path}}` : ''
  ].filter(Boolean).map(item => `<span class="pill">${{esc(item)}}</span>`).join('');
  resultEl.innerHTML = `
    <div class="panel hero-panel">
      <div>
        <h3>${{esc(factor.factor_id)}} · ${{esc(payload.parser.source)}} / ${{esc(payload.parser.provider)}} / ${{esc(payload.parser.model)}}</h3>
        <div class="formula">${{esc(factor.formula)}}</div>
        <p>${{esc(factor.description || '')}}</p>
        <p class="meta">test period: ${{esc(profilePeriodText(profile))}}</p>
        <p class="meta">研究口径，不是生产交易口径。</p>
      </div>
      <div class="formula-badge">
        H${{factor.horizon_days}}<br>
        ${{esc((factor.universe_filters || []).join(' · ') || 'FULL')}}
      </div>
    </div>
    <div class="grid">
      <div class="tile">Rank IC<b>${{num(evaluation.rank_ic_mean)}}</b></div>
      <div class="tile">ICIR<b>${{num(evaluation.rank_icir, 2)}}</b></div>
      <div class="tile">覆盖率<b>${{pct(evaluation.coverage)}}</b></div>
      <div class="tile">IC Days<b>${{evaluation.ic_days}}</b></div>
      <div class="tile">毛累计收益<b>${{pct(backtest.gross_cumulative_return ?? backtest.cumulative_return)}}</b></div>
      <div class="tile">净累计收益<b>${{pct(backtest.net_cumulative_return || 0)}}</b></div>
      <div class="tile">毛年化收益<b>${{pct(backtest.gross_annualized_return ?? backtest.annualized_return)}}</b></div>
      <div class="tile">净年化收益<b>${{pct(backtest.net_annualized_return || 0)}}</b></div>
      <div class="tile">年化波动<b>${{pct(backtest.annualized_volatility)}}</b></div>
      <div class="tile">最大回撤<b>${{pct(backtest.max_drawdown)}}</b></div>
      <div class="tile">持有期<b>${{backtest.holding_days}}日</b></div>
      <div class="tile">Decay<b>${{profile.decay_days || 0}}</b></div>
      <div class="tile">Top Quantile<b>${{num(profile.top_quantile || backtest.top_quantile || 0, 2)}}</b></div>
      <div class="tile">Delay<b>${{profile.execution_delay_days || 1}}日</b></div>
      <div class="tile">净多空Sharpe<b>${{num(backtest.net_long_short_sharpe || backtest.long_short_sharpe || 0, 2)}}</b></div>
      <div class="tile">调仓率<b>${{pct(backtest.rebalance_rate || 0)}}</b></div>
      <div class="tile">换手率<b>${{pct(backtest.turnover_rate || 0)}}</b></div>
    </div>
    <div class="evidence-grid">
      <div class="panel">
        <h3>三段验证</h3>
        <p>${{splitRows || '<span class="pill">暂无</span>'}}</p>
        <h3>回测分段</h3>
        <p>${{segmentRows || '<span class="pill">暂无</span>'}}</p>
        <h3>多周期评价</h3>
        <p>${{horizonRows || '<span class="pill">暂无</span>'}}</p>
      </div>
      <div class="panel">
        <h3>分组收益</h3>
        <p>${{groupRows || '<span class="pill">暂无</span>'}}</p>
        <h3>风险提示</h3>
        <p>${{warningRows || '<span class="pill">研究口径，不是生产交易口径</span>'}}</p>
        <h3>因子值缓存</h3>
        <p>${{cacheRows || '<span class="pill">computed</span>'}}</p>
      </div>
    </div>
    <div class="panel">
      <h3>Artifacts</h3>
      <p class="meta">${{esc(evaluation.artifact_path)}}</p>
      <p class="meta">${{esc(backtest.artifact_path)}}</p>
    </div>`;
}}
function renderResearch(payload) {{
  const candidates = payload.candidates || [];
  const accepted = payload.accepted_candidate_ids || [];
  const cards = candidates.map(candidate => {{
    const factor = candidate.factor;
    const evaluation = candidate.evaluation;
    const backtest = candidate.backtest;
    const profile = backtest.simulation_profile || {{}};
    const gate = candidate.gate_passed ? '<span class="ok">candidate</span>' : '<span class="err">draft</span>';
    const cacheText = `${{evaluation.score_source || 'computed'}} / ${{backtest.score_source || 'computed'}} · cached ${{evaluation.score_cached_rows || 0}}/${{backtest.score_cached_rows || 0}} · computed ${{evaluation.score_computed_rows || 0}}/${{backtest.score_computed_rows || 0}}`;
    const cachePaths = [evaluation.factor_values_path, backtest.factor_values_path].filter(Boolean).join(' / ');
    const artifacts = [evaluation.artifact_path, backtest.artifact_path].filter(Boolean).join(' / ');
    const reviewWarnings = ((candidate.self_review && candidate.self_review.normalization_warnings) || []).join('; ');
    return `
      <div class="panel hero-panel">
        <div>
          <h3>${{esc(factor.factor_id)}} · ${{gate}}</h3>
          <div class="formula">${{esc(factor.formula)}}</div>
          <p>${{esc(candidate.hypothesis.text)}}</p>
          <p class="meta">${{esc(candidate.hypothesis.rationale)}}</p>
          <p class="meta">test period: ${{esc(profilePeriodText(profile))}}</p>
          <p class="meta">研究口径，不是生产交易口径。</p>
        </div>
        <div class="formula-badge">
          score<br>${{num(candidate.score, 4)}}
        </div>
        <p>
          <span class="pill">score ${{num(candidate.score, 4)}}</span>
          <span class="pill">split ICIR ${{num(candidate.split_weighted_icir || 0, 2)}}</span>
          <span class="pill">IC ${{num(evaluation.rank_ic_mean)}}</span>
          <span class="pill">ICIR ${{num(evaluation.rank_icir, 2)}}</span>
          <span class="pill">decay ${{profile.decay_days || 0}}</span>
          <span class="pill">top ${{num(profile.top_quantile || backtest.top_quantile || 0, 2)}}</span>
          <span class="pill">net LS Sharpe ${{num(backtest.net_long_short_sharpe || backtest.long_short_sharpe || 0, 2)}}</span>
          <span class="pill">gross ${{pct(backtest.gross_annualized_return ?? backtest.annualized_return)}}</span>
          <span class="pill">net ${{pct(backtest.net_annualized_return || 0)}}</span>
          <span class="pill">rebalance rate ${{pct(backtest.rebalance_rate || 0)}}</span>
          <span class="pill">turnover rate ${{pct(backtest.turnover_rate || 0)}}</span>
          <span class="pill">factor cache ${{esc(cacheText)}}</span>
        </p>
        <p class="meta">${{esc((candidate.self_review && candidate.self_review.summary) || '')}}</p>
        <p class="meta">review normalization: ${{esc(reviewWarnings || 'none')}}</p>
        <p class="meta">factor_values: ${{esc(cachePaths || 'none')}}</p>
        <p class="meta">artifacts: ${{esc(artifacts || 'not generated')}}</p>
        <p class="meta">${{esc((backtest.warnings || []).join('; ') || 'research semantics, not production trading semantics')}}</p>
        <p class="meta">${{esc((candidate.gate_reasons || []).join('; '))}}</p>
      </div>`;
  }}).join('');
  rdResultEl.innerHTML = `
    <div class="panel">
      <h3>${{esc(payload.seed_factor_id)}} · ${{esc(payload.objective)}}</h3>
      <p class="meta">workflow: ${{esc(payload.workflow_type || payload.rd_stage || 'research')}}</p>
      <p class="meta">optimization: ${{payload.optimization_performed ? 'performed' : 'no_optimization_performed'}}</p>
      <p class="meta">accepted: ${{esc(accepted.join(', ') || 'none')}}</p>
      <p class="meta">report: ${{esc(payload.report_path || 'not generated')}}</p>
    </div>
    ${{cards || '<div class="panel"><h3>无候选</h3></div>'}}`;
}}
function rdPayload() {{
  return {{
    seed_factor_id: document.getElementById('rd-seed').value,
    objective: document.getElementById('rd-objective').value,
    max_candidates: Number(document.getElementById('rd-max').value)
  }};
}}
function sleep(ms) {{
  return new Promise(resolve => setTimeout(resolve, ms));
}}
function controlHeaders() {{
  const headers = {{'Content-Type': 'application/json'}};
  if (!controlTokenRequired) return headers;
  let token = window.sessionStorage.getItem('qf_control_token') || '';
  if (!token) {{
    token = window.prompt('请输入本次 Web 控制令牌') || '';
    if (token) window.sessionStorage.setItem('qf_control_token', token);
  }}
  if (!token) throw new Error('需要 Web 控制令牌');
  headers.Authorization = `Bearer ${{token}}`;
  return headers;
}}
async function postJson(url, payload) {{
  const response = await fetch(url, {{
    method: 'POST',
    headers: controlHeaders(),
    body: JSON.stringify(payload)
  }});
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || 'request failed');
  return body;
}}
async function getJob(jobId) {{
  const response = await fetch(`/api/jobs/${{encodeURIComponent(jobId)}}`, {{
    headers: controlHeaders()
  }});
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || 'request failed');
  return body;
}}
async function cancelJob(jobId) {{
  return postJson(`/api/jobs/${{encodeURIComponent(jobId)}}/cancel`, {{}});
}}
async function waitForJob(jobId, statusEl, slowText, isActive) {{
  const slowTimer = setTimeout(() => {{
    if (isActive(jobId)) {{
      statusEl.innerHTML = `<span class="warn">${{esc(slowText)}}</span>`;
    }}
  }}, 10000);
  try {{
    while (isActive(jobId)) {{
      const job = await getJob(jobId);
      if (job.status === 'completed') return job.result;
      if (job.status === 'failed') throw new Error(job.error || 'request failed');
      if (job.status === 'cancelled') throw new Error('运行已中断');
      if (job.slow) {{
        statusEl.innerHTML = `<span class="warn">${{esc(slowText)}} · ${{Math.round(job.runtime_seconds)}}s</span>`;
      }}
      await sleep(750);
    }}
    throw new Error('运行已中断');
  }} finally {{
    clearTimeout(slowTimer);
  }}
}}
async function submitIdea(parserMode) {{
  const job = await postJson('/api/jobs/run-idea', {{
      text: document.getElementById('idea').value,
      parser_mode: parserMode,
      llm_provider: document.getElementById('llm-provider').value
  }});
  activeIdeaJobId = job.job_id;
  cancelButton.disabled = false;
  return waitForJob(
    job.job_id,
    statusEl,
    '已运行超过10秒，系统仍在解析、计算或回测',
    jobId => activeIdeaJobId === jobId
  );
}}
button.addEventListener('click', async () => {{
  button.disabled = true;
  cancelButton.disabled = true;
  errorEl.textContent = '';
  statusEl.textContent = '运行中...';
  const parserMode = document.getElementById('parser').value;
  try {{
    const payload = await submitIdea(parserMode);
    render(payload);
    statusEl.innerHTML = '<span class="ok">验证完成</span>';
  }} catch (error) {{
    if (error.message === '运行已中断') {{
      statusEl.innerHTML = '<span class="warn">运行已中断</span>';
      return;
    }}
    if (parserMode === 'llm') {{
      const fallback = window.confirm(`LLM 无法使用：${{error.message}}\n\n是否改用本地规则解析？`);
      if (fallback) {{
        try {{
          const payload = await submitIdea('rule');
          render(payload);
          statusEl.innerHTML = '<span class="ok">已使用本地规则解析完成</span>';
          return;
        }} catch (fallbackError) {{
          errorEl.textContent = fallbackError.message;
          statusEl.textContent = '运行失败';
          return;
        }}
      }}
    }}
    errorEl.textContent = error.message;
    statusEl.textContent = '运行失败';
  }} finally {{
    activeIdeaJobId = null;
    button.disabled = false;
    cancelButton.disabled = true;
  }}
}});
cancelButton.addEventListener('click', async () => {{
  const jobId = activeIdeaJobId;
  if (!jobId) return;
  cancelButton.disabled = true;
  statusEl.innerHTML = '<span class="warn">已请求中断本次运行；当前安全阶段结束后停止</span>';
  try {{
    await cancelJob(jobId);
  }} catch (error) {{
    errorEl.textContent = error.message;
    cancelButton.disabled = false;
  }}
}});
rdRun.addEventListener('click', async () => {{
  rdRun.disabled = true;
  rdCancel.disabled = true;
  rdStatusEl.textContent = 'RD 运行中...';
  try {{
    const job = await postJson('/api/jobs/research-run-once', rdPayload());
    activeRdJobId = job.job_id;
    rdCancel.disabled = false;
    const payload = await waitForJob(
      job.job_id,
      rdStatusEl,
      '已运行超过10秒，RD 仍在生成、评价或回测',
      jobId => activeRdJobId === jobId
    );
    renderResearch(payload);
    rdStatusEl.innerHTML = '<span class="ok">RD 完成</span>';
  }} catch (error) {{
    rdStatusEl.textContent = error.message === '运行已中断' ? 'RD 已中断' : error.message;
  }} finally {{
    activeRdJobId = null;
    rdRun.disabled = false;
    rdCancel.disabled = true;
  }}
}});
rdCancel.addEventListener('click', async () => {{
  const jobId = activeRdJobId;
  if (!jobId) return;
  rdCancel.disabled = true;
  rdStatusEl.innerHTML = '<span class="warn">已请求中断本次RD；当前安全阶段结束后停止</span>';
  try {{
    await cancelJob(jobId);
  }} catch (error) {{
    rdStatusEl.textContent = error.message;
    rdCancel.disabled = false;
  }}
}});
rdStart.addEventListener('click', async () => {{
  rdStart.disabled = true;
  rdStatusEl.textContent = '调度启动中...';
  try {{
    const payload = rdPayload();
    payload.action = 'start';
    payload.interval_days = Number(document.getElementById('rd-interval').value);
    const status = await postJson('/api/research/schedule', payload);
    rdStatusEl.innerHTML = '<span class="ok">调度已开启</span>';
    if (status.last_result) renderResearch(status.last_result);
  }} catch (error) {{
    rdStatusEl.textContent = error.message;
  }} finally {{
    rdStart.disabled = false;
  }}
}});
rdStop.addEventListener('click', async () => {{
  rdStop.disabled = true;
  try {{
    const status = await postJson('/api/research/schedule', {{action: 'stop'}});
    rdStatusEl.textContent = status.run_count ? `调度已停止，累计运行 ${{status.run_count}} 次` : '调度已停止';
  }} catch (error) {{
    rdStatusEl.textContent = error.message;
  }} finally {{
    rdStop.disabled = false;
  }}
}});
</script>
</body>
</html>"""

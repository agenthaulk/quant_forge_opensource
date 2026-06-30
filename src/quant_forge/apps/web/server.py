"""Minimal local-only web/API adapter."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import UTC, date, datetime
from html import escape
import json
import logging
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
import time
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from quant_forge.backtesting.service import run_factor_backtest
from quant_forge.config import QuantForgeConfig, simulation_profile_from_mapping, validate_llm_runtime
from quant_forge.core.contracts import (
    BacktestResult,
    EvaluationResult,
    FactorDefinition,
    SimulationProfile,
    TransactionCostModel,
)
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
MAX_RD_ITERATIONS = 5
_TERMINAL_JOB_STATUSES = {"completed", "failed", "cancelled"}
_WEB_PATH_KEYS = {
    "artifact_path",
    "factor_values_path",
    "factor_values_write_path",
    "trace_root",
    "report_path",
    "round_report_paths",
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


@dataclass(frozen=True)
class _IdeaValidationSettings:
    holding_days: int
    evaluation_profile: SimulationProfile
    backtest_profile: SimulationProfile
    transaction_costs: TransactionCostModel
    parameters: dict[str, Any]


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
    """Compatibility shim: parse an idea, then validate it with default parameters."""

    research_config = rd_config or load_research_loop_config(DEFAULT_RD_CONFIG_PATH, config.research, config.simulation)
    parsed = _parse_idea(
        config,
        text,
        parser_mode=parser_mode,
        llm_provider=llm_provider,
        cancel_event=cancel_event,
    )
    return _validate_factor_workflow(
        config,
        parsed.factor,
        parser=_parser_payload(parsed),
        parameters=None,
        rd_config=research_config,
        cancel_event=cancel_event,
    )


def run_idea_parse_workflow(
    config: QuantForgeConfig,
    text: str,
    *,
    parser_mode: str = "llm",
    llm_provider: str | None = None,
    rd_config: ResearchLoopConfig | None = None,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    """Parse natural language into a draft factor and editable validation defaults."""

    research_config = rd_config or load_research_loop_config(DEFAULT_RD_CONFIG_PATH, config.research, config.simulation)
    parsed = _parse_idea(
        config,
        text,
        parser_mode=parser_mode,
        llm_provider=llm_provider,
        cancel_event=cancel_event,
    )
    return _parse_payload(parsed, research_config)


def run_idea_validation_workflow(
    config: QuantForgeConfig,
    factor: dict[str, Any] | FactorDefinition,
    *,
    parser: dict[str, Any] | None = None,
    parameters: dict[str, Any] | None = None,
    rd_config: ResearchLoopConfig | None = None,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    """Persist an edited draft factor, then evaluate and backtest it."""

    research_config = rd_config or load_research_loop_config(DEFAULT_RD_CONFIG_PATH, config.research, config.simulation)
    return _validate_factor_workflow(
        config,
        _factor_from_request(factor),
        parser=parser,
        parameters=parameters,
        rd_config=research_config,
        cancel_event=cancel_event,
    )


def _parse_idea(
    config: QuantForgeConfig,
    text: str,
    *,
    parser_mode: str,
    llm_provider: str | None,
    cancel_event: threading.Event | None,
) -> ParsedFactor:
    if not text.strip():
        raise ValueError("idea text is required")
    _raise_if_cancelled(cancel_event)
    llm_settings = config.llm.select_provider(llm_provider) if parser_mode == "llm" else config.llm
    if parser_mode == "llm":
        validate_llm_runtime(config.llm, llm_provider)
    _raise_if_cancelled(cancel_event)
    parsed = parse_factor_idea(text, llm_settings, mode=parser_mode)
    _raise_if_cancelled(cancel_event)
    return parsed


def _validate_factor_workflow(
    config: QuantForgeConfig,
    factor: FactorDefinition,
    *,
    parser: dict[str, Any] | None,
    parameters: dict[str, Any] | None,
    rd_config: ResearchLoopConfig,
    cancel_event: threading.Event | None,
) -> dict[str, Any]:
    settings = _idea_validation_settings(factor, parameters, rd_config)
    repo = FactorRepository(config.paths.factor_root)
    previous_factor = _existing_factor(repo, factor.factor_id)
    try:
        repo.save(factor)
        _raise_if_cancelled(cancel_event)
        evaluation = evaluate_factor(
            factor.factor_id,
            factor_root=config.paths.factor_root,
            data_root=config.paths.data_root,
            artifact_root=config.paths.artifact_root,
            horizon_days=settings.holding_days,
            horizon_days_matrix=rd_config.horizon_days_matrix,
            sample_splits=rd_config.sample_splits,
            simulation_profile=settings.evaluation_profile,
            factor_values_root=config.paths.factor_values_root,
            factor_values_overlay_root=config.paths.factor_values_overlay_root,
            factor_values_manifest_root=config.paths.factor_values_manifest_root,
        )
        _raise_if_cancelled(cancel_event)
        backtest = run_factor_backtest(
            factor.factor_id,
            factor_root=config.paths.factor_root,
            data_root=config.paths.data_root,
            artifact_root=config.paths.artifact_root,
            simulation_profile=settings.backtest_profile,
            holding_days=settings.holding_days,
            transaction_costs=settings.transaction_costs,
            sample_splits=rd_config.sample_splits,
            factor_values_root=config.paths.factor_values_root,
            factor_values_overlay_root=config.paths.factor_values_overlay_root,
            factor_values_manifest_root=config.paths.factor_values_manifest_root,
        )
        _raise_if_cancelled(cancel_event)
    except Exception:
        _restore_factor_after_failed_validation(repo, factor.factor_id, previous_factor)
        raise
    return _validation_payload(
        factor,
        parser=parser,
        evaluation=evaluation,
        backtest=backtest,
        parameters=settings.parameters,
    )


def run_research_once_workflow(
    config: QuantForgeConfig,
    seed_factor_id: str,
    *,
    objective: str | None = None,
    max_candidates: int | None = None,
    iterations: int | None = None,
    rd_config: ResearchLoopConfig | None = None,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    """Run one or more local RD iterations and return JSON-safe data."""

    _raise_if_cancelled(cancel_event)
    research_config = rd_config or load_research_loop_config(DEFAULT_RD_CONFIG_PATH, config.research, config.simulation)
    return _run_research_iterations(
        config,
        research_config,
        seed_factor_id,
        objective=objective or research_config.objective,
        max_candidates=max_candidates if max_candidates is not None else research_config.default_max_candidates,
        iterations=iterations if iterations is not None else 1,
        cancel_event=cancel_event,
    )


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
        lambda seed_factor_id, objective, max_candidates, iterations: run_research_once_workflow(
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
                    result = run_idea_workflow(
                        config,
                        str(payload.get("text", "")),
                        parser_mode=str(payload.get("parser_mode", "llm")),
                        llm_provider=_optional_str(payload.get("llm_provider")),
                        rd_config=research_config,
                    )
                    self._json(result)
                    return
                if path == "/api/parse-idea":
                    result = run_idea_parse_workflow(
                        config,
                        str(payload.get("text", "")),
                        parser_mode=str(payload.get("parser_mode", "llm")),
                        llm_provider=_optional_str(payload.get("llm_provider")),
                        rd_config=research_config,
                    )
                    self._json(result)
                    return
                if path == "/api/validate-idea":
                    result = run_idea_validation_workflow(
                        config,
                        _factor_from_validation_payload(payload, config),
                        parser=_optional_parser_payload(payload.get("parser")),
                        parameters=_optional_parameters_payload(payload.get("parameters")),
                        rd_config=research_config,
                    )
                    self._json(result)
                    return
                if path == "/api/research/run-once":
                    result = run_research_once_workflow(
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
                if path == "/api/jobs/parse-idea":
                    self._json(
                        job_manager.start(
                            "parse_idea",
                            lambda cancel_event: run_idea_parse_workflow(
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
                            lambda cancel_event: run_idea_validation_workflow(
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
                if path == "/api/jobs/research-run-once":
                    self._json(
                        job_manager.start(
                            "research_run_once",
                            lambda cancel_event: run_research_once_workflow(
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


def _parse_payload(parsed: ParsedFactor, rd_config: ResearchLoopConfig) -> dict[str, Any]:
    return {
        "parser": _parser_payload(parsed),
        "factor": _json_safe(parsed.factor),
        "parameters": _default_validation_parameters(parsed.factor, rd_config),
    }


def _validation_payload(
    factor: FactorDefinition,
    *,
    parser: dict[str, Any] | None,
    evaluation: EvaluationResult,
    backtest: BacktestResult,
    parameters: dict[str, Any],
) -> dict[str, Any]:
    return {
        "parser": _parser_payload_from_request(parser, factor),
        "factor": _json_safe(factor),
        "parameters": _json_safe(parameters),
        "evaluation": _json_safe(evaluation),
        "backtest": _json_safe(backtest),
    }


def _parser_payload(parsed: ParsedFactor) -> dict[str, str]:
    return {
        "source": parsed.source,
        "provider": parsed.provider,
        "model": parsed.model,
    }


def _parser_payload_from_request(parser: dict[str, Any] | None, factor: FactorDefinition) -> dict[str, str]:
    raw = parser if isinstance(parser, dict) else {}
    source = str(raw.get("source") or factor.source or "user")
    provider = str(raw.get("provider") or source)
    model = str(raw.get("model") or "")
    return {
        "source": source,
        "provider": provider,
        "model": model,
    }


def _default_validation_parameters(factor: FactorDefinition, rd_config: ResearchLoopConfig) -> dict[str, Any]:
    backtest_profile = rd_config.backtest_profile
    evaluation_profile = rd_config.evaluation_profile
    costs = rd_config.transaction_costs
    return {
        "holding_days": factor.horizon_days,
        "execution_delay_days": backtest_profile.execution_delay_days,
        "decay_days": backtest_profile.decay_days,
        "top_quantile": backtest_profile.top_quantile,
        "evaluation_start": evaluation_profile.test_period_start,
        "evaluation_end": evaluation_profile.test_period_end,
        "backtest_start": backtest_profile.test_period_start,
        "backtest_end": backtest_profile.test_period_end,
        "commission_bps": costs.commission_bps,
        "slippage_bps": costs.slippage_bps,
        "short_borrow_bps_annual": costs.short_borrow_bps_annual,
        "evaluation": _simulation_profile_payload(evaluation_profile),
        "backtest": _simulation_profile_payload(backtest_profile),
        "transaction_costs": _transaction_costs_payload(costs),
    }


def _idea_validation_settings(
    factor: FactorDefinition,
    raw_parameters: dict[str, Any] | None,
    rd_config: ResearchLoopConfig,
) -> _IdeaValidationSettings:
    defaults = _default_validation_parameters(factor, rd_config)
    raw = raw_parameters or {}
    if not isinstance(raw, dict):
        raise ValueError("validation parameters must be a JSON object")
    holding_days = _positive_int_parameter(raw.get("holding_days", defaults["holding_days"]), "holding_days")
    evaluation_overrides = _test_period_override("evaluation", raw)
    evaluation_overrides.update(_role_profile_overrides("evaluation", raw))
    backtest_overrides = _flat_backtest_profile_overrides(raw)
    backtest_overrides.update(_test_period_override("backtest", raw))
    backtest_overrides.update(_role_profile_overrides("backtest", raw))
    cost_payload = _cost_parameters(raw, defaults)
    transaction_costs = TransactionCostModel(
        commission_bps=_nonnegative_float_parameter(cost_payload["commission_bps"], "commission_bps"),
        slippage_bps=_nonnegative_float_parameter(cost_payload["slippage_bps"], "slippage_bps"),
        short_borrow_bps_annual=_nonnegative_float_parameter(
            cost_payload["short_borrow_bps_annual"],
            "short_borrow_bps_annual",
        ),
    )
    evaluation_profile = simulation_profile_from_mapping(evaluation_overrides, rd_config.evaluation_profile)
    backtest_profile = simulation_profile_from_mapping(backtest_overrides, rd_config.backtest_profile)
    return _IdeaValidationSettings(
        holding_days=holding_days,
        evaluation_profile=evaluation_profile,
        backtest_profile=backtest_profile,
        transaction_costs=transaction_costs,
        parameters={
            "holding_days": holding_days,
            "execution_delay_days": backtest_profile.execution_delay_days,
            "decay_days": backtest_profile.decay_days,
            "top_quantile": backtest_profile.top_quantile,
            "evaluation_start": evaluation_profile.test_period_start,
            "evaluation_end": evaluation_profile.test_period_end,
            "backtest_start": backtest_profile.test_period_start,
            "backtest_end": backtest_profile.test_period_end,
            "commission_bps": transaction_costs.commission_bps,
            "slippage_bps": transaction_costs.slippage_bps,
            "short_borrow_bps_annual": transaction_costs.short_borrow_bps_annual,
            "evaluation": _simulation_profile_payload(evaluation_profile),
            "backtest": _simulation_profile_payload(backtest_profile),
            "transaction_costs": _transaction_costs_payload(transaction_costs),
        },
    )


def _simulation_profile_payload(profile: SimulationProfile) -> dict[str, Any]:
    return {
        "simulation": {
            "execution_delay_days": profile.execution_delay_days,
            "decay_days": profile.decay_days,
            "top_quantile": profile.top_quantile,
        },
        "test_period": {
            "start": profile.test_period_start,
            "end": profile.test_period_end,
        },
    }


def _transaction_costs_payload(costs: TransactionCostModel) -> dict[str, float]:
    return {
        "commission_bps": costs.commission_bps,
        "slippage_bps": costs.slippage_bps,
        "short_borrow_bps_annual": costs.short_borrow_bps_annual,
    }


def _role_profile_overrides(role: str, raw: dict[str, Any]) -> dict[str, Any]:
    role_payload = raw.get(role)
    if role_payload is None:
        return {}
    if not isinstance(role_payload, dict):
        raise ValueError(f"{role} parameters must be a JSON object")
    simulation_payload = role_payload.get("simulation", {})
    if simulation_payload is None:
        simulation_payload = {}
    if not isinstance(simulation_payload, dict):
        raise ValueError(f"{role}.simulation must be a JSON object")
    overrides = _simulation_parameter_overrides(simulation_payload, f"{role}.simulation")
    overrides.update(_role_test_period_override(role, role_payload))
    return overrides


def _flat_backtest_profile_overrides(raw: dict[str, Any]) -> dict[str, Any]:
    return _simulation_parameter_overrides(raw, "")


def _simulation_parameter_overrides(raw: dict[str, Any], prefix: str) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    label = f"{prefix}." if prefix else ""
    if "execution_delay_days" in raw:
        overrides["execution_delay_days"] = _positive_int_parameter(
            raw["execution_delay_days"],
            f"{label}execution_delay_days",
        )
    if "decay_days" in raw:
        overrides["decay_days"] = _nonnegative_int_parameter(raw["decay_days"], f"{label}decay_days")
    if "top_quantile" in raw:
        overrides["top_quantile"] = _float_parameter(raw["top_quantile"], f"{label}top_quantile")
    return overrides


def _role_test_period_override(role: str, role_payload: dict[str, Any]) -> dict[str, Any]:
    test_period = role_payload.get("test_period", {})
    if test_period is None:
        test_period = {}
    if not isinstance(test_period, dict):
        raise ValueError(f"{role}.test_period must be a JSON object")
    overrides: dict[str, dict[str, str | None]] = {}
    if "start" in test_period:
        overrides.setdefault("test_period", {})["start"] = _optional_date_parameter(
            test_period["start"],
            f"{role}.test_period.start",
        )
    if "end" in test_period:
        overrides.setdefault("test_period", {})["end"] = _optional_date_parameter(
            test_period["end"],
            f"{role}.test_period.end",
        )
    return overrides


def _cost_parameters(raw: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    costs = raw.get("transaction_costs")
    if costs is None:
        costs = {}
    if not isinstance(costs, dict):
        raise ValueError("transaction_costs must be a JSON object")
    return {
        "commission_bps": costs.get("commission_bps", raw.get("commission_bps", defaults["commission_bps"])),
        "slippage_bps": costs.get("slippage_bps", raw.get("slippage_bps", defaults["slippage_bps"])),
        "short_borrow_bps_annual": costs.get(
            "short_borrow_bps_annual",
            raw.get("short_borrow_bps_annual", defaults["short_borrow_bps_annual"]),
        ),
    }


def _test_period_override(prefix: str, raw: dict[str, Any]) -> dict[str, dict[str, str | None]]:
    start_key = f"{prefix}_start"
    end_key = f"{prefix}_end"
    test_period: dict[str, str | None] = {}
    if start_key in raw:
        test_period["start"] = _optional_date_parameter(raw[start_key], start_key)
    if end_key in raw:
        test_period["end"] = _optional_date_parameter(raw[end_key], end_key)
    if not test_period:
        return {}
    return {"test_period": test_period}


def _optional_date_parameter(value: Any, name: str) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{name} must use YYYY-MM-DD") from exc
    return text


def _positive_int_parameter(value: Any, name: str) -> int:
    parsed = _int_parameter(value, name)
    if parsed < 1:
        raise ValueError(f"{name} must be positive")
    return parsed


def _nonnegative_int_parameter(value: Any, name: str) -> int:
    parsed = _int_parameter(value, name)
    if parsed < 0:
        raise ValueError(f"{name} must be non-negative")
    return parsed


def _int_parameter(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value.is_integer():
            return int(value)
        raise ValueError(f"{name} must be an integer")
    if isinstance(value, str):
        text = value.strip()
        if re.fullmatch(r"[+-]?\d+", text):
            return int(text)
        raise ValueError(f"{name} must be an integer")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _float_parameter(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a number")
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number") from exc


def _nonnegative_float_parameter(value: Any, name: str) -> float:
    parsed = _float_parameter(value, name)
    if parsed < 0:
        raise ValueError(f"{name} must be non-negative")
    return parsed


def _factor_from_request(raw: dict[str, Any] | FactorDefinition) -> FactorDefinition:
    if isinstance(raw, FactorDefinition):
        return raw
    if not isinstance(raw, dict):
        raise ValueError("factor must be a JSON object")
    filters_raw = raw.get("universe_filters", ())
    if not isinstance(filters_raw, list | tuple):
        raise ValueError("factor.universe_filters must be a list")
    return FactorDefinition(
        factor_id=str(raw["factor_id"]),
        name=str(raw.get("name", raw["factor_id"])),
        formula=str(raw["formula"]),
        status=str(raw.get("status", "draft")),  # type: ignore[arg-type]
        description=str(raw.get("description", "")),
        horizon_days=_positive_int_parameter(raw.get("horizon_days", 5), "factor.horizon_days"),
        universe_filters=tuple(str(item) for item in filters_raw),
        source=str(raw.get("source", "user")),
    )


def _factor_from_validation_payload(payload: dict[str, Any], config: QuantForgeConfig) -> FactorDefinition:
    if "factor" in payload:
        return _factor_from_request(payload["factor"])
    factor_id = str(payload.get("factor_id", "")).strip()
    if not factor_id:
        raise ValueError("factor is required")
    return FactorRepository(config.paths.factor_root).get(factor_id)


def _optional_parser_payload(value: Any) -> dict[str, Any] | None:
    if value is None or value == "":
        return None
    if not isinstance(value, dict):
        raise ValueError("parser must be a JSON object")
    return value


def _optional_parameters_payload(value: Any) -> dict[str, Any] | None:
    if value is None or value == "":
        return None
    if not isinstance(value, dict):
        raise ValueError("parameters must be a JSON object")
    return value


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
        trial_simulation_overlays=rd_config.trial_overlays,
        evaluation_simulation_profile=rd_config.evaluation_profile,
        backtest_simulation_profile=rd_config.backtest_profile,
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


def _run_research_iterations(
    config: QuantForgeConfig,
    rd_config: ResearchLoopConfig,
    seed_factor_id: str,
    *,
    objective: str,
    max_candidates: int,
    iterations: int,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    requested_iterations = _rd_iterations_parameter(iterations)
    original_seed = seed_factor_id
    current_seed = seed_factor_id
    rounds: list[dict[str, Any]] = []
    last_result: ResearchLoopResult | None = None
    stopped_reason = "completed"

    for round_index in range(1, requested_iterations + 1):
        _raise_if_cancelled(cancel_event)
        result = _run_research_once(
            config,
            rd_config,
            current_seed,
            objective=objective,
            max_candidates=max_candidates,
            cancel_event=cancel_event,
        )
        last_result = result
        next_seed, selection_reason = _next_research_seed(result, current_seed)
        rounds.append(_research_iteration_summary(result, round_index, next_seed, selection_reason))
        if round_index >= requested_iterations:
            break
        if next_seed is None:
            stopped_reason = selection_reason
            break
        if next_seed == current_seed:
            stopped_reason = "no_new_seed"
            rounds[-1]["selection_reason"] = stopped_reason
            break
        current_seed = next_seed

    if last_result is None:
        raise ValueError("research iterations require at least one round")
    payload = _json_safe(last_result)
    optimization_summary = _research_optimization_summary(rounds)
    accepted_factor_id = _last_accepted_research_factor_id(rounds)
    last_explored_factor_id = _last_explored_research_factor_id(last_result, original_seed)
    recommended_factor_id = accepted_factor_id or original_seed
    recommendation_basis = "accepted_candidate" if accepted_factor_id else "original_seed_retained"
    exploration_summary = _research_exploration_seed_summary(rounds)
    payload["requested_iterations"] = requested_iterations
    payload["iteration_count"] = len(rounds)
    payload["seed_factor_id"] = original_seed
    payload["original_seed_factor_id"] = original_seed
    payload["last_round_seed_factor_id"] = last_result.seed_factor_id
    payload["last_accepted_factor_id"] = accepted_factor_id
    payload["last_explored_factor_id"] = last_explored_factor_id
    payload["recommended_factor_id"] = recommended_factor_id
    payload["recommendation_basis"] = recommendation_basis
    payload["final_factor_id"] = recommended_factor_id
    payload["stopped_reason"] = stopped_reason
    payload.update(optimization_summary)
    payload["optimization_performed"] = optimization_summary["chain_optimization_performed"]
    payload["no_optimization_performed"] = optimization_summary["chain_no_optimization_performed"]
    payload.update(exploration_summary)
    payload["accepted_candidate_ids"] = _aggregate_research_accepted_ids(rounds)
    payload["round_report_paths"] = [str(item["report_path"]) for item in rounds if item.get("report_path")]
    payload["iteration_chain"] = {
        "requested_iterations": requested_iterations,
        "completed_iterations": len(rounds),
        "original_seed_factor_id": original_seed,
        "last_round_seed_factor_id": last_result.seed_factor_id,
        "final_seed_factor_id": last_result.seed_factor_id,
        "last_accepted_factor_id": accepted_factor_id,
        "last_explored_factor_id": last_explored_factor_id,
        "recommended_factor_id": recommended_factor_id,
        "recommendation_basis": recommendation_basis,
        "final_factor_id": payload["final_factor_id"],
        "stopped_reason": stopped_reason,
        **optimization_summary,
        **exploration_summary,
        "round_report_paths": payload["round_report_paths"],
        "rounds": rounds,
    }
    return payload


def _rd_iterations_parameter(value: Any) -> int:
    iterations = _positive_int_parameter(value, "iterations")
    if iterations > MAX_RD_ITERATIONS:
        raise ValueError(f"iterations must be between 1 and {MAX_RD_ITERATIONS}")
    return iterations


def _next_research_seed(result: ResearchLoopResult, current_seed: str) -> tuple[str | None, str]:
    for factor_id in result.accepted_candidate_ids:
        if factor_id and factor_id != current_seed:
            return factor_id, "accepted_candidate"
    if result.candidates:
        best = result.candidates[0].factor.factor_id
        if best and best != current_seed:
            return best, "fallback_best_score"
        return best, "best_candidate_is_seed"
    return None, "no_candidates"


def _last_explored_research_factor_id(result: ResearchLoopResult, original_seed: str) -> str:
    next_seed, reason = _next_research_seed(result, result.seed_factor_id)
    if next_seed is not None and reason != "best_candidate_is_seed":
        return next_seed
    if result.candidates:
        return result.candidates[0].factor.factor_id
    return result.seed_factor_id or original_seed


def _last_accepted_research_factor_id(rounds: list[dict[str, Any]]) -> str:
    for item in reversed(rounds):
        accepted = [str(factor_id) for factor_id in item.get("accepted_candidate_ids") or () if factor_id]
        if accepted:
            return accepted[-1]
    return ""


def _research_exploration_seed_summary(rounds: list[dict[str, Any]]) -> dict[str, Any]:
    if not rounds:
        return {
            "next_exploration_seed_factor_id": "",
            "next_exploration_seed_reason": "no_rounds",
            "next_exploration_seed_gate_passed": None,
        }
    last_round = rounds[-1]
    selected_seed = str(last_round.get("selected_next_seed_factor_id") or "")
    reason = str(last_round.get("selection_reason") or "")
    if not selected_seed or reason in {"no_candidates", "no_new_seed", "best_candidate_is_seed"}:
        return {
            "next_exploration_seed_factor_id": "",
            "next_exploration_seed_reason": reason or "none",
            "next_exploration_seed_gate_passed": None,
        }
    return {
        "next_exploration_seed_factor_id": selected_seed,
        "next_exploration_seed_reason": reason,
        "next_exploration_seed_gate_passed": reason == "accepted_candidate",
    }


def _research_iteration_summary(
    result: ResearchLoopResult,
    round_index: int,
    selected_next_seed: str | None,
    selection_reason: str,
) -> dict[str, Any]:
    top_candidate = result.candidates[0] if result.candidates else None
    return {
        "round": round_index,
        "seed_factor_id": result.seed_factor_id,
        "candidate_count": len(result.candidates),
        "accepted_candidate_ids": list(result.accepted_candidate_ids),
        "top_candidate_id": top_candidate.factor.factor_id if top_candidate is not None else "",
        "top_score": top_candidate.score if top_candidate is not None else None,
        "selected_next_seed_factor_id": selected_next_seed,
        "selection_reason": selection_reason,
        "optimization_performed": result.optimization_performed,
        "no_optimization_performed": result.no_optimization_performed,
        "report_path": str(result.report_path) if result.report_path is not None else "",
    }


def _research_optimization_summary(rounds: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate RD progress across chained rounds for user-facing status."""

    generated = any(
        int(item.get("candidate_count") or 0) > 0
        and item.get("selection_reason") not in {"best_candidate_is_seed", "no_new_seed"}
        for item in rounds
    )
    accepted = any(item.get("accepted_candidate_ids") for item in rounds)
    performed = any(bool(item.get("optimization_performed")) for item in rounds) or accepted
    if performed:
        status = "performed"
    elif generated:
        status = "attempted_no_acceptance"
    else:
        status = "no_optimization_performed"
    return {
        "optimization_status": status,
        "chain_optimization_status": status,
        "chain_optimization_performed": performed,
        "chain_candidate_generation_performed": generated,
        "chain_no_optimization_performed": status == "no_optimization_performed",
    }


def _aggregate_research_accepted_ids(rounds: list[dict[str, Any]]) -> list[str]:
    accepted: list[str] = []
    seen: set[str] = set()
    for item in rounds:
        for factor_id in item.get("accepted_candidate_ids") or ():
            if factor_id and factor_id not in seen:
                accepted.append(str(factor_id))
                seen.add(str(factor_id))
    return accepted


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


def _restore_factor_after_failed_validation(
    repo: FactorRepository,
    factor_id: str,
    previous_factor: FactorDefinition | None,
) -> None:
    if previous_factor is None:
        repo.delete(factor_id)
    else:
        repo.save(previous_factor)


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
        return text
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
                result[name] = _web_public_path_value(item)
            else:
                result[name] = _web_public_json(item)
        return result
    if value is None or isinstance(value, bool | int | float | str):
        return value
    return str(value)


def _path_label(path: Path) -> str:
    return path.name or "path"


def _web_public_path_value(value: Any) -> Any:
    if isinstance(value, str | os.PathLike):
        return _path_label(Path(value))
    if isinstance(value, tuple):
        return [_web_public_path_value(item) for item in value]
    if isinstance(value, set):
        return [_web_public_path_value(item) for item in sorted(value, key=str)]
    if isinstance(value, list):
        return [_web_public_path_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _web_public_path_value(item) for key, item in value.items()}
    return _web_public_json(value)


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


def _optional_int(value: Any, name: str = "value") -> int | None:
    if value in {None, ""}:
        return None
    return _int_parameter(value, name)


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


def _script_json(value: Any) -> str:
    return (
        json.dumps(_json_safe(value), ensure_ascii=False)
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )


def _provider_options_script_payload(options: tuple[dict[str, str], ...]) -> tuple[dict[str, str], ...]:
    return tuple(
        {
            "provider": option.get("provider", ""),
            "apiKeyEnv": option.get("api_key_env", ""),
            "runtimeReady": option.get("runtime_ready", "false"),
        }
        for option in options
    )


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
    llm_provider_options_json = _script_json(_provider_options_script_payload(provider_options))
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
    .param-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 8px;
      margin-top: 10px;
    }}
    .param-grid label {{
      margin: 0;
    }}
    .param-grid span {{
      display: block;
      margin-bottom: 4px;
      color: var(--muted);
      font-size: 11px;
      font-weight: 800;
    }}
    .param-grid input {{
      min-width: 0;
      padding: 9px 10px;
      font-family: var(--mono);
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
      <label for="llm-api-key-mode">LLM API Key</label>
      <select id="llm-api-key-mode">
        <option value="config">配置文件 / 环境变量加载</option>
        <option value="manual">手动输入（仅前端联调）</option>
      </select>
      <input id="llm-api-key" type="password" autocomplete="off" data-secret-policy="not-submitted" disabled>
      <p id="llm-api-key-status" class="meta"></p>
      <label>评测参数</label>
      <div class="param-grid" id="validation-controls">
        <label><span>持有期 / 天</span><input id="param-holding-days" type="number" min="1" step="1" disabled></label>
        <label><span>Decay / 天</span><input id="param-decay-days" type="number" min="0" step="1" disabled></label>
        <label><span>Top Quantile</span><input id="param-top-quantile" type="number" min="0.01" max="0.5" step="0.01" disabled></label>
        <label><span>Delay / 天</span><input id="param-delay-days" type="number" min="1" step="1" disabled></label>
        <label><span>评测开始</span><input id="param-evaluation-start" type="date" disabled></label>
        <label><span>评测结束</span><input id="param-evaluation-end" type="date" disabled></label>
        <label><span>回测开始</span><input id="param-backtest-start" type="date" disabled></label>
        <label><span>回测结束</span><input id="param-backtest-end" type="date" disabled></label>
        <label><span>手续费 bps</span><input id="param-commission-bps" type="number" min="0" step="0.1" disabled></label>
        <label><span>滑点 bps</span><input id="param-slippage-bps" type="number" min="0" step="0.1" disabled></label>
        <label><span>融券成本 bps/年</span><input id="param-short-borrow-bps" type="number" min="0" step="1" disabled></label>
      </div>
      <button id="run">解析因子</button>
      <button id="validate-run" class="secondary" disabled>验证并评测</button>
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
      <label for="rd-iterations">RD迭代次数</label>
      <input id="rd-iterations" type="number" min="1" max="{MAX_RD_ITERATIONS}" step="1" value="1">
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
const validateButton = document.getElementById('validate-run');
const cancelButton = document.getElementById('cancel-run');
const statusEl = document.getElementById('status');
const errorEl = document.getElementById('error');
const resultEl = document.getElementById('result');
const llmProviderSelect = document.getElementById('llm-provider');
const llmApiKeyMode = document.getElementById('llm-api-key-mode');
const llmApiKeyInput = document.getElementById('llm-api-key');
const llmApiKeyStatus = document.getElementById('llm-api-key-status');
const llmProviderOptions = {llm_provider_options_json};
const validationInputs = {{
  holding_days: document.getElementById('param-holding-days'),
  decay_days: document.getElementById('param-decay-days'),
  top_quantile: document.getElementById('param-top-quantile'),
  execution_delay_days: document.getElementById('param-delay-days'),
  evaluation_start: document.getElementById('param-evaluation-start'),
  evaluation_end: document.getElementById('param-evaluation-end'),
  backtest_start: document.getElementById('param-backtest-start'),
  backtest_end: document.getElementById('param-backtest-end'),
  commission_bps: document.getElementById('param-commission-bps'),
  slippage_bps: document.getElementById('param-slippage-bps'),
  short_borrow_bps_annual: document.getElementById('param-short-borrow-bps')
}};
const rdRun = document.getElementById('rd-run');
const rdStart = document.getElementById('rd-start');
const rdStop = document.getElementById('rd-stop');
const rdCancel = document.getElementById('rd-cancel');
const rdStatusEl = document.getElementById('rd-status');
const rdResultEl = document.getElementById('rd-result');
let activeIdeaJobId = null;
let activeRdJobId = null;
let parsedIdea = null;
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
function clearGlobalError() {{
  errorEl.textContent = '';
}}
function resetIdeaResult(title, message) {{
  resultEl.innerHTML = `
    <div class="panel empty-state">
      <h3>${{esc(title)}}</h3>
      <p class="meta">${{esc(message)}}</p>
    </div>`;
}}
function resetRdResult(title, message) {{
  rdResultEl.innerHTML = `
    <div class="placeholder">
      <div class="panel">
        <h3>${{esc(title)}}</h3>
        <p class="meta">${{esc(message)}}</p>
      </div>
    </div>`;
}}
function optimizationStatusText(payload) {{
  const status = payload.optimization_status || (payload.optimization_performed ? 'performed' : 'no_optimization_performed');
  if (status === 'performed') return 'performed';
  if (status === 'attempted_no_acceptance') return 'attempted_no_acceptance';
  return 'no_optimization_performed';
}}
function setValidationInputsEnabled(enabled) {{
  Object.values(validationInputs).forEach(input => {{
    input.disabled = !enabled;
  }});
  validateButton.disabled = !enabled;
}}
function currentProviderOption() {{
  return llmProviderOptions.find(option => option.provider === llmProviderSelect.value) || null;
}}
function syncLlmApiKeyControls() {{
  const option = currentProviderOption();
  const keyEnv = option && option.apiKeyEnv ? option.apiKeyEnv : '';
  const configReady = option && option.runtimeReady === 'true';
  const manual = llmApiKeyMode.value === 'manual';
  llmApiKeyInput.disabled = !manual;
  if (!manual) llmApiKeyInput.value = '';
  if (manual) {{
    llmApiKeyInput.placeholder = '仅前端联调，不提交后端';
    llmApiKeyStatus.textContent = keyEnv
      ? `手动输入不会保存或提交；后端正式调用仍读取 ${{keyEnv}}`
      : '手动输入不会保存或提交；请在 local config 中配置 API key 环境变量名后运行';
    return;
  }}
  llmApiKeyInput.placeholder = configReady
    ? `已通过 ${{keyEnv || 'provider config'}} 加载`
    : (keyEnv ? `未检测到 ${{keyEnv}}` : '当前 provider 未配置 API key 环境变量名');
  llmApiKeyStatus.textContent = configReady
    ? 'API key 已由配置文件 / 环境变量加载，前端不展示密钥'
    : 'LLM 运行前需要在本地配置 API key 环境变量名并设置对应环境变量';
}}
function fillValidationInputs(parameters) {{
  const values = parameters || {{}};
  const evaluationPeriod = ((values.evaluation || {{}}).test_period) || {{}};
  const backtest = values.backtest || {{}};
  const backtestSimulation = backtest.simulation || {{}};
  const backtestPeriod = backtest.test_period || {{}};
  const costs = values.transaction_costs || {{}};
  const resolved = {{
    holding_days: values.holding_days,
    decay_days: valueOr(values.decay_days, backtestSimulation.decay_days),
    top_quantile: valueOr(values.top_quantile, backtestSimulation.top_quantile),
    execution_delay_days: valueOr(values.execution_delay_days, backtestSimulation.execution_delay_days),
    evaluation_start: valueOr(values.evaluation_start, evaluationPeriod.start),
    evaluation_end: valueOr(values.evaluation_end, evaluationPeriod.end),
    backtest_start: valueOr(values.backtest_start, backtestPeriod.start),
    backtest_end: valueOr(values.backtest_end, backtestPeriod.end),
    commission_bps: valueOr(values.commission_bps, costs.commission_bps),
    slippage_bps: valueOr(values.slippage_bps, costs.slippage_bps),
    short_borrow_bps_annual: valueOr(values.short_borrow_bps_annual, costs.short_borrow_bps_annual)
  }};
  Object.entries(validationInputs).forEach(([name, input]) => {{
    const value = resolved[name];
    input.value = value === undefined || value === null ? '' : value;
  }});
}}
function currentEvaluationSimulation() {{
  const source = (parsedIdea && parsedIdea.parameters && parsedIdea.parameters.evaluation) || {{}};
  const simulation = source.simulation || {{}};
  return {{
    decay_days: simulation.decay_days,
    top_quantile: simulation.top_quantile,
    execution_delay_days: simulation.execution_delay_days
  }};
}}
function validationParameters() {{
  const evaluationStart = validationInputs.evaluation_start.value || null;
  const evaluationEnd = validationInputs.evaluation_end.value || null;
  const backtestStart = validationInputs.backtest_start.value || null;
  const backtestEnd = validationInputs.backtest_end.value || null;
  const decayDays = Number(validationInputs.decay_days.value);
  const topQuantile = Number(validationInputs.top_quantile.value);
  const executionDelayDays = Number(validationInputs.execution_delay_days.value);
  const commissionBps = Number(validationInputs.commission_bps.value);
  const slippageBps = Number(validationInputs.slippage_bps.value);
  const shortBorrowBpsAnnual = Number(validationInputs.short_borrow_bps_annual.value);
  const payload = {{
    holding_days: Number(validationInputs.holding_days.value),
    decay_days: decayDays,
    top_quantile: topQuantile,
    execution_delay_days: executionDelayDays,
    evaluation_start: evaluationStart,
    evaluation_end: evaluationEnd,
    backtest_start: backtestStart,
    backtest_end: backtestEnd,
    commission_bps: commissionBps,
    slippage_bps: slippageBps,
    short_borrow_bps_annual: shortBorrowBpsAnnual,
    evaluation: {{
      test_period: {{ start: evaluationStart, end: evaluationEnd }}
    }},
    backtest: {{
      simulation: {{
        decay_days: decayDays,
        top_quantile: topQuantile,
        execution_delay_days: executionDelayDays
      }},
      test_period: {{ start: backtestStart, end: backtestEnd }}
    }},
    transaction_costs: {{
      commission_bps: commissionBps,
      slippage_bps: slippageBps,
      short_borrow_bps_annual: shortBorrowBpsAnnual
    }}
  }};
  const evaluationSimulation = currentEvaluationSimulation();
  if (
    evaluationSimulation.decay_days !== undefined ||
    evaluationSimulation.top_quantile !== undefined ||
    evaluationSimulation.execution_delay_days !== undefined
  ) {{
    payload.evaluation.simulation = evaluationSimulation;
  }}
  return payload;
}}
function valueOr(value, fallback) {{
  return value === undefined || value === null ? fallback : value;
}}
function hasStableDispersion(periods) {{
  return Number(periods || 0) > 1;
}}
function numIfStable(value, periods, digits = 2) {{
  return hasStableDispersion(periods) ? num(value, digits) : 'n/a';
}}
function pctIfStable(value, periods) {{
  return hasStableDispersion(periods) ? pct(value) : 'n/a';
}}
function parserDefaultParameterMessage(parser) {{
  const source = (parser && parser.source) || '';
  if (source.toLowerCase() === 'llm') {{
    return 'LLM 已生成默认评测参数。确认或修改左侧参数后，点击“验证并评测”。';
  }}
  return '解析器已生成默认评测参数。确认或修改左侧参数后，点击“验证并评测”。';
}}
function assumptionLabel(text) {{
  if (text === 'rebalance_rate tracks component replacement per rebalance') {{
    return '调仓率 = 相邻调仓的成分替换率';
  }}
  if (text === 'turnover_rate estimates true portfolio weight turnover') {{
    return '换手率 = 基于组合权重变化估算的真实换手率';
  }}
  return text;
}}
function renderParsed(payload) {{
  const factor = payload.factor;
  resultEl.innerHTML = `
    <div class="panel hero-panel">
      <div>
        <h3>${{esc(factor.factor_id)}} · ${{esc(payload.parser.source)}} / ${{esc(payload.parser.provider)}} / ${{esc(payload.parser.model)}}</h3>
        <div class="formula">${{esc(factor.formula)}}</div>
        <p>${{esc(factor.description || '')}}</p>
        <p class="meta">${{esc(parserDefaultParameterMessage(payload.parser))}}</p>
        <p class="meta">研究口径，不是生产交易口径。</p>
      </div>
      <div class="formula-badge">
        H${{factor.horizon_days}}<br>
        ${{esc((factor.universe_filters || []).join(' · ') || 'FULL')}}
      </div>
    </div>
    <div class="panel">
      <h3>待确认参数</h3>
      <p>
        <span class="pill">holding ${{esc(payload.parameters.holding_days)}}d</span>
        <span class="pill">decay ${{esc(payload.parameters.decay_days)}}</span>
        <span class="pill">top ${{esc(payload.parameters.top_quantile)}}</span>
        <span class="pill">delay ${{esc(payload.parameters.execution_delay_days)}}d</span>
        <span class="pill">evaluation ${{esc(profilePeriodText({{test_period_start: payload.parameters.evaluation_start, test_period_end: payload.parameters.evaluation_end}}))}}</span>
        <span class="pill">backtest ${{esc(profilePeriodText({{test_period_start: payload.parameters.backtest_start, test_period_end: payload.parameters.backtest_end}}))}}</span>
        <span class="pill">commission ${{esc(payload.parameters.commission_bps)}} bps</span>
        <span class="pill">slippage ${{esc(payload.parameters.slippage_bps)}} bps</span>
        <span class="pill">short borrow ${{esc(payload.parameters.short_borrow_bps_annual)}} bps/year</span>
      </p>
    </div>`;
}}
function render(payload) {{
  const factor = payload.factor;
  const evaluation = payload.evaluation;
  const backtest = payload.backtest;
  const effectiveHoldingDays = (payload.parameters && payload.parameters.holding_days) || backtest.holding_days || factor.horizon_days;
  const evaluationProfile = evaluation.simulation_profile || {{}};
  const backtestProfile = backtest.simulation_profile || {{}};
  const profile = Object.keys(backtestProfile).length ? backtestProfile : evaluationProfile;
  const splitRows = (evaluation.split_metrics || []).map(metric =>
    `<span class="pill">${{esc(metric.name)}} ICIR ${{num(metric.rank_icir, 2)}} · t ${{num(valueOr(metric.rank_ic_t_stat, 0), 2)}} · days ${{metric.ic_days}}</span>`
  ).join(' ');
  const horizonRows = (evaluation.horizon_metrics || []).map(metric =>
    `<span class="pill">${{metric.horizon_days}}日 IC ${{num(metric.rank_ic_mean)}} / ICIR ${{num(metric.rank_icir, 2)}} / t ${{num(valueOr(metric.rank_ic_t_stat, 0), 2)}}</span>`
  ).join(' ');
  const groupRows = (backtest.group_returns || []).map(metric =>
    `<span class="pill">${{esc(metric.group)}} ${{pct(metric.mean_return)}}</span>`
  ).join(' ');
  const segmentRows = (backtest.segment_metrics || []).map(metric =>
    `<span class="pill">${{esc(metric.name)}} net ${{pct(metric.net_annualized_return)}} · sharpe ${{numIfStable(valueOr(metric.net_long_short_sharpe, 0), metric.periods, 2)}}</span>`
  ).join(' ');
  const warningRows = [...(evaluation.warnings || []), ...(backtest.warnings || [])].map(item =>
    `<span class="pill">${{esc(item)}}</span>`
  ).join(' ');
  const assumptionRows = (backtest.assumptions || []).map(item =>
    `<span class="pill">${{esc(assumptionLabel(item))}}</span>`
  ).join(' ');
  const cacheRows = [
    `eval ${{evaluation.score_source || 'computed'}} · cached ${{evaluation.score_cached_rows || 0}} · computed ${{evaluation.score_computed_rows || 0}}`,
    evaluation.factor_values_path ? `eval path ${{evaluation.factor_values_path}}` : '',
    `backtest ${{backtest.score_source || 'computed'}} · cached ${{backtest.score_cached_rows || 0}} · computed ${{backtest.score_computed_rows || 0}}`,
    backtest.factor_values_path ? `backtest path ${{backtest.factor_values_path}}` : ''
  ].filter(Boolean).map(item => `<span class="pill">${{esc(item)}}</span>`).join(' ');
  resultEl.innerHTML = `
    <div class="panel hero-panel">
      <div>
        <h3>${{esc(factor.factor_id)}} · ${{esc(payload.parser.source)}} / ${{esc(payload.parser.provider)}} / ${{esc(payload.parser.model)}}</h3>
        <div class="formula">${{esc(factor.formula)}}</div>
        <p>${{esc(factor.description || '')}}</p>
        <p class="meta">evaluation period: ${{esc(profilePeriodText(evaluationProfile))}}</p>
        <p class="meta">backtest period: ${{esc(profilePeriodText(backtestProfile))}}</p>
        <p class="meta">研究口径，不是生产交易口径。</p>
      </div>
      <div class="formula-badge">
        H${{effectiveHoldingDays}}<br>
        ${{esc((factor.universe_filters || []).join(' · ') || 'FULL')}}
      </div>
    </div>
    <div class="grid">
      <div class="tile">Rank IC<b>${{num(evaluation.rank_ic_mean)}}</b></div>
      <div class="tile">ICIR<b>${{num(evaluation.rank_icir, 2)}}</b></div>
      <div class="tile">IC t-stat<b>${{num(valueOr(evaluation.rank_ic_t_stat, 0), 2)}}</b></div>
      <div class="tile">覆盖率<b>${{pct(evaluation.coverage)}}</b></div>
      <div class="tile">IC Days<b>${{evaluation.ic_days}}</b></div>
      <div class="tile">毛累计收益<b>${{pct(backtest.gross_cumulative_return ?? backtest.cumulative_return)}}</b></div>
      <div class="tile">净累计收益<b>${{pct(valueOr(backtest.net_cumulative_return, 0))}}</b></div>
      <div class="tile">毛年化收益<b>${{pct(backtest.gross_annualized_return ?? backtest.annualized_return)}}</b></div>
      <div class="tile">净年化收益<b>${{pct(valueOr(backtest.net_annualized_return, 0))}}</b></div>
      <div class="tile">年化波动<b>${{pctIfStable(backtest.annualized_volatility, backtest.periods)}}</b></div>
      <div class="tile">最大回撤<b>${{pct(backtest.max_drawdown)}}</b></div>
      <div class="tile">回测期数<b>${{backtest.periods}}</b></div>
      <div class="tile">持有期<b>${{backtest.holding_days}}日</b></div>
      <div class="tile">Decay<b>${{valueOr(profile.decay_days, 0)}}</b></div>
      <div class="tile">Top Quantile<b>${{num(valueOr(profile.top_quantile, valueOr(backtest.top_quantile, 0)), 2)}}</b></div>
      <div class="tile">Delay<b>${{valueOr(profile.execution_delay_days, 1)}}日</b></div>
      <div class="tile">净多空Sharpe<b>${{numIfStable(valueOr(backtest.net_long_short_sharpe, valueOr(backtest.long_short_sharpe, 0)), backtest.periods, 2)}}</b></div>
      <div class="tile">调仓率<b>${{pct(valueOr(backtest.rebalance_rate, 0))}}</b></div>
      <div class="tile">换手率<b>${{pct(valueOr(backtest.turnover_rate, 0))}}</b></div>
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
        <h3>口径说明</h3>
        <p>${{assumptionRows || '<span class="pill">研究口径，不是生产交易口径</span>'}}</p>
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
  const chain = payload.iteration_chain || {{}};
  const rounds = chain.rounds || [];
  const reportPaths = payload.round_report_paths || chain.round_report_paths || [];
  const aggregateAccepted = Array.from(new Set([
    ...((payload.accepted_candidate_ids || []).filter(Boolean)),
    ...rounds.flatMap(item => item.accepted_candidate_ids || []).filter(Boolean)
  ]));
  const recommendedFactor = payload.recommended_factor_id || payload.final_factor_id || 'none';
  const lastAcceptedFactor = payload.last_accepted_factor_id || 'none';
  const lastExploredFactor = payload.last_explored_factor_id || payload.final_factor_id || 'none';
  const recommendationBasis = payload.recommendation_basis || (payload.last_accepted_factor_id ? 'accepted_candidate' : 'original_seed_retained');
  const recommendationLabel = recommendationBasis === 'accepted_candidate'
    ? '通过 gate 的最终推荐'
    : '无通过 gate 候选，保留原始 seed';
  const explorationSeed = payload.next_exploration_seed_factor_id || 'none';
  const explorationReason = payload.next_exploration_seed_reason || 'none';
  const explorationGate = payload.next_exploration_seed_gate_passed === true
    ? '通过 gate'
    : (payload.next_exploration_seed_gate_passed === false ? '未过 gate，仅用于探索' : '无下一轮探索 seed');
  const optimizationLabel = optimizationStatusText(payload);
  const optimizationScope = Number(payload.iteration_count || 1) > 1 ? ' (aggregate)' : '';
  const roundRows = rounds.map(item =>
    `<span class="pill">#${{item.round}} seed ${{esc(item.seed_factor_id)}} → ${{esc(item.selected_next_seed_factor_id || item.top_candidate_id || 'stop')}} · ${{esc(item.selection_reason || 'completed')}} · score ${{item.top_score === null || item.top_score === undefined ? 'n/a' : num(item.top_score, 4)}}</span>`
  ).join(' ');
  const reportRows = reportPaths.map(path => `<span class="pill">${{esc(path)}}</span>`).join(' ');
  const cards = candidates.map(candidate => {{
    const factor = candidate.factor;
    const evaluation = candidate.evaluation;
    const backtest = candidate.backtest;
    const evaluationProfile = evaluation.simulation_profile || {{}};
    const backtestProfile = backtest.simulation_profile || {{}};
    const profile = Object.keys(backtestProfile).length ? backtestProfile : evaluationProfile;
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
          <p class="meta">evaluation period: ${{esc(profilePeriodText(evaluationProfile))}}</p>
          <p class="meta">backtest period: ${{esc(profilePeriodText(backtestProfile))}}</p>
          <p class="meta">研究口径，不是生产交易口径。</p>
        </div>
        <div class="formula-badge">
          score<br>${{num(candidate.score, 4)}}
        </div>
        <p>
          <span class="pill">score ${{num(candidate.score, 4)}}</span>
          <span class="pill">split ICIR ${{num(valueOr(candidate.split_weighted_icir, 0), 2)}}</span>
          <span class="pill">IC ${{num(evaluation.rank_ic_mean)}}</span>
          <span class="pill">ICIR ${{num(evaluation.rank_icir, 2)}}</span>
          <span class="pill">IC t-stat ${{num(valueOr(evaluation.rank_ic_t_stat, 0), 2)}}</span>
          <span class="pill">decay ${{valueOr(profile.decay_days, 0)}}</span>
          <span class="pill">top ${{num(valueOr(profile.top_quantile, valueOr(backtest.top_quantile, 0)), 2)}}</span>
          <span class="pill">periods ${{esc(backtest.periods)}}</span>
          <span class="pill">net LS Sharpe ${{numIfStable(valueOr(backtest.net_long_short_sharpe, valueOr(backtest.long_short_sharpe, 0)), backtest.periods, 2)}}</span>
          <span class="pill">gross ${{pct(backtest.gross_annualized_return ?? backtest.annualized_return)}}</span>
          <span class="pill">net ${{pct(valueOr(backtest.net_annualized_return, 0))}}</span>
          <span class="pill">rebalance rate ${{pct(valueOr(backtest.rebalance_rate, 0))}}</span>
          <span class="pill">turnover rate ${{pct(valueOr(backtest.turnover_rate, 0))}}</span>
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
      <p class="meta">iterations: ${{esc(payload.iteration_count || 1)}} / ${{esc(payload.requested_iterations || 1)}} · original seed ${{esc(payload.original_seed_factor_id || payload.seed_factor_id)}} · recommended factor ${{esc(recommendedFactor)}} (${{esc(recommendationLabel)}}) · last accepted ${{esc(lastAcceptedFactor)}} · last explored ${{esc(lastExploredFactor)}} · ${{esc(payload.stopped_reason || 'completed')}}</p>
      <p class="meta">next exploration seed: ${{esc(explorationSeed)}} · ${{esc(explorationReason)}} · ${{esc(explorationGate)}}</p>
      <p class="meta">optimization: ${{esc(optimizationLabel)}}${{optimizationScope}}</p>
      <p class="meta">accepted: ${{esc(aggregateAccepted.join(', ') || 'none')}}</p>
      <p class="meta">report: ${{esc(payload.report_path || 'not generated')}}</p>
      <p class="meta">round reports: ${{reportRows || '<span class="pill">same as report</span>'}}</p>
      <p>${{roundRows || '<span class="pill">single round</span>'}}</p>
    </div>
    ${{cards || '<div class="panel"><h3>无候选</h3></div>'}}`;
}}
function rdPayload() {{
  return {{
    seed_factor_id: document.getElementById('rd-seed').value,
    objective: document.getElementById('rd-objective').value,
    max_candidates: Number(document.getElementById('rd-max').value),
    iterations: Number(document.getElementById('rd-iterations').value)
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
async function submitParse(parserMode) {{
  const job = await postJson('/api/jobs/parse-idea', {{
      text: document.getElementById('idea').value,
      parser_mode: parserMode,
      llm_provider: llmProviderSelect.value
  }});
  activeIdeaJobId = job.job_id;
  cancelButton.disabled = false;
  return waitForJob(
    job.job_id,
    statusEl,
    '已运行超过10秒，LLM 仍在解析因子',
    jobId => activeIdeaJobId === jobId
  );
}}
async function submitValidation() {{
  if (!parsedIdea) throw new Error('请先解析因子');
  const job = await postJson('/api/jobs/validate-idea', {{
      factor: parsedIdea.factor,
      parser: parsedIdea.parser,
      parameters: validationParameters()
  }});
  activeIdeaJobId = job.job_id;
  cancelButton.disabled = false;
  return waitForJob(
    job.job_id,
    statusEl,
    '已运行超过10秒，系统仍在计算因子或回测',
    jobId => activeIdeaJobId === jobId
  );
}}
llmProviderSelect.addEventListener('change', syncLlmApiKeyControls);
llmApiKeyMode.addEventListener('change', syncLlmApiKeyControls);
syncLlmApiKeyControls();
button.addEventListener('click', async () => {{
  button.disabled = true;
  validateButton.disabled = true;
  cancelButton.disabled = true;
  clearGlobalError();
  resetIdeaResult('解析中', '因子解析完成后，公式和默认评测参数会在这里刷新。');
  statusEl.textContent = '解析中...';
  parsedIdea = null;
  fillValidationInputs({{}});
  setValidationInputsEnabled(false);
  const parserMode = document.getElementById('parser').value;
  try {{
    const payload = await submitParse(parserMode);
    parsedIdea = payload;
    fillValidationInputs(payload.parameters);
    setValidationInputsEnabled(true);
    renderParsed(payload);
    statusEl.innerHTML = '<span class="ok">解析完成，等待确认参数</span>';
  }} catch (error) {{
    if (error.message === '运行已中断') {{
      statusEl.innerHTML = '<span class="warn">运行已中断</span>';
      return;
    }}
    if (parserMode === 'llm') {{
      const fallback = window.confirm(`LLM 无法使用：${{error.message}}\n\n是否改用本地规则解析？`);
      if (fallback) {{
        try {{
          const payload = await submitParse('rule');
          parsedIdea = payload;
          fillValidationInputs(payload.parameters);
          setValidationInputsEnabled(true);
          renderParsed(payload);
          statusEl.innerHTML = '<span class="ok">已使用本地规则解析，等待确认参数</span>';
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
validateButton.addEventListener('click', async () => {{
  validateButton.disabled = true;
  button.disabled = true;
  cancelButton.disabled = true;
  clearGlobalError();
  resetIdeaResult('验证与评测中', '评测完成后，IC、回测收益和 artifact 路径会在这里刷新。');
  statusEl.textContent = '验证与评测中...';
  try {{
    const payload = await submitValidation();
    render(payload);
    parsedIdea = {{
      parser: payload.parser,
      factor: payload.factor,
      parameters: payload.parameters || validationParameters()
    }};
    fillValidationInputs(parsedIdea.parameters);
    document.getElementById('rd-seed').value = payload.factor.factor_id;
    statusEl.innerHTML = '<span class="ok">验证完成</span>';
  }} catch (error) {{
    if (error.message === '运行已中断') {{
      statusEl.innerHTML = '<span class="warn">运行已中断</span>';
      return;
    }}
    errorEl.textContent = error.message;
    statusEl.textContent = '验证失败';
  }} finally {{
    activeIdeaJobId = null;
    button.disabled = false;
    cancelButton.disabled = true;
    setValidationInputsEnabled(Boolean(parsedIdea));
  }}
}});
cancelButton.addEventListener('click', async () => {{
  const jobId = activeIdeaJobId;
  if (!jobId) return;
  cancelButton.disabled = true;
  clearGlobalError();
  resetIdeaResult('中断中', '已请求取消当前运行，等待后端安全停止。');
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
  clearGlobalError();
  resetRdResult('RD 运行中', 'RD 候选、gate、report path 和分段证据会在本次运行完成后刷新。');
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
    clearGlobalError();
    rdStatusEl.innerHTML = '<span class="ok">RD 完成</span>';
  }} catch (error) {{
    if (error.message === '运行已中断') {{
      resetRdResult('RD 已中断', '本次 RD 已取消，未产生新的候选结果。');
      rdStatusEl.textContent = 'RD 已中断';
    }} else {{
      rdStatusEl.textContent = error.message;
    }}
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
  clearGlobalError();
  resetRdResult('RD 中断中', '已请求取消当前 RD，等待后端安全停止。');
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
  clearGlobalError();
  resetRdResult('调度启动中', '调度开启后，最近一次 RD 结果会在这里刷新。');
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
  clearGlobalError();
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

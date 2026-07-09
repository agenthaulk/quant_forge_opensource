"""Background job primitives for the local web adapter."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import gc
import logging
import threading
import time
from typing import Any
from uuid import uuid4

from quant_forge.core.contracts import SimulationProfile, TransactionCostModel


# Keep the pre-decomposition logger channel: the web adapter logs as one unit.
LOGGER = logging.getLogger("quant_forge.apps.web.server")


LONG_RUNNING_JOB_SECONDS = 10.0


_TERMINAL_JOB_STATUSES = {"completed", "failed", "cancelled"}


class _WebJobCancelled(RuntimeError):
    pass


class RequestBodyTooLarge(Exception):
    """Raised when a request body exceeds MAX_REQUEST_BODY_BYTES."""


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
    include_partial_final_period: bool = False


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
            from quant_forge.apps.web import server as _server

            gc_was_enabled = gc.isenabled()
            if gc_was_enabled:
                gc.disable()
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
                        public_result = _server._web_public_json(result)
                    except Exception as exc:
                        LOGGER.exception("web job %s result serialization failed", job_id)
                        self._finish(
                            job_id,
                            status="failed",
                            error=_client_error_message(exc, fallback="job result serialization failed"),
                        )
                    else:
                        self._finish(job_id, status="completed", result=public_result)
            finally:
                if gc_was_enabled:
                    gc.enable()
                    gc.collect()

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


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _client_error_message(exc: Exception, *, fallback: str) -> str:
    if isinstance(exc, _WebJobCancelled):
        return str(exc) or "run cancelled by user"
    if isinstance(exc, PermissionError):
        return "unauthorized"
    if isinstance(exc, ValueError):
        return str(exc)
    text = str(exc)
    if "Missing API key" in text or "requires api_key_env" in text or text.startswith("LLM request "):
        return text
    return fallback

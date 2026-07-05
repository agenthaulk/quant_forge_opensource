"""In-process scheduler for the local web research loop."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
import threading
from typing import Any, Callable

MAX_SCHEDULED_RD_ITERATIONS = 5

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResearchScheduleRequest:
    seed_factor_id: str
    objective: str = "balanced"
    interval_days: int = 1
    max_candidates: int = 3
    iterations: int = 1

    def __post_init__(self) -> None:
        if not self.seed_factor_id.strip():
            raise ValueError("seed_factor_id is required")
        if self.max_candidates < 1 or self.max_candidates > 10:
            raise ValueError("max_candidates must be between 1 and 10")
        if self.iterations < 1 or self.iterations > MAX_SCHEDULED_RD_ITERATIONS:
            raise ValueError(f"iterations must be between 1 and {MAX_SCHEDULED_RD_ITERATIONS}")


@dataclass(frozen=True)
class ResearchScheduleStatus:
    enabled: bool
    request: ResearchScheduleRequest | None = None
    run_count: int = 0
    last_run_at: str | None = None
    next_run_at: str | None = None
    last_error: str | None = None
    last_result: Any = None


ResearchRunner = Callable[[str, str, int, int], Any]


class ResearchLoopScheduler:
    """A small local scheduler scoped to one web server process."""

    def __init__(self, runner: ResearchRunner, *, allowed_interval_days: tuple[int, ...]) -> None:
        self._runner = runner
        self._allowed = allowed_interval_days
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._status = ResearchScheduleStatus(enabled=False)

    def start(self, request: ResearchScheduleRequest, *, run_immediately: bool = True) -> ResearchScheduleStatus:
        if request.interval_days not in self._allowed:
            raise ValueError(f"interval_days must be one of: {list(self._allowed)}")
        self.stop()
        self._stop = threading.Event()
        with self._lock:
            self._status = ResearchScheduleStatus(
                enabled=True,
                request=request,
                next_run_at=_iso(_now()) if run_immediately else _iso(_now() + _interval(request)),
            )
        if run_immediately:
            self._run_current()
        self._thread = threading.Thread(target=self._loop, args=(False,), daemon=True)
        self._thread.start()
        return self.status()

    def stop(self) -> ResearchScheduleStatus:
        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._thread = None
        with self._lock:
            current = self._status
            self._status = ResearchScheduleStatus(
                enabled=False,
                request=current.request,
                run_count=current.run_count,
                last_run_at=current.last_run_at,
                next_run_at=None,
                last_error=current.last_error,
                last_result=current.last_result,
            )
            return self._status

    def status(self) -> ResearchScheduleStatus:
        with self._lock:
            return self._status

    def _loop(self, run_immediately: bool) -> None:
        first = True
        while not self._stop.is_set():
            current = self.status()
            if current.request is None:
                return
            if not (first and run_immediately):
                if self._stop.wait(_interval(current.request).total_seconds()):
                    return
            first = False
            self._run_current()

    def _run_current(self) -> None:
        current = self.status()
        if current.request is None:
            return
        request = current.request
        timestamp = _now()
        try:
            result = self._runner(
                request.seed_factor_id,
                request.objective,
                request.max_candidates,
                request.iterations,
            )
            error = None
        except Exception as exc:
            result = current.last_result
            # Log full detail server-side; only a sanitized message is exposed
            # on the token-gated status surface.
            logger.exception("scheduled research run failed")
            error = _scheduler_error_message(exc)
        with self._lock:
            self._status = ResearchScheduleStatus(
                enabled=True,
                request=request,
                run_count=current.run_count + 1,
                last_run_at=_iso(timestamp),
                next_run_at=_iso(timestamp + _interval(request)),
                last_error=error,
                last_result=result,
            )


def _scheduler_error_message(exc: Exception) -> str:
    """Sanitize a scheduler exception before exposing it on status.

    Mirrors the web POST path's allowlist (apps/web/server.py:_client_error_message)
    without importing it (server.py imports this module, so importing back would
    create a circular dependency). Full detail is logged server-side; only these
    vetted messages are returned to the token-gated status surface.
    """
    if isinstance(exc, PermissionError):
        return "unauthorized"
    if isinstance(exc, ValueError):
        return str(exc)
    text = str(exc)
    if "Missing API key" in text or "requires api_key_env" in text or text.startswith("LLM request "):
        return text
    return "scheduled research run failed"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _interval(request: ResearchScheduleRequest) -> timedelta:
    return timedelta(days=request.interval_days)


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")

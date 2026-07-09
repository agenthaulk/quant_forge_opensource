"""RunEvent: the typed event contract for the run timeline (Codex B5 #8).

Every observable moment of a run is one immutable event drawn from closed
vocabularies (type, stage, actor, severity), so an event the UI or the
governance log cannot classify is unrepresentable. The module also encodes
the run state machine from the design corpus: ``RUN_STATES``,
``LEGAL_TRANSITIONS`` and the pure ``is_legal_transition`` predicate.
Everything here is pure — timestamps are caller-supplied ISO strings,
nothing reads the clock.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

RUN_EVENT_SCHEMA_VERSION = "qf.run.event.v1"

EVENT_TYPES: frozenset[str] = frozenset(
    {
        "create",
        "queue",
        "start",
        "stage_transition",
        "tool_call_started",
        "tool_call_completed",
        "artifact_created",
        "warning",
        "approval_required",
        "pause",
        "resume",
        "cancel_requested",
        "cancelled",
        "complete",
        "partial",
        "fail",
    }
)

EVENT_STAGES: frozenset[str] = frozenset(
    {
        "planning",
        "data",
        "factor",
        "evaluation",
        "backtest",
        "governance",
        "report",
    }
)

EVENT_ACTORS: frozenset[str] = frozenset({"user", "agent", "system", "kernel"})

EVENT_SEVERITIES: frozenset[str] = frozenset({"info", "warning", "error"})

RUN_STATES: frozenset[str] = frozenset(
    {
        "queued",
        "running",
        "paused",
        "partial",
        "failed",
        "cancelled",
        "completed",
    }
)

# Exactly the corpus state machine. Terminal states map to the empty set:
# nothing transitions out of failed / cancelled / completed.
LEGAL_TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset({"running"}),
    "running": frozenset({"paused", "completed", "partial", "failed", "cancelled"}),
    "paused": frozenset({"running", "cancelled"}),
    "partial": frozenset({"completed"}),
    "failed": frozenset(),
    "cancelled": frozenset(),
    "completed": frozenset(),
}


def is_legal_transition(source: str, target: str) -> bool:
    """Pure predicate over the corpus run state machine.

    Raises ValueError for states outside ``RUN_STATES`` — an unknown state is
    a contract violation, never a False.
    """

    if source not in RUN_STATES:
        raise ValueError(f"unknown run state: {source!r} (expected one of {sorted(RUN_STATES)})")
    if target not in RUN_STATES:
        raise ValueError(f"unknown run state: {target!r} (expected one of {sorted(RUN_STATES)})")
    return target in LEGAL_TRANSITIONS[source]


@dataclass(frozen=True)
class RunEvent:
    event_id: str
    run_id: str
    ts: str
    type: str
    stage: str
    actor: str
    parent_event_id: str = ""
    payload_ref: str = ""
    message: str = ""
    severity: str = "info"
    schema_version: str = RUN_EVENT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != RUN_EVENT_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported run event schema_version: {self.schema_version} "
                f"(expected {RUN_EVENT_SCHEMA_VERSION})"
            )
        if not self.event_id.strip():
            raise ValueError("event_id is required")
        if not self.run_id.strip():
            raise ValueError("run_id is required")
        if not self.ts.strip():
            raise ValueError("ts is required")
        try:
            datetime.fromisoformat(self.ts)
        except ValueError as exc:
            raise ValueError("ts must be an ISO timestamp") from exc
        if self.type not in EVENT_TYPES:
            raise ValueError(
                f"invalid event type: {self.type!r} (expected one of {sorted(EVENT_TYPES)})"
            )
        if self.stage not in EVENT_STAGES:
            raise ValueError(
                f"invalid event stage: {self.stage!r} (expected one of {sorted(EVENT_STAGES)})"
            )
        if self.actor not in EVENT_ACTORS:
            raise ValueError(
                f"invalid event actor: {self.actor!r} (expected one of {sorted(EVENT_ACTORS)})"
            )
        if self.severity not in EVENT_SEVERITIES:
            raise ValueError(
                f"invalid event severity: {self.severity!r} "
                f"(expected one of {sorted(EVENT_SEVERITIES)})"
            )

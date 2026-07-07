"""Immutable research goal artifacts with append-only audit logs.

Adopted from the reference goal-ledger mechanics (memo section 1) with the
corrections in ``docs/coordination/WAVE1_REVIEW_RESOLUTION.md``: the reference
keeps its goal ledger in a session database, so the portable artifact-file form
here is our deliberate adaptation, and the record fields are OUR schema, not a
port.

Layout under ``artifact_root``:

- ``research_goals/<goal_id>.json`` — the immutable goal artifact
  (``qf.research_goal.v1``); written exactly once and never rewritten.
- ``research_goals/<goal_id>.audit.jsonl`` — append-only audit log holding
  criterion audit rows and status transitions. The effective goal status is
  the base artifact status replayed through the status-transition rows.

Completion rule (FP-2: absence of evidence is not evidence of compliance):
a goal may become ``complete`` only when the LATEST audit row of every
REQUIRED criterion carries a completion-eligible result, and every
``satisfied`` / ``satisfied_with_caveat`` row cites at least one evidence ref
that exists on disk under ``artifact_root`` at completion time. Anything else
raises :class:`GoalCompletionError` instead of transitioning.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import json
from pathlib import Path
import re
from typing import Any, Iterable, Mapping

# Lane contract: relative-path and redaction guards come from the lineage
# store helpers so goal artifacts share one definition of "safe path/text".
from quant_forge.lineage.store import (
    _require_relative_path as require_relative_path,
    canonical_fingerprint,
    redact_free_text,
)
from quant_forge.utils import write_json

GOAL_SCHEMA_VERSION = "qf.research_goal.v1"
GOAL_AUDIT_SCHEMA_VERSION = "qf.research_goal_audit.v1"

GOAL_STATUSES = ("draft", "active", "insufficient_evidence", "complete", "abandoned")
TERMINAL_GOAL_STATUSES = ("complete", "abandoned")

#: Results that count toward completing a REQUIRED criterion.
COMPLETION_AUDIT_RESULTS = ("satisfied", "satisfied_with_caveat", "not_applicable_user_accepted")
#: Results a criterion audit row may carry.
GOAL_AUDIT_RESULTS = COMPLETION_AUDIT_RESULTS + ("not_satisfied", "insufficient_evidence")
#: Results that must cite on-disk evidence (FP-4: never fabricate evidence).
EVIDENCE_REQUIRED_RESULTS = ("satisfied", "satisfied_with_caveat")

AUDIT_ROW_TYPE_CRITERION = "criterion_audit"
AUDIT_ROW_TYPE_STATUS = "status_transition"

#: Transitions allowed via :meth:`ResearchGoalStore.transition`. ``complete``
#: is reachable ONLY through :meth:`ResearchGoalStore.complete_goal`.
_ALLOWED_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "draft": ("active", "abandoned"),
    "active": ("insufficient_evidence", "abandoned"),
    "insufficient_evidence": ("active", "abandoned"),
    "complete": (),
    "abandoned": (),
}

_SHA256_HEX_RE = re.compile(r"[0-9a-f]{64}")
_GOAL_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}-[0-9a-f]{12}")
_CRITERION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


class GoalCompletionError(ValueError):
    """The completion rule was not met; the goal must not become complete."""


@dataclass(frozen=True)
class GoalCriterion:
    criterion_id: str
    text: str
    required: bool = True

    def __post_init__(self) -> None:
        if not _CRITERION_ID_RE.fullmatch(self.criterion_id):
            raise ValueError(f"criterion_id must be a simple identifier: {self.criterion_id!r}")
        object.__setattr__(self, "text", redact_free_text(str(self.text)))
        if not self.text.strip():
            raise ValueError(f"criterion {self.criterion_id} requires non-empty text")
        if not isinstance(self.required, bool):
            raise ValueError(f"criterion {self.criterion_id} 'required' must be a bool")


@dataclass(frozen=True)
class ResearchGoal:
    goal_id: str
    objective: str
    criteria: tuple[GoalCriterion, ...]
    seed_factor_id: str
    runtime_config_hash: str
    created_at: str
    status: str = "active"
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not (_GOAL_ID_RE.fullmatch(self.goal_id) or _SHA256_HEX_RE.fullmatch(self.goal_id)):
            raise ValueError(f"goal_id must be slug+hash or a sha256 hex digest: {self.goal_id!r}")
        object.__setattr__(self, "objective", redact_free_text(str(self.objective)))
        if not self.objective.strip():
            raise ValueError("objective is required")
        object.__setattr__(self, "criteria", tuple(self.criteria))
        if not self.criteria:
            raise ValueError("a research goal requires at least one criterion")
        for criterion in self.criteria:
            if not isinstance(criterion, GoalCriterion):
                raise ValueError("criteria entries must be GoalCriterion instances")
        criterion_ids = [criterion.criterion_id for criterion in self.criteria]
        if len(set(criterion_ids)) != len(criterion_ids):
            raise ValueError("criterion ids must be unique within a goal")
        if not self.seed_factor_id.strip():
            raise ValueError("seed_factor_id is required")
        if not _SHA256_HEX_RE.fullmatch(self.runtime_config_hash):
            raise ValueError("runtime_config_hash must be a sha256 hex digest")
        _require_tz_aware_iso(self.created_at, field_name="created_at")
        if self.status not in GOAL_STATUSES:
            raise ValueError(f"status must be one of {GOAL_STATUSES}, got {self.status!r}")
        object.__setattr__(self, "evidence_refs", tuple(str(ref) for ref in self.evidence_refs))
        for ref in self.evidence_refs:
            require_relative_path(ref)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": GOAL_SCHEMA_VERSION,
            "goal_id": self.goal_id,
            "objective": self.objective,
            "criteria": [asdict(criterion) for criterion in self.criteria],
            "seed_factor_id": self.seed_factor_id,
            "runtime_config_hash": self.runtime_config_hash,
            "created_at": self.created_at,
            "status": self.status,
            "evidence_refs": list(self.evidence_refs),
        }


@dataclass(frozen=True)
class GoalAuditRow:
    criterion_id: str
    result: str
    evidence_refs: tuple[str, ...] = ()
    notes: str = ""
    recorded_at: str = ""

    def __post_init__(self) -> None:
        if not _CRITERION_ID_RE.fullmatch(self.criterion_id):
            raise ValueError(f"criterion_id must be a simple identifier: {self.criterion_id!r}")
        if self.result not in GOAL_AUDIT_RESULTS:
            raise ValueError(f"result must be one of {GOAL_AUDIT_RESULTS}, got {self.result!r}")
        object.__setattr__(self, "evidence_refs", tuple(str(ref) for ref in self.evidence_refs))
        for ref in self.evidence_refs:
            require_relative_path(ref)
        if self.result in EVIDENCE_REQUIRED_RESULTS and not self.evidence_refs:
            raise ValueError(f"result {self.result!r} requires at least one evidence ref")
        object.__setattr__(self, "notes", redact_free_text(str(self.notes)))
        _require_tz_aware_iso(self.recorded_at, field_name="recorded_at")


class ResearchGoalStore:
    """Immutable goal artifacts + append-only audit logs under ``artifact_root``."""

    def __init__(self, artifact_root: Path) -> None:
        self.artifact_root = artifact_root.expanduser()
        self.goals_root = self.artifact_root / "research_goals"

    def goal_path(self, goal_id: str) -> Path:
        return self.goals_root / f"{goal_id}.json"

    def audit_log_path(self, goal_id: str) -> Path:
        return self.goals_root / f"{goal_id}.audit.jsonl"

    def create_goal(
        self,
        *,
        objective: str,
        criteria: Iterable[GoalCriterion],
        seed_factor_id: str,
        runtime_config_hash: str,
        created_at: str,
        status: str = "active",
    ) -> ResearchGoal:
        if status in TERMINAL_GOAL_STATUSES:
            raise ValueError(f"a new goal cannot be created in terminal status {status!r}")
        criteria_tuple = tuple(criteria)
        redacted_objective = redact_free_text(str(objective))
        fingerprint = canonical_fingerprint(
            {
                "created_at": created_at,
                "criteria": [asdict(criterion) for criterion in criteria_tuple],
                "objective": redacted_objective,
                "runtime_config_hash": runtime_config_hash,
                "schema_version": GOAL_SCHEMA_VERSION,
                "seed_factor_id": seed_factor_id,
            }
        )
        goal = ResearchGoal(
            goal_id=f"{_slugify(redacted_objective)}-{fingerprint[:12]}",
            objective=objective,
            criteria=criteria_tuple,
            seed_factor_id=seed_factor_id,
            runtime_config_hash=runtime_config_hash,
            created_at=created_at,
            status=status,
        )
        path = self.goal_path(goal.goal_id)
        if path.exists():
            raise FileExistsError(f"goal artifact already exists and is immutable: {goal.goal_id}")
        write_json(path, goal.to_dict())
        return goal

    def load_goal(self, goal_id: str) -> ResearchGoal:
        path = self.goal_path(goal_id)
        if not path.exists():
            raise FileNotFoundError(f"goal not found: {goal_id}")
        return _goal_from_dict(json.loads(path.read_text(encoding="utf-8")))

    def list_goals(self) -> list[dict[str, Any]]:
        if not self.goals_root.exists():
            return []
        summaries: list[dict[str, Any]] = []
        for path in sorted(self.goals_root.glob("*.json")):
            goal = _goal_from_dict(json.loads(path.read_text(encoding="utf-8")))
            summaries.append(
                {
                    "goal_id": goal.goal_id,
                    "objective": goal.objective,
                    "seed_factor_id": goal.seed_factor_id,
                    "created_at": goal.created_at,
                    "status": self.effective_status(goal.goal_id),
                    "required_criteria_count": sum(1 for criterion in goal.criteria if criterion.required),
                    "criteria_count": len(goal.criteria),
                    "path_rel": f"research_goals/{goal.goal_id}.json",
                }
            )
        summaries.sort(key=lambda row: (str(row["created_at"]), str(row["goal_id"])))
        return summaries

    def describe(self, goal_id: str) -> dict[str, Any]:
        goal = self.load_goal(goal_id)
        return {
            "goal": goal.to_dict(),
            "effective_status": self.effective_status(goal_id),
            "audit_rows": self.read_audit_rows(goal_id),
            "path_rel": f"research_goals/{goal_id}.json",
            "audit_log_rel": f"research_goals/{goal_id}.audit.jsonl",
        }

    def read_audit_rows(self, goal_id: str) -> list[dict[str, Any]]:
        return _read_jsonl(self.audit_log_path(goal_id))

    def effective_status(self, goal_id: str) -> str:
        status = self.load_goal(goal_id).status
        for row in self.read_audit_rows(goal_id):
            if row.get("row_type") == AUDIT_ROW_TYPE_STATUS:
                status = str(row.get("to_status"))
        return status

    def append_audit(
        self,
        goal_id: str,
        *,
        criterion_id: str,
        result: str,
        evidence_refs: Iterable[str] = (),
        notes: str = "",
        recorded_at: str,
    ) -> dict[str, Any]:
        goal = self.load_goal(goal_id)
        status = self.effective_status(goal_id)
        if status in TERMINAL_GOAL_STATUSES:
            raise ValueError(f"goal {goal_id} is {status}; its audit log is closed")
        known_ids = {criterion.criterion_id for criterion in goal.criteria}
        if criterion_id not in known_ids:
            raise ValueError(f"unknown criterion for goal {goal_id}: {criterion_id!r}")
        audit = GoalAuditRow(
            criterion_id=criterion_id,
            result=result,
            evidence_refs=tuple(evidence_refs),
            notes=notes,
            recorded_at=recorded_at,
        )
        if audit.result in EVIDENCE_REQUIRED_RESULTS:
            missing = [ref for ref in audit.evidence_refs if not (self.artifact_root / ref).exists()]
            if missing:
                # FP-4: citing evidence that is not on disk is fabrication.
                raise ValueError(f"evidence refs do not exist under artifact_root: {missing}")
        row: dict[str, Any] = {
            "schema_version": GOAL_AUDIT_SCHEMA_VERSION,
            "row_type": AUDIT_ROW_TYPE_CRITERION,
            "goal_id": goal_id,
            "criterion_id": audit.criterion_id,
            "result": audit.result,
            "evidence_refs": list(audit.evidence_refs),
            "notes": audit.notes,
            "recorded_at": audit.recorded_at,
        }
        _append_jsonl(self.audit_log_path(goal_id), row)
        return row

    def transition(self, goal_id: str, to_status: str, *, recorded_at: str, reason: str = "") -> dict[str, Any]:
        if to_status == "complete":
            raise ValueError("'complete' is reachable only via complete_goal(), which runs the completion rule")
        if to_status not in GOAL_STATUSES:
            raise ValueError(f"status must be one of {GOAL_STATUSES}, got {to_status!r}")
        current = self.effective_status(goal_id)
        if to_status not in _ALLOWED_TRANSITIONS[current]:
            raise ValueError(f"transition {current!r} -> {to_status!r} is not allowed")
        return self._append_status_row(goal_id, from_status=current, to_status=to_status, recorded_at=recorded_at, reason=reason)

    def complete_goal(self, goal_id: str, *, recorded_at: str) -> dict[str, Any]:
        goal = self.load_goal(goal_id)
        _require_tz_aware_iso(recorded_at, field_name="recorded_at")
        current = self.effective_status(goal_id)
        if current in TERMINAL_GOAL_STATUSES:
            raise GoalCompletionError(f"goal {goal_id} is already {current}")
        failures: list[str] = []
        latest_rows = self._latest_criterion_rows(goal_id)
        for criterion in goal.criteria:
            if not criterion.required:
                continue
            row = latest_rows.get(criterion.criterion_id)
            if row is None:
                failures.append(f"required criterion {criterion.criterion_id} has no audit row")
                continue
            result = str(row.get("result"))
            if result not in COMPLETION_AUDIT_RESULTS:
                failures.append(f"required criterion {criterion.criterion_id} latest result is {result!r}")
                continue
            if result in EVIDENCE_REQUIRED_RESULTS:
                refs = [str(ref) for ref in row.get("evidence_refs") or []]
                if not any((self.artifact_root / ref).exists() for ref in refs):
                    failures.append(
                        f"required criterion {criterion.criterion_id} cites no evidence ref "
                        "that exists under artifact_root"
                    )
        if failures:
            raise GoalCompletionError(f"goal {goal_id} cannot complete: " + "; ".join(failures))
        return self._append_status_row(
            goal_id, from_status=current, to_status="complete", recorded_at=recorded_at, reason="completion rule satisfied"
        )

    def _latest_criterion_rows(self, goal_id: str) -> dict[str, dict[str, Any]]:
        latest: dict[str, dict[str, Any]] = {}
        for row in self.read_audit_rows(goal_id):
            if row.get("row_type") == AUDIT_ROW_TYPE_CRITERION:
                latest[str(row.get("criterion_id"))] = row
        return latest

    def _append_status_row(
        self, goal_id: str, *, from_status: str, to_status: str, recorded_at: str, reason: str
    ) -> dict[str, Any]:
        _require_tz_aware_iso(recorded_at, field_name="recorded_at")
        row: dict[str, Any] = {
            "schema_version": GOAL_AUDIT_SCHEMA_VERSION,
            "row_type": AUDIT_ROW_TYPE_STATUS,
            "goal_id": goal_id,
            "from_status": from_status,
            "to_status": to_status,
            "reason": redact_free_text(str(reason)),
            "recorded_at": recorded_at,
        }
        _append_jsonl(self.audit_log_path(goal_id), row)
        return row


def _goal_from_dict(raw: Mapping[str, Any]) -> ResearchGoal:
    schema_version = raw.get("schema_version")
    if schema_version != GOAL_SCHEMA_VERSION:
        raise ValueError(f"unsupported goal schema version: {schema_version!r}")
    return ResearchGoal(
        goal_id=str(raw.get("goal_id", "")),
        objective=str(raw.get("objective", "")),
        criteria=tuple(
            GoalCriterion(
                criterion_id=str(entry.get("criterion_id", "")),
                text=str(entry.get("text", "")),
                required=bool(entry.get("required", True)),
            )
            for entry in raw.get("criteria") or ()
        ),
        seed_factor_id=str(raw.get("seed_factor_id", "")),
        runtime_config_hash=str(raw.get("runtime_config_hash", "")),
        created_at=str(raw.get("created_at", "")),
        status=str(raw.get("status", "")),
        evidence_refs=tuple(str(ref) for ref in raw.get("evidence_refs") or ()),
    )


def _slugify(text: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return normalized[:24].strip("-") or "research-goal"


def _require_tz_aware_iso(value: str, *, field_name: str) -> None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an ISO timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must be timezone-aware: {value!r}")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped:
            rows.append(json.loads(stripped))
    return rows


def _append_jsonl(path: Path, row: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(row, ensure_ascii=False, sort_keys=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")

"""Durable research memory: append-only JSONL stores under ``artifact_root/research_memory``.

Adopted from memo section 3 (research memory) with the wave-1 corrections in
mind: this is OUR schema, not a port. Three knowledge files —
``rules.jsonl``, ``findings.jsonl``, ``failures.jsonl`` — hold promoted
knowledge rows; ``observations.jsonl`` is the pending trace tier (a single
observation stays trace-only and never creates a knowledge row).

Invariants:

- every file is append-only; an update is a new superseding row, existing
  lines are never rewritten and statements are never edited in place;
- promotion is a PURE deterministic function (:func:`promote`) of the
  observation set — no LLM prose ever decides promotion;
- rules NEVER auto-activate (FP-2 / human governance): a rule decision or row
  produced by promotion always carries status ``needs_human_review``, and the
  contract layer rejects any other status for a promoted rule;
- statements pass :func:`quant_forge.lineage.store.redact_free_text` before
  reaching disk; evidence refs must be run ids or artifact-root-relative
  paths, never absolute paths.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
from pathlib import Path
import re
from typing import Any, Iterable, Mapping

from quant_forge.lineage.store import canonical_fingerprint, redact_free_text

RESEARCH_MEMORY_SCHEMA_VERSION = "qf.research_memory.v1"
MEMORY_KINDS = ("rule", "finding", "failure")
RULE_CANDIDATE_STATUS = "needs_human_review"
FAILURE_SIGNATURE_CLASSES = frozenset({"gate_blocked", "validation_error"})

_KIND_FILES = {"rule": "rules.jsonl", "finding": "findings.jsonl", "failure": "failures.jsonl"}
_ACTIVE_ROW_STATUSES = frozenset({"active", RULE_CANDIDATE_STATUS})
_MEMORY_DIR = "research_memory"
_OBSERVATIONS_FILE = "observations.jsonl"


@dataclass(frozen=True)
class MemoryObservation:
    """One raw observation submitted toward promotion (trace tier, not a row)."""

    signature: str
    statement: str
    run_id: str
    observed_at: str
    data_window: str = ""
    failure_class: str = ""
    evidence_ref: str = ""
    scope: str = "global"

    def __post_init__(self) -> None:
        if not self.signature.strip():
            raise ValueError("observation signature is required")
        if not self.statement.strip():
            raise ValueError("observation statement is required")
        if not self.run_id.strip():
            raise ValueError("observation run_id is required")
        _require_iso_timestamp(self.observed_at)
        if self.evidence_ref:
            _require_relative_ref(self.evidence_ref)

    def to_dict(self) -> dict[str, Any]:
        return {
            "signature": self.signature,
            "statement": self.statement,
            "run_id": self.run_id,
            "observed_at": self.observed_at,
            "data_window": self.data_window,
            "failure_class": self.failure_class,
            "evidence_ref": self.evidence_ref,
            "scope": self.scope,
        }


@dataclass(frozen=True)
class PromotionDecision:
    """Deterministic outcome of :func:`promote` for one signature."""

    signature: str
    kind: str
    status: str
    statement: str
    scope: str
    observation_count: int
    first_seen: str
    last_seen: str
    evidence_refs: tuple[str, ...] = field(default_factory=tuple)
    run_ids: tuple[str, ...] = field(default_factory=tuple)
    data_windows: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.kind not in MEMORY_KINDS:
            raise ValueError(f"unknown memory kind: {self.kind!r}")
        if self.kind == "rule" and self.status != RULE_CANDIDATE_STATUS:
            # FP-2 / human governance: promotion can propose a rule but can
            # never activate one. Any other status is unrepresentable here.
            raise ValueError(f"promoted rules must carry status {RULE_CANDIDATE_STATUS!r}; rules never auto-activate")
        if self.kind in {"finding", "failure"} and self.status != "active":
            raise ValueError(f"promoted {self.kind} rows must carry status 'active', got {self.status!r}")
        if not self.signature.strip():
            raise ValueError("decision signature is required")
        if not self.statement.strip():
            raise ValueError("decision statement is required")
        if self.observation_count < 2:
            raise ValueError("a promotion decision requires at least 2 observations")
        if self.kind == "rule" and self.observation_count < 3:
            raise ValueError("a rule candidate requires at least 3 observations")
        _require_iso_timestamp(self.first_seen)
        _require_iso_timestamp(self.last_seen)
        for ref in self.evidence_refs:
            _require_relative_ref(ref)


def promote(observations: Iterable[MemoryObservation]) -> tuple[PromotionDecision, ...]:
    """PURE deterministic promotion policy over the full observation set.

    - 1 observation of a signature -> trace only, no decision (no row);
    - >=2 observations across >=2 distinct run ids -> finding, or failure when
      the signature carries a gate-blocking/validation-error class;
    - >=3 observations across >=2 distinct non-empty data windows -> rule
      CANDIDATE with status ``needs_human_review`` (never active).

    Empty data windows are unknowns (FP-4) and never count as distinct.
    """

    groups: dict[str, list[MemoryObservation]] = {}
    for observation in observations:
        groups.setdefault(observation.signature, []).append(observation)
    decisions: list[PromotionDecision] = []
    for signature in sorted(groups):
        group = sorted(groups[signature], key=lambda item: (item.observed_at, item.run_id, item.evidence_ref))
        run_ids = tuple(sorted({item.run_id for item in group}))
        windows = tuple(sorted({item.data_window for item in group if item.data_window}))
        if len(group) >= 3 and len(windows) >= 2:
            kind, status = "rule", RULE_CANDIDATE_STATUS
        elif len(group) >= 2 and len(run_ids) >= 2:
            is_failure = any(item.failure_class in FAILURE_SIGNATURE_CLASSES for item in group)
            kind, status = ("failure", "active") if is_failure else ("finding", "active")
        else:
            continue  # trace only: below every promotion threshold
        decisions.append(
            PromotionDecision(
                signature=signature,
                kind=kind,
                status=status,
                statement=group[0].statement,
                scope=group[0].scope,
                observation_count=len(group),
                first_seen=group[0].observed_at,
                last_seen=group[-1].observed_at,
                evidence_refs=tuple(sorted({item.evidence_ref or item.run_id for item in group})),
                run_ids=run_ids,
                data_windows=windows,
            )
        )
    return tuple(decisions)


class ResearchMemoryStore:
    """Append-only research memory under ``artifact_root/research_memory``."""

    def __init__(self, artifact_root: str | Path) -> None:
        self.artifact_root = Path(artifact_root).expanduser()
        self.memory_root = self.artifact_root / _MEMORY_DIR

    @property
    def observations_path(self) -> Path:
        return self.memory_root / _OBSERVATIONS_FILE

    def path_for(self, kind: str) -> Path:
        if kind not in _KIND_FILES:
            raise ValueError(f"unknown memory kind: {kind!r}")
        return self.memory_root / _KIND_FILES[kind]

    def record_observation(
        self,
        *,
        signature: str,
        statement: str,
        run_id: str,
        data_window: str = "",
        failure_class: str = "",
        evidence_ref: str = "",
        observed_at: str | None = None,
        scope: str = "global",
    ) -> MemoryObservation:
        """Append one observation to the trace tier (never a knowledge row)."""

        observation = MemoryObservation(
            signature=redact_free_text(signature),
            statement=redact_free_text(statement),
            run_id=run_id,
            observed_at=observed_at if observed_at is not None else _utc_now_iso(),
            data_window=data_window,
            failure_class=failure_class,
            evidence_ref=evidence_ref,
            scope=redact_free_text(scope),
        )
        _append_jsonl(
            self.observations_path,
            {"schema_version": RESEARCH_MEMORY_SCHEMA_VERSION, "record": "observation", **observation.to_dict()},
        )
        return observation

    def promote_pending(self) -> tuple[dict[str, Any], ...]:
        """Run :func:`promote` over all recorded observations; append new rows.

        Duplicate submissions update ``observation_count``/``last_seen`` by
        appending a superseding row that preserves the original statement and
        ``first_seen``; nothing is ever rewritten in place. Idempotent when no
        new observations arrived.
        """

        decisions = promote(self._read_observations())
        appended: list[dict[str, Any]] = []
        for decision in decisions:
            row = self._row_for_decision(decision)
            if row is not None:
                _append_jsonl(self.path_for(decision.kind), row)
                appended.append(row)
        return tuple(appended)

    def read_recent(self, kind: str, limit: int = 5) -> tuple[dict[str, Any], ...]:
        """Latest live rows for ``kind``: superseded rows dropped, newest first."""

        rows = _read_jsonl(self.path_for(kind))
        superseded = {row.get("supersedes") for row in rows if row.get("supersedes")}
        live_by_signature: dict[str, dict[str, Any]] = {}
        for row in rows:
            if row.get("entry_id") in superseded:
                continue
            if str(row.get("status") or "") not in _ACTIVE_ROW_STATUSES:
                continue
            live_by_signature[str(row.get("signature") or row.get("entry_id"))] = row
        ordered = sorted(live_by_signature.values(), key=lambda row: str(row.get("last_seen") or ""), reverse=True)
        return tuple(ordered[: max(limit, 0)])

    def _read_observations(self) -> tuple[MemoryObservation, ...]:
        observations: list[MemoryObservation] = []
        for row in _read_jsonl(self.observations_path):
            observations.append(
                MemoryObservation(
                    signature=str(row.get("signature") or ""),
                    statement=str(row.get("statement") or ""),
                    run_id=str(row.get("run_id") or ""),
                    observed_at=str(row.get("observed_at") or ""),
                    data_window=str(row.get("data_window") or ""),
                    failure_class=str(row.get("failure_class") or ""),
                    evidence_ref=str(row.get("evidence_ref") or ""),
                    scope=str(row.get("scope") or "global"),
                )
            )
        return tuple(observations)

    def _row_for_decision(self, decision: PromotionDecision) -> dict[str, Any] | None:
        latest = self._latest_live_row(decision.kind, decision.signature)
        if latest is None:
            payload: dict[str, Any] = {
                "schema_version": RESEARCH_MEMORY_SCHEMA_VERSION,
                "kind": decision.kind,
                "signature": decision.signature,
                "statement": redact_free_text(decision.statement),
                "evidence_refs": list(decision.evidence_refs),
                "observation_count": decision.observation_count,
                "first_seen": decision.first_seen,
                "last_seen": decision.last_seen,
                "scope": decision.scope,
                "status": decision.status,
                "supersedes": None,
            }
        elif decision.observation_count <= int(latest.get("observation_count") or 0):
            return None  # nothing new for this signature; append nothing
        else:
            refs = sorted(set(str(item) for item in latest.get("evidence_refs") or []) | set(decision.evidence_refs))
            payload = {
                "schema_version": RESEARCH_MEMORY_SCHEMA_VERSION,
                "kind": decision.kind,
                "signature": decision.signature,
                # Statements are never rewritten: the superseding row carries
                # the original statement; only counts/recency/evidence move.
                "statement": str(latest.get("statement") or ""),
                "evidence_refs": refs,
                "observation_count": decision.observation_count,
                "first_seen": str(latest.get("first_seen") or decision.first_seen),
                "last_seen": decision.last_seen,
                "scope": str(latest.get("scope") or decision.scope),
                "status": str(latest.get("status") or decision.status),
                "supersedes": str(latest.get("entry_id") or ""),
            }
        return {"entry_id": canonical_fingerprint(payload), **payload}

    def _latest_live_row(self, kind: str, signature: str) -> dict[str, Any] | None:
        rows = _read_jsonl(self.path_for(kind))
        superseded = {row.get("supersedes") for row in rows if row.get("supersedes")}
        latest: dict[str, Any] | None = None
        for row in rows:
            if row.get("signature") == signature and row.get("entry_id") not in superseded:
                latest = row
        return latest


def _require_relative_ref(ref: str) -> None:
    """Evidence refs are run ids or artifact-root-relative paths, never absolute.

    Mirrors the lineage-store relative-path contract (no absolute paths, no
    drive letters, no user-home markers, no ``..`` traversal).
    """

    if not ref.strip():
        raise ValueError("evidence ref must not be empty")
    if Path(ref).is_absolute() or re.match(r"^[A-Za-z]:[\\/]", ref) or ref.startswith("~"):
        raise ValueError(f"evidence ref must be a run id or a path relative to artifact_root: {ref}")
    if ".." in Path(ref).parts:
        raise ValueError(f"evidence ref must not traverse outside artifact_root: {ref}")


def _require_iso_timestamp(value: str) -> None:
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"timestamp must be ISO format: {value!r}") from exc


def _utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


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

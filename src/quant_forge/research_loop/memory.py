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

Review events (SE-iii, DECISIONS.md "2026-07-13 -- Self-evolution engine
CP0"): promoted rule/finding/failure ROWS are never mutated to express human
governance decisions. Instead ``activations.jsonl`` is a SEPARATE append-only
event log of ``activate`` / ``deactivate`` / ``retire`` / ``unretire``
decisions; "effective" rule activation or finding/failure retirement is a
PURE function of the latest valid event per signature (:meth:`
ResearchMemoryStore.rule_states`, :meth:`ResearchMemoryStore.
retired_signatures`) and is never derivable from promotion output alone. A
promoted rule row's ``status`` therefore stays ``needs_human_review`` forever
(FP-2): promotion structurally cannot mint activity.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
from pathlib import Path
import re
from typing import Any, Iterable, Mapping

# ``_advisory_file_lock`` is module-private in lineage.store but this reuses
# the SAME lock primitive on purpose (SE-ix / S2-F10): the lineage store
# already established the pattern of serializing read-then-append critical
# sections on JSONL files with a sidecar ``fcntl.flock``; goals.py similarly
# imports lineage.store's private relative-path guard rather than forking a
# second implementation.
from quant_forge.lineage.store import _advisory_file_lock, canonical_fingerprint, redact_free_text

RESEARCH_MEMORY_SCHEMA_VERSION = "qf.research_memory.v1"
MEMORY_KINDS = ("rule", "finding", "failure")
RULE_CANDIDATE_STATUS = "needs_human_review"
FAILURE_SIGNATURE_CLASSES = frozenset({"gate_blocked", "validation_error"})

# --- review events (SE-iii): append-only activate/deactivate/retire/unretire ---
RESEARCH_MEMORY_REVIEW_SCHEMA_VERSION = "qf.memory_review.v1"
REVIEW_ACTIONS = ("activate", "deactivate", "retire", "unretire")
_RULE_REVIEW_ACTIONS = frozenset({"activate", "deactivate"})
_RETIRE_REVIEW_ACTIONS = frozenset({"retire", "unretire"})
_RETIRE_TARGET_KINDS = frozenset({"finding", "failure"})

_KIND_FILES = {"rule": "rules.jsonl", "finding": "findings.jsonl", "failure": "failures.jsonl"}
_ACTIVE_ROW_STATUSES = frozenset({"active", RULE_CANDIDATE_STATUS})
_MEMORY_DIR = "research_memory"
_OBSERVATIONS_FILE = "observations.jsonl"
_REVIEW_EVENTS_FILE = "activations.jsonl"
_LOCK_FILE = "memory.lock"
_EVENT_ID_RE = re.compile(r"^[0-9a-f]{64}$")


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


@dataclass(frozen=True)
class MemoryReviewEvent:
    """One append-only human governance decision over a promoted row (SE-iii).

    ``event_id`` is never a stored field: it is the kernel
    :func:`canonical_fingerprint` over the FULL payload (:meth:`to_dict`,
    schema_version included) computed by :meth:`event_id`, mirroring how
    :class:`PromotionDecision` rows derive ``entry_id`` outside the dataclass.
    Because nothing is excluded from the fingerprint (not even ``decided_at``),
    only an EXACT byte-identical replay collapses to the same id — a genuinely
    new decision (even one immediately reversing a prior one) always mints a
    new event, and the previous decision remains readable in the log (FP-2:
    rows/events are never mutated).
    """

    target_kind: str
    target_signature: str
    reviewed_entry_id: str
    action: str
    actor: str
    decided_at: str
    rationale: str = ""
    supersedes: str = ""
    schema_version: str = RESEARCH_MEMORY_REVIEW_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != RESEARCH_MEMORY_REVIEW_SCHEMA_VERSION:
            raise ValueError(f"unsupported review event schema_version: {self.schema_version!r}")
        if self.target_kind not in MEMORY_KINDS:
            raise ValueError(f"unknown review target_kind: {self.target_kind!r}")
        if self.action not in REVIEW_ACTIONS:
            raise ValueError(f"unknown review action: {self.action!r}; must be one of {REVIEW_ACTIONS}")
        if self.action in _RULE_REVIEW_ACTIONS and self.target_kind != "rule":
            raise ValueError(f"action {self.action!r} is only valid for target_kind='rule', got {self.target_kind!r}")
        if self.action in _RETIRE_REVIEW_ACTIONS and self.target_kind not in _RETIRE_TARGET_KINDS:
            raise ValueError(
                f"action {self.action!r} is only valid for target_kind in {sorted(_RETIRE_TARGET_KINDS)}, "
                f"got {self.target_kind!r}"
            )
        if not self.target_signature.strip():
            raise ValueError("target_signature is required")
        if not self.reviewed_entry_id.strip():
            raise ValueError("reviewed_entry_id is required")
        if not self.actor.strip():
            raise ValueError("actor is required")
        _require_iso_timestamp(self.decided_at)
        if self.supersedes and not _EVENT_ID_RE.fullmatch(self.supersedes):
            raise ValueError(f"supersedes must be a sha256 hex event id or empty, got {self.supersedes!r}")

    def event_id(self) -> str:
        """Content identity of this event: fingerprint over the full payload."""

        return canonical_fingerprint(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "target_kind": self.target_kind,
            "target_signature": self.target_signature,
            "reviewed_entry_id": self.reviewed_entry_id,
            "action": self.action,
            "actor": self.actor,
            "rationale": self.rationale,
            "decided_at": self.decided_at,
            "supersedes": self.supersedes,
        }


def promote(observations: Iterable[MemoryObservation]) -> tuple[PromotionDecision, ...]:
    """PURE deterministic promotion policy over the full observation set.

    - exact duplicate resubmissions of the same event (identical payload) are
      counted ONCE, and beyond that at most ONE observation per
      (signature, run_id) reaches the thresholds: ``observation_count``
      counts independent evidence units, never retries or re-measurements of
      the same unit (SE-ii evidence-unit cap);
    - 1 distinct observation of a signature -> trace only, no decision (no row);
    - >=2 distinct observations across >=2 distinct run ids -> finding, or
      failure when the signature carries a gate-blocking/validation-error class;
    - >=3 distinct observations across >=2 distinct non-empty data windows AND
      >=2 distinct run ids -> rule CANDIDATE with status ``needs_human_review``
      (never active); a single run can never propose a rule;
    - a failure-class signature that crosses the rule threshold yields BOTH the
      rule candidate and the failure row — rule minting must not swallow the
      failure record.

    Empty data windows are unknowns (FP-4) and never count as distinct.
    """

    groups: dict[str, list[MemoryObservation]] = {}
    seen_events: set[str] = set()
    for observation in observations:
        event_fingerprint = canonical_fingerprint(observation.to_dict())
        if event_fingerprint in seen_events:
            continue  # exact retry of an already-counted event
        seen_events.add(event_fingerprint)
        groups.setdefault(observation.signature, []).append(observation)
    decisions: list[PromotionDecision] = []
    for signature in sorted(groups):
        group = sorted(groups[signature], key=lambda item: (item.observed_at, item.run_id, item.evidence_ref))
        # Evidence-unit cap (SE-ii, DECISIONS 2026-07-13): at most ONE
        # observation per (signature, run_id) reaches the thresholds. A rerun
        # of one evidence unit — a re-simulation, a UI retry, a jittered
        # re-measurement — is a correction of the same study, never
        # additional confirmation. The sort above makes the kept row
        # deterministic (earliest observed_at, then run_id/evidence_ref).
        first_per_run: dict[str, MemoryObservation] = {}
        for item in group:
            first_per_run.setdefault(item.run_id, item)
        group = sorted(first_per_run.values(), key=lambda item: (item.observed_at, item.run_id, item.evidence_ref))
        run_ids = tuple(sorted({item.run_id for item in group}))
        windows = tuple(sorted({item.data_window for item in group if item.data_window}))
        is_failure = any(item.failure_class in FAILURE_SIGNATURE_CLASSES for item in group)
        kinds: list[tuple[str, str]] = []
        if len(group) >= 3 and len(windows) >= 2 and len(run_ids) >= 2:
            kinds.append(("rule", RULE_CANDIDATE_STATUS))
            if is_failure:
                kinds.append(("failure", "active"))
        elif len(group) >= 2 and len(run_ids) >= 2:
            kinds.append(("failure", "active") if is_failure else ("finding", "active"))
        else:
            continue  # trace only: below every promotion threshold
        for kind, status in kinds:
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

    @property
    def review_events_path(self) -> Path:
        return self.memory_root / _REVIEW_EVENTS_FILE

    @property
    def _lock_path(self) -> Path:
        # ONE sidecar lock file serializes every read-then-append critical
        # section this store instance performs (observation append, the
        # promote read-decide-append cycle, and review-event append). SE-ix /
        # S2-F10: sharing one lock keeps cross-file interleavings (e.g. a
        # promote() read racing an observation append) impossible, not merely
        # each file's own append serialized in isolation.
        return self.memory_root / _LOCK_FILE

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
            data_window=redact_free_text(data_window),
            failure_class=redact_free_text(failure_class),
            evidence_ref=evidence_ref,
            scope=redact_free_text(scope),
        )
        # Critical section (S2-F10): serialize this append against a
        # concurrent promote_pending() read of the same file under the SAME
        # store-wide lock, so a reader can never observe a torn write.
        with _advisory_file_lock(self._lock_path):
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
        new observations arrived. NOTE: this method wraps the pure
        :func:`promote` function's read-decide-append cycle in the store's
        advisory lock; :func:`promote` itself stays lock-free and pure (S2-F10
        locks the STORE method, never the pure policy function).
        """

        appended: list[dict[str, Any]] = []
        with _advisory_file_lock(self._lock_path):
            decisions = promote(self._read_observations())
            for decision in decisions:
                row = self._row_for_decision(decision)
                if row is not None:
                    _append_jsonl(self.path_for(decision.kind), row)
                    appended.append(row)
        return tuple(appended)

    def read_recent(self, kind: str, limit: int = 5) -> tuple[dict[str, Any], ...]:
        """Latest live rows for ``kind``: superseded rows dropped, newest first."""

        live_by_signature = self._live_rows_by_signature(kind)
        ordered = sorted(live_by_signature.values(), key=lambda row: str(row.get("last_seen") or ""), reverse=True)
        return tuple(ordered[: max(limit, 0)])

    def list_promoted(self, kind: str) -> tuple[dict[str, Any], ...]:
        """Every live row for ``kind``, newest first, uncapped.

        Unlike :meth:`read_recent` (bounded, for the LLM prompt feed) this is
        the CLI/review listing path: it must show every live row so a
        reviewer or ``qf memory rules list`` can see the whole pending/active
        set, not just the 5 most recent.
        """

        live_by_signature = self._live_rows_by_signature(kind)
        return tuple(sorted(live_by_signature.values(), key=lambda row: str(row.get("last_seen") or ""), reverse=True))

    def resolve_signature_prefix(self, kind: str, prefix: str) -> dict[str, Any]:
        """Resolve ``prefix`` to exactly one live row's signature for ``kind``.

        An exact signature match always wins outright (so pasting a full
        signature never trips over another signature that happens to extend
        it as a prefix). Otherwise the prefix must match exactly one live
        signature; zero or multiple matches raise ``ValueError`` naming the
        candidates, which is the CLI's anti-fat-finger confirmation (R3): no
        interactive prompts, an ambiguous or absent prefix simply fails.
        """

        if not prefix.strip():
            raise ValueError("signature prefix must not be empty")
        rows = self.list_promoted(kind)
        exact = [row for row in rows if str(row.get("signature") or "") == prefix]
        if len(exact) == 1:
            return exact[0]
        candidates = [row for row in rows if str(row.get("signature") or "").startswith(prefix)]
        if not candidates:
            raise ValueError(f"no {kind} signature matches prefix {prefix!r}")
        if len(candidates) > 1:
            matched = ", ".join(sorted(str(row.get("signature") or "") for row in candidates))
            raise ValueError(f"ambiguous {kind} signature prefix {prefix!r} matches: {matched}")
        return candidates[0]

    def _live_rows_by_signature(self, kind: str) -> dict[str, dict[str, Any]]:
        rows = _read_jsonl(self.path_for(kind))
        superseded = {row.get("supersedes") for row in rows if row.get("supersedes")}
        live_by_signature: dict[str, dict[str, Any]] = {}
        for row in rows:
            if row.get("entry_id") in superseded:
                continue
            if str(row.get("status") or "") not in _ACTIVE_ROW_STATUSES:
                continue
            live_by_signature[str(row.get("signature") or row.get("entry_id"))] = row
        return live_by_signature

    # ------------------------------------------------------------------
    # Review events (SE-iii): append-only human governance decisions layered
    # OVER promoted rows. Rows are never mutated; "effective" state is always
    # derived fresh from the latest valid event per signature.
    # ------------------------------------------------------------------

    def record_review_event(
        self,
        *,
        target_kind: str,
        target_signature: str,
        reviewed_entry_id: str,
        action: str,
        actor: str,
        rationale: str = "",
        decided_at: str | None = None,
        supersedes: str = "",
    ) -> MemoryReviewEvent:
        """Append one review decision. Exact-payload replays are idempotent:
        an event whose id already exists on disk is dropped, not duplicated.
        """

        event = MemoryReviewEvent(
            target_kind=target_kind,
            target_signature=target_signature,
            reviewed_entry_id=reviewed_entry_id,
            action=action,
            actor=redact_free_text(actor),
            rationale=redact_free_text(rationale),
            decided_at=decided_at if decided_at is not None else _utc_now_iso(),
            supersedes=supersedes,
        )
        event_id = event.event_id()
        with _advisory_file_lock(self._lock_path):
            if not self._event_id_recorded(event_id):
                _append_jsonl(self.review_events_path, {"event_id": event_id, **event.to_dict()})
        return event

    def rule_activation_events(self) -> dict[str, dict[str, Any]]:
        """Latest 'rule' review event per signature, as dicts.

        Richer than :meth:`rule_states`: callers that need more than the
        derived active/inactive label (e.g. sorting by activation recency)
        read ``decided_at``/``actor``/``rationale`` from here.
        """

        return {
            signature: {"event_id": event.event_id(), **event.to_dict()}
            for signature, event in self._latest_events_by_signature(frozenset({"rule"})).items()
        }

    def rule_states(self) -> dict[str, str]:
        """Effective activation state per rule signature: {signature: "active"|"inactive"}.

        Derived ONLY from the latest valid review event per signature
        (activate -> active, deactivate -> inactive). A signature absent from
        this mapping has never been reviewed and defaults to "inactive" —
        callers must treat a missing key as inactive, never as active. This
        is the ONLY path to "active": promoted rule ROWS keep status
        ``needs_human_review`` forever and are never consulted here (FP-2).
        """

        return {
            signature: ("active" if event["action"] == "activate" else "inactive")
            for signature, event in self.rule_activation_events().items()
        }

    def retired_signatures(self, target_kind: str) -> frozenset[str]:
        """Signatures whose latest valid review event for ``target_kind`` is
        'retire' (never 'unretire'). ``target_kind`` must be 'finding' or
        'failure' -- scoped per kind so retiring a finding can never also
        hide an unrelated failure row that happens to share the signature
        string (promote() can mint the same signature as both a rule and a
        failure row for one signature group; kind-scoping keeps retirement
        from unifying across kinds it was never decided for).
        """

        if target_kind not in _RETIRE_TARGET_KINDS:
            raise ValueError(
                f"retired_signatures target_kind must be one of {sorted(_RETIRE_TARGET_KINDS)}, got {target_kind!r}"
            )
        latest = self._latest_events_by_signature(frozenset({target_kind}))
        return frozenset(signature for signature, event in latest.items() if event.action == "retire")

    def _read_review_events(self) -> tuple[MemoryReviewEvent, ...]:
        events: list[MemoryReviewEvent] = []
        for row in _read_jsonl(self.review_events_path):
            events.append(
                MemoryReviewEvent(
                    target_kind=str(row.get("target_kind") or ""),
                    target_signature=str(row.get("target_signature") or ""),
                    reviewed_entry_id=str(row.get("reviewed_entry_id") or ""),
                    action=str(row.get("action") or ""),
                    actor=str(row.get("actor") or ""),
                    rationale=str(row.get("rationale") or ""),
                    decided_at=str(row.get("decided_at") or ""),
                    supersedes=str(row.get("supersedes") or ""),
                )
            )
        return tuple(events)

    def _event_id_recorded(self, event_id: str) -> bool:
        for row in _read_jsonl(self.review_events_path):
            if row.get("event_id") == event_id:
                return True
        return False

    def _latest_events_by_signature(self, target_kinds: frozenset[str]) -> dict[str, MemoryReviewEvent]:
        """Latest-by-``decided_at`` review event per signature, restricted to
        ``target_kinds``. An event named by a later event's ``supersedes`` is
        excluded from contention first, mirroring how promoted rows use
        ``supersedes`` to identify the live end of a correction chain even
        when timestamps alone would be ambiguous (e.g. clock skew across
        processes); the remaining ("live") events per signature are then
        ordered by ``decided_at`` with ``event_id`` as a stable tiebreak.
        """

        scoped = [event for event in self._read_review_events() if event.target_kind in target_kinds]
        superseded_ids = {event.supersedes for event in scoped if event.supersedes}
        live = [event for event in scoped if event.event_id() not in superseded_ids]
        ordered = sorted(live, key=lambda event: (event.decided_at, event.event_id()))
        latest: dict[str, MemoryReviewEvent] = {}
        for event in ordered:
            latest[event.target_signature] = event  # later in sorted order wins
        return latest

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
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"timestamp must be ISO format: {value!r}") from exc
    if parsed.tzinfo is None:
        # Consistent with the goal store: naive timestamps are ambiguous
        # evidence and are rejected instead of being silently assumed UTC.
        raise ValueError(f"timestamp must be timezone-aware: {value!r}")


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

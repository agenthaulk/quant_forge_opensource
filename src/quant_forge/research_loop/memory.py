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
(FP-2): promotion structurally cannot mint activity. A signature that merely
REACHES the rule tier (a live row exists, reviewed or not) already leaves the
passive finding/failure feed — see :meth:`ResearchMemoryStore.
rule_tier_signatures` and ``context_builder._memory_items``.

"Latest valid event" (P4a rework, dual-phase review): ordering is FILE APPEND
ORDER under the store's advisory lock (a monotonic revision index), never
``decided_at`` — same-second or out-of-order writer clocks must not be able
to invert the true decision sequence (a same-second activate-then-deactivate
must yield inactive). ``decided_at`` is DISPLAY metadata only. An event
additionally only counts if ``reviewed_entry_id`` equals the row CURRENTLY
live for ``(target_kind, target_signature)``: once a row is superseded (new
observations promote a new version), any event bound to the prior version
lapses — the signature reverts to unreviewed until a human re-reviews the new
content. ``supersedes`` is audit-trail metadata (validated same-(kind,
signature); :meth:`ResearchMemoryStore.record_review_event` auto-populates it
with the current live event when the caller leaves it unset); it plays no
role in state derivation, only in the write-time referential-integrity check.

Trusted boundary: this module trusts whoever already has write access to
``artifact_root`` — that is the existing local-first security model (see
``AGENTS.md``), unchanged here. Read-side validation in this module (event
schema/kind/action checks, row binding, readback fingerprint verification,
and the trailing-line-only JSON quarantine below) defends against ON-DISK
CORRUPTION and a FOREIGN (non-Python) writer producing malformed rows. It is
not a defense against another in-process caller that already holds a
`ResearchMemoryStore` instance: that caller is, by definition, already
inside the trust boundary. In particular (R2 rework item R2-6, Fable
ruling): in-process forgery of ``ResearchContext.active_rules`` — a caller
constructing a ``ResearchContext`` with fabricated ``active_rules`` content
directly, bypassing this module's read path entirely — is OUT OF THREAT
MODEL. That requires the same in-process code execution as patching
:func:`promote` itself; this module's validation was never designed to
defend against a caller already inside the trust boundary, and no amount of
additional shape-only checking changes that. What this module DOES provide
against that scenario is traceability, not prevention: every active_rules
item :meth:`ResearchMemoryStore.effective_active_rules` returns (and
``llm.py`` forwards to the prompt) carries its ``event_id`` and
``reviewed_entry_id``, so any rule accepted into a prompt is traceable to
the exact review event that activated it in trace/debug output. No further
"authentication theater" is added beyond that.

Single-host writers only: the advisory lock is a same-host ``fcntl.flock``;
it does not coordinate across hosts or across a cloud-sync tool's own
conflict resolution. :meth:`ResearchMemoryStore.__init__` warns loudly if it
finds a Dropbox-style "conflicted copy" sidecar file under the memory
directory (see ``docs/configuration.md`` for the operational recommendation).
There is no distributed locking here — that is explicitly out of scope.

Drop-stats surfacing beyond the ``llm.py`` active_rules prompt channel's own
returned stats mapping (e.g. persisting counts into a queryable/disclosed
artifact) is deferred to SE-P5's ``planning_influence_snapshot`` disclosure
contract by design; it does not belong in this kernel module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
import logging
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Sequence

# ``advisory_file_lock`` is a public export of lineage.store (promoted from a
# private name in the P4a rework, item 12) but this reuses the SAME lock
# primitive on purpose (SE-ix / S2-F10): the lineage store already
# established the pattern of serializing read-then-append critical sections
# on JSONL files with a sidecar ``fcntl.flock``; goals.py similarly imports
# lineage.store's private relative-path guard rather than forking a second
# implementation.
from quant_forge.lineage.store import advisory_file_lock, canonical_fingerprint, redact_free_text

logger = logging.getLogger(__name__)

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
# SE-P2: one append-only ResearchOutcome.to_record() envelope per outcome_id
# (see the "Outcome ledger" section below).
_OUTCOMES_LEDGER_FILE = "outcomes_ledger.jsonl"
_LOCK_FILE = "memory.lock"
_EVENT_ID_RE = re.compile(r"^[0-9a-f]{64}$")
_CONFLICTED_COPY_MARKER = "conflicted copy"


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
    only an EXACT byte-identical payload collapses to the same id.
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
        self._warn_of_conflicted_copies()

    @property
    def observations_path(self) -> Path:
        return self.memory_root / _OBSERVATIONS_FILE

    @property
    def review_events_path(self) -> Path:
        return self.memory_root / _REVIEW_EVENTS_FILE

    @property
    def outcomes_ledger_path(self) -> Path:
        return self.memory_root / _OUTCOMES_LEDGER_FILE

    @property
    def _lock_path(self) -> Path:
        # ONE sidecar lock file serializes every read-then-append critical
        # section this store instance performs: observation append, the
        # promote read-decide-append cycle, review-event append, AND (P4a
        # rework item 7) every public state reader below. Locking reads too
        # means a reader can never observe a row-file / event-file pair that
        # straddles a concurrent writer (SE-ix / S2-F10).
        return self.memory_root / _LOCK_FILE

    def path_for(self, kind: str) -> Path:
        if kind not in _KIND_FILES:
            raise ValueError(f"unknown memory kind: {kind!r}")
        return self.memory_root / _KIND_FILES[kind]

    def _warn_of_conflicted_copies(self) -> None:
        """Loud, specific warning if a cloud-sync conflict sidecar is present
        under the memory directory (P4a rework item 9).

        Dropbox (and similar tools) resolve a same-file write conflict by
        creating a SECOND physical file, conventionally named like
        ``rules (name's conflicted copy 2026-07-14).jsonl``, rather than
        merging. This store's advisory lock only serializes writers on ONE
        host; it cannot detect or prevent a synced conflicted-copy fork of
        the append-only history. There is no distributed locking here
        (explicitly out of scope) — the only honest mitigation available is
        to name the exact file loudly so an operator notices, per the
        single-host-writer recommendation in ``docs/configuration.md``.
        """

        if not self.memory_root.exists():
            return
        for candidate in sorted(self.memory_root.iterdir()):
            if candidate.is_file() and _CONFLICTED_COPY_MARKER in candidate.name:
                logger.warning(
                    "Dropbox-style sync-conflict file detected under research_memory: %s -- this store "
                    "assumes a SINGLE host writer and cannot merge a synced conflicted copy; move the "
                    "memory root off a synced path or resolve the conflict manually before trusting "
                    "activation/retirement state.",
                    candidate.name,
                )

    # ------------------------------------------------------------------
    # Observations + promotion.
    # ------------------------------------------------------------------

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

        observation = self._normalized_observation(
            signature=signature,
            statement=statement,
            run_id=run_id,
            data_window=data_window,
            failure_class=failure_class,
            evidence_ref=evidence_ref,
            observed_at=observed_at,
            scope=scope,
        )
        # Critical section (S2-F10): serialize this append against a
        # concurrent promote_pending() read of the same file under the SAME
        # store-wide lock, so a reader can never observe a torn write.
        with advisory_file_lock(self._lock_path):
            self._append_observation_unlocked(observation)
        return observation

    def _normalized_observation(
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
        """The ONE normalization applied to every recorded observation --
        shared by :meth:`record_observation` and :meth:`ingest_outcome_rows`
        so the two write paths can never drift apart."""

        return MemoryObservation(
            signature=redact_free_text(signature),
            statement=redact_free_text(statement),
            run_id=run_id,
            observed_at=observed_at if observed_at is not None else _utc_now_iso(),
            data_window=redact_free_text(data_window),
            failure_class=redact_free_text(failure_class),
            evidence_ref=evidence_ref,
            scope=redact_free_text(scope),
        )

    def _append_observation_unlocked(self, observation: MemoryObservation) -> None:
        _append_jsonl(
            self.observations_path,
            {"schema_version": RESEARCH_MEMORY_SCHEMA_VERSION, "record": "observation", **observation.to_dict()},
        )

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
        with advisory_file_lock(self._lock_path):
            decisions = promote(self._read_observations())
            for decision in decisions:
                row = self._row_for_decision(decision)
                if row is not None:
                    _append_jsonl(self.path_for(decision.kind), row)
                    appended.append(row)
        return tuple(appended)

    # ------------------------------------------------------------------
    # Public state readers (P4a rework item 7: every one of these takes the
    # store lock so a read can never straddle a concurrent writer). Each
    # delegates to an ``_..._unlocked`` internal so composed operations
    # (e.g. :meth:`resolve_validate_append`) can hold the lock ONCE across
    # several steps without nesting ``advisory_file_lock`` (which would
    # deadlock: two separate ``open()`` calls on the same lock file, even
    # from the same thread, are two independent flock holders).
    # ------------------------------------------------------------------

    def read_recent(self, kind: str, limit: int = 5) -> tuple[dict[str, Any], ...]:
        """Latest live rows for ``kind``: superseded rows dropped, newest first."""

        with advisory_file_lock(self._lock_path):
            return self._read_recent_unlocked(kind, limit)

    def list_promoted(self, kind: str) -> tuple[dict[str, Any], ...]:
        """Every live row for ``kind``, newest first, uncapped.

        Unlike :meth:`read_recent` (bounded, for the LLM prompt feed) this is
        the CLI/review listing path: it must show every live row so a
        reviewer or ``qf memory rules list`` can see the whole pending/active
        set, not just the 5 most recent.
        """

        with advisory_file_lock(self._lock_path):
            return self._list_promoted_unlocked(kind)

    def resolve_signature_prefix(self, kind: str, prefix: str) -> dict[str, Any]:
        """Resolve ``prefix`` to exactly one live row's signature for ``kind``.

        An exact signature match always wins outright (so pasting a full
        signature never trips over another signature that happens to extend
        it as a prefix). Otherwise the prefix must match exactly one live
        signature; zero or multiple matches raise ``ValueError`` naming the
        candidates, which is the CLI's anti-fat-finger confirmation (R3): no
        interactive prompts, an ambiguous or absent prefix simply fails.

        This alone does not protect against a concurrent writer changing the
        row between resolution and a later separate append; callers that
        need the two to happen atomically should use
        :meth:`resolve_validate_append` instead.
        """

        with advisory_file_lock(self._lock_path):
            return self._resolve_signature_prefix_unlocked(kind, prefix)

    def rule_states(self) -> dict[str, str]:
        """Effective activation state per rule signature: {signature: "active"|"inactive"}.

        Derived ONLY from the latest valid, row-bound review event per
        signature (activate -> active, deactivate -> inactive; "latest" is
        FILE APPEND ORDER, never ``decided_at`` — see the module docstring).
        A signature absent from this mapping has never been (validly)
        reviewed and defaults to "inactive" — callers must treat a missing
        key as inactive, never as active. This is the ONLY path to "active":
        promoted rule ROWS keep status ``needs_human_review`` forever and are
        never consulted here (FP-2).
        """

        with advisory_file_lock(self._lock_path):
            events = self._latest_events_by_signature_unlocked("rule")
        return {
            signature: ("active" if event.action == "activate" else "inactive") for signature, event in events.items()
        }

    def retired_signatures(self, target_kind: str) -> frozenset[str]:
        """Signatures whose latest valid, row-bound review event for
        ``target_kind`` is 'retire' (never 'unretire'). ``target_kind`` must
        be 'finding' or 'failure' -- scoped per kind so retiring a finding
        can never also hide an unrelated failure row that happens to share
        the signature string (promote() can mint the same signature as both
        a rule and a failure row for one signature group; kind-scoping keeps
        retirement from unifying across kinds it was never decided for).
        """

        if target_kind not in _RETIRE_TARGET_KINDS:
            raise ValueError(
                f"retired_signatures target_kind must be one of {sorted(_RETIRE_TARGET_KINDS)}, got {target_kind!r}"
            )
        with advisory_file_lock(self._lock_path):
            events = self._latest_events_by_signature_unlocked(target_kind)
        return frozenset(signature for signature, event in events.items() if event.action == "retire")

    def rule_tier_signatures(self) -> frozenset[str]:
        """Every signature with a LIVE rule-tier row, active or pending
        (P4a rework item 1: pre-activation silencing). A signature reaching
        the rule tier at all has "graduated" out of the passive finding/
        failure feed until a human reviews it — unlike :meth:`rule_states`
        this makes no distinction between an activated row and a
        merely-pending one; both silence their signature's lower tiers.
        """

        with advisory_file_lock(self._lock_path):
            return frozenset(self._live_rows_by_signature("rule").keys())

    def effective_active_rules(self) -> tuple[dict[str, Any], ...]:
        """Atomic, store-owned read of every rule signature whose LATEST
        event is CURRENTLY row-bound and says ``activate`` (R2 rework item
        R2-1, MAJOR: closes the split-snapshot race).

        Rows and events are read under ONE lock hold via
        :meth:`_rule_snapshot_unlocked`. The bug this fixes: the previous
        design read events (one locked call) and then separately read rows
        (a second locked call); a ``promote_pending()`` landing a NEW
        superseding row between the two calls could pair a STALE activation
        event with UNREVIEWED new row content, since the second call only
        filtered rows by signature membership, never re-checked the
        binding. ``context_builder._active_rules()`` and the CLI list/label
        paths (:meth:`rule_review_snapshot`) consume ONLY the atomic
        snapshot this method (and its sibling) are built from -- no
        separately-locked row read is ever combined with a separately-read
        event to answer "is this rule effectively active".

        Each item is the row's own fields PLUS:

        - ``event_id``: the binding event's content-identity fingerprint
          (R2-6: forwarded all the way to the LLM prompt so any accepted
          rule is traceable to its event).
        - ``reviewed_entry_id``: the row entry_id the event was bound to
          (== the row's own current ``entry_id``, by construction).
        - ``activation_seq``: the event's index in the file-append-order
          sequence of VALID events (R2-3's ranking key -- NEVER
          ``decided_at``, which stays display metadata only).
        - ``decided_at``: display metadata only.
        """

        with advisory_file_lock(self._lock_path):
            snapshot = self._rule_snapshot_unlocked()
        results: list[dict[str, Any]] = []
        for info in snapshot.values():
            if info["state"] != "active":
                continue
            results.append(
                {
                    **info["row"],
                    "event_id": info["event_id"],
                    "reviewed_entry_id": info["reviewed_entry_id"],
                    "activation_seq": info["activation_seq"],
                    "decided_at": info["decided_at"],
                }
            )
        return tuple(results)

    def rule_review_snapshot(self) -> dict[str, dict[str, Any]]:
        """Atomic, full state classification of every LIVE rule row (R2-7):
        ``{signature: {"row": ..., "state": ..., "event_id": ...,
        "reviewed_entry_id": ..., "activation_seq": ..., "decided_at": ...}}``
        where ``state`` is one of:

        - ``"active"``: latest event for this signature is row-bound and
          says ``activate``.
        - ``"deactivated"``: latest event for this signature is row-bound
          and says ``deactivate``.
        - ``"lapsed_pending_re_review"``: at least one event for this
          signature is bound to a REAL row it once held (its
          ``reviewed_entry_id`` matches some entry_id this signature's
          append-only history actually contains), but none is bound to the
          row's CURRENT content -- the row was genuinely superseded since
          the last review (R3-2: "row content changed" is therefore always
          true for this state).
        - ``"never_reviewed"``: either no event has ever targeted this
          signature, or every event that has is dangling -- its
          ``reviewed_entry_id`` never matched ANY row this signature has
          ever had, live or superseded (R3-2: a forged/corrupted/stale
          reference is not treated as a real review that later lapsed;
          such events are logged as warnings, not surfaced as a false
          "content changed" claim).

        Built from the EXACT SAME single-lock-hold traversal
        :meth:`effective_active_rules` uses (:meth:`_rule_snapshot_unlocked`),
        so the CLI's full listing and the active_rules steering channel can
        never observe two different snapshots of the same instant -- the CLI
        list/label paths (R2-1, R2-7) consume ONLY this method (plus
        :meth:`effective_active_rules`), never a second separately-locked
        row read.
        """

        with advisory_file_lock(self._lock_path):
            return self._rule_snapshot_unlocked()

    def _all_entry_ids_by_signature(self, kind: str) -> dict[str, set[str]]:
        """Every entry_id EVER recorded for a signature in ``kind``'s file,
        LIVE or superseded (R3 rework item R3-2). ``kind``'s JSONL is
        append-only, so a superseded row's entry_id is still on disk; this
        reads the raw rows directly (not filtered through
        :meth:`_live_rows_by_signature`'s supersede-chain/status logic) so a
        signature's FULL history of ever-promoted content is visible. Used
        by :meth:`_rule_snapshot_unlocked` to tell apart a dangling review
        event (``reviewed_entry_id`` never matched ANY row this signature has
        ever had -- a forged, corrupted, or copy-pasted-wrong reference) from
        a lapsed one (matched a row that is real but has since been
        superseded) -- only the latter makes "row content changed" a TRUE
        statement.
        """

        rows = _read_jsonl(self.path_for(kind))
        by_signature: dict[str, set[str]] = {}
        for row in rows:
            signature = str(row.get("signature") or "")
            entry_id = str(row.get("entry_id") or "")
            if signature and entry_id:
                by_signature.setdefault(signature, set()).add(entry_id)
        return by_signature

    def _rule_snapshot_unlocked(self) -> dict[str, dict[str, Any]]:
        """Shared snapshot builder (callers must already hold the lock): for
        every LIVE rule row, resolve its currently-bound event (if any) and
        classify its state, all from ONE traversal of the SAME
        already-in-memory rows + events -- :meth:`effective_active_rules`
        and :meth:`rule_review_snapshot` are thin filters/formatters over
        this, never a second disk read (R2-1).

        R3-2: an event that fails to bind the CURRENT row is further split
        into two genuinely different cases rather than one catch-all
        "reviewed at some point" flag:

        - the event's ``reviewed_entry_id`` matches some OTHER row this
          signature has held in the past (rules.jsonl is append-only, so a
          superseded row's entry_id is still on disk) -- the row's content
          really did change since that review, so
          ``"lapsed_pending_re_review"`` and its "row content changed" label
          (see the CLI's ``_MEMORY_RULE_STATE_LABELS``) are true by
          construction;
        - the event's ``reviewed_entry_id`` never matched ANY row this
          signature has EVER had -- this is not a lifecycle transition, so
          it is treated as if the signature had never been reviewed
          (``"never_reviewed"``) rather than falsely implying content
          changed that never existed; a warning is logged so the dangling
          reference is still visible operationally.
        """

        live_rows = self._live_rows_by_signature("rule")
        all_entry_ids = self._all_entry_ids_by_signature("rule")
        events = self._read_review_events_unlocked()  # natural file order = append order
        bound_by_signature: dict[str, tuple[int, MemoryReviewEvent]] = {}
        lapsed_signatures: set[str] = set()  # bound to a REAL, now-superseded row at some point
        for index, event in enumerate(events):
            if event.target_kind != "rule":
                continue
            live_row = live_rows.get(event.target_signature)
            current_entry_id = str(live_row.get("entry_id")) if live_row is not None else None
            if current_entry_id is not None and event.reviewed_entry_id == current_entry_id:
                bound_by_signature[event.target_signature] = (index, event)  # later append order overwrites earlier
                continue
            if event.reviewed_entry_id in all_entry_ids.get(event.target_signature, ()):
                lapsed_signatures.add(event.target_signature)
            else:
                logger.warning(
                    "dangling review event %s for rule/%s: reviewed_entry_id %r never matched any row ever "
                    "recorded for this signature (treated as never_reviewed, not lapsed)",
                    event.event_id()[:12],
                    event.target_signature,
                    event.reviewed_entry_id,
                )

        snapshot: dict[str, dict[str, Any]] = {}
        for signature, row in live_rows.items():
            bound = bound_by_signature.get(signature)
            if bound is not None:
                seq, event = bound
                snapshot[signature] = {
                    "row": row,
                    "state": "active" if event.action == "activate" else "deactivated",
                    "event_id": event.event_id(),
                    "reviewed_entry_id": event.reviewed_entry_id,
                    "activation_seq": seq,
                    "decided_at": event.decided_at,
                }
            else:
                state = "lapsed_pending_re_review" if signature in lapsed_signatures else "never_reviewed"
                snapshot[signature] = {
                    "row": row,
                    "state": state,
                    "event_id": "",
                    "reviewed_entry_id": "",
                    "activation_seq": None,
                    "decided_at": "",
                }
        return snapshot

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
        supersedes: str | None = None,
    ) -> MemoryReviewEvent:
        """Append one review decision.

        ``supersedes`` defaults to ``None``, meaning "auto-populate with the
        current live event for this (target_kind, target_signature), if any"
        (P4a rework item 3) — pass ``""`` explicitly to force a fresh,
        unchained event even when a prior one exists. An explicitly-supplied
        non-empty ``supersedes`` must reference an existing event for the
        SAME (target_kind, target_signature); a cross-signature claim raises
        ``ValueError``.

        Idempotent replay: a call whose caller-supplied fields (excluding
        the derived ``supersedes``) exactly match the CURRENT last event for
        this signature returns that event unchanged rather than appending a
        new one — this preserves "exact replays are dropped" even though
        auto-populated ``supersedes`` would otherwise differ between the
        first call and an immediate identical retry. A byte-identical event
        with an EXPLICIT ``supersedes`` matching an existing row is also
        deduplicated (defense in depth, scanning the full log by event id).

        Callers that need prefix resolution and this append to happen
        atomically (no TOCTOU window between the two) should use
        :meth:`resolve_validate_append` instead.
        """

        with advisory_file_lock(self._lock_path):
            return self._record_review_event_unlocked(
                target_kind=target_kind,
                target_signature=target_signature,
                reviewed_entry_id=reviewed_entry_id,
                action=action,
                actor=actor,
                rationale=rationale,
                decided_at=decided_at,
                supersedes=supersedes,
            )

    def resolve_validate_append(
        self,
        *,
        target_kind: str,
        prefix: str,
        action: str,
        actor: str,
        rationale: str = "",
        decided_at: str | None = None,
    ) -> MemoryReviewEvent:
        """Atomic CLI-facing operation (P4a rework item 8): prefix
        resolution, row binding, and event append all happen inside ONE
        lock hold.

        Two processes racing to review the same prefix cannot interleave
        between "resolve the row" and "append the event": whichever
        acquires the lock first resolves against the row state AT THAT
        MOMENT and appends immediately, still holding the lock, so the
        second process either observes the first's fully-applied result
        before it starts (clean sequential behavior — no torn state, no
        exception, the store's single append-order lock decides who is
        "last" deterministically) or, if its request is byte-identical to
        what just landed, gets the SAME event back via the idempotent-replay
        path. Prefer this over separately calling
        :meth:`resolve_signature_prefix` then :meth:`record_review_event`,
        which leaves that exact TOCTOU window open.
        """

        with advisory_file_lock(self._lock_path):
            row = self._resolve_signature_prefix_unlocked(target_kind, prefix)
            return self._record_review_event_unlocked(
                target_kind=target_kind,
                target_signature=str(row.get("signature") or ""),
                reviewed_entry_id=str(row.get("entry_id") or ""),
                action=action,
                actor=actor,
                rationale=rationale,
                decided_at=decided_at,
                supersedes=None,
            )

    # ------------------------------------------------------------------
    # Outcome ledger (SE-P2 ingress sink; SE-ii/SE-ix/SE-P5): one
    # append-only JSONL envelope per ``ResearchOutcome.to_record()``, keyed
    # by ``outcome_id`` for exact replay-drop. Lives beside rules/findings/
    # failures/observations under THIS store's OWN ``research_memory`` root,
    # so dual-domain isolation (SE-i) is automatic: a plugin's own
    # ``ResearchMemoryStore`` instance (a different ``artifact_root``) keeps
    # its own ledger, and this store never reads or writes another root.
    # This kernel only appends/reads the envelope; ``outcomes.py``'s frozen
    # ``ResearchOutcome`` contract already validated its shape before it
    # reaches here, so this mirrors the plain observations/promotion append
    # style (reuse ``_read_jsonl``/``_append_jsonl`` as-is) rather than the
    # heavier review-event schema/tamper-recompute validation: ``outcome_id``
    # IS already the recomputed content fingerprint the frozen contract
    # minted, so a second recompute here would just duplicate ``outcomes.
    # ResearchOutcome.outcome_id()``'s own logic (do not hand-roll a second
    # reader/writer).
    # ------------------------------------------------------------------

    def record_outcome_envelope(self, record: Mapping[str, Any]) -> bool:
        """Append one ``ResearchOutcome.to_record()`` envelope; exact replay-drop.

        ``record["outcome_id"]`` is the content-identity key (SE-ii): a
        caller resubmitting the EXACT SAME envelope (e.g. retrying a crashed
        ingest) is dropped -- nothing is appended and this returns False --
        while any outcome with a NEW id (a genuinely new measurement, even
        of the same evidence run) is appended and this returns True. The
        membership check and the append happen inside ONE lock hold so two
        concurrent ingests of the same ``outcome_id`` cannot both observe
        "absent" and both append.
        """

        outcome_id = str(record.get("outcome_id") or "")
        if not outcome_id:
            raise ValueError("outcome record is missing outcome_id")
        with advisory_file_lock(self._lock_path):
            if outcome_id in self._known_outcome_ids_unlocked():
                return False
            _append_jsonl(self.outcomes_ledger_path, record)
            return True

    def known_outcome_ids(self) -> frozenset[str]:
        """Every ``outcome_id`` currently on the ledger (locked read)."""

        with advisory_file_lock(self._lock_path):
            return self._known_outcome_ids_unlocked()

    def ingest_outcome_rows(
        self, record: Mapping[str, Any], observations: Sequence[MemoryObservation]
    ) -> tuple[bool, int]:
        """One critical section for the whole SE-P2 sink write (RV2-F3).

        The known-``outcome_id`` replay check, every derived observation
        append, and the envelope append (LAST -- the completion marker)
        happen under ONE store-lock hold, so two concurrent ingests of the
        same outcome can never both observe "absent" and double-append the
        observations the way the sink's earlier check-then-write sequence
        (three separate lock acquisitions) could. Returns
        ``(recorded, appended_observation_count)``: ``(False, 0)`` for a
        replay, which appends NOTHING. Promotion stays OUTSIDE this method
        (its own lock hold; a pure, deterministic function of the full
        observation set) -- holding one lock across append+promote would
        add latency for no correctness gain and re-nest the advisory lock
        :meth:`promote_pending` already takes.

        Note the crash-window contract is unchanged from the sink's
        docstring: a crash between the observation appends and the envelope
        append is impossible WITHIN this method's single hold only for
        concurrency, not for process death -- a killed process mid-method
        still leaves observations without the envelope marker, and the
        retry re-appends both (duplicates stay scientifically inert under
        promote's ``(signature, run_id)`` cap).
        """

        outcome_id = str(record.get("outcome_id") or "")
        if not outcome_id:
            raise ValueError("outcome record is missing outcome_id")
        with advisory_file_lock(self._lock_path):
            if outcome_id in self._known_outcome_ids_unlocked():
                return False, 0
            for observation in observations:
                self._append_observation_unlocked(
                    self._normalized_observation(
                        signature=observation.signature,
                        statement=observation.statement,
                        run_id=observation.run_id,
                        data_window=observation.data_window,
                        failure_class=observation.failure_class,
                        evidence_ref=observation.evidence_ref,
                        observed_at=observation.observed_at,
                        scope=observation.scope,
                    )
                )
            _append_jsonl(self.outcomes_ledger_path, record)
            return True, len(observations)

    def outcomes_revision(self) -> int:
        """Monotonic append index: count of valid ledger rows.

        This is SE-P5's ``as_of`` snapshot-fingerprint input: the ledger only
        ever grows (append-only; replay-dropped duplicates never append), so
        two priors snapshots taken at the same revision saw the exact same
        ledger content, and a genuinely new outcome always strictly
        increases it.
        """

        with advisory_file_lock(self._lock_path):
            return len(_read_jsonl(self.outcomes_ledger_path))

    def _known_outcome_ids_unlocked(self) -> frozenset[str]:
        return frozenset(
            str(row["outcome_id"]) for row in _read_jsonl(self.outcomes_ledger_path) if row.get("outcome_id")
        )

    # ------------------------------------------------------------------
    # Unlocked internals: never self-lock (callers hold the lock already).
    # ------------------------------------------------------------------

    def _read_recent_unlocked(self, kind: str, limit: int) -> tuple[dict[str, Any], ...]:
        live_by_signature = self._live_rows_by_signature(kind)
        ordered = sorted(live_by_signature.values(), key=lambda row: str(row.get("last_seen") or ""), reverse=True)
        return tuple(ordered[: max(limit, 0)])

    def _list_promoted_unlocked(self, kind: str) -> tuple[dict[str, Any], ...]:
        live_by_signature = self._live_rows_by_signature(kind)
        return tuple(sorted(live_by_signature.values(), key=lambda row: str(row.get("last_seen") or ""), reverse=True))

    def _resolve_signature_prefix_unlocked(self, kind: str, prefix: str) -> dict[str, Any]:
        if not prefix.strip():
            raise ValueError("signature prefix must not be empty")
        rows = self._list_promoted_unlocked(kind)
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

    def _record_review_event_unlocked(
        self,
        *,
        target_kind: str,
        target_signature: str,
        reviewed_entry_id: str,
        action: str,
        actor: str,
        rationale: str,
        decided_at: str | None,
        supersedes: str | None,
    ) -> MemoryReviewEvent:
        redacted_actor = redact_free_text(actor)
        redacted_rationale = redact_free_text(rationale)
        resolved_decided_at = decided_at if decided_at is not None else _default_review_decided_at()

        last = self._last_event_for_signature_unlocked(target_kind, target_signature)

        # Idempotent replay (excluding the DERIVED `supersedes` field from
        # the comparison, since auto-population would otherwise make an
        # immediate identical retry compute a different value than the
        # original call did — see the docstring on record_review_event).
        if (
            last is not None
            and last.reviewed_entry_id == reviewed_entry_id
            and last.action == action
            and last.actor == redacted_actor
            and last.rationale == redacted_rationale
            and last.decided_at == resolved_decided_at
            and (supersedes is None or supersedes == last.supersedes)
        ):
            return last

        if supersedes is None:
            resolved_supersedes = last.event_id() if last is not None else ""
        else:
            resolved_supersedes = supersedes
            if resolved_supersedes:
                referenced = self._find_event_by_id_unlocked(resolved_supersedes)
                if (
                    referenced is None
                    or referenced.target_kind != target_kind
                    or referenced.target_signature != target_signature
                ):
                    raise ValueError(
                        f"supersedes {resolved_supersedes!r} must reference an existing event for the same "
                        f"(target_kind={target_kind!r}, target_signature={target_signature!r})"
                    )

        event = MemoryReviewEvent(
            target_kind=target_kind,
            target_signature=target_signature,
            reviewed_entry_id=reviewed_entry_id,
            action=action,
            actor=redacted_actor,
            rationale=redacted_rationale,
            decided_at=resolved_decided_at,
            supersedes=resolved_supersedes,
        )
        event_id = event.event_id()
        if not self._event_id_recorded_unlocked(event_id):
            _append_jsonl(self.review_events_path, {"event_id": event_id, **event.to_dict()})
        return event

    def _read_review_events_unlocked(self) -> tuple[MemoryReviewEvent, ...]:
        """Tolerant, natural-file-order read of ``activations.jsonl``, in
        REVIEW ORDER (append order): line-level JSON tolerance is the shared
        :func:`_read_jsonl` behavior (R2-5, trailing quarantine / interior
        raise); this layer adds readback-integrity checks specific to review
        events (R2-2):

        - ``schema_version`` must be PRESENT and equal to the current
          :data:`RESEARCH_MEMORY_REVIEW_SCHEMA_VERSION` exactly -- missing or
          stale is skipped-with-a-warning, never silently defaulted to
          "current" (a defaulted schema_version would let a genuinely
          different historical shape masquerade as validated-current).
        - the row must pass :class:`MemoryReviewEvent`'s own schema/kind/
          action validation -- skipped-with-a-warning, never raised (one
          corrupted or foreign-written row must not take the whole log
          down).
        - the row's stored ``event_id`` must equal the RECOMPUTED content
          fingerprint of the reconstructed event -- skipped-with-a-warning
          on mismatch (a single tampered or corrupted field would otherwise
          be silently trusted, since JSON parsing alone cannot catch it).
        """

        path = self.review_events_path
        events: list[MemoryReviewEvent] = []
        for row in _read_jsonl(path):
            schema_version = row.get("schema_version")
            if schema_version != RESEARCH_MEMORY_REVIEW_SCHEMA_VERSION:
                logger.warning(
                    "skipping review event in %s: missing or non-current schema_version %r (expected %r)",
                    path,
                    schema_version,
                    RESEARCH_MEMORY_REVIEW_SCHEMA_VERSION,
                )
                continue
            try:
                event = MemoryReviewEvent(
                    target_kind=str(row.get("target_kind") or ""),
                    target_signature=str(row.get("target_signature") or ""),
                    reviewed_entry_id=str(row.get("reviewed_entry_id") or ""),
                    action=str(row.get("action") or ""),
                    actor=str(row.get("actor") or ""),
                    rationale=str(row.get("rationale") or ""),
                    decided_at=str(row.get("decided_at") or ""),
                    supersedes=str(row.get("supersedes") or ""),
                    schema_version=schema_version,
                )
            except ValueError as exc:
                logger.warning("skipping semantically-invalid review event in %s: %s", path, exc)
                continue
            stored_event_id = row.get("event_id")
            recomputed_event_id = event.event_id()
            if stored_event_id != recomputed_event_id:
                logger.warning(
                    "skipping review event in %s: stored event_id %r does not match the recomputed "
                    "content fingerprint %r (tampered or corrupted field)",
                    path,
                    stored_event_id,
                    recomputed_event_id,
                )
                continue
            events.append(event)
        return tuple(events)

    def _event_id_recorded_unlocked(self, event_id: str) -> bool:
        return any(event.event_id() == event_id for event in self._read_review_events_unlocked())

    def _find_event_by_id_unlocked(self, event_id: str) -> MemoryReviewEvent | None:
        for event in self._read_review_events_unlocked():
            if event.event_id() == event_id:
                return event
        return None

    def _last_event_for_signature_unlocked(self, target_kind: str, target_signature: str) -> MemoryReviewEvent | None:
        last: MemoryReviewEvent | None = None
        for event in self._read_review_events_unlocked():
            if event.target_kind == target_kind and event.target_signature == target_signature:
                last = event  # natural file order: later assignment wins
        return last

    def _latest_events_by_signature_unlocked(self, kind: str) -> dict[str, MemoryReviewEvent]:
        """Latest-by-FILE-APPEND-ORDER, row-bound review event per signature
        for ``kind`` (P4a rework items 2 + 3). Iterates events in their
        natural on-disk order (the store's advisory lock guarantees this
        equals true decision order, S2-F10) so the LAST matching, row-bound
        event for a signature always wins — ``decided_at`` never enters the
        comparison, closing the same-second/clock-skew inversion the
        original decided_at-sorted design was vulnerable to.

        An event only counts if ``reviewed_entry_id`` equals the row
        CURRENTLY live for ``(kind, target_signature)``: once that row is
        superseded, the event is ignored (with a warning) and the signature
        reverts to unreviewed until re-reviewed against the new content.
        """

        live_rows = self._live_rows_by_signature(kind)
        latest: dict[str, MemoryReviewEvent] = {}
        for event in self._read_review_events_unlocked():
            if event.target_kind != kind:
                continue
            live_row = live_rows.get(event.target_signature)
            current_entry_id = str(live_row.get("entry_id")) if live_row is not None else None
            if current_entry_id is None or event.reviewed_entry_id != current_entry_id:
                logger.warning(
                    "ignoring review event %s for %s/%s: reviewed_entry_id %r no longer matches the live "
                    "row (row superseded, retracted, or the event references an unknown row)",
                    event.event_id()[:12],
                    kind,
                    event.target_signature,
                    event.reviewed_entry_id,
                )
                continue
            latest[event.target_signature] = event  # later append order overwrites earlier
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


def _default_review_decided_at() -> str:
    """UTC now, WITH microseconds (P4a rework item 3).

    Review-event ``decided_at`` is DISPLAY metadata only now — ordering is
    file append order, never this value — but truncating to whole seconds
    (as :func:`_utc_now_iso` does for observations) makes same-second
    collisions needlessly common for a field reviewers still read.
    """

    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Tolerant JSONL read shared by EVERY memory file kind (rules, findings,
    failures, observations, review events -- R2 rework item R2-5, "uniform
    tail tolerance"): a malformed JSON line that is the LAST line in the file
    is the benign "writer died mid-append" shape and is quarantined with a
    warning, dropped from the read, never rewritten away (append-only). A
    malformed line found ANYWHERE ELSE is a stronger corruption signal --
    not the benign shape -- and still raises, mirroring SE-P3's local-
    producer trailing-corruption-quarantine pattern
    (docs/coordination/ENGINEERING_PROGRESS.md).
    """

    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    last_line_number = len(lines)
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            rows.append(json.loads(stripped))
        except json.JSONDecodeError:
            if line_number == last_line_number:
                logger.warning(
                    "quarantining malformed trailing line %d in %s (writer-died-mid-append shape)",
                    line_number,
                    path,
                )
                continue
            raise ValueError(
                f"malformed JSONL line {line_number} in {path} is not the trailing line; this is not "
                "the benign writer-died-mid-append shape and is treated as corruption"
            ) from None
    return rows


_QUARANTINE_SUFFIX = ".corrupt"


def _quarantine_path_for(path: Path) -> Path:
    return path.with_name(path.name + _QUARANTINE_SUFFIX)


def _repair_torn_tail(path: Path) -> None:
    """Write-side companion to :func:`_read_jsonl`'s trailing tolerance
    (R3 rework item R3-1, MAJOR).

    The read side SKIPS a torn trailing line but never removes it from
    disk, so the NEXT blind append either concatenates onto the torn bytes
    (the old fragment and the new line become ONE still-torn trailing
    line -- the new data is silently lost on the following read) or, once
    a THIRD line follows, turns the merged garbage into INTERIOR
    corruption, which :func:`_read_jsonl` raises on (a ``promote_pending()``
    outage). Called from :func:`_append_jsonl`, already inside the caller's
    advisory-lock hold, before every append: if ``path`` exists and its
    last byte is not a newline, the trailing segment (everything after the
    last ``\\n``) is inspected.

    "Exactly like the read-side quarantine does": only a segment that FAILS
    to parse as JSON is torn. A segment that parses fine but is merely
    missing its newline terminator (the read side would have accepted it
    as a normal row) is healed in place -- the terminator is restored, and
    nothing is quarantined or lost. A segment that fails to parse is moved
    to a ``.corrupt`` sidecar file and the main file is truncated to the
    last complete line, so the caller's append lands on a clean boundary.

    Idempotent: if the sidecar's own tail already ends with this exact
    fragment, it is not written again -- a repeated repair attempt against
    the same torn tail (e.g. a retry after a crash mid-repair) never
    duplicates the fragment.

    Crash-safety: quarantining the fragment happens BEFORE truncating the
    main file, truncating happens BEFORE the caller's new-data append, and
    truncation itself is a single in-place ``ftruncate`` (no bytes
    rewritten, so no "half-written" intermediate state). Each step only
    ever makes the main file's on-disk state MORE valid; the new-data
    append is always the LAST filesystem operation, so a crash at any
    point up to (but not including) that final append leaves a clean,
    valid (if incomplete) file, never a torn one.
    """

    if not path.exists():
        return
    with path.open("r+b") as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            return
        handle.seek(0)
        data = handle.read()
        if data.endswith(b"\n"):
            return  # already newline-terminated: nothing to repair
        last_newline = data.rfind(b"\n")
        tail = data[last_newline + 1 :]
        try:
            json.loads(tail.strip())
        except json.JSONDecodeError:
            pass
        else:
            # Valid JSON, merely missing its newline terminator: not the
            # corruption case the read-side tolerance exists for. Restore
            # the terminator in place.
            handle.seek(0, 2)
            handle.write(b"\n")
            return

        sidecar = _quarantine_path_for(path)
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        existing_tail = sidecar.read_bytes() if sidecar.exists() else b""
        if not (existing_tail.endswith(tail) or existing_tail.endswith(tail + b"\n")):
            with sidecar.open("ab") as sidecar_handle:
                sidecar_handle.write(tail)
                if not tail.endswith(b"\n"):
                    sidecar_handle.write(b"\n")
        handle.truncate(last_newline + 1)


def _append_jsonl(path: Path, row: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _repair_torn_tail(path)  # R3-1: quarantine any torn trailing fragment BEFORE appending
    line = json.dumps(row, ensure_ascii=False, sort_keys=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")

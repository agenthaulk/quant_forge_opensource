"""The server-owned pipeline aggregate (agent_sidecar_frontend.md §2.3, D10/D11).

``apps/web/jobs.py``'s ``_WebJob`` only ever has running/cancel-requested/
terminal states and starts its worker immediately -- "paused, awaiting a
human" is new server state this module adds, not a UI trick layered on top
of the job manager. A :class:`PipelineRecord` is a durable, journaled record
under ``artifact_root/pipelines/`` that can sit in ``awaiting_confirm``
indefinitely with **no worker thread parked on a human**; the human gate
(spec G1) is a stored status, not a blocked call.

Only ``kind="factor_study"`` is constructible here (specs/pipeline.py closes
the vocabulary to that one value -- phase-review F14; ``rd_optimize`` is not
deserializable at all in this schema, not merely refused by an API-level
check).

Design notes worth reading before touching this file:

* **FE-L3 (server decides truth).** :func:`create_pipeline` never accepts a
  client-supplied ``parser``/``factor`` claim -- it takes a ``parse_job_id``
  and reads the SERVER'S OWN stored result off ``job_manager.get(...)``,
  including the job's own recorded ``request`` (phase-review F3: the raw
  idea text, plumbed through so per-field provenance can be derived
  honestly instead of guessed from parser mode alone -- see
  ``apps/web/provenance.py``). Confirm-time ``parameters`` overrides ARE
  legitimately client-supplied; they earn ``human_override`` against the
  IMMUTABLE ``original_parameters`` baseline, never silently keep a stale
  source badge.
* **Idempotent confirm, live epoch (phase-review F1/F2).** ``confirm.nonce``
  is issued by the SERVER when a pipeline first reaches ``awaiting_confirm``
  (never client-generated). Unlike a single fixed value for the pipeline's
  whole ``awaiting_confirm`` lifetime, the token ROTATES:
  :func:`update_pipeline_parameters` issues a new nonce and bumps
  ``confirm.version`` on every draft mutation while still
  ``awaiting_confirm``, so a tab holding a token from BEFORE another tab's
  edit is provably stale and :func:`confirm_pipeline` rejects it -- it can
  never silently confirm against values it never saw. A double click / a
  second tab / a retried request carrying the CURRENT token still resolves
  to the same run.
* **Exactly-once launch (phase-review F1).** :func:`confirm_pipeline`
  reserves a job id, persists the ``running`` snapshot naming that exact id,
  and only THEN starts the job. A crash between "decided to launch" and
  "durably recorded that decision" is therefore impossible to observe as a
  launch: either the record still shows no running snapshot at all (the
  save itself never landed, so nothing ever started) or a ``running``
  snapshot exists naming a specific job id that reconciliation can
  independently verify -- never a state where a job is silently running
  with no durable trace of the decision to launch it. ``attempt.number`` is
  derived from how many compute jobs THIS pipeline has durably launched so
  far (counting ``compute_job`` artifact refs already on the record) --
  never incremented on a bare retry click, since a retry that is itself
  abandoned before the next confirm must not have spent an attempt number.
* **Snapshot isolation (phase-review F6).** ``working_factor_id`` is scoped
  by ``pipeline_id`` (never by input content), so no two pipelines EVER
  share a ``factor_root`` row, even when confirmed with byte-identical
  input -- strictly stronger than the original input-hash scoping this
  replaces. On success the canonical (non-scoped) definition is published
  and the working row is deleted; on failure/abort/expiry the working row
  is deleted with zero residue. See the module-level ``KNOWN LIMITATION``
  note below for the one part of the original finding this build does not
  close, and why.
* **Reconciliation on read, not a background sweep.** There is no scheduler
  thread polling job status. :func:`get_pipeline` / :func:`list_active_pipelines`
  / :func:`confirm_pipeline` / :func:`cancel_pipeline` all call
  :func:`_reconcile` first, which folds a live job's current status into the
  record. Phase-review F9 + re-verify RV-F4: when the job manager has no
  memory of a child job (pruned, or wiped by a restart), reconciliation does
  not assume failure -- it checks the durable, ATTEMPT-SCOPED completion
  artifact the compute job itself wrote at the moment of success (keyed by
  pipeline, child job id, attempt, and input hash) before concluding
  ``JOB_NOT_FOUND``, so a job that actually finished is recognized as
  completed instead of being rewritten as a failure, and a PRIOR attempt's
  side effects can never be credited to the current one.

KNOWN LIMITATION (phase-review F6, escalated, not silently dropped):
the phase review asked that a pipeline's working factor be "EXCLUDED from
the registry AND the synthesis picker" while it is running, by reusing the
existing overlay/materialization mechanism. Verified against
``evaluation/service.py::evaluate_factor`` and
``backtesting/service.py::run_factor_backtest``: both resolve their
``factor_id`` argument through ``FactorCatalog(factor_root, ...)``, which
never consults ``factor_values_overlay_root`` for a DEFINITION (only for
already-registered factors' cached VALUES) -- so there is no way to make a
factor computable without it being registered in ``factor_root`` first, and
``factor_root`` registration is exactly what ``FactorCatalog.list()`` (and
therefore the registry endpoint and the synthesis picker, both in the
FROZEN ``apps/web/api.py``) surfaces, with no status/source filter to
exploit instead. Synthesis's own ``materialize_composite`` does not defer
registration either -- it registers into ``factor_root`` BEFORE backtesting
(``docs/design/multi_factor_portfolio_backtest.md`` RF-2 treats that
ordering as load-bearing). Closing "invisible while running" would require
changing ``evaluate_factor``/``run_factor_backtest``'s signatures (kernel,
well outside this module's boundary) or adding a filter to
``_registry_factors_payload`` (frozen ``api.py``). This build closes the
other three sub-complaints in full (no permanent pollution -- publish
consolidates to one canonical id per formula; no dangling failed rows --
proactive delete plus the RunIndex-backed reconciliation catches the crash
case too; no concurrent-edit collision -- pipeline_id scoping is strictly
tighter than the input-hash scoping it replaces) and leaves the
picker-visibility-during-compute window open, reported for steward
adjudication rather than worked around with a second store.

ADJUDICATED (re-verify round 2): the visibility window is ACCEPTED as the
documented V1 boundary -- closing it requires either kernel signature
changes or a filter in the frozen api.py, both out of this phase's
authority -- on three conditions, all implemented here: the working row is
self-describing (its description names the owning pipeline, so a human
browsing the registry sees the truth -- ``_working_factor``); every
terminal path cleans BOTH the registry row and its cached value files,
with a persisted ``cleanup_pending`` retry when cleanup fails
(``_cleanup_working_artifacts`` / RV-F2/RV-F3); and the completion-time
publish is CAS-guarded against concurrent canonical edits
(``_publish_canonical_factor`` / RV-F1). Completion itself is proven by an
attempt-scoped durable artifact written by the compute job at the moment
of success (``_write_completion_artifact`` / RV-F4), never inferred from
unscoped registry side effects.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import replace as dataclass_replace
from datetime import UTC, datetime, timedelta
import json
import logging
import os
from pathlib import Path
import re
import threading
from typing import Any
from uuid import uuid4

from quant_forge.apps.web.api import (
    MAX_RD_ITERATIONS,
    _factor_from_request,
    run_idea_validation_workflow,
    run_research_once_workflow,
)
from quant_forge.apps.web.jobs import _WebJobManager, _utc_now
from quant_forge.apps.web.provenance import (
    PARAMETER_FIELDS,
    RD_CONFIRM_CARD_FIELDS,
    assert_provenance_matches_current_values,
    derive_baseline_provenance,
    derive_current_provenance,
    derive_rd_baseline_provenance,
    derive_rd_current_provenance,
)
from quant_forge.config import QuantForgeConfig
from quant_forge.core.contracts import FactorDefinition
from quant_forge.factor_engine.value_store import remove_stored_values_for_factor_id
from quant_forge.factor_library.repository import FactorRepository
from quant_forge.operator_registry import canonical_formula_fingerprint
from quant_forge.research_loop.config import ResearchLoopConfig, weights_for_objective
from quant_forge.specs.factor_spec import FactorSpec
from quant_forge.specs.pipeline import (
    ArtifactRef,
    AttemptState,
    ConfirmState,
    FailureState,
    PipelineRecord,
    StageRecord,
    TERMINAL_PIPELINE_STATUSES,
    can_transition,
    initial_stages_for,
)
from quant_forge.specs.run_manifest import canonical_fingerprint
from quant_forge.specs.validation_gate import validate_factor_spec

LOGGER = logging.getLogger("quant_forge.apps.web.pipeline")

__all__ = [
    "PipelineNotFoundError",
    "PipelineConflictError",
    "PipelineStore",
    "create_pipeline",
    "create_rd_pipeline",
    "get_pipeline",
    "pipeline_report",
    "list_active_pipelines",
    "confirm_pipeline",
    "cancel_pipeline",
    "retry_pipeline",
    "update_pipeline_parameters",
    "fork_pipeline_from_failure",
    "create_pipeline_as_fallback",
    "create_pipeline_from_edited_formula",
    "pre_validate_formula",
    "capture_planning_influence",
]

_PIPELINE_DIR_NAME = "pipelines"
_PIPELINE_ID_RE = re.compile(r"^PL_[0-9a-f]{32}$")
# Abandonment TTL for draft/awaiting_confirm/paused_failure (spec §12 open
# question #4, resolved for P1: a fixed, generous default). `running` is
# deliberately exempt -- a live compute must never be silently expired out
# from under itself; it resolves to completed/paused_failure through
# reconciliation when its job finishes (or JOB_NOT_FOUND on rejoin if it
# didn't survive a restart and no matching completion artifact exists).
DEFAULT_DRAFT_TTL_SECONDS = 24 * 3600


# ---------------------------------------------------------------------------
# SE-ix seam (cross-track amendment, binding). THIN FE-SIDE INTERFACE ONLY.
# ---------------------------------------------------------------------------
#
# ESCALATION (FE-P3, recorded per the FE-P1 Cluster C honest-escalation
# precedent): the real capture lives at
# `quant_forge.research_loop.planning_influence.capture_planning_influence`,
# which — together with its whole dependency chain (priors.py,
# memory.planning_influence_inputs, llm.authenticate_active_rule_item) — is on
# the SE trunk ONLY, NOT on this FE base branch. Importing it here would fail
# at import time. So this is a THIN seam and nothing more: the confirm path
# CALLS it (below, in every confirm), which freezes the wiring and the
# input_hash contract NOW; but with the SE module absent the default returns
# "" and the reserved `PipelineRecord.planning_influence_hash` slot stays
# empty, so input_hash is stable. The real `capture_planning_influence(store)`
# call wires up at CP-INT when the FE and SE branches unite — by reassigning
# THIS module attribute, with no change to any call site and no schema
# migration. Do NOT import, vendor, or reimplement the SE module here; the
# golden-vector-pinned frozen hash contract must not be forked.
def _default_capture_planning_influence(*, store: "PipelineStore", record: PipelineRecord) -> str:
    return ""


# Injectable indirection so CP-INT (or a test) can substitute the real capture
# without editing every call site. Never reassigned on this FE branch.
capture_planning_influence = _default_capture_planning_influence


# ---------------------------------------------------------------------------
# Kind config. Every pipeline kind shares the aggregate (record, journal,
# idempotent confirm, rejoin, expiry, attempt lineage, freeze); they differ
# ONLY in their compute/terminal stage ids and their compute-stage side
# effects. `_KindPlan` is the single place that divergence is declared, so the
# shared machinery (_reconcile / _complete_from_reconciliation) stays one code
# path with small, explicit branches rather than a forked duplicate.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _KindPlan:
    run_stage_id: str  # the stage a child job runs under ("compute" | "run" | "backtest")
    terminal_stage_id: str  # the TERMINAL display stage ("report" | "leaderboard")
    publishes: bool  # factor_study publishes a canonical factor on success; rd/timing do not
    run_job_ref_kind: str  # artifact_ref.kind counting durable launches for attempt numbering
    # Which confirm launch body this kind dispatches to. Empty means the kind
    # has NO confirm path in this build, and its confirm is a structured refusal
    # rather than a fall-through into another kind's body -- a fall-through
    # would run the wrong workflow and advance a stage id that kind does not
    # even have.
    confirm_launch: str = ""


_KIND_PLANS: dict[str, _KindPlan] = {
    "factor_study": _KindPlan(
        run_stage_id="compute",
        terminal_stage_id="report",
        publishes=True,
        run_job_ref_kind="compute_job",
        confirm_launch="validate_idea",
    ),
    "rd_optimize": _KindPlan(
        run_stage_id="run",
        terminal_stage_id="leaderboard",
        publishes=False,
        run_job_ref_kind="run_job",
        confirm_launch="research_run_once",
    ),
    # timing (specs/pipeline.py::TIMING_STAGE_IDS) runs the position-series
    # backtest under "backtest" and terminates on "report". It publishes no
    # canonical factor: a position-series study evaluates a caller-supplied
    # weight series, it does not mint a registry factor definition. Its stage
    # vocabulary and plan row are in place; its CONSTRUCTION and its confirm
    # launch belong to the downstream app and are not built at this layer, so
    # ``confirm_launch`` stays empty and a confirm on a timing record is refused
    # with a named reason instead of silently taking factor_study's path.
    "timing": _KindPlan(
        run_stage_id="backtest", terminal_stage_id="report", publishes=False, run_job_ref_kind="backtest_job"
    ),
}


def _kind_plan(kind: str) -> _KindPlan:
    plan = _KIND_PLANS.get(kind)
    if plan is None:
        raise ValueError(f"no kind plan for pipeline kind: {kind!r}")
    return plan


class PipelineNotFoundError(KeyError):
    """No pipeline exists on disk for the given (validated) pipeline_id."""


class PipelineConflictError(ValueError):
    """Illegal transition or a stale/mismatched idempotency token."""


class PipelineStageError(ValueError):
    """A stage id addressed on a record whose kind has no such stage.

    Subclasses ``ValueError`` so existing invalid-request mappings keep working,
    but it names an INTERNAL contract break (a kind routed down another kind's
    code path), which is why it is raised rather than quietly ignored.
    """


def _validate_pipeline_id(pipeline_id: str) -> None:
    if not _PIPELINE_ID_RE.match(pipeline_id):
        raise PipelineNotFoundError(pipeline_id)


def _new_pipeline_id() -> str:
    return f"PL_{uuid4().hex}"


def _new_nonce() -> str:
    return uuid4().hex


def _expires_at(created_at: str, ttl_seconds: int) -> str:
    parsed = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    return (parsed + timedelta(seconds=ttl_seconds)).astimezone(UTC).isoformat().replace("+00:00", "Z")


def _is_expired(expires_at: str, *, now: str) -> bool:
    return datetime.fromisoformat(now.replace("Z", "+00:00")) > datetime.fromisoformat(expires_at.replace("Z", "+00:00"))


# ---------------------------------------------------------------------------
# Durable persistence: append-only, monotonic-revision journal + snapshot
# (phase-review F10)
# ---------------------------------------------------------------------------


def _read_journal_tolerant(path: Path) -> list[dict[str, Any]]:
    """Torn-tail-tolerant JSONL reader (byte-level -- re-verify RV-F5).

    Append-only writers can only ever leave the LAST line partially written
    (a crash mid-``write``/``flush`` truncates the tail; every earlier line
    was already fully flushed by a prior, completed append). The file is
    read as BYTES and each newline-delimited record is decoded
    independently: a crash can truncate the tail mid-way through a
    multi-byte UTF-8 codepoint (e.g. a Chinese journal field), and a
    whole-file ``read_text`` would raise ``UnicodeDecodeError`` before any
    JSON tolerance could apply. A malformed FINAL line -- undecodable OR
    unparsable -- is the expected torn tail and is dropped; a malformed line
    anywhere else is NOT explicable by that failure mode and means real
    corruption, which raises rather than silently losing history (mirrors,
    and is deliberately stricter than, ``lineage/store.py::_read_jsonl``'s
    "skip any bad line" tolerance).
    """

    if not path.exists():
        return []
    raw_lines = path.read_bytes().split(b"\n")
    # A cleanly-terminated file ends with a newline, so the split yields a
    # trailing empty chunk: the REAL torn-tail candidate is the last
    # non-empty chunk.
    tail_index = max((index for index, chunk in enumerate(raw_lines) if chunk.strip()), default=-1)
    rows: list[dict[str, Any]] = []
    for index, raw_line in enumerate(raw_lines):
        if not raw_line.strip():
            continue
        try:
            rows.append(json.loads(raw_line.decode("utf-8").strip()))
        except (UnicodeDecodeError, json.JSONDecodeError):
            if index == tail_index:
                LOGGER.warning("ignoring torn tail line in %s (line %d)", path, index + 1)
                continue
            raise ValueError(f"corrupt pipeline journal at {path} (interior line {index + 1})") from None
    return rows


class PipelineStore:
    """Durable persistence under ``artifact_root/pipelines/``.

    Each pipeline gets one JSON snapshot (``<id>.json``, atomically
    rewritten via a temp-file-then-``os.replace`` on every transition) plus
    an append-only, monotonic-revision transition journal
    (``<id>.journal.jsonl``, phase-review F10). ``load`` always checks
    whether the journal's last replayable row is NEWER (higher
    ``revision``) than the snapshot -- true exactly when a process crashed
    between the journal append and the snapshot replace inside ``save`` --
    and prefers the journal's reconstruction in that case; it also recovers
    entirely from the journal when the snapshot is missing or unreadable.
    The journal is the source of truth; the snapshot is a read-optimization
    cache of it. A single :class:`threading.RLock` serializes
    read-modify-write sections -- mirrors ``_WebJobManager``'s own single
    lock, and is adequate here for the same reason: this is a local,
    single-user tool, and the lock only ever wraps a fast state transition
    (starting a background job returns immediately; the lock is never held
    for the job's own runtime).
    """

    def __init__(self, artifact_root: Path) -> None:
        self._root = Path(artifact_root).expanduser() / _PIPELINE_DIR_NAME
        self.lock = threading.RLock()

    def _snapshot_path(self, pipeline_id: str) -> Path:
        _validate_pipeline_id(pipeline_id)
        return self._root / f"{pipeline_id}.json"

    def _journal_path(self, pipeline_id: str) -> Path:
        _validate_pipeline_id(pipeline_id)
        return self._root / f"{pipeline_id}.journal.jsonl"

    def _recover_from_journal(self, pipeline_id: str) -> PipelineRecord | None:
        rows = _read_journal_tolerant(self._journal_path(pipeline_id))
        replayable = [row for row in rows if row.get("record") is not None]
        if not replayable:
            return None
        return PipelineRecord.from_dict(replayable[-1]["record"])

    def load(self, pipeline_id: str) -> PipelineRecord:
        snapshot_record: PipelineRecord | None = None
        try:
            raw = self._snapshot_path(pipeline_id).read_text(encoding="utf-8")
        except FileNotFoundError:
            snapshot_record = None
        else:
            try:
                snapshot_record = PipelineRecord.from_dict(json.loads(raw))
            except (json.JSONDecodeError, KeyError, ValueError):
                LOGGER.warning("snapshot for %s unreadable; recovering from journal", pipeline_id)
                snapshot_record = None
        journal_record = self._recover_from_journal(pipeline_id)
        if journal_record is not None and (snapshot_record is None or journal_record.revision > snapshot_record.revision):
            # The journal append in `save` always lands before the snapshot
            # replace; seeing a strictly newer journal revision than the
            # snapshot means a crash landed in that exact window, and the
            # journal (not the stale-but-valid snapshot) is the true state.
            record = journal_record
        else:
            record = snapshot_record
        if record is None:
            raise PipelineNotFoundError(pipeline_id)
        # phase-review F5: a GET must never serve a confirm card whose
        # badges are missing, malformed, or stale relative to the values
        # they describe.
        assert_provenance_matches_current_values(
            record.provenance, factor=record.factor, parameters=record.parameters, kind=record.kind
        )
        return record

    def list_ids(self) -> list[str]:
        """Union of snapshot AND journal ids (re-verify RV-F6): a crash after
        the very first journal fsync but before the first snapshot replace
        leaves a journal-only pipeline, which must still be discoverable so
        rejoin can reconcile it instead of silently orphaning it."""

        if not self._root.is_dir():
            return []
        snapshot_ids = {path.stem for path in self._root.glob("PL_*.json") if not path.stem.endswith(".completion")}
        journal_ids = {path.name.removesuffix(".journal.jsonl") for path in self._root.glob("PL_*.journal.jsonl")}
        return sorted(pipeline_id for pipeline_id in snapshot_ids | journal_ids if _PIPELINE_ID_RE.match(pipeline_id))

    def save(self, record: PipelineRecord, *, event: str, detail: dict[str, Any] | None = None) -> PipelineRecord:
        with self.lock:
            self._root.mkdir(parents=True, exist_ok=True)
            record = record.with_updates(revision=record.revision + 1)
            journal_row = {
                "ts": _utc_now(),
                "event": event,
                "pipeline_id": record.pipeline_id,
                "status": record.status,
                "revision": record.revision,
                "detail": detail or {},
                # The full record travels in every journal row (not just a
                # delta) so _recover_from_journal can reconstruct current
                # state from the journal ALONE if the snapshot is missing or
                # trailing -- "replayable record/delta payloads carrying
                # enough state to reconstruct the aggregate" (phase-review
                # F10) without needing a separate delta-application engine.
                "record": record.to_dict(),
            }
            with self._journal_path(record.pipeline_id).open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(journal_row, ensure_ascii=False, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            snapshot_path = self._snapshot_path(record.pipeline_id)
            tmp_path = snapshot_path.with_name(snapshot_path.name + ".tmp")
            tmp_path.write_text(
                json.dumps(record.to_dict(), ensure_ascii=False, sort_keys=True, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp_path, snapshot_path)
        return record


def _transition(record: PipelineRecord, to_status: str, **extra: Any) -> PipelineRecord:
    if not can_transition(record.status, to_status):
        raise PipelineConflictError(f"illegal pipeline transition: {record.status} -> {to_status}")
    changes: dict[str, Any] = {"status": to_status}
    if to_status != "paused_failure":
        # Leaving (or never entering) paused_failure always clears any
        # stale failure record -- PipelineRecord.__post_init__ enforces
        # failure is set if-and-only-if status == paused_failure.
        changes["failure"] = None
    changes.update(extra)
    return record.with_updates(**changes)


def _advance_stage(
    record: PipelineRecord,
    stage_id: str,
    *,
    status: str,
    started_at: str | None = None,
    ended_at: str | None = None,
    child_job_id: str | None = None,
) -> PipelineRecord:
    """Set one stage's status/timestamps, by id.

    An id that is not on THIS record's stage set is a structured failure, not a
    silent no-op: the stage sets differ per kind, so a stage id that belongs to
    another kind (or a typo) would otherwise return an unchanged record and the
    caller would go on to persist a "running" pipeline whose stage strip never
    left its initial state -- a status the UI reads as truth.
    """

    if stage_id not in {stage.stage_id for stage in record.stages}:
        raise PipelineStageError(
            f"stage_id {stage_id!r} does not exist on pipeline {record.pipeline_id!r} "
            f"(kind={record.kind!r}, stages={tuple(stage.stage_id for stage in record.stages)})"
        )
    new_stages = tuple(
        StageRecord(
            stage_id=stage.stage_id,
            status=status if stage.stage_id == stage_id else stage.status,
            child_job_id=(child_job_id if child_job_id is not None else stage.child_job_id)
            if stage.stage_id == stage_id
            else stage.child_job_id,
            started_at=(started_at if started_at is not None else stage.started_at)
            if stage.stage_id == stage_id
            else stage.started_at,
            ended_at=(ended_at if ended_at is not None else stage.ended_at)
            if stage.stage_id == stage_id
            else stage.ended_at,
        )
        for stage in record.stages
    )
    return record.with_updates(stages=new_stages)


def _reset_stage(record: PipelineRecord, stage_id: str) -> PipelineRecord:
    new_stages = tuple(
        StageRecord(stage_id=stage.stage_id) if stage.stage_id == stage_id else stage for stage in record.stages
    )
    return record.with_updates(stages=new_stages)


def _source_text_for(record: PipelineRecord) -> str:
    """The raw idea text the parse stage consumed (re-verify RV-F8/RV-F10).

    Persisted ON the record at create time (``PipelineRecord.source_text``),
    never re-read from the job manager's bounded in-memory retention: a
    pruned parse job or a server restart must not change a provenance badge
    or strand the rule-fallback exit. The pre-rework job-store lookup lives
    on only in ``create_pipeline``, which captures the text exactly once
    while the parse job is guaranteed to still exist.
    """

    return record.source_text


# ---------------------------------------------------------------------------
# Snapshot isolation (phase-review F6): pipeline_id-scoped working factor,
# publish-on-success, delete-on-terminal-non-success.
# ---------------------------------------------------------------------------


def _working_factor_id_for(pipeline_id: str, parsed_factor_id: str) -> str:
    """A factor_root id EXCLUSIVE to this pipeline, forever.

    Scoped by ``pipeline_id`` (globally unique, never reused, never shared)
    rather than by input content -- strictly stronger than hashing the
    confirmed formula+params+seed+config: two pipelines confirmed with
    byte-identical input still get two different rows, so a failure-
    triggered cleanup on one can never even theoretically touch the other's
    row, and there is no need to recompute this id if a later edit changes
    what "the input" would have hashed to.
    """

    suffix = pipeline_id.removeprefix("PL_")[:16]
    return f"{parsed_factor_id}_PW{suffix}"


def _working_factor(record: PipelineRecord) -> FactorDefinition:
    base = _factor_from_request(dict(record.factor))
    # Self-describing registry row (re-verify Cluster C adjudication): the
    # working row IS visible in the registry/picker while compute runs (the
    # documented V1 boundary -- see the module docstring's KNOWN LIMITATION),
    # so its own content must say what it is instead of masquerading as a
    # normal draft factor.
    description = str(getattr(base, "description", "") or "")
    label = f"[pipeline working draft {record.pipeline_id}]"
    if not description.startswith(label):
        description = f"{label} {description}".strip()
    return dataclass_replace(base, factor_id=record.working_factor_id, description=description)


def _cleanup_working_artifacts(config: QuantForgeConfig, record: PipelineRecord) -> bool:
    """Remove the working registry row AND its cached value files.

    Covers both the registry (``FactorRepository.delete`` -- a safe no-op
    when the row was never created) and every value root a compute run can
    write factor values under (``factor_values_root`` plus the configured
    overlay root -- re-verify RV-F2: terminal cleanup used to leave orphan
    ``factor_id=<working_id>`` value directories behind). Returns True only
    when EVERY step succeeded; a False return is the caller's signal to
    persist ``cleanup_pending`` so reconciliation retries later instead of
    the old swallow-and-forget (re-verify RV-F3).
    """

    working_factor_id = record.working_factor_id
    if not working_factor_id:
        return True
    ok = True
    repo = FactorRepository(config.paths.factor_root)
    try:
        repo.delete(working_factor_id)
    except Exception:
        LOGGER.warning("failed to delete working factor %s during pipeline cleanup", working_factor_id, exc_info=True)
        ok = False
    # Ownership guard input (rv2 round): the canonical value-dir naming is
    # non-injective, so hand the removal helper every OTHER registered id --
    # a contested directory spelling is skipped rather than deleted.
    try:
        other_ids = tuple(item.factor_id for item in repo.list() if item.factor_id != working_factor_id)
    except Exception:
        LOGGER.warning("registry listing failed during cleanup; treating cleanup as incomplete", exc_info=True)
        return False
    for root_name in ("factor_values_root", "factor_values_overlay_root"):
        root = getattr(config.paths, root_name, None)
        try:
            remove_stored_values_for_factor_id(root, working_factor_id, other_known_factor_ids=other_ids)
        except Exception:
            LOGGER.warning(
                "failed to remove cached values under %s for %s during pipeline cleanup",
                root_name,
                working_factor_id,
                exc_info=True,
            )
            ok = False
    return ok


def _with_cleanup(record: PipelineRecord, config: QuantForgeConfig) -> PipelineRecord:
    """Run working-artifact cleanup and fold the outcome into the record."""

    return record.with_updates(cleanup_pending=not _cleanup_working_artifacts(config, record))


class _CanonicalRowUnreadable(Exception):
    """A registry read failed for a reason OTHER than absence."""


def _factor_content_fingerprint(factor: FactorDefinition) -> str:
    """Content fingerprint of a factor definition.

    The single fingerprint shape used for BOTH the canonical registry row
    (loaded from disk) and an in-memory intended-publish output, so a
    saved-then-reloaded row and the object it was built from fingerprint
    identically -- the F3 recovery check in ``_publish_canonical_factor``
    depends on that symmetry.
    """

    from dataclasses import asdict

    payload = {key: str(value) for key, value in sorted(asdict(factor).items())}
    return canonical_fingerprint(payload)


def _canonical_row_fingerprint(config: QuantForgeConfig, canonical_id: str) -> str:
    """Content fingerprint of the canonical registry row; "" when absent.

    The CAS baseline for publish (re-verify RV-F1): captured at confirm
    time, re-checked at publish time. Any change to the row's content --
    formula, name, description, status, anything serialized -- between
    those two moments makes the fingerprints differ and blocks the publish.
    An UNREADABLE row raises (rv2 round: mapping read errors to "" made an
    unreadable row indistinguishable from an absent one, which could match
    an absent-at-confirm baseline and publish blind over content we never
    saw). ``confirm_pipeline`` maps the raise back to "" for the BASELINE
    capture (an unreadable row there simply guarantees a later conflict --
    fail-closed in the safe direction).
    """

    if not canonical_id:
        return ""
    try:
        existing = FactorRepository(config.paths.factor_root).get(canonical_id)
    except FileNotFoundError:
        return ""
    except Exception as exc:
        raise _CanonicalRowUnreadable(canonical_id) from exc
    return _factor_content_fingerprint(existing)


def _publish_lock_path(config: QuantForgeConfig) -> Path:
    return Path(config.paths.factor_root).expanduser() / ".qf_pipeline_publish.lock"


def _publish_canonical_factor(config: QuantForgeConfig, record: PipelineRecord) -> tuple[str | None, str]:
    """On success only: consolidate the working row into the canonical id.

    The canonical id is the bare id the parser itself produced
    (``record.factor["factor_id"]``, never mutated after create) -- one row
    per distinct formula/idea, matching the pre-P1 convention, so a
    successful run does not permanently multiply registry rows per
    parameter combination. Guards:

    * G3 (unchanged): refuses to overwrite an EXISTING canonical row that
      has already been human-promoted (status other than "draft") --
      ``publish_state="declined_promoted"``.
    * CAS (re-verify RV-F1, tightened in rv2): the check+save sequence runs
      under a publisher advisory file lock with the fingerprint re-read
      IMMEDIATELY before the save, so two publishers can never interleave
      and a concurrent edit's window shrinks to the single read->write
      step. An unreadable row fails CLOSED as a conflict. KNOWN RESIDUAL
      (documented, adjudicated): a non-publisher writer (the registry edit
      route in the frozen api.py) takes no lock, so a sub-millisecond
      last-instant edit can still be overwritten -- full CAS needs
      registry-level versioning, a kernel change recorded in the deferred
      register; within this local single-user tool the residual window is
      human-scale unreachable.

    Returns ``(published_id, publish_state)``.
    """

    canonical_id = str(record.factor.get("factor_id", ""))
    if not canonical_id:
        return None, ""
    # Underscore spelling on purpose: this branch's lineage/store.py still
    # has only the private def, while the sibling engine track promoted a
    # public `advisory_file_lock` and kept `_advisory_file_lock` as an
    # alias -- the private name is the one import valid on BOTH sides of
    # the eventual union merge.
    from quant_forge.lineage.store import _advisory_file_lock

    lock_path = _publish_lock_path(config)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _advisory_file_lock(lock_path):
        try:
            current_fingerprint = _canonical_row_fingerprint(config, canonical_id)
        except _CanonicalRowUnreadable:
            LOGGER.warning("declining to publish %s: canonical row unreadable (fail closed)", canonical_id)
            return None, "conflict"
        if current_fingerprint != record.canonical_baseline_fingerprint:
            # F3 (restart-idempotent publish): recognize our OWN prior publish
            # before calling a foreign conflict. A crash BETWEEN this pipeline's
            # canonical repo.save and the completion store.save leaves the row
            # present while the on-disk snapshot still says "running"; the
            # restart's reconcile-republish then re-reads a row that was ABSENT
            # at confirm (baseline ""), so the CAS baseline no longer matches --
            # yet if the existing row is byte-identical to what THIS attempt
            # would publish, the publish already landed and recording None would
            # strand a factor that plainly exists. Treat that as idempotent
            # success (recovered prior publish), NOT a conflict. A genuinely
            # foreign concurrent edit still differs and still fails closed below.
            intended = dataclass_replace(
                _factor_from_request(dict(record.factor)), factor_id=canonical_id, status="draft"
            )
            try:
                existing_row = FactorRepository(config.paths.factor_root).get(canonical_id)
            except FileNotFoundError:
                existing_row = None
            if existing_row is not None and _factor_content_fingerprint(existing_row) == _factor_content_fingerprint(
                intended
            ):
                LOGGER.info(
                    "recovered prior publish for %s (canonical row already matches this attempt's output)",
                    canonical_id,
                )
                return canonical_id, "published"
            LOGGER.info(
                "declining to publish %s: canonical row changed since confirm (CAS mismatch)",
                canonical_id,
            )
            return None, "conflict"
        repo = FactorRepository(config.paths.factor_root)
        try:
            existing = repo.get(canonical_id)
        except FileNotFoundError:
            existing = None
        except Exception:
            LOGGER.warning("declining to publish %s: canonical row unreadable (fail closed)", canonical_id)
            return None, "conflict"
        if existing is not None and existing.status != "draft":
            LOGGER.info("declining to publish over promoted factor %s (status=%s)", canonical_id, existing.status)
            return None, "declined_promoted"
        working = _factor_from_request(dict(record.factor))
        canonical = dataclass_replace(working, factor_id=canonical_id, status="draft")
        repo.save(canonical)
    return canonical_id, "published"


# ---------------------------------------------------------------------------
# Completion artifact (re-verify RV-F4): durable, ATTEMPT-SCOPED proof that a
# compute run finished, written by the compute job itself at the moment of
# success -- replaces the unscoped RunIndex row-count probe, which could
# mistake a PRIOR attempt's rows for the current attempt's completion.
# ---------------------------------------------------------------------------


def _completion_artifact_path(store: PipelineStore, pipeline_id: str) -> Path:
    _validate_pipeline_id(pipeline_id)
    return store._root / f"{pipeline_id}.completion.json"  # noqa: SLF001 - module-internal store layout


def _write_completion_artifact(
    store: PipelineStore,
    *,
    pipeline_id: str,
    child_job_id: str,
    attempt: int,
    input_hash: str,
    result: dict[str, Any],
) -> None:
    """Atomically persist the completion proof + the full result payload.

    Runs INSIDE the compute job thread, after the validate workflow
    succeeded and before the job manager marks the job completed. Keyed by
    (pipeline, child job, attempt, input hash) so reconciliation can never
    credit the wrong attempt, and carrying the result payload so a recovered
    pipeline still has a servable report after the in-memory job record is
    gone (RV-F4's second half).
    """

    path = _completion_artifact_path(store, pipeline_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "pipeline_id": pipeline_id,
        "child_job_id": child_job_id,
        "attempt": attempt,
        "input_hash": input_hash,
        "completed_at": _utc_now(),
        "result": result,
    }
    tmp_path = path.with_name(path.name + f".tmp-{uuid4().hex[:8]}")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    os.replace(tmp_path, path)


def _read_completion_artifact(store: PipelineStore, pipeline_id: str) -> dict[str, Any] | None:
    try:
        raw = _completion_artifact_path(store, pipeline_id).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except UnicodeDecodeError:
        # RV3-F3: a byte-corrupt artifact is NON-EVIDENCE (honest pause
        # downstream), never an exception escaping reconciliation.
        LOGGER.warning("undecodable completion artifact for %s; ignoring it", pipeline_id)
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        LOGGER.warning("unreadable completion artifact for %s; ignoring it", pipeline_id)
        return None
    return payload if isinstance(payload, dict) else None


def _completion_matches_record(payload: dict[str, Any] | None, record: PipelineRecord) -> bool:
    """Completion evidence counts ONLY for the exact attempt it proves.

    Malformed field TYPES are non-evidence, never an exception (rv2 round:
    ``attempt="bad"`` used to raise out of reconciliation instead of
    degrading to the honest JOB_NOT_FOUND pause).
    """

    if payload is None:
        return False
    compute = record.stage(_kind_plan(record.kind).run_stage_id)
    attempt = payload.get("attempt")
    # Strict type, not int() coercion (re-verify RV3-F2): True, 1.9 and "1"
    # all coerce to a matching attempt number, and forged near-miss types
    # must never count as completion evidence. `type(...) is int` also
    # excludes bool (a subclass of int).
    if type(attempt) is not int:
        return False
    return (
        str(payload.get("pipeline_id", "")) == record.pipeline_id
        and str(payload.get("child_job_id", "")) == str(compute.child_job_id or "")
        and attempt == record.attempt.number
        and str(payload.get("input_hash", "")) == record.input_hash
    )


def _compute_input_hash(
    *,
    factor: dict[str, Any],
    parameters: dict[str, Any],
    parse_job_id: str,
    parser_source: str,
    rd_config: ResearchLoopConfig,
    planning_influence_hash: str,
) -> str:
    """formula + params + seed + config, plus the reserved SE-ix slot.

    ``config`` is intentionally coarse in P1 (the research-loop objective
    only) -- the fully-resolved simulation/backtest numbers already live in
    ``params``, so a broader config fingerprint would mostly duplicate that;
    it exists as its own named component so the hash contract can grow it
    later without changing shape (mirrors why ``planning_influence_hash`` is
    a real field today even though it is always empty until SE-P5).
    """

    payload = {
        "formula": str(factor.get("formula", "")),
        "params": {name: parameters.get(name) for name in PARAMETER_FIELDS},
        "seed": {
            "factor_id": str(factor.get("factor_id", "")),
            "parse_job_id": parse_job_id,
            "parser_source": parser_source,
        },
        "config": {"objective": rd_config.objective},
        "planning_influence_hash": planning_influence_hash,
    }
    return canonical_fingerprint(payload)


def _flat_parameters_only(parameters: dict[str, Any]) -> dict[str, Any]:
    """Keep only the 11 flat confirm-card fields.

    ``_default_validation_parameters`` (apps/web/api.py) also emits nested
    convenience mirrors (``evaluation.simulation.*``, ``backtest.simulation.*``,
    ``transaction_costs.*``) that duplicate the same values for
    ``_idea_validation_settings``'s own convenience. Those mirrors are
    computed once at PARSE time; a confirm-time override only ever touches
    the flat key it edited, so keeping the mirrors around risks
    ``_idea_validation_settings`` silently preferring a stale nested value
    over a fresher flat override (it merges nested overrides AFTER flat
    ones -- see ``apps/web/api.py::_idea_validation_settings``). This
    pipeline never sends the mirrors at all: ``_idea_validation_settings``
    already derives everything it needs from the flat keys alone, so
    dropping the mirrors closes the staleness risk instead of trying to
    keep two representations of the same numbers in sync.
    """

    return {name: parameters.get(name) for name in PARAMETER_FIELDS}


def create_pipeline(
    store: PipelineStore,
    *,
    job_manager: _WebJobManager,
    parse_job_id: str,
    rd_config: ResearchLoopConfig,
    kind: str = "factor_study",
    parent_run_id: str | None = None,
) -> PipelineRecord:
    """Wrap an already-completed ``parse_idea`` job into a new pipeline.

    Takes a job id, not a parser/factor payload (FE-L3 -- see module
    docstring): the ``parser``/``factor``/``parameters``/``warnings`` this
    pipeline stores come directly from ``job_manager.get(parse_job_id)``'s
    OWN stored result, and the raw idea text comes from that SAME job's own
    recorded ``request`` (phase-review F3) -- never from anything the
    client's request body claims. ``parent_run_id`` records fork/fallback
    lineage (phase-review F7); it does not change how THIS pipeline itself
    behaves.
    """

    if kind != "factor_study":
        # This entry constructs pipeline A only. It is not a claim about the
        # kind vocabulary: rd_optimize is constructed by ``create_rd_pipeline``
        # (a seeded factor id, not a parse job), and timing has no constructor
        # at this layer at all -- its stage ids and kind plan are in place, its
        # construction path belongs to the downstream app. Naming the right
        # entry beats a wrong claim that the kind does not exist.
        raise ValueError(
            f"create_pipeline builds kind='factor_study' only, got {kind!r}; "
            "kind='rd_optimize' is built by create_rd_pipeline, and kind='timing' has no "
            "constructor in this build (see specs/pipeline.py for the kind vocabulary)"
        )
    parse_job = job_manager.get(parse_job_id)
    if parse_job.get("kind") != "parse_idea":
        raise ValueError(f"parse_job_id {parse_job_id!r} is not a parse_idea job")
    if parse_job.get("status") != "completed":
        raise ValueError(f"parse_job_id {parse_job_id!r} has not completed (status={parse_job.get('status')!r})")
    parse_result = parse_job.get("result") or {}
    parser = dict(parse_result.get("parser") or {})
    factor = dict(parse_result.get("factor") or {})
    # Flat-only from the start (see _flat_parameters_only): the nested
    # evaluation/backtest/transaction_costs mirrors _default_validation_parameters
    # also emits are never stored on the record, so there is no second
    # representation of these 11 numbers to drift out of sync with an edit.
    parameters = _flat_parameters_only(dict(parse_result.get("parameters") or {}))
    warnings = tuple(str(item) for item in (parse_result.get("warnings") or ()))
    text = str((parse_job.get("request") or {}).get("text", ""))
    if not factor.get("factor_id"):
        raise ValueError("parse result carries no factor.factor_id; cannot create a pipeline from it")

    now = _utc_now()
    planning_influence_hash = ""  # SE-ix reserved slot; empty until SE-P5.
    input_hash = _compute_input_hash(
        factor=factor,
        parameters=parameters,
        parse_job_id=parse_job_id,
        parser_source=str(parser.get("source", "")),
        rd_config=rd_config,
        planning_influence_hash=planning_influence_hash,
    )
    # The immutable per-field origin artifact is computed ONCE, right here,
    # while the parse job (and its idea text) is guaranteed to still exist
    # (re-verify RV-F8); every later badge derivation is a pure function of
    # this baseline plus the then-current values. The initial current badges
    # are derived through the SAME pure function the edit/confirm paths use,
    # so the confirm card has a full badge set from the very first render
    # (WORKORDER P1 pin: missing badge = fail).
    baseline_provenance = tuple(
        entry.to_dict()
        for entry in derive_baseline_provenance(parser=parser, factor=factor, parameters=parameters, text=text)
    )
    provenance = derive_current_provenance(
        baseline=baseline_provenance, factor=factor, current_parameters=parameters
    )
    pipeline_id = _new_pipeline_id()
    record = PipelineRecord(
        pipeline_id=pipeline_id,
        kind=kind,
        created_at=now,
        expires_at=_expires_at(now, DEFAULT_DRAFT_TTL_SECONDS),
        status="draft",
        stages=initial_stages_for(kind),
        input_hash=input_hash,
        planning_influence_hash=planning_influence_hash,
        confirm=ConfirmState(nonce=_new_nonce(), version=1),
        parser=parser,
        factor=factor,
        parameters=parameters,
        source_text=text,
        original_parameters=parameters,
        warnings=warnings,
        provenance=tuple(entry.to_dict() for entry in provenance),
        baseline_provenance=baseline_provenance,
        attempt=AttemptState(number=1, parent_run_id=parent_run_id),
        working_factor_id=_working_factor_id_for(pipeline_id, str(factor["factor_id"])),
        artifact_refs=(ArtifactRef(kind="parse", job_id=parse_job_id),),
    )
    with store.lock:
        # Two real, journaled transitions (draft -> parse-completed ->
        # awaiting_confirm) inside one call: parse already ran, synchronously,
        # before this function was ever invoked, so there is no window where
        # a half-parsed draft could be observed by a concurrent reader.
        record = _advance_stage(record, "parse", status="completed", started_at=now, ended_at=now)
        record = _transition(record, "awaiting_confirm")
        record = _advance_stage(record, "confirm", status="active", started_at=now)
        record = store.save(record, event="created")
    return record


# ---------------------------------------------------------------------------
# Pipeline B (rd_optimize) creation + server-side rounds validation.
# ---------------------------------------------------------------------------


def _validate_rd_rounds(value: Any) -> int:
    """Server-authoritative rounds bound (WORKORDER pin: out-of-range rounds
    rejected SERVER-SIDE, never client-only). Mirrors api.py's
    ``_rd_iterations_parameter`` (1..MAX_RD_ITERATIONS) so the pipeline-B
    confirm gate and the legacy RD endpoint agree on the same closed range."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"rounds must be an integer between 1 and {MAX_RD_ITERATIONS}")
    if value < 1 or value > MAX_RD_ITERATIONS:
        raise ValueError(f"rounds must be between 1 and {MAX_RD_ITERATIONS}")
    return value


# The research service bounds candidates-per-round to 1..10
# (research_loop/service.py::ResearchLoopService.run_once). Mirror that exact
# range at BOTH the pipeline-B create AND confirm gates (WORKORDER F8) so an
# out-of-range value is a synchronous 400 rather than a research job that dies
# with the same error minutes later.
MAX_RD_CANDIDATES_PER_ROUND = 10


def _validate_rd_candidates(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"candidates_per_round must be an integer between 1 and {MAX_RD_CANDIDATES_PER_ROUND}")
    if value < 1 or value > MAX_RD_CANDIDATES_PER_ROUND:
        raise ValueError(f"candidates_per_round must be between 1 and {MAX_RD_CANDIDATES_PER_ROUND}")
    return value


def _validate_rd_objective(value: Any, rd_config: ResearchLoopConfig) -> str:
    """Validate the objective against the RD config's ACTUAL known objective
    set (WORKORDER F8) -- not merely "non-empty string".

    ``weights_for_objective`` raises ``ValueError`` for any objective the run
    could not actually score with (the same check ``run_once`` would hit), so
    an unknown objective is a synchronous 400 at create/confirm instead of a
    late job failure. Returns the objective string unchanged when valid.
    """

    objective = str(value)
    weights_for_objective(rd_config, objective)  # raises ValueError if unknown
    return objective


def _rd_parameters_only(parameters: dict[str, Any]) -> dict[str, Any]:
    """Keep only the three pipeline-B confirm-card fields (RD inherits the
    evaluation interval/sample contract; R3.1 adds no other RD parameter)."""

    return {name: parameters.get(name) for name in RD_CONFIRM_CARD_FIELDS}


def _rd_input_hash(
    *,
    seed_factor_id: str,
    rd_parameters: dict[str, Any],
    rd_config: ResearchLoopConfig,
    planning_influence_hash: str,
) -> str:
    """seed + (rounds, candidates_per_round, objective) + config, plus the
    reserved SE-ix slot -- same shape discipline as ``_compute_input_hash``."""

    payload = {
        "seed": {"factor_id": seed_factor_id},
        "rd": {name: rd_parameters.get(name) for name in RD_CONFIRM_CARD_FIELDS},
        "config": {"objective": rd_config.objective},
        "planning_influence_hash": planning_influence_hash,
    }
    return canonical_fingerprint(payload)


def create_rd_pipeline(
    store: PipelineStore,
    *,
    job_manager: _WebJobManager,
    config: QuantForgeConfig,
    seed_factor_id: str,
    rd_config: ResearchLoopConfig,
    rounds: Any = None,
    candidates_per_round: Any = None,
    objective: Any = None,
    parent_run_id: str | None = None,
) -> PipelineRecord:
    """Create a pipeline B (rd_optimize) aggregate, seeded from a factor id.

    Pipeline B is ALWAYS user-initiated with an explicit seed (a completed
    report's factor, or a registry factor): there is NO automatic A->B bridge
    (spec §2.1) -- nothing in the aggregate ever calls this on A's completion.
    Rounds are validated server-side here AND again at confirm, so an
    out-of-range value can never launch even if a client skips its own check.
    """

    if not seed_factor_id.strip():
        raise ValueError("seed_factor_id is required to create an rd_optimize pipeline")
    # F8: the seed factor must exist AND be resolvable through the SAME
    # catalog the research run will load it from (factor_root + mounted
    # precomputed values), so a nonexistent/ineligible seed is a synchronous
    # 400 at create rather than a research job that dies on catalog.get(...)
    # minutes later. FileNotFoundError is the catalog's "not found" signal.
    from quant_forge.factor_library.catalog import FactorCatalog

    try:
        FactorCatalog(
            config.paths.factor_root,
            factor_values_root=config.paths.factor_values_root,
            factor_values_manifest_root=config.paths.factor_values_manifest_root,
        ).get(seed_factor_id)
    except FileNotFoundError as exc:
        raise ValueError(f"seed factor not found or not eligible: {seed_factor_id}") from exc
    user_explicit: set[str] = set()
    if rounds is None:
        rounds_value = 1
    else:
        rounds_value = _validate_rd_rounds(rounds)
        user_explicit.add("rounds")
    if candidates_per_round is None:
        candidates_value = int(rd_config.default_max_candidates)
    else:
        candidates_value = _validate_rd_candidates(candidates_per_round)
        user_explicit.add("candidates_per_round")
    if objective is None or (isinstance(objective, str) and not objective.strip()):
        objective_value = str(rd_config.objective)
    else:
        # F8: validate against the ACTUAL known objective set, not just
        # non-empty. An unknown objective is a 400 here (create) as well as at
        # confirm.
        objective_value = _validate_rd_objective(objective, rd_config)
        user_explicit.add("objective")

    rd_parameters = {
        "rounds": rounds_value,
        "candidates_per_round": candidates_value,
        "objective": objective_value,
    }
    now = _utc_now()
    planning_influence_hash = ""  # SE-ix reserved slot; empty until CP-INT (see module header).
    input_hash = _rd_input_hash(
        seed_factor_id=seed_factor_id,
        rd_parameters=rd_parameters,
        rd_config=rd_config,
        planning_influence_hash=planning_influence_hash,
    )
    baseline_provenance = tuple(
        entry.to_dict()
        for entry in derive_rd_baseline_provenance(
            rounds=rounds_value,
            candidates_per_round=candidates_value,
            objective=objective_value,
            user_explicit_fields=frozenset(user_explicit),
        )
    )
    provenance = derive_rd_current_provenance(baseline=baseline_provenance, current_parameters=rd_parameters)
    pipeline_id = _new_pipeline_id()
    record = PipelineRecord(
        pipeline_id=pipeline_id,
        kind="rd_optimize",
        created_at=now,
        expires_at=_expires_at(now, DEFAULT_DRAFT_TTL_SECONDS),
        status="draft",
        stages=initial_stages_for("rd_optimize"),
        input_hash=input_hash,
        planning_influence_hash=planning_influence_hash,
        confirm=ConfirmState(nonce=_new_nonce(), version=1),
        # `factor` carries only the seed id (pipeline B does not parse or
        # publish a factor); the leaderboard renders the RD job's own payload.
        factor={"factor_id": seed_factor_id},
        parameters=rd_parameters,
        original_parameters=rd_parameters,
        provenance=tuple(entry.to_dict() for entry in provenance),
        baseline_provenance=baseline_provenance,
        attempt=AttemptState(number=1, parent_run_id=parent_run_id),
        # No working factor row: RD seeds from an already-registered factor and
        # never overwrites it (promotion is G3, outside the sidecar).
        working_factor_id="",
    )
    with store.lock:
        record = _transition(record, "awaiting_confirm")
        record = _advance_stage(record, "confirm", status="active", started_at=now)
        record = store.save(record, event="created")
    return record


def _reconcile(
    record: PipelineRecord, *, store: PipelineStore, job_manager: _WebJobManager, config: QuantForgeConfig
) -> PipelineRecord:
    """Fold live job status (or its absence) into the record before returning it.

    Called at the top of every read/write entrypoint (spec §2.3 "Rejoin");
    see the module docstring's "Reconciliation on read" note for why this
    replaces a background sweep thread.
    """

    if record.cleanup_pending:
        # Re-verify RV-F3: a terminal transition whose working-artifact
        # cleanup failed persists cleanup_pending; every subsequent load
        # retries it here until it succeeds, instead of swallowing the
        # failure once and leaving the _PW row / cached values forever.
        if _cleanup_working_artifacts(config, record):
            record = record.with_updates(cleanup_pending=False)
            record = store.save(record, event="cleanup_retried")
    if record.status in TERMINAL_PIPELINE_STATUSES:
        return record
    now = _utc_now()
    if record.status in {"draft", "awaiting_confirm", "paused_failure"} and _is_expired(record.expires_at, now=now):
        record = _with_cleanup(record, config)
        # Rotate the confirm token on the expiry mutation too (re-verify
        # Cluster A): expired is terminal, so no confirm can ever consume the
        # old token -- rotation makes that structural rather than incidental.
        record = record.with_updates(confirm=ConfirmState(nonce=_new_nonce(), version=record.confirm.version + 1))
        record = _transition(record, "expired")
        return store.save(record, event="expired")
    if record.status != "running":
        return record
    # Kind-aware run stage: factor_study runs its compute job under "compute",
    # rd_optimize runs its research job under "run" -- the SAME reconciliation
    # logic folds the child job's status into whichever stage holds it.
    plan = _kind_plan(record.kind)
    run_stage = record.stage(plan.run_stage_id)
    if run_stage.child_job_id is None:
        return record
    try:
        job = job_manager.get(run_stage.child_job_id)
    except KeyError:
        # Re-verify RV-F4: do not assume failure just because the in-memory
        # job manager has forgotten this job -- but ONLY the attempt-scoped
        # completion artifact (pipeline, child job, attempt, input hash),
        # written by the compute job itself at the moment of success, counts
        # as completion evidence. The old unscoped RunIndex row-count probe
        # could credit a PRIOR attempt's rows to the current attempt.
        if _completion_matches_record(_read_completion_artifact(store, record.pipeline_id), record):
            return _complete_from_reconciliation(record, store=store, config=config, now=now)
        record = _with_cleanup(record, config)
        record = _advance_stage(record, plan.run_stage_id, status="failed", ended_at=now)
        record = _transition(
            record,
            "paused_failure",
            failure=FailureState(stage_id=plan.run_stage_id, reason_code="JOB_NOT_FOUND"),
            # F2 (§5.4): count this failed attempt durably so the disclosure
            # survives a later retry that clears `failure`.
            failed_attempts=record.failed_attempts + 1,
        )
        return store.save(record, event="job_not_found_on_reconcile")
    job_status = job.get("status")
    if job_status in {"running", "cancel_requested"}:
        return record
    if job_status == "completed":
        return _complete_from_reconciliation(
            record, store=store, config=config, now=now, compute_job_id=run_stage.child_job_id
        )
    if job_status == "cancelled":
        record = _with_cleanup(record, config)
        record = _advance_stage(record, plan.run_stage_id, status="failed", ended_at=now)
        record = _transition(record, "aborted")
        return store.save(record, event="compute_cancelled")
    # failed
    record = _with_cleanup(record, config)
    reason = str(job.get("error") or "").strip()[:200] or "COMPUTE_FAILED"
    record = _advance_stage(record, plan.run_stage_id, status="failed", ended_at=now)
    record = _transition(
        record,
        "paused_failure",
        failure=FailureState(stage_id=plan.run_stage_id, reason_code=reason),
        failed_attempts=record.failed_attempts + 1,
    )
    return store.save(record, event="compute_failed")


def _complete_from_reconciliation(
    record: PipelineRecord,
    *,
    store: PipelineStore,
    config: QuantForgeConfig,
    now: str,
    compute_job_id: str | None = None,
) -> PipelineRecord:
    plan = _kind_plan(record.kind)
    # factor_study publishes ONE canonical factor on success (CAS-guarded);
    # rd_optimize produces a candidate leaderboard, not a single publishable
    # factor -- promotion to active is G3, outside the sidecar (spec §3) --
    # so it never publishes and has no working row to clean.
    if plan.publishes:
        published, publish_state = _publish_canonical_factor(config, record)
    else:
        published, publish_state = None, ""
    record = _with_cleanup(record, config)
    # The report ref names BOTH the job id and the durable completion
    # artifact path (re-verify RV-F4): the report endpoint serves from the
    # live job when it still exists and falls back to the artifact when a
    # restart has wiped the in-memory manager, so a legitimately recovered
    # pipeline still renders its report/leaderboard.
    report_ref = ArtifactRef(
        kind="report",
        job_id=compute_job_id or record.stage(plan.run_stage_id).child_job_id,
        artifact_path=str(_completion_artifact_path(store, record.pipeline_id)),
    )
    record = _advance_stage(record, plan.run_stage_id, status="completed", ended_at=now)
    record = _advance_stage(record, plan.terminal_stage_id, status="completed", started_at=now, ended_at=now)
    record = record.with_updates(
        artifact_refs=record.artifact_refs + (report_ref,),
        published_factor_id=published,
        publish_state=publish_state,
    )
    record = _transition(record, "completed")
    return store.save(record, event="compute_completed", detail={"publish_state": publish_state})


def get_pipeline(store: PipelineStore, pipeline_id: str, *, job_manager: _WebJobManager, config: QuantForgeConfig) -> PipelineRecord:
    with store.lock:
        record = store.load(pipeline_id)
        return _reconcile(record, store=store, job_manager=job_manager, config=config)


def pipeline_report(
    store: PipelineStore, pipeline_id: str, *, job_manager: _WebJobManager, config: QuantForgeConfig
) -> dict[str, Any]:
    """The completed pipeline's report payload, restart-proof (re-verify RV-F4).

    Serves the live job result while the job manager still remembers the
    compute job, and falls back to the durable completion artifact once a
    restart has wiped it -- so a legitimately recovered pipeline still
    renders a report instead of a silently-suppressed 404. Raises
    ``PipelineConflictError`` when the pipeline has not completed, and
    ``PipelineNotFoundError`` when no evidence of the completed result
    exists anywhere (job gone AND artifact missing/mismatched).
    """

    with store.lock:
        record = store.load(pipeline_id)
        record = _reconcile(record, store=store, job_manager=job_manager, config=config)
        if record.status != "completed":
            raise PipelineConflictError(f"pipeline {pipeline_id} has no report (status={record.status})")
        report_ref = next((ref for ref in record.artifact_refs if ref.kind == "report"), None)
        job_id = (report_ref.job_id if report_ref is not None else None) or record.stage(
            _kind_plan(record.kind).run_stage_id
        ).child_job_id
        if job_id:
            try:
                job = job_manager.get(job_id)
            except KeyError:
                job = None
            if job is not None and job.get("status") == "completed" and job.get("result") is not None:
                return {"pipeline_id": pipeline_id, "source": "job", "result": job.get("result")}
        payload = _read_completion_artifact(store, pipeline_id)
        if _completion_matches_record(payload, record):
            # Defensive re-sanitize (rv2 round): new artifacts are written
            # public-projected already, but artifacts written by the
            # pre-fix build carried the raw workflow result (absolute
            # artifact/value paths) -- serve-side projection guarantees no
            # stored copy, old or new, can leak them.
            from quant_forge.apps.web import server as _server

            return {
                "pipeline_id": pipeline_id,
                "source": "artifact",
                "result": _server._web_public_json((payload or {}).get("result")),
            }
        raise PipelineNotFoundError(f"no durable report evidence for completed pipeline {pipeline_id}")


# F7 (durable report rejoin): recently-completed pipelines stay visible in the
# rejoin listing (bounded) instead of surfacing exactly once. A reload, Back, or
# a second consumer must all still re-attach to a just-finished report -- the
# old one-shot consumption made the completion invisible on the very next
# listing. Bounded by COUNT (the N most recent by created_at) so the listing can
# never grow without limit as completed pipelines accumulate.
_MAX_RECENT_COMPLETED_IN_LISTING = 10


def list_active_pipelines(store: PipelineStore, *, job_manager: _WebJobManager, config: QuantForgeConfig) -> list[PipelineRecord]:
    """Every non-terminal pipeline plus the recently-completed ones, reconciled.

    Phase-review F9: rejoin must not reconcile only the pipeline the
    frontend happens to render -- every id under the store is loaded and
    reconciled here, unconditionally, before the (separate) decision of
    which one a caller's UI attaches to.

    F7 (durable report rejoin): the listing carries non-terminal pipelines AND
    the ``_MAX_RECENT_COMPLETED_IN_LISTING`` most-recent COMPLETED ones, with NO
    one-shot consumption -- a completed report stays discoverable across a
    reload and to a second consumer instead of being surfaced exactly once.
    aborted/expired terminals carry no report to rejoin and are excluded (they
    are still reconciled here, so their terminal transition still persists).
    """

    with store.lock:
        active: list[PipelineRecord] = []
        completed: list[PipelineRecord] = []
        for pipeline_id in store.list_ids():
            try:
                record = store.load(pipeline_id)
            except PipelineNotFoundError:
                continue
            record = _reconcile(record, store=store, job_manager=job_manager, config=config)
            if record.status not in TERMINAL_PIPELINE_STATUSES:
                active.append(record)
            elif record.status == "completed":
                completed.append(record)
    completed.sort(key=lambda item: item.created_at, reverse=True)
    records = active + completed[:_MAX_RECENT_COMPLETED_IN_LISTING]
    records.sort(key=lambda item: item.created_at, reverse=True)
    return records


def confirm_pipeline(
    config: QuantForgeConfig,
    store: PipelineStore,
    pipeline_id: str,
    *,
    nonce: str,
    version: int,
    job_manager: _WebJobManager,
    rd_config: ResearchLoopConfig,
    parameters: dict[str, Any] | None = None,
) -> PipelineRecord:
    """Idempotent confirm (spec §2.3 / WORKORDER P1 pin), exactly-once launch.

    ``(pipeline_id, nonce, version)`` is the idempotency key, and the token
    is SINGLE-USE per EFFECTIVE payload (re-verify Cluster A): the SAME key
    seen again -- double click, second tab, retried request -- returns the
    SAME run untouched ONLY when its effective parameter payload equals
    what the consumed confirm actually launched; the same key with a
    DIFFERENT payload is a conflict (the old behavior silently discarded
    the second tab's divergent edits as an "idempotent replay").

    Equality is deliberately SEMANTIC, not raw-request-shape (rv2-round
    adjudication, upheld by design): ``{}`` after ``{"holding_days": 5}``
    replays cleanly when 5 was already the displayed draft value, because
    both requests mean "launch exactly what the card shows" -- the human
    meaning of the confirm gesture. Hashing the raw request body instead
    would reject that harmless refresh-then-reclick while adding no
    protection: any payload whose EFFECTIVE values differ already
    conflicts, and unknown fields are dropped by the same flat-field
    filter every write path applies. A different/stale key while still
    awaiting confirmation is rejected (phase-review F1/F2) so a late
    request from a superseded draft can never confirm the wrong offer.

    Launch ordering (phase-review F1): reserve the child job id, persist the
    ``running`` snapshot naming that id, and only THEN actually start it. If
    starting fails (e.g. a same-kind job collision from an unrelated
    pipeline), the already-durable ``running`` record is immediately
    reconciled to ``paused_failure`` in this SAME response rather than left
    to a later poll to notice a job id that was never registered.
    """

    with store.lock:
        record = store.load(pipeline_id)
        record = _reconcile(record, store=store, job_manager=job_manager, config=config)
        # Dispatch off the KIND PLAN, not off a hardcoded kind literal. Pipeline
        # B has a distinct launch (a research job; no canonical publish, no
        # working-factor row, no CAS) but shares the SAME idempotent-confirm
        # token, exactly-once launch, attempt lineage and rejoin machinery, so
        # it dispatches to its own confirm body with this already-loaded +
        # reconciled record under the same (reentrant) store lock. A kind whose
        # plan declares NO confirm launch is refused by name here: falling
        # through to the factor_study body would run validate_idea for it and
        # address a "compute" stage its stage set does not contain.
        plan = _kind_plan(record.kind)
        if plan.confirm_launch == "research_run_once":
            return _confirm_rd_pipeline(
                config,
                store,
                record,
                nonce=nonce,
                version=version,
                job_manager=job_manager,
                rd_config=rd_config,
                parameters=parameters,
            )
        if plan.confirm_launch != "validate_idea":
            raise PipelineConflictError(
                f"pipeline kind {record.kind!r} has no confirm launch path in this build; "
                "its stage vocabulary and kind plan are in place but nothing here can start its run"
            )
        if record.confirm.nonce == nonce and record.confirm.version == version and record.confirm.confirmed_at is not None:
            replay_effective = _flat_parameters_only({**dict(record.parameters), **(parameters or {})})
            if replay_effective == (record.confirmed_parameters or {}):
                return record
            raise PipelineConflictError(
                "confirm token already consumed with a different parameter payload; "
                "reload the pipeline to see what actually launched"
            )
        if record.status not in {"draft", "awaiting_confirm"}:
            raise PipelineConflictError(f"pipeline {pipeline_id} is not awaiting confirmation (status={record.status})")
        if record.cleanup_pending:
            # Defense-in-depth twin of the retry gate (rv2 round): never
            # launch a compute over a working id whose prior-attempt cleanup
            # is still owed -- a later cleanup retry would delete the live
            # attempt's artifacts.
            raise PipelineConflictError(
                f"pipeline {pipeline_id} has unfinished working-artifact cleanup; cannot launch"
            )
        if record.confirm.nonce != nonce or record.confirm.version != version:
            raise PipelineConflictError("stale confirm token; reload the pipeline and try again")

        effective_parameters = dict(record.parameters)
        if parameters:
            effective_parameters.update(parameters)
        effective_parameters = _flat_parameters_only(effective_parameters)
        # Pure function of the PERSISTED baseline artifact + the effective
        # values (re-verify RV-F8): no job-manager lookup, no idea-text
        # re-read, so a restart between create and confirm cannot move a
        # single badge.
        provenance = derive_current_provenance(
            baseline=record.baseline_provenance,
            factor=record.factor,
            current_parameters=effective_parameters,
        )
        # SE-ix seam (see module header): CALLED here so the wiring and hash
        # contract are frozen now; the default returns "" with the SE module
        # absent, so the reserved slot stays empty and input_hash is stable.
        planning_influence_hash = capture_planning_influence(store=store, record=record)
        # Recompute input_hash from the EFFECTIVE (post-override) parameters:
        # it must fingerprint what is actually about to run, not the
        # pre-edit draft default.
        input_hash = _compute_input_hash(
            factor=record.factor,
            parameters=effective_parameters,
            parse_job_id=next((ref.job_id for ref in record.artifact_refs if ref.kind == "parse"), ""),
            parser_source=str(record.parser.get("source", "")),
            rd_config=rd_config,
            planning_influence_hash=planning_influence_hash,
        )
        record = record.with_updates(input_hash=input_hash, planning_influence_hash=planning_influence_hash)
        factor = _working_factor(record)

        # Exactly-once launch: reserve -> durably save "running" -> start.
        # attempt.number counts prior durable launches for THIS pipeline
        # (phase-review F1) -- a fresh pipeline's first confirm has zero
        # compute_job refs yet, so its launch is attempt 1; a post-retry
        # reconfirm already has one (or more), so it becomes attempt 2, 3, ...
        prior_launches = sum(1 for ref in record.artifact_refs if ref.kind == plan.run_job_ref_kind)
        child_job_id = job_manager.reserve_id()
        now = _utc_now()
        attempt_number = prior_launches + 1
        running_record = record.with_updates(
            parameters=effective_parameters,
            confirmed_parameters=effective_parameters,
            confirm=ConfirmState(nonce=record.confirm.nonce, version=record.confirm.version, confirmed_at=now),
            provenance=tuple(entry.to_dict() for entry in provenance),
            attempt=AttemptState(number=attempt_number, parent_run_id=record.attempt.parent_run_id),
            artifact_refs=record.artifact_refs
            + (ArtifactRef(kind=plan.run_job_ref_kind, job_id=child_job_id),),
            # CAS baseline for the completion-time publish (re-verify RV-F1):
            # whatever the canonical registry row holds at THIS freeze moment
            # is what publish may later replace -- any concurrent change in
            # between blocks the publish instead of being clobbered.
            canonical_baseline_fingerprint=_canonical_row_fingerprint(
                config, str(record.factor.get("factor_id", ""))
            ),
        )
        running_record = _advance_stage(running_record, "confirm", status="completed", ended_at=now)
        running_record = _advance_stage(
            running_record, plan.run_stage_id, status="active", started_at=now, child_job_id=child_job_id
        )
        running_record = _transition(running_record, "running")
        running_record = store.save(
            running_record,
            event="confirmed",
            detail={"nonce": nonce, "version": version, "child_job_id": child_job_id},
        )
        frozen_input_hash = running_record.input_hash

        def _compute_and_mark(cancel_event: Any) -> dict[str, Any]:
            # Runs in the compute job's own thread. The completion artifact
            # is the durable, attempt-scoped completion proof (re-verify
            # RV-F4), written AFTER the workflow succeeds and BEFORE the job
            # manager flips the job to completed -- so "artifact exists" is
            # never ahead of the truth, and a restart that wipes the job
            # manager can still prove (and serve) this attempt's result.
            result = run_idea_validation_workflow(
                config,
                factor,
                parser=record.parser,
                parameters=effective_parameters,
                rd_config=rd_config,
                cancel_event=cancel_event,
            )
            # PUBLIC-sanitized before persisting (rv2 round: the raw workflow
            # result carries absolute artifact paths; the job manager applies
            # the same projection before storing its own copy, so the
            # artifact and the live job serve identical, leak-free payloads).
            from quant_forge.apps.web import server as _server

            public_result = _server._web_public_json(result)
            # A write failure PROPAGATES (rv2 round: swallowing it let the
            # record complete from job memory and silently lose its report at
            # the next restart). The workflow's own RunIndex rows stand --
            # computed truth is recorded -- but this pipeline pauses honestly
            # (paused_failure via the failed job) with retry available,
            # instead of claiming a durable completion it cannot prove.
            _write_completion_artifact(
                store,
                pipeline_id=pipeline_id,
                child_job_id=child_job_id,
                attempt=attempt_number,
                input_hash=frozen_input_hash,
                result=public_result,
            )
            return result

        try:
            job_manager.start(
                "validate_idea",
                _compute_and_mark,
                job_id=child_job_id,
            )
        except Exception as exc:
            LOGGER.warning("job launch failed for pipeline %s after durable save", pipeline_id, exc_info=True)
            failed_record = _with_cleanup(running_record, config)
            failed_record = _advance_stage(
                failed_record, plan.run_stage_id, status="failed", ended_at=_utc_now()
            )
            failed_record = _transition(
                failed_record,
                "paused_failure",
                failure=FailureState(
                    stage_id=plan.run_stage_id, reason_code=f"LAUNCH_FAILED: {exc}"[:200]
                ),
                failed_attempts=failed_record.failed_attempts + 1,
            )
            return store.save(failed_record, event="launch_failed")
        return running_record


def _confirm_rd_pipeline(
    config: QuantForgeConfig,
    store: PipelineStore,
    record: PipelineRecord,
    *,
    nonce: str,
    version: int,
    job_manager: _WebJobManager,
    rd_config: ResearchLoopConfig,
    parameters: dict[str, Any] | None = None,
) -> PipelineRecord:
    """Confirm gate + exactly-once launch for pipeline B (rd_optimize).

    Called under ``store.lock`` (reentrant) with an already-loaded+reconciled
    ``record`` (see :func:`confirm_pipeline`). Reuses the SAME idempotent-token
    contract, exactly-once launch ordering, and attempt lineage as
    factor_study, but launches the research job (``run_research_once_workflow``)
    instead of the validate chain, and has NO canonical publish, NO working
    factor row, and NO CAS baseline -- pipeline B ranks candidates; it never
    overwrites a registry factor (promotion is G3, outside the sidecar).

    Rounds are re-validated SERVER-SIDE here from the EFFECTIVE (post-override)
    parameters, so an out-of-range value can never launch even if the client
    skipped its own check and even if the create-time value was later edited.
    """

    pipeline_id = record.pipeline_id
    plan = _kind_plan(record.kind)
    if record.confirm.nonce == nonce and record.confirm.version == version and record.confirm.confirmed_at is not None:
        replay_effective = _rd_parameters_only({**dict(record.parameters), **(parameters or {})})
        if replay_effective == (record.confirmed_parameters or {}):
            return record
        raise PipelineConflictError(
            "confirm token already consumed with a different parameter payload; "
            "reload the pipeline to see what actually launched"
        )
    if record.status not in {"draft", "awaiting_confirm"}:
        raise PipelineConflictError(f"pipeline {pipeline_id} is not awaiting confirmation (status={record.status})")
    if record.confirm.nonce != nonce or record.confirm.version != version:
        raise PipelineConflictError("stale confirm token; reload the pipeline and try again")

    effective_parameters = _rd_parameters_only({**dict(record.parameters), **(parameters or {})})
    # Server-authoritative revalidation of the EFFECTIVE values (WORKORDER pin):
    # rounds 1..MAX_RD_ITERATIONS, candidates 1..MAX_RD_CANDIDATES_PER_ROUND,
    # objective in the known set -- all rechecked here so an out-of-range value
    # can never launch even if the client skipped its own check or the
    # create-time value was later edited.
    effective_parameters["rounds"] = _validate_rd_rounds(effective_parameters.get("rounds"))
    effective_parameters["candidates_per_round"] = _validate_rd_candidates(
        effective_parameters.get("candidates_per_round")
    )
    if not str(effective_parameters.get("objective") or "").strip():
        effective_parameters["objective"] = str(rd_config.objective)
    else:
        effective_parameters["objective"] = _validate_rd_objective(effective_parameters["objective"], rd_config)

    provenance = derive_rd_current_provenance(
        baseline=record.baseline_provenance, current_parameters=effective_parameters
    )
    # SE-ix seam (see module header): CALLED so the wiring/hash contract are
    # frozen now; default returns "" with the SE module absent, so the
    # reserved slot stays empty and input_hash is stable.
    planning_influence_hash = capture_planning_influence(store=store, record=record)
    input_hash = _rd_input_hash(
        seed_factor_id=str(record.factor.get("factor_id", "")),
        rd_parameters=effective_parameters,
        rd_config=rd_config,
        planning_influence_hash=planning_influence_hash,
    )
    record = record.with_updates(input_hash=input_hash, planning_influence_hash=planning_influence_hash)

    # Exactly-once launch: reserve -> durably save "running" -> start. attempt
    # counts prior durable launches of THIS pipeline's run job (never a bare
    # retry click), the SAME contract as factor_study's compute_job counting.
    prior_launches = sum(1 for ref in record.artifact_refs if ref.kind == plan.run_job_ref_kind)
    child_job_id = job_manager.reserve_id()
    now = _utc_now()
    attempt_number = prior_launches + 1
    seed_factor_id = str(record.factor.get("factor_id", ""))
    objective = str(effective_parameters["objective"])
    max_candidates = int(effective_parameters["candidates_per_round"])
    rounds = int(effective_parameters["rounds"])
    running_record = record.with_updates(
        parameters=effective_parameters,
        confirmed_parameters=effective_parameters,
        confirm=ConfirmState(nonce=record.confirm.nonce, version=record.confirm.version, confirmed_at=now),
        provenance=tuple(entry.to_dict() for entry in provenance),
        attempt=AttemptState(number=attempt_number, parent_run_id=record.attempt.parent_run_id),
        artifact_refs=record.artifact_refs
        + (ArtifactRef(kind=plan.run_job_ref_kind, job_id=child_job_id),),
    )
    running_record = _advance_stage(running_record, "confirm", status="completed", ended_at=now)
    running_record = _advance_stage(
        running_record, plan.run_stage_id, status="active", started_at=now, child_job_id=child_job_id
    )
    running_record = _transition(running_record, "running")
    running_record = store.save(
        running_record,
        event="confirmed",
        detail={"nonce": nonce, "version": version, "child_job_id": child_job_id, "rounds": rounds},
    )
    frozen_input_hash = running_record.input_hash

    def _run_and_mark(cancel_event: Any) -> dict[str, Any]:
        result = run_research_once_workflow(
            config,
            seed_factor_id,
            objective=objective,
            max_candidates=max_candidates,
            iterations=rounds,
            rd_config=rd_config,
            cancel_event=cancel_event,
        )
        from quant_forge.apps.web import server as _server

        public_result = _server._web_public_json(result)
        # Attempt-scoped durable completion proof (re-verify RV-F4): written
        # AFTER the workflow succeeds and BEFORE the job flips to completed, so
        # a restart that wipes the job manager can still recognize and serve
        # this leaderboard. A write failure propagates -> the pipeline pauses
        # honestly instead of claiming a completion it cannot prove.
        _write_completion_artifact(
            store,
            pipeline_id=pipeline_id,
            child_job_id=child_job_id,
            attempt=attempt_number,
            input_hash=frozen_input_hash,
            result=public_result,
        )
        return result

    try:
        job_manager.start("research_run_once", _run_and_mark, job_id=child_job_id)
    except Exception as exc:
        LOGGER.warning("rd job launch failed for pipeline %s after durable save", pipeline_id, exc_info=True)
        failed_record = _advance_stage(
            running_record, plan.run_stage_id, status="failed", ended_at=_utc_now()
        )
        failed_record = _transition(
            failed_record,
            "paused_failure",
            failure=FailureState(
                stage_id=plan.run_stage_id, reason_code=f"LAUNCH_FAILED: {exc}"[:200]
            ),
            failed_attempts=failed_record.failed_attempts + 1,
        )
        return store.save(failed_record, event="launch_failed")
    return running_record


def cancel_pipeline(store: PipelineStore, pipeline_id: str, *, job_manager: _WebJobManager, config: QuantForgeConfig) -> PipelineRecord:
    """Abort a pipeline -- but honor a completion that won the cancel race (F6).

    The job manager's completion-wins contract (``apps/web/jobs.py`` PF-F4:
    a worker that already returned a result is marked ``completed`` even with
    cancel requested) means a bare "request cancel, then unconditionally
    mark aborted" throws away a result the run actually produced. So after
    requesting cancel this RE-RECONCILES under the same lock: if the child
    job completed (or failed) concurrently with the cancel, the SAME
    reconciliation a GET would apply folds that outcome in -- a completion is
    honored (leaderboard/report + durable artifact), a failure pauses -- and
    only a run that is genuinely still un-terminal is aborted in place. Works
    for BOTH kinds (factor_study compute + rd_optimize run) through the shared
    run-stage plan.
    """

    with store.lock:
        record = store.load(pipeline_id)
        record = _reconcile(record, store=store, job_manager=job_manager, config=config)
        if record.status in TERMINAL_PIPELINE_STATUSES:
            return record  # cancel is idempotent, not an error, once terminal
        run_stage = record.stage(_kind_plan(record.kind).run_stage_id)
        if record.status == "running" and run_stage.child_job_id is not None:
            try:
                job_manager.cancel(run_stage.child_job_id)
            except KeyError:
                pass
            # Re-reconcile: a job that completed/failed in the window between
            # the reconcile above and this cancel (or that the completion-wins
            # semantics let finish despite the cancel) is folded in by the
            # SAME code path a GET uses. If that lands the pipeline in a
            # terminal state (completed) or paused_failure, honor it verbatim
            # instead of overwriting a real result with an abort.
            record = _reconcile(record, store=store, job_manager=job_manager, config=config)
            if record.status in TERMINAL_PIPELINE_STATUSES:
                return record
            if record.status == "paused_failure":
                return record
            # F2 (cancellation is NON-terminal): the child job is still
            # un-terminal here (``cancel_requested`` -- the worker has NOT yet
            # observed the stop, or is inside the completion-wins window where
            # jobs.py PF-F4 lets an already-returned result still land as
            # ``completed``). Terminalizing and cleaning up NOW would (a) delete
            # the working factor row / cached values out from under a run that
            # may still finish and (b) orphan a completion the worker is about
            # to durably record. So DEFER both the working-artifact cleanup and
            # the ``aborted`` transition to reconciliation: the next read folds
            # in the child job's real terminal status -- ``cancelled`` cleans +
            # aborts, ``completed`` honors the completion (the cancel becomes a
            # truthful no-op). The cancel is already requested on the child job;
            # nothing on the record changed, so the still-running snapshot
            # stands untouched.
            return record
        # Non-running (draft / awaiting_confirm / paused_failure): there is no
        # live worker to wait for, so cancel is immediately terminal -- clean
        # the working artifacts and abort in place, exactly as before.
        record = _with_cleanup(record, config)
        record = _transition(record, "aborted")
        return store.save(record, event="cancelled")


def retry_pipeline(store: PipelineStore, pipeline_id: str, *, job_manager: _WebJobManager, config: QuantForgeConfig) -> PipelineRecord:
    """paused_failure -> awaiting_confirm: re-run the SAME frozen inputs.

    Phase-review F8: reconciles expiry UNDER THE LOCK first -- a direct
    ``/retry`` on an already-expired ``paused_failure`` record must not
    resurrect it just because this call path skipped the reconciliation a
    ``GET``/``list`` would have applied; ``_reconcile`` handles that here
    before the status check below can even see a (now-stale) "paused_failure".

    The parse stage stays ``completed`` (reused, per spec §2.3 "retry
    declares which completed stages are reused" -- P1 has exactly one
    reusable completed stage); compute resets to ``pending`` for a fresh
    attempt; a new nonce/version is issued so the re-confirmation is its own
    idempotency epoch, distinct from the failed attempt's token.
    ``attempt.number`` is NOT touched here (phase-review F1) -- only a
    durably-launched compute spends an attempt number, in
    :func:`confirm_pipeline`.
    """

    with store.lock:
        record = store.load(pipeline_id)
        record = _reconcile(record, store=store, job_manager=job_manager, config=config)
        if record.status != "paused_failure":
            raise PipelineConflictError(f"pipeline {pipeline_id} is not paused (status={record.status})")
        if record.cleanup_pending:
            # rv2 round: a pending old-attempt cleanup must NEVER escape into
            # a new attempt -- the retry reuses the SAME working id, so a
            # delayed cleanup firing later would delete the live attempt's
            # row and cached values out from under it. One synchronous
            # attempt here; still failing -> the retry is refused, honestly.
            if _cleanup_working_artifacts(config, record):
                record = store.save(record.with_updates(cleanup_pending=False), event="cleanup_retried")
            else:
                raise PipelineConflictError(
                    f"pipeline {pipeline_id} has unfinished working-artifact cleanup; retry once it clears"
                )
        now = _utc_now()
        record = _reset_stage(record, _kind_plan(record.kind).run_stage_id)
        # F7 (retry coherence): the prior confirm marked the confirm stage
        # `completed`; re-entering the gate must reset AND reactivate it, or
        # the aggregate would report `awaiting_confirm` while the confirm
        # stage row still reads `completed` with a stale ended_at
        # (incoherent). Reset clears the prior timestamps; advance re-opens
        # the gate. Confirm is the gate stage of BOTH kinds (factor_study AND
        # rd_optimize), so this keeps the stage strip truthful for each.
        record = _reset_stage(record, "confirm")
        record = _advance_stage(record, "confirm", status="active", started_at=now)
        record = _transition(record, "awaiting_confirm")
        record = record.with_updates(
            confirm=ConfirmState(nonce=_new_nonce(), version=record.confirm.version + 1),
            expires_at=_expires_at(now, DEFAULT_DRAFT_TTL_SECONDS),
        )
        return store.save(record, event="retried")


def update_pipeline_parameters(
    store: PipelineStore,
    pipeline_id: str,
    parameters: dict[str, Any],
    *,
    job_manager: _WebJobManager,
    config: QuantForgeConfig,
) -> PipelineRecord:
    """Edit the pipeline's current draft parameters.

    Pre-confirm (``draft``/``awaiting_confirm``) this IS the live draft, and
    the edit ROTATES the confirm token (phase-review F1/F2): any other tab's
    currently-held nonce/version becomes provably stale, so it can never
    confirm the values it never saw. While ``running``/``paused_failure``
    this is exactly the "仅用于下次尝试" case (spec §2.3 freeze semantics):
    ``confirmed_parameters`` -- what the in-flight/completed run actually
    used -- is untouched, and there is no live confirm token to protect, so
    no rotation happens there.
    """

    with store.lock:
        record = store.load(pipeline_id)
        record = _reconcile(record, store=store, job_manager=job_manager, config=config)
        # F7 (reject-by-kind): /parameters is factor-study machinery (the
        # 11-field simulation/backtest draft grid). Pipeline B (rd_optimize)
        # sets its three fields (rounds / candidates_per_round / objective) on
        # its OWN confirm card, applied at confirm time -- it has no /parameters
        # draft-mutation surface, so a kind=rd_optimize call is a clean 400
        # rather than a kind-blind mutation of a record that has none of those
        # fields.
        if record.kind != "factor_study":
            raise PipelineConflictError(
                f"/parameters is not available for kind={record.kind}; "
                "pipeline B (rd_optimize) edits rounds/candidates/objective on its own confirm card"
            )
        if record.status in TERMINAL_PIPELINE_STATUSES:
            raise PipelineConflictError(
                f"pipeline {pipeline_id} is terminal (status={record.status}); start a new pipeline instead"
            )
        merged = dict(record.parameters)
        merged.update(parameters)
        merged = _flat_parameters_only(merged)
        # Pure function of the persisted baseline artifact (re-verify RV-F8):
        # an unrelated field's badge can never move because THIS field
        # changed, and a restart can never move any badge at all.
        provenance = derive_current_provenance(
            baseline=record.baseline_provenance,
            factor=record.factor,
            current_parameters=merged,
        )
        changes: dict[str, Any] = {
            "parameters": merged,
            "provenance": tuple(entry.to_dict() for entry in provenance),
        }
        if record.status in {"draft", "awaiting_confirm"}:
            changes["confirm"] = ConfirmState(nonce=_new_nonce(), version=record.confirm.version + 1)
        record = record.with_updates(**changes)
        return store.save(record, event="parameters_updated")


def fork_pipeline_from_failure(
    store: PipelineStore,
    pipeline_id: str,
    *,
    job_manager: _WebJobManager,
    config: QuantForgeConfig,
    rd_config: ResearchLoopConfig,
    parameters: dict[str, Any] | None = None,
) -> PipelineRecord:
    """paused_failure "edit" exit (spec §2.3 / phase-review F7): forks the
    frozen inputs into a brand-new draft pipeline with its own attempt
    lineage, and terminalizes (aborts) the old one -- it does not mutate the
    failed record in place, so the failed attempt's own history stays
    intact and inspectable under its own id.

    ``parameters`` carries the user's PENDING edits from the paused card
    (re-verify RV-F9: the old fork posted ``{}`` and silently discarded the
    edits the user could literally see on screen). The fork's provenance
    baseline is the FROZEN inputs of the failed attempt, so every carried
    edit shows up as ``human_override`` against what actually ran -- exactly
    what "edit and fork" means.
    """

    # P2-F3 (agent sidecar clarify -- documented decision, NOT built here): the
    # forked child starts with NO clarify session, so a blocking question left
    # open on the PARENT is not carried onto it. Intentional and safe today: a
    # fork is only reachable from paused_failure (AFTER a confirm already ran)
    # and is itself a human re-confirmation that supersedes the parent's
    # interview; nothing in this build poses clarify questions on a pre-confirm
    # pipeline anyway (the pose path is the deferred live LLM loop). IF that
    # pose path later needs a parent's open blocking question to survive an
    # edit-fork, that carry-over lands in P3 with the live sidecar orchestration.
    with store.lock:
        old = store.load(pipeline_id)
        old = _reconcile(old, store=store, job_manager=job_manager, config=config)
        # F7 (reject-by-kind): /fork is factor-study's documented "edit"
        # exit from a paused failure -- it forks the frozen 11-field inputs
        # into a new draft. Pipeline B (rd_optimize) is leaderboard-driven;
        # its edit exit is the formula card / a fresh run, NOT a fork. Reject
        # a kind=rd_optimize fork with a clean 400 (checked before the status
        # gate so the reason names the real cause).
        if old.kind != "factor_study":
            raise PipelineConflictError(
                f"/fork is not available for kind={old.kind}; "
                "pipeline B (rd_optimize)'s edit exit is the formula card / a new run, not a fork"
            )
        if old.status != "paused_failure":
            raise PipelineConflictError(f"pipeline {pipeline_id} is not paused (status={old.status})")

        # The frozen inputs are what the failed attempt actually ran with --
        # confirmed_parameters if a compute genuinely launched, else the
        # draft parameters at the point of failure (e.g. a launch failure
        # before any job started).
        frozen_parameters = _flat_parameters_only(
            dict(old.confirmed_parameters if old.confirmed_parameters is not None else old.parameters)
        )
        # The fork draft starts from the parent's DURABLE next-attempt draft
        # (old.parameters -- the 「仅用于下次尝试」 edits the user already
        # SAVED), then the request's unsaved overrides on top (rv2 round: a
        # refreshed client posts no local overrides, and starting from the
        # frozen inputs silently reverted saved edits the user could see).
        draft_parameters = _flat_parameters_only({**dict(old.parameters), **(parameters or {})})
        now = _utc_now()
        new_pipeline_id = _new_pipeline_id()
        input_hash = _compute_input_hash(
            factor=old.factor,
            parameters=draft_parameters,
            parse_job_id=next((ref.job_id for ref in old.artifact_refs if ref.kind == "parse"), ""),
            parser_source=str(old.parser.get("source", "")),
            rd_config=rd_config,
            planning_influence_hash=old.planning_influence_hash,
        )
        # The fork's baseline is the parent's CURRENT badge truth evaluated
        # at the frozen inputs: an origin inherited from the parent's own
        # persisted baseline (including an inherited human_override with its
        # parent_value) stays honest, and nothing is re-derived from parser
        # mode or idea text (re-verify RV-F8).
        fork_baseline = tuple(
            entry.to_dict()
            for entry in derive_current_provenance(
                baseline=old.baseline_provenance, factor=old.factor, current_parameters=frozen_parameters
            )
        )
        provenance = derive_current_provenance(
            baseline=fork_baseline, factor=old.factor, current_parameters=draft_parameters
        )
        forked = PipelineRecord(
            pipeline_id=new_pipeline_id,
            kind=old.kind,
            created_at=now,
            expires_at=_expires_at(now, DEFAULT_DRAFT_TTL_SECONDS),
            status="draft",
            stages=initial_stages_for(old.kind),
            input_hash=input_hash,
            planning_influence_hash=old.planning_influence_hash,
            confirm=ConfirmState(nonce=_new_nonce(), version=1),
            parser=old.parser,
            factor=old.factor,
            parameters=draft_parameters,
            source_text=old.source_text,
            original_parameters=frozen_parameters,
            warnings=old.warnings,
            provenance=tuple(entry.to_dict() for entry in provenance),
            baseline_provenance=fork_baseline,
            attempt=AttemptState(number=1, parent_run_id=old.pipeline_id),
            working_factor_id=_working_factor_id_for(new_pipeline_id, str(old.factor["factor_id"])),
            artifact_refs=tuple(ref for ref in old.artifact_refs if ref.kind == "parse"),
        )
        forked = _advance_stage(forked, "parse", status="completed", started_at=now, ended_at=now)
        forked = _transition(forked, "awaiting_confirm")
        forked = _advance_stage(forked, "confirm", status="active", started_at=now)
        forked = store.save(forked, event="forked_from_failure", detail={"parent_run_id": old.pipeline_id})

        old = _with_cleanup(old, config)
        old = _transition(old, "aborted")
        store.save(old, event="aborted_by_fork", detail={"forked_to": new_pipeline_id})
        return forked


def create_pipeline_as_fallback(
    store: PipelineStore,
    *,
    job_manager: _WebJobManager,
    rd_config: ResearchLoopConfig,
    parent_pipeline_id: str,
    config: QuantForgeConfig,
) -> PipelineRecord:
    """paused_failure "fall back to rule parse" exit, fully server-side
    (re-verify RV-F10).

    The rule parse runs HERE, against the failed pipeline's own persisted
    ``source_text`` -- the client no longer supplies a parse job id at all,
    so it can neither substitute an unrelated parse (the old crafted-request
    hole) nor be stranded by job-manager pruning (the old durability hole).
    The freshly parsed artifact seeds a brand-new pipeline with new parse
    provenance and ``parent_run_id`` lineage back to the failed pipeline,
    which is then terminalized (aborted) -- atomically from the caller's
    point of view, so there is no window where both the new draft and the
    old failure are simultaneously "live" without the lineage link recorded.
    """

    # P2-F3 (agent sidecar clarify -- documented decision, NOT built here): the
    # fallback child starts with NO clarify session, so a blocking question left
    # open on the PARENT is not carried onto it. Intentional and safe today: a
    # fallback is only reachable from paused_failure (AFTER a confirm already
    # ran) and is itself a human re-confirmation that supersedes the parent's
    # interview; nothing in this build poses clarify questions on a pre-confirm
    # pipeline anyway (the pose path is the deferred live LLM loop). IF that pose
    # path later needs a parent's open blocking question to survive a fallback,
    # that carry-over lands in P3 with the live sidecar orchestration.
    # Deferred import mirrors api.py::_parse_idea's own pattern: the server
    # facade imports widely and a module-level import here would risk an
    # apps.web import cycle.
    from quant_forge.apps.web import server as _server

    with store.lock:
        old = store.load(parent_pipeline_id)
        old = _reconcile(old, store=store, job_manager=job_manager, config=config)
        if old.status != "paused_failure":
            raise PipelineConflictError(f"pipeline {parent_pipeline_id} is not paused (status={old.status})")
        text = _source_text_for(old)
        if not text.strip():
            raise PipelineConflictError(
                f"pipeline {parent_pipeline_id} carries no persisted idea text; rule fallback is unavailable"
            )
        parse_result = _server.run_idea_parse_workflow(
            config,
            text,
            parser_mode="rule",
            llm_provider=None,
            rd_config=rd_config,
        )
        parser = dict(parse_result.get("parser") or {})
        factor = dict(parse_result.get("factor") or {})
        parameters = _flat_parameters_only(dict(parse_result.get("parameters") or {}))
        warnings = tuple(str(item) for item in (parse_result.get("warnings") or ()))
        if not factor.get("factor_id"):
            raise PipelineConflictError("rule fallback parse produced no factor_id; cannot create a pipeline")

        now = _utc_now()
        planning_influence_hash = old.planning_influence_hash
        input_hash = _compute_input_hash(
            factor=factor,
            parameters=parameters,
            parse_job_id="",  # server-side synchronous parse: no job id exists
            parser_source=str(parser.get("source", "")),
            rd_config=rd_config,
            planning_influence_hash=planning_influence_hash,
        )
        baseline_provenance = tuple(
            entry.to_dict()
            for entry in derive_baseline_provenance(parser=parser, factor=factor, parameters=parameters, text=text)
        )
        provenance = derive_current_provenance(
            baseline=baseline_provenance, factor=factor, current_parameters=parameters
        )
        new_pipeline_id = _new_pipeline_id()
        new_record = PipelineRecord(
            pipeline_id=new_pipeline_id,
            kind=old.kind,
            created_at=now,
            expires_at=_expires_at(now, DEFAULT_DRAFT_TTL_SECONDS),
            status="draft",
            stages=initial_stages_for(old.kind),
            input_hash=input_hash,
            planning_influence_hash=planning_influence_hash,
            confirm=ConfirmState(nonce=_new_nonce(), version=1),
            parser=parser,
            factor=factor,
            parameters=parameters,
            source_text=text,
            original_parameters=parameters,
            warnings=warnings,
            provenance=tuple(entry.to_dict() for entry in provenance),
            baseline_provenance=baseline_provenance,
            attempt=AttemptState(number=1, parent_run_id=parent_pipeline_id),
            working_factor_id=_working_factor_id_for(new_pipeline_id, str(factor["factor_id"])),
            artifact_refs=(ArtifactRef(kind="parse", job_id=None),),
        )
        new_record = _advance_stage(new_record, "parse", status="completed", started_at=now, ended_at=now)
        new_record = _transition(new_record, "awaiting_confirm")
        new_record = _advance_stage(new_record, "confirm", status="active", started_at=now)
        new_record = store.save(new_record, event="created_as_rule_fallback", detail={"parent_run_id": parent_pipeline_id})

        old = _with_cleanup(old, config)
        old = _transition(old, "aborted")
        store.save(old, event="aborted_by_rule_fallback", detail={"forked_to": new_record.pipeline_id})
        return new_record


def create_pipeline_from_edited_formula(
    store: PipelineStore,
    *,
    job_manager: _WebJobManager,
    config: QuantForgeConfig,
    rd_config: ResearchLoopConfig,
    parent_pipeline_id: str,
    formula: Any,
    universe_filters: tuple[str, ...] | None = None,
    horizon_days: Any = None,
) -> PipelineRecord:
    """Editable-formula compare loop (spec §5.3/§5.4, F2d): a validated edit the
    user RUNS becomes a NEW immutable factor_study run, branched from the parent.

    The compare loop's ``edited_by=human`` is derived SERVER-side by fingerprint
    comparison, never client-asserted: the new pipeline's formula-field
    provenance becomes ``human_override`` (with ``parent_value`` = the parent's
    formula) exactly when the edited formula's value differs from the parent's,
    through the SAME ``derive_current_provenance`` fingerprint machinery every
    other edit uses -- there is no client-supplied ``edited_by`` anywhere.

    Wires the existing pre-validate -> confirm flow: the edit must PASS
    read-only pre-validation (``status="ready"``) before it can create a run --
    an unknown-operator / unparseable edit is not runnable and is refused here
    (the user resolves it via operator-draft review first), so a run is only
    ever created from a canonicalized, gate-passing formula. The parent may be
    any factor_study pipeline in any status (deliberately unguarded): the
    confirm-card formula edit branches BEFORE the parent ever runs, while the
    report compare loop branches after completion. The parent is NOT
    terminalized -- the compare surface needs both runs side by side.
    """

    with store.lock:
        parent = store.load(parent_pipeline_id)
        parent = _reconcile(parent, store=store, job_manager=job_manager, config=config)
        if parent.kind != "factor_study":
            raise PipelineConflictError(
                f"editable-formula runs branch from a factor_study pipeline only (parent is kind={parent.kind})"
            )
        parent_horizon = parent.factor.get("horizon_days")
        effective_horizon = horizon_days if horizon_days is not None else parent_horizon
        if effective_horizon is None:
            effective_horizon = 5
        parent_filters = tuple(str(item) for item in (parent.factor.get("universe_filters") or ()))
        new_filters = tuple(str(item) for item in universe_filters) if universe_filters is not None else parent_filters
        # Pre-validate the edit (strict types F8; canonical fingerprint F3).
        # ONLY a runnable ("ready") edit may create a run -- an unknown operator
        # returns a review packet and must go through operator-draft review, so
        # pre-validation is the gate the confirm flow is wired behind.
        pre = pre_validate_formula(
            formula,
            name=str(parent.factor.get("name", "")),
            horizon_days=effective_horizon,
            universe_filters=new_filters,
        )
        if pre.get("status") != "ready":
            raise PipelineConflictError(
                f"edited formula is not runnable (status={pre.get('status')}); "
                "resolve it via pre-validation / operator-draft review before running"
            )
        # Build the new factor: the parent's factor with the edited formula (and
        # any edited filters/horizon). A NEW canonical factor_id derived from the
        # edited formula's CANONICAL fingerprint, so this run publishes its OWN
        # canonical factor and never overwrites the parent's on success.
        new_factor = dict(parent.factor)
        new_factor["formula"] = formula
        new_factor["universe_filters"] = list(new_filters)
        new_factor["horizon_days"] = int(effective_horizon)
        new_factor["status"] = "draft"
        new_factor["factor_id"] = f"FTR_EDIT_{pre['fingerprint'][:16]}"
        parameters = _flat_parameters_only(dict(parent.parameters))

        now = _utc_now()
        planning_influence_hash = parent.planning_influence_hash
        input_hash = _compute_input_hash(
            factor=new_factor,
            parameters=parameters,
            parse_job_id="",  # server-side edit: no parse job id exists
            parser_source=str(parent.parser.get("source", "")),
            rd_config=rd_config,
            planning_influence_hash=planning_influence_hash,
        )
        # Baseline = the parent's CURRENT badge truth (formula = parent's
        # formula), so deriving the new run's provenance against the EDITED
        # factor makes the formula human_override <=> the fingerprint changed
        # (server-derived edited_by, parent_value = the parent's formula).
        edit_baseline = tuple(
            entry.to_dict()
            for entry in derive_current_provenance(
                baseline=parent.baseline_provenance, factor=parent.factor, current_parameters=parameters
            )
        )
        provenance = derive_current_provenance(baseline=edit_baseline, factor=new_factor, current_parameters=parameters)
        new_pipeline_id = _new_pipeline_id()
        new_record = PipelineRecord(
            pipeline_id=new_pipeline_id,
            kind="factor_study",
            created_at=now,
            expires_at=_expires_at(now, DEFAULT_DRAFT_TTL_SECONDS),
            status="draft",
            stages=initial_stages_for("factor_study"),
            input_hash=input_hash,
            planning_influence_hash=planning_influence_hash,
            confirm=ConfirmState(nonce=_new_nonce(), version=1),
            parser=parent.parser,
            factor=new_factor,
            parameters=parameters,
            source_text=parent.source_text,
            original_parameters=parameters,
            warnings=parent.warnings,
            provenance=tuple(entry.to_dict() for entry in provenance),
            baseline_provenance=edit_baseline,
            attempt=AttemptState(number=1, parent_run_id=parent.pipeline_id),
            working_factor_id=_working_factor_id_for(new_pipeline_id, str(new_factor["factor_id"])),
            artifact_refs=tuple(ref for ref in parent.artifact_refs if ref.kind == "parse"),
        )
        new_record = _advance_stage(new_record, "parse", status="completed", started_at=now, ended_at=now)
        new_record = _transition(new_record, "awaiting_confirm")
        new_record = _advance_stage(new_record, "confirm", status="active", started_at=now)
        new_record = store.save(
            new_record, event="created_from_edited_formula", detail={"parent_run_id": parent.pipeline_id}
        )
        return new_record


# ---------------------------------------------------------------------------
# Editable-formula pre-validation endpoint (spec §5.3). Net-new: today's
# revalidate path runs the whole evaluation chain (apps/web/api.py); this one
# does NOT.
# ---------------------------------------------------------------------------


def pre_validate_formula(
    formula: Any,
    *,
    name: Any = "",
    horizon_days: Any = 5,
    universe_filters: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Canonicalize + ValidationGate a formula WITHOUT persisting, evaluating,
    or backtesting (spec §5.3).

    Returns a CANONICAL fingerprint of the (canonical-formula, sorted-filters,
    validated-horizon) spec -- via the operator_registry's own
    ``canonical_formula_fingerprint`` helper (F3), so alias / whitespace / case
    / filter-order variants of the SAME logical formula collapse to one
    fingerprint, and repeated calls are byte-identical. An unknown operator
    returns an ``operator_drafts`` review-packet REF (a deterministic logical
    reference derived from that canonical fingerprint, WITHOUT persisting
    anything) and NEVER hot-executes: ``ValidationGate`` only RESOLVES the
    formula against the read-only registry -- it never runs it -- and this
    endpoint never touches the evaluation/backtest chain or writes anything.

    Request types are STRICT (F8): a non-string ``formula``, a non-integer
    ``horizon_days``, or a ``universe_filters`` that is not a tuple of strings
    is a ``ValueError`` (mapped to 400), never a silent ``str()`` coercion or a
    silently-defaulted horizon.
    """

    # Strict types (F8): no str() coercion of a non-string formula, no int()
    # coercion / silent default of a bad horizon, no stringification of
    # non-string filter items. `type(...) is int` also excludes bool.
    if not isinstance(formula, str):
        raise ValueError("formula must be a string")
    if not formula.strip():
        raise ValueError("formula is required")
    if type(horizon_days) is not int or horizon_days < 1:
        raise ValueError("horizon_days must be a positive integer")
    if not all(isinstance(item, str) for item in universe_filters):
        raise ValueError("universe_filters must be a list of strings")
    horizon = horizon_days
    filters = tuple(universe_filters)
    display_name = str(name or "")
    # Canonical fingerprint: resolve to canonical operators, compact/lowercase
    # the formula, sort+compact the filters, include the validated horizon
    # (operator_registry/fingerprint.py). This is the project's shared
    # RD/cache canonical-formula fingerprint -- alias/whitespace/case/
    # filter-order variants map to the SAME value; repeat calls are identical.
    fingerprint = canonical_formula_fingerprint(formula, horizon, filters)
    base_payload: dict[str, Any] = {
        "fingerprint": fingerprint,
        "formula": formula,
        # Guarantees to the caller/renderer: pre-validation never runs the
        # formula and never persists (spec §5.3 / WORKORDER pin).
        "executed": False,
        "persisted": False,
    }
    try:
        spec = FactorSpec(
            factor_id="PREVALIDATE",
            name=display_name or "pre-validation",
            formula_dsl=formula,
            horizon_days=horizon,
            universe_filters=filters,
        )
        result = validate_factor_spec(spec)
    except Exception:
        # A malformed formula (parse error) is an honest "blocked", never a
        # 500 and never an execution. The message is generic and leak-free
        # (it describes the formula surface, carries no path/secret).
        LOGGER.debug("pre-validation rejected a malformed formula", exc_info=True)
        return {
            **base_payload,
            "status": "blocked",
            "unresolved_operators": [],
            "unresolved_fields": [],
            "blocking_reasons": ["formula is not parseable under the canonical operator registry"],
            "warnings": [],
            "unchecked": [],
        }
    payload = {
        **base_payload,
        "status": result.status,
        "unresolved_operators": list(result.unresolved_operators),
        "unresolved_fields": list(result.unresolved_fields),
        "blocking_reasons": list(result.blocking_reasons),
        "warnings": list(result.warnings),
        "unchecked": list(result.unchecked),
    }
    if result.unresolved_operators:
        payload["status"] = "review_required"
        payload["review_packet"] = {
            "channel": "operator_drafts",
            # Deterministic logical review reference derived from the CANONICAL
            # fingerprint (F3): stable across alias/whitespace variants and
            # byte-identical on repeat calls, WITHOUT persisting anything --
            # the real operator_drafts artifact is only written later by the
            # Codex/developer audit path, never by pre-validation.
            "review_ref": f"operator_drafts/prevalidate-{fingerprint[:32]}",
            "unresolved_operators": list(result.unresolved_operators),
            "hot_executed": False,
            "persisted": False,
            "note": (
                "unknown operator requires operator-draft review (Codex/developer audit); "
                "never hot-executed and not persisted by pre-validation"
            ),
        }
    return payload

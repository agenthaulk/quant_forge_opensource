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
  record. Phase-review F9: when the job manager has no memory of a child job
  (pruned, or wiped by a restart), reconciliation does not assume failure --
  it independently checks the durable ``RunIndex`` lineage for this
  pipeline's ``working_factor_id`` before concluding ``JOB_NOT_FOUND``, so a
  job that actually finished (the compute chain's own ``record_run`` calls
  already landed on disk) is recognized as completed instead of being
  rewritten as a failure.

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
picker-visibility-during-compute window open, reported here for Fable's
adjudication rather than worked around with a second store.
"""

from __future__ import annotations

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

from quant_forge.apps.web.api import _factor_from_request, run_idea_validation_workflow
from quant_forge.apps.web.jobs import _WebJobManager, _utc_now
from quant_forge.apps.web.provenance import (
    PARAMETER_FIELDS,
    assert_provenance_matches_current_values,
    derive_confirm_provenance,
)
from quant_forge.config import QuantForgeConfig
from quant_forge.core.contracts import FactorDefinition
from quant_forge.factor_library.repository import FactorRepository
from quant_forge.lineage.store import RunIndex
from quant_forge.research_loop.config import ResearchLoopConfig
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

LOGGER = logging.getLogger("quant_forge.apps.web.pipeline")

__all__ = [
    "PipelineNotFoundError",
    "PipelineConflictError",
    "PipelineStore",
    "create_pipeline",
    "get_pipeline",
    "list_active_pipelines",
    "confirm_pipeline",
    "cancel_pipeline",
    "retry_pipeline",
    "update_pipeline_parameters",
    "fork_pipeline_from_failure",
    "create_pipeline_as_fallback",
]

_PIPELINE_DIR_NAME = "pipelines"
_PIPELINE_ID_RE = re.compile(r"^PL_[0-9a-f]{32}$")
# Abandonment TTL for draft/awaiting_confirm/paused_failure (spec §12 open
# question #4, resolved for P1: a fixed, generous default). `running` is
# deliberately exempt -- a live compute must never be silently expired out
# from under itself; it resolves to completed/paused_failure through
# reconciliation when its job finishes (or JOB_NOT_FOUND on rejoin if it
# didn't survive a restart and the RunIndex has no record of it either).
DEFAULT_DRAFT_TTL_SECONDS = 24 * 3600


class PipelineNotFoundError(KeyError):
    """No pipeline exists on disk for the given (validated) pipeline_id."""


class PipelineConflictError(ValueError):
    """Illegal transition or a stale/mismatched idempotency token."""


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
    """Torn-tail-tolerant JSONL reader.

    Append-only writers can only ever leave the LAST line partially written
    (a crash mid-``write``/``flush`` truncates the tail; every earlier line
    was already fully flushed by a prior, completed append). So: a malformed
    FINAL line is treated as an expected torn tail and dropped; a malformed
    line anywhere else is NOT explicable by that failure mode and means real
    corruption, which raises rather than silently losing history (mirrors,
    and is deliberately stricter than, ``lineage/store.py::_read_jsonl``'s
    "skip any bad line" tolerance -- interior corruption there would also be
    swallowed silently, which this module treats as a bug worth surfacing
    rather than a routine crash artifact).
    """

    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    rows: list[dict[str, Any]] = []
    last_index = len(lines) - 1
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            rows.append(json.loads(stripped))
        except json.JSONDecodeError:
            if index == last_index:
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
        assert_provenance_matches_current_values(record.provenance, factor=record.factor, parameters=record.parameters)
        return record

    def list_ids(self) -> list[str]:
        if not self._root.is_dir():
            return []
        return sorted(path.stem for path in self._root.glob("PL_*.json"))

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


def _source_text_for(record: PipelineRecord, *, job_manager: _WebJobManager) -> str:
    """Re-derive the raw idea text a pipeline's parse job originally saw.

    ``PipelineRecord`` does not persist the text itself -- it is already
    captured, once, inside the ``parse`` job's own stored ``request``
    (``apps/web/jobs.py::_WebJob.request``, plumbed through by
    ``apps/web/routing.py``'s ``/api/jobs/parse-idea`` handler) -- so
    re-reading it via the SAME parse job id every pipeline already carries
    in ``artifact_refs`` is a single source of truth instead of a second,
    potentially-drifting copy. Falls back to ``""`` if the parse job has
    since been pruned from the job manager's bounded retention (a benign
    degradation: universe_filters provenance falls back to its
    parser-mode-based rule instead of the sharper user_explicit phrase
    check -- see ``apps/web/provenance.py::_origin_for_universe_filters``).
    """

    parse_job_id = next((ref.job_id for ref in record.artifact_refs if ref.kind == "parse"), None)
    if not parse_job_id:
        return ""
    try:
        parse_job = job_manager.get(parse_job_id)
    except KeyError:
        return ""
    return str((parse_job.get("request") or {}).get("text", ""))


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
    return dataclass_replace(base, factor_id=record.working_factor_id)


def _delete_working_factor(config: QuantForgeConfig, working_factor_id: str) -> None:
    """Best-effort cleanup; ``FactorRepository.delete`` itself is a safe
    no-op (returns 0) when the id was never registered, so this is cheap
    to call speculatively from every terminal-non-success path (including
    an expiring draft/awaiting_confirm pipeline whose working row was never
    actually created)."""

    if not working_factor_id:
        return
    try:
        FactorRepository(config.paths.factor_root).delete(working_factor_id)
    except Exception:
        LOGGER.warning("failed to delete working factor %s during pipeline cleanup", working_factor_id, exc_info=True)


def _publish_canonical_factor(config: QuantForgeConfig, record: PipelineRecord) -> str | None:
    """On success only: consolidate the working row into the canonical id.

    The canonical id is the bare id the parser itself produced
    (``record.factor["factor_id"]``, never mutated after create) -- one row
    per distinct formula/idea, matching the pre-P1 convention, so a
    successful run does not permanently multiply registry rows per
    parameter combination. Refuses to overwrite an EXISTING canonical row
    that has already been human-promoted (status other than "draft") -- a
    completed background pipeline must never silently demote or rewrite a
    candidate/active factor a human already acted on (G3). Returns the
    published id, or None if publish was declined (row already promoted) --
    the working row is deleted by the caller either way once compute has
    succeeded, since nothing downstream re-reads factor_root for an
    already-completed run's report (the report comes from the job's own
    serialized result).
    """

    canonical_id = str(record.factor.get("factor_id", ""))
    if not canonical_id:
        return None
    repo = FactorRepository(config.paths.factor_root)
    try:
        existing = repo.get(canonical_id)
    except FileNotFoundError:
        existing = None
    if existing is not None and existing.status != "draft":
        LOGGER.info("declining to publish over promoted factor %s (status=%s)", canonical_id, existing.status)
        return None
    working = _factor_from_request(dict(record.factor))
    canonical = dataclass_replace(working, factor_id=canonical_id, status="draft")
    repo.save(canonical)
    return canonical_id


def _compute_completed_per_run_index(config: QuantForgeConfig, working_factor_id: str) -> bool:
    """Phase-review F9: independent confirmation that compute finished, even
    when the job manager has forgotten about it (pruned, or wiped by a
    restart).

    ``_record_validate_factor_runs`` (apps/web/api.py) writes exactly one
    "evaluate" RunIndex row and two "backtest" rows (in_sample_backtest,
    external_oos_backtest, in that order) -- ONLY after all three results
    are already fully computed -- as the very last step of a successful
    compute job. Seeing both backtest rows for this pipeline's working
    factor id is therefore proof the full chain completed, independent of
    whatever the in-memory job manager currently remembers.
    """

    if not working_factor_id:
        return False
    try:
        rows = RunIndex(config.paths.artifact_root).search(factor_id=working_factor_id, kind="backtest")
    except Exception:
        LOGGER.warning("RunIndex probe failed for %s during reconciliation", working_factor_id, exc_info=True)
        return False
    return len(rows) >= 2


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
        raise ValueError(
            "only kind='factor_study' pipelines are constructible in this build "
            "(kind='rd_optimize' is not part of the P1 schema at all -- see specs/pipeline.py)"
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
    # Provenance is computed NOW and stored on the record (not re-derived on
    # every GET -- see specs/pipeline.py's `provenance` field comment) so the
    # confirm card has a full set of badges from the very first render, not
    # only after the user's first confirm click (WORKORDER P1 pin: every
    # confirm-card field carries a badge, missing badge = fail). Baseline
    # and current are identical here -- this IS the baseline being set.
    provenance = derive_confirm_provenance(
        parser=parser, factor=factor, baseline_parameters=parameters, current_parameters=parameters, text=text
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
        original_parameters=parameters,
        warnings=warnings,
        provenance=tuple(entry.to_dict() for entry in provenance),
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


def _reconcile(
    record: PipelineRecord, *, store: PipelineStore, job_manager: _WebJobManager, config: QuantForgeConfig
) -> PipelineRecord:
    """Fold live job status (or its absence) into the record before returning it.

    Called at the top of every read/write entrypoint (spec §2.3 "Rejoin");
    see the module docstring's "Reconciliation on read" note for why this
    replaces a background sweep thread.
    """

    if record.status in TERMINAL_PIPELINE_STATUSES:
        return record
    now = _utc_now()
    if record.status in {"draft", "awaiting_confirm", "paused_failure"} and _is_expired(record.expires_at, now=now):
        _delete_working_factor(config, record.working_factor_id)
        record = _transition(record, "expired")
        return store.save(record, event="expired")
    if record.status != "running":
        return record
    compute = record.stage("compute")
    if compute.child_job_id is None:
        return record
    try:
        job = job_manager.get(compute.child_job_id)
    except KeyError:
        # phase-review F9: do not assume failure just because the in-memory
        # job manager has forgotten this job -- independently check the
        # durable RunIndex lineage first.
        if _compute_completed_per_run_index(config, record.working_factor_id):
            return _complete_from_reconciliation(record, store=store, config=config, now=now)
        _delete_working_factor(config, record.working_factor_id)
        record = _advance_stage(record, "compute", status="failed", ended_at=now)
        record = _transition(record, "paused_failure", failure=FailureState(stage_id="compute", reason_code="JOB_NOT_FOUND"))
        return store.save(record, event="job_not_found_on_reconcile")
    job_status = job.get("status")
    if job_status in {"running", "cancel_requested"}:
        return record
    if job_status == "completed":
        return _complete_from_reconciliation(record, store=store, config=config, now=now, compute_job_id=compute.child_job_id)
    if job_status == "cancelled":
        _delete_working_factor(config, record.working_factor_id)
        record = _advance_stage(record, "compute", status="failed", ended_at=now)
        record = _transition(record, "aborted")
        return store.save(record, event="compute_cancelled")
    # failed
    _delete_working_factor(config, record.working_factor_id)
    reason = str(job.get("error") or "").strip()[:200] or "COMPUTE_FAILED"
    record = _advance_stage(record, "compute", status="failed", ended_at=now)
    record = _transition(record, "paused_failure", failure=FailureState(stage_id="compute", reason_code=reason))
    return store.save(record, event="compute_failed")


def _complete_from_reconciliation(
    record: PipelineRecord,
    *,
    store: PipelineStore,
    config: QuantForgeConfig,
    now: str,
    compute_job_id: str | None = None,
) -> PipelineRecord:
    published = _publish_canonical_factor(config, record)
    _delete_working_factor(config, record.working_factor_id)
    report_ref = ArtifactRef(kind="report", job_id=compute_job_id or record.stage("compute").child_job_id)
    record = _advance_stage(record, "compute", status="completed", ended_at=now)
    record = _advance_stage(record, "report", status="completed", started_at=now, ended_at=now)
    record = record.with_updates(
        artifact_refs=record.artifact_refs + (report_ref,),
        published_factor_id=published,
    )
    record = _transition(record, "completed")
    return store.save(record, event="compute_completed")


def get_pipeline(store: PipelineStore, pipeline_id: str, *, job_manager: _WebJobManager, config: QuantForgeConfig) -> PipelineRecord:
    with store.lock:
        record = store.load(pipeline_id)
        return _reconcile(record, store=store, job_manager=job_manager, config=config)


def list_active_pipelines(store: PipelineStore, *, job_manager: _WebJobManager, config: QuantForgeConfig) -> list[PipelineRecord]:
    """Every non-terminal pipeline, each independently reconciled.

    Phase-review F9: rejoin must not reconcile only the pipeline the
    frontend happens to render -- every id under the store is loaded and
    reconciled here, unconditionally, before the (separate) decision of
    which one a caller's UI attaches to.
    """

    with store.lock:
        records = []
        for pipeline_id in store.list_ids():
            try:
                record = store.load(pipeline_id)
            except PipelineNotFoundError:
                continue
            record = _reconcile(record, store=store, job_manager=job_manager, config=config)
            if record.status not in TERMINAL_PIPELINE_STATUSES:
                records.append(record)
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

    ``(pipeline_id, nonce, version)`` is the idempotency key. The SAME key
    seen again -- double click, second tab, retried request -- returns the
    SAME run untouched; a DIFFERENT/stale key while still awaiting
    confirmation is rejected (phase-review F1/F2) so a late request from a
    superseded draft can never confirm the wrong offer.

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
        if record.confirm.nonce == nonce and record.confirm.version == version and record.confirm.confirmed_at is not None:
            return record
        if record.status not in {"draft", "awaiting_confirm"}:
            raise PipelineConflictError(f"pipeline {pipeline_id} is not awaiting confirmation (status={record.status})")
        if record.confirm.nonce != nonce or record.confirm.version != version:
            raise PipelineConflictError("stale confirm token; reload the pipeline and try again")

        effective_parameters = dict(record.parameters)
        if parameters:
            effective_parameters.update(parameters)
        effective_parameters = _flat_parameters_only(effective_parameters)
        # Recomputed against the IMMUTABLE parse-time baseline (phase-review
        # F4), never the mutable current draft -- see provenance.py. Text is
        # re-derived from the SAME parse job every pipeline already
        # references, so a confirm with zero edits reproduces the exact
        # same badges create_pipeline originally computed.
        text = _source_text_for(record, job_manager=job_manager)
        provenance = derive_confirm_provenance(
            parser=record.parser,
            factor=record.factor,
            baseline_parameters=record.original_parameters,
            current_parameters=effective_parameters,
            text=text,
        )
        # Recompute input_hash from the EFFECTIVE (post-override) parameters:
        # it must fingerprint what is actually about to run, not the
        # pre-edit draft default.
        input_hash = _compute_input_hash(
            factor=record.factor,
            parameters=effective_parameters,
            parse_job_id=next((ref.job_id for ref in record.artifact_refs if ref.kind == "parse"), ""),
            parser_source=str(record.parser.get("source", "")),
            rd_config=rd_config,
            planning_influence_hash=record.planning_influence_hash,
        )
        record = record.with_updates(input_hash=input_hash)
        factor = _working_factor(record)

        # Exactly-once launch: reserve -> durably save "running" -> start.
        # attempt.number counts prior durable launches for THIS pipeline
        # (phase-review F1) -- a fresh pipeline's first confirm has zero
        # compute_job refs yet, so its launch is attempt 1; a post-retry
        # reconfirm already has one (or more), so it becomes attempt 2, 3, ...
        prior_launches = sum(1 for ref in record.artifact_refs if ref.kind == "compute_job")
        child_job_id = job_manager.reserve_id()
        now = _utc_now()
        running_record = record.with_updates(
            parameters=effective_parameters,
            confirmed_parameters=effective_parameters,
            confirm=ConfirmState(nonce=record.confirm.nonce, version=record.confirm.version, confirmed_at=now),
            provenance=tuple(entry.to_dict() for entry in provenance),
            attempt=AttemptState(number=prior_launches + 1, parent_run_id=record.attempt.parent_run_id),
            artifact_refs=record.artifact_refs + (ArtifactRef(kind="compute_job", job_id=child_job_id),),
        )
        running_record = _advance_stage(running_record, "confirm", status="completed", ended_at=now)
        running_record = _advance_stage(running_record, "compute", status="active", started_at=now, child_job_id=child_job_id)
        running_record = _transition(running_record, "running")
        running_record = store.save(
            running_record,
            event="confirmed",
            detail={"nonce": nonce, "version": version, "child_job_id": child_job_id},
        )
        try:
            job_manager.start(
                "validate_idea",
                lambda cancel_event: run_idea_validation_workflow(
                    config,
                    factor,
                    parser=record.parser,
                    parameters=effective_parameters,
                    rd_config=rd_config,
                    cancel_event=cancel_event,
                ),
                job_id=child_job_id,
            )
        except Exception as exc:
            LOGGER.warning("job launch failed for pipeline %s after durable save", pipeline_id, exc_info=True)
            _delete_working_factor(config, running_record.working_factor_id)
            failed_record = _advance_stage(running_record, "compute", status="failed", ended_at=_utc_now())
            failed_record = _transition(
                failed_record, "paused_failure", failure=FailureState(stage_id="compute", reason_code=f"LAUNCH_FAILED: {exc}"[:200])
            )
            return store.save(failed_record, event="launch_failed")
        return running_record


def cancel_pipeline(store: PipelineStore, pipeline_id: str, *, job_manager: _WebJobManager, config: QuantForgeConfig) -> PipelineRecord:
    with store.lock:
        record = store.load(pipeline_id)
        record = _reconcile(record, store=store, job_manager=job_manager, config=config)
        if record.status in TERMINAL_PIPELINE_STATUSES:
            return record  # cancel is idempotent, not an error, once terminal
        compute = record.stage("compute")
        if compute.child_job_id is not None:
            try:
                job_manager.cancel(compute.child_job_id)
            except KeyError:
                pass
        _delete_working_factor(config, record.working_factor_id)
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
        now = _utc_now()
        record = _reset_stage(record, "compute")
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
        if record.status in TERMINAL_PIPELINE_STATUSES:
            raise PipelineConflictError(
                f"pipeline {pipeline_id} is terminal (status={record.status}); start a new pipeline instead"
            )
        merged = dict(record.parameters)
        merged.update(parameters)
        merged = _flat_parameters_only(merged)
        # Compared against the IMMUTABLE baseline (phase-review F4), not the
        # pre-edit `record.parameters` -- an unrelated field's badge must
        # never move because THIS field changed.
        text = _source_text_for(record, job_manager=job_manager)
        provenance = derive_confirm_provenance(
            parser=record.parser,
            factor=record.factor,
            baseline_parameters=record.original_parameters,
            current_parameters=merged,
            text=text,
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
    store: PipelineStore, pipeline_id: str, *, job_manager: _WebJobManager, config: QuantForgeConfig, rd_config: ResearchLoopConfig
) -> PipelineRecord:
    """paused_failure "edit" exit (spec §2.3 / phase-review F7): forks the
    frozen inputs into a brand-new draft pipeline with its own attempt
    lineage, and terminalizes (aborts) the old one -- it does not mutate the
    failed record in place, so the failed attempt's own history stays
    intact and inspectable under its own id.
    """

    with store.lock:
        old = store.load(pipeline_id)
        old = _reconcile(old, store=store, job_manager=job_manager, config=config)
        if old.status != "paused_failure":
            raise PipelineConflictError(f"pipeline {pipeline_id} is not paused (status={old.status})")

        # The frozen inputs are what the failed attempt actually ran with --
        # confirmed_parameters if a compute genuinely launched, else the
        # draft parameters at the point of failure (e.g. a launch failure
        # before any job started).
        frozen_parameters = old.confirmed_parameters if old.confirmed_parameters is not None else old.parameters
        now = _utc_now()
        new_pipeline_id = _new_pipeline_id()
        text = _source_text_for(old, job_manager=job_manager)
        input_hash = _compute_input_hash(
            factor=old.factor,
            parameters=frozen_parameters,
            parse_job_id=next((ref.job_id for ref in old.artifact_refs if ref.kind == "parse"), ""),
            parser_source=str(old.parser.get("source", "")),
            rd_config=rd_config,
            planning_influence_hash=old.planning_influence_hash,
        )
        provenance = derive_confirm_provenance(
            parser=old.parser,
            factor=old.factor,
            baseline_parameters=frozen_parameters,
            current_parameters=frozen_parameters,
            text=text,
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
            parameters=frozen_parameters,
            original_parameters=frozen_parameters,
            warnings=old.warnings,
            provenance=tuple(entry.to_dict() for entry in provenance),
            attempt=AttemptState(number=1, parent_run_id=old.pipeline_id),
            working_factor_id=_working_factor_id_for(new_pipeline_id, str(old.factor["factor_id"])),
            artifact_refs=tuple(ref for ref in old.artifact_refs if ref.kind == "parse"),
        )
        forked = _advance_stage(forked, "parse", status="completed", started_at=now, ended_at=now)
        forked = _transition(forked, "awaiting_confirm")
        forked = _advance_stage(forked, "confirm", status="active", started_at=now)
        forked = store.save(forked, event="forked_from_failure", detail={"parent_run_id": old.pipeline_id})

        old = _transition(old, "aborted")
        store.save(old, event="aborted_by_fork", detail={"forked_to": new_pipeline_id})
        return forked


def create_pipeline_as_fallback(
    store: PipelineStore,
    *,
    job_manager: _WebJobManager,
    parse_job_id: str,
    rd_config: ResearchLoopConfig,
    parent_pipeline_id: str,
    config: QuantForgeConfig,
) -> PipelineRecord:
    """paused_failure "fall back to rule parse" exit (phase-review F7): the
    caller already ran a NEW ``parse_idea`` job (parser_mode="rule") against
    the same idea text; this wraps it into a brand-new pipeline with new
    parse provenance and ``parent_run_id`` lineage back to the failed
    pipeline, then terminalizes (aborts) the old one -- atomically from the
    caller's point of view (one function call), so there is no window where
    both the new draft and the old failure are simultaneously "live"
    without the lineage link recorded.
    """

    with store.lock:
        old = store.load(parent_pipeline_id)
        old = _reconcile(old, store=store, job_manager=job_manager, config=config)
        if old.status != "paused_failure":
            raise PipelineConflictError(f"pipeline {parent_pipeline_id} is not paused (status={old.status})")
        new_record = create_pipeline(
            store, job_manager=job_manager, parse_job_id=parse_job_id, rd_config=rd_config, parent_run_id=parent_pipeline_id
        )
        old = store.load(parent_pipeline_id)
        old = _transition(old, "aborted")
        store.save(old, event="aborted_by_rule_fallback", detail={"forked_to": new_record.pipeline_id})
        return new_record

"""The server-owned pipeline aggregate (agent_sidecar_frontend.md §2.3, D10/D11).

``apps/web/jobs.py``'s ``_WebJob`` only ever has running/cancel-requested/
terminal states and starts its worker immediately -- "paused, awaiting a
human" is new server state this module adds, not a UI trick layered on top
of the job manager. A :class:`PipelineRecord` is a durable, journaled record
under ``artifact_root/pipelines/`` that can sit in ``awaiting_confirm``
indefinitely with **no worker thread parked on a human**; the human gate
(spec G1) is a stored status, not a blocked call.

Only ``kind="factor_study"`` is constructible here in P1 (解析→假设确认→计算→报告,
compute is ONE stage backed by the existing validate chain
``apps/web/api.py::run_idea_validation_workflow``, report is TERMINAL --
D11 honest granularity). ``rd_optimize`` stays a reserved kind name in
``specs/pipeline.py`` for P3.

Design notes worth reading before touching this file:

* **FE-L3 (server decides truth).** :func:`create_pipeline` never accepts a
  client-supplied ``parser``/``factor`` claim -- it takes a ``parse_job_id``
  and reads the SERVER'S OWN stored result off ``job_manager.get(...)``. A
  client can reference a job it doesn't control the content of, but it
  cannot forge what that job produced. Confirm-time ``parameters`` overrides
  ARE legitimately client-supplied (that is the point of an editable expert
  density); :mod:`quant_forge.apps.web.provenance` labels any changed field
  ``human_override`` rather than silently keeping its original source badge.
* **Idempotent confirm.** ``confirm.nonce`` is issued by the SERVER when a
  pipeline first reaches ``awaiting_confirm`` (never client-generated), so a
  double click, a second tab, and a retried request all read and echo back
  the SAME nonce -- :func:`confirm_pipeline` recognizes the replay and
  returns the already-running/completed record untouched, never starting a
  second job. The whole read-check-write sequence runs under
  :class:`PipelineStore`'s lock so two concurrent confirms for the same
  pipeline can't both win the race.
* **Snapshot isolation (WORKORDER P1 pin).** The parser assigns a
  deterministic ``factor_id`` from (name, formula, filters, idea text) --
  entirely independent of simulation parameters
  (``llm_factor_parser.py``/``factor_library/repository.py::
  parse_idea_to_definition``). Two pipelines seeded from the same idea text
  but confirmed with different parameters would therefore collide on the
  SAME row in ``factor_root`` under the existing overwrite-then-restore-on-
  failure behavior (``apps/web/api.py::_validate_factor_workflow``, frozen,
  not touched here) -- one pipeline's failure-triggered restore could revert
  or delete a concurrently-running sibling's definition, or a third actor's
  registry promotion. This module closes that hole at the ONE layer it is
  allowed to touch: :func:`_pipeline_scoped_factor` derives a
  pipeline-input-hash-scoped ``factor_id`` (``_pipeline_scoped_factor_id``)
  before ever calling into the validate chain, so two pipelines with
  different confirmed input never share a ``factor_root`` row -- and two
  confirms of the IDENTICAL input resolve to the identical scoped id, which
  is exactly the idempotent-replay case, not a collision.
  ``tests/test_web_pipeline_aggregate.py`` exercises this directly. Cost:
  registry ids for pipeline-originated factors carry a ``_P<hash>`` suffix
  instead of the bare parser id.
* **Reconciliation on read, not a background sweep.** There is no scheduler
  thread polling job status. :func:`get_pipeline` / :func:`list_active_pipelines`
  / :func:`confirm_pipeline` / :func:`cancel_pipeline` all call
  :func:`_reconcile` first, which folds a live job's current status (or its
  absence -- e.g. after a server restart wiped ``_WebJobManager``'s
  in-memory table) into the pipeline record before anything is returned.
  This is also how "refresh / server restart never silently strand a
  running computation" (spec §2.3) is satisfied: a pipeline whose child job
  vanished reconciles to ``paused_failure`` with reason ``JOB_NOT_FOUND``
  instead of claiming to still be running forever.
"""

from __future__ import annotations

from dataclasses import replace as dataclass_replace
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import re
import threading
from typing import Any
from uuid import uuid4

from quant_forge.apps.web.api import _factor_from_request, run_idea_validation_workflow
from quant_forge.apps.web.jobs import _WebJobManager, _utc_now
from quant_forge.apps.web.provenance import PARAMETER_FIELDS, derive_confirm_provenance
from quant_forge.config import QuantForgeConfig
from quant_forge.core.contracts import FactorDefinition
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
]

_PIPELINE_DIR_NAME = "pipelines"
_PIPELINE_ID_RE = re.compile(r"^PL_[0-9a-f]{32}$")
# Abandonment TTL for draft/awaiting_confirm/paused_failure (spec §12 open
# question #4, resolved for P1: a fixed, generous default). `running` is
# deliberately exempt -- a live compute must never be silently expired out
# from under itself; it resolves to completed/paused_failure through
# reconciliation when its job finishes (or JOB_NOT_FOUND on rejoin if it
# didn't survive a restart).
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


class PipelineStore:
    """Durable persistence under ``artifact_root/pipelines/``.

    Each pipeline gets one JSON snapshot (``<id>.json``, atomically
    rewritten via a temp-file-then-``os.replace`` on every transition) plus
    an append-only transition journal (``<id>.journal.jsonl``). A single
    :class:`threading.RLock` serializes read-modify-write sections --
    mirrors ``_WebJobManager``'s own single lock, and is adequate here for
    the same reason: this is a local, single-user tool, and the lock only
    ever wraps a fast state transition (starting a background job returns
    immediately; the lock is never held for the job's own runtime).
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

    def load(self, pipeline_id: str) -> PipelineRecord:
        path = self._snapshot_path(pipeline_id)
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise PipelineNotFoundError(pipeline_id) from None
        return PipelineRecord.from_dict(json.loads(raw))

    def list_ids(self) -> list[str]:
        if not self._root.is_dir():
            return []
        return sorted(path.stem for path in self._root.glob("PL_*.json"))

    def save(self, record: PipelineRecord, *, event: str, detail: dict[str, Any] | None = None) -> PipelineRecord:
        with self.lock:
            self._root.mkdir(parents=True, exist_ok=True)
            journal_row = {
                "ts": _utc_now(),
                "event": event,
                "pipeline_id": record.pipeline_id,
                "status": record.status,
                "detail": detail or {},
            }
            with self._journal_path(record.pipeline_id).open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(journal_row, ensure_ascii=False, sort_keys=True) + "\n")
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


def _pipeline_scoped_factor_id(parsed_factor_id: str, input_hash: str) -> str:
    """Snapshot-isolation id: see the module docstring's "Snapshot isolation" note.

    ``parsed_factor_id`` is always ``[A-Za-z][A-Za-z0-9_]*`` (parser output);
    ``input_hash`` is lowercase hex. Both charsets are already inside
    ``FactorDefinition``'s ``[A-Za-z][A-Za-z0-9_=-]*`` id pattern, so the
    concatenation always validates.
    """

    return f"{parsed_factor_id}_P{input_hash[:12]}"


def _pipeline_scoped_factor(record: PipelineRecord) -> FactorDefinition:
    base = _factor_from_request(dict(record.factor))
    return dataclass_replace(base, factor_id=_pipeline_scoped_factor_id(base.factor_id, record.input_hash))


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
) -> PipelineRecord:
    """Wrap an already-completed ``parse_idea`` job into a new pipeline.

    Takes a job id, not a parser/factor payload (FE-L3 -- see module
    docstring): the ``parser``/``factor``/``parameters``/``warnings`` this
    pipeline stores come directly from ``job_manager.get(parse_job_id)``'s
    OWN stored result, never from anything the client's request body claims.
    """

    if kind != "factor_study":
        raise ValueError(
            "only kind='factor_study' pipelines are constructible in this build "
            "(kind='rd_optimize' is reserved for a later phase)"
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
    # confirm-card field carries a badge, missing badge = fail). No
    # overrides yet -- this is the fresh, unedited parse artifact.
    provenance = derive_confirm_provenance(parser=parser, factor=factor, parameters=parameters, overrides=None)
    record = PipelineRecord(
        pipeline_id=_new_pipeline_id(),
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
        warnings=warnings,
        provenance=tuple(entry.to_dict() for entry in provenance),
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
        store.save(record, event="created")
    return record


def _reconcile(record: PipelineRecord, *, store: PipelineStore, job_manager: _WebJobManager) -> PipelineRecord:
    """Fold live job status (or its absence) into the record before returning it.

    Called at the top of every read/write entrypoint (spec §2.3 "Rejoin");
    see the module docstring's "Reconciliation on read" note for why this
    replaces a background sweep thread.
    """

    if record.status in TERMINAL_PIPELINE_STATUSES:
        return record
    now = _utc_now()
    if record.status in {"draft", "awaiting_confirm", "paused_failure"} and _is_expired(record.expires_at, now=now):
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
        record = _advance_stage(record, "compute", status="failed", ended_at=now)
        record = _transition(record, "paused_failure", failure=FailureState(stage_id="compute", reason_code="JOB_NOT_FOUND"))
        return store.save(record, event="job_not_found_on_reconcile")
    job_status = job.get("status")
    if job_status in {"running", "cancel_requested"}:
        return record
    if job_status == "completed":
        report_ref = ArtifactRef(kind="report", job_id=compute.child_job_id)
        record = _advance_stage(record, "compute", status="completed", ended_at=now)
        record = _advance_stage(record, "report", status="completed", started_at=now, ended_at=now)
        record = record.with_updates(artifact_refs=record.artifact_refs + (report_ref,))
        record = _transition(record, "completed")
        return store.save(record, event="compute_completed")
    if job_status == "cancelled":
        record = _advance_stage(record, "compute", status="failed", ended_at=now)
        record = _transition(record, "aborted")
        return store.save(record, event="compute_cancelled")
    # failed
    reason = str(job.get("error") or "").strip()[:200] or "COMPUTE_FAILED"
    record = _advance_stage(record, "compute", status="failed", ended_at=now)
    record = _transition(record, "paused_failure", failure=FailureState(stage_id="compute", reason_code=reason))
    return store.save(record, event="compute_failed")


def get_pipeline(store: PipelineStore, pipeline_id: str, *, job_manager: _WebJobManager) -> PipelineRecord:
    with store.lock:
        record = store.load(pipeline_id)
        return _reconcile(record, store=store, job_manager=job_manager)


def list_active_pipelines(store: PipelineStore, *, job_manager: _WebJobManager) -> list[PipelineRecord]:
    with store.lock:
        records = []
        for pipeline_id in store.list_ids():
            try:
                record = store.load(pipeline_id)
            except PipelineNotFoundError:
                continue
            record = _reconcile(record, store=store, job_manager=job_manager)
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
    """Idempotent confirm (spec §2.3 / WORKORDER P1 pin).

    ``(pipeline_id, nonce, version)`` is the idempotency key. The SAME key
    seen again -- double click, second tab, retried request -- returns the
    SAME run untouched; a DIFFERENT/stale key while still awaiting
    confirmation is rejected so a late request from a superseded draft can
    never confirm the wrong offer.
    """

    with store.lock:
        record = store.load(pipeline_id)
        record = _reconcile(record, store=store, job_manager=job_manager)
        if record.confirm.nonce == nonce and record.confirm.version == version and record.confirm.confirmed_at is not None:
            return record
        if record.status not in {"draft", "awaiting_confirm"}:
            raise PipelineConflictError(f"pipeline {pipeline_id} is not awaiting confirmation (status={record.status})")
        if record.confirm.nonce != nonce or record.confirm.version != version:
            raise PipelineConflictError("stale confirm token; reload the pipeline and try again")

        # Recomputed WITH overrides so an edited field earns its
        # human_override badge (spec §5.1); stored on the record (below) so
        # every subsequent read reflects this confirm's provenance, not a
        # re-derivation that would lose the override signal (see
        # specs/pipeline.py's `provenance` field comment).
        provenance = derive_confirm_provenance(
            parser=record.parser, factor=record.factor, parameters=record.parameters, overrides=parameters
        )
        effective_parameters = dict(record.parameters)
        if parameters:
            effective_parameters.update(parameters)
        effective_parameters = _flat_parameters_only(effective_parameters)
        # Recompute input_hash from the EFFECTIVE (post-override) parameters:
        # it must fingerprint what is actually about to run, not the
        # pre-edit draft default -- otherwise two pipelines confirmed with
        # different parameter overrides but seeded from the same idea text
        # would still hash identically, which would defeat the snapshot-
        # isolation scoping below (two different confirmed inputs need two
        # different factor_root rows).
        record = record.with_updates(
            input_hash=_compute_input_hash(
                factor=record.factor,
                parameters=effective_parameters,
                parse_job_id=next((ref.job_id for ref in record.artifact_refs if ref.kind == "parse"), ""),
                parser_source=str(record.parser.get("source", "")),
                rd_config=rd_config,
                planning_influence_hash=record.planning_influence_hash,
            )
        )
        factor = _pipeline_scoped_factor(record)

        job = job_manager.start(
            "validate_idea",
            lambda cancel_event: run_idea_validation_workflow(
                config,
                factor,
                parser=record.parser,
                parameters=effective_parameters,
                rd_config=rd_config,
                cancel_event=cancel_event,
            ),
        )
        now = _utc_now()
        record = record.with_updates(
            parameters=effective_parameters,
            confirmed_parameters=effective_parameters,
            confirm=ConfirmState(nonce=record.confirm.nonce, version=record.confirm.version, confirmed_at=now),
            provenance=tuple(entry.to_dict() for entry in provenance),
            artifact_refs=record.artifact_refs + (ArtifactRef(kind="compute_job", job_id=job["job_id"]),),
        )
        record = _advance_stage(record, "confirm", status="completed", ended_at=now)
        record = _advance_stage(record, "compute", status="active", started_at=now, child_job_id=job["job_id"])
        record = _transition(record, "running")
        store.save(
            record,
            event="confirmed",
            detail={"nonce": nonce, "version": version, "provenance_fields": [entry.field for entry in provenance]},
        )
        return record


def cancel_pipeline(store: PipelineStore, pipeline_id: str, *, job_manager: _WebJobManager) -> PipelineRecord:
    with store.lock:
        record = store.load(pipeline_id)
        record = _reconcile(record, store=store, job_manager=job_manager)
        if record.status in TERMINAL_PIPELINE_STATUSES:
            return record  # cancel is idempotent, not an error, once terminal
        compute = record.stage("compute")
        if compute.child_job_id is not None:
            try:
                job_manager.cancel(compute.child_job_id)
            except KeyError:
                pass
        record = _transition(record, "aborted")
        return store.save(record, event="cancelled")


def retry_pipeline(store: PipelineStore, pipeline_id: str) -> PipelineRecord:
    """paused_failure -> awaiting_confirm: re-run the SAME frozen inputs.

    The parse stage stays ``completed`` (reused, per spec §2.3 "retry
    declares which completed stages are reused" -- P1 has exactly one
    reusable completed stage); compute resets to ``pending`` for a fresh
    attempt; a new nonce/version is issued so the re-confirmation is its own
    idempotency epoch, distinct from the failed attempt's token.
    """

    with store.lock:
        record = store.load(pipeline_id)
        if record.status != "paused_failure":
            raise PipelineConflictError(f"pipeline {pipeline_id} is not paused (status={record.status})")
        now = _utc_now()
        record = _reset_stage(record, "compute")
        record = _transition(record, "awaiting_confirm")
        record = record.with_updates(
            confirm=ConfirmState(nonce=_new_nonce(), version=record.confirm.version + 1),
            attempt=AttemptState(number=record.attempt.number + 1, parent_run_id=record.attempt.parent_run_id),
            expires_at=_expires_at(now, DEFAULT_DRAFT_TTL_SECONDS),
        )
        return store.save(record, event="retried")


def update_pipeline_parameters(
    store: PipelineStore,
    pipeline_id: str,
    parameters: dict[str, Any],
    *,
    job_manager: _WebJobManager,
) -> PipelineRecord:
    """Edit the pipeline's current draft parameters.

    Pre-confirm this IS the live draft. While ``running``/``paused_failure``
    this is exactly the "仅用于下次尝试" case (spec §2.3 freeze semantics):
    ``confirmed_parameters`` -- what the in-flight/completed run actually
    used -- is untouched, so the gap between it and this updated
    ``parameters`` is what the confirm card renders as "for next attempt
    only, preserved, never silently attached to the live run".
    """

    with store.lock:
        record = store.load(pipeline_id)
        record = _reconcile(record, store=store, job_manager=job_manager)
        if record.status in TERMINAL_PIPELINE_STATUSES:
            raise PipelineConflictError(
                f"pipeline {pipeline_id} is terminal (status={record.status}); start a new pipeline instead"
            )
        merged = dict(record.parameters)
        merged.update(parameters)
        merged = _flat_parameters_only(merged)
        # A next-attempt edit earns human_override too (compared against
        # the PRE-edit draft, so an unrelated field's badge is untouched).
        provenance = derive_confirm_provenance(
            parser=record.parser, factor=record.factor, parameters=record.parameters, overrides=parameters
        )
        record = record.with_updates(parameters=merged, provenance=tuple(entry.to_dict() for entry in provenance))
        return store.save(record, event="parameters_updated")

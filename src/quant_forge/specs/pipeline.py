"""PipelineRecord: the server-owned pipeline aggregate (agent_sidecar_frontend.md §2.3).

A pipeline is the durable, server-authoritative record of one research
attempt moving through named stages toward a human confirmation gate and a
terminal report. It replaces the pre-P1 fiction that "awaiting a human" is a
UI-only state (`apps/web/jobs.py`'s ``_WebJob`` only has running / terminal
statuses and starts its worker immediately) with a real, persisted status
that can sit `awaiting_confirm` indefinitely with NO worker thread parked on
a human.

Three kinds are constructible (P3 schema addition, landing exactly as the P1
F14 note below promised — a reviewed schema change, not a today-inert
placeholder that could be built by accident):

* ``factor_study`` (pipeline A: 解析→假设确认→计算→报告, report TERMINAL);
* ``rd_optimize`` (pipeline B: RD 确认→RD 运行→排行榜, leaderboard TERMINAL),
  seeded from a completed report or a registry factor, never auto-bridged
  from A (spec §2.1). Its N rounds run inside ONE job (spec §2.2), so it is
  three truth-mapped stages, not one-per-round.
* ``timing`` (信号准备→确认→回测→报告, report TERMINAL), the position-series
  study backed by
  :func:`quant_forge.backtesting.position_series.run_position_series_backtest`.

All kinds share the SAME status graph (``LEGAL_TRANSITIONS`` is keyed by
status, not kind) and the SAME idempotent-confirm / rejoin / expiry
machinery in :mod:`quant_forge.apps.web.pipeline`; only their stage ids and
their compute-stage side effects differ.

Every function here is pure (dataclasses + validation + serialization only);
no filesystem or clock access. Persistence, locking, and journaling live in
:mod:`quant_forge.apps.web.pipeline`. Confirm-card-field-coverage validation
(phase-review F5) deliberately does NOT live here either: it needs
``apps.web.provenance``'s vocabulary, and this module stays a pure type layer
with no dependency the other direction (apps.web already depends on specs,
not vice versa) -- see ``apps/web/pipeline.py::PipelineStore.load``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

__all__ = [
    "PIPELINE_SCHEMA_VERSION",
    "PIPELINE_KINDS",
    "PIPELINE_STATUSES",
    "STAGE_STATUSES",
    "TERMINAL_PIPELINE_STATUSES",
    "FACTOR_STUDY_STAGE_IDS",
    "RD_OPTIMIZE_STAGE_IDS",
    "TIMING_STAGE_IDS",
    "STAGE_IDS_BY_KIND",
    "LEGAL_TRANSITIONS",
    "PipelineKind",
    "PipelineStatus",
    "StageStatus",
    "StageRecord",
    "ConfirmState",
    "AttemptState",
    "FailureState",
    "ArtifactRef",
    "PipelineRecord",
    "can_transition",
    "initial_stages_for",
]

PIPELINE_SCHEMA_VERSION = "qf.pipeline.v1"

# F14 (phase review) was: closed to factor_study only, with "rd_optimize" to
# land in P3 as a deliberate, reviewed schema change (never a today-inert
# placeholder). P3 is here: "rd_optimize" is now a first-class kind with its
# own stage ids (below). No NEW legal-transition edges were needed -- both
# kinds share the status graph in LEGAL_TRANSITIONS; only stage ids and the
# compute-stage side effects (publish vs. no-publish) differ, and those live
# in apps/web/pipeline.py, not in this pure type layer.
PipelineKind = Literal["factor_study", "rd_optimize", "timing"]
PIPELINE_KINDS: frozenset[str] = frozenset({"factor_study", "rd_optimize", "timing"})

PipelineStatus = Literal[
    "draft",
    "awaiting_confirm",
    "running",
    "paused_failure",
    "completed",
    "aborted",
    "expired",
]
PIPELINE_STATUSES: tuple[str, ...] = (
    "draft",
    "awaiting_confirm",
    "running",
    "paused_failure",
    "completed",
    "aborted",
    "expired",
)
TERMINAL_PIPELINE_STATUSES: frozenset[str] = frozenset({"completed", "aborted", "expired"})

StageStatus = Literal["pending", "active", "completed", "failed", "skipped"]
STAGE_STATUSES: tuple[str, ...] = ("pending", "active", "completed", "failed", "skipped")

# Honest stage granularity (D11): factor_study = 解析→假设确认→计算→报告. Compute
# is ONE stage backed by ONE job (the existing validate chain,
# apps/web/api.py::_validate_factor_workflow); report is TERMINAL. No
# per-round or per-substep stages exist until the backend emits real
# stage/round events (spec §12 open question #1).
FACTOR_STUDY_STAGE_IDS: tuple[str, ...] = ("parse", "confirm", "compute", "report")

# rd_optimize = RD 确认 → RD 运行 → 排行榜 (spec §2.1/§2.2). Three stages, NOT
# one-per-round: the whole N-round chain runs inside ONE research job (the
# existing `research_run_once` workflow), so honest stage granularity (D11)
# maps the run to a single stage and never fakes per-round lights. There is
# no "parse" stage: pipeline B is seeded from an already-resolved factor
# (a completed report or a registry factor), so it opens directly on the RD
# confirm gate. `leaderboard` is TERMINAL (the candidate ranking display),
# mirroring `report` for factor_study.
RD_OPTIMIZE_STAGE_IDS: tuple[str, ...] = ("confirm", "run", "leaderboard")

# timing = 信号准备 → 确认 → 回测 → 报告. The position-series (single- or
# multi-instrument) study: `signal_prepare` resolves the target-weight series
# the run will trade, `backtest` is ONE stage backed by ONE job running
# `backtesting.position_series.run_position_series_backtest`, and `report` is
# TERMINAL, mirroring factor_study's shape.
#
# The `confirm` stage is NOT optional padding: the shared machinery in
# apps/web/pipeline.py advances, resets, and re-arms a stage literally named
# "confirm" on every create / confirm / retry / fork path, and the
# awaiting_confirm status is the server-side human gate itself. A kind without
# it could not traverse the shared status graph, so the honest minimal timing
# stage set is four ids, not three. Honest stage granularity (D11) still
# applies: no per-parameter or per-segment stages exist until the backend emits
# real events for them.
TIMING_STAGE_IDS: tuple[str, ...] = ("signal_prepare", "confirm", "backtest", "report")

STAGE_IDS_BY_KIND: dict[str, tuple[str, ...]] = {
    "factor_study": FACTOR_STUDY_STAGE_IDS,
    "rd_optimize": RD_OPTIMIZE_STAGE_IDS,
    "timing": TIMING_STAGE_IDS,
}

# Legal transitions (spec §2.3). Keyed by FROM status; value is the set of
# TO statuses one transition may target. paused_failure -> awaiting_confirm
# is "retry" (re-enter confirm with the same frozen inputs, new nonce);
# paused_failure -> aborted is one terminal exit among three (spec §2.3 /
# phase-review F7: edit-fork and rule-parse-fallback are NEW pipelines
# created via create_pipeline with parent_run_id set, terminalizing this one
# via the SAME aborted transition -- they are not additional edges on THIS
# table).
LEGAL_TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"awaiting_confirm", "aborted", "expired"}),
    "awaiting_confirm": frozenset({"running", "aborted", "expired"}),
    "running": frozenset({"completed", "paused_failure", "aborted"}),
    "paused_failure": frozenset({"awaiting_confirm", "aborted", "expired"}),
    "completed": frozenset(),
    "aborted": frozenset(),
    "expired": frozenset(),
}


def can_transition(from_status: str, to_status: str) -> bool:
    """Whether one transition step from ``from_status`` to ``to_status`` is legal."""

    return to_status in LEGAL_TRANSITIONS.get(from_status, frozenset())


@dataclass(frozen=True)
class StageRecord:
    stage_id: str
    status: str = "pending"
    child_job_id: str | None = None
    started_at: str | None = None
    ended_at: str | None = None

    def __post_init__(self) -> None:
        if not self.stage_id.strip():
            raise ValueError("stage_id is required")
        if self.status not in STAGE_STATUSES:
            raise ValueError(f"invalid stage status: {self.status!r} (expected one of {STAGE_STATUSES})")

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage_id": self.stage_id,
            "status": self.status,
            "child_job_id": self.child_job_id,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
        }

    @staticmethod
    def from_dict(payload: dict[str, Any]) -> "StageRecord":
        return StageRecord(
            stage_id=str(payload["stage_id"]),
            status=str(payload.get("status", "pending")),
            child_job_id=payload.get("child_job_id"),
            started_at=payload.get("started_at"),
            ended_at=payload.get("ended_at"),
        )


@dataclass(frozen=True)
class ConfirmState:
    """Idempotent-confirm token (spec §2.3). A double click, a second tab, or a
    retried request that carries the SAME ``(pipeline_id, nonce, version)``
    must resolve to the same run -- never start a second one.

    Phase-review F1/F2: ``version``/``nonce`` advance on every mutation to an
    ``awaiting_confirm`` draft (apps/web/pipeline.py's
    ``update_pipeline_parameters``), so a token held by a stale tab that
    missed that mutation is provably stale -- ``confirm_pipeline`` rejects it
    instead of silently applying a confirm against values the token-holder
    never saw.
    """

    nonce: str
    version: int = 1
    confirmed_at: str | None = None

    def __post_init__(self) -> None:
        if not self.nonce.strip():
            raise ValueError("confirm.nonce is required")
        if self.version < 1:
            raise ValueError("confirm.version must be >= 1")

    def to_dict(self) -> dict[str, Any]:
        return {"nonce": self.nonce, "version": self.version, "confirmed_at": self.confirmed_at}

    @staticmethod
    def from_dict(payload: dict[str, Any]) -> "ConfirmState":
        return ConfirmState(
            nonce=str(payload["nonce"]),
            version=int(payload.get("version", 1)),
            confirmed_at=payload.get("confirmed_at"),
        )


@dataclass(frozen=True)
class AttemptState:
    """Server-authoritative lineage (spec §5.4): attempt numbers reflect
    research history, not browser behavior.

    Phase-review F1: incremented ONLY when a compute job is durably
    launched (apps/web/pipeline.py::confirm_pipeline, at the moment the
    running snapshot is saved) -- never on a bare retry click, an idempotent
    replay, or a retry that is itself abandoned before the next confirm.
    """

    number: int = 1
    parent_run_id: str | None = None

    def __post_init__(self) -> None:
        if self.number < 1:
            raise ValueError("attempt.number must be >= 1")

    def to_dict(self) -> dict[str, Any]:
        return {"number": self.number, "parent_run_id": self.parent_run_id}

    @staticmethod
    def from_dict(payload: dict[str, Any]) -> "AttemptState":
        return AttemptState(number=int(payload.get("number", 1)), parent_run_id=payload.get("parent_run_id"))


@dataclass(frozen=True)
class FailureState:
    """Pause-in-place record (spec §2.3): which stage failed and why.

    ``reason_code`` is a small set of structural codes for conditions
    apps/web/pipeline.py itself detects (e.g. ``JOB_NOT_FOUND`` on rejoin
    after a job the manager no longer knows about), or a truncated pass-
    through of the underlying job's own error text for a genuine compute
    failure -- the underlying job error is free text
    (``apps/web/jobs.py::_client_error_message``), not a closed vocabulary,
    so this field is validated as non-empty text, not membership in an enum."""

    stage_id: str
    reason_code: str

    def __post_init__(self) -> None:
        if not self.stage_id.strip():
            raise ValueError("failure.stage_id is required")
        if not self.reason_code.strip():
            raise ValueError("failure.reason_code is required")

    def to_dict(self) -> dict[str, Any]:
        return {"stage_id": self.stage_id, "reason_code": self.reason_code}

    @staticmethod
    def from_dict(payload: dict[str, Any]) -> "FailureState":
        return FailureState(stage_id=str(payload["stage_id"]), reason_code=str(payload["reason_code"]))


@dataclass(frozen=True)
class ArtifactRef:
    """A pointer, never a payload copy (spec §2.3 ``artifact_refs``): the
    frontend re-fetches the canonical job/artifact endpoint to render, so the
    pipeline snapshot on disk stays small and never duplicates report data."""

    kind: str
    job_id: str | None = None
    artifact_path: str | None = None

    def __post_init__(self) -> None:
        if not self.kind.strip():
            raise ValueError("artifact_ref.kind is required")

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "job_id": self.job_id, "artifact_path": self.artifact_path}

    @staticmethod
    def from_dict(payload: dict[str, Any]) -> "ArtifactRef":
        return ArtifactRef(
            kind=str(payload["kind"]),
            job_id=payload.get("job_id"),
            artifact_path=payload.get("artifact_path"),
        )


def initial_stages_for(kind: str) -> tuple[StageRecord, ...]:
    stage_ids = STAGE_IDS_BY_KIND.get(kind)
    if stage_ids is None:
        raise ValueError(f"unknown pipeline kind: {kind!r} (expected one of {sorted(PIPELINE_KINDS)})")
    return tuple(StageRecord(stage_id=stage_id) for stage_id in stage_ids)


@dataclass(frozen=True)
class PipelineRecord:
    pipeline_id: str
    kind: str
    created_at: str
    expires_at: str
    status: str
    stages: tuple[StageRecord, ...]
    input_hash: str
    confirm: ConfirmState
    # Content needed to redisplay the confirm card on rejoin (spec §2.3
    # "Rejoin" -- refresh/restart must never silently strand the user with
    # an empty card). `parser`/`factor` are the SERVER'S OWN parse-artifact
    # output (never a client-echoed claim, FE-L3); `parameters` is the
    # current/draft parameter set (editable pre-confirm, and post-confirm
    # represents "next attempt" edits per the freeze semantics below);
    # `confirmed_parameters` is the FROZEN snapshot the running/completed
    # compute stage actually used, set once at confirm and never mutated
    # again -- the gap between it and a later-edited `parameters` is exactly
    # what the UI labels 「仅用于下次尝试」. `warnings` carries parse-time
    # negative evidence (e.g. the generic-fallback-formula warning) so it
    # stays visible at both confirm-card densities.
    parser: dict[str, Any] = field(default_factory=dict)
    factor: dict[str, Any] = field(default_factory=dict)
    parameters: dict[str, Any] = field(default_factory=dict)
    # Re-verify RV-F8/RV-F10: the raw idea text the parse stage actually
    # consumed, persisted ON the record at create time. Provenance baselines
    # and the rule-parse fallback both read THIS copy -- never the job
    # manager's bounded in-memory retention, whose pruning (or a restart)
    # used to silently flip a user_explicit badge to fixed_policy and strand
    # the fallback exit.
    source_text: str = ""
    # Phase-review F4: the IMMUTABLE parse-time baseline every provenance
    # derivation compares against. Never updated after create_pipeline sets
    # it once -- comparing against the mutable `parameters` instead would
    # let editing field B silently "revert" field A's badge from
    # human_override back to profile_default the moment A's current value
    # happened to re-equal ITS OWN original default while B was mid-edit.
    original_parameters: dict[str, Any] = field(default_factory=dict)
    confirmed_parameters: dict[str, Any] | None = None
    warnings: tuple[str, ...] = field(default_factory=tuple)
    # Per-value provenance badges (spec §5.1), stored as plain dicts (each
    # shaped like apps/web/provenance.py's ProvenanceEntry.to_dict()) rather
    # than importing that type here -- specs/ stays a pure-type module with
    # no dependency on apps/web/. Recomputed and overwritten by
    # apps/web/pipeline.py on every transition that can change a value's
    # source (create / confirm / a next-attempt parameter edit), so a plain
    # GET never needs to re-derive it: WORKORDER P1 pin "missing badge =
    # server-side assertion failure" is enforced at those write sites via
    # apps.web.provenance.assert_full_coverage, AND re-checked on every
    # PipelineStore.load() (phase-review F5) so a GET can never serve a
    # confirm card silently missing badges.
    provenance: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    # Re-verify RV-F7/RV-F8: the IMMUTABLE per-field {origin, fingerprint}
    # parse artifact (each entry shaped like ProvenanceEntry.to_dict()),
    # written ONCE when the record is created (or forked) and never
    # re-derived from volatile state afterwards. Current badges are a pure
    # function of (this baseline, the current values): unchanged fingerprint
    # keeps the baseline origin, a changed one is human_override. A declared
    # confirm-card field missing from this artifact raises
    # MissingProvenanceError -- never a silent default badge.
    baseline_provenance: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    attempt: AttemptState = field(default_factory=AttemptState)
    # F2 (compare-loop disclosure, spec §5.4 "failed/cancelled counting
    # policy"): a monotonic COUNT of attempts that reached paused_failure
    # (a launched compute/run that ended un-completed). Unlike `failure`
    # -- which `_transition` clears the moment the pipeline leaves
    # paused_failure on a retry -- this counter is NEVER decremented or
    # cleared, so the multiple-comparison honesty disclosure ("attempt N
    # failed") survives every retry and stays visible on the aggregate.
    # It is a plain integer count, not a per-attempt ledger (steward
    # boundary: no general append-only ledger beyond what §5.4 requires).
    failed_attempts: int = 0
    artifact_refs: tuple[ArtifactRef, ...] = field(default_factory=tuple)
    failure: FailureState | None = None
    # Phase-review F6 (snapshot isolation redesign): `working_factor_id` is
    # the pipeline-EXCLUSIVE, never-shared factor_root id compute runs
    # against (set at confirm time; distinct per pipeline_id, so no two
    # pipelines -- even confirmed with byte-identical input -- ever share a
    # row, and a failure-triggered restore on one can never clobber
    # another). `published_factor_id` is set ONLY once compute succeeds and
    # the canonical (non-pipeline-scoped) definition has been published;
    # None means either "not confirmed yet" or "published step has not run
    # (or was skipped -- see apps/web/pipeline.py::_publish_canonical_factor
    # for when it legitimately declines to publish)".
    working_factor_id: str = ""
    published_factor_id: str | None = None
    # Re-verify RV-F1 (CAS publish): fingerprint of the canonical registry
    # row's content captured at CONFIRM time ("" = row absent then). The
    # completion-time publish step re-reads the canonical row and publishes
    # ONLY if its current fingerprint still equals this baseline -- a
    # concurrent draft edit between confirm and completion is preserved and
    # the conflict recorded in publish_state instead of silently clobbered.
    canonical_baseline_fingerprint: str = ""
    # "" (not attempted yet) | "published" | "declined_promoted" (row was
    # human-promoted; G3 forbids overwriting) | "conflict" (RV-F1: canonical
    # content changed while this pipeline ran).
    publish_state: str = ""
    # Re-verify RV-F3: True when a terminal transition could not finish its
    # working-artifact cleanup (registry row / cached values). Reconciliation
    # retries the cleanup on every subsequent load until it succeeds, instead
    # of the old swallow-and-forget.
    cleanup_pending: bool = False
    # SE-ix cross-track amendment (binding): reserved slot for the
    # self-evolution planning-influence snapshot. Empty until SE-P5 wires it
    # up; the FIELD and its participation in input_hash exist now so the
    # hash contract never migrates later.
    planning_influence_hash: str = ""
    # Phase-review F10: monotonic revision, bumped by
    # apps/web/pipeline.py::PipelineStore.save on every write. Lets the
    # journal carry a strictly-increasing sequence independent of wall-clock
    # timestamps (which can collide or, on a clock step, go backwards) and
    # lets snapshot-recovery detect "the journal has rows newer than this
    # snapshot" unambiguously.
    revision: int = 0
    schema_version: str = PIPELINE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != PIPELINE_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported pipeline schema_version: {self.schema_version} (expected {PIPELINE_SCHEMA_VERSION})"
            )
        if not self.pipeline_id.strip():
            raise ValueError("pipeline_id is required")
        if self.kind not in PIPELINE_KINDS:
            raise ValueError(f"invalid pipeline kind: {self.kind!r} (expected one of {sorted(PIPELINE_KINDS)})")
        if self.status not in PIPELINE_STATUSES:
            raise ValueError(f"invalid pipeline status: {self.status!r} (expected one of {PIPELINE_STATUSES})")
        expected_stage_ids = STAGE_IDS_BY_KIND[self.kind]
        actual_stage_ids = tuple(stage.stage_id for stage in self.stages)
        if actual_stage_ids != expected_stage_ids:
            raise ValueError(
                f"stages must map 1:1 onto {self.kind} execution units: expected {expected_stage_ids}, "
                f"got {actual_stage_ids}"
            )
        if not self.input_hash.strip():
            raise ValueError("input_hash is required")
        if self.failure is not None and self.status != "paused_failure":
            raise ValueError("failure may only be set while status == paused_failure")
        if self.status == "paused_failure" and self.failure is None:
            raise ValueError("paused_failure requires a failure record")
        if self.revision < 0:
            raise ValueError("revision must be >= 0")
        if self.failed_attempts < 0:
            raise ValueError("failed_attempts must be >= 0")

    def stage(self, stage_id: str) -> StageRecord:
        for stage in self.stages:
            if stage.stage_id == stage_id:
                return stage
        raise KeyError(f"unknown stage_id for kind {self.kind}: {stage_id}")

    def with_updates(self, **changes: Any) -> "PipelineRecord":
        """A new record with the given fields replaced (records are frozen;
        every transition is a brand-new value, matching the journal's
        append-only discipline -- nothing is mutated in place)."""

        from dataclasses import replace

        return replace(self, **changes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "pipeline_id": self.pipeline_id,
            "kind": self.kind,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "status": self.status,
            "stages": [stage.to_dict() for stage in self.stages],
            "input_hash": self.input_hash,
            "planning_influence_hash": self.planning_influence_hash,
            "confirm": self.confirm.to_dict(),
            "parser": dict(self.parser),
            "factor": dict(self.factor),
            "parameters": dict(self.parameters),
            "source_text": self.source_text,
            "original_parameters": dict(self.original_parameters),
            "confirmed_parameters": dict(self.confirmed_parameters) if self.confirmed_parameters is not None else None,
            "warnings": list(self.warnings),
            "provenance": [dict(entry) for entry in self.provenance],
            "baseline_provenance": [dict(entry) for entry in self.baseline_provenance],
            "attempt": self.attempt.to_dict(),
            "failed_attempts": self.failed_attempts,
            "artifact_refs": [ref.to_dict() for ref in self.artifact_refs],
            "failure": self.failure.to_dict() if self.failure is not None else None,
            "working_factor_id": self.working_factor_id,
            "published_factor_id": self.published_factor_id,
            "canonical_baseline_fingerprint": self.canonical_baseline_fingerprint,
            "publish_state": self.publish_state,
            "cleanup_pending": self.cleanup_pending,
            "revision": self.revision,
        }

    @staticmethod
    def from_dict(payload: dict[str, Any]) -> "PipelineRecord":
        failure_payload = payload.get("failure")
        confirmed_parameters = payload.get("confirmed_parameters")
        return PipelineRecord(
            schema_version=str(payload.get("schema_version", PIPELINE_SCHEMA_VERSION)),
            pipeline_id=str(payload["pipeline_id"]),
            kind=str(payload["kind"]),
            created_at=str(payload["created_at"]),
            expires_at=str(payload["expires_at"]),
            status=str(payload["status"]),
            stages=tuple(StageRecord.from_dict(item) for item in payload["stages"]),
            input_hash=str(payload["input_hash"]),
            planning_influence_hash=str(payload.get("planning_influence_hash", "")),
            confirm=ConfirmState.from_dict(payload["confirm"]),
            parser=dict(payload.get("parser", {})),
            factor=dict(payload.get("factor", {})),
            parameters=dict(payload.get("parameters", {})),
            source_text=str(payload.get("source_text", "")),
            original_parameters=dict(payload.get("original_parameters", {})),
            confirmed_parameters=dict(confirmed_parameters) if confirmed_parameters is not None else None,
            warnings=tuple(str(item) for item in payload.get("warnings", ())),
            provenance=tuple(dict(entry) for entry in payload.get("provenance", ())),
            baseline_provenance=tuple(dict(entry) for entry in payload.get("baseline_provenance", ())),
            attempt=AttemptState.from_dict(payload.get("attempt", {"number": 1})),
            failed_attempts=int(payload.get("failed_attempts", 0)),
            artifact_refs=tuple(ArtifactRef.from_dict(item) for item in payload.get("artifact_refs", ())),
            failure=FailureState.from_dict(failure_payload) if failure_payload else None,
            working_factor_id=str(payload.get("working_factor_id", "")),
            published_factor_id=payload.get("published_factor_id"),
            canonical_baseline_fingerprint=str(payload.get("canonical_baseline_fingerprint", "")),
            publish_state=str(payload.get("publish_state", "")),
            cleanup_pending=bool(payload.get("cleanup_pending", False)),
            revision=int(payload.get("revision", 0)),
        )

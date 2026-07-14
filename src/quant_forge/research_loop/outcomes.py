"""Neutral research-outcome contract and pure observation mapper (Seam 1).

Adjudicated contract for the self-evolution engine v2 — DECISIONS.md
"2026-07-13 — Self-evolution engine CP0", rulings SE-i/SE-ii/SE-iv/SE-vii.
This module is the provider-neutral ingress vocabulary: any producer (the
local evaluator today, an external-plugin adapter locally) emits
:class:`ResearchOutcome`, and :func:`outcome_to_observations` turns it into
:class:`~quant_forge.research_loop.memory.MemoryObservation` rows for ONE
:class:`~quant_forge.research_loop.memory.ResearchMemoryStore` instance.

Dual-domain rule (SE-i): the MAIN store ingests ``origin="local"`` outcomes
only; external-plugin outcomes go to that plugin's OWN store instance under a
plugin-local root and steer only that plugin's work. This module enforces the
vocabulary; the ingress caller enforces the store routing and must also
resolve ``factor_id`` against the local factor registry before recording.

Anti-gaming identity (SE-ii): ``run_id`` handed to memory is the LOGICAL
EVIDENCE RUN — ``hash(factor_fingerprint × canonical window × stage)`` — not
an invocation id. Re-simulating one alpha, or retrying one frozen pipeline,
reuses the same evidence run, so promotion's ">=2 distinct runs" rule keeps
meaning "independent evidence", and the platform's single fixed window keeps
external evidence below the rule tier (>=2 distinct windows) by mechanism,
not by policy prose.

Purity: no I/O, no clock, no network, no provider imports. Identity fields
are rejected (never rewritten) when redaction would alter them; statements
are derived from closed templates only, so free text never reaches disk.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from types import MappingProxyType
from typing import Any, Mapping

from quant_forge.lineage.store import canonical_fingerprint, redact_free_text
from quant_forge.research_loop.memory import MemoryObservation

RESEARCH_OUTCOME_SCHEMA_VERSION = "qf.research_outcome.v2"
RESEARCH_OUTCOME_RECORD_SCHEMA_VERSION = "qf.research_outcome_record.v1"
SIGNATURE_CONTRACT_VERSION = "sig.v2.1"
MEASUREMENT_CONTRACT_VERSION = "meas.v1"

# --- closed vocabularies (SE-ii; extension is a reviewed contract change) ---

ORIGINS = ("local", "external_plugin")

STAGES = ("evaluate", "backtest", "gate", "prescreen", "simulate", "submit")

VERDICTS = ("passed", "blocked", "unknown", "not_applicable")

# Reason vocabulary is neutral: no provider composites (``fitness`` stays in
# the plugin), and the correlation family is first-class (owner ruling R5-2).
REASON_NONE = "NONE"
REASON_CODES = frozenset(
    {
        REASON_NONE,
        "SHARPE_BELOW_GATE",
        "RETURNS_BELOW_GATE",
        "SUBWINDOW_SHARPE_BELOW_GATE",
        "TURNOVER_TOO_HIGH",
        "TURNOVER_TOO_LOW",
        "DRAWDOWN_TOO_DEEP",
        "WEIGHT_CONCENTRATION_HIGH",
        "SELF_CORRELATION_HIGH",
        "REDUNDANCY_HIGH",
        "REGION_MISMATCH",
        "INSUFFICIENT_SAMPLE",
        "DATA_UNAVAILABLE",
        "VALIDATION_ERROR",
        "EXECUTION_ERROR",
    }
)

# Submission lifecycle is bookkeeping, never performance evidence: it is only
# representable on the ``submit`` stage and never enters scientific
# denominators (the priors view filters it out by stage).
LIFECYCLE_STATUSES = ("", "submitted", "not_confirmed", "accepted", "rejected")

# Evidence strength is DERIVED from the stage (owner ruling R5-3): it can
# never exceed the DECLARED stage. Stage truthfulness itself is caller-owned
# under SE-i and is verified by the trusted ingress sink (SE-P2 derives the
# stage from the route/result type and recomputes fingerprint/window; a
# producer mismatch is rejected there). Registries are read-only proxies so
# in-process code cannot remap them (review finding R-F5).
STAGE_EVIDENCE_STRENGTH: Mapping[str, str] = MappingProxyType(
    {
        "evaluate": "local_backtest",
        "backtest": "local_backtest",
        "gate": "local_backtest",
        "prescreen": "prescreen",
        "simulate": "platform_simulated",
        "submit": "submitted_live",
    }
)
EVIDENCE_STRENGTHS = ("prescreen", "local_backtest", "platform_simulated", "submitted_live")
EVIDENCE_STRENGTH_RANK: Mapping[str, int] = MappingProxyType(
    {name: rank for rank, name in enumerate(EVIDENCE_STRENGTHS)}
)

# Closed metric-key registry: key -> (unit, description). Values carry basis /
# method / sample count on the reading itself; the unit is fixed per key here
# so it can never drift per row (S1-F6).
METRIC_SPECS: Mapping[str, tuple[str, str]] = MappingProxyType({
    "sharpe": ("ratio", "annualized Sharpe ratio of the evaluated stage"),
    "annualized_return": ("fraction_per_year", "net annualized return unless basis says gross"),
    "max_drawdown": ("fraction", "maximum peak-to-trough drawdown, non-negative magnitude"),
    "turnover": ("fraction", "average per-rebalance turnover"),
    "max_weight": ("fraction", "largest single-name weight"),
    "subwindow_sharpe": ("ratio", "worst sub-window Sharpe (public gate vocabulary)"),
    "self_correlation": ("correlation", "correlation vs the producer's own accepted pool"),
    "redundancy": ("correlation", "redundancy vs the local candidate set"),
    "ic_mean": ("correlation", "mean information coefficient"),
    "icir": ("ratio", "IC information ratio"),
})

METRIC_BASES = ("", "net", "gross")

# FP-G sample-role axis (review finding R-F3): closed vocabulary recorded on
# every outcome so the priors view (SE-P5) can keep OOS-role metric values
# out of steering without parsing prose. "unspecified" is honest-unknown.
SAMPLE_ROLES = ("unspecified", "in_sample", "out_of_sample", "mixed")

# Reserved sentinel spellings for scope dimensions: the signature renders an
# absent dimension as "unknown"/"global", so accepting those literals as
# VALUES would let unrelated factors unify into one signature (R-F4).
_RESERVED_DIM_SENTINELS = frozenset({"unknown", "global"})

_ID_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
_HEX_RE = re.compile(r"^[0-9a-f]{16,64}$")
_DIM_RE = re.compile(r"^[a-z0-9_.\-]{0,32}$")
_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_REF_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]{0,63}$")
_MAX_REF_LENGTH = 200


def _require_clean_token(field_name: str, value: str, pattern: re.Pattern[str]) -> None:
    if not pattern.fullmatch(value):
        raise ValueError(f"{field_name} must match {pattern.pattern}, got {value!r}")
    if redact_free_text(value) != value:
        # Identity fields are rejected, never rewritten: masking would mint a
        # colliding placeholder identity (S1-F5).
        raise ValueError(f"{field_name} would be altered by redaction; refusing to record it")


def _require_relative_ref(value: str) -> None:
    """Allowlist grammar (R-F8): bounded ASCII POSIX-relative paths only.

    Segments must start alphanumeric/underscore (no dot segments, no hidden
    files), separators are single forward slashes, and the printable-ASCII
    wall kills zero-width/NFD/control/encoded tricks wholesale. The extra
    slash-prefixed redaction probe catches home-path fragments whose leading
    slash was hand-stripped (opus finding F4).
    """

    if len(value) > _MAX_REF_LENGTH:
        raise ValueError(f"evidence_ref exceeds {_MAX_REF_LENGTH} characters")
    if any(not (0x21 <= ord(char) <= 0x7E) for char in value):
        raise ValueError(f"evidence_ref must be printable ASCII without spaces: {value!r}")
    for banned in ("\\", ":", "%", "$", "{", "}"):
        if banned in value:
            raise ValueError(f"evidence_ref must not contain {banned!r}: {value!r}")
    segments = value.split("/")
    if not segments or any(not _REF_SEGMENT_RE.fullmatch(segment) for segment in segments):
        raise ValueError(f"evidence_ref must be a run id or artifact-root-relative POSIX path, got {value!r}")
    if redact_free_text(value) != value or redact_free_text("/" + value) != "/" + value:
        raise ValueError("evidence_ref would be altered by redaction; refusing to record it")


def _require_tz_aware_iso(field_name: str, value: str) -> None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp, got {value!r}") from error
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must be timezone-aware, got {value!r}")


@dataclass(frozen=True)
class OutcomeWindow:
    """Typed data window; unknown windows are honest and never count (FP-4)."""

    status: str = "unavailable"
    start_date: str = ""
    end_date: str = ""

    def __post_init__(self) -> None:
        if self.status not in ("available", "unavailable"):
            raise ValueError(f"window status must be available|unavailable, got {self.status!r}")
        if self.status == "available":
            parsed: dict[str, date] = {}
            for name, value in (("start_date", self.start_date), ("end_date", self.end_date)):
                if not _DATE_RE.fullmatch(value):
                    raise ValueError(f"available window needs ASCII ISO dates, bad {name}: {value!r}")
                try:
                    parsed[name] = date.fromisoformat(value)
                except ValueError as error:
                    # Real calendar validation (R-F7): 2024-02-31 must die here,
                    # not mint a fake distinct window for the rule tier.
                    raise ValueError(f"window {name} is not a real calendar date: {value!r}") from error
            if parsed["end_date"] < parsed["start_date"]:
                raise ValueError("window end_date precedes start_date")
        elif self.start_date or self.end_date:
            raise ValueError("unavailable window must carry empty dates (null, never a guess)")

    def canonical(self) -> str:
        """Single canonical rendering so format drift cannot mint fake windows."""

        if self.status != "available":
            return ""
        return f"{self.start_date}:{self.end_date}"

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "start_date": self.start_date, "end_date": self.end_date}


@dataclass(frozen=True)
class OutcomeScope:
    """Structured generalization dimensions (owner ruling R5-1).

    Empty string means unknown. ``factor_family`` / ``horizon_bucket`` /
    ``settings_profile`` also enter the promotion signature; unknown family or
    profile keeps the outcome at observation tier (S1-F8) because unknowns
    must not unify with anything.
    """

    asset_class: str = ""
    universe: str = ""
    factor_family: str = ""
    horizon_bucket: str = ""
    settings_profile: str = ""

    def __post_init__(self) -> None:
        for name in ("asset_class", "universe", "factor_family", "horizon_bucket", "settings_profile"):
            value = getattr(self, name)
            _require_clean_token(name, value, _DIM_RE)
            if value in _RESERVED_DIM_SENTINELS:
                raise ValueError(
                    f"{name} must not be the reserved sentinel {value!r}; leave it empty for unknown (R-F4)"
                )

    def scope_key(self) -> str:
        parts = [
            f"{key}={value}"
            for key, value in (
                ("asset", self.asset_class),
                ("universe", self.universe),
                ("family", self.factor_family),
                ("horizon", self.horizon_bucket),
                ("settings", self.settings_profile),
            )
            if value
        ]
        return ";".join(parts) if parts else "global"

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset_class": self.asset_class,
            "universe": self.universe,
            "factor_family": self.factor_family,
            "horizon_bucket": self.horizon_bucket,
            "settings_profile": self.settings_profile,
        }


@dataclass(frozen=True)
class MetricReading:
    """Status-carrying scalar: ``None`` is a labeled unknown, never 0 (FP-4)."""

    value: float | None = None
    basis: str = ""
    method_version: str = MEASUREMENT_CONTRACT_VERSION
    sample_count: int | None = None

    def __post_init__(self) -> None:
        if self.value is not None:
            if isinstance(self.value, bool) or not isinstance(self.value, (int, float)):
                raise ValueError(f"metric value must be a real number or None, got {self.value!r}")
            if not math.isfinite(float(self.value)):
                raise ValueError(f"metric value must be finite, got {self.value!r}")
        if self.basis not in METRIC_BASES:
            raise ValueError(f"metric basis must be one of {METRIC_BASES}, got {self.basis!r}")
        _require_clean_token("method_version", self.method_version, _DIM_RE)
        if self.sample_count is not None:
            if isinstance(self.sample_count, bool) or not isinstance(self.sample_count, int):
                raise ValueError("sample_count must be an int or None")
            if self.sample_count < 0:
                raise ValueError("sample_count must be non-negative")

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": None if self.value is None else float(self.value),
            "basis": self.basis,
            "method_version": self.method_version,
            "sample_count": self.sample_count,
        }


@dataclass(frozen=True)
class ResearchOutcome:
    """One provider-neutral outcome event (SE-ii identity + four axes)."""

    origin: str
    stage: str
    verdict: str
    factor_id: str
    factor_fingerprint: str
    observed_at: str
    reason_codes: tuple[str, ...] = (REASON_NONE,)
    lifecycle_status: str = ""
    sample_role: str = "unspecified"
    window: OutcomeWindow = field(default_factory=OutcomeWindow)
    scope: OutcomeScope = field(default_factory=OutcomeScope)
    metric_snapshot: Mapping[str, MetricReading] = field(default_factory=dict)
    evidence_ref: str = ""
    schema_version: str = RESEARCH_OUTCOME_SCHEMA_VERSION

    def __post_init__(self) -> None:
        # Canonicalize caller-held mutables FIRST (R-F6 / opus F2): frozen=True
        # is shallow, so a caller mutating its own dict/list after construction
        # would otherwise drift outcome_id()/to_dict() past validation.
        object.__setattr__(self, "reason_codes", tuple(self.reason_codes))
        object.__setattr__(self, "metric_snapshot", MappingProxyType(dict(self.metric_snapshot)))
        if self.schema_version != RESEARCH_OUTCOME_SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version {self.schema_version!r}")
        if self.origin not in ORIGINS:
            raise ValueError(f"origin must be one of {ORIGINS}, got {self.origin!r}")
        if self.stage not in STAGES:
            raise ValueError(f"stage must be one of {STAGES}, got {self.stage!r}")
        if self.verdict not in VERDICTS:
            raise ValueError(f"verdict must be one of {VERDICTS}, got {self.verdict!r}")
        _require_clean_token("factor_id", self.factor_id, _ID_RE)
        _require_clean_token("factor_fingerprint", self.factor_fingerprint, _HEX_RE)
        _require_tz_aware_iso("observed_at", self.observed_at)

        reasons = tuple(self.reason_codes)
        if not reasons:
            raise ValueError("reason_codes must not be empty; a pass carries the single NONE reason")
        if len(set(reasons)) != len(reasons):
            raise ValueError("reason_codes must not repeat")
        if tuple(sorted(reasons)) != reasons:
            raise ValueError("reason_codes must be sorted (canonical order, deterministic identity)")
        unknown = set(reasons) - REASON_CODES
        if unknown:
            raise ValueError(f"unknown reason codes {sorted(unknown)!r}; the set is closed (SE-ii)")
        if self.verdict == "passed" and reasons != (REASON_NONE,):
            raise ValueError("a passed outcome carries exactly the NONE reason")
        if self.verdict == "blocked" and REASON_NONE in reasons:
            raise ValueError("a blocked outcome must name real reasons, not NONE")

        if self.lifecycle_status not in LIFECYCLE_STATUSES:
            raise ValueError(f"lifecycle_status must be one of {LIFECYCLE_STATUSES}, got {self.lifecycle_status!r}")
        if self.lifecycle_status and self.stage != "submit":
            raise ValueError("lifecycle_status is only representable on the submit stage")
        if self.stage == "submit" and not self.lifecycle_status:
            # Bidirectional coherence (opus F3): a submit-stage outcome without
            # a lifecycle would claim submitted_live strength for work that
            # never demonstrably went live.
            raise ValueError("submit-stage outcomes must carry a lifecycle_status")
        if self.sample_role not in SAMPLE_ROLES:
            raise ValueError(f"sample_role must be one of {SAMPLE_ROLES}, got {self.sample_role!r}")

        for key, reading in self.metric_snapshot.items():
            if key not in METRIC_SPECS:
                raise ValueError(f"metric key {key!r} is outside the closed registry (SE-ii)")
            if not isinstance(reading, MetricReading):
                raise ValueError(f"metric {key!r} must be a MetricReading, got {type(reading).__name__}")
        if self.evidence_ref:
            _require_relative_ref(self.evidence_ref)

    # -- derived identity ---------------------------------------------------

    @property
    def evidence_strength(self) -> str:
        """Derived from the stage; producers cannot inflate it (R5-3)."""

        return STAGE_EVIDENCE_STRENGTH[self.stage]

    def evidence_run_id(self) -> str:
        """Logical evidence unit: one study of one subject on one window.

        Re-simulating the same factor fingerprint on the same canonical window
        at the same stage reuses this id, so promotion's distinct-run rule
        counts independent studies, never invocations (SE-ii / S1-F3).
        """

        return canonical_fingerprint(
            {
                "v": SIGNATURE_CONTRACT_VERSION,
                "factor_fingerprint": self.factor_fingerprint,
                "window": self.window.canonical(),
                "stage": self.stage,
            }
        )

    def outcome_id(self) -> str:
        """Content identity of the logical outcome (timestamp excluded).

        An exact administrative replay (same payload re-sent later) keeps the
        same id so ingress can drop it; a genuinely new measurement differs in
        its snapshot and gets a new id while still sharing the evidence run.
        """

        payload = self.to_dict()
        payload.pop("observed_at")
        return canonical_fingerprint(payload)

    def signature_payloads(self) -> tuple[dict[str, Any], ...]:
        """One canonical 10-field signature payload per reason (S1-F8)."""

        scope_part = ";".join(
            part
            for part in (
                f"asset={self.scope.asset_class}" if self.scope.asset_class else "",
                f"universe={self.scope.universe}" if self.scope.universe else "",
            )
            if part
        )
        # Unknown generalization dimensions must NOT unify (R-F4): when the
        # family or settings profile is unknown, the payload gains this
        # outcome's evidence-run as a disambiguator, so such observations can
        # never accumulate across factors — they stay at trace tier until the
        # producer supplies real dimensions.
        disambiguator = (
            "" if (self.scope.factor_family and self.scope.settings_profile) else self.evidence_run_id()
        )
        payloads = []
        for reason in self.reason_codes:
            payload: dict[str, Any] = {
                "v": SIGNATURE_CONTRACT_VERSION,
                "origin": self.origin,
                "stage": self.stage,
                "verdict": self.verdict,
                "reason_code": reason,
                "factor_family": self.scope.factor_family or "unknown",
                "horizon_bucket": self.scope.horizon_bucket or "unknown",
                "settings_profile": self.scope.settings_profile or "unknown",
                "scope": scope_part or "global",
                "measurement_contract_version": MEASUREMENT_CONTRACT_VERSION,
            }
            if disambiguator:
                payload["disambiguator"] = disambiguator
            payloads.append(payload)
        return tuple(payloads)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "origin": self.origin,
            "stage": self.stage,
            "verdict": self.verdict,
            "reason_codes": list(self.reason_codes),
            "lifecycle_status": self.lifecycle_status,
            "sample_role": self.sample_role,
            "factor_id": self.factor_id,
            "factor_fingerprint": self.factor_fingerprint,
            "observed_at": self.observed_at,
            "window": self.window.to_dict(),
            "scope": self.scope.to_dict(),
            "metric_snapshot": {key: reading.to_dict() for key, reading in sorted(self.metric_snapshot.items())},
            "evidence_ref": self.evidence_ref,
        }

    def to_record(self) -> dict[str, Any]:
        """Canonical persisted envelope for the outcomes ledger (R-F3).

        This — not bare ``to_dict()`` — is what the SE-P2 ingress sink appends
        per outcome, so SE-P5 priors can count the four verdicts separately,
        weight by strength, filter lifecycle bookkeeping and sample role, and
        dedup by ``(signature, evidence_run_id)`` without parsing statements.
        Note the envelope carries the LOCAL ``factor_id``/``factor_fingerprint``
        inside ``outcome``; it lives under the owning store's local root and
        never enters ``MemoryObservation`` rows (identity is hashed away).
        """

        return {
            "record_schema": RESEARCH_OUTCOME_RECORD_SCHEMA_VERSION,
            "outcome_id": self.outcome_id(),
            "evidence_run_id": self.evidence_run_id(),
            "evidence_strength": self.evidence_strength,
            "signatures": [canonical_fingerprint(payload) for payload in self.signature_payloads()],
            "outcome": self.to_dict(),
        }


def _statement_for(outcome: ResearchOutcome, reason: str) -> str:
    """Closed template: fully derived from signature axes, no free text."""

    scope_key = outcome.scope.scope_key()
    family = outcome.scope.factor_family or "unknown"
    statement = (
        f"[{outcome.origin}/{outcome.stage}] {outcome.verdict}: {reason}; "
        f"family={family}; strength={outcome.evidence_strength}; scope={scope_key}"
    )
    if redact_free_text(statement) != statement:
        raise ValueError("derived statement failed redaction invariance; refusing to emit it")
    return statement


def _failure_class_for(outcome: ResearchOutcome, reason: str) -> str:
    if outcome.verdict != "blocked":
        return ""
    if reason in ("VALIDATION_ERROR", "EXECUTION_ERROR"):
        return "validation_error"
    return "gate_blocked"


def outcome_to_observations(outcome: ResearchOutcome) -> tuple[MemoryObservation, ...]:
    """Pure mapper: one :class:`MemoryObservation` per (signature, reason).

    ``run_id`` is the LOGICAL evidence run (see :meth:`ResearchOutcome.
    evidence_run_id`), so the existing pure ``memory.promote`` thresholds
    (>=2 distinct runs; >=2 distinct windows for the rule tier) measure
    independent studies by mechanism. Lifecycle-only submit bookkeeping
    (``verdict="unknown"`` with a lifecycle status) still maps, but its
    verdict keeps it out of pass/fail learning; the priors view additionally
    filters by stage. The caller owns store routing (SE-i), factor-id
    resolution against the local registry, and outcome-id replay dropping.
    """

    if outcome.verdict not in ("passed", "blocked"):
        # FP-4 + R-F2: unknown / not-applicable verdicts carry no scientific
        # answer, and submit lifecycle bookkeeping must never become a
        # "success lesson" in feed-forward. Such outcomes are ledger-only
        # (see to_record()); they mint zero observations.
        return ()
    evidence_run = outcome.evidence_run_id()
    observations = []
    for payload, reason in zip(outcome.signature_payloads(), outcome.reason_codes):
        observations.append(
            MemoryObservation(
                signature=canonical_fingerprint(payload),
                statement=_statement_for(outcome, reason),
                run_id=evidence_run,
                observed_at=outcome.observed_at,
                data_window=outcome.window.canonical(),
                failure_class=_failure_class_for(outcome, reason),
                evidence_ref=outcome.evidence_ref,
                scope=outcome.scope.scope_key(),
            )
        )
    return tuple(observations)

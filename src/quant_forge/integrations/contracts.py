"""Provider-neutral factor-backend contracts (CP0 rulings D-ii/D-iv/D-viii).

One typed port, four declared capabilities (D-ii): an external factor backend
describes itself through :class:`BackendDescriptor` and implements only the
capabilities it declares; calling an undeclared capability raises
:class:`CapabilityNotSupported` instead of degrading silently. Every request
and result crosses the seam as a typed dataclass, and every degradation
travels as a closed-set warning code (FP-4 honesty: labels, never scores,
never free-form guesses).

This module is pure contract: no imports of any concrete backend, no network,
no credentials, no environment reads. Executable binding to a backend happens
exclusively in :mod:`quant_forge.integrations.registry` through the reviewed
static import table (D-iv).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Closed capability set (D-ii)
# ---------------------------------------------------------------------------

CAPABILITIES: frozenset[str] = frozenset(("translate", "prescreen", "simulate", "submit"))


# ---------------------------------------------------------------------------
# Closed warning-code set (D-ii + CP0 amendments 2 and 4)
#
# Values equal the constant names so the codes match the decision register
# spelling exactly and grep one-to-one across code, tests, and payloads.
# Extending this set is a reviewed contract change, never an ad-hoc string.
# ---------------------------------------------------------------------------

BACKEND_NOT_INSTALLED = "BACKEND_NOT_INSTALLED"
BACKEND_NOT_ENABLED = "BACKEND_NOT_ENABLED"
BACKEND_NOT_CONFIGURED = "BACKEND_NOT_CONFIGURED"
REGION_MISMATCH = "REGION_MISMATCH"
NOT_TRANSLATABLE = "NOT_TRANSLATABLE"
MEMBER_FORMULA_DRIFT = "MEMBER_FORMULA_DRIFT"
SUBMIT_NOT_CONFIRMED = "SUBMIT_NOT_CONFIRMED"
PRESCREEN_LOCAL_PROXY_ONLY = "PRESCREEN_LOCAL_PROXY_ONLY"
UNKNOWN_BACKEND = "UNKNOWN_BACKEND"
# A requested target region the backend does not serve: prescreening for it
# would be a claim about a market the gate spec does not describe, so the
# flow refuses instead of guessing (FP-4 / FP-G).
TARGET_REGION_UNSUPPORTED = "TARGET_REGION_UNSUPPORTED"
# A platform-side failure surfaced by an adapter (client/API error, failed
# simulation, or a platform response missing its object id). Carried on
# typed results/receipts so live-path failures stay inside the contract
# instead of escaping as raw exceptions mid-flow.
BACKEND_ERROR = "BACKEND_ERROR"

WARNING_CODES: frozenset[str] = frozenset(
    (
        BACKEND_NOT_INSTALLED,
        BACKEND_NOT_ENABLED,
        BACKEND_NOT_CONFIGURED,
        REGION_MISMATCH,
        NOT_TRANSLATABLE,
        MEMBER_FORMULA_DRIFT,
        SUBMIT_NOT_CONFIRMED,
        PRESCREEN_LOCAL_PROXY_ONLY,
        UNKNOWN_BACKEND,
        TARGET_REGION_UNSUPPORTED,
        BACKEND_ERROR,
    )
)


# ---------------------------------------------------------------------------
# Typed errors
# ---------------------------------------------------------------------------


class IntegrationContractError(Exception):
    """Base class for typed factor-backend contract failures."""


class CapabilityNotSupported(IntegrationContractError):
    """A port was asked for a capability its descriptor does not declare.

    Raised by the default :class:`FactorBackendPort` method bodies so an
    adapter that implements a capability subset stays honest by construction:
    forgetting to override a method can never silently no-op.
    """

    def __init__(self, capability: str) -> None:
        self.capability = capability
        super().__init__(f"backend capability is not supported: {capability}")


class BackendContractViolation(IntegrationContractError):
    """An adapter module or port object failed the typed contract.

    This is a loud software defect (a reviewed adapter misdeclaring itself),
    not a degradation status: resolution statuses cover absence and opt-in,
    while a contract violation must never be reported as mere unavailability.
    """


# ---------------------------------------------------------------------------
# Descriptor
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BackendDescriptor:
    """Identity and declared capability subset of one external backend."""

    backend_id: str
    label: str
    regions: tuple[str, ...]
    capabilities: frozenset[str]

    def __post_init__(self) -> None:
        if not isinstance(self.backend_id, str) or not self.backend_id.strip():
            raise ValueError("backend_id must be a non-empty string")
        if not isinstance(self.label, str) or not self.label.strip():
            raise ValueError("label must be a non-empty string")
        regions = tuple(self.regions)
        if not all(isinstance(region, str) and region for region in regions):
            raise ValueError("regions must be non-empty strings")
        object.__setattr__(self, "regions", regions)
        capabilities = frozenset(self.capabilities)
        unknown = sorted(capabilities - CAPABILITIES)
        if unknown:
            raise ValueError(
                f"unknown capabilities {unknown}; the closed set is {sorted(CAPABILITIES)}"
            )
        object.__setattr__(self, "capabilities", capabilities)

    def supports(self, capability: str) -> bool:
        return capability in self.capabilities


# ---------------------------------------------------------------------------
# Translate (D-viii honesty boundary)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TranslationRequest:
    """One factor to translate into a target platform expression.

    ``parameters`` carries simulation parameters pinned from the local report
    artifact (for example decay), and ``provenance`` carries the full
    ``synthesis_provenance`` payload for composite factors. Per the CP0
    amendment to D-viii, a translator consumes pinned report data and never
    re-resolves member formulas from the live registry; drift between the two
    is refused with :data:`MEMBER_FORMULA_DRIFT`.
    """

    factor_id: str
    formula: str
    horizon_days: int
    parameters: Mapping[str, object] = field(default_factory=dict)
    provenance: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if isinstance(self.horizon_days, bool) or not isinstance(self.horizon_days, int):
            raise ValueError("horizon_days must be an integer")
        if self.horizon_days <= 0:
            raise ValueError("horizon_days must be positive")


@dataclass(frozen=True)
class TranslationResult:
    """A target-platform expression plus the settings it was derived for."""

    expression: str
    target_settings: Mapping[str, object] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "warnings", _validated_warning_codes(self.warnings, field_name="warnings")
        )
        object.__setattr__(self, "notes", tuple(self.notes))


# ---------------------------------------------------------------------------
# Prescreen (D-iii honesty: local proxy, never a predicted pass-rate)
# ---------------------------------------------------------------------------

PRESCREEN_CHECK_STATUSES: tuple[str, ...] = (
    "passed",
    "failed",
    "not_evaluable",
    "not_configured",
)

_CHECK_STATUS_TO_PASSED: dict[str, bool | None] = {
    "passed": True,
    "failed": False,
    "not_evaluable": None,
    "not_configured": None,
}

REGION_ALIGNMENTS: tuple[str, ...] = ("aligned", "mismatched", "unknown")


@dataclass(frozen=True)
class PrescreenCheck:
    """One gate check row. ``passed`` is the boolean projection of ``status``.

    ``status`` is authoritative and closed-set; ``passed`` is derived when
    omitted and validated for agreement when supplied, so the two can never
    drift. ``not_evaluable`` maps to ``passed=None`` — a check that could not
    run is never defaulted to a failure or a pass (FP-4). ``not_configured``
    also maps to ``passed=None``: it marks a check the gate spec left
    unconfigured (D-iii optional thresholds), which is a skip, not a verdict.
    """

    name: str
    value: float | None
    threshold: float | None
    status: str
    passed: bool | None = None

    def __post_init__(self) -> None:
        if self.status not in _CHECK_STATUS_TO_PASSED:
            raise ValueError(
                f"unknown check status {self.status!r}; "
                f"the closed set is {PRESCREEN_CHECK_STATUSES}"
            )
        expected = _CHECK_STATUS_TO_PASSED[self.status]
        if self.passed is None:
            object.__setattr__(self, "passed", expected)
        elif self.passed is not expected:
            raise ValueError(
                f"passed={self.passed!r} disagrees with status {self.status!r}"
            )


@dataclass(frozen=True)
class PrescreenRequest:
    """Evaluate one local backtest report against a target platform gate."""

    factor_id: str
    data_region: str
    target_region: str
    report: Mapping[str, object]


@dataclass(frozen=True)
class PrescreenReport:
    """Gate-check outcome over local outputs (a proxy, never a promise).

    ``checks`` and ``region_alignment`` are required so the evaluator states
    them explicitly; when the local data region differs from the target
    platform region the report must carry :data:`REGION_MISMATCH` and must
    not claim a predicted pass-rate (D-iii).
    """

    checks: tuple[PrescreenCheck, ...]
    region_alignment: str
    warning_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        checks = tuple(self.checks)
        if not all(isinstance(check, PrescreenCheck) for check in checks):
            raise ValueError("checks must contain PrescreenCheck rows")
        object.__setattr__(self, "checks", checks)
        if self.region_alignment not in REGION_ALIGNMENTS:
            raise ValueError(
                f"unknown region_alignment {self.region_alignment!r}; "
                f"the closed set is {REGION_ALIGNMENTS}"
            )
        object.__setattr__(
            self,
            "warning_codes",
            _validated_warning_codes(self.warning_codes, field_name="warning_codes"),
        )


# ---------------------------------------------------------------------------
# Simulate
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SimulationRequest:
    """Run one translated expression on the target platform."""

    factor_id: str
    expression: str
    settings: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class SimulationResult:
    """Platform-side simulation outcome, reported verbatim.

    ``backend_ref`` is the platform-side identifier of the simulated object;
    ``metrics`` are the platform's own numbers, never re-derived locally.
    """

    backend_ref: str
    metrics: Mapping[str, object] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "warnings", _validated_warning_codes(self.warnings, field_name="warnings")
        )
        object.__setattr__(self, "notes", tuple(self.notes))


# ---------------------------------------------------------------------------
# Submit (explicit confirmation, provenance end-to-end per D-viii)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SubmitRequest:
    """An outward submission request.

    ``confirm`` is required with no default: a submit request cannot be
    constructed without an explicit confirmation decision, and adapters must
    refuse unconfirmed requests with :data:`SUBMIT_NOT_CONFIRMED` rather than
    submitting on anyone's behalf implicitly.
    """

    factor_id: str
    backend_ref: str
    confirm: bool
    provenance: Mapping[str, object] | None = None


@dataclass(frozen=True)
class SubmitReceipt:
    """What actually happened on submission, echoed with its provenance."""

    submission_ref: str
    status: str
    warnings: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    provenance: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "warnings", _validated_warning_codes(self.warnings, field_name="warnings")
        )
        object.__setattr__(self, "notes", tuple(self.notes))


# ---------------------------------------------------------------------------
# The port (D-ii: one typed port, four declared capabilities)
# ---------------------------------------------------------------------------


class FactorBackendPort(ABC):
    """Typed seam every external factor backend implements.

    ``describe`` is the only mandatory method. The four capability methods
    default to raising :class:`CapabilityNotSupported`, so an adapter
    overrides exactly the capabilities its descriptor declares and an
    undeclared call fails loudly instead of pretending.
    """

    @abstractmethod
    def describe(self) -> BackendDescriptor:
        """Identity, regions, and the declared capability subset."""

    def translate(self, request: TranslationRequest) -> TranslationResult:
        raise CapabilityNotSupported("translate")

    def prescreen(self, request: PrescreenRequest) -> PrescreenReport:
        raise CapabilityNotSupported("prescreen")

    def simulate(self, request: SimulationRequest) -> SimulationResult:
        raise CapabilityNotSupported("simulate")

    def submit(self, request: SubmitRequest) -> SubmitReceipt:
        raise CapabilityNotSupported("submit")


def _validated_warning_codes(values: object, *, field_name: str) -> tuple[str, ...]:
    codes = tuple(values)  # type: ignore[arg-type]
    unknown = [code for code in codes if code not in WARNING_CODES]
    if unknown:
        raise ValueError(
            f"{field_name} outside the closed warning-code set: {unknown}"
        )
    return codes

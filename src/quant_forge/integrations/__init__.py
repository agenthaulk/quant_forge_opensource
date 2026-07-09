"""Provider-neutral seam for external factor backends (CP0 D-i/D-ii/D-iv).

This package is the public pluggability boundary: typed contracts in
:mod:`quant_forge.integrations.contracts`, the reviewed static binding
registry in :mod:`quant_forge.integrations.registry`, and the pure-local
submission-gate evaluator in :mod:`quant_forge.integrations.gate` (D-iii).
Concrete adapters live in separately installed packages and are bound only
through the registry's reviewed import table behind an explicit
``QF_ENABLE_BACKEND_<ID>`` opt-in; this package itself never imports any
concrete backend at module load.
"""

from quant_forge.integrations.contracts import (
    BACKEND_NOT_CONFIGURED,
    BACKEND_NOT_ENABLED,
    BACKEND_NOT_INSTALLED,
    CAPABILITIES,
    MEMBER_FORMULA_DRIFT,
    NOT_TRANSLATABLE,
    PRESCREEN_CHECK_STATUSES,
    PRESCREEN_LOCAL_PROXY_ONLY,
    REGION_ALIGNMENTS,
    REGION_MISMATCH,
    SUBMIT_NOT_CONFIRMED,
    UNKNOWN_BACKEND,
    WARNING_CODES,
    BackendContractViolation,
    BackendDescriptor,
    CapabilityNotSupported,
    FactorBackendPort,
    IntegrationContractError,
    PrescreenCheck,
    PrescreenReport,
    PrescreenRequest,
    SimulationRequest,
    SimulationResult,
    SubmitReceipt,
    SubmitRequest,
    TranslationRequest,
    TranslationResult,
)
from quant_forge.integrations.gate import (
    GATE_CHECK_NAMES,
    SubmissionGateSpec,
    evaluate_submission_gate,
)
from quant_forge.integrations.registry import (
    BACKEND_STATUSES,
    KNOWN_FACTOR_BACKENDS,
    BackendResolution,
    enable_env_var,
    is_known_backend,
    list_backends,
    resolve_backend,
)

__all__ = [
    "BACKEND_NOT_CONFIGURED",
    "BACKEND_NOT_ENABLED",
    "BACKEND_NOT_INSTALLED",
    "BACKEND_STATUSES",
    "CAPABILITIES",
    "GATE_CHECK_NAMES",
    "KNOWN_FACTOR_BACKENDS",
    "MEMBER_FORMULA_DRIFT",
    "NOT_TRANSLATABLE",
    "PRESCREEN_CHECK_STATUSES",
    "PRESCREEN_LOCAL_PROXY_ONLY",
    "REGION_ALIGNMENTS",
    "REGION_MISMATCH",
    "SUBMIT_NOT_CONFIRMED",
    "UNKNOWN_BACKEND",
    "WARNING_CODES",
    "BackendContractViolation",
    "BackendDescriptor",
    "BackendResolution",
    "CapabilityNotSupported",
    "FactorBackendPort",
    "IntegrationContractError",
    "PrescreenCheck",
    "PrescreenReport",
    "PrescreenRequest",
    "SimulationRequest",
    "SimulationResult",
    "SubmissionGateSpec",
    "SubmitReceipt",
    "SubmitRequest",
    "TranslationRequest",
    "TranslationResult",
    "enable_env_var",
    "evaluate_submission_gate",
    "is_known_backend",
    "list_backends",
    "resolve_backend",
]

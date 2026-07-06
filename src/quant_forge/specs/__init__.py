"""Phase B spec contracts: thin typed views over the deterministic kernel.

Specs never redefine kernel semantics (FP-5); they delegate every invariant to
`quant_forge.core.contracts` and the audited registry/parser machinery.
"""

from quant_forge.specs.agent_task import AGENT_TASK_SCHEMA_VERSION, AgentTaskSpec
from quant_forge.specs.factor_spec import FACTOR_SPEC_SCHEMA_VERSION, FactorSpec
from quant_forge.specs.nl_flow import factor_spec_from_idea, load_factor_spec, save_factor_spec
from quant_forge.specs.run_manifest import (
    RUN_MANIFEST_SCHEMA_VERSION,
    RunManifest,
    canonical_fingerprint,
    manifest_for,
)
from quant_forge.specs.strategy_spec import STRATEGY_SPEC_SCHEMA_VERSION, StrategySpec
from quant_forge.specs.validation_gate import SpecValidationResult, validate_factor_spec

__all__ = [
    "AGENT_TASK_SCHEMA_VERSION",
    "FACTOR_SPEC_SCHEMA_VERSION",
    "RUN_MANIFEST_SCHEMA_VERSION",
    "STRATEGY_SPEC_SCHEMA_VERSION",
    "AgentTaskSpec",
    "FactorSpec",
    "RunManifest",
    "SpecValidationResult",
    "StrategySpec",
    "canonical_fingerprint",
    "factor_spec_from_idea",
    "load_factor_spec",
    "manifest_for",
    "save_factor_spec",
    "validate_factor_spec",
]

"""Phase B spec contracts: thin typed views over the deterministic kernel.

Specs never redefine kernel semantics (FP-5); they delegate every invariant to
`quant_forge.core.contracts` and the audited registry/parser machinery, and
close their vocabularies (sample roles, spec kinds, agent tools, capabilities,
run event types and run states) so axiom-violating states are unrepresentable.
"""

from quant_forge.specs._vocab import SAMPLE_ROLES, SPEC_KINDS, UNVERIFIED_PROVENANCE
from quant_forge.specs.agent_task import (
    AGENT_TASK_SCHEMA_VERSION,
    KNOWN_AGENT_TOOLS,
    AgentTaskSpec,
)
from quant_forge.specs.factor_spec import FACTOR_SPEC_SCHEMA_VERSION, FactorSpec
from quant_forge.specs.nl_flow import factor_spec_from_idea, load_factor_spec, save_factor_spec
from quant_forge.specs.run_event import (
    EVENT_ACTORS,
    EVENT_SEVERITIES,
    EVENT_STAGES,
    EVENT_TYPES,
    LEGAL_TRANSITIONS,
    RUN_EVENT_SCHEMA_VERSION,
    RUN_STATES,
    RunEvent,
    is_legal_transition,
)
from quant_forge.specs.run_manifest import (
    RUN_MANIFEST_SCHEMA_VERSION,
    RunManifest,
    canonical_fingerprint,
    manifest_for,
)
from quant_forge.specs.strategy_spec import STRATEGY_SPEC_SCHEMA_VERSION, StrategySpec
from quant_forge.specs.validation_gate import (
    KNOWN_CAPABILITIES,
    SpecValidationResult,
    validate_factor_spec,
)

__all__ = [
    "AGENT_TASK_SCHEMA_VERSION",
    "EVENT_ACTORS",
    "EVENT_SEVERITIES",
    "EVENT_STAGES",
    "EVENT_TYPES",
    "FACTOR_SPEC_SCHEMA_VERSION",
    "KNOWN_AGENT_TOOLS",
    "KNOWN_CAPABILITIES",
    "LEGAL_TRANSITIONS",
    "RUN_EVENT_SCHEMA_VERSION",
    "RUN_MANIFEST_SCHEMA_VERSION",
    "RUN_STATES",
    "SAMPLE_ROLES",
    "SPEC_KINDS",
    "STRATEGY_SPEC_SCHEMA_VERSION",
    "UNVERIFIED_PROVENANCE",
    "AgentTaskSpec",
    "FactorSpec",
    "RunEvent",
    "RunManifest",
    "SpecValidationResult",
    "StrategySpec",
    "canonical_fingerprint",
    "factor_spec_from_idea",
    "is_legal_transition",
    "load_factor_spec",
    "manifest_for",
    "save_factor_spec",
    "validate_factor_spec",
]

"""FactorSpec: a thin typed view over the kernel `FactorDefinition` contract.

Specs never redefine kernel semantics (FP-5). Every kernel-owned invariant is
enforced by constructing the canonical `core.contracts` dataclasses inside
`__post_init__`, so the kernel remains the single source of truth for what a
valid factor looks like.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from quant_forge.core.contracts import FactorDefinition, SimulationProfile, TransactionCostModel
from quant_forge.specs._normalize import coerce_component, set_tuple

FACTOR_SPEC_SCHEMA_VERSION = "qf.factor_spec.v1"

ExpectedDirection = Literal["positive", "negative", "unknown"]
_ALLOWED_DIRECTIONS: tuple[str, ...] = ("positive", "negative", "unknown")


@dataclass(frozen=True)
class FactorSpec:
    factor_id: str
    name: str
    formula_dsl: str
    thesis: str = ""
    expected_direction: ExpectedDirection = "unknown"
    horizon_days: int = 5
    universe_filters: tuple[str, ...] = field(default_factory=tuple)
    simulation: SimulationProfile = field(default_factory=SimulationProfile)
    costs: TransactionCostModel = field(default_factory=TransactionCostModel)
    capabilities_required: tuple[str, ...] = field(default_factory=tuple)
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: str = FACTOR_SPEC_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != FACTOR_SPEC_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported factor spec schema_version: {self.schema_version} (expected {FACTOR_SPEC_SCHEMA_VERSION})"
            )
        set_tuple(self, "universe_filters")
        set_tuple(self, "capabilities_required")
        if not self.formula_dsl.strip():
            raise ValueError("formula_dsl is required")
        if self.expected_direction not in _ALLOWED_DIRECTIONS:
            raise ValueError(f"invalid expected_direction: {self.expected_direction}")
        if not isinstance(self.simulation, SimulationProfile):
            raise ValueError("simulation must be a core SimulationProfile")
        if not isinstance(self.costs, TransactionCostModel):
            raise ValueError("costs must be a core TransactionCostModel")
        # Kernel-invariant delegation (FP-5): factor_id discipline and
        # horizon bounds are owned by core.contracts.FactorDefinition.
        self.as_factor_definition()

    def as_factor_definition(self) -> FactorDefinition:
        """Project this spec onto the canonical kernel contract."""

        return FactorDefinition(
            factor_id=self.factor_id,
            name=self.name,
            formula=self.formula_dsl,
            status="draft",
            description=self.thesis,
            horizon_days=self.horizon_days,
            universe_filters=self.universe_filters,
            source="spec",
        )

    def unsupported_capabilities(self, known: tuple[str, ...]) -> tuple[str, ...]:
        """Capabilities this spec requires but the executing adapter lacks.

        Mirrors `StrategySpec.unsupported_capabilities`: reserved-not-yet-
        executable capabilities must never silently pass — callers MUST check
        this before routing the spec to execution and fail closed when the
        result is non-empty (FP-2).
        """

        known_set = {item.strip() for item in known}
        return tuple(cap for cap in self.capabilities_required if cap not in known_set)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["universe_filters"] = list(self.universe_filters)
        payload["capabilities_required"] = list(self.capabilities_required)
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "FactorSpec":
        data = dict(payload)
        schema_version = str(data.get("schema_version", ""))
        if schema_version != FACTOR_SPEC_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported factor spec schema_version: {schema_version} (expected {FACTOR_SPEC_SCHEMA_VERSION})"
            )
        simulation = data.get("simulation", {})
        costs = data.get("costs", {})
        return cls(
            factor_id=str(data["factor_id"]),
            name=str(data["name"]),
            formula_dsl=str(data["formula_dsl"]),
            thesis=str(data.get("thesis", "")),
            expected_direction=str(data.get("expected_direction", "unknown")),  # type: ignore[arg-type]
            horizon_days=int(data.get("horizon_days", 5)),
            universe_filters=tuple(str(item) for item in data.get("universe_filters", ())),
            simulation=coerce_component(SimulationProfile, simulation, "simulation"),
            costs=coerce_component(TransactionCostModel, costs, "costs"),
            capabilities_required=tuple(str(item) for item in data.get("capabilities_required", ())),
            metadata=dict(data.get("metadata", {})),
            schema_version=schema_version,
        )

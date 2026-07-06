"""StrategySpec: typed parameters over kernel capabilities, not a new DSL.

Per the Phase B target architecture, a strategy is a parameterization of the
audited kernel (ranking factors, holding, costs, simulation profile). Identifier
discipline is delegated to `core.contracts.FactorDefinition` so the spec layer
never re-states the kernel id grammar (FP-5).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from quant_forge.core.contracts import FactorDefinition, SimulationProfile, TransactionCostModel
from quant_forge.specs._normalize import coerce_component, set_tuple

STRATEGY_SPEC_SCHEMA_VERSION = "qf.strategy_spec.v1"

# Dummy formula used only to run kernel id validation through FactorDefinition.
_ID_PROBE_FORMULA = "rank(close)"


def _validate_kernel_identifier(identifier: str, label: str) -> None:
    """Delegate id-grammar validation to the kernel contract (FP-5)."""

    try:
        FactorDefinition(factor_id=identifier, name=label, formula=_ID_PROBE_FORMULA, horizon_days=1)
    except ValueError as exc:
        raise ValueError(f"{label} fails kernel id validation: {exc}") from exc


@dataclass(frozen=True)
class StrategySpec:
    strategy_id: str
    name: str
    ranking_factor_ids: tuple[str, ...]
    holding_days: int = 5
    simulation: SimulationProfile = field(default_factory=SimulationProfile)
    costs: TransactionCostModel = field(default_factory=TransactionCostModel)
    benchmark: str = "cash"
    capabilities_required: tuple[str, ...] = field(default_factory=tuple)
    schema_version: str = STRATEGY_SPEC_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != STRATEGY_SPEC_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported strategy spec schema_version: {self.schema_version} "
                f"(expected {STRATEGY_SPEC_SCHEMA_VERSION})"
            )
        set_tuple(self, "ranking_factor_ids")
        set_tuple(self, "capabilities_required")
        _validate_kernel_identifier(self.strategy_id, "strategy_id")
        if not self.name.strip():
            raise ValueError("strategy name is required")
        if not self.ranking_factor_ids:
            raise ValueError("at least one ranking factor id is required")
        for factor_id in self.ranking_factor_ids:
            _validate_kernel_identifier(factor_id, "ranking_factor_id")
        if self.holding_days < 1:
            raise ValueError("holding_days must be at least 1")
        if not self.benchmark.strip():
            raise ValueError("benchmark is required (use 'cash' for none)")
        if not isinstance(self.simulation, SimulationProfile):
            raise ValueError("simulation must be a core SimulationProfile")
        if not isinstance(self.costs, TransactionCostModel):
            raise ValueError("costs must be a core TransactionCostModel")

    def unsupported_capabilities(self, known: tuple[str, ...]) -> tuple[str, ...]:
        """Capabilities this spec requires but the executing adapter lacks.

        Reserved-not-yet-executable capabilities must never silently pass:
        callers MUST check this before routing the spec to execution and fail
        closed when the result is non-empty (FP-2).
        """

        known_set = {item.strip() for item in known}
        return tuple(cap for cap in self.capabilities_required if cap not in known_set)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["ranking_factor_ids"] = list(self.ranking_factor_ids)
        payload["capabilities_required"] = list(self.capabilities_required)
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "StrategySpec":
        data = dict(payload)
        schema_version = str(data.get("schema_version", ""))
        if schema_version != STRATEGY_SPEC_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported strategy spec schema_version: {schema_version} "
                f"(expected {STRATEGY_SPEC_SCHEMA_VERSION})"
            )
        simulation = data.get("simulation", {})
        costs = data.get("costs", {})
        return cls(
            strategy_id=str(data["strategy_id"]),
            name=str(data["name"]),
            ranking_factor_ids=tuple(str(item) for item in data.get("ranking_factor_ids", ())),
            holding_days=int(data.get("holding_days", 5)),
            simulation=coerce_component(SimulationProfile, simulation, "simulation"),
            costs=coerce_component(TransactionCostModel, costs, "costs"),
            benchmark=str(data.get("benchmark", "cash")),
            capabilities_required=tuple(str(item) for item in data.get("capabilities_required", ())),
            schema_version=schema_version,
        )

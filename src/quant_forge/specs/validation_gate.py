"""ValidationGate: fail-closed spec validation before any run (FP-2/FP-4).

Resolution is fully delegated to existing kernel machinery:
- operators: `operator_registry.resolver.resolve_formula_operators`, which
  parses via the safe-AST `formula_parser` and applies the registry's own
  canonical/alias mechanics (canonical-only execution; aliases rewrite or block
  per the registry, never here);
- fields: `mcp.read_models.list_available_fields`, the canonical data catalog.

The gate never executes the formula. Unknown operators or fields block the
spec with explicit reasons — there is no silent pass.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from quant_forge.mcp.read_models import list_available_fields
from quant_forge.operator_registry.loader import load_default_operator_registry
from quant_forge.operator_registry.models import OperatorRegistry
from quant_forge.operator_registry.resolver import resolve_formula_operators
from quant_forge.specs.factor_spec import FactorSpec

GateStatus = Literal["ready", "blocked"]


@dataclass(frozen=True)
class SpecValidationResult:
    status: GateStatus
    unresolved_operators: tuple[str, ...] = field(default_factory=tuple)
    unresolved_fields: tuple[str, ...] = field(default_factory=tuple)
    blocking_reasons: tuple[str, ...] = field(default_factory=tuple)
    warnings: tuple[str, ...] = field(default_factory=tuple)


def validate_factor_spec(spec: FactorSpec, registry: OperatorRegistry | None = None) -> SpecValidationResult:
    registry = registry or load_default_operator_registry()
    resolution = resolve_formula_operators(spec.formula_dsl, registry)

    blocking: list[str] = list(resolution.blocking_errors)
    unresolved_operators = tuple(
        dict.fromkeys(item.original_name for item in resolution.items if not item.signature_valid)
    )

    known_fields = {entry["name"] for entry in list_available_fields()}
    unresolved_fields = tuple(
        dict.fromkeys(name for name in resolution.used_fields if name not in known_fields)
    )
    for name in unresolved_fields:
        blocking.append(f"unknown field: {name}")

    if not resolution.executable and not blocking:
        # Fail closed: never report ready without an executable resolution.
        blocking.append("formula is not executable under the canonical operator registry")

    status: GateStatus = "ready" if resolution.executable and not blocking else "blocked"
    return SpecValidationResult(
        status=status,
        unresolved_operators=unresolved_operators,
        unresolved_fields=unresolved_fields,
        blocking_reasons=tuple(dict.fromkeys(blocking)),
        warnings=tuple(dict.fromkeys(resolution.warnings)),
    )

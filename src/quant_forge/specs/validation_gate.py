"""ValidationGate: fail-closed spec validation before any run (FP-2/FP-4).

Resolution is fully delegated to existing kernel machinery:
- operators: `operator_registry.resolver.resolve_formula_operators`, which
  parses via the safe-AST `formula_parser` and applies the registry's own
  canonical/alias mechanics (canonical-only execution; aliases rewrite or block
  per the registry, never here);
- fields: `mcp.read_models.list_available_fields`, the canonical data catalog;
- universe filters: the accepted forms are owned by
  `factor_engine.executor._eval_filter`; the gate replicates only that
  accepted-form check (see `_ACCEPTED_FILTER_FORMS`);
- capabilities: `KNOWN_CAPABILITIES` is the registry of adapter capabilities
  (empty until Phase C adapters register any), so every non-empty
  `capabilities_required` blocks loudly today.

The gate never executes the formula. Unknown operators, fields, filter forms
or capabilities block the spec with explicit reasons — there is no silent
pass. Any surface the gate cannot verify at all is disclosed in
`SpecValidationResult.unchecked` instead of being silently skipped.
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

# Capabilities executable by registered adapters. Empty on purpose: no
# adapters exist yet, so any spec that requires a capability is blocked
# (fail loud) until Phase C adapters register what they can execute.
KNOWN_CAPABILITIES: frozenset[str] = frozenset()

# Accepted universe-filter forms. Owner: factor_engine/executor.py
# (`_eval_filter`) — its parsing is private/inline, so the gate replicates
# ONLY the accepted-form membership check. Keep this set identical to the
# executor's; the executor remains the single owner of filter semantics.
_ACCEPTED_FILTER_FORMS: frozenset[str] = frozenset({"is_st == false", "is_st == 0", "not is_st"})


@dataclass(frozen=True)
class SpecValidationResult:
    status: GateStatus
    unresolved_operators: tuple[str, ...] = field(default_factory=tuple)
    unresolved_fields: tuple[str, ...] = field(default_factory=tuple)
    blocking_reasons: tuple[str, ...] = field(default_factory=tuple)
    warnings: tuple[str, ...] = field(default_factory=tuple)
    # Spec surfaces the gate could NOT verify (disclosed, never silently
    # skipped). Empty today: formula, fields, filters and capabilities are
    # all checked above.
    unchecked: tuple[str, ...] = ()


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

    # Fail loud on reserved capabilities until adapters register them.
    for capability in spec.unsupported_capabilities(tuple(KNOWN_CAPABILITIES)):
        blocking.append(f"capability not available: {capability}")

    # Same normalization as executor._eval_filter (strip + lower) before the
    # accepted-form membership check; anything else is blocked.
    for expression in spec.universe_filters:
        if expression.strip().lower() not in _ACCEPTED_FILTER_FORMS:
            blocking.append(f"unsupported universe filter: {expression}")

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
        unchecked=(),
    )

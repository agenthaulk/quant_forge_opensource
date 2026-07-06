"""Beginner NL flow: idea text -> validated FactorSpec draft (deterministic).

Uses the existing deterministic idea parser only — no LLM, no network, and no
writes to any factor_root. The returned spec is a draft proposal; execution
requires the caller to observe the validation gate result and the human
confirmation points defined in the LLM-boundary document.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from quant_forge.factor_library.repository import parse_idea_to_definition
from quant_forge.specs.factor_spec import FactorSpec
from quant_forge.specs.validation_gate import SpecValidationResult, validate_factor_spec
from quant_forge.utils import read_yaml, write_yaml

# The generic catch-all formula the deterministic parser falls back to when it
# recognizes nothing in the idea text. Owner:
# factor_library/repository.py::parse_idea_to_definition (final else branch).
_GENERIC_FALLBACK_FORMULA = "rank(close)"
_FALLBACK_WARNING = (
    "idea parsed to the generic fallback formula rank(close); the parser may "
    "not have understood the idea - review before running"
)


def factor_spec_from_idea(text: str) -> tuple[FactorSpec, SpecValidationResult]:
    """Deterministically draft a FactorSpec from a beginner idea and gate it.

    When the parser falls back to its generic formula the result carries an
    explicit warning: a fallback parse must never look like a confident one.
    """

    definition = parse_idea_to_definition(text)
    spec = FactorSpec(
        factor_id=definition.factor_id,
        name=definition.name,
        formula_dsl=definition.formula,
        thesis=definition.description,
        # The deterministic parser bakes orientation into the formula sign,
        # so the drafted composite is expected to score positively.
        expected_direction="positive",
        horizon_days=definition.horizon_days,
        universe_filters=definition.universe_filters,
        metadata={"idea_text": text.strip(), "source": definition.source},
    )
    result = validate_factor_spec(spec)
    if definition.formula == _GENERIC_FALLBACK_FORMULA:
        result = replace(result, warnings=result.warnings + (_FALLBACK_WARNING,))
    return spec, result


def save_factor_spec(spec: FactorSpec, path: Path) -> None:
    """Persist a spec as an expert-editable YAML file."""

    write_yaml(path, spec.to_dict())


def load_factor_spec(path: Path) -> FactorSpec:
    """Load an expert-edited YAML spec, re-validating every invariant.

    Raises ValueError on schema_version mismatch or any kernel-invariant
    violation introduced by manual edits (validation happens in from_dict /
    __post_init__ — a bad file can never yield a spec object).
    """

    return FactorSpec.from_dict(read_yaml(path))

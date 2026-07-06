"""Beginner NL flow: idea text -> validated FactorSpec draft (deterministic).

Uses the existing deterministic idea parser only — no LLM, no network, and no
writes to any factor_root. The returned spec is a draft proposal; execution
requires the caller to observe the validation gate result and the human
confirmation points defined in the LLM-boundary document.
"""

from __future__ import annotations

from pathlib import Path

from quant_forge.factor_library.repository import parse_idea_to_definition
from quant_forge.specs.factor_spec import FactorSpec
from quant_forge.specs.validation_gate import SpecValidationResult, validate_factor_spec
from quant_forge.utils import read_yaml, write_yaml


def factor_spec_from_idea(text: str) -> tuple[FactorSpec, SpecValidationResult]:
    """Deterministically draft a FactorSpec from a beginner idea and gate it."""

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
    return spec, validate_factor_spec(spec)


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

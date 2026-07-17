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

# No-silent-fallback, extended past the rank(close) catch-all. The workbench
# now carries price-volume AND point-in-time fundamentals (growth / profitability
# / valuation / leverage / cashflow, e.g. netprofit_yoy / roe / pe_ttm — see
# data/fundamentals.py). Those domains are expressible, so they are NOT flagged.
# What remains genuinely unavailable is per-stock ALTERNATIVE data — sentiment /
# analyst-report / northbound-flow (hsgt is market-level, not per-stock) /
# dragon-tiger / institutional-holding / shareholder-count. An idea about those
# can only degrade to a price-volume/fundamental formula, so it must carry a
# review warning. (Any unexposed single line-item — goodwill, inventory — is
# caught honestly instead by the missing-field validation gate.)
#
# Curated, conservative recall over a fixed alt-data set; terms avoid colliding
# with in-scope vocabulary. English terms stay multi-char to avoid matching
# inside unrelated words (e.g. "analyst" not "anal").
_OUT_OF_SCOPE_DATA_TERMS: tuple[str, ...] = (
    # alternative data (Chinese) — genuinely unavailable per-stock
    "舆情", "研报", "分析师", "北向", "龙虎榜", "机构持仓", "增减持", "股东户数",
    "股吧", "新闻",
    # alternative data (English)
    "sentiment", "analyst", "news flow", "northbound",
)
_OUT_OF_SCOPE_DATA_WARNING = (
    "本数据集有量价 + 基本面字段，但没有该想法涉及的另类数据（舆情/研报/资金流/"
    "龙虎榜/机构持仓 等），公式只是最接近的近似，并非真实实现，请复核 / this idea "
    "references alternative data (sentiment / analyst / flow) not in the dataset; "
    "the formula is only a nearest approximation, not a faithful implementation - "
    "review before running"
)


def out_of_scope_data_warnings(text: str) -> tuple[str, ...]:
    """Warn when an idea references a data domain the price-volume panel lacks.

    Returns a single review warning (or empty) so both parse modes can attach
    it: an idea about profit growth, valuation multiples, or alt-data can only
    degrade to a price-volume formula here, and that degradation must not be
    presented as a confident match.
    """

    lowered = text.lower()
    for term in _OUT_OF_SCOPE_DATA_TERMS:
        if term in lowered:
            return (_OUT_OF_SCOPE_DATA_WARNING,)
    return ()


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
    extra = out_of_scope_data_warnings(text)
    if extra:
        result = replace(result, warnings=result.warnings + extra)
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

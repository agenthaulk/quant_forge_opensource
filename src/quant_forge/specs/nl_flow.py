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

# No-silent-fallback, extended past the rank(close) catch-all. The public
# workbench panel has only price-volume fields (close / is_st / market_cap /
# return_* / volatility_* / volume). When an idea references a data domain the
# panel cannot express -- financial statements, valuation multiples, alt-data --
# the parser (LLM or rule) can only degrade to a price-volume formula, so it may
# silently return e.g. rank(return_5d) for a fundamentals idea and look
# confident. Detect the domain mismatch from the idea text and attach a review
# warning so an approximation is never presented as a faithful implementation.
#
# This is a curated, deliberately conservative heuristic (recall over a fixed
# set, not exhaustive): terms are chosen to avoid colliding with in-scope
# price-volume vocabulary (收益/回报/动量/波动/成交量/市值/价格) and with the
# built-in demo seeds (notably the bare word 估值 is intentionally NOT listed,
# so the "低估值" seed keeps its existing rank(close) catch-all behavior).
_OUT_OF_SCOPE_DATA_TERMS: tuple[str, ...] = (
    # fundamentals / financial statements (Chinese)
    "基本面", "利润", "净利", "毛利", "营收", "营业收入", "财报", "年报", "季报",
    "中报", "业绩", "每股收益", "净资产", "资产负债", "现金流", "负债", "商誉",
    "分红", "股息", "派息", "应收", "存货", "扣非",
    # valuation multiples that need fundamentals (the bare 估值/价值 is excluded)
    "市盈", "市净", "市销", "股息率",
    # alternative data (Chinese)
    "舆情", "研报", "分析师", "北向", "龙虎榜", "机构持仓", "增减持", "股东户数",
    # fundamentals / valuation / alt-data (English, multi-char to avoid matches
    # inside unrelated words like "approach" / "broad")
    "earnings", "revenue", "profit", "dividend", "cash flow", "cashflow",
    "book value", "ebitda", "fundamental", "p/e", "p/b", "sentiment", "analyst",
)
_OUT_OF_SCOPE_DATA_WARNING = (
    "本数据集只有量价字段（close/market_cap/return/volatility/volume 等），"
    "无法表达该想法涉及的基本面/估值/另类数据，公式只是最接近的量价近似，"
    "并非该想法的真实实现，请复核 / this idea references data not in the "
    "price-volume dataset; the formula is only a nearest price-volume "
    "approximation, not a faithful implementation - review before running"
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

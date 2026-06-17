"""Convert structured RD hypotheses into validated factor experiment plans."""

from __future__ import annotations

import re

from quant_forge.factor_engine.formula_parser import inspect_formula
from quant_forge.factor_library.catalog import is_precomputed_formula
from quant_forge.mcp.read_models import list_available_fields, list_available_operators
from quant_forge.operator_registry.resolver import resolve_formula_operators
from quant_forge.research_loop.contracts import (
    FactorExperimentPlan,
    PlanStatus,
    ResearchContext,
    StructuredResearchHypothesis,
)


class ExperimentPlanner:
    """Resolve fields, operators, filters, and direction before factor calculation."""

    def __init__(self, *, require_expected_direction: bool = True, canonicalize_to_positive_alpha: bool = True) -> None:
        self.require_expected_direction = require_expected_direction
        self.canonicalize_to_positive_alpha = canonicalize_to_positive_alpha

    def plan(
        self,
        hypothesis: StructuredResearchHypothesis,
        context: ResearchContext,
    ) -> FactorExperimentPlan:
        formula = hypothesis.formula_dsl.strip()
        raw_formula = formula
        canonical_formula = formula
        operator_resolution: dict[str, object] = {}
        blocking: list[str] = []
        warnings: list[str] = []
        fields = _available_fields(context)
        operators = _available_operators(context)

        if not formula or formula == "未指定":
            blocking.append("formula_dsl is missing")
        elif _contains_st_numeric_feature(formula):
            blocking.append("ST status must be a universe filter, not a numeric formula field")

        allow_whole_precomputed = hypothesis.parameter_search_fallback
        resolution = None
        if formula and formula != "未指定" and not allow_whole_precomputed:
            resolution = resolve_formula_operators(formula)
            operator_resolution = resolution.to_dict()
            canonical_formula = resolution.canonical_formula
            if resolution.executable:
                formula = canonical_formula
            elif resolution.requires_operator_draft_review:
                blocking.extend(resolution.blocking_errors or ("operator requires draft review",))
            else:
                blocking.extend(resolution.blocking_errors or ("formula validation failed",))

        parsed = (
            inspect_formula(
                formula,
                known_operators=operators,
                allow_whole_precomputed=allow_whole_precomputed,
                is_whole_precomputed=is_precomputed_formula,
            )
            if formula and formula != "未指定" and (resolution is None or resolution.executable)
            else None
        )
        if parsed is not None and parsed.is_valid:
            for operator in parsed.operators:
                if operator not in operators:
                    blocking.append(f"unknown operator: {operator}")
            for field_name in parsed.fields:
                if field_name not in fields:
                    blocking.append(f"unknown field: {field_name}")
        elif parsed is not None:
            unknown_operators = [operator for operator in parsed.operators if operator not in operators]
            if unknown_operators:
                blocking.append(f"unknown operator: {unknown_operators[0]}")
            blocking.extend(parsed.errors or ("formula validation failed",))
        elif formula and formula != "未指定":
            blocking.append("formula validation failed")

        universe_filters, filter_reasons = _resolve_filters(hypothesis, fields)
        blocking.extend(filter_reasons)

        formula_canonicalized = False
        plan_direction = hypothesis.expected_direction
        if (
            formula
            and parsed is not None
            and parsed.is_valid
            and not blocking
            and hypothesis.expected_direction == "negative"
            and self.canonicalize_to_positive_alpha
        ):
            formula, formula_canonicalized = _canonical_formula(formula)
            canonical_formula = formula
            plan_direction = "positive"

        if self.require_expected_direction and hypothesis.expected_direction == "unknown":
            blocking.append("expected_direction is unknown")

        status = _plan_status(blocking)
        field_resolution = {
            "available_fields": sorted(fields),
            "used_fields": list(parsed.fields) if parsed else [],
            "unknown_fields": [
                reason.removeprefix("unknown field: ")
                for reason in blocking
                if reason.startswith("unknown field: ")
            ],
        }
        operator_validation = {
            "available_operators": sorted(operators),
            "used_operator": parsed.operators[0] if parsed and parsed.operators else "",
            "used_operators": list(parsed.operators) if parsed else [],
            "unknown_operators": _unknown_operators_from_resolution(operator_resolution, blocking),
            "operator_resolution": operator_resolution,
            "is_valid": status == "ready",
        }
        if hypothesis.parameter_search_fallback:
            warnings.append("hypothesis uses parameter-search fallback")
        return FactorExperimentPlan(
            plan_id=f"{hypothesis.hypothesis_id}-p01",
            hypothesis_id=hypothesis.hypothesis_id,
            status=status,
            factor_name=_factor_name(hypothesis),
            formula_dsl=formula,
            raw_formula_dsl=raw_formula,
            canonical_formula_dsl=canonical_formula,
            inputs=hypothesis.input_fields or (parsed.fields if parsed else ()),
            universe_filters=tuple(universe_filters),
            expected_direction=plan_direction,
            field_resolution=field_resolution,
            operator_validation=operator_validation,
            blocking_reasons=tuple(dict.fromkeys(blocking)),
            warnings=tuple(dict.fromkeys(warnings)),
            metadata={
                "hypothesis_source": hypothesis.source,
                "source_detail": hypothesis.source_detail,
                "priority": hypothesis.priority,
                "parameter_search_fallback": hypothesis.parameter_search_fallback,
                "raw_expected_direction": hypothesis.expected_direction,
                "formula_canonicalized_to_positive_alpha": formula_canonicalized,
                "operator_resolution": operator_resolution,
            },
        )


def default_context() -> ResearchContext:
    return ResearchContext(
        available_fields=tuple(field["name"] for field in list_available_fields()),
        available_operators=tuple(operator["name"] for operator in list_available_operators()),
        available_filters=("is_st == false",),
    )


def _available_fields(context: ResearchContext) -> set[str]:
    if context.available_fields:
        return {field.split(".")[-1] for field in context.available_fields}
    return {field["name"] for field in list_available_fields()}


def _available_operators(context: ResearchContext) -> set[str]:
    if context.available_operators:
        return set(context.available_operators)
    return {str(operator["name"]) for operator in list_available_operators()}


def _unknown_operators_from_resolution(
    operator_resolution: dict[str, object],
    blocking: list[str],
) -> list[str]:
    items = operator_resolution.get("items", []) if operator_resolution else []
    unknowns: list[str] = []
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("status") in {"unknown_operator", "unknown_requires_draft"}:
                original = str(item.get("original_name", "")).strip()
                if original:
                    unknowns.append(original)
    unknowns.extend(
        reason.removeprefix("unknown operator: ")
        for reason in blocking
        if reason.startswith("unknown operator: ")
    )
    return list(dict.fromkeys(unknowns))


def _contains_st_numeric_feature(formula: str) -> bool:
    lowered = formula.lower()
    return any(token in lowered for token in ("is_st", "special_treatment", "st_status"))


def _resolve_filters(hypothesis: StructuredResearchHypothesis, fields: set[str]) -> tuple[list[str], list[str]]:
    filters: list[str] = []
    reasons: list[str] = []
    for constraint in hypothesis.universe_constraints:
        text = constraint.strip()
        if not text:
            continue
        if _requests_non_st_filter(text):
            if "is_st" in fields:
                filters.append("is_st == false")
            else:
                reasons.append("non-ST filter requested but is_st is unavailable")
        elif "st" in text.lower() or "风险警示" in text:
            reasons.append("ST event factors are not available in the lightweight public kernel")
    return filters, reasons


def _requests_non_st_filter(text: str) -> bool:
    normalized = text.lower().replace(" ", "").replace("-", "_")
    return any(
        token in normalized
        for token in (
            "非st",
            "non_st",
            "excludest",
            "exclude_st",
            "剔除st",
            "排除st",
            "is_st==false",
            "is_st==0",
            "notis_st",
        )
    )


def _canonical_formula(formula: str) -> tuple[str, bool]:
    text = formula.strip()
    if text.startswith("-"):
        return text, True
    return f"-{text}", True


def _plan_status(blocking_reasons: list[str]) -> PlanStatus:
    if not blocking_reasons:
        return "ready"
    text = "\n".join(blocking_reasons)
    if "formula_dsl" in text:
        return "blocked_missing_formula"
    if "ST status" in text or "ST event" in text:
        return "blocked_pit_event_feature_required"
    if "operator" in text and "draft" in text:
        return "requires_operator_draft_review"
    if "operator" in text:
        return "blocked_formula_invalid"
    if "field" in text or "non-ST filter" in text:
        return "blocked_missing_field"
    if "expected_direction" in text:
        return "blocked_direction_unknown"
    return "blocked_formula_invalid"


def _factor_name(hypothesis: StructuredResearchHypothesis) -> str:
    return re.sub(r"[^a-zA-Z0-9_]+", "_", hypothesis.hypothesis_id.strip().lower()).strip("_") or "rd_factor"

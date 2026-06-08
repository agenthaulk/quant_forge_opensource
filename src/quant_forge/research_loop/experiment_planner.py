"""Convert structured RD hypotheses into validated factor experiment plans."""

from __future__ import annotations

from dataclasses import dataclass
import re

from quant_forge.factor_library.catalog import is_precomputed_formula
from quant_forge.mcp.read_models import list_available_fields, list_available_operators
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
        blocking: list[str] = []
        warnings: list[str] = []
        fields = _available_fields(context)
        operators = _available_operators(context)

        if not formula or formula == "未指定":
            blocking.append("formula_dsl is missing")
        elif _contains_st_numeric_feature(formula):
            blocking.append("ST status must be a universe filter, not a numeric formula field")

        allow_whole_precomputed = hypothesis.parameter_search_fallback
        parsed = (
            _parse_formula(formula, known_operators=operators, allow_whole_precomputed=allow_whole_precomputed)
            if formula and formula != "未指定"
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
            "unknown_operators": [
                reason.removeprefix("unknown operator: ")
                for reason in blocking
                if reason.startswith("unknown operator: ")
            ],
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
    return {operator["name"] for operator in list_available_operators()}


@dataclass(frozen=True)
class _FormulaParts:
    operators: tuple[str, ...]
    fields: tuple[str, ...]
    errors: tuple[str, ...]
    is_valid: bool


def _parse_formula(
    formula: str,
    *,
    known_operators: set[str] | None = None,
    allow_whole_precomputed: bool = False,
) -> _FormulaParts | None:
    operators: list[str] = []
    fields: list[str] = []
    errors: list[str] = []
    is_valid = _collect_formula_parts(
        formula,
        operators=operators,
        fields=fields,
        errors=errors,
        known_operators=known_operators or set(),
        allow_precomputed=allow_whole_precomputed,
    )
    return _FormulaParts(
        operators=tuple(dict.fromkeys(operators)),
        fields=tuple(dict.fromkeys(fields)),
        errors=tuple(dict.fromkeys(errors)),
        is_valid=is_valid,
    )


def _collect_formula_parts(
    text: str,
    *,
    operators: list[str],
    fields: list[str],
    errors: list[str],
    known_operators: set[str],
    allow_precomputed: bool,
) -> bool:
    expression = text.strip()
    while expression.startswith("-"):
        expression = expression[1:].strip()
    if not expression:
        errors.append("empty expression")
        return False
    if expression.lower().startswith("precomputed:"):
        if allow_precomputed and is_precomputed_formula(expression):
            return True
        errors.append("precomputed formulas can only be used as whole seed factors")
        return False
    if _is_number(expression):
        return True
    binary = _split_top_level_binary(expression)
    if binary is not None:
        left, _, right = binary
        return all(
            _collect_formula_parts(
                item,
                operators=operators,
                fields=fields,
                errors=errors,
                known_operators=known_operators,
                allow_precomputed=False,
            )
            for item in (left, right)
        )
    if re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_.]*", expression):
        fields.append(expression.split(".")[-1])
        return True
    call = _parse_call(expression)
    if call is None:
        errors.append("formula validation failed")
        return False
    operator, args = call
    operators.append(operator)
    signature_ok = _validate_operator_signature(operator, args, known_operators=known_operators, errors=errors)
    child_ok = bool(args) and all(
        _collect_formula_parts(
            arg,
            operators=operators,
            fields=fields,
            errors=errors,
            known_operators=known_operators,
            allow_precomputed=False,
        )
        for arg in args
    )
    return signature_ok and child_ok


def _split_top_level_binary(expression: str) -> tuple[str, str, str] | None:
    for operators in ("+-", "*/"):
        depth = 0
        for index in range(len(expression) - 1, -1, -1):
            char = expression[index]
            if char == ")":
                depth += 1
            elif char == "(":
                depth -= 1
            elif depth == 0 and char in operators and not _is_unary_operator(expression, index):
                left = expression[:index].strip()
                right = expression[index + 1 :].strip()
                if not left or not right:
                    return None
                return left, char, right
    return None


def _is_unary_operator(expression: str, index: int) -> bool:
    if expression[index] not in "+-":
        return False
    previous = expression[:index].rstrip()
    return not previous or previous[-1] in "(,+-*/"


def _parse_call(expression: str) -> tuple[str, list[str]] | None:
    match = re.match(r"^([a-zA-Z_][a-zA-Z0-9_]*)\(", expression)
    if not match or not expression.endswith(")"):
        return None
    operator = match.group(1)
    body = expression[len(operator) + 1 : -1]
    try:
        args = _split_args(body)
    except ValueError:
        return None
    return operator, args


def _split_args(text: str) -> list[str]:
    args: list[str] = []
    depth = 0
    start = 0
    for index, char in enumerate(text):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                raise ValueError("unbalanced formula parentheses")
        elif char == "," and depth == 0:
            args.append(text[start:index].strip())
            start = index + 1
    if depth != 0:
        raise ValueError("unbalanced formula parentheses")
    tail = text[start:].strip()
    if tail:
        args.append(tail)
    return args


def _is_number(value: str) -> bool:
    try:
        float(value)
    except ValueError:
        return False
    return True


def _validate_operator_signature(
    operator: str,
    args: list[str],
    *,
    known_operators: set[str],
    errors: list[str],
) -> bool:
    if operator not in known_operators:
        return True
    if operator in {"rank", "zscore", "abs", "log", "sign"}:
        return _expect_arity(operator, args, 1, errors)
    if operator in {"delay", "delta", "ts_sum", "ts_mean", "ts_min", "ts_max", "stddev", "ts_rank", "decay_linear"}:
        return _expect_arity(operator, args, 2, errors) and _expect_number_arg(operator, args, 1, errors)
    if operator in {"correlation", "covariance"}:
        return _expect_arity(operator, args, 3, errors) and _expect_number_arg(operator, args, 2, errors)
    if operator == "scale":
        if len(args) not in {1, 2}:
            errors.append("scale expects 1 or 2 arguments")
            return False
        return len(args) == 1 or _expect_number_arg(operator, args, 1, errors)
    if operator in {"signedpower", "wq_min", "wq_max"}:
        return _expect_arity(operator, args, 2, errors)
    return True


def _expect_arity(operator: str, args: list[str], expected: int, errors: list[str]) -> bool:
    if len(args) == expected:
        return True
    errors.append(f"{operator} expects {expected} argument{'s' if expected != 1 else ''}")
    return False


def _expect_number_arg(operator: str, args: list[str], index: int, errors: list[str]) -> bool:
    if index < len(args) and _is_number(args[index]):
        return True
    errors.append(f"{operator} argument {index + 1} must be a number")
    return False


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
    if "operator" in text:
        return "requires_operator_draft_review"
    if "field" in text or "non-ST filter" in text:
        return "blocked_missing_field"
    if "expected_direction" in text:
        return "blocked_direction_unknown"
    return "blocked_formula_invalid"


def _factor_name(hypothesis: StructuredResearchHypothesis) -> str:
    return re.sub(r"[^a-zA-Z0-9_]+", "_", hypothesis.hypothesis_id.strip().lower()).strip("_") or "rd_factor"

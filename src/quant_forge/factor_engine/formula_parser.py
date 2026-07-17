"""Safe AST parsing helpers for public factor formulas."""

from __future__ import annotations

import ast
from dataclasses import dataclass
import math
from typing import Callable

# Robustness bounds for model-supplied formulas (P5): windows above three
# trading years or degenerate formula sizes are rejected at the parse gate
# before they can drive lookback/rolling-window memory blowups.
MAX_WINDOW_ROWS = 750
MAX_FORMULA_CHARS = 2000

SUPPORTED_OPERATORS = {
    "abs",
    "correlation",
    "covariance",
    "decay_linear",
    "delay",
    "delta",
    "log",
    "ntile",
    "rank",
    "scale",
    "sign",
    "signedpower",
    "stddev",
    "ts_argmax",
    "ts_argmin",
    "ts_max",
    "ts_mean",
    "ts_min",
    "ts_rank",
    "ts_sum",
    "winsorize",
    "wq_max",
    "wq_min",
    "zscore",
}


class FormulaParseError(ValueError):
    """Raised when a formula falls outside the public safe expression grammar."""


@dataclass(frozen=True)
class FormulaInspection:
    operators: tuple[str, ...]
    fields: tuple[str, ...]
    errors: tuple[str, ...]
    is_valid: bool


def parse_formula_node(formula: str) -> ast.AST:
    expression = formula.strip()
    if not expression:
        raise FormulaParseError("empty expression")
    if len(expression) > MAX_FORMULA_CHARS:
        raise FormulaParseError(f"formula length {len(expression)} exceeds the {MAX_FORMULA_CHARS}-character limit")
    if "precomputed:" in expression.lower():
        raise FormulaParseError("precomputed formulas can only be used as whole seed factors")
    try:
        node = ast.parse(expression, mode="eval").body
    except SyntaxError as exc:
        raise FormulaParseError("formula validation failed") from exc
    _validate_safe_node(node)
    return node


def inspect_formula(
    formula: str,
    *,
    known_operators: set[str] | None = None,
    allow_whole_precomputed: bool = False,
    is_whole_precomputed: Callable[[str], bool] | None = None,
) -> FormulaInspection:
    expression = formula.strip()
    if expression.lower().startswith("precomputed:"):
        if allow_whole_precomputed and is_whole_precomputed is not None and is_whole_precomputed(expression):
            return FormulaInspection(operators=(), fields=(), errors=(), is_valid=True)
        return FormulaInspection(
            operators=(),
            fields=(),
            errors=("precomputed formulas can only be used as whole seed factors",),
            is_valid=False,
        )
    try:
        node = parse_formula_node(expression)
    except FormulaParseError as exc:
        return FormulaInspection(operators=(), fields=(), errors=(str(exc),), is_valid=False)

    operators: list[str] = []
    fields: list[str] = []
    errors: list[str] = []
    _collect_formula_parts(
        node,
        operators=operators,
        fields=fields,
        errors=errors,
        known_operators=known_operators or set(),
    )
    return FormulaInspection(
        operators=tuple(dict.fromkeys(operators)),
        fields=tuple(dict.fromkeys(fields)),
        errors=tuple(dict.fromkeys(errors)),
        is_valid=not errors,
    )


def formula_lookback_rows(formula: str) -> int:
    """Return the prior per-instrument rows needed to evaluate a formula."""

    return _node_lookback_rows(parse_formula_node(formula))


def field_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = field_name(node.value)
        if node.attr.startswith("_"):
            raise FormulaParseError("formula validation failed")
        return f"{base}.{node.attr}"
    raise FormulaParseError("formula validation failed")


def numeric_constant(node: ast.AST) -> float | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        value = float(node.value)
        return value if math.isfinite(value) else None
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = numeric_constant(node.operand)
        if value is None:
            return None
        return -value if isinstance(node.op, ast.USub) else value
    return None


def _validate_safe_node(node: ast.AST) -> None:
    if isinstance(node, ast.Constant):
        if numeric_constant(node) is None:
            raise FormulaParseError("formula validation failed")
        return
    if isinstance(node, (ast.Name, ast.Attribute)):
        field_name(node)
        return
    if isinstance(node, ast.UnaryOp):
        if isinstance(node.op, ast.UAdd):
            if numeric_constant(node) is None:
                raise FormulaParseError("formula validation failed")
            return
        if not isinstance(node.op, ast.USub):
            raise FormulaParseError("formula validation failed")
        _validate_safe_node(node.operand)
        return
    if isinstance(node, ast.BinOp):
        if not isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
            raise FormulaParseError("formula validation failed")
        _validate_safe_node(node.left)
        _validate_safe_node(node.right)
        return
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.keywords:
            raise FormulaParseError("formula validation failed")
        for arg in node.args:
            _validate_safe_node(arg)
        return
    raise FormulaParseError("formula validation failed")


def _collect_formula_parts(
    node: ast.AST,
    *,
    operators: list[str],
    fields: list[str],
    errors: list[str],
    known_operators: set[str],
) -> None:
    if isinstance(node, ast.Call):
        operator = node.func.id
        operators.append(operator)
        _validate_operator_signature(operator, node.args, known_operators=known_operators, errors=errors)
        for arg in node.args:
            _collect_formula_parts(
                arg,
                operators=operators,
                fields=fields,
                errors=errors,
                known_operators=known_operators,
            )
        return
    if isinstance(node, (ast.Name, ast.Attribute)):
        fields.append(field_name(node).split(".")[-1])
        return
    if isinstance(node, ast.BinOp):
        _collect_formula_parts(
            node.left,
            operators=operators,
            fields=fields,
            errors=errors,
            known_operators=known_operators,
        )
        _collect_formula_parts(
            node.right,
            operators=operators,
            fields=fields,
            errors=errors,
            known_operators=known_operators,
        )
        return
    if isinstance(node, ast.UnaryOp):
        _collect_formula_parts(
            node.operand,
            operators=operators,
            fields=fields,
            errors=errors,
            known_operators=known_operators,
        )


def _node_lookback_rows(node: ast.AST) -> int:
    if isinstance(node, ast.Call):
        operator = node.func.id
        args = list(node.args)
        child_lookbacks = [_node_lookback_rows(arg) for arg in args]
        if operator in {"delay", "delta"} and len(args) >= 2:
            return child_lookbacks[0] + _positive_int_arg(args[1])
        if (
            operator
            in {"ts_sum", "ts_mean", "ts_min", "ts_max", "stddev", "ts_rank", "decay_linear", "ts_argmax", "ts_argmin"}
            and len(args) >= 2
        ):
            return child_lookbacks[0] + max(_positive_int_arg(args[1]) - 1, 0)
        if operator in {"correlation", "covariance"} and len(args) >= 3:
            return max(child_lookbacks[0], child_lookbacks[1]) + max(_positive_int_arg(args[2]) - 1, 0)
        if operator in {"wq_min", "wq_max"} and len(args) >= 2 and numeric_constant(args[1]) is not None:
            return child_lookbacks[0] + max(_positive_int_arg(args[1]) - 1, 0)
        return max(child_lookbacks, default=0)
    if isinstance(node, ast.BinOp):
        return max(_node_lookback_rows(node.left), _node_lookback_rows(node.right))
    if isinstance(node, ast.UnaryOp):
        return _node_lookback_rows(node.operand)
    return 0


def _positive_int_arg(node: ast.AST) -> int:
    value = numeric_constant(node)
    if value is None:
        return 1
    window = max(int(math.floor(value)), 1)
    if window > MAX_WINDOW_ROWS:
        raise FormulaParseError(f"window argument {window} exceeds the {MAX_WINDOW_ROWS}-row limit")
    return window


def _validate_operator_signature(
    operator: str,
    args: list[ast.AST],
    *,
    known_operators: set[str],
    errors: list[str],
) -> bool:
    if operator not in known_operators:
        return True
    if operator in {"rank", "zscore", "abs", "log", "sign"}:
        return _expect_arity(operator, args, 1, errors)
    if operator in {
        "delay",
        "delta",
        "ts_sum",
        "ts_mean",
        "ts_min",
        "ts_max",
        "stddev",
        "ts_rank",
        "decay_linear",
        "ts_argmax",
        "ts_argmin",
    }:
        return _expect_arity(operator, args, 2, errors) and _expect_window_arg(operator, args, 1, errors)
    if operator == "winsorize":
        if not _expect_arity(operator, args, 2, errors):
            return False
        fraction = numeric_constant(args[1])
        if fraction is None or not (0.0 <= fraction < 0.5):
            errors.append("winsorize argument 2 must be a quantile fraction in [0, 0.5)")
            return False
        return True
    if operator == "ntile":
        if not _expect_arity(operator, args, 2, errors):
            return False
        buckets = numeric_constant(args[1])
        if buckets is None or math.floor(buckets) < 2:
            errors.append("ntile argument 2 must be an integer bucket count >= 2")
            return False
        return True
    if operator in {"correlation", "covariance"}:
        return _expect_arity(operator, args, 3, errors) and _expect_window_arg(operator, args, 2, errors)
    if operator == "scale":
        if len(args) not in {1, 2}:
            errors.append("scale expects 1 or 2 arguments")
            return False
        return len(args) == 1 or _expect_number_arg(operator, args, 1, errors)
    if operator in {"signedpower", "wq_min", "wq_max"}:
        if not _expect_arity(operator, args, 2, errors):
            return False
        # wq_min/wq_max accept EITHER a scalar rolling window (wq_max(x, 20) ->
        # ts_max) OR a series (wq_max(x, volume) -> pairwise max); only the
        # scalar-window form carries a window to bound. Match _node_lookback_rows
        # and the executor, which treat arg[1] as a window exactly when it is a
        # numeric constant, so the resolver/inspect gate rejects oversized
        # windows for these operators too (P5) without touching the pairwise form.
        if operator in {"wq_min", "wq_max"} and numeric_constant(args[1]) is not None:
            return _expect_window_arg(operator, args, 1, errors)
        return True
    return True


def _expect_arity(operator: str, args: list[ast.AST], expected: int, errors: list[str]) -> bool:
    if len(args) == expected:
        return True
    errors.append(f"{operator} expects {expected} argument{'s' if expected != 1 else ''}")
    return False


def _expect_number_arg(operator: str, args: list[ast.AST], index: int, errors: list[str]) -> bool:
    if index < len(args) and numeric_constant(args[index]) is not None:
        return True
    errors.append(f"{operator} argument {index + 1} must be a number")
    return False


def _expect_window_arg(operator: str, args: list[ast.AST], index: int, errors: list[str]) -> bool:
    if not _expect_number_arg(operator, args, index, errors):
        return False
    value = numeric_constant(args[index])
    if value is not None and math.floor(value) > MAX_WINDOW_ROWS:
        errors.append(f"{operator} argument {index + 1} must be <= {MAX_WINDOW_ROWS}")
        return False
    return True

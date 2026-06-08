"""Small safe formula executor for public factors."""

from __future__ import annotations

import math
import re

import numpy as np
import pandas as pd

SUPPORTED_OPERATORS = {
    "abs",
    "correlation",
    "covariance",
    "decay_linear",
    "delay",
    "delta",
    "log",
    "rank",
    "scale",
    "sign",
    "signedpower",
    "stddev",
    "ts_max",
    "ts_mean",
    "ts_min",
    "ts_rank",
    "ts_sum",
    "wq_max",
    "wq_min",
    "zscore",
}


def execute_factor_formula(panel: pd.DataFrame, formula: str, universe_filters: tuple[str, ...] = ()) -> pd.DataFrame:
    required = {"trade_date", "instrument"}
    missing = required.difference(panel.columns)
    if missing:
        raise ValueError(f"panel missing required columns: {sorted(missing)}")

    result = panel[["trade_date", "instrument"]].copy()
    result["score"] = _eval_expression(panel, formula.strip())
    for filter_expression in universe_filters:
        result.loc[~_eval_filter(panel, filter_expression), "score"] = pd.NA
    return result


def _eval_expression(panel: pd.DataFrame, expression: str) -> pd.Series:
    expression = expression.strip()
    if expression.startswith("-"):
        return -_eval_expression(panel, expression[1:].strip())
    if _is_number(expression):
        return pd.Series(float(expression), index=panel.index, dtype="float64")
    if expression.lower().startswith("precomputed:"):
        raise ValueError("precomputed factors are only supported as whole factor formulas")

    binary = _split_top_level_binary(expression)
    if binary is not None:
        left_expression, operator, right_expression = binary
        left = _eval_expression(panel, left_expression)
        right = _eval_expression(panel, right_expression)
        if operator == "+":
            return left + right
        if operator == "-":
            return left - right
        if operator == "*":
            return left * right
        if operator == "/":
            return left / right.replace(0, pd.NA)

    call = _parse_call(expression)
    if call:
        operator, args = call
        if operator not in SUPPORTED_OPERATORS:
            raise ValueError(f"unsupported factor operator: {operator}")
        if operator in {"rank", "zscore"}:
            values = _one_series_arg(panel, operator, args)
            grouped = values.groupby(panel["trade_date"])
            if operator == "rank":
                return grouped.rank(pct=True)
            mean = grouped.transform("mean")
            std = grouped.transform("std").replace(0, pd.NA)
            return (values - mean) / std
        if operator == "abs":
            return _one_series_arg(panel, operator, args).abs()
        if operator == "log":
            values = _one_series_arg(panel, operator, args)
            return pd.Series(np.log(values.where(values > 0)), index=panel.index)
        if operator == "sign":
            return pd.Series(np.sign(_one_series_arg(panel, operator, args)), index=panel.index)
        if operator == "delay":
            return _by_instrument(panel, _one_series_arg(panel, operator, args[:1]), lambda values: values.shift(_window(args, 1)))
        if operator == "delta":
            values = _one_series_arg(panel, operator, args[:1])
            return values - _by_instrument(panel, values, lambda series: series.shift(_window(args, 1)))
        if operator in {"ts_sum", "ts_mean", "ts_min", "ts_max", "stddev", "ts_rank", "decay_linear"}:
            return _rolling_operator(panel, operator, args)
        if operator in {"correlation", "covariance"}:
            if len(args) != 3:
                raise ValueError(f"{operator} expects 3 arguments")
            left = _eval_expression(panel, args[0])
            right = _eval_expression(panel, args[1])
            window = _window(args, 2)
            return _rolling_pairwise(panel, left, right, window=window, operator=operator)
        if operator == "scale":
            if len(args) not in {1, 2}:
                raise ValueError("scale expects 1 or 2 arguments")
            values = _eval_expression(panel, args[0])
            target = float(args[1]) if len(args) == 2 else 1.0
            denom = values.abs().groupby(panel["trade_date"]).transform("sum").replace(0, pd.NA)
            return values / denom * target
        if operator == "signedpower":
            if len(args) != 2:
                raise ValueError("signedpower expects 2 arguments")
            values = _eval_expression(panel, args[0])
            exponent = _eval_expression(panel, args[1])
            return pd.Series(np.sign(values) * (values.abs() ** exponent), index=panel.index)
        if operator in {"wq_min", "wq_max"}:
            if len(args) != 2:
                raise ValueError(f"{operator} expects 2 arguments")
            left = _eval_expression(panel, args[0])
            if _is_number(args[1]):
                rolling_name = "ts_min" if operator == "wq_min" else "ts_max"
                return _rolling_operator(panel, rolling_name, [args[0], args[1]])
            right = _eval_expression(panel, args[1])
            func = np.minimum if operator == "wq_min" else np.maximum
            return pd.Series(func(left, right), index=panel.index)

    return _field(panel, expression)


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
                    raise ValueError("binary operator requires two operands")
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
    start = len(operator) + 1
    depth = 0
    for index, char in enumerate(expression[start:-1], start=start):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                return None
    if depth != 0:
        return None
    return operator, _split_args(expression[start:-1])


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
    tail = text[start:].strip()
    if tail:
        args.append(tail)
    return args


def _one_series_arg(panel: pd.DataFrame, operator: str, args: list[str]) -> pd.Series:
    if len(args) != 1:
        raise ValueError(f"{operator} expects 1 argument")
    return _eval_expression(panel, args[0])


def _rolling_operator(panel: pd.DataFrame, operator: str, args: list[str]) -> pd.Series:
    if len(args) != 2:
        raise ValueError(f"{operator} expects 2 arguments")
    values = _eval_expression(panel, args[0])
    window = _window(args, 1)
    grouped = values.groupby(panel["instrument"], sort=False)
    if operator == "ts_sum":
        return grouped.rolling(window, min_periods=window).sum().reset_index(level=0, drop=True)
    if operator == "ts_mean":
        return grouped.rolling(window, min_periods=window).mean().reset_index(level=0, drop=True)
    if operator == "ts_min":
        return grouped.rolling(window, min_periods=window).min().reset_index(level=0, drop=True)
    if operator == "ts_max":
        return grouped.rolling(window, min_periods=window).max().reset_index(level=0, drop=True)
    if operator == "stddev":
        return grouped.rolling(window, min_periods=window).std().reset_index(level=0, drop=True)
    if operator == "ts_rank":
        return grouped.rolling(window, min_periods=window).apply(_last_rank_pct, raw=False).reset_index(level=0, drop=True)
    if operator == "decay_linear":
        weights = np.arange(1, window + 1, dtype=float)
        weights = weights / weights.sum()
        return (
            grouped.rolling(window, min_periods=window)
            .apply(lambda window_values: float(np.dot(window_values, weights)), raw=True)
            .reset_index(level=0, drop=True)
        )
    raise ValueError(f"unsupported rolling operator: {operator}")


def _rolling_pairwise(
    panel: pd.DataFrame,
    left: pd.Series,
    right: pd.Series,
    *,
    window: int,
    operator: str,
) -> pd.Series:
    result = pd.Series(np.nan, index=panel.index, dtype="float64")
    for _, positions in panel.groupby("instrument", sort=False).groups.items():
        left_values = left.loc[positions]
        right_values = right.loc[positions]
        if operator == "correlation":
            result.loc[positions] = left_values.rolling(window, min_periods=window).corr(right_values)
        elif operator == "covariance":
            result.loc[positions] = left_values.rolling(window, min_periods=window).cov(right_values)
        else:
            raise ValueError(f"unsupported pairwise rolling operator: {operator}")
    return result


def _by_instrument(panel: pd.DataFrame, values: pd.Series, transform) -> pd.Series:
    return values.groupby(panel["instrument"], sort=False).transform(transform)


def _window(args: list[str], index: int) -> int:
    if index >= len(args) or not _is_number(args[index]):
        raise ValueError("window argument must be a number")
    return max(int(math.floor(float(args[index]))), 1)


def _last_rank_pct(values: pd.Series) -> float:
    return float(values.rank(pct=True).iloc[-1])


def _is_number(value: str) -> bool:
    try:
        float(value)
    except ValueError:
        return False
    return True


def _field(panel: pd.DataFrame, name: str) -> pd.Series:
    column = name.split(".")[-1]
    if column not in panel.columns:
        raise ValueError(f"unsupported or missing factor field: {column}")
    if not pd.api.types.is_numeric_dtype(panel[column]):
        raise ValueError(f"factor field must be numeric: {column}")
    return panel[column].astype(float)


def _eval_filter(panel: pd.DataFrame, expression: str) -> pd.Series:
    normalized = expression.strip().lower()
    if normalized in {"is_st == false", "is_st == 0", "not is_st"}:
        if "is_st" not in panel.columns:
            raise ValueError("filter requires missing field: is_st")
        return ~panel["is_st"].astype(bool)
    raise ValueError(f"unsupported universe filter: {expression}")

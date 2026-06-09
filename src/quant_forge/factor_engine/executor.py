"""Small safe formula executor for public factors."""

from __future__ import annotations

import ast
import math

import numpy as np
import pandas as pd

from quant_forge.factor_engine.formula_parser import (
    SUPPORTED_OPERATORS,
    field_name,
    numeric_constant,
    parse_formula_node,
)


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
    return _eval_node(panel, parse_formula_node(expression))


def _eval_node(panel: pd.DataFrame, node: ast.AST) -> pd.Series:
    if isinstance(node, ast.Constant):
        value = numeric_constant(node)
        if value is None:
            raise ValueError("formula validation failed")
        return pd.Series(value, index=panel.index, dtype="float64")
    if isinstance(node, (ast.Name, ast.Attribute)):
        return _field(panel, field_name(node))
    if isinstance(node, ast.UnaryOp):
        values = _eval_node(panel, node.operand)
        if isinstance(node.op, ast.USub):
            return -values
        if isinstance(node.op, ast.UAdd):
            return values
    if isinstance(node, ast.BinOp):
        left = _eval_node(panel, node.left)
        right = _eval_node(panel, node.right)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div):
            return left / right.replace(0, pd.NA)
    if isinstance(node, ast.Call):
        operator = node.func.id
        args = list(node.args)
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
            values = _one_series_arg(panel, operator, args[:1])
            return _by_instrument(panel, values, lambda series: series.shift(_window(args, 1)))
        if operator == "delta":
            values = _one_series_arg(panel, operator, args[:1])
            return values - _by_instrument(panel, values, lambda series: series.shift(_window(args, 1)))
        if operator in {"ts_sum", "ts_mean", "ts_min", "ts_max", "stddev", "ts_rank", "decay_linear"}:
            return _rolling_operator(panel, operator, args)
        if operator in {"correlation", "covariance"}:
            if len(args) != 3:
                raise ValueError(f"{operator} expects 3 arguments")
            left = _eval_node(panel, args[0])
            right = _eval_node(panel, args[1])
            window = _window(args, 2)
            return _rolling_pairwise(panel, left, right, window=window, operator=operator)
        if operator == "scale":
            if len(args) not in {1, 2}:
                raise ValueError("scale expects 1 or 2 arguments")
            values = _eval_node(panel, args[0])
            target = _number_arg(args[1], "scale target") if len(args) == 2 else 1.0
            denom = values.abs().groupby(panel["trade_date"]).transform("sum").replace(0, pd.NA)
            return values / denom * target
        if operator == "signedpower":
            if len(args) != 2:
                raise ValueError("signedpower expects 2 arguments")
            values = _eval_node(panel, args[0])
            exponent = _eval_node(panel, args[1])
            return pd.Series(np.sign(values) * (values.abs() ** exponent), index=panel.index)
        if operator in {"wq_min", "wq_max"}:
            if len(args) != 2:
                raise ValueError(f"{operator} expects 2 arguments")
            left = _eval_node(panel, args[0])
            if numeric_constant(args[1]) is not None:
                rolling_name = "ts_min" if operator == "wq_min" else "ts_max"
                return _rolling_operator(panel, rolling_name, [args[0], args[1]])
            right = _eval_node(panel, args[1])
            func = np.minimum if operator == "wq_min" else np.maximum
            return pd.Series(func(left, right), index=panel.index)
    raise ValueError("formula validation failed")


def _one_series_arg(panel: pd.DataFrame, operator: str, args: list[ast.AST]) -> pd.Series:
    if len(args) != 1:
        raise ValueError(f"{operator} expects 1 argument")
    return _eval_node(panel, args[0])


def _rolling_operator(panel: pd.DataFrame, operator: str, args: list[ast.AST]) -> pd.Series:
    if len(args) != 2:
        raise ValueError(f"{operator} expects 2 arguments")
    values = _eval_node(panel, args[0])
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
        return (
            grouped.rolling(window, min_periods=window)
            .apply(_last_rank_pct, raw=False)
            .reset_index(level=0, drop=True)
        )
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


def _window(args: list[ast.AST], index: int) -> int:
    if index >= len(args):
        raise ValueError("window argument must be a number")
    return max(int(math.floor(_number_arg(args[index], "window argument"))), 1)


def _last_rank_pct(values: pd.Series) -> float:
    return float(values.rank(pct=True).iloc[-1])


def _number_arg(node: ast.AST, label: str) -> float:
    value = numeric_constant(node)
    if value is None:
        raise ValueError(f"{label} must be a number")
    return value


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

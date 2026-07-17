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

    # Rolling operators (ts_*, decay_linear, correlation, ...) assume each
    # instrument's rows are in ascending trade_date order. Sort by
    # (instrument, trade_date) before evaluating so the result is independent of
    # the caller's row order, then restore the caller's original order so
    # positionally-aligned consumers are unaffected.
    ordered_positions = _stable_panel_order(panel)
    ordered = panel.iloc[ordered_positions].reset_index(drop=True)

    result = ordered[["trade_date", "instrument"]].copy()
    result["score"] = _eval_expression(ordered, formula.strip())
    for filter_expression in universe_filters:
        result.loc[~_eval_filter(ordered, filter_expression), "score"] = pd.NA

    # Reorder rows back to the caller's original input order.
    restore = np.empty(len(panel), dtype=int)
    restore[ordered_positions] = np.arange(len(panel))
    return result.iloc[restore].reset_index(drop=True)


def _stable_panel_order(panel: pd.DataFrame) -> np.ndarray:
    """Positional order that sorts rows by (instrument, trade_date) stably.

    Returns integer positions into ``panel`` (0..n-1) rather than index labels,
    so the caller can restore the original row order regardless of the panel's
    index. Uses a stable mergesort so ties preserve input order.
    """

    keyframe = pd.DataFrame(
        {
            "instrument": panel["instrument"].to_numpy(),
            "trade_date": panel["trade_date"].to_numpy(),
        }
    )
    order = keyframe.sort_values(["instrument", "trade_date"], kind="stable").index
    return np.asarray(order, dtype=int)


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
        if operator == "winsorize":
            if len(args) != 2:
                raise ValueError("winsorize expects 2 arguments")
            values = _eval_node(panel, args[0])
            fraction = _number_arg(args[1], "winsorize quantile")
            if not 0.0 <= fraction < 0.5:
                raise ValueError("winsorize quantile must be in [0, 0.5)")
            grouped = values.groupby(panel["trade_date"])
            lower = grouped.transform(lambda group: group.quantile(fraction))
            upper = grouped.transform(lambda group: group.quantile(1.0 - fraction))
            return values.clip(lower=lower, upper=upper)
        if operator == "ntile":
            if len(args) != 2:
                raise ValueError("ntile expects 2 arguments")
            values = _eval_node(panel, args[0])
            buckets = int(math.floor(_number_arg(args[1], "ntile bucket count")))
            if buckets < 2:
                raise ValueError("ntile bucket count must be >= 2")
            percentile = values.groupby(panel["trade_date"]).rank(pct=True)
            return np.ceil(percentile * buckets).clip(lower=1, upper=buckets)
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
        if operator in {"ts_sum", "ts_mean", "ts_min", "ts_max", "stddev", "ts_rank", "decay_linear", "ts_argmax", "ts_argmin"}:
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
        return _rolling_last_rank_pct(panel, values, window=window)
    if operator == "decay_linear":
        weights = np.arange(1, window + 1, dtype=float)
        weights = weights / weights.sum()
        return _rolling_weighted_sum(panel, values, weights=weights)
    if operator in {"ts_argmax", "ts_argmin"}:
        return _rolling_days_since_extreme(panel, values, window=window, operator=operator)
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


def _rolling_last_rank_pct(panel: pd.DataFrame, values: pd.Series, *, window: int) -> pd.Series:
    result = pd.Series(np.nan, index=panel.index, dtype="float64")
    for _, positions in panel.groupby("instrument", sort=False).groups.items():
        group_values = values.loc[positions].to_numpy(dtype=float)
        if group_values.size < window:
            continue
        windows = np.lib.stride_tricks.sliding_window_view(group_values, window)
        last = windows[:, -1]
        finite = np.isfinite(windows)
        valid_count = finite.sum(axis=1)
        valid_last = np.isfinite(last)
        less = ((windows < last[:, None]) & finite).sum(axis=1)
        equal = ((windows == last[:, None]) & finite).sum(axis=1)
        ranks = (less + (equal + 1) / 2) / valid_count
        ranks[(valid_count == 0) | ~valid_last] = np.nan
        group_result = np.full(group_values.size, np.nan, dtype=float)
        group_result[window - 1 :] = ranks
        result.loc[positions] = group_result
    return result


def _rolling_days_since_extreme(
    panel: pd.DataFrame,
    values: pd.Series,
    *,
    window: int,
    operator: str,
) -> pd.Series:
    """Days since the most recent rolling max/min per instrument (0 at bar t).

    For the trailing window ending at ``t`` (inclusive) the result is the number
    of bars between ``t`` and the extreme value's position: 0 when the current
    bar holds the extreme, ``window - 1`` when the oldest bar does. Ties resolve
    to the most recent occurrence. Only rows up to ``t`` enter the window
    (PIT-safe), and any window containing a NaN yields NaN, matching the
    ``min_periods=window`` contract shared by the other ``ts_*`` operators.
    """

    take_argextreme = np.argmax if operator == "ts_argmax" else np.argmin
    result = pd.Series(np.nan, index=panel.index, dtype="float64")
    for _, positions in panel.groupby("instrument", sort=False).groups.items():
        group_values = values.loc[positions].to_numpy(dtype=float)
        if group_values.size < window:
            continue
        windows = np.lib.stride_tricks.sliding_window_view(group_values, window)
        # Scan each window from the current bar backwards: the index of the
        # extreme in the reversed window is exactly the days-since count, and the
        # first hit wins so ties collapse to the most recent bar.
        days_since = take_argextreme(windows[:, ::-1], axis=1).astype(float)
        days_since[~np.isfinite(windows).all(axis=1)] = np.nan
        group_result = np.full(group_values.size, np.nan, dtype=float)
        group_result[window - 1 :] = days_since
        result.loc[positions] = group_result
    return result


def _rolling_weighted_sum(panel: pd.DataFrame, values: pd.Series, *, weights: np.ndarray) -> pd.Series:
    result = pd.Series(np.nan, index=panel.index, dtype="float64")
    window = int(weights.size)
    for _, positions in panel.groupby("instrument", sort=False).groups.items():
        group_values = values.loc[positions].to_numpy(dtype=float)
        if group_values.size < window:
            continue
        group_result = np.full(group_values.size, np.nan, dtype=float)
        group_result[window - 1 :] = np.correlate(group_values, weights, mode="valid")
        result.loc[positions] = group_result
    return result


def _window(args: list[ast.AST], index: int) -> int:
    if index >= len(args):
        raise ValueError("window argument must be a number")
    return max(int(math.floor(_number_arg(args[index], "window argument"))), 1)


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

"""Small safe formula executor for public factors."""

from __future__ import annotations

import re

import pandas as pd

SUPPORTED_OPERATORS = {"rank", "zscore"}


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
    if expression.startswith("-"):
        return -_eval_expression(panel, expression[1:].strip())

    call = re.fullmatch(r"([a-zA-Z_][a-zA-Z0-9_]*)\(([^()]+)\)", expression)
    if call:
        operator, argument = call.group(1), call.group(2).strip()
        if operator not in SUPPORTED_OPERATORS:
            raise ValueError(f"unsupported factor operator: {operator}")
        values = _field(panel, argument)
        grouped = values.groupby(panel["trade_date"])
        if operator == "rank":
            return grouped.rank(pct=True)
        if operator == "zscore":
            mean = grouped.transform("mean")
            std = grouped.transform("std").replace(0, pd.NA)
            return (values - mean) / std

    return _field(panel, expression)


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

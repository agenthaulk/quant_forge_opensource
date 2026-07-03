"""COR-5 regression: rolling operators must be invariant to caller row order.

Encodes Appendix A of docs/first_principles_review_20260703.md — a shuffled
panel must yield the same rolling-operator scores as a sorted panel, and the
returned row order must match the caller's input order.
"""

from __future__ import annotations

import pandas as pd
import pytest

from quant_forge.factor_engine.executor import execute_factor_formula


def _build_panels() -> tuple[pd.DataFrame, pd.DataFrame]:
    closes = {"AAA": [10, 11, 12, 13, 14], "BBB": [20, 18, 16, 14, 12]}
    dates = ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04", "2026-01-05"]
    rows = [
        {"trade_date": d, "instrument": i, "close": float(c)}
        for i, series in closes.items()
        for d, c in zip(dates, series)
    ]
    sorted_panel = (
        pd.DataFrame(rows)
        .sort_values(["trade_date", "instrument"])
        .reset_index(drop=True)
    )
    shuffled = (
        sorted_panel.sort_values(["instrument", "trade_date"], ascending=[True, False])
        .reset_index(drop=True)
    )
    return sorted_panel, shuffled


@pytest.mark.parametrize(
    "formula",
    ["ts_rank(close, 3)", "decay_linear(close, 3)", "ts_mean(close, 3)"],
)
def test_rolling_operators_are_row_order_invariant(formula: str) -> None:
    sorted_panel, shuffled = _build_panels()

    a = (
        execute_factor_formula(sorted_panel, formula)
        .set_index(["trade_date", "instrument"])
        .sort_index()["score"]
    )
    b = (
        execute_factor_formula(shuffled, formula)
        .set_index(["trade_date", "instrument"])
        .sort_index()["score"]
    )
    assert a.round(9).equals(b.round(9))


def test_result_preserves_caller_input_row_order() -> None:
    _, shuffled = _build_panels()

    result = execute_factor_formula(shuffled, "ts_mean(close, 3)")

    assert list(result["trade_date"]) == list(shuffled["trade_date"])
    assert list(result["instrument"]) == list(shuffled["instrument"])

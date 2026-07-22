"""Gold-vector coverage for group_neutralize / residualize (upstream batch 2).

Two cross-sectional neutralization operators, each pinned to a fixed small panel
with frozen expected values (NaN-aware comparison), plus its point-in-time
contract (each trade date is independent, so future bars never change a past
result):

* ``group_neutralize(factor, group)`` — within-group demean by trade date. A
  single-member group demeans to 0; a row with a missing group label yields
  null; NaN factor values stay NaN.
* ``residualize(y, x)`` — per-trade-date OLS residual of y on x with an
  intercept. Degenerate cross-sections (fewer than two finite pairs, or a
  regressor with zero variance) yield null, as do rows with a missing y or x.

The registry reconciliation (executor ``SUPPORTED_OPERATORS`` <-> registry) is
exercised so the two sides cannot drift.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from quant_forge.data.local import LocalPanelDataProvider, create_demo_workspace
from quant_forge.factor_engine.executor import execute_factor_formula
from quant_forge.factor_engine.formula_parser import SUPPORTED_OPERATORS, inspect_formula
from quant_forge.operator_registry import (
    load_default_operator_registry,
    resolve_formula_operators,
)

NEW_OPERATORS = ("group_neutralize", "residualize")


def _scores(panel: pd.DataFrame, formula: str) -> np.ndarray:
    return execute_factor_formula(panel, formula)["score"].to_numpy(dtype=float)


def _one_date_grouped(values: list[float], groups: list[object]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "trade_date": pd.to_datetime(["2024-01-01"] * len(values)),
            "instrument": [f"I{i:02d}" for i in range(len(values))],
            "close": values,
            "industry": groups,
        }
    )


def _one_date_xy(y_values: list[float], x_values: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "trade_date": pd.to_datetime(["2024-01-01"] * len(y_values)),
            "instrument": [f"I{i:02d}" for i in range(len(y_values))],
            "y": y_values,
            "x": x_values,
        }
    )


# ---------------------------------------------------------------------------
# Registry / executor reconciliation
# ---------------------------------------------------------------------------


def test_new_operators_reconcile_registry_and_executor() -> None:
    registry = load_default_operator_registry()  # raises if the two sides drift
    for name in NEW_OPERATORS:
        assert name in SUPPORTED_OPERATORS
        spec = registry.operators[name]
        assert spec.execution_status == "implemented"
        assert spec.audit_status == "core_reviewed"
        assert spec.pit_safety == {"uses_future_data": False, "alignment": "t"}


def test_new_operators_are_executable_through_the_resolver() -> None:
    for formula in (
        "group_neutralize(rank(return_1d), industry)",
        "residualize(return_5d, return_1d)",
    ):
        result = resolve_formula_operators(formula)
        assert result.executable is True, (formula, result.blocking_errors)


# ---------------------------------------------------------------------------
# group_neutralize gold vectors
# ---------------------------------------------------------------------------


def test_group_neutralize_gold_vector() -> None:
    # Groups A=[1,3] (mean 2) and B=[5,7] (mean 6); C=[10] is a singleton.
    panel = _one_date_grouped(
        [1.0, 3.0, 10.0, 5.0, 7.0],
        ["801080", "801080", "801120", "801150", "801150"],
    )
    np.testing.assert_allclose(
        _scores(panel, "group_neutralize(close, industry)"),
        [-1.0, 1.0, 0.0, -1.0, 1.0],
    )


def test_group_neutralize_singleton_is_zero_and_missing_group_is_null() -> None:
    # C=[10] is a single-member group -> demeans to 0; the last row has no
    # group label, so it cannot be neutralized and stays null.
    panel = _one_date_grouped(
        [1.0, 3.0, 10.0, 5.0, 7.0, 9.0],
        ["801080", "801080", "801120", "801150", "801150", None],
    )
    np.testing.assert_allclose(
        _scores(panel, "group_neutralize(close, industry)"),
        [-1.0, 1.0, 0.0, -1.0, 1.0, np.nan],
        equal_nan=True,
    )


def test_group_neutralize_propagates_nan_factor_values() -> None:
    # A NaN factor value stays NaN; the group mean is taken over the finite
    # members, so the peer's residual is still well defined.
    panel = _one_date_grouped(
        [2.0, np.nan, 4.0, 8.0],
        ["801080", "801080", "801150", "801150"],
    )
    np.testing.assert_allclose(
        _scores(panel, "group_neutralize(close, industry)"),
        [0.0, np.nan, -2.0, 2.0],
        equal_nan=True,
    )


def test_group_neutralize_is_independent_per_trade_date() -> None:
    first = _one_date_grouped([1.0, 3.0, 5.0, 7.0], ["801080", "801080", "801150", "801150"])
    second = pd.concat(
        [
            first,
            pd.DataFrame(
                {
                    "trade_date": pd.to_datetime(["2024-01-02"] * 4),
                    "instrument": [f"I{i:02d}" for i in range(4)],
                    "close": [10.0, 20.0, 30.0, 40.0],
                    "industry": ["801080", "801080", "801150", "801150"],
                }
            ),
        ],
        ignore_index=True,
    )
    np.testing.assert_allclose(
        _scores(second, "group_neutralize(close, industry)")[:4],
        _scores(first, "group_neutralize(close, industry)"),
    )


def test_group_neutralize_requires_a_group_field_name() -> None:
    known = set(SUPPORTED_OPERATORS)
    # The group argument must be a field reference, not a numeric literal.
    assert not inspect_formula("group_neutralize(close, 5)", known_operators=known).is_valid
    assert inspect_formula("group_neutralize(close, industry)", known_operators=known).is_valid
    panel = _one_date_grouped([1.0, 3.0], ["801080", "801080"])
    with pytest.raises(ValueError, match="group argument must be a field name"):
        execute_factor_formula(panel, "group_neutralize(close, 5)")


# ---------------------------------------------------------------------------
# residualize gold vectors
# ---------------------------------------------------------------------------


def test_residualize_gold_vector() -> None:
    # OLS of y on x with intercept: slope 4, intercept -4, so the fitted line is
    # 4x - 4 and the residuals sum to zero.
    panel = _one_date_xy([2.0, 4.0, 6.0, 8.0, 20.0], [1.0, 2.0, 3.0, 4.0, 5.0])
    np.testing.assert_allclose(
        _scores(panel, "residualize(y, x)"),
        [2.0, 0.0, -2.0, -4.0, 4.0],
    )


def test_residualize_perfect_fit_is_zero_residual() -> None:
    panel = _one_date_xy([2.0, 4.0, 6.0, 8.0], [1.0, 2.0, 3.0, 4.0])
    np.testing.assert_allclose(_scores(panel, "residualize(y, x)"), [0.0, 0.0, 0.0, 0.0], atol=1e-12)


def test_residualize_degenerate_cross_sections_are_null() -> None:
    # A single-point cross-section cannot be fitted.
    single = _one_date_xy([3.0], [5.0])
    np.testing.assert_allclose(_scores(single, "residualize(y, x)"), [np.nan], equal_nan=True)
    # A regressor with zero variance leaves the slope unidentified.
    flat_x = _one_date_xy([1.0, 2.0, 3.0], [5.0, 5.0, 5.0])
    np.testing.assert_allclose(_scores(flat_x, "residualize(y, x)"), [np.nan, np.nan, np.nan], equal_nan=True)


def test_residualize_nulls_rows_with_missing_inputs() -> None:
    # The NaN-x row drops out of the fit and is reported null; the remaining
    # points still define a perfect line (residuals 0).
    panel = _one_date_xy([2.0, 4.0, 6.0, 99.0], [1.0, 2.0, 3.0, np.nan])
    np.testing.assert_allclose(
        _scores(panel, "residualize(y, x)"),
        [0.0, 0.0, 0.0, np.nan],
        equal_nan=True,
        atol=1e-12,
    )


def test_residualize_is_independent_per_trade_date() -> None:
    first = _one_date_xy([2.0, 4.0, 6.0, 9.0], [1.0, 2.0, 3.0, 4.0])
    second = pd.concat(
        [
            first,
            pd.DataFrame(
                {
                    "trade_date": pd.to_datetime(["2024-01-02"] * 4),
                    "instrument": [f"I{i:02d}" for i in range(4)],
                    "y": [10.0, 5.0, 30.0, 12.0],
                    "x": [1.0, 2.0, 3.0, 4.0],
                }
            ),
        ],
        ignore_index=True,
    )
    np.testing.assert_allclose(
        _scores(second, "residualize(y, x)")[:4],
        _scores(first, "residualize(y, x)"),
    )


# ---------------------------------------------------------------------------
# Demo panel executability
# ---------------------------------------------------------------------------


def test_operators_execute_on_the_demo_panel(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    panel = LocalPanelDataProvider(paths["data_root"]).load_panel()

    neutral = execute_factor_formula(panel, "group_neutralize(rank(return_1d), industry)")
    assert neutral["score"].notna().any()
    # Within each (date, industry) group the neutralized factor sums to zero.
    grouped = neutral.assign(industry=panel["industry"]).dropna(subset=["score"])
    sums = grouped.groupby(["trade_date", "industry"])["score"].sum()
    np.testing.assert_allclose(sums.to_numpy(), np.zeros(len(sums)), atol=1e-9)

    residual = execute_factor_formula(panel, "residualize(return_5d, return_1d)")
    assert residual["score"].notna().any()

"""Gold-vector coverage for ts_argmax / ts_argmin / winsorize / ntile.

Each operator is pinned to a fixed small panel with frozen expected values
(NaN-aware comparison), plus its point-in-time contract:

* ``ts_argmax`` / ``ts_argmin`` — days since the most recent rolling extreme,
  0 at the current bar, ties to the most recent bar, ``min_periods=window`` so a
  window with any NaN yields NaN; future bars never change a past result.
* ``winsorize`` / ``ntile`` — cross-sectional by trade date, so one date's
  output is independent of every other date (PIT-safe by construction).

The registry reconciliation (executor ``SUPPORTED_OPERATORS`` <-> registry) is
exercised so the two sides cannot drift.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quant_forge.factor_engine.executor import execute_factor_formula
from quant_forge.factor_engine.formula_parser import (
    MAX_WINDOW_ROWS,
    SUPPORTED_OPERATORS,
    inspect_formula,
)
from quant_forge.operator_registry import (
    load_default_operator_registry,
    resolve_formula_operators,
)

NEW_OPERATORS = ("ts_argmax", "ts_argmin", "winsorize", "ntile")


def _scores(panel: pd.DataFrame, formula: str) -> np.ndarray:
    return execute_factor_formula(panel, formula)["score"].to_numpy(dtype=float)


def _single_instrument(close: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "trade_date": pd.bdate_range("2024-01-01", periods=len(close)),
            "instrument": ["A"] * len(close),
            "close": close,
        }
    )


def _one_date(values: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "trade_date": pd.to_datetime(["2024-01-01"] * len(values)),
            "instrument": [f"I{i:02d}" for i in range(len(values))],
            "close": values,
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
        "ts_argmax(close, 20)",
        "ts_argmin(close, 20)",
        "winsorize(return_5d, 0.02)",
        "ntile(rank(return_5d), 5)",
    ):
        result = resolve_formula_operators(formula)
        assert result.executable is True, (formula, result.blocking_errors)


# ---------------------------------------------------------------------------
# ts_argmax / ts_argmin gold vectors
# ---------------------------------------------------------------------------


def test_ts_argmax_gold_vector() -> None:
    # window=3, min_periods=3 -> first two bars NaN. Current bar = position 0.
    panel = _single_instrument([1.0, 3.0, 2.0, 5.0, 4.0, 4.0])
    np.testing.assert_allclose(
        _scores(panel, "ts_argmax(close, 3)"),
        [np.nan, np.nan, 1.0, 0.0, 1.0, 2.0],
        equal_nan=True,
    )


def test_ts_argmin_gold_vector_breaks_ties_to_most_recent() -> None:
    # Last window [5, 4, 4]: the minimum (4) is tied on the current bar and one
    # bar back; the most-recent tie wins, so days-since is 0.
    panel = _single_instrument([1.0, 3.0, 2.0, 5.0, 4.0, 4.0])
    np.testing.assert_allclose(
        _scores(panel, "ts_argmin(close, 3)"),
        [np.nan, np.nan, 2.0, 1.0, 2.0, 0.0],
        equal_nan=True,
    )


def test_ts_argmax_is_computed_per_instrument() -> None:
    dates = pd.bdate_range("2024-01-01", periods=6)
    panel = pd.DataFrame(
        {
            "trade_date": list(dates) * 2,
            "instrument": ["A"] * 6 + ["B"] * 6,
            "close": [1.0, 3.0, 2.0, 5.0, 4.0, 4.0, 9.0, 1.0, 9.0, 2.0, 9.0, 3.0],
        }
    )
    np.testing.assert_allclose(
        _scores(panel, "ts_argmax(close, 3)"),
        [np.nan, np.nan, 1.0, 0.0, 1.0, 2.0, np.nan, np.nan, 0.0, 1.0, 0.0, 1.0],
        equal_nan=True,
    )


def test_ts_argmax_window_with_nan_yields_nan() -> None:
    # min_periods=window: a trailing window that still contains a NaN produces
    # NaN, matching the other ts_* operators.
    panel = _single_instrument([1.0, np.nan, 2.0, 5.0])
    scores = _scores(panel, "ts_argmax(close, 3)")
    # bar 2 window [1, nan, 2] -> NaN; bar 3 window [nan, 2, 5] -> NaN.
    np.testing.assert_allclose(scores, [np.nan, np.nan, np.nan, np.nan], equal_nan=True)


def test_ts_argmax_is_point_in_time() -> None:
    base = _single_instrument([1.0, 3.0, 2.0, 5.0, 4.0, 4.0])
    extended = _single_instrument([1.0, 3.0, 2.0, 5.0, 4.0, 4.0, 999.0, 0.0])
    np.testing.assert_allclose(
        _scores(extended, "ts_argmax(close, 3)")[:6],
        _scores(base, "ts_argmax(close, 3)"),
        equal_nan=True,
    )


# ---------------------------------------------------------------------------
# winsorize gold vectors
# ---------------------------------------------------------------------------


def test_winsorize_gold_vector_clips_cross_section_to_quantile_bounds() -> None:
    # Cross-section [1, 2, 3, 4, 100], q=0.2 -> bounds [1.8, 23.2] (linear
    # interpolation); tails clip, interior untouched.
    panel = _one_date([1.0, 2.0, 3.0, 4.0, 100.0])
    np.testing.assert_allclose(
        _scores(panel, "winsorize(close, 0.2)"),
        [1.8, 2.0, 3.0, 4.0, 23.2],
    )


def test_winsorize_zero_quantile_is_identity() -> None:
    panel = _one_date([1.0, 2.0, 3.0, 4.0, 100.0])
    np.testing.assert_allclose(_scores(panel, "winsorize(close, 0)"), [1.0, 2.0, 3.0, 4.0, 100.0])


def test_winsorize_is_independent_per_trade_date() -> None:
    first = _one_date([1.0, 2.0, 3.0, 50.0])
    second = pd.concat(
        [
            first,
            pd.DataFrame(
                {
                    "trade_date": pd.to_datetime(["2024-01-02"] * 4),
                    "instrument": [f"I{i:02d}" for i in range(4)],
                    "close": [7.0, 8.0, 9.0, 10.0],
                }
            ),
        ],
        ignore_index=True,
    )
    np.testing.assert_allclose(
        _scores(second, "winsorize(close, 0.25)")[:4],
        _scores(first, "winsorize(close, 0.25)"),
    )


# ---------------------------------------------------------------------------
# ntile gold vectors
# ---------------------------------------------------------------------------


def test_ntile_gold_vector_labels_quintiles() -> None:
    panel = _one_date([float(v) for v in range(10, 101, 10)])
    np.testing.assert_allclose(
        _scores(panel, "ntile(close, 5)"),
        [1.0, 1.0, 2.0, 2.0, 3.0, 3.0, 4.0, 4.0, 5.0, 5.0],
    )


def test_ntile_propagates_nan_and_stays_in_range() -> None:
    panel = _one_date([10.0, 20.0, 30.0, np.nan])
    scores = _scores(panel, "ntile(close, 2)")
    np.testing.assert_allclose(scores, [1.0, 2.0, 2.0, np.nan], equal_nan=True)
    finite = scores[np.isfinite(scores)]
    assert finite.min() >= 1.0 and finite.max() <= 2.0


# ---------------------------------------------------------------------------
# Signature / bound validation
# ---------------------------------------------------------------------------


def test_signature_validation_rejects_out_of_range_arguments() -> None:
    known = set(SUPPORTED_OPERATORS)
    assert not inspect_formula("winsorize(close, 0.5)", known_operators=known).is_valid
    assert not inspect_formula("winsorize(close, 0.6)", known_operators=known).is_valid
    assert not inspect_formula("ntile(close, 1)", known_operators=known).is_valid
    assert not inspect_formula(f"ts_argmax(close, {MAX_WINDOW_ROWS + 1})", known_operators=known).is_valid
    # Valid forms pass.
    assert inspect_formula("winsorize(close, 0.02)", known_operators=known).is_valid
    assert inspect_formula("ntile(close, 5)", known_operators=known).is_valid
    assert inspect_formula("ts_argmax(close, 20)", known_operators=known).is_valid


def test_executor_defends_against_out_of_range_arguments() -> None:
    panel = _one_date([1.0, 2.0, 3.0, 4.0])
    with pytest.raises(ValueError, match=r"winsorize quantile must be in \[0, 0.5\)"):
        execute_factor_formula(panel, "winsorize(close, 0.5)")
    with pytest.raises(ValueError, match="ntile bucket count must be >= 2"):
        execute_factor_formula(panel, "ntile(close, 1)")

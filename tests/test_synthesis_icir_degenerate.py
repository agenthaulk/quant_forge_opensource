"""§13 ICIR degeneracy hardening: std==0 guard, finite clip, equal-weight fallback.

Design contract (docs/design/multi_factor_portfolio_backtest.md §4.4,
"ICIR NaN-hardening" review resolution): a constant IC series has zero
standard deviation and carries NO stability signal — the raw ICIR weight is
0 through an EXPLICIT ``std == 0`` guard, never an inf/NaN division
artifact. Negative and non-finite raw weights clip to 0 through an explicit
finite check (never a bare ``max``, whose result depends on NaN argument
order). A clipped vector that sums to zero falls back to per-date
equal-weight flagged ``IC_DEGENERATE_EQUAL_WEIGHT`` — the rebalance still
trades a full cross-section instead of collapsing to NaN and silently
vanishing (RB-7 posture). And because a degenerate fallback is NOT a
genuine fit, a window where EVERY fittable rebalance degenerates reports
``is_fitted=False`` + ``NO_FITTED_PERIODS`` (RB-8): the run traded equal
weight throughout and must say so.

Fixtures reuse the exact-return close-chain construction from
tests/test_synthesis_ic_pit.py: per-period returns are rank-ordered by a
chosen target permutation, so per-period rank ICs are exact closed-form
values (+-1 and +-RHO).
"""

from __future__ import annotations

import math

import pandas as pd
import pytest
from quant_forge.synthesis.service import (
    IC_DEGENERATE_EQUAL_WEIGHT,
    NO_FITTED_PERIODS,
    WARM_UP_IC_UNFITTED,
    _normalize_ic_weights,
    build_fitted_composite,
)

from tests.test_synthesis_ic_pit import (
    IC_MIN_PERIODS,
    RHO,
    SCORES_A,
    SCORES_B,
    _build_fixture,
)

DIRECTIONS = {"F_ALPHA": 1, "F_BRAVO": 1}
EQUAL = {"F_ALPHA": 0.5, "F_BRAVO": 0.5}
REVERSED_TARGET = [5.0, 4.0, 3.0, 2.0, 1.0, 0.0]
DELAY, HOLDING = 1, 2


def _fitted(members, close, dates, *, method: str, ic_min_periods: int = IC_MIN_PERIODS):
    return build_fitted_composite(
        members,
        directions=DIRECTIONS,
        standardization="rank",
        method=method,
        close=close,
        dates=dates,
        delay=DELAY,
        holding=HOLDING,
        ic_min_periods=ic_min_periods,
    )


def _assert_rebalances_never_emptied(result, dates) -> None:
    """The fallback keeps every grid signal date tradeable, never all-NaN."""

    frame = result.composite
    for entry in result.weights_path:
        cross_section = frame[frame["trade_date"] == dates[entry.signal_index]]
        finite = int(cross_section["score"].notna().sum())
        assert finite == len(SCORES_A), (
            f"rebalance at index {entry.signal_index} lost its cross-section"
        )
        assert dates[entry.signal_index] not in result.degenerate_dates


# ---------------------------------------------------------------------------
# Constant IC series: the explicit std == 0 guard (ICIR only)
# ---------------------------------------------------------------------------


def test_constant_ic_series_zeroes_icir_and_falls_back_equal_weight() -> None:
    # Every period return-ordered by A: IC_A is constantly 1, IC_B constantly
    # RHO. Both stds are exactly 0 -> both raw ICIR weights are 0 via the
    # guard -> all-zero vector -> per-date equal-weight fallback, flagged.
    dates, close, members, grid, _ = _build_fixture(
        delay=DELAY, holding=HOLDING, target_for_period=lambda ordinal: SCORES_A
    )
    result = _fitted(members, close, dates, method="icir_weighted")

    fittable = [entry for entry in result.weights_path if entry.flag != WARM_UP_IC_UNFITTED]
    assert fittable, "fixture must admit fittable rebalances"
    assert all(entry.flag == IC_DEGENERATE_EQUAL_WEIGHT for entry in fittable)
    assert all(entry.weights == EQUAL for entry in fittable)
    assert IC_DEGENERATE_EQUAL_WEIGHT in result.warning_codes
    assert result.degenerate_weight_period_count == len(fittable)
    # A degenerate fallback is not a genuine fit: an all-degenerate window is
    # the RB-8 downgrade — it traded equal weight throughout.
    assert result.fitted_period_count == 0
    assert result.is_fitted is False
    assert NO_FITTED_PERIODS in result.warning_codes
    assert result.fitted_weights_latest is None
    _assert_rebalances_never_emptied(result, dates)


def test_same_constant_series_still_fits_under_ic_weighted() -> None:
    # Contrast pin: the std==0 guard is ICIR-specific. ic_weighted sees the
    # constant positive means (1 and RHO) and fits genuinely.
    dates, close, members, _, _ = _build_fixture(
        delay=DELAY, holding=HOLDING, target_for_period=lambda ordinal: SCORES_A
    )
    result = _fitted(members, close, dates, method="ic_weighted")
    assert result.is_fitted is True
    assert IC_DEGENERATE_EQUAL_WEIGHT not in result.warning_codes
    fitted_entries = [entry for entry in result.weights_path if entry.flag is None]
    assert fitted_entries
    for entry in fitted_entries:
        assert entry.weights["F_ALPHA"] == pytest.approx(1.0 / (1.0 + RHO), abs=1e-12)


def test_alternating_ic_series_fits_genuinely_under_icir() -> None:
    # Alternating targets give both members a non-constant, positive-mean IC
    # series (values alternate between 1 and RHO): std > 0, so ICIR fits —
    # the guard must not over-trigger on healthy variation.
    dates, close, members, _, _ = _build_fixture(
        delay=DELAY,
        holding=HOLDING,
        target_for_period=lambda ordinal: SCORES_A if ordinal % 2 == 0 else SCORES_B,
    )
    result = _fitted(members, close, dates, method="icir_weighted")
    assert result.is_fitted is True
    assert NO_FITTED_PERIODS not in result.warning_codes
    fitted_entries = [entry for entry in result.weights_path if entry.flag is None]
    assert fitted_entries
    for entry in fitted_entries:
        assert entry.weights["F_ALPHA"] > 0.0
        assert entry.weights["F_BRAVO"] > 0.0
        assert sum(entry.weights.values()) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# All-negative means: clip -> all-zero -> fallback, never an empty rebalance
# ---------------------------------------------------------------------------


def test_all_negative_ic_means_fall_back_and_downgrade() -> None:
    # Every period return-ordered by the REVERSE of A: IC_A is constantly -1,
    # IC_B constantly -RHO. Both means are negative -> clipped to 0 -> the
    # per-date fallback fires on every fittable rebalance and the run
    # downgrades (it never produced a fitted vector).
    dates, close, members, _, _ = _build_fixture(
        delay=DELAY, holding=HOLDING, target_for_period=lambda ordinal: REVERSED_TARGET
    )
    result = _fitted(members, close, dates, method="ic_weighted")

    fittable = [entry for entry in result.weights_path if entry.flag != WARM_UP_IC_UNFITTED]
    assert fittable
    assert all(entry.flag == IC_DEGENERATE_EQUAL_WEIGHT for entry in fittable)
    assert all(entry.weights == EQUAL for entry in fittable)
    assert result.is_fitted is False
    assert NO_FITTED_PERIODS in result.warning_codes
    _assert_rebalances_never_emptied(result, dates)


def test_mixed_window_keeps_is_fitted_and_reports_last_genuine_vector() -> None:
    # Periods 0..1 ordered by A, the rest reversed: early rebalances still
    # see positive means (genuine fits), later ones clip to all-zero and
    # fall back. is_fitted stays True, both flags surface, and
    # fitted_weights_latest is the LAST genuine vector — never a fallback.
    def target(ordinal: int) -> list[float]:
        return SCORES_A if ordinal < 2 else REVERSED_TARGET

    dates, close, members, _, _ = _build_fixture(
        delay=DELAY, holding=HOLDING, target_for_period=target
    )
    result = _fitted(members, close, dates, method="ic_weighted")
    flags = [entry.flag for entry in result.weights_path]
    assert WARM_UP_IC_UNFITTED in flags
    assert IC_DEGENERATE_EQUAL_WEIGHT in flags
    assert None in flags
    assert result.is_fitted is True
    assert NO_FITTED_PERIODS not in result.warning_codes
    assert set(result.warning_codes) >= {WARM_UP_IC_UNFITTED, IC_DEGENERATE_EQUAL_WEIGHT}
    last_genuine = [entry for entry in result.weights_path if entry.flag is None][-1]
    assert result.fitted_weights_latest == last_genuine.weights
    assert result.fitted_weights_latest != EQUAL
    _assert_rebalances_never_emptied(result, dates)


# ---------------------------------------------------------------------------
# The clip itself: explicit finite check, never a bare (NaN-order-dependent) max
# ---------------------------------------------------------------------------


def test_normalize_clips_negative_and_non_finite_raw_weights_to_zero() -> None:
    weights, flag = _normalize_ic_weights({"A": 0.4, "B": -0.7}, ["A", "B"])
    assert flag is None
    assert weights == {"A": 1.0, "B": 0.0}

    weights, flag = _normalize_ic_weights({"A": math.nan, "B": 2.0}, ["A", "B"])
    assert flag is None
    assert weights == {"A": 0.0, "B": 1.0}

    weights, flag = _normalize_ic_weights({"A": math.inf, "B": 1.0}, ["A", "B"])
    assert flag is None
    assert weights == {"A": 0.0, "B": 1.0}

    weights, flag = _normalize_ic_weights({"A": -math.inf, "B": 0.25}, ["A", "B"])
    assert flag is None
    assert weights == {"A": 0.0, "B": 1.0}


def test_normalize_is_insensitive_to_nan_argument_order() -> None:
    # The review resolution's exact concern with bare max(): in Python,
    # max(nan, 0.0) is nan while max(0.0, nan) is 0.0 — an order-dependent
    # clip. The explicit finite check must give identical output for every
    # insertion order of the same raw mapping.
    forward = _normalize_ic_weights({"A": math.nan, "B": 0.5}, ["A", "B"])
    backward = _normalize_ic_weights({"B": 0.5, "A": math.nan}, ["A", "B"])
    assert forward == backward == ({"A": 0.0, "B": 1.0}, None)


def test_normalize_all_zero_vector_falls_back_flagged() -> None:
    for raw in (
        {"A": 0.0, "B": 0.0},
        {"A": -1.0, "B": -0.2},
        {"A": math.nan, "B": math.nan},
        {"A": math.nan, "B": -3.0},
    ):
        weights, flag = _normalize_ic_weights(raw, ["A", "B"])
        assert flag == IC_DEGENERATE_EQUAL_WEIGHT
        assert weights == {"A": 0.5, "B": 0.5}


def test_normalize_output_always_sums_to_one() -> None:
    weights, _ = _normalize_ic_weights({"A": 0.3, "B": 0.1, "C": 0.6}, ["A", "B", "C"])
    assert sum(weights.values()) == pytest.approx(1.0)
    assert weights["C"] == pytest.approx(0.6)
    assert weights["A"] == pytest.approx(0.3)

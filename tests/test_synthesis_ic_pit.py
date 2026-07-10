"""§13 anti-peek test: the fitted IC weights are point-in-time embargoed.

Design contract (docs/design/multi_factor_portfolio_backtest.md §4.4, §3
RB-2/RB-8, §12): at a rebalance date ``d`` the IC estimate may use ONLY grid
periods ``s`` with ``idx(s) + delay + holding <= idx(d)`` — periods whose
forward-return window has FULLY closed on or before ``d``, measured on the
SAME ``rebalance_indices`` grid the engine trades. A period closing exactly
AT ``d`` is eligible (the boundary is ``<=``); one closing at ``d + 1`` is
not. Warm-up rebalances (< ``ic_min_periods`` closed periods) run
equal-weight flagged ``WARM_UP_IC_UNFITTED``; a window with ZERO genuinely
fitted rebalances downgrades the run to ``is_fitted=False`` +
``NO_FITTED_PERIODS`` (RB-8) — never advertising a fit that did not happen.

The fixture is engineered so peeking is DETECTABLE, not just forbidden:
member A is the identity ordering, member B a pair-swap permutation with
exact Spearman ``RHO = 29/35`` to A, and the per-period returns are built
directly into the close chain (each instrument's close changes only at each
period's exit bar) so ``_with_period_return`` realizes EXACT rank-target
returns. Periods before the flip are return-ordered by A (IC_A = 1,
IC_B = RHO); periods at/after the flip are ordered by B (IC_A = RHO,
IC_B = 1). Because the two IC patterns are NOT proportional, any leaked
post-flip period changes the normalized weight vector — the pre-flip weight
is exactly ``1/(1+RHO)`` for A and any contamination moves it. Every
expectation below is recomputed from first principles (closed-form means
over the eligible sets), never by calling the code under test.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from quant_forge.backtesting.service import rebalance_indices
from quant_forge.synthesis.service import (
    IC_DEGENERATE_EQUAL_WEIGHT,
    NO_FITTED_PERIODS,
    WARM_UP_IC_UNFITTED,
    build_apriori_composite,
    build_fitted_composite,
    fitted_weights_by_rebalance,
)

N_INSTRUMENTS = 6
INSTRUMENTS = [f"STK{index:03d}" for index in range(N_INSTRUMENTS)]
# Member A: identity ordering. Member B: adjacent pair-swap permutation.
# spearman(A, B) = 1 - 6*sum(d^2)/(n*(n^2-1)) = 1 - 36/210 = 29/35 exactly.
SCORES_A = [0.0, 1.0, 2.0, 3.0, 4.0, 5.0]
SCORES_B = [1.0, 0.0, 3.0, 2.0, 5.0, 4.0]
RHO = 29.0 / 35.0
N_DATES = 30
FLIP_AFTER_PERIOD = 5  # periods 0..4 return-ordered by A; 5.. ordered by B
IC_MIN_PERIODS = 3


def _member_frame(dates: list[pd.Timestamp], scores: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"trade_date": date, "instrument": INSTRUMENTS[index], "score": scores[index]}
            for date in dates
            for index in range(N_INSTRUMENTS)
        ]
    )


def _build_fixture(*, delay: int, holding: int, target_for_period=None):
    """Synthetic calendar + members + close chain with EXACT period returns.

    Instrument ``i``'s close is a running product that steps ONLY at each
    closed grid period's exit bar by ``1 + 0.01 * (target[i] + 1)``, so the
    engine primitive ``_with_period_return`` over ``[entry, exit]`` realizes
    exactly one step: the period return is 0.01*(target[i]+1) and its
    cross-sectional ordering IS the target permutation (rank IC of +-1/RHO
    by construction). ``target_for_period`` maps period ordinal -> score
    vector whose ordering the period's returns follow (default: the
    sign-flip pattern around FLIP_AFTER_PERIOD).
    """

    if target_for_period is None:
        def target_for_period(period_ordinal: int) -> list[float]:
            return SCORES_A if period_ordinal < FLIP_AFTER_PERIOD else SCORES_B

    dates = list(pd.bdate_range("2026-01-05", periods=N_DATES))
    grid = list(range(0, N_DATES - delay - 1, holding))
    closed = [s for s in grid if s + delay + holding < N_DATES]
    step_by_exit_index = {}
    for period_ordinal, signal_index in enumerate(closed):
        target = target_for_period(period_ordinal)
        step_by_exit_index[signal_index + delay + holding] = {
            index: 1.0 + 0.01 * (target[index] + 1.0) for index in range(N_INSTRUMENTS)
        }
    close_rows: list[dict[str, object]] = []
    for index, instrument in enumerate(INSTRUMENTS):
        level = 100.0
        for date_index, date in enumerate(dates):
            if date_index in step_by_exit_index:
                level *= step_by_exit_index[date_index][index]
            close_rows.append({"trade_date": date, "instrument": instrument, "close": level})
    members = {
        "F_ALPHA": _member_frame(dates, SCORES_A),
        "F_BRAVO": _member_frame(dates, SCORES_B),
    }
    return dates, pd.DataFrame(close_rows), members, grid, closed


def _directed_matrix(members, dates):
    """Standardized (rank), direction-applied matrix — the fit's real input."""

    from quant_forge.synthesis.service import (
        apply_directions,
        build_score_matrix,
        standardize_matrix,
    )

    matrix = build_score_matrix(members)
    standardized = standardize_matrix(matrix, standardization="rank")
    return apply_directions(standardized.matrix, {"F_ALPHA": 1, "F_BRAVO": 1})


def _expected_weight_alpha(pre_count: int, post_count: int) -> float:
    """Closed-form normalized weight for A given eligible period mix.

    mean IC_A = (pre*1 + post*RHO)/n, mean IC_B = (pre*RHO + post*1)/n; both
    positive, so the normalized weight of A is (pre + post*RHO) /
    (pre + post*RHO + pre*RHO + post).
    """

    raw_alpha = pre_count + post_count * RHO
    raw_bravo = pre_count * RHO + post_count
    return raw_alpha / (raw_alpha + raw_bravo)


# ---------------------------------------------------------------------------
# The embargo boundary, both sides of it, on the shared grid
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("delay", "holding"),
    [
        # delay=2, holding=2: every period's forward window closes EXACTLY on
        # a later rebalance index (s + 4 == d) — the equal-boundary case.
        (2, 2),
        # delay=1, holding=2: every period closes at d + 1 relative to the
        # nearest earlier rebalance — the strictly-after exclusion case.
        (1, 2),
    ],
)
def test_weights_use_only_periods_closed_on_or_before_each_rebalance(
    delay: int, holding: int
) -> None:
    dates, close, members, grid, closed = _build_fixture(delay=delay, holding=holding)
    directed = _directed_matrix(members, dates)
    entries = fitted_weights_by_rebalance(
        directed,
        close=close,
        dates=dates,
        delay=delay,
        holding=holding,
        method="ic_weighted",
        ic_min_periods=IC_MIN_PERIODS,
    )

    # RB-5: the fit covers exactly the shared engine grid — every index the
    # helper yields, in order, never a re-derived schedule.
    assert [entry.signal_index for entry in entries] == rebalance_indices(
        dates, delay=delay, holding=holding, start_signal_index=0
    )

    ordinal_of = {signal_index: ordinal for ordinal, signal_index in enumerate(closed)}
    saw_equal_boundary = False
    saw_first_mixed = False
    previous_pure_weight: float | None = None
    for entry in entries:
        # First principles: the §4.4 embargo inequality, recomputed here.
        eligible = [s for s in grid if s + delay + holding <= entry.signal_index]
        assert entry.eligible_period_count == len(eligible)
        assert all(s + delay + holding <= entry.signal_index for s in eligible)
        if delay % holding == 0 and eligible:
            # Equal-boundary case: the newest eligible period closes EXACTLY
            # at this rebalance index (<= includes equality).
            assert max(eligible) + delay + holding == entry.signal_index
            saw_equal_boundary = True

        if len(eligible) < IC_MIN_PERIODS:
            assert entry.flag == WARM_UP_IC_UNFITTED
            assert entry.weights == {"F_ALPHA": 0.5, "F_BRAVO": 0.5}
            continue
        assert entry.flag is None
        pre_count = sum(1 for s in eligible if ordinal_of[s] < FLIP_AFTER_PERIOD)
        post_count = len(eligible) - pre_count
        expected_alpha = _expected_weight_alpha(pre_count, post_count)
        assert entry.weights["F_ALPHA"] == pytest.approx(expected_alpha, abs=1e-12)
        assert entry.weights["F_BRAVO"] == pytest.approx(1.0 - expected_alpha, abs=1e-12)
        if post_count == 0:
            # Anti-peek: post-flip periods EXIST in the data (their returns
            # are realized later in the close chain), yet a rebalance whose
            # eligible set is pure pre-flip carries the exact pre-flip vector.
            assert entry.weights["F_ALPHA"] == pytest.approx(1.0 / (1.0 + RHO), abs=1e-12)
            previous_pure_weight = entry.weights["F_ALPHA"]
        elif not saw_first_mixed:
            saw_first_mixed = True
            # The first post-flip period entered exactly when its window
            # closed — one rebalance earlier it was still excluded (it closed
            # at d (equal case) resp. d+1 (strict case) relative to the two
            # adjacent rebalances). The weight moves, so a leak either way
            # would have been visible.
            assert previous_pure_weight is not None
            assert entry.weights["F_ALPHA"] != pytest.approx(previous_pure_weight, abs=1e-9)
            assert entry.weights["F_ALPHA"] == pytest.approx(
                _expected_weight_alpha(FLIP_AFTER_PERIOD, len(eligible) - FLIP_AFTER_PERIOD),
                abs=1e-12,
            )
    assert saw_first_mixed, "fixture must exercise the first mixed rebalance"
    if delay % holding == 0:
        assert saw_equal_boundary, "fixture must exercise the equal-boundary case"


def test_period_closing_at_d_plus_one_is_not_eligible() -> None:
    """The strictly-after side of the boundary, pinned explicitly.

    With delay=1, holding=2 the first post-flip period (ordinal 5, signal
    index 10) closes at index 13 — exactly ONE bar after the rebalance at
    index 12. That rebalance must still carry the pure pre-flip vector; the
    next one (index 14, closure 13 <= 14) must include it.
    """

    delay, holding = 1, 2
    dates, close, members, grid, closed = _build_fixture(delay=delay, holding=holding)
    entries = fitted_weights_by_rebalance(
        _directed_matrix(members, dates),
        close=close,
        dates=dates,
        delay=delay,
        holding=holding,
        method="ic_weighted",
        ic_min_periods=IC_MIN_PERIODS,
    )
    by_index = {entry.signal_index: entry for entry in entries}
    flip_signal = closed[FLIP_AFTER_PERIOD]
    assert flip_signal == 10
    closure = flip_signal + delay + holding
    assert closure == 13  # closes one bar AFTER the rebalance at index 12

    at_12 = by_index[12]
    assert at_12.flag is None
    assert at_12.weights["F_ALPHA"] == pytest.approx(1.0 / (1.0 + RHO), abs=1e-12)

    at_14 = by_index[14]
    assert at_14.flag is None
    assert at_14.weights["F_ALPHA"] == pytest.approx(
        _expected_weight_alpha(FLIP_AFTER_PERIOD, at_14.eligible_period_count - FLIP_AFTER_PERIOD),
        abs=1e-12,
    )
    assert at_14.weights["F_ALPHA"] < at_12.weights["F_ALPHA"]


# ---------------------------------------------------------------------------
# Fold-in before materialization + step-function point-in-time validity
# ---------------------------------------------------------------------------


def test_composite_carries_already_combined_values_per_signal_date() -> None:
    delay, holding = 2, 2
    dates, close, members, grid, closed = _build_fixture(delay=delay, holding=holding)
    result = build_fitted_composite(
        members,
        directions={"F_ALPHA": 1, "F_BRAVO": 1},
        standardization="rank",
        method="ic_weighted",
        close=close,
        dates=dates,
        delay=delay,
        holding=holding,
        ic_min_periods=IC_MIN_PERIODS,
    )
    assert result.is_fitted is True
    assert WARM_UP_IC_UNFITTED in result.warning_codes
    assert NO_FITTED_PERIODS not in result.warning_codes
    assert IC_DEGENERATE_EQUAL_WEIGHT not in result.warning_codes
    assert result.method == "ic_weighted"
    assert result.standardization == "rank"
    # Grid-complete path: one weight vector per shared-grid rebalance.
    assert [entry.signal_index for entry in result.weights_path] == grid
    assert result.warmup_period_count == sum(
        1 for entry in result.weights_path if entry.flag == WARM_UP_IC_UNFITTED
    )
    assert result.fitted_period_count == sum(
        1 for entry in result.weights_path if entry.flag is None
    )
    assert result.fitted_period_fraction == pytest.approx(
        result.fitted_period_count / len(result.weights_path)
    )
    last_fitted = [entry for entry in result.weights_path if entry.flag is None][-1]
    assert result.fitted_weights_latest == last_fitted.weights

    # The stored frame is ALREADY combined: at each grid signal date it must
    # equal sum_f w_{f,d} * t_{f,d,i} for that date's fitted vector, and a
    # non-grid date adopts the most recent earlier vector (step function —
    # weights from strictly older closed periods stay PIT-valid later).
    standardized_alpha = 2.0 * pd.Series(SCORES_A).rank(pct=True, method="first") - 1.0
    standardized_bravo = 2.0 * pd.Series(SCORES_B).rank(pct=True, method="first") - 1.0
    frame = result.composite
    by_index = {entry.signal_index: entry for entry in result.weights_path}
    for signal_index in (8, 14):
        weights = by_index[signal_index].weights
        expected = (
            weights["F_ALPHA"] * standardized_alpha + weights["F_BRAVO"] * standardized_bravo
        ).to_numpy()
        observed = (
            frame[frame["trade_date"] == dates[signal_index]]
            .sort_values("instrument")["score"]
            .to_numpy()
        )
        assert np.allclose(observed, expected)
    # dates[15] sits between grid rebalances 14 and 16 -> uses the vector
    # fitted at 14 (never the one that will only exist at 16).
    weights_at_14 = by_index[14].weights
    expected_15 = (
        weights_at_14["F_ALPHA"] * standardized_alpha
        + weights_at_14["F_BRAVO"] * standardized_bravo
    ).to_numpy()
    observed_15 = (
        frame[frame["trade_date"] == dates[15]].sort_values("instrument")["score"].to_numpy()
    )
    assert np.allclose(observed_15, expected_15)


# ---------------------------------------------------------------------------
# Warm-up flagging + the RB-8 all-warmup downgrade
# ---------------------------------------------------------------------------


def test_all_warmup_window_downgrades_to_equal_weight_unfitted() -> None:
    delay, holding = 2, 2
    dates, close, members, grid, _ = _build_fixture(delay=delay, holding=holding)
    result = build_fitted_composite(
        members,
        directions={"F_ALPHA": 1, "F_BRAVO": 1},
        standardization="rank",
        method="ic_weighted",
        close=close,
        dates=dates,
        delay=delay,
        holding=holding,
        # More than the window can ever close -> every rebalance is warm-up.
        ic_min_periods=60,
    )
    assert result.is_fitted is False
    assert NO_FITTED_PERIODS in result.warning_codes
    assert WARM_UP_IC_UNFITTED in result.warning_codes
    assert result.fitted_weights_latest is None
    assert result.fitted_period_count == 0
    assert result.fitted_period_fraction == 0.0
    assert result.warmup_period_count == len(grid)
    assert all(entry.flag == WARM_UP_IC_UNFITTED for entry in result.weights_path)
    assert all(
        entry.weights == {"F_ALPHA": 0.5, "F_BRAVO": 0.5} for entry in result.weights_path
    )

    # The downgraded run IS the equal-weight run: its composite equals the
    # a-priori equal_weight composite up to the declared raw-1.0 vs 1/N
    # weight convention (a positive scale that cannot change any ordering,
    # book, or metric downstream).
    apriori = build_apriori_composite(
        members,
        directions={"F_ALPHA": 1, "F_BRAVO": 1},
        standardization="rank",
        method="equal_weight",
    )
    merged = result.composite.merge(
        apriori.composite,
        on=["trade_date", "instrument"],
        suffixes=("_fitted", "_apriori"),
    )
    assert len(merged) == len(result.composite) == len(apriori.composite)
    both = merged.dropna(subset=["score_fitted", "score_apriori"])
    assert np.allclose(
        both["score_fitted"].to_numpy() * 2.0, both["score_apriori"].to_numpy()
    )
    assert (
        merged["score_fitted"].isna() == merged["score_apriori"].isna()
    ).all()

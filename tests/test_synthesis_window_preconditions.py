"""RB-2 window preconditions: realized period count N and WINDOW_TOO_SHORT.

Design contract (docs/design/multi_factor_portfolio_backtest.md §3 RB-2/RB-8):
before materializing, the synthesis layer computes the realized
non-overlapping period count ``N = max(0, floor((len(in_window_dates) - 1 -
delay - holding) / holding) + 1)`` — the count of **D3-complete** grid periods
the engine actually realizes — and rejects ``N < 2`` with the typed
``WindowTooShortError`` (code ``WINDOW_TOO_SHORT``) that the workflow maps to a
client-error response — the engine's own tiny ``max(2, holding + delay + 1)``
gate is not the synthesis precondition.

Audit S1 fix: the formula previously read ``floor((dates - delay - 1) /
holding) + 1``, which counted the D3-excluded final-partial signal (and, on
exact divisibility, exceeded even the grid signal count by 1). Every case
below is cross-checked against the real ``rebalance_indices`` grid truth so the
count provably equals the engine's realized complete-period ledger.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
from quant_forge.backtesting.service import rebalance_indices, run_factor_backtest
from quant_forge.core.contracts import FactorDefinition
from quant_forge.factor_library.repository import FactorRepository
from quant_forge.synthesis.service import (
    WINDOW_TOO_SHORT,
    SynthesisPreconditionError,
    WindowTooShortError,
    count_non_overlapping_periods,
    require_backtest_window,
)


def _realized_complete_periods(date_count: int, *, delay: int, holding: int) -> int:
    """Ground truth: complete D3 periods the engine actually trades.

    Mirrors ``run_factor_backtest``'s loop over the shared grid: a signal index
    ``s`` yields a *complete* period iff its scheduled exit ``s + delay +
    holding`` lands strictly inside the ``date_count`` panel (owner decision D3
    drops the final partial period).
    """

    grid = rebalance_indices(
        list(range(date_count)), delay=delay, holding=holding, start_signal_index=0
    )
    return sum(1 for s in grid if s + delay + holding < date_count)


@pytest.mark.parametrize(
    ("date_count", "delay", "holding", "expected"),
    [
        # Exact-divisibility boundary: holding | (dates - delay - 1). The old
        # formula returned 3 here — exceeding even the 2-signal grid by 1.
        (10, 1, 4, 2),
        (12, 1, 5, 2),
        # D3-excluded final partial: the last grid signal's window falls beyond
        # the panel, so it is dropped; the old formula counted it.
        (10, 1, 5, 1),
        (7, 1, 5, 1),
        (11, 1, 5, 1),
        (130, 1, 20, 6),
        (592, 2, 10, 58),
        # Golden fixture window (test_synthesis_equal_weight_golden realizes 7
        # complete periods; the old formula reported 8).
        (40, 1, 5, 7),
        # Degenerate / empty windows floor at 0.
        (6, 1, 5, 0),
        (3, 1, 1, 1),
        (2, 1, 1, 0),
        (0, 1, 5, 0),
    ],
)
def test_period_count_equals_realized_complete_periods(
    date_count: int, delay: int, holding: int, expected: int
) -> None:
    result = count_non_overlapping_periods(date_count, delay=delay, holding=holding)
    assert result == expected
    # Ground truth: the count provably equals the engine's realized complete
    # (D3) period ledger derived from the shared rebalance grid (audit S1).
    assert result == _realized_complete_periods(date_count, delay=delay, holding=holding)
    # And it is the corrected closed form, never the old over-counting one.
    assert result == max(0, (date_count - 1 - delay - holding) // holding + 1)


def test_require_backtest_window_returns_n_on_success() -> None:
    # 10 dates / delay=1 / holding=4 realize exactly 2 complete periods.
    assert require_backtest_window(10, delay=1, holding=4) == 2
    # Boundary: exactly 2 realized periods is enough (4 dates / delay=1 /
    # holding=1 → signals at 0 and 1 both close inside the window).
    assert require_backtest_window(4, delay=1, holding=1) == 2
    assert require_backtest_window(12, delay=1, holding=5) == 2


@pytest.mark.parametrize(
    ("date_count", "delay", "holding"),
    [
        (6, 1, 5),
        (2, 1, 1),
        (0, 1, 5),
        (5, 3, 20),
        # Audit S1: windows that realize a single complete period. The old
        # formula reported N=2 and wrongly admitted them; the corrected count
        # rejects them.
        (7, 1, 5),
        (11, 1, 5),
        (10, 1, 5),
        (3, 1, 1),
    ],
)
def test_too_short_windows_raise_the_typed_error(
    date_count: int, delay: int, holding: int
) -> None:
    with pytest.raises(WindowTooShortError) as excinfo:
        require_backtest_window(date_count, delay=delay, holding=holding)
    error = excinfo.value
    # Typed for the workflow's client-error mapping; still a ValueError so
    # existing invalid-request handling keeps working.
    assert isinstance(error, ValueError)
    assert isinstance(error, SynthesisPreconditionError)
    assert error.code == WINDOW_TOO_SHORT
    message = str(error)
    assert WINDOW_TOO_SHORT in message
    assert f"delay={delay}" in message
    assert f"holding={holding}" in message


@pytest.mark.parametrize(
    ("date_count", "delay", "holding"),
    [(-1, 1, 5), (10, 0, 5), (10, 1, 0), (True, 1, 5), (10, True, 5), (10, 1, True)],
)
def test_invalid_inputs_raise_plain_value_errors(
    date_count: object, delay: object, holding: object
) -> None:
    with pytest.raises(ValueError) as excinfo:
        count_non_overlapping_periods(date_count, delay=delay, holding=holding)
    # Input-shape problems are plain ValueError, not the typed
    # window-precondition outcome.
    assert not isinstance(excinfo.value, SynthesisPreconditionError)


def test_period_count_matches_engine_realized_ledger_end_to_end(tmp_path: Path) -> None:
    # Audit S1's central claim was that the pre-scan N over-counts the engine's
    # realized ledger. Drive the real engine on a fully-covered window and
    # assert the corrected pre-scan count equals `completed_periods` exactly —
    # and is strictly below the old over-counting formula.
    date_count, delay, holding = 60, 1, 5
    data_root = tmp_path / "data"
    factor_root = tmp_path / "factor_root"
    data_root.mkdir(parents=True)
    dates = list(pd.bdate_range("2026-01-05", periods=date_count))
    rows = [
        {
            "trade_date": trade_date,
            "instrument": f"STK{index:02d}",
            "close": 10.0 + index + 0.03 * day * (index + 1),
            "market_cap": 1_000_000_000.0 * (index + 1),
            "is_st": False,
        }
        for index in range(6)
        for day, trade_date in enumerate(dates)
    ]
    pd.DataFrame(rows).to_parquet(data_root / "panel.parquet", index=False)
    FactorRepository(factor_root).save(
        FactorDefinition(
            factor_id="FTR_WINDOW_TIE",
            name="window_tie",
            formula="-rank(market_cap)",
            horizon_days=holding,
            universe_filters=("is_st == false",),
        )
    )

    result = run_factor_backtest(
        "FTR_WINDOW_TIE",
        factor_root=factor_root,
        data_root=data_root,
        artifact_root=tmp_path / "artifacts",
        holding_days=holding,
    )

    n = count_non_overlapping_periods(date_count, delay=delay, holding=holding)
    assert result.skipped_rebalances == 0  # full coverage: N is exact, not an upper bound
    assert n == result.completed_periods == 11
    # The old spec formula would have advertised one phantom period.
    assert (date_count - delay - 1) // holding + 1 == n + 1

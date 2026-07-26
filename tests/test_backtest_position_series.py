"""Gold-vector coverage for the position-series backtest entry (upstream batch 3).

Every vector below is a fixed, tiny panel whose expected NAV, period returns,
turnover and costs are written out by hand in the test body, so a reader can
re-derive each frozen number with a calculator and a red test names the exact
quantity that moved.

Vectors
-------
* ``A`` — one instrument, ``+1 -> +1 -> -1 -> 0`` three-state switching over
  exact price relatives ``+0.20 / +0.25 / -0.20 / -0.20 / +0.25``, next-bar
  (``t+1``) close execution, BOTH cost channels (commission + slippage) and a
  NON-ZERO ``short_borrow_bps_annual`` accruing only over the short bar.
* ``B`` — ``t+1`` OPEN execution against a panel whose closes are deliberately
  flat, so a close-priced result would be identically zero and the vector
  proves the open column drove the math; also pins the carry-forward contract.
* ``C`` — two instruments with fractional (``±0.5``) weights, both cost
  channels and a short-notional-scaled borrow accrual.
* ``D`` — a 127-bar ramp: the only vector long enough to clear the engine's
  half-year annualization gate, so the reportable/suppressed split is covered
  in both directions.

Contracts pinned alongside them: no lookahead (an explicit shift, proven by
perturbation), fail-closed input handling (an absent ``open`` column never
falls back to ``close``), the kernel tri-state metric vocabulary, and numeric
IDENTITY with the pinned engine's own annualization/Sharpe formulas at the
trading-day frequency, so the two entries can never hold two different
versions of the same math.
"""

from __future__ import annotations

import dataclasses
import math

import numpy as np
import pandas as pd
import pytest

from quant_forge.backtesting import (
    PositionSeriesInputError,
    run_position_series_backtest,
)
from quant_forge.backtesting.position_series import (
    CALENDAR_TOO_SHORT,
    CAPITAL_EXHAUSTED,
    DUPLICATE_POSITION_ROWS,
    DUPLICATE_PRICE_ROWS,
    EMPTY_POSITION_SERIES,
    EMPTY_PRICE_PANEL,
    EXECUTION_PRICE_COLUMN_UNAVAILABLE,
    INVALID_COST_MODEL,
    INVALID_EXECUTION_DELAY,
    INVALID_EXECUTION_PRICE,
    INVALID_PERIODS_PER_YEAR,
    INVALID_TRADE_DATE,
    MISSING_POSITION_COLUMNS,
    MISSING_PRICE_COLUMNS,
    NON_FINITE_RETURN_SERIES,
    NON_FINITE_TARGET_WEIGHT,
    POSITION_SERIES_ERROR_CODES,
    SAME_PERIOD_EXECUTION,
    SIGNAL_DATE_OUTSIDE_CALENDAR,
    TERMINAL_BAR_SETTLEMENT,
    TRADING_PERIODS_PER_YEAR,
    UNMARKABLE_HELD_POSITION,
    _COST_MODEL_FIELDS,
    _annualized_return_periodic,
    _compounded,
    _minimum_annualization_periods,
    _sharpe_periodic,
)
from quant_forge.backtesting.service import (
    INSUFFICIENT_ANNUALIZATION_HISTORY,
    INSUFFICIENT_SHARPE_OBSERVATIONS,
    MIN_ANNUALIZATION_EXPOSURE_DAYS,
    _annualized_return,
    _long_short_sharpe,
)
from quant_forge.core.contracts import TransactionCostModel

REL = 1e-12
DATES_6 = pd.bdate_range("2026-01-05", periods=6)
DATES_5 = pd.bdate_range("2026-01-05", periods=5)
DATES_4 = pd.bdate_range("2026-01-05", periods=4)

# 2520 bps a year over a 252-bar year is exactly 10 bp -- 0.001 of notional --
# per bar, so a borrow accrual stays hand-checkable. The name states the
# per-PERIOD rate the arithmetic below actually uses (2520 / 252 = 10 bp); an
# earlier spelling claimed 1 bp, which no expected value in this file matches.
BORROW_BPS_TEN_BP_PER_PERIOD = 2520.0


def _prices(dates: pd.DatetimeIndex, **columns: dict[str, list[float]]) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for column_name, series_by_instrument in columns.items():
        for instrument, values in series_by_instrument.items():
            for trade_date, value in zip(dates, values, strict=True):
                rows.append(
                    {"trade_date": trade_date, "instrument": instrument, column_name: value}
                )
    frame = pd.DataFrame(rows)
    return frame.groupby(["trade_date", "instrument"], as_index=False).first()


def _positions(pairs: list[tuple[pd.Timestamp, str, float]]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"trade_date": trade_date, "instrument": instrument, "target_weight": weight}
            for trade_date, instrument, weight in pairs
        ]
    )


# ---------------------------------------------------------------------------
# Vector A: three-state switching, t+1 close execution, both cost channels,
# non-zero short borrow.
# ---------------------------------------------------------------------------

# closes chosen so every price relative is an exact decimal:
#   +0.20, +0.25, -0.20, -0.20, +0.25
A_CLOSES = [100.0, 120.0, 150.0, 120.0, 96.0, 120.0]
A_COSTS = TransactionCostModel(
    commission_bps=10.0, slippage_bps=5.0, short_borrow_bps_annual=BORROW_BPS_TEN_BP_PER_PERIOD
)


def _vector_a_inputs() -> tuple[pd.DataFrame, pd.DataFrame]:
    prices = _prices(DATES_6, close={"CU": A_CLOSES})
    positions = _positions(
        [
            (DATES_6[0], "CU", 1.0),   # executes on bar 1 -> long
            (DATES_6[1], "CU", 1.0),   # executes on bar 2 -> still long
            (DATES_6[2], "CU", -1.0),  # executes on bar 3 -> short
            (DATES_6[3], "CU", 0.0),   # executes on bar 4 -> flat
            (DATES_6[4], "CU", 0.0),   # executes on bar 5 -> flat (no interval follows)
        ]
    )
    return positions, prices


def test_vector_a_three_state_switching_with_both_cost_channels_and_short_borrow() -> None:
    positions, prices = _vector_a_inputs()
    result = run_position_series_backtest(positions, prices, transaction_costs=A_COSTS)

    assert result.execution_price == "close"
    assert result.execution_delay_periods == 1
    assert result.instruments == ("CU",)
    assert result.periods == 5

    # Held book per period: flat until the first signal executes, then
    # long / long / short / flat.
    assert [row.net_exposure for row in result.period_rows] == [0.0, 1.0, 1.0, -1.0, 0.0]
    # Every held book comes from a STRICTLY earlier bar (t+1 execution).
    for row in result.period_rows:
        if row.signal_date is not None:
            assert row.signal_date < row.entry_date
    assert result.period_rows[0].signal_date is None

    # gross period returns = held weight x execution-price relative
    expected_gross = [0.0, 0.25, -0.20, 0.20, 0.0]
    assert [row.gross_period_return for row in result.period_rows] == pytest.approx(expected_gross, rel=REL)

    # traded notional = sum |dw|, starting from a flat book
    expected_traded = [0.0, 1.0, 0.0, 2.0, 1.0]
    assert [row.traded_notional for row in result.period_rows] == pytest.approx(expected_traded, rel=REL)
    assert [row.turnover for row in result.period_rows] == pytest.approx(
        [value / 2.0 for value in expected_traded], rel=REL
    )
    assert result.traded_notional_total == pytest.approx(4.0, rel=REL)
    assert result.turnover_total == pytest.approx(2.0, rel=REL)
    assert result.turnover_mean == pytest.approx(2.0 / 5.0, rel=REL)

    # trade cost = traded notional x 15bps; borrow accrues ONLY on the short bar
    trade_rate = (10.0 + 5.0) / 10_000.0
    expected_trade_cost = [value * trade_rate for value in expected_traded]
    assert [row.trade_cost for row in result.period_rows] == pytest.approx(expected_trade_cost, rel=REL)
    assert [row.borrow_cost for row in result.period_rows] == pytest.approx(
        [0.0, 0.0, 0.0, 0.001, 0.0], rel=REL
    )
    assert result.trade_cost_total == pytest.approx(0.0060, rel=REL)
    assert result.borrow_cost_total == pytest.approx(0.0010, rel=REL)
    assert result.transaction_cost_total == pytest.approx(0.0070, rel=REL)

    expected_net = [0.0, 0.2485, -0.20, 0.1960, -0.0015]
    assert [row.net_period_return for row in result.period_rows] == pytest.approx(expected_net, rel=REL)

    # NAV compounds; the series carries the 1.0 base at the first execution bar.
    assert len(result.nav_series) == result.periods + 1
    assert [row["gross_nav"] for row in result.nav_series] == pytest.approx(
        [1.0, 1.0, 1.25, 1.00, 1.20, 1.20], rel=REL
    )
    assert [row["net_nav"] for row in result.nav_series] == pytest.approx(
        [1.0, 1.0, 1.2485, 0.9988, 1.19456480, 1.1927729528], rel=REL
    )
    assert result.gross_cumulative_return == pytest.approx(0.20, rel=REL)
    assert result.net_cumulative_return == pytest.approx(0.1927729528, rel=REL)
    # Costs strictly reduce the terminal book; the reconciliation block agrees
    # with the NAV series it summarizes.
    assert result.net_cumulative_return < result.gross_cumulative_return
    assert result.cost_reconciliation["gross_terminal_equity"] == pytest.approx(1.20, rel=REL)
    assert result.cost_reconciliation["net_terminal_equity"] == pytest.approx(1.1927729528, rel=REL)
    assert result.cost_reconciliation["period_transaction_cost_sum"] == pytest.approx(0.0070, rel=REL)

    # peak 1.25 -> trough 1.00
    assert result.metrics["max_drawdown"].value == pytest.approx(-0.20, rel=REL)
    # peak 1.2485 -> trough 0.9988 is the same -20%
    assert result.metrics["net_max_drawdown"].value == pytest.approx(-0.20, rel=REL)

    # Sharpe over the five gross periods: mean 0.05, sample variance 0.0325.
    assert result.metrics["sharpe"].value == pytest.approx(
        0.05 / math.sqrt(0.0325) * math.sqrt(252.0), rel=REL
    )

    assert result.diagnostics["long_periods"] == 2
    assert result.diagnostics["short_periods"] == 1
    assert result.diagnostics["flat_periods"] == 2
    assert result.diagnostics["carried_forward_periods"] == 0
    assert result.diagnostics["unexecuted_terminal_weights"] == {}


def test_vector_a_short_borrow_scales_with_held_short_notional_only() -> None:
    """The borrow leg is charged on the SHORT notional actually held, and on
    nothing else: rerunning with borrow disabled moves exactly the one short
    period's cost, by exactly the one bar's accrual."""

    positions, prices = _vector_a_inputs()
    with_borrow = run_position_series_backtest(positions, prices, transaction_costs=A_COSTS)
    without_borrow = run_position_series_backtest(
        positions,
        prices,
        transaction_costs=TransactionCostModel(commission_bps=10.0, slippage_bps=5.0),
    )
    assert without_borrow.borrow_cost_total == 0.0
    assert with_borrow.trade_cost_total == pytest.approx(without_borrow.trade_cost_total, rel=REL)
    differences = [
        round(a.net_period_return - b.net_period_return, 12)
        for a, b in zip(with_borrow.period_rows, without_borrow.period_rows, strict=True)
    ]
    assert differences == [0.0, 0.0, 0.0, -0.001, 0.0]
    # Halving the short position halves the accrual (linear in short notional).
    half_short = _positions(
        [
            (DATES_6[0], "CU", 1.0),
            (DATES_6[1], "CU", 1.0),
            (DATES_6[2], "CU", -0.5),
            (DATES_6[3], "CU", 0.0),
            (DATES_6[4], "CU", 0.0),
        ]
    )
    halved = run_position_series_backtest(half_short, prices, transaction_costs=A_COSTS)
    assert halved.borrow_cost_total == pytest.approx(0.0005, rel=REL)


def test_vector_a_is_deterministic() -> None:
    positions, prices = _vector_a_inputs()
    first = run_position_series_backtest(positions, prices, transaction_costs=A_COSTS)
    second = run_position_series_backtest(positions, prices, transaction_costs=A_COSTS)
    assert first == second


# ---------------------------------------------------------------------------
# Vector B: t+1 OPEN execution (and the carry-forward contract).
# ---------------------------------------------------------------------------

B_OPENS = [100.0, 125.0, 100.0, 80.0, 100.0]
# Deliberately flat: a close-priced run of this vector is identically zero.
B_CLOSES = [100.0, 100.0, 100.0, 100.0, 100.0]


def _vector_b_inputs() -> tuple[pd.DataFrame, pd.DataFrame]:
    prices = _prices(DATES_5, close={"CU": B_CLOSES}, open={"CU": B_OPENS})
    positions = _positions([(DATES_5[0], "CU", 1.0)])
    return positions, prices


def test_vector_b_open_execution_prices_the_open_to_open_relatives() -> None:
    positions, prices = _vector_b_inputs()
    result = run_position_series_backtest(positions, prices, execution_price="open")

    assert result.execution_price == "open"
    assert result.periods == 4
    # open relatives: +0.25 (unheld), -0.20, -0.20, +0.25
    assert [row.gross_period_return for row in result.period_rows] == pytest.approx(
        [0.0, -0.20, -0.20, 0.25], rel=REL
    )
    assert [row["gross_nav"] for row in result.nav_series] == pytest.approx(
        [1.0, 1.0, 0.80, 0.64, 0.80], rel=REL
    )
    assert result.gross_cumulative_return == pytest.approx(-0.20, rel=REL)
    # No cost model configured -> net is the gross series exactly.
    assert result.net_cumulative_return == pytest.approx(result.gross_cumulative_return, rel=REL)
    assert result.transaction_cost_total == 0.0
    assert result.metrics["max_drawdown"].value == pytest.approx(0.64 / 1.0 - 1.0, rel=REL)

    # The SAME panel priced on close is identically flat, so the vector proves
    # the open column (not the close column) drove the numbers above.
    on_close = run_position_series_backtest(positions, prices, execution_price="close")
    assert [row.gross_period_return for row in on_close.period_rows] == [0.0, 0.0, 0.0, 0.0]


def test_vector_b_carries_a_stale_target_book_forward_and_reports_it() -> None:
    positions, prices = _vector_b_inputs()
    result = run_position_series_backtest(positions, prices, execution_price="open")
    # One signal bar, four periods: bars 2 and 3 hold a book older than the
    # delay alone implies, and each says so.
    assert [row.carried_forward for row in result.period_rows] == [False, False, True, True]
    assert result.diagnostics["carried_forward_periods"] == 2
    assert result.diagnostics["signal_bar_count"] == 1
    held_sources = {row.signal_date for row in result.period_rows if row.signal_date is not None}
    assert held_sources == {DATES_5[0].date().isoformat()}
    assert any("carried_forward=True" in warning for warning in result.warnings)


def test_open_execution_against_a_panel_without_an_open_column_fails_closed() -> None:
    positions = _positions([(DATES_5[0], "CU", 1.0)])
    prices = _prices(DATES_5, close={"CU": B_CLOSES})
    with pytest.raises(PositionSeriesInputError) as excinfo:
        run_position_series_backtest(positions, prices, execution_price="open")
    assert excinfo.value.code == EXECUTION_PRICE_COLUMN_UNAVAILABLE
    assert "no fallback price column is substituted" in str(excinfo.value)
    assert excinfo.value.details["available_columns"] == ["trade_date", "instrument", "close"]


# ---------------------------------------------------------------------------
# Vector C: two instruments, fractional weights.
# ---------------------------------------------------------------------------

C_CLOSES_A = [100.0, 110.0, 99.0, 108.9]  # relatives +0.10, -0.10, +0.10
C_CLOSES_B = [200.0, 180.0, 198.0, 178.2]  # relatives -0.10, +0.10, -0.10
C_COSTS = TransactionCostModel(
    commission_bps=8.0, slippage_bps=2.0, short_borrow_bps_annual=BORROW_BPS_TEN_BP_PER_PERIOD
)


def test_vector_c_multi_instrument_fractional_weights() -> None:
    prices = _prices(DATES_4, close={"AAA": C_CLOSES_A, "BBB": C_CLOSES_B})
    positions = _positions(
        [
            (DATES_4[0], "AAA", 0.5),
            (DATES_4[0], "BBB", -0.5),
            (DATES_4[1], "AAA", 0.0),
            (DATES_4[1], "BBB", -1.0),
            (DATES_4[2], "AAA", 1.0),
            (DATES_4[2], "BBB", 0.0),
        ]
    )
    result = run_position_series_backtest(positions, prices, transaction_costs=C_COSTS)

    assert result.instruments == ("AAA", "BBB")
    # Three holding intervals PLUS the terminal-bar settlement: bar 2's signal
    # (AAA +1.0 / BBB flat) executes on bar 3, which exists on the calendar, so
    # that book is established and charged there.
    assert result.diagnostics["holding_periods"] == 3
    assert result.periods == 4
    assert [row.is_terminal_settlement for row in result.period_rows] == [False, False, False, True]
    settlement = result.period_rows[3]
    assert settlement.entry_date == settlement.exit_date == DATES_4[3].date().isoformat()
    assert settlement.weights == {"AAA": 1.0}
    assert settlement.price_relatives == {}

    # P1: 0.5*(-0.10) + (-0.5)*(+0.10) = -0.10 ; P2: (-1.0)*(-0.10) = +0.10 ;
    # the settlement has no interval, so its gross return is a true zero.
    assert [row.gross_period_return for row in result.period_rows] == pytest.approx(
        [0.0, -0.10, 0.10, 0.0], rel=1e-9
    )
    # Terminal traded notional: |1.0 - 0.0| (AAA opened) + |0.0 - (-1.0)| (BBB
    # covered) = 2.0, so 2.0 + 2.0 = 4.0 over the run.
    assert [row.traded_notional for row in result.period_rows] == pytest.approx(
        [0.0, 1.0, 1.0, 2.0], rel=REL
    )
    assert result.traded_notional_total == pytest.approx(4.0, rel=REL)
    assert [row.short_exposure for row in result.period_rows] == pytest.approx(
        [0.0, 0.5, 1.0, 0.0], rel=REL
    )
    # borrow accrual is linear in the short notional actually held, and the
    # settlement holds nothing over any interval, so it accrues exactly nothing
    assert [row.borrow_cost for row in result.period_rows] == pytest.approx(
        [0.0, 0.0005, 0.0010, 0.0], rel=REL
    )
    # 10 bps on traded notional: 0.0010, 0.0010, and 2.0 * 0.0010 = 0.0020
    assert [row.trade_cost for row in result.period_rows] == pytest.approx(
        [0.0, 0.0010, 0.0010, 0.0020], rel=REL
    )
    assert result.trade_cost_total == pytest.approx(0.0040, rel=REL)
    assert result.borrow_cost_total == pytest.approx(0.0015, rel=REL)
    assert [row.net_period_return for row in result.period_rows] == pytest.approx(
        [0.0, -0.1015, 0.0980, -0.0020], rel=1e-9
    )
    # Gross is untouched by the settlement (it earns nothing and costs nothing
    # gross), so the gross book still ends at 0.99.
    assert result.gross_cumulative_return == pytest.approx(-0.01, rel=1e-9)
    # Net compounds the settlement as its own step, exactly as every other cost
    # compounds: 1.0 * 0.8985 * 1.0980 * 0.9980 = 0.984579894.
    assert result.net_cumulative_return == pytest.approx(0.8985 * 1.0980 * 0.9980 - 1.0, rel=1e-9)
    assert result.cost_reconciliation["net_terminal_equity"] == pytest.approx(0.984579894, rel=1e-9)
    # NAV stays one row longer than the period series; the last two rows are the
    # pre-trade and post-trade marks of the same terminal bar.
    assert len(result.nav_series) == result.periods + 1
    assert [row["trade_date"] for row in result.nav_series[-2:]] == [
        DATES_4[3].date().isoformat(),
        DATES_4[3].date().isoformat(),
    ]
    assert [row["net_nav"] for row in result.nav_series[-2:]] == pytest.approx(
        [0.986553, 0.984579894], rel=1e-9
    )
    # A weight of exactly zero is a real flat leg: it never appears in the held
    # book and its price relative is not reported as if it had been earned.
    assert result.period_rows[2].weights == {"BBB": -1.0}
    assert set(result.period_rows[2].price_relatives) == {"BBB"}


# ---------------------------------------------------------------------------
# Vector D: the only vector long enough to clear the annualization gate.
# ---------------------------------------------------------------------------


def test_vector_d_reportable_annualization_over_a_half_year_basis() -> None:
    bar_count = 127
    dates = pd.bdate_range("2026-01-05", periods=bar_count)
    closes = [100.0 * (1.001**index) for index in range(bar_count)]
    prices = _prices(dates, close={"CU": closes})
    positions = _positions([(dates[0], "CU", 1.0)])

    result = run_position_series_backtest(positions, prices)

    assert result.periods == 126
    assert result.diagnostics["minimum_annualization_periods"] == MIN_ANNUALIZATION_EXPOSURE_DAYS
    # 125 compounded bars of +0.1% (bar 0 is flat: the signal has not executed),
    # annualized over a 126-bar basis at 252 bars a year -> 1.001 ** 250 - 1.
    assert result.gross_cumulative_return == pytest.approx(1.001**125 - 1.0, rel=1e-9)
    annualized = result.metrics["annualized_return"]
    assert annualized.status == "available"
    assert annualized.value == pytest.approx(1.001**250 - 1.0, rel=1e-9)
    assert annualized.observation_count == 126
    assert annualized.minimum_required == MIN_ANNUALIZATION_EXPOSURE_DAYS
    assert annualized.warning_codes == ()
    assert INSUFFICIENT_ANNUALIZATION_HISTORY not in result.warning_codes
    # A monotone-rising NAV never drew down: a TRUE zero, reported available,
    # not a placeholder standing in for an unavailable statistic.
    drawdown = result.metrics["max_drawdown"]
    assert drawdown.status == "available"
    assert drawdown.value == 0.0


# ---------------------------------------------------------------------------
# Tri-state honesty: an unavailable statistic is null + status, never a 0.0.
# ---------------------------------------------------------------------------


def test_short_series_suppresses_annualization_and_sharpe_instead_of_faking_them() -> None:
    positions, prices = _vector_a_inputs()
    result = run_position_series_backtest(positions, prices, transaction_costs=A_COSTS)
    for key in ("annualized_return", "net_annualized_return"):
        metric = result.metrics[key]
        assert metric.value is None
        assert metric.status == "insufficient_sample"
        assert metric.observation_count == 5
        assert metric.minimum_required == MIN_ANNUALIZATION_EXPOSURE_DAYS
        assert INSUFFICIENT_ANNUALIZATION_HISTORY in metric.warning_codes
    assert INSUFFICIENT_ANNUALIZATION_HISTORY in result.warning_codes
    # Drawdown is genuinely observable on this book, so it stays available.
    assert result.metrics["max_drawdown"].status == "available"


def test_zero_dispersion_and_single_period_series_suppress_sharpe() -> None:
    # A perfectly flat book: two periods, both returning exactly zero, so the
    # dispersion is zero and the ratio is undefined rather than 0.0.
    prices = _prices(DATES_4, close={"CU": [100.0, 100.0, 100.0, 100.0]})
    positions = _positions([(DATES_4[0], "CU", 1.0)])
    flat = run_position_series_backtest(positions, prices)
    sharpe = flat.metrics["sharpe"]
    assert sharpe.value is None
    assert sharpe.status == "insufficient_sample"
    assert INSUFFICIENT_SHARPE_OBSERVATIONS in sharpe.warning_codes

    # A single period cannot carry a sample standard deviation at all.
    short_prices = _prices(DATES_4[:2], close={"CU": [100.0, 110.0]})
    single = run_position_series_backtest(
        _positions([(DATES_4[0], "CU", 1.0)]), short_prices, execution_delay_periods=0
    )
    assert single.periods == 1
    assert single.metrics["sharpe"].value is None
    assert single.metrics["sharpe"].status == "insufficient_sample"


def test_a_wiped_out_book_reports_minus_one_regardless_of_the_short_basis() -> None:
    """Mirrors ``service._annualization_metric``: terminal equity at or below
    zero is -100% annualized over any horizon, so it is reported even on a
    basis too short to annualize a surviving book -- while the short-history
    disclosure still fires."""

    # A 2x long book into a -60% bar: 1 + 2*(-0.6) = -0.2 arithmetic equity, so
    # the book is wiped out and compounding terminates at 0.0 (see
    # test_capital_exhaustion_freezes_nav_at_zero_and_floors_drawdown).
    prices = _prices(DATES_4, close={"CU": [100.0, 100.0, 40.0, 40.0]})
    positions = _positions([(DATES_4[0], "CU", 2.0)])
    result = run_position_series_backtest(positions, prices)
    assert result.gross_cumulative_return == pytest.approx(-1.0, rel=1e-9)
    annualized = result.metrics["annualized_return"]
    assert annualized.value == -1.0
    assert annualized.status == "available"
    assert INSUFFICIENT_ANNUALIZATION_HISTORY in annualized.warning_codes


def test_metric_provenance_covers_every_metric() -> None:
    positions, prices = _vector_a_inputs()
    result = run_position_series_backtest(positions, prices, transaction_costs=A_COSTS)
    assert set(result.metrics) == {
        "annualized_return",
        "net_annualized_return",
        "sharpe",
        "net_sharpe",
        "max_drawdown",
        "net_max_drawdown",
    }
    assert set(result.metric_provenance) == set(result.metrics)
    for key, entry in result.metric_provenance.items():
        assert entry["status"] == result.metrics[key].status
        assert entry["method"]
        assert entry["source_series"]
        assert entry["sample_role"] == "position_series_backtest"


# ---------------------------------------------------------------------------
# No lookahead: the shift is explicit, and perturbation proves it.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("delay", [0, 1])
def test_no_lookahead_future_price_cannot_affect_earlier_navs(delay: int) -> None:
    """Perturbing the bar-3 close may move periods 2 and 3 onward (bar 3 bounds
    them) and NOTHING earlier, at EITHER execution delay -- the price window a
    period consumes is fixed by the period, not by the signal delay."""

    positions, prices = _vector_a_inputs()
    baseline = run_position_series_backtest(
        positions, prices, transaction_costs=A_COSTS, execution_delay_periods=delay
    )

    perturbed_closes = list(A_CLOSES)
    perturbed_closes[3] = 999.0
    perturbed = run_position_series_backtest(
        positions,
        _prices(DATES_6, close={"CU": perturbed_closes}),
        transaction_costs=A_COSTS,
        execution_delay_periods=delay,
    )
    # Strictly earlier than the perturbed bar: byte-identical.
    assert perturbed.period_rows[:2] == baseline.period_rows[:2]
    assert perturbed.nav_series[:3] == baseline.nav_series[:3]
    # From the window the bar bounds: the vector is live, so it MOVED.
    assert perturbed.period_rows[2].gross_period_return != baseline.period_rows[2].gross_period_return


@pytest.mark.parametrize("delay", [0, 1])
def test_no_lookahead_signal_cannot_affect_earlier_periods(delay: int) -> None:
    """Flipping the bar-2 signal may move the period that first HOLDS it --
    period ``2 + delay`` -- and nothing before it. Both directions are asserted
    at both delays: unchanged before the window, changed at the window."""

    positions, prices = _vector_a_inputs()
    baseline = run_position_series_backtest(
        positions, prices, transaction_costs=A_COSTS, execution_delay_periods=delay
    )

    # Flip the bar-2 signal (+1 instead of -1).
    flipped = _positions(
        [
            (DATES_6[0], "CU", 1.0),
            (DATES_6[1], "CU", 1.0),
            (DATES_6[2], "CU", 1.0),
            (DATES_6[3], "CU", 0.0),
            (DATES_6[4], "CU", 0.0),
        ]
    )
    result = run_position_series_backtest(
        flipped, prices, transaction_costs=A_COSTS, execution_delay_periods=delay
    )
    first_affected = 2 + delay
    assert result.period_rows[:first_affected] == baseline.period_rows[:first_affected]
    assert result.nav_series[: first_affected + 1] == baseline.nav_series[: first_affected + 1]
    assert result.period_rows[first_affected].net_exposure == 1.0
    assert baseline.period_rows[first_affected].net_exposure == -1.0


# ---------------------------------------------------------------------------
# Terminal-bar settlement: an execution bar that EXISTS trades; one that does
# not exist is disclosed in full.
# ---------------------------------------------------------------------------


def test_a_target_book_whose_execution_bar_is_the_last_bar_is_established_there() -> None:
    """The final bar is an execution bar like any other. A book that changes
    there IS traded: the |dw| cost is charged at that bar's execution price, and
    the row carries no holding interval, so no return and no borrow."""

    positions, prices = _vector_a_inputs()
    baseline = run_position_series_backtest(positions, prices, transaction_costs=A_COSTS)

    terminal_target = _positions(
        [
            (DATES_6[0], "CU", 1.0),
            (DATES_6[1], "CU", 1.0),
            (DATES_6[2], "CU", -1.0),
            (DATES_6[3], "CU", 0.0),
            (DATES_6[4], "CU", -1.0),  # establishes on bar 5, the last bar
        ]
    )
    with_terminal = run_position_series_backtest(terminal_target, prices, transaction_costs=A_COSTS)

    # Every HOLDING interval is untouched -- a trade on the last bar cannot
    # reach backwards -- and the settlement is appended after them.
    assert with_terminal.period_rows[:5] == baseline.period_rows
    assert with_terminal.periods == baseline.periods + 1
    assert with_terminal.diagnostics["holding_periods"] == baseline.diagnostics["holding_periods"]
    settlement = with_terminal.period_rows[5]
    assert settlement.is_terminal_settlement is True
    assert settlement.entry_date == settlement.exit_date == DATES_6[5].date().isoformat()
    assert settlement.weights == {"CU": -1.0}
    assert settlement.gross_period_return == 0.0
    assert settlement.borrow_cost == 0.0
    # |-1.0 - 0.0| = 1.0 of notional at (10 + 5) bps
    assert settlement.traded_notional == pytest.approx(1.0, rel=REL)
    assert settlement.trade_cost == pytest.approx(0.0015, rel=REL)
    assert settlement.net_period_return == pytest.approx(-0.0015, rel=REL)
    assert with_terminal.transaction_cost_total == pytest.approx(
        baseline.transaction_cost_total + 0.0015, rel=REL
    )
    # Compounded as its own step on top of the baseline's terminal equity.
    assert with_terminal.cost_reconciliation["net_terminal_equity"] == pytest.approx(
        baseline.cost_reconciliation["net_terminal_equity"] * (1.0 - 0.0015), rel=REL
    )
    assert with_terminal.cost_reconciliation["gross_terminal_equity"] == pytest.approx(
        baseline.cost_reconciliation["gross_terminal_equity"], rel=REL
    )
    assert TERMINAL_BAR_SETTLEMENT in with_terminal.warning_codes
    # It executed, so nothing is left unexecuted.
    assert with_terminal.diagnostics["unexecuted_terminal_weights"] == {}
    assert with_terminal.diagnostics["unexecuted_signal_books"] == ()
    assert any("FINAL bar included" in item for item in with_terminal.assumptions)

    # A terminal book that does NOT change trades nothing: no settlement row, no
    # cost, no warning -- baseline itself is that case.
    assert baseline.diagnostics["terminal_settlement_executed"] is False
    assert baseline.diagnostics["terminal_settlement_traded_notional"] == 0.0
    assert TERMINAL_BAR_SETTLEMENT not in baseline.warning_codes
    assert all(row.is_terminal_settlement is False for row in baseline.period_rows)


def test_a_signal_whose_execution_bar_is_beyond_the_calendar_discloses_every_leg() -> None:
    """An execution bar that does not exist executes nothing, and the whole
    unexecuted book is disclosed -- every leg, the legs targeted flat
    included, because 'close this short' is exactly as unexecuted as 'open this
    long' and a non-zero filter would hide half of what did not happen."""

    positions, prices = _vector_a_inputs()
    baseline = run_position_series_backtest(positions, prices, transaction_costs=A_COSTS)

    beyond = pd.concat([positions, _positions([(DATES_6[5], "CU", -1.0)])], ignore_index=True)
    unexecutable = run_position_series_backtest(beyond, prices, transaction_costs=A_COSTS)
    assert unexecutable.period_rows == baseline.period_rows
    assert unexecutable.transaction_cost_total == pytest.approx(baseline.transaction_cost_total, rel=REL)
    assert unexecutable.diagnostics["unexecutable_signal_bars"] == 1
    assert unexecutable.diagnostics["unexecuted_signal_dates"] == (DATES_6[5].date().isoformat(),)
    assert unexecutable.diagnostics["unexecuted_terminal_weights"] == {"CU": -1.0}
    assert unexecutable.diagnostics["unexecuted_signal_books"] == (
        {"trade_date": DATES_6[5].date().isoformat(), "target_weights": {"CU": -1.0}},
    )
    assert any("execution bar does not exist" in item for item in unexecutable.warnings)

    # The flat legs of an unexecuted book are disclosed too: BBB going to 0.0 is
    # a short that never got covered, and dropping it from the disclosure would
    # report only half the unexecuted book.
    two_name_prices = _prices(DATES_4, close={"AAA": [100.0] * 4, "BBB": [100.0] * 4})
    two_name_positions = _positions(
        [
            (DATES_4[0], "AAA", 0.0),
            (DATES_4[0], "BBB", -1.0),
            (DATES_4[3], "AAA", 1.0),  # never executes: bar 3 + delay 1 is off-calendar
            (DATES_4[3], "BBB", 0.0),  # the leg an earlier disclosure dropped
        ]
    )
    partial = run_position_series_backtest(two_name_positions, two_name_prices)
    assert partial.diagnostics["unexecuted_terminal_weights"] == {"AAA": 1.0, "BBB": 0.0}


def test_a_leg_that_only_trades_on_the_terminal_bar_must_still_be_markable() -> None:
    """The terminal settlement executes at that bar's price, so a leg opening
    there needs a usable quote even though it was flat over every interval."""

    prices = _prices(
        DATES_4,
        close={"AAA": [100.0, 110.0, 121.0, 133.1], "BBB": [50.0, 55.0, 60.0, float("nan")]},
    )
    positions = _positions(
        [(DATES_4[0], "AAA", 1.0), (DATES_4[2], "AAA", 0.0), (DATES_4[2], "BBB", 1.0)]
    )
    with pytest.raises(PositionSeriesInputError) as excinfo:
        run_position_series_backtest(positions, prices)
    assert excinfo.value.code == UNMARKABLE_HELD_POSITION
    assert excinfo.value.details["sample"][0]["instrument"] == "BBB"


# ---------------------------------------------------------------------------
# Capital exhaustion: compounding terminates, it does not go negative.
# ---------------------------------------------------------------------------


def test_capital_exhaustion_freezes_nav_at_zero_and_floors_drawdown() -> None:
    """The reported scenario: a 2x book into -60% then -10%.

    Arithmetic compounding carries -0.20 forward and the SECOND loss RAISES it
    to -0.16 -- a recovery no book can make, off a NAV that is already
    impossible, with a drawdown past -1.0. Equity at or below zero must instead
    terminate compounding at exactly 0.0.
    """

    prices = _prices(DATES_4, close={"CU": [100.0, 100.0, 40.0, 36.0]})
    positions = _positions([(DATES_4[0], "CU", 2.0)])
    result = run_position_series_backtest(positions, prices)

    # The period returns are reported verbatim -- the book really did lose 120%
    # and then 20% -- but the NAV they move is frozen from the wipe-out on.
    assert [row.gross_period_return for row in result.period_rows] == pytest.approx(
        [0.0, -1.20, -0.20], rel=1e-9
    )
    navs = [row["gross_nav"] for row in result.nav_series]
    assert navs == pytest.approx([1.0, 1.0, 0.0, 0.0], rel=REL)
    assert all(value >= 0.0 for value in navs), "NAV must never go negative"
    # No loss may lift the cumulative: the series is monotone non-increasing
    # from the wipe-out onward.
    assert navs[3] <= navs[2]
    assert result.gross_cumulative_return == pytest.approx(-1.0, rel=1e-9)
    assert result.net_cumulative_return == pytest.approx(-1.0, rel=1e-9)

    drawdown = result.metrics["max_drawdown"]
    assert drawdown.status == "available"
    assert drawdown.value == pytest.approx(-1.0, rel=REL)
    assert drawdown.value >= -1.0, "max_drawdown is bounded below by -1.0"
    assert result.metrics["net_max_drawdown"].value >= -1.0

    assert result.diagnostics["gross_capital_exhausted_period"] == 1
    assert result.diagnostics["net_capital_exhausted_period"] == 1
    assert CAPITAL_EXHAUSTED in result.warning_codes
    assert any("compounding terminates" in item for item in result.warnings)
    assert any("terminates compounding" in item for item in result.assumptions)


def test_the_compounding_step_floors_at_zero_but_never_fakes_a_non_finite_one() -> None:
    """A non-finite step must PROPAGATE so the metric tri-state can report it;
    flooring it to 0.0 would disguise an unknown as a wipe-out."""

    assert _compounded(1.0, 0.10) == pytest.approx(1.10, rel=REL)
    assert _compounded(1.0, -1.0) == 0.0
    assert _compounded(1.0, -1.5) == 0.0
    assert _compounded(0.0, 5.0) == 0.0  # frozen: no later gain revives it
    assert math.isnan(_compounded(1.0, float("nan")))
    assert math.isinf(_compounded(1.0, float("inf")))


def test_a_non_finite_source_series_suppresses_every_metric_derived_from_it() -> None:
    """An overflowing book leaves no knowable Sharpe, annualized return,
    drawdown or terminal value. Each is reported null + the kernel's
    ``unavailable_source_series`` status -- never available, never 0.0."""

    # A finite, strictly positive panel (so the mark gate passes) and a finite
    # weight, whose PRODUCT overflows the float range: weight 1e10 against a
    # price relative of ~1e308.
    prices = _prices(DATES_4, close={"CU": [1.0, 1e308, 1e308, 1e308]})
    positions = _positions([(DATES_4[0], "CU", 1e10)])
    with np.errstate(over="ignore", invalid="ignore"):
        result = run_position_series_backtest(positions, prices, execution_delay_periods=0)

    assert result.diagnostics["non_finite_metric_source_series"] == ("gross_nav", "net_nav")
    for key in (
        "annualized_return",
        "net_annualized_return",
        "sharpe",
        "net_sharpe",
        "max_drawdown",
        "net_max_drawdown",
    ):
        metric = result.metrics[key]
        assert metric.value is None, key
        assert metric.status == "unavailable_source_series", key
        assert NON_FINITE_RETURN_SERIES in metric.warning_codes, key
    assert NON_FINITE_RETURN_SERIES in result.warning_codes
    # Provenance still describes every metric, including the suppressed ones.
    assert set(result.metric_provenance) == set(result.metrics)


def test_execution_delay_zero_is_disclosed_and_shifts_the_book_one_bar_earlier() -> None:
    positions, prices = _vector_a_inputs()
    same_bar = run_position_series_backtest(
        positions, prices, transaction_costs=A_COSTS, execution_delay_periods=0
    )
    assert SAME_PERIOD_EXECUTION in same_bar.warning_codes
    assert same_bar.period_rows[0].signal_date == same_bar.period_rows[0].entry_date
    assert [row.net_exposure for row in same_bar.period_rows] == [1.0, 1.0, -1.0, 0.0, 0.0]


# ---------------------------------------------------------------------------
# Structural: this entry accepts exactly the shapes the cross-sectional engine
# cannot express.
# ---------------------------------------------------------------------------


def test_single_instrument_universe_and_directional_book_are_first_class() -> None:
    """The cross-sectional engine keeps a sub-``max(4, group_count)`` slice as a
    flat stub and builds a structurally dollar-neutral +1/n / -1/n book, so a
    one-name directional position is not expressible there. Here it is the
    normal case."""

    positions, prices = _vector_a_inputs()
    result = run_position_series_backtest(positions, prices, transaction_costs=A_COSTS)
    assert len(result.instruments) == 1
    # A live, one-sided book: gross exposure 1 with net exposure +1 and -1 on
    # different periods (never forced to net zero).
    assert {row.net_exposure for row in result.period_rows} == {0.0, 1.0, -1.0}
    assert any(row.gross_period_return != 0.0 for row in result.period_rows)


def test_an_all_flat_target_series_is_legal_and_earns_nothing() -> None:
    prices = _prices(DATES_4, close={"CU": [100.0, 110.0, 121.0, 133.1]})
    positions = _positions([(date, "CU", 0.0) for date in DATES_4[:3]])
    result = run_position_series_backtest(positions, prices, transaction_costs=A_COSTS)
    assert result.periods == 3
    assert result.gross_cumulative_return == 0.0
    assert result.transaction_cost_total == 0.0
    assert result.diagnostics["flat_periods"] == 3


# ---------------------------------------------------------------------------
# Shared math: identical to the pinned engine at the trading-day frequency.
# ---------------------------------------------------------------------------


def test_annualization_is_identical_to_the_engine_at_252_bars_a_year() -> None:
    for cumulative_return, periods in ((0.2, 5), (-0.5, 200), (1.5, 126), (-1.4, 30), (0.0, 1)):
        assert _annualized_return_periodic(cumulative_return, periods, TRADING_PERIODS_PER_YEAR) == (
            _annualized_return(cumulative_return, periods)
        )
    assert _annualized_return_periodic(0.1, 0, TRADING_PERIODS_PER_YEAR) is None


def test_sharpe_is_identical_to_the_engine_at_252_bars_a_year() -> None:
    cases = (
        np.array([0.0, 0.25, -0.20, 0.20, 0.0]),
        np.array([0.01, 0.02, 0.03]),
        np.array([-0.05, 0.05]),
    )
    for returns in cases:
        assert _sharpe_periodic(returns, TRADING_PERIODS_PER_YEAR) == _long_short_sharpe(returns, 1)
    assert _sharpe_periodic(np.array([0.01]), TRADING_PERIODS_PER_YEAR) is None
    assert _sharpe_periodic(np.array([0.01, 0.01]), TRADING_PERIODS_PER_YEAR) is None


def test_annualization_gate_is_derived_from_the_engine_constant_and_scales() -> None:
    assert _minimum_annualization_periods(TRADING_PERIODS_PER_YEAR) == MIN_ANNUALIZATION_EXPOSURE_DAYS
    # An intraday frequency keeps the SAME half-year of wall-clock time.
    assert _minimum_annualization_periods(252.0 * 8) == MIN_ANNUALIZATION_EXPOSURE_DAYS * 8
    assert _minimum_annualization_periods(0.5) == 1


def test_an_odd_frequency_rounds_the_annualization_gate_UP_not_to_nearest() -> None:
    """A half year that lands mid-bar must require the bar that COMPLETES it.

    Rounding to nearest hands back a basis one bar SHORT of the half year --
    and under banker's rounding ``round(126.5) == 126``, so the gate would let a
    253-bar-a-year series annualize off 126 bars of a 126.5-bar half year.
    """

    # 253 * 126 / 252 = 126.5 -> 127, not 126.
    assert _minimum_annualization_periods(253.0) == 127
    # 365 * 126 / 252 = 182.5 -> 183, not 182.
    assert _minimum_annualization_periods(365.0) == 183


def test_periods_per_year_drives_annualization_and_borrow_de_annualization() -> None:
    positions, prices = _vector_a_inputs()
    intraday = run_position_series_backtest(
        positions, prices, transaction_costs=A_COSTS, periods_per_year=8.0
    )
    assert intraday.periods_per_year == 8.0
    assert intraday.diagnostics["minimum_annualization_periods"] == 4
    # 5 periods now clears a 4-period gate, so the statistic becomes reportable.
    assert intraday.metrics["annualized_return"].status == "available"
    assert intraday.metrics["annualized_return"].value == pytest.approx(1.20 ** (8.0 / 5.0) - 1.0, rel=REL)
    # The same annual borrow rate over a coarser bar accrues proportionally more.
    assert intraday.borrow_cost_total == pytest.approx(
        BORROW_BPS_TEN_BP_PER_PERIOD / 10_000.0 / 8.0, rel=REL
    )


# ---------------------------------------------------------------------------
# Fail-closed input gate.
# ---------------------------------------------------------------------------


def _raise_code(positions: pd.DataFrame, prices: pd.DataFrame, **kwargs: object) -> str:
    with pytest.raises(PositionSeriesInputError) as excinfo:
        run_position_series_backtest(positions, prices, **kwargs)  # type: ignore[arg-type]
    return excinfo.value.code


def test_every_precondition_failure_carries_a_code_from_the_closed_set() -> None:
    positions, prices = _vector_a_inputs()
    observed = {
        _raise_code(positions.drop(columns=["target_weight"]), prices),
        _raise_code(positions, prices.drop(columns=["instrument"])),
        _raise_code(positions.iloc[0:0], prices),
        _raise_code(positions, prices.iloc[0:0]),
        _raise_code(positions, prices, execution_price="open"),
        _raise_code(positions, prices, execution_price="vwap"),
        _raise_code(positions, prices, execution_delay_periods=-1),
        _raise_code(positions, prices, periods_per_year=0.0),
        _raise_code(pd.concat([positions, positions.iloc[[0]]], ignore_index=True), prices),
        _raise_code(positions, pd.concat([prices, prices.iloc[[0]]], ignore_index=True)),
        _raise_code(positions.assign(target_weight=[1.0, np.nan, -1.0, 0.0, 0.0]), prices),
        _raise_code(
            pd.concat(
                [positions, _positions([(pd.Timestamp("2026-06-01"), "CU", 1.0)])], ignore_index=True
            ),
            prices,
        ),
        _raise_code(positions, prices, execution_delay_periods=6),
        _raise_code(
            positions,
            _prices(DATES_6, close={"CU": [100.0, 120.0, float("nan"), 120.0, 96.0, 120.0]}),
        ),
        _raise_code(
            positions,
            prices,
            transaction_costs=TransactionCostModel(commission_bps=float("nan")),
        ),
        # BOTH frames are gated, so both directions are exercised here.
        _raise_code(positions.assign(trade_date="not-a-date"), prices),
        _raise_code(positions, prices.assign(trade_date="not-a-date")),
    }
    # Every declared code is reachable, and nothing is raised outside the set.
    assert observed == set(POSITION_SERIES_ERROR_CODES)
    assert observed == {
        MISSING_POSITION_COLUMNS,
        MISSING_PRICE_COLUMNS,
        EMPTY_POSITION_SERIES,
        EMPTY_PRICE_PANEL,
        EXECUTION_PRICE_COLUMN_UNAVAILABLE,
        INVALID_EXECUTION_PRICE,
        INVALID_EXECUTION_DELAY,
        INVALID_PERIODS_PER_YEAR,
        DUPLICATE_POSITION_ROWS,
        DUPLICATE_PRICE_ROWS,
        NON_FINITE_TARGET_WEIGHT,
        SIGNAL_DATE_OUTSIDE_CALENDAR,
        CALENDAR_TOO_SHORT,
        UNMARKABLE_HELD_POSITION,
        INVALID_COST_MODEL,
        INVALID_TRADE_DATE,
    }


# ---------------------------------------------------------------------------
# The gate is TOTAL over the numbers that enter the math: cost rates and dates.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field",
    ["commission_bps", "slippage_bps", "short_borrow_bps_annual"],
)
@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_a_non_finite_cost_rate_is_rejected_at_the_gate(field: str, bad: float) -> None:
    """``TransactionCostModel.__post_init__`` rejects negative rates, but
    ``NaN < 0`` and ``inf < 0`` are both False, so a non-finite rate constructs
    cleanly. It must be refused HERE rather than reaching the cost math and
    turning every downstream return, NAV and metric into a NaN reported as if
    it were a number."""

    positions, prices = _vector_a_inputs()
    costs = TransactionCostModel(**{field: bad})
    with pytest.raises(PositionSeriesInputError) as excinfo:
        run_position_series_backtest(positions, prices, transaction_costs=costs)
    assert excinfo.value.code == INVALID_COST_MODEL
    assert [item["field"] for item in excinfo.value.details["invalid_fields"]] == [field]


def test_the_cost_gate_covers_every_field_of_the_cost_model() -> None:
    """The gated field set is read off the dataclass, so it cannot fall behind a
    rate added upstream."""

    assert set(_COST_MODEL_FIELDS) == {item.name for item in dataclasses.fields(TransactionCostModel)}


def test_the_audit_scenario_a_nan_commission_is_rejected_not_propagated() -> None:
    """Regression for the reported scenario: commission_bps=NaN. The expected
    behavior is refusal at the input gate -- not a run that completes and
    reports NaN costs, NaN NAVs and metrics marked available."""

    positions, prices = _vector_a_inputs()
    with pytest.raises(PositionSeriesInputError) as excinfo:
        run_position_series_backtest(
            positions,
            prices,
            transaction_costs=TransactionCostModel(commission_bps=float("nan"), slippage_bps=5.0),
        )
    assert excinfo.value.code == INVALID_COST_MODEL
    assert "finite, non-negative" in str(excinfo.value)


@pytest.mark.parametrize("frame", ["positions", "prices"])
def test_an_unparsable_trade_date_is_rejected_on_either_frame(frame: str) -> None:
    positions, prices = _vector_a_inputs()
    if frame == "positions":
        positions = positions.assign(trade_date=["2026-01-05", "nope", "2026-01-07", "2026-01-08", "2026-01-09"])
    else:
        prices = prices.assign(trade_date=["nope"] * len(prices))
    with pytest.raises(PositionSeriesInputError) as excinfo:
        run_position_series_backtest(positions, prices)
    assert excinfo.value.code == INVALID_TRADE_DATE
    assert excinfo.value.details["frame"] == frame
    assert excinfo.value.details["format"] == "ISO8601"
    assert "'nope'" in excinfo.value.details["sample"]


@pytest.mark.parametrize("frame", ["positions", "prices"])
def test_a_null_trade_date_is_rejected_on_either_frame(frame: str) -> None:
    """A row with no date is a date that did not parse, not a row to drop."""

    positions, prices = _vector_a_inputs()
    if frame == "positions":
        positions = positions.assign(trade_date=[DATES_6[0], None, DATES_6[2], DATES_6[3], DATES_6[4]])
    else:
        prices = prices.assign(trade_date=[None] * len(prices))
    with pytest.raises(PositionSeriesInputError) as excinfo:
        run_position_series_backtest(positions, prices)
    assert excinfo.value.code == INVALID_TRADE_DATE
    assert excinfo.value.details["frame"] == frame


def test_the_trade_date_format_is_explicit_so_no_shape_is_inferred() -> None:
    """The same calendar spelled as ISO strings, ``date`` objects and
    ``Timestamp``s parses identically -- and an integer, which an inferred
    parse would silently read as a nanosecond epoch, is refused."""

    _, prices = _vector_a_inputs()
    baseline = run_position_series_backtest(*_vector_a_inputs(), transaction_costs=A_COSTS)
    for spelling in (
        [date.date().isoformat() for date in DATES_6[:5]],
        [date.date() for date in DATES_6[:5]],
        list(DATES_6[:5]),
    ):
        positions = _positions(
            [(DATES_6[0], "CU", 1.0), (DATES_6[1], "CU", 1.0), (DATES_6[2], "CU", -1.0),
             (DATES_6[3], "CU", 0.0), (DATES_6[4], "CU", 0.0)]
        ).assign(trade_date=spelling)
        assert run_position_series_backtest(
            positions, prices, transaction_costs=A_COSTS
        ).period_rows == baseline.period_rows

    with pytest.raises(PositionSeriesInputError) as excinfo:
        run_position_series_backtest(
            _positions([(DATES_6[0], "CU", 1.0)]).assign(trade_date=[0]), prices
        )
    assert excinfo.value.code == INVALID_TRADE_DATE


def test_an_unmarkable_bar_under_a_zero_weight_is_not_an_error() -> None:
    """The quality gate is scoped to positions actually held: a name the book is
    flat in contributes nothing, so its missing quote cannot corrupt a return
    and must not block the run."""

    prices = _prices(
        DATES_4,
        close={"AAA": [100.0, 110.0, 121.0, 133.1], "BBB": [50.0, float("nan"), 60.0, 70.0]},
    )
    positions = _positions(
        [(date, "AAA", 1.0) for date in DATES_4[:3]] + [(date, "BBB", 0.0) for date in DATES_4[:3]]
    )
    result = run_position_series_backtest(positions, prices)
    assert result.periods == 3
    assert [row.gross_period_return for row in result.period_rows] == pytest.approx(
        [0.0, 0.10, 0.10], rel=1e-9
    )

    # Give BBB a live weight over that same bar and the run fails closed.
    held_over_gap = _positions(
        [(date, "AAA", 1.0) for date in DATES_4[:3]] + [(DATES_4[0], "BBB", -1.0)]
    )
    with pytest.raises(PositionSeriesInputError) as excinfo:
        run_position_series_backtest(held_over_gap, prices)
    assert excinfo.value.code == UNMARKABLE_HELD_POSITION
    assert excinfo.value.details["sample"][0]["instrument"] == "BBB"


def test_a_non_positive_execution_price_under_a_held_position_fails_closed() -> None:
    positions = _positions([(date, "CU", 1.0) for date in DATES_4[:3]])
    prices = _prices(DATES_4, close={"CU": [100.0, 0.0, 121.0, 133.1]})
    with pytest.raises(PositionSeriesInputError) as excinfo:
        run_position_series_backtest(positions, prices)
    assert excinfo.value.code == UNMARKABLE_HELD_POSITION
    assert excinfo.value.details["sample"][0]["value"] == 0.0

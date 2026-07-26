"""Position-series backtest entry (single- or multi-instrument timing).

Why a SECOND entry rather than a change to ``run_factor_backtest``
------------------------------------------------------------------
:func:`quant_forge.backtesting.service.run_factor_backtest` evaluates a
CROSS-SECTIONAL factor. Four of its structural choices are part of that
contract -- none is a defect -- and together they make it inapplicable to a
position-series (timing) study:

1. the caller supplies a ``factor_id``; there is no weight-series input;
2. a matched cross-section thinner than ``max(4, group_count)`` rows is kept as
   a flat ledger stub (``service.py``'s ``len(merged) < max(4, group_count)``),
   so a one-instrument universe skips every scheduled period;
3. ``service._portfolio_weights`` builds ``+1/n`` long and ``-1/n`` short legs,
   so the book is dollar-neutral by construction and cannot express a
   directional ``+1 / 0 / -1`` position;
4. period returns are close-to-close only (``service._with_period_return``).

This module is purely ADDITIVE: it adds an independent entry for the case where
the caller already owns the target weights. Nothing in ``service.py`` changes --
every existing function, signature, artifact field, and metric stays identical.
Shared math is IMPORTED from the pinned engine rather than restated
(:func:`service._max_drawdown` is reused verbatim); the two rate-scaled helpers
this module needs in a general per-period frequency (annualization, Sharpe) are
written here because the engine's are pinned to a 252-trading-day basis, and
``tests/test_backtest_position_series.py`` asserts they are numerically
IDENTICAL to the engine's at ``periods_per_year == 252``, so the two can never
drift into a second kernel formula.

Position semantics (state, not delta)
-------------------------------------
``target_weight`` is a STATE. Each ``trade_date`` present in the position table
is one complete target book: an instrument with no row on that date is targeted
flat (0.0). A calendar bar absent from the position table carries the previous
target book forward unchanged. Every emitted period reports the
``signal_date`` its held weights actually came from, so a carry-forward is
always visible in the output (and counted in ``diagnostics``), never silent.

Execution and return math
-------------------------
Let ``timeline`` be the sorted distinct trade dates of the price panel,
``d = execution_delay_periods``, and ``P`` the execution-price series selected
by ``execution_price`` (``close`` or ``open``).

* The target book determined on signal bar ``t`` is executed on bar ``t + d``
  and held from there until the next execution bar. Held weights on bar ``j``
  therefore come from the newest signal bar ``s`` with ``s + d <= j``; before
  any signal has been executed the book is flat. The shift is explicit in the
  code (``_held_weight_matrix``), and
  ``test_no_lookahead_signal_cannot_affect_earlier_periods`` /
  ``test_no_lookahead_future_price_cannot_affect_earlier_navs`` prove it.
* Period ``j`` spans execution bars ``j -> j + 1`` (``j = 0 .. n - 2``):

  - ``gross_period_return[j] = sum_i W[j][i] * (P[j+1][i] / P[j][i] - 1)``
  - ``traded_notional[j]     = sum_i |W[j][i] - W[j-1][i]|`` (``W[-1] = 0``)
  - ``trade_cost[j]          = traded_notional[j] * (commission_bps + slippage_bps) / 10_000``
  - ``short_notional[j]      = sum_i max(-W[j][i], 0)``
  - ``borrow_cost[j]         = short_notional[j] * short_borrow_bps_annual / 10_000 / periods_per_year``
  - ``net_period_return[j]   = gross_period_return[j] - trade_cost[j] - borrow_cost[j]``

  The borrow leg reduces EXACTLY to the engine's
  ``service._transaction_cost_rate`` accrual (``rate / 10_000 * held / 252``)
  for a book that is 100% short over the interval -- the engine's long/short
  book always is; this entry's book need not be, so the accrual is scaled by
  the realized short notional.
* NAV compounds the period returns: ``nav[0] = 1.0`` on the first execution bar
  and ``nav[j+1] = nav[j] * (1 + return[j])``.
* The weights targeted for the LAST bar are never established (no interval
  follows it), so no closing trade is charged there. The unestablished book is
  reported as ``diagnostics["unexecuted_terminal_weights"]`` and disclosed in
  ``assumptions``.

Fail-closed input gate
----------------------
``execution_price="open"`` against a panel with no ``open`` column raises
:class:`PositionSeriesInputError` (code ``EXECUTION_PRICE_COLUMN_UNAVAILABLE``);
it never falls back to ``close``. Likewise a non-finite or non-positive
execution price on a bar where the instrument carries a non-zero weight raises
(``UNMARKABLE_HELD_POSITION``) instead of imputing a return -- mapping an
unavailable price to a flat position is the caller's (data layer's) decision,
made explicit as a ``0.0`` target weight.

Honest metrics
--------------
Summary metrics are :class:`~quant_forge.core.contracts.MetricValue` with the
kernel's tri-state vocabulary. Annualized return is suppressed to ``None`` /
``insufficient_sample`` below the same half-year basis the engine uses
(:data:`~quant_forge.backtesting.service.MIN_ANNUALIZATION_EXPOSURE_DAYS`,
rescaled to the configured ``periods_per_year``); Sharpe is suppressed below two
periods or at zero dispersion. No metric is ever faked as ``0.0``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

import numpy as np
import pandas as pd

from quant_forge.backtesting.service import (
    INSUFFICIENT_ANNUALIZATION_HISTORY,
    INSUFFICIENT_SHARPE_OBSERVATIONS,
    MIN_ANNUALIZATION_EXPOSURE_DAYS,
    _max_drawdown,
)
from quant_forge.core.contracts import (
    METRICS_SCHEMA_VERSION,
    MetricValue,
    TransactionCostModel,
)

__all__ = [
    "POSITION_SERIES_ERROR_CODES",
    "POSITION_SERIES_ROLE",
    "SAME_PERIOD_EXECUTION",
    "TRADING_PERIODS_PER_YEAR",
    "PositionSeriesBacktestResult",
    "PositionSeriesInputError",
    "PositionSeriesPeriod",
    "run_position_series_backtest",
]


POSITION_SERIES_ROLE = "position_series_backtest"
# The engine's trading-day year; the default period frequency of this entry is
# one trading day, so the two agree by construction.
TRADING_PERIODS_PER_YEAR = 252.0

POSITION_COLUMNS: tuple[str, str, str] = ("trade_date", "instrument", "target_weight")
REQUIRED_PRICE_COLUMNS: tuple[str, str] = ("trade_date", "instrument")
EXECUTION_PRICE_CHOICES: tuple[str, str] = ("close", "open")

# Structured precondition codes. Closed vocabulary in BOTH directions -- every
# raise uses one of these AND every one of these is reachable -- asserted by
# ``tests/test_backtest_position_series.py``::
# ``test_every_precondition_failure_carries_a_code_from_the_closed_set``.
MISSING_POSITION_COLUMNS = "MISSING_POSITION_COLUMNS"
MISSING_PRICE_COLUMNS = "MISSING_PRICE_COLUMNS"
EMPTY_POSITION_SERIES = "EMPTY_POSITION_SERIES"
EMPTY_PRICE_PANEL = "EMPTY_PRICE_PANEL"
EXECUTION_PRICE_COLUMN_UNAVAILABLE = "EXECUTION_PRICE_COLUMN_UNAVAILABLE"
INVALID_EXECUTION_PRICE = "INVALID_EXECUTION_PRICE"
INVALID_EXECUTION_DELAY = "INVALID_EXECUTION_DELAY"
INVALID_PERIODS_PER_YEAR = "INVALID_PERIODS_PER_YEAR"
DUPLICATE_POSITION_ROWS = "DUPLICATE_POSITION_ROWS"
DUPLICATE_PRICE_ROWS = "DUPLICATE_PRICE_ROWS"
NON_FINITE_TARGET_WEIGHT = "NON_FINITE_TARGET_WEIGHT"
SIGNAL_DATE_OUTSIDE_CALENDAR = "SIGNAL_DATE_OUTSIDE_CALENDAR"
CALENDAR_TOO_SHORT = "CALENDAR_TOO_SHORT"
UNMARKABLE_HELD_POSITION = "UNMARKABLE_HELD_POSITION"

POSITION_SERIES_ERROR_CODES: tuple[str, ...] = (
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
)

# Disclosure codes. The two annualization/Sharpe codes are the ENGINE's own
# spellings (imported above), so a reader sees one vocabulary across both
# entries; SAME_PERIOD_EXECUTION is new because the engine has no zero-delay
# mode to disclose.
SAME_PERIOD_EXECUTION = "SAME_PERIOD_EXECUTION"

# How many offending rows an error payload lists before truncating; a bounded
# sample keeps the message actionable without echoing an entire panel.
_ERROR_SAMPLE_LIMIT = 5


class PositionSeriesInputError(ValueError):
    """Typed request-precondition failure for the position-series entry.

    Mirrors ``synthesis.service.SynthesisPreconditionError``: subclassing
    ``ValueError`` keeps existing invalid-request mappings working, and the
    stable ``code`` (one of :data:`POSITION_SERIES_ERROR_CODES`) is what a
    caller branches on. ``details`` carries a bounded, machine-readable
    description of what failed so the fault is diagnosable without re-running.
    """

    code = "POSITION_SERIES_PRECONDITION"

    def __init__(self, message: str, *, code: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details: dict[str, Any] = dict(details or {})


# ---------------------------------------------------------------------------
# Result contracts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PositionSeriesPeriod:
    """One execution-bar interval: the book held over it and what it earned.

    ``signal_date`` is the bar whose target book is held here (never later than
    ``entry_date`` minus the configured delay); ``carried_forward`` marks the
    periods where that book is older than the delay alone would imply because
    the position table has no row for the intervening bar.
    """

    period_id: int
    signal_date: str | None
    entry_date: str
    exit_date: str
    weights: dict[str, float]
    long_exposure: float
    short_exposure: float
    gross_exposure: float
    net_exposure: float
    price_relatives: dict[str, float]
    gross_period_return: float
    net_period_return: float
    traded_notional: float
    turnover: float
    trade_cost: float
    borrow_cost: float
    transaction_cost: float
    gross_nav: float
    net_nav: float
    carried_forward: bool


@dataclass(frozen=True)
class PositionSeriesBacktestResult:
    """Structured position-series backtest output.

    ``nav_series`` has one row per execution bar INCLUDING the ``1.0`` base at
    the first bar, so it is one longer than ``period_rows``; ``period_rows`` is
    simultaneously the period-return series, the held-position series, and the
    per-period turnover series (one row carries all three), which keeps the
    three from ever drifting out of alignment.
    """

    periods: int
    execution_price: str
    execution_delay_periods: int
    periods_per_year: float
    instruments: tuple[str, ...]
    start_date: str
    end_date: str
    period_rows: tuple[PositionSeriesPeriod, ...]
    nav_series: tuple[dict[str, object], ...]
    gross_cumulative_return: float
    net_cumulative_return: float
    traded_notional_total: float
    turnover_total: float
    turnover_mean: float
    trade_cost_total: float
    borrow_cost_total: float
    transaction_cost_total: float
    transaction_costs: TransactionCostModel
    metrics: dict[str, MetricValue]
    metric_provenance: dict[str, dict[str, object]] = field(default_factory=dict)
    cost_reconciliation: dict[str, float] = field(default_factory=dict)
    diagnostics: dict[str, object] = field(default_factory=dict)
    warning_codes: tuple[str, ...] = field(default_factory=tuple)
    warnings: tuple[str, ...] = field(default_factory=tuple)
    assumptions: tuple[str, ...] = field(default_factory=tuple)
    sample_role: str = POSITION_SERIES_ROLE
    schema_version: str = METRICS_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Rate-scaled helpers (identical to the engine's at periods_per_year == 252;
# see the module docstring and the equivalence tests)
# ---------------------------------------------------------------------------


def _annualized_return_periodic(cumulative_return: float, periods: int, periods_per_year: float) -> float | None:
    """Geometric annualization over ``periods`` bars of the stated frequency.

    Same shape as ``service._annualized_return``: a wiped-out book always
    reports ``-1.0``, an empty basis reports ``None``.
    """

    if periods <= 0:
        return None
    terminal_equity = 1.0 + cumulative_return
    if terminal_equity <= 0.0:
        return -1.0
    return float(terminal_equity ** (periods_per_year / periods) - 1.0)


def _sharpe_periodic(returns: np.ndarray, periods_per_year: float) -> float | None:
    """Same shape as ``service._long_short_sharpe``: mean/std scaled by the
    square root of the periods in a year. ``None`` below two observations or at
    zero dispersion -- never a fabricated ``0.0``."""

    if len(returns) < 2:
        return None
    std = float(np.std(returns, ddof=1))
    if std == 0.0:
        return None
    return float(np.mean(returns) / std * np.sqrt(periods_per_year))


def _minimum_annualization_periods(periods_per_year: float) -> int:
    """The engine's half-year reportability gate, expressed in bars.

    Derived FROM ``MIN_ANNUALIZATION_EXPOSURE_DAYS`` (126 of 252 trading days)
    so the daily case reproduces the engine's gate exactly and an intraday
    frequency scales to the same wall-clock half year.
    """

    scaled = periods_per_year * MIN_ANNUALIZATION_EXPOSURE_DAYS / TRADING_PERIODS_PER_YEAR
    return max(1, int(round(scaled)))


# ---------------------------------------------------------------------------
# Input normalization (fail-closed)
# ---------------------------------------------------------------------------


def _normalized_frame(
    frame: pd.DataFrame,
    required: tuple[str, ...],
    *,
    label: str,
    missing_code: str,
) -> pd.DataFrame:
    if not isinstance(frame, pd.DataFrame):
        raise PositionSeriesInputError(
            f"{label} must be a pandas DataFrame, got {type(frame).__name__}",
            code=missing_code,
            details={"frame": label, "received_type": type(frame).__name__},
        )
    missing = [column for column in required if column not in frame.columns]
    if missing:
        raise PositionSeriesInputError(
            f"{label} is missing required column(s): {', '.join(missing)}",
            code=missing_code,
            details={"frame": label, "missing_columns": missing, "present_columns": list(frame.columns)},
        )
    normalized = frame.copy()
    normalized["trade_date"] = pd.to_datetime(normalized["trade_date"])
    normalized["instrument"] = normalized["instrument"].astype(str)
    return normalized


def _reject_duplicates(frame: pd.DataFrame, *, label: str, code: str) -> None:
    duplicated = frame.duplicated(subset=["trade_date", "instrument"], keep=False)
    if not bool(duplicated.any()):
        return
    offenders = (
        frame.loc[duplicated, ["trade_date", "instrument"]]
        .drop_duplicates()
        .head(_ERROR_SAMPLE_LIMIT)
    )
    sample = [
        {"trade_date": row.trade_date.date().isoformat(), "instrument": row.instrument}
        for row in offenders.itertuples()
    ]
    raise PositionSeriesInputError(
        f"{label} carries more than one row for the same (trade_date, instrument)",
        code=code,
        details={"frame": label, "duplicate_count": int(duplicated.sum()), "sample": sample},
    )


def _resolve_execution_price_column(prices: pd.DataFrame, execution_price: str) -> str:
    if execution_price not in EXECUTION_PRICE_CHOICES:
        raise PositionSeriesInputError(
            f"execution_price must be one of {EXECUTION_PRICE_CHOICES}, got {execution_price!r}",
            code=INVALID_EXECUTION_PRICE,
            details={"execution_price": execution_price, "supported": list(EXECUTION_PRICE_CHOICES)},
        )
    if execution_price in prices.columns:
        return execution_price
    # Fail-closed: an absent execution-price column is a structured error, never
    # a silent fallback to another column (a close-priced result reported as an
    # open-priced one would be indistinguishable downstream).
    raise PositionSeriesInputError(
        f"execution_price={execution_price!r} requires a {execution_price!r} column on the price panel; "
        "no fallback price column is substituted",
        code=EXECUTION_PRICE_COLUMN_UNAVAILABLE,
        details={"execution_price": execution_price, "available_columns": list(prices.columns)},
    )


def _target_weight_pivot(positions: pd.DataFrame, instruments: list[str]) -> pd.DataFrame:
    weights = pd.to_numeric(positions["target_weight"], errors="coerce")
    non_finite = ~np.isfinite(weights.to_numpy(dtype=float))
    if bool(non_finite.any()):
        offenders = positions.loc[non_finite, ["trade_date", "instrument"]].head(_ERROR_SAMPLE_LIMIT)
        sample = [
            {"trade_date": row.trade_date.date().isoformat(), "instrument": row.instrument}
            for row in offenders.itertuples()
        ]
        raise PositionSeriesInputError(
            "target_weight must be finite on every position row; a position that cannot be sized is the "
            "caller's decision to express as an explicit 0.0 flat target",
            code=NON_FINITE_TARGET_WEIGHT,
            details={"non_finite_count": int(non_finite.sum()), "sample": sample},
        )
    frame = positions.assign(target_weight=weights.astype(float))
    # An instrument absent from a signal date's rows is targeted flat: each
    # signal date is a COMPLETE target book, not a patch on the previous one.
    pivot = frame.pivot(index="trade_date", columns="instrument", values="target_weight")
    return pivot.reindex(columns=instruments).fillna(0.0).sort_index()


def _held_weight_matrix(
    *,
    signal_positions: np.ndarray,
    signal_weights: np.ndarray,
    bar_count: int,
    delay: int,
) -> tuple[np.ndarray, list[int | None]]:
    """Held weights per execution bar, plus the source signal-bar index.

    The shift is explicit: bar ``j`` may only consume a signal bar ``s`` with
    ``s + delay <= j``. Bars before the first executable signal are flat. This
    is the single place the no-lookahead rule is enforced.
    """

    held = np.zeros((bar_count, signal_weights.shape[1]), dtype=float)
    sources: list[int | None] = [None] * bar_count
    for bar_index in range(bar_count):
        latest_allowed = bar_index - delay
        if latest_allowed < 0:
            continue
        slot = int(np.searchsorted(signal_positions, latest_allowed, side="right")) - 1
        if slot < 0:
            continue
        held[bar_index] = signal_weights[slot]
        sources[bar_index] = int(signal_positions[slot])
    return held, sources


def _assert_marked(
    *,
    price_matrix: np.ndarray,
    held: np.ndarray,
    timeline: list[pd.Timestamp],
    instruments: list[str],
    price_column: str,
) -> None:
    """Every bar that bounds a held position must carry a usable price.

    A weight is live over ``[j, j + 1]``, so both ends must be finite and
    strictly positive; otherwise the price relative is undefined and the honest
    answer is a structured failure, not an imputed return.
    """

    live = np.zeros(price_matrix.shape, dtype=bool)
    period_count = price_matrix.shape[0] - 1
    for bar_index in range(period_count):
        carrying = held[bar_index] != 0.0
        live[bar_index] |= carrying
        live[bar_index + 1] |= carrying
    usable = np.isfinite(price_matrix) & (price_matrix > 0.0)
    offending = live & ~usable
    if not bool(offending.any()):
        return
    rows, columns = np.nonzero(offending)
    sample = [
        {
            "trade_date": timeline[int(row)].date().isoformat(),
            "instrument": instruments[int(column)],
            "price_column": price_column,
            "value": (None if not np.isfinite(price_matrix[row, column]) else float(price_matrix[row, column])),
        }
        for row, column in list(zip(rows, columns, strict=True))[:_ERROR_SAMPLE_LIMIT]
    ]
    raise PositionSeriesInputError(
        f"a non-zero position is held across a bar with no usable {price_column!r} price "
        "(a finite, strictly positive quote is required at both ends of every holding interval)",
        code=UNMARKABLE_HELD_POSITION,
        details={"unmarkable_count": int(offending.sum()), "sample": sample},
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_position_series_backtest(
    positions: pd.DataFrame,
    prices: pd.DataFrame,
    *,
    transaction_costs: TransactionCostModel | None = None,
    execution_price: Literal["close", "open"] = "close",
    execution_delay_periods: int = 1,
    periods_per_year: float = TRADING_PERIODS_PER_YEAR,
    sample_role: str = POSITION_SERIES_ROLE,
) -> PositionSeriesBacktestResult:
    """Backtest a caller-supplied target-weight series against a price panel.

    Args:
        positions: Long table with ``trade_date``, ``instrument``,
            ``target_weight``. One instrument (``universe == 1``) is legal;
            ``target_weight == 0.0`` is a legal flat target; negative weights
            are short positions. Each ``trade_date`` present is a complete
            target book (see the module docstring); a bar absent from the table
            carries the previous book forward.
        prices: Long panel with ``trade_date``, ``instrument`` and the
            execution-price column (``close``, plus ``open`` when
            ``execution_price="open"``). Its distinct trade dates ARE the
            evaluation calendar.
        transaction_costs: Reused verbatim from the engine's cost contract.
            ``commission_bps`` + ``slippage_bps`` charge traded notional;
            ``short_borrow_bps_annual`` accrues on held short notional,
            de-annualized by ``periods_per_year``.
        execution_price: ``"close"`` or ``"open"``. A requested column that the
            panel does not carry is a structured error, never a silent fallback.
        execution_delay_periods: Bars between a signal bar and its execution
            bar. ``1`` (the default) is the no-lookahead next-bar convention;
            ``0`` executes on the signal bar itself and is disclosed via the
            ``SAME_PERIOD_EXECUTION`` warning code.
        periods_per_year: Bars per year for annualization and short-borrow
            de-annualization. ``252.0`` (daily) by default; an intraday series
            must state its own bar count.
        sample_role: Recorded on every :class:`MetricValue`.

    Returns:
        :class:`PositionSeriesBacktestResult`.

    Raises:
        PositionSeriesInputError: Any precondition failure, carrying a stable
            ``code`` from :data:`POSITION_SERIES_ERROR_CODES`.
    """

    costs = transaction_costs or TransactionCostModel()
    if not isinstance(execution_delay_periods, (int, np.integer)) or isinstance(execution_delay_periods, bool):
        raise PositionSeriesInputError(
            f"execution_delay_periods must be an integer, got {type(execution_delay_periods).__name__}",
            code=INVALID_EXECUTION_DELAY,
            details={"execution_delay_periods": repr(execution_delay_periods)},
        )
    delay = int(execution_delay_periods)
    if delay < 0:
        raise PositionSeriesInputError(
            "execution_delay_periods must be >= 0",
            code=INVALID_EXECUTION_DELAY,
            details={"execution_delay_periods": delay},
        )
    periods_per_year = float(periods_per_year)
    if not np.isfinite(periods_per_year) or periods_per_year <= 0.0:
        raise PositionSeriesInputError(
            "periods_per_year must be finite and positive",
            code=INVALID_PERIODS_PER_YEAR,
            details={"periods_per_year": periods_per_year},
        )

    position_frame = _normalized_frame(
        positions, POSITION_COLUMNS, label="positions", missing_code=MISSING_POSITION_COLUMNS
    )
    price_frame = _normalized_frame(
        prices, REQUIRED_PRICE_COLUMNS, label="prices", missing_code=MISSING_PRICE_COLUMNS
    )
    if position_frame.empty:
        raise PositionSeriesInputError(
            "positions carries no rows; there is no target book to evaluate",
            code=EMPTY_POSITION_SERIES,
            details={},
        )
    if price_frame.empty:
        raise PositionSeriesInputError(
            "prices carries no rows; there is no execution calendar",
            code=EMPTY_PRICE_PANEL,
            details={},
        )
    _reject_duplicates(position_frame, label="positions", code=DUPLICATE_POSITION_ROWS)
    _reject_duplicates(price_frame, label="prices", code=DUPLICATE_PRICE_ROWS)
    price_column = _resolve_execution_price_column(price_frame, execution_price)

    timeline = sorted(price_frame["trade_date"].drop_duplicates())
    bar_count = len(timeline)
    if bar_count < delay + 2:
        raise PositionSeriesInputError(
            f"the price calendar has {bar_count} bar(s); at least {delay + 2} are required to form one "
            f"holding interval at execution_delay_periods={delay}",
            code=CALENDAR_TOO_SHORT,
            details={"bar_count": bar_count, "minimum_required": delay + 2, "execution_delay_periods": delay},
        )
    bar_index_of = {timestamp: index for index, timestamp in enumerate(timeline)}

    signal_dates = sorted(position_frame["trade_date"].drop_duplicates())
    outside = [item for item in signal_dates if item not in bar_index_of]
    if outside:
        raise PositionSeriesInputError(
            "every position trade_date must exist on the price calendar; dates off-calendar are a structured "
            "failure rather than a silent drop",
            code=SIGNAL_DATE_OUTSIDE_CALENDAR,
            details={
                "outside_count": len(outside),
                "sample": [item.date().isoformat() for item in outside[:_ERROR_SAMPLE_LIMIT]],
            },
        )

    instruments = sorted(position_frame["instrument"].drop_duplicates().tolist())
    weight_pivot = _target_weight_pivot(position_frame, instruments)
    signal_positions = np.array([bar_index_of[item] for item in weight_pivot.index], dtype=int)
    signal_weights = weight_pivot.to_numpy(dtype=float)
    held, held_sources = _held_weight_matrix(
        signal_positions=signal_positions,
        signal_weights=signal_weights,
        bar_count=bar_count,
        delay=delay,
    )

    # Coerce first so a non-numeric quote becomes NaN and is reported by the
    # held-position mark check below (a structured UNMARKABLE_HELD_POSITION)
    # rather than an untyped dtype error from the pivot.
    numeric_prices = price_frame.assign(
        **{price_column: pd.to_numeric(price_frame[price_column], errors="coerce")}
    )
    price_pivot = (
        numeric_prices.pivot(index="trade_date", columns="instrument", values=price_column)
        .reindex(index=timeline, columns=instruments)
    )
    price_matrix = price_pivot.to_numpy(dtype=float)
    _assert_marked(
        price_matrix=price_matrix,
        held=held,
        timeline=timeline,
        instruments=instruments,
        price_column=price_column,
    )

    trade_rate = (costs.commission_bps + costs.slippage_bps) / 10_000.0
    borrow_rate_per_period = costs.short_borrow_bps_annual / 10_000.0 / periods_per_year

    period_rows: list[PositionSeriesPeriod] = []
    nav_series: list[dict[str, object]] = [
        {"trade_date": timeline[0].date().isoformat(), "gross_nav": 1.0, "net_nav": 1.0}
    ]
    gross_nav = 1.0
    net_nav = 1.0
    previous_weights = np.zeros(len(instruments), dtype=float)
    carried_forward_periods = 0
    for bar_index in range(bar_count - 1):
        weights = held[bar_index]
        carrying = weights != 0.0
        # A flat leg contributes nothing, so its (possibly absent) price never
        # enters the sum: 0 * NaN would poison the period return.
        relatives = np.zeros(len(instruments), dtype=float)
        if bool(carrying.any()):
            relatives[carrying] = price_matrix[bar_index + 1][carrying] / price_matrix[bar_index][carrying] - 1.0
        gross_period_return = float(np.dot(weights, relatives))
        delta = weights - previous_weights
        traded_notional = float(np.abs(delta).sum())
        short_notional = float(np.clip(-weights, 0.0, None).sum())
        trade_cost = traded_notional * trade_rate
        borrow_cost = short_notional * borrow_rate_per_period
        transaction_cost = trade_cost + borrow_cost
        net_period_return = gross_period_return - transaction_cost
        gross_nav *= 1.0 + gross_period_return
        net_nav *= 1.0 + net_period_return
        source_bar = held_sources[bar_index]
        is_carried = source_bar is not None and source_bar < bar_index - delay
        if is_carried:
            carried_forward_periods += 1
        period_rows.append(
            PositionSeriesPeriod(
                period_id=bar_index,
                signal_date=(timeline[source_bar].date().isoformat() if source_bar is not None else None),
                entry_date=timeline[bar_index].date().isoformat(),
                exit_date=timeline[bar_index + 1].date().isoformat(),
                weights={
                    instrument: float(weight)
                    for instrument, weight in zip(instruments, weights, strict=True)
                    if weight != 0.0
                },
                long_exposure=float(np.clip(weights, 0.0, None).sum()),
                short_exposure=short_notional,
                gross_exposure=float(np.abs(weights).sum()),
                net_exposure=float(weights.sum()),
                price_relatives={
                    instrument: float(relative)
                    for instrument, relative, live in zip(instruments, relatives, carrying, strict=True)
                    if live
                },
                gross_period_return=gross_period_return,
                net_period_return=net_period_return,
                traded_notional=traded_notional,
                turnover=traded_notional / 2.0,
                trade_cost=float(trade_cost),
                borrow_cost=float(borrow_cost),
                transaction_cost=float(transaction_cost),
                gross_nav=gross_nav,
                net_nav=net_nav,
                carried_forward=is_carried,
            )
        )
        nav_series.append(
            {
                "trade_date": timeline[bar_index + 1].date().isoformat(),
                "gross_nav": gross_nav,
                "net_nav": net_nav,
            }
        )
        previous_weights = weights

    periods = len(period_rows)
    gross_returns = np.array([row.gross_period_return for row in period_rows], dtype=float)
    net_returns = np.array([row.net_period_return for row in period_rows], dtype=float)
    gross_cumulative_return = float(gross_nav - 1.0)
    net_cumulative_return = float(net_nav - 1.0)
    minimum_periods = _minimum_annualization_periods(periods_per_year)

    metrics: dict[str, MetricValue] = {}
    for prefix, returns, cumulative, nav_key in (
        ("", gross_returns, gross_cumulative_return, "gross_nav"),
        ("net_", net_returns, net_cumulative_return, "net_nav"),
    ):
        # Same reportability rule as ``service._annualization_metric``: the
        # value is suppressed below the half-year basis UNLESS the book was
        # wiped out, which is -100% annualized over any horizon. The
        # insufficient-history disclosure still fires on the short basis.
        reportable = periods >= minimum_periods or (1.0 + cumulative) <= 0.0
        annualized = (
            _annualized_return_periodic(cumulative, periods, periods_per_year) if reportable else None
        )
        annualized_warnings = () if periods >= minimum_periods else (INSUFFICIENT_ANNUALIZATION_HISTORY,)
        metrics[f"{prefix}annualized_return"] = MetricValue(
            value=annualized,
            unit="return",
            status="available" if annualized is not None else "insufficient_sample",
            observation_count=periods,
            minimum_required=minimum_periods,
            method="geometric_annualization_period_basis",
            source_series="position_series_period_returns",
            sample_role=sample_role,
            warning_codes=annualized_warnings,
        )
        sharpe = _sharpe_periodic(returns, periods_per_year)
        metrics[f"{prefix}sharpe"] = MetricValue(
            value=sharpe,
            unit="ratio",
            status="available" if sharpe is not None else "insufficient_sample",
            observation_count=periods,
            minimum_required=2,
            method="period_return_mean_over_std_scaled",
            source_series="position_series_period_returns",
            sample_role=sample_role,
            warning_codes=() if sharpe is not None else (INSUFFICIENT_SHARPE_OBSERVATIONS,),
        )
        # The NAV base (1.0 at the first execution bar) is excluded here because
        # ``service._max_drawdown`` prepends its own 1.0 start; passing both
        # would double the base point without changing the result.
        drawdown_navs = np.array([float(row[nav_key]) for row in nav_series[1:]], dtype=float)
        metrics[f"{prefix}max_drawdown"] = MetricValue(
            value=float(_max_drawdown(drawdown_navs)),
            unit="return",
            status="available",
            observation_count=periods,
            minimum_required=1,
            method="peak_to_trough_nav",
            source_series=f"position_series_{nav_key}",
            sample_role=sample_role,
        )

    warning_code_items: list[str] = []
    warning_items: list[str] = []
    for metric in metrics.values():
        warning_code_items.extend(metric.warning_codes)
    if delay == 0:
        warning_code_items.append(SAME_PERIOD_EXECUTION)
        warning_items.append(
            "execution_delay_periods=0: the target book determined on a bar is executed on that SAME bar's "
            "execution price; the default next-bar convention (1) is what keeps the signal strictly prior "
            "to the price it trades at"
        )
    if carried_forward_periods:
        warning_items.append(
            f"{carried_forward_periods} period(s) held a target book older than the configured delay because "
            "the position table has no row for the intervening bar; each such period reports its actual "
            "signal_date and carried_forward=True"
        )
    terminal_weights = {
        instrument: float(weight)
        for instrument, weight in zip(instruments, held[bar_count - 1], strict=True)
        if weight != 0.0
    }
    # A signal on one of the last `delay` bars has no execution bar on this
    # calendar at all, so it can move nothing. Disclosed rather than silently
    # dropped: a caller whose whole tail of signals does nothing must be able
    # to see it in the result.
    unexecutable_signal_bars = int(np.count_nonzero(signal_positions + delay > bar_count - 1))
    if unexecutable_signal_bars:
        warning_items.append(
            f"{unexecutable_signal_bars} signal bar(s) fall within the last {delay} bar(s) of the calendar, "
            "so their execution bar does not exist and they contribute no position, return, or cost"
        )

    traded_notional_total = float(sum(row.traded_notional for row in period_rows))
    trade_cost_total = float(sum(row.trade_cost for row in period_rows))
    borrow_cost_total = float(sum(row.borrow_cost for row in period_rows))
    metric_provenance = {
        key: {
            "method": value.method,
            "source_series": value.source_series,
            "sample_role": value.sample_role,
            "observation_count": value.observation_count,
            "minimum_required": value.minimum_required,
            "status": value.status,
            "warning_codes": list(value.warning_codes),
        }
        for key, value in metrics.items()
    }
    return PositionSeriesBacktestResult(
        periods=periods,
        execution_price=price_column,
        execution_delay_periods=delay,
        periods_per_year=periods_per_year,
        instruments=tuple(instruments),
        start_date=timeline[0].date().isoformat(),
        end_date=timeline[-1].date().isoformat(),
        period_rows=tuple(period_rows),
        nav_series=tuple(nav_series),
        gross_cumulative_return=gross_cumulative_return,
        net_cumulative_return=net_cumulative_return,
        traded_notional_total=traded_notional_total,
        turnover_total=traded_notional_total / 2.0,
        # periods >= 1 is guaranteed by the CALENDAR_TOO_SHORT gate above.
        turnover_mean=float(traded_notional_total / 2.0 / periods),
        trade_cost_total=trade_cost_total,
        borrow_cost_total=borrow_cost_total,
        transaction_cost_total=float(trade_cost_total + borrow_cost_total),
        transaction_costs=costs,
        metrics=metrics,
        metric_provenance=metric_provenance,
        cost_reconciliation={
            "period_transaction_cost_sum": float(trade_cost_total + borrow_cost_total),
            "gross_terminal_equity": float(gross_nav),
            "net_terminal_equity": float(net_nav),
        },
        diagnostics={
            "bar_count": bar_count,
            "signal_bar_count": int(len(signal_positions)),
            "carried_forward_periods": carried_forward_periods,
            "unexecutable_signal_bars": unexecutable_signal_bars,
            "long_periods": int(sum(1 for row in period_rows if row.net_exposure > 0.0)),
            "short_periods": int(sum(1 for row in period_rows if row.net_exposure < 0.0)),
            "flat_periods": int(sum(1 for row in period_rows if row.gross_exposure == 0.0)),
            "unexecuted_terminal_weights": terminal_weights,
            "minimum_annualization_periods": minimum_periods,
        },
        warning_codes=tuple(dict.fromkeys(warning_code_items)),
        warnings=tuple(dict.fromkeys(warning_items)),
        assumptions=_assumptions(execution_price=price_column, delay=delay),
        sample_role=sample_role,
    )


def _assumptions(*, execution_price: str, delay: int) -> tuple[str, ...]:
    return (
        "research_only_not_production_trading",
        f"target weights execute {delay} bar(s) after the signal bar at the {execution_price} price",
        "target_weight is a state: a bar absent from the position table carries the previous target book forward",
        f"period returns are {execution_price}-to-{execution_price} price relatives of the execution bars",
        "transaction costs are configurable research assumptions",
        "short borrow accrues on held short notional, de-annualized by periods_per_year",
        "the target book of the final bar is never established and carries no closing trade cost",
        "annualization spans every evaluated bar, including bars the book is flat",
    )

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

Terminal-bar settlement
-----------------------
An execution bar that EXISTS on the calendar establishes its target book, and
the LAST bar is an execution bar like any other. So when ``W[n-1]`` differs from
``W[n-2]`` the trade is executed at bar ``n-1``'s execution price and its
``sum_i |W[n-1][i] - W[n-2][i]|`` trade cost is charged; no holding or borrow
interval follows it (there is no bar ``n``), so its gross return, price
relatives and borrow accrual are all exactly zero. It is emitted as a period row
with ``is_terminal_settlement=True`` and ``entry_date == exit_date ==`` the last
bar, so the ``nav_series``/``period_rows`` alignment (one NAV row per period plus
the base) is unchanged and the cost is visible in the same series every other
cost is. A terminal bar whose book does NOT change trades nothing and emits no
settlement row.

A signal on one of the last ``delay`` bars has an execution bar BEYOND the
calendar and therefore executes nothing at all. Those books are disclosed in
full -- every leg, including the legs targeted flat, because "close this short"
is exactly as unexecuted as "open this long" -- via
``diagnostics["unexecuted_terminal_weights"]`` (the latest such book),
``diagnostics["unexecuted_signal_books"]`` (all of them) and
``diagnostics["unexecuted_signal_dates"]``.

Capital exhaustion (compounding terminates)
-------------------------------------------
A NAV step that lands at or below zero FREEZES that channel's NAV at exactly
``0.0``: the book has no capital left, so compounding terminates there. The
arithmetic recurrence ``nav[j+1] = nav[j] * (1 + return[j])`` therefore
describes every step STRICTLY BEFORE the exhaustion bar; the exhaustion bar
and everything after it follow FROZEN semantics instead -- the breach step
realizes exactly ``-100%`` of the capital that was left (whatever larger
arithmetic loss the book printed), and every later step moves nothing.

Both series are reported, and neither is dressed as the other:

* ``period_rows`` keep the RAW arithmetic period return, so the book's own
  ``-1.20`` bar stays legible, and the whole raw series is disclosed in
  ``diagnostics["raw_arithmetic_returns"]``;
* the series the SUMMARY METRICS consume is reconciled with the frozen NAV
  (``-1.0`` at the breach step, ``0.0`` after it), so a Sharpe is never
  computed off a loss the capital never took.

The exhaustion bar is named in ``diagnostics["gross_capital_exhausted_period"]``
/ ``["net_capital_exhausted_period"]`` with the ``CAPITAL_EXHAUSTED`` warning
code. Without the freeze a levered wipe-out carries a NEGATIVE NAV forward and a
subsequent LOSS raises it (``-0.20 * (1 - 0.20) = -0.16``), reporting a recovery
no book can make and a drawdown past ``-1.0``. With it, ``max_drawdown`` is
bounded below by ``-1.0`` structurally -- by the NAV series it reads, not by a
clamp on a second copy of the drawdown formula.

A NAV that is NOT FINITE is never read as a wipe-out. ``-inf <= 0.0`` is
``True``, so the freeze branch would otherwise report an equity nobody can know
as a clean bankruptcy and name an exhaustion period for it; a non-finite NAV is
propagated unchanged instead and reported by the metric tri-state below.

Fail-closed input gate
----------------------
``execution_price="open"`` against a panel with no ``open`` column raises
:class:`PositionSeriesInputError` (code ``EXECUTION_PRICE_COLUMN_UNAVAILABLE``);
it never falls back to ``close``. Likewise a non-finite or non-positive
execution price on a bar where the instrument carries a non-zero weight (or
trades at the terminal settlement) raises (``UNMARKABLE_HELD_POSITION``) instead
of imputing a return -- mapping an unavailable price to a flat position is the
caller's (data layer's) decision, made explicit as a ``0.0`` target weight.

The gate is TOTAL over every number that enters the math. ``trade_date`` is
parsed with an explicit ``format="ISO8601"`` (never an inferred format, which
can read the same column two different ways) and anything that does not parse --
on EITHER frame -- is ``INVALID_TRADE_DATE``. Every numeric field of the cost
model is checked finite and non-negative (``INVALID_COST_MODEL``):
``TransactionCostModel.__post_init__`` rejects negative rates, but ``NaN < 0``
and ``inf < 0`` are both ``False``, so a non-finite rate would otherwise reach
the cost math intact and turn every downstream return, NAV and metric into NaN.

Honest metrics
--------------
Summary metrics are :class:`~quant_forge.core.contracts.MetricValue` with the
kernel's tri-state vocabulary.

Their basis is ELAPSED TIME, not the row count. ``len(period_rows)`` counts the
terminal-settlement row too, and that row is a zero-length execution event
(``entry_date == exit_date``) that adds a cost step to the NAV without adding a
bar of exposure -- so ``holding_periods`` (``bar_count - 1``, also in
``diagnostics``) is what annualization exponentiates by AND what its
reportability gate compares, and the Sharpe sample is the holding rows alone.
Counting rows instead would let ``125`` held bars plus one settlement row clear
a ``126``-bar half-year gate the sample has not reached, and would mix a
zero-length cost row into a dispersion estimate of per-bar returns.

Annualized return is suppressed to ``None`` / ``insufficient_sample`` below the
same half-year basis the engine uses
(:data:`~quant_forge.backtesting.service.MIN_ANNUALIZATION_EXPOSURE_DAYS`,
rescaled to the configured ``periods_per_year`` and rounded UP, so a frequency
that lands mid-bar cannot report an annualization off a basis short of the half
year); Sharpe is suppressed below two holding periods or at zero dispersion. A
source series carrying a non-finite value suppresses every metric derived from
it to ``None`` / ``unavailable_source_series`` + ``NON_FINITE_RETURN_SERIES`` --
the same rule ``service.py``'s ``max_drawdown`` block applies -- and the FLAT
compatibility restatements of that same channel (``gross_cumulative_return`` /
``net_cumulative_return`` and the ``cost_reconciliation`` terminal equities) are
suppressed to ``None`` with it, so the summary surface can never hand back a
bare ``inf``/``NaN`` next to a metric that already declared the number unknown.
The raw evidence stays: the affected channel is named in
``diagnostics["non_finite_metric_source_series"]``, and ``period_rows`` /
``nav_series`` / ``diagnostics["raw_arithmetic_returns"]`` keep the series that
overflowed. No metric is ever faked as ``0.0``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from dataclasses import fields as dataclass_fields
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
    "CAPITAL_EXHAUSTED",
    "NON_FINITE_RETURN_SERIES",
    "POSITION_SERIES_ERROR_CODES",
    "POSITION_SERIES_ROLE",
    "SAME_PERIOD_EXECUTION",
    "TERMINAL_BAR_SETTLEMENT",
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
# EVERY field of TransactionCostModel, read off the dataclass itself rather than
# hand-listed, so a rate added upstream is gated the day it appears instead of
# slipping past a subset someone forgot to extend.
_COST_MODEL_FIELDS: tuple[str, ...] = tuple(item.name for item in dataclass_fields(TransactionCostModel))
# ISO 8601 is the ONLY accepted spelling of a trade date, stated explicitly so
# pandas never infers a format per call -- an inference that reads 03/04/2026 as
# March 4 or April 3 depending on the rest of the column, and silently reads a
# bare integer as a nanosecond epoch.
_TRADE_DATE_FORMAT = "ISO8601"

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
INVALID_COST_MODEL = "INVALID_COST_MODEL"
INVALID_TRADE_DATE = "INVALID_TRADE_DATE"

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
    INVALID_COST_MODEL,
    INVALID_TRADE_DATE,
)

# Disclosure codes. The two annualization/Sharpe codes are the ENGINE's own
# spellings (imported above), so a reader sees one vocabulary across both
# entries; the three below are new because the engine has no zero-delay mode,
# no terminal-bar settlement and no per-channel NAV floor to disclose.
SAME_PERIOD_EXECUTION = "SAME_PERIOD_EXECUTION"
TERMINAL_BAR_SETTLEMENT = "TERMINAL_BAR_SETTLEMENT"
CAPITAL_EXHAUSTED = "CAPITAL_EXHAUSTED"
NON_FINITE_RETURN_SERIES = "NON_FINITE_RETURN_SERIES"

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

    ``is_terminal_settlement`` marks the ZERO-LENGTH row that executes the last
    bar's target book (``entry_date == exit_date``, gross return / price
    relatives / borrow all exactly zero, trade cost only); see the module
    docstring. It is a cost event, not a bar of exposure, so it is excluded from
    the Sharpe sample and from the annualization basis while staying in this
    series and in ``nav_series``.

    ``gross_period_return`` / ``net_period_return`` are always the RAW
    arithmetic returns. At and after a capital-exhaustion bar they are no longer
    the return the capital took -- the NAV they carry is frozen at ``0.0``, so
    those returns move nothing -- and the summary metrics read a reconciled
    series instead (``-1.0`` at the breach bar, ``0.0`` after it; module
    docstring, "Capital exhaustion").
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
    is_terminal_settlement: bool = False


@dataclass(frozen=True)
class PositionSeriesBacktestResult:
    """Structured position-series backtest output.

    ``nav_series`` carries the ``1.0`` base at the first execution bar followed
    by one row per period, so it is always exactly one longer than
    ``period_rows`` and ``period_rows[j].net_nav == nav_series[j + 1]["net_nav"]``;
    ``period_rows`` is simultaneously the period-return series, the held-position
    series, and the per-period turnover series (one row carries all three), which
    keeps the three from ever drifting out of alignment. When the last bar
    settles a changed book (module docstring, "Terminal-bar settlement") that
    zero-length row is the final entry of both, so the last two ``nav_series``
    rows share the terminal ``trade_date``: the pre-trade mark and the
    post-trade mark of the same bar.

    ``gross_cumulative_return`` / ``net_cumulative_return`` and the
    ``cost_reconciliation`` terminal equities are the FLAT restatement of the
    two NAV channels, and they carry the same tri-state as the metrics computed
    off those channels: ``None`` when that channel's source series is
    non-finite (module docstring, "Honest metrics"), a real number otherwise.
    ``cost_reconciliation["period_transaction_cost_sum"]`` is never suppressed
    -- it is a sum of gated, finite cost terms.
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
    gross_cumulative_return: float | None
    net_cumulative_return: float | None
    traded_notional_total: float
    turnover_total: float
    turnover_mean: float
    trade_cost_total: float
    borrow_cost_total: float
    transaction_cost_total: float
    transaction_costs: TransactionCostModel
    metrics: dict[str, MetricValue]
    metric_provenance: dict[str, dict[str, object]] = field(default_factory=dict)
    cost_reconciliation: dict[str, float | None] = field(default_factory=dict)
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

    Rounded UP, never to nearest: a frequency whose half year lands mid-bar
    (``253`` bars/year -> ``126.5``, ``365`` -> ``182.5``) must require the bar
    that COMPLETES the half year, and ``round`` would hand back a basis one bar
    short of it -- ``round(126.5) == 126`` under banker's rounding, so the gate
    would pass a sample that has not reached the horizon it annualizes from.
    """

    scaled = periods_per_year * MIN_ANNUALIZATION_EXPOSURE_DAYS / TRADING_PERIODS_PER_YEAR
    return max(1, int(math.ceil(scaled)))


def _is_finite_non_negative(value: object) -> bool:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    return bool(np.isfinite(number)) and number >= 0.0


def _cost_model_field_names(costs: object) -> tuple[str, ...]:
    """Every field the GIVEN cost model carries, base contract included.

    Read off the object actually handed in, not off a hand-written list, so the
    gate covers a rate added to :class:`TransactionCostModel` upstream the day
    it appears AND a rate carried by a caller's extended model. The base
    contract's names are unioned in first so a duck-typed (non-dataclass) input
    is still checked against everything this module reads.
    """

    try:
        own = tuple(item.name for item in dataclass_fields(costs))  # type: ignore[arg-type]
    except TypeError:
        own = ()
    return tuple(dict.fromkeys(_COST_MODEL_FIELDS + own))


def _validated_cost_model(costs: TransactionCostModel) -> TransactionCostModel:
    """Total input gate over EVERY numeric field of the cost model.

    ``TransactionCostModel.__post_init__`` rejects negative rates, but ``NaN < 0``
    and ``inf < 0`` are both ``False``, so a non-finite rate constructs cleanly
    and would reach the cost math intact -- turning every trade cost, net period
    return, NAV and derived metric into NaN, with the fault surfacing far from
    where it entered. Naming it here keeps the failure structured and local.
    """

    offenders = [
        {"field": name, "value": repr(getattr(costs, name, None))}
        for name in _cost_model_field_names(costs)
        if not _is_finite_non_negative(getattr(costs, name, None))
    ]
    if offenders:
        raise PositionSeriesInputError(
            "every transaction-cost rate must be a finite, non-negative number of basis points; "
            "a cost that cannot be quoted is the caller's decision to express as an explicit 0.0",
            code=INVALID_COST_MODEL,
            details={"invalid_fields": offenders},
        )
    return costs


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
    normalized["trade_date"] = _parsed_trade_dates(normalized["trade_date"], label=label)
    normalized["instrument"] = normalized["instrument"].astype(str)
    return normalized


def _parsed_trade_dates(column: pd.Series, *, label: str) -> pd.Series:
    """Parse ``trade_date`` under an EXPLICIT format, on BOTH frames.

    A bare ``pd.to_datetime`` infers a format from whatever the column happens
    to contain, so the same calendar can parse two different ways on two
    different inputs (day-first vs month-first, integers as nanosecond epochs)
    and an unparsable value raises an untyped pandas error. Here the format is
    pinned to ISO 8601 and anything that does not parse -- including a null,
    which is a date the caller did not supply -- is a structured
    ``INVALID_TRADE_DATE`` naming the offending values.
    """

    try:
        parsed = pd.to_datetime(column, format=_TRADE_DATE_FORMAT)
    except (ValueError, TypeError) as exc:
        raise PositionSeriesInputError(
            f"{label}.trade_date must parse as ISO 8601 timestamps; no format is inferred",
            code=INVALID_TRADE_DATE,
            details={
                "frame": label,
                "format": _TRADE_DATE_FORMAT,
                "sample": _unparsable_trade_dates(column),
            },
        ) from exc
    missing = parsed.isna()
    if bool(missing.any()):
        raise PositionSeriesInputError(
            f"{label}.trade_date carries {int(missing.sum())} null date(s); every row must be dated",
            code=INVALID_TRADE_DATE,
            details={
                "frame": label,
                "format": _TRADE_DATE_FORMAT,
                "null_count": int(missing.sum()),
            },
        )
    return parsed


def _unparsable_trade_dates(column: pd.Series) -> list[str]:
    """A bounded sample of the values that failed to parse (best effort)."""

    try:
        coerced = pd.to_datetime(column, format=_TRADE_DATE_FORMAT, errors="coerce")
    except (ValueError, TypeError):
        return [repr(value) for value in column.head(_ERROR_SAMPLE_LIMIT).tolist()]
    offending = coerced.isna()
    return [repr(value) for value in column[offending].head(_ERROR_SAMPLE_LIMIT).tolist()]


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
    """Every bar that bounds a held position or executes a trade must carry a
    usable price.

    A weight is live over ``[j, j + 1]``, so both ends must be finite and
    strictly positive; otherwise the price relative is undefined and the honest
    answer is a structured failure, not an imputed return. The LAST bar is an
    execution bar too: a leg whose target changes there trades at that bar's
    price even though no interval follows, so it is required to be markable on
    the same terms.
    """

    live = np.zeros(price_matrix.shape, dtype=bool)
    last_bar = price_matrix.shape[0] - 1
    for bar_index in range(last_bar):
        carrying = held[bar_index] != 0.0
        live[bar_index] |= carrying
        live[bar_index + 1] |= carrying
    if last_bar >= 1:
        live[last_bar] |= held[last_bar] != held[last_bar - 1]
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
        f"a position is held across, or traded on, a bar with no usable {price_column!r} price "
        "(a finite, strictly positive quote is required at both ends of every holding interval "
        "and on every bar a leg's target changes)",
        code=UNMARKABLE_HELD_POSITION,
        details={"unmarkable_count": int(offending.sum()), "sample": sample},
    )


def _compounded(nav: float, period_return: float) -> float:
    """One NAV compounding step, with compounding TERMINATED at zero equity.

    An equity that reaches zero or below has no capital left to compound: the
    NAV is frozen at exactly ``0.0`` and every later step returns ``0.0``, so
    the series can never go negative and a later LOSS can never raise it
    (``-0.20 * (1 - 0.20) = -0.16`` is a recovery no book can make).

    A non-finite value is propagated untouched at BOTH ends of the step, never
    floored -- faking it as a wipe-out would hide the fault the metric tri-state
    exists to report:

    * a non-finite STEP (``nav`` finite, the product overflows) returns the
      overflowed product;
    * a non-finite INCOMING NAV short-circuits ahead of the freeze branch and is
      returned unchanged. This ordering is load-bearing: ``-inf <= 0.0`` is
      ``True``, so the freeze branch would otherwise convert an equity nobody
      can know into an exact ``0.0`` -- reporting a clean bankruptcy, naming an
      exhaustion period for it, and erasing the very non-finiteness the metric
      gate keys off. It would also let the sign of the next return decide
      whether the unknown reappears as ``+inf`` or ``-inf``.
    """

    if not np.isfinite(nav):
        return float(nav)
    if nav <= 0.0:
        return 0.0
    stepped = nav * (1.0 + period_return)
    if not np.isfinite(stepped):
        return float(stepped)
    return float(stepped) if stepped > 0.0 else 0.0


def _frozen_adjusted_returns(raw_returns: np.ndarray, *, exhausted_period: int | None) -> np.ndarray:
    """The raw arithmetic return series RECONCILED with the frozen NAV.

    ``_compounded`` freezes an exhausted channel at ``0.0``, so from the
    exhaustion bar on the arithmetic return the book printed is no longer the
    return the CAPITAL took: a ``-1.20`` bar cannot take more than the ``100%``
    that was there, and a ``-0.20`` bar after it takes nothing at all. Feeding
    the raw series to a dispersion statistic therefore measures a book that the
    NAV series says stopped existing.

    The reconciled series is what the summary metrics consume: unchanged before
    the exhaustion bar, exactly ``-1.0`` at it, and ``0.0`` after it -- so
    ``nav[j+1] == nav[j] * (1 + reconciled[j])`` holds across the WHOLE series,
    including the frozen tail. The raw series is preserved verbatim on the
    period rows and in ``diagnostics["raw_arithmetic_returns"]``.

    ``exhausted_period`` is a ``period_id``, which indexes ``period_rows``
    directly: the holding rows occupy ``0 .. bar_count - 2`` under their own bar
    index, and the terminal settlement occupies ``bar_count - 1`` under the
    terminal bar's. ``test_every_period_id_is_its_own_row_index`` pins that.
    """

    if exhausted_period is None:
        return raw_returns
    adjusted = raw_returns.copy()
    adjusted[exhausted_period] = -1.0
    adjusted[exhausted_period + 1 :] = 0.0
    return adjusted


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
            de-annualized by ``periods_per_year``. Every rate must be finite and
            non-negative (``INVALID_COST_MODEL``).
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

    costs = _validated_cost_model(transaction_costs or TransactionCostModel())
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
    gross_exhausted_period: int | None = None
    net_exhausted_period: int | None = None
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
        gross_nav = _compounded(gross_nav, gross_period_return)
        net_nav = _compounded(net_nav, net_period_return)
        if gross_nav == 0.0 and gross_exhausted_period is None:
            gross_exhausted_period = bar_index
        if net_nav == 0.0 and net_exhausted_period is None:
            net_exhausted_period = bar_index
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

    # Terminal-bar settlement. The last bar is an execution bar like any other:
    # a target book that differs from the one held into it IS established there
    # and pays its |dw| trade cost at that bar's execution price. What does NOT
    # follow it is an interval, so the gross return, the price relatives and the
    # borrow accrual of this row are all exactly zero -- and a terminal book
    # that does not change trades nothing, so no row is emitted at all.
    terminal_bar = bar_count - 1
    terminal_weights_vector = held[terminal_bar]
    terminal_delta = terminal_weights_vector - previous_weights
    terminal_traded_notional = float(np.abs(terminal_delta).sum())
    terminal_settlement_executed = terminal_traded_notional > 0.0
    if terminal_settlement_executed:
        terminal_trade_cost = float(terminal_traded_notional * trade_rate)
        terminal_net_return = -terminal_trade_cost
        net_nav = _compounded(net_nav, terminal_net_return)
        if net_nav == 0.0 and net_exhausted_period is None:
            net_exhausted_period = terminal_bar
        terminal_source_bar = held_sources[terminal_bar]
        terminal_is_carried = (
            terminal_source_bar is not None and terminal_source_bar < terminal_bar - delay
        )
        if terminal_is_carried:
            carried_forward_periods += 1
        period_rows.append(
            PositionSeriesPeriod(
                period_id=terminal_bar,
                signal_date=(
                    timeline[terminal_source_bar].date().isoformat()
                    if terminal_source_bar is not None
                    else None
                ),
                entry_date=timeline[terminal_bar].date().isoformat(),
                exit_date=timeline[terminal_bar].date().isoformat(),
                weights={
                    instrument: float(weight)
                    for instrument, weight in zip(instruments, terminal_weights_vector, strict=True)
                    if weight != 0.0
                },
                long_exposure=float(np.clip(terminal_weights_vector, 0.0, None).sum()),
                short_exposure=float(np.clip(-terminal_weights_vector, 0.0, None).sum()),
                gross_exposure=float(np.abs(terminal_weights_vector).sum()),
                net_exposure=float(terminal_weights_vector.sum()),
                price_relatives={},
                gross_period_return=0.0,
                net_period_return=terminal_net_return,
                traded_notional=terminal_traded_notional,
                turnover=terminal_traded_notional / 2.0,
                trade_cost=terminal_trade_cost,
                borrow_cost=0.0,
                transaction_cost=terminal_trade_cost,
                gross_nav=gross_nav,
                net_nav=net_nav,
                carried_forward=terminal_is_carried,
                is_terminal_settlement=True,
            )
        )
        nav_series.append(
            {
                "trade_date": timeline[terminal_bar].date().isoformat(),
                "gross_nav": gross_nav,
                "net_nav": net_nav,
            }
        )

    periods = len(period_rows)
    # ELAPSED TIME, not row count. ``periods`` counts the terminal-settlement
    # row, which is a zero-length execution event on the last bar: it adds a
    # cost step to the NAV but not one bar of exposure. Annualization
    # exponentiates by, and gates on, the holding basis instead -- 125 held bars
    # plus one settlement row is 126 ROWS but only 125 bars of elapsed time, and
    # counting rows would clear a 126-bar half-year gate the sample never
    # reached.
    holding_periods = bar_count - 1
    # ... and the Sharpe sample is the holding rows alone, for the same reason:
    # a zero-length row carries a pure cost, not a per-bar return, so mixing it
    # into a dispersion estimate measures an interval that does not exist.
    holding_rows_mask = np.array([not row.is_terminal_settlement for row in period_rows], dtype=bool)
    raw_returns_by_channel: dict[str, np.ndarray] = {
        "gross_nav": np.array([row.gross_period_return for row in period_rows], dtype=float),
        "net_nav": np.array([row.net_period_return for row in period_rows], dtype=float),
    }
    terminal_equity_by_channel: dict[str, float] = {"gross_nav": gross_nav, "net_nav": net_nav}
    exhausted_by_channel: dict[str, int | None] = {
        "gross_nav": gross_exhausted_period,
        "net_nav": net_exhausted_period,
    }
    minimum_periods = _minimum_annualization_periods(periods_per_year)

    metrics: dict[str, MetricValue] = {}
    non_finite_channels: list[str] = []
    # Flat compatibility restatements of each channel's terminal equity. They
    # are populated per channel BELOW rather than computed up front, because a
    # channel whose source series is non-finite must report them as ``None``
    # exactly like the metrics derived from the same series (a bare inf/NaN
    # cumulative return alongside a metric that already said "unknown" would be
    # the one place the tri-state leaks).
    cumulative_by_channel: dict[str, float | None] = {}
    for prefix, nav_key in (("", "gross_nav"), ("net_", "net_nav")):
        raw_returns = raw_returns_by_channel[nav_key]
        # The NAV base (1.0 at the first execution bar) is excluded here because
        # ``service._max_drawdown`` prepends its own 1.0 start; passing both
        # would double the base point without changing the result.
        drawdown_navs = np.array([float(row[nav_key]) for row in nav_series[1:]], dtype=float)
        cumulative = float(terminal_equity_by_channel[nav_key] - 1.0)
        # Reconciled with the frozen NAV before any statistic reads it: past an
        # exhaustion bar the arithmetic return is no longer the return the
        # capital took (see ``_frozen_adjusted_returns``).
        adjusted_returns = _frozen_adjusted_returns(
            raw_returns, exhausted_period=exhausted_by_channel[nav_key]
        )
        sharpe_sample = adjusted_returns[holding_rows_mask]
        sharpe_observations = int(sharpe_sample.size)
        # Tri-state gate on the SOURCE SERIES, ahead of every statistic derived
        # from it (annualized return, Sharpe, drawdown, terminal equity). A
        # non-finite return or NAV -- an overflow on an extreme book, say --
        # makes each of them unknowable, and the honest report is null + the
        # kernel's ``unavailable_source_series`` status, never a 0.0 standing in
        # for a number nobody has. The RAW returns are what is checked: they are
        # the series that actually entered the NAV.
        series_is_finite = bool(
            np.isfinite(raw_returns).all()
            and np.isfinite(drawdown_navs).all()
            and np.isfinite(cumulative)
        )
        cumulative_by_channel[nav_key] = cumulative if series_is_finite else None
        if not series_is_finite:
            non_finite_channels.append(nav_key)
            for suffix, unit, observations, minimum, method, source in (
                ("annualized_return", "return", holding_periods, minimum_periods,
                 "geometric_annualization_period_basis", "position_series_period_returns"),
                ("sharpe", "ratio", sharpe_observations, 2,
                 "period_return_mean_over_std_scaled", "position_series_period_returns"),
                ("max_drawdown", "return", periods, 1, "peak_to_trough_nav",
                 f"position_series_{nav_key}"),
            ):
                metrics[f"{prefix}{suffix}"] = MetricValue(
                    value=None,
                    unit=unit,
                    status="unavailable_source_series",
                    observation_count=observations,
                    minimum_required=minimum,
                    method=method,
                    source_series=source,
                    sample_role=sample_role,
                    warning_codes=(NON_FINITE_RETURN_SERIES,),
                )
            continue
        # Same reportability rule as ``service._annualization_metric``: the
        # value is suppressed below the half-year basis UNLESS the book was
        # wiped out, which is -100% annualized over any horizon. The
        # insufficient-history disclosure still fires on the short basis.
        reportable = holding_periods >= minimum_periods or (1.0 + cumulative) <= 0.0
        annualized = (
            _annualized_return_periodic(cumulative, holding_periods, periods_per_year)
            if reportable
            else None
        )
        annualized_warnings = (
            () if holding_periods >= minimum_periods else (INSUFFICIENT_ANNUALIZATION_HISTORY,)
        )
        metrics[f"{prefix}annualized_return"] = MetricValue(
            value=annualized,
            unit="return",
            status="available" if annualized is not None else "insufficient_sample",
            observation_count=holding_periods,
            minimum_required=minimum_periods,
            method="geometric_annualization_period_basis",
            source_series="position_series_period_returns",
            sample_role=sample_role,
            warning_codes=annualized_warnings,
        )
        sharpe = _sharpe_periodic(sharpe_sample, periods_per_year)
        metrics[f"{prefix}sharpe"] = MetricValue(
            value=sharpe,
            unit="ratio",
            status="available" if sharpe is not None else "insufficient_sample",
            observation_count=sharpe_observations,
            minimum_required=2,
            method="period_return_mean_over_std_scaled",
            source_series="position_series_period_returns",
            sample_role=sample_role,
            warning_codes=() if sharpe is not None else (INSUFFICIENT_SHARPE_OBSERVATIONS,),
        )
        # ``service._max_drawdown`` is reused verbatim -- there is no second copy
        # of the drawdown math here, and no clamp on top of it. The -1.0 floor is
        # STRUCTURAL: ``_compounded`` never lets a NAV go below 0.0, so the worst
        # ratio the engine's own formula can return off this series is
        # ``0.0 / peak - 1 == -1.0``. Status follows ``service.py``'s own
        # convention for this metric: available when a value exists, otherwise
        # ``unavailable_source_series`` (taken above).
        max_drawdown = float(_max_drawdown(drawdown_navs)) if len(drawdown_navs) else None
        metrics[f"{prefix}max_drawdown"] = MetricValue(
            value=max_drawdown,
            unit="return",
            status="available" if max_drawdown is not None else "unavailable_source_series",
            observation_count=periods,
            minimum_required=1,
            method="peak_to_trough_nav",
            source_series=f"position_series_{nav_key}",
            sample_role=sample_role,
            warning_codes=() if max_drawdown is not None else (NON_FINITE_RETURN_SERIES,),
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
    if terminal_settlement_executed:
        warning_code_items.append(TERMINAL_BAR_SETTLEMENT)
        warning_items.append(
            f"the target book of the final bar differs from the one held into it, so it was ESTABLISHED "
            f"there: {terminal_traded_notional:.10g} of traded notional charged at that bar's "
            f"{price_column} price, with no holding or borrow interval following it"
        )
    for channel, exhausted_period in (("gross", gross_exhausted_period), ("net", net_exhausted_period)):
        if exhausted_period is None:
            continue
        if CAPITAL_EXHAUSTED not in warning_code_items:
            warning_code_items.append(CAPITAL_EXHAUSTED)
        warning_items.append(
            f"{channel} equity reached zero at period {exhausted_period}; compounding terminates there and "
            f"{channel}_nav is frozen at 0.0, so every later period return is reported but moves no capital"
        )
    if non_finite_channels:
        warning_items.append(
            "a non-finite value entered the "
            + ", ".join(sorted(non_finite_channels))
            + " series, so every statistic derived from it -- including the flat cumulative-return and "
            "terminal-equity restatements -- is reported as null + unavailable_source_series rather than "
            "a fabricated number; the series itself is preserved in period_rows / nav_series / "
            "diagnostics['raw_arithmetic_returns'] as the evidence of what overflowed"
        )
    # A signal on one of the last `delay` bars has no execution bar on this
    # calendar at all, so it can move nothing. Disclosed rather than silently
    # dropped -- and disclosed IN FULL: every leg of every such book, including
    # the legs targeted flat, because "close this short" is exactly as
    # unexecuted as "open this long" and a non-zero filter would hide half of
    # what did not happen.
    unexecutable_slots = [
        slot for slot, position in enumerate(signal_positions) if int(position) + delay > bar_count - 1
    ]
    unexecutable_signal_bars = len(unexecutable_slots)
    unexecuted_signal_dates = tuple(
        timeline[int(signal_positions[slot])].date().isoformat() for slot in unexecutable_slots
    )
    unexecuted_signal_books = tuple(
        {
            "trade_date": timeline[int(signal_positions[slot])].date().isoformat(),
            "target_weights": {
                instrument: float(weight)
                for instrument, weight in zip(instruments, signal_weights[slot], strict=True)
            },
        }
        for slot in unexecutable_slots
    )
    terminal_weights: dict[str, float] = (
        dict(unexecuted_signal_books[-1]["target_weights"])  # type: ignore[arg-type]
        if unexecuted_signal_books
        else {}
    )
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
        gross_cumulative_return=cumulative_by_channel["gross_nav"],
        net_cumulative_return=cumulative_by_channel["net_nav"],
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
            # The cost sum is always a real number (every rate passed the finite
            # gate and every weight is finite), so it is never suppressed. The
            # two terminal equities restate the NAV channels, so they follow
            # exactly the suppression their own channel's metrics took.
            "period_transaction_cost_sum": float(trade_cost_total + borrow_cost_total),
            "gross_terminal_equity": (
                float(gross_nav) if cumulative_by_channel["gross_nav"] is not None else None
            ),
            "net_terminal_equity": (
                float(net_nav) if cumulative_by_channel["net_nav"] is not None else None
            ),
        },
        diagnostics={
            "bar_count": bar_count,
            "signal_bar_count": int(len(signal_positions)),
            "holding_periods": holding_periods,
            "carried_forward_periods": carried_forward_periods,
            "unexecutable_signal_bars": unexecutable_signal_bars,
            "long_periods": int(sum(1 for row in period_rows if row.net_exposure > 0.0)),
            "short_periods": int(sum(1 for row in period_rows if row.net_exposure < 0.0)),
            "flat_periods": int(sum(1 for row in period_rows if row.gross_exposure == 0.0)),
            # The COMPLETE target book(s) that never reached an execution bar --
            # every leg, zero-weight legs included. ``unexecuted_terminal_weights``
            # is the latest of them (the book that would ultimately have been
            # held); ``unexecuted_signal_books`` carries all of them.
            "unexecuted_terminal_weights": terminal_weights,
            "unexecuted_signal_books": unexecuted_signal_books,
            "unexecuted_signal_dates": unexecuted_signal_dates,
            "terminal_settlement_executed": terminal_settlement_executed,
            "terminal_settlement_traded_notional": terminal_traded_notional,
            "gross_capital_exhausted_period": gross_exhausted_period,
            "net_capital_exhausted_period": net_exhausted_period,
            # The UNRECONCILED arithmetic period returns, whole, per channel --
            # the same numbers the rows carry, gathered as a series so the
            # frozen-NAV reconciliation the summary metrics consume can be
            # compared against what the book actually printed (see
            # ``_frozen_adjusted_returns``). Past an exhaustion bar the two
            # differ by construction; everywhere else they are identical.
            "raw_arithmetic_returns": {
                "gross": tuple(float(value) for value in raw_returns_by_channel["gross_nav"]),
                "net": tuple(float(value) for value in raw_returns_by_channel["net_nav"]),
            },
            "non_finite_metric_source_series": tuple(non_finite_channels),
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
        "a target book whose execution bar exists on the calendar is established there, the FINAL bar "
        "included: its |dw| trade cost is charged at that bar's execution price and no holding or borrow "
        "interval follows it",
        "a signal whose execution bar falls beyond the calendar establishes nothing; its complete target "
        "book is disclosed in diagnostics rather than charged or held",
        "equity at or below zero terminates compounding: NAV is frozen at 0.0 from that period on, and "
        "the return series the summary metrics consume is reconciled with it (-1.0 at the breach period, "
        "0.0 after it) while the rows keep the raw arithmetic return",
        "annualization and the Sharpe sample span the HOLDING bars -- every evaluated bar including the "
        "bars the book is flat, but not the zero-length terminal settlement, which is a cost event",
    )

"""Regression tests: backtest segment attribution must not cross split boundaries (A-P1-2).

A period attributed to a segment must be fully realized before the next
segment's first signal date; otherwise its return uses prices from the later
segment's window (backtest mirror of the evaluation-side purge/embargo).
"""

from __future__ import annotations

from quant_forge.backtesting.service import (
    SEGMENT_BOUNDARY_PURGED,
    _segment_metrics,
    _split_rows_by_signal_date,
)
from quant_forge.core.contracts import SampleSplitSpec


SPLITS = (
    SampleSplitSpec(name="IS", fraction=0.5, score_weight=1.0),
    SampleSplitSpec(name="OOS1", fraction=0.5, score_weight=0.0),
)


def _period_row(signal_date: str, exit_date: str, gross: float = 0.02, net: float = 0.015) -> dict[str, object]:
    return {
        "signal_date": signal_date,
        "entry_date": signal_date,
        "exit_date": exit_date,
        "gross_period_return": gross,
        "net_period_return": net,
    }


def test_boundary_crossing_periods_are_purged_from_earlier_segment() -> None:
    # Six signals split 50/50: IS = {01-01, 01-08, 01-15}, OOS1 starts 2024-01-22.
    rows = [
        _period_row("2024-01-01", "2024-01-08"),
        _period_row("2024-01-08", "2024-01-22", gross=0.5, net=0.49),
        _period_row("2024-01-15", "2024-01-23", gross=0.4, net=0.39),
        _period_row("2024-01-22", "2024-01-29"),
        _period_row("2024-01-29", "2024-02-05"),
        _period_row("2024-02-05", "2024-02-12"),
    ]
    metrics = _segment_metrics(rows, 5, SPLITS)
    is_metric, oos_metric = metrics
    # The 01-08 period exits ON the boundary and the 01-15 period exits inside
    # OOS1; both must leave the IS segment so IS never realizes OOS prices.
    assert is_metric.periods == 1
    assert abs(is_metric.gross_cumulative_return - 0.02) < 1e-12
    assert SEGMENT_BOUNDARY_PURGED in is_metric.warning_codes
    # Purged periods are excluded, not reassigned: OOS1 keeps only its own.
    assert oos_metric.periods == 3
    assert oos_metric.start_date == "2024-01-22"
    assert SEGMENT_BOUNDARY_PURGED not in oos_metric.warning_codes


def test_fully_realized_periods_keep_previous_attribution() -> None:
    rows = [
        _period_row("2024-01-01", "2024-01-08"),
        _period_row("2024-01-08", "2024-01-15"),
        _period_row("2024-01-15", "2024-01-19"),
        _period_row("2024-01-22", "2024-01-29"),
        _period_row("2024-01-29", "2024-02-05"),
        _period_row("2024-02-05", "2024-02-12"),
    ]
    segments = _split_rows_by_signal_date(rows, SPLITS)
    assert [len(segment) for segment in segments] == [3, 3]
    metrics = _segment_metrics(rows, 5, SPLITS)
    assert metrics[0].periods == 3
    assert metrics[0].warning_codes == ()
    assert sum(metric.periods for metric in metrics) == len(rows)

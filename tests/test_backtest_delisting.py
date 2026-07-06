"""Regression tests: names lost before scheduled exit realize their last mark (A-P1-1).

Old behavior conditioned formation on exit-date price availability (future
information at entry time) and let mid-period disappearances drop out of the
present-subset NAV mean, so delisting losses never realized.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from quant_forge.backtesting.service import _leg_cumulative_returns, _with_period_return


def _close_frame(rows: list[tuple[str, str, float]]) -> pd.DataFrame:
    frame = pd.DataFrame(rows, columns=["trade_date", "instrument", "close"])
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    return frame


def test_formation_keeps_names_missing_at_exit_and_realizes_last_mark() -> None:
    close = _close_frame(
        [
            ("2024-01-02", "AAA", 10.0),
            ("2024-01-03", "AAA", 10.5),
            ("2024-01-04", "AAA", 11.0),
            ("2024-01-02", "BBB", 20.0),
            ("2024-01-03", "BBB", 19.0),
            ("2024-01-04", "BBB", 18.0),
            ("2024-01-02", "CCC", 30.0),
            ("2024-01-03", "CCC", 18.0),
        ]
    )
    result = _with_period_return(close, pd.Timestamp("2024-01-02"), pd.Timestamp("2024-01-04"))
    by_name = result.set_index("instrument")
    assert set(by_name.index) == {"AAA", "BBB", "CCC"}
    assert abs(float(by_name.loc["CCC", "period_return"]) - (-0.4)) < 1e-12
    assert bool(by_name.loc["CCC", "position_lost"]) is True
    assert bool(by_name.loc["AAA", "position_lost"]) is False
    assert abs(float(by_name.loc["AAA", "period_return"]) - 0.1) < 1e-12


def test_name_with_no_post_entry_quotes_realizes_zero_and_is_lost() -> None:
    close = _close_frame(
        [
            ("2024-01-02", "AAA", 10.0),
            ("2024-01-03", "AAA", 10.0),
            ("2024-01-04", "AAA", 10.0),
            ("2024-01-02", "DDD", 50.0),
        ]
    )
    result = _with_period_return(close, pd.Timestamp("2024-01-02"), pd.Timestamp("2024-01-04")).set_index("instrument")
    assert abs(float(result.loc["DDD", "period_return"])) < 1e-12
    assert bool(result.loc["DDD", "position_lost"]) is True


def test_leg_nav_carries_frozen_mark_for_partially_absent_names() -> None:
    close = _close_frame(
        [
            ("2024-01-02", "AAA", 10.0),
            ("2024-01-03", "AAA", 11.0),
            ("2024-01-04", "AAA", 12.0),
            ("2024-01-02", "CCC", 30.0),
            ("2024-01-03", "CCC", 15.0),
        ]
    )
    dates = [pd.Timestamp("2024-01-02"), pd.Timestamp("2024-01-03"), pd.Timestamp("2024-01-04")]
    returns = _leg_cumulative_returns(close, {"AAA", "CCC"}, dates[0], dates)
    # Day 3: AAA at +20%, CCC frozen at -50% -> leg mark -15%. The old
    # present-subset mean jumped to +20%, silently erasing CCC's loss.
    assert abs(returns[2] - (-0.15)) < 1e-12


def test_leg_nav_still_unmarkable_when_no_held_name_quotes() -> None:
    close = _close_frame(
        [
            ("2024-01-02", "CCC", 30.0),
            ("2024-01-03", "CCC", 15.0),
            ("2024-01-04", "ZZZ", 1.0),
        ]
    )
    dates = [pd.Timestamp("2024-01-02"), pd.Timestamp("2024-01-03"), pd.Timestamp("2024-01-04")]
    returns = _leg_cumulative_returns(close, {"CCC"}, dates[0], dates)
    assert np.isnan(returns[2])

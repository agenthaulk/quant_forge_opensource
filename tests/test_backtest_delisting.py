"""Regression tests: names lost before scheduled exit realize their last mark (A-P1-1).

Old behavior conditioned formation on exit-date price availability (future
information at entry time) and let mid-period disappearances drop out of the
present-subset NAV mean, so delisting losses never realized.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from quant_forge.backtesting.service import (
    POSITIONS_LOST_BEFORE_EXIT,
    _aggregate_staggered_nav,
    _daily_ledger_from_nav,
    _leg_cumulative_returns,
    _with_period_return,
    run_factor_backtest,
)
from quant_forge.data.local import PANEL_FILE, create_demo_workspace


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


def test_run_factor_backtest_reports_lost_positions_on_gapped_panel(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    panel_path = paths["data_root"] / PANEL_FILE
    panel = pd.read_parquet(panel_path)
    dates = sorted(panel["trade_date"].unique())
    victim = panel.groupby("instrument")["market_cap"].mean().sort_values().index[0]
    # Default schedule (delay=1, holding=5) enters/exits on indices ≡ 1 mod 5;
    # keep the cutoff off that grid so the gap opens strictly mid-period.
    cut_index = len(dates) * 2 // 3
    if cut_index % 5 == 1:
        cut_index += 1
    cutoff = dates[cut_index]
    gapped = panel[~((panel["instrument"] == victim) & (panel["trade_date"] > cutoff))]
    gapped.to_parquet(panel_path, index=False)

    result = run_factor_backtest(
        "FTR_DEMO_SMALL_CAP",
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"],
    )

    assert result.lost_positions >= 1
    assert POSITIONS_LOST_BEFORE_EXIT in result.warning_codes

    def _reject_constant(constant: str) -> None:
        raise AssertionError(f"non-finite JSON constant {constant}")

    payload = json.loads(result.artifact_path.read_text(encoding="utf-8"), parse_constant=_reject_constant)
    assert payload["lost_positions"] == result.lost_positions


def test_daily_ledger_skips_unmarkable_days() -> None:
    daily_nav = [
        {"date": "2024-01-02", "gross_nav": 1.01, "net_nav": 1.0, "period_index": 0},
        {"date": "2024-01-03", "gross_nav": float("nan"), "net_nav": float("nan"), "period_index": 0},
        {"date": "2024-01-04", "gross_nav": 1.02, "net_nav": 1.01, "period_index": 0},
    ]
    ledger = _daily_ledger_from_nav(daily_nav)
    assert [row["trade_date"] for row in ledger] == ["2024-01-02", "2024-01-04"]
    assert all(np.isfinite(float(row["daily_net_return"])) for row in ledger)


def test_staggered_aggregate_ignores_non_finite_cohort_marks() -> None:
    dates = [pd.Timestamp("2024-01-02"), pd.Timestamp("2024-01-03"), pd.Timestamp("2024-01-04")]
    cohorts = [
        {
            "capital_weight": 1.0,
            "daily_nav": [
                {"date": "2024-01-02", "net_nav": 1.01},
                {"date": "2024-01-03", "net_nav": float("nan")},
                {"date": "2024-01-04", "net_nav": 1.03},
            ],
        }
    ]
    rows = _aggregate_staggered_nav(dates, cohorts)
    assert [round(float(row["net_nav"]), 6) for row in rows] == [1.01, 1.01, 1.03]
    assert all(np.isfinite(float(row["daily_net_return"])) for row in rows)

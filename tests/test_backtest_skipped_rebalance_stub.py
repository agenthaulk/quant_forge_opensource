"""RB-7: skipped rebalances are flat-flagged ledger stubs, never silent gaps.

A scheduled rebalance whose cross-section is empty (no scored names) or too
thin to split emits an explicit stub row in resolved_schedule/rebalance_ledger
with a REBALANCE_SKIPPED_* code, keeps NAV flat across the gap, carries the
previous book forward as the turnover reference for the next traded period,
and stays out of every period-return statistic and completed-period count.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from quant_forge.apps.web.api import _backtest_payload, _json_safe
from quant_forge.backtesting.service import (
    REBALANCE_SKIPPED_NO_COVERAGE,
    REBALANCE_SKIPPED_THIN,
    run_factor_backtest,
)
from quant_forge.core.contracts import FactorDefinition
from quant_forge.factor_library.repository import FactorRepository

FACTOR_ID = "FTR_SKIP_PROBE"
INSTRUMENTS = ("SSS01", "SSS02", "SSS03", "SSS04", "SSS05")


def _skip_workspace(
    root: Path,
    *,
    all_st_day_indices: tuple[int, ...] = (),
    single_st_day_indices: tuple[int, ...] = (),
    days: int = 30,
) -> tuple[dict[str, Path], list[pd.Timestamp]]:
    """Five-instrument panel; is_st toggles create formation-coverage gaps.

    An all-st day removes every scored name at that signal date (empty
    cross-section). A single-st day removes one name, dropping the matched
    cross-section to 4 < max(4, group_count=5) (thin cross-section). Prices
    stay fully populated so period returns and NAV marks are undisturbed.
    """

    data_root = root / "data"
    factor_root = root / "factor_root"
    data_root.mkdir(parents=True)
    dates = list(pd.bdate_range("2026-01-05", periods=days))
    rows: list[dict[str, object]] = []
    for index, instrument in enumerate(INSTRUMENTS):
        for day_index, trade_date in enumerate(dates):
            is_st = day_index in all_st_day_indices or (
                index == 0 and day_index in single_st_day_indices
            )
            rows.append(
                {
                    "trade_date": trade_date,
                    "instrument": instrument,
                    "close": 10.0 + index + 0.011 * day_index * (index + 1),
                    "market_cap": 1_000_000_000.0 * (index + 1),
                    "is_st": bool(is_st),
                }
            )
    pd.DataFrame(rows).to_parquet(data_root / "panel.parquet", index=False)
    FactorRepository(factor_root).save(
        FactorDefinition(
            factor_id=FACTOR_ID,
            name="skip_probe",
            formula="-rank(market_cap)",
            horizon_days=2,
            universe_filters=("is_st == false",),
        )
    )
    return {"data_root": data_root, "factor_root": factor_root}, dates


def _run(paths: dict[str, Path], artifact_root: Path):
    return run_factor_backtest(
        FACTOR_ID,
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=artifact_root,
        holding_days=2,
    )


def test_coverage_gaps_emit_flat_ledger_stubs_with_codes(tmp_path: Path) -> None:
    # Grid: delay=1, holding=2 over 30 dates -> signal indices 0,2,...,26.
    # Index 8 (all st) -> empty cross-section; index 12 (one st name) -> thin.
    paths, dates = _skip_workspace(
        tmp_path, all_st_day_indices=(8,), single_st_day_indices=(12,)
    )
    result = _run(paths, tmp_path / "artifacts")
    payload = json.loads(result.artifact_path.read_text(encoding="utf-8"))

    def label(index: int) -> str:
        return dates[index].date().isoformat()

    # Counts and codes are exposed, never silent.
    assert result.skipped_rebalances == 2
    assert payload["skipped_rebalances"] == 2
    assert payload["skipped_rebalance_dates"] == [label(8), label(12)]
    assert REBALANCE_SKIPPED_NO_COVERAGE in payload["warning_codes"]
    assert REBALANCE_SKIPPED_THIN in payload["warning_codes"]
    assert REBALANCE_SKIPPED_NO_COVERAGE in result.warning_codes
    assert REBALANCE_SKIPPED_THIN in result.warning_codes

    # The skipped slots are visible in the resolved schedule with skip marking.
    schedule = payload["resolved_schedule"]
    assert len(schedule) == payload["periods"] + 2 == 14
    stubs = {row["signal_date"]: row for row in schedule if row["skipped"]}
    assert set(stubs) == {label(8), label(12)}
    assert stubs[label(8)]["skip_reason"] == REBALANCE_SKIPPED_NO_COVERAGE
    assert stubs[label(12)]["skip_reason"] == REBALANCE_SKIPPED_THIN
    for stub in stubs.values():
        assert stub["period_id"] is None
        assert stub["is_complete_period"] is False
        assert stub["is_partial_final_period"] is False
        assert stub["trading_days_held"] == 0
    assert [row["signal_date"] for row in schedule] == [label(i) for i in range(0, 27, 2)]
    traded_ids = [row["period_id"] for row in schedule if not row["skipped"]]
    assert traded_ids == list(range(12))

    # The ledger stub is flat: zero returns, no positions, no executed trade.
    ledger_stubs = [row for row in payload["rebalance_ledger"] if row["skipped"]]
    assert len(ledger_stubs) == 2
    for stub in ledger_stubs:
        assert stub["long_count"] == 0
        assert stub["short_count"] == 0
        assert stub["gross_period_return"] == 0.0
        assert stub["net_period_return"] == 0.0
        assert stub["transaction_cost"] == 0.0
        assert stub["turnover_rate"] == 0.0
        assert stub["rebalance_turnover"] is None
        assert stub["initial_build_turnover"] is None

    # NAV stays flat across each gap: the uncovered day has no NAV row and the
    # first mark after the gap compounds off the pre-gap base unchanged.
    nav_by_date = {row["date"]: row for row in payload["daily_nav"]}
    for gap_day, before_day, after_day in ((10, 9, 11), (14, 13, 15)):
        assert label(gap_day) not in nav_by_date
        before = nav_by_date[label(before_day)]
        after = nav_by_date[label(after_day)]
        assert after["gross_nav"] == pytest.approx(
            before["gross_nav"] * after["gross_period_nav"]
        )
        assert after["net_nav"] == pytest.approx(before["net_nav"] * after["net_period_nav"])

    # The previous book carries forward as the turnover reference: with a
    # static cross-section the post-gap rebalance trades nothing (turnover 0),
    # and it is a rebalance observation, never a fresh initial build.
    ledger_by_date = {row["signal_date"]: row for row in payload["rebalance_ledger"]}
    for post_gap_index in (10, 14):
        row = ledger_by_date[label(post_gap_index)]
        assert row["initial_build_turnover"] is None
        assert row["rebalance_turnover"] == pytest.approx(0.0)
    assert ledger_by_date[label(0)]["initial_build_turnover"] is not None

    # Stub rows never enter the metric series or period counts.
    assert payload["periods"] == 12
    assert payload["completed_periods"] == 12
    assert payload["partial_periods"] == 0
    assert len(payload["period_returns"]) == 12
    assert payload["exposure_days"] == 24
    assert payload["metrics"]["annualized_volatility"]["observation_count"] == 12
    gross_returns = np.array(
        [row["gross_period_return"] for row in payload["period_returns"]], dtype=float
    )
    assert payload["gross_annualized_volatility"] == pytest.approx(
        float(np.std(gross_returns, ddof=1) * np.sqrt(252 / 2))
    )
    assert sum(row["is_complete_period"] for row in schedule) == payload["completed_periods"]


def test_gap_before_first_build_keeps_initial_build_on_first_traded_period(
    tmp_path: Path,
) -> None:
    paths, dates = _skip_workspace(tmp_path, all_st_day_indices=(0,))
    result = _run(paths, tmp_path / "artifacts")
    payload = json.loads(result.artifact_path.read_text(encoding="utf-8"))

    assert payload["skipped_rebalances"] == 1
    schedule = payload["resolved_schedule"]
    assert schedule[0]["skipped"] is True
    assert schedule[0]["skip_reason"] == REBALANCE_SKIPPED_NO_COVERAGE
    assert schedule[0]["period_id"] is None
    assert schedule[1]["skipped"] is False
    assert schedule[1]["period_id"] == 0
    first = payload["period_returns"][0]
    assert first["signal_date"] == dates[2].date().isoformat()
    assert first["initial_build_turnover"] is not None
    # No NAV rows exist before the first traded entry; the base starts at 1.
    assert payload["daily_nav"][0]["date"] == dates[3].date().isoformat()
    assert payload["daily_nav"][0]["gross_nav"] == pytest.approx(
        payload["daily_nav"][0]["gross_period_nav"]
    )


def test_skip_stubs_serialize_through_web_payload_helpers(tmp_path: Path) -> None:
    paths, _dates = _skip_workspace(
        tmp_path, all_st_day_indices=(8,), single_st_day_indices=(12,)
    )
    result = _run(paths, tmp_path / "artifacts")

    payload = _backtest_payload(result)
    assert payload["skipped_rebalances"] == 2
    assert REBALANCE_SKIPPED_NO_COVERAGE in payload["warning_codes"]
    assert REBALANCE_SKIPPED_THIN in payload["warning_codes"]
    # The full payload and the raw ledger/schedule survive the JSON encoder the
    # web layer uses (stub rows contain only JSON-safe scalar values).
    encoded = json.dumps(_json_safe(payload))
    assert "skipped_rebalances" in encoded
    ledger = json.loads(json.dumps(_json_safe(result.rebalance_ledger)))
    stub_rows = [row for row in ledger if row["skipped"]]
    assert {row["skip_reason"] for row in stub_rows} == {
        REBALANCE_SKIPPED_NO_COVERAGE,
        REBALANCE_SKIPPED_THIN,
    }
    schedule = json.loads(json.dumps(_json_safe(result.resolved_schedule)))
    assert sum(1 for row in schedule if row["skipped"]) == 2

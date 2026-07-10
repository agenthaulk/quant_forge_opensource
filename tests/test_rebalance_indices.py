"""RB-5: the shared rebalance grid helper matches the engine's realized schedule.

`rebalance_indices` is the single source of truth for the non-overlapping
rebalance grid. These tests lock (a) the helper's exact arithmetic against the
reference expression and (b) grid fidelity: across delay/holding/start
permutations, the engine's realized `resolved_schedule` signal dates equal the
dates selected by the helper (assertion-style parity, kept out of the hot path
by design).
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest
from quant_forge.backtesting.service import (
    _resolve_start_signal_index,
    rebalance_indices,
    run_factor_backtest,
)
from quant_forge.core.contracts import FactorDefinition, SimulationProfile
from quant_forge.factor_library.repository import FactorRepository

FACTOR_ID = "FTR_GRID_PROBE"


def _grid_workspace(root: Path, *, days: int = 40, instruments: int = 8) -> dict[str, Path]:
    data_root = root / "data"
    factor_root = root / "factor_root"
    data_root.mkdir(parents=True)
    dates = pd.bdate_range("2026-01-05", periods=days)
    rows: list[dict[str, object]] = []
    for index in range(instruments):
        for day_index, trade_date in enumerate(dates):
            rows.append(
                {
                    "trade_date": trade_date,
                    "instrument": f"STK{index:03d}",
                    # Distinct closes so scores are unique (no tie effects here).
                    "close": 10.0 + index + 0.01 * day_index * (index + 1),
                    "market_cap": 1_000_000_000.0 * (index + 1),
                    "is_st": False,
                }
            )
    pd.DataFrame(rows).to_parquet(data_root / "panel.parquet", index=False)
    FactorRepository(factor_root).save(
        FactorDefinition(
            factor_id=FACTOR_ID,
            name="grid_probe",
            formula="rank(close)",
            horizon_days=5,
        )
    )
    return {"data_root": data_root, "factor_root": factor_root}


def test_rebalance_indices_matches_reference_expression() -> None:
    # The helper is pinned to the engine's historical inline expression.
    for total in (0, 1, 2, 3, 7, 40, 161):
        dates = list(range(total))
        for delay in (1, 2, 3):
            for holding in (1, 2, 5, 21):
                for start in (0, 1, 5):
                    assert rebalance_indices(
                        dates, delay=delay, holding=holding, start_signal_index=start
                    ) == list(range(start, total - delay - 1, holding))


def test_rebalance_indices_is_empty_when_window_cannot_hold_a_period() -> None:
    assert rebalance_indices([], delay=1, holding=5, start_signal_index=0) == []
    assert rebalance_indices([1, 2], delay=1, holding=5, start_signal_index=0) == []
    assert rebalance_indices(list(range(10)), delay=1, holding=5, start_signal_index=9) == []


@pytest.mark.parametrize("delay", [1, 2])
@pytest.mark.parametrize("holding", [1, 3, 5])
@pytest.mark.parametrize("first_signal_offset", [None, 4])
def test_engine_realized_signal_dates_equal_shared_grid(
    tmp_path: Path, delay: int, holding: int, first_signal_offset: int | None
) -> None:
    paths = _grid_workspace(tmp_path)
    panel = pd.read_parquet(paths["data_root"] / "panel.parquet")
    dates = sorted(pd.to_datetime(panel["trade_date"]).drop_duplicates())
    first_signal_date = (
        dates[first_signal_offset].date().isoformat() if first_signal_offset is not None else None
    )
    start_index = _resolve_start_signal_index(dates, first_signal_date)
    grid = rebalance_indices(dates, delay=delay, holding=holding, start_signal_index=start_index)
    grid_labels = [dates[index].date().isoformat() for index in grid]

    # Mark-to-market tail included: every grid slot trades, so the realized
    # schedule must equal the shared grid exactly.
    included = run_factor_backtest(
        FACTOR_ID,
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=tmp_path / "artifacts_included",
        holding_days=holding,
        simulation_profile=SimulationProfile(execution_delay_days=delay),
        first_signal_date=first_signal_date,
        include_partial_final_period=True,
    )
    included_payload = json.loads(included.artifact_path.read_text(encoding="utf-8"))
    realized = [row["signal_date"] for row in included_payload["resolved_schedule"]]
    assert realized == grid_labels
    assert not any(row["skipped"] for row in included_payload["resolved_schedule"])

    # Default D3 exclusion: the realized schedule is the completeness-filtered
    # prefix of the same shared grid, never an independently derived schedule.
    excluded = run_factor_backtest(
        FACTOR_ID,
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=tmp_path / "artifacts_excluded",
        holding_days=holding,
        simulation_profile=SimulationProfile(execution_delay_days=delay),
        first_signal_date=first_signal_date,
    )
    excluded_payload = json.loads(excluded.artifact_path.read_text(encoding="utf-8"))
    complete_labels = [
        dates[index].date().isoformat()
        for index in grid
        if index + delay + holding < len(dates)
    ]
    assert [row["signal_date"] for row in excluded_payload["resolved_schedule"]] == complete_labels

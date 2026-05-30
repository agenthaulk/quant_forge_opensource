from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from quant_forge.backtesting.service import _max_drawdown, run_factor_backtest
from quant_forge.core.contracts import SimulationProfile
from quant_forge.data.local import create_demo_workspace


def test_backtest_uses_next_day_execution(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    result = run_factor_backtest(
        "FTR_DEMO_SMALL_CAP",
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"],
    )
    payload = json.loads(result.artifact_path.read_text(encoding="utf-8"))
    first = payload["period_returns"][0]
    assert first["signal_date"] < first["entry_date"] < first["exit_date"]
    assert payload["execution"] == "signal_date plus one trading day; non-overlapping holding periods"
    assert payload["holding_days"] == 5
    assert payload["simulation_profile"]["execution_delay_days"] == 1
    assert result.holding_days == 5
    assert -1.0 < payload["max_drawdown"] <= 0.0
    assert payload["cumulative_return"] < 10.0
    assert "long_short_sharpe" in payload
    assert "average_turnover" in payload
    assert len(payload["group_returns"]) == 5
    assert result.group_returns


def test_backtest_can_run_one_day_holding_path(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    result = run_factor_backtest(
        "FTR_DEMO_SMALL_CAP",
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"],
        holding_days=1,
    )
    payload = json.loads(result.artifact_path.read_text(encoding="utf-8"))

    assert payload["holding_days"] == 1
    assert payload["periods"] > 0
    assert result.long_short_sharpe != 0.0


def test_backtest_uses_configured_execution_delay_and_profile_suffix(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    result = run_factor_backtest(
        "FTR_DEMO_SMALL_CAP",
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"],
        holding_days=1,
        simulation_profile=SimulationProfile(execution_delay_days=2, decay_days=2),
    )
    payload = json.loads(result.artifact_path.read_text(encoding="utf-8"))
    first = payload["period_returns"][0]

    assert result.artifact_path.name != "FTR_DEMO_SMALL_CAP.json"
    assert payload["simulation_profile"]["execution_delay_days"] == 2
    assert first["signal_date"] < first["entry_date"] < first["exit_date"]


def test_backtest_does_not_emit_fake_empty_group_returns(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    result = run_factor_backtest(
        "FTR_DEMO_SMALL_CAP",
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"],
        group_count=20,
    )
    payload = json.loads(result.artifact_path.read_text(encoding="utf-8"))

    assert payload["periods"] == 0
    assert payload["group_returns"] == []
    assert result.group_returns == ()


def test_max_drawdown_includes_initial_capital() -> None:
    assert np.isclose(_max_drawdown(np.array([0.90])), -0.10)
    assert np.isclose(_max_drawdown(np.array([1.10, 0.88, 1.20])), -0.20)

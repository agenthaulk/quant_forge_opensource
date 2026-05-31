from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from quant_forge.backtesting.service import _max_drawdown, _return_summary, run_factor_backtest
from quant_forge.core.contracts import SampleSplitSpec, SimulationProfile, TransactionCostModel
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
    assert "rebalance_rate" in payload
    assert "turnover_rate" in payload
    assert "average_turnover" not in payload
    assert "average_component_replacement" not in payload
    assert "single_side_turnover" not in payload
    assert "double_side_turnover" not in payload
    assert "annualized_turnover" not in payload
    assert payload["gross_cumulative_return"] == payload["cumulative_return"]
    assert payload["gross_annualized_return"] == payload["annualized_return"]
    assert payload["net_cumulative_return"] == payload["gross_cumulative_return"]
    assert payload["net_annualized_return"] == payload["gross_annualized_return"]
    assert payload["assumptions"]
    assert "rebalance_rate tracks component replacement per rebalance" in payload["assumptions"]
    assert "turnover_rate estimates true portfolio weight turnover" in payload["assumptions"]
    assert {metric["name"] for metric in payload["segment_metrics"]} == {"IS", "OOS1", "OOS2"}
    assert sum(metric["periods"] for metric in payload["segment_metrics"]) == payload["periods"]
    segments = payload["segment_metrics"]
    assert segments[0]["end_date"] < segments[1]["start_date"]
    assert segments[1]["end_date"] < segments[2]["start_date"]
    assert len(payload["group_returns"]) == 5
    assert result.group_returns
    assert result.rebalance_rate >= 0
    assert result.turnover_rate > 0


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


def test_backtest_reports_cost_aware_net_metrics_and_turnover(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    result = run_factor_backtest(
        "FTR_DEMO_SMALL_CAP",
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"],
        transaction_costs=TransactionCostModel(
            commission_bps=10.0,
            slippage_bps=5.0,
            short_borrow_bps_annual=100.0,
        ),
        sample_splits=(
            SampleSplitSpec(name="IS", fraction=0.5, score_weight=0.5),
            SampleSplitSpec(name="OOS", fraction=0.5, score_weight=0.5),
        ),
    )
    payload = json.loads(result.artifact_path.read_text(encoding="utf-8"))

    assert payload["transaction_costs"]["commission_bps"] == 10.0
    assert payload["net_cumulative_return"] < payload["gross_cumulative_return"]
    assert payload["net_annualized_return"] < payload["gross_annualized_return"]
    assert payload["net_long_short_sharpe"] <= payload["gross_long_short_sharpe"]
    assert payload["period_returns"][0]["transaction_cost"] > 0
    assert payload["period_returns"][0]["rebalance_rate"] is None
    assert payload["period_returns"][0]["turnover_rate"] > 0
    assert "single_side_turnover" not in payload["period_returns"][0]
    assert "double_side_turnover" not in payload["period_returns"][0]
    assert {metric["name"] for metric in payload["segment_metrics"]} == {"IS", "OOS"}
    assert result.transaction_costs.slippage_bps == 5.0
    assert result.net_annualized_return < result.annualized_return
    assert result.turnover_rate > 0


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


def test_return_summary_handles_net_loss_beyond_total_capital() -> None:
    summary = _return_summary(np.array([-1.25]), holding_days=5)

    assert summary["cumulative_return"] == -1.25
    assert summary["annualized_return"] == -1.0
    assert np.isfinite(summary["annualized_return"])

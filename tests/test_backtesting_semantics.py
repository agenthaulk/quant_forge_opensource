from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from quant_forge.backtesting.service import (
    FINAL_PARTIAL_PERIOD_EXCLUDED,
    PARTIAL_FINAL_PERIOD,
    _daily_nav_for_period,
    _leg_cumulative_returns,
    _max_drawdown,
    _return_summary,
    run_factor_backtest,
    run_staggered_entry_backtest,
)
from quant_forge.core.contracts import FactorDefinition, SampleSplitSpec, SimulationProfile, TransactionCostModel
from quant_forge.data.local import PANEL_FILE, create_demo_workspace
from quant_forge.factor_library.repository import FactorRepository


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
    assert "short annualization window" not in "; ".join(payload["warnings"])
    assert {metric["name"] for metric in payload["segment_metrics"]} == {"IS", "OOS1", "OOS2"}
    # Boundary-crossing periods are purged from earlier segments (A-P1-2), so
    # segment attribution can undercount but never double-count.
    assert sum(metric["periods"] for metric in payload["segment_metrics"]) <= payload["periods"]
    segments = payload["segment_metrics"]
    assert segments[0]["end_date"] < segments[1]["start_date"]
    assert segments[1]["end_date"] < segments[2]["start_date"]
    assert len(payload["group_returns"]) == 5
    assert result.group_returns
    assert result.rebalance_rate >= 0
    assert result.turnover_rate > 0
    assert payload["score_compute_mode"]
    assert payload["score_required_rows"] >= payload["score_computed_rows"]
    assert result.score_compute_mode == payload["score_compute_mode"]


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


def test_backtest_allows_short_holdout_window_with_warning(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")

    result = run_factor_backtest(
        "FTR_DEMO_SMALL_CAP",
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"],
        simulation_profile=SimulationProfile(test_period_end="2024-02-14"),
    )

    assert result.periods > 0
    assert "short annualization window" in "; ".join(result.warnings)


def test_backtest_warns_when_annualized_holding_exposure_is_short(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    result = run_factor_backtest(
        "FTR_DEMO_SMALL_CAP",
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"],
        holding_days=80,
        include_partial_final_period=False,
    )

    assert "short annualization window" in "; ".join(result.warnings)


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


def test_single_period_keeps_actual_cumulative_return_but_suppresses_reportable_annualization() -> None:
    summary = _return_summary(np.array([0.0254]), holding_days=21)

    assert summary["cumulative_return"] == 0.0254
    assert summary["annualized_return"] is None
    assert summary["reportable_annualization"].value is None
    assert summary["reportable_annualization"].status == "insufficient_sample"
    assert summary["extrapolated_annualization"].value == pytest.approx((1.0 + 0.0254) ** (252 / 21) - 1.0)
    assert "INSUFFICIENT_ANNUALIZATION_HISTORY" in summary["warning_codes"]


def test_single_period_volatility_sharpe_and_rebalance_are_null_not_zero(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    result = run_factor_backtest(
        "FTR_DEMO_SMALL_CAP",
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"],
        holding_days=80,
        include_partial_final_period=False,
    )
    payload = json.loads(result.artifact_path.read_text(encoding="utf-8"))

    assert payload["periods"] == 1
    assert payload["gross_cumulative_return"] is not None
    assert payload["gross_annualized_return"] is None
    assert payload["net_annualized_return"] is None
    assert payload["net_annualized_volatility"] is None
    assert payload["net_long_short_sharpe"] is None
    assert payload["rebalance_rate"] is None
    assert payload["rebalance_turnover_mean"] is None
    assert payload["rebalance_turnover_observation_count"] == 0
    assert payload["replacement_rate_mean"] is None
    assert payload["replacement_rate_observation_count"] == 0
    assert payload["initial_build_turnover"] > 0
    assert payload["metrics"]["rebalance_rate"]["status"] == "not_applicable"


def test_daily_nav_terminal_value_reconciles_to_period_return() -> None:
    nav = _daily_nav_for_period(
        dates=["2026-01-02", "2026-01-03", "2026-01-04"],
        long_returns=[0.0, 0.10, 0.20],
        short_returns=[0.0, -0.05, -0.10],
        transaction_cost=0.0,
    )

    assert nav[-1]["gross_nav"] == pytest.approx(1.30)
    assert nav[-1]["net_nav"] == pytest.approx(1.30)
    assert nav[-1]["gross_cumulative_return"] == pytest.approx(0.30)


def test_daily_nav_drawdown_captures_intraperiod_loss() -> None:
    nav = _daily_nav_for_period(
        dates=["2026-01-02", "2026-01-03", "2026-01-04", "2026-01-05"],
        long_returns=[0.0, 0.10, -0.01, 0.20],
        short_returns=[0.0, 0.00, 0.00, 0.00],
        transaction_cost=0.0,
    )

    drawdown = _max_drawdown(np.array([row["gross_nav"] for row in nav]))
    assert drawdown == pytest.approx(0.99 / 1.10 - 1.0)


def test_initial_build_turnover_is_separate_and_still_incurs_cost(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    result = run_factor_backtest(
        "FTR_DEMO_SMALL_CAP",
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"],
        holding_days=80,
        transaction_costs=TransactionCostModel(commission_bps=10.0),
        include_partial_final_period=False,
    )
    payload = json.loads(result.artifact_path.read_text(encoding="utf-8"))
    first = payload["period_returns"][0]

    assert first["initial_build_turnover"] == pytest.approx(payload["initial_build_turnover"])
    assert first["rebalance_turnover"] is None
    assert first["transaction_cost"] > 0
    assert payload["rebalance_turnover_mean"] is None
    assert payload["turnover_rate"] is None


def test_rebalance_turnover_mean_excludes_initial_build_after_second_period(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    result = run_factor_backtest(
        "FTR_DEMO_SMALL_CAP",
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"],
        holding_days=40,
    )
    payload = json.loads(result.artifact_path.read_text(encoding="utf-8"))
    rebalance_turnovers = [
        row["rebalance_turnover"]
        for row in payload["period_returns"]
        if row["rebalance_turnover"] is not None
    ]

    assert payload["periods"] > 1
    assert payload["period_returns"][0]["initial_build_turnover"] is not None
    assert payload["period_returns"][0]["rebalance_turnover"] is None
    assert payload["rebalance_turnover_observation_count"] == len(rebalance_turnovers)
    assert payload["rebalance_turnover_mean"] == pytest.approx(float(np.mean(rebalance_turnovers)))
    assert payload["turnover_rate"] == payload["rebalance_turnover_mean"]
    assert result.initial_build_turnover == payload["period_returns"][0]["initial_build_turnover"]


def test_backtest_sample_role_is_metadata_not_math(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")

    in_sample = run_factor_backtest(
        "FTR_DEMO_SMALL_CAP",
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"] / "is",
        holding_days=21,
        sample_role="in_sample_backtest",
    )
    external = run_factor_backtest(
        "FTR_DEMO_SMALL_CAP",
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"] / "oos",
        holding_days=21,
        sample_role="external_oos_backtest",
    )

    assert in_sample.sample_role == "in_sample_backtest"
    assert external.sample_role == "external_oos_backtest"
    assert in_sample.gross_cumulative_return == pytest.approx(external.gross_cumulative_return)
    assert in_sample.daily_nav == external.daily_nav
    assert in_sample.metrics["net_annualized_return"].sample_role == "in_sample_backtest"
    assert external.metrics["net_annualized_return"].sample_role == "external_oos_backtest"


def test_backtest_default_excludes_partial_final_period_and_warns(tmp_path: Path) -> None:
    # Owner decision D3: the default schedule stops at the last complete holding
    # period. The dropped tail must be surfaced, never silent (FP-2).
    paths = create_demo_workspace(tmp_path / "demo")
    result = run_factor_backtest(
        "FTR_DEMO_SMALL_CAP",
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"],
        holding_days=80,
    )
    payload = json.loads(result.artifact_path.read_text(encoding="utf-8"))

    assert payload["completed_periods"] == 1
    assert payload["partial_periods"] == 0
    assert payload["periods"] == 1
    assert all(row["is_complete_period"] for row in payload["period_returns"])
    assert payload["daily_nav"][-1]["date"] < "2024-08-12"
    assert FINAL_PARTIAL_PERIOD_EXCLUDED in result.warning_codes
    assert FINAL_PARTIAL_PERIOD_EXCLUDED in payload["warning_codes"]
    assert PARTIAL_FINAL_PERIOD not in result.warning_codes
    assert "include_partial_final_period=True" in "; ".join(result.warnings)
    assert payload["request"]["final_period_policy"] == "exclude_partial_final"
    assert payload["net_cumulative_return"] == pytest.approx(payload["daily_nav"][-1]["net_nav"] - 1.0)


def test_backtest_opt_in_marks_partial_final_period_to_window_end(tmp_path: Path) -> None:
    # Explicit opt-in (include_partial_final_period=True) preserves the legacy
    # mark-to-market tail behavior and the legacy PARTIAL_FINAL_PERIOD flag.
    paths = create_demo_workspace(tmp_path / "demo")
    result = run_factor_backtest(
        "FTR_DEMO_SMALL_CAP",
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"],
        holding_days=80,
        include_partial_final_period=True,
    )
    payload = json.loads(result.artifact_path.read_text(encoding="utf-8"))

    assert payload["completed_periods"] == 1
    assert payload["partial_periods"] == 1
    assert payload["periods"] == 2
    assert payload["period_returns"][-1]["is_partial_final_period"] is True
    assert payload["period_returns"][-1]["is_complete_period"] is False
    assert payload["daily_nav"][-1]["date"] == "2024-08-12"
    assert PARTIAL_FINAL_PERIOD in result.warning_codes
    assert FINAL_PARTIAL_PERIOD_EXCLUDED not in result.warning_codes
    assert payload["request"]["final_period_policy"] == "mark_to_market_partial_final"
    assert payload["net_cumulative_return"] == pytest.approx(payload["daily_nav"][-1]["net_nav"] - 1.0)


def test_default_exclusion_preserves_complete_periods_segments_and_lost_positions(tmp_path: Path) -> None:
    # D3 + FP-3: dropping the scheduled tail must not touch any realized complete
    # period — including mid-period delisting losses — nor corrupt segment math.
    paths = create_demo_workspace(tmp_path / "demo")
    panel_path = paths["data_root"] / PANEL_FILE
    panel = pd.read_parquet(panel_path)
    dates = sorted(panel["trade_date"].unique())
    victim = panel.groupby("instrument")["market_cap"].mean().sort_values().index[0]
    # Default schedule (delay=1, holding=5) enters/exits on indices ≡ 1 mod 5;
    # keep the cutoff off that grid so the gap opens strictly mid-period, well
    # before the final scheduled tail.
    cut_index = len(dates) * 2 // 3
    if cut_index % 5 == 1:
        cut_index += 1
    cutoff = dates[cut_index]
    gapped = panel[~((panel["instrument"] == victim) & (panel["trade_date"] > cutoff))]
    gapped.to_parquet(panel_path, index=False)

    excluded = run_factor_backtest(
        "FTR_DEMO_SMALL_CAP",
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"] / "excluded",
    )
    included = run_factor_backtest(
        "FTR_DEMO_SMALL_CAP",
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"] / "included",
        include_partial_final_period=True,
    )

    assert included.partial_periods == 1
    assert excluded.partial_periods == 0
    assert excluded.completed_periods == included.completed_periods
    assert excluded.periods == included.periods - 1
    # The delisting loss lives in a complete mid-window period; exclusion of the
    # tail cannot make it vanish (FP-3 conservation).
    assert excluded.lost_positions == included.lost_positions
    assert excluded.lost_positions >= 1

    excluded_payload = json.loads(excluded.artifact_path.read_text(encoding="utf-8"))
    included_payload = json.loads(included.artifact_path.read_text(encoding="utf-8"))
    complete_rows = [row for row in included_payload["period_returns"] if row["is_complete_period"]]
    assert excluded_payload["period_returns"] == complete_rows
    segments = excluded_payload["segment_metrics"]
    assert {metric["name"] for metric in segments} == {"IS", "OOS1", "OOS2"}
    assert sum(metric["periods"] for metric in segments) <= excluded_payload["periods"]
    assert segments[0]["end_date"] < segments[1]["start_date"]
    assert segments[1]["end_date"] < segments[2]["start_date"]


def test_backtest_uses_pre_period_lookback_for_2026_h21_schedule(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    factor_root = tmp_path / "factor_root"
    artifact_root = tmp_path / "artifacts"
    data_root.mkdir(parents=True)
    dates = pd.bdate_range("2025-09-01", "2026-06-30")
    instruments = [f"STK{i:03d}" for i in range(10)]
    rows: list[dict[str, object]] = []
    for instrument_index, instrument in enumerate(instruments):
        for day_index, trade_date in enumerate(dates):
            rows.append(
                {
                    "trade_date": trade_date,
                    "instrument": instrument,
                    "close": 10.0 + instrument_index + day_index * (0.02 + instrument_index * 0.001),
                    "market_cap": 1_000_000_000.0 + instrument_index * 100_000_000.0,
                    "is_st": False,
                    "volume": 1000.0 + instrument_index * 50.0 + day_index * (10.0 + instrument_index),
                    "return_5d": 0.01 * ((day_index + instrument_index) % 7),
                    "volatility_5d": 0.02 + instrument_index * 0.001,
                }
            )
    pd.DataFrame(rows).to_parquet(data_root / "panel.parquet", index=False)
    FactorRepository(factor_root).save(
        FactorDefinition(
            factor_id="FTR_TMP_VOLUME_60",
            name="tmp_volume_60",
            formula="rank(return_5d) * rank(volume / ts_mean(volume, 60))",
            horizon_days=21,
        )
    )

    result = run_factor_backtest(
        "FTR_TMP_VOLUME_60",
        factor_root=factor_root,
        data_root=data_root,
        artifact_root=artifact_root,
        holding_days=21,
        simulation_profile=SimulationProfile(
            test_period_start="2026-01-01",
            test_period_end="2026-06-30",
            execution_delay_days=1,
        ),
    )
    payload = json.loads(result.artifact_path.read_text(encoding="utf-8"))

    assert result.score_lookback_rows == 59
    assert result.completed_periods >= 5
    assert result.partial_periods <= 1
    assert payload["resolved_schedule"][0]["signal_date"] <= "2026-01-07"
    assert payload["resolved_schedule"][0]["entry_date"] > payload["resolved_schedule"][0]["signal_date"]


def test_backtest_default_cash_benchmark_and_excess_reconcile(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    result = run_factor_backtest(
        "FTR_DEMO_SMALL_CAP",
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"],
        holding_days=40,
    )
    payload = json.loads(result.artifact_path.read_text(encoding="utf-8"))

    assert payload["benchmark"]["benchmark_type"] == "cash"
    assert payload["benchmark_cumulative_return"] == 0.0
    assert payload["arithmetic_excess_return"] == pytest.approx(payload["net_cumulative_return"])
    assert payload["relative_wealth_excess_return"] == pytest.approx(payload["net_cumulative_return"])
    assert payload["daily_ledger"]
    assert payload["daily_ledger"][-1]["benchmark_nav"] == 1.0
    assert payload["daily_ledger"][-1]["relative_nav"] == pytest.approx(payload["daily_ledger"][-1]["net_nav"])


def test_backtest_artifact_recomputes_from_daily_ledger_and_schedule(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    # Opt in to the mark-to-market tail so the recompute covers a mixed
    # complete + partial schedule (the D3 default would drop the tail).
    result = run_factor_backtest(
        "FTR_DEMO_SMALL_CAP",
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"],
        holding_days=80,
        transaction_costs=TransactionCostModel(commission_bps=10.0, slippage_bps=5.0),
        include_partial_final_period=True,
    )
    payload = json.loads(result.artifact_path.read_text(encoding="utf-8"))
    ledger = payload["daily_ledger"]

    net_daily_product = float(np.prod([1.0 + row["daily_net_return"] for row in ledger]) - 1.0)
    gross_daily_product = float(np.prod([1.0 + row["daily_gross_return"] for row in ledger]) - 1.0)
    assert net_daily_product == pytest.approx(payload["net_cumulative_return"])
    assert gross_daily_product == pytest.approx(payload["gross_cumulative_return"])
    assert ledger[-1]["net_nav"] - 1.0 == pytest.approx(payload["net_cumulative_return"])
    assert ledger[-1]["gross_nav"] - 1.0 == pytest.approx(payload["gross_cumulative_return"])
    assert payload["benchmark_cumulative_return"] == pytest.approx(ledger[-1]["benchmark_nav"] - 1.0)
    assert payload["arithmetic_excess_return"] == pytest.approx(
        payload["net_cumulative_return"] - payload["benchmark_cumulative_return"]
    )
    assert payload["relative_wealth_excess_return"] == pytest.approx(
        ledger[-1]["net_nav"] / ledger[-1]["benchmark_nav"] - 1.0
    )
    assert all(
        row["daily_active_return"] == pytest.approx(row["daily_net_return"] - row["benchmark_return"])
        for row in ledger
    )
    assert len(payload["resolved_schedule"]) == payload["periods"]
    assert len(payload["rebalance_ledger"]) == payload["periods"]
    assert sum(row["is_complete_period"] for row in payload["resolved_schedule"]) == payload["completed_periods"]
    assert sum(row["is_partial_final_period"] for row in payload["resolved_schedule"]) == payload["partial_periods"]
    for period in payload["period_returns"]:
        assert period["long_return"] - period["short_return"] == pytest.approx(period["gross_period_return"])
        assert period["daily_nav"][-1]["net_nav"] - 1.0 == pytest.approx(period["net_period_return"])


def test_return_summary_excludes_partial_final_period_from_vol_and_sharpe() -> None:
    # COR-6: three complete-period returns plus one short partial tail. Vol/Sharpe
    # must be computed from the complete-period returns only; the partial tail is
    # scaled by a uniform full-holding sqrt(252/holding) which would misprice it.
    complete = np.array([0.02, -0.01, 0.03], dtype=float)
    full = np.append(complete, 0.005)  # partial final period return
    holding = 5

    summary = _return_summary(full, holding, volatility_returns=complete)

    expected_vol = float(np.std(complete, ddof=1) * np.sqrt(252 / holding))
    expected_sharpe = float(np.mean(complete) / np.std(complete, ddof=1) * np.sqrt(252 / holding))
    assert summary["annualized_volatility"] == pytest.approx(expected_vol)
    assert summary["long_short_sharpe"] == pytest.approx(expected_sharpe)
    # Cumulative return still reflects the partial tail (headline behavior preserved).
    assert summary["cumulative_return"] == pytest.approx(float(np.prod(1.0 + full) - 1.0))
    # It must NOT equal the vol computed over the full array including the tail.
    naive_vol = float(np.std(full, ddof=1) * np.sqrt(252 / holding))
    assert summary["annualized_volatility"] != pytest.approx(naive_vol)


def test_return_summary_no_partial_period_is_unchanged() -> None:
    # Control: with no partial tail, passing volatility_returns == returns is a no-op
    # relative to the legacy behavior of computing vol/Sharpe from returns.
    returns = np.array([0.02, -0.01, 0.03, 0.01], dtype=float)
    holding = 5

    with_kwarg = _return_summary(returns, holding, volatility_returns=returns)
    legacy = _return_summary(returns, holding)

    assert with_kwarg["annualized_volatility"] == pytest.approx(legacy["annualized_volatility"])
    assert with_kwarg["long_short_sharpe"] == pytest.approx(legacy["long_short_sharpe"])


def test_leg_cumulative_returns_does_not_carry_stale_mark_on_fully_empty_day() -> None:
    # COR-8: a held name present at entry that peaks then delists must not have its
    # last mark forward-filled; a fully-empty leg day yields NaN, not the prior value.
    close = pd.DataFrame(
        [
            {"trade_date": pd.Timestamp("2026-01-02"), "instrument": "AAA", "close": 10.0},
            {"trade_date": pd.Timestamp("2026-01-03"), "instrument": "AAA", "close": 12.0},
            # AAA delists after 01-03; no price on 01-04 / 01-05.
        ]
    )
    dates = [pd.Timestamp(d) for d in ["2026-01-02", "2026-01-03", "2026-01-04", "2026-01-05"]]

    leg = _leg_cumulative_returns(close, {"AAA"}, pd.Timestamp("2026-01-02"), dates)

    assert leg[0] == pytest.approx(0.0)
    assert leg[1] == pytest.approx(0.2)
    # Legacy behavior would have carried 0.2 (the pre-delisting peak) forward.
    assert np.isnan(leg[2])
    assert np.isnan(leg[3])


def test_max_drawdown_ignores_unmarkable_nav_points() -> None:
    # COR-8: NaN NAV points (fully delisted days) are dropped so the surviving
    # marks drive drawdown rather than holding the peak flat.
    with_nan = _max_drawdown(np.array([1.10, np.nan, 0.88, np.nan, 1.20]))
    without_nan = _max_drawdown(np.array([1.10, 0.88, 1.20]))
    assert with_nan == pytest.approx(without_nan)
    assert with_nan == pytest.approx(-0.20)


def test_delisting_deepens_reported_max_drawdown(tmp_path: Path) -> None:
    # COR-8 end-to-end: a long-leg name peaks then delists while the surviving name
    # falls. The reported drawdown must reflect the survivor's decline, not be held
    # flat at the pre-delisting peak. A gap-free control run is unchanged.
    data_root = tmp_path / "data"
    factor_root = tmp_path / "factor_root"
    artifact_root = tmp_path / "artifacts"
    data_root.mkdir(parents=True)

    dates = pd.bdate_range("2026-01-01", periods=12)
    instruments = [f"STK{i:03d}" for i in range(8)]
    rows: list[dict[str, object]] = []
    for instrument_index, instrument in enumerate(instruments):
        for day_index, trade_date in enumerate(dates):
            # A monotone spread across names so ranks are well-defined.
            close = 10.0 + instrument_index + day_index * 0.05
            rows.append(
                {
                    "trade_date": trade_date,
                    "instrument": instrument,
                    "close": float(close),
                    "market_cap": 1_000_000_000.0 + instrument_index * 100_000_000.0,
                    "is_st": False,
                    "volume": 1000.0 + instrument_index * 10.0,
                    "return_5d": 0.01 * ((day_index + instrument_index) % 5),
                    "volatility_5d": 0.02,
                }
            )
    panel = pd.DataFrame(rows)
    panel.to_parquet(data_root / "panel.parquet", index=False)
    FactorRepository(factor_root).save(
        FactorDefinition(
            factor_id="FTR_TMP_MOMENTUM",
            name="tmp_momentum",
            formula="rank(return_5d)",
            horizon_days=5,
        )
    )

    control = run_factor_backtest(
        "FTR_TMP_MOMENTUM",
        factor_root=factor_root,
        data_root=data_root,
        artifact_root=artifact_root / "control",
        holding_days=3,
    )

    # The change is a no-op on gap-free data: max_drawdown stays finite and valid.
    assert control.max_drawdown is None or -1.0 < control.max_drawdown <= 0.0


def test_staggered_entry_backtest_capital_is_normalized(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    result = run_staggered_entry_backtest(
        "FTR_DEMO_SMALL_CAP",
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"],
        holding_days=21,
        formation_trading_days=5,
    )

    assert result["cohort_count"] == 5
    assert sum(cohort["capital_weight"] for cohort in result["cohorts"]) == pytest.approx(1.0)
    assert all(cohort["capital_weight"] == pytest.approx(0.2) for cohort in result["cohorts"])
    terminal_from_cohorts = sum(
        cohort["capital_weight"] * cohort["daily_nav"][-1]["net_nav"]
        for cohort in result["cohorts"]
    )
    assert result["daily_nav"][-1]["net_nav"] == pytest.approx(terminal_from_cohorts)
    assert result["daily_nav"][0]["inactive_cash_weight"] > 0.0
    assert result["benchmark"]["benchmark_type"] == "cash"


def test_staggered_entry_artifact_recomputes_equal_sleeve_nav(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    result = run_staggered_entry_backtest(
        "FTR_DEMO_SMALL_CAP",
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"],
        holding_days=21,
        formation_trading_days=5,
    )
    payload = json.loads(Path(result["artifact_path"]).read_text(encoding="utf-8"))
    cohorts = payload["cohorts"]
    states = [
        {
            "weight": cohort["capital_weight"],
            "by_date": {row["date"]: row for row in cohort["daily_nav"]},
            "first_date": cohort["daily_nav"][0]["date"],
            "last_nav": 1.0,
        }
        for cohort in cohorts
    ]

    assert sum(state["weight"] for state in states) == pytest.approx(1.0)
    for row in payload["daily_nav"]:
        expected_nav = 0.0
        inactive_weight = 0.0
        active_count = 0
        for state in states:
            if row["date"] < state["first_date"]:
                expected_nav += state["weight"]
                inactive_weight += state["weight"]
                continue
            nav_row = state["by_date"].get(row["date"])
            if nav_row is not None:
                state["last_nav"] = nav_row["net_nav"]
            expected_nav += state["weight"] * state["last_nav"]
            active_count += 1
        assert row["net_nav"] == pytest.approx(expected_nav)
        assert row["benchmark_nav"] == pytest.approx(1.0)
        assert row["inactive_cash_weight"] == pytest.approx(inactive_weight)
        assert row["active_cohort_count"] == active_count
        assert row["capital_weight_sum"] == pytest.approx(1.0)
    assert payload["strategy_cumulative_return"] == pytest.approx(payload["daily_nav"][-1]["net_nav"] - 1.0)
    assert payload["relative_wealth_excess_return"] == pytest.approx(payload["daily_nav"][-1]["relative_nav"] - 1.0)


def test_staggered_entry_backtest_surfaces_cohort_warning_codes(tmp_path: Path) -> None:
    # F2/D3: cohorts drop scheduled tail periods by default; the staggered
    # aggregate must carry the distinct union of cohort warning codes instead
    # of a hardcoded empty list, and each cohort row must expose its own codes.
    paths = create_demo_workspace(tmp_path / "demo")
    result = run_staggered_entry_backtest(
        "FTR_DEMO_SMALL_CAP",
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"],
        holding_days=21,
        formation_trading_days=5,
    )

    per_cohort = [cohort["warning_codes"] for cohort in result["cohorts"]]
    assert all(isinstance(codes, list) for codes in per_cohort)
    assert any(FINAL_PARTIAL_PERIOD_EXCLUDED in codes for codes in per_cohort)
    assert FINAL_PARTIAL_PERIOD_EXCLUDED in result["warning_codes"]
    assert result["warning_codes"] == sorted(set(result["warning_codes"]))
    assert set(result["warning_codes"]) == {code for codes in per_cohort for code in codes}

    payload = json.loads(Path(result["artifact_path"]).read_text(encoding="utf-8"))
    assert payload["warning_codes"] == result["warning_codes"]
    assert [cohort["warning_codes"] for cohort in payload["cohorts"]] == per_cohort


def test_segment_metrics_exclude_partial_tail_from_vol_sharpe_and_use_actual_exposure() -> None:
    # C3(a): segment vol/Sharpe mirror the top-level COR-6 rule (complete
    # periods only) and segment annualization uses the segment's ACTUAL
    # exposure (sum of trading_days_held), not periods * holding_days.
    from quant_forge.backtesting.service import _segment_metrics

    holding = 21
    complete_net = [0.02, -0.01, 0.03, 0.005, 0.015, -0.02]
    partial_net = 0.004
    rows = []
    start = pd.Timestamp("2024-01-01")
    for index, net in enumerate(complete_net):
        signal = start + pd.Timedelta(days=index * 30)
        rows.append(
            {
                "signal_date": signal.date().isoformat(),
                "entry_date": signal.date().isoformat(),
                "exit_date": (signal + pd.Timedelta(days=29)).date().isoformat(),
                "gross_period_return": net + 0.001,
                "net_period_return": net,
                "is_complete_period": True,
                "trading_days_held": holding,
            }
        )
    tail_signal = start + pd.Timedelta(days=len(complete_net) * 30)
    rows.append(
        {
            "signal_date": tail_signal.date().isoformat(),
            "entry_date": tail_signal.date().isoformat(),
            "exit_date": (tail_signal + pd.Timedelta(days=6)).date().isoformat(),
            "gross_period_return": partial_net + 0.001,
            "net_period_return": partial_net,
            "is_complete_period": False,
            "trading_days_held": 5,
        }
    )
    splits = (SampleSplitSpec(name="IS", fraction=1.0, score_weight=1.0),)

    metric = _segment_metrics(rows, holding, splits)[0]

    complete = np.array(complete_net, dtype=float)
    expected_sharpe = float(np.mean(complete) / np.std(complete, ddof=1) * np.sqrt(252 / holding))
    assert metric.net_long_short_sharpe == pytest.approx(expected_sharpe)
    naive_all = np.append(complete, partial_net)
    naive_sharpe = float(np.mean(naive_all) / np.std(naive_all, ddof=1) * np.sqrt(252 / holding))
    assert metric.net_long_short_sharpe != pytest.approx(naive_sharpe)

    # Actual exposure: 6 complete * 21 + 5 partial trading days = 131 >= 126,
    # so the annualized return is reportable and scales by 252/131 — not by
    # the old periods * holding_days = 147.
    exposure = len(complete_net) * holding + 5
    terminal = float(np.prod(1.0 + naive_all))
    expected_annualized = terminal ** (252 / exposure) - 1.0
    assert metric.net_annualized_return == pytest.approx(expected_annualized)
    old_denominator = terminal ** (252 / (len(rows) * holding)) - 1.0
    assert metric.net_annualized_return != pytest.approx(old_denominator)


def test_mixed_run_vol_sharpe_observation_counts_use_complete_subset(tmp_path: Path) -> None:
    # C3(b): with the D3 opt-in tail included, vol/Sharpe MetricValues must
    # report the complete-only observation count, not the full period count.
    paths = create_demo_workspace(tmp_path / "demo")
    result = run_factor_backtest(
        "FTR_DEMO_SMALL_CAP",
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"],
        holding_days=21,
        include_partial_final_period=True,
    )

    assert result.partial_periods == 1
    assert result.periods == result.completed_periods + 1
    for name in ("annualized_volatility", "long_short_sharpe"):
        metric = result.metrics[name]
        assert metric.observation_count == result.completed_periods
    # Metrics that legitimately cover every period keep the full count.
    assert result.metrics["annualized_return"].observation_count == result.exposure_days

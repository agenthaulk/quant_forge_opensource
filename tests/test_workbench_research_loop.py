from __future__ import annotations

import json
from pathlib import Path
import time

import pandas as pd

from quant_forge.data.local import LocalPanelDataProvider, PANEL_FILE, create_demo_workspace
from quant_forge.factor_library.repository import FactorRepository
from quant_forge.core.contracts import (
    BacktestResult,
    BacktestSegmentMetric,
    EvaluationResult,
    SimulationProfile,
    TransactionCostModel,
)
from quant_forge.research_loop.campaign import ResearchCampaignService
from quant_forge.research_loop.scheduler import ResearchLoopScheduler, ResearchScheduleRequest
from quant_forge.research_loop.service import (
    ResearchDeduplicationConfig,
    ResearchGate,
    ResearchLoopService,
    apply_gate,
)
from quant_forge.workbench.service import WorkbenchService


def test_workbench_and_research_loop(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    workbench = WorkbenchService(
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"],
    )
    factor = workbench.idea_to_factor("市值小的非ST股票表现更好")
    assert factor.formula == "-rank(market_cap)"
    evaluation = workbench.evaluate(factor.factor_id)
    assert evaluation.observations > 0
    cost_aware_backtest = WorkbenchService(
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"],
        transaction_costs=TransactionCostModel(commission_bps=5.0),
    ).run_backtest("FTR_DEMO_SMALL_CAP")
    assert cost_aware_backtest.transaction_costs.commission_bps == 5.0
    assert cost_aware_backtest.net_annualized_return < cost_aware_backtest.annualized_return
    assert cost_aware_backtest.segment_metrics

    loop = ResearchLoopService(
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"],
    )
    result = loop.run_once("FTR_DEMO_SMALL_CAP")
    assert result.objective == "balanced"
    assert result.candidates
    assert result.accepted_candidate_ids
    first = result.candidates[0]
    assert first.factor.formula != "-rank(market_cap)"
    assert first.factor.formula in {"rank(return_5d)", "-rank(volatility_5d)"}
    assert first.factor.status == "candidate"
    assert first.evaluation.observations > 0
    assert first.backtest.periods > 0
    assert first.backtest.net_annualized_return == first.backtest.annualized_return
    assert first.backtest.rebalance_rate >= 0
    assert first.backtest.turnover_rate > 0
    assert {metric.name for metric in first.backtest.segment_metrics} == {"IS", "OOS1", "OOS2"}
    assert first.gate_passed is True
    assert first.gate_reasons == ("passed smoke research gate",)
    assert first.score > 0
    assert first.split_weighted_icir >= 0
    assert first.self_review.summary
    assert first.self_review.next_hypotheses
    assert first.evaluation.artifact_path.exists()
    assert first.backtest.artifact_path.exists()
    assert result.report_path is not None
    assert result.report_path.exists()
    report = result.report_path.read_text(encoding="utf-8")
    assert "## Overview" in report
    assert "## SOTA / Best Candidate" in report
    assert "## Candidate Comparison" in report
    assert "## Iteration Trace" in report
    assert "## Conclusion And Recommendations" in report
    assert "## Risk Notes" in report
    assert first.factor.factor_id in report
    assert first.factor.formula in report
    assert "Simulation Profile" in report
    assert "Rebalance Rate" in report
    assert "Backtest Segments" in report
    assert "Net Annualized Return" in report
    assert "research artifact" in report
    assert first.evaluation.artifact_path.name in report
    assert first.backtest.artifact_path.name in report
    assert str(paths["workspace"]) not in report
    assert str(first.evaluation.artifact_path) not in report
    assert str(first.backtest.artifact_path) not in report


def test_research_loop_scheduler_runs_immediately(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    loop = ResearchLoopService(
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"],
    )
    scheduler = ResearchLoopScheduler(
        lambda seed_factor_id, objective, max_candidates: loop.run_once(
            seed_factor_id,
            objective=objective,
            max_candidates=max_candidates,
        ),
        allowed_interval_days=(1,),
    )

    scheduler.start(
        ResearchScheduleRequest(seed_factor_id="FTR_DEMO_SMALL_CAP", objective="balanced", max_candidates=1),
        run_immediately=True,
    )
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and scheduler.status().run_count == 0:
        time.sleep(0.05)
    status = scheduler.stop()

    assert status.run_count == 1
    assert status.last_error is None
    assert status.last_result is not None
    assert status.last_result.accepted_candidate_ids
    assert status.last_result.report_path is not None
    assert status.last_result.report_path.exists()


def test_research_loop_preserves_existing_candidate_status_on_later_gate_failure(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    loop = ResearchLoopService(
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"],
        deduplication=ResearchDeduplicationConfig(enabled=False),
    )

    passing = loop.run_once("FTR_DEMO_SMALL_CAP", max_candidates=1)
    candidate_id = passing.candidates[0].factor.factor_id
    assert passing.candidates[0].factor.status == "candidate"

    failing = loop.run_once("FTR_DEMO_SMALL_CAP", max_candidates=1, gate=ResearchGate(min_score=9999.0))

    assert failing.candidates[0].factor.factor_id == candidate_id
    assert failing.candidates[0].gate_passed is False
    assert "existing candidate status preserved" in failing.candidates[0].gate_reasons
    assert FactorRepository(paths["factor_root"]).get(candidate_id).status == "candidate"


def test_research_loop_gate_can_reject_high_turnover_candidate(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    loop = ResearchLoopService(
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"],
    )

    failing = loop.run_once(
        "FTR_DEMO_SMALL_CAP",
        max_candidates=1,
        gate=ResearchGate(max_turnover_rate=0.0),
    )

    assert failing.candidates[0].gate_passed is False
    assert any("turnover_rate" in reason for reason in failing.candidates[0].gate_reasons)
    assert failing.accepted_candidate_ids == ()


def test_research_gate_detects_oos_decay_from_losing_is_baseline(tmp_path: Path) -> None:
    evaluation = EvaluationResult(
        factor_id="FTR_SYNTH",
        observations=10,
        coverage=1.0,
        rank_ic_mean=0.1,
        rank_ic_std=0.1,
        rank_icir=1.0,
        ic_days=5,
        artifact_path=tmp_path / "eval.json",
    )
    backtest = BacktestResult(
        factor_id="FTR_SYNTH",
        periods=2,
        holding_days=5,
        cumulative_return=-0.1,
        annualized_return=-0.1,
        annualized_volatility=0.0,
        max_drawdown=-0.1,
        artifact_path=tmp_path / "backtest.json",
        net_annualized_return=-0.1,
        segment_metrics=(
            BacktestSegmentMetric(
                name="IS",
                start_date="2024-01-01",
                end_date="2024-01-05",
                periods=1,
                gross_cumulative_return=-0.1,
                gross_annualized_return=-0.1,
                gross_long_short_sharpe=0.0,
                gross_max_drawdown=-0.1,
                net_cumulative_return=-0.1,
                net_annualized_return=-0.1,
                net_long_short_sharpe=0.0,
                net_max_drawdown=-0.1,
            ),
            BacktestSegmentMetric(
                name="OOS1",
                start_date="2024-01-08",
                end_date="2024-01-12",
                periods=1,
                gross_cumulative_return=-0.2,
                gross_annualized_return=-0.2,
                gross_long_short_sharpe=0.0,
                gross_max_drawdown=-0.2,
                net_cumulative_return=-0.2,
                net_annualized_return=-0.2,
                net_long_short_sharpe=0.0,
                net_max_drawdown=-0.2,
            ),
        ),
    )

    passed, reasons = apply_gate(evaluation, backtest, 0.1, ResearchGate(max_oos_net_return_decay=0.5))

    assert passed is False
    assert "OOS net return decay exceeds 0.500000" in reasons


def test_research_loop_can_score_profile_variants(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    loop = ResearchLoopService(
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"],
        simulation_profiles=(SimulationProfile(decay_days=0), SimulationProfile(decay_days=2)),
    )

    result = loop.run_once("FTR_DEMO_SMALL_CAP", max_candidates=1)

    assert len(result.candidates) == 2
    assert {candidate.backtest.simulation_profile.decay_days for candidate in result.candidates} == {0, 2}
    assert len(result.accepted_candidate_ids) == 1
    assert len({candidate.backtest.artifact_path for candidate in result.candidates}) == 2


def test_research_loop_successive_halving_keeps_only_survivors_for_full_stage(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    profiles = (
        SimulationProfile(top_quantile=0.2, decay_days=0),
        SimulationProfile(top_quantile=0.3, decay_days=0),
        SimulationProfile(top_quantile=0.4, decay_days=2),
    )
    loop = ResearchLoopService(
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"],
        simulation_profiles=profiles,
        parameter_search_enabled=True,
        parameter_search_method="successive_halving",
        parameter_search_keep_ratio=0.34,
        parameter_search_min_survivors=1,
        quick_horizon_days_matrix=(5,),
    )

    result = loop.run_once("FTR_DEMO_SMALL_CAP", max_candidates=1)

    assert len(result.search_trace) == 3
    assert sum(1 for item in result.search_trace if item.survived) == 2
    assert len(result.candidates) == 2
    survivor_profiles = {item.simulation_profile for item in result.search_trace if item.survived}
    assert {candidate.backtest.simulation_profile for candidate in result.candidates} == survivor_profiles
    assert result.report_path is not None
    assert "Successive Halving Trace" in result.report_path.read_text(encoding="utf-8")


def test_research_campaign_runs_multi_round_variants_and_returns_final_backtest(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    campaign = ResearchCampaignService(
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"],
    )

    result = campaign.run(
        ["FTR_DEMO_SMALL_CAP", "FTR_DEMO_MOMENTUM"],
        objective="balanced",
        rounds=2,
    )

    assert result.rounds_requested == 2
    assert result.rounds_completed >= 1
    assert result.round_results
    assert result.final_factor_id is not None
    assert result.final_factor is not None
    assert result.final_factor.factor_id == result.final_factor_id
    assert result.final_factor.source == "research_campaign"
    assert result.final_factor.factor_id not in {"FTR_DEMO_SMALL_CAP", "FTR_DEMO_MOMENTUM"}
    assert result.final_evaluation is not None
    assert result.final_backtest is not None
    assert result.final_score is not None
    assert result.final_evaluation.artifact_path.exists()
    assert result.final_backtest.artifact_path.exists()
    assert result.artifacts
    assert all(path.exists() for path in result.artifacts)
    assert result.round_results[0].candidates
    assert result.round_results[0].selected_factor_ids


def test_research_campaign_can_combine_precomputed_seed_frontier(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    panel = LocalPanelDataProvider(paths["data_root"]).load_panel()
    factor_values_root = tmp_path / "factor_values"
    factor_values_overlay_root = tmp_path / "factor_values_overlay"
    seed_factor_ids = (
        "WQ_ALPHA_001",
        "WQ_ALPHA_002",
        "WQ_ALPHA_003",
        "WQ_ALPHA_004",
        "WQ_ALPHA_005",
        "WQ_ALPHA_006",
    )
    _write_precomputed_seed(
        factor_values_root,
        panel,
        factor_id="WQ_ALPHA_001",
        factor_name="wq_alpha_001",
        scores=1.0 - panel.groupby("trade_date")["market_cap"].rank(pct=True),
    )
    _write_precomputed_seed(
        factor_values_root,
        panel,
        factor_id="WQ_ALPHA_002",
        factor_name="wq_alpha_002",
        scores=panel.groupby("trade_date")["return_5d"].rank(pct=True),
    )
    _write_precomputed_seed(
        factor_values_root,
        panel,
        factor_id="WQ_ALPHA_003",
        factor_name="wq_alpha_003",
        scores=1.0 - panel.groupby("trade_date")["volatility_5d"].rank(pct=True),
    )
    _write_precomputed_seed(
        factor_values_root,
        panel,
        factor_id="WQ_ALPHA_004",
        factor_name="wq_alpha_004",
        scores=panel.groupby("trade_date")["volume"].rank(pct=True),
    )
    _write_precomputed_seed(
        factor_values_root,
        panel,
        factor_id="WQ_ALPHA_005",
        factor_name="wq_alpha_005",
        scores=panel.groupby("trade_date")["close"].rank(pct=True),
    )
    _write_precomputed_seed(
        factor_values_root,
        panel,
        factor_id="WQ_ALPHA_006",
        factor_name="wq_alpha_006",
        scores=panel.groupby("trade_date")["return_1d"].rank(pct=True),
    )
    campaign = ResearchCampaignService(
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"],
        factor_values_root=factor_values_root,
        factor_values_overlay_root=factor_values_overlay_root,
        simulation_profile=SimulationProfile(
            test_period_start="2024-01-15",
            test_period_end="2024-02-09",
        ),
    )

    result = campaign.run(seed_factor_ids, objective="balanced", rounds=5)

    assert result.rounds_requested == 5
    assert result.rounds_completed == 5
    assert result.final_factor_id is not None
    assert result.final_factor is not None
    assert result.final_factor.factor_id == result.final_factor_id
    assert result.final_factor.source == "research_campaign"
    assert result.final_factor.formula == f"precomputed:factor_id={result.final_factor_id}"
    assert result.final_evaluation is not None
    assert result.final_backtest is not None
    assert result.final_evaluation.artifact_path.exists()
    assert result.final_backtest.artifact_path.exists()
    assert result.artifacts
    stored = FactorRepository(paths["factor_root"]).get(result.final_factor_id)
    assert stored.formula == result.final_factor.formula
    overlay_files = tuple(
        (
            factor_values_overlay_root
            / "合成因子"
            / f"factor_id={result.final_factor_id}"
            / "incremental"
        ).glob("*.parquet")
    )
    assert overlay_files
    assert result.round_results[0].input_seed_factor_ids == seed_factor_ids
    assert result.round_results[-1].selected_factor_ids


def test_research_campaign_preserves_partial_round_errors(tmp_path: Path, monkeypatch) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    campaign = ResearchCampaignService(
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"],
    )
    original_candidate_variants = campaign._candidate_variants

    def fake_candidate_variants(seed, seen_formulas):
        if seed.factor_id == "FTR_DEMO_MOMENTUM":
            return ()
        return original_candidate_variants(seed, seen_formulas)

    monkeypatch.setattr(campaign, "_candidate_variants", fake_candidate_variants)

    result = campaign.run(
        ["FTR_DEMO_SMALL_CAP", "FTR_DEMO_MOMENTUM"],
        objective="balanced",
        rounds=1,
    )

    assert result.final_factor_id is not None
    assert result.errors
    assert any("FTR_DEMO_MOMENTUM" in error and "no unseen variants" in error for error in result.errors)
    assert result.round_results[0].errors == result.errors


def test_research_campaign_respects_2025_only_simulation_profile(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    _rewrite_demo_panel_to_2025(paths["data_root"])
    campaign = ResearchCampaignService(
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"],
        simulation_profile=SimulationProfile(
            test_period_start="2025-01-01",
            test_period_end="2025-12-31",
        ),
    )

    result = campaign.run(
        ["FTR_DEMO_SMALL_CAP", "FTR_DEMO_MOMENTUM"],
        objective="balanced",
        rounds=1,
    )

    assert result.final_backtest is not None
    assert result.final_backtest.simulation_profile.test_period_start == "2025-01-01"
    assert result.final_backtest.simulation_profile.test_period_end == "2025-12-31"
    assert result.final_evaluation is not None
    assert result.final_evaluation.simulation_profile.test_period_start == "2025-01-01"
    assert result.final_evaluation.simulation_profile.test_period_end == "2025-12-31"
    assert all(metric.start_date.startswith("2025-") for metric in result.final_backtest.segment_metrics)


def _write_precomputed_seed(
    factor_values_root: Path,
    panel: pd.DataFrame,
    *,
    factor_id: str,
    factor_name: str,
    scores: pd.Series,
) -> None:
    factor_dir = factor_values_root / f"factor_id={factor_id}"
    factor_dir.mkdir(parents=True, exist_ok=True)
    (factor_dir / "2024.metadata.json").write_text(
        json.dumps(
            {
                "factor_id": factor_id,
                "factor_name": factor_name,
                "factor_store_key": f"factor_id={factor_id}",
                "schema_version": "qf.canonical_factor_values.v1",
            }
        ),
        encoding="utf-8",
    )
    payload = panel[["trade_date", "instrument"]].copy()
    payload["factor_id"] = factor_id
    payload["factor_value"] = pd.to_numeric(scores, errors="coerce")
    payload["trade_date"] = pd.to_datetime(payload["trade_date"]).dt.strftime("%Y-%m-%d")
    payload[["trade_date", "instrument", "factor_id", "factor_value"]].to_parquet(
        factor_dir / "2024.parquet",
        index=False,
    )


def _rewrite_demo_panel_to_2025(data_root: Path) -> None:
    panel_path = data_root / PANEL_FILE
    panel = pd.read_parquet(panel_path)
    trade_dates = pd.to_datetime(panel["trade_date"])
    panel["trade_date"] = trade_dates + pd.offsets.DateOffset(years=1)
    panel.to_parquet(panel_path, index=False)

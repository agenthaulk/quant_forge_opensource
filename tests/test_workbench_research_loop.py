from __future__ import annotations

import json
from pathlib import Path
import time

import pytest
import quant_forge.research_loop.service as rd_service
from quant_forge.data.local import create_demo_workspace
from quant_forge.factor_library.repository import FactorRepository
from quant_forge.core.contracts import (
    BacktestResult,
    BacktestSegmentMetric,
    EvaluationResult,
    FactorDefinition,
    MetricValue,
    SimulationProfile,
    TransactionCostModel,
)
from quant_forge.research_loop.reporting import render_research_report
from quant_forge.research_loop.scheduler import ResearchLoopScheduler, ResearchScheduleRequest
from quant_forge.research_loop.service import (
    ResearchCandidateResult,
    ResearchDeduplicationConfig,
    ResearchGate,
    ResearchLoopResult,
    ResearchLoopService,
    ResearchHypothesis,
    ResearchObjectiveWeights,
    ResearchSelfReview,
    ResearchTrialSimulationOverlay,
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
        lambda seed_factor_id, objective, max_candidates, iterations: loop.run_once(
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


def test_research_loop_scheduler_forwards_iteration_count() -> None:
    captured: list[tuple[str, str, int, int]] = []

    def runner(seed_factor_id: str, objective: str, max_candidates: int, iterations: int) -> dict[str, int | str]:
        captured.append((seed_factor_id, objective, max_candidates, iterations))
        return {"seed_factor_id": seed_factor_id, "iterations": iterations}

    scheduler = ResearchLoopScheduler(runner, allowed_interval_days=(1,))

    status = scheduler.start(
        ResearchScheduleRequest(
            seed_factor_id="FTR_DEMO_SMALL_CAP",
            objective="balanced",
            max_candidates=2,
            iterations=3,
        ),
        run_immediately=True,
    )
    scheduler.stop()

    assert captured == [("FTR_DEMO_SMALL_CAP", "balanced", 2, 3)]
    assert status.last_error is None
    assert status.last_result == {"seed_factor_id": "FTR_DEMO_SMALL_CAP", "iterations": 3}


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
    assert result.report_path is not None
    report = result.report_path.read_text(encoding="utf-8")
    assert "No successive-halving trace was recorded for this run." in report
    assert "Parameter search was not enabled for this run." not in report


def test_research_loop_legacy_simulation_profiles_preserve_full_profile_fields(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    profiles = (
        SimulationProfile(
            execution_delay_days=1,
            top_quantile=0.2,
            decay_days=0,
            test_period_start="2024-01-10",
            test_period_end="2024-07-30",
        ),
        SimulationProfile(
            execution_delay_days=2,
            top_quantile=0.3,
            decay_days=1,
            test_period_start="2024-01-15",
            test_period_end="2024-07-25",
        ),
    )
    loop = ResearchLoopService(
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"],
        simulation_profiles=profiles,
    )

    result = loop.run_once("FTR_DEMO_SMALL_CAP", max_candidates=1)

    assert len(result.candidates) == 2
    assert {candidate.backtest.simulation_profile for candidate in result.candidates} == set(profiles)
    assert {candidate.evaluation.simulation_profile for candidate in result.candidates} == set(profiles)
    assert loop.simulation_profiles == profiles


def test_research_loop_disabled_parameter_search_preserves_role_profiles(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    loop = ResearchLoopService(
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"],
        simulation_profile=SimulationProfile(top_quantile=0.25, decay_days=1),
        evaluation_simulation_profile=SimulationProfile(top_quantile=0.10, decay_days=0),
        backtest_simulation_profile=SimulationProfile(top_quantile=0.30, decay_days=4),
        parameter_search_enabled=False,
    )

    result = loop.run_once("FTR_DEMO_SMALL_CAP", max_candidates=1)

    assert result.candidates
    assert {candidate.evaluation.simulation_profile.top_quantile for candidate in result.candidates} == {0.10}
    assert {candidate.evaluation.simulation_profile.decay_days for candidate in result.candidates} == {0}
    assert {candidate.backtest.simulation_profile.top_quantile for candidate in result.candidates} == {0.30}
    assert {candidate.backtest.simulation_profile.decay_days for candidate in result.candidates} == {4}
    assert result.trace_root is not None
    config_snapshot = json.loads((result.trace_root / "config_snapshot.json").read_text(encoding="utf-8"))
    assert config_snapshot["trial_simulation_overlays"] == [{"top_quantile": None, "decay_days": None}]
    assert config_snapshot["effective_trial_configs"][0]["evaluation_profile"]["top_quantile"] == 0.10
    assert config_snapshot["effective_trial_configs"][0]["evaluation_profile"]["decay_days"] == 0
    assert config_snapshot["effective_trial_configs"][0]["backtest_profile"]["top_quantile"] == 0.30
    assert config_snapshot["effective_trial_configs"][0]["backtest_profile"]["decay_days"] == 4


def test_research_loop_trial_overlay_only_replaces_explicit_fields(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    loop = ResearchLoopService(
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"],
        simulation_profile=SimulationProfile(top_quantile=0.25, decay_days=1),
        trial_simulation_overlays=(ResearchTrialSimulationOverlay(top_quantile=0.20),),
        evaluation_simulation_profile=SimulationProfile(top_quantile=0.10, decay_days=0),
        backtest_simulation_profile=SimulationProfile(top_quantile=0.30, decay_days=4),
        parameter_search_enabled=True,
    )

    result = loop.run_once("FTR_DEMO_SMALL_CAP", max_candidates=1)

    assert result.candidates
    assert {candidate.evaluation.simulation_profile.top_quantile for candidate in result.candidates} == {0.20}
    assert {candidate.evaluation.simulation_profile.decay_days for candidate in result.candidates} == {0}
    assert {candidate.backtest.simulation_profile.top_quantile for candidate in result.candidates} == {0.20}
    assert {candidate.backtest.simulation_profile.decay_days for candidate in result.candidates} == {4}


def test_research_loop_trial_overlay_explicit_fields_override_profile_base(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    profile = SimulationProfile(
        execution_delay_days=2,
        top_quantile=0.30,
        decay_days=4,
        test_period_start="2024-01-10",
        test_period_end="2024-07-30",
    )
    loop = ResearchLoopService(
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"],
        trial_simulation_overlays=(ResearchTrialSimulationOverlay(profile=profile, top_quantile=0.20, decay_days=1),),
        evaluation_simulation_profile=SimulationProfile(top_quantile=0.10, decay_days=0),
        backtest_simulation_profile=SimulationProfile(top_quantile=0.30, decay_days=4),
    )

    result = loop.run_once("FTR_DEMO_SMALL_CAP", max_candidates=1)

    assert result.candidates
    effective = result.candidates[0].backtest.simulation_profile
    assert effective.execution_delay_days == 2
    assert effective.test_period_start == "2024-01-10"
    assert effective.test_period_end == "2024-07-30"
    assert effective.top_quantile == 0.20
    assert effective.decay_days == 1


def test_research_loop_single_round_performed_flags_remain_compatible(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    loop = ResearchLoopService(
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"],
    )

    result = loop.run_once("FTR_DEMO_SMALL_CAP", max_candidates=1)

    assert result.optimization_performed is True
    assert result.no_optimization_performed is False
    assert result.accepted_candidate_ids


def test_research_loop_emits_seed_and_candidate_assessment_bundle(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    loop = ResearchLoopService(
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"],
        evaluation_simulation_profile=SimulationProfile(test_period_end="2024-07-01"),
        backtest_simulation_profile=SimulationProfile(test_period_start="2024-07-02"),
    )

    result = loop.run_once("FTR_DEMO_SMALL_CAP", max_candidates=1)

    assert result.seed_assessment is not None
    assert result.seed_assessment.factor_id == "FTR_DEMO_SMALL_CAP"
    assert result.seed_assessment.role == "seed"
    assert result.seed_assessment.selection_backtest.sample_role == "in_sample_backtest"
    assert result.seed_assessment.external_oos_backtest.sample_role == "external_oos_backtest"
    assert result.candidates[0].assessment is not None
    assert result.candidates[0].assessment.role == "candidate"
    assert result.candidates[0].assessment.selection_backtest.sample_role == "in_sample_backtest"
    assert result.candidates[0].assessment.external_oos_backtest.sample_role == "external_oos_backtest"
    assert result.comparison_rows[0]["role"] == "seed"
    assert {row["role"] for row in result.comparison_rows} == {"seed", "candidate"}
    assert all("selection_score" in row for row in result.comparison_rows)
    assert all("external_oos_net_cumulative_return" in row for row in result.comparison_rows)


def test_research_loop_selection_ignores_external_oos_audit(monkeypatch, tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    repo = FactorRepository(paths["factor_root"])

    def fake_backtest(factor_id: str, **kwargs) -> BacktestResult:
        formula = repo.get(factor_id).formula
        sample_role = str(kwargs.get("sample_role") or "external_oos_backtest")
        selection_return = 0.20 if formula == "-rank(volatility_5d)" else 0.05
        external_return = -0.50 if formula == "-rank(volatility_5d)" else 0.50
        value = selection_return if sample_role == "in_sample_backtest" else external_return
        return BacktestResult(
            factor_id=factor_id,
            periods=6,
            holding_days=5,
            cumulative_return=value,
            annualized_return=value,
            annualized_volatility=0.05,
            max_drawdown=-0.01,
            artifact_path=paths["artifact_root"] / f"{factor_id}_{sample_role}.json",
            net_cumulative_return=value,
            net_annualized_return=value,
            net_annualized_volatility=0.05,
            net_long_short_sharpe=value * 10.0,
            net_max_drawdown=-0.01,
            sample_role=sample_role,
        )

    monkeypatch.setattr(rd_service, "run_factor_backtest", fake_backtest)
    loop = ResearchLoopService(
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"],
        deduplication=ResearchDeduplicationConfig(enabled=False),
    )

    result = loop.run_once(
        "FTR_DEMO_SMALL_CAP",
        max_candidates=2,
        weights=ResearchObjectiveWeights(
            weighted_split_icir=0.0,
            rank_ic_mean=0.0,
            rank_icir=0.0,
            annualized_return=1.0,
            max_drawdown=0.0,
        ),
        hypotheses=(
            ResearchHypothesis(
                text="momentum",
                rationale="external OOS looks better but selection is weaker",
                formula_dsl="rank(return_5d)",
            ),
            ResearchHypothesis(
                text="low volatility",
                rationale="selection evidence is stronger",
                formula_dsl="-rank(volatility_5d)",
            ),
        ),
    )

    assert result.candidates[0].factor.formula == "-rank(volatility_5d)"
    assert result.candidates[0].selection_backtest is not None
    assert result.candidates[0].selection_backtest.sample_role == "in_sample_backtest"
    assert result.candidates[0].external_oos_backtest is not None
    assert result.candidates[0].external_oos_backtest.net_cumulative_return == pytest.approx(-0.50)
    assert result.accepted_candidate_ids[0] == result.candidates[0].factor.factor_id


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


def test_research_loop_scheduler_sanitizes_last_error() -> None:
    # SEC-4 regression: a raw exception carrying a local path / internal detail
    # must not surface on the token-gated status; ValueError passes through.
    sensitive = "/home/agent/secret/path leaked internal detail"

    def leaky_runner(seed_factor_id, objective, max_candidates, iterations):
        raise RuntimeError(sensitive)

    scheduler = ResearchLoopScheduler(leaky_runner, allowed_interval_days=(1,))
    scheduler.start(
        ResearchScheduleRequest(seed_factor_id="FTR_DEMO_SMALL_CAP", objective="balanced", max_candidates=1),
        run_immediately=True,
    )
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and scheduler.status().run_count == 0:
        time.sleep(0.05)
    status = scheduler.stop()

    assert status.run_count == 1
    assert status.last_error is not None
    assert sensitive not in status.last_error
    assert "secret" not in status.last_error
    assert status.last_error == "scheduled research run failed"


def test_research_loop_scheduler_passes_value_error_through() -> None:
    # SEC-4: ValueError is user-actionable and mirrors the web allowlist.
    def value_error_runner(seed_factor_id, objective, max_candidates, iterations):
        raise ValueError("bad seed")

    scheduler = ResearchLoopScheduler(value_error_runner, allowed_interval_days=(1,))
    scheduler.start(
        ResearchScheduleRequest(seed_factor_id="FTR_DEMO_SMALL_CAP", objective="balanced", max_candidates=1),
        run_immediately=True,
    )
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and scheduler.status().run_count == 0:
        time.sleep(0.05)
    status = scheduler.stop()

    assert status.run_count == 1
    assert status.last_error == "bad seed"


def test_research_report_renders_metric_status_instead_of_placeholder_zero(tmp_path: Path) -> None:
    factor = FactorDefinition(
        factor_id="FTR_STATUS_DEMO",
        name="status demo",
        formula="rank(return_5d)",
        status="candidate",
        source="research_loop",
    )
    evaluation = EvaluationResult(
        factor_id=factor.factor_id,
        observations=3,
        coverage=1.0,
        rank_ic_mean=0.1,
        rank_ic_std=0.0,
        rank_icir=0.0,
        ic_days=1,
        artifact_path=tmp_path / "evaluation.json",
        rank_ic_t_stat=0.0,
        metrics={
            "rank_ic_mean": MetricValue(
                value=0.1,
                unit="correlation",
                status="available",
                observation_count=1,
            ),
            "rank_icir": MetricValue(
                value=None,
                unit="ratio",
                status="insufficient_sample",
                observation_count=1,
                minimum_required=2,
            ),
            "rank_ic_t_stat": MetricValue(
                value=None,
                unit="t_stat",
                status="insufficient_sample",
                observation_count=1,
                minimum_required=2,
            ),
        },
    )
    backtest = BacktestResult(
        factor_id=factor.factor_id,
        periods=1,
        holding_days=5,
        cumulative_return=0.01,
        annualized_return=0.01,
        annualized_volatility=0.0,
        max_drawdown=0.0,
        artifact_path=tmp_path / "backtest.json",
    )
    candidate = ResearchCandidateResult(
        hypothesis=ResearchHypothesis(
            text="status demo",
            rationale="insufficient-sample metrics must not render as numbers",
            formula_dsl=factor.formula,
        ),
        factor=factor,
        evaluation=evaluation,
        backtest=backtest,
        split_weighted_icir=1.0,
        score=1.0,
        gate_passed=True,
        gate_reasons=(),
        self_review=ResearchSelfReview(
            source="local_self_review",
            summary="status demo",
            strengths=(),
            risks=(),
            next_hypotheses=(),
        ),
    )
    result = ResearchLoopResult(
        rd_stage="research",
        seed_factor_id=factor.factor_id,
        objective="balanced",
        objective_weights=ResearchObjectiveWeights(),
        gate=ResearchGate(),
        candidates=(candidate,),
        accepted_candidate_ids=(factor.factor_id,),
        report_path=tmp_path / "report.md",
        optimization_performed=True,
        no_optimization_performed=False,
    )

    report = render_research_report(result)

    assert "- Rank ICIR: insufficient_sample" in report
    assert "- Rank IC t-stat: insufficient_sample" in report
    assert "- Rank IC: 0.1000" in report
    assert "- Rank ICIR: 0.0000" not in report
    assert "- Rank IC t-stat: 0.0000" not in report
    assert "| insufficient_sample | insufficient_sample " in report

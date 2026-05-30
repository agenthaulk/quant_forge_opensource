from __future__ import annotations

from pathlib import Path
import time

from quant_forge.data.local import create_demo_workspace
from quant_forge.factor_library.repository import FactorRepository
from quant_forge.core.contracts import SimulationProfile
from quant_forge.research_loop.scheduler import ResearchLoopScheduler, ResearchScheduleRequest
from quant_forge.research_loop.service import ResearchGate, ResearchLoopService
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
    assert first.factor.formula == "-rank(market_cap)"
    assert first.factor.status == "candidate"
    assert first.evaluation.observations > 0
    assert first.backtest.periods > 0
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
    )

    passing = loop.run_once("FTR_DEMO_SMALL_CAP", max_candidates=1)
    candidate_id = passing.candidates[0].factor.factor_id
    assert passing.candidates[0].factor.status == "candidate"

    failing = loop.run_once("FTR_DEMO_SMALL_CAP", max_candidates=1, gate=ResearchGate(min_score=9999.0))

    assert failing.candidates[0].factor.factor_id == candidate_id
    assert failing.candidates[0].gate_passed is False
    assert "existing candidate status preserved" in failing.candidates[0].gate_reasons
    assert FactorRepository(paths["factor_root"]).get(candidate_id).status == "candidate"


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

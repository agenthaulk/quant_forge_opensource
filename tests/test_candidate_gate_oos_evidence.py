"""Regression tests: configured OOS gates must not pass on missing OOS evidence (A-P1-3)."""

from __future__ import annotations

from pathlib import Path

from quant_forge.core.contracts import BacktestResult, BacktestSegmentMetric, EvaluationResult
from quant_forge.research_loop.candidate_gate import (
    INSUFFICIENT_OOS_EVIDENCE,
    CandidateGateConfig,
    evaluate_candidate,
)
from quant_forge.research_loop.contracts import FactorExperimentPlan, FactorExperimentResult
from quant_forge.research_loop.service import ResearchGate, apply_gate


def _ready_result(backtest_metrics: dict[str, object] | None = None) -> FactorExperimentResult:
    plan = FactorExperimentPlan(
        plan_id="plan-oos-evidence",
        hypothesis_id="hyp-1",
        status="ready",
        factor_name="Evidence Test Factor",
        formula_dsl="rank(close)",
        expected_direction="positive",
    )
    return FactorExperimentResult(
        plan=plan,
        evaluation_metrics={
            "coverage": 0.9,
            "observations": 10_000,
            "ic_days": 200,
            "rank_ic_mean": 0.05,
            "rank_icir": 0.5,
            "score": 1.0,
        },
        backtest_metrics=dict(backtest_metrics or {}),
    )


def _marker_reasons(reasons: tuple[str, ...]) -> list[str]:
    return [reason for reason in reasons if reason.startswith(INSUFFICIENT_OOS_EVIDENCE)]


def test_min_oos_return_blocks_without_any_segment_evidence() -> None:
    decision = evaluate_candidate(
        _ready_result(),
        CandidateGateConfig(min_oos_net_annualized_return=0.0),
    )
    assert decision.status == "blocked"
    assert _marker_reasons(decision.blocking_reasons)


def test_min_oos_return_blocks_when_oos_values_are_null() -> None:
    metrics = {
        "segment_metrics": [
            {"name": "IS", "net_annualized_return": 0.25},
            {"name": "OOS1", "net_annualized_return": None},
        ]
    }
    decision = evaluate_candidate(
        _ready_result(metrics),
        CandidateGateConfig(min_oos_net_annualized_return=0.0),
    )
    assert decision.status == "blocked"
    assert _marker_reasons(decision.blocking_reasons)


def test_decay_gate_blocks_without_is_or_oos_evidence() -> None:
    decision = evaluate_candidate(
        _ready_result(),
        CandidateGateConfig(max_oos_net_return_decay=0.5),
    )
    assert decision.status == "blocked"
    assert _marker_reasons(decision.blocking_reasons)


def test_missing_evidence_downgrades_to_warning_when_configured() -> None:
    decision = evaluate_candidate(
        _ready_result(),
        CandidateGateConfig(min_oos_net_annualized_return=0.0, missing_oos_evidence_blocks=False),
    )
    assert decision.status == "passed"
    assert _marker_reasons(decision.warnings)


def test_present_oos_evidence_keeps_existing_semantics() -> None:
    metrics = {
        "segment_metrics": [
            {"name": "IS", "net_annualized_return": 0.25},
            {"name": "OOS1", "net_annualized_return": 0.10},
        ]
    }
    clean = evaluate_candidate(
        _ready_result(metrics),
        CandidateGateConfig(min_oos_net_annualized_return=0.0),
    )
    assert not _marker_reasons(clean.blocking_reasons)
    assert not _marker_reasons(clean.warnings)

    below = evaluate_candidate(
        _ready_result(metrics),
        CandidateGateConfig(min_oos_net_annualized_return=0.2),
    )
    assert any("OOS1 net_annualized_return below threshold" in reason for reason in below.blocking_reasons)

    decay_ok = evaluate_candidate(
        _ready_result(metrics),
        CandidateGateConfig(max_oos_net_return_decay=0.9),
    )
    assert not _marker_reasons(decay_ok.blocking_reasons)


def _evaluation_stub() -> EvaluationResult:
    return EvaluationResult(
        factor_id="f",
        observations=1000,
        coverage=0.9,
        rank_ic_mean=0.05,
        rank_ic_std=0.1,
        rank_icir=0.5,
        ic_days=100,
        artifact_path=Path("eval.json"),
    )


def _backtest_stub(segment_metrics: tuple[BacktestSegmentMetric, ...] = ()) -> BacktestResult:
    return BacktestResult(
        factor_id="f",
        periods=10,
        holding_days=5,
        cumulative_return=0.1,
        annualized_return=0.2,
        annualized_volatility=0.1,
        max_drawdown=-0.05,
        artifact_path=Path("bt.json"),
        segment_metrics=segment_metrics,
    )


def _segment(name: str, net_annualized_return: float | None, periods: int = 5) -> BacktestSegmentMetric:
    return BacktestSegmentMetric(
        name=name,
        start_date="2024-01-01",
        end_date="2024-06-30",
        periods=periods,
        gross_cumulative_return=0.1,
        gross_annualized_return=0.2,
        gross_long_short_sharpe=1.0,
        gross_max_drawdown=-0.05,
        net_cumulative_return=0.08,
        net_annualized_return=net_annualized_return,
        net_long_short_sharpe=0.9,
        net_max_drawdown=-0.06,
    )


def test_apply_gate_blocks_when_oos_segments_missing() -> None:
    passed, reasons = apply_gate(
        _evaluation_stub(),
        _backtest_stub(),
        1.0,
        ResearchGate(min_oos_net_annualized_return=0.0),
    )
    assert not passed
    assert any(reason.startswith(INSUFFICIENT_OOS_EVIDENCE) for reason in reasons)


def test_apply_gate_blocks_decay_clause_without_evidence() -> None:
    passed, reasons = apply_gate(
        _evaluation_stub(),
        _backtest_stub(),
        1.0,
        ResearchGate(max_oos_net_return_decay=0.5),
    )
    assert not passed
    assert any(reason.startswith(INSUFFICIENT_OOS_EVIDENCE) for reason in reasons)


def test_apply_gate_with_present_evidence_keeps_existing_semantics() -> None:
    segments = (_segment("IS", 0.25), _segment("OOS1", 0.2))
    passed, reasons = apply_gate(
        _evaluation_stub(),
        _backtest_stub(segments),
        1.0,
        ResearchGate(min_oos_net_annualized_return=0.0, max_oos_net_return_decay=0.5),
    )
    assert passed
    assert not any(reason.startswith(INSUFFICIENT_OOS_EVIDENCE) for reason in reasons)

"""Regression tests: configured OOS gates must not pass on missing OOS evidence (A-P1-3)."""

from __future__ import annotations

from quant_forge.research_loop.candidate_gate import (
    INSUFFICIENT_OOS_EVIDENCE,
    CandidateGateConfig,
    evaluate_candidate,
)
from quant_forge.research_loop.contracts import FactorExperimentPlan, FactorExperimentResult


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

"""Regression tests: configured OOS gates must not pass on missing OOS evidence (A-P1-3)."""

from __future__ import annotations

from pathlib import Path

from quant_forge.core.contracts import BacktestResult, BacktestSegmentMetric, EvaluationResult
from quant_forge.research_loop.candidate_gate import (
    INSUFFICIENT_EVIDENCE,
    INSUFFICIENT_OOS_EVIDENCE,
    CandidateGateConfig,
    SegmentEvidence,
    evaluate_candidate,
    min_oos_return_reasons,
    oos_decay_evidence_available,
    oos_return_evidence,
)
from quant_forge.research_loop.contracts import FactorExperimentPlan, FactorExperimentResult
from quant_forge.research_loop.service import ResearchGate, apply_gate


def _ready_result(
    backtest_metrics: dict[str, object] | None = None,
    correlation_summary: dict[str, object] | None = None,
) -> FactorExperimentResult:
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
        correlation_summary=dict(correlation_summary or {}),
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


def _backtest_stub(
    segment_metrics: tuple[BacktestSegmentMetric, ...] = (),
    *,
    rebalance_rate: float | None = 0.0,
    turnover_rate: float | None = 0.0,
) -> BacktestResult:
    return BacktestResult(
        factor_id="f",
        periods=10,
        holding_days=5,
        cumulative_return=0.1,
        annualized_return=0.2,
        annualized_volatility=0.1,
        max_drawdown=-0.05,
        artifact_path=Path("bt.json"),
        rebalance_rate=rebalance_rate,
        turnover_rate=turnover_rate,
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


# ---------------------------------------------------------------------------
# F-1: retention/turnover/correlation clauses must not pass on missing
# evidence either (same FP-2 class as A-P1-3, quantitative_core_audit.md §5)
# ---------------------------------------------------------------------------


def test_correlation_cap_blocks_without_correlation_evidence() -> None:
    decision = evaluate_candidate(
        _ready_result(),
        CandidateGateConfig(max_abs_corr_with_active=0.5),
    )
    assert decision.status == "blocked"
    assert any(
        reason.startswith(INSUFFICIENT_EVIDENCE) and "max_abs_corr_with_active" in reason
        for reason in decision.blocking_reasons
    )


def test_correlation_cap_keeps_existing_semantics_with_evidence() -> None:
    clean = evaluate_candidate(
        _ready_result(correlation_summary={"max_abs_corr_with_active": 0.3}),
        CandidateGateConfig(max_abs_corr_with_active=0.5),
    )
    assert clean.status == "passed"

    too_high = evaluate_candidate(
        _ready_result(correlation_summary={"max_abs_corr_with_active": 0.6}),
        CandidateGateConfig(max_abs_corr_with_active=0.5),
    )
    assert too_high.status == "blocked"
    assert any("correlation too high" in reason for reason in too_high.blocking_reasons)


def test_turnover_clauses_block_without_evidence() -> None:
    decision = evaluate_candidate(
        _ready_result(),
        CandidateGateConfig(max_turnover_rate=1.0, max_rebalance_rate=0.8),
    )
    assert decision.status == "blocked"
    markers = [
        reason for reason in decision.blocking_reasons if reason.startswith(INSUFFICIENT_EVIDENCE)
    ]
    assert any("max_turnover_rate" in reason for reason in markers)
    assert any("max_rebalance_rate" in reason for reason in markers)

    with_evidence = evaluate_candidate(
        _ready_result({"turnover_rate": 0.5, "rebalance_rate": 0.4}),
        CandidateGateConfig(max_turnover_rate=1.0, max_rebalance_rate=0.8),
    )
    assert with_evidence.status == "passed"


def test_turnover_missing_evidence_rides_the_warning_channel_when_configured() -> None:
    decision = evaluate_candidate(
        _ready_result(),
        CandidateGateConfig(max_turnover_rate=1.0, turnover_blocks_candidate=False),
    )
    assert decision.status == "passed"
    assert any(reason.startswith(INSUFFICIENT_EVIDENCE) for reason in decision.warnings)


def test_apply_gate_turnover_clauses_block_without_evidence() -> None:
    passed, reasons = apply_gate(
        _evaluation_stub(),
        _backtest_stub(rebalance_rate=None, turnover_rate=None),
        1.0,
        ResearchGate(max_turnover_rate=1.0, max_rebalance_rate=0.8),
    )
    assert not passed
    markers = [reason for reason in reasons if reason.startswith(INSUFFICIENT_EVIDENCE)]
    assert any("max_turnover_rate" in reason for reason in markers)
    assert any("max_rebalance_rate" in reason for reason in markers)


# ---------------------------------------------------------------------------
# Zero-period segments are MISSING decay evidence, not usable evidence: the
# evidence-availability check must apply the same usability rule as the
# exceedance check (periods > 0), so a returns-present/periods=0 segment
# fails closed under missing_oos_evidence_blocks instead of silently passing.
# ---------------------------------------------------------------------------


def test_apply_gate_blocks_zero_period_decay_evidence() -> None:
    """Codex xhigh reproduction: IS/OOS1 report returns but periods=0, with
    max_oos_net_return_decay=0.1 configured. The exceedance check rightly
    skips zero-period segments, but the evidence check trusted the bare
    returns and the gate PASSED. It must block with the missing-evidence
    marker instead."""

    passed, reasons = apply_gate(
        _evaluation_stub(),
        _backtest_stub((_segment("IS", 0.25, periods=0), _segment("OOS1", 0.2, periods=0))),
        1.0,
        ResearchGate(max_oos_net_return_decay=0.1),
    )
    assert not passed
    assert any(reason.startswith(INSUFFICIENT_OOS_EVIDENCE) for reason in reasons)


def test_apply_gate_zero_period_decay_evidence_warns_in_warn_mode() -> None:
    passed, reasons = apply_gate(
        _evaluation_stub(),
        _backtest_stub((_segment("IS", 0.25, periods=0), _segment("OOS1", 0.2, periods=0))),
        1.0,
        ResearchGate(max_oos_net_return_decay=0.1, missing_oos_evidence_blocks=False),
    )
    assert passed
    # FP-2/FP-7: the downgrade must stay visible, never silent.
    assert any(reason.startswith(INSUFFICIENT_OOS_EVIDENCE) for reason in reasons)


def test_candidate_gate_treats_zero_period_segments_as_missing_decay_evidence() -> None:
    metrics = {
        "segment_metrics": [
            {"name": "IS", "net_annualized_return": 0.25, "periods": 0},
            {"name": "OOS1", "net_annualized_return": 0.2, "periods": 0},
        ]
    }
    decision = evaluate_candidate(
        _ready_result(metrics),
        CandidateGateConfig(max_oos_net_return_decay=0.1),
    )
    assert decision.status == "blocked"
    assert _marker_reasons(decision.blocking_reasons)


# ---------------------------------------------------------------------------
# F5: the min_oos_net_annualized_return evidence classifier must apply the
# same _usable rule as the decay clause. A zero-period OOS segment that reports
# a non-null return is NOT observed evidence — it is unavailable (fail closed),
# so the two OOS clauses share one 'sufficient evidence' definition.
# ---------------------------------------------------------------------------


def test_oos_return_evidence_zero_period_segment_is_unavailable() -> None:
    zero_period_oos = SegmentEvidence(name="OOS1", net_annualized_return=0.2, periods=0)
    usable_oos = SegmentEvidence(name="OOS2", net_annualized_return=0.15, periods=5)

    evidence = oos_return_evidence((zero_period_oos, usable_oos))

    # The zero-period segment is unavailable, not observed (pre-fix it was
    # counted as observed because its return was non-null).
    assert "OOS1" in evidence.unavailable
    assert all(segment.name != "OOS1" for segment in evidence.observed)
    # A periods>0 segment is still observed.
    assert any(segment.name == "OOS2" for segment in evidence.observed)

    # min_oos clause now agrees with the decay clause: the zero-period OOS
    # segment is missing evidence (the decay-availability check treats it the
    # same, because _usable is False for periods=0).
    is_segment = SegmentEvidence(name="IS", net_annualized_return=0.25, periods=5)
    assert not oos_decay_evidence_available((is_segment, zero_period_oos))

    # With missing_blocks=True the unavailable zero-period segment produces a
    # blocking INSUFFICIENT_OOS_EVIDENCE reason (no warnings).
    blocking, warnings = min_oos_return_reasons(
        oos_return_evidence((zero_period_oos,)),
        0.0,
        missing_blocks=True,
    )
    assert any(reason.startswith(INSUFFICIENT_OOS_EVIDENCE) for reason in blocking)
    assert warnings == ()


def test_candidate_gate_min_oos_blocks_on_zero_period_segment() -> None:
    metrics = {
        "segment_metrics": [
            {"name": "OOS1", "net_annualized_return": 0.2, "periods": 0},
        ]
    }
    decision = evaluate_candidate(
        _ready_result(metrics),
        CandidateGateConfig(min_oos_net_annualized_return=0.0),
    )
    assert decision.status == "blocked"
    assert _marker_reasons(decision.blocking_reasons)

"""F-2 / F-3 (Phase A residual register, quantitative_core_audit.md §5):
one authoritative gate-clause definition shared by the structured candidate
gate and the research smoke gate (FP-5), and segment evidence propagated into
structured backtest metrics so externally configured OOS clauses judge real
segments instead of fail-closing on absent evidence (FP-2)."""

from __future__ import annotations

import json
from pathlib import Path

import quant_forge.research_loop.service as rd_service
from quant_forge.core.contracts import BacktestResult, BacktestSegmentMetric, EvaluationResult
from quant_forge.research_loop.candidate_gate import (
    INSUFFICIENT_EVIDENCE,
    INSUFFICIENT_OOS_EVIDENCE,
    CandidateGateConfig,
    SegmentEvidence,
    evaluate_candidate,
    oos_net_decay_exceeded,
)
from quant_forge.research_loop.contracts import FactorExperimentPlan, FactorExperimentResult
from quant_forge.research_loop.service import ResearchGate, apply_gate


def _ready_result(
    backtest_metrics: dict[str, object] | None = None,
) -> FactorExperimentResult:
    plan = FactorExperimentPlan(
        plan_id="plan-gate-unification",
        hypothesis_id="hyp-1",
        status="ready",
        factor_name="Gate Unification Factor",
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
    annualized_return: float = 0.2,
    net_annualized_return: float | None = None,
    rebalance_rate: float | None = 0.0,
    turnover_rate: float | None = 0.0,
) -> BacktestResult:
    return BacktestResult(
        factor_id="f",
        periods=10,
        holding_days=5,
        cumulative_return=0.1,
        annualized_return=annualized_return,
        annualized_volatility=0.1,
        max_drawdown=-0.05,
        artifact_path=Path("bt.json"),
        net_annualized_return=net_annualized_return,
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


def _segment_dicts(*pairs: tuple[str, float | None]) -> dict[str, object]:
    return {
        "segment_metrics": [
            {"name": name, "net_annualized_return": value, "periods": 5} for name, value in pairs
        ]
    }


# ---------------------------------------------------------------------------
# F-2: both gate surfaces share one clause definition
# ---------------------------------------------------------------------------


def test_null_oos_segment_blocks_identically_on_both_gates() -> None:
    """Pre-unification, the candidate gate passed when ANY one OOS segment had
    a value ('any-one-non-null'); the smoke gate blocked per-segment. Both now
    share the stricter per-segment definition, with the same reason string."""

    expected = f"{INSUFFICIENT_OOS_EVIDENCE}: OOS1 net_annualized_return unavailable"

    candidate = evaluate_candidate(
        _ready_result(_segment_dicts(("IS", 0.25), ("OOS1", None), ("OOS2", 0.15))),
        CandidateGateConfig(min_oos_net_annualized_return=0.0),
    )
    assert candidate.status == "blocked"
    assert expected in candidate.blocking_reasons

    passed, reasons = apply_gate(
        _evaluation_stub(),
        _backtest_stub((_segment("IS", 0.25), _segment("OOS1", None), _segment("OOS2", 0.15))),
        1.0,
        ResearchGate(min_oos_net_annualized_return=0.0),
    )
    assert not passed
    assert expected in reasons


def test_decay_clause_reads_the_knob_as_a_decay_fraction_on_both_gates() -> None:
    """One config quantity, one reading (FP-5): max_oos_net_return_decay bounds
    the decay fraction (IS - OOS) / IS. The smoke gate previously read the same
    knob as a minimum OOS/IS ratio: IS=0.10 -> OOS1=0.06 is a 0.4 decay, which
    the old ratio reading (0.6 >= 0.35) waved through."""

    expected = "OOS net return decay exceeds 0.35"
    segments = (("IS", 0.10), ("OOS1", 0.06))

    candidate = evaluate_candidate(
        _ready_result(_segment_dicts(*segments)),
        CandidateGateConfig(max_oos_net_return_decay=0.35),
    )
    assert candidate.status == "blocked"
    assert expected in candidate.blocking_reasons

    passed, reasons = apply_gate(
        _evaluation_stub(),
        _backtest_stub(tuple(_segment(name, value) for name, value in segments)),
        1.0,
        ResearchGate(max_oos_net_return_decay=0.35),
    )
    assert not passed
    assert expected in reasons


def test_retention_clause_shares_definition_and_missing_evidence_marker() -> None:
    missing_marker = (
        f"{INSUFFICIENT_EVIDENCE}: min_net_return_retention is configured "
        "but net/gross annualized return evidence is missing"
    )

    # Candidate gate: no net/gross evidence at all -> explicit marker, never a
    # fabricated 0.0 or 1.0 retention (FP-4).
    candidate = evaluate_candidate(
        _ready_result(),
        CandidateGateConfig(min_net_return_retention=0.5),
    )
    assert candidate.status == "blocked"
    assert missing_marker in candidate.blocking_reasons

    # Smoke gate: gross observed, net unobserved -> the same marker (the old
    # smoke-gate helper fabricated retention 1.0 here and passed silently).
    passed, reasons = apply_gate(
        _evaluation_stub(),
        _backtest_stub(annualized_return=0.2, net_annualized_return=None),
        1.0,
        ResearchGate(min_net_return_retention=0.5),
    )
    assert not passed
    assert missing_marker in reasons

    # Observed retention below threshold: identical message on both surfaces.
    below_marker = "net_return_retention below threshold: 0.25"
    candidate_below = evaluate_candidate(
        _ready_result({"net_annualized_return": 0.05, "annualized_return": 0.2}),
        CandidateGateConfig(min_net_return_retention=0.5),
    )
    assert below_marker in candidate_below.blocking_reasons
    passed_below, reasons_below = apply_gate(
        _evaluation_stub(),
        _backtest_stub(annualized_return=0.2, net_annualized_return=0.05),
        1.0,
        ResearchGate(min_net_return_retention=0.5),
    )
    assert not passed_below
    assert below_marker in reasons_below


def _turnover_only(reasons: tuple[str, ...]) -> list[str]:
    return [reason for reason in reasons if "rebalance_rate" in reason or "turnover_rate" in reason]


def test_turnover_clauses_share_definition_and_reason_strings() -> None:
    """FP-5 completion: apply_gate consumes the shared candidate_gate turnover
    helper, so both surfaces emit identical reason strings — for observed
    violations and for missing evidence. (Behavior-preserving parity pin: the
    blocking decisions were already identical on both surfaces.)"""

    thresholds = {"max_rebalance_rate": 0.8, "max_turnover_rate": 1.0}

    candidate = evaluate_candidate(
        _ready_result({"rebalance_rate": 0.9, "turnover_rate": 1.5}),
        CandidateGateConfig(**thresholds),
    )
    passed, reasons = apply_gate(
        _evaluation_stub(),
        _backtest_stub(rebalance_rate=0.9, turnover_rate=1.5),
        1.0,
        ResearchGate(**thresholds),
    )
    assert candidate.status == "blocked"
    assert not passed
    expected = [
        "rebalance_rate above threshold: 0.9",
        "turnover_rate above threshold: 1.5",
    ]
    assert _turnover_only(candidate.blocking_reasons) == expected
    assert _turnover_only(reasons) == expected

    candidate_missing = evaluate_candidate(
        _ready_result(),
        CandidateGateConfig(**thresholds),
    )
    passed_missing, reasons_missing = apply_gate(
        _evaluation_stub(),
        _backtest_stub(rebalance_rate=None, turnover_rate=None),
        1.0,
        ResearchGate(**thresholds),
    )
    assert candidate_missing.status == "blocked"
    assert not passed_missing
    missing_candidate = _turnover_only(candidate_missing.blocking_reasons)
    assert missing_candidate == _turnover_only(reasons_missing)
    assert missing_candidate
    assert all(reason.startswith(INSUFFICIENT_EVIDENCE) for reason in missing_candidate)


def test_decay_exceedance_boundary_is_strict_at_the_threshold() -> None:
    """Regression pin (reviewer-verified): a decay fraction exactly equal to
    the threshold is NOT exceedance (strict >); any threshold strictly below
    the observed 0.5 decay is."""

    segments = (
        SegmentEvidence(name="IS", net_annualized_return=0.2, periods=5),
        SegmentEvidence(name="OOS1", net_annualized_return=0.1, periods=5),
    )
    assert oos_net_decay_exceeded(segments, 0.5) is False
    assert oos_net_decay_exceeded(segments, 0.499999) is True


# ---------------------------------------------------------------------------
# F-3: segment evidence propagates into structured backtest metrics
# ---------------------------------------------------------------------------


def test_backtest_metrics_carries_segment_evidence() -> None:
    backtest = _backtest_stub((_segment("IS", 0.25), _segment("OOS1", 0.2)))

    metrics = rd_service._backtest_metrics(backtest)  # noqa: SLF001 - pins the F-3 propagation seam.

    segments = metrics["segment_metrics"]
    assert [segment["name"] for segment in segments] == ["IS", "OOS1"]
    assert segments[1]["net_annualized_return"] == 0.2
    assert segments[1]["periods"] == 5
    # The dict rides trace JSONL and artifacts: it must stay JSON-safe.
    json.dumps(metrics)


def test_externally_configured_oos_clauses_consume_propagated_segments() -> None:
    """The audit's F-3 scenario: an externally configured OOS clause on the
    structured gate must judge the real segments, not fail closed on absent
    evidence when the backtest actually produced segment metrics."""

    backtest = _backtest_stub((_segment("IS", 0.25), _segment("OOS1", 0.2)))
    metrics = rd_service._backtest_metrics(backtest)  # noqa: SLF001 - pins the F-3 propagation seam.

    decision = evaluate_candidate(
        _ready_result(metrics),
        CandidateGateConfig(min_oos_net_annualized_return=0.0, max_oos_net_return_decay=0.9),
    )

    assert decision.status == "passed"
    assert not any(
        reason.startswith((INSUFFICIENT_OOS_EVIDENCE, INSUFFICIENT_EVIDENCE))
        for reason in (*decision.blocking_reasons, *decision.warnings)
    )

"""F-5 (Phase A residual register, quantitative_core_audit.md §5):
``missing_oos_evidence_blocks`` is parseable from rd.yaml, loader-validated,
threads into the research smoke gate as its warn-mode equivalent, and the
default preserves the pre-existing fail-closed behavior."""

from __future__ import annotations

from pathlib import Path

import pytest

from quant_forge.core.contracts import (
    BacktestResult,
    BacktestSegmentMetric,
    EvaluationResult,
    FactorDefinition,
)
from quant_forge.research_loop.candidate_gate import CandidateGateConfig, INSUFFICIENT_OOS_EVIDENCE
from quant_forge.research_loop.config import load_research_loop_config
from quant_forge.research_loop.service import ResearchGate, apply_gate

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _write_config(tmp_path: Path, gate_body: str) -> Path:
    path = tmp_path / "rd.yaml"
    path.write_text("gate:\n" + gate_body, encoding="utf-8")
    return path


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


def _segment(name: str, net_annualized_return: float | None) -> BacktestSegmentMetric:
    return BacktestSegmentMetric(
        name=name,
        start_date="2024-01-01",
        end_date="2024-06-30",
        periods=5,
        gross_cumulative_return=0.1,
        gross_annualized_return=0.2,
        gross_long_short_sharpe=1.0,
        gross_max_drawdown=-0.05,
        net_cumulative_return=0.08,
        net_annualized_return=net_annualized_return,
        net_long_short_sharpe=0.9,
        net_max_drawdown=-0.06,
    )


# ---------------------------------------------------------------------------
# rd.yaml parsing (loader-validated)
# ---------------------------------------------------------------------------


def test_knob_parses_from_rd_yaml(tmp_path: Path) -> None:
    config = load_research_loop_config(
        _write_config(tmp_path, "  missing_oos_evidence_blocks: false\n")
    )
    assert config.gate.missing_oos_evidence_blocks is False


def test_knob_defaults_to_blocking_when_absent(tmp_path: Path) -> None:
    config = load_research_loop_config(_write_config(tmp_path, "  min_ic_days: 5\n"))
    assert config.gate.missing_oos_evidence_blocks is True
    assert ResearchGate().missing_oos_evidence_blocks is True


def test_repo_default_config_preserves_fail_closed_behavior() -> None:
    config = load_research_loop_config(_REPO_ROOT / "configs" / "rd.yaml")
    assert config.gate.missing_oos_evidence_blocks is True


@pytest.mark.parametrize("raw", ['"false"', "1", "[]"])
def test_non_boolean_knob_values_are_rejected(tmp_path: Path, raw: str) -> None:
    path = _write_config(tmp_path, f"  missing_oos_evidence_blocks: {raw}\n")
    with pytest.raises(ValueError, match="missing_oos_evidence_blocks must be a boolean"):
        load_research_loop_config(path)


# ---------------------------------------------------------------------------
# warn-mode threading into the research smoke gate
# ---------------------------------------------------------------------------


def test_warn_mode_downgrades_missing_evidence_to_a_visible_warning() -> None:
    passed, reasons = apply_gate(
        _evaluation_stub(),
        _backtest_stub(),
        1.0,
        ResearchGate(min_oos_net_annualized_return=0.0, missing_oos_evidence_blocks=False),
    )
    assert passed
    assert "passed smoke research gate" in reasons
    # FP-2/FP-7: the downgrade must stay visible, never silent.
    assert any(reason.startswith(INSUFFICIENT_OOS_EVIDENCE) for reason in reasons)


def test_warn_mode_still_blocks_observed_threshold_violations() -> None:
    passed, reasons = apply_gate(
        _evaluation_stub(),
        _backtest_stub((_segment("IS", 0.25), _segment("OOS1", -0.5))),
        1.0,
        ResearchGate(min_oos_net_annualized_return=0.0, missing_oos_evidence_blocks=False),
    )
    assert not passed
    assert any("OOS1 net_annualized_return below threshold" in reason for reason in reasons)


def test_default_mode_blocks_missing_evidence() -> None:
    passed, reasons = apply_gate(
        _evaluation_stub(),
        _backtest_stub(),
        1.0,
        ResearchGate(min_oos_net_annualized_return=0.0),
    )
    assert not passed
    assert any(reason.startswith(INSUFFICIENT_OOS_EVIDENCE) for reason in reasons)


# ---------------------------------------------------------------------------
# strict-bool validation at construction (loader parity)
# ---------------------------------------------------------------------------


def test_direct_construction_rejects_non_boolean_knob() -> None:
    """The YAML loader already rejects non-bool values; direct construction
    must apply the same rule so a quoted "false" cannot silently read truthy."""

    with pytest.raises(ValueError, match="missing_oos_evidence_blocks must be a boolean"):
        ResearchGate(missing_oos_evidence_blocks="false")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="missing_oos_evidence_blocks must be a boolean"):
        CandidateGateConfig(missing_oos_evidence_blocks="false")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# warn-mode messages never ride the blocking channel
# ---------------------------------------------------------------------------


def test_warn_mode_warning_never_rides_the_blocking_channel() -> None:
    """When another clause blocks in warn mode, the INSUFFICIENT_OOS_EVIDENCE
    warning must stay out of the blocking payload: previously apply_gate
    flattened blocking + warnings into one tuple on failure, making the
    warning indistinguishable from a blocker downstream."""

    gate = ResearchGate(
        min_score=5.0,
        min_oos_net_annualized_return=0.0,
        missing_oos_evidence_blocks=False,
    )
    passed, reasons = apply_gate(_evaluation_stub(), _backtest_stub(), 1.0, gate)
    assert not passed
    assert any(reason.startswith("score") for reason in reasons)
    assert not any(reason.startswith(INSUFFICIENT_OOS_EVIDENCE) for reason in reasons)

    from quant_forge.research_loop.service import apply_gate_detailed

    detailed_passed, blocking, warnings = apply_gate_detailed(
        _evaluation_stub(), _backtest_stub(), 1.0, gate
    )
    assert not detailed_passed
    assert any(reason.startswith("score") for reason in blocking)
    assert not any(reason.startswith(INSUFFICIENT_OOS_EVIDENCE) for reason in blocking)
    assert any(reason.startswith(INSUFFICIENT_OOS_EVIDENCE) for reason in warnings)


def test_structured_payload_separates_gate_warnings_from_blockers() -> None:
    """The structured gate decision must carry warn-mode findings in its
    warnings channel, never merged into blocking_reasons (which previously
    received every gate reason on a blocked candidate)."""

    import quant_forge.research_loop.service as rd_service
    from quant_forge.research_loop.service import (
        ResearchCandidateResult,
        ResearchHypothesis,
        ResearchSelfReview,
    )

    warning = (
        f"{INSUFFICIENT_OOS_EVIDENCE}: max_oos_net_return_decay is configured "
        "but IS/OOS net_annualized_return evidence is missing"
    )
    blocker = "score 1.000000 < 5.000000"
    factor = FactorDefinition(factor_id="warn_channel_demo", name="Warn Channel", formula="rank(close)")
    candidate = ResearchCandidateResult(
        hypothesis=ResearchHypothesis(
            text="warn-channel separation",
            rationale="warn-mode messages must not read as blockers",
            formula_dsl=factor.formula,
        ),
        factor=factor,
        evaluation=_evaluation_stub(),
        backtest=_backtest_stub(),
        split_weighted_icir=0.5,
        score=1.0,
        gate_passed=False,
        gate_reasons=(blocker,),
        self_review=ResearchSelfReview(
            source="local_self_review",
            summary="warn-channel separation",
            strengths=(),
            risks=(),
            next_hypotheses=(),
        ),
        gate_warnings=(warning,),
    )

    structured = rd_service._structured_result_from_candidate(candidate, None)  # noqa: SLF001 - pins the seam.
    decision = structured.gate_decision

    assert decision.status == "blocked"
    assert blocker in decision.blocking_reasons
    assert warning not in decision.blocking_reasons
    assert warning in decision.warnings

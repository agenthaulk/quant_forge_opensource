"""F-6 (Phase A residual register): purged-period counts must be persisted.

docs/reviews/quantitative_core_audit.md, deferred follow-up register:
"F-6: purged-period counts not persisted (only warning codes)."

The backtest computes, per segment, how many attributed periods were dropped
because their exit crosses the next segment's first signal date (the A-P1-2
boundary purge). Pre-CP7 only the ``SEGMENT_BOUNDARY_PURGED`` warning code
survived, losing the magnitude. Each segment's metrics map now carries a
``purged_period_count`` MetricValue (FP-7: the statistic travels with its
validity context) which serializes into the backtest artifact, so the count
survives to lineage.
"""

from __future__ import annotations

import json
from pathlib import Path

from quant_forge.backtesting.service import (
    SEGMENT_BOUNDARY_PURGED,
    _segment_metrics,
    run_factor_backtest,
)
from quant_forge.core.contracts import (
    BacktestResult,
    BacktestSegmentMetric,
    EvaluationResult,
    FactorDefinition,
    MetricValue,
    SampleSplitSpec,
)
from quant_forge.data.local import create_demo_workspace
from quant_forge.research_loop.reporting import render_research_report
from quant_forge.research_loop.service import (
    ResearchCandidateResult,
    ResearchGate,
    ResearchHypothesis,
    ResearchLoopResult,
    ResearchObjectiveWeights,
    ResearchSelfReview,
)

SPLITS = (
    SampleSplitSpec(name="IS", fraction=0.5, score_weight=1.0),
    SampleSplitSpec(name="OOS1", fraction=0.5, score_weight=0.0),
)


def _period_row(signal_date: str, exit_date: str, gross: float = 0.02, net: float = 0.015) -> dict[str, object]:
    return {
        "signal_date": signal_date,
        "entry_date": signal_date,
        "exit_date": exit_date,
        "gross_period_return": gross,
        "net_period_return": net,
    }


def test_segment_metrics_carry_purged_period_counts() -> None:
    # Same purge scenario as test_backtest_segment_embargo: the 01-08 and
    # 01-15 IS periods cross the 2024-01-22 OOS1 boundary and are dropped.
    rows = [
        _period_row("2024-01-01", "2024-01-08"),
        _period_row("2024-01-08", "2024-01-22"),
        _period_row("2024-01-15", "2024-01-23"),
        _period_row("2024-01-22", "2024-01-29"),
        _period_row("2024-01-29", "2024-02-05"),
        _period_row("2024-02-05", "2024-02-12"),
    ]
    is_metric, oos_metric = _segment_metrics(rows, 5, SPLITS)

    purged = is_metric.metrics["purged_period_count"]
    assert purged.value == 2.0
    assert purged.status == "available"
    assert purged.unit == "count"
    # FP-7 context: the count is observed over the periods attributed to the
    # segment BEFORE the purge (1 kept + 2 purged).
    assert purged.observation_count == 3
    assert purged.segment == "IS"
    assert purged.method == "segment_boundary_purge_count"
    assert SEGMENT_BOUNDARY_PURGED in purged.warning_codes

    untouched = oos_metric.metrics["purged_period_count"]
    # A true observed zero, not a fabricated placeholder: the boundary rule
    # ran and dropped nothing (FP-4 allows observed zeros, not invented ones).
    assert untouched.value == 0.0
    assert untouched.status == "available"
    assert untouched.warning_codes == ()

    # Conservation: kept + purged == every attributed period, no double count.
    kept_total = is_metric.periods + oos_metric.periods
    purged_total = int(purged.value) + int(untouched.value)
    assert kept_total + purged_total == len(rows)


def test_backtest_artifact_persists_purged_period_counts(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    result = run_factor_backtest(
        "FTR_DEMO_SMALL_CAP",
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"],
    )
    payload = json.loads(result.artifact_path.read_text(encoding="utf-8"))

    segments = payload["segment_metrics"]
    assert segments
    for segment in segments:
        entry = segment["metrics"]["purged_period_count"]
        assert entry["status"] == "available"
        assert entry["value"] is not None
        assert entry["value"] >= 0.0
        assert entry["value"] == int(entry["value"])  # whole periods only
        assert entry["unit"] == "count"
        assert entry["method"] == "segment_boundary_purge_count"
        assert entry["segment"] == segment["name"]
        assert entry["sample_role"] == segment["sample_role"]

    kept_total = sum(segment["periods"] for segment in segments)
    purged_total = sum(int(segment["metrics"]["purged_period_count"]["value"]) for segment in segments)
    assert kept_total + purged_total == payload["periods"]

    # The in-memory contract mirrors the artifact (FP-5: one definition).
    for metric, segment in zip(result.segment_metrics, segments, strict=True):
        persisted = segment["metrics"]["purged_period_count"]
        assert metric.metrics["purged_period_count"].value == persisted["value"]


def _report_segment(name: str, start: str, end: str, periods: int, **overrides: object) -> BacktestSegmentMetric:
    return BacktestSegmentMetric(
        name=name,
        start_date=start,
        end_date=end,
        periods=periods,
        gross_cumulative_return=0.01,
        gross_annualized_return=0.01,
        gross_long_short_sharpe=0.5,
        gross_max_drawdown=-0.01,
        net_cumulative_return=0.005,
        net_annualized_return=0.005,
        net_long_short_sharpe=0.4,
        net_max_drawdown=-0.015,
        **overrides,  # type: ignore[arg-type]
    )


def test_research_report_renders_purged_periods_null_not_zero(tmp_path: Path) -> None:
    # CP7 report surface: the "Purged Periods" column must render the
    # persisted count when the segment carries ``purged_period_count`` and
    # "n/a" for old-style segments without the metric — never a fabricated
    # 0 (FP-4: null is not zero).
    factor = FactorDefinition(
        factor_id="FTR_PURGE_REPORT_DEMO",
        name="purge report demo",
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
    )
    segment_with_count = _report_segment(
        "IS",
        "2024-01-01",
        "2024-01-15",
        3,
        metrics={
            "purged_period_count": MetricValue(
                value=2.0,
                unit="count",
                status="available",
                observation_count=5,
                method="segment_boundary_purge_count",
                segment="IS",
            )
        },
    )
    # No metrics map at all: an old-style artifact predating CP7 purge-count
    # persistence.
    old_style_segment = _report_segment("OOS1", "2024-01-16", "2024-01-31", 4)
    backtest = BacktestResult(
        factor_id=factor.factor_id,
        periods=7,
        holding_days=5,
        cumulative_return=0.01,
        annualized_return=0.01,
        annualized_volatility=0.0,
        max_drawdown=0.0,
        artifact_path=tmp_path / "backtest.json",
        segment_metrics=(segment_with_count, old_style_segment),
    )
    candidate = ResearchCandidateResult(
        hypothesis=ResearchHypothesis(
            text="purge report demo",
            rationale="purged-period counts must survive into the report surface",
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
            summary="purge report demo",
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

    assert (
        "| Segment | Dates | Periods | Purged Periods | Gross Return | Net Return "
        "| Gross Sharpe | Net Sharpe | Net Drawdown |"
    ) in report
    # (a) The persisted count renders as a whole number in its own cell.
    assert "| IS | 2024-01-01 to 2024-01-15 | 3 | 2 |" in report
    # (b) The old-style segment renders "n/a", never a fabricated 0.
    assert "| OOS1 | 2024-01-16 to 2024-01-31 | 4 | n/a |" in report
    assert "| OOS1 | 2024-01-16 to 2024-01-31 | 4 | 0 |" not in report

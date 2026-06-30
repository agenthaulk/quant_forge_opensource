"""Markdown research reports for local RD runs."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from quant_forge.research_loop.service import ResearchCandidateResult, ResearchLoopResult
from quant_forge.utils import write_text


def write_research_report(result: ResearchLoopResult, artifact_root: Path) -> Path:
    """Write one local Markdown report for an RD result."""

    generated_at = datetime.now(UTC)
    report_path = (
        artifact_root.expanduser()
        / "research_reports"
        / f"{_safe_slug(result.seed_factor_id)}_{generated_at.strftime('%Y%m%dT%H%M%S%fZ')}.md"
    )
    write_text(report_path, render_research_report(result, generated_at=generated_at))
    return report_path


def render_research_report(result: ResearchLoopResult, *, generated_at: datetime | None = None) -> str:
    generated = generated_at or datetime.now(UTC)
    best = _best_candidate(result)
    lines: list[str] = [
        "# Quant Forge Research Report",
        "",
        "## Overview",
        "",
        f"- Generated At: {generated.isoformat()}",
        f"- Seed Factor: `{result.seed_factor_id}`",
        f"- Objective: `{result.objective}`",
        f"- Accepted Candidates: {_inline_code_list(result.accepted_candidate_ids)}",
        f"- Candidate Count: {len(result.candidates)}",
        f"- Optimization Performed: {'yes' if result.optimization_performed else 'no'}",
        f"- No Optimization Performed: {'yes' if result.no_optimization_performed else 'no'}",
        "",
        "## Objective Weights",
        "",
        "| Metric | Weight |",
        "| --- | ---: |",
        f"| Weighted Split ICIR | {_fmt(result.objective_weights.weighted_split_icir)} |",
        f"| Rank IC Mean | {_fmt(result.objective_weights.rank_ic_mean)} |",
        f"| Rank ICIR | {_fmt(result.objective_weights.rank_icir)} |",
        f"| Annualized Return | {_fmt(result.objective_weights.annualized_return)} |",
        f"| Max Drawdown | {_fmt(result.objective_weights.max_drawdown)} |",
        "",
        "## Deduplication",
        "",
    ]
    lines.extend(_deduplication_lines(result))
    lines.extend(["## Successive Halving Trace", ""])
    lines.extend(_search_trace_lines(result))
    lines.extend(["## SOTA / Best Candidate", ""])
    if best is None:
        lines.extend(["No candidate was generated.", ""])
    else:
        lines.extend(_candidate_summary(best))
    lines.extend(
        [
            "## Candidate Comparison",
            "",
            "| Factor | Profile | Status | Score | Split ICIR | Rank IC | ICIR | IC t-stat | Gross Return "
            "| Net Return | Net LS Sharpe | Rebalance Rate | Turnover Rate | Gate |",
            "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    if result.candidates:
        for candidate in result.candidates:
            lines.append(_candidate_row(candidate))
    else:
        lines.append(
            "| none | - | - | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.00% "
            "| 0.00% | 0.0000 | 0.00% | 0.00% | - |"
        )
    lines.extend(["", "## Blocked / Skipped Plans", ""])
    lines.extend(_blocked_plan_lines(result))
    lines.extend(["", "## Iteration Trace", ""])
    if result.candidates:
        for index, candidate in enumerate(result.candidates, start=1):
            lines.extend(
                [
                    f"### Step {index}: `{candidate.factor.factor_id}`",
                    "",
                    f"- Hypothesis: {candidate.hypothesis.text}",
                    f"- Rationale: {candidate.hypothesis.rationale}",
                    f"- Formula: `{candidate.factor.formula}`",
                    f"- Formula Fingerprint: `{candidate.formula_fingerprint or '-'}`",
                    f"- Result Signature: `{candidate.result_signature or '-'}`",
                    f"- Candidate Shape Fingerprint: `{candidate.candidate_shape_fingerprint or '-'}`",
                    f"- Gate: {'passed' if candidate.gate_passed else 'failed'}",
                    f"- Gate Reasons: {'; '.join(candidate.gate_reasons)}",
                    f"- Self Review: {candidate.self_review.summary}",
                    f"- Review Normalization: {_sentence_list(candidate.self_review.normalization_warnings)}",
                    "",
                ]
            )
    else:
        lines.extend(["No iteration steps were recorded.", ""])
    lines.extend(["## Candidate Details", ""])
    for candidate in result.candidates:
        lines.extend(_candidate_detail(candidate))
    lines.extend(_conclusion_lines(best))
    if result.no_optimization_performed:
        lines.extend(
            [
                "## No Optimization Performed",
                "",
                "The RD run did not produce a formula or profile variant that differs from the seed. "
                "Treat this as a failed or smoke-only research attempt, not as an optimized factor.",
                "",
            ]
        )
    lines.extend(
        [
            "## Risk Notes",
            "",
            "- This report is a local research artifact, not an investment recommendation.",
            "- Active promotion remains a separate user decision.",
            "- Rebalance rate measures long/short membership changes per rebalance.",
            "- Turnover rate estimates true portfolio turnover from weight changes.",
            "- Net returns apply configured research cost assumptions only; they are not production execution results.",
            "- Evaluation labels use future returns for research scoring; backtests keep next-trading-day "
            "entry semantics.",
            "",
        ]
    )
    return "\n".join(lines)


def _candidate_summary(candidate: ResearchCandidateResult) -> list[str]:
    return [
        f"- Factor: `{candidate.factor.factor_id}`",
        f"- Name: `{candidate.factor.name}`",
        f"- Formula: `{candidate.factor.formula}`",
        f"- Status: `{candidate.factor.status}`",
        f"- Formula Fingerprint: `{candidate.formula_fingerprint or '-'}`",
        f"- Result Signature: `{candidate.result_signature or '-'}`",
        f"- Score: {_fmt(candidate.score)}",
        f"- Weighted Split ICIR: {_fmt(candidate.split_weighted_icir)}",
        f"- Rank IC: {_fmt(candidate.evaluation.rank_ic_mean)}",
        f"- Rank ICIR: {_fmt(candidate.evaluation.rank_icir)}",
        f"- Rank IC t-stat: {_fmt(candidate.evaluation.rank_ic_t_stat)}",
        f"- Gross Annualized Return: {_pct(candidate.backtest.annualized_return)}",
        f"- Net Annualized Return: {_pct(candidate.backtest.net_annualized_return)}",
        f"- Net Long-Short Sharpe: {_fmt(candidate.backtest.net_long_short_sharpe)}",
        f"- Rebalance Rate: {_pct(candidate.backtest.rebalance_rate)}",
        f"- Turnover Rate: {_pct(candidate.backtest.turnover_rate)}",
        f"- Simulation Profile: {_profile_label(candidate)}",
        "",
    ]


def _deduplication_lines(result: ResearchLoopResult) -> list[str]:
    summary = result.deduplication or {}
    if not summary:
        return ["No duplicate-control summary was recorded.", ""]
    enabled = "yes" if summary.get("enabled") else "no"
    return [
        f"- Enabled: {enabled}",
        f"- Formula Fingerprint Skips: {int(summary.get('formula_skipped') or 0)}",
        f"- Candidate Diversity Skips: {int(summary.get('diversity_skipped') or 0)}",
        f"- Result Signature Duplicates: {int(summary.get('result_duplicates') or 0)}",
        "",
    ]


def _search_trace_lines(result: ResearchLoopResult) -> list[str]:
    if not result.search_trace:
        return ["No successive-halving trace was recorded for this run.", ""]
    lines = [
        "| Rank | Survived | Factor | Profile | Score | Split ICIR |",
        "| ---: | --- | --- | --- | ---: | ---: |",
    ]
    for item in result.search_trace:
        survived = "yes" if item.survived else "no"
        lines.append(
            f"| {item.rank} | {survived} | `{item.factor_id}` | {_profile_label_from_profile(item.simulation_profile)} "
            f"| {_fmt(item.score)} | {_fmt(item.split_weighted_icir)} |"
        )
    lines.append("")
    return lines


def _blocked_plan_lines(result: ResearchLoopResult) -> list[str]:
    if not result.blocked_plans:
        return ["No plans were blocked or skipped.", ""]
    lines = [
        "| Plan | Status | Formula | Reason |",
        "| --- | --- | --- | --- |",
    ]
    for item in result.blocked_plans:
        reason = "; ".join(item.plan.blocking_reasons) or item.error or "-"
        lines.append(
            f"| `{item.plan.plan_id}` | {item.plan.status} | `{item.plan.formula_dsl or '-'}` | {reason} |"
        )
    lines.append("")
    return lines


def _candidate_row(candidate: ResearchCandidateResult) -> str:
    gate = "passed" if candidate.gate_passed else "failed"
    return (
        f"| `{candidate.factor.factor_id}` | {_profile_label(candidate)} | {candidate.factor.status} "
        f"| {_fmt(candidate.score)} "
        f"| {_fmt(candidate.split_weighted_icir)} | {_fmt(candidate.evaluation.rank_ic_mean)} "
        f"| {_fmt(candidate.evaluation.rank_icir)} | {_fmt(candidate.evaluation.rank_ic_t_stat)} "
        f"| {_pct(candidate.backtest.annualized_return)} "
        f"| {_pct(candidate.backtest.net_annualized_return)} | {_fmt(candidate.backtest.net_long_short_sharpe)} "
        f"| {_pct(candidate.backtest.rebalance_rate)} "
        f"| {_pct(candidate.backtest.turnover_rate)} | {gate} |"
    )


def _candidate_detail(candidate: ResearchCandidateResult) -> list[str]:
    lines: list[str] = [
        f"### `{candidate.factor.factor_id}`",
        "",
        f"- Formula: `{candidate.factor.formula}`",
        f"- Formula Fingerprint: `{candidate.formula_fingerprint or '-'}`",
        f"- Result Signature: `{candidate.result_signature or '-'}`",
        f"- Candidate Shape Fingerprint: `{candidate.candidate_shape_fingerprint or '-'}`",
        f"- Horizon Days: {candidate.factor.horizon_days}",
        f"- Filters: {_inline_code_list(candidate.factor.universe_filters)}",
        f"- Simulation Profile: {_profile_label(candidate)}",
        f"- Evaluation Artifact: `{_artifact_label(candidate.evaluation.artifact_path)}`",
        f"- Backtest Artifact: `{_artifact_label(candidate.backtest.artifact_path)}`",
        f"- Backtest Warnings: {_sentence_list(candidate.backtest.warnings)}",
        f"- Research Assumptions: {_sentence_list(candidate.backtest.assumptions)}",
        "",
        "#### Self Review",
        "",
        f"- Summary: {candidate.self_review.summary}",
        f"- Strengths: {_sentence_list(candidate.self_review.strengths)}",
        f"- Risks: {_sentence_list(candidate.self_review.risks)}",
        f"- Next Hypotheses: {_sentence_list(candidate.self_review.next_hypotheses)}",
        f"- Normalization Warnings: {_sentence_list(candidate.self_review.normalization_warnings)}",
        "",
        "#### Split Metrics",
        "",
        "| Split | Dates | IC Days | Coverage | Rank IC | ICIR | IC t-stat | Weight |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for metric in candidate.evaluation.split_metrics:
        lines.append(
            f"| {metric.name} | {metric.start_date} to {metric.end_date} | {metric.ic_days} "
            f"| {_pct(metric.coverage)} | {_fmt(metric.rank_ic_mean)} | {_fmt(metric.rank_icir)} "
            f"| {_fmt(metric.rank_ic_t_stat)} "
            f"| {_fmt(metric.score_weight)} |"
        )
    lines.extend(
        [
            "",
            "#### Horizon Matrix",
            "",
            "| Horizon | Observations | Coverage | Rank IC | ICIR | IC t-stat | IC Days |",
            "| ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for metric in candidate.evaluation.horizon_metrics:
        lines.append(
            f"| {metric.horizon_days} | {metric.observations} | {_pct(metric.coverage)} "
            f"| {_fmt(metric.rank_ic_mean)} | {_fmt(metric.rank_icir)} "
            f"| {_fmt(metric.rank_ic_t_stat)} | {metric.ic_days} |"
        )
    lines.extend(
        [
            "",
            "#### Backtest Segments",
            "",
            "| Segment | Dates | Periods | Gross Return | Net Return | Gross Sharpe | Net Sharpe | Net Drawdown |",
            "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for metric in candidate.backtest.segment_metrics:
        lines.append(
            f"| {metric.name} | {metric.start_date} to {metric.end_date} | {metric.periods} "
            f"| {_pct(metric.gross_annualized_return)} | {_pct(metric.net_annualized_return)} "
            f"| {_fmt(metric.gross_long_short_sharpe)} | {_fmt(metric.net_long_short_sharpe)} "
            f"| {_pct(metric.net_max_drawdown)} |"
        )
    lines.extend(
        [
            "",
            "#### Group Returns",
            "",
            "| Group | Mean Return | Periods |",
            "| --- | ---: | ---: |",
        ]
    )
    for metric in candidate.backtest.group_returns:
        lines.append(f"| {metric.group} | {_pct(metric.mean_return)} | {metric.periods} |")
    lines.append("")
    return lines


def _best_candidate(result: ResearchLoopResult) -> ResearchCandidateResult | None:
    if not result.candidates:
        return None
    accepted = [candidate for candidate in result.candidates if candidate.gate_passed]
    pool = accepted or list(result.candidates)
    return max(pool, key=lambda candidate: candidate.score)


def _conclusion_lines(candidate: ResearchCandidateResult | None) -> list[str]:
    lines = ["## Conclusion And Recommendations", ""]
    if candidate is None:
        lines.extend(
            [
                "- No candidate was generated, so no research conclusion is available.",
                "- Check the hypothesis generator and parser before running another RD cycle.",
                "",
            ]
        )
        return lines
    decision = "candidate queue" if candidate.gate_passed else "manual review queue"
    lines.extend(
        [
            f"- Best current candidate: `{candidate.factor.factor_id}`.",
            f"- Suggested next queue: {decision}.",
            f"- Primary evidence: score {_fmt(candidate.score)}, weighted split ICIR "
            f"{_fmt(candidate.split_weighted_icir)}, Rank IC {_fmt(candidate.evaluation.rank_ic_mean)}.",
            "- Next research step: review the self-review hypotheses before starting another iteration.",
            "",
        ]
    )
    return lines


def _inline_code_list(values: tuple[str, ...]) -> str:
    if not values:
        return "none"
    return ", ".join(f"`{value}`" for value in values)


def _sentence_list(values: tuple[str, ...]) -> str:
    if not values:
        return "none"
    return "; ".join(values)


def _profile_label(candidate: ResearchCandidateResult) -> str:
    return _profile_label_from_profile(candidate.backtest.simulation_profile)


def _artifact_label(path: Path | str) -> str:
    name = Path(path).name
    return name or "<artifact>"


def _profile_label_from_profile(profile) -> str:
    return f"top={_fmt(profile.top_quantile)}, decay={profile.decay_days}, delay={profile.execution_delay_days}"


def _fmt(value: float) -> str:
    return f"{value:.4f}"


def _pct(value: float) -> str:
    return f"{value * 100:.2f}%"


def _safe_slug(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in value.strip())
    return cleaned or "research_report"

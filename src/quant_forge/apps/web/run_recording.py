"""Web-side run-recording wiring for the validate and multi-factor backtest workflows.

Split out of :mod:`quant_forge.apps.web.api` per DECISIONS.md D12 (the
2026-07-13 agent-sidecar frontend design ruling): api.py is frozen — new
endpoints/code land in new modules, api.py may only shrink. The BUG #007
web-run-recording wiring (closing "pure-web runs never reached the RunIndex,
leaving the registry evidence chain and research-history panel permanently
empty for web users") was added directly to api.py in commit fe23744, before
D12 existed. This module is its new home.

The bodies below are moved verbatim from api.py; behavior is unchanged. The
call sites that invoke them (with their BUG #007 / PF-F3 / PF-F4 rationale
comments and last-look ``_raise_if_cancelled`` checkpoints) are intentionally
left in place in api.py — that placement is review-hardened and documents
control-flow decisions that belong with the calling workflow, not the
recording helper. Only these definitions, and the imports used exclusively by
them, moved.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from quant_forge.apps.web.jobs import _IdeaValidationSettings
from quant_forge.config import QuantForgeConfig
from quant_forge.core.contracts import BacktestResult, EvaluationResult, FactorDefinition
from quant_forge.lineage.recording import record_run
from quant_forge.lineage.store import metric_highlight
from quant_forge.research_loop.config import ResearchLoopConfig
from quant_forge.synthesis.service import CompositeBacktestRun
from quant_forge.workbench.service import (
    BACKTEST_HIGHLIGHT_METRICS,
    EVALUATION_HIGHLIGHT_METRICS,
    backtest_data_window,
    evaluation_data_window,
    result_warnings_count,
)

if TYPE_CHECKING:
    from quant_forge.apps.web.api import _MultiFactorBacktestPlan


def _record_validate_factor_runs(
    config: QuantForgeConfig,
    factor: FactorDefinition,
    settings: _IdeaValidationSettings,
    rd_config: ResearchLoopConfig,
    *,
    evaluation: EvaluationResult,
    in_sample_backtest: BacktestResult,
    backtest: BacktestResult,
) -> None:
    """Give a web-originated validate run the same lineage/run-index trail a
    CLI/workbench run leaves (BUG #007: web runs never reached RunIndex, so
    the registry evidence chain and 研究历史/research-history panel stayed
    empty for web-only users).

    Mirrors ``WorkbenchService.evaluate`` / ``run_backtest`` exactly: the same
    highlight metric sets (``EVALUATION_HIGHLIGHT_METRICS`` /
    ``BACKTEST_HIGHLIGHT_METRICS``), the same ``data_window`` / warnings-count
    builders, and the same shared ``record_run`` helper the workbench service
    now calls too, so a factor validated through the web leaves the same kind
    ("evaluate" / "backtest") and highlight shape a CLI run of the same
    artifact type would.
    """

    sample_splits_payload = [asdict(split) for split in rd_config.sample_splits] if rd_config.sample_splits else None
    record_run(
        factor_root=config.paths.factor_root,
        artifact_root=config.paths.artifact_root,
        factor_values_root=config.paths.factor_values_root,
        factor_values_manifest_root=config.paths.factor_values_manifest_root,
        kind="evaluate",
        factor_id=factor.factor_id,
        artifact_type="evaluation",
        artifact_path=evaluation.artifact_path,
        generated_by="web.validate_factor.evaluate",
        request={
            "kind": "evaluate",
            "factor_id": factor.factor_id,
            "horizon_days": settings.holding_days,
            "horizon_days_matrix": list(rd_config.horizon_days_matrix) if rd_config.horizon_days_matrix else None,
            "sample_splits": sample_splits_payload,
            "simulation_profile": asdict(settings.evaluation_profile),
        },
        metric_highlights={
            name: metric_highlight(evaluation.metrics[name])
            for name in EVALUATION_HIGHLIGHT_METRICS
            if name in evaluation.metrics
        },
        data_window=evaluation_data_window(evaluation),
        warnings_count=result_warnings_count(evaluation),
    )
    for sample_role, profile, result in (
        ("in_sample_backtest", settings.evaluation_profile, in_sample_backtest),
        ("external_oos_backtest", settings.backtest_profile, backtest),
    ):
        record_run(
            factor_root=config.paths.factor_root,
            artifact_root=config.paths.artifact_root,
            factor_values_root=config.paths.factor_values_root,
            factor_values_manifest_root=config.paths.factor_values_manifest_root,
            kind="backtest",
            factor_id=factor.factor_id,
            artifact_type="backtest",
            artifact_path=result.artifact_path,
            generated_by=f"web.validate_factor.{sample_role}",
            request={
                "kind": "backtest",
                "sample_role": sample_role,
                "factor_id": factor.factor_id,
                "holding_days": settings.holding_days,
                "include_partial_final_period": settings.include_partial_final_period,
                "sample_splits": sample_splits_payload,
                "simulation_profile": asdict(profile),
                "transaction_costs": asdict(settings.transaction_costs),
            },
            metric_highlights={
                name: metric_highlight(result.metrics[name])
                for name in BACKTEST_HIGHLIGHT_METRICS
                if name in result.metrics
            },
            data_window=backtest_data_window(result),
            warnings_count=result_warnings_count(result),
        )


def _staggered_data_window(result: dict[str, Any]) -> dict[str, str | None]:
    """Observed staggered-aggregate NAV window; unavailable when there is no NAV."""

    nav_rows = result.get("daily_nav") or []
    dates = [str(row["date"]) for row in nav_rows if isinstance(row, dict) and row.get("date")]
    if dates:
        return {"start_date": min(dates), "end_date": max(dates), "status": "available"}
    return {"start_date": None, "end_date": None, "status": "unavailable"}


def _staggered_warnings_count(result: dict[str, Any]) -> int:
    return len({str(code) for code in (result.get("warning_codes") or [])})


def _record_multi_factor_backtest_run(
    config: QuantForgeConfig, plan: _MultiFactorBacktestPlan, run: CompositeBacktestRun
) -> None:
    """Record the composite backtest under ``run.composite_id`` (BUG #007).

    Mirrors ``WorkbenchService.run_backtest``'s kind/highlight semantics for a
    ``BacktestResult`` (``kind="backtest"``, ``BACKTEST_HIGHLIGHT_METRICS``,
    ``backtest_data_window``) so a synthesized ``COMPOSITE_*`` factor gets the
    same registry evidence chain a single-factor backtest gets, using the
    shared ``record_run`` helper.
    """

    record_run(
        factor_root=config.paths.factor_root,
        artifact_root=config.paths.artifact_root,
        factor_values_root=config.paths.factor_values_root,
        factor_values_manifest_root=config.paths.factor_values_manifest_root,
        kind="backtest",
        factor_id=run.composite_id,
        artifact_type="backtest",
        artifact_path=run.result.artifact_path,
        generated_by="web.multi_factor_backtest",
        request={
            "kind": "multi_factor_backtest",
            "composite_id": run.composite_id,
            "factor_refs": [{"factor_id": factor_id, "direction": direction} for factor_id, direction in plan.factor_refs],
            "method": plan.method.name,
            "method_params": plan.method_params,
            "weights": plan.weights,
            "standardization": plan.standardization,
            "holding_days": plan.settings.holding_days,
            "include_partial_final_period": plan.settings.include_partial_final_period,
            "simulation_profile": asdict(plan.settings.profile),
            "transaction_costs": asdict(plan.settings.transaction_costs),
            "universe_filters": list(plan.universe_filters),
        },
        metric_highlights={
            name: metric_highlight(run.result.metrics[name])
            for name in BACKTEST_HIGHLIGHT_METRICS
            if name in run.result.metrics
        },
        data_window=backtest_data_window(run.result),
        warnings_count=result_warnings_count(run.result),
    )

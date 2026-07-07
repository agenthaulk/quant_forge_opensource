"""Thin orchestration layer over public kernel services."""

from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from quant_forge.backtesting.service import run_factor_backtest
from quant_forge.core.contracts import (
    BacktestResult,
    EvaluationResult,
    FactorDefinition,
    SampleSplitSpec,
    SimulationProfile,
    TransactionCostModel,
)
from quant_forge.evaluation.falsification import FalsificationReport
from quant_forge.evaluation.falsification import run_falsification as run_falsification_diagnostics
from quant_forge.evaluation.service import evaluate_factor, falsification_frame
from quant_forge.factor_engine.signal_processing import simulation_profile_suffix
from quant_forge.factor_library.catalog import FactorCatalog
from quant_forge.factor_library.repository import FactorRepository, parse_idea_to_definition
from quant_forge.lineage.store import (
    LineageRecord,
    LineageStore,
    RunIndex,
    canonical_fingerprint,
    locate_factor_definition_file,
    metric_highlight,
    new_run_id,
)
from quant_forge.utils import write_json

EVALUATION_HIGHLIGHT_METRICS = ("rank_ic_mean", "rank_icir", "rank_ic_t_stat")
BACKTEST_HIGHLIGHT_METRICS = (
    "annualized_return",
    "net_annualized_return",
    "net_long_short_sharpe",
    "net_max_drawdown",
    "rebalance_turnover_mean",
)
FALSIFICATION_HIGHLIGHT_METRICS = ("placebo_percentile", "ic_half_life_days", "block_sign_consistency")


def evaluation_data_window(result: EvaluationResult) -> dict[str, str | None]:
    """Observed evaluation window; unavailable (null dates) when unknown."""

    starts = [metric.start_date for metric in result.split_metrics if metric.start_date]
    ends = [metric.end_date for metric in result.split_metrics if metric.end_date]
    if starts and ends:
        return {"start_date": min(starts), "end_date": max(ends), "status": "available"}
    return {"start_date": None, "end_date": None, "status": "unavailable"}


def backtest_data_window(result: BacktestResult) -> dict[str, str | None]:
    """Observed backtest window from daily NAV or segments; else unavailable."""

    nav_dates = [str(row["date"]) for row in result.daily_nav if row.get("date")]
    if nav_dates:
        return {"start_date": nav_dates[0], "end_date": nav_dates[-1], "status": "available"}
    starts = [segment.start_date for segment in result.segment_metrics if segment.start_date]
    ends = [segment.end_date for segment in result.segment_metrics if segment.end_date]
    if starts and ends:
        return {"start_date": min(starts), "end_date": max(ends), "status": "available"}
    return {"start_date": None, "end_date": None, "status": "unavailable"}


def falsification_data_window(frame: pd.DataFrame) -> dict[str, str | None]:
    """Observed window of labeled (forward-return-bearing) dates; else unavailable."""

    labeled_dates = pd.to_datetime(frame.dropna(subset=["forward_return"])["trade_date"])
    if len(labeled_dates):
        return {
            "start_date": labeled_dates.min().date().isoformat(),
            "end_date": labeled_dates.max().date().isoformat(),
            "status": "available",
        }
    return {"start_date": None, "end_date": None, "status": "unavailable"}


def result_warnings_count(result: EvaluationResult | BacktestResult | FalsificationReport) -> int:
    """Distinct warning messages plus distinct warning codes."""

    return len(set(result.warnings)) + len(set(result.warning_codes))


class WorkbenchService:
    def __init__(
        self,
        *,
        factor_root: Path,
        data_root: Path,
        artifact_root: Path,
        factor_values_root: Path | None = None,
        factor_values_overlay_root: Path | None = None,
        factor_values_manifest_root: Path | None = None,
        simulation_profile: SimulationProfile | None = None,
        transaction_costs: TransactionCostModel | None = None,
        sample_splits: tuple[SampleSplitSpec, ...] | None = None,
        horizon_days_matrix: tuple[int, ...] | None = None,
    ) -> None:
        self.factor_root = factor_root
        self.data_root = data_root
        self.artifact_root = artifact_root
        self.factor_values_root = factor_values_root
        self.factor_values_overlay_root = factor_values_overlay_root
        self.factor_values_manifest_root = factor_values_manifest_root
        self.simulation_profile = simulation_profile or SimulationProfile()
        self.transaction_costs = transaction_costs or TransactionCostModel()
        self.sample_splits = sample_splits
        self.horizon_days_matrix = horizon_days_matrix

    def list_factors(self) -> list[FactorDefinition]:
        return FactorCatalog(
            self.factor_root,
            factor_values_root=self.factor_values_root,
            factor_values_manifest_root=self.factor_values_manifest_root,
        ).list()

    def idea_to_factor(self, text: str) -> FactorDefinition:
        factor = parse_idea_to_definition(text)
        FactorRepository(self.factor_root).save(factor)
        return factor

    def evaluate(
        self, factor_id: str, *, horizon_days: int | None = None, simulation_profile: SimulationProfile | None = None
    ) -> EvaluationResult:
        profile = simulation_profile or self.simulation_profile
        result = evaluate_factor(
            factor_id,
            factor_root=self.factor_root,
            data_root=self.data_root,
            artifact_root=self.artifact_root,
            horizon_days=horizon_days,
            horizon_days_matrix=self.horizon_days_matrix,
            sample_splits=self.sample_splits,
            simulation_profile=profile,
            factor_values_root=self.factor_values_root,
            factor_values_overlay_root=self.factor_values_overlay_root,
            factor_values_manifest_root=self.factor_values_manifest_root,
        )
        self._record_run(
            kind="evaluate",
            factor_id=factor_id,
            artifact_type="evaluation",
            artifact_path=result.artifact_path,
            generated_by="workbench.evaluate",
            request={
                "kind": "evaluate",
                "factor_id": factor_id,
                "horizon_days": horizon_days,
                "horizon_days_matrix": list(self.horizon_days_matrix) if self.horizon_days_matrix else None,
                "sample_splits": [asdict(split) for split in self.sample_splits] if self.sample_splits else None,
                "simulation_profile": asdict(profile),
            },
            metric_highlights={
                name: metric_highlight(result.metrics[name])
                for name in EVALUATION_HIGHLIGHT_METRICS
                if name in result.metrics
            },
            data_window=evaluation_data_window(result),
            warnings_count=result_warnings_count(result),
        )
        return result

    def run_backtest(
        self,
        factor_id: str,
        *,
        top_quantile: float | None = None,
        holding_days: int | None = None,
        include_partial_final_period: bool = False,
    ) -> BacktestResult:
        profile = self.simulation_profile
        if top_quantile is not None:
            profile = replace(profile, top_quantile=top_quantile)
        result = run_factor_backtest(
            factor_id,
            factor_root=self.factor_root,
            data_root=self.data_root,
            artifact_root=self.artifact_root,
            simulation_profile=profile,
            holding_days=holding_days,
            transaction_costs=self.transaction_costs,
            sample_splits=self.sample_splits,
            factor_values_root=self.factor_values_root,
            factor_values_overlay_root=self.factor_values_overlay_root,
            factor_values_manifest_root=self.factor_values_manifest_root,
            include_partial_final_period=include_partial_final_period,
        )
        self._record_run(
            kind="backtest",
            factor_id=factor_id,
            artifact_type="backtest",
            artifact_path=result.artifact_path,
            generated_by="workbench.run_backtest",
            request={
                "kind": "backtest",
                "factor_id": factor_id,
                "holding_days": holding_days,
                "include_partial_final_period": include_partial_final_period,
                "sample_splits": [asdict(split) for split in self.sample_splits] if self.sample_splits else None,
                "simulation_profile": asdict(profile),
                "transaction_costs": asdict(self.transaction_costs),
            },
            metric_highlights={
                name: metric_highlight(result.metrics[name])
                for name in BACKTEST_HIGHLIGHT_METRICS
                if name in result.metrics
            },
            data_window=backtest_data_window(result),
            warnings_count=result_warnings_count(result),
        )
        return result

    def run_falsification(
        self,
        factor_id: str,
        *,
        seed: int,
        horizon_days: int | None = None,
    ) -> FalsificationReport:
        """Run advisory falsification diagnostics on the evaluation IC input.

        The score/label frame is built exactly as :meth:`evaluate` builds its
        IC input (same execution-delay/horizon alignment), the report is
        written as a JSON artifact with status-carrying MetricValues, and the
        run is indexed with a factor-definition -> falsification lineage edge.
        Advisory only: nothing here feeds a promotion gate.
        """

        profile = self.simulation_profile
        factor = FactorCatalog(
            self.factor_root,
            factor_values_root=self.factor_values_root,
            factor_values_manifest_root=self.factor_values_manifest_root,
        ).get(factor_id)
        horizon = horizon_days or factor.horizon_days
        frame = falsification_frame(
            factor_id,
            factor_root=self.factor_root,
            data_root=self.data_root,
            horizon_days=horizon,
            simulation_profile=profile,
            factor_values_root=self.factor_values_root,
            factor_values_overlay_root=self.factor_values_overlay_root,
            factor_values_manifest_root=self.factor_values_manifest_root,
        )
        report = run_falsification_diagnostics(frame, seed=seed)
        artifact_path = (
            self.artifact_root.expanduser()
            / "falsification"
            / f"{factor_id}{simulation_profile_suffix(profile)}.json"
        )
        write_json(
            artifact_path,
            {
                "factor_id": factor_id,
                "formula": factor.formula,
                "horizon_days": horizon,
                "execution_delay_days": profile.execution_delay_days,
                "simulation_profile": asdict(profile),
                **asdict(report),
            },
        )
        self._record_run(
            kind="falsification",
            factor_id=factor_id,
            artifact_type="falsification",
            artifact_path=artifact_path,
            generated_by="workbench.run_falsification",
            request={
                "kind": "falsification",
                "factor_id": factor_id,
                "seed": seed,
                "horizon_days": horizon,
                "simulation_profile": asdict(profile),
            },
            metric_highlights={
                name: metric_highlight(getattr(report, name)) for name in FALSIFICATION_HIGHLIGHT_METRICS
            },
            data_window=falsification_data_window(frame),
            warnings_count=result_warnings_count(report),
        )
        return report

    def _record_run(
        self,
        *,
        kind: str,
        factor_id: str,
        artifact_type: str,
        artifact_path: Path,
        generated_by: str,
        request: dict[str, object],
        metric_highlights: dict[str, dict[str, object]],
        data_window: dict[str, str | None],
        warnings_count: int,
    ) -> None:
        # created_at comes from the adapter layer by design (run history is
        # bookkeeping, not evaluation input); results are already final here.
        created_at = datetime.now(timezone.utc)
        created_at_iso = created_at.isoformat()
        fingerprint = canonical_fingerprint(request)
        store = LineageStore(self.artifact_root)
        definition_record = self._record_factor_definition(store, factor_id, created_at_iso)
        artifact_record = store.record_artifact(
            artifact_type=artifact_type,
            path=artifact_path,
            created_at=created_at_iso,
            generated_by=generated_by,
            parents=(definition_record.artifact_id,),
        )
        RunIndex(self.artifact_root).append_run(
            run_id=new_run_id(kind, created_at, fingerprint),
            kind=kind,
            factor_ids=(factor_id,),
            created_at=created_at_iso,
            data_window=data_window,
            config_fingerprint=fingerprint,
            metric_highlights=metric_highlights,
            artifact_paths_rel=(artifact_record.path_rel,) if artifact_record.path_rel else (),
            warnings_count=warnings_count,
        )

    def _record_factor_definition(self, store: LineageStore, factor_id: str, created_at_iso: str) -> LineageRecord:
        path = locate_factor_definition_file(self.factor_root, factor_id)
        payload: dict[str, object] | None = None
        if path is None:
            definition = FactorCatalog(
                self.factor_root,
                factor_values_root=self.factor_values_root,
                factor_values_manifest_root=self.factor_values_manifest_root,
            ).get(factor_id)
            payload = asdict(definition)
        # The definition is sourced from factor_root, not generated by this
        # run; a stable generated_by also deduplicates the root lineage row.
        return store.record_artifact(
            artifact_type="factor_definition",
            path=path,
            payload=payload,
            created_at=created_at_iso,
            generated_by="factor_root",
        )

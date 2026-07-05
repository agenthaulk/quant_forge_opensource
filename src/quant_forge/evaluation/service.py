"""Deterministic evaluation metrics for local factors."""

from __future__ import annotations

from dataclasses import asdict
import math
from pathlib import Path

import numpy as np
import pandas as pd

from quant_forge.core.contracts import (
    EvaluationResult,
    EvaluationSplitMetric,
    HorizonEvaluationMetric,
    METRICS_SCHEMA_VERSION,
    MetricValue,
    SampleSplitSpec,
    SimulationProfile,
)
from quant_forge.data.local import LocalPanelDataProvider
from quant_forge.factor_engine.signal_processing import (
    apply_test_period,
    prepare_factor_scores_result,
    require_minimum_display_trading_days,
    simulation_profile_suffix,
)
from quant_forge.factor_library.catalog import FactorCatalog
from quant_forge.utils import write_json

DEFAULT_HORIZON_DAYS = (5, 10, 21, 63)
DEFAULT_SAMPLE_SPLITS = (
    SampleSplitSpec(name="IS", fraction=0.5, score_weight=0.5),
    SampleSplitSpec(name="OOS1", fraction=0.3, score_weight=0.3),
    SampleSplitSpec(name="OOS2", fraction=0.2, score_weight=0.2),
)
RESEARCH_EVALUATION_ROLE = "research_evaluation"
OVERLAPPING_IC_REQUIRES_HAC = "OVERLAPPING_IC_REQUIRES_HAC"
INCOMPLETE_FORWARD_HORIZON_DROPPED = "INCOMPLETE_FORWARD_HORIZON_DROPPED"
DEGENERATE_IC_SERIES = "DEGENERATE_IC_SERIES"
NUMERICAL_ZERO_TOLERANCE = 1e-12
NUMERICAL_RELATIVE_ZERO_TOLERANCE = 1e-6


def evaluate_factor(
    factor_id: str,
    *,
    factor_root: Path,
    data_root: Path,
    artifact_root: Path,
    horizon_days: int | None = None,
    horizon_days_matrix: tuple[int, ...] | None = None,
    sample_splits: tuple[SampleSplitSpec, ...] | None = None,
    simulation_profile: SimulationProfile | None = None,
    embargo_days: int | None = None,
    factor_values_root: Path | None = None,
    factor_values_overlay_root: Path | None = None,
    factor_values_manifest_root: Path | None = None,
) -> EvaluationResult:
    profile = simulation_profile or SimulationProfile()
    factor = FactorCatalog(
        factor_root,
        factor_values_root=factor_values_root,
        factor_values_manifest_root=factor_values_manifest_root,
    ).get(factor_id)
    horizon = horizon_days or factor.horizon_days
    if horizon < 1:
        raise ValueError("horizon_days must be positive")
    panel = LocalPanelDataProvider(data_root).load_panel()
    working_panel = apply_test_period(panel, profile)
    require_minimum_display_trading_days(working_panel)
    score_result = prepare_factor_scores_result(
        panel,
        factor.formula,
        factor.universe_filters,
        profile=profile,
        factor_id=factor.factor_id,
        factor_name=factor.name,
        factor_values_root=factor_values_root,
        factor_values_overlay_root=factor_values_overlay_root,
    )
    scores = score_result.scores
    split_specs = _validate_sample_splits(sample_splits or DEFAULT_SAMPLE_SPLITS)
    horizons = _unique_horizons(horizon, horizon_days_matrix or DEFAULT_HORIZON_DAYS)
    horizon_metrics = tuple(
        _evaluate_horizon(
            working_panel,
            scores,
            item,
            split_specs,
            execution_delay_days=profile.execution_delay_days,
            embargo_days=embargo_days,
        )
        for item in horizons
    )
    primary = next(metric for metric in horizon_metrics if metric.horizon_days == horizon)
    warnings = _evaluation_warnings(primary.split_metrics)

    artifact_path = artifact_root.expanduser() / "evaluations" / f"{factor_id}{simulation_profile_suffix(profile)}.json"
    write_json(
        artifact_path,
        {
            "schema_version": METRICS_SCHEMA_VERSION,
            "sample_role": RESEARCH_EVALUATION_ROLE,
            "factor_id": factor_id,
            "formula": factor.formula,
            "horizon_days": horizon,
            "simulation_profile": asdict(profile),
            "score_source": score_result.source,
            "score_cached_rows": score_result.cached_rows,
            "score_computed_rows": score_result.computed_rows,
            "score_compute_mode": score_result.compute_mode,
            "score_compute_reason": score_result.compute_reason,
            "score_missing_rows": score_result.missing_rows,
            "score_required_rows": score_result.required_rows,
            "score_missing_ratio": score_result.missing_ratio,
            "score_lookback_rows": score_result.lookback_rows,
            "score_context_rows": score_result.context_rows,
            "factor_values_path": str(score_result.factor_values_path) if score_result.factor_values_path else None,
            "factor_values_write_path": (
                str(score_result.factor_values_write_path) if score_result.factor_values_write_path else None
            ),
            "observations": primary.observations,
            "coverage": primary.coverage,
            "rank_ic_mean": primary.rank_ic_mean,
            "rank_ic_std": primary.rank_ic_std,
            "rank_icir": primary.rank_icir,
            "rank_ic_t_stat": primary.rank_ic_t_stat,
            "rank_ic_t_stat_naive": primary.rank_ic_t_stat_naive,
            "rank_ic_t_stat_hac": primary.rank_ic_t_stat_hac,
            "rank_ic_hac_standard_error": primary.rank_ic_hac_standard_error,
            "rank_ic_hac_lag": primary.rank_ic_hac_lag,
            "rank_ic_p_value_hac": primary.rank_ic_p_value_hac,
            "ic_days": primary.ic_days,
            "ic_series": list(primary.ic_series),
            "coverage_lineage": primary.coverage_lineage,
            "boundary_diagnostics": primary.boundary_diagnostics,
            "metric_provenance": primary.metric_provenance,
            "warning_codes": list(primary.warning_codes),
            "metrics": {key: asdict(value) for key, value in primary.metrics.items()},
            "sample_splits": [asdict(split) for split in split_specs],
            "split_metrics": [asdict(metric) for metric in primary.split_metrics],
            "horizon_matrix": [asdict(metric) for metric in horizon_metrics],
            "warnings": list(warnings),
        },
    )
    return EvaluationResult(
        factor_id=factor_id,
        observations=primary.observations,
        coverage=primary.coverage,
        rank_ic_mean=primary.rank_ic_mean,
        rank_ic_std=primary.rank_ic_std,
        rank_icir=primary.rank_icir,
        ic_days=primary.ic_days,
        artifact_path=artifact_path,
        rank_ic_t_stat=primary.rank_ic_t_stat,
        split_metrics=primary.split_metrics,
        horizon_metrics=horizon_metrics,
        simulation_profile=profile,
        score_source=score_result.source,
        score_cached_rows=score_result.cached_rows,
        score_computed_rows=score_result.computed_rows,
        factor_values_path=score_result.factor_values_path,
        factor_values_write_path=score_result.factor_values_write_path,
        score_compute_mode=score_result.compute_mode,
        score_compute_reason=score_result.compute_reason,
        score_missing_rows=score_result.missing_rows,
        score_required_rows=score_result.required_rows,
        score_missing_ratio=score_result.missing_ratio,
        score_lookback_rows=score_result.lookback_rows,
        score_context_rows=score_result.context_rows,
        warnings=warnings,
        schema_version=METRICS_SCHEMA_VERSION,
        sample_role=RESEARCH_EVALUATION_ROLE,
        rank_ic_t_stat_naive=primary.rank_ic_t_stat_naive,
        rank_ic_t_stat_hac=primary.rank_ic_t_stat_hac,
        rank_ic_hac_standard_error=primary.rank_ic_hac_standard_error,
        rank_ic_hac_lag=primary.rank_ic_hac_lag,
        rank_ic_p_value_hac=primary.rank_ic_p_value_hac,
        ic_series=primary.ic_series,
        coverage_lineage=primary.coverage_lineage,
        boundary_diagnostics=primary.boundary_diagnostics,
        metric_provenance=primary.metric_provenance,
        warning_codes=primary.warning_codes,
        metrics=primary.metrics,
    )


def _with_forward_return(panel: pd.DataFrame, horizon_days: int, *, execution_delay_days: int) -> pd.DataFrame:
    if execution_delay_days < 1:
        raise ValueError("execution_delay_days must be at least 1")
    labeled = panel[["trade_date", "instrument", "close"]].copy()
    by_instrument = labeled.groupby("instrument")["close"]
    labeled["entry_close"] = by_instrument.shift(-execution_delay_days)
    labeled["future_close"] = by_instrument.shift(-(execution_delay_days + horizon_days))
    labeled["forward_return"] = labeled["future_close"] / labeled["entry_close"] - 1.0
    return labeled[["trade_date", "instrument", "forward_return"]]


def _evaluate_horizon(
    panel: pd.DataFrame,
    scores: pd.DataFrame,
    horizon_days: int,
    split_specs: tuple[SampleSplitSpec, ...],
    *,
    execution_delay_days: int,
    embargo_days: int | None = None,
) -> HorizonEvaluationMetric:
    if horizon_days < 1:
        raise ValueError("horizon_days_matrix values must be positive")
    labeled = _with_forward_return(
        panel,
        horizon_days,
        execution_delay_days=execution_delay_days,
    ).merge(scores, on=["trade_date", "instrument"], how="left")
    overall = _ic_summary(labeled, horizon_days=horizon_days, execution_delay_days=execution_delay_days)
    dates = list(labeled.dropna(subset=["forward_return"])["trade_date"].drop_duplicates().sort_values())
    # Purge/embargo: drop the tail of each non-final split whose forward-return
    # labels (execution_delay + horizon trading days ahead) would use data from
    # the following split's window. None => auto gap; 0 => disabled (legacy).
    embargo = embargo_days if embargo_days is not None else execution_delay_days + horizon_days
    split_dates = _split_dates(dates, split_specs, embargo=embargo)
    split_metrics = tuple(
        _split_metric(
            spec,
            dates_for_split,
            labeled[labeled["trade_date"].isin(dates_for_split)],
            horizon_days=horizon_days,
            execution_delay_days=execution_delay_days,
        )
        for spec, dates_for_split in zip(split_specs, split_dates, strict=True)
    )
    return HorizonEvaluationMetric(
        horizon_days=horizon_days,
        observations=overall["observations"],
        coverage=overall["coverage"],
        rank_ic_mean=overall["rank_ic_mean"],
        rank_ic_std=overall["rank_ic_std"],
        rank_icir=overall["rank_icir"],
        ic_days=overall["ic_days"],
        rank_ic_t_stat=overall["rank_ic_t_stat"],
        split_metrics=split_metrics,
        sample_role=RESEARCH_EVALUATION_ROLE,
        rank_ic_t_stat_naive=overall["rank_ic_t_stat_naive"],
        rank_ic_t_stat_hac=overall["rank_ic_t_stat_hac"],
        rank_ic_hac_standard_error=overall["rank_ic_hac_standard_error"],
        rank_ic_hac_lag=overall["rank_ic_hac_lag"],
        rank_ic_p_value_hac=overall["rank_ic_p_value_hac"],
        ic_series=overall["ic_series"],
        coverage_lineage=overall["coverage_lineage"],
        boundary_diagnostics=overall["boundary_diagnostics"],
        metric_provenance=overall["metric_provenance"],
        warning_codes=overall["warning_codes"],
        metrics=overall["metrics"],
    )


def _split_metric(
    spec: SampleSplitSpec,
    dates: tuple[pd.Timestamp, ...],
    labeled: pd.DataFrame,
    *,
    horizon_days: int,
    execution_delay_days: int,
) -> EvaluationSplitMetric:
    summary = _ic_summary(labeled, horizon_days=horizon_days, execution_delay_days=execution_delay_days)
    return EvaluationSplitMetric(
        name=spec.name,
        start_date=_date_label(dates[0]) if dates else "",
        end_date=_date_label(dates[-1]) if dates else "",
        date_count=len(dates),
        observations=summary["observations"],
        coverage=summary["coverage"],
        rank_ic_mean=summary["rank_ic_mean"],
        rank_ic_std=summary["rank_ic_std"],
        rank_icir=summary["rank_icir"],
        ic_days=summary["ic_days"],
        rank_ic_t_stat=summary["rank_ic_t_stat"],
        score_weight=spec.score_weight,
        sample_role=RESEARCH_EVALUATION_ROLE,
        rank_ic_t_stat_naive=summary["rank_ic_t_stat_naive"],
        rank_ic_t_stat_hac=summary["rank_ic_t_stat_hac"],
        rank_ic_hac_standard_error=summary["rank_ic_hac_standard_error"],
        rank_ic_hac_lag=summary["rank_ic_hac_lag"],
        rank_ic_p_value_hac=summary["rank_ic_p_value_hac"],
        warning_codes=summary["warning_codes"],
        metrics=summary["metrics"],
    )


def _evaluation_warnings(split_metrics: tuple[EvaluationSplitMetric, ...]) -> tuple[str, ...]:
    by_name = {metric.name.upper(): metric for metric in split_metrics}
    is_metric = by_name.get("IS")
    oos_metrics = [metric for name, metric in by_name.items() if name.startswith("OOS")]
    if is_metric is None or not oos_metrics:
        return ()
    warnings: list[str] = []
    is_abs_icir = abs(is_metric.rank_icir)
    if is_abs_icir >= 1.0:
        weak = [
            metric.name
            for metric in oos_metrics
            if abs(metric.rank_icir) < is_abs_icir * 0.5
            or metric.rank_ic_mean * is_metric.rank_ic_mean < 0
        ]
        if weak:
            warnings.append(
                "OOS decay warning: IS ICIR is materially stronger than "
                f"{', '.join(weak)}; do not rely on full-sample metrics only."
            )
    return tuple(warnings)


def _ic_summary(labeled: pd.DataFrame, *, horizon_days: int, execution_delay_days: int) -> dict[str, object]:
    usable = labeled.dropna(subset=["score", "forward_return"])
    ic_series: list[dict[str, object]] = []
    ic_by_date: list[float] = []
    for date_value, group in usable.groupby("trade_date"):
        if group["score"].nunique() < 2 or group["forward_return"].nunique() < 2:
            continue
        ic = group["score"].rank().corr(group["forward_return"].rank())
        if pd.notna(ic):
            value = float(ic)
            ic_by_date.append(value)
            ic_series.append(
                {
                    "date": _date_label(pd.Timestamp(date_value)),
                    "ic": value,
                    "cross_section_count": int(len(group)),
                }
            )

    rank_ic_mean = float(np.mean(ic_by_date)) if ic_by_date else 0.0
    rank_ic_std = float(np.std(ic_by_date, ddof=1)) if len(ic_by_date) > 1 else 0.0
    degenerate_std_floor = max(
        NUMERICAL_ZERO_TOLERANCE,
        abs(rank_ic_mean) * NUMERICAL_RELATIVE_ZERO_TOLERANCE,
    )
    degenerate_ic_series = len(ic_by_date) > 1 and abs(rank_ic_std) < degenerate_std_floor
    if degenerate_ic_series:
        rank_ic_std = 0.0
    rank_icir = float(rank_ic_mean / rank_ic_std) if rank_ic_std else 0.0
    rank_ic_t_stat_naive = float(rank_icir * np.sqrt(len(ic_by_date))) if rank_ic_std else None
    hac_lag = max(0, horizon_days - 1)
    hac_se = _hac_mean_standard_error(ic_by_date, lag=hac_lag) if len(ic_by_date) > 1 else None
    rank_ic_t_stat_hac = float(rank_ic_mean / hac_se) if hac_se and hac_se > 0 else None
    rank_ic_p_value_hac = _normal_two_sided_p_value(rank_ic_t_stat_hac)
    rank_ic_t_stat = rank_ic_t_stat_hac
    possible = len(labeled.dropna(subset=["forward_return"]))
    coverage = float(len(usable) / possible) if possible else 0.0
    raw_universe_rows = int(len(labeled))
    factor_non_null_rows = int(labeled["score"].notna().sum()) if "score" in labeled else 0
    label_non_null_rows = int(labeled["forward_return"].notna().sum()) if "forward_return" in labeled else 0
    coverage_lineage = {
        "raw_universe_rows": raw_universe_rows,
        "eligible_universe_rows": raw_universe_rows,
        "factor_non_null_rows": factor_non_null_rows,
        "label_non_null_rows": label_non_null_rows,
        "joint_valid_rows": int(len(usable)),
        "factor_coverage": float(factor_non_null_rows / raw_universe_rows) if raw_universe_rows else 0.0,
        "label_coverage": float(label_non_null_rows / raw_universe_rows) if raw_universe_rows else 0.0,
        "joint_coverage": coverage,
    }
    dropped_boundary = raw_universe_rows - label_non_null_rows
    warning_codes: list[str] = []
    if horizon_days > 1 and len(ic_by_date) > 1:
        warning_codes.append(OVERLAPPING_IC_REQUIRES_HAC)
    if dropped_boundary:
        warning_codes.append(INCOMPLETE_FORWARD_HORIZON_DROPPED)
    if degenerate_ic_series:
        warning_codes.append(DEGENERATE_IC_SERIES)
    t_status = "available" if rank_ic_t_stat is not None else "insufficient_sample"
    metric = MetricValue(
        value=rank_ic_t_stat,
        unit="t_stat",
        status=t_status,
        observation_count=len(ic_by_date),
        minimum_required=2,
        method="newey_west_bartlett",
        source_series="daily_rank_ic",
        sample_role=RESEARCH_EVALUATION_ROLE,
        horizon_days=horizon_days,
        execution_delay_days=execution_delay_days,
        warning_codes=tuple(warning_codes),
    )
    icir_available = len(ic_by_date) > 1 and rank_ic_std > 0
    icir_status = "available" if icir_available else "insufficient_sample"
    rank_icir_metric = MetricValue(
        value=rank_icir if icir_available else None,
        unit="ratio",
        status=icir_status,
        observation_count=len(ic_by_date),
        minimum_required=2,
        method="rank_ic_mean_over_std",
        source_series="daily_rank_ic",
        sample_role=RESEARCH_EVALUATION_ROLE,
        horizon_days=horizon_days,
        execution_delay_days=execution_delay_days,
        warning_codes=tuple(warning_codes),
    )
    ic_mean_available = bool(ic_by_date)
    ic_mean_status = "available" if ic_mean_available else "insufficient_sample"
    rank_ic_mean_metric = MetricValue(
        value=rank_ic_mean if ic_mean_available else None,
        unit="correlation",
        status=ic_mean_status,
        observation_count=len(ic_by_date),
        minimum_required=1,
        method="daily_rank_ic_mean",
        source_series="daily_rank_ic",
        sample_role=RESEARCH_EVALUATION_ROLE,
        horizon_days=horizon_days,
        execution_delay_days=execution_delay_days,
        warning_codes=tuple(warning_codes),
    )
    metrics = {
        "rank_ic_t_stat": metric,
        "rank_icir": rank_icir_metric,
        "rank_ic_mean": rank_ic_mean_metric,
    }
    metric_provenance = {
        "rank_ic_t_stat": {
            "method": "newey_west_bartlett",
            "source_series": "daily_rank_ic",
            "sample_role": RESEARCH_EVALUATION_ROLE,
            "horizon_days": horizon_days,
            "execution_delay_days": execution_delay_days,
            "observation_count": len(ic_by_date),
        }
    }
    return {
        "observations": int(len(usable)),
        "coverage": coverage,
        "rank_ic_mean": rank_ic_mean,
        "rank_ic_std": rank_ic_std,
        "rank_icir": rank_icir,
        "rank_ic_t_stat": rank_ic_t_stat,
        "rank_ic_t_stat_naive": rank_ic_t_stat_naive,
        "rank_ic_t_stat_hac": rank_ic_t_stat_hac,
        "rank_ic_hac_standard_error": hac_se,
        "rank_ic_hac_lag": hac_lag,
        "rank_ic_p_value_hac": rank_ic_p_value_hac,
        "ic_days": len(ic_by_date),
        "ic_series": tuple(ic_series),
        "coverage_lineage": coverage_lineage,
        "boundary_diagnostics": {
            "dropped_boundary_signal_rows": dropped_boundary,
            "horizon_days": horizon_days,
            "execution_delay_days": execution_delay_days,
        },
        "metric_provenance": metric_provenance,
        "warning_codes": tuple(warning_codes),
        "metrics": metrics,
    }


def _hac_mean_standard_error(values: list[float] | tuple[float, ...], *, lag: int) -> float | None:
    sample = np.asarray(values, dtype=float)
    n = len(sample)
    if n < 2:
        return None
    centered = sample - float(np.mean(sample))
    max_lag = min(max(lag, 0), n - 1)
    long_run_variance = float(np.sum(centered * centered) / n)
    for item in range(1, max_lag + 1):
        covariance = float(np.sum(centered[item:] * centered[:-item]) / n)
        weight = 1.0 - item / (max_lag + 1)
        long_run_variance += 2.0 * weight * covariance
    if long_run_variance <= NUMERICAL_ZERO_TOLERANCE:
        return None
    standard_error = float(np.sqrt(long_run_variance / n))
    if standard_error <= NUMERICAL_ZERO_TOLERANCE:
        return None
    return standard_error


def _normal_two_sided_p_value(t_stat: float | None) -> float | None:
    if t_stat is None:
        return None
    return float(math.erfc(abs(t_stat) / math.sqrt(2.0)))


def _split_dates(
    dates: list[pd.Timestamp],
    split_specs: tuple[SampleSplitSpec, ...],
    *,
    embargo: int = 0,
) -> tuple[tuple[pd.Timestamp, ...], ...]:
    if not dates:
        return tuple(tuple() for _ in split_specs)
    total_fraction = sum(split.fraction for split in split_specs)
    chunks: list[tuple[pd.Timestamp, ...]] = []
    start = 0
    cumulative_fraction = 0.0
    for index, split in enumerate(split_specs):
        if index == len(split_specs) - 1:
            end = len(dates)
        else:
            cumulative_fraction += split.fraction
            end = int(round(cumulative_fraction / total_fraction * len(dates)))
            end = max(start, min(end, len(dates)))
        chunks.append(tuple(dates[start:end]))
        start = end
    if embargo > 0:
        # Drop the trailing `embargo` dates of every split except the last so a
        # split's labels never reach into the next split's window. A split
        # shorter than the gap is fully purged (date_count == 0).
        chunks = [
            chunk[:-embargo] if index < len(chunks) - 1 else chunk
            for index, chunk in enumerate(chunks)
        ]
    return tuple(chunks)


def _validate_sample_splits(split_specs: tuple[SampleSplitSpec, ...]) -> tuple[SampleSplitSpec, ...]:
    if not split_specs:
        raise ValueError("at least one sample split is required")
    names = [split.name for split in split_specs]
    if len(set(names)) != len(names):
        raise ValueError("sample split names must be unique")
    if sum(split.fraction for split in split_specs) <= 0:
        raise ValueError("sample split fractions must sum to a positive value")
    if sum(split.score_weight for split in split_specs) <= 0:
        raise ValueError("sample split score weights must sum to a positive value")
    return split_specs


def _unique_horizons(primary: int, requested: tuple[int, ...]) -> tuple[int, ...]:
    horizons: list[int] = []
    for horizon in (primary, *requested):
        if horizon not in horizons:
            horizons.append(horizon)
    return tuple(horizons)


def _date_label(value: pd.Timestamp) -> str:
    return pd.Timestamp(value).date().isoformat()

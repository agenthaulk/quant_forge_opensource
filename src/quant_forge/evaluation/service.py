"""Deterministic evaluation metrics for local factors."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd

from quant_forge.core.contracts import (
    EvaluationResult,
    EvaluationSplitMetric,
    HorizonEvaluationMetric,
    SampleSplitSpec,
    SimulationProfile,
)
from quant_forge.data.local import LocalPanelDataProvider
from quant_forge.factor_engine.signal_processing import (
    apply_test_period,
    prepare_factor_scores,
    simulation_profile_suffix,
)
from quant_forge.factor_library.repository import FactorRepository
from quant_forge.utils import write_json

DEFAULT_HORIZON_DAYS = (5, 10, 21, 63)
DEFAULT_SAMPLE_SPLITS = (
    SampleSplitSpec(name="IS", fraction=0.5, score_weight=0.5),
    SampleSplitSpec(name="OOS1", fraction=0.3, score_weight=0.3),
    SampleSplitSpec(name="OOS2", fraction=0.2, score_weight=0.2),
)


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
) -> EvaluationResult:
    profile = simulation_profile or SimulationProfile()
    factor = FactorRepository(factor_root).get(factor_id)
    horizon = horizon_days or factor.horizon_days
    if horizon < 1:
        raise ValueError("horizon_days must be positive")
    panel = LocalPanelDataProvider(data_root).load_panel()
    working_panel = apply_test_period(panel, profile)
    scores = prepare_factor_scores(working_panel, factor.formula, factor.universe_filters, profile=profile)
    split_specs = _validate_sample_splits(sample_splits or DEFAULT_SAMPLE_SPLITS)
    horizons = _unique_horizons(horizon, horizon_days_matrix or DEFAULT_HORIZON_DAYS)
    horizon_metrics = tuple(_evaluate_horizon(working_panel, scores, item, split_specs) for item in horizons)
    primary = next(metric for metric in horizon_metrics if metric.horizon_days == horizon)

    artifact_path = artifact_root.expanduser() / "evaluations" / f"{factor_id}{simulation_profile_suffix(profile)}.json"
    write_json(
        artifact_path,
        {
            "factor_id": factor_id,
            "formula": factor.formula,
            "horizon_days": horizon,
            "simulation_profile": asdict(profile),
            "observations": primary.observations,
            "coverage": primary.coverage,
            "rank_ic_mean": primary.rank_ic_mean,
            "rank_ic_std": primary.rank_ic_std,
            "rank_icir": primary.rank_icir,
            "ic_days": primary.ic_days,
            "sample_splits": [asdict(split) for split in split_specs],
            "split_metrics": [asdict(metric) for metric in primary.split_metrics],
            "horizon_matrix": [asdict(metric) for metric in horizon_metrics],
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
        split_metrics=primary.split_metrics,
        horizon_metrics=horizon_metrics,
        simulation_profile=profile,
    )


def _with_forward_return(panel: pd.DataFrame, horizon_days: int) -> pd.DataFrame:
    labeled = panel[["trade_date", "instrument", "close"]].copy()
    labeled["future_close"] = labeled.groupby("instrument")["close"].shift(-horizon_days)
    labeled["forward_return"] = labeled["future_close"] / labeled["close"] - 1.0
    return labeled[["trade_date", "instrument", "forward_return"]]


def _evaluate_horizon(
    panel: pd.DataFrame,
    scores: pd.DataFrame,
    horizon_days: int,
    split_specs: tuple[SampleSplitSpec, ...],
) -> HorizonEvaluationMetric:
    if horizon_days < 1:
        raise ValueError("horizon_days_matrix values must be positive")
    labeled = _with_forward_return(panel, horizon_days).merge(scores, on=["trade_date", "instrument"], how="left")
    overall = _ic_summary(labeled)
    dates = list(labeled.dropna(subset=["forward_return"])["trade_date"].drop_duplicates().sort_values())
    split_dates = _split_dates(dates, split_specs)
    split_metrics = tuple(
        _split_metric(spec, dates_for_split, labeled[labeled["trade_date"].isin(dates_for_split)])
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
        split_metrics=split_metrics,
    )


def _split_metric(
    spec: SampleSplitSpec, dates: tuple[pd.Timestamp, ...], labeled: pd.DataFrame
) -> EvaluationSplitMetric:
    summary = _ic_summary(labeled)
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
        score_weight=spec.score_weight,
    )


def _ic_summary(labeled: pd.DataFrame) -> dict[str, float | int]:
    usable = labeled.dropna(subset=["score", "forward_return"])
    ic_by_date: list[float] = []
    for _, group in usable.groupby("trade_date"):
        if group["score"].nunique() < 2 or group["forward_return"].nunique() < 2:
            continue
        ic = group["score"].rank().corr(group["forward_return"].rank())
        if pd.notna(ic):
            ic_by_date.append(float(ic))

    rank_ic_mean = float(np.mean(ic_by_date)) if ic_by_date else 0.0
    rank_ic_std = float(np.std(ic_by_date, ddof=1)) if len(ic_by_date) > 1 else 0.0
    if abs(rank_ic_std) < 1e-12:
        rank_ic_std = 0.0
    rank_icir = float(rank_ic_mean / rank_ic_std * np.sqrt(len(ic_by_date))) if rank_ic_std else 0.0
    possible = len(labeled.dropna(subset=["forward_return"]))
    coverage = float(len(usable) / possible) if possible else 0.0
    return {
        "observations": int(len(usable)),
        "coverage": coverage,
        "rank_ic_mean": rank_ic_mean,
        "rank_ic_std": rank_ic_std,
        "rank_icir": rank_icir,
        "ic_days": len(ic_by_date),
    }


def _split_dates(
    dates: list[pd.Timestamp], split_specs: tuple[SampleSplitSpec, ...]
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

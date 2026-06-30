"""Shared score preparation for evaluation, backtesting, and RD."""

from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from pathlib import Path

import pandas as pd

from quant_forge.core.contracts import SimulationProfile
from quant_forge.factor_engine.executor import execute_factor_formula
from quant_forge.factor_engine.value_store import FactorScoreResult, FactorValueStore
from quant_forge.factor_library.catalog import is_precomputed_formula


MIN_DISPLAY_TRADING_DAYS = 126


def prepare_factor_scores(
    panel: pd.DataFrame,
    formula: str,
    universe_filters: tuple[str, ...] = (),
    *,
    profile: SimulationProfile | None = None,
    factor_id: str | None = None,
    factor_name: str | None = None,
    factor_values_root: Path | None = None,
    factor_values_overlay_root: Path | None = None,
) -> pd.DataFrame:
    return prepare_factor_scores_result(
        panel,
        formula,
        universe_filters,
        profile=profile,
        factor_id=factor_id,
        factor_name=factor_name,
        factor_values_root=factor_values_root,
        factor_values_overlay_root=factor_values_overlay_root,
    ).scores


def prepare_factor_scores_result(
    panel: pd.DataFrame,
    formula: str,
    universe_filters: tuple[str, ...] = (),
    *,
    profile: SimulationProfile | None = None,
    factor_id: str | None = None,
    factor_name: str | None = None,
    factor_values_root: Path | None = None,
    factor_values_overlay_root: Path | None = None,
) -> FactorScoreResult:
    simulation_profile = profile or SimulationProfile()
    _validate_profile(simulation_profile)
    working_panel = apply_test_period(panel, simulation_profile)
    cache_only = is_precomputed_formula(formula)
    read_root = factor_values_root or factor_values_overlay_root
    factor_values_write_path = None
    compute_mode = "computed_formula"
    compute_reason = "no factor value store configured; computed formula directly"
    missing_rows = 0
    required_rows = 0
    missing_ratio = 0.0
    lookback_rows = 0
    context_rows = 0
    if read_root is not None and factor_id is not None:
        score_result = FactorValueStore(read_root, write_root=factor_values_overlay_root).prepare_scores(
            working_panel,
            factor_id=factor_id,
            factor_name=factor_name or factor_id,
            formula=formula,
            universe_filters=universe_filters,
            cache_only=cache_only,
        )
        scores = score_result.scores
        source = score_result.source
        cached_rows = score_result.cached_rows
        computed_rows = score_result.computed_rows
        factor_values_path = score_result.factor_values_path
        factor_values_write_path = score_result.factor_values_write_path
        compute_mode = score_result.compute_mode
        compute_reason = score_result.compute_reason
        missing_rows = score_result.missing_rows
        required_rows = score_result.required_rows
        missing_ratio = score_result.missing_ratio
        lookback_rows = score_result.lookback_rows
        context_rows = score_result.context_rows
    elif cache_only:
        raise ValueError("precomputed factors require factor_values_root or factor_values_overlay_root")
    else:
        from quant_forge.operator_registry.resolver import resolve_executable_formula

        scores = execute_factor_formula(working_panel, resolve_executable_formula(formula), universe_filters)
        source = "computed_formula"
        cached_rows = 0
        computed_rows = int(len(scores))
        factor_values_path = None
        missing_rows = computed_rows
        required_rows = computed_rows
        missing_ratio = 1.0 if computed_rows else 0.0
        context_rows = computed_rows
    if simulation_profile.decay_days > 1:
        scores = _apply_ewma_decay(scores, simulation_profile.decay_days)
    prepared = (
        scores[["trade_date", "instrument", "score"]]
        .sort_values(["trade_date", "instrument"])
        .reset_index(drop=True)
    )
    return FactorScoreResult(
        scores=prepared,
        source=source,
        cached_rows=cached_rows,
        computed_rows=computed_rows,
        factor_values_path=factor_values_path,
        factor_values_write_path=factor_values_write_path,
        compute_mode=compute_mode,
        compute_reason=compute_reason,
        missing_rows=missing_rows,
        required_rows=required_rows,
        missing_ratio=missing_ratio,
        lookback_rows=lookback_rows,
        context_rows=context_rows,
    )


def apply_test_period(panel: pd.DataFrame, profile: SimulationProfile) -> pd.DataFrame:
    result = panel.copy()
    if profile.test_period_start:
        result = result[result["trade_date"] >= pd.Timestamp(profile.test_period_start)]
    if profile.test_period_end:
        result = result[result["trade_date"] <= pd.Timestamp(profile.test_period_end)]
    return result


def require_minimum_display_trading_days(
    panel: pd.DataFrame,
    *,
    min_trading_days: int = MIN_DISPLAY_TRADING_DAYS,
) -> None:
    dates = pd.to_datetime(panel.get("trade_date", pd.Series(dtype="datetime64[ns]")), errors="coerce")
    unique_dates = dates.dropna().drop_duplicates().sort_values()
    date_count = int(len(unique_dates))
    if date_count >= min_trading_days:
        return
    start = unique_dates.iloc[0].date().isoformat() if date_count else "none"
    end = unique_dates.iloc[-1].date().isoformat() if date_count else "none"
    raise ValueError(
        "factor display requires at least "
        f"{min_trading_days} daily trading dates, approximately six months, after test_period; "
        f"found {date_count} from {start} to {end}. "
        "Expand simulation.test_period or load a longer daily panel."
    )


def _validate_profile(profile: SimulationProfile) -> None:
    if profile.nan_policy != "drop":
        raise ValueError("only nan_policy='drop' is supported")
    if profile.neutralization != "none":
        raise ValueError("only neutralization='none' is supported")
    if profile.truncation is not None:
        raise ValueError("truncation is not supported in the first public profile")


def _apply_ewma_decay(scores: pd.DataFrame, decay_days: int) -> pd.DataFrame:
    result = scores.sort_values(["instrument", "trade_date"]).copy()
    raw_missing = result["score"].isna()
    result["score"] = result.groupby("instrument", group_keys=False)["score"].transform(
        lambda values: values.astype("float64").ewm(span=decay_days, adjust=False).mean()
    )
    result.loc[raw_missing, "score"] = pd.NA
    return result


def simulation_profile_suffix(profile: SimulationProfile) -> str:
    if profile == SimulationProfile():
        return ""
    payload = json.dumps(asdict(profile), ensure_ascii=True, sort_keys=True)
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:8]
    return f"_{digest}"

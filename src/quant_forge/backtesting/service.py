"""Next-day execution factor backtest."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd

from quant_forge.core.contracts import BacktestGroupMetric, BacktestResult, SimulationProfile
from quant_forge.data.local import LocalPanelDataProvider
from quant_forge.factor_engine.signal_processing import (
    apply_test_period,
    prepare_factor_scores,
    simulation_profile_suffix,
)
from quant_forge.factor_library.repository import FactorRepository
from quant_forge.utils import write_json


def run_factor_backtest(
    factor_id: str,
    *,
    factor_root: Path,
    data_root: Path,
    artifact_root: Path,
    top_quantile: float | None = None,
    holding_days: int | None = None,
    group_count: int = 5,
    simulation_profile: SimulationProfile | None = None,
) -> BacktestResult:
    profile = simulation_profile or SimulationProfile()
    if top_quantile is not None:
        profile = SimulationProfile(
            market=profile.market,
            instrument_type=profile.instrument_type,
            universe=profile.universe,
            execution_delay_days=profile.execution_delay_days,
            top_quantile=top_quantile,
            nan_policy=profile.nan_policy,
            neutralization=profile.neutralization,
            truncation=profile.truncation,
            decay_days=profile.decay_days,
            test_period_start=profile.test_period_start,
            test_period_end=profile.test_period_end,
        )
    top_quantile = profile.top_quantile
    if group_count < 2:
        raise ValueError("group_count must be at least 2")
    factor = FactorRepository(factor_root).get(factor_id)
    holding = holding_days or factor.horizon_days
    if holding < 1:
        raise ValueError("holding_days must be positive")
    panel = LocalPanelDataProvider(data_root).load_panel()
    working_panel = apply_test_period(panel, profile)
    scores = prepare_factor_scores(working_panel, factor.formula, factor.universe_filters, profile=profile)
    close = working_panel[["trade_date", "instrument", "close"]].copy()
    dates = sorted(working_panel["trade_date"].drop_duplicates())
    rows: list[dict[str, object]] = []
    grouped_returns: dict[str, list[float]] = {}
    previous_long: set[str] | None = None
    previous_short: set[str] | None = None
    turnovers: list[float] = []

    delay = profile.execution_delay_days
    for signal_index in range(0, len(dates) - holding - delay, holding):
        signal_date = dates[signal_index]
        entry_date = dates[signal_index + delay]
        exit_date = dates[signal_index + delay + holding]
        signal = scores[scores["trade_date"] == signal_date].dropna(subset=["score"])
        if signal.empty:
            continue
        merged = signal.merge(
            _with_period_return(close, entry_date, exit_date),
            on="instrument",
            how="inner",
        ).dropna(subset=["period_return"])
        if len(merged) < max(4, group_count):
            continue
        count = max(1, int(len(merged) * top_quantile))
        ordered = merged.sort_values("score").reset_index(drop=True)
        short_leg = ordered.head(count)
        long_leg = ordered.tail(count)
        short_return = float(short_leg["period_return"].mean())
        long_return = float(long_leg["period_return"].mean())
        period_return = long_return - short_return
        period_group_returns = _group_returns(ordered, group_count)
        for group_name, group_return in period_group_returns.items():
            grouped_returns.setdefault(group_name, []).append(group_return)
        long_names = set(long_leg["instrument"].astype(str))
        short_names = set(short_leg["instrument"].astype(str))
        turnover: float | None = None
        if previous_long is not None and previous_short is not None:
            long_turnover = _leg_turnover(previous_long, long_names)
            short_turnover = _leg_turnover(previous_short, short_names)
            turnover = (long_turnover + short_turnover) / 2.0
            turnovers.append(turnover)
        previous_long = long_names
        previous_short = short_names
        rows.append(
            {
                "signal_date": signal_date.date().isoformat(),
                "entry_date": entry_date.date().isoformat(),
                "exit_date": exit_date.date().isoformat(),
                "long_return": long_return,
                "short_return": short_return,
                "period_return": period_return,
                "group_returns": period_group_returns,
                "turnover": turnover,
            }
        )

    returns = np.array([float(row["period_return"]) for row in rows], dtype=float)
    equity = np.cumprod(1.0 + returns) if len(returns) else np.array([], dtype=float)
    cumulative_return = float(equity[-1] - 1.0) if len(equity) else 0.0
    annualized_return = (
        float((1.0 + cumulative_return) ** (252 / (holding * len(returns))) - 1.0) if len(returns) else 0.0
    )
    annualized_volatility = float(np.std(returns, ddof=1) * np.sqrt(252 / holding)) if len(returns) > 1 else 0.0
    long_short_sharpe = _long_short_sharpe(returns, holding)
    max_drawdown = _max_drawdown(equity)
    average_turnover = float(np.mean(turnovers)) if turnovers else 0.0
    group_metrics = tuple(
        BacktestGroupMetric(
            group=group_name,
            mean_return=float(np.mean(values)) if values else 0.0,
            periods=len(values),
        )
        for group_name, values in grouped_returns.items()
        if values
    )

    artifact_path = artifact_root.expanduser() / "backtests" / f"{factor_id}{simulation_profile_suffix(profile)}.json"
    execution = (
        "signal_date plus one trading day; non-overlapping holding periods"
        if delay == 1
        else f"signal_date plus {delay} trading days; non-overlapping holding periods"
    )
    write_json(
        artifact_path,
        {
            "factor_id": factor_id,
            "formula": factor.formula,
            "execution": execution,
            "holding_days": holding,
            "top_quantile": top_quantile,
            "simulation_profile": asdict(profile),
            "periods": len(rows),
            "cumulative_return": cumulative_return,
            "annualized_return": annualized_return,
            "annualized_volatility": annualized_volatility,
            "long_short_sharpe": long_short_sharpe,
            "max_drawdown": max_drawdown,
            "average_turnover": average_turnover,
            "group_returns": [asdict(metric) for metric in group_metrics],
            "period_returns": rows,
        },
    )
    return BacktestResult(
        factor_id=factor_id,
        periods=len(rows),
        holding_days=holding,
        cumulative_return=cumulative_return,
        annualized_return=annualized_return,
        annualized_volatility=annualized_volatility,
        max_drawdown=max_drawdown,
        artifact_path=artifact_path,
        long_short_sharpe=long_short_sharpe,
        average_turnover=average_turnover,
        top_quantile=top_quantile,
        simulation_profile=profile,
        group_returns=group_metrics,
    )


def _with_period_return(close: pd.DataFrame, entry_date: pd.Timestamp, exit_date: pd.Timestamp) -> pd.DataFrame:
    entry = close[close["trade_date"] == entry_date][["instrument", "close"]].rename(columns={"close": "entry_close"})
    exit_ = close[close["trade_date"] == exit_date][["instrument", "close"]].rename(columns={"close": "exit_close"})
    result = entry.merge(exit_, on="instrument", how="inner")
    result["period_return"] = result["exit_close"] / result["entry_close"] - 1.0
    return result[["instrument", "period_return"]]


def _max_drawdown(equity: np.ndarray) -> float:
    if len(equity) == 0:
        return 0.0
    equity_with_start = np.concatenate(([1.0], equity))
    peak = np.maximum.accumulate(equity_with_start)
    drawdowns = equity_with_start / peak - 1.0
    return float(np.min(drawdowns))


def _group_returns(ordered: pd.DataFrame, group_count: int) -> dict[str, float]:
    if len(ordered) < group_count:
        raise ValueError("group_count cannot exceed the available cross-section size")
    result: dict[str, float] = {}
    for index, positions in enumerate(np.array_split(np.arange(len(ordered)), group_count), start=1):
        label = f"Q{index}"
        if len(positions) == 0:
            raise ValueError("group split produced an empty group")
        else:
            result[label] = float(ordered.iloc[positions]["period_return"].mean())
    return result


def _leg_turnover(previous: set[str], current: set[str]) -> float:
    denominator = max(len(previous), len(current), 1)
    overlap = len(previous.intersection(current))
    return float(1.0 - overlap / denominator)


def _long_short_sharpe(returns: np.ndarray, holding_days: int) -> float:
    if len(returns) < 2:
        return 0.0
    std = float(np.std(returns, ddof=1))
    if std == 0.0:
        return 0.0
    return float(np.mean(returns) / std * np.sqrt(252 / holding_days))

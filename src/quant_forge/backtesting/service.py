"""Next-day execution factor backtest."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd

from quant_forge.core.contracts import (
    BacktestGroupMetric,
    BacktestResult,
    BacktestSegmentMetric,
    SampleSplitSpec,
    SimulationProfile,
    TransactionCostModel,
)
from quant_forge.data.local import LocalPanelDataProvider
from quant_forge.evaluation.service import DEFAULT_SAMPLE_SPLITS
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
    transaction_costs: TransactionCostModel | None = None,
    sample_splits: tuple[SampleSplitSpec, ...] | None = None,
) -> BacktestResult:
    profile = simulation_profile or SimulationProfile()
    costs = transaction_costs or TransactionCostModel()
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
    previous_weights: dict[str, float] | None = None
    rebalance_rates: list[float] = []
    turnover_rates: list[float] = []

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
        rebalance_rate: float | None = None
        if previous_long is not None and previous_short is not None:
            long_rebalance = _leg_rebalance_rate(previous_long, long_names)
            short_rebalance = _leg_rebalance_rate(previous_short, short_names)
            rebalance_rate = (long_rebalance + short_rebalance) / 2.0
            rebalance_rates.append(rebalance_rate)
        weights = _portfolio_weights(long_leg, short_leg)
        turnover_rate, traded_notional_rate = _portfolio_turnover(previous_weights, weights)
        turnover_rates.append(turnover_rate)
        transaction_cost = _transaction_cost_rate(
            traded_notional_rate=traded_notional_rate,
            holding_days=holding,
            costs=costs,
        )
        net_period_return = period_return - transaction_cost
        previous_long = long_names
        previous_short = short_names
        previous_weights = weights
        rows.append(
            {
                "signal_date": signal_date.date().isoformat(),
                "entry_date": entry_date.date().isoformat(),
                "exit_date": exit_date.date().isoformat(),
                "long_return": long_return,
                "short_return": short_return,
                "period_return": period_return,
                "gross_period_return": period_return,
                "net_period_return": net_period_return,
                "transaction_cost": transaction_cost,
                "group_returns": period_group_returns,
                "rebalance_rate": rebalance_rate,
                "turnover_rate": turnover_rate,
            }
        )

    gross_returns = np.array([float(row["gross_period_return"]) for row in rows], dtype=float)
    net_returns = np.array([float(row["net_period_return"]) for row in rows], dtype=float)
    gross_summary = _return_summary(gross_returns, holding)
    net_summary = _return_summary(net_returns, holding)
    rebalance_rate = float(np.mean(rebalance_rates)) if rebalance_rates else 0.0
    turnover_rate = float(np.mean(turnover_rates)) if turnover_rates else 0.0
    segment_metrics = _segment_metrics(rows, holding, sample_splits or DEFAULT_SAMPLE_SPLITS)
    warnings = _backtest_warnings(
        periods=len(rows),
        rebalance_rate=rebalance_rate,
        turnover_rate=turnover_rate,
        gross_annualized_return=gross_summary["annualized_return"],
        net_annualized_return=net_summary["annualized_return"],
        segment_metrics=segment_metrics,
    )
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
    assumptions = _assumptions()
    execution = (
        "signal_date plus one trading day; non-overlapping holding periods"
        if delay == 1
        else f"signal_date plus {delay} trading days; non-overlapping holding periods"
    )
    write_json(
        artifact_path,
        {
            "assumptions": assumptions,
            "factor_id": factor_id,
            "formula": factor.formula,
            "execution": execution,
            "holding_days": holding,
            "top_quantile": top_quantile,
            "simulation_profile": asdict(profile),
            "transaction_costs": asdict(costs),
            "periods": len(rows),
            "cumulative_return": gross_summary["cumulative_return"],
            "annualized_return": gross_summary["annualized_return"],
            "annualized_volatility": gross_summary["annualized_volatility"],
            "long_short_sharpe": gross_summary["long_short_sharpe"],
            "max_drawdown": gross_summary["max_drawdown"],
            "gross_cumulative_return": gross_summary["cumulative_return"],
            "gross_annualized_return": gross_summary["annualized_return"],
            "gross_annualized_volatility": gross_summary["annualized_volatility"],
            "gross_long_short_sharpe": gross_summary["long_short_sharpe"],
            "gross_max_drawdown": gross_summary["max_drawdown"],
            "net_cumulative_return": net_summary["cumulative_return"],
            "net_annualized_return": net_summary["annualized_return"],
            "net_annualized_volatility": net_summary["annualized_volatility"],
            "net_long_short_sharpe": net_summary["long_short_sharpe"],
            "net_max_drawdown": net_summary["max_drawdown"],
            "rebalance_rate": rebalance_rate,
            "turnover_rate": turnover_rate,
            "group_returns": [asdict(metric) for metric in group_metrics],
            "segment_metrics": [asdict(metric) for metric in segment_metrics],
            "warnings": list(warnings),
            "period_returns": rows,
        },
    )
    return BacktestResult(
        factor_id=factor_id,
        periods=len(rows),
        holding_days=holding,
        cumulative_return=gross_summary["cumulative_return"],
        annualized_return=gross_summary["annualized_return"],
        annualized_volatility=gross_summary["annualized_volatility"],
        max_drawdown=gross_summary["max_drawdown"],
        artifact_path=artifact_path,
        long_short_sharpe=gross_summary["long_short_sharpe"],
        gross_cumulative_return=gross_summary["cumulative_return"],
        gross_annualized_return=gross_summary["annualized_return"],
        gross_annualized_volatility=gross_summary["annualized_volatility"],
        gross_long_short_sharpe=gross_summary["long_short_sharpe"],
        gross_max_drawdown=gross_summary["max_drawdown"],
        rebalance_rate=rebalance_rate,
        turnover_rate=turnover_rate,
        net_cumulative_return=net_summary["cumulative_return"],
        net_annualized_return=net_summary["annualized_return"],
        net_annualized_volatility=net_summary["annualized_volatility"],
        net_long_short_sharpe=net_summary["long_short_sharpe"],
        net_max_drawdown=net_summary["max_drawdown"],
        top_quantile=top_quantile,
        transaction_costs=costs,
        simulation_profile=profile,
        group_returns=group_metrics,
        segment_metrics=segment_metrics,
        warnings=warnings,
        assumptions=assumptions,
    )


def _with_period_return(close: pd.DataFrame, entry_date: pd.Timestamp, exit_date: pd.Timestamp) -> pd.DataFrame:
    entry = close[close["trade_date"] == entry_date][["instrument", "close"]].rename(columns={"close": "entry_close"})
    exit_ = close[close["trade_date"] == exit_date][["instrument", "close"]].rename(columns={"close": "exit_close"})
    result = entry.merge(exit_, on="instrument", how="inner")
    result["period_return"] = result["exit_close"] / result["entry_close"] - 1.0
    return result[["instrument", "period_return"]]


def _return_summary(returns: np.ndarray, holding_days: int) -> dict[str, float]:
    equity = np.cumprod(1.0 + returns) if len(returns) else np.array([], dtype=float)
    cumulative_return = float(equity[-1] - 1.0) if len(equity) else 0.0
    annualized_return = (
        float((1.0 + cumulative_return) ** (252 / (holding_days * len(returns))) - 1.0) if len(returns) else 0.0
    )
    annualized_volatility = (
        float(np.std(returns, ddof=1) * np.sqrt(252 / holding_days)) if len(returns) > 1 else 0.0
    )
    return {
        "cumulative_return": cumulative_return,
        "annualized_return": annualized_return,
        "annualized_volatility": annualized_volatility,
        "long_short_sharpe": _long_short_sharpe(returns, holding_days),
        "max_drawdown": _max_drawdown(equity),
    }


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


def _leg_rebalance_rate(previous: set[str], current: set[str]) -> float:
    denominator = max(len(previous), len(current), 1)
    overlap = len(previous.intersection(current))
    return float(1.0 - overlap / denominator)


def _portfolio_weights(long_leg: pd.DataFrame, short_leg: pd.DataFrame) -> dict[str, float]:
    weights: dict[str, float] = {}
    if not long_leg.empty:
        long_weight = 1.0 / len(long_leg)
        for instrument in long_leg["instrument"].astype(str):
            weights[instrument] = weights.get(instrument, 0.0) + long_weight
    if not short_leg.empty:
        short_weight = -1.0 / len(short_leg)
        for instrument in short_leg["instrument"].astype(str):
            weights[instrument] = weights.get(instrument, 0.0) + short_weight
    return weights


def _portfolio_turnover(previous: dict[str, float] | None, current: dict[str, float]) -> tuple[float, float]:
    previous_weights = previous or {}
    instruments = set(previous_weights).union(current)
    traded_notional_rate = float(
        sum(abs(current.get(instrument, 0.0) - previous_weights.get(instrument, 0.0)) for instrument in instruments)
    )
    return traded_notional_rate / 2.0, traded_notional_rate


def _transaction_cost_rate(
    *,
    traded_notional_rate: float,
    holding_days: int,
    costs: TransactionCostModel,
) -> float:
    traded_cost = traded_notional_rate * (costs.commission_bps + costs.slippage_bps) / 10_000.0
    borrow_cost = costs.short_borrow_bps_annual / 10_000.0 * holding_days / 252.0
    return float(traded_cost + borrow_cost)


def _long_short_sharpe(returns: np.ndarray, holding_days: int) -> float:
    if len(returns) < 2:
        return 0.0
    std = float(np.std(returns, ddof=1))
    if std == 0.0:
        return 0.0
    return float(np.mean(returns) / std * np.sqrt(252 / holding_days))


def _segment_metrics(
    rows: list[dict[str, object]],
    holding_days: int,
    split_specs: tuple[SampleSplitSpec, ...],
) -> tuple[BacktestSegmentMetric, ...]:
    split_rows = _split_rows_by_signal_date(rows, split_specs)
    metrics: list[BacktestSegmentMetric] = []
    for spec, segment in zip(split_specs, split_rows, strict=True):
        gross_returns = np.array([float(row["gross_period_return"]) for row in segment], dtype=float)
        net_returns = np.array([float(row["net_period_return"]) for row in segment], dtype=float)
        gross = _return_summary(gross_returns, holding_days)
        net = _return_summary(net_returns, holding_days)
        metrics.append(
            BacktestSegmentMetric(
                name=spec.name,
                start_date=str(segment[0]["signal_date"]) if segment else "",
                end_date=str(segment[-1]["signal_date"]) if segment else "",
                periods=len(segment),
                gross_cumulative_return=gross["cumulative_return"],
                gross_annualized_return=gross["annualized_return"],
                gross_long_short_sharpe=gross["long_short_sharpe"],
                gross_max_drawdown=gross["max_drawdown"],
                net_cumulative_return=net["cumulative_return"],
                net_annualized_return=net["annualized_return"],
                net_long_short_sharpe=net["long_short_sharpe"],
                net_max_drawdown=net["max_drawdown"],
            )
        )
    return tuple(metrics)


def _split_rows_by_signal_date(
    rows: list[dict[str, object]],
    split_specs: tuple[SampleSplitSpec, ...],
) -> tuple[tuple[dict[str, object], ...], ...]:
    if not rows:
        return tuple(tuple() for _ in split_specs)
    dates = sorted({pd.Timestamp(str(row["signal_date"])) for row in rows})
    date_chunks = _split_dates(dates, split_specs)
    chunk_sets = [{date.date().isoformat() for date in chunk} for chunk in date_chunks]
    return tuple(tuple(row for row in rows if str(row["signal_date"]) in chunk) for chunk in chunk_sets)


def _split_dates(
    dates: list[pd.Timestamp],
    split_specs: tuple[SampleSplitSpec, ...],
) -> tuple[tuple[pd.Timestamp, ...], ...]:
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


def _backtest_warnings(
    *,
    periods: int,
    rebalance_rate: float,
    turnover_rate: float,
    gross_annualized_return: float,
    net_annualized_return: float,
    segment_metrics: tuple[BacktestSegmentMetric, ...],
) -> tuple[str, ...]:
    warnings: list[str] = []
    if periods < 2:
        warnings.append("too few backtest periods for stable Sharpe or drawdown estimates")
    if rebalance_rate > 0.8:
        warnings.append("high rebalance rate")
    if turnover_rate > 1.5:
        warnings.append("high turnover rate")
    if gross_annualized_return > 0 and net_annualized_return < gross_annualized_return * 0.5:
        warnings.append("net-of-cost performance is highly sensitive to transaction costs")
    if _oos_segment_decay(segment_metrics):
        warnings.append("OOS segment net performance decays versus IS")
    return tuple(dict.fromkeys(warnings))


def _oos_segment_decay(segment_metrics: tuple[BacktestSegmentMetric, ...]) -> bool:
    by_name = {metric.name.upper(): metric for metric in segment_metrics}
    is_metric = by_name.get("IS")
    if is_metric is None or is_metric.periods == 0 or is_metric.net_annualized_return <= 0:
        return False
    oos_metrics = [metric for name, metric in by_name.items() if name.startswith("OOS") and metric.periods > 0]
    return any(metric.net_annualized_return < is_metric.net_annualized_return * 0.5 for metric in oos_metrics)


def _assumptions() -> tuple[str, ...]:
    return (
        "research_only_not_production_trading",
        "signals enter after configured trading-day delay",
        "non-overlapping holding periods",
        "close-to-close period returns",
        "transaction costs are configurable research assumptions",
        "rebalance_rate tracks component replacement per rebalance",
        "turnover_rate estimates true portfolio weight turnover",
    )

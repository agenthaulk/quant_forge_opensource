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
    METRICS_SCHEMA_VERSION,
    MetricValue,
    SampleSplitSpec,
    SimulationProfile,
    TransactionCostModel,
)
from quant_forge.data.local import LocalPanelDataProvider
from quant_forge.evaluation.service import DEFAULT_SAMPLE_SPLITS
from quant_forge.factor_engine.signal_processing import (
    apply_test_period,
    prepare_factor_scores_result,
    require_minimum_display_trading_days,
    simulation_profile_suffix,
)
from quant_forge.factor_library.catalog import FactorCatalog
from quant_forge.utils import write_json


MIN_ANNUALIZATION_EXPOSURE_DAYS = 126
EXTERNAL_OOS_ROLE = "external_oos_backtest"
IN_SAMPLE_ROLE = "in_sample_backtest"
STAGGERED_COHORT_ROLE = "staggered_entry_cohort"
STAGGERED_AGGREGATE_ROLE = "staggered_entry_backtest"
INSUFFICIENT_ANNUALIZATION_HISTORY = "INSUFFICIENT_ANNUALIZATION_HISTORY"
INSUFFICIENT_VOLATILITY_OBSERVATIONS = "INSUFFICIENT_VOLATILITY_OBSERVATIONS"
INSUFFICIENT_SHARPE_OBSERVATIONS = "INSUFFICIENT_SHARPE_OBSERVATIONS"
DAILY_NAV_UNAVAILABLE = "DAILY_NAV_UNAVAILABLE"
NO_REBALANCE_OBSERVATIONS = "NO_REBALANCE_OBSERVATIONS"
PARTIAL_FINAL_PERIOD = "PARTIAL_FINAL_PERIOD"


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
    factor_values_root: Path | None = None,
    factor_values_overlay_root: Path | None = None,
    factor_values_manifest_root: Path | None = None,
    sample_role: str = EXTERNAL_OOS_ROLE,
    first_signal_date: str | None = None,
    include_partial_final_period: bool = True,
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
    factor = FactorCatalog(
        factor_root,
        factor_values_root=factor_values_root,
        factor_values_manifest_root=factor_values_manifest_root,
    ).get(factor_id)
    holding = holding_days or factor.horizon_days
    if holding < 1:
        raise ValueError("holding_days must be positive")
    panel = LocalPanelDataProvider(data_root).load_panel()
    working_panel = apply_test_period(panel, profile)
    require_minimum_display_trading_days(
        working_panel,
        min_trading_days=max(2, holding + profile.execution_delay_days + 1),
    )
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
    close = working_panel[["trade_date", "instrument", "close"]].copy()
    dates = sorted(working_panel["trade_date"].drop_duplicates())
    rows: list[dict[str, object]] = []
    grouped_returns: dict[str, list[float]] = {}
    previous_long: set[str] | None = None
    previous_short: set[str] | None = None
    previous_weights: dict[str, float] | None = None
    gross_nav_base = 1.0
    net_nav_base = 1.0
    rebalance_rates: list[float] = []
    rebalance_turnovers: list[float] = []
    initial_build_turnover: float | None = None
    daily_nav: list[dict[str, object]] = []

    delay = profile.execution_delay_days
    start_signal_index = _resolve_start_signal_index(dates, first_signal_date)
    completed_periods = 0
    partial_periods = 0
    for signal_index in range(start_signal_index, len(dates) - delay - 1, holding):
        signal_date = dates[signal_index]
        entry_date = dates[signal_index + delay]
        scheduled_exit_index = signal_index + delay + holding
        is_complete_period = scheduled_exit_index < len(dates)
        if is_complete_period:
            actual_exit_index = scheduled_exit_index
        elif include_partial_final_period:
            actual_exit_index = len(dates) - 1
        else:
            break
        if actual_exit_index <= signal_index + delay:
            break
        exit_date = dates[actual_exit_index]
        trading_days_held = int(actual_exit_index - (signal_index + delay))
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
        initial_build_for_period: float | None = None
        rebalance_turnover: float | None = None
        if previous_weights is None:
            initial_build_for_period = turnover_rate
            initial_build_turnover = turnover_rate
        else:
            rebalance_turnover = turnover_rate
            rebalance_turnovers.append(turnover_rate)
        transaction_cost = _transaction_cost_rate(
            traded_notional_rate=traded_notional_rate,
            holding_days=trading_days_held,
            costs=costs,
        )
        net_period_return = period_return - transaction_cost
        period_dates = dates[signal_index + delay : actual_exit_index + 1]
        period_nav = _daily_nav_for_period(
            dates=[item.date().isoformat() for item in period_dates],
            long_returns=_leg_cumulative_returns(close, long_names, entry_date, period_dates),
            short_returns=_leg_cumulative_returns(close, short_names, entry_date, period_dates),
            transaction_cost=transaction_cost,
        )
        for nav_row in period_nav:
            daily_nav.append(
                {
                    "date": nav_row["date"],
                    "period_index": len(rows),
                    "gross_nav": gross_nav_base * float(nav_row["gross_nav"]),
                    "net_nav": net_nav_base * float(nav_row["net_nav"]),
                    "gross_period_nav": nav_row["gross_nav"],
                    "net_period_nav": nav_row["net_nav"],
                }
            )
        if period_nav:
            # Compound off the last finite mark; a trailing unmarkable (NaN) day
            # must not poison the running base for subsequent periods (COR-8).
            last_gross = next(
                (float(row["gross_nav"]) for row in reversed(period_nav) if not np.isnan(float(row["gross_nav"]))),
                None,
            )
            last_net = next(
                (float(row["net_nav"]) for row in reversed(period_nav) if not np.isnan(float(row["net_nav"]))),
                None,
            )
            if last_gross is not None:
                gross_nav_base *= last_gross
            if last_net is not None:
                net_nav_base *= last_net
        previous_long = long_names
        previous_short = short_names
        previous_weights = weights
        if is_complete_period:
            completed_periods += 1
        else:
            partial_periods += 1
        rows.append(
            {
                "period_id": len(rows),
                "signal_date": signal_date.date().isoformat(),
                "entry_date": entry_date.date().isoformat(),
                "scheduled_exit_date": (
                    dates[scheduled_exit_index].date().isoformat() if scheduled_exit_index < len(dates) else None
                ),
                "actual_exit_or_rebalance_date": exit_date.date().isoformat(),
                "exit_date": exit_date.date().isoformat(),
                "is_complete_period": is_complete_period,
                "is_partial_final_period": not is_complete_period,
                "trading_days_held": trading_days_held,
                "eligible_universe_count": int(len(signal)),
                "valid_score_count": int(len(signal)),
                "long_count": int(len(long_leg)),
                "short_count": int(len(short_leg)),
                "long_instruments": tuple(sorted(long_names)),
                "short_instruments": tuple(sorted(short_names)),
                "long_return": long_return,
                "short_return": short_return,
                "period_return": period_return,
                "gross_period_return": period_return,
                "net_period_return": net_period_return,
                "transaction_cost": transaction_cost,
                "group_returns": period_group_returns,
                "rebalance_rate": rebalance_rate,
                "turnover_rate": turnover_rate,
                "initial_build_turnover": initial_build_for_period,
                "rebalance_turnover": rebalance_turnover,
                "replacement_rate": rebalance_rate,
                "daily_nav": period_nav,
            }
        )

    gross_returns = np.array([float(row["gross_period_return"]) for row in rows], dtype=float)
    net_returns = np.array([float(row["net_period_return"]) for row in rows], dtype=float)
    # Exclude any partial final period from vol/Sharpe: those metrics scale by a
    # uniform full holding period and would misprice a short tail (COR-6).
    complete_gross_returns = np.array(
        [float(row["gross_period_return"]) for row in rows if row["is_complete_period"]], dtype=float
    )
    complete_net_returns = np.array(
        [float(row["net_period_return"]) for row in rows if row["is_complete_period"]], dtype=float
    )
    exposure_days = int(sum(int(row.get("trading_days_held", holding)) for row in rows))
    gross_summary = _return_summary(
        gross_returns,
        holding,
        daily_nav=daily_nav,
        exposure_days=exposure_days,
        sample_role=sample_role,
        volatility_returns=complete_gross_returns,
    )
    net_summary = _return_summary(
        net_returns,
        holding,
        daily_nav=daily_nav,
        exposure_days=exposure_days,
        nav_key="net_nav",
        sample_role=sample_role,
        volatility_returns=complete_net_returns,
    )
    rebalance_rate = float(np.mean(rebalance_rates)) if rebalance_rates else None
    rebalance_turnover_mean = float(np.mean(rebalance_turnovers)) if rebalance_turnovers else None
    turnover_rate = rebalance_turnover_mean
    segment_metrics = _segment_metrics(rows, holding, sample_splits or DEFAULT_SAMPLE_SPLITS, sample_role=sample_role)
    warnings = _backtest_warnings(
        periods=len(rows),
        holding_days=holding,
        exposure_days=exposure_days,
        rebalance_rate=rebalance_rate,
        turnover_rate=turnover_rate,
        gross_annualized_return=gross_summary["annualized_return"],
        net_annualized_return=net_summary["annualized_return"],
        segment_metrics=segment_metrics,
    )
    warning_code_items = [
        *gross_summary["warning_codes"],
        *net_summary["warning_codes"],
    ]
    if rebalance_rate is None:
        warning_code_items.append(NO_REBALANCE_OBSERVATIONS)
    if partial_periods:
        warning_code_items.append(PARTIAL_FINAL_PERIOD)
    warning_codes = tuple(dict.fromkeys(warning_code_items))
    metrics = {
        "annualized_return": gross_summary["metrics"]["annualized_return"],
        "net_annualized_return": net_summary["metrics"]["annualized_return"],
        "annualized_volatility": gross_summary["metrics"]["annualized_volatility"],
        "net_annualized_volatility": net_summary["metrics"]["annualized_volatility"],
        "long_short_sharpe": gross_summary["metrics"]["long_short_sharpe"],
        "net_long_short_sharpe": net_summary["metrics"]["long_short_sharpe"],
        "max_drawdown": gross_summary["metrics"]["max_drawdown"],
        "net_max_drawdown": net_summary["metrics"]["max_drawdown"],
        "rebalance_rate": MetricValue(
            value=rebalance_rate,
            unit="ratio",
            status="available" if rebalance_rate is not None else "not_applicable",
            observation_count=len(rebalance_rates),
            minimum_required=1,
            method="membership_replacement_mean",
            source_series="period_rebalance_rows",
            sample_role=sample_role,
            warning_codes=() if rebalance_rate is not None else (NO_REBALANCE_OBSERVATIONS,),
        ),
        "rebalance_turnover_mean": MetricValue(
            value=rebalance_turnover_mean,
            unit="ratio",
            status="available" if rebalance_turnover_mean is not None else "not_applicable",
            observation_count=len(rebalance_turnovers),
            minimum_required=1,
            method="weight_turnover_mean_ex_initial_build",
            source_series="period_rebalance_rows",
            sample_role=sample_role,
            warning_codes=() if rebalance_turnover_mean is not None else (NO_REBALANCE_OBSERVATIONS,),
        ),
    }
    metric_provenance = {key: _metric_provenance(value) for key, value in metrics.items()}
    cost_reconciliation = {
        "period_transaction_cost_sum": float(sum(float(row["transaction_cost"]) for row in rows)),
        "gross_terminal_equity": float(1.0 + gross_summary["cumulative_return"]),
        "net_terminal_equity": float(1.0 + net_summary["cumulative_return"]),
    }
    benchmark = _cash_benchmark_spec()
    daily_ledger = _daily_ledger_from_nav(daily_nav)
    benchmark_cumulative_return = 0.0 if daily_ledger else None
    arithmetic_excess_return = (
        float(net_summary["cumulative_return"] - benchmark_cumulative_return)
        if benchmark_cumulative_return is not None
        else None
    )
    relative_wealth_excess_return = (
        float((1.0 + net_summary["cumulative_return"]) / (1.0 + benchmark_cumulative_return) - 1.0)
        if benchmark_cumulative_return is not None
        else None
    )
    tracking_error, information_ratio = _active_risk_metrics(daily_ledger)
    resolved_schedule = tuple(_schedule_row(row) for row in rows)
    rebalance_ledger = tuple(_rebalance_row(row) for row in rows)
    request_snapshot = {
        "factor_id": factor_id,
        "sample_role": sample_role,
        "start_date": profile.test_period_start,
        "end_date": profile.test_period_end,
        "holding_days": holding,
        "execution_delay_days": delay,
        "execution_price": "close",
        "first_entry_policy": "first_signal_inside_window_then_next_day_entry",
        "final_period_policy": "mark_to_market_partial_final",
        "portfolio_mode": "long_short",
        "top_quantile": top_quantile,
        "benchmark": benchmark,
        "transaction_costs": asdict(costs),
    }
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
            "schema_version": METRICS_SCHEMA_VERSION,
            "sample_role": sample_role,
            "return_series_kind": "scheduled_horizon_daily_mark_to_market",
            "request": request_snapshot,
            "assumptions": assumptions,
            "factor_id": factor_id,
            "formula": factor.formula,
            "execution": execution,
            "holding_days": holding,
            "top_quantile": top_quantile,
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
            "transaction_costs": asdict(costs),
            "periods": len(rows),
            "completed_periods": completed_periods,
            "partial_periods": partial_periods,
            "exposure_days": exposure_days,
            "calendar_days": _calendar_days(rows),
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
            "reportable_annualization": asdict(gross_summary["reportable_annualization"]),
            "extrapolated_annualization": asdict(gross_summary["extrapolated_annualization"]),
            "daily_nav": daily_nav,
            "daily_ledger": daily_ledger,
            "benchmark": benchmark,
            "benchmark_cumulative_return": benchmark_cumulative_return,
            "arithmetic_excess_return": arithmetic_excess_return,
            "relative_wealth_excess_return": relative_wealth_excess_return,
            "tracking_error": tracking_error,
            "information_ratio": information_ratio,
            "resolved_schedule": list(resolved_schedule),
            "rebalance_ledger": list(rebalance_ledger),
            "initial_build_turnover": initial_build_turnover,
            "rebalance_turnover_mean": rebalance_turnover_mean,
            "rebalance_turnover_observation_count": len(rebalance_turnovers),
            "replacement_rate_mean": rebalance_rate,
            "replacement_rate_observation_count": len(rebalance_rates),
            "cost_reconciliation": cost_reconciliation,
            "metric_provenance": metric_provenance,
            "warning_codes": list(warning_codes),
            "metrics": {key: asdict(value) for key, value in metrics.items()},
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
        schema_version=METRICS_SCHEMA_VERSION,
        sample_role=sample_role,
        return_series_kind="scheduled_horizon_daily_mark_to_market",
        completed_periods=completed_periods,
        partial_periods=partial_periods,
        exposure_days=exposure_days,
        calendar_days=_calendar_days(rows),
        reportable_annualization=gross_summary["reportable_annualization"],
        extrapolated_annualization=gross_summary["extrapolated_annualization"],
        daily_nav=tuple(daily_nav),
        initial_build_turnover=initial_build_turnover,
        rebalance_turnover_mean=rebalance_turnover_mean,
        rebalance_turnover_observation_count=len(rebalance_turnovers),
        replacement_rate_mean=rebalance_rate,
        replacement_rate_observation_count=len(rebalance_rates),
        cost_reconciliation=cost_reconciliation,
        metric_provenance=metric_provenance,
        warning_codes=warning_codes,
        metrics=metrics,
        benchmark=benchmark,
        benchmark_cumulative_return=benchmark_cumulative_return,
        arithmetic_excess_return=arithmetic_excess_return,
        relative_wealth_excess_return=relative_wealth_excess_return,
        tracking_error=tracking_error,
        information_ratio=information_ratio,
        daily_ledger=tuple(daily_ledger),
        resolved_schedule=resolved_schedule,
        rebalance_ledger=rebalance_ledger,
        request_snapshot=request_snapshot,
    )


def run_staggered_entry_backtest(
    factor_id: str,
    *,
    factor_root: Path,
    data_root: Path,
    artifact_root: Path,
    holding_days: int | None = None,
    simulation_profile: SimulationProfile | None = None,
    transaction_costs: TransactionCostModel | None = None,
    formation_trading_days: int | None = None,
    factor_values_root: Path | None = None,
    factor_values_overlay_root: Path | None = None,
    factor_values_manifest_root: Path | None = None,
) -> dict[str, object]:
    """Run a first-month staggered-entry robustness backtest.

    Each cohort delegates to ``run_factor_backtest`` with a different first
    signal date. Aggregation only combines sleeve NAVs with fixed capital
    weights, so it does not duplicate the portfolio return formula.
    """

    profile = simulation_profile or SimulationProfile()
    panel = LocalPanelDataProvider(data_root).load_panel()
    working_panel = apply_test_period(panel, profile)
    dates = sorted(working_panel["trade_date"].drop_duplicates())
    if not dates:
        raise ValueError("staggered-entry backtest requires at least one trading date")
    signal_dates = _formation_signal_dates(dates, profile.execution_delay_days, formation_trading_days)
    if not signal_dates:
        raise ValueError("staggered-entry formation window has no eligible signal dates")
    capital_weight = 1.0 / len(signal_dates)
    cohorts: list[dict[str, object]] = []
    for signal_date in signal_dates:
        signal_label = signal_date.date().isoformat()
        cohort_profile = SimulationProfile(
            market=profile.market,
            instrument_type=profile.instrument_type,
            universe=profile.universe,
            execution_delay_days=profile.execution_delay_days,
            top_quantile=profile.top_quantile,
            nan_policy=profile.nan_policy,
            neutralization=profile.neutralization,
            truncation=profile.truncation,
            decay_days=profile.decay_days,
            test_period_start=signal_label,
            test_period_end=profile.test_period_end,
        )
        cohort_result = run_factor_backtest(
            factor_id,
            factor_root=factor_root,
            data_root=data_root,
            artifact_root=artifact_root,
            holding_days=holding_days,
            simulation_profile=cohort_profile,
            transaction_costs=transaction_costs,
            factor_values_root=factor_values_root,
            factor_values_overlay_root=factor_values_overlay_root,
            factor_values_manifest_root=factor_values_manifest_root,
            sample_role=STAGGERED_COHORT_ROLE,
            first_signal_date=signal_label,
        )
        cohorts.append(
            {
                "signal_date": signal_label,
                "capital_weight": capital_weight,
                "artifact_path": str(cohort_result.artifact_path),
                "net_cumulative_return": cohort_result.net_cumulative_return,
                "benchmark_cumulative_return": cohort_result.benchmark_cumulative_return,
                "arithmetic_excess_return": cohort_result.arithmetic_excess_return,
                "relative_wealth_excess_return": cohort_result.relative_wealth_excess_return,
                "daily_nav": list(cohort_result.daily_nav),
                "completed_periods": cohort_result.completed_periods,
                "partial_periods": cohort_result.partial_periods,
            }
        )
    aggregate_nav = _aggregate_staggered_nav(dates, cohorts)
    benchmark = _cash_benchmark_spec()
    terminal_nav = float(aggregate_nav[-1]["net_nav"]) if aggregate_nav else 1.0
    benchmark_terminal = float(aggregate_nav[-1]["benchmark_nav"]) if aggregate_nav else 1.0
    result = {
        "schema_version": METRICS_SCHEMA_VERSION,
        "sample_role": STAGGERED_AGGREGATE_ROLE,
        "factor_id": factor_id,
        "holding_days": holding_days,
        "simulation_profile": asdict(profile),
        "formation_window_mode": "first_n_trading_days" if formation_trading_days else "first_calendar_month",
        "formation_signal_dates": [item.date().isoformat() for item in signal_dates],
        "cohort_count": len(cohorts),
        "capital_allocation": "equal_sleeves",
        "benchmark": benchmark,
        "strategy_cumulative_return": terminal_nav - 1.0,
        "benchmark_cumulative_return": benchmark_terminal - 1.0,
        "arithmetic_excess_return": terminal_nav - benchmark_terminal,
        "relative_wealth_excess_return": terminal_nav / benchmark_terminal - 1.0 if benchmark_terminal else None,
        "daily_nav": aggregate_nav,
        "cohorts": cohorts,
        "warning_codes": [],
    }
    artifact_path = artifact_root.expanduser() / "staggered_backtests" / f"{factor_id}{simulation_profile_suffix(profile)}.json"
    write_json(artifact_path, result)
    result["artifact_path"] = str(artifact_path)
    return result


def _with_period_return(close: pd.DataFrame, entry_date: pd.Timestamp, exit_date: pd.Timestamp) -> pd.DataFrame:
    entry = close[close["trade_date"] == entry_date][["instrument", "close"]].rename(columns={"close": "entry_close"})
    exit_ = close[close["trade_date"] == exit_date][["instrument", "close"]].rename(columns={"close": "exit_close"})
    result = entry.merge(exit_, on="instrument", how="inner")
    result["period_return"] = result["exit_close"] / result["entry_close"] - 1.0
    return result[["instrument", "period_return"]]


def _resolve_start_signal_index(dates: list[pd.Timestamp], first_signal_date: str | None) -> int:
    if not first_signal_date:
        return 0
    target = pd.Timestamp(first_signal_date)
    for index, item in enumerate(dates):
        if item >= target:
            return index
    return len(dates)


def _formation_signal_dates(
    dates: list[pd.Timestamp],
    execution_delay_days: int,
    formation_trading_days: int | None,
) -> tuple[pd.Timestamp, ...]:
    eligible = tuple(item for index, item in enumerate(dates) if index + execution_delay_days < len(dates))
    if formation_trading_days is not None:
        if formation_trading_days < 1:
            raise ValueError("formation_trading_days must be positive")
        return eligible[:formation_trading_days]
    if not eligible:
        return ()
    first = eligible[0]
    return tuple(item for item in eligible if item.year == first.year and item.month == first.month)


def _aggregate_staggered_nav(
    dates: list[pd.Timestamp],
    cohorts: list[dict[str, object]],
) -> list[dict[str, object]]:
    cohort_states: list[dict[str, object]] = []
    for cohort in cohorts:
        nav_rows = list(cohort["daily_nav"])
        by_date = {str(row["date"]): row for row in nav_rows}
        cohort_states.append(
            {
                "capital_weight": float(cohort["capital_weight"]),
                "by_date": by_date,
                "first_date": str(nav_rows[0]["date"]) if nav_rows else None,
                "last_net_nav": 1.0,
                "last_benchmark_nav": 1.0,
            }
        )

    rows: list[dict[str, object]] = []
    previous_net_nav = 1.0
    for index, item in enumerate(dates):
        date_label = item.date().isoformat()
        net_nav = 0.0
        benchmark_nav = 0.0
        active_count = 0
        inactive_cash_weight = 0.0
        for state in cohort_states:
            weight = float(state["capital_weight"])
            first_date = state["first_date"]
            by_date = state["by_date"]
            if first_date is None or date_label < first_date:
                net_nav += weight
                benchmark_nav += weight
                inactive_cash_weight += weight
                continue
            nav_row = by_date.get(date_label)
            if nav_row is not None:
                state["last_net_nav"] = float(nav_row["net_nav"])
                state["last_benchmark_nav"] = 1.0
            net_nav += weight * float(state["last_net_nav"])
            benchmark_nav += weight * float(state["last_benchmark_nav"])
            active_count += 1
        daily_net_return = net_nav / previous_net_nav - 1.0 if index else net_nav - 1.0
        rows.append(
            {
                "date": date_label,
                "net_nav": net_nav,
                "benchmark_nav": benchmark_nav,
                "relative_nav": net_nav / benchmark_nav if benchmark_nav else None,
                "daily_net_return": daily_net_return,
                "active_cohort_count": active_count,
                "inactive_cash_weight": inactive_cash_weight,
                "capital_weight_sum": sum(float(cohort["capital_weight"]) for cohort in cohorts),
            }
        )
        previous_net_nav = net_nav
    return rows


def _cash_benchmark_spec() -> dict[str, object]:
    return {
        "benchmark_id": "cash",
        "benchmark_type": "cash",
        "return_field": "cash_return",
        "currency": "local",
        "missing_policy": "fill_zero",
        "cost_policy": "none",
    }


def _daily_ledger_from_nav(daily_nav: list[dict[str, object]]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    previous_gross = 1.0
    previous_net = 1.0
    previous_benchmark = 1.0
    for index, item in enumerate(daily_nav):
        gross_nav = float(item["gross_nav"])
        net_nav = float(item["net_nav"])
        benchmark_nav = 1.0
        gross_daily_return = gross_nav / previous_gross - 1.0 if index else gross_nav - 1.0
        net_daily_return = net_nav / previous_net - 1.0 if index else net_nav - 1.0
        benchmark_return = benchmark_nav / previous_benchmark - 1.0 if index else 0.0
        active_return = net_daily_return - benchmark_return
        rows.append(
            {
                "trade_date": item["date"],
                "period_index": item.get("period_index"),
                "gross_nav": gross_nav,
                "net_nav": net_nav,
                "benchmark_nav": benchmark_nav,
                "relative_nav": net_nav / benchmark_nav if benchmark_nav else None,
                "daily_gross_return": gross_daily_return,
                "daily_net_return": net_daily_return,
                "benchmark_return": benchmark_return,
                "daily_active_return": active_return,
                "cash_weight": 0.0,
                "gross_exposure": 2.0,
                "net_exposure": 0.0,
            }
        )
        previous_gross = gross_nav
        previous_net = net_nav
        previous_benchmark = benchmark_nav
    return rows


def _active_risk_metrics(daily_ledger: list[dict[str, object]]) -> tuple[float | None, float | None]:
    active = np.array([float(row["daily_active_return"]) for row in daily_ledger[1:]], dtype=float)
    if len(active) < 2:
        return None, None
    std = float(np.std(active, ddof=1))
    if std == 0.0:
        return 0.0, None
    tracking_error = float(std * np.sqrt(252))
    information_ratio = float(np.mean(active) / std * np.sqrt(252))
    return tracking_error, information_ratio


def _schedule_row(row: dict[str, object]) -> dict[str, object]:
    return {
        "period_id": row["period_id"],
        "signal_date": row["signal_date"],
        "entry_date": row["entry_date"],
        "scheduled_exit_date": row["scheduled_exit_date"],
        "actual_exit_or_rebalance_date": row["actual_exit_or_rebalance_date"],
        "is_complete_period": row["is_complete_period"],
        "is_partial_final_period": row["is_partial_final_period"],
        "trading_days_held": row["trading_days_held"],
    }


def _rebalance_row(row: dict[str, object]) -> dict[str, object]:
    return {
        "period_id": row["period_id"],
        "signal_date": row["signal_date"],
        "entry_date": row["entry_date"],
        "long_count": row["long_count"],
        "short_count": row["short_count"],
        "turnover_rate": row["turnover_rate"],
        "initial_build_turnover": row["initial_build_turnover"],
        "rebalance_turnover": row["rebalance_turnover"],
        "replacement_rate": row["replacement_rate"],
        "transaction_cost": row["transaction_cost"],
    }


def _leg_cumulative_returns(
    close: pd.DataFrame,
    instruments: set[str],
    entry_date: pd.Timestamp,
    dates: list[pd.Timestamp],
) -> list[float]:
    if not dates:
        return []
    if not instruments:
        return [0.0 for _ in dates]
    leg_prices = close[close["instrument"].astype(str).isin(instruments)][["trade_date", "instrument", "close"]].copy()
    entry_prices = leg_prices[leg_prices["trade_date"] == entry_date][["instrument", "close"]].rename(
        columns={"close": "entry_close"}
    )
    leg_prices = leg_prices.merge(entry_prices, on="instrument", how="inner")
    leg_prices["instrument_return"] = leg_prices["close"] / leg_prices["entry_close"] - 1.0
    result: list[float] = []
    for item in dates:
        day = leg_prices[leg_prices["trade_date"] == item]
        if not day.empty:
            # Present-subset mean: names missing that day are already excluded,
            # so a partially-held leg reflects only the surviving names.
            result.append(float(day["instrument_return"].mean()))
        else:
            # Fully-empty day (every held name absent, e.g. delisting/halt): do
            # NOT carry the prior day's leg return, which would hold NAV flat at
            # a stale mark and understate max drawdown (COR-8). Emit NaN so the
            # NAV/drawdown path can drop the unmarkable day.
            result.append(float("nan"))
    return result


def _daily_nav_for_period(
    *,
    dates: list[str],
    long_returns: list[float],
    short_returns: list[float],
    transaction_cost: float,
) -> list[dict[str, object]]:
    if not (len(dates) == len(long_returns) == len(short_returns)):
        raise ValueError("dates, long_returns, and short_returns must have the same length")
    rows: list[dict[str, object]] = []
    for date_label, long_return, short_return in zip(dates, long_returns, short_returns, strict=True):
        gross_return = float(long_return - short_return)
        net_return = gross_return - transaction_cost
        rows.append(
            {
                "date": date_label,
                "gross_nav": 1.0 + gross_return,
                "net_nav": 1.0 + net_return,
                "gross_cumulative_return": gross_return,
                "net_cumulative_return": net_return,
            }
        )
    return rows


def _return_summary(
    returns: np.ndarray,
    holding_days: int,
    *,
    daily_nav: list[dict[str, object]] | None = None,
    exposure_days: int | None = None,
    nav_key: str = "gross_nav",
    sample_role: str = EXTERNAL_OOS_ROLE,
    volatility_returns: np.ndarray | None = None,
) -> dict[str, object]:
    periods = len(returns)
    # Volatility/Sharpe must only use uniform full-holding periods; a partial
    # final period (trading_days_held < holding) would otherwise be treated as a
    # full holding period by the sqrt(252/holding_days) scaling (COR-6).
    vol_returns = returns if volatility_returns is None else volatility_returns
    equity = np.cumprod(1.0 + returns) if periods else np.array([], dtype=float)
    if periods == 1:
        cumulative_return = float(returns[0])
    else:
        cumulative_return = float(equity[-1] - 1.0) if len(equity) else 0.0
    terminal_equity = 1.0 + cumulative_return
    exposure = exposure_days if exposure_days is not None else periods * holding_days
    extrapolated = _annualized_return(cumulative_return, exposure)
    annualized_return = extrapolated if terminal_equity <= 0.0 or exposure >= MIN_ANNUALIZATION_EXPOSURE_DAYS else None
    annualized_status = "available" if annualized_return is not None else "insufficient_sample"
    annualized_warnings = () if exposure >= MIN_ANNUALIZATION_EXPOSURE_DAYS else (INSUFFICIENT_ANNUALIZATION_HISTORY,)
    annualized_volatility = (
        float(np.std(vol_returns, ddof=1) * np.sqrt(252 / holding_days)) if len(vol_returns) > 1 else None
    )
    volatility_warnings = () if annualized_volatility is not None else (INSUFFICIENT_VOLATILITY_OBSERVATIONS,)
    long_short_sharpe = _long_short_sharpe(vol_returns, holding_days)
    sharpe_warnings = () if long_short_sharpe is not None else (INSUFFICIENT_SHARPE_OBSERVATIONS,)
    nav_values = np.array([float(row[nav_key]) for row in daily_nav], dtype=float) if daily_nav else np.array([])
    max_drawdown = _max_drawdown(nav_values) if len(nav_values) else None
    drawdown_warnings = () if max_drawdown is not None else (DAILY_NAV_UNAVAILABLE,)
    warning_codes = tuple(
        dict.fromkeys((*annualized_warnings, *volatility_warnings, *sharpe_warnings, *drawdown_warnings))
    )
    return {
        "cumulative_return": cumulative_return,
        "annualized_return": annualized_return,
        "reportable_annualization": MetricValue(
            value=annualized_return,
            unit="return",
            status=annualized_status,
            observation_count=exposure,
            minimum_required=MIN_ANNUALIZATION_EXPOSURE_DAYS,
            method="geometric_annualization",
            source_series="non_overlapping_period_returns",
            sample_role=sample_role,
            warning_codes=annualized_warnings,
        ),
        "extrapolated_annualization": MetricValue(
            value=extrapolated,
            unit="return",
            status="available" if extrapolated is not None else "invalid",
            observation_count=exposure,
            method="geometric_annualization_extrapolated",
            source_series="non_overlapping_period_returns",
            sample_role=sample_role,
            warning_codes=annualized_warnings,
        ),
        "annualized_volatility": annualized_volatility,
        "long_short_sharpe": long_short_sharpe,
        "max_drawdown": max_drawdown,
        "warning_codes": warning_codes,
        "metrics": {
            "annualized_return": MetricValue(
                value=annualized_return,
                unit="return",
                status=annualized_status,
                observation_count=exposure,
                minimum_required=MIN_ANNUALIZATION_EXPOSURE_DAYS,
                method="geometric_annualization",
                source_series="non_overlapping_period_returns",
                sample_role=sample_role,
                warning_codes=annualized_warnings,
            ),
            "annualized_volatility": MetricValue(
                value=annualized_volatility,
                unit="return_volatility",
                status="available" if annualized_volatility is not None else "insufficient_sample",
                observation_count=periods,
                minimum_required=2,
                method="period_return_std_scaled",
                source_series="non_overlapping_period_returns",
                sample_role=sample_role,
                warning_codes=volatility_warnings,
            ),
            "long_short_sharpe": MetricValue(
                value=long_short_sharpe,
                unit="ratio",
                status="available" if long_short_sharpe is not None else "insufficient_sample",
                observation_count=periods,
                minimum_required=2,
                method="period_return_mean_over_std_scaled",
                source_series="non_overlapping_period_returns",
                sample_role=sample_role,
                warning_codes=sharpe_warnings,
            ),
            "max_drawdown": MetricValue(
                value=max_drawdown,
                unit="return",
                status="available" if max_drawdown is not None else "unavailable_source_series",
                observation_count=len(nav_values),
                method="daily_mark_to_market_nav",
                source_series="daily_nav",
                sample_role=sample_role,
                warning_codes=drawdown_warnings,
            ),
        },
    }


def _annualized_return(cumulative_return: float, exposure_days: int) -> float | None:
    if exposure_days <= 0:
        return None
    terminal_equity = 1.0 + cumulative_return
    if terminal_equity <= 0.0:
        return -1.0
    return float(terminal_equity ** (252 / exposure_days) - 1.0)


def _metric_provenance(metric: MetricValue) -> dict[str, object]:
    return {
        "method": metric.method,
        "source_series": metric.source_series,
        "sample_role": metric.sample_role,
        "observation_count": metric.observation_count,
        "minimum_required": metric.minimum_required,
        "status": metric.status,
        "warning_codes": list(metric.warning_codes),
    }


def _calendar_days(rows: list[dict[str, object]]) -> int:
    if not rows:
        return 0
    start = pd.Timestamp(str(rows[0]["entry_date"]))
    end = pd.Timestamp(str(rows[-1]["exit_date"]))
    return int((end - start).days) + 1


def _max_drawdown(equity: np.ndarray) -> float:
    # Drop unmarkable (NaN) NAV points so a fully-delisted day neither injects a
    # NaN nor holds the peak flat; the surviving marks still drive drawdown (COR-8).
    equity = equity[~np.isnan(equity)] if len(equity) else equity
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


def _long_short_sharpe(returns: np.ndarray, holding_days: int) -> float | None:
    if len(returns) < 2:
        return None
    std = float(np.std(returns, ddof=1))
    if std == 0.0:
        return None
    return float(np.mean(returns) / std * np.sqrt(252 / holding_days))


def _segment_metrics(
    rows: list[dict[str, object]],
    holding_days: int,
    split_specs: tuple[SampleSplitSpec, ...],
    *,
    sample_role: str = EXTERNAL_OOS_ROLE,
) -> tuple[BacktestSegmentMetric, ...]:
    split_rows = _split_rows_by_signal_date(rows, split_specs)
    metrics: list[BacktestSegmentMetric] = []
    for spec, segment in zip(split_specs, split_rows, strict=True):
        gross_returns = np.array([float(row["gross_period_return"]) for row in segment], dtype=float)
        net_returns = np.array([float(row["net_period_return"]) for row in segment], dtype=float)
        gross = _return_summary(gross_returns, holding_days, sample_role=sample_role)
        net = _return_summary(net_returns, holding_days, sample_role=sample_role)
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
                sample_role=sample_role,
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
    holding_days: int,
    exposure_days: int,
    rebalance_rate: float | None,
    turnover_rate: float | None,
    gross_annualized_return: float | None,
    net_annualized_return: float | None,
    segment_metrics: tuple[BacktestSegmentMetric, ...],
) -> tuple[str, ...]:
    warnings: list[str] = []
    if periods < 2:
        warnings.append("too few backtest periods for stable Sharpe or drawdown estimates")
    if 0 < exposure_days < MIN_ANNUALIZATION_EXPOSURE_DAYS:
        warnings.append(
            "short annualization window: annualized return and volatility are highly extrapolated"
        )
    if rebalance_rate is not None and rebalance_rate > 0.8:
        warnings.append("high rebalance rate")
    if turnover_rate is not None and turnover_rate > 1.5:
        warnings.append("high turnover rate")
    if (
        gross_annualized_return is not None
        and net_annualized_return is not None
        and gross_annualized_return > 0
        and net_annualized_return < gross_annualized_return * 0.5
    ):
        warnings.append("net-of-cost performance is highly sensitive to transaction costs")
    if _oos_segment_decay(segment_metrics):
        warnings.append("OOS segment net performance decays versus IS")
    return tuple(dict.fromkeys(warnings))


def _oos_segment_decay(segment_metrics: tuple[BacktestSegmentMetric, ...]) -> bool:
    by_name = {metric.name.upper(): metric for metric in segment_metrics}
    is_metric = by_name.get("IS")
    if is_metric is None or is_metric.periods == 0 or is_metric.net_annualized_return is None or is_metric.net_annualized_return <= 0:
        return False
    oos_metrics = [metric for name, metric in by_name.items() if name.startswith("OOS") and metric.periods > 0]
    return any(
        metric.net_annualized_return is not None
        and metric.net_annualized_return < is_metric.net_annualized_return * 0.5
        for metric in oos_metrics
    )


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

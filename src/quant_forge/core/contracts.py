"""Typed contracts used across the public workbench."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
import re
from typing import Literal


FactorStatus = Literal["draft", "candidate", "active", "inactive", "archived"]
NanPolicy = Literal["drop"]
NeutralizationPolicy = Literal["none"]
MetricStatus = Literal[
    "available",
    "insufficient_sample",
    "not_applicable",
    "unavailable_source_series",
    "invalid",
]
METRICS_SCHEMA_VERSION = "qf.metrics.v2"


@dataclass(frozen=True)
class MetricValue:
    value: float | None
    unit: str
    status: MetricStatus
    observation_count: int
    minimum_required: int | None = None
    method: str = ""
    source_series: str = ""
    sample_role: str = ""
    segment: str = ""
    start_date: str = ""
    end_date: str = ""
    horizon_days: int | None = None
    execution_delay_days: int | None = None
    warning_codes: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class FactorDefinition:
    factor_id: str
    name: str
    formula: str
    status: FactorStatus = "draft"
    description: str = ""
    horizon_days: int = 5
    universe_filters: tuple[str, ...] = field(default_factory=tuple)
    source: str = "user"

    def __post_init__(self) -> None:
        allowed: set[str] = {"draft", "candidate", "active", "inactive", "archived"}
        if self.status not in allowed:
            raise ValueError(f"invalid factor status: {self.status}")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_=-]*", self.factor_id):
            raise ValueError("factor_id must start with a letter and contain only letters, digits, underscores, =, or -")
        if self.horizon_days < 1:
            raise ValueError("horizon_days must be positive")


@dataclass(frozen=True)
class DataValidationResult:
    data_root: Path
    ok: bool
    rows: int
    instruments: int
    date_count: int
    missing_columns: tuple[str, ...] = field(default_factory=tuple)
    panel_path: Path | None = None
    start_date: str = ""
    end_date: str = ""
    optional_columns: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class SampleSplitSpec:
    name: str
    fraction: float
    score_weight: float

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("sample split name is required")
        if self.fraction <= 0:
            raise ValueError("sample split fraction must be positive")
        if self.score_weight < 0:
            raise ValueError("sample split score_weight must be non-negative")


@dataclass(frozen=True)
class SimulationProfile:
    market: str = "cn_a"
    instrument_type: str = "equity"
    universe: str = "local_panel"
    execution_delay_days: int = 1
    top_quantile: float = 0.3
    nan_policy: NanPolicy = "drop"
    neutralization: NeutralizationPolicy = "none"
    truncation: str | None = None
    decay_days: int = 0
    test_period_start: str | None = None
    test_period_end: str | None = None

    def __post_init__(self) -> None:
        if not self.market.strip():
            raise ValueError("simulation market is required")
        if not self.instrument_type.strip():
            raise ValueError("simulation instrument_type is required")
        if not self.universe.strip():
            raise ValueError("simulation universe is required")
        if self.execution_delay_days < 1:
            raise ValueError("execution_delay_days must be at least 1")
        if not 0.0 < self.top_quantile <= 0.5:
            raise ValueError("top_quantile must be in (0, 0.5]")
        if self.nan_policy != "drop":
            raise ValueError("only nan_policy='drop' is supported")
        if self.neutralization != "none":
            raise ValueError("only neutralization='none' is supported")
        if self.truncation is not None:
            raise ValueError("truncation is not supported in the first public profile")
        if self.decay_days < 0:
            raise ValueError("decay_days must be non-negative")
        start = _optional_iso_date(self.test_period_start, "test_period_start")
        end = _optional_iso_date(self.test_period_end, "test_period_end")
        if start and end and start > end:
            raise ValueError("test_period_start must be <= test_period_end")


@dataclass(frozen=True)
class EvaluationSplitMetric:
    name: str
    start_date: str
    end_date: str
    date_count: int
    observations: int
    coverage: float
    rank_ic_mean: float
    rank_ic_std: float
    rank_icir: float
    ic_days: int
    rank_ic_t_stat: float | None = 0.0
    score_weight: float = 0.0
    sample_role: str = "research_evaluation"
    rank_ic_t_stat_naive: float | None = None
    rank_ic_t_stat_hac: float | None = None
    rank_ic_hac_standard_error: float | None = None
    rank_ic_hac_lag: int = 0
    rank_ic_p_value_hac: float | None = None
    warning_codes: tuple[str, ...] = field(default_factory=tuple)
    metrics: dict[str, MetricValue] = field(default_factory=dict)


@dataclass(frozen=True)
class HorizonEvaluationMetric:
    horizon_days: int
    observations: int
    coverage: float
    rank_ic_mean: float
    rank_ic_std: float
    rank_icir: float
    ic_days: int
    rank_ic_t_stat: float | None = 0.0
    split_metrics: tuple[EvaluationSplitMetric, ...] = field(default_factory=tuple)
    sample_role: str = "research_evaluation"
    rank_ic_t_stat_naive: float | None = None
    rank_ic_t_stat_hac: float | None = None
    rank_ic_hac_standard_error: float | None = None
    rank_ic_hac_lag: int = 0
    rank_ic_p_value_hac: float | None = None
    ic_series: tuple[dict[str, object], ...] = field(default_factory=tuple)
    coverage_lineage: dict[str, float | int] = field(default_factory=dict)
    boundary_diagnostics: dict[str, object] = field(default_factory=dict)
    metric_provenance: dict[str, dict[str, object]] = field(default_factory=dict)
    warning_codes: tuple[str, ...] = field(default_factory=tuple)
    metrics: dict[str, MetricValue] = field(default_factory=dict)


@dataclass(frozen=True)
class EvaluationResult:
    factor_id: str
    observations: int
    coverage: float
    rank_ic_mean: float
    rank_ic_std: float
    rank_icir: float
    ic_days: int
    artifact_path: Path
    rank_ic_t_stat: float = 0.0
    split_metrics: tuple[EvaluationSplitMetric, ...] = field(default_factory=tuple)
    horizon_metrics: tuple[HorizonEvaluationMetric, ...] = field(default_factory=tuple)
    simulation_profile: SimulationProfile = field(default_factory=SimulationProfile)
    score_source: str = "computed"
    score_cached_rows: int = 0
    score_computed_rows: int = 0
    factor_values_path: Path | None = None
    factor_values_write_path: Path | None = None
    score_compute_mode: str = ""
    score_compute_reason: str = ""
    score_missing_rows: int = 0
    score_required_rows: int = 0
    score_missing_ratio: float = 0.0
    score_lookback_rows: int = 0
    score_context_rows: int = 0
    warnings: tuple[str, ...] = field(default_factory=tuple)
    schema_version: str = METRICS_SCHEMA_VERSION
    sample_role: str = "research_evaluation"
    rank_ic_t_stat_naive: float | None = None
    rank_ic_t_stat_hac: float | None = None
    rank_ic_hac_standard_error: float | None = None
    rank_ic_hac_lag: int = 0
    rank_ic_p_value_hac: float | None = None
    ic_series: tuple[dict[str, object], ...] = field(default_factory=tuple)
    coverage_lineage: dict[str, float | int] = field(default_factory=dict)
    boundary_diagnostics: dict[str, object] = field(default_factory=dict)
    metric_provenance: dict[str, dict[str, object]] = field(default_factory=dict)
    warning_codes: tuple[str, ...] = field(default_factory=tuple)
    metrics: dict[str, MetricValue] = field(default_factory=dict)


@dataclass(frozen=True)
class BacktestGroupMetric:
    group: str
    mean_return: float
    periods: int


@dataclass(frozen=True)
class TransactionCostModel:
    commission_bps: float = 0.0
    slippage_bps: float = 0.0
    short_borrow_bps_annual: float = 0.0

    def __post_init__(self) -> None:
        if self.commission_bps < 0:
            raise ValueError("commission_bps must be non-negative")
        if self.slippage_bps < 0:
            raise ValueError("slippage_bps must be non-negative")
        if self.short_borrow_bps_annual < 0:
            raise ValueError("short_borrow_bps_annual must be non-negative")


@dataclass(frozen=True)
class BacktestSegmentMetric:
    name: str
    start_date: str
    end_date: str
    periods: int
    gross_cumulative_return: float
    gross_annualized_return: float | None
    gross_long_short_sharpe: float | None
    gross_max_drawdown: float | None
    net_cumulative_return: float
    net_annualized_return: float | None
    net_long_short_sharpe: float | None
    net_max_drawdown: float | None
    sample_role: str = "external_oos_backtest"
    metrics: dict[str, MetricValue] = field(default_factory=dict)
    warning_codes: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class BacktestResult:
    factor_id: str
    periods: int
    holding_days: int
    cumulative_return: float
    annualized_return: float | None
    annualized_volatility: float | None
    max_drawdown: float | None
    artifact_path: Path
    long_short_sharpe: float | None = 0.0
    gross_cumulative_return: float = 0.0
    gross_annualized_return: float | None = 0.0
    gross_annualized_volatility: float | None = 0.0
    gross_long_short_sharpe: float | None = 0.0
    gross_max_drawdown: float | None = 0.0
    rebalance_rate: float | None = 0.0
    turnover_rate: float | None = 0.0
    net_cumulative_return: float = 0.0
    net_annualized_return: float | None = 0.0
    net_annualized_volatility: float | None = 0.0
    net_long_short_sharpe: float | None = 0.0
    net_max_drawdown: float | None = 0.0
    top_quantile: float = 0.3
    transaction_costs: TransactionCostModel = field(default_factory=TransactionCostModel)
    simulation_profile: SimulationProfile = field(default_factory=SimulationProfile)
    group_returns: tuple[BacktestGroupMetric, ...] = field(default_factory=tuple)
    segment_metrics: tuple[BacktestSegmentMetric, ...] = field(default_factory=tuple)
    warnings: tuple[str, ...] = field(default_factory=tuple)
    assumptions: tuple[str, ...] = field(default_factory=tuple)
    score_source: str = "computed"
    score_cached_rows: int = 0
    score_computed_rows: int = 0
    factor_values_path: Path | None = None
    factor_values_write_path: Path | None = None
    score_compute_mode: str = ""
    score_compute_reason: str = ""
    score_missing_rows: int = 0
    score_required_rows: int = 0
    score_missing_ratio: float = 0.0
    score_lookback_rows: int = 0
    score_context_rows: int = 0
    schema_version: str = METRICS_SCHEMA_VERSION
    sample_role: str = "external_oos_backtest"
    return_series_kind: str = "non_overlapping_horizon_return"
    completed_periods: int = 0
    partial_periods: int = 0
    lost_positions: int = 0
    exposure_days: int = 0
    calendar_days: int = 0
    reportable_annualization: MetricValue | None = None
    extrapolated_annualization: MetricValue | None = None
    daily_nav: tuple[dict[str, object], ...] = field(default_factory=tuple)
    initial_build_turnover: float | None = None
    rebalance_turnover_mean: float | None = None
    rebalance_turnover_observation_count: int = 0
    replacement_rate_mean: float | None = None
    replacement_rate_observation_count: int = 0
    cost_reconciliation: dict[str, float] = field(default_factory=dict)
    metric_provenance: dict[str, dict[str, object]] = field(default_factory=dict)
    warning_codes: tuple[str, ...] = field(default_factory=tuple)
    metrics: dict[str, MetricValue] = field(default_factory=dict)
    benchmark: dict[str, object] = field(default_factory=dict)
    benchmark_cumulative_return: float | None = None
    arithmetic_excess_return: float | None = None
    relative_wealth_excess_return: float | None = None
    tracking_error: float | None = None
    information_ratio: float | None = None
    daily_ledger: tuple[dict[str, object], ...] = field(default_factory=tuple)
    resolved_schedule: tuple[dict[str, object], ...] = field(default_factory=tuple)
    rebalance_ledger: tuple[dict[str, object], ...] = field(default_factory=tuple)
    request_snapshot: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class FactorAssessmentBundle:
    factor_id: str
    role: str
    evaluation: EvaluationResult
    selection_backtest: BacktestResult
    external_oos_backtest: BacktestResult
    selection_score: float = 0.0
    split_weighted_icir: float = 0.0
    gate_passed: bool | None = None
    gate_reasons: tuple[str, ...] = field(default_factory=tuple)
    round_index: int = 0
    parent_seed_factor_id: str = ""
    selection_basis: str = "research_evaluation_and_in_sample_backtest"
    audit_basis: str = "external_oos_backtest_only"


def _optional_iso_date(value: str | None, label: str) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO date") from exc

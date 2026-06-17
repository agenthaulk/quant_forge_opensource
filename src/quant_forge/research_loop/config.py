"""Configuration loader for the public research loop."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from quant_forge.config import ResearchSettings, simulation_profile_from_mapping
from quant_forge.core.contracts import SampleSplitSpec, SimulationProfile, TransactionCostModel
from quant_forge.evaluation.service import DEFAULT_HORIZON_DAYS, DEFAULT_SAMPLE_SPLITS
from quant_forge.research_loop.service import (
    ResearchDeduplicationConfig,
    ResearchGate,
    ResearchObjectiveWeights,
    objective_weights_for,
)

DEFAULT_RD_CONFIG_PATH = Path("configs/rd.yaml")


@dataclass(frozen=True)
class ResearchWeightProfile:
    objective: str
    weights: ResearchObjectiveWeights


def default_research_weight_profiles() -> tuple[ResearchWeightProfile, ...]:
    return (
        ResearchWeightProfile("rank_ic", objective_weights_for("rank_ic")),
        ResearchWeightProfile("rank_icir", objective_weights_for("rank_icir")),
        ResearchWeightProfile("annualized_return", objective_weights_for("annualized_return")),
    )


@dataclass(frozen=True)
class ResearchParameterSearchConfig:
    enabled: bool = False
    method: str = "successive_halving"
    max_profile_variants: int = 6
    keep_ratio: float = 0.34
    min_survivors: int = 2
    quick_horizon_days_matrix: tuple[int, ...] = (5, 21)
    quick_sample_splits: tuple[SampleSplitSpec, ...] = (SampleSplitSpec(name="IS", fraction=1.0, score_weight=1.0),)
    top_quantile: tuple[float, ...] = ()
    decay_days: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if self.method not in {"full_grid", "successive_halving"}:
            raise ValueError("parameter_search.method must be full_grid or successive_halving")
        if self.max_profile_variants < 1:
            raise ValueError("parameter_search.max_profile_variants must be positive")
        if not 0.0 < self.keep_ratio <= 1.0:
            raise ValueError("parameter_search.keep_ratio must be in (0, 1]")
        if self.min_survivors < 1:
            raise ValueError("parameter_search.min_survivors must be positive")
        if not self.quick_horizon_days_matrix:
            raise ValueError("parameter_search.quick_horizon_days_matrix must not be empty")
        if any(value < 1 for value in self.quick_horizon_days_matrix):
            raise ValueError("parameter_search.quick_horizon_days_matrix values must be positive")
        _validate_sample_splits(self.quick_sample_splits)
        if any(not 0.0 < value <= 0.5 for value in self.top_quantile):
            raise ValueError("parameter_search.top_quantile values must be in (0, 0.5]")
        if any(value < 0 for value in self.decay_days):
            raise ValueError("parameter_search.decay_days values must be non-negative")

    def profiles(self, base: SimulationProfile) -> tuple[SimulationProfile, ...]:
        if not self.enabled:
            return (base,)
        top_quantiles = self.top_quantile or (base.top_quantile,)
        decay_values = self.decay_days or (base.decay_days,)
        profiles: list[SimulationProfile] = []
        for top_quantile in top_quantiles:
            for decay_days in decay_values:
                profile = SimulationProfile(
                    market=base.market,
                    instrument_type=base.instrument_type,
                    universe=base.universe,
                    execution_delay_days=base.execution_delay_days,
                    top_quantile=top_quantile,
                    nan_policy=base.nan_policy,
                    neutralization=base.neutralization,
                    truncation=base.truncation,
                    decay_days=decay_days,
                    test_period_start=base.test_period_start,
                    test_period_end=base.test_period_end,
                )
                if profile not in profiles:
                    profiles.append(profile)
                if len(profiles) >= self.max_profile_variants:
                    return tuple(profiles)
        return tuple(profiles)


@dataclass(frozen=True)
class ResearchLLMConfig:
    hypothesis_mode: str = "local"
    review_mode: str = "local"
    max_formula_repair_attempts: int = 2

    def __post_init__(self) -> None:
        for name, value in (
            ("hypothesis_mode", self.hypothesis_mode),
            ("review_mode", self.review_mode),
        ):
            if _canonical_generation_mode(value) not in {"llm", "local"}:
                raise ValueError(f"RD llm.{name} must be llm or local")
        if not 0 <= self.max_formula_repair_attempts <= 3:
            raise ValueError("RD llm.max_formula_repair_attempts must be between 0 and 3")

    @property
    def uses_llm(self) -> bool:
        return self.research_uses_llm

    @property
    def research_uses_llm(self) -> bool:
        return any(
            _canonical_generation_mode(value) == "llm"
            for value in (self.hypothesis_mode, self.review_mode)
        )


@dataclass(frozen=True)
class ResearchLoopConfig:
    objective: str = "balanced"
    default_max_candidates: int = 3
    default_interval_days: int = 1
    allowed_interval_days: tuple[int, ...] = (1, 5, 15, 30)
    simulation_profile: SimulationProfile = field(default_factory=SimulationProfile)
    evaluation_simulation_profile: SimulationProfile | None = None
    backtest_simulation_profile: SimulationProfile | None = None
    horizon_days_matrix: tuple[int, ...] = DEFAULT_HORIZON_DAYS
    sample_splits: tuple[SampleSplitSpec, ...] = DEFAULT_SAMPLE_SPLITS
    gate: ResearchGate = field(default_factory=ResearchGate)
    weights: ResearchObjectiveWeights = field(default_factory=ResearchObjectiveWeights)
    weight_profiles: tuple[ResearchWeightProfile, ...] = field(default_factory=default_research_weight_profiles)
    parameter_search: ResearchParameterSearchConfig = field(default_factory=ResearchParameterSearchConfig)
    transaction_costs: TransactionCostModel = field(default_factory=TransactionCostModel)
    llm: ResearchLLMConfig = field(default_factory=ResearchLLMConfig)
    deduplication: ResearchDeduplicationConfig = field(default_factory=ResearchDeduplicationConfig)

    def __post_init__(self) -> None:
        if self.default_max_candidates < 1 or self.default_max_candidates > 10:
            raise ValueError("default_max_candidates must be between 1 and 10")
        if self.default_interval_days not in self.allowed_interval_days:
            raise ValueError("default_interval_days must be included in allowed_interval_days")
        if not self.horizon_days_matrix:
            raise ValueError("horizon_days_matrix must not be empty")
        if any(horizon < 1 for horizon in self.horizon_days_matrix):
            raise ValueError("horizon_days_matrix values must be positive")
        profile_names = [_canonical_objective(profile.objective) for profile in self.weight_profiles]
        if len(set(profile_names)) != len(profile_names):
            raise ValueError("RD weight profile objectives must be unique")

    @property
    def top_quantile(self) -> float:
        return self.simulation_profile.top_quantile

    @property
    def simulation_profiles(self) -> tuple[SimulationProfile, ...]:
        return self.parameter_search.profiles(self.simulation_profile)

    @property
    def evaluation_profile(self) -> SimulationProfile:
        return self.evaluation_simulation_profile or self.simulation_profile

    @property
    def backtest_profile(self) -> SimulationProfile:
        return self.backtest_simulation_profile or self.simulation_profile


def default_research_loop_config(
    settings: ResearchSettings | None = None, simulation_profile: SimulationProfile | None = None
) -> ResearchLoopConfig:
    if simulation_profile is not None:
        return ResearchLoopConfig(simulation_profile=simulation_profile)
    if settings is None:
        return ResearchLoopConfig()
    return ResearchLoopConfig(simulation_profile=SimulationProfile(top_quantile=settings.default_top_quantile))


def load_research_loop_config(
    config_path: Path | None = None,
    settings: ResearchSettings | None = None,
    simulation_profile: SimulationProfile | None = None,
) -> ResearchLoopConfig:
    base = default_research_loop_config(settings, simulation_profile)
    if config_path is None:
        return base
    path = config_path.expanduser()
    if not path.exists():
        raise FileNotFoundError(f"RD config file does not exist: {path}")
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(loaded, dict):
        raise ValueError("RD config file must contain a mapping")
    objective = str(loaded.get("objective", base.objective))
    default_weights = (
        base.weights
        if "weights" in loaded or _canonical_objective(objective) == _canonical_objective(base.objective)
        else objective_weights_for(objective)
    )
    simulation_section_raw = loaded.get("simulation")
    if simulation_section_raw is not None and not isinstance(simulation_section_raw, dict):
        raise ValueError("RD config simulation must be a mapping")
    simulation_section = dict(simulation_section_raw or {})
    if "top_quantile" in loaded and "top_quantile" not in simulation_section:
        simulation_section["top_quantile"] = loaded["top_quantile"]
    primary_profile = simulation_profile_from_mapping(simulation_section, base.simulation_profile)
    return ResearchLoopConfig(
        objective=objective,
        default_max_candidates=int(loaded.get("default_max_candidates", base.default_max_candidates)),
        default_interval_days=int(loaded.get("default_interval_days", base.default_interval_days)),
        allowed_interval_days=tuple(
            int(item) for item in loaded.get("allowed_interval_days", base.allowed_interval_days)
        ),
        simulation_profile=primary_profile,
        evaluation_simulation_profile=_load_role_simulation_profile(loaded, "evaluation", primary_profile),
        backtest_simulation_profile=_load_role_simulation_profile(loaded, "backtest", primary_profile),
        horizon_days_matrix=tuple(int(item) for item in loaded.get("horizon_days_matrix", base.horizon_days_matrix)),
        sample_splits=_load_sample_splits(loaded.get("sample_splits"), base.sample_splits),
        gate=ResearchGate(
            min_ic_days=int(_nested(loaded, "gate", "min_ic_days", base.gate.min_ic_days)),
            min_coverage=float(_nested(loaded, "gate", "min_coverage", base.gate.min_coverage)),
            min_score=float(_nested(loaded, "gate", "min_score", base.gate.min_score)),
            min_backtest_periods=int(
                _nested(loaded, "gate", "min_backtest_periods", base.gate.min_backtest_periods)
            ),
            min_oos_net_annualized_return=_optional_float(
                _nested(
                    loaded,
                    "gate",
                    "min_oos_net_annualized_return",
                    base.gate.min_oos_net_annualized_return,
                )
            ),
            max_rebalance_rate=_optional_float(
                _nested(
                    loaded,
                    "gate",
                    "max_rebalance_rate",
                    _nested(loaded, "gate", "max_component_replacement", base.gate.max_rebalance_rate),
                )
            ),
            max_turnover_rate=_optional_float(
                _nested(
                    loaded,
                    "gate",
                    "max_turnover_rate",
                    _nested(loaded, "gate", "max_single_side_turnover", base.gate.max_turnover_rate),
                )
            ),
            min_net_return_retention=_optional_float(
                _nested(loaded, "gate", "min_net_return_retention", base.gate.min_net_return_retention)
            ),
            max_oos_net_return_decay=_optional_float(
                _nested(loaded, "gate", "max_oos_net_return_decay", base.gate.max_oos_net_return_decay)
            ),
        ),
        weights=_load_weights(loaded.get("weights"), default_weights),
        weight_profiles=_load_weight_profiles(loaded.get("weight_profiles"), base.weight_profiles),
        parameter_search=_load_parameter_search(loaded.get("parameter_search"), base.parameter_search),
        transaction_costs=_load_transaction_costs(loaded.get("transaction_costs"), base.transaction_costs),
        llm=_load_rd_llm_config(loaded.get("llm"), base.llm),
        deduplication=_load_deduplication(loaded.get("deduplication"), base.deduplication),
    )


def weights_for_objective(config: ResearchLoopConfig, objective: str) -> ResearchObjectiveWeights:
    requested = _canonical_objective(objective)
    if requested == _canonical_objective(config.objective):
        return config.weights
    for profile in config.weight_profiles:
        if _canonical_objective(profile.objective) == requested:
            return profile.weights
    raise ValueError(f"RD config missing weight profile for objective: {objective}")


def _nested(raw: dict[str, Any], section: str, key: str, default: Any) -> Any:
    section_value = raw.get(section, {})
    if section_value is None:
        return default
    if not isinstance(section_value, dict):
        raise ValueError(f"RD config section must be a mapping: {section}")
    return section_value.get(key, default)


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def _load_role_simulation_profile(raw: dict[str, Any], section: str, base: SimulationProfile) -> SimulationProfile:
    section_value = raw.get(section)
    if section_value is None:
        return base
    if not isinstance(section_value, dict):
        raise ValueError(f"RD config {section} section must be a mapping")
    simulation_value = section_value.get("simulation", section_value)
    if simulation_value is None:
        return base
    if not isinstance(simulation_value, dict):
        raise ValueError(f"RD config {section}.simulation must be a mapping")
    return simulation_profile_from_mapping(simulation_value, base)


def _load_sample_splits(raw: Any, default: tuple[SampleSplitSpec, ...]) -> tuple[SampleSplitSpec, ...]:
    if raw is None:
        return default
    if not isinstance(raw, list):
        raise ValueError("RD config sample_splits must be a list")
    splits: list[SampleSplitSpec] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("each sample_splits item must be a mapping")
        splits.append(
            SampleSplitSpec(
                name=str(item["name"]),
                fraction=float(item["fraction"]),
                score_weight=float(item.get("score_weight", item["fraction"])),
            )
        )
    return tuple(splits)


def _load_parameter_search(raw: Any, default: ResearchParameterSearchConfig) -> ResearchParameterSearchConfig:
    if raw is None:
        return default
    if not isinstance(raw, dict):
        raise ValueError("RD config parameter_search must be a mapping")
    return ResearchParameterSearchConfig(
        enabled=bool(raw.get("enabled", default.enabled)),
        method=str(raw.get("method", default.method)),
        max_profile_variants=int(raw.get("max_profile_variants", default.max_profile_variants)),
        keep_ratio=float(raw.get("keep_ratio", default.keep_ratio)),
        min_survivors=int(raw.get("min_survivors", default.min_survivors)),
        quick_horizon_days_matrix=tuple(
            int(item) for item in raw.get("quick_horizon_days_matrix", default.quick_horizon_days_matrix)
        ),
        quick_sample_splits=_load_sample_splits(raw.get("quick_sample_splits"), default.quick_sample_splits),
        top_quantile=tuple(float(item) for item in raw.get("top_quantile", default.top_quantile)),
        decay_days=tuple(int(item) for item in raw.get("decay_days", default.decay_days)),
    )


def _load_transaction_costs(raw: Any, default: TransactionCostModel) -> TransactionCostModel:
    if raw is None:
        return default
    if not isinstance(raw, dict):
        raise ValueError("RD config transaction_costs must be a mapping")
    return TransactionCostModel(
        commission_bps=float(raw.get("commission_bps", default.commission_bps)),
        slippage_bps=float(raw.get("slippage_bps", default.slippage_bps)),
        short_borrow_bps_annual=float(raw.get("short_borrow_bps_annual", default.short_borrow_bps_annual)),
    )


def _load_rd_llm_config(raw: Any, default: ResearchLLMConfig) -> ResearchLLMConfig:
    if raw is None:
        return default
    if not isinstance(raw, dict):
        raise ValueError("RD config llm section must be a mapping")
    if "campaign_mode" in raw:
        raise ValueError("RD config llm.campaign_mode is not supported in the public research workbench")
    return ResearchLLMConfig(
        hypothesis_mode=_canonical_generation_mode(raw.get("hypothesis_mode", default.hypothesis_mode)),
        review_mode=_canonical_generation_mode(raw.get("review_mode", default.review_mode)),
        max_formula_repair_attempts=int(
            raw.get("max_formula_repair_attempts", default.max_formula_repair_attempts)
        ),
    )


def _load_deduplication(raw: Any, default: ResearchDeduplicationConfig) -> ResearchDeduplicationConfig:
    if raw is None:
        return default
    if not isinstance(raw, dict):
        raise ValueError("RD config deduplication must be a mapping")
    return ResearchDeduplicationConfig(
        enabled=bool(raw.get("enabled", default.enabled)),
        formula_fingerprint=bool(raw.get("formula_fingerprint", default.formula_fingerprint)),
        result_signature=bool(raw.get("result_signature", default.result_signature)),
        candidate_diversity=bool(raw.get("candidate_diversity", default.candidate_diversity)),
        result_precision=int(raw.get("result_precision", default.result_precision)),
        recent_trace_limit=int(raw.get("recent_trace_limit", default.recent_trace_limit)),
        max_same_shape_per_run=int(raw.get("max_same_shape_per_run", default.max_same_shape_per_run)),
    )


def _canonical_generation_mode(value: Any) -> str:
    normalized = str(value).strip().lower()
    if normalized in {"deterministic", "rule", "local_rule", "local"}:
        return "local"
    return normalized


def _validate_sample_splits(split_specs: tuple[SampleSplitSpec, ...]) -> None:
    if not split_specs:
        raise ValueError("parameter_search.quick_sample_splits must not be empty")
    names = [split.name for split in split_specs]
    if len(set(names)) != len(names):
        raise ValueError("parameter_search.quick_sample_splits names must be unique")
    if sum(split.fraction for split in split_specs) <= 0:
        raise ValueError("parameter_search.quick_sample_splits fractions must sum to a positive value")
    if sum(split.score_weight for split in split_specs) <= 0:
        raise ValueError("parameter_search.quick_sample_splits score weights must sum to a positive value")


def _load_weight_profiles(
    raw: Any, default: tuple[ResearchWeightProfile, ...]
) -> tuple[ResearchWeightProfile, ...]:
    if raw is None:
        return default
    if not isinstance(raw, dict):
        raise ValueError("RD config weight_profiles must be a mapping")
    profiles: list[ResearchWeightProfile] = []
    for objective, payload in raw.items():
        profiles.append(
            ResearchWeightProfile(str(objective), _load_weights(payload, objective_weights_for(str(objective))))
        )
    return tuple(profiles)


def _load_weights(raw: Any, default: ResearchObjectiveWeights) -> ResearchObjectiveWeights:
    if raw is None:
        return default
    if not isinstance(raw, dict):
        raise ValueError("RD config weights must be a mapping")
    return ResearchObjectiveWeights(
        weighted_split_icir=float(raw.get("weighted_split_icir", default.weighted_split_icir)),
        rank_ic_mean=float(raw.get("rank_ic_mean", default.rank_ic_mean)),
        rank_icir=float(raw.get("rank_icir", default.rank_icir)),
        annualized_return=float(raw.get("annualized_return", default.annualized_return)),
        max_drawdown=float(raw.get("max_drawdown", default.max_drawdown)),
    )


def _canonical_objective(objective: str) -> str:
    normalized = objective.strip().lower()
    aliases = {
        "ic": "rank_ic",
        "rank_ic": "rank_ic",
        "icir": "rank_icir",
        "rank_icir": "rank_icir",
        "return": "annualized_return",
        "backtest_return": "annualized_return",
        "annualized_return": "annualized_return",
        "balanced": "balanced",
    }
    if normalized not in aliases:
        raise ValueError("objective must be one of: rank_ic, rank_icir, annualized_return, balanced")
    return aliases[normalized]

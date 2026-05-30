from __future__ import annotations

from pathlib import Path

import pytest

from quant_forge.config import load_config, simulation_profile_from_mapping
from quant_forge.core.contracts import FactorDefinition, SimulationProfile, TransactionCostModel
from quant_forge.research_loop.config import load_research_loop_config, weights_for_objective


def test_default_config_uses_relative_paths() -> None:
    config = load_config(Path("configs/default.yaml"))
    assert not config.paths.data_root.is_absolute()
    assert not config.paths.factor_root.is_absolute()
    assert not config.paths.artifact_root.is_absolute()
    assert config.web.host == "127.0.0.1"
    assert config.simulation.decay_days == 0
    assert config.simulation.top_quantile == 0.3
    assert config.llm.provider == "deepseek"
    assert config.llm.select_provider("deepseek").api_key_env == "DEEPSEEK_API_KEY"
    assert {option["provider"] for option in config.llm.public_provider_options()} >= {
        "openai",
        "glm",
        "deepseek",
        "minimax",
        "claude",
    }


def test_config_workspace_resolves_paths(tmp_path: Path) -> None:
    config = load_config(Path("configs/default.yaml"), workspace=tmp_path)
    assert config.paths.data_root == tmp_path / "data"
    assert config.paths.factor_root == tmp_path / "factor_root"


def test_rd_config_loads_defaults_and_overrides(tmp_path: Path) -> None:
    default_config = load_research_loop_config(Path("configs/rd.yaml"))
    assert default_config.objective == "balanced"
    assert default_config.default_interval_days == 1
    assert default_config.weights.weighted_split_icir == 0.4
    assert default_config.weights.rank_ic_mean == 0.25
    assert default_config.simulation_profile.decay_days == 0
    assert default_config.simulation_profile.top_quantile == 0.3
    assert default_config.transaction_costs.commission_bps == 0.0
    assert default_config.transaction_costs.slippage_bps == 0.0
    assert default_config.gate.max_turnover_rate is None
    assert default_config.simulation_profiles == (default_config.simulation_profile,)
    assert default_config.horizon_days_matrix == (5, 10, 21, 63)
    assert [split.name for split in default_config.sample_splits] == ["IS", "OOS1", "OOS2"]
    assert weights_for_objective(default_config, "rank_icir").weighted_split_icir == 0.5

    rd_path = tmp_path / "rd.yaml"
    rd_path.write_text(
        """objective: rank_icir
default_max_candidates: 2
default_interval_days: 5
allowed_interval_days: [1, 5]
simulation:
  top_quantile: 0.2
  decay_days: 3
horizon_days_matrix: [5, 21]
sample_splits:
  - name: IS
    fraction: 0.5
    score_weight: 0.5
  - name: OOS
    fraction: 0.5
    score_weight: 0.5
gate:
  min_ic_days: 3
  min_oos_net_annualized_return: -0.05
  max_rebalance_rate: 0.9
  max_turnover_rate: 1.5
  min_net_return_retention: 0.5
  max_oos_net_return_decay: 0.25
transaction_costs:
  commission_bps: 3.0
  slippage_bps: 5.0
  short_borrow_bps_annual: 120.0
weights:
  weighted_split_icir: 0.5
  rank_icir: 0.6
weight_profiles:
  rank_ic:
    weighted_split_icir: 0.9
    rank_ic_mean: 0.0
    rank_icir: 0.1
    annualized_return: 0.0
    max_drawdown: 0.0
parameter_search:
  enabled: true
  max_profile_variants: 4
  top_quantile: [0.2, 0.3]
  decay_days: [0, 3]
""",
        encoding="utf-8",
    )
    custom_config = load_research_loop_config(rd_path)

    assert custom_config.objective == "rank_icir"
    assert custom_config.default_max_candidates == 2
    assert custom_config.default_interval_days == 5
    assert custom_config.allowed_interval_days == (1, 5)
    assert custom_config.simulation_profile.top_quantile == 0.2
    assert custom_config.simulation_profile.decay_days == 3
    assert custom_config.parameter_search.method == "successive_halving"
    assert custom_config.parameter_search.keep_ratio == 0.34
    assert custom_config.parameter_search.min_survivors == 2
    assert len(custom_config.simulation_profiles) == 4
    assert {profile.decay_days for profile in custom_config.simulation_profiles} == {0, 3}
    assert {profile.top_quantile for profile in custom_config.simulation_profiles} == {0.2, 0.3}
    assert custom_config.horizon_days_matrix == (5, 21)
    assert [split.name for split in custom_config.sample_splits] == ["IS", "OOS"]
    assert custom_config.gate.min_ic_days == 3
    assert custom_config.gate.min_oos_net_annualized_return == -0.05
    assert custom_config.gate.max_rebalance_rate == 0.9
    assert custom_config.gate.max_turnover_rate == 1.5
    assert custom_config.gate.min_net_return_retention == 0.5
    assert custom_config.gate.max_oos_net_return_decay == 0.25
    assert custom_config.transaction_costs.commission_bps == 3.0
    assert custom_config.transaction_costs.slippage_bps == 5.0
    assert custom_config.transaction_costs.short_borrow_bps_annual == 120.0
    assert custom_config.weights.weighted_split_icir == 0.5
    assert custom_config.weights.rank_icir == 0.6
    assert weights_for_objective(custom_config, "rank_icir").weighted_split_icir == 0.5
    assert weights_for_objective(custom_config, "rank_ic").weighted_split_icir == 0.9

    legacy_path = tmp_path / "rd-legacy.yaml"
    legacy_path.write_text(
        """gate:
  max_component_replacement: 0.7
  max_single_side_turnover: 1.2
""",
        encoding="utf-8",
    )
    legacy_config = load_research_loop_config(legacy_path)
    assert legacy_config.gate.max_rebalance_rate == 0.7
    assert legacy_config.gate.max_turnover_rate == 1.2


def test_factor_status_validation() -> None:
    with pytest.raises(ValueError, match="invalid factor status"):
        FactorDefinition(
            factor_id="FTR_BAD", name="bad", formula="rank(close)", status="unknown"  # type: ignore[arg-type]
        )


def test_simulation_profile_validation() -> None:
    assert simulation_profile_from_mapping({"decay_days": 2}, SimulationProfile()).decay_days == 2
    with pytest.raises(ValueError, match="only neutralization='none'"):
        SimulationProfile(neutralization="industry")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="truncation is not supported"):
        SimulationProfile(truncation="winsorize")
    with pytest.raises(ValueError, match="commission_bps must be non-negative"):
        TransactionCostModel(commission_bps=-1.0)
    with pytest.raises(ValueError, match="slippage_bps must be non-negative"):
        TransactionCostModel(slippage_bps=-1.0)


def test_config_reports_missing_required_llm_provider_fields(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
llm:
  provider: deepseek
  providers:
    deepseek:
      model: deepseek-chat
      api_key_env: DEEPSEEK_API_KEY
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=r"llm\.providers\.deepseek\.base_url"):
        load_config(config_path)


def test_config_reports_empty_path_settings(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
paths:
  data_root: ""
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=r"paths\.data_root is required"):
        load_config(config_path)

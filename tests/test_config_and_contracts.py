from __future__ import annotations

from pathlib import Path
import subprocess

import pandas as pd
import pytest

from quant_forge.config import (
    LLMProviderSettings,
    LLMSettings,
    QuantForgeConfig,
    load_config,
    simulation_profile_from_mapping,
    validate_any_llm_runtime,
    validate_llm_runtime,
)
from quant_forge.core.contracts import FactorDefinition, SimulationProfile, TransactionCostModel
from quant_forge.data.local import LocalPanelDataProvider, create_demo_workspace
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
    assert {option["provider"] for option in config.llm.public_provider_options()} == {"deepseek"}


def test_config_workspace_resolves_paths(tmp_path: Path) -> None:
    config = load_config(Path("configs/default.yaml"), workspace=tmp_path)
    assert config.paths.data_root == tmp_path / "data"
    assert config.paths.factor_root == tmp_path / "factor_root"
    assert config.paths.factor_values_root is None
    assert config.paths.factor_values_overlay_root is None


def test_config_resolves_optional_factor_value_paths(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
paths:
  factor_values_root: factor_values
  factor_values_overlay_root: factor_values_overlay
  factor_values_manifest_root: manifests/factor_values
""",
        encoding="utf-8",
    )

    config = load_config(config_path, workspace=tmp_path)

    assert config.paths.factor_values_root == tmp_path / "factor_values"
    assert config.paths.factor_values_overlay_root == tmp_path / "factor_values_overlay"
    assert config.paths.factor_values_manifest_root == tmp_path / "manifests" / "factor_values"


def test_config_loads_explicit_runtime_env_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("QF_TEST_API_KEY", raising=False)
    config_path = tmp_path / "config.yaml"
    env_path = tmp_path / "local.env"
    env_path.write_text(
        """
# Plain KEY=value syntax only.
QF_TEST_API_KEY="test-value"
""",
        encoding="utf-8",
    )
    config_path.write_text(
        """
runtime:
  env_files:
    - local.env
llm:
  provider: openai_compatible
  providers:
    openai_compatible:
      provider: openai_compatible
      model: test-model
      base_url: https://example.invalid/v1
      api_key_env: QF_TEST_API_KEY
""",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.runtime.env_files == (env_path,)
    validate_llm_runtime(config.llm)


def test_runtime_env_files_reject_absolute_or_home_paths(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
runtime:
  env_files:
    - ~/secret.env
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="relative to the config file"):
        load_config(config_path)

    config_path.write_text(
        f"""
runtime:
  env_files:
    - {tmp_path / "secret.env"}
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="relative to the config file"):
        load_config(config_path)


def test_runtime_env_files_reject_parent_traversal(tmp_path: Path) -> None:
    config_dir = tmp_path / "nested"
    config_dir.mkdir()
    config_path = config_dir / "config.yaml"
    config_path.write_text(
        """
runtime:
  env_files:
    - ../secret.env
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="under the config file directory"):
        load_config(config_path)


def test_runtime_env_files_require_git_ignore_inside_worktree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.yaml"
    env_path = tmp_path / "local.env"
    env_path.write_text("QF_TEST_API_KEY=test-value\n", encoding="utf-8")
    config_path.write_text(
        """
runtime:
  env_files:
    - local.env
""",
        encoding="utf-8",
    )

    def fake_run(command, check=False, capture_output=False, text=False):
        if command[3:5] == ["rev-parse", "--show-toplevel"]:
            return subprocess.CompletedProcess(command, 0, stdout=f"{tmp_path}\n", stderr="")
        if command[3] == "ls-files":
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="")
        if command[3] == "check-ignore":
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="")
        raise AssertionError(f"unexpected git command: {command}")

    monkeypatch.setattr("quant_forge.config.subprocess.run", fake_run)

    with pytest.raises(ValueError, match="must be ignored by git"):
        load_config(config_path)


def test_runtime_env_files_reject_shell_syntax(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    env_path = tmp_path / "local.env"
    env_path.write_text("QF_TEST_API_KEY=$(security find-generic-password)\n", encoding="utf-8")
    config_path.write_text(
        """
runtime:
  env_files:
    - local.env
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="shell syntax is not allowed"):
        load_config(config_path)


@pytest.mark.parametrize(
    "line",
    [
        "QF_TEST_API_KEY=${DEEPSEEK_API_KEY}\n",
        "QF_TEST_API_KEY=value&&other\n",
        "QF_TEST_API_KEY=value|other\n",
        "QF_TEST_API_KEY=value # comment\n",
    ],
)
def test_runtime_env_files_reject_non_plain_values(tmp_path: Path, line: str) -> None:
    config_path = tmp_path / "config.yaml"
    env_path = tmp_path / "local.env"
    env_path.write_text(line, encoding="utf-8")
    config_path.write_text(
        """
runtime:
  env_files:
    - local.env
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="shell syntax|whitespace"):
        load_config(config_path)


@pytest.mark.parametrize("line", ["QF_TEST_API_KEY=\n", 'QF_TEST_API_KEY=""\n'])
def test_runtime_env_files_reject_empty_values(tmp_path: Path, line: str) -> None:
    config_path = tmp_path / "config.yaml"
    env_path = tmp_path / "local.env"
    env_path.write_text(line, encoding="utf-8")
    config_path.write_text(
        """
runtime:
  env_files:
    - local.env
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="must not be empty"):
        load_config(config_path)


def test_validate_llm_runtime_reports_missing_active_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("QF_MISSING_API_KEY", raising=False)
    config = load_config(Path("configs/default.yaml"))
    provider = config.llm.providers["deepseek"]
    config = QuantForgeConfig(
        paths=config.paths,
        web=config.web,
        research=config.research,
        runtime=config.runtime,
        simulation=config.simulation,
        llm=LLMSettings(
            provider="deepseek",
            model=provider.model,
            base_url=provider.base_url,
            api_key_env="QF_MISSING_API_KEY",
            providers={
                **config.llm.providers,
                "deepseek": LLMProviderSettings(
                    provider=provider.provider,
                    model=provider.model,
                    base_url=provider.base_url,
                    api_key_env="QF_MISSING_API_KEY",
                ),
            },
        ),
    )

    with pytest.raises(RuntimeError, match="QF_MISSING_API_KEY"):
        validate_llm_runtime(config.llm)


def test_config_allows_no_auth_openai_compatible_provider_without_env_name(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "llm:\n"
        "  provider: openai_compatible\n"
        "  providers:\n"
        "    openai_compatible:\n"
        "      provider: openai_compatible\n"
        "      model: local-model\n"
        "      base_url: http://127.0.0.1:11434/v1\n"
        "      require_api_" "key: false\n",
        encoding="utf-8",
    )

    config = load_config(config_path)
    selected = config.llm.select_provider()

    assert selected.provider == "openai_compatible"
    assert selected.api_key_required is False
    assert selected.api_key_env == ""
    validate_llm_runtime(config.llm)


def test_validate_any_llm_runtime_allows_active_rule_with_optional_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "DEEPSEEK_API_KEY",
        "GLM_API_KEY",
        "OPENAI_API_KEY",
        "MINIMAX_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENAI_COMPATIBLE_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    config = load_config(Path("configs/default.draft.yaml"))

    validate_any_llm_runtime(config.llm)


def test_validate_any_llm_runtime_requires_active_non_rule_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("QF_MISSING_ACTIVE_KEY", raising=False)
    monkeypatch.setenv("QF_READY_OPTIONAL_KEY", "set")
    llm = LLMSettings(
        provider="deepseek",
        providers={
            "deepseek": LLMProviderSettings(
                provider="deepseek",
                model="deepseek-chat",
                base_url="https://api.deepseek.com",
                api_key_env="QF_MISSING_ACTIVE_KEY",
            ),
            "glm": LLMProviderSettings(
                provider="glm",
                model="glm-test",
                base_url="https://example.invalid/glm",
                api_key_env="QF_READY_OPTIONAL_KEY",
            ),
        },
    )

    with pytest.raises(RuntimeError, match="QF_MISSING_ACTIVE_KEY"):
        validate_any_llm_runtime(llm)


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
    assert FactorDefinition(factor_id="WQ_ALPHA_003", name="wq_alpha_003", formula="precomputed:worldquant_alpha_003")
    with pytest.raises(ValueError, match="invalid factor status"):
        FactorDefinition(
            factor_id="FTR_BAD", name="bad", formula="rank(close)", status="unknown"  # type: ignore[arg-type]
        )


def test_data_root_can_point_to_workspace_with_nested_panel(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    provider = LocalPanelDataProvider(paths["workspace"])

    assert provider.panel_path == paths["data_root"] / "panel.parquet"
    assert provider.validate().ok is True


def test_data_root_can_point_to_source_snapshot(tmp_path: Path) -> None:
    snapshot = tmp_path / "lakehouse" / "source_snapshot" / "provider=test" / "market=cn_a"
    price_dir = snapshot / "price"
    basic_dir = snapshot / "daily_basic"
    price_dir.mkdir(parents=True)
    basic_dir.mkdir(parents=True)
    pd.DataFrame(
        {
            "ts_code": ["AAA", "BBB", "AAA", "BBB", "AAA", "BBB"],
            "trade_date": ["20250102", "20250102", "20250103", "20250103", "20250106", "20250106"],
            "close": [10.0, 20.0, 11.0, 19.0, 12.0, 21.0],
            "vol": [100.0, 200.0, 110.0, 190.0, 120.0, 210.0],
        }
    ).to_parquet(price_dir / "2025.parquet", index=False)
    pd.DataFrame(
        {
            "ts_code": ["AAA", "BBB", "AAA", "BBB", "AAA", "BBB"],
            "trade_date": ["20250102", "20250102", "20250103", "20250103", "20250106", "20250106"],
            "total_mv": [1000.0, 2000.0, 1100.0, 1900.0, 1200.0, 2100.0],
            "circ_mv": [900.0, 1800.0, 990.0, 1710.0, 1080.0, 1890.0],
        }
    ).to_parquet(basic_dir / "2025.parquet", index=False)

    provider = LocalPanelDataProvider(tmp_path / "lakehouse")
    validation = provider.validate()
    panel = provider.load_panel()

    assert validation.ok is True
    assert validation.panel_path == snapshot
    assert validation.optional_columns[0] == "source_snapshot"
    assert len(panel) == 6
    assert list(panel.columns) == [
        "trade_date",
        "instrument",
        "close",
        "market_cap",
        "is_st",
        "volume",
        "return_1d",
        "return_5d",
        "volatility_5d",
    ]
    assert panel["market_cap"].tolist() == [1000.0, 2000.0, 1100.0, 1900.0, 1200.0, 2100.0]


@pytest.mark.parametrize(
    ("price_columns", "basic_columns", "missing_columns"),
    (
        (
            {"ts_code": ["AAA"], "trade_date": ["20250102"], "vol": [100.0]},
            {"ts_code": ["AAA"], "trade_date": ["20250102"], "total_mv": [1000.0], "circ_mv": [900.0]},
            ("price",),
        ),
        (
            {"ts_code": ["AAA"], "trade_date": ["20250102"], "close": [10.0], "vol": [100.0]},
            {"ts_code": ["AAA"], "trade_date": ["20250102"], "circ_mv": [900.0]},
            ("daily_basic",),
        ),
    ),
)
def test_source_snapshot_validation_requires_load_time_columns(
    tmp_path: Path,
    price_columns: dict[str, list[object]],
    basic_columns: dict[str, list[object]],
    missing_columns: tuple[str, ...],
) -> None:
    snapshot = tmp_path / "lakehouse" / "source_snapshot" / "provider=test" / "market=cn_a"
    price_dir = snapshot / "price"
    basic_dir = snapshot / "daily_basic"
    price_dir.mkdir(parents=True)
    basic_dir.mkdir(parents=True)
    pd.DataFrame(price_columns).to_parquet(price_dir / "2025.parquet", index=False)
    pd.DataFrame(basic_columns).to_parquet(basic_dir / "2025.parquet", index=False)

    validation = LocalPanelDataProvider(tmp_path / "lakehouse").validate()

    assert validation.ok is False
    assert validation.missing_columns == missing_columns
    assert validation.optional_columns[0].startswith("source_snapshot_error=")


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

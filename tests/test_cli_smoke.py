from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pandas as pd

from quant_forge.data.local import create_demo_workspace


def run_cli(*args: str) -> dict:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path.cwd() / "src")
    completed = subprocess.run(
        [sys.executable, "-m", "quant_forge.apps.cli.main", *args],
        check=True,
        capture_output=True,
        env=env,
        text=True,
    )
    return json.loads(completed.stdout)


def run_cli_raw(*args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path.cwd() / "src")
    return subprocess.run(
        [sys.executable, "-m", "quant_forge.apps.cli.main", *args],
        capture_output=True,
        env=env,
        text=True,
    )


def test_cli_smoke_path(tmp_path: Path) -> None:
    workspace = tmp_path / "demo"
    init_payload = run_cli("init", "--workspace", str(workspace))
    assert Path(init_payload["data_root"]).exists()
    assert Path(init_payload["factor_root"]).exists()

    data = run_cli("data", "validate", "--data-root", str(workspace / "data"))
    assert data["ok"] is True
    assert data["rows"] > 0
    assert data["panel_path"].endswith("panel.parquet")
    assert data["start_date"]
    assert data["end_date"]

    factors = run_cli("factor", "list", "--factor-root", str(workspace / "factor_root"))
    assert {factor["factor_id"] for factor in factors} >= {"FTR_DEMO_SMALL_CAP", "FTR_DEMO_MOMENTUM"}

    mounted_factor_values = workspace / "mounted_factor_values" / "worldquant_alpha_003"
    mounted_factor_values.mkdir(parents=True)
    (mounted_factor_values / "2024.metadata.json").write_text(
        json.dumps({"factor_id": "WQ_ALPHA_003", "factor_name": "worldquant_alpha_003"}),
        encoding="utf-8",
    )
    pd.DataFrame(
        {
            "trade_date": ["20240102"],
            "instrument": ["STK001"],
            "factor_id": ["WQ_ALPHA_003"],
            "factor_value": [0.1],
        }
    ).to_parquet(mounted_factor_values / "2024.parquet", index=False)
    factors_with_mounted_values = run_cli(
        "factor",
        "list",
        "--factor-root",
        str(workspace / "factor_root"),
        "--factor-values-root",
        str(workspace / "mounted_factor_values"),
    )
    assert {factor["factor_id"] for factor in factors_with_mounted_values} >= {
        "FTR_DEMO_SMALL_CAP",
        "FTR_DEMO_MOMENTUM",
        "WQ_ALPHA_003",
    }
    imported_precomputed = run_cli(
        "factor",
        "import-precomputed",
        "alpha_003",
        "--factor-root",
        str(workspace / "factor_root"),
        "--factor-values-root",
        str(workspace / "mounted_factor_values"),
    )
    assert imported_precomputed["imported_count"] == 1
    assert imported_precomputed["factor_ids"] == ["WQ_ALPHA_003"]
    assert (workspace / "factor_root" / "inactive_factors" / "WQ_ALPHA_003" / "factor.yaml").exists()

    doctor = run_cli("doctor", "--workspace", str(workspace))
    assert doctor["ok"] is True
    assert doctor["data"]["ok"] is True
    assert doctor["factor_root"]["factor_count"] >= 2
    assert any(doctor["factor_root"]["sample_factor_ids"][0] in command for command in doctor["next_commands"])
    assert any(check["name"] == "factor_values" and check["status"] == "warning" for check in doctor["checks"])

    parsed = run_cli(
        "idea-to-factor",
        "--text",
        "非ST的小市值股票在未来一个月表现更好",
        "--factor-root",
        str(workspace / "factor_root"),
    )
    assert parsed["status"] == "draft"
    assert parsed["formula"] == "-rank(market_cap)"
    assert parsed["universe_filters"] == ["is_st == false"]

    factor_values_root = workspace / "factor_values"
    evaluation = run_cli(
        "eval-factor",
        "FTR_DEMO_SMALL_CAP",
        "--factor-root",
        str(workspace / "factor_root"),
        "--data-root",
        str(workspace / "data"),
        "--artifact-root",
        str(workspace / "artifacts"),
        "--factor-values-root",
        str(factor_values_root),
    )
    assert evaluation["observations"] > 0
    assert Path(evaluation["artifact_path"]).exists()
    assert evaluation["score_source"] == "factor_values_incremental"
    assert evaluation["score_computed_rows"] > 0
    assert evaluation["factor_values_path"]
    assert (factor_values_root / "demo_small_cap" / "incremental" / "2024.parquet").exists()

    backtest = run_cli(
        "run-backtest",
        "FTR_DEMO_SMALL_CAP",
        "--factor-root",
        str(workspace / "factor_root"),
        "--data-root",
        str(workspace / "data"),
        "--artifact-root",
        str(workspace / "artifacts"),
        "--factor-values-root",
        str(factor_values_root),
    )
    assert backtest["periods"] > 0
    assert Path(backtest["artifact_path"]).exists()
    assert "gross_annualized_return" in backtest
    assert "net_annualized_return" in backtest
    assert "rebalance_rate" in backtest
    assert "turnover_rate" in backtest
    assert backtest["score_source"] == "factor_values_cached"
    assert backtest["score_cached_rows"] > 0
    backtest_artifact = json.loads(Path(backtest["artifact_path"]).read_text(encoding="utf-8"))
    assert backtest_artifact["score_source"] == "factor_values_cached"

    rd_config = workspace / "rd.yaml"
    rd_config.write_text(
        """
objective: balanced
default_max_candidates: 1
default_interval_days: 1
allowed_interval_days: [1, 5, 15, 30]
weights:
  rank_ic_mean: 0.45
  rank_icir: 0.35
  annualized_return: 0.15
  max_drawdown: 0.05
""",
        encoding="utf-8",
    )
    research = run_cli(
        "research",
        "run-once",
        "FTR_DEMO_SMALL_CAP",
        "--workspace",
        str(workspace),
        "--rd-config",
        str(rd_config),
    )
    assert research["seed_factor_id"] == "FTR_DEMO_SMALL_CAP"
    assert research["objective"] == "balanced"
    assert research["candidates"][0]["factor"]["status"] == "candidate"
    assert research["accepted_candidate_ids"]
    assert Path(research["report_path"]).exists()

    invalid = run_cli_raw(
        "research",
        "run-once",
        "FTR_DEMO_SMALL_CAP",
        "--workspace",
        str(workspace),
        "--max-candidates",
        "0",
    )
    assert invalid.returncode != 0
    assert "max_candidates must be between 1 and 10" in invalid.stderr


def test_doctor_reports_missing_llm_key(tmp_path: Path) -> None:
    workspace = tmp_path / "doctor_demo"
    run_cli("init", "--workspace", str(workspace))
    config_path = tmp_path / "llm_config.yaml"
    config_path.write_text(
        """
llm:
  provider: openai_compatible
  providers:
    openai_compatible:
      provider: openai_compatible
      model: test-model
      base_url: https://example.invalid/v1
      api_key_env: QF_DOCTOR_MISSING_KEY
""",
        encoding="utf-8",
    )

    result = run_cli_raw("doctor", "--workspace", str(workspace), "--config", str(config_path))

    assert result.returncode == 2
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["llm"]["missing_providers"][0]["api_key_env"] == "QF_DOCTOR_MISSING_KEY"
    assert "QF_DOCTOR_MISSING_KEY" in payload["llm"]["missing_providers"][0]["error"]


def test_doctor_allows_rule_config_with_unready_optional_llms(tmp_path: Path, monkeypatch) -> None:
    for name in (
        "DEEPSEEK_API_KEY",
        "GLM_API_KEY",
        "OPENAI_API_KEY",
        "MINIMAX_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENAI_COMPATIBLE_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    workspace = tmp_path / "rule_demo"
    run_cli("init", "--workspace", str(workspace))

    result = run_cli_raw("doctor", "--workspace", str(workspace), "--config", "configs/default.draft.yaml")

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["llm"]["active_provider"] == "rule"
    assert payload["llm"]["active_requires_api_key"] is False
    assert any(check["name"] == "llm" and check["status"] == "warning" for check in payload["checks"])


def test_doctor_does_not_suggest_factor_commands_without_factors(tmp_path: Path) -> None:
    workspace = tmp_path / "empty_demo"
    data_root = workspace / "data"
    factor_root = workspace / "factor_root"
    artifact_root = workspace / "artifacts"
    create_demo_workspace(workspace, data_root=data_root, factor_root=workspace / "unused_factors", artifact_root=artifact_root)
    factor_root.mkdir(parents=True)

    result = run_cli_raw(
        "doctor",
        "--data-root",
        str(data_root),
        "--factor-root",
        str(factor_root),
        "--artifact-root",
        str(artifact_root),
    )

    assert result.returncode == 2
    payload = json.loads(result.stdout)
    assert payload["factor_root"]["factor_count"] == 0
    assert all("FTR_DEMO_SMALL_CAP" not in command for command in payload["next_commands"])
    assert all("eval-factor" not in command for command in payload["next_commands"])


def test_cli_uses_workspace_config_roots(tmp_path: Path) -> None:
    workspace = tmp_path / "config_demo"
    run_cli("init", "--workspace", str(workspace))
    data = run_cli("data", "validate", "--workspace", str(workspace))
    factors = run_cli("factor", "list", "--workspace", str(workspace))
    evaluation = run_cli("eval-factor", "FTR_DEMO_SMALL_CAP", "--workspace", str(workspace))
    backtest = run_cli("run-backtest", "FTR_DEMO_SMALL_CAP", "--workspace", str(workspace))
    research = run_cli(
        "research",
        "run-once",
        "FTR_DEMO_SMALL_CAP",
        "--workspace",
        str(workspace),
        "--max-candidates",
        "1",
    )

    assert data["ok"] is True
    assert factors
    assert evaluation["observations"] > 0
    assert backtest["periods"] > 0
    assert backtest["top_quantile"] == 0.3
    assert backtest["simulation_profile"]["decay_days"] == 0
    assert research["objective"] == "balanced"
    assert research["accepted_candidate_ids"]
    assert Path(research["report_path"]).exists()


def test_run_backtest_uses_rd_config_top_quantile(tmp_path: Path) -> None:
    workspace = tmp_path / "backtest_config_demo"
    run_cli("init", "--workspace", str(workspace))
    rd_config = workspace / "rd.yaml"
    rd_config.write_text(
        """simulation:
  top_quantile: 0.2
  decay_days: 2
transaction_costs:
  commission_bps: 5.0
  slippage_bps: 3.0
""",
        encoding="utf-8",
    )

    configured = run_cli(
        "run-backtest",
        "FTR_DEMO_SMALL_CAP",
        "--workspace",
        str(workspace),
        "--rd-config",
        str(rd_config),
    )
    overridden = run_cli(
        "run-backtest",
        "FTR_DEMO_SMALL_CAP",
        "--workspace",
        str(workspace),
        "--rd-config",
        str(rd_config),
        "--top-quantile",
        "0.4",
    )

    assert configured["top_quantile"] == 0.2
    assert configured["simulation_profile"]["decay_days"] == 2
    assert configured["transaction_costs"]["commission_bps"] == 5.0
    assert configured["net_annualized_return"] < configured["gross_annualized_return"]
    assert overridden["top_quantile"] == 0.4
    assert overridden["simulation_profile"]["decay_days"] == 2
    assert overridden["transaction_costs"]["slippage_bps"] == 3.0

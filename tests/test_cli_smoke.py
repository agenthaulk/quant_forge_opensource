from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


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

    factors = run_cli("factor", "list", "--factor-root", str(workspace / "factor_root"))
    assert {factor["factor_id"] for factor in factors} >= {"FTR_DEMO_SMALL_CAP", "FTR_DEMO_MOMENTUM"}

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

    evaluation = run_cli(
        "eval-factor",
        "FTR_DEMO_SMALL_CAP",
        "--factor-root",
        str(workspace / "factor_root"),
        "--data-root",
        str(workspace / "data"),
        "--artifact-root",
        str(workspace / "artifacts"),
    )
    assert evaluation["observations"] > 0
    assert Path(evaluation["artifact_path"]).exists()

    backtest = run_cli(
        "run-backtest",
        "FTR_DEMO_SMALL_CAP",
        "--factor-root",
        str(workspace / "factor_root"),
        "--data-root",
        str(workspace / "data"),
        "--artifact-root",
        str(workspace / "artifacts"),
    )
    assert backtest["periods"] > 0
    assert Path(backtest["artifact_path"]).exists()

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
    assert overridden["top_quantile"] == 0.4
    assert overridden["simulation_profile"]["decay_days"] == 2

from __future__ import annotations

import json
from pathlib import Path

import pytest

from quant_forge.data.local import LocalPanelDataProvider, create_demo_workspace
from quant_forge.evaluation.service import evaluate_factor
from quant_forge.factor_engine.executor import execute_factor_formula
from quant_forge.core.contracts import SimulationProfile


def test_local_provider_and_engine(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    provider = LocalPanelDataProvider(paths["data_root"])
    panel = provider.load_panel()

    scores = execute_factor_formula(panel, "-rank(market_cap)", ("is_st == false",))
    assert {"trade_date", "instrument", "score"} <= set(scores.columns)
    assert scores["score"].notna().any()

    with pytest.raises(ValueError, match="unsupported or missing factor field"):
        execute_factor_formula(panel, "rank(unknown_field)")


def test_evaluation_writes_artifact(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    result = evaluate_factor(
        "FTR_DEMO_SMALL_CAP",
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"],
    )
    assert result.ic_days > 0
    assert {metric.name for metric in result.split_metrics} == {"IS", "OOS1", "OOS2"}
    assert {metric.horizon_days for metric in result.horizon_metrics} >= {5, 10, 21, 63}
    assert result.artifact_path.exists()
    payload = json.loads(result.artifact_path.read_text(encoding="utf-8"))
    assert payload["split_metrics"]
    assert payload["horizon_matrix"]
    assert payload["simulation_profile"]["decay_days"] == 0
    assert result.simulation_profile.decay_days == 0


def test_evaluation_records_non_default_simulation_profile(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    result = evaluate_factor(
        "FTR_DEMO_SMALL_CAP",
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"],
        simulation_profile=SimulationProfile(decay_days=2),
    )
    payload = json.loads(result.artifact_path.read_text(encoding="utf-8"))

    assert result.artifact_path.name != "FTR_DEMO_SMALL_CAP.json"
    assert payload["simulation_profile"]["decay_days"] == 2

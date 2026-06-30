from __future__ import annotations

import json
import math
from pathlib import Path

import pandas as pd
import pytest

from quant_forge.data.local import LocalPanelDataProvider, create_demo_workspace
from quant_forge.evaluation.service import _with_forward_return, evaluate_factor
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


def test_forward_return_respects_execution_delay() -> None:
    panel = pd.DataFrame(
        {
            "trade_date": pd.date_range("2024-01-01", periods=5, freq="D"),
            "instrument": ["A"] * 5,
            "close": [10.0, 11.0, 13.0, 16.0, 20.0],
        }
    )

    labeled = _with_forward_return(panel, horizon_days=2, execution_delay_days=1)

    assert labeled.loc[0, "forward_return"] == pytest.approx(16.0 / 11.0 - 1.0)
    assert labeled.loc[1, "forward_return"] == pytest.approx(20.0 / 13.0 - 1.0)
    assert labeled.loc[2:, "forward_return"].isna().all()
    delayed = _with_forward_return(panel, horizon_days=1, execution_delay_days=2)
    assert delayed.loc[0, "forward_return"] == pytest.approx(16.0 / 13.0 - 1.0)
    assert delayed.loc[1, "forward_return"] == pytest.approx(20.0 / 16.0 - 1.0)
    assert delayed.loc[2:, "forward_return"].isna().all()


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
    assert "warnings" in payload
    assert isinstance(result.warnings, tuple)
    assert result.simulation_profile.decay_days == 0
    assert payload["score_compute_mode"]
    assert payload["score_required_rows"] >= payload["score_computed_rows"]
    assert result.score_compute_mode == payload["score_compute_mode"]
    assert payload["rank_ic_t_stat"] == result.rank_ic_t_stat
    assert result.rank_ic_t_stat == pytest.approx(result.rank_icir * math.sqrt(result.ic_days))
    assert result.rank_icir == pytest.approx(result.rank_ic_mean / result.rank_ic_std)
    assert result.split_metrics[0].rank_ic_t_stat == pytest.approx(
        result.split_metrics[0].rank_icir * math.sqrt(result.split_metrics[0].ic_days)
    )


def test_evaluation_warns_when_oos_decays(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    result = evaluate_factor(
        "FTR_DEMO_SMALL_CAP",
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"],
    )

    assert result.warnings
    assert "OOS decay warning" in result.warnings[0]


def test_evaluation_records_non_default_simulation_profile(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    result = evaluate_factor(
        "FTR_DEMO_SMALL_CAP",
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"],
        simulation_profile=SimulationProfile(execution_delay_days=2, decay_days=2),
    )
    payload = json.loads(result.artifact_path.read_text(encoding="utf-8"))

    assert result.artifact_path.name != "FTR_DEMO_SMALL_CAP.json"
    assert payload["simulation_profile"]["execution_delay_days"] == 2
    assert payload["simulation_profile"]["decay_days"] == 2


def test_evaluation_accepts_non_default_execution_delay(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    delayed = evaluate_factor(
        "FTR_DEMO_SMALL_CAP",
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"],
        simulation_profile=SimulationProfile(execution_delay_days=2),
    )
    payload = json.loads(delayed.artifact_path.read_text(encoding="utf-8"))

    assert delayed.ic_days > 0
    assert delayed.observations > 0
    assert payload["simulation_profile"]["execution_delay_days"] == 2
    assert payload["rank_ic_mean"] == delayed.rank_ic_mean


def test_evaluation_rejects_display_window_shorter_than_six_months(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")

    with pytest.raises(ValueError, match="at least 126 daily trading dates"):
        evaluate_factor(
            "FTR_DEMO_SMALL_CAP",
            factor_root=paths["factor_root"],
            data_root=paths["data_root"],
            artifact_root=paths["artifact_root"],
            simulation_profile=SimulationProfile(test_period_end="2024-02-14"),
        )


def test_evaluation_loads_precomputed_factor_without_local_definition(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    panel = LocalPanelDataProvider(paths["data_root"]).load_panel()
    factor_values_root = tmp_path / "factor_values"
    factor_dir = factor_values_root / "worldquant_alpha_003"
    factor_dir.mkdir(parents=True)
    (factor_dir / "2024.metadata.json").write_text(
        json.dumps(
            {
                "factor_id": "WQ_ALPHA_003",
                "factor_name": "worldquant_alpha_003",
                "factor_store_key": "worldquant_alpha_003",
            }
        ),
        encoding="utf-8",
    )
    values = panel[["trade_date", "instrument", "market_cap"]].copy()
    values["trade_date"] = pd.to_datetime(values["trade_date"]).dt.strftime("%Y%m%d")
    values["factor_id"] = "WQ_ALPHA_003"
    values["factor_value"] = values.groupby("trade_date")["market_cap"].rank(pct=True)
    values.rename(columns={"instrument": "instrument_id"}, inplace=True)
    values[["trade_date", "instrument_id", "factor_id", "factor_value"]].to_parquet(
        factor_dir / "2024.parquet",
        index=False,
    )

    result = evaluate_factor(
        "WQ_ALPHA_003",
        factor_root=tmp_path / "empty_factor_root",
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"],
        factor_values_root=factor_values_root,
    )

    assert result.ic_days > 0
    assert result.score_source == "factor_values_cached"
    assert result.score_cached_rows == len(panel)
    assert result.score_computed_rows == 0
    assert result.factor_values_path == factor_dir

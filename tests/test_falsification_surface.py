"""Lane F regression tests: falsification diagnostics reachable from the workbench.

Covers the wave-2 falsification surface:
- ``falsification_frame`` matches ``evaluate_factor``'s IC input alignment;
- ``WorkbenchService.run_falsification`` writes a status-carrying JSON artifact
  (null-not-zero for insufficient panels);
- the run index gains a ``falsification`` row with status-preserving highlights
  and a factor-definition -> falsification lineage edge;
- identical seed and inputs reproduce the identical report and artifact bytes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from quant_forge.data.local import LocalPanelDataProvider, create_demo_workspace
from quant_forge.evaluation.falsification import (
    BELOW_FALSIFICATION_SAMPLE_FLOOR,
    FALSIFICATION_SCHEMA_VERSION,
)
from quant_forge.evaluation.service import falsification_frame
from quant_forge.lineage.store import LineageStore, RunIndex
from quant_forge.workbench.service import FALSIFICATION_HIGHLIGHT_METRICS, WorkbenchService

FACTOR_ID = "FTR_DEMO_MOMENTUM"
FACTOR_HORIZON_DAYS = 5  # demo factor definition
EXECUTION_DELAY_DAYS = 1  # SimulationProfile default
HIGHLIGHT_NAMES = {"placebo_percentile", "ic_half_life_days", "block_sign_consistency"}


def _workbench(tmp_path: Path) -> tuple[WorkbenchService, dict[str, Path]]:
    paths = create_demo_workspace(tmp_path / "demo")
    workbench = WorkbenchService(
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"],
    )
    return workbench, paths


def _read_artifact(paths: dict[str, Path]) -> dict:
    artifact_path = paths["artifact_root"] / "falsification" / f"{FACTOR_ID}.json"
    assert artifact_path.is_file()
    return json.loads(artifact_path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Frame alignment: the falsification input IS the evaluation IC input
# ---------------------------------------------------------------------------


def test_falsification_frame_matches_evaluation_ic_input(tmp_path: Path) -> None:
    workbench, paths = _workbench(tmp_path)
    evaluation = workbench.evaluate(FACTOR_ID)
    frame = falsification_frame(
        FACTOR_ID,
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
    )

    assert list(frame.columns) == ["trade_date", "instrument", "score", "forward_return"]
    assert not frame.duplicated(subset=["trade_date", "instrument"]).any()

    # Same evaluable-date rule as evaluation/service._ic_summary: joint non-null
    # rows, at least two distinct scores and forward returns per date.
    usable = frame.dropna(subset=["score", "forward_return"])
    evaluable_dates = sum(
        1
        for _, group in usable.groupby("trade_date")
        if group["score"].nunique() >= 2 and group["forward_return"].nunique() >= 2
    )
    assert evaluation.ic_days > 0
    assert evaluable_dates == evaluation.ic_days

    # Spot-check one (date, instrument, forward_return) triple against the raw
    # panel: entry at t + delay, exit at t + delay + horizon (same shifts as
    # evaluate_factor's forward-return construction).
    panel = LocalPanelDataProvider(paths["data_root"]).load_panel()
    instrument = "STK005"
    closes = panel[panel["instrument"] == instrument].sort_values("trade_date").reset_index(drop=True)
    signal_index = 10
    entry_close = closes.loc[signal_index + EXECUTION_DELAY_DAYS, "close"]
    future_close = closes.loc[signal_index + EXECUTION_DELAY_DAYS + FACTOR_HORIZON_DAYS, "close"]
    expected_forward_return = future_close / entry_close - 1.0
    signal_date = closes.loc[signal_index, "trade_date"]
    row = frame[(frame["instrument"] == instrument) & (frame["trade_date"] == signal_date)]
    assert len(row) == 1
    assert row["forward_return"].iloc[0] == pytest.approx(expected_forward_return)


# ---------------------------------------------------------------------------
# Workbench surface: artifact, run-index row, lineage edge
# ---------------------------------------------------------------------------


def test_run_falsification_writes_artifact_run_row_and_lineage(tmp_path: Path) -> None:
    workbench, paths = _workbench(tmp_path)
    report = workbench.run_falsification(FACTOR_ID, seed=17)

    payload = _read_artifact(paths)
    assert payload["schema_version"] == FALSIFICATION_SCHEMA_VERSION
    assert payload["factor_id"] == FACTOR_ID
    assert payload["formula"] == "rank(return_5d)"
    assert payload["horizon_days"] == FACTOR_HORIZON_DAYS
    assert payload["seed"] == 17
    # MetricValues are serialized with their statuses intact.
    assert payload["placebo_percentile"]["status"] == report.placebo_percentile.status == "available"
    assert payload["placebo_percentile"]["value"] == report.placebo_percentile.value
    assert payload["block_sign_consistency"]["status"] == report.block_sign_consistency.status
    assert [metric["segment"] for metric in payload["ic_lag_metrics"]] == [f"lag_{lag}" for lag in range(1, 6)]
    assert all("status" in metric for metric in payload["ic_lag_metrics"] + payload["block_metrics"])

    rows = RunIndex(paths["artifact_root"]).search(factor_id=FACTOR_ID, kind="falsification")
    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == "falsification"
    assert row["run_id"].startswith("falsification-")
    assert row["factor_ids"] == [FACTOR_ID]
    assert row["data_window"]["status"] == "available"
    assert row["artifact_paths_rel"] == [f"falsification/{FACTOR_ID}.json"]
    assert set(row["metric_highlights"]) == HIGHLIGHT_NAMES == set(FALSIFICATION_HIGHLIGHT_METRICS)
    for name, highlight in row["metric_highlights"].items():
        metric = getattr(report, name)
        assert highlight["status"] == metric.status
        assert highlight["value"] == metric.value
        assert highlight["observation_count"] == metric.observation_count
    # No absolute paths anywhere in the run index (redaction/relative-path rule).
    index_text = RunIndex(paths["artifact_root"]).index_path.read_text(encoding="utf-8")
    assert str(tmp_path) not in index_text

    # Lineage edge: factor definition -> falsification artifact.
    lineage_rows = LineageStore(paths["artifact_root"]).read_rows()
    definition_rows = [item for item in lineage_rows if item["artifact_type"] == "factor_definition"]
    falsification_rows = [item for item in lineage_rows if item["artifact_type"] == "falsification"]
    assert len(definition_rows) == 1
    assert len(falsification_rows) == 1
    assert falsification_rows[0]["parents"] == [definition_rows[0]["artifact_id"]]
    assert falsification_rows[0]["path_rel"] == f"falsification/{FACTOR_ID}.json"
    assert falsification_rows[0]["generated_by"] == "workbench.run_falsification"


# ---------------------------------------------------------------------------
# Null-not-zero: an insufficient panel surfaces statuses, never numbers
# ---------------------------------------------------------------------------


def test_insufficient_panel_reports_null_not_zero(tmp_path: Path) -> None:
    workbench, paths = _workbench(tmp_path)
    # A horizon near the panel length leaves fewer labeled dates than the
    # falsification floor (30), so every diagnostic must be null with status.
    report = workbench.run_falsification(FACTOR_ID, seed=3, horizon_days=140)

    assert 0 < report.evaluable_dates < report.min_evaluable_dates
    assert BELOW_FALSIFICATION_SAMPLE_FLOOR in report.warning_codes

    payload = _read_artifact(paths)
    assert payload["horizon_days"] == 140
    for name in HIGHLIGHT_NAMES | {"real_rank_ic_mean", "ic_decay_rate", "block_ic_spread"}:
        assert payload[name]["value"] is None
        assert payload[name]["status"] == "insufficient_sample"
        assert BELOW_FALSIFICATION_SAMPLE_FLOOR in payload[name]["warning_codes"]

    row = RunIndex(paths["artifact_root"]).search(factor_id=FACTOR_ID, kind="falsification")[-1]
    assert row["warnings_count"] > 0
    for highlight in row["metric_highlights"].values():
        assert highlight["value"] is None
        assert highlight["status"] == "insufficient_sample"


# ---------------------------------------------------------------------------
# Determinism: same seed, same inputs => identical report and artifact bytes
# ---------------------------------------------------------------------------


def test_same_seed_is_deterministic_across_calls(tmp_path: Path) -> None:
    workbench, paths = _workbench(tmp_path)
    first = workbench.run_falsification(FACTOR_ID, seed=11)
    artifact_path = paths["artifact_root"] / "falsification" / f"{FACTOR_ID}.json"
    first_bytes = artifact_path.read_bytes()

    second = workbench.run_falsification(FACTOR_ID, seed=11)
    assert first == second
    assert artifact_path.read_bytes() == first_bytes

    rows = RunIndex(paths["artifact_root"]).search(factor_id=FACTOR_ID, kind="falsification")
    assert len(rows) == 2  # run history stays append-only
    assert rows[0]["config_fingerprint"] == rows[1]["config_fingerprint"]
    assert rows[0]["metric_highlights"] == rows[1]["metric_highlights"]

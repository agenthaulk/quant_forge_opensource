"""BUG #007 regression: web-originated runs must reach the lineage/run index.

Before this fix, ``_validate_factor_workflow`` (idea validation),
``run_staggered_entry_workflow``, and ``run_multi_factor_backtest_workflow``
called ``evaluate_factor`` / ``run_factor_backtest`` / the composite engine
directly and never recorded anything, so ``RunIndex``, the registry detail
view, and 研究历史 (research history) stayed permanently empty for pure-web
users even though the CLI/workbench path recorded every run (see
tests/test_lineage_and_runs.py::test_workbench_evaluate_and_backtest_append_run_rows_and_lineage
for the CLI-side contract these web runs must now match).

These tests exercise the REAL (unmocked) compute path against the demo
fixture, not a monkeypatched seam: the shared
``quant_forge.lineage.recording.record_run`` helper hashes the artifact file
at the reported ``artifact_path``, so the artifact has to genuinely exist on
disk, which only the real evaluate/backtest/composite-engine path guarantees.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import quant_forge.apps.web.api as web_api
import quant_forge.apps.web.server as web_server
from quant_forge.apps.web.server import (
    _registry_factor_detail_payload,
    _validate_factor_workflow,
    run_multi_factor_backtest_workflow,
    run_staggered_entry_workflow,
)
from quant_forge.config import QuantForgeConfig
from quant_forge.data.local import create_demo_workspace
from quant_forge.factor_library.repository import FactorRepository
from quant_forge.lineage.store import RunIndex
from quant_forge.research_loop.config import ResearchLoopConfig, load_research_loop_config


def _rd_config(config: QuantForgeConfig) -> ResearchLoopConfig:
    return load_research_loop_config(web_server.DEFAULT_RD_CONFIG_PATH, config.research, config.simulation)


def test_validate_factor_workflow_records_evaluate_and_backtest_runs(tmp_path: Path) -> None:
    create_demo_workspace(tmp_path / "demo")
    config = QuantForgeConfig().resolve(tmp_path / "demo")
    factor = FactorRepository(config.paths.factor_root).get("FTR_DEMO_SMALL_CAP")
    assert RunIndex(config.paths.artifact_root).read_rows() == []

    payload = _validate_factor_workflow(
        config,
        factor,
        parser=None,
        parameters={"holding_days": 5},
        rd_config=_rd_config(config),
        cancel_event=None,
    )

    assert payload["factor"]["factor_id"] == "FTR_DEMO_SMALL_CAP"
    rows = RunIndex(config.paths.artifact_root).read_rows()
    # evaluate once, backtest twice (in-sample selection + external OOS) - the
    # same kind/highlight semantics WorkbenchService.evaluate/run_backtest
    # already leave for a CLI evaluate/backtest of the same artifact types.
    assert [row["kind"] for row in rows] == ["evaluate", "backtest", "backtest"]
    assert all(row["factor_ids"] == ["FTR_DEMO_SMALL_CAP"] for row in rows)
    assert all(row["warnings_count"] >= 0 for row in rows)
    for row in rows:
        assert row["artifact_paths_rel"]
        assert not row["artifact_paths_rel"][0].startswith("/")

    # The registry evidence chain (BUG #007's other symptom) now shows the
    # web-originated runs, not just CLI/workbench ones.
    detail = _registry_factor_detail_payload(config, "FTR_DEMO_SMALL_CAP")
    assert detail["factor"]["factor_id"] == "FTR_DEMO_SMALL_CAP"
    assert len(detail["runs"]) == 3

    # The pinned catalog/registry-list projection this run must not disturb
    # (registry rows are independent of the run index).
    from quant_forge.apps.web.api import _registry_factors_payload

    registry_ids = {row["factor_id"] for row in _registry_factors_payload(config)["factors"]}
    assert "FTR_DEMO_SMALL_CAP" in registry_ids


def test_staggered_entry_workflow_adds_one_backtest_run(tmp_path: Path) -> None:
    create_demo_workspace(tmp_path / "demo")
    config = QuantForgeConfig().resolve(tmp_path / "demo")
    assert RunIndex(config.paths.artifact_root).read_rows() == []

    result = run_staggered_entry_workflow(
        config,
        "FTR_DEMO_SMALL_CAP",
        formation_trading_days=5,
        rd_config=_rd_config(config),
    )

    assert result["cohort_count"] > 0
    rows = RunIndex(config.paths.artifact_root).read_rows()
    assert len(rows) == 1
    assert rows[0]["kind"] == "backtest"
    assert rows[0]["factor_ids"] == ["FTR_DEMO_SMALL_CAP"]
    assert rows[0]["artifact_paths_rel"]


def test_multi_factor_backtest_workflow_records_run_under_composite_id(tmp_path: Path) -> None:
    create_demo_workspace(tmp_path / "demo")
    config = QuantForgeConfig().resolve(tmp_path / "demo")
    assert RunIndex(config.paths.artifact_root).read_rows() == []

    payload = run_multi_factor_backtest_workflow(
        config,
        factor_refs=[
            {"factor_id": "FTR_DEMO_SMALL_CAP", "direction": 1},
            {"factor_id": "FTR_DEMO_MOMENTUM", "direction": -1},
        ],
        synthesis={"method": "equal_weight", "params": {}},
        standardization={"method": "zscore", "params": {}},
        parameters={"holding_days": 5},
        rd_config=_rd_config(config),
    )

    composite_id = payload["factor"]["factor_id"]
    assert composite_id.startswith("COMPOSITE_")
    rows = RunIndex(config.paths.artifact_root).read_rows()
    assert len(rows) == 1
    assert rows[0]["kind"] == "backtest"
    assert rows[0]["factor_ids"] == [composite_id]

    by_composite = RunIndex(config.paths.artifact_root).search(factor_id=composite_id, kind="backtest")
    assert len(by_composite) == 1


def test_multi_factor_backtest_workflow_payload_failure_leaves_no_dangling_run_row(
    monkeypatch, tmp_path: Path
) -> None:
    # PF-F3: before this fix, the COMPOSITE_ RunIndex row was appended BEFORE
    # _multi_factor_backtest_payload() was built; a payload-construction
    # failure still triggered cleanup of the synthesized factor definition
    # and overlay, but left the just-recorded RunIndex row dangling (a
    # success-shaped row for a run that actually failed). Recording now sits
    # LAST, immediately before return, so a payload failure records nothing.
    create_demo_workspace(tmp_path / "demo")
    config = QuantForgeConfig().resolve(tmp_path / "demo")
    assert RunIndex(config.paths.artifact_root).read_rows() == []

    def _boom(*args, **kwargs):
        raise RuntimeError("payload assembly blew up")

    monkeypatch.setattr(web_api, "_multi_factor_backtest_payload", _boom)

    with pytest.raises(RuntimeError, match="payload assembly blew up"):
        run_multi_factor_backtest_workflow(
            config,
            factor_refs=[
                {"factor_id": "FTR_DEMO_SMALL_CAP", "direction": 1},
                {"factor_id": "FTR_DEMO_MOMENTUM", "direction": -1},
            ],
            synthesis={"method": "equal_weight", "params": {}},
            standardization={"method": "zscore", "params": {}},
            parameters={"holding_days": 5},
            rd_config=_rd_config(config),
        )

    # Cleanup ran (the synthesized COMPOSITE_ definition does not survive)
    # AND the run index gained zero new rows.
    remaining_ids = {factor.factor_id for factor in FactorRepository(config.paths.factor_root).list()}
    assert not any(factor_id.startswith("COMPOSITE_") for factor_id in remaining_ids)
    assert RunIndex(config.paths.artifact_root).read_rows() == []

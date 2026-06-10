from __future__ import annotations

from pathlib import Path

import pytest

from quant_forge.agent_workspace import desktop_chrome_rd_prompt
from quant_forge.agent_workspace.tools import AgentWorkspaceTools
from quant_forge.data.local import create_demo_workspace
from quant_forge.factor_library.repository import FactorRepository
from quant_forge.mcp.read_models import list_available_fields, list_available_operators


def test_mcp_catalogs_are_read_only_shapes() -> None:
    fields = list_available_fields()
    operators = list_available_operators()
    assert {"name": "market_cap", "description": "Point-in-time market capitalization supplied by local data."} in fields
    assert any(operator["name"] == "rank" for operator in operators)
    assert any(operator["name"] == "delta" for operator in operators)
    assert any(operator["name"] == "correlation" for operator in operators)


def test_desktop_chrome_contract_is_prompt_only() -> None:
    prompt = desktop_chrome_rd_prompt()

    assert "Google Chrome" in prompt
    assert "fallback_used" in prompt
    assert "cgWindowNotFound" in prompt


def test_agent_tools_do_not_mutate_factor_root_directly(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    tools = AgentWorkspaceTools(
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"],
    )
    proposed = tools.propose_factor_from_idea("small non-st stocks perform better")
    decision = tools.request_promotion(proposed["factor_id"], "active")
    assert decision["status"] == "requires_user_decision"
    with pytest.raises(FileNotFoundError):
        FactorRepository(paths["factor_root"]).get(proposed["factor_id"])


def test_agent_tools_treat_factor_value_cache_root_as_read_only_without_overlay(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    factor_values_root = tmp_path / "factor_values"
    tools = AgentWorkspaceTools(
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"],
        factor_values_root=factor_values_root,
    )

    result = tools.evaluate_factor("FTR_DEMO_SMALL_CAP")

    assert result["observations"] > 0
    assert result["factor_values_write_path"] in (None, "")
    assert not (
        factor_values_root
        / "原始因子"
        / "factor_id=FTR_DEMO_SMALL_CAP"
        / "incremental"
        / "2024.parquet"
    ).exists()


def test_agent_tools_can_write_factor_value_increments_to_overlay(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    factor_values_root = tmp_path / "factor_values_read"
    factor_values_overlay_root = tmp_path / "factor_values_overlay"
    tools = AgentWorkspaceTools(
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        artifact_root=paths["artifact_root"],
        factor_values_root=factor_values_root,
        factor_values_overlay_root=factor_values_overlay_root,
    )

    result = tools.evaluate_factor("FTR_DEMO_SMALL_CAP")

    overlay_factor_dir = factor_values_overlay_root / "原始因子" / "factor_id=FTR_DEMO_SMALL_CAP"
    assert result["observations"] > 0
    assert result["factor_values_write_path"] == str(overlay_factor_dir)
    assert (overlay_factor_dir / "incremental" / "2024.parquet").exists()
    assert not (factor_values_root / "factor_id=FTR_DEMO_SMALL_CAP" / "incremental").exists()

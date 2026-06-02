"""Agent-facing tools that route through typed workbench services."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from quant_forge.factor_library.repository import parse_idea_to_definition
from quant_forge.mcp import read_models
from quant_forge.workbench.service import WorkbenchService


class AgentWorkspaceTools:
    def __init__(
        self,
        *,
        factor_root: Path,
        data_root: Path,
        artifact_root: Path,
        factor_values_root: Path | None = None,
    ) -> None:
        self.workbench = WorkbenchService(
            factor_root=factor_root,
            data_root=data_root,
            artifact_root=artifact_root,
            factor_values_root=factor_values_root,
        )
        self.factor_root = factor_root
        self.factor_values_root = factor_values_root
        self.artifact_root = artifact_root

    def read_catalog(self) -> dict[str, object]:
        return {
            "fields": read_models.list_available_fields(),
            "operators": read_models.list_available_operators(),
            "factors": read_models.list_factors(self.factor_root, self.factor_values_root),
            "artifacts": read_models.list_artifacts(self.artifact_root),
        }

    def propose_factor_from_idea(self, text: str) -> dict[str, object]:
        factor = parse_idea_to_definition(text)
        return asdict(factor)

    def evaluate_factor(self, factor_id: str) -> dict[str, object]:
        result = self.workbench.evaluate(factor_id)
        payload = asdict(result)
        payload["artifact_path"] = str(result.artifact_path)
        return payload

    def request_promotion(self, factor_id: str, target_status: str) -> dict[str, str]:
        return {
            "factor_id": factor_id,
            "target_status": target_status,
            "status": "requires_user_decision",
            "reason": "Agents cannot directly promote factors in the public workbench.",
        }

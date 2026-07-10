"""Agent-facing tools that route through typed workbench services."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from quant_forge.core.contracts import SampleSplitSpec, SimulationProfile, TransactionCostModel
from quant_forge.factor_library.repository import parse_idea_to_definition
from quant_forge.integrations.dry_run import run_translate_prescreen
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
        factor_values_overlay_root: Path | None = None,
        factor_values_manifest_root: Path | None = None,
        simulation_profile: SimulationProfile | None = None,
        transaction_costs: TransactionCostModel | None = None,
        sample_splits: tuple[SampleSplitSpec, ...] | None = None,
        horizon_days_matrix: tuple[int, ...] | None = None,
    ) -> None:
        self.workbench = WorkbenchService(
            factor_root=factor_root,
            data_root=data_root,
            artifact_root=artifact_root,
            factor_values_root=factor_values_root,
            factor_values_overlay_root=factor_values_overlay_root,
            factor_values_manifest_root=factor_values_manifest_root,
            simulation_profile=simulation_profile,
            transaction_costs=transaction_costs,
            sample_splits=sample_splits,
            horizon_days_matrix=horizon_days_matrix,
        )
        self.factor_root = factor_root
        self.factor_values_root = factor_values_root
        self.factor_values_manifest_root = factor_values_manifest_root
        self.artifact_root = artifact_root

    def read_catalog(self) -> dict[str, object]:
        return {
            "fields": read_models.list_available_fields(),
            "operators": read_models.list_available_operators(),
            "factors": read_models.list_factors(
                self.factor_root,
                self.factor_values_root,
                self.factor_values_manifest_root,
            ),
            "artifacts": read_models.list_artifacts(self.artifact_root),
        }

    def propose_factor_from_idea(self, text: str) -> dict[str, object]:
        factor = parse_idea_to_definition(text)
        return asdict(factor)

    def evaluate_factor(self, factor_id: str) -> dict[str, object]:
        result = self.workbench.evaluate(factor_id)
        payload = asdict(result)
        payload["artifact_path"] = str(result.artifact_path)
        payload["factor_values_path"] = str(result.factor_values_path) if result.factor_values_path else None
        payload["factor_values_write_path"] = (
            str(result.factor_values_write_path) if result.factor_values_write_path else None
        )
        return payload

    def request_promotion(self, factor_id: str, target_status: str) -> dict[str, str]:
        return {
            "factor_id": factor_id,
            "target_status": target_status,
            "status": "requires_user_decision",
            "reason": "Agents cannot directly promote factors in the public workbench.",
        }

    def backend_translate_prescreen(
        self,
        backend_id: str,
        factor_id: str,
        *,
        data_region: str | None = None,
        target_region: str | None = None,
    ) -> dict[str, object]:
        """Read-only external-backend dry run: resolve, translate, prescreen.

        FP-D boundary: outward submission is irreversible and stays gated
        behind the human CLI (`qf factor submit --confirm-submit`). This
        method routes through the deliberately submission-free dry-run flow
        in :mod:`quant_forge.integrations.dry_run`, so the agent-facing
        surface has no path to a backend submit call at all.
        """

        return run_translate_prescreen(
            backend_id,
            factor_id,
            factor_root=self.factor_root,
            artifact_root=self.artifact_root,
            data_region=data_region,
            target_region=target_region,
        ).payload

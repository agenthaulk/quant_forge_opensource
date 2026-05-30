"""Thin orchestration layer over public kernel services."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from quant_forge.backtesting.service import run_factor_backtest
from quant_forge.core.contracts import BacktestResult, EvaluationResult, FactorDefinition, SimulationProfile
from quant_forge.evaluation.service import evaluate_factor
from quant_forge.factor_library.repository import FactorRepository, parse_idea_to_definition


class WorkbenchService:
    def __init__(
        self,
        *,
        factor_root: Path,
        data_root: Path,
        artifact_root: Path,
        simulation_profile: SimulationProfile | None = None,
    ) -> None:
        self.factor_root = factor_root
        self.data_root = data_root
        self.artifact_root = artifact_root
        self.simulation_profile = simulation_profile or SimulationProfile()

    def list_factors(self) -> list[FactorDefinition]:
        return FactorRepository(self.factor_root).list()

    def idea_to_factor(self, text: str) -> FactorDefinition:
        factor = parse_idea_to_definition(text)
        FactorRepository(self.factor_root).save(factor)
        return factor

    def evaluate(
        self, factor_id: str, *, horizon_days: int | None = None, simulation_profile: SimulationProfile | None = None
    ) -> EvaluationResult:
        return evaluate_factor(
            factor_id,
            factor_root=self.factor_root,
            data_root=self.data_root,
            artifact_root=self.artifact_root,
            horizon_days=horizon_days,
            simulation_profile=simulation_profile or self.simulation_profile,
        )

    def run_backtest(
        self, factor_id: str, *, top_quantile: float | None = None, holding_days: int | None = None
    ) -> BacktestResult:
        profile = self.simulation_profile
        if top_quantile is not None:
            profile = replace(profile, top_quantile=top_quantile)
        return run_factor_backtest(
            factor_id,
            factor_root=self.factor_root,
            data_root=self.data_root,
            artifact_root=self.artifact_root,
            simulation_profile=profile,
            holding_days=holding_days,
        )

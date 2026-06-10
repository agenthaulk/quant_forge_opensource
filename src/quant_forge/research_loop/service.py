"""Small, decoupled factor research loop."""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from math import ceil
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from quant_forge.backtesting.service import run_factor_backtest
from quant_forge.core.contracts import (
    BacktestResult,
    BacktestSegmentMetric,
    EvaluationResult,
    FactorDefinition,
    SampleSplitSpec,
    SimulationProfile,
    TransactionCostModel,
)
from quant_forge.evaluation.service import evaluate_factor
from quant_forge.factor_engine.formula_parser import SUPPORTED_OPERATORS, inspect_formula
from quant_forge.factor_library.catalog import FactorCatalog, is_precomputed_formula
from quant_forge.factor_library.repository import FactorRepository, parse_idea_to_definition
from quant_forge.research_loop.candidate_gate import evaluate_candidate as evaluate_structured_candidate
from quant_forge.research_loop.context_builder import ResearchContextBuilder
from quant_forge.research_loop.contracts import (
    FactorExperimentPlan,
    FactorExperimentResult as StructuredFactorExperimentResult,
    ResearchContext,
    ResearchTraceEntry,
    StructuredResearchHypothesis,
)
from quant_forge.research_loop.experiment_planner import ExperimentPlanner
from quant_forge.research_loop.feedback_builder import build_feedback
from quant_forge.research_loop.operator_drafts import write_operator_draft_artifacts
from quant_forge.research_loop.trace_store import ResearchTraceStore, utc_timestamp


DEFAULT_QUICK_HORIZON_DAYS = (5, 21)
DEFAULT_QUICK_SAMPLE_SPLITS = (SampleSplitSpec(name="IS", fraction=1.0, score_weight=1.0),)
RD_RESEARCH_STAGE = "research"


@dataclass(frozen=True)
class ResearchObjectiveWeights:
    weighted_split_icir: float = 0.4
    rank_ic_mean: float = 0.25
    rank_icir: float = 0.2
    annualized_return: float = 0.1
    max_drawdown: float = 0.05

    def __post_init__(self) -> None:
        values = (
            self.weighted_split_icir,
            self.rank_ic_mean,
            self.rank_icir,
            self.annualized_return,
            self.max_drawdown,
        )
        if any(value < 0 for value in values):
            raise ValueError("research objective weights must be non-negative")
        if sum(values) <= 0:
            raise ValueError("at least one research objective weight must be positive")


@dataclass(frozen=True)
class ResearchGate:
    min_ic_days: int = 5
    min_coverage: float = 0.5
    min_score: float = 0.0
    min_backtest_periods: int = 1
    min_oos_net_annualized_return: float | None = None
    max_rebalance_rate: float | None = None
    max_turnover_rate: float | None = None
    min_net_return_retention: float | None = None
    max_oos_net_return_decay: float | None = None

    def __post_init__(self) -> None:
        if self.min_ic_days < 0:
            raise ValueError("min_ic_days must be non-negative")
        if not 0 <= self.min_coverage <= 1:
            raise ValueError("min_coverage must be in [0, 1]")
        if self.min_backtest_periods < 0:
            raise ValueError("min_backtest_periods must be non-negative")
        if self.max_rebalance_rate is not None and self.max_rebalance_rate < 0:
            raise ValueError("max_rebalance_rate must be non-negative")
        if self.max_turnover_rate is not None and self.max_turnover_rate < 0:
            raise ValueError("max_turnover_rate must be non-negative")
        if self.min_net_return_retention is not None and self.min_net_return_retention < 0:
            raise ValueError("min_net_return_retention must be non-negative")
        if self.max_oos_net_return_decay is not None and not 0 <= self.max_oos_net_return_decay <= 1:
            raise ValueError("max_oos_net_return_decay must be in [0, 1]")


@dataclass(frozen=True)
class ResearchDeduplicationConfig:
    enabled: bool = True
    formula_fingerprint: bool = True
    result_signature: bool = True
    candidate_diversity: bool = True
    result_precision: int = 6
    recent_trace_limit: int = 500
    max_same_shape_per_run: int = 2

    def __post_init__(self) -> None:
        if self.result_precision < 0:
            raise ValueError("deduplication.result_precision must be non-negative")
        if self.recent_trace_limit < 0:
            raise ValueError("deduplication.recent_trace_limit must be non-negative")
        if self.max_same_shape_per_run < 1:
            raise ValueError("deduplication.max_same_shape_per_run must be positive")


@dataclass(frozen=True)
class ResearchHypothesis:
    text: str
    rationale: str
    source: str = "local"
    source_detail: str = ""
    parameter_search_fallback: bool = False
    formula_dsl: str = ""
    input_fields: tuple[str, ...] = ()
    expected_direction: str = "positive"
    universe_constraints: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "input_fields", _string_tuple(self.input_fields))
        object.__setattr__(self, "universe_constraints", _string_tuple(self.universe_constraints))


@dataclass(frozen=True)
class ResearchGenerationMetadata:
    source: str = "local_hypothesis"
    provider: str = "rule"
    model: str = "deterministic"
    raw_response: str = ""


class HypothesisGenerator(Protocol):
    def generate(
        self, seed: FactorDefinition, *, objective: str, max_candidates: int
    ) -> tuple[ResearchHypothesis, ...]:
        """Generate bounded, human-readable factor hypotheses."""


@dataclass(frozen=True)
class ResearchSelfReview:
    source: str
    summary: str
    strengths: tuple[str, ...]
    risks: tuple[str, ...]
    next_hypotheses: tuple[str, ...]
    normalization_warnings: tuple[str, ...] = ()


class ResearchReviewGenerator(Protocol):
    def review(
        self,
        *,
        seed: FactorDefinition,
        candidate: FactorDefinition,
        evaluation: EvaluationResult,
        backtest: BacktestResult,
        split_weighted_icir: float,
        score: float,
        gate_passed: bool,
        gate_reasons: tuple[str, ...],
    ) -> ResearchSelfReview:
        """Review one candidate result and propose bounded next-step hypotheses."""


class LocalHypothesisGenerator:
    """Deterministic public hypothesis generator for local smoke research."""

    def metadata(self) -> ResearchGenerationMetadata:
        return ResearchGenerationMetadata()

    def generate(
        self, seed: FactorDefinition, *, objective: str, max_candidates: int
    ) -> tuple[ResearchHypothesis, ...]:
        horizon = f"{seed.horizon_days}日"
        hypotheses = (
            ResearchHypothesis(
                text=f"非ST的小市值股票在未来{horizon}表现更好",
                rationale="Retest a small-cap thesis with the public non-ST universe filter.",
                source="financial_analyst",
                formula_dsl="-rank(market_cap)",
                input_fields=("market_cap",),
                expected_direction="positive",
                universe_constraints=("非ST",),
            ),
            ResearchHypothesis(
                text=f"非ST的动量股票在未来{horizon}表现更好",
                rationale="Compare the seed against a simple recent-momentum alternative.",
                source="financial_analyst",
                formula_dsl="rank(return_5d)",
                input_fields=("return_5d",),
                expected_direction="positive",
                universe_constraints=("非ST",),
            ),
            ResearchHypothesis(
                text=f"非ST的低波动股票在未来{horizon}表现更好",
                rationale="Compare the seed against a simple defensive low-volatility alternative.",
                source="financial_analyst",
                formula_dsl="-rank(volatility_5d)",
                input_fields=("volatility_5d",),
                expected_direction="positive",
                universe_constraints=("非ST",),
            ),
        )
        seed_fingerprint = factor_formula_fingerprint(seed)
        unique = tuple(
            hypothesis
            for hypothesis in hypotheses
            if _hypothesis_formula_fingerprint(hypothesis, seed.horizon_days) != seed_fingerprint
        )
        return (unique or hypotheses)[:max_candidates]


class LocalSelfReviewGenerator:
    """Deterministic self-review adapter for the public branch."""

    def review(
        self,
        *,
        seed: FactorDefinition,
        candidate: FactorDefinition,
        evaluation: EvaluationResult,
        backtest: BacktestResult,
        split_weighted_icir: float,
        score: float,
        gate_passed: bool,
        gate_reasons: tuple[str, ...],
    ) -> ResearchSelfReview:
        strengths: list[str] = []
        risks: list[str] = []
        next_hypotheses: list[str] = []

        if evaluation.rank_ic_mean > 0:
            strengths.append("positive whole-sample Rank IC")
        else:
            risks.append("whole-sample Rank IC is not positive")
        if split_weighted_icir > 0:
            strengths.append("positive weighted split ICIR")
        else:
            risks.append("weighted split ICIR is not positive")
        if backtest.net_long_short_sharpe > 0:
            strengths.append("positive net long-short Sharpe")
        else:
            risks.append("net long-short Sharpe is not positive")
        if backtest.rebalance_rate > 0.8:
            risks.append("high rebalance rate")
            next_hypotheses.append(f"smooth or slow down {candidate.name} to reduce rebalance rate")
        if backtest.turnover_rate > 1.5:
            risks.append("high turnover rate")
            next_hypotheses.append(f"smooth or slow down {candidate.name} to reduce turnover rate")
        if _cost_sensitive(backtest):
            risks.append("net performance is sensitive to transaction costs")
        if backtest.net_max_drawdown < -0.2:
            risks.append("large net drawdown in lightweight backtest")
        if _oos_decay(evaluation):
            risks.append("OOS2 ICIR decays versus IS")
            next_hypotheses.append(f"test a simpler or more robust variant of {candidate.name}")
        if _oos_net_decay(backtest):
            risks.append("OOS net return decays versus IS")
            next_hypotheses.append(f"validate {candidate.name} on a later OOS period")
        if not next_hypotheses:
            next_hypotheses.append(f"compare {candidate.name} against a lower-cost, lower-turnover variant")

        status = "passed" if gate_passed else "did not pass"
        summary = (
            f"{candidate.factor_id} {status} the smoke research gate with score {score:.4f}; "
            f"weighted split ICIR is {split_weighted_icir:.4f}."
        )
        if not gate_passed:
            risks.extend(gate_reasons)
        return ResearchSelfReview(
            source="local_self_review",
            summary=summary,
            strengths=tuple(strengths),
            risks=tuple(dict.fromkeys(risks)),
            next_hypotheses=tuple(dict.fromkeys(next_hypotheses)),
        )


@dataclass(frozen=True)
class ResearchCandidateResult:
    hypothesis: ResearchHypothesis
    factor: FactorDefinition
    evaluation: EvaluationResult
    backtest: BacktestResult
    split_weighted_icir: float
    score: float
    gate_passed: bool
    gate_reasons: tuple[str, ...]
    self_review: ResearchSelfReview
    transitioned_to_candidate: bool = False


@dataclass(frozen=True)
class ResearchSearchTraceEntry:
    stage: str
    rank: int
    survived: bool
    hypothesis_text: str
    factor_id: str
    formula: str
    simulation_profile: SimulationProfile
    split_weighted_icir: float
    score: float


@dataclass(frozen=True)
class ResearchLoopResult:
    rd_stage: str
    seed_factor_id: str
    objective: str
    objective_weights: ResearchObjectiveWeights
    gate: ResearchGate
    candidates: tuple[ResearchCandidateResult, ...]
    accepted_candidate_ids: tuple[str, ...]
    generation: ResearchGenerationMetadata = field(default_factory=ResearchGenerationMetadata)
    search_trace: tuple[ResearchSearchTraceEntry, ...] = ()
    blocked_plans: tuple[StructuredFactorExperimentResult, ...] = ()
    trace_root: Path | None = None
    report_path: Path | None = None
    workflow_type: str = "research"
    deduplication: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class _ResearchTrial:
    hypothesis: ResearchHypothesis
    factor: FactorDefinition
    simulation_profile: SimulationProfile
    plan: FactorExperimentPlan | None = None


@dataclass(frozen=True)
class _ScoredTrial:
    trial: _ResearchTrial
    evaluation: EvaluationResult
    backtest: BacktestResult
    split_weighted_icir: float
    score: float


class ResearchLoopService:
    def __init__(
        self,
        *,
        factor_root: Path,
        data_root: Path,
        artifact_root: Path,
        factor_values_root: Path | None = None,
        factor_values_overlay_root: Path | None = None,
        factor_values_manifest_root: Path | None = None,
        top_quantile: float | None = None,
        simulation_profile: SimulationProfile | None = None,
        simulation_profiles: tuple[SimulationProfile, ...] | None = None,
        horizon_days_matrix: tuple[int, ...] | None = None,
        sample_splits: tuple[SampleSplitSpec, ...] | None = None,
        parameter_search_enabled: bool = False,
        parameter_search_method: str = "full_grid",
        parameter_search_keep_ratio: float = 0.34,
        parameter_search_min_survivors: int = 2,
        quick_horizon_days_matrix: tuple[int, ...] | None = None,
        quick_sample_splits: tuple[SampleSplitSpec, ...] | None = None,
        transaction_costs: TransactionCostModel | None = None,
        hypothesis_generator: HypothesisGenerator | None = None,
        review_generator: ResearchReviewGenerator | None = None,
        trace_store: ResearchTraceStore | None = None,
        experiment_planner: ExperimentPlanner | None = None,
        deduplication: ResearchDeduplicationConfig | None = None,
    ) -> None:
        self.factor_root = factor_root
        self.data_root = data_root
        self.artifact_root = artifact_root
        self.factor_values_root = factor_values_root
        self.factor_values_overlay_root = factor_values_overlay_root
        self.factor_values_manifest_root = factor_values_manifest_root
        profile = simulation_profile or SimulationProfile()
        if top_quantile is not None:
            profile = replace(profile, top_quantile=top_quantile)
        self.simulation_profile = profile
        self.simulation_profiles = simulation_profiles or (profile,)
        if not self.simulation_profiles:
            raise ValueError("research loop requires at least one simulation profile")
        self.horizon_days_matrix = horizon_days_matrix
        self.sample_splits = sample_splits
        self.parameter_search_enabled = parameter_search_enabled
        self.parameter_search_method = parameter_search_method
        self.parameter_search_keep_ratio = parameter_search_keep_ratio
        self.parameter_search_min_survivors = parameter_search_min_survivors
        self.quick_horizon_days_matrix = quick_horizon_days_matrix or DEFAULT_QUICK_HORIZON_DAYS
        self.quick_sample_splits = quick_sample_splits or DEFAULT_QUICK_SAMPLE_SPLITS
        self.transaction_costs = transaction_costs or TransactionCostModel()
        _validate_search_settings(
            enabled=parameter_search_enabled,
            method=parameter_search_method,
            keep_ratio=parameter_search_keep_ratio,
            min_survivors=parameter_search_min_survivors,
            quick_horizon_days_matrix=self.quick_horizon_days_matrix,
            quick_sample_splits=self.quick_sample_splits,
        )
        self.hypothesis_generator = hypothesis_generator or LocalHypothesisGenerator()
        self.review_generator = review_generator or LocalSelfReviewGenerator()
        self.trace_store = trace_store or ResearchTraceStore(self.artifact_root / "research_loop")
        self.experiment_planner = experiment_planner or ExperimentPlanner()
        self.deduplication = deduplication or ResearchDeduplicationConfig()

    def run_once(
        self,
        seed_factor_id: str,
        *,
        objective: str = "balanced",
        max_candidates: int = 3,
        weights: ResearchObjectiveWeights | None = None,
        gate: ResearchGate | None = None,
        hypotheses: tuple[ResearchHypothesis, ...] | None = None,
    ) -> ResearchLoopResult:
        if max_candidates < 1 or max_candidates > 10:
            raise ValueError("max_candidates must be between 1 and 10")
        repo = FactorRepository(self.factor_root)
        catalog = FactorCatalog(
            self.factor_root,
            factor_values_root=self.factor_values_root,
            factor_values_manifest_root=self.factor_values_manifest_root,
        )
        seed = catalog.get(seed_factor_id)
        objective_weights = weights or objective_weights_for(objective)
        candidate_gate = gate or ResearchGate()
        run_id = _research_run_id(seed_factor_id)
        self.trace_store.ensure_run_dirs(run_id)
        self.trace_store.write_run(run_id, {"run_id": run_id, "status": "running", "started_at": utc_timestamp()})
        context = ResearchContextBuilder(
            factor_root=self.factor_root,
            data_root=self.data_root,
            factor_values_root=self.factor_values_root,
            factor_values_manifest_root=self.factor_values_manifest_root,
            trace_store=self.trace_store,
        ).build(objective=objective, seed_factor_ids=(seed_factor_id,))
        self.trace_store.write_context(run_id, context)
        planned = hypotheses or _generate_hypotheses(
            self.hypothesis_generator,
            seed,
            context=context,
            objective=objective,
            max_candidates=max_candidates,
        )
        if not planned:
            if not self.parameter_search_enabled:
                raise ValueError("research loop requires at least one hypothesis")
            planned = (_parameter_search_fallback_hypothesis(seed),)
            generation = ResearchGenerationMetadata(
                source="parameter_search",
                provider="local",
                model="bounded_profile_search",
            )
        else:
            generation = _hypothesis_generator_metadata(self.hypothesis_generator)
        self.trace_store.write_config_snapshot(
            run_id,
            {
                "objective": objective,
                "max_candidates": max_candidates,
                "generation": _generation_snapshot(generation),
                "parameter_search_enabled": self.parameter_search_enabled,
                "parameter_search_method": self.parameter_search_method,
                "deduplication": _deduplication_snapshot(self.deduplication),
            },
        )

        trials: list[_ResearchTrial] = []
        blocked_plans: list[StructuredFactorExperimentResult] = []
        blocked_due_to_missing_llm_formula = False
        formula_index = (
            _formula_fingerprint_index(catalog.list())
            if self.deduplication.enabled and self.deduplication.formula_fingerprint
            else {}
        )
        result_index = (
            _recent_result_signature_index(
                self.trace_store,
                precision=self.deduplication.result_precision,
                limit=self.deduplication.recent_trace_limit,
            )
            if self.deduplication.enabled and self.deduplication.result_signature
            else {}
        )
        shape_counts: Counter[str] = Counter()
        dedup_summary: dict[str, object] = {
            "enabled": self.deduplication.enabled,
            "formula_skipped": 0,
            "diversity_skipped": 0,
            "result_duplicates": 0,
            "skipped_plans": [],
        }
        for lane_index, raw_hypothesis in enumerate(planned[:max_candidates], start=1):
            hypothesis = _normalize_llm_research_hypothesis(raw_hypothesis, generation)
            if _llm_formula_required_but_missing(hypothesis, generation):
                blocked_due_to_missing_llm_formula = True
                structured = _structured_hypothesis_for_missing_formula(hypothesis, generation, lane_index=lane_index)
                plan = self.experiment_planner.plan(structured, context)
                self._trace_plan(run_id, structured, plan)
                blocked_result = _blocked_structured_result(plan)
                blocked_plans.append(blocked_result)
                self._trace_structured_result(run_id, blocked_result, phase="plan_blocked")
                continue
            draft = (
                seed
                if hypothesis.parameter_search_fallback
                else _candidate_from_hypothesis(hypothesis, seed.horizon_days)
            )
            structured = _structured_hypothesis_from_candidate(hypothesis, draft, generation, lane_index=lane_index)
            plan = self.experiment_planner.plan(structured, context)
            self._trace_plan(run_id, structured, plan)
            if plan.status != "ready":
                blocked_result = _blocked_structured_result(
                    plan,
                    artifact_refs=_operator_draft_refs(self.artifact_root, plan),
                )
                blocked_plans.append(blocked_result)
                self._trace_structured_result(run_id, blocked_result, phase="plan_blocked")
                continue
            planned_candidate = (
                seed if hypothesis.parameter_search_fallback else replace(
                    draft,
                    formula=plan.formula_dsl,
                    universe_filters=plan.universe_filters,
                )
            )
            duplicate_plan = _deduplicate_plan(
                plan,
                planned_candidate,
                formula_index=formula_index,
                shape_counts=shape_counts,
                config=self.deduplication,
                allow_existing=hypothesis.parameter_search_fallback,
            )
            if duplicate_plan is not None:
                duplicate_result = _blocked_structured_result(duplicate_plan)
                blocked_plans.append(duplicate_result)
                _record_dedup_skip(dedup_summary, duplicate_plan)
                self._trace_structured_result(run_id, duplicate_result, phase="plan_blocked")
                continue
            candidate = (
                seed
                if hypothesis.parameter_search_fallback
                else _load_or_save_candidate(repo, planned_candidate)
            )
            for profile in self.simulation_profiles:
                trials.append(_ResearchTrial(hypothesis, candidate, profile, plan))
        if not trials and blocked_plans and self.parameter_search_enabled and not blocked_due_to_missing_llm_formula:
            fallback = _parameter_search_fallback_hypothesis(seed)
            structured = _structured_hypothesis_from_candidate(
                fallback,
                seed,
                generation,
                lane_index=len(planned[:max_candidates]) + 1,
            )
            plan = self.experiment_planner.plan(structured, context)
            self._trace_plan(run_id, structured, plan)
            if plan.status == "ready":
                for profile in self.simulation_profiles:
                    trials.append(_ResearchTrial(fallback, seed, profile, plan))
            else:
                blocked_result = _blocked_structured_result(
                    plan,
                    artifact_refs=_operator_draft_refs(self.artifact_root, plan),
                )
                blocked_plans.append(blocked_result)
                self._trace_structured_result(run_id, blocked_result, phase="plan_blocked")

        search_trace, final_trials, failed_quick_results = self._select_final_trials(trials, objective_weights)
        blocked_plans.extend(failed_quick_results)
        for failed in failed_quick_results:
            self._trace_structured_result(run_id, failed, phase="experiment_failed")
        results: list[ResearchCandidateResult] = []
        for trial in final_trials:
            try:
                candidate_result = self._evaluate_final_trial(
                    repo,
                    seed,
                    trial,
                    objective_weights,
                    candidate_gate,
                    result_signature_index=result_index,
                )
            except Exception as exc:
                failed = _failed_structured_result(trial.plan, error=str(exc))
                blocked_plans.append(failed)
                self._trace_structured_result(run_id, failed, phase="experiment_failed")
                continue
            if any(reason.startswith("duplicate result signature") for reason in candidate_result.gate_reasons):
                dedup_summary["result_duplicates"] = int(dedup_summary["result_duplicates"]) + 1
            results.append(candidate_result)
            self._trace_candidate_result(run_id, candidate_result, trial.plan)
        results = sorted(results, key=lambda result: result.score, reverse=True)
        accepted = tuple(
            dict.fromkeys(
                result.factor.factor_id
                for result in results
                if result.gate_passed and result.factor.status in {"candidate", "active"}
                and not (result.hypothesis.parameter_search_fallback and result.factor.factor_id == seed_factor_id)
            )
        )
        result = ResearchLoopResult(
            rd_stage=RD_RESEARCH_STAGE,
            seed_factor_id=seed_factor_id,
            objective=objective,
            objective_weights=objective_weights,
            gate=candidate_gate,
            candidates=tuple(results),
            accepted_candidate_ids=accepted,
            generation=generation,
            search_trace=search_trace,
            blocked_plans=tuple(blocked_plans),
            trace_root=self.trace_store.run_dir(run_id),
            deduplication=dedup_summary,
        )
        from quant_forge.research_loop.reporting import write_research_report

        result = replace(result, report_path=write_research_report(result, self.artifact_root))
        run_status = "completed" if results else ("partial" if blocked_plans else "failed")
        self.trace_store.write_run(
            run_id,
            {
                "run_id": run_id,
                "status": run_status,
                "finished_at": utc_timestamp(),
                "candidate_count": len(result.candidates),
                "blocked_plan_count": len(result.blocked_plans),
                "accepted_candidate_ids": list(result.accepted_candidate_ids),
                "report_path": result.report_path,
            },
        )
        return result

    def _trace_plan(self, run_id: str, hypothesis: StructuredResearchHypothesis, plan: FactorExperimentPlan) -> None:
        self.trace_store.append_trace(
            ResearchTraceEntry(
                run_id=run_id,
                lane_id=plan.plan_id,
                phase="experiment_plan",
                timestamp=utc_timestamp(),
                hypothesis=hypothesis.to_dict(),
                experiment_plan=plan.to_dict(),
                formula_dsl=plan.formula_dsl,
                inputs=plan.inputs,
                universe_filters=plan.universe_filters,
                field_resolution=plan.field_resolution,
                operator_validation=plan.operator_validation,
                unresolved_items=plan.blocking_reasons,
            )
        )

    def _trace_structured_result(
        self,
        run_id: str,
        result: StructuredFactorExperimentResult,
        *,
        phase: str,
    ) -> None:
        feedback = build_feedback(result)
        self.trace_store.append_trace(
            ResearchTraceEntry(
                run_id=run_id,
                lane_id=result.plan.plan_id,
                phase=phase,
                timestamp=utc_timestamp(),
                experiment_plan=result.plan.to_dict(),
                candidate_ref=result.candidate_ref,
                formula_dsl=result.plan.formula_dsl,
                inputs=result.plan.inputs,
                universe_filters=result.plan.universe_filters,
                field_resolution=result.plan.field_resolution,
                operator_validation=result.plan.operator_validation,
                evaluation_summary=result.evaluation_metrics,
                backtest_summary=result.backtest_metrics,
                correlation_summary=result.correlation_summary,
                objective_score=result.objective_score.to_dict() if result.objective_score else {},
                gate_decision=result.gate_decision.to_dict() if result.gate_decision else {},
                feedback=feedback.to_dict(),
                next_hypothesis_hint=feedback.next_hypothesis_hint,
                unresolved_items=feedback.unresolved_items,
                artifact_refs=result.artifact_refs,
                error=result.error,
            )
        )

    def _trace_candidate_result(
        self,
        run_id: str,
        candidate: ResearchCandidateResult,
        plan: FactorExperimentPlan | None,
    ) -> None:
        structured = _structured_result_from_candidate(candidate, plan)
        self._trace_structured_result(run_id, structured, phase="experiment_result")

    def _select_final_trials(
        self, trials: list[_ResearchTrial], objective_weights: ResearchObjectiveWeights
    ) -> tuple[tuple[ResearchSearchTraceEntry, ...], list[_ResearchTrial], list[StructuredFactorExperimentResult]]:
        if (
            not self.parameter_search_enabled
            or self.parameter_search_method == "full_grid"
            or len(trials) <= self.parameter_search_min_survivors
        ):
            return (), trials, []
        if self.parameter_search_method != "successive_halving":
            raise ValueError("parameter_search_method must be full_grid or successive_halving")

        scored_trials: list[_ScoredTrial] = []
        failed_results: list[StructuredFactorExperimentResult] = []
        for trial in trials:
            try:
                scored_trials.append(
                    self._score_trial(
                        trial,
                        objective_weights,
                        horizon_days_matrix=self.quick_horizon_days_matrix,
                        sample_splits=self.quick_sample_splits,
                    )
                )
            except Exception as exc:
                failed_results.append(_failed_structured_result(trial.plan, error=str(exc)))
        if not scored_trials:
            return (), [], failed_results
        ranked = sorted(scored_trials, key=_scored_trial_sort_key, reverse=True)
        survivor_count = _survivor_count(
            len(ranked), keep_ratio=self.parameter_search_keep_ratio, min_survivors=self.parameter_search_min_survivors
        )
        survivors = ranked[:survivor_count]
        survivor_keys = {_trial_key(item.trial) for item in survivors}
        trace = tuple(
            ResearchSearchTraceEntry(
                stage="quick",
                rank=index,
                survived=_trial_key(scored.trial) in survivor_keys,
                hypothesis_text=scored.trial.hypothesis.text,
                factor_id=scored.trial.factor.factor_id,
                formula=scored.trial.factor.formula,
                simulation_profile=scored.trial.simulation_profile,
                split_weighted_icir=scored.split_weighted_icir,
                score=scored.score,
            )
            for index, scored in enumerate(ranked, start=1)
        )
        return trace, [item.trial for item in survivors], failed_results

    def _score_trial(
        self,
        trial: _ResearchTrial,
        objective_weights: ResearchObjectiveWeights,
        *,
        horizon_days_matrix: tuple[int, ...] | None,
        sample_splits: tuple[SampleSplitSpec, ...] | None,
    ) -> _ScoredTrial:
        evaluation = evaluate_factor(
            trial.factor.factor_id,
            factor_root=self.factor_root,
            data_root=self.data_root,
            artifact_root=self.artifact_root,
            horizon_days=trial.factor.horizon_days,
            horizon_days_matrix=horizon_days_matrix,
            sample_splits=sample_splits,
            simulation_profile=trial.simulation_profile,
            factor_values_root=self.factor_values_root,
            factor_values_overlay_root=self.factor_values_overlay_root,
            factor_values_manifest_root=self.factor_values_manifest_root,
        )
        backtest = run_factor_backtest(
            trial.factor.factor_id,
            factor_root=self.factor_root,
            data_root=self.data_root,
            artifact_root=self.artifact_root,
            holding_days=trial.factor.horizon_days,
            simulation_profile=trial.simulation_profile,
            transaction_costs=self.transaction_costs,
            sample_splits=sample_splits,
            factor_values_root=self.factor_values_root,
            factor_values_overlay_root=self.factor_values_overlay_root,
            factor_values_manifest_root=self.factor_values_manifest_root,
        )
        split_weighted_icir = weighted_split_icir(evaluation)
        score = score_candidate(evaluation, backtest, objective_weights, split_weighted_icir)
        return _ScoredTrial(trial, evaluation, backtest, split_weighted_icir, score)

    def _evaluate_final_trial(
        self,
        repo: FactorRepository,
        seed: FactorDefinition,
        trial: _ResearchTrial,
        objective_weights: ResearchObjectiveWeights,
        candidate_gate: ResearchGate,
        result_signature_index: dict[str, str] | None = None,
    ) -> ResearchCandidateResult:
        scored = self._score_trial(
            trial,
            objective_weights,
            horizon_days_matrix=self.horizon_days_matrix,
            sample_splits=self.sample_splits,
        )
        gate_passed, gate_reasons = apply_gate(scored.evaluation, scored.backtest, scored.score, candidate_gate)
        result_signature = (
            _result_signature_from_scored(scored, precision=self.deduplication.result_precision)
            if result_signature_index is not None
            else ""
        )
        if result_signature_index is not None and result_signature:
            existing_factor_id = result_signature_index.get(result_signature)
            if existing_factor_id and existing_factor_id != trial.factor.factor_id:
                gate_passed = False
                gate_reasons = (
                    *tuple(reason for reason in gate_reasons if reason != "passed smoke research gate"),
                    f"duplicate result signature matches {existing_factor_id}",
                )
            else:
                result_signature_index[result_signature] = trial.factor.factor_id
        candidate = repo.get(trial.factor.factor_id)
        transitioned_to_candidate = False
        if gate_passed:
            if candidate.factor_id == seed.factor_id:
                pass
            elif candidate.status == "draft":
                candidate = repo.promote(
                    candidate.factor_id,
                    "candidate",
                    "Research loop smoke gate passed; active promotion still requires user decision.",
                )
                transitioned_to_candidate = True
            elif candidate.status in {"candidate", "active"}:
                pass
            else:
                gate_passed = False
                gate_reasons = (*gate_reasons, f"existing {candidate.status} status requires explicit user decision")
        elif candidate.status != "draft":
            gate_reasons = (*gate_reasons, f"existing {candidate.status} status preserved")
        self_review = self.review_generator.review(
            seed=seed,
            candidate=candidate,
            evaluation=scored.evaluation,
            backtest=scored.backtest,
            split_weighted_icir=scored.split_weighted_icir,
            score=scored.score,
            gate_passed=gate_passed,
            gate_reasons=gate_reasons,
        )
        return ResearchCandidateResult(
            hypothesis=trial.hypothesis,
            factor=candidate,
            evaluation=scored.evaluation,
            backtest=scored.backtest,
            split_weighted_icir=scored.split_weighted_icir,
            score=scored.score,
            gate_passed=gate_passed,
            gate_reasons=gate_reasons,
            self_review=self_review,
            transitioned_to_candidate=transitioned_to_candidate,
        )


def objective_weights_for(objective: str) -> ResearchObjectiveWeights:
    normalized = objective.strip().lower()
    if normalized in {"rank_ic", "ic"}:
        return ResearchObjectiveWeights(
            weighted_split_icir=0.2, rank_ic_mean=0.6, rank_icir=0.1, annualized_return=0.1, max_drawdown=0.0
        )
    if normalized in {"rank_icir", "icir"}:
        return ResearchObjectiveWeights(
            weighted_split_icir=0.5, rank_ic_mean=0.1, rank_icir=0.3, annualized_return=0.05, max_drawdown=0.05
        )
    if normalized in {"annualized_return", "return", "backtest_return"}:
        return ResearchObjectiveWeights(
            weighted_split_icir=0.2, rank_ic_mean=0.15, rank_icir=0.15, annualized_return=0.4, max_drawdown=0.1
        )
    if normalized == "balanced":
        return ResearchObjectiveWeights()
    raise ValueError("objective must be one of: rank_ic, rank_icir, annualized_return, balanced")


def score_candidate(
    evaluation: EvaluationResult,
    backtest: BacktestResult,
    weights: ResearchObjectiveWeights,
    split_weighted_icir: float | None = None,
) -> float:
    split_component = (
        split_weighted_icir if split_weighted_icir is not None else weighted_split_icir(evaluation)
    ) / 10.0
    normalized_icir = evaluation.rank_icir / 10.0
    return float(
        split_component * weights.weighted_split_icir
        + evaluation.rank_ic_mean * weights.rank_ic_mean
        + normalized_icir * weights.rank_icir
        + backtest.net_annualized_return * weights.annualized_return
        + backtest.net_max_drawdown * weights.max_drawdown
    )


def weighted_split_icir(evaluation: EvaluationResult) -> float:
    if not evaluation.split_metrics:
        raise ValueError("weighted split ICIR requires evaluation split metrics")
    weighted = [
        (metric.rank_icir, metric.score_weight)
        for metric in evaluation.split_metrics
        if metric.ic_days > 0 and metric.score_weight > 0
    ]
    if not weighted:
        return 0.0
    total_weight = sum(weight for _, weight in weighted)
    return float(sum(value * weight for value, weight in weighted) / total_weight)


def apply_gate(
    evaluation: EvaluationResult, backtest: BacktestResult, score: float, gate: ResearchGate
) -> tuple[bool, tuple[str, ...]]:
    reasons: list[str] = []
    if evaluation.ic_days < gate.min_ic_days:
        reasons.append(f"ic_days {evaluation.ic_days} < {gate.min_ic_days}")
    if evaluation.coverage < gate.min_coverage:
        reasons.append(f"coverage {evaluation.coverage:.4f} < {gate.min_coverage:.4f}")
    if backtest.periods < gate.min_backtest_periods:
        reasons.append(f"backtest_periods {backtest.periods} < {gate.min_backtest_periods}")
    if score < gate.min_score:
        reasons.append(f"score {score:.6f} < {gate.min_score:.6f}")
    if gate.min_oos_net_annualized_return is not None:
        for metric in _oos_segments(backtest):
            if metric.net_annualized_return < gate.min_oos_net_annualized_return:
                reasons.append(
                    f"{metric.name} net_annualized_return {metric.net_annualized_return:.6f} "
                    f"< {gate.min_oos_net_annualized_return:.6f}"
                )
    if gate.max_rebalance_rate is not None and backtest.rebalance_rate > gate.max_rebalance_rate:
        reasons.append(
            f"rebalance_rate {backtest.rebalance_rate:.6f} "
            f"> {gate.max_rebalance_rate:.6f}"
        )
    if gate.max_turnover_rate is not None and backtest.turnover_rate > gate.max_turnover_rate:
        reasons.append(f"turnover_rate {backtest.turnover_rate:.6f} > {gate.max_turnover_rate:.6f}")
    if gate.min_net_return_retention is not None:
        retention = _net_return_retention(backtest)
        if retention < gate.min_net_return_retention:
            reasons.append(f"net_return_retention {retention:.6f} < {gate.min_net_return_retention:.6f}")
    if gate.max_oos_net_return_decay is not None and _oos_net_decay(backtest, gate.max_oos_net_return_decay):
        reasons.append(f"OOS net return decay exceeds {gate.max_oos_net_return_decay:.6f}")
    if not reasons:
        reasons.append("passed smoke research gate")
    return len(reasons) == 1 and reasons[0] == "passed smoke research gate", tuple(reasons)


def _validate_search_settings(
    *,
    enabled: bool,
    method: str,
    keep_ratio: float,
    min_survivors: int,
    quick_horizon_days_matrix: tuple[int, ...],
    quick_sample_splits: tuple[SampleSplitSpec, ...],
) -> None:
    if method not in {"full_grid", "successive_halving"}:
        raise ValueError("parameter_search_method must be full_grid or successive_halving")
    if not 0.0 < keep_ratio <= 1.0:
        raise ValueError("parameter_search_keep_ratio must be in (0, 1]")
    if min_survivors < 1:
        raise ValueError("parameter_search_min_survivors must be positive")
    if enabled and not quick_horizon_days_matrix:
        raise ValueError("quick_horizon_days_matrix must not be empty")
    if any(horizon < 1 for horizon in quick_horizon_days_matrix):
        raise ValueError("quick_horizon_days_matrix values must be positive")
    if enabled and not quick_sample_splits:
        raise ValueError("quick_sample_splits must not be empty")


def _survivor_count(total: int, *, keep_ratio: float, min_survivors: int) -> int:
    if total < 1:
        return 0
    return min(total, max(min_survivors, ceil(total * keep_ratio)))


def _scored_trial_sort_key(scored: _ScoredTrial) -> tuple[float, float, float]:
    return (scored.score, scored.split_weighted_icir, scored.evaluation.rank_ic_mean)


def _trial_key(trial: _ResearchTrial) -> tuple[str, SimulationProfile]:
    return (trial.factor.factor_id, trial.simulation_profile)


def _candidate_from_hypothesis(hypothesis: ResearchHypothesis, horizon_days: int) -> FactorDefinition:
    formula = hypothesis.formula_dsl.strip()
    if formula:
        filters = _universe_filters_from_constraints(hypothesis.universe_constraints)
        name = _safe_factor_name(hypothesis.text)
        digest = hashlib.sha1(f"{name}:{formula}:{filters}:{hypothesis.text}".encode("utf-8")).hexdigest()[:8].upper()
        return FactorDefinition(
            factor_id=f"FTR_{digest}",
            name=name,
            formula=formula,
            status="draft",
            description=hypothesis.rationale or hypothesis.text,
            horizon_days=horizon_days,
            universe_filters=filters,
            source="research_loop",
        )
    parsed = parse_idea_to_definition(hypothesis.text)
    return replace(parsed, horizon_days=horizon_days, source="research_loop")


def _hypothesis_generator_metadata(generator: HypothesisGenerator) -> ResearchGenerationMetadata:
    metadata = getattr(generator, "metadata", None)
    if callable(metadata):
        value = metadata()
        if isinstance(value, ResearchGenerationMetadata):
            return value
    return ResearchGenerationMetadata(source=generator.__class__.__name__)


def _generation_snapshot(generation: ResearchGenerationMetadata) -> dict[str, str]:
    return {
        "source": generation.source,
        "provider": generation.provider,
        "model": generation.model,
    }


def _deduplication_snapshot(config: ResearchDeduplicationConfig) -> dict[str, object]:
    return {
        "enabled": config.enabled,
        "formula_fingerprint": config.formula_fingerprint,
        "result_signature": config.result_signature,
        "candidate_diversity": config.candidate_diversity,
        "result_precision": config.result_precision,
        "recent_trace_limit": config.recent_trace_limit,
        "max_same_shape_per_run": config.max_same_shape_per_run,
    }


def _deduplicate_plan(
    plan: FactorExperimentPlan,
    candidate: FactorDefinition,
    *,
    formula_index: dict[str, str],
    shape_counts: Counter[str],
    config: ResearchDeduplicationConfig,
    allow_existing: bool,
) -> FactorExperimentPlan | None:
    if not config.enabled or allow_existing:
        return None
    fingerprint = factor_formula_fingerprint(candidate)
    shape = _candidate_shape_fingerprint(plan, candidate)
    if config.formula_fingerprint:
        existing_factor_id = formula_index.get(fingerprint)
        if existing_factor_id is not None:
            return replace(
                plan,
                status="blocked_duplicate_formula",
                blocking_reasons=(
                    f"formula fingerprint already exists: {existing_factor_id}",
                ),
                metadata={
                    **plan.metadata,
                    "formula_fingerprint": fingerprint,
                    "duplicate_factor_id": existing_factor_id,
                },
            )
    if config.candidate_diversity and shape_counts[shape] >= config.max_same_shape_per_run:
        return replace(
            plan,
            status="blocked_candidate_diversity",
            blocking_reasons=(
                f"candidate shape repeated more than {config.max_same_shape_per_run} times in this RD run",
            ),
            metadata={
                **plan.metadata,
                "formula_fingerprint": fingerprint,
                "candidate_shape_fingerprint": shape,
            },
        )
    formula_index[fingerprint] = candidate.factor_id
    shape_counts[shape] += 1
    return None


def _record_dedup_skip(summary: dict[str, object], plan: FactorExperimentPlan) -> None:
    if plan.status == "blocked_duplicate_formula":
        summary["formula_skipped"] = int(summary["formula_skipped"]) + 1
    elif plan.status == "blocked_candidate_diversity":
        summary["diversity_skipped"] = int(summary["diversity_skipped"]) + 1
    skipped = list(summary.get("skipped_plans") or [])
    skipped.append(
        {
            "plan_id": plan.plan_id,
            "status": plan.status,
            "factor_name": plan.factor_name,
            "formula": plan.formula_dsl,
            "reasons": list(plan.blocking_reasons),
            "metadata": dict(plan.metadata),
        }
    )
    summary["skipped_plans"] = skipped


def _formula_fingerprint_index(factors: list[FactorDefinition]) -> dict[str, str]:
    index: dict[str, str] = {}
    for factor in factors:
        index.setdefault(factor_formula_fingerprint(factor), factor.factor_id)
    return index


def factor_formula_fingerprint(factor: FactorDefinition) -> str:
    return _formula_fingerprint(
        formula=factor.formula,
        horizon_days=factor.horizon_days,
        universe_filters=factor.universe_filters,
    )


def _hypothesis_formula_fingerprint(hypothesis: ResearchHypothesis, horizon_days: int) -> str:
    formula = hypothesis.formula_dsl.strip()
    if not formula:
        parsed = parse_idea_to_definition(hypothesis.text)
        formula = parsed.formula
        filters = parsed.universe_filters
    else:
        filters = _universe_filters_from_constraints(hypothesis.universe_constraints)
    return _formula_fingerprint(formula=formula, horizon_days=horizon_days, universe_filters=filters)


def _formula_fingerprint(*, formula: str, horizon_days: int, universe_filters: tuple[str, ...]) -> str:
    return _hash_parts(
        "formula",
        _canonical_formula_for_fingerprint(formula),
        str(horizon_days),
        _canonical_filters_for_fingerprint(universe_filters),
    )


def _candidate_shape_fingerprint(plan: FactorExperimentPlan, candidate: FactorDefinition) -> str:
    operators = tuple(str(item) for item in plan.operator_validation.get("used_operators", ()) or ())
    fields = tuple(plan.inputs or _formula_input_fields(candidate.formula))
    return _hash_parts(
        "shape",
        ",".join(sorted(set(field.lower() for field in fields))),
        ",".join(sorted(set(operator.lower() for operator in operators))),
        str(candidate.horizon_days),
        _canonical_filters_for_fingerprint(candidate.universe_filters),
    )


def _canonical_formula_for_fingerprint(formula: str) -> str:
    stripped = formula.strip()
    if is_precomputed_formula(stripped):
        return re.sub(r"\s+", "", stripped).lower()
    return re.sub(r"\s+", "", stripped).lower()


def _canonical_filters_for_fingerprint(filters: tuple[str, ...]) -> str:
    return ",".join(sorted(re.sub(r"\s+", "", item).lower() for item in filters))


def _recent_result_signature_index(
    trace_store: ResearchTraceStore,
    *,
    precision: int,
    limit: int,
) -> dict[str, str]:
    index: dict[str, str] = {}
    for entry in trace_store.read_recent_entries(limit=limit):
        if str(entry.get("phase") or "") != "experiment_result":
            continue
        signature = _result_signature_from_trace_entry(entry, precision=precision)
        candidate_ref = str(entry.get("candidate_ref") or "")
        if signature and candidate_ref:
            index.setdefault(signature, candidate_ref)
    return index


def _result_signature_from_scored(scored: _ScoredTrial, *, precision: int) -> str:
    return _hash_parts(
        "result",
        *_result_signature_values(
            evaluation={
                "rank_ic_mean": scored.evaluation.rank_ic_mean,
                "rank_icir": scored.evaluation.rank_icir,
                "coverage": scored.evaluation.coverage,
                "ic_days": scored.evaluation.ic_days,
            },
            backtest={
                "net_annualized_return": scored.backtest.net_annualized_return,
                "net_long_short_sharpe": scored.backtest.net_long_short_sharpe,
                "net_max_drawdown": scored.backtest.net_max_drawdown,
                "rebalance_rate": scored.backtest.rebalance_rate,
                "turnover_rate": scored.backtest.turnover_rate,
            },
            precision=precision,
        ),
    )


def _result_signature_from_trace_entry(entry: dict[str, Any], *, precision: int) -> str:
    evaluation = entry.get("evaluation_summary")
    backtest = entry.get("backtest_summary")
    if not isinstance(evaluation, dict) or not isinstance(backtest, dict):
        return ""
    return _hash_parts(
        "result",
        *_result_signature_values(
            evaluation=evaluation,
            backtest=backtest,
            precision=precision,
        ),
    )


def _result_signature_values(
    *,
    evaluation: dict[str, Any],
    backtest: dict[str, Any],
    precision: int,
) -> tuple[str, ...]:
    values = (
        _rounded_signature_value(evaluation.get("rank_ic_mean"), precision),
        _rounded_signature_value(evaluation.get("rank_icir"), precision),
        _rounded_signature_value(evaluation.get("coverage"), precision),
        _rounded_signature_value(evaluation.get("ic_days"), precision),
        _rounded_signature_value(backtest.get("net_annualized_return"), precision),
        _rounded_signature_value(backtest.get("net_long_short_sharpe"), precision),
        _rounded_signature_value(backtest.get("net_max_drawdown"), precision),
        _rounded_signature_value(backtest.get("rebalance_rate"), precision),
        _rounded_signature_value(backtest.get("turnover_rate"), precision),
    )
    return tuple(values)


def _rounded_signature_value(value: Any, precision: int) -> str:
    if value is None:
        return "missing"
    try:
        return f"{float(value):.{precision}f}"
    except (TypeError, ValueError):
        return str(value)


def _hash_parts(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16].upper()


def _generate_hypotheses(
    generator: HypothesisGenerator,
    seed: FactorDefinition,
    *,
    context: ResearchContext,
    objective: str,
    max_candidates: int,
) -> tuple[ResearchHypothesis, ...]:
    contextual = getattr(generator, "generate_with_context", None)
    if callable(contextual):
        return tuple(contextual(seed, context=context, objective=objective, max_candidates=max_candidates))
    return generator.generate(seed, objective=objective, max_candidates=max_candidates)


def _parameter_search_fallback_hypothesis(seed: FactorDefinition) -> ResearchHypothesis:
    return ResearchHypothesis(
        text=f"参数搜索兜底：围绕 {seed.name} 的现有公式进行 profile 参数优化",
        rationale=(
            "No executable improved hypothesis was available; use bounded parameter search over the existing "
            "seed formula."
        ),
        source="parameter_search",
        source_detail="bounded_profile_search",
        parameter_search_fallback=True,
    )


def _llm_formula_required_but_missing(hypothesis: ResearchHypothesis, generation: ResearchGenerationMetadata) -> bool:
    return _is_llm_research_hypothesis(hypothesis, generation) and not hypothesis.formula_dsl.strip()


def _normalize_llm_research_hypothesis(
    hypothesis: ResearchHypothesis,
    generation: ResearchGenerationMetadata,
) -> ResearchHypothesis:
    if not _is_llm_research_hypothesis(hypothesis, generation) or not hypothesis.parameter_search_fallback:
        return hypothesis
    source = "llm" if hypothesis.source == "parameter_search" else hypothesis.source
    source_detail = hypothesis.source_detail or "provider_parameter_search_fallback_ignored"
    return replace(hypothesis, source=source, source_detail=source_detail, parameter_search_fallback=False)


def _is_llm_research_hypothesis(hypothesis: ResearchHypothesis, generation: ResearchGenerationMetadata) -> bool:
    provenance = " ".join((generation.source, hypothesis.source, hypothesis.source_detail)).lower()
    return "llm" in provenance


def _structured_hypothesis_for_missing_formula(
    hypothesis: ResearchHypothesis,
    generation: ResearchGenerationMetadata,
    *,
    lane_index: int,
) -> StructuredResearchHypothesis:
    return StructuredResearchHypothesis(
        hypothesis_id=f"llm_missing_formula_h{lane_index:02d}",
        text=hypothesis.text,
        rationale=hypothesis.rationale,
        formula_dsl="",
        input_fields=hypothesis.input_fields,
        expected_direction=hypothesis.expected_direction,
        universe_constraints=hypothesis.universe_constraints,
        source=_structured_source(hypothesis, generation),
        source_detail=hypothesis.source_detail or f"{generation.source}:{generation.provider}:{generation.model}",
        parameter_search_fallback=hypothesis.parameter_search_fallback,
        unknowns=("LLM hypothesis did not provide formula_dsl.",),
    )


def _research_run_id(seed_factor_id: str) -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return f"rd_{_safe_id(seed_factor_id)}_{timestamp}_{uuid4().hex[:8]}"


def _safe_id(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in value)[:80] or "seed"


def _structured_hypothesis_from_candidate(
    hypothesis: ResearchHypothesis,
    candidate: FactorDefinition,
    generation: ResearchGenerationMetadata,
    *,
    lane_index: int,
) -> StructuredResearchHypothesis:
    return StructuredResearchHypothesis(
        hypothesis_id=f"{candidate.factor_id}_h{lane_index:02d}",
        text=hypothesis.text,
        rationale=hypothesis.rationale,
        formula_dsl=hypothesis.formula_dsl or candidate.formula,
        input_fields=hypothesis.input_fields or _formula_input_fields(candidate.formula),
        expected_direction=hypothesis.expected_direction or "positive",
        universe_constraints=hypothesis.universe_constraints or candidate.universe_filters,
        source=_structured_source(hypothesis, generation),
        source_detail=hypothesis.source_detail or f"{generation.source}:{generation.provider}:{generation.model}",
        parameter_search_fallback=hypothesis.parameter_search_fallback,
    )


def _structured_source(hypothesis: ResearchHypothesis, generation: ResearchGenerationMetadata) -> str:
    source = (hypothesis.source or generation.source).lower()
    if source in {"financial_analyst", "effective_idea", "operator_mcp", "parameter_search", "local", "llm"}:
        return source
    if "llm" in source:
        return "llm"
    if "parameter" in source:
        return "parameter_search"
    if "operator" in source:
        return "operator_mcp"
    return "local"


def _formula_input_fields(formula: str) -> tuple[str, ...]:
    if is_precomputed_formula(formula):
        return ()
    inspection = inspect_formula(formula, known_operators=set(SUPPORTED_OPERATORS))
    return inspection.fields


def _safe_factor_name(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]+", "_", text.strip().lower()).strip("_")[:80] or "rd_factor"


def _universe_filters_from_constraints(constraints: tuple[str, ...]) -> tuple[str, ...]:
    filters: list[str] = []
    for constraint in constraints:
        normalized = constraint.strip().lower().replace(" ", "").replace("-", "_")
        if normalized in {"is_st==false", "is_st==0", "notis_st"} or "非st" in normalized or "non_st" in normalized:
            filters.append("is_st == false")
    return tuple(dict.fromkeys(filters))


def _string_tuple(value: tuple[str, ...] | list[str] | str | None) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def _blocked_structured_result(
    plan: FactorExperimentPlan,
    *,
    artifact_refs: dict[str, str] | None = None,
) -> StructuredFactorExperimentResult:
    result = StructuredFactorExperimentResult(
        plan=plan,
        evaluation_status="skipped",
        backtest_status="skipped",
        artifact_refs=artifact_refs or {},
        error="; ".join(plan.blocking_reasons),
    )
    return replace(result, gate_decision=evaluate_structured_candidate(result))


def _failed_structured_result(plan: FactorExperimentPlan, *, error: str) -> StructuredFactorExperimentResult:
    result = StructuredFactorExperimentResult(
        plan=plan,
        evaluation_status="failed",
        backtest_status="skipped",
        error=error,
    )
    return replace(result, gate_decision=evaluate_structured_candidate(result))


def _operator_draft_refs(artifact_root: Path, plan: FactorExperimentPlan) -> dict[str, str]:
    artifacts = write_operator_draft_artifacts(artifact_root, plan)
    if artifacts is None:
        return {}
    refs = artifacts.to_refs()
    refs["operator_review_required"] = "true"
    return refs


def _structured_result_from_candidate(
    candidate: ResearchCandidateResult,
    plan: FactorExperimentPlan | None,
) -> StructuredFactorExperimentResult:
    experiment_plan = plan or FactorExperimentPlan(
        plan_id=f"{candidate.factor.factor_id}-p01",
        hypothesis_id=candidate.factor.factor_id,
        status="ready",
        factor_name=candidate.factor.name,
        formula_dsl=candidate.factor.formula,
        inputs=_formula_input_fields(candidate.factor.formula),
        universe_filters=candidate.factor.universe_filters,
        expected_direction="positive",
    )
    decision = evaluate_structured_candidate(
        StructuredFactorExperimentResult(
            plan=experiment_plan,
            candidate_ref=candidate.factor.factor_id,
            evaluation_status="completed",
            evaluation_metrics=_evaluation_metrics(candidate.evaluation),
            backtest_status="completed",
            backtest_metrics=_backtest_metrics(candidate.backtest),
            artifact_refs=_artifact_refs(candidate),
        )
    )
    if candidate.gate_passed:
        decision = replace(
            decision,
            should_transition_to_candidate=candidate.transitioned_to_candidate,
            transition_reason=decision.transition_reason
            if candidate.transitioned_to_candidate
            else "Candidate passed research gates without a new status transition.",
        )
    else:
        decision = replace(
            decision,
            status="blocked",
            accepted=False,
            blocking_reasons=tuple(dict.fromkeys((*decision.blocking_reasons, *candidate.gate_reasons))),
            should_transition_to_candidate=False,
            transition_reason="",
        )
    return StructuredFactorExperimentResult(
        plan=experiment_plan,
        candidate_ref=candidate.factor.factor_id,
        evaluation_status="completed",
        evaluation_metrics=_evaluation_metrics(candidate.evaluation),
        backtest_status="completed",
        backtest_metrics=_backtest_metrics(candidate.backtest),
        artifact_refs=_artifact_refs(candidate),
        gate_decision=decision,
    )


def _evaluation_metrics(evaluation: EvaluationResult) -> dict[str, object]:
    return {
        "observations": evaluation.observations,
        "coverage": evaluation.coverage,
        "rank_ic_mean": evaluation.rank_ic_mean,
        "rank_icir": evaluation.rank_icir,
        "ic_days": evaluation.ic_days,
        "score_source": evaluation.score_source,
        "score_cached_rows": evaluation.score_cached_rows,
        "score_computed_rows": evaluation.score_computed_rows,
    }


def _backtest_metrics(backtest: BacktestResult) -> dict[str, object]:
    return {
        "periods": backtest.periods,
        "annualized_return": backtest.annualized_return,
        "net_annualized_return": backtest.net_annualized_return,
        "max_drawdown": backtest.max_drawdown,
        "net_long_short_sharpe": backtest.net_long_short_sharpe,
        "rebalance_rate": backtest.rebalance_rate,
        "turnover_rate": backtest.turnover_rate,
        "score_source": backtest.score_source,
        "score_cached_rows": backtest.score_cached_rows,
        "score_computed_rows": backtest.score_computed_rows,
    }


def _artifact_refs(candidate: ResearchCandidateResult) -> dict[str, str]:
    return {
        "evaluation": candidate.evaluation.artifact_path.name,
        "backtest": candidate.backtest.artifact_path.name,
    }


def _load_or_save_candidate(repo: FactorRepository, draft: FactorDefinition) -> FactorDefinition:
    try:
        existing = repo.get(draft.factor_id)
    except FileNotFoundError:
        repo.save(draft)
        return draft
    if (
        existing.formula != draft.formula
        or existing.horizon_days != draft.horizon_days
        or existing.universe_filters != draft.universe_filters
    ):
        raise ValueError(f"research candidate id collision with different definition: {draft.factor_id}")
    return existing


def _oos_decay(evaluation: EvaluationResult) -> bool:
    split_by_name = {metric.name.upper(): metric for metric in evaluation.split_metrics}
    is_metric = split_by_name.get("IS")
    oos2_metric = split_by_name.get("OOS2")
    if is_metric is None or oos2_metric is None:
        return False
    if is_metric.ic_days == 0 or oos2_metric.ic_days == 0:
        return False
    return oos2_metric.rank_icir < is_metric.rank_icir * 0.5


def _oos_segments(backtest: BacktestResult) -> tuple[BacktestSegmentMetric, ...]:
    return tuple(metric for metric in backtest.segment_metrics if metric.name.upper().startswith("OOS"))


def _cost_sensitive(backtest: BacktestResult) -> bool:
    return backtest.annualized_return > 0 and backtest.net_annualized_return < backtest.annualized_return * 0.5


def _net_return_retention(backtest: BacktestResult) -> float:
    if backtest.annualized_return <= 0:
        return 1.0 if backtest.net_annualized_return >= backtest.annualized_return else 0.0
    return float(backtest.net_annualized_return / backtest.annualized_return)


def _oos_net_decay(backtest: BacktestResult, max_decay: float = 0.5) -> bool:
    split_by_name = {metric.name.upper(): metric for metric in backtest.segment_metrics}
    is_metric = split_by_name.get("IS")
    if is_metric is None or is_metric.periods == 0:
        return False
    for name, metric in split_by_name.items():
        if name.startswith("OOS") and metric.periods > 0:
            if is_metric.net_annualized_return <= 0:
                if metric.net_annualized_return < is_metric.net_annualized_return:
                    return True
                continue
            ratio = metric.net_annualized_return / is_metric.net_annualized_return
            if ratio < max_decay:
                return True
    return False

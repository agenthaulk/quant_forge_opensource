"""Small, decoupled factor research loop."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import Counter
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from math import ceil, isfinite
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from quant_forge.backtesting.service import EXTERNAL_OOS_ROLE, IN_SAMPLE_ROLE, run_factor_backtest
from quant_forge.core.contracts import (
    BacktestResult,
    EvaluationResult,
    FactorAssessmentBundle,
    FactorDefinition,
    MetricValue,
    SampleSplitSpec,
    SimulationProfile,
    TransactionCostModel,
)
from quant_forge.evaluation.service import evaluate_factor
from quant_forge.factor_engine.formula_parser import SUPPORTED_OPERATORS, inspect_formula
from quant_forge.factor_library.catalog import FactorCatalog, is_precomputed_formula
from quant_forge.factor_library.repository import FactorRepository, parse_idea_to_definition
from quant_forge.lineage.store import (
    RunIndex,
    canonical_fingerprint,
    metric_highlight,
    relative_artifact_path,
)
from quant_forge.research_loop.candidate_gate import (
    SegmentEvidence,
    max_oos_decay_reasons,
    min_net_return_retention_reasons,
    min_oos_return_reasons,
    net_return_retention_value,
    oos_net_decay_exceeded,
    oos_return_evidence,
    turnover_reasons,
)
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
from quant_forge.research_loop.local_outcomes import experiment_result_to_outcome
from quant_forge.research_loop.memory import ResearchMemoryStore
from quant_forge.research_loop.operator_drafts import write_operator_draft_artifacts
from quant_forge.research_loop.outcome_ingest import ingest_outcome
from quant_forge.research_loop.strategy_selector import (
    StrategyContext,
    StrategyDecision,
    select_strategy,
)
from quant_forge.research_loop.trace_store import ResearchTraceStore, utc_timestamp
from quant_forge.workbench.service import evaluation_data_window

logger = logging.getLogger(__name__)

DEFAULT_QUICK_HORIZON_DAYS = (5, 21)
DEFAULT_QUICK_SAMPLE_SPLITS = (SampleSplitSpec(name="IS", fraction=1.0, score_weight=1.0),)
DEFAULT_LLM_FORMULA_REPAIR_ATTEMPTS = 2
RD_RESEARCH_STAGE = "research"
RD_RUN_INDEX_KIND = "rd"
RD_RUN_HIGHLIGHT_METRICS = ("rank_ic_mean", "rank_icir", "rank_ic_t_stat")
# Bounded evidence window the strategy selector reads from the trace store.
# Decoupled from deduplication.recent_trace_limit so disabling result-signature
# dedup does not silently blind the selector.
STRATEGY_CONTEXT_TRACE_LIMIT = 200
_STRATEGY_TRAIL_LIMIT = 10
_STRATEGY_MECHANISM_LIMIT = 10
_STRATEGY_FINGERPRINT_LIMIT = 20
_DUPLICATE_PLAN_STATUSES = frozenset({"blocked_duplicate_formula", "blocked_candidate_diversity"})


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
    # F-5: when False, missing-evidence findings for the OOS/net-return
    # clauses downgrade from blocking reasons to warnings (candidate-gate
    # parity). Threshold violations always block. Default True preserves the
    # pre-existing fail-closed behavior.
    missing_oos_evidence_blocks: bool = True

    def __post_init__(self) -> None:
        # Loader parity (F-5): a truthy non-bool (e.g. the string "false")
        # must not silently flip the missing-evidence channel when the gate
        # is constructed directly instead of via the strict YAML loader.
        if not isinstance(self.missing_oos_evidence_blocks, bool):
            raise ValueError("missing_oos_evidence_blocks must be a boolean")
        # SE-P2 re-verify RV2-F4: NaN slips through every `< 0` comparison
        # below and +/-inf passes the minimum checks, then blows up LATE in
        # the settings-token serialization after research artifacts were
        # already written. A non-finite threshold is never meaningful; fail
        # at construction.
        for numeric_field in (
            "min_ic_days",
            "min_coverage",
            "min_score",
            "min_backtest_periods",
            "min_oos_net_annualized_return",
            "max_rebalance_rate",
            "max_turnover_rate",
            "min_net_return_retention",
            "max_oos_net_return_decay",
        ):
            value = getattr(self, numeric_field)
            if value is not None and not isfinite(value):
                raise ValueError(f"{numeric_field} must be finite")
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
        if backtest.net_long_short_sharpe is not None:
            if backtest.net_long_short_sharpe > 0:
                strengths.append("positive net long-short Sharpe")
            else:
                risks.append("net long-short Sharpe is not positive")
        if backtest.rebalance_rate is not None and backtest.rebalance_rate > 0.8:
            risks.append("high rebalance rate")
            next_hypotheses.append(f"smooth or slow down {candidate.name} to reduce rebalance rate")
        if backtest.turnover_rate is not None and backtest.turnover_rate > 1.5:
            risks.append("high turnover rate")
            next_hypotheses.append(f"smooth or slow down {candidate.name} to reduce turnover rate")
        if _cost_sensitive(backtest):
            risks.append("net performance is sensitive to transaction costs")
        if backtest.net_max_drawdown is not None and backtest.net_max_drawdown < -0.2:
            risks.append("large net drawdown in lightweight backtest")
        if _oos_decay(evaluation):
            risks.append("OOS2 ICIR decays versus IS")
            next_hypotheses.append(f"test a simpler or more robust variant of {candidate.name}")
        if oos_net_decay_exceeded(_segment_gate_evidence(backtest), 0.5):
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


def _failed_self_review(exc: Exception, candidate: FactorDefinition) -> ResearchSelfReview:
    error = str(exc).strip() or exc.__class__.__name__
    return ResearchSelfReview(
        source="llm_self_review_error",
        summary=f"LLM self-review unavailable for {candidate.factor_id}: {error[:300]}",
        strengths=(),
        risks=(f"LLM self-review failed: {error[:300]}",),
        next_hypotheses=(),
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
    # Warn-mode gate findings (e.g. INSUFFICIENT_OOS_EVIDENCE with
    # missing_oos_evidence_blocks=False). A separate channel from
    # ``gate_reasons`` so a warning can never read as a blocker downstream.
    gate_warnings: tuple[str, ...] = ()
    transitioned_to_candidate: bool = False
    formula_fingerprint: str = ""
    result_signature: str = ""
    candidate_shape_fingerprint: str = ""
    selection_backtest: BacktestResult | None = None
    external_oos_backtest: BacktestResult | None = None
    assessment: FactorAssessmentBundle | None = None


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
    seed_assessment: FactorAssessmentBundle | None = None
    comparison_rows: tuple[dict[str, object], ...] = ()
    generation: ResearchGenerationMetadata = field(default_factory=ResearchGenerationMetadata)
    search_trace: tuple[ResearchSearchTraceEntry, ...] = ()
    blocked_plans: tuple[StructuredFactorExperimentResult, ...] = ()
    trace_root: Path | None = None
    report_path: Path | None = None
    workflow_type: str = "research"
    deduplication: dict[str, object] = field(default_factory=dict)
    optimization_performed: bool = False
    no_optimization_performed: bool = False
    # Strategy selector observability: the current round's decision (None when
    # the selector is disabled) and the bounded per-round decision trail.
    strategy_decision: dict[str, object] | None = None
    strategy_trail: tuple[dict[str, object], ...] = ()


@dataclass(frozen=True)
class ResearchTrialSimulationOverlay:
    profile: SimulationProfile | None = None
    top_quantile: float | None = None
    decay_days: int | None = None


@dataclass(frozen=True)
class ResearchEffectiveTrialConfig:
    overlay: ResearchTrialSimulationOverlay
    evaluation_profile: SimulationProfile
    backtest_profile: SimulationProfile


@dataclass(frozen=True)
class _ResearchTrial:
    hypothesis: ResearchHypothesis
    factor: FactorDefinition
    effective_config: ResearchEffectiveTrialConfig
    plan: FactorExperimentPlan | None = None


@dataclass(frozen=True)
class _ScoredTrial:
    trial: _ResearchTrial
    evaluation: EvaluationResult
    backtest: BacktestResult
    split_weighted_icir: float
    score: float
    external_oos_backtest: BacktestResult | None = None


@dataclass(frozen=True)
class _RepairOutcome:
    hypothesis: ResearchHypothesis | None
    draft: FactorDefinition | None
    plan: FactorExperimentPlan
    exhausted: bool = False


class _ResearchRunCancelled(RuntimeError):
    pass


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
        trial_simulation_overlays: tuple[ResearchTrialSimulationOverlay, ...] | None = None,
        evaluation_simulation_profile: SimulationProfile | None = None,
        backtest_simulation_profile: SimulationProfile | None = None,
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
        llm_formula_repair_attempts: int = DEFAULT_LLM_FORMULA_REPAIR_ATTEMPTS,
        strategy_selector_enabled: bool = True,
        research_memory_enabled: bool = True,
        cancel_event: Any | None = None,
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
        if trial_simulation_overlays is not None:
            self.trial_simulation_overlays = trial_simulation_overlays
        elif simulation_profiles is not None:
            self.trial_simulation_overlays = _trial_overlays_from_profiles(simulation_profiles)
        else:
            self.trial_simulation_overlays = (ResearchTrialSimulationOverlay(),)
        self.evaluation_simulation_profile = evaluation_simulation_profile or profile
        self.backtest_simulation_profile = backtest_simulation_profile or profile
        if not self.simulation_profiles:
            raise ValueError("research loop requires at least one simulation profile")
        if not self.trial_simulation_overlays:
            raise ValueError("research loop requires at least one trial simulation overlay")
        self.effective_trial_configs = tuple(
            _effective_trial_config(
                overlay,
                evaluation_profile=self.evaluation_simulation_profile,
                backtest_profile=self.backtest_simulation_profile,
            )
            for overlay in self.trial_simulation_overlays
        )
        self.simulation_profiles = tuple(config.backtest_profile for config in self.effective_trial_configs)
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
        if not 0 <= llm_formula_repair_attempts <= 3:
            raise ValueError("llm_formula_repair_attempts must be between 0 and 3")
        self.llm_formula_repair_attempts = llm_formula_repair_attempts
        self.strategy_selector_enabled = strategy_selector_enabled
        self.research_memory_enabled = research_memory_enabled
        # Durable cross-run memory (observations + promoted knowledge rows)
        # rooted at artifact_root; None when disabled so nothing is written.
        self.memory_store = ResearchMemoryStore(self.artifact_root) if research_memory_enabled else None
        self.cancel_event = cancel_event
        self._active_run_id: str | None = None
        self._cancel_written = False
        self._created_factor_ids: set[str] = set()
        self._promoted_factor_snapshots: dict[str, FactorDefinition] = {}

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
        self._raise_if_cancelled()
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
        self._active_run_id = run_id
        self._cancel_written = False
        self._created_factor_ids = set()
        self._promoted_factor_snapshots = {}
        self.trace_store.ensure_run_dirs(run_id)
        self.trace_store.write_run(run_id, {"run_id": run_id, "status": "running", "started_at": utc_timestamp()})
        self._raise_if_cancelled()
        context = ResearchContextBuilder(
            factor_root=self.factor_root,
            data_root=self.data_root,
            factor_values_root=self.factor_values_root,
            factor_values_manifest_root=self.factor_values_manifest_root,
            trace_store=self.trace_store,
            memory_store=self.memory_store,
        ).build(objective=objective, seed_factor_ids=(seed_factor_id,))
        # Strategy selection happens BEFORE candidate generation and reads only
        # evidence already persisted by prior rounds (trace/dedup/gate state);
        # it never peeks at this round's evaluation data (FP-6).
        strategy_round_index: int | None = None
        strategy_decision_payload: dict[str, Any] | None = None
        strategy_trail: tuple[dict[str, Any], ...] = ()
        if self.strategy_selector_enabled:
            # F3: the selector's evidence is scoped to THIS seed's run chain
            # (run ids embed the seed id). Rounds of other seeds must never
            # leak gate reasons, round summaries, duplicate counts, or
            # fingerprints into this run's context; same-seed history is the
            # legitimate "prior rounds" evidence (and the fingerprint dedup
            # memory) the selector was designed to consume. The filter runs
            # INSIDE read_recent_entries, before its window limit, so heavy
            # other-seed traffic cannot erase this seed's history (C2).
            recent_strategy_entries = self.trace_store.read_recent_entries(
                limit=STRATEGY_CONTEXT_TRACE_LIMIT,
                run_id_filter=lambda candidate_run_id: _run_id_in_seed_chain(
                    candidate_run_id, seed_factor_id=seed_factor_id, current_run_id=run_id
                ),
            )
            strategy_context = _strategy_context_from_trace_entries(recent_strategy_entries)
            strategy_decision = select_strategy(strategy_context)
            strategy_round_index = strategy_context.round_index
            strategy_decision_payload = strategy_decision.to_dict()
            strategy_trail = (
                *_strategy_trail_from_trace_entries(recent_strategy_entries),
                {
                    "round_index": strategy_context.round_index,
                    "strategy": strategy_decision.strategy,
                    "reason": strategy_decision.reason,
                },
            )
            self.trace_store.append_trace(
                {
                    "run_id": run_id,
                    "lane_id": "strategy_selector",
                    "phase": "strategy_decision",
                    "timestamp": utc_timestamp(),
                    "schema_version": "qf.research_loop.trace.v1",
                    "strategy_context": strategy_context.to_dict(),
                    "strategy_decision": strategy_decision_payload,
                }
            )
            context = replace(
                context,
                next_focus_hints=(
                    *context.next_focus_hints,
                    *_strategy_prompt_hints(strategy_decision),
                ),
            )
        self.trace_store.write_context(run_id, context)
        self._raise_if_cancelled()
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
        config_snapshot: dict[str, Any] = {
            "objective": objective,
            "max_candidates": max_candidates,
            "generation": _generation_snapshot(generation),
            "llm_formula_repair_attempts": self.llm_formula_repair_attempts,
            "parameter_search_enabled": self.parameter_search_enabled,
            "parameter_search_method": self.parameter_search_method,
            "simulation_profile": asdict(self.simulation_profile),
            "evaluation_profile": asdict(self.evaluation_simulation_profile),
            "backtest_profile": asdict(self.backtest_simulation_profile),
            "trial_simulation_overlays": [
                _trial_simulation_overlay_snapshot(overlay) for overlay in self.trial_simulation_overlays
            ],
            "effective_trial_configs": [
                _effective_trial_config_snapshot(config) for config in self.effective_trial_configs
            ],
            "deduplication": _deduplication_snapshot(self.deduplication),
            "strategy_selector_enabled": self.strategy_selector_enabled,
            "research_memory_enabled": self.research_memory_enabled,
        }
        self.trace_store.write_config_snapshot(run_id, config_snapshot)
        # BUG #006 / PF-F1: the seed's own score can be unscorable (a
        # nonzero-weight component's required metric is unavailable, e.g.
        # net_annualized_return under INSUFFICIENT_ANNUALIZATION_HISTORY on a
        # short RD window). That must never abort the whole run — it mirrors
        # the existing per-trial exception handling below (final_trials
        # loop), just for the one assessment that happens before any
        # candidate exists. Only _RequiredMetricUnavailable degrades to
        # seed_assessment=None (never a fabricated score) with a trace entry
        # for observability; any other exception (a programming error, I/O
        # failure, or artifact corruption) is a real failure and propagates
        # exactly as it did before this handling existed.
        seed_assessment: FactorAssessmentBundle | None
        try:
            seed_assessment = self._assess_factor(
                seed,
                role="seed",
                parent_seed_factor_id=seed.factor_id,
                objective_weights=objective_weights,
                gate=candidate_gate,
            )
        except _ResearchRunCancelled:
            raise
        except _RequiredMetricUnavailable as exc:
            seed_assessment = None
            self.trace_store.append_trace(
                {
                    "run_id": run_id,
                    "lane_id": "seed",
                    "phase": "seed_unscorable",
                    "timestamp": utc_timestamp(),
                    "schema_version": "qf.research_loop.trace.v1",
                    "error": str(exc),
                }
            )

        trials: list[_ResearchTrial] = []
        blocked_plans: list[StructuredFactorExperimentResult] = []
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
        duplicate_fallback_requests: list[tuple[int, FactorExperimentPlan, _RepairOutcome | None]] = []
        dedup_summary: dict[str, object] = {
            "enabled": self.deduplication.enabled,
            "formula_skipped": 0,
            "diversity_skipped": 0,
            "result_duplicates": 0,
            "skipped_plans": [],
        }
        for lane_index, raw_hypothesis in enumerate(planned[:max_candidates], start=1):
            self._raise_if_cancelled()
            hypothesis = _normalize_llm_research_hypothesis(raw_hypothesis, generation)
            if _llm_formula_required_but_missing(hypothesis, generation):
                structured = _structured_hypothesis_for_missing_formula(hypothesis, generation, lane_index=lane_index)
                plan = self.experiment_planner.plan(structured, context)
                self._trace_plan(run_id, structured, plan)
                repair = self._repair_invalid_llm_plan(
                    run_id=run_id,
                    seed=seed,
                    context=context,
                    objective=objective,
                    generation=generation,
                    hypothesis=hypothesis,
                    plan=plan,
                    lane_index=lane_index,
                )
                if repair is not None and repair.hypothesis is not None and repair.draft is not None:
                    hypothesis = repair.hypothesis
                    draft = repair.draft
                    plan = repair.plan
                else:
                    self._record_blocked_plan(
                        run_id=run_id,
                        plan=plan,
                        repair=repair,
                        blocked_plans=blocked_plans,
                    )
                    continue
            else:
                draft = (
                    seed
                    if hypothesis.parameter_search_fallback
                    else _candidate_from_hypothesis(hypothesis, seed.horizon_days)
                )
                structured = _structured_hypothesis_from_candidate(hypothesis, draft, generation, lane_index=lane_index)
                plan = self.experiment_planner.plan(structured, context)
                self._trace_plan(run_id, structured, plan)
            if plan.status != "ready":
                repair = self._repair_invalid_llm_plan(
                    run_id=run_id,
                    seed=seed,
                    context=context,
                    objective=objective,
                    generation=generation,
                    hypothesis=hypothesis,
                    plan=plan,
                    lane_index=lane_index,
                )
                if repair is not None and repair.hypothesis is not None and repair.draft is not None:
                    hypothesis = repair.hypothesis
                    draft = repair.draft
                    plan = repair.plan
                else:
                    self._record_blocked_plan(
                        run_id=run_id,
                        plan=plan,
                        repair=repair,
                        blocked_plans=blocked_plans,
                    )
                    continue
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
                repair = self._repair_duplicate_llm_plan(
                    run_id=run_id,
                    seed=seed,
                    context=context,
                    objective=objective,
                    generation=generation,
                    hypothesis=hypothesis,
                    duplicate_plan=duplicate_plan,
                    lane_index=lane_index,
                    formula_index=formula_index,
                    shape_counts=shape_counts,
                )
                if repair is not None and repair.hypothesis is not None and repair.draft is not None:
                    hypothesis = repair.hypothesis
                    draft = repair.draft
                    plan = repair.plan
                    planned_candidate = (
                        seed
                        if hypothesis.parameter_search_fallback
                        else replace(
                            draft,
                            formula=plan.formula_dsl,
                            universe_filters=plan.universe_filters,
                        )
                    )
                else:
                    if repair is not None and repair.exhausted:
                        duplicate_plan = repair.plan
                    _record_dedup_skip(dedup_summary, duplicate_plan)
                    duplicate_result = _blocked_structured_result(
                        duplicate_plan,
                        artifact_refs=_operator_draft_refs(self.artifact_root, duplicate_plan),
                    )
                    blocked_plans.append(duplicate_result)
                    self._trace_structured_result(run_id, duplicate_result, phase="plan_blocked")
                    if repair is not None and repair.exhausted:
                        duplicate_fallback_requests.append((lane_index, duplicate_plan, repair))
                    continue
            candidate = (
                seed
                if hypothesis.parameter_search_fallback
                else self._load_or_save_candidate(repo, planned_candidate)
            )
            for effective_config in self.effective_trial_configs:
                trials.append(_ResearchTrial(hypothesis, candidate, effective_config, plan))
        if not trials and duplicate_fallback_requests:
            lane_index, duplicate_plan, repair = duplicate_fallback_requests[0]
            self._append_duplicate_exhaustion_formula_fallback_trials(
                run_id=run_id,
                repo=repo,
                seed=seed,
                context=context,
                generation=generation,
                lane_index=lane_index,
                formula_index=formula_index,
                shape_counts=shape_counts,
                dedup_summary=dedup_summary,
                trials=trials,
                blocked_plans=blocked_plans,
            )
        self._raise_if_cancelled()
        search_trace, final_trials, failed_quick_results = self._select_final_trials(trials, objective_weights)
        blocked_plans.extend(failed_quick_results)
        for failed in failed_quick_results:
            self._trace_structured_result(run_id, failed, phase="experiment_failed")
        results: list[ResearchCandidateResult] = []
        structured_by_result: dict[int, StructuredFactorExperimentResult] = {}
        for trial in final_trials:
            self._raise_if_cancelled()
            try:
                candidate_result = self._evaluate_final_trial(
                    repo,
                    seed,
                    trial,
                    objective_weights,
                    candidate_gate,
                    result_signature_index=result_index,
                )
            except _ResearchRunCancelled:
                raise
            except Exception as exc:
                failed = _failed_structured_result(trial.plan, error=str(exc))
                blocked_plans.append(failed)
                self._trace_structured_result(run_id, failed, phase="experiment_failed")
                continue
            if any(reason.startswith("duplicate result signature") for reason in candidate_result.gate_reasons):
                dedup_summary["result_duplicates"] = int(dedup_summary["result_duplicates"]) + 1
            results.append(candidate_result)
            structured_by_result[id(candidate_result)] = self._trace_candidate_result(
                run_id, candidate_result, trial.plan
            )
        results = sorted(results, key=lambda result: result.score, reverse=True)
        # C1: the round WINNER's evidence (post-sort), not whichever candidate
        # happened to be traced last in evaluation order. Persisted with the
        # round summary so the next round's selector reads the winner's decay
        # and gate reasons alongside best_score_delta_vs_seed.
        winner = results[0] if results else None
        winner_structured = structured_by_result.get(id(winner)) if winner is not None else None
        winner_gate_blocking_reasons: tuple[str, ...] = ()
        if winner_structured is not None and winner_structured.gate_decision is not None:
            winner_gate_blocking_reasons = tuple(
                str(reason) for reason in winner_structured.gate_decision.blocking_reasons
            )
        elif winner is not None and not winner.gate_passed:
            winner_gate_blocking_reasons = tuple(str(reason) for reason in winner.gate_reasons)
        accepted = tuple(
            dict.fromkeys(
                result.factor.factor_id
                for result in results
                if result.gate_passed and result.factor.status in {"candidate", "active"}
                and not (result.hypothesis.parameter_search_fallback and result.factor.factor_id == seed_factor_id)
            )
        )
        optimization_performed = _optimization_performed(results, seed, self.evaluation_simulation_profile)
        # Persist compact per-round evidence so the NEXT round's strategy
        # selector can consume duplicate rate and score deltas from the trace
        # (run history), not from in-memory state that dies with this call.
        self.trace_store.append_trace(
            {
                "run_id": run_id,
                "lane_id": "run_history",
                "phase": "round_summary",
                "timestamp": utc_timestamp(),
                "schema_version": "qf.research_loop.trace.v1",
                "round_summary": _round_summary_payload(
                    seed_factor_id=seed_factor_id,
                    round_index=strategy_round_index,
                    planned_count=len(planned[:max_candidates]),
                    results=results,
                    # FP-4: no fabricated score when the seed itself could not
                    # be scored (_round_summary_payload's _finite_float_or_none
                    # already turns None into an honest missing seed_score).
                    seed_score=(seed_assessment.selection_score if seed_assessment is not None else None),
                    dedup_summary=dedup_summary,
                    accepted_candidate_ids=accepted,
                    winner_candidate_ref=winner.factor.factor_id if winner is not None else None,
                    winner_oos_net_return_decay=(
                        _oos_net_return_decay_value(winner.backtest) if winner is not None else None
                    ),
                    winner_gate_blocking_reasons=winner_gate_blocking_reasons,
                ),
            }
        )
        result = ResearchLoopResult(
            rd_stage=RD_RESEARCH_STAGE,
            seed_factor_id=seed_factor_id,
            objective=objective,
            objective_weights=objective_weights,
            gate=candidate_gate,
            candidates=tuple(results),
            accepted_candidate_ids=accepted,
            seed_assessment=seed_assessment,
            comparison_rows=(
                # FP-4: an unscorable seed contributes no row rather than one
                # built from a fabricated/missing score (mirrors the candidate
                # side, which already only emits rows for a real assessment).
                *((_assessment_comparison_row(seed_assessment, seed),) if seed_assessment is not None else ()),
                *tuple(
                    _assessment_comparison_row(candidate.assessment, candidate.factor, candidate.hypothesis.text)
                    for candidate in results
                    if candidate.assessment is not None
                ),
            ),
            generation=generation,
            search_trace=search_trace,
            blocked_plans=tuple(blocked_plans),
            trace_root=self.trace_store.run_dir(run_id),
            deduplication=dedup_summary,
            optimization_performed=optimization_performed,
            no_optimization_performed=not optimization_performed,
            strategy_decision=strategy_decision_payload,
            strategy_trail=strategy_trail,
        )
        from quant_forge.research_loop.reporting import write_research_report

        self._raise_if_cancelled()
        result = replace(result, report_path=write_research_report(result, self.artifact_root))
        run_status = (
            "completed"
            if results and optimization_performed
            else ("no_optimization_performed" if results else ("partial" if blocked_plans else "failed"))
        )
        self.trace_store.write_run(
            run_id,
            {
                "run_id": run_id,
                "status": run_status,
                "finished_at": utc_timestamp(),
                "candidate_count": len(result.candidates),
                "blocked_plan_count": len(result.blocked_plans),
                "accepted_candidate_ids": list(result.accepted_candidate_ids),
                "optimization_performed": result.optimization_performed,
                "no_optimization_performed": result.no_optimization_performed,
                "report_path": result.report_path,
            },
        )
        self._append_rd_run_index_row(
            run_id=run_id,
            seed_factor_id=seed_factor_id,
            result=result,
            seed_assessment=seed_assessment,
            config_snapshot=config_snapshot,
        )
        self._record_memory_observations(run_id, results, candidate_gate)
        self._created_factor_ids.clear()
        self._promoted_factor_snapshots.clear()
        return result

    def _record_memory_observations(
        self, run_id: str, results: list[ResearchCandidateResult], gate: ResearchGate
    ) -> None:
        """Record this run's candidate outcomes as durable memory (SE-P2).

        Each candidate is mapped to one neutral ``ResearchOutcome``
        (``local_outcomes.experiment_result_to_outcome``) and ingested through
        the shared kernel sink (``outcome_ingest.ingest_outcome``): an
        outcomes-ledger envelope (exact replay-drop by ``outcome_id``), its
        derived ``MemoryObservation`` rows, and deterministic promotion. This
        REPLACES the pre-SE-P2 ad-hoc ``rd_accepted:``/``rd_gate_blocked:``
        raw-fingerprint signatures with the v2 four-axis identity (stage /
        verdict / reason_codes / lifecycle_status): grouping now unifies by
        reason-code family and scope rather than raw formula fingerprint,
        which is the DESIGNED generalization behavior (SE-ii), not a
        regression.

        ``ingest_outcome`` calls ``promote_pending()`` once per outcome; that
        is acceptable here (promotion is a pure, deterministic function of
        the full observation set each time, so calling it once per candidate
        in a small per-run loop costs some redundant re-reads but never
        changes the outcome) rather than batching all of this run's envelopes
        and observations first and promoting once at the end — the simpler,
        per-outcome call keeps this loop a thin pass-through over the shared
        sink instead of re-implementing its steps here.

        ``gate`` is the effective ``ResearchGate`` this run's candidates were
        judged under; the producer folds it into the derived
        ``settings_profile`` scope token so evidence produced under
        materially different thresholds can never share a signature
        (SE-P2 review finding P2-F4).

        A candidate result with no representable outcome in the neutral
        vocabulary (``experiment_result_to_outcome`` returns None: an
        unrepresentable factor identity, or a block carried ONLY by
        administrative/unmapped reason families) is skipped and logged here,
        at the caller, rather than inside the pure mapper.
        """

        if self.memory_store is None:
            return
        for result in results:
            outcome = experiment_result_to_outcome(result, run_id=run_id, gate=gate)
            if outcome is None:
                logger.info(
                    "skipping memory observation for run %s candidate %s: no representable outcome "
                    "(factor identity or reason families outside the neutral vocabulary)",
                    run_id,
                    result.factor.factor_id if result.factor is not None else "<unknown>",
                )
                continue
            # F1: the MAIN store is LOCAL-only (SE-i), and this producer only
            # ever mints origin="local" outcomes -- assert that at the ingress
            # boundary so a future non-local outcome can never be persisted here.
            ingest_outcome(self.memory_store, outcome, expected_origin="local")

    def _raise_if_cancelled(self) -> None:
        if self.cancel_event is not None and self.cancel_event.is_set():
            self._mark_cancelled()
            self._rollback_cancelled_mutations()
            raise _ResearchRunCancelled("research run cancelled by user")

    def _mark_cancelled(self) -> None:
        if self._active_run_id is None or self._cancel_written:
            return
        self.trace_store.write_run(
            self._active_run_id,
            {
                "run_id": self._active_run_id,
                "status": "cancelled",
                "finished_at": utc_timestamp(),
            },
        )
        self._cancel_written = True

    def _rollback_cancelled_mutations(self) -> None:
        if not self._created_factor_ids and not self._promoted_factor_snapshots:
            return
        repo = FactorRepository(self.factor_root)
        for factor_id, original in tuple(self._promoted_factor_snapshots.items()):
            repo.save(original)
            self._promoted_factor_snapshots.pop(factor_id, None)
        for factor_id in tuple(self._created_factor_ids):
            repo.delete(factor_id)
            self._created_factor_ids.discard(factor_id)

    def _load_or_save_candidate(self, repo: FactorRepository, draft: FactorDefinition) -> FactorDefinition:
        existed = _factor_exists(repo, draft.factor_id)
        candidate = _load_or_save_candidate(repo, draft)
        if not existed:
            self._created_factor_ids.add(candidate.factor_id)
        return candidate

    def _promote_candidate(self, repo: FactorRepository, candidate: FactorDefinition, *, reason: str) -> FactorDefinition:
        self._promoted_factor_snapshots.setdefault(candidate.factor_id, candidate)
        promoted = repo.promote(candidate.factor_id, "candidate", reason)
        self._raise_if_cancelled()
        return promoted

    def _record_blocked_plan(
        self,
        *,
        run_id: str,
        plan: FactorExperimentPlan,
        repair: _RepairOutcome | None,
        blocked_plans: list[StructuredFactorExperimentResult],
    ) -> None:
        blocked_plan = repair.plan if repair is not None else plan
        blocked_result = _blocked_structured_result(
            blocked_plan,
            artifact_refs=_operator_draft_refs(self.artifact_root, blocked_plan),
        )
        blocked_plans.append(blocked_result)
        self._trace_structured_result(run_id, blocked_result, phase="plan_blocked")

    def _append_parameter_search_fallback_trials(
        self,
        *,
        run_id: str,
        seed: FactorDefinition,
        context: ResearchContext,
        generation: ResearchGenerationMetadata,
        lane_index: int,
        trials: list[_ResearchTrial],
        blocked_plans: list[StructuredFactorExperimentResult],
    ) -> None:
        fallback = _parameter_search_fallback_hypothesis(seed)
        structured = _structured_hypothesis_from_candidate(
            fallback,
            seed,
            generation,
            lane_index=lane_index,
        )
        fallback_plan = self.experiment_planner.plan(structured, context)
        self._trace_plan(run_id, structured, fallback_plan)
        if fallback_plan.status == "ready":
            for effective_config in self.effective_trial_configs:
                trials.append(_ResearchTrial(fallback, seed, effective_config, fallback_plan))
            return
        fallback_result = _blocked_structured_result(
            fallback_plan,
            artifact_refs=_operator_draft_refs(self.artifact_root, fallback_plan),
        )
        blocked_plans.append(fallback_result)
        self._trace_structured_result(run_id, fallback_result, phase="plan_blocked")

    def _append_duplicate_exhaustion_formula_fallback_trials(
        self,
        *,
        run_id: str,
        repo: FactorRepository,
        seed: FactorDefinition,
        context: ResearchContext,
        generation: ResearchGenerationMetadata,
        lane_index: int,
        formula_index: dict[str, str],
        shape_counts: Counter[str],
        dedup_summary: dict[str, object],
        trials: list[_ResearchTrial],
        blocked_plans: list[StructuredFactorExperimentResult],
    ) -> None:
        for fallback in _duplicate_exhaustion_fallback_hypotheses(seed):
            draft = _candidate_from_hypothesis(fallback, seed.horizon_days)
            structured = _structured_hypothesis_from_candidate(fallback, draft, generation, lane_index=lane_index)
            fallback_plan = self.experiment_planner.plan(structured, context)
            self._trace_plan(run_id, structured, fallback_plan)
            if fallback_plan.status != "ready":
                fallback_result = _blocked_structured_result(
                    fallback_plan,
                    artifact_refs=_operator_draft_refs(self.artifact_root, fallback_plan),
                )
                blocked_plans.append(fallback_result)
                self._trace_structured_result(run_id, fallback_result, phase="plan_blocked")
                continue
            planned_candidate = replace(
                draft,
                formula=fallback_plan.formula_dsl,
                universe_filters=fallback_plan.universe_filters,
            )
            duplicate_plan = _deduplicate_plan(
                fallback_plan,
                planned_candidate,
                formula_index=formula_index,
                shape_counts=shape_counts,
                config=self.deduplication,
                allow_existing=False,
            )
            if duplicate_plan is not None:
                _record_dedup_skip(dedup_summary, duplicate_plan)
                duplicate_result = _blocked_structured_result(
                    duplicate_plan,
                    artifact_refs=_operator_draft_refs(self.artifact_root, duplicate_plan),
                )
                blocked_plans.append(duplicate_result)
                self._trace_structured_result(run_id, duplicate_result, phase="plan_blocked")
                continue
            candidate = self._load_or_save_candidate(repo, planned_candidate)
            for effective_config in self.effective_trial_configs:
                trials.append(_ResearchTrial(fallback, candidate, effective_config, fallback_plan))
            return

    def _repair_invalid_llm_plan(
        self,
        *,
        run_id: str,
        seed: FactorDefinition,
        context: ResearchContext,
        objective: str,
        generation: ResearchGenerationMetadata,
        hypothesis: ResearchHypothesis,
        plan: FactorExperimentPlan,
        lane_index: int,
    ) -> _RepairOutcome | None:
        if not _should_repair_llm_plan(hypothesis, generation, plan, self.llm_formula_repair_attempts):
            return None
        repair = getattr(self.hypothesis_generator, "repair_invalid_hypothesis", None)
        if not callable(repair):
            return None

        current_hypothesis = hypothesis
        current_plan = plan
        for attempt in range(1, self.llm_formula_repair_attempts + 1):
            self._raise_if_cancelled()
            try:
                repaired = repair(
                    seed,
                    hypothesis=current_hypothesis,
                    context=context,
                    objective=objective,
                    validation_error=_plan_validation_error(current_plan),
                    attempt=attempt,
                    max_attempts=self.llm_formula_repair_attempts,
                )
            except Exception as exc:
                return _RepairOutcome(None, None, _repair_failed_plan(current_plan, exc), exhausted=True)
            if not isinstance(repaired, ResearchHypothesis):
                continue
            repaired = _normalize_llm_research_hypothesis(repaired, generation)
            if _llm_formula_required_but_missing(repaired, generation):
                structured = _structured_hypothesis_for_missing_formula(repaired, generation, lane_index=lane_index)
                draft = None
            else:
                draft = _candidate_from_hypothesis(repaired, seed.horizon_days)
                structured = _structured_hypothesis_from_candidate(repaired, draft, generation, lane_index=lane_index)
            repaired_plan = self.experiment_planner.plan(structured, context)
            self._trace_plan(run_id, structured, repaired_plan)
            current_hypothesis = repaired
            current_plan = repaired_plan
            if repaired_plan.status == "ready" and draft is not None:
                return _RepairOutcome(repaired, draft, repaired_plan)
        return _RepairOutcome(None, None, current_plan, exhausted=True)

    def _repair_duplicate_llm_plan(
        self,
        *,
        run_id: str,
        seed: FactorDefinition,
        context: ResearchContext,
        objective: str,
        generation: ResearchGenerationMetadata,
        hypothesis: ResearchHypothesis,
        duplicate_plan: FactorExperimentPlan,
        lane_index: int,
        formula_index: dict[str, str],
        shape_counts: Counter[str],
    ) -> _RepairOutcome | None:
        if not _should_repair_llm_plan(hypothesis, generation, duplicate_plan, self.llm_formula_repair_attempts):
            return None
        repair = getattr(self.hypothesis_generator, "repair_invalid_hypothesis", None)
        if not callable(repair):
            return None

        current_hypothesis = hypothesis
        current_plan = duplicate_plan
        forbidden_formulas: list[str] = [duplicate_plan.formula_dsl]
        for attempt in range(1, self.llm_formula_repair_attempts + 1):
            self._raise_if_cancelled()
            try:
                repaired = repair(
                    seed,
                    hypothesis=current_hypothesis,
                    context=context,
                    objective=objective,
                    validation_error=_duplicate_repair_validation_error(current_plan, forbidden_formulas),
                    attempt=attempt,
                    max_attempts=self.llm_formula_repair_attempts,
                )
            except Exception as exc:
                return _RepairOutcome(None, None, _repair_failed_plan(current_plan, exc), exhausted=True)
            if not isinstance(repaired, ResearchHypothesis):
                continue
            repaired = _normalize_llm_research_hypothesis(repaired, generation)
            if _llm_formula_required_but_missing(repaired, generation):
                structured = _structured_hypothesis_for_missing_formula(repaired, generation, lane_index=lane_index)
                draft = None
            else:
                draft = _candidate_from_hypothesis(repaired, seed.horizon_days)
                structured = _structured_hypothesis_from_candidate(repaired, draft, generation, lane_index=lane_index)
            repaired_plan = self.experiment_planner.plan(structured, context)
            self._trace_plan(run_id, structured, repaired_plan)
            current_hypothesis = repaired
            current_plan = repaired_plan
            if repaired_plan.status != "ready" or draft is None:
                continue
            planned_candidate = replace(
                draft,
                formula=repaired_plan.formula_dsl,
                universe_filters=repaired_plan.universe_filters,
            )
            duplicate_again = _deduplicate_plan(
                repaired_plan,
                planned_candidate,
                formula_index=formula_index,
                shape_counts=shape_counts,
                config=self.deduplication,
                allow_existing=repaired.parameter_search_fallback,
            )
            if duplicate_again is None:
                return _RepairOutcome(repaired, draft, repaired_plan)
            current_plan = duplicate_again
            if duplicate_again.formula_dsl:
                forbidden_formulas.append(duplicate_again.formula_dsl)
        return _RepairOutcome(None, None, current_plan, exhausted=True)

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
    ) -> StructuredFactorExperimentResult:
        structured = _structured_result_from_candidate(candidate, plan)
        self._trace_structured_result(run_id, structured, phase="experiment_result")
        return structured

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
            self._raise_if_cancelled()
            try:
                scored_trials.append(
                    self._score_trial(
                        trial,
                        objective_weights,
                        horizon_days_matrix=self.quick_horizon_days_matrix,
                        sample_splits=self.quick_sample_splits,
                    )
                )
            except _ResearchRunCancelled:
                raise
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
                simulation_profile=scored.backtest.simulation_profile,
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
        include_external_oos: bool = False,
    ) -> _ScoredTrial:
        self._raise_if_cancelled()
        evaluation_profile = trial.effective_config.evaluation_profile
        backtest_profile = trial.effective_config.backtest_profile
        evaluation = evaluate_factor(
            trial.factor.factor_id,
            factor_root=self.factor_root,
            data_root=self.data_root,
            artifact_root=self.artifact_root,
            horizon_days=trial.factor.horizon_days,
            horizon_days_matrix=horizon_days_matrix,
            sample_splits=sample_splits,
            simulation_profile=evaluation_profile,
            factor_values_root=self.factor_values_root,
            factor_values_overlay_root=self.factor_values_overlay_root,
            factor_values_manifest_root=self.factor_values_manifest_root,
        )
        self._raise_if_cancelled()
        backtest = run_factor_backtest(
            trial.factor.factor_id,
            factor_root=self.factor_root,
            data_root=self.data_root,
            artifact_root=self.artifact_root,
            holding_days=trial.factor.horizon_days,
            simulation_profile=evaluation_profile,
            transaction_costs=self.transaction_costs,
            sample_splits=sample_splits,
            factor_values_root=self.factor_values_root,
            factor_values_overlay_root=self.factor_values_overlay_root,
            factor_values_manifest_root=self.factor_values_manifest_root,
            sample_role=IN_SAMPLE_ROLE,
        )
        self._raise_if_cancelled()
        external_oos_backtest = None
        if include_external_oos:
            external_oos_backtest = run_factor_backtest(
                trial.factor.factor_id,
                factor_root=self.factor_root,
                data_root=self.data_root,
                artifact_root=self.artifact_root,
                holding_days=trial.factor.horizon_days,
                simulation_profile=backtest_profile,
                transaction_costs=self.transaction_costs,
                sample_splits=sample_splits,
                factor_values_root=self.factor_values_root,
                factor_values_overlay_root=self.factor_values_overlay_root,
                factor_values_manifest_root=self.factor_values_manifest_root,
                sample_role=EXTERNAL_OOS_ROLE,
            )
            self._raise_if_cancelled()
        split_weighted_icir = weighted_split_icir(evaluation)
        score = score_candidate(evaluation, backtest, objective_weights, split_weighted_icir)
        return _ScoredTrial(trial, evaluation, backtest, split_weighted_icir, score, external_oos_backtest)

    def _evaluate_final_trial(
        self,
        repo: FactorRepository,
        seed: FactorDefinition,
        trial: _ResearchTrial,
        objective_weights: ResearchObjectiveWeights,
        candidate_gate: ResearchGate,
        result_signature_index: dict[str, str] | None = None,
    ) -> ResearchCandidateResult:
        self._raise_if_cancelled()
        scored = self._score_trial(
            trial,
            objective_weights,
            horizon_days_matrix=self.horizon_days_matrix,
            sample_splits=self.sample_splits,
            include_external_oos=True,
        )
        gate_passed, gate_blocking, gate_warnings = apply_gate_detailed(
            scored.evaluation,
            scored.backtest,
            scored.score,
            candidate_gate,
            oos_backtest=scored.external_oos_backtest,
        )
        # gate_reasons is the blocking-facing channel: on a pass it carries the
        # pass marker plus visible warn-mode findings (FP-7); on a failure it
        # carries ONLY blockers — warnings ride gate_warnings separately.
        gate_reasons = (
            tuple(dict.fromkeys(("passed smoke research gate", *gate_warnings)))
            if gate_passed
            else gate_blocking
        )
        result_signature = (
            _result_signature_from_scored(scored, precision=self.deduplication.result_precision)
            if result_signature_index is not None
            else ""
        )
        if result_signature_index is not None and result_signature:
            existing_factor_id = result_signature_index.get(result_signature)
            if existing_factor_id and existing_factor_id != trial.factor.factor_id:
                gate_passed = False
                # Rebuild from the blocking channel so warn-mode findings do
                # not leak into blocking reasons when a pass flips to a
                # duplicate rejection.
                gate_reasons = (
                    *gate_blocking,
                    f"duplicate result signature matches {existing_factor_id}",
                )
            else:
                result_signature_index[result_signature] = trial.factor.factor_id
        candidate = repo.get(trial.factor.factor_id)
        transitioned_to_candidate = False
        promote_after_review = False
        if gate_passed:
            if candidate.factor_id == seed.factor_id:
                pass
            elif candidate.status == "draft":
                promote_after_review = True
            elif candidate.status in {"candidate", "active"}:
                pass
            else:
                gate_passed = False
                gate_reasons = (*gate_reasons, f"existing {candidate.status} status requires explicit user decision")
        elif candidate.status != "draft":
            gate_reasons = (*gate_reasons, f"existing {candidate.status} status preserved")
        self._raise_if_cancelled()
        try:
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
        except _ResearchRunCancelled:
            raise
        except Exception as exc:
            self_review = _failed_self_review(exc, candidate)
        self._raise_if_cancelled()
        if promote_after_review:
            candidate = self._promote_candidate(
                repo,
                candidate,
                reason="Research loop smoke gate passed; active promotion still requires user decision.",
            )
            transitioned_to_candidate = True
        external_oos_backtest = scored.external_oos_backtest or scored.backtest
        assessment = FactorAssessmentBundle(
            factor_id=candidate.factor_id,
            role="candidate",
            evaluation=scored.evaluation,
            selection_backtest=scored.backtest,
            external_oos_backtest=external_oos_backtest,
            selection_score=scored.score,
            split_weighted_icir=scored.split_weighted_icir,
            gate_passed=gate_passed,
            gate_reasons=gate_reasons,
            parent_seed_factor_id=seed.factor_id,
        )
        return ResearchCandidateResult(
            hypothesis=trial.hypothesis,
            factor=candidate,
            evaluation=scored.evaluation,
            backtest=external_oos_backtest,
            split_weighted_icir=scored.split_weighted_icir,
            score=scored.score,
            gate_passed=gate_passed,
            gate_reasons=gate_reasons,
            self_review=self_review,
            gate_warnings=gate_warnings,
            transitioned_to_candidate=transitioned_to_candidate,
            formula_fingerprint=factor_formula_fingerprint(candidate),
            result_signature=result_signature,
            candidate_shape_fingerprint=_candidate_shape_fingerprint(trial.plan, candidate),
            selection_backtest=scored.backtest,
            external_oos_backtest=external_oos_backtest,
            assessment=assessment,
        )

    def _assess_factor(
        self,
        factor: FactorDefinition,
        *,
        role: str,
        parent_seed_factor_id: str,
        objective_weights: ResearchObjectiveWeights,
        gate: ResearchGate,
    ) -> FactorAssessmentBundle:
        evaluation = evaluate_factor(
            factor.factor_id,
            factor_root=self.factor_root,
            data_root=self.data_root,
            artifact_root=self.artifact_root,
            horizon_days=factor.horizon_days,
            horizon_days_matrix=self.horizon_days_matrix,
            sample_splits=self.sample_splits,
            simulation_profile=self.evaluation_simulation_profile,
            factor_values_root=self.factor_values_root,
            factor_values_overlay_root=self.factor_values_overlay_root,
            factor_values_manifest_root=self.factor_values_manifest_root,
        )
        self._raise_if_cancelled()
        selection_backtest = run_factor_backtest(
            factor.factor_id,
            factor_root=self.factor_root,
            data_root=self.data_root,
            artifact_root=self.artifact_root,
            holding_days=factor.horizon_days,
            simulation_profile=self.evaluation_simulation_profile,
            transaction_costs=self.transaction_costs,
            sample_splits=self.sample_splits,
            factor_values_root=self.factor_values_root,
            factor_values_overlay_root=self.factor_values_overlay_root,
            factor_values_manifest_root=self.factor_values_manifest_root,
            sample_role=IN_SAMPLE_ROLE,
        )
        self._raise_if_cancelled()
        external_oos_backtest = run_factor_backtest(
            factor.factor_id,
            factor_root=self.factor_root,
            data_root=self.data_root,
            artifact_root=self.artifact_root,
            holding_days=factor.horizon_days,
            simulation_profile=self.backtest_simulation_profile,
            transaction_costs=self.transaction_costs,
            sample_splits=self.sample_splits,
            factor_values_root=self.factor_values_root,
            factor_values_overlay_root=self.factor_values_overlay_root,
            factor_values_manifest_root=self.factor_values_manifest_root,
            sample_role=EXTERNAL_OOS_ROLE,
        )
        self._raise_if_cancelled()
        split_weighted = weighted_split_icir(evaluation)
        score = score_candidate(evaluation, selection_backtest, objective_weights, split_weighted)
        gate_passed, gate_reasons = apply_gate(
            evaluation, selection_backtest, score, gate, oos_backtest=external_oos_backtest
        )
        return FactorAssessmentBundle(
            factor_id=factor.factor_id,
            role=role,
            evaluation=evaluation,
            selection_backtest=selection_backtest,
            external_oos_backtest=external_oos_backtest,
            selection_score=score,
            split_weighted_icir=split_weighted,
            gate_passed=gate_passed,
            gate_reasons=gate_reasons,
            parent_seed_factor_id=parent_seed_factor_id,
        )

    def _append_rd_run_index_row(
        self,
        *,
        run_id: str,
        seed_factor_id: str,
        result: ResearchLoopResult,
        seed_assessment: FactorAssessmentBundle | None,
        config_snapshot: dict[str, Any],
    ) -> None:
        """Append one honest run-history row (kind "rd") at run completion.

        Artifact paths are stored relative to ``artifact_root`` (or dropped
        when outside it); metric highlights keep their MetricValue statuses so
        an unavailable metric is never rendered as a number (FP-2/FP-4).
        ``seed_assessment`` is None when the seed itself could not be scored
        (BUG #006); the run is still recorded, honestly, with empty highlights
        and an unavailable data_window rather than being skipped or crashing
        on a missing evaluation.
        """
        created_at = datetime.now(UTC)
        best = result.candidates[0] if result.candidates else None
        highlight_evaluation = (
            best.evaluation if best is not None else (seed_assessment.evaluation if seed_assessment is not None else None)
        )
        highlights = (
            {
                name: metric_highlight(highlight_evaluation.metrics[name])
                for name in RD_RUN_HIGHLIGHT_METRICS
                if name in highlight_evaluation.metrics
            }
            if highlight_evaluation is not None
            else {}
        )
        artifact_paths_rel = [
            path_rel
            for path_rel in (
                relative_artifact_path(self.artifact_root, result.report_path),
                relative_artifact_path(self.artifact_root, result.trace_root),
            )
            if path_rel
        ]
        RunIndex(self.artifact_root).append_run(
            run_id=run_id,
            kind=RD_RUN_INDEX_KIND,
            factor_ids=tuple(dict.fromkeys((seed_factor_id, *result.accepted_candidate_ids))),
            created_at=created_at.isoformat(),
            data_window=(
                evaluation_data_window(highlight_evaluation)
                if highlight_evaluation is not None
                else {"start_date": None, "end_date": None, "status": "unavailable"}
            ),
            config_fingerprint=canonical_fingerprint(
                {"kind": RD_RUN_INDEX_KIND, "seed_factor_id": seed_factor_id, "config": config_snapshot}
            ),
            metric_highlights=highlights,
            artifact_paths_rel=artifact_paths_rel,
            warnings_count=_rd_warnings_count(seed_assessment, result.candidates),
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


class _RequiredMetricUnavailable(ValueError):
    """A nonzero-weight scoring component has no available metric value.

    BUG #006: this is a ``ValueError`` subclass (not a new exception family)
    so every existing ``except ValueError`` / ``except Exception`` catch —
    including the pre-existing per-trial rejection handling in ``run_once``'s
    final-trial loop and (now) the seed-assessment handling right above it —
    keeps catching it unchanged. The message is a typed, parseable reason
    (``metric_unavailable:<name> (<status>[: <warning codes>])``) so a
    rejected candidate/seed carries WHY it was skipped instead of an opaque
    trace, matching the ``INSUFFICIENT_*``-style typed reasons already used by
    ``candidate_gate``.
    """


def score_candidate(
    evaluation: EvaluationResult,
    backtest: BacktestResult,
    weights: ResearchObjectiveWeights,
    split_weighted_icir: float | None = None,
) -> float:
    """Weighted objective score.

    Weight-gated laziness (BUG #006, F-1/FP-2 style honesty): a component
    whose weight is 0 is never fetched, so a metric that is genuinely
    unavailable (e.g. ``net_annualized_return`` under
    ``INSUFFICIENT_ANNUALIZATION_HISTORY`` on a short RD window) cannot raise
    when the objective does not even use it. A component with a NONZERO
    weight whose required metric is unavailable still raises
    ``_RequiredMetricUnavailable`` — the caller (a candidate's final-trial
    evaluation, or the seed's own assessment) is responsible for treating
    that as "this factor cannot be scored" rather than letting it propagate
    and abort the whole run.
    """

    split_component = 0.0
    if weights.weighted_split_icir > 0:
        split_component = (
            split_weighted_icir if split_weighted_icir is not None else weighted_split_icir(evaluation)
        ) / 10.0
    normalized_icir = evaluation.rank_icir / 10.0
    score = (
        split_component * weights.weighted_split_icir
        + evaluation.rank_ic_mean * weights.rank_ic_mean
        + normalized_icir * weights.rank_icir
    )
    if weights.annualized_return > 0:
        score += _required_backtest_metric(backtest, "net_annualized_return") * weights.annualized_return
    if weights.max_drawdown > 0:
        score += _required_backtest_metric(backtest, "net_max_drawdown") * weights.max_drawdown
    return float(score)


def _required_backtest_metric(backtest: BacktestResult, name: str) -> float:
    metric = backtest.metrics.get(name)
    if isinstance(metric, MetricValue):
        if metric.status != "available" or metric.value is None:
            detail = metric.status
            if metric.warning_codes:
                detail = f"{detail}: {', '.join(metric.warning_codes)}"
            raise _RequiredMetricUnavailable(f"metric_unavailable:{name} ({detail})")
        return float(metric.value)
    value = getattr(backtest, name)
    if value is None:
        raise _RequiredMetricUnavailable(f"metric_unavailable:{name} (missing)")
    return float(value)


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


def _assessment_comparison_row(
    assessment: FactorAssessmentBundle,
    factor: FactorDefinition,
    hypothesis_text: str = "",
) -> dict[str, object]:
    selection = assessment.selection_backtest
    external = assessment.external_oos_backtest
    return {
        "role": assessment.role,
        "factor_id": assessment.factor_id,
        "parent_seed_factor_id": assessment.parent_seed_factor_id,
        "factor_status": factor.status,
        "formula": factor.formula,
        "hypothesis": hypothesis_text,
        "selection_basis": assessment.selection_basis,
        "audit_basis": assessment.audit_basis,
        "selection_score": assessment.selection_score,
        "split_weighted_icir": assessment.split_weighted_icir,
        "gate_passed": assessment.gate_passed,
        "gate_reasons": list(assessment.gate_reasons),
        "selection_rank_ic": assessment.evaluation.rank_ic_mean,
        "selection_icir": assessment.evaluation.rank_icir,
        "selection_ic_days": assessment.evaluation.ic_days,
        "selection_coverage": assessment.evaluation.coverage,
        "selection_backtest_periods": selection.periods,
        "selection_completed_periods": selection.completed_periods,
        "selection_net_cumulative_return": selection.net_cumulative_return,
        "selection_net_annualized_return": selection.net_annualized_return,
        "selection_net_long_short_sharpe": selection.net_long_short_sharpe,
        "selection_turnover_rate": selection.turnover_rate,
        "selection_rebalance_rate": selection.rebalance_rate,
        "external_oos_periods": external.periods,
        "external_oos_completed_periods": external.completed_periods,
        "external_oos_net_cumulative_return": external.net_cumulative_return,
        "external_oos_net_annualized_return": external.net_annualized_return,
        "external_oos_net_long_short_sharpe": external.net_long_short_sharpe,
        "external_oos_turnover_rate": external.turnover_rate,
        "external_oos_rebalance_rate": external.rebalance_rate,
        "evaluation_artifact_path": assessment.evaluation.artifact_path,
        "selection_backtest_artifact_path": selection.artifact_path,
        "external_oos_artifact_path": external.artifact_path,
    }


def apply_gate(
    evaluation: EvaluationResult,
    backtest: BacktestResult,
    score: float,
    gate: ResearchGate,
    *,
    oos_backtest: BacktestResult | None = None,
) -> tuple[bool, tuple[str, ...]]:
    """Research smoke gate; clause definitions shared with candidate_gate (F-2, FP-5).

    Compatibility surface over ``apply_gate_detailed``: on a pass the reasons
    tuple keeps warn-mode findings visible next to the pass marker (FP-7); on
    a failure it carries ONLY blocking reasons — warn-mode messages ride the
    separate warnings channel and must never read as blockers.
    """

    passed, blocking, warnings = apply_gate_detailed(
        evaluation, backtest, score, gate, oos_backtest=oos_backtest
    )
    if passed:
        return True, tuple(dict.fromkeys(("passed smoke research gate", *warnings)))
    return False, blocking


def apply_gate_detailed(
    evaluation: EvaluationResult,
    backtest: BacktestResult,
    score: float,
    gate: ResearchGate,
    *,
    oos_backtest: BacktestResult | None = None,
) -> tuple[bool, tuple[str, ...], tuple[str, ...]]:
    """Research smoke gate returning ``(passed, blocking, warnings)``.

    The warn-mode channel (missing OOS/net-return evidence with
    ``missing_oos_evidence_blocks=False``) stays separate from the blocking
    payload so downstream consumers (memory failure statements, structured
    gate decisions) can never mistake a warning for a blocker.
    """

    # The OOS-specific gate clauses must judge the external out-of-sample
    # backtest when a distinct holdout window is configured; when no external
    # OOS backtest is supplied we fall back to the in-sample backtest so the
    # default (evaluation_profile == backtest_profile) behavior is unchanged.
    oos = oos_backtest if oos_backtest is not None else backtest
    blocking: list[str] = []
    warnings: list[str] = []
    if evaluation.ic_days < gate.min_ic_days:
        blocking.append(f"ic_days {evaluation.ic_days} < {gate.min_ic_days}")
    if evaluation.coverage < gate.min_coverage:
        blocking.append(f"coverage {evaluation.coverage:.4f} < {gate.min_coverage:.4f}")
    if backtest.periods < gate.min_backtest_periods:
        blocking.append(f"backtest_periods {backtest.periods} < {gate.min_backtest_periods}")
    if score < gate.min_score:
        blocking.append(f"score {score:.6f} < {gate.min_score:.6f}")
    segments = _segment_gate_evidence(oos)
    if gate.min_oos_net_annualized_return is not None:
        clause_blocking, clause_warnings = min_oos_return_reasons(
            oos_return_evidence(segments),
            gate.min_oos_net_annualized_return,
            missing_blocks=gate.missing_oos_evidence_blocks,
        )
        blocking.extend(clause_blocking)
        warnings.extend(clause_warnings)
    # FP-5 completion: the turnover-family clauses consume the same shared
    # helper as the structured candidate gate (one definition, one message
    # format). The research gate has no turnover warn channel, so findings
    # always block here.
    blocking.extend(
        turnover_reasons(
            {"rebalance_rate": backtest.rebalance_rate, "turnover_rate": backtest.turnover_rate},
            max_rebalance_rate=gate.max_rebalance_rate,
            max_turnover_rate=gate.max_turnover_rate,
        )
    )
    if gate.min_net_return_retention is not None:
        clause_blocking, clause_warnings = min_net_return_retention_reasons(
            net_return_retention_value(oos.net_annualized_return, oos.annualized_return),
            gate.min_net_return_retention,
            missing_blocks=gate.missing_oos_evidence_blocks,
        )
        blocking.extend(clause_blocking)
        warnings.extend(clause_warnings)
    if gate.max_oos_net_return_decay is not None:
        clause_blocking, clause_warnings = max_oos_decay_reasons(
            segments,
            gate.max_oos_net_return_decay,
            missing_blocks=gate.missing_oos_evidence_blocks,
        )
        blocking.extend(clause_blocking)
        warnings.extend(clause_warnings)
    blocking_out = tuple(dict.fromkeys(blocking))
    warnings_out = tuple(dict.fromkeys(warnings))
    return not blocking_out, blocking_out, warnings_out


def _segment_gate_evidence(backtest: BacktestResult) -> tuple[SegmentEvidence, ...]:
    return tuple(
        SegmentEvidence(
            name=metric.name,
            net_annualized_return=metric.net_annualized_return,
            periods=metric.periods,
        )
        for metric in backtest.segment_metrics
    )


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


def _trial_key(trial: _ResearchTrial) -> tuple[str, ResearchEffectiveTrialConfig]:
    return (trial.factor.factor_id, trial.effective_config)


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


def _effective_trial_config_snapshot(config: ResearchEffectiveTrialConfig) -> dict[str, object]:
    return {
        "overlay": _trial_simulation_overlay_snapshot(config.overlay),
        "evaluation_profile": asdict(config.evaluation_profile),
        "backtest_profile": asdict(config.backtest_profile),
    }


def _trial_simulation_overlay_snapshot(overlay: ResearchTrialSimulationOverlay) -> dict[str, object]:
    snapshot: dict[str, object] = {
        "top_quantile": overlay.top_quantile,
        "decay_days": overlay.decay_days,
    }
    if overlay.profile is not None:
        snapshot["profile"] = asdict(overlay.profile)
    return snapshot


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
    for entry in trace_store.read_recent_entries(limit=limit, phases={"experiment_result"}):
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


def _duplicate_exhaustion_fallback_hypotheses(seed: FactorDefinition) -> tuple[ResearchHypothesis, ...]:
    horizon = f"{seed.horizon_days}日"
    return (
        ResearchHypothesis(
            text=f"重复公式耗尽兜底：用{horizon}收益的时间序排名替换线性收益腿",
            rationale=(
                "LLM duplicate repair did not produce a distinct executable formula; use a bounded "
                "non-additive momentum-persistence variant that differs from the seed formula."
            ),
            source="local",
            source_detail="duplicate_exhaustion_bounded_formula_fallback: momentum persistence",
            formula_dsl="rank((1 - rank(market_cap)) * rank(ts_rank(return_5d, 10)))",
            input_fields=("market_cap", "return_5d"),
            expected_direction="positive",
            universe_constraints=seed.universe_filters,
        ),
        ResearchHypothesis(
            text=f"重复公式耗尽兜底：用低波动和短期均值收益构造{horizon}防御动量",
            rationale=(
                "LLM duplicate repair repeated existing formulas; use a bounded low-volatility "
                "and smoothed-return interaction to keep the candidate distinct from the seed."
            ),
            source="local",
            source_detail="duplicate_exhaustion_bounded_formula_fallback: defensive smoothed momentum",
            formula_dsl="rank((1 - rank(volatility_5d)) * rank(ts_mean(return_5d, 5)))",
            input_fields=("volatility_5d", "return_5d"),
            expected_direction="positive",
            universe_constraints=seed.universe_filters,
        ),
        ResearchHypothesis(
            text=f"重复公式耗尽兜底：用小市值和收益稳定性构造{horizon}稳健候选",
            rationale=(
                "LLM duplicate repair was exhausted; use a bounded small-cap and return-stability "
                "variant so the fallback is a real formula candidate instead of seed reuse."
            ),
            source="local",
            source_detail="duplicate_exhaustion_bounded_formula_fallback: return stability",
            formula_dsl="rank((1 - rank(market_cap)) * (1 - rank(stddev(return_5d, 20))))",
            input_fields=("market_cap", "return_5d"),
            expected_direction="positive",
            universe_constraints=seed.universe_filters,
        ),
    )


def _should_repair_llm_plan(
    hypothesis: ResearchHypothesis,
    generation: ResearchGenerationMetadata,
    plan: FactorExperimentPlan,
    max_attempts: int,
) -> bool:
    if max_attempts <= 0 or hypothesis.parameter_search_fallback:
        return False
    if not _is_llm_research_hypothesis(hypothesis, generation):
        return False
    return plan.status in {
        "blocked_formula_invalid",
        "blocked_missing_field",
        "blocked_missing_formula",
        "blocked_direction_unknown",
        "blocked_duplicate_formula",
    }


def _plan_validation_error(plan: FactorExperimentPlan) -> str:
    reasons = "; ".join(plan.blocking_reasons)
    if reasons:
        return reasons
    if not plan.operator_validation.get("is_valid", True):
        return "formula validation failed"
    return f"plan status is {plan.status}"


def _repair_failed_plan(plan: FactorExperimentPlan, exc: Exception) -> FactorExperimentPlan:
    message = f"LLM formula repair failed: {exc.__class__.__name__}: {exc}"
    return replace(plan, blocking_reasons=tuple(dict.fromkeys((*plan.blocking_reasons, message))))


def _duplicate_repair_validation_error(plan: FactorExperimentPlan, forbidden_formulas: list[str]) -> str:
    error = _plan_validation_error(plan)
    formulas = tuple(dict.fromkeys(formula for formula in forbidden_formulas if formula))
    if not formulas:
        return error
    return (
        f"{error}; forbidden_formula_dsl={json.dumps(formulas, ensure_ascii=False)}; "
        "return a formula_dsl not equal to any forbidden_formula_dsl and not already present in the factor library"
    )


def _optimization_performed(
    results: list[ResearchCandidateResult],
    seed: FactorDefinition,
    seed_profile: SimulationProfile,
) -> bool:
    return any(
        not (result.hypothesis.parameter_search_fallback and result.factor.factor_id == seed.factor_id)
        and (
            result.factor.factor_id != seed.factor_id
            or result.factor.formula != seed.formula
            or result.factor.universe_filters != seed.universe_filters
            or result.factor.horizon_days != seed.horizon_days
            or (result.selection_backtest or result.backtest).simulation_profile != seed_profile
        )
        for result in results
    )


def _trial_overlays_from_profiles(
    profiles: tuple[SimulationProfile, ...],
) -> tuple[ResearchTrialSimulationOverlay, ...]:
    return tuple(ResearchTrialSimulationOverlay(profile=profile) for profile in profiles)


def _effective_trial_config(
    overlay: ResearchTrialSimulationOverlay,
    *,
    evaluation_profile: SimulationProfile,
    backtest_profile: SimulationProfile,
) -> ResearchEffectiveTrialConfig:
    return ResearchEffectiveTrialConfig(
        overlay=overlay,
        evaluation_profile=_role_profile_for_trial(evaluation_profile, overlay),
        backtest_profile=_role_profile_for_trial(backtest_profile, overlay),
    )


def _role_profile_for_trial(
    role_profile: SimulationProfile,
    overlay: ResearchTrialSimulationOverlay,
) -> SimulationProfile:
    profile = overlay.profile or role_profile
    updates: dict[str, float | int] = {}
    if overlay.top_quantile is not None:
        updates["top_quantile"] = overlay.top_quantile
    if overlay.decay_days is not None:
        updates["decay_days"] = overlay.decay_days
    if not updates:
        return profile
    return replace(profile, **updates)


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
    selection_backtest = candidate.selection_backtest or candidate.backtest
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
    backtest_metrics = _backtest_metrics(selection_backtest)
    # Additive trace evidence for the next round's strategy selector: worst
    # OOS-vs-IS net-return decay from the same backtest the gate audited
    # (external OOS when configured, else the selection backtest). None, never
    # a fabricated number, when segment evidence is missing (FP-2/FP-4).
    backtest_metrics["oos_net_return_decay"] = _oos_net_return_decay_value(candidate.backtest)
    decision = evaluate_structured_candidate(
        StructuredFactorExperimentResult(
            plan=experiment_plan,
            candidate_ref=candidate.factor.factor_id,
            evaluation_status="completed",
            evaluation_metrics=_evaluation_metrics(candidate.evaluation),
            backtest_status="completed",
            backtest_metrics=backtest_metrics,
            artifact_refs=_artifact_refs(candidate),
        )
    )
    # Warn-mode smoke-gate findings ride the decision's warnings channel on
    # both branches — never blocking_reasons — so a downgraded
    # INSUFFICIENT_OOS_EVIDENCE message stays distinguishable from a blocker.
    merged_warnings = tuple(dict.fromkeys((*decision.warnings, *candidate.gate_warnings)))
    if candidate.gate_passed:
        decision = replace(
            decision,
            should_transition_to_candidate=candidate.transitioned_to_candidate,
            transition_reason=decision.transition_reason
            if candidate.transitioned_to_candidate
            else "Candidate passed research gates without a new status transition.",
            warnings=merged_warnings,
        )
    else:
        decision = replace(
            decision,
            status="blocked",
            accepted=False,
            blocking_reasons=tuple(dict.fromkeys((*decision.blocking_reasons, *candidate.gate_reasons))),
            warnings=merged_warnings,
            should_transition_to_candidate=False,
            transition_reason="",
        )
    return StructuredFactorExperimentResult(
        plan=experiment_plan,
        candidate_ref=candidate.factor.factor_id,
        evaluation_status="completed",
        evaluation_metrics=_evaluation_metrics(candidate.evaluation),
        backtest_status="completed",
        backtest_metrics=backtest_metrics,
        artifact_refs=_artifact_refs(candidate),
        gate_decision=decision,
    )


def _evaluation_metrics(evaluation: EvaluationResult) -> dict[str, object]:
    return {
        "observations": evaluation.observations,
        "coverage": evaluation.coverage,
        "rank_ic_mean": evaluation.rank_ic_mean,
        "rank_icir": evaluation.rank_icir,
        "rank_ic_t_stat": evaluation.rank_ic_t_stat,
        "ic_days": evaluation.ic_days,
        "score_source": evaluation.score_source,
        "score_cached_rows": evaluation.score_cached_rows,
        "score_computed_rows": evaluation.score_computed_rows,
        "score_compute_mode": evaluation.score_compute_mode,
        "score_missing_ratio": evaluation.score_missing_ratio,
        "score_context_rows": evaluation.score_context_rows,
    }


def _backtest_metrics(backtest: BacktestResult) -> dict[str, object]:
    return {
        "periods": backtest.periods,
        "annualized_return": backtest.annualized_return,
        "net_annualized_return": backtest.net_annualized_return,
        "max_drawdown": backtest.max_drawdown,
        "net_max_drawdown": backtest.net_max_drawdown,
        "net_long_short_sharpe": backtest.net_long_short_sharpe,
        "rebalance_rate": backtest.rebalance_rate,
        "turnover_rate": backtest.turnover_rate,
        # F-3: propagate segment evidence so externally configured OOS clauses
        # on the structured candidate gate judge real segments instead of
        # fail-closing on absent evidence (FP-2); the gate reads these via
        # candidate_gate._segments.
        "segment_metrics": [asdict(metric) for metric in backtest.segment_metrics],
        "score_source": backtest.score_source,
        "score_cached_rows": backtest.score_cached_rows,
        "score_computed_rows": backtest.score_computed_rows,
        "score_compute_mode": backtest.score_compute_mode,
        "score_missing_ratio": backtest.score_missing_ratio,
        "score_context_rows": backtest.score_context_rows,
    }


def _artifact_refs(candidate: ResearchCandidateResult) -> dict[str, str]:
    selection_backtest = candidate.selection_backtest or candidate.backtest
    external_oos_backtest = candidate.external_oos_backtest or candidate.backtest
    return {
        "evaluation": candidate.evaluation.artifact_path.name,
        "backtest": selection_backtest.artifact_path.name,
        "selection_backtest": selection_backtest.artifact_path.name,
        "external_oos_backtest": external_oos_backtest.artifact_path.name,
        "formula_fingerprint": candidate.formula_fingerprint,
        "result_signature": candidate.result_signature,
        "candidate_shape_fingerprint": candidate.candidate_shape_fingerprint,
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


def _factor_exists(repo: FactorRepository, factor_id: str) -> bool:
    try:
        repo.get(factor_id)
    except FileNotFoundError:
        return False
    return True


def _oos_decay(evaluation: EvaluationResult) -> bool:
    split_by_name = {metric.name.upper(): metric for metric in evaluation.split_metrics}
    is_metric = split_by_name.get("IS")
    oos2_metric = split_by_name.get("OOS2")
    if is_metric is None or oos2_metric is None:
        return False
    if is_metric.ic_days == 0 or oos2_metric.ic_days == 0:
        return False
    return oos2_metric.rank_icir < is_metric.rank_icir * 0.5


def _cost_sensitive(backtest: BacktestResult) -> bool:
    if backtest.annualized_return is None or backtest.net_annualized_return is None:
        return False
    return backtest.annualized_return > 0 and backtest.net_annualized_return < backtest.annualized_return * 0.5


def _oos_net_return_decay_value(backtest: BacktestResult) -> float | None:
    """Worst OOS-vs-IS net-return decay, ``1 - min(OOS/IS)``, from segments.

    Returns None — never a fabricated number (FP-2/FP-4) — when the IS
    baseline or every OOS segment lacks ``net_annualized_return`` evidence,
    or when the IS baseline is non-positive so the ratio is undefined. At the
    selector's 0.5 threshold this matches the shared gate definition
    ``candidate_gate.oos_net_decay_exceeded`` (F-2, FP-5): ``1 - ratio > 0.5``
    if and only if ``ratio < 0.5``.
    """

    split_by_name = {metric.name.upper(): metric for metric in backtest.segment_metrics}
    is_metric = split_by_name.get("IS")
    if is_metric is None or is_metric.periods == 0 or is_metric.net_annualized_return is None:
        return None
    if is_metric.net_annualized_return <= 0:
        return None
    ratios = [
        metric.net_annualized_return / is_metric.net_annualized_return
        for name, metric in split_by_name.items()
        if name.startswith("OOS") and metric.periods > 0 and metric.net_annualized_return is not None
    ]
    if not ratios:
        return None
    decay = 1.0 - min(ratios)
    return float(decay) if isfinite(decay) else None


def _finite_float_or_none(value: Any) -> float | None:
    """A finite float, or None for missing/non-numeric/NaN/inf evidence."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if isfinite(number) else None


_RUN_ID_SUFFIX_RE = re.compile(r"\d{8}T\d{12}Z_[0-9a-f]{8}")


def _run_id_in_seed_chain(
    run_id: str,
    *,
    seed_factor_id: str,
    current_run_id: str,
) -> bool:
    """True when ``run_id`` is the current run or a prior run of the SAME seed.

    Run ids are structural (``rd_<safe_seed>_<UTC timestamp>_<hex8>``, see
    :func:`_research_run_id`), so the seed chain is recognized without reading
    entry payloads. The timestamp/uuid suffix match prevents one seed id that
    is a prefix of another (``FTR_A`` vs ``FTR_A_X``) from matching the wrong
    chain. Passed to ``ResearchTraceStore.read_recent_entries`` as its
    ``run_id_filter`` so seed scoping happens BEFORE the window limit (C2).
    """

    if not run_id:
        return False
    if run_id == current_run_id:
        return True
    prefix = f"rd_{_safe_id(seed_factor_id)}_"
    if not run_id.startswith(prefix):
        return False
    return _RUN_ID_SUFFIX_RE.fullmatch(run_id[len(prefix) :]) is not None


def _strategy_context_from_trace_entries(entries: list[dict[str, Any]]) -> StrategyContext:
    """Build the selector's evidence snapshot from persisted trace state only.

    ``entries`` must already be scoped to the current seed's run chain (F3:
    no cross-seed contamination; ``round_index`` counts prior rounds of this
    seed, and ``recent_fingerprints`` aggregate only across runs of the same
    seed). Every input is derived from prior rounds' recorded evidence
    (duplicate counters via round summaries or plan statuses, OOS decay via
    the segment decay value traced with each result, gate reasons from the
    last gate decision). Missing evidence stays None/empty — never a
    fabricated value.
    """

    run_ids: set[str] = set()
    candidate_count = 0
    blocked_plan_count = 0
    failed_plan_count = 0
    duplicate_plan_count = 0
    last_round_summary: dict[str, Any] | None = None
    last_result_entry: dict[str, Any] | None = None
    mechanisms: list[str] = []
    fingerprints: list[str] = []
    for entry in entries:
        run_id = str(entry.get("run_id") or "")
        if run_id:
            run_ids.add(run_id)
        phase = str(entry.get("phase") or "")
        if phase == "experiment_failed":
            failed_plan_count += 1
        elif phase == "plan_blocked":
            blocked_plan_count += 1
            plan = entry.get("experiment_plan")
            if isinstance(plan, dict):
                if str(plan.get("status") or "") in _DUPLICATE_PLAN_STATUSES:
                    duplicate_plan_count += 1
                metadata = plan.get("metadata")
                if isinstance(metadata, dict):
                    fingerprint = str(metadata.get("formula_fingerprint") or "")
                    if fingerprint:
                        fingerprints.append(fingerprint)
        elif phase == "experiment_result":
            candidate_count += 1
            last_result_entry = entry
            decision = entry.get("gate_decision")
            formula = str(entry.get("formula_dsl") or "")
            if isinstance(decision, dict) and bool(decision.get("accepted")) and formula:
                mechanisms.append(formula)
            refs = entry.get("artifact_refs")
            if isinstance(refs, dict):
                fingerprint = str(refs.get("formula_fingerprint") or "")
                if fingerprint:
                    fingerprints.append(fingerprint)
        elif phase == "round_summary":
            summary = entry.get("round_summary")
            if isinstance(summary, dict):
                last_round_summary = summary

    duplicate_rate = None
    best_objective_score = None
    best_score_delta_vs_seed = None
    if last_round_summary is not None:
        duplicate_rate = _finite_float_or_none(last_round_summary.get("duplicate_rate"))
        best_objective_score = _finite_float_or_none(last_round_summary.get("best_score"))
        best_score_delta_vs_seed = _finite_float_or_none(last_round_summary.get("best_score_delta_vs_seed"))
    if duplicate_rate is None:
        # F8: the denominator is every ATTEMPTED plan (ready plans that ran to
        # a result or failed, plus blocked plans) — not the count of planner
        # calls, which repair retries inflate and which would dilute the rate.
        attempted_plan_count = candidate_count + failed_plan_count + blocked_plan_count
        duplicate_rate = (
            min(1.0, duplicate_plan_count / attempted_plan_count) if attempted_plan_count > 0 else 0.0
        )
    duplicate_rate = min(1.0, max(0.0, duplicate_rate))

    gate_blocking_reasons: tuple[str, ...] = ()
    oos_net_return_decay = None
    if last_round_summary is not None and "winner_candidate_ref" in last_round_summary:
        # C1: rounds completed after the winner-evidence fix persist the round
        # WINNER's decay and gate reasons in the round summary; consume those
        # so the evidence matches best_score_delta_vs_seed. Null/empty values
        # are honest (a round with no candidates has no winner evidence).
        gate_blocking_reasons = tuple(
            str(reason) for reason in (last_round_summary.get("winner_gate_blocking_reasons") or ())
        )
        oos_net_return_decay = _finite_float_or_none(last_round_summary.get("winner_oos_net_return_decay"))
    elif last_result_entry is not None:
        # Fallback for traces written BEFORE the winner-evidence round summary
        # existed: best effort from the last-traced experiment_result (which
        # may be a losing candidate — exactly the bias the new rows remove).
        decision = last_result_entry.get("gate_decision")
        if isinstance(decision, dict):
            gate_blocking_reasons = tuple(
                str(reason) for reason in (decision.get("blocking_reasons") or ())
            )
        backtest_summary = last_result_entry.get("backtest_summary")
        if isinstance(backtest_summary, dict):
            oos_net_return_decay = _finite_float_or_none(backtest_summary.get("oos_net_return_decay"))
    turnover_breach = any(reason.startswith("turnover_rate") for reason in gate_blocking_reasons)

    return StrategyContext(
        round_index=len(run_ids),
        candidate_count=candidate_count,
        best_objective_score=best_objective_score,
        best_score_delta_vs_seed=best_score_delta_vs_seed,
        duplicate_rate=duplicate_rate,
        oos_net_return_decay=oos_net_return_decay,
        gate_blocking_reasons=gate_blocking_reasons,
        turnover_breach=turnover_breach,
        successful_mechanisms=tuple(dict.fromkeys(mechanisms))[:_STRATEGY_MECHANISM_LIMIT],
        recent_fingerprints=tuple(dict.fromkeys(fingerprints))[-_STRATEGY_FINGERPRINT_LIMIT:],
    )


def _strategy_trail_from_trace_entries(entries: list[dict[str, Any]]) -> tuple[dict[str, Any], ...]:
    trail: list[dict[str, Any]] = []
    for entry in entries:
        if str(entry.get("phase") or "") != "strategy_decision":
            continue
        decision = entry.get("strategy_decision")
        if not isinstance(decision, dict):
            continue
        strategy_context = entry.get("strategy_context")
        round_index = strategy_context.get("round_index") if isinstance(strategy_context, dict) else None
        trail.append(
            {
                "round_index": round_index if isinstance(round_index, int) and not isinstance(round_index, bool) else None,
                "strategy": str(decision.get("strategy") or ""),
                "reason": str(decision.get("reason") or ""),
            }
        )
    return tuple(trail[-(_STRATEGY_TRAIL_LIMIT - 1):])


def _strategy_prompt_hints(decision: StrategyDecision) -> tuple[str, ...]:
    """Additional structured hints for the existing prompt-context channel.

    These append to ``ResearchContext.next_focus_hints`` only; the prompt
    structure itself is not changed.
    """

    hints = [f"strategy_selector: strategy={decision.strategy}; reason={decision.reason}"]
    if decision.allowed_formula_transformations:
        hints.append(
            "strategy_selector: allowed_formula_transformations="
            + ", ".join(decision.allowed_formula_transformations)
        )
    if decision.forbidden_patterns:
        hints.append(
            "strategy_selector: forbidden_formula_fingerprints="
            + ", ".join(decision.forbidden_patterns[:_STRATEGY_TRAIL_LIMIT])
        )
    return tuple(hints)


def _round_summary_payload(
    *,
    seed_factor_id: str,
    round_index: int | None,
    planned_count: int,
    results: list[ResearchCandidateResult],
    seed_score: float | None,
    dedup_summary: dict[str, object],
    accepted_candidate_ids: tuple[str, ...],
    winner_candidate_ref: str | None = None,
    winner_oos_net_return_decay: float | None = None,
    winner_gate_blocking_reasons: tuple[str, ...] = (),
) -> dict[str, Any]:
    best_score = _finite_float_or_none(results[0].score) if results else None
    seed_score_value = _finite_float_or_none(seed_score)
    delta = (
        _finite_float_or_none(best_score - seed_score_value)
        if best_score is not None and seed_score_value is not None
        else None
    )
    duplicate_count = (
        int(dedup_summary.get("formula_skipped") or 0)
        + int(dedup_summary.get("diversity_skipped") or 0)
        + int(dedup_summary.get("result_duplicates") or 0)
    )
    duplicate_rate = min(1.0, duplicate_count / planned_count) if planned_count > 0 else 0.0
    return {
        "seed_factor_id": seed_factor_id,
        "round_index": round_index,
        "planned_count": planned_count,
        "candidate_count": len(results),
        "duplicate_count": duplicate_count,
        "duplicate_rate": duplicate_rate,
        "seed_score": seed_score_value,
        "best_score": best_score,
        "best_score_delta_vs_seed": delta,
        "accepted_candidate_ids": list(accepted_candidate_ids),
        # C1: winner-specific evidence, persisted at round completion so the
        # NEXT round's selector context reads the same candidate that produced
        # best_score / best_score_delta_vs_seed — never a losing candidate
        # that merely happened to be traced last. None/empty when the round
        # produced no candidates (missing evidence is never fabricated).
        "winner_candidate_ref": winner_candidate_ref,
        "winner_oos_net_return_decay": _finite_float_or_none(winner_oos_net_return_decay),
        "winner_gate_blocking_reasons": [str(reason) for reason in winner_gate_blocking_reasons],
    }


def _rd_warnings_count(
    seed_assessment: FactorAssessmentBundle | None,
    candidates: tuple[ResearchCandidateResult, ...],
) -> int:
    """Distinct warning messages plus distinct warning codes across the run."""

    sources: list[Any] = (
        [
            seed_assessment.evaluation,
            seed_assessment.selection_backtest,
            seed_assessment.external_oos_backtest,
        ]
        if seed_assessment is not None
        else []
    )
    for candidate in candidates:
        sources.extend((candidate.evaluation, candidate.backtest))
        if candidate.selection_backtest is not None:
            sources.append(candidate.selection_backtest)
        if candidate.external_oos_backtest is not None:
            sources.append(candidate.external_oos_backtest)
    messages: set[str] = set()
    codes: set[str] = set()
    for source in sources:
        messages.update(str(item) for item in getattr(source, "warnings", ()) or ())
        codes.update(str(item) for item in getattr(source, "warning_codes", ()) or ())
    return len(messages) + len(codes)

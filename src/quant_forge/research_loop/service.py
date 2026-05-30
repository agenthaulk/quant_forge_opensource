"""Small, decoupled factor research loop."""

from __future__ import annotations

from dataclasses import dataclass, replace
from math import ceil
from pathlib import Path
from typing import Protocol

from quant_forge.backtesting.service import run_factor_backtest
from quant_forge.core.contracts import (
    BacktestResult,
    EvaluationResult,
    FactorDefinition,
    SampleSplitSpec,
    SimulationProfile,
)
from quant_forge.evaluation.service import evaluate_factor
from quant_forge.factor_library.repository import FactorRepository, parse_idea_to_definition


DEFAULT_QUICK_HORIZON_DAYS = (5, 21)
DEFAULT_QUICK_SAMPLE_SPLITS = (SampleSplitSpec(name="IS", fraction=1.0, score_weight=1.0),)


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

    def __post_init__(self) -> None:
        if self.min_ic_days < 0:
            raise ValueError("min_ic_days must be non-negative")
        if not 0 <= self.min_coverage <= 1:
            raise ValueError("min_coverage must be in [0, 1]")
        if self.min_backtest_periods < 0:
            raise ValueError("min_backtest_periods must be non-negative")


@dataclass(frozen=True)
class ResearchHypothesis:
    text: str
    rationale: str


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

    def generate(
        self, seed: FactorDefinition, *, objective: str, max_candidates: int
    ) -> tuple[ResearchHypothesis, ...]:
        horizon = f"{seed.horizon_days}日"
        hypotheses = (
            ResearchHypothesis(
                text=f"非ST的小市值股票在未来{horizon}表现更好",
                rationale="Retest a small-cap thesis with the public non-ST universe filter.",
            ),
            ResearchHypothesis(
                text=f"非ST的动量股票在未来{horizon}表现更好",
                rationale="Compare the seed against a simple recent-momentum alternative.",
            ),
            ResearchHypothesis(
                text=f"非ST的低波动股票在未来{horizon}表现更好",
                rationale="Compare the seed against a simple defensive low-volatility alternative.",
            ),
        )
        return hypotheses[:max_candidates]


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
        if backtest.long_short_sharpe > 0:
            strengths.append("positive long-short Sharpe")
        else:
            risks.append("long-short Sharpe is not positive")
        if backtest.average_turnover > 0.8:
            risks.append("high average rebalance turnover")
            next_hypotheses.append(f"smooth or slow down {candidate.name} to reduce turnover")
        if backtest.max_drawdown < -0.2:
            risks.append("large drawdown in lightweight backtest")
        if _oos_decay(evaluation):
            risks.append("OOS2 ICIR decays versus IS")
            next_hypotheses.append(f"test a simpler or more robust variant of {candidate.name}")
        if not next_hypotheses:
            next_hypotheses.append(f"compare {candidate.name} against a lower-turnover variant")

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
    seed_factor_id: str
    objective: str
    objective_weights: ResearchObjectiveWeights
    gate: ResearchGate
    candidates: tuple[ResearchCandidateResult, ...]
    accepted_candidate_ids: tuple[str, ...]
    search_trace: tuple[ResearchSearchTraceEntry, ...] = ()
    report_path: Path | None = None


@dataclass(frozen=True)
class _ResearchTrial:
    hypothesis: ResearchHypothesis
    factor: FactorDefinition
    simulation_profile: SimulationProfile


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
        hypothesis_generator: HypothesisGenerator | None = None,
        review_generator: ResearchReviewGenerator | None = None,
    ) -> None:
        self.factor_root = factor_root
        self.data_root = data_root
        self.artifact_root = artifact_root
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
        seed = repo.get(seed_factor_id)
        objective_weights = weights or objective_weights_for(objective)
        candidate_gate = gate or ResearchGate()
        planned = hypotheses or self.hypothesis_generator.generate(
            seed, objective=objective, max_candidates=max_candidates
        )
        if not planned:
            raise ValueError("research loop requires at least one hypothesis")

        trials: list[_ResearchTrial] = []
        for hypothesis in planned[:max_candidates]:
            candidate = _load_or_save_candidate(repo, _candidate_from_hypothesis(hypothesis, seed.horizon_days))
            for profile in self.simulation_profiles:
                trials.append(_ResearchTrial(hypothesis, candidate, profile))

        search_trace, final_trials = self._select_final_trials(trials, objective_weights)
        results = [
            self._evaluate_final_trial(repo, seed, trial, objective_weights, candidate_gate) for trial in final_trials
        ]
        accepted = tuple(
            dict.fromkeys(
                result.factor.factor_id
                for result in results
                if result.gate_passed and result.factor.status in {"candidate", "active"}
            )
        )
        result = ResearchLoopResult(
            seed_factor_id=seed_factor_id,
            objective=objective,
            objective_weights=objective_weights,
            gate=candidate_gate,
            candidates=tuple(results),
            accepted_candidate_ids=accepted,
            search_trace=search_trace,
        )
        from quant_forge.research_loop.reporting import write_research_report

        return replace(result, report_path=write_research_report(result, self.artifact_root))

    def _select_final_trials(
        self, trials: list[_ResearchTrial], objective_weights: ResearchObjectiveWeights
    ) -> tuple[tuple[ResearchSearchTraceEntry, ...], list[_ResearchTrial]]:
        if (
            not self.parameter_search_enabled
            or self.parameter_search_method == "full_grid"
            or len(trials) <= self.parameter_search_min_survivors
        ):
            return (), trials
        if self.parameter_search_method != "successive_halving":
            raise ValueError("parameter_search_method must be full_grid or successive_halving")

        scored_trials = [
            self._score_trial(
                trial,
                objective_weights,
                horizon_days_matrix=self.quick_horizon_days_matrix,
                sample_splits=self.quick_sample_splits,
            )
            for trial in trials
        ]
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
        return trace, [item.trial for item in survivors]

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
        )
        backtest = run_factor_backtest(
            trial.factor.factor_id,
            factor_root=self.factor_root,
            data_root=self.data_root,
            artifact_root=self.artifact_root,
            holding_days=trial.factor.horizon_days,
            simulation_profile=trial.simulation_profile,
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
    ) -> ResearchCandidateResult:
        scored = self._score_trial(
            trial,
            objective_weights,
            horizon_days_matrix=self.horizon_days_matrix,
            sample_splits=self.sample_splits,
        )
        gate_passed, gate_reasons = apply_gate(scored.evaluation, scored.backtest, scored.score, candidate_gate)
        candidate = repo.get(trial.factor.factor_id)
        if gate_passed:
            if candidate.status == "draft":
                candidate = repo.promote(
                    candidate.factor_id,
                    "candidate",
                    "Research loop smoke gate passed; active promotion still requires user decision.",
                )
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
        + backtest.annualized_return * weights.annualized_return
        + backtest.max_drawdown * weights.max_drawdown
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
    parsed = parse_idea_to_definition(hypothesis.text)
    return replace(parsed, horizon_days=horizon_days, source="research_loop")


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

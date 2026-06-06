"""Minimal multi-round research campaign support for local web workflows."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import re

import pandas as pd

from quant_forge.backtesting.service import run_factor_backtest
from quant_forge.core.contracts import BacktestResult, EvaluationResult, FactorDefinition, SampleSplitSpec, SimulationProfile, TransactionCostModel
from quant_forge.data.local import LocalPanelDataProvider
from quant_forge.evaluation.service import evaluate_factor
from quant_forge.factor_engine.signal_processing import apply_test_period, prepare_factor_scores_result
from quant_forge.factor_engine.value_store import FactorValueStore, _formula_signature
from quant_forge.factor_library.catalog import FactorCatalog, is_precomputed_formula, precomputed_formula
from quant_forge.factor_library.repository import FactorRepository
from quant_forge.research_loop.service import (
    ResearchGate,
    ResearchObjectiveWeights,
    apply_gate,
    objective_weights_for,
    score_candidate,
    weighted_split_icir,
)

DEFAULT_CAMPAIGN_ROUNDS = 5
DEFAULT_CAMPAIGN_FRONTIER_SIZE = 3
MAX_CAMPAIGN_SEEDS = 20
PRECOMPUTED_CAMPAIGN_MIN_SEEDS = 2


@dataclass(frozen=True)
class _PrecomputedCampaignStrategy:
    round_index: int
    seed_limit: int
    weighting: str
    smoothing_span: int | None
    suffix: str


PRECOMPUTED_CAMPAIGN_STRATEGIES = (
    _PrecomputedCampaignStrategy(1, 20, "equal", None, "top20_equal"),
    _PrecomputedCampaignStrategy(2, 10, "equal", None, "top10_equal"),
    _PrecomputedCampaignStrategy(3, 5, "equal", None, "top5_equal"),
    _PrecomputedCampaignStrategy(4, 10, "position_weighted", None, "top10_weighted"),
    _PrecomputedCampaignStrategy(5, 10, "position_weighted", 3, "top10_weighted_ewm"),
)


@dataclass(frozen=True)
class ResearchCampaignCandidate:
    round_index: int
    seed_factor_id: str
    factor: FactorDefinition
    evaluation: EvaluationResult
    backtest: BacktestResult
    split_weighted_icir: float
    score: float
    gate_passed: bool
    gate_reasons: tuple[str, ...]


@dataclass(frozen=True)
class ResearchCampaignRoundResult:
    round_index: int
    input_seed_factor_ids: tuple[str, ...]
    candidates: tuple[ResearchCampaignCandidate, ...]
    selected_factor_ids: tuple[str, ...]
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResearchCampaignResult:
    seed_factor_ids: tuple[str, ...]
    objective: str
    rounds_requested: int
    rounds_completed: int
    round_results: tuple[ResearchCampaignRoundResult, ...]
    final_factor_id: str | None
    final_factor: FactorDefinition | None
    final_evaluation: EvaluationResult | None
    final_backtest: BacktestResult | None
    final_score: float | None
    artifacts: tuple[Path, ...]
    errors: tuple[str, ...] = ()


class ResearchCampaignService:
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
        horizon_days_matrix: tuple[int, ...] | None = None,
        sample_splits: tuple[SampleSplitSpec, ...] | None = None,
        transaction_costs: TransactionCostModel | None = None,
        frontier_size: int = DEFAULT_CAMPAIGN_FRONTIER_SIZE,
    ) -> None:
        self.factor_root = factor_root
        self.data_root = data_root
        self.artifact_root = artifact_root
        self.factor_values_root = factor_values_root
        self.factor_values_overlay_root = factor_values_overlay_root
        self.factor_values_manifest_root = factor_values_manifest_root
        self.simulation_profile = simulation_profile or SimulationProfile()
        self.horizon_days_matrix = horizon_days_matrix
        self.sample_splits = sample_splits
        self.transaction_costs = transaction_costs or TransactionCostModel()
        self.frontier_size = max(1, frontier_size)

    def run(
        self,
        seed_factor_ids: tuple[str, ...] | list[str],
        *,
        objective: str = "balanced",
        rounds: int = DEFAULT_CAMPAIGN_ROUNDS,
        weights: ResearchObjectiveWeights | None = None,
        gate: ResearchGate | None = None,
    ) -> ResearchCampaignResult:
        normalized_seed_ids = _normalize_seed_factor_ids(seed_factor_ids)
        if not normalized_seed_ids:
            raise ValueError("seed_factor_ids are required")
        if rounds < 1:
            raise ValueError("rounds must be positive")

        catalog = FactorCatalog(
            self.factor_root,
            factor_values_root=self.factor_values_root,
            factor_values_manifest_root=self.factor_values_manifest_root,
        )
        repo = FactorRepository(self.factor_root)
        objective_weights = weights or objective_weights_for(objective)
        candidate_gate = gate or ResearchGate()

        frontier = normalized_seed_ids[:MAX_CAMPAIGN_SEEDS]
        frontier_factors = tuple(catalog.get(seed_factor_id) for seed_factor_id in frontier)
        seen_formulas = {factor.formula for factor in frontier_factors}
        precomputed_seed_ids = _precomputed_campaign_seed_ids(frontier_factors)
        round_results: list[ResearchCampaignRoundResult] = []
        all_candidates: list[ResearchCampaignCandidate] = []
        errors: list[str] = []

        for round_index in range(1, rounds + 1):
            candidates: list[ResearchCampaignCandidate] = []
            round_errors: list[str] = []
            next_frontier: list[str] = []
            round_input_seed_ids = frontier

            if precomputed_seed_ids:
                round_input_seed_ids = precomputed_seed_ids
                try:
                    candidate = self._evaluate_precomputed_candidate(
                        repo,
                        catalog,
                        seed_factor_ids=precomputed_seed_ids,
                        round_index=round_index,
                        weights=objective_weights,
                        gate=candidate_gate,
                    )
                    candidates.append(candidate)
                    next_frontier.append(candidate.factor.factor_id)
                except Exception as exc:
                    round_errors.append(f"round {round_index} precomputed frontier: {exc}")
            else:
                for seed_factor_id in frontier:
                    try:
                        seed = catalog.get(seed_factor_id)
                        drafts = self._candidate_variants(seed, seen_formulas)
                        if not drafts:
                            round_errors.append(f"round {round_index} seed {seed_factor_id}: no unseen variants")
                            continue
                        candidate = self._evaluate_candidate(
                            repo,
                            seed_factor_id=seed_factor_id,
                            round_index=round_index,
                            draft=drafts[0],
                            weights=objective_weights,
                            gate=candidate_gate,
                        )
                        candidates.append(candidate)
                    except Exception as exc:
                        round_errors.append(f"round {round_index} seed {seed_factor_id}: {exc}")

            ranked = sorted(candidates, key=_campaign_sort_key, reverse=True)
            if not precomputed_seed_ids:
                selected = ranked[: self.frontier_size]
                next_frontier.extend(item.factor.factor_id for item in selected)
            all_candidates.extend(ranked)
            errors.extend(round_errors)
            round_results.append(
                ResearchCampaignRoundResult(
                    round_index=round_index,
                    input_seed_factor_ids=round_input_seed_ids,
                    candidates=tuple(ranked),
                    selected_factor_ids=tuple(next_frontier),
                    errors=tuple(round_errors),
                )
            )
            if precomputed_seed_ids:
                continue
            if not next_frontier:
                break
            frontier = tuple(dict.fromkeys(next_frontier))

        final_candidate = _best_campaign_candidate(all_candidates)
        artifacts = _campaign_artifacts(round_results, final_candidate)
        return ResearchCampaignResult(
            seed_factor_ids=normalized_seed_ids,
            objective=objective,
            rounds_requested=rounds,
            rounds_completed=len(round_results),
            round_results=tuple(round_results),
            final_factor_id=final_candidate.factor.factor_id if final_candidate is not None else None,
            final_factor=final_candidate.factor if final_candidate is not None else None,
            final_evaluation=final_candidate.evaluation if final_candidate is not None else None,
            final_backtest=final_candidate.backtest if final_candidate is not None else None,
            final_score=final_candidate.score if final_candidate is not None else None,
            artifacts=artifacts,
            errors=tuple(errors),
        )

    def _candidate_variants(self, seed: FactorDefinition, seen_formulas: set[str]) -> tuple[FactorDefinition, ...]:
        variants: list[FactorDefinition] = []
        for formula, suffix in _variant_formulas(seed.formula):
            if formula == seed.formula or formula in seen_formulas:
                continue
            seen_formulas.add(formula)
            variants.append(_campaign_factor_definition(seed, formula=formula, suffix=suffix))
        return tuple(variants)

    def _evaluate_candidate(
        self,
        repo: FactorRepository,
        *,
        seed_factor_id: str,
        round_index: int,
        draft: FactorDefinition,
        weights: ResearchObjectiveWeights,
        gate: ResearchGate,
    ) -> ResearchCampaignCandidate:
        candidate = _load_or_save_candidate(repo, draft)
        return self._evaluate_registered_candidate(
            repo,
            seed_factor_id=seed_factor_id,
            round_index=round_index,
            candidate=candidate,
            weights=weights,
            gate=gate,
        )

    def _evaluate_precomputed_candidate(
        self,
        repo: FactorRepository,
        catalog: FactorCatalog,
        *,
        seed_factor_ids: tuple[str, ...],
        round_index: int,
        weights: ResearchObjectiveWeights,
        gate: ResearchGate,
    ) -> ResearchCampaignCandidate:
        write_root = self.factor_values_overlay_root or self.factor_values_root
        if write_root is None:
            raise ValueError("precomputed campaign seeds require factor_values_root or factor_values_overlay_root")
        strategy = _precomputed_campaign_strategy(round_index)
        selected_seed_ids = seed_factor_ids[: min(len(seed_factor_ids), strategy.seed_limit)]
        selected_seeds = tuple(catalog.get(seed_factor_id) for seed_factor_id in selected_seed_ids)
        if len(selected_seeds) < PRECOMPUTED_CAMPAIGN_MIN_SEEDS:
            raise ValueError("precomputed campaign requires at least two readable precomputed seeds")
        candidate = _load_or_save_candidate(repo, _precomputed_campaign_factor_definition(selected_seeds, strategy))
        combined_scores = self._combine_precomputed_scores(selected_seeds, strategy)
        factor_dir = write_root.expanduser() / f"factor_id={candidate.factor_id}"
        formula_signature = _formula_signature(candidate.factor_id, candidate.formula, candidate.universe_filters)
        FactorValueStore(write_root, write_root=write_root).write_incremental_values(
            factor_dir,
            factor_id=candidate.factor_id,
            factor_name=candidate.name,
            formula_signature=formula_signature,
            scores=combined_scores,
        )
        return self._evaluate_registered_candidate(
            repo,
            seed_factor_id=_campaign_seed_label(selected_seed_ids),
            round_index=round_index,
            candidate=candidate,
            weights=weights,
            gate=gate,
        )

    def _combine_precomputed_scores(
        self,
        seeds: tuple[FactorDefinition, ...],
        strategy: _PrecomputedCampaignStrategy,
    ) -> pd.DataFrame:
        raw_profile = _raw_precomputed_profile(self.simulation_profile)
        panel = LocalPanelDataProvider(self.data_root).load_panel()
        panel_keys = (
            apply_test_period(panel, raw_profile)[["trade_date", "instrument"]]
            .drop_duplicates()
            .reset_index(drop=True)
        )
        merged = panel_keys.copy()
        weights: list[float] = []

        for position, seed in enumerate(seeds, start=1):
            result = prepare_factor_scores_result(
                panel,
                seed.formula,
                seed.universe_filters,
                profile=raw_profile,
                factor_id=seed.factor_id,
                factor_name=seed.name,
                factor_values_root=self.factor_values_root,
                factor_values_overlay_root=self.factor_values_overlay_root,
            )
            column = f"seed_{position}"
            merged = merged.merge(
                result.scores.rename(columns={"score": column}),
                on=["trade_date", "instrument"],
                how="left",
            )
            weights.append(_precomputed_weight(strategy, position, len(seeds)))

        score_columns = [column for column in merged.columns if column.startswith("seed_")]
        if not score_columns:
            raise ValueError("no precomputed seed scores were available for campaign combination")
        weight_series = pd.Series(weights, index=score_columns, dtype="float64")
        available = merged[score_columns].notna().astype("float64")
        weighted_sum = merged[score_columns].mul(weight_series, axis=1).sum(axis=1, min_count=1)
        weight_total = available.mul(weight_series, axis=1).sum(axis=1)
        scores = pd.DataFrame(
            {
                "trade_date": pd.to_datetime(merged["trade_date"]),
                "instrument": merged["instrument"].astype(str),
                "score": weighted_sum.where(weight_total > 0) / weight_total.where(weight_total > 0),
            }
        )
        if strategy.smoothing_span is not None:
            scores = _smooth_scores(scores, strategy.smoothing_span)
        if scores["score"].notna().sum() == 0:
            raise ValueError("combined precomputed campaign factor contains no usable scores")
        return scores.dropna(subset=["trade_date", "instrument"]).reset_index(drop=True)

    def _evaluate_registered_candidate(
        self,
        repo: FactorRepository,
        *,
        seed_factor_id: str,
        round_index: int,
        candidate: FactorDefinition,
        weights: ResearchObjectiveWeights,
        gate: ResearchGate,
    ) -> ResearchCampaignCandidate:
        evaluation = evaluate_factor(
            candidate.factor_id,
            factor_root=self.factor_root,
            data_root=self.data_root,
            artifact_root=self.artifact_root,
            horizon_days=candidate.horizon_days,
            horizon_days_matrix=self.horizon_days_matrix,
            sample_splits=self.sample_splits,
            simulation_profile=self.simulation_profile,
            factor_values_root=self.factor_values_root,
            factor_values_overlay_root=self.factor_values_overlay_root,
            factor_values_manifest_root=self.factor_values_manifest_root,
        )
        backtest = run_factor_backtest(
            candidate.factor_id,
            factor_root=self.factor_root,
            data_root=self.data_root,
            artifact_root=self.artifact_root,
            holding_days=candidate.horizon_days,
            simulation_profile=self.simulation_profile,
            transaction_costs=self.transaction_costs,
            sample_splits=self.sample_splits,
            factor_values_root=self.factor_values_root,
            factor_values_overlay_root=self.factor_values_overlay_root,
            factor_values_manifest_root=self.factor_values_manifest_root,
        )
        split_score = weighted_split_icir(evaluation)
        score = score_candidate(evaluation, backtest, weights, split_score)
        gate_passed, gate_reasons = apply_gate(evaluation, backtest, score, gate)
        if gate_passed and candidate.status == "draft":
            candidate = repo.promote(
                candidate.factor_id,
                "candidate",
                f"Research campaign round {round_index} passed the smoke gate from seed {seed_factor_id}.",
            )
        elif not gate_passed and candidate.status != "draft":
            gate_reasons = (*gate_reasons, f"existing {candidate.status} status preserved")
        return ResearchCampaignCandidate(
            round_index=round_index,
            seed_factor_id=seed_factor_id,
            factor=candidate,
            evaluation=evaluation,
            backtest=backtest,
            split_weighted_icir=split_score,
            score=score,
            gate_passed=gate_passed,
            gate_reasons=gate_reasons,
        )


def _normalize_seed_factor_ids(seed_factor_ids: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    for item in seed_factor_ids:
        value = str(item).strip()
        if value and value not in normalized:
            normalized.append(value)
    return tuple(normalized)


def _precomputed_campaign_seed_ids(frontier_factors: tuple[FactorDefinition, ...]) -> tuple[str, ...]:
    precomputed = tuple(factor.factor_id for factor in frontier_factors if is_precomputed_formula(factor.formula))
    if len(precomputed) < PRECOMPUTED_CAMPAIGN_MIN_SEEDS:
        return ()
    if len(precomputed) * 2 < len(frontier_factors):
        return ()
    return precomputed[:MAX_CAMPAIGN_SEEDS]


def _precomputed_campaign_strategy(round_index: int) -> _PrecomputedCampaignStrategy:
    if round_index <= len(PRECOMPUTED_CAMPAIGN_STRATEGIES):
        return PRECOMPUTED_CAMPAIGN_STRATEGIES[round_index - 1]
    return PRECOMPUTED_CAMPAIGN_STRATEGIES[-1]


def _precomputed_campaign_factor_definition(
    seeds: tuple[FactorDefinition, ...],
    strategy: _PrecomputedCampaignStrategy,
) -> FactorDefinition:
    seed_ids = tuple(seed.factor_id for seed in seeds)
    common_filters = _shared_universe_filters(seeds)
    formula = precomputed_formula(f"factor_id={_campaign_factor_id(seed_ids, strategy, common_filters)}")
    factor_id = formula.split("=", 1)[1]
    return FactorDefinition(
        factor_id=factor_id,
        name=f"campaign_{strategy.suffix}",
        formula=formula,
        status="draft",
        description=(
            f"Campaign precomputed combination from {len(seed_ids)} seed factors using {strategy.suffix}."
        ),
        horizon_days=seeds[0].horizon_days,
        universe_filters=common_filters,
        source="research_campaign",
    )


def _campaign_factor_id(
    seed_ids: tuple[str, ...],
    strategy: _PrecomputedCampaignStrategy,
    universe_filters: tuple[str, ...],
) -> str:
    digest = hashlib.sha1(
        f"{strategy.suffix}:{seed_ids}:{universe_filters}".encode("utf-8")
    ).hexdigest()[:10].upper()
    return f"FTR_CAMP_PRE_{strategy.round_index}_{digest}"


def _shared_universe_filters(seeds: tuple[FactorDefinition, ...]) -> tuple[str, ...]:
    first = seeds[0].universe_filters
    if all(seed.universe_filters == first for seed in seeds):
        return first
    return ()


def _raw_precomputed_profile(profile: SimulationProfile) -> SimulationProfile:
    return SimulationProfile(
        market=profile.market,
        instrument_type=profile.instrument_type,
        universe=profile.universe,
        execution_delay_days=profile.execution_delay_days,
        top_quantile=profile.top_quantile,
        nan_policy=profile.nan_policy,
        neutralization=profile.neutralization,
        truncation=profile.truncation,
        decay_days=0,
        test_period_start=profile.test_period_start,
        test_period_end=profile.test_period_end,
    )


def _precomputed_weight(strategy: _PrecomputedCampaignStrategy, position: int, size: int) -> float:
    if strategy.weighting == "equal":
        return 1.0
    if strategy.weighting == "position_weighted":
        return float(size - position + 1)
    raise ValueError(f"unsupported precomputed campaign weighting: {strategy.weighting}")


def _smooth_scores(scores: pd.DataFrame, span: int) -> pd.DataFrame:
    result = scores.sort_values(["instrument", "trade_date"]).copy()
    raw_missing = result["score"].isna()
    result["score"] = result.groupby("instrument", group_keys=False)["score"].transform(
        lambda values: values.astype("float64").ewm(span=span, adjust=False).mean()
    )
    result.loc[raw_missing, "score"] = pd.NA
    return result.sort_values(["trade_date", "instrument"]).reset_index(drop=True)


def _campaign_seed_label(seed_factor_ids: tuple[str, ...]) -> str:
    if len(seed_factor_ids) <= 3:
        return "+".join(seed_factor_ids)
    return f"{'+'.join(seed_factor_ids[:3])}+{len(seed_factor_ids) - 3}"


def _campaign_sort_key(candidate: ResearchCampaignCandidate) -> tuple[int, float, float, float]:
    return (
        1 if candidate.gate_passed else 0,
        candidate.score,
        candidate.split_weighted_icir,
        candidate.evaluation.rank_ic_mean,
    )


def _best_campaign_candidate(
    candidates: list[ResearchCampaignCandidate],
) -> ResearchCampaignCandidate | None:
    if not candidates:
        return None
    return sorted(candidates, key=_campaign_sort_key, reverse=True)[0]


def _campaign_artifacts(
    round_results: list[ResearchCampaignRoundResult],
    final_candidate: ResearchCampaignCandidate | None,
) -> tuple[Path, ...]:
    artifacts: list[Path] = []
    for round_result in round_results:
        for candidate in round_result.candidates:
            artifacts.extend([candidate.evaluation.artifact_path, candidate.backtest.artifact_path])
    if final_candidate is not None:
        artifacts.extend([final_candidate.evaluation.artifact_path, final_candidate.backtest.artifact_path])
    return tuple(dict.fromkeys(artifacts))


def _variant_formulas(formula: str) -> tuple[tuple[str, str], ...]:
    sign, operator, field = _parse_formula(formula)
    variants: list[tuple[str, str]] = []
    for candidate_operator, candidate_sign, suffix in (
        ("rank", sign, "same_rank"),
        ("zscore", sign, "same_zscore"),
        ("rank", -sign, "flip_rank"),
        ("zscore", -sign, "flip_zscore"),
        ("field", sign, "same_field"),
        ("field", -sign, "flip_field"),
    ):
        candidate_formula = _compose_formula(candidate_sign, candidate_operator, field)
        if candidate_formula != formula and (candidate_operator != operator or candidate_sign != sign):
            variants.append((candidate_formula, suffix))
    unique: list[tuple[str, str]] = []
    seen: set[str] = set()
    for candidate_formula, suffix in variants:
        if candidate_formula not in seen:
            unique.append((candidate_formula, suffix))
            seen.add(candidate_formula)
    return tuple(unique)


def _parse_formula(formula: str) -> tuple[int, str, str]:
    normalized = formula.strip()
    sign = -1 if normalized.startswith("-") else 1
    if sign == -1:
        normalized = normalized[1:].strip()
    call = re.fullmatch(r"([a-zA-Z_][a-zA-Z0-9_]*)\(([^()]+)\)", normalized)
    if call:
        return sign, call.group(1), call.group(2).strip()
    return sign, "field", normalized


def _compose_formula(sign: int, operator: str, field: str) -> str:
    base = field if operator == "field" else f"{operator}({field})"
    return f"-{base}" if sign < 0 else base


def _campaign_factor_definition(seed: FactorDefinition, *, formula: str, suffix: str) -> FactorDefinition:
    digest = hashlib.sha1(
        f"{formula}:{seed.horizon_days}:{seed.universe_filters}".encode("utf-8")
    ).hexdigest()[:10].upper()
    name = _slug(f"{seed.name}_{suffix}")
    return FactorDefinition(
        factor_id=f"FTR_CAMP_{digest}",
        name=name,
        formula=formula,
        status="draft",
        description=f"Campaign variant of {seed.factor_id} using {formula}.",
        horizon_days=seed.horizon_days,
        universe_filters=seed.universe_filters,
        source="research_campaign",
    )


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
        raise ValueError(f"research campaign factor id collision with different definition: {draft.factor_id}")
    return existing


def _slug(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_]+", "_", value.strip().lower())
    return normalized.strip("_") or "campaign_factor"

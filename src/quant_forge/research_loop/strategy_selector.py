"""Deterministic research strategy selection for the bounded RD loop.

Adopted from the memo `docs/research_platform_optimization_from_vibe_quantgpt.md`
section 4 ("RD Strategy Selector"): a QuantGPT-inspired, rule-based selector
that consumes prior candidate evidence and picks one research strategy for the
next round. It is a pure function over typed inputs — no LLM calls, no I/O,
no randomness — so the same context always yields the same decision.

First-principles alignment (docs/reviews/quantitative_core_audit.md):

- FP-2 (absence of evidence is not evidence of compliance): an unknown
  ``best_score_delta_vs_seed`` (None) never counts as "improving" for exploit
  and never counts as "stalled" for recombine; unknown OOS decay (None) never
  counts as a decay breach.
- FP-4 (unobserved values are never fabricated): unknown numeric inputs are
  represented as None, and NaN inputs are rejected at construction time.
- FP-6 (a sample touched by selection is no longer out-of-sample): the
  selector consumes only gate outputs and score deltas already produced by
  the bounded RD loop; it introduces no new peeks at evaluation data.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Literal

from quant_forge.research_loop.candidate_gate import INSUFFICIENT_OOS_EVIDENCE

STRATEGY_SCHEMA_VERSION = "qf.rd.strategy.v1"

ResearchStrategy = Literal["exploit", "explore", "recombine", "simplify", "parameter_search"]

STRATEGY_VOCABULARY: tuple[str, ...] = (
    "exploit",
    "explore",
    "recombine",
    "simplify",
    "parameter_search",
)

HIGH_DUPLICATE_RATE = 0.5
HIGH_OOS_NET_RETURN_DECAY = 0.5

PARAMETER_SEARCH_TRANSFORMATIONS: tuple[str, ...] = (
    "tune_decay_parameter",
    "tune_holding_period",
    "tune_window_length",
)

_ALLOWED_TRANSFORMATIONS: dict[str, tuple[str, ...]] = {
    "explore": (
        "regenerate_formula",
        "new_input_field",
        "operator_replacement",
        "nonlinear_transform",
    ),
    "exploit": (
        "window_change",
        "operator_replacement",
        "normalization",
        "nonlinear_transform",
    ),
    "recombine": (
        "combine_successful_mechanisms",
        "interaction_terms",
    ),
    "simplify": (
        "remove_operator",
        "reduce_nesting",
        "drop_interaction_terms",
        "shorten_formula",
    ),
    "parameter_search": PARAMETER_SEARCH_TRANSFORMATIONS,
}

_EXPECTED_FAILURE_MODES: dict[str, str] = {
    "explore": "low hit rate: most new mechanisms fail candidate gates",
    "exploit": "overfitting the incumbent mechanism with diminishing marginal gains",
    "recombine": "combined mechanisms turn out redundant or interact destructively",
    "simplify": "simplified formula loses the in-sample edge entirely",
    "parameter_search": "parameters overfit to the tuning window without new signal",
}


@dataclass(frozen=True)
class StrategyContext:
    """Evidence snapshot the selector consumes before the next RD round.

    Unknown numeric evidence must be passed as None (null-not-zero); NaN is
    rejected because a fabricated number is indistinguishable from evidence.
    ``operator_risk_flags`` and ``best_objective_score`` are carried for trace
    observability and later wiring phases; they do not alter rule outcomes yet.
    """

    round_index: int
    candidate_count: int
    best_objective_score: float | None = None
    best_score_delta_vs_seed: float | None = None
    duplicate_rate: float = 0.0
    oos_net_return_decay: float | None = None
    gate_blocking_reasons: tuple[str, ...] = ()
    turnover_breach: bool = False
    operator_risk_flags: tuple[str, ...] = ()
    successful_mechanisms: tuple[str, ...] = ()
    recent_fingerprints: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.round_index < 0:
            raise ValueError("round_index must be >= 0")
        if self.candidate_count < 0:
            raise ValueError("candidate_count must be >= 0")
        _require_finite_or_none(self, "best_objective_score")
        _require_finite_or_none(self, "best_score_delta_vs_seed")
        _require_finite_or_none(self, "oos_net_return_decay")
        if math.isnan(self.duplicate_rate) or not 0.0 <= self.duplicate_rate <= 1.0:
            raise ValueError("duplicate_rate must be within [0, 1]")
        _set_tuple(self, "gate_blocking_reasons")
        _set_tuple(self, "operator_risk_flags")
        _set_tuple(self, "successful_mechanisms")
        _set_tuple(self, "recent_fingerprints")

    def to_dict(self) -> dict[str, Any]:
        return {
            "round_index": self.round_index,
            "candidate_count": self.candidate_count,
            "best_objective_score": self.best_objective_score,
            "best_score_delta_vs_seed": self.best_score_delta_vs_seed,
            "duplicate_rate": self.duplicate_rate,
            "oos_net_return_decay": self.oos_net_return_decay,
            "gate_blocking_reasons": list(self.gate_blocking_reasons),
            "turnover_breach": self.turnover_breach,
            "operator_risk_flags": list(self.operator_risk_flags),
            "successful_mechanisms": list(self.successful_mechanisms),
            "recent_fingerprints": list(self.recent_fingerprints),
        }


@dataclass(frozen=True)
class StrategyDecision:
    """One deterministic strategy pick with its evidence trail.

    ``forbidden_patterns`` always carries the recently tried formula
    fingerprints so downstream generation cannot re-propose duplicates.
    """

    strategy: ResearchStrategy
    reason: str
    expected_failure_mode: str
    allowed_formula_transformations: tuple[str, ...] = ()
    forbidden_patterns: tuple[str, ...] = ()
    schema_version: str = STRATEGY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.strategy not in STRATEGY_VOCABULARY:
            raise ValueError(
                f"strategy must be one of {STRATEGY_VOCABULARY}, got {self.strategy!r}"
            )
        if not self.reason.strip():
            raise ValueError("reason is required")
        if not self.expected_failure_mode.strip():
            raise ValueError("expected_failure_mode is required")
        if self.schema_version != STRATEGY_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {STRATEGY_SCHEMA_VERSION}")
        _set_tuple(self, "allowed_formula_transformations")
        _set_tuple(self, "forbidden_patterns")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "strategy": self.strategy,
            "reason": self.reason,
            "allowed_formula_transformations": list(self.allowed_formula_transformations),
            "forbidden_patterns": list(self.forbidden_patterns),
            "expected_failure_mode": self.expected_failure_mode,
        }


def select_strategy(context: StrategyContext) -> StrategyDecision:
    """Pick the next-round research strategy from prior-round evidence.

    Deterministic rule table, evaluated strictly in precedence order; the
    first matching rule wins and later rules are not consulted:

    R1 — explore. ``candidate_count == 0`` (nothing evaluated yet, so there
        is no incumbent to refine) OR ``duplicate_rate > 0.5`` (generation
        is collapsing onto already-tried fingerprints; more of the same
        neighborhood cannot add evidence). Highest precedence because every
        other strategy presupposes usable, non-duplicate candidates.
    R2 — simplify. ``oos_net_return_decay > 0.5`` (known, not None), OR any
        gate blocking reason of the INSUFFICIENT_OOS_EVIDENCE class, OR any
        drawdown-breach gate reason. Overfit/fragility evidence outranks
        tuning and refinement: adding complexity to a decaying formula
        compounds the failure. None decay is unknown, never a breach (FP-2).
    R3 — parameter_search. ``turnover_breach`` is True. The mechanism may be
        sound but the trading profile is too costly, so only decay/holding/
        window parameters may be tuned (see allowed transformations); formula
        structure must not change under this strategy.
    R4 — exploit. ``best_score_delta_vs_seed > 0`` (known, not None) AND
        ``round_index >= 1``: the loop has a verified improving incumbent, so
        refine it. An unknown delta never counts as improving (FP-2).
    R5 — recombine. ``round_index >= 2`` AND at least two DISTINCT
        ``successful_mechanisms`` AND improvement stalled
        (``best_score_delta_vs_seed`` known and <= 0). Recombination is the
        alternative to exploit once multiple mechanisms have independently
        worked but single-mechanism refinement has stopped paying. An unknown
        delta is not evidence of a stall, so it cannot trigger recombine.
    R6 — explore. Fallback when no rule above fires (including when the
        score delta is unknown): with no breach, no verified improvement,
        and no recombination evidence, widening the search is the only move
        that cannot fabricate a justification.

    Every decision carries ``forbidden_patterns`` = the deduplicated
    ``recent_fingerprints`` from the context, so no strategy may re-propose a
    recently tried formula.
    """
    if context.candidate_count == 0:
        return _decision(
            "explore",
            "R1: no candidates evaluated yet; there is no incumbent to refine",
            context,
        )
    if context.duplicate_rate > HIGH_DUPLICATE_RATE:
        return _decision(
            "explore",
            (
                f"R1: duplicate_rate {context.duplicate_rate:.6g} exceeds "
                f"{HIGH_DUPLICATE_RATE:.6g}; generation is collapsing onto "
                "recently tried fingerprints"
            ),
            context,
        )

    decay = context.oos_net_return_decay
    if decay is not None and decay > HIGH_OOS_NET_RETURN_DECAY:
        return _decision(
            "simplify",
            (
                f"R2: oos_net_return_decay {decay:.6g} exceeds "
                f"{HIGH_OOS_NET_RETURN_DECAY:.6g}; the formula is overfit to IS"
            ),
            context,
        )
    fragility_reason = _first_fragility_gate_reason(context.gate_blocking_reasons)
    if fragility_reason:
        return _decision(
            "simplify",
            f"R2: gate blocking reason indicates fragility: {fragility_reason}",
            context,
        )

    if context.turnover_breach:
        return _decision(
            "parameter_search",
            (
                "R3: turnover breach; only decay/holding/window parameters may "
                "be tuned, formula structure is frozen"
            ),
            context,
        )

    delta = context.best_score_delta_vs_seed
    if delta is not None and delta > 0.0 and context.round_index >= 1:
        return _decision(
            "exploit",
            (
                f"R4: best candidate improves on seed by {delta:.6g} at "
                f"round {context.round_index}; refine the incumbent"
            ),
            context,
        )

    distinct_mechanisms = tuple(dict.fromkeys(context.successful_mechanisms))
    if (
        context.round_index >= 2
        and len(distinct_mechanisms) >= 2
        and delta is not None
        and delta <= 0.0
    ):
        return _decision(
            "recombine",
            (
                f"R5: improvement stalled (delta {delta:.6g} <= 0) with "
                f"{len(distinct_mechanisms)} distinct successful mechanisms "
                f"at round {context.round_index}; recombine them"
            ),
            context,
        )

    return _decision(
        "explore",
        (
            "R6: no breach, no verified improvement, and no recombination "
            "evidence; widen the search"
        ),
        context,
    )


def _decision(strategy: ResearchStrategy, reason: str, context: StrategyContext) -> StrategyDecision:
    return StrategyDecision(
        strategy=strategy,
        reason=reason,
        expected_failure_mode=_EXPECTED_FAILURE_MODES[strategy],
        allowed_formula_transformations=_ALLOWED_TRANSFORMATIONS[strategy],
        forbidden_patterns=tuple(dict.fromkeys(context.recent_fingerprints)),
    )


def _first_fragility_gate_reason(reasons: tuple[str, ...]) -> str:
    for reason in reasons:
        if INSUFFICIENT_OOS_EVIDENCE in reason.upper():
            return reason
        if "drawdown" in reason.lower():
            return reason
    return ""


def _require_finite_or_none(instance: object, field_name: str) -> None:
    value = getattr(instance, field_name)
    if value is None:
        return
    if not math.isfinite(value):
        raise ValueError(f"{field_name} must be a finite number or None")


def _set_tuple(instance: object, field_name: str) -> None:
    value = getattr(instance, field_name)
    if value is None:
        normalized: tuple[str, ...] = ()
    elif isinstance(value, tuple):
        normalized = value
    elif isinstance(value, list):
        normalized = tuple(value)
    else:
        normalized = (value,)
    object.__setattr__(instance, field_name, tuple(str(item) for item in normalized))

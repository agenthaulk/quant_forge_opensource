"""Rule-by-rule tests for the deterministic RD ResearchStrategySelector."""

from __future__ import annotations

import pytest

from quant_forge.research_loop.strategy_selector import (
    HIGH_DUPLICATE_RATE,
    HIGH_OOS_NET_RETURN_DECAY,
    PARAMETER_SEARCH_TRANSFORMATIONS,
    STRATEGY_SCHEMA_VERSION,
    STRATEGY_VOCABULARY,
    StrategyContext,
    StrategyDecision,
    select_strategy,
)


def _context(**overrides: object) -> StrategyContext:
    base: dict[str, object] = {
        "round_index": 1,
        "candidate_count": 3,
        "duplicate_rate": 0.1,
    }
    base.update(overrides)
    return StrategyContext(**base)  # type: ignore[arg-type]


# --- R1: explore -------------------------------------------------------------


def test_rule1_explore_when_no_candidates() -> None:
    decision = select_strategy(_context(round_index=0, candidate_count=0))
    assert decision.strategy == "explore"
    assert decision.reason.startswith("R1")


def test_rule1_explore_when_duplicate_rate_high() -> None:
    decision = select_strategy(_context(duplicate_rate=0.6))
    assert decision.strategy == "explore"
    assert decision.reason.startswith("R1")


def test_rule1_duplicate_rate_boundary_is_strict() -> None:
    decision = select_strategy(_context(duplicate_rate=HIGH_DUPLICATE_RATE))
    assert not (decision.strategy == "explore" and "duplicate_rate" in decision.reason)


# --- R2: simplify ------------------------------------------------------------


def test_rule2_simplify_on_oos_decay() -> None:
    decision = select_strategy(_context(oos_net_return_decay=0.6))
    assert decision.strategy == "simplify"
    assert decision.reason.startswith("R2")


def test_rule2_decay_boundary_is_strict() -> None:
    decision = select_strategy(_context(oos_net_return_decay=HIGH_OOS_NET_RETURN_DECAY))
    assert decision.strategy != "simplify"


def test_rule2_none_decay_is_not_a_breach() -> None:
    decision = select_strategy(_context(oos_net_return_decay=None))
    assert decision.strategy != "simplify"


def test_rule2_simplify_on_insufficient_oos_evidence_gate_reason() -> None:
    reason = (
        "INSUFFICIENT_OOS_EVIDENCE: max_oos_net_return_decay is configured "
        "but no OOS net_annualized_return evidence is available"
    )
    decision = select_strategy(_context(gate_blocking_reasons=(reason,)))
    assert decision.strategy == "simplify"
    assert "INSUFFICIENT_OOS_EVIDENCE" in decision.reason


def test_rule2_simplify_on_drawdown_breach_reason() -> None:
    decision = select_strategy(
        _context(gate_blocking_reasons=("max_drawdown below threshold: -0.42",))
    )
    assert decision.strategy == "simplify"
    assert "drawdown" in decision.reason


def test_rule2_unrelated_gate_reason_does_not_simplify() -> None:
    decision = select_strategy(
        _context(gate_blocking_reasons=("coverage below threshold: 0.1",))
    )
    assert decision.strategy != "simplify"


# --- R3: parameter_search ----------------------------------------------------


def test_rule3_parameter_search_on_turnover_breach() -> None:
    decision = select_strategy(_context(turnover_breach=True))
    assert decision.strategy == "parameter_search"
    assert decision.reason.startswith("R3")


def test_rule3_transformations_limited_to_decay_holding_window() -> None:
    decision = select_strategy(_context(turnover_breach=True))
    assert decision.allowed_formula_transformations == PARAMETER_SEARCH_TRANSFORMATIONS
    assert decision.allowed_formula_transformations == (
        "tune_decay_parameter",
        "tune_holding_period",
        "tune_window_length",
    )


# --- R4: exploit -------------------------------------------------------------


def test_rule4_exploit_when_improving_after_round_zero() -> None:
    decision = select_strategy(_context(round_index=1, best_score_delta_vs_seed=0.2))
    assert decision.strategy == "exploit"
    assert decision.reason.startswith("R4")


def test_rule4_requires_round_index_at_least_one() -> None:
    decision = select_strategy(_context(round_index=0, best_score_delta_vs_seed=0.2))
    assert decision.strategy == "explore"
    assert decision.reason.startswith("R6")


def test_rule4_unknown_delta_is_not_improvement() -> None:
    decision = select_strategy(_context(round_index=3, best_score_delta_vs_seed=None))
    assert decision.strategy == "explore"
    assert decision.reason.startswith("R6")


def test_rule4_zero_delta_is_not_improvement() -> None:
    decision = select_strategy(_context(round_index=1, best_score_delta_vs_seed=0.0))
    assert decision.strategy != "exploit"


# --- R5: recombine -----------------------------------------------------------


def test_rule5_recombine_when_stalled_with_two_mechanisms() -> None:
    decision = select_strategy(
        _context(
            round_index=2,
            best_score_delta_vs_seed=-0.1,
            successful_mechanisms=("momentum", "value"),
        )
    )
    assert decision.strategy == "recombine"
    assert decision.reason.startswith("R5")


def test_rule5_exploit_wins_over_recombine_when_improving() -> None:
    decision = select_strategy(
        _context(
            round_index=2,
            best_score_delta_vs_seed=0.3,
            successful_mechanisms=("momentum", "value"),
        )
    )
    assert decision.strategy == "exploit"


def test_rule5_requires_two_distinct_mechanisms() -> None:
    decision = select_strategy(
        _context(
            round_index=2,
            best_score_delta_vs_seed=-0.1,
            successful_mechanisms=("momentum", "momentum"),
        )
    )
    assert decision.strategy == "explore"
    assert decision.reason.startswith("R6")


def test_rule5_requires_round_index_at_least_two() -> None:
    decision = select_strategy(
        _context(
            round_index=1,
            best_score_delta_vs_seed=-0.1,
            successful_mechanisms=("momentum", "value"),
        )
    )
    assert decision.strategy == "explore"


def test_rule5_unknown_delta_is_not_a_stall() -> None:
    decision = select_strategy(
        _context(
            round_index=2,
            best_score_delta_vs_seed=None,
            successful_mechanisms=("momentum", "value"),
        )
    )
    assert decision.strategy == "explore"


# --- R6: fallback ------------------------------------------------------------


def test_rule6_fallback_explore() -> None:
    decision = select_strategy(_context())
    assert decision.strategy == "explore"
    assert decision.reason.startswith("R6")


# --- Precedence conflicts ----------------------------------------------------


def test_precedence_high_duplicates_beat_turnover_breach() -> None:
    decision = select_strategy(_context(duplicate_rate=0.9, turnover_breach=True))
    assert decision.strategy == "explore"
    assert decision.reason.startswith("R1")


def test_precedence_no_candidates_beats_decay() -> None:
    decision = select_strategy(
        _context(candidate_count=0, oos_net_return_decay=0.9)
    )
    assert decision.strategy == "explore"
    assert decision.reason.startswith("R1")


def test_precedence_decay_beats_turnover_breach() -> None:
    decision = select_strategy(
        _context(oos_net_return_decay=0.9, turnover_breach=True)
    )
    assert decision.strategy == "simplify"


def test_precedence_turnover_beats_exploit() -> None:
    decision = select_strategy(
        _context(round_index=2, best_score_delta_vs_seed=0.5, turnover_breach=True)
    )
    assert decision.strategy == "parameter_search"


def test_precedence_simplify_beats_exploit() -> None:
    decision = select_strategy(
        _context(round_index=2, best_score_delta_vs_seed=0.5, oos_net_return_decay=0.9)
    )
    assert decision.strategy == "simplify"


# --- Determinism -------------------------------------------------------------


def test_select_strategy_is_deterministic() -> None:
    context = _context(
        round_index=2,
        best_score_delta_vs_seed=-0.05,
        successful_mechanisms=("momentum", "value"),
        recent_fingerprints=("fp-a", "fp-b"),
        gate_blocking_reasons=("coverage below threshold: 0.1",),
    )
    first = select_strategy(context)
    second = select_strategy(context)
    assert first == second
    assert first.to_dict() == second.to_dict()


# --- forbidden_patterns passthrough -------------------------------------------


def test_forbidden_patterns_pass_through_recent_fingerprints() -> None:
    decision = select_strategy(
        _context(recent_fingerprints=("fp-1", "fp-2", "fp-1"))
    )
    assert decision.forbidden_patterns == ("fp-1", "fp-2")


def test_forbidden_patterns_present_for_every_strategy() -> None:
    fingerprints = ("fp-x", "fp-y")
    contexts = {
        "explore": _context(candidate_count=0, recent_fingerprints=fingerprints),
        "simplify": _context(oos_net_return_decay=0.9, recent_fingerprints=fingerprints),
        "parameter_search": _context(turnover_breach=True, recent_fingerprints=fingerprints),
        "exploit": _context(best_score_delta_vs_seed=0.2, recent_fingerprints=fingerprints),
        "recombine": _context(
            round_index=2,
            best_score_delta_vs_seed=-0.1,
            successful_mechanisms=("momentum", "value"),
            recent_fingerprints=fingerprints,
        ),
    }
    for expected_strategy, context in contexts.items():
        decision = select_strategy(context)
        assert decision.strategy == expected_strategy
        assert decision.forbidden_patterns == fingerprints


# --- Contract validation -----------------------------------------------------


def test_invalid_strategy_is_impossible_to_construct() -> None:
    with pytest.raises(ValueError, match="strategy must be one of"):
        StrategyDecision(
            strategy="mutate",  # type: ignore[arg-type]
            reason="not a valid strategy",
            expected_failure_mode="n/a",
        )


def test_every_vocabulary_strategy_is_constructible() -> None:
    for strategy in STRATEGY_VOCABULARY:
        decision = StrategyDecision(
            strategy=strategy,  # type: ignore[arg-type]
            reason="vocabulary check",
            expected_failure_mode="none expected",
        )
        assert decision.schema_version == STRATEGY_SCHEMA_VERSION


def test_decision_requires_reason_and_failure_mode() -> None:
    with pytest.raises(ValueError, match="reason is required"):
        StrategyDecision(strategy="explore", reason="  ", expected_failure_mode="x")
    with pytest.raises(ValueError, match="expected_failure_mode is required"):
        StrategyDecision(strategy="explore", reason="x", expected_failure_mode="")


def test_decision_rejects_foreign_schema_version() -> None:
    with pytest.raises(ValueError, match="schema_version"):
        StrategyDecision(
            strategy="explore",
            reason="x",
            expected_failure_mode="y",
            schema_version="qf.rd.strategy.v0",
        )


def test_context_rejects_out_of_range_duplicate_rate() -> None:
    with pytest.raises(ValueError, match="duplicate_rate"):
        _context(duplicate_rate=1.5)
    with pytest.raises(ValueError, match="duplicate_rate"):
        _context(duplicate_rate=-0.1)
    with pytest.raises(ValueError, match="duplicate_rate"):
        _context(duplicate_rate=float("nan"))


def test_context_rejects_nan_evidence_instead_of_fabricating() -> None:
    with pytest.raises(ValueError, match="oos_net_return_decay"):
        _context(oos_net_return_decay=float("nan"))
    with pytest.raises(ValueError, match="best_score_delta_vs_seed"):
        _context(best_score_delta_vs_seed=float("nan"))
    with pytest.raises(ValueError, match="best_objective_score"):
        _context(best_objective_score=float("inf"))


def test_context_rejects_negative_counters() -> None:
    with pytest.raises(ValueError, match="round_index"):
        _context(round_index=-1)
    with pytest.raises(ValueError, match="candidate_count"):
        _context(candidate_count=-1)


def test_context_normalizes_list_inputs_to_tuples() -> None:
    context = _context(
        gate_blocking_reasons=["a"],
        operator_risk_flags=["b"],
        successful_mechanisms=["c"],
        recent_fingerprints=["d"],
    )
    assert context.gate_blocking_reasons == ("a",)
    assert context.operator_risk_flags == ("b",)
    assert context.successful_mechanisms == ("c",)
    assert context.recent_fingerprints == ("d",)


def test_decision_serializes_with_schema_version() -> None:
    payload = select_strategy(_context(recent_fingerprints=("fp-1",))).to_dict()
    assert payload["schema_version"] == "qf.rd.strategy.v1"
    assert payload["strategy"] in STRATEGY_VOCABULARY
    assert payload["forbidden_patterns"] == ["fp-1"]
    assert isinstance(payload["allowed_formula_transformations"], list)

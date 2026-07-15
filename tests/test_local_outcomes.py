"""SE-P2 regression tests: the local RD-loop -> ResearchOutcome v2 producer.

Covers ``src/quant_forge/research_loop/local_outcomes.py`` against the
binding contract for DECISIONS.md "2026-07-13 -- Self-evolution engine
CP0", rulings SE-i/SE-ii/SE-vii, plus the SE-P2 review rework (2026-07-14,
P2-F1..F4): pass -> passed/NONE; block -> blocked with mapped closed
reasons; administrative and unmapped families FAIL CLOSED (a block carried
only by them maps to NO outcome, never a fabricated code); score maps to
the amended OBJECTIVE_SCORE_BELOW_GATE; max_drawdown is a non-negative
magnitude; settings_profile is a deterministic token of the effective
gate; no provider composite ever in metric_snapshot; None-for-absent
metrics; typed window; valid fingerprint; conservative sample_role;
producer neutrality (no integrations/ import, no clock).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from quant_forge.backtesting.service import EXTERNAL_OOS_ROLE, IN_SAMPLE_ROLE
from quant_forge.core.contracts import (
    BacktestResult,
    EvaluationResult,
    EvaluationSplitMetric,
    FactorDefinition,
    SimulationProfile,
)
from quant_forge.research_loop import local_outcomes
from quant_forge.research_loop.local_outcomes import experiment_result_to_outcome
from quant_forge.research_loop.outcomes import (
    METRIC_SPECS,
    REASON_CODES,
    REASON_NONE,
    STAGE_EVIDENCE_STRENGTH,
)
from quant_forge.research_loop.service import (
    ResearchCandidateResult,
    ResearchGate,
    ResearchHypothesis,
    ResearchSelfReview,
)

FP_HEX_UPPER = "AB12CD34EF560000"
REAL_RUN_ID = "rd_FTR_SEED_20260701T120000123456Z_deadbeef"
# The effective gate is now a REQUIRED mapper input (P2-F4): it feeds the
# derived settings_profile token and nothing else.
_GATE = ResearchGate()

_ALLOWED_METRIC_KEYS = frozenset(
    {"sharpe", "annualized_return", "max_drawdown", "turnover", "subwindow_sharpe", "self_correlation", "max_weight"}
)


def _factor(factor_id: str = "FTR_TEST", formula: str = "rank(close)") -> FactorDefinition:
    return FactorDefinition(factor_id=factor_id, name="test", formula=formula, status="candidate")


def _evaluation(
    *,
    split_metrics: tuple[EvaluationSplitMetric, ...] = (),
    simulation_profile: SimulationProfile | None = None,
) -> EvaluationResult:
    return EvaluationResult(
        factor_id="FTR_TEST",
        observations=100,
        coverage=0.9,
        rank_ic_mean=0.05,
        rank_ic_std=0.1,
        rank_icir=0.5,
        ic_days=100,
        artifact_path=Path("evaluation.json"),
        split_metrics=split_metrics,
        simulation_profile=simulation_profile or SimulationProfile(),
    )


def _backtest(
    *,
    sample_role: str = EXTERNAL_OOS_ROLE,
    net_long_short_sharpe: float | None = 1.2,
    gross_long_short_sharpe: float | None = 1.3,
    net_annualized_return: float | None = 0.1,
    gross_annualized_return: float | None = 0.12,
    net_max_drawdown: float | None = -0.05,
    gross_max_drawdown: float | None = -0.06,
    turnover_rate: float | None = 0.3,
) -> BacktestResult:
    return BacktestResult(
        factor_id="FTR_TEST",
        periods=50,
        holding_days=5,
        cumulative_return=0.2,
        annualized_return=gross_annualized_return,
        annualized_volatility=0.1,
        max_drawdown=gross_max_drawdown,
        artifact_path=Path("backtest.json"),
        sample_role=sample_role,
        net_long_short_sharpe=net_long_short_sharpe,
        gross_long_short_sharpe=gross_long_short_sharpe,
        net_annualized_return=net_annualized_return,
        gross_annualized_return=gross_annualized_return,
        net_max_drawdown=net_max_drawdown,
        gross_max_drawdown=gross_max_drawdown,
        turnover_rate=turnover_rate,
    )


def _result(
    *,
    gate_passed: bool,
    gate_reasons: tuple[str, ...] = (),
    factor: FactorDefinition | None = None,
    evaluation: EvaluationResult | None = None,
    backtest: BacktestResult | None = None,
    selection_backtest: BacktestResult | None | str = "default",
    formula_fingerprint: str = FP_HEX_UPPER,
) -> ResearchCandidateResult:
    factor = factor or _factor()
    evaluation = evaluation or _evaluation()
    backtest = backtest or _backtest()
    if selection_backtest == "default":
        selection_backtest = _backtest(sample_role=IN_SAMPLE_ROLE)
    return ResearchCandidateResult(
        hypothesis=ResearchHypothesis(text="idea", rationale="because"),
        factor=factor,
        evaluation=evaluation,
        backtest=backtest,
        split_weighted_icir=0.4,
        score=0.3,
        gate_passed=gate_passed,
        gate_reasons=gate_reasons,
        self_review=ResearchSelfReview(
            source="local_self_review", summary="s", strengths=(), risks=(), next_hypotheses=()
        ),
        formula_fingerprint=formula_fingerprint,
        selection_backtest=selection_backtest,
    )


# ---------------------------------------------------------------------------
# verdict / reason_codes
# ---------------------------------------------------------------------------


def test_gate_passed_maps_to_passed_none() -> None:
    outcome = experiment_result_to_outcome(_result(gate_passed=True), run_id=REAL_RUN_ID, gate=_GATE)
    assert outcome is not None
    assert outcome.origin == "local"
    assert outcome.stage == "gate"
    assert outcome.verdict == "passed"
    assert outcome.reason_codes == (REASON_NONE,)
    assert outcome.evidence_strength == STAGE_EVIDENCE_STRENGTH["gate"] == "local_backtest"


@pytest.mark.parametrize(
    ("reason", "expected_code"),
    [
        ("ic_days 1 < 5", "INSUFFICIENT_SAMPLE"),
        ("backtest_periods 0 < 1", "INSUFFICIENT_SAMPLE"),
        ("INSUFFICIENT_OOS_EVIDENCE: min_oos_net_annualized_return is configured", "INSUFFICIENT_SAMPLE"),
        ("INSUFFICIENT_EVIDENCE: max_turnover_rate is configured", "INSUFFICIENT_SAMPLE"),
        ("coverage 0.1000 < 0.5000", "DATA_UNAVAILABLE"),
        ("rebalance_rate above threshold: 0.9", "TURNOVER_TOO_HIGH"),
        ("turnover_rate above threshold: 1.2", "TURNOVER_TOO_HIGH"),
        ("net_return_retention below threshold: 0.1", "RETURNS_BELOW_GATE"),
        # Reviewed contract amendment (P2-F1): the blended objective
        # composite has its own honest closed code now.
        ("score 0.010000 < 0.500000", "OBJECTIVE_SCORE_BELOW_GATE"),
        # The OOS segment name is the ONLY variable family the local gate
        # emits (segment shortfall + decay shapes), anchored-matched:
        ("OOS net return decay exceeds 0.3", "RETURNS_BELOW_GATE"),
        ("OOS net_annualized_return below threshold: 0.01", "RETURNS_BELOW_GATE"),
        ("OOS_2024H2 net_annualized_return below threshold: 0.01", "RETURNS_BELOW_GATE"),
        ("oos_2 net_annualized_return below threshold: 0.01", "RETURNS_BELOW_GATE"),
    ],
)
def test_blocked_reason_family_maps_to_closed_code(reason: str, expected_code: str) -> None:
    outcome = experiment_result_to_outcome(_result(gate_passed=False, gate_reasons=(reason,)), run_id=REAL_RUN_ID, gate=_GATE)
    assert outcome is not None
    assert outcome.verdict == "blocked"
    assert outcome.reason_codes == (expected_code,)
    assert expected_code in REASON_CODES


@pytest.mark.parametrize(
    "reason",
    [
        # Administrative families: workflow bookkeeping, not scientific
        # evidence (P2-F1) -- alone they must produce NO outcome at all.
        "duplicate result signature matches FTR_OTHER",
        "existing active status requires explicit user decision",
        "passed smoke research gate",
        # Unknown families FAIL CLOSED (P2-F2): the pre-review behavior
        # collapsed these onto VALIDATION_ERROR, fabricating a validation
        # failure that never happened.
        "something totally unrecognized happened",
        "max_single_name_weight above threshold: 0.4",
        "self_correlation too high: 0.9",
        "redundancy too high: 0.9",
        "max_drawdown_floor breached: -0.5",
        "region mismatch detected",
        # Token-boundary adversarial probes (P2-F2): the retired substring
        # fallback dishonestly classified every one of these.
        "returning_candidate rejected",
        "no_return_path found",
        "return_code_error 137",
        "sharpening_failed for kernel",
        "lightweight_error in adapter",
        "regionalization pending",
        "oosmalformed_no_separator value",
    ],
)
def test_unrepresentable_families_fail_closed_to_no_outcome(reason: str) -> None:
    outcome = experiment_result_to_outcome(
        _result(gate_passed=False, gate_reasons=(reason,)), run_id=REAL_RUN_ID, gate=_GATE
    )
    assert outcome is None


def test_administrative_families_are_omitted_when_real_blockers_coexist() -> None:
    outcome = experiment_result_to_outcome(
        _result(
            gate_passed=False,
            gate_reasons=(
                "duplicate result signature matches FTR_OTHER",
                "turnover_rate above threshold: 1.2",
                "something totally unrecognized happened",
            ),
        ),
        run_id=REAL_RUN_ID,
        gate=_GATE,
    )
    assert outcome is not None
    assert outcome.reason_codes == ("TURNOVER_TOO_HIGH",)


def test_blocked_reason_codes_are_sorted_and_deduped_closed_set() -> None:
    outcome = experiment_result_to_outcome(
        _result(gate_passed=False, gate_reasons=("turnover_rate above threshold: 1.2", "rebalance_rate above threshold: 0.9")),
        run_id=REAL_RUN_ID, gate=_GATE,
    )
    assert outcome is not None
    # Both reasons map to the SAME closed code: deduped to one entry.
    assert outcome.reason_codes == ("TURNOVER_TOO_HIGH",)


def test_blocked_reason_codes_never_contain_none() -> None:
    outcome = experiment_result_to_outcome(
        _result(gate_passed=False, gate_reasons=("score 0.1 < 0.5",)), run_id=REAL_RUN_ID, gate=_GATE
    )
    assert outcome is not None
    assert REASON_NONE not in outcome.reason_codes


def test_reason_code_table_targets_are_all_in_closed_vocabulary() -> None:
    targets = set(local_outcomes._EXACT_FAMILY_REASON_CODES.values())
    targets.update(code for _, code in local_outcomes._VARIABLE_FAMILY_REASON_CODES)
    assert targets <= REASON_CODES
    assert REASON_NONE not in targets  # a blocked outcome never carries NONE
    # The rejected pre-review fallback must stay gone: no mapping target is
    # VALIDATION_ERROR, and administrative families map to no code at all.
    assert "VALIDATION_ERROR" not in targets
    assert not (set(local_outcomes._EXACT_FAMILY_REASON_CODES) & local_outcomes._ADMINISTRATIVE_FAMILIES)


# ---------------------------------------------------------------------------
# reason-family extraction (mirrors the retired service._gate_reason_families
# rule -- that function was removed from service.py as dead code once the
# seam migrated to this module, so local_outcomes._reason_families is now
# the SOLE implementation of the "leading colon-segment, then leading
# space-token" family rule; this is a characterization test of ITS behavior,
# not a parity check against a sibling that no longer exists)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reasons", "expected"),
    [
        ((), ()),
        (("score 0.1 < 0.5",), ("score",)),
        (("ic_days 1 < 5", "coverage 0.1 < 0.5"), ("coverage", "ic_days")),
        (("INSUFFICIENT_OOS_EVIDENCE: x is configured but y",), ("INSUFFICIENT_OOS_EVIDENCE",)),
        (
            ("duplicate result signature matches FTR_X", "existing active status requires explicit user decision"),
            ("duplicate", "existing"),
        ),
        (
            ("OOS net return decay exceeds 0.3", "OOS_2 net_annualized_return below threshold: 0.1"),
            ("OOS", "OOS_2"),
        ),
        # exact duplicate families collapse (sorted + deduped, like the
        # retired service helper).
        (("score 0.1 < 0.5", "score 0.2 < 0.5"), ("score",)),
    ],
)
def test_reason_family_extraction(reasons: tuple[str, ...], expected: tuple[str, ...]) -> None:
    assert local_outcomes._reason_families(reasons) == expected


# ---------------------------------------------------------------------------
# metric_snapshot: closed allowlist, None-for-absent, never a fabricated 0
# ---------------------------------------------------------------------------


def test_metric_snapshot_only_allowlisted_keys_ever_populated() -> None:
    outcome = experiment_result_to_outcome(_result(gate_passed=True), run_id=REAL_RUN_ID, gate=_GATE)
    assert outcome is not None
    assert set(outcome.metric_snapshot) <= _ALLOWED_METRIC_KEYS
    for key in outcome.metric_snapshot:
        assert key in METRIC_SPECS
    # No provider composite (fitness) and no IC-family key this producer
    # never claims to measure.
    assert "fitness" not in outcome.metric_snapshot
    assert "icir" not in outcome.metric_snapshot
    assert "ic_mean" not in outcome.metric_snapshot
    assert "redundancy" not in outcome.metric_snapshot


def test_metric_snapshot_unavailable_metrics_are_none_not_zero() -> None:
    outcome = experiment_result_to_outcome(_result(gate_passed=True), run_id=REAL_RUN_ID, gate=_GATE)
    assert outcome is not None
    for key in ("subwindow_sharpe", "self_correlation", "max_weight"):
        reading = outcome.metric_snapshot[key]
        assert reading.value is None
        assert reading.basis == ""


def test_metric_snapshot_prefers_net_falls_back_to_gross_labeled_honestly() -> None:
    selection = _backtest(
        sample_role=IN_SAMPLE_ROLE,
        net_long_short_sharpe=None,
        gross_long_short_sharpe=0.7,
        net_annualized_return=0.2,
        gross_annualized_return=0.25,
        net_max_drawdown=None,
        gross_max_drawdown=-0.3,
        turnover_rate=0.4,
    )
    outcome = experiment_result_to_outcome(
        _result(gate_passed=True, selection_backtest=selection), run_id=REAL_RUN_ID, gate=_GATE
    )
    assert outcome is not None
    assert outcome.metric_snapshot["sharpe"].value == 0.7
    assert outcome.metric_snapshot["sharpe"].basis == "gross"
    assert outcome.metric_snapshot["annualized_return"].value == 0.2
    assert outcome.metric_snapshot["annualized_return"].basis == "net"
    # abs() of the backtester's -0.3 (P2-F3): the frozen contract defines
    # max_drawdown as a non-negative magnitude; basis still says gross.
    assert outcome.metric_snapshot["max_drawdown"].value == 0.3
    assert outcome.metric_snapshot["max_drawdown"].basis == "gross"
    assert outcome.metric_snapshot["turnover"].value == 0.4
    assert outcome.metric_snapshot["turnover"].basis == ""


def test_metric_snapshot_max_drawdown_is_nonnegative_magnitude() -> None:
    # P2-F3: the local backtester reports drawdown in a negative-return
    # convention; the frozen METRIC_SPECS entry demands a non-negative
    # magnitude. Sign flips to magnitude; None stays None; basis preserved.
    selection = _backtest(sample_role=IN_SAMPLE_ROLE, net_max_drawdown=-0.42)
    outcome = experiment_result_to_outcome(
        _result(gate_passed=True, selection_backtest=selection), run_id=REAL_RUN_ID, gate=_GATE
    )
    assert outcome is not None
    assert outcome.metric_snapshot["max_drawdown"].value == 0.42
    assert outcome.metric_snapshot["max_drawdown"].basis == "net"

    absent = _backtest(sample_role=IN_SAMPLE_ROLE, net_max_drawdown=None, gross_max_drawdown=None)
    outcome_absent = experiment_result_to_outcome(
        _result(gate_passed=True, selection_backtest=absent), run_id=REAL_RUN_ID, gate=_GATE
    )
    assert outcome_absent is not None
    assert outcome_absent.metric_snapshot["max_drawdown"].value is None
    assert outcome_absent.metric_snapshot["max_drawdown"].basis == ""


def test_metric_snapshot_absent_both_sides_is_none() -> None:
    selection = _backtest(
        sample_role=IN_SAMPLE_ROLE,
        net_long_short_sharpe=None,
        gross_long_short_sharpe=None,
        turnover_rate=None,
    )
    outcome = experiment_result_to_outcome(
        _result(gate_passed=True, selection_backtest=selection), run_id=REAL_RUN_ID, gate=_GATE
    )
    assert outcome is not None
    assert outcome.metric_snapshot["sharpe"].value is None
    assert outcome.metric_snapshot["turnover"].value is None


def test_metric_snapshot_uses_selection_backtest_not_the_oos_field() -> None:
    # result.backtest is actually the EXTERNAL OOS backtest by construction
    # (service._evaluate_final_trial passes backtest=external_oos_backtest);
    # metric_snapshot must come from result.selection_backtest instead (see
    # module docstring "sample_role" section), so a distinctive OOS-only
    # value must NOT leak into the snapshot.
    oos_only = _backtest(sample_role=EXTERNAL_OOS_ROLE, net_long_short_sharpe=99.0)
    selection = _backtest(sample_role=IN_SAMPLE_ROLE, net_long_short_sharpe=1.5)
    outcome = experiment_result_to_outcome(
        _result(gate_passed=True, backtest=oos_only, selection_backtest=selection), run_id=REAL_RUN_ID, gate=_GATE
    )
    assert outcome is not None
    assert outcome.metric_snapshot["sharpe"].value == 1.5


# ---------------------------------------------------------------------------
# sample_role: conservative, never guesses OOS
# ---------------------------------------------------------------------------


def test_sample_role_in_sample_when_selection_backtest_self_reports_it() -> None:
    outcome = experiment_result_to_outcome(_result(gate_passed=True), run_id=REAL_RUN_ID, gate=_GATE)
    assert outcome is not None
    assert outcome.sample_role == "in_sample"


def test_sample_role_unspecified_when_selection_backtest_missing() -> None:
    outcome = experiment_result_to_outcome(
        _result(gate_passed=True, selection_backtest=None), run_id=REAL_RUN_ID, gate=_GATE
    )
    assert outcome is not None
    assert outcome.sample_role == "unspecified"


def test_sample_role_never_guesses_out_of_sample() -> None:
    # Even though result.backtest is literally the "external_oos_backtest"
    # object, the mapper must never claim "out_of_sample" for it (see module
    # docstring): only a demonstrably in_sample selection_backtest earns
    # "in_sample"; everything else is "unspecified", never "out_of_sample".
    mislabeled_selection = _backtest(sample_role=EXTERNAL_OOS_ROLE)  # not IN_SAMPLE_ROLE
    outcome = experiment_result_to_outcome(
        _result(gate_passed=True, selection_backtest=mislabeled_selection), run_id=REAL_RUN_ID, gate=_GATE
    )
    assert outcome is not None
    assert outcome.sample_role == "unspecified"
    assert outcome.sample_role != "out_of_sample"


# ---------------------------------------------------------------------------
# factor_id / factor_fingerprint identity
# ---------------------------------------------------------------------------


def test_factor_id_with_disallowed_equals_character_returns_none() -> None:
    # FactorDefinition allows "=" (mirrors factor_library's id charset); the
    # frozen outcomes.py identity contract does not. No representable
    # identity -> None (log-skip at the caller), never rewritten.
    factor = _factor(factor_id="FTR_A=1")
    outcome = experiment_result_to_outcome(_result(gate_passed=True, factor=factor), run_id=REAL_RUN_ID, gate=_GATE)
    assert outcome is None


def test_factor_fingerprint_is_lowercased_and_hex_valid() -> None:
    outcome = experiment_result_to_outcome(
        _result(gate_passed=True, formula_fingerprint="AB12CD34EF560000"), run_id=REAL_RUN_ID, gate=_GATE
    )
    assert outcome is not None
    assert outcome.factor_fingerprint == "ab12cd34ef560000"
    assert re.fullmatch(r"[0-9a-f]{16,64}", outcome.factor_fingerprint)


def test_factor_fingerprint_falls_back_to_factor_formula_fingerprint_when_blank() -> None:
    outcome = experiment_result_to_outcome(_result(gate_passed=True, formula_fingerprint=""), run_id=REAL_RUN_ID, gate=_GATE)
    assert outcome is not None
    assert re.fullmatch(r"[0-9a-f]{16,64}", outcome.factor_fingerprint)


# ---------------------------------------------------------------------------
# window: typed, honest-unavailable, never raises
# ---------------------------------------------------------------------------


def test_window_available_from_split_metrics() -> None:
    split = EvaluationSplitMetric(
        name="IS",
        start_date="2024-01-01",
        end_date="2024-06-30",
        date_count=100,
        observations=100,
        coverage=0.9,
        rank_ic_mean=0.05,
        rank_ic_std=0.1,
        rank_icir=0.5,
        ic_days=100,
    )
    outcome = experiment_result_to_outcome(
        _result(gate_passed=True, evaluation=_evaluation(split_metrics=(split,))), run_id=REAL_RUN_ID, gate=_GATE
    )
    assert outcome is not None
    assert outcome.window.status == "available"
    assert outcome.window.start_date == "2024-01-01"
    assert outcome.window.end_date == "2024-06-30"


def test_window_unavailable_when_no_split_metrics() -> None:
    outcome = experiment_result_to_outcome(_result(gate_passed=True), run_id=REAL_RUN_ID, gate=_GATE)
    assert outcome is not None
    assert outcome.window.status == "unavailable"
    assert outcome.window.canonical() == ""


# ---------------------------------------------------------------------------
# scope
# ---------------------------------------------------------------------------


def test_scope_asset_and_universe_from_simulation_profile() -> None:
    profile = SimulationProfile(instrument_type="equity", universe="local_panel")
    outcome = experiment_result_to_outcome(
        _result(gate_passed=True, evaluation=_evaluation(simulation_profile=profile)), run_id=REAL_RUN_ID, gate=_GATE
    )
    assert outcome is not None
    assert outcome.scope.asset_class == "equity"
    assert outcome.scope.universe == "local_panel"


def test_scope_family_literal_and_settings_token_are_nonempty_valid_dims() -> None:
    # Required non-empty: outcomes.OutcomeScope.signature_payloads()
    # disambiguates every signature by evidence_run_id whenever EITHER is
    # empty (R-F4), which would make promotion structurally unreachable for
    # every local outcome (see module docstring "scope" section).
    outcome = experiment_result_to_outcome(_result(gate_passed=True), run_id=REAL_RUN_ID, gate=_GATE)
    assert outcome is not None
    assert outcome.scope.factor_family == "rd_local_candidate"
    assert re.fullmatch(r"rd_[0-9a-f]{10}", outcome.scope.settings_profile)
    assert outcome.scope.factor_family not in ("unknown", "global")
    assert outcome.scope.settings_profile not in ("unknown", "global")


def test_settings_token_is_deterministic_per_gate_and_distinct_across_gates() -> None:
    # P2-F4: evidence produced under materially different gate settings must
    # never share a signature. Equal gates -> equal token (promotion within
    # one configuration still works); different thresholds -> different
    # token; the constructor-default gate gets no special name.
    strict = ResearchGate(max_turnover_rate=0.2)
    loose = ResearchGate(max_turnover_rate=1.5)
    reason = ("turnover_rate above threshold: 0.9",)

    strict_a = experiment_result_to_outcome(
        _result(gate_passed=False, gate_reasons=reason), run_id=REAL_RUN_ID, gate=strict
    )
    strict_b = experiment_result_to_outcome(
        _result(gate_passed=False, gate_reasons=reason), run_id=REAL_RUN_ID, gate=ResearchGate(max_turnover_rate=0.2)
    )
    loose_outcome = experiment_result_to_outcome(
        _result(gate_passed=False, gate_reasons=reason), run_id=REAL_RUN_ID, gate=loose
    )
    default_outcome = experiment_result_to_outcome(
        _result(gate_passed=False, gate_reasons=reason), run_id=REAL_RUN_ID, gate=_GATE
    )
    assert strict_a is not None and strict_b is not None and loose_outcome is not None and default_outcome is not None
    assert strict_a.scope.settings_profile == strict_b.scope.settings_profile
    assert strict_a.scope.settings_profile != loose_outcome.scope.settings_profile
    assert strict_a.scope.settings_profile != default_outcome.scope.settings_profile
    assert loose_outcome.scope.settings_profile != default_outcome.scope.settings_profile
    # Distinct settings tokens flow into distinct signatures: the merged
    # cross-gate promotion path P2-F4 attacked is structurally closed.
    assert strict_a.signature_payloads() != loose_outcome.signature_payloads()


def test_scope_horizon_bucket_stays_unknown() -> None:
    outcome = experiment_result_to_outcome(_result(gate_passed=True), run_id=REAL_RUN_ID, gate=_GATE)
    assert outcome is not None
    assert outcome.scope.horizon_bucket == ""


def test_scope_dimension_outside_grammar_degrades_to_empty_not_a_raise() -> None:
    profile = SimulationProfile(instrument_type="US Equity", universe="local_panel")
    outcome = experiment_result_to_outcome(
        _result(gate_passed=True, evaluation=_evaluation(simulation_profile=profile)), run_id=REAL_RUN_ID, gate=_GATE
    )
    assert outcome is not None
    assert outcome.scope.asset_class == ""  # "US Equity" has a space + uppercase: not a valid dim token
    assert outcome.scope.universe == "local_panel"


# ---------------------------------------------------------------------------
# observed_at: no clock, run_id-derived, STOPs rather than fabricating
# ---------------------------------------------------------------------------


def test_observed_at_parsed_from_run_id_embedded_timestamp() -> None:
    outcome = experiment_result_to_outcome(
        _result(gate_passed=True), run_id="rd_FTR_SEED_20260701T120000123456Z_deadbeef", gate=_GATE
    )
    assert outcome is not None
    assert outcome.observed_at == "2026-07-01T12:00:00.123456+00:00"


def test_observed_at_raises_when_run_id_carries_no_timestamp() -> None:
    with pytest.raises(ValueError, match="cannot derive observed_at"):
        experiment_result_to_outcome(_result(gate_passed=True), run_id="not-a-real-run-id", gate=_GATE)


# ---------------------------------------------------------------------------
# neutrality: no integrations/ or provider/network imports; no clock
# ---------------------------------------------------------------------------


def _module_import_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_local_outcomes_module_imports_nothing_from_integrations_or_provider() -> None:
    import quant_forge.research_loop.local_outcomes as module

    names = _module_import_names(Path(module.__file__))
    assert not any("integrations" in name for name in names)
    assert not any(name.startswith("quant_forge_worldquant") for name in names)
    assert not any("worldquant" in name for name in names)


_CLOCK_CALL_NAMES = frozenset({"now", "utcnow", "today", "time", "time_ns", "perf_counter", "monotonic"})


def _clock_call_sites(path: Path) -> list[str]:
    """Real (non-docstring/comment) calls to a clock-shaped function name.

    AST-based, not a substring search: a naive ``"datetime.now(" in source``
    check false-positives on this module's OWN docstring prose describing
    the no-clock design (see module docstring "observed_at" section), which
    literally contains the text ``datetime.now()`` inside a code-quoted
    sentence. Walking actual ``Call`` nodes only sees real code.
    """

    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name in _CLOCK_CALL_NAMES:
                hits.append(name)
    return hits


def test_local_outcomes_module_calls_no_clock_anywhere_in_code() -> None:
    import quant_forge.research_loop.local_outcomes as module

    assert _clock_call_sites(Path(module.__file__)) == []

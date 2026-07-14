"""SE-P1 regression tests: the neutral ResearchOutcome v2 contract (Seam 1).

Covers ``src/quant_forge/research_loop/outcomes.py`` against the binding
battery for DECISIONS.md "2026-07-13 -- Self-evolution engine CP0", rulings
SE-i/SE-ii/SE-iv/SE-vii. Pure module: no tmp_path, no disk I/O -- this file
only exercises the neutral dataclasses, the pure mapper, and their
integration with the existing pure ``memory.promote`` policy.
"""

from __future__ import annotations

import dataclasses
import os
import random
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from quant_forge.lineage.store import canonical_fingerprint, redact_free_text
from quant_forge.research_loop.memory import RULE_CANDIDATE_STATUS, MemoryObservation, promote
from quant_forge.research_loop.outcomes import (
    EVIDENCE_STRENGTH_RANK,
    EVIDENCE_STRENGTHS,
    METRIC_SPECS,
    REASON_CODES,
    REASON_NONE,
    RESEARCH_OUTCOME_SCHEMA_VERSION,
    SAMPLE_ROLES,
    SIGNATURE_CONTRACT_VERSION,
    STAGE_EVIDENCE_STRENGTH,
    STAGES,
    MetricReading,
    OutcomeScope,
    OutcomeWindow,
    ResearchOutcome,
    outcome_to_observations,
)

T1 = "2026-07-01T00:00:00+00:00"
T2 = "2026-07-02T00:00:00+00:00"
T3 = "2026-07-03T00:00:00+00:00"
T4 = "2026-07-04T00:00:00+00:00"

# 16-char lowercase-hex fingerprints for two distinct "factors".
FP_A = "a1b2c3d4e5f6a1b2"
FP_B = "1a2b3c4d5e6f1a2b"

WINDOW_1 = OutcomeWindow(status="available", start_date="2024-01-01", end_date="2024-06-30")
WINDOW_2 = OutcomeWindow(status="available", start_date="2024-07-01", end_date="2024-12-31")

FP_C = "9f8e7d6c5b4a9f8e"

# Promotion-semantics tests need KNOWN generalization dimensions: unknown
# family/settings gain a per-evidence-run signature disambiguator (R-F4) and
# mechanically stay at trace tier, which is pinned separately below.
SCOPE_KNOWN = OutcomeScope(
    asset_class="us_equity",
    universe="sp500",
    factor_family="reversal",
    horizon_bucket="d5",
    settings_profile="default",
)


def _outcome(**overrides: Any) -> ResearchOutcome:
    """A valid baseline passed/NONE outcome; override only what a test needs."""

    kwargs: dict[str, Any] = dict(
        origin="local",
        stage="evaluate",
        verdict="passed",
        factor_id="FTR_DEMO",
        factor_fingerprint=FP_A,
        observed_at=T1,
        reason_codes=(REASON_NONE,),
    )
    kwargs.update(overrides)
    return ResearchOutcome(**kwargs)


def _blocked_outcome(**overrides: Any) -> ResearchOutcome:
    """A valid baseline blocked outcome carrying one real reason code."""

    kwargs: dict[str, Any] = dict(
        origin="local",
        stage="evaluate",
        verdict="blocked",
        factor_id="FTR_DEMO",
        factor_fingerprint=FP_A,
        observed_at=T1,
        reason_codes=("TURNOVER_TOO_HIGH",),
    )
    kwargs.update(overrides)
    return ResearchOutcome(**kwargs)


def _sig(outcome: ResearchOutcome, index: int = 0) -> str:
    return canonical_fingerprint(outcome.signature_payloads()[index])


# ---------------------------------------------------------------------------
# 1. IDENTITY: outcome_id / evidence_run_id / signature hash
# ---------------------------------------------------------------------------


def test_outcome_id_invariant_under_observed_at_change() -> None:
    a = _outcome(observed_at=T1)
    b = _outcome(observed_at=T2)
    assert a.outcome_id() == b.outcome_id()


def test_outcome_id_changes_with_verdict() -> None:
    a = _outcome(verdict="passed", reason_codes=(REASON_NONE,))
    b = _outcome(verdict="unknown", reason_codes=(REASON_NONE,))
    assert a.outcome_id() != b.outcome_id()


def test_outcome_id_changes_with_metric_value() -> None:
    a = _outcome(metric_snapshot={"sharpe": MetricReading(value=1.0)})
    b = _outcome(metric_snapshot={"sharpe": MetricReading(value=1.5)})
    assert a.outcome_id() != b.outcome_id()


def test_outcome_id_changes_with_scope_dim() -> None:
    a = _outcome(scope=OutcomeScope())
    b = _outcome(scope=OutcomeScope(asset_class="us_equity"))
    assert a.outcome_id() != b.outcome_id()


def test_evidence_run_id_invariant_under_timestamp_and_metric_changes() -> None:
    a = _outcome(observed_at=T1, metric_snapshot={"sharpe": MetricReading(value=1.0)})
    b = _outcome(observed_at=T2, metric_snapshot={"sharpe": MetricReading(value=99.0)})
    assert a.evidence_run_id() == b.evidence_run_id()


def test_evidence_run_id_changes_with_factor_fingerprint() -> None:
    a = _outcome(factor_fingerprint=FP_A)
    b = _outcome(factor_fingerprint=FP_B)
    assert a.evidence_run_id() != b.evidence_run_id()


def test_evidence_run_id_changes_with_window() -> None:
    a = _outcome(window=WINDOW_1)
    b = _outcome(window=WINDOW_2)
    assert a.evidence_run_id() != b.evidence_run_id()


def test_evidence_run_id_changes_with_stage() -> None:
    a = _outcome(stage="evaluate")
    b = _outcome(stage="gate")
    assert a.evidence_run_id() != b.evidence_run_id()


def test_signature_byte_equal_across_metric_and_timestamp_only_changes() -> None:
    a = _outcome(observed_at=T1, metric_snapshot={"sharpe": MetricReading(value=1.0)})
    b = _outcome(observed_at=T2, metric_snapshot={"sharpe": MetricReading(value=-3.5)})
    assert _sig(a) == _sig(b)


def test_signature_differs_across_origin() -> None:
    a = _outcome(origin="local")
    b = _outcome(origin="external_plugin")
    assert _sig(a) != _sig(b)


def test_signature_differs_across_stage() -> None:
    a = _outcome(stage="evaluate")
    b = _outcome(stage="gate")
    assert _sig(a) != _sig(b)


def test_signature_differs_across_verdict() -> None:
    a = _outcome(verdict="passed", reason_codes=(REASON_NONE,))
    b = _outcome(verdict="unknown", reason_codes=(REASON_NONE,))
    assert _sig(a) != _sig(b)


def test_signature_differs_across_reason() -> None:
    a = _blocked_outcome(reason_codes=("TURNOVER_TOO_HIGH",))
    b = _blocked_outcome(reason_codes=("REDUNDANCY_HIGH",))
    assert _sig(a) != _sig(b)


def test_signature_differs_across_family() -> None:
    a = _outcome(scope=OutcomeScope(factor_family="momentum"))
    b = _outcome(scope=OutcomeScope(factor_family="reversal"))
    assert _sig(a) != _sig(b)


def test_signature_differs_across_horizon() -> None:
    a = _outcome(scope=OutcomeScope(horizon_bucket="short"))
    b = _outcome(scope=OutcomeScope(horizon_bucket="long"))
    assert _sig(a) != _sig(b)


def test_signature_differs_across_settings() -> None:
    a = _outcome(scope=OutcomeScope(settings_profile="default"))
    b = _outcome(scope=OutcomeScope(settings_profile="alt"))
    assert _sig(a) != _sig(b)


def test_signature_differs_across_asset_and_universe_scope() -> None:
    a = _outcome(scope=OutcomeScope(asset_class="us_equity", universe="sp500"))
    b = _outcome(scope=OutcomeScope(asset_class="us_futures", universe="cme"))
    assert _sig(a) != _sig(b)


# ---------------------------------------------------------------------------
# 2. ANTI-GAMING: promote() integration over outcome_to_observations rows
# ---------------------------------------------------------------------------


def test_resimulated_factor_shares_one_run_id_and_never_promotes() -> None:
    # Same fingerprint/window/stage, jittered timestamps + metrics: one
    # logical evidence run no matter how many times it is re-simulated.
    outcomes = [
        _blocked_outcome(scope=SCOPE_KNOWN, window=WINDOW_1, observed_at=T1, metric_snapshot={"turnover": MetricReading(value=0.65)}),
        _blocked_outcome(scope=SCOPE_KNOWN, window=WINDOW_1, observed_at=T2, metric_snapshot={"turnover": MetricReading(value=0.66)}),
        _blocked_outcome(scope=SCOPE_KNOWN, window=WINDOW_1, observed_at=T3, metric_snapshot={"turnover": MetricReading(value=0.64)}),
    ]
    observations = [row for outcome in outcomes for row in outcome_to_observations(outcome)]

    assert len(observations) == 3
    run_ids = {observation.run_id for observation in observations}
    assert run_ids == {outcomes[0].evidence_run_id()}
    assert promote(observations) == ()


def test_second_factor_hitting_same_reason_yields_exactly_one_failure_row() -> None:
    outcomes = [
        _blocked_outcome(scope=SCOPE_KNOWN, window=WINDOW_1, observed_at=T1, metric_snapshot={"turnover": MetricReading(value=0.65)}),
        _blocked_outcome(scope=SCOPE_KNOWN, window=WINDOW_1, observed_at=T2, metric_snapshot={"turnover": MetricReading(value=0.66)}),
        _blocked_outcome(scope=SCOPE_KNOWN, window=WINDOW_1, observed_at=T3, metric_snapshot={"turnover": MetricReading(value=0.64)}),
        _blocked_outcome(
            scope=SCOPE_KNOWN,
            factor_id="FTR_SECOND",
            factor_fingerprint=FP_B,
            window=WINDOW_1,
            observed_at=T4,
            metric_snapshot={"turnover": MetricReading(value=0.70)},
        ),
    ]
    observations = [row for outcome in outcomes for row in outcome_to_observations(outcome)]
    run_ids = {observation.run_id for observation in observations}
    assert len(run_ids) == 2  # a genuinely second factor -> a second evidence run

    decisions = promote(observations)

    assert len(decisions) == 1
    decision = decisions[0]
    assert decision.kind == "failure"
    assert decision.status == "active"
    # SE-ii evidence-unit cap: three rows from factor A collapse to ONE unit;
    # observation_count counts independent studies, never retries.
    assert decision.observation_count == 2


def test_passed_corpus_two_factors_yields_finding_not_failure() -> None:
    outcomes = [
        _outcome(scope=SCOPE_KNOWN, factor_id="FTR_ONE", factor_fingerprint=FP_A, observed_at=T1),
        _outcome(scope=SCOPE_KNOWN, factor_id="FTR_TWO", factor_fingerprint=FP_B, observed_at=T2),
    ]
    observations = [row for outcome in outcomes for row in outcome_to_observations(outcome)]
    run_ids = {observation.run_id for observation in observations}
    assert len(run_ids) == 2

    decisions = promote(observations)

    assert len(decisions) == 1
    assert decisions[0].kind == "finding"
    assert decisions[0].status == "active"


# ---------------------------------------------------------------------------
# 3. WINDOW CEILING: a rule candidate needs >=2 distinct canonical windows
# ---------------------------------------------------------------------------


def test_single_canonical_window_never_yields_rule_candidate() -> None:
    # 3 observations, 2 distinct evidence runs (factor A resimulated once +
    # a second factor B), but every row shares ONE canonical window.
    outcomes = [
        _blocked_outcome(scope=SCOPE_KNOWN, factor_fingerprint=FP_A, window=WINDOW_1, observed_at=T1),
        _blocked_outcome(scope=SCOPE_KNOWN, factor_fingerprint=FP_A, window=WINDOW_1, observed_at=T2),
        _blocked_outcome(scope=SCOPE_KNOWN, factor_fingerprint=FP_B, window=WINDOW_1, observed_at=T3),
    ]
    observations = [row for outcome in outcomes for row in outcome_to_observations(outcome)]
    run_ids = {observation.run_id for observation in observations}
    windows = {observation.data_window for observation in observations}
    assert len(run_ids) == 2
    assert len(windows) == 1

    decisions = promote(observations)

    assert len(decisions) == 1
    assert decisions[0].kind == "failure"
    assert not any(decision.kind == "rule" for decision in decisions)
    assert decisions[0].observation_count == 2  # units, not rows: A's resim collapses


def test_resim_plus_second_window_cannot_reach_rule_tier() -> None:
    # THE anti-gaming pin (SE-ii / review finding R-F1): factor A re-simulated
    # in window 1 plus factor B in window 2 is only TWO evidence units — the
    # jittered re-sim must not be the third observation that opens the rule
    # gate, no matter how many retries pile up.
    outcomes = [
        _blocked_outcome(scope=SCOPE_KNOWN, factor_fingerprint=FP_A, window=WINDOW_1, observed_at=T1),
        _blocked_outcome(scope=SCOPE_KNOWN, factor_fingerprint=FP_A, window=WINDOW_1, observed_at=T2),
        _blocked_outcome(scope=SCOPE_KNOWN, factor_fingerprint=FP_B, window=WINDOW_2, observed_at=T3),
    ]
    observations = [row for outcome in outcomes for row in outcome_to_observations(outcome)]
    assert len({observation.run_id for observation in observations}) == 2
    assert len({observation.data_window for observation in observations}) == 2

    decisions = promote(observations)

    assert [decision.kind for decision in decisions] == ["failure"]
    assert decisions[0].observation_count == 2


def test_three_evidence_units_over_two_windows_yield_rule_candidate_with_failure_alongside() -> None:
    # THREE genuinely independent studies across two canonical windows open
    # the rule gate, and mirror memory.promote's dual-emit: the failure row
    # must survive alongside the rule candidate.
    outcomes = [
        _blocked_outcome(scope=SCOPE_KNOWN, factor_fingerprint=FP_A, window=WINDOW_1, observed_at=T1),
        _blocked_outcome(scope=SCOPE_KNOWN, factor_fingerprint=FP_B, window=WINDOW_2, observed_at=T2),
        _blocked_outcome(scope=SCOPE_KNOWN, factor_fingerprint=FP_C, window=WINDOW_2, observed_at=T3),
    ]
    observations = [row for outcome in outcomes for row in outcome_to_observations(outcome)]
    assert len({observation.run_id for observation in observations}) == 3
    assert len({observation.data_window for observation in observations}) == 2

    decisions = promote(observations)

    assert sorted(decision.kind for decision in decisions) == ["failure", "rule"]
    by_kind = {decision.kind: decision for decision in decisions}
    assert by_kind["rule"].status == RULE_CANDIDATE_STATUS
    assert by_kind["failure"].status == "active"
    assert by_kind["rule"].observation_count == 3
    assert by_kind["failure"].observation_count == 3


def test_unknown_generalization_dims_never_promote() -> None:
    # R-F4: without factor_family/settings_profile the signature carries a
    # per-evidence-run disambiguator, so unrelated default-scope factors can
    # never unify into a promoted row — they stay at trace tier.
    outcomes = [
        _blocked_outcome(factor_fingerprint=FP_A, window=WINDOW_1, observed_at=T1),
        _blocked_outcome(factor_fingerprint=FP_B, window=WINDOW_1, observed_at=T2),
        _blocked_outcome(factor_fingerprint=FP_C, window=WINDOW_2, observed_at=T3),
    ]
    observations = [row for outcome in outcomes for row in outcome_to_observations(outcome)]
    assert len({observation.signature for observation in observations}) == 3
    assert promote(observations) == ()


# ---------------------------------------------------------------------------
# 4. REJECTION / SMUGGLING CORPUS: each construction must raise ValueError
# ---------------------------------------------------------------------------

_BAD_EVIDENCE_REFS = (
    "/opt/example/artifacts/run.json",
    "~/artifacts/run.json",
    "C:\\artifacts\\run.json",
    "file:///opt/example/artifacts/run.json",
    "../escape/run.json",
)


@pytest.mark.parametrize("bad_ref", _BAD_EVIDENCE_REFS)
def test_research_outcome_rejects_smuggled_evidence_ref(bad_ref: str) -> None:
    with pytest.raises(ValueError):
        _outcome(evidence_ref=bad_ref)


_SCOPE_DIM_FIELDS = ("asset_class", "universe", "factor_family", "horizon_bucket", "settings_profile")


@pytest.mark.parametrize("dim_field", _SCOPE_DIM_FIELDS)
def test_outcome_scope_rejects_uppercase_token_in_every_dim(dim_field: str) -> None:
    with pytest.raises(ValueError):
        OutcomeScope(**{dim_field: "ALPHA_123"})


_BAD_FACTOR_FINGERPRINTS = (
    "0123456789ABCDEF",  # uppercase hex
    "abc123",  # too short (<16)
    "ghijklmnopqrstuv",  # 16 chars but non-hex letters
)


@pytest.mark.parametrize("bad_fingerprint", _BAD_FACTOR_FINGERPRINTS)
def test_research_outcome_rejects_bad_factor_fingerprint(bad_fingerprint: str) -> None:
    with pytest.raises(ValueError):
        _outcome(factor_fingerprint=bad_fingerprint)


_BAD_FACTOR_IDS = (
    "FTR/DEMO",
    "A" * 65,
)


@pytest.mark.parametrize("bad_factor_id", _BAD_FACTOR_IDS)
def test_research_outcome_rejects_bad_factor_id(bad_factor_id: str) -> None:
    with pytest.raises(ValueError):
        _outcome(factor_id=bad_factor_id)


_BAD_METRIC_VALUES = (float("nan"), float("inf"), float("-inf"), True)


@pytest.mark.parametrize("bad_value", _BAD_METRIC_VALUES)
def test_metric_reading_rejects_non_finite_or_bool_value(bad_value: Any) -> None:
    with pytest.raises(ValueError):
        MetricReading(value=bad_value)


@pytest.mark.parametrize("bad_sample_count", [-1, True])
def test_metric_reading_rejects_bad_sample_count(bad_sample_count: Any) -> None:
    with pytest.raises(ValueError):
        MetricReading(sample_count=bad_sample_count)


def test_research_outcome_rejects_unknown_metric_key_fitness() -> None:
    # Provider-vocabulary trap (SE-ii): "fitness" is BRAIN-specific and must
    # never leak into the neutral kernel's closed metric registry.
    with pytest.raises(ValueError, match="fitness"):
        _outcome(metric_snapshot={"fitness": MetricReading(value=1.0)})


def test_research_outcome_rejects_unknown_reason_code() -> None:
    with pytest.raises(ValueError, match="unknown reason codes"):
        _blocked_outcome(reason_codes=("NOT_A_REAL_REASON",))


def test_research_outcome_rejects_duplicate_reasons() -> None:
    with pytest.raises(ValueError, match="not repeat"):
        _blocked_outcome(reason_codes=("TURNOVER_TOO_HIGH", "TURNOVER_TOO_HIGH"))


def test_research_outcome_rejects_unsorted_reasons() -> None:
    with pytest.raises(ValueError, match="must be sorted"):
        _blocked_outcome(reason_codes=("TURNOVER_TOO_HIGH", "REDUNDANCY_HIGH"))


def test_research_outcome_rejects_passed_verdict_with_non_none_reason() -> None:
    with pytest.raises(ValueError, match="exactly the NONE reason"):
        _outcome(verdict="passed", reason_codes=("SHARPE_BELOW_GATE",))


def test_research_outcome_rejects_blocked_verdict_with_none_reason() -> None:
    with pytest.raises(ValueError, match="not NONE"):
        _outcome(verdict="blocked", reason_codes=(REASON_NONE,))


def test_research_outcome_rejects_empty_reason_tuple() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        _outcome(reason_codes=())


def test_research_outcome_rejects_lifecycle_status_off_submit_stage() -> None:
    with pytest.raises(ValueError, match="only representable on the submit stage"):
        _outcome(stage="evaluate", lifecycle_status="submitted")


def test_research_outcome_rejects_unknown_lifecycle_status() -> None:
    with pytest.raises(ValueError, match="lifecycle_status must be one of"):
        _outcome(stage="submit", lifecycle_status="bogus_status")


def test_research_outcome_rejects_naive_timestamp() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        _outcome(observed_at="2026-07-01T00:00:00")


def test_research_outcome_rejects_garbage_timestamp() -> None:
    with pytest.raises(ValueError, match="ISO-8601 timestamp"):
        _outcome(observed_at="not-a-timestamp")


def test_outcome_window_rejects_available_status_without_dates() -> None:
    with pytest.raises(ValueError, match="needs ASCII ISO dates"):
        OutcomeWindow(status="available", start_date="", end_date="")


def test_outcome_window_rejects_unavailable_status_with_dates() -> None:
    with pytest.raises(ValueError, match="must carry empty dates"):
        OutcomeWindow(status="unavailable", start_date="2024-01-01", end_date="")


def test_outcome_window_rejects_end_before_start() -> None:
    with pytest.raises(ValueError, match="precedes start_date"):
        OutcomeWindow(status="available", start_date="2024-06-01", end_date="2024-01-01")


def test_research_outcome_rejects_wrong_schema_version() -> None:
    with pytest.raises(ValueError, match="unsupported schema_version"):
        _outcome(schema_version="qf.research_outcome.v1")


def test_research_outcome_rejects_non_metric_reading_snapshot_value() -> None:
    with pytest.raises(ValueError, match="must be a MetricReading"):
        _outcome(metric_snapshot={"sharpe": 1.5})


def test_metric_reading_rejects_basis_outside_closed_set() -> None:
    with pytest.raises(ValueError, match="metric basis must be one of"):
        MetricReading(basis="adjusted")


# ---------------------------------------------------------------------------
# 5. MULTI-REASON FAN-OUT: one blocked outcome -> N observations, one per reason
# ---------------------------------------------------------------------------


def test_multi_reason_fanout_shares_run_id_window_scope_but_distinct_signatures() -> None:
    outcome = _blocked_outcome(reason_codes=("REDUNDANCY_HIGH", "TURNOVER_TOO_HIGH"), window=WINDOW_1)
    observations = outcome_to_observations(outcome)

    assert len(observations) == 2
    assert observations[0].signature != observations[1].signature
    assert observations[0].run_id == observations[1].run_id == outcome.evidence_run_id()
    assert observations[0].data_window == observations[1].data_window == outcome.window.canonical()
    assert observations[0].scope == observations[1].scope == outcome.scope.scope_key()
    assert {observation.failure_class for observation in observations} == {"gate_blocked"}


def test_multi_reason_fanout_validation_reasons_map_to_validation_error() -> None:
    outcome = _blocked_outcome(reason_codes=("EXECUTION_ERROR", "VALIDATION_ERROR"))
    observations = outcome_to_observations(outcome)

    assert len(observations) == 2
    assert {observation.failure_class for observation in observations} == {"validation_error"}


def test_multi_reason_fanout_mixed_reasons_map_per_reason_not_per_outcome() -> None:
    outcome = _blocked_outcome(reason_codes=("EXECUTION_ERROR", "REDUNDANCY_HIGH"))
    observations = outcome_to_observations(outcome)

    assert len(observations) == 2
    assert [observation.failure_class for observation in observations] == ["validation_error", "gate_blocked"]


def test_passed_outcome_maps_to_empty_failure_class() -> None:
    outcome = _outcome(verdict="passed", reason_codes=(REASON_NONE,))
    observations = outcome_to_observations(outcome)

    assert len(observations) == 1
    assert observations[0].failure_class == ""


# ---------------------------------------------------------------------------
# 6. STATEMENT INVARIANCE
# ---------------------------------------------------------------------------


def test_statement_identical_across_metric_and_timestamp_only_variations() -> None:
    a = _outcome(observed_at=T1, metric_snapshot={"sharpe": MetricReading(value=1.0)})
    b = _outcome(observed_at=T2, metric_snapshot={"sharpe": MetricReading(value=-2.5)})

    statement_a = outcome_to_observations(a)[0].statement
    statement_b = outcome_to_observations(b)[0].statement

    assert statement_a == statement_b


def test_statement_is_redaction_invariant() -> None:
    outcome = _outcome(scope=OutcomeScope(asset_class="us_equity", factor_family="momentum"))
    statement = outcome_to_observations(outcome)[0].statement

    assert redact_free_text(statement) == statement


def test_statement_contains_derived_strength_token() -> None:
    outcome = _outcome(stage="simulate", verdict="passed", reason_codes=(REASON_NONE,))
    statement = outcome_to_observations(outcome)[0].statement

    assert outcome.evidence_strength == "platform_simulated"
    assert f"strength={outcome.evidence_strength}" in statement


# ---------------------------------------------------------------------------
# 7. STRENGTH DERIVATION
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stage", STAGES)
def test_evidence_strength_matches_stage_table(stage: str) -> None:
    lifecycle = "submitted" if stage == "submit" else ""
    outcome = _outcome(stage=stage, verdict="passed", reason_codes=(REASON_NONE,), lifecycle_status=lifecycle)
    assert outcome.evidence_strength == STAGE_EVIDENCE_STRENGTH[stage]


def test_evidence_strength_rank_strictly_increasing() -> None:
    ranks = [EVIDENCE_STRENGTH_RANK[name] for name in EVIDENCE_STRENGTHS]
    assert len(set(ranks)) == len(ranks)
    assert all(earlier < later for earlier, later in zip(ranks, ranks[1:]))


def test_evidence_strength_is_read_only_property_not_a_field() -> None:
    assert "evidence_strength" not in ResearchOutcome.__dataclass_fields__
    assert isinstance(vars(ResearchOutcome)["evidence_strength"], property)
    outcome = _outcome()
    with pytest.raises(dataclasses.FrozenInstanceError):
        outcome.evidence_strength = "submitted_live"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 8. DETERMINISM
# ---------------------------------------------------------------------------


def test_to_dict_metric_keys_are_sorted() -> None:
    outcome = _outcome(
        metric_snapshot={
            "turnover": MetricReading(value=0.3),
            "sharpe": MetricReading(value=1.2),
            "max_drawdown": MetricReading(value=0.1),
        }
    )
    keys = list(outcome.to_dict()["metric_snapshot"].keys())
    assert keys == sorted(keys)
    assert keys == ["max_drawdown", "sharpe", "turnover"]


def test_canonical_fingerprint_stable_across_shuffled_metric_insertion_order() -> None:
    readings = {
        "turnover": MetricReading(value=0.3),
        "sharpe": MetricReading(value=1.2),
        "max_drawdown": MetricReading(value=0.1),
    }
    ordered_forward = {key: readings[key] for key in ("turnover", "sharpe", "max_drawdown")}
    ordered_reverse = {key: readings[key] for key in ("max_drawdown", "sharpe", "turnover")}

    a = _outcome(metric_snapshot=ordered_forward)
    b = _outcome(metric_snapshot=ordered_reverse)

    assert canonical_fingerprint(a.to_dict()) == canonical_fingerprint(b.to_dict())
    assert a.to_dict() == b.to_dict()


# ---------------------------------------------------------------------------
# 9. NEUTRALITY: no provider/network imports, at runtime or in source text
# ---------------------------------------------------------------------------


def test_outcomes_module_avoids_importing_integrations_at_runtime() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    src_path = repo_root / "src"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(src_path)
    script = (
        "import quant_forge.research_loop.outcomes, sys; "
        "assert not any(k.startswith('quant_forge.integrations') for k in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(repo_root),
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


_FORBIDDEN_IMPORT_ROOTS = ("quant_forge.integrations", "requests", "urllib", "http", "socket")


def test_outcomes_module_source_has_no_forbidden_imports() -> None:
    source_path = Path(__file__).resolve().parents[1] / "src" / "quant_forge" / "research_loop" / "outcomes.py"
    text = source_path.read_text(encoding="utf-8")
    for line_number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        match = re.match(r"^(?:from|import)\s+([A-Za-z_][A-Za-z0-9_.]*)", stripped)
        if not match:
            continue
        module = match.group(1)
        for forbidden in _FORBIDDEN_IMPORT_ROOTS:
            assert module != forbidden and not module.startswith(f"{forbidden}."), (
                f"line {line_number} imports forbidden module {forbidden!r}: {stripped!r}"
            )


# ---------------------------------------------------------------------------
# SE-P1 EXTENSION BATTERY (adjudicated spec, items 1-12 below). Each section
# banner cites the originating item number so the coverage map in the task
# report can point straight at these tests.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 10. GOLDEN FROZEN-VECTOR PINS (item 1): hard-coded literals freeze
# cross-version identity for persisted memory. Unlike the relative
# invariant/differs tests in section 1 above (which only ever compare two
# freshly-computed hashes to each other), these compare the REAL output to a
# hex literal computed once and hard-coded here, so a silent change to field
# ordering, canonicalization, or a version constant fails loudly.
# ---------------------------------------------------------------------------

_GOLDEN_FINGERPRINT = "0123456789abcdef"
_GOLDEN_OBSERVED_AT = "2026-01-01T00:00:00+00:00"
_GOLDEN_SCOPE = OutcomeScope(
    asset_class="us_equity",
    universe="sp500",
    factor_family="reversal",
    horizon_bucket="d5",
    settings_profile="default",
)
_GOLDEN_WINDOW = OutcomeWindow(status="available", start_date="2024-01-01", end_date="2024-06-30")

# Computed once (PYTHONPATH=src python3, this exact fixture) and pinned as a
# literal. Do NOT "fix" a failing golden test by recomputing and pasting a new
# value here without understanding why identity moved -- that is precisely
# the silent-drift failure mode this test exists to catch.
_GOLDEN_OUTCOME_ID = "05fc36e757f9d9ce1c3a748f3d5acc863ff30c325a49945e172b5fb8f0d65be6"
_GOLDEN_EVIDENCE_RUN_ID = "721d791a4a35f0a6f9b288691652184c8a74f9c3620f3fc23637bb9a29600469"
_GOLDEN_FIRST_SIGNATURE = "6b901e8690a25c5091672d195ccb80b0b56898853e659cb074471e6e3c9b3fc0"


def _golden_outcome() -> ResearchOutcome:
    """Fully-specified fixture pinned by the golden-vector tests below."""

    return ResearchOutcome(
        origin="local",
        stage="evaluate",
        verdict="blocked",
        factor_id="FTR_GOLDEN",
        factor_fingerprint=_GOLDEN_FINGERPRINT,
        observed_at=_GOLDEN_OBSERVED_AT,
        reason_codes=("SHARPE_BELOW_GATE", "TURNOVER_TOO_HIGH"),
        sample_role="out_of_sample",
        window=_GOLDEN_WINDOW,
        scope=_GOLDEN_SCOPE,
        metric_snapshot={
            "sharpe": MetricReading(value=0.42, basis="net", sample_count=252),
            "turnover": MetricReading(value=0.55),
        },
        evidence_ref="runs/n2/prescreen.json",
    )


def test_golden_schema_and_signature_contract_version_literals_are_pinned() -> None:
    assert RESEARCH_OUTCOME_SCHEMA_VERSION == "qf.research_outcome.v2"
    assert SIGNATURE_CONTRACT_VERSION == "sig.v2.1"


def test_golden_outcome_id_matches_frozen_hex_vector() -> None:
    assert _golden_outcome().outcome_id() == _GOLDEN_OUTCOME_ID


def test_golden_evidence_run_id_matches_frozen_hex_vector() -> None:
    assert _golden_outcome().evidence_run_id() == _GOLDEN_EVIDENCE_RUN_ID


def test_golden_first_signature_matches_frozen_hex_vector() -> None:
    assert _sig(_golden_outcome(), 0) == _GOLDEN_FIRST_SIGNATURE


# ---------------------------------------------------------------------------
# 11. HEX BOUNDARIES (item 2): factor_fingerprint's ``_HEX_RE`` is
# ``^[0-9a-f]{16,64}$``. Length is isolated from character-class validity
# (already covered by ``_BAD_FACTOR_FINGERPRINTS`` above) by holding every
# character fixed at ``"a"`` and varying only the string length.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("length", [16, 64])
def test_research_outcome_accepts_factor_fingerprint_at_hex_length_boundaries(length: int) -> None:
    fingerprint = "a" * length
    outcome = _outcome(factor_fingerprint=fingerprint)
    assert outcome.factor_fingerprint == fingerprint


@pytest.mark.parametrize("length", [15, 65, 12])
def test_research_outcome_rejects_factor_fingerprint_outside_hex_length_boundaries(length: int) -> None:
    # 15/65 probe the boundary edges; 12 is the opus probe M short-fingerprint case.
    with pytest.raises(ValueError):
        _outcome(factor_fingerprint="a" * length)


# ---------------------------------------------------------------------------
# 12. DIM BOUNDARIES (item 3): ``OutcomeScope``'s ``_DIM_RE`` is
# ``^[a-z0-9_.\-]{0,32}$``; ``factor_id``'s ``_ID_RE`` is
# ``^[A-Za-z0-9_.\-]{1,64}$``. The 65-char factor_id rejection already exists
# in ``_BAD_FACTOR_IDS`` (``"A" * 65``) above; only the missing accepted
# boundary is added here.
# ---------------------------------------------------------------------------


def test_outcome_scope_accepts_32_char_dim() -> None:
    value = "a" * 32
    scope = OutcomeScope(asset_class=value)
    assert scope.asset_class == value


def test_outcome_scope_rejects_33_char_dim() -> None:
    with pytest.raises(ValueError):
        OutcomeScope(asset_class="a" * 33)


def test_research_outcome_accepts_64_char_factor_id() -> None:
    factor_id = "A" * 64
    outcome = _outcome(factor_id=factor_id)
    assert outcome.factor_id == factor_id


# ---------------------------------------------------------------------------
# 13. WINDOW (item 4): equal start==end, leap-day handling, and real calendar
# validation (R-F7) -- ``date.fromisoformat`` rejects the invalid dates
# rather than the module minting a fake distinct window for them.
# ---------------------------------------------------------------------------


def test_outcome_window_accepts_equal_start_and_end_date() -> None:
    window = OutcomeWindow(status="available", start_date="2024-03-15", end_date="2024-03-15")
    assert window.canonical() == "2024-03-15:2024-03-15"


def test_outcome_window_accepts_leap_day_2024_02_29() -> None:
    window = OutcomeWindow(status="available", start_date="2024-02-29", end_date="2024-02-29")
    assert window.canonical() == "2024-02-29:2024-02-29"


def test_outcome_window_rejects_non_leap_year_february_29() -> None:
    with pytest.raises(ValueError, match="not a real calendar date"):
        OutcomeWindow(status="available", start_date="2023-02-29", end_date="2023-02-29")


def test_outcome_window_rejects_february_31() -> None:
    with pytest.raises(ValueError, match="not a real calendar date"):
        OutcomeWindow(status="available", start_date="2024-02-31", end_date="2024-02-31")


def test_outcome_window_rejects_month_99_day_99() -> None:
    with pytest.raises(ValueError, match="not a real calendar date"):
        OutcomeWindow(status="available", start_date="2024-99-99", end_date="2024-99-99")


def test_outcome_window_rejects_arabic_indic_digit_date() -> None:
    # Arabic-Indic digits (U+0660-U+0669) look like digits to a human eye but
    # do not match the ASCII-only ``[0-9]`` character class in ``_DATE_RE``;
    # this renders as "2024-01-01" but must be rejected, not silently coerced.
    arabic_2024_01_01 = "٢٠٢٤-٠١-٠١"
    with pytest.raises(ValueError, match="needs ASCII ISO dates"):
        OutcomeWindow(status="available", start_date=arabic_2024_01_01, end_date="2024-01-01")


# ---------------------------------------------------------------------------
# 14. MUTATION REGRESSIONS (item 5, R-F5/R-F6): frozen=True is shallow, so
# the constructor must defensively copy caller-held mutables, and every
# read-only registry must reject item assignment.
# ---------------------------------------------------------------------------


def test_metric_snapshot_mutation_after_construction_does_not_affect_identity() -> None:
    source: dict[str, MetricReading] = {"sharpe": MetricReading(value=1.0)}
    outcome = _outcome(metric_snapshot=source)
    original_outcome_id = outcome.outcome_id()
    original_to_dict = outcome.to_dict()

    source["turnover"] = MetricReading(value=0.9)  # forbidden: mutate the caller's dict post-construction

    assert outcome.outcome_id() == original_outcome_id
    assert outcome.to_dict() == original_to_dict
    assert "turnover" not in outcome.to_dict()["metric_snapshot"]


def test_metric_snapshot_item_assignment_raises_type_error() -> None:
    outcome = _outcome(metric_snapshot={"sharpe": MetricReading(value=1.0)})
    with pytest.raises(TypeError):
        outcome.metric_snapshot["sharpe"] = MetricReading(value=99.0)  # type: ignore[index]


def test_reason_codes_list_input_is_stored_as_tuple_and_detached_from_source() -> None:
    source: list[str] = ["TURNOVER_TOO_HIGH"]
    outcome = _blocked_outcome(reason_codes=source)
    assert isinstance(outcome.reason_codes, tuple)
    assert outcome.reason_codes == ("TURNOVER_TOO_HIGH",)

    source.append("REDUNDANCY_HIGH")  # mutate the caller's list after construction

    assert outcome.reason_codes == ("TURNOVER_TOO_HIGH",)


def test_stage_evidence_strength_item_assignment_raises_type_error() -> None:
    with pytest.raises(TypeError):
        STAGE_EVIDENCE_STRENGTH["evaluate"] = "submitted_live"  # type: ignore[index]


def test_evidence_strength_rank_item_assignment_raises_type_error() -> None:
    with pytest.raises(TypeError):
        EVIDENCE_STRENGTH_RANK["prescreen"] = 99  # type: ignore[index]


def test_metric_specs_item_assignment_raises_type_error() -> None:
    with pytest.raises(TypeError):
        METRIC_SPECS["sharpe"] = ("ratio", "tampered")  # type: ignore[index]


# ---------------------------------------------------------------------------
# 15. LIFECYCLE/VERDICT ELIGIBILITY (item 6, R-F2): lifecycle bookkeeping and
# unknown/not_applicable verdicts carry no scientific answer and must mint
# zero observations; submit-stage bidirectional coherence (opus F3) is
# enforced both ways.
# ---------------------------------------------------------------------------


def test_submit_stage_unknown_verdict_with_submitted_lifecycle_yields_no_observations() -> None:
    outcome = _outcome(
        stage="submit",
        verdict="unknown",
        reason_codes=(REASON_NONE,),
        lifecycle_status="submitted",
    )
    assert outcome_to_observations(outcome) == ()


def test_not_applicable_verdict_yields_no_observations() -> None:
    outcome = _outcome(stage="evaluate", verdict="not_applicable", reason_codes=(REASON_NONE,))
    assert outcome_to_observations(outcome) == ()


def test_submit_stage_passed_accepted_yields_one_observation_with_submitted_live_strength() -> None:
    outcome = _outcome(
        stage="submit",
        verdict="passed",
        reason_codes=(REASON_NONE,),
        lifecycle_status="accepted",
    )
    observations = outcome_to_observations(outcome)
    assert len(observations) == 1
    assert outcome.evidence_strength == "submitted_live"
    assert outcome.to_record()["evidence_strength"] == "submitted_live"


def test_submit_stage_with_empty_lifecycle_status_raises() -> None:
    with pytest.raises(ValueError, match="must carry a lifecycle_status"):
        _outcome(stage="submit", verdict="passed", reason_codes=(REASON_NONE,), lifecycle_status="")


_NON_SUBMIT_STAGES = tuple(stage for stage in STAGES if stage != "submit")


@pytest.mark.parametrize("stage", _NON_SUBMIT_STAGES)
def test_non_submit_stage_rejects_any_non_empty_lifecycle_status(stage: str) -> None:
    with pytest.raises(ValueError, match="only representable on the submit stage"):
        _outcome(stage=stage, verdict="passed", reason_codes=(REASON_NONE,), lifecycle_status="submitted")


# ---------------------------------------------------------------------------
# 16. SENTINELS (item 7, R-F4): "unknown"/"global" are the signature's own
# absent-dimension renderings, so accepting them as VALUES would let
# unrelated factors unify into one signature. Checked across all five scope
# dimensions x both reserved sentinels (10 cases).
# ---------------------------------------------------------------------------

_RESERVED_SCOPE_SENTINELS = ("unknown", "global")


@pytest.mark.parametrize("dim_field", _SCOPE_DIM_FIELDS)
@pytest.mark.parametrize("sentinel", _RESERVED_SCOPE_SENTINELS)
def test_outcome_scope_rejects_reserved_sentinel_in_every_dim(sentinel: str, dim_field: str) -> None:
    with pytest.raises(ValueError, match="reserved sentinel"):
        OutcomeScope(**{dim_field: sentinel})


# ---------------------------------------------------------------------------
# 17. REF ALLOWLIST CORPUS (item 8, R-F8/opus F4).
#
# DEVIATION FROM THE LITERAL ADJUDICATED SPEC TEXT: the spec's rejected-corpus
# list included a fragment shaped like "Users" + slash + a real local account
# name + "/artifacts/run.json". That exact literal is not used here. Two
# independent rules in the same task forbid it: this task's own gate
# instruction (no leading-slash-Users-style or private-path-shaped strings in
# test data -- use "/opt/example" or Users-less fragments per the existing
# corpus convention) and AGENTS.md's Coding Rules ("Never commit ... local
# absolute paths"). Substituted with "opt/example/artifacts/run.json"
# (Users-less, per the gate's own suggested style), which exercises the
# IDENTICAL mechanism: prefixing a leading slash still resolves to an
# allowlisted root name ("opt", alongside "Users" in ``_POSIX_PATH_RE``)
# followed by >=1 more "/segment" groups, so
# ``redact_free_text("/" + value) != "/" + value`` fires the same
# slash-stripped-absolute-path probe the finding is about. Flagged in the
# task report per the escalation clause.
# ---------------------------------------------------------------------------

_REF_ALLOWLIST_REJECTED = (
    "opt/example/artifacts/run.json",  # slash-stripped absolute-path shape (see deviation note above)
    "home/user/x",  # slash-stripped absolute-path shape, second allowlisted root name
    "runs/n2/pre​screen.json",  # zero-width space (U+200B) embedded mid-segment
    "$HOME/x",  # banned char '$'
    "%2e%2e/x",  # banned char '%'
    "a b",  # space is outside the printable-ASCII wall
    "a//b",  # double slash -> empty segment
    ".hidden/x",  # dot-segment: first char must be alnum/underscore
    "a/./b",  # "." segment, same rule as above
    "a/b/",  # trailing slash -> empty final segment
    "/a/b",  # leading slash -> empty first segment
    "a\\b",  # banned char '\\'
    "C:/x",  # banned char ':'
    "a" * 201,  # exceeds the 200-char ceiling
)


@pytest.mark.parametrize("bad_ref", _REF_ALLOWLIST_REJECTED)
def test_research_outcome_rejects_ref_allowlist_corpus(bad_ref: str) -> None:
    with pytest.raises(ValueError):
        _outcome(evidence_ref=bad_ref)


_REF_ALLOWLIST_ACCEPTED = (
    "runs/n2/prescreen.json",
    "run_0001",
    "a" * 64,  # single segment exactly at the per-segment length ceiling
)


@pytest.mark.parametrize("good_ref", _REF_ALLOWLIST_ACCEPTED)
def test_research_outcome_accepts_ref_allowlist_corpus(good_ref: str) -> None:
    outcome = _outcome(evidence_ref=good_ref)
    assert outcome.evidence_ref == good_ref


# ---------------------------------------------------------------------------
# 18. TO_RECORD ENVELOPE (item 9, R-F3): the persisted ledger envelope, not
# bare ``to_dict()``, is what SE-P2 ingress appends per outcome.
# ---------------------------------------------------------------------------


def test_to_record_envelope_has_exact_keys_and_pinned_schema_literal() -> None:
    outcome = _blocked_outcome(reason_codes=("REDUNDANCY_HIGH", "TURNOVER_TOO_HIGH"))
    record = outcome.to_record()

    assert record["record_schema"] == "qf.research_outcome_record.v1"
    assert set(record.keys()) == {
        "record_schema",
        "outcome_id",
        "evidence_run_id",
        "evidence_strength",
        "signatures",
        "outcome",
    }
    assert len(record["signatures"]) == len(outcome.reason_codes)
    assert "sample_role" in record["outcome"]


def test_research_outcome_rejects_invalid_sample_role() -> None:
    with pytest.raises(ValueError, match="sample_role must be one of"):
        _outcome(sample_role="oos_garbage")


@pytest.mark.parametrize("sample_role", SAMPLE_ROLES)
def test_research_outcome_accepts_every_sample_role(sample_role: str) -> None:
    outcome = _outcome(sample_role=sample_role)
    assert outcome.sample_role == sample_role


# ---------------------------------------------------------------------------
# 19. FULL-KEY SNAPSHOT (item 10): every registered metric key at once, and
# ``to_dict()`` output order is independent of construction/insertion order.
# ---------------------------------------------------------------------------


def test_outcome_accepts_full_metric_snapshot_with_every_registered_key() -> None:
    snapshot = {key: MetricReading(value=0.1) for key in METRIC_SPECS}
    outcome = _outcome(metric_snapshot=snapshot)
    assert set(outcome.metric_snapshot.keys()) == set(METRIC_SPECS.keys())


def test_full_metric_snapshot_to_dict_keys_emerge_sorted_regardless_of_shuffled_insertion() -> None:
    keys = list(METRIC_SPECS.keys())
    shuffled_keys = list(keys)
    random.Random(20260713).shuffle(shuffled_keys)
    assert shuffled_keys != keys  # sanity: the shuffle actually reordered insertion

    snapshot = {key: MetricReading(value=0.2) for key in shuffled_keys}
    outcome = _outcome(metric_snapshot=snapshot)

    assert list(outcome.to_dict()["metric_snapshot"].keys()) == sorted(keys)


# ---------------------------------------------------------------------------
# 20. PROMOTE() DEDUP DIRECT (item 11, R-F1): the SE-ii evidence-unit cap
# pinned directly against raw ``MemoryObservation`` rows, independent of the
# outcomes.py mapper -- this is memory.py's own contract, not a derived one.
# ---------------------------------------------------------------------------


def test_promote_dedups_same_signature_same_run_id_to_one_kept_row_with_earliest_first_seen() -> None:
    signature = "sig_promote_dedup_direct"
    rows = [
        MemoryObservation(
            signature=signature,
            statement="s",
            run_id="run_a",
            observed_at=T2,
            evidence_ref="ev_a_t2",
            failure_class="gate_blocked",
        ),
        MemoryObservation(
            signature=signature,
            statement="s",
            run_id="run_a",
            observed_at=T1,  # earliest of the three run_a rows
            evidence_ref="ev_a_t1",
            failure_class="gate_blocked",
        ),
        MemoryObservation(
            signature=signature,
            statement="s",
            run_id="run_a",
            observed_at=T3,
            evidence_ref="ev_a_t3",
            failure_class="gate_blocked",
        ),
        MemoryObservation(
            signature=signature,
            statement="s",
            run_id="run_b",
            observed_at=T4,
            evidence_ref="ev_b_t4",
            failure_class="gate_blocked",
        ),
    ]

    decisions = promote(rows)

    assert len(decisions) == 1
    decision = decisions[0]
    assert decision.kind == "failure"
    assert decision.status == "active"
    # Three run_a rows collapse to ONE evidence unit; only run_a + run_b remain.
    assert decision.observation_count == 2
    # First-kept determinism: the EARLIEST observed_at among the duplicate
    # run_a rows (T1, not the list-literal-first T2) is the one retained.
    assert decision.first_seen == T1
    assert "ev_a_t1" in decision.evidence_refs
    assert "ev_a_t2" not in decision.evidence_refs
    assert "ev_a_t3" not in decision.evidence_refs


# ---------------------------------------------------------------------------
# 21. ALL-REASONS (item 12): a blocked outcome carrying every non-NONE reason
# code in canonical sorted order fans out to one distinct-signature
# observation per reason.
# ---------------------------------------------------------------------------


def test_blocked_outcome_with_every_non_none_reason_code_yields_one_observation_per_reason() -> None:
    reasons = tuple(sorted(REASON_CODES - {REASON_NONE}))
    outcome = _blocked_outcome(reason_codes=reasons)

    observations = outcome_to_observations(outcome)

    assert len(observations) == len(reasons)
    signatures = {observation.signature for observation in observations}
    assert len(signatures) == len(reasons)

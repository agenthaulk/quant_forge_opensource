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
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from quant_forge.lineage.store import canonical_fingerprint, redact_free_text
from quant_forge.research_loop.memory import RULE_CANDIDATE_STATUS, promote
from quant_forge.research_loop.outcomes import (
    EVIDENCE_STRENGTH_RANK,
    EVIDENCE_STRENGTHS,
    REASON_NONE,
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
        _blocked_outcome(window=WINDOW_1, observed_at=T1, metric_snapshot={"turnover": MetricReading(value=0.65)}),
        _blocked_outcome(window=WINDOW_1, observed_at=T2, metric_snapshot={"turnover": MetricReading(value=0.66)}),
        _blocked_outcome(window=WINDOW_1, observed_at=T3, metric_snapshot={"turnover": MetricReading(value=0.64)}),
    ]
    observations = [row for outcome in outcomes for row in outcome_to_observations(outcome)]

    assert len(observations) == 3
    run_ids = {observation.run_id for observation in observations}
    assert run_ids == {outcomes[0].evidence_run_id()}
    assert promote(observations) == ()


def test_second_factor_hitting_same_reason_yields_exactly_one_failure_row() -> None:
    outcomes = [
        _blocked_outcome(window=WINDOW_1, observed_at=T1, metric_snapshot={"turnover": MetricReading(value=0.65)}),
        _blocked_outcome(window=WINDOW_1, observed_at=T2, metric_snapshot={"turnover": MetricReading(value=0.66)}),
        _blocked_outcome(window=WINDOW_1, observed_at=T3, metric_snapshot={"turnover": MetricReading(value=0.64)}),
        _blocked_outcome(
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
    assert decision.observation_count == 4


def test_passed_corpus_two_factors_yields_finding_not_failure() -> None:
    outcomes = [
        _outcome(factor_id="FTR_ONE", factor_fingerprint=FP_A, observed_at=T1),
        _outcome(factor_id="FTR_TWO", factor_fingerprint=FP_B, observed_at=T2),
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
        _blocked_outcome(factor_fingerprint=FP_A, window=WINDOW_1, observed_at=T1),
        _blocked_outcome(factor_fingerprint=FP_A, window=WINDOW_1, observed_at=T2),
        _blocked_outcome(factor_fingerprint=FP_B, window=WINDOW_1, observed_at=T3),
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
    assert decisions[0].observation_count == 3


def test_two_distinct_windows_yields_rule_candidate_with_failure_alongside() -> None:
    # Same shape, but factor B's evidence lands in a second canonical window
    # -> the rule gate opens, and mirrors memory.promote's dual-emit: the
    # failure row must survive alongside the rule candidate.
    outcomes = [
        _blocked_outcome(factor_fingerprint=FP_A, window=WINDOW_1, observed_at=T1),
        _blocked_outcome(factor_fingerprint=FP_A, window=WINDOW_1, observed_at=T2),
        _blocked_outcome(factor_fingerprint=FP_B, window=WINDOW_2, observed_at=T3),
    ]
    observations = [row for outcome in outcomes for row in outcome_to_observations(outcome)]
    run_ids = {observation.run_id for observation in observations}
    windows = {observation.data_window for observation in observations}
    assert len(run_ids) == 2
    assert len(windows) == 2

    decisions = promote(observations)

    assert sorted(decision.kind for decision in decisions) == ["failure", "rule"]
    by_kind = {decision.kind: decision for decision in decisions}
    assert by_kind["rule"].status == RULE_CANDIDATE_STATUS
    assert by_kind["failure"].status == "active"
    assert by_kind["rule"].observation_count == 3
    assert by_kind["failure"].observation_count == 3


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
    with pytest.raises(ValueError, match="needs ISO dates"):
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
    outcome = _outcome(stage=stage, verdict="passed", reason_codes=(REASON_NONE,))
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

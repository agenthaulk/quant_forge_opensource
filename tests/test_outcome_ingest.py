"""SE-P2 regression tests: the shared ResearchOutcome ingress sink.

Covers ``src/quant_forge/research_loop/outcome_ingest.py`` (and, through it,
the ADDITIVE outcome-ledger methods on ``ResearchMemoryStore``) against
DECISIONS.md "2026-07-13 -- Self-evolution engine CP0", rulings
SE-i/SE-ii/SE-ix: envelope written with exact replay-drop by ``outcome_id``;
observations recorded and promotion runs; ``as_of`` monotonic across
distinct outcomes and stable across an exact replay.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from quant_forge.research_loop.memory import ResearchMemoryStore
from quant_forge.research_loop.outcome_ingest import ingest_outcome
from quant_forge.research_loop.outcomes import (
    REASON_NONE,
    OutcomeScope,
    ResearchOutcome,
)

T1 = "2026-07-01T00:00:00+00:00"
T2 = "2026-07-02T00:00:00+00:00"

FP_A = "a1b2c3d4e5f6a1b2"
FP_B = "1a2b3c4d5e6f1a2b"

# Non-empty family/settings so distinct evidence runs can unify at the
# signature level (see local_outcomes.py's module docstring "scope" section
# for why an empty family/settings would make promotion structurally
# unreachable -- outcomes.OutcomeScope.signature_payloads()'s R-F4
# disambiguator).
SCOPE = OutcomeScope(asset_class="us_equity", universe="sp500", factor_family="fam", settings_profile="prof")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _outcome(**overrides: Any) -> ResearchOutcome:
    kwargs: dict[str, Any] = dict(
        origin="local",
        stage="gate",
        verdict="passed",
        factor_id="FTR_A",
        factor_fingerprint=FP_A,
        observed_at=T1,
        reason_codes=(REASON_NONE,),
        scope=SCOPE,
    )
    kwargs.update(overrides)
    return ResearchOutcome(**kwargs)


def _store(tmp_path: Path, name: str = "artifact_root") -> ResearchMemoryStore:
    return ResearchMemoryStore(tmp_path / name)


# ---------------------------------------------------------------------------
# envelope: written, exact replay-drop
# ---------------------------------------------------------------------------


def test_ingest_writes_one_envelope_to_outcomes_ledger(tmp_path: Path) -> None:
    store = _store(tmp_path)
    outcome = _outcome()
    receipt = ingest_outcome(store, outcome)

    assert receipt.recorded is True
    assert receipt.outcome_id == outcome.outcome_id()
    rows = _read_jsonl(store.outcomes_ledger_path)
    assert len(rows) == 1
    assert rows[0]["outcome_id"] == outcome.outcome_id()
    assert rows[0]["record_schema"] == "qf.research_outcome_record.v1"
    assert rows[0]["evidence_run_id"] == outcome.evidence_run_id()
    assert rows[0]["outcome"]["factor_id"] == "FTR_A"


def test_ingest_exact_replay_drops_envelope_second_time(tmp_path: Path) -> None:
    store = _store(tmp_path)
    outcome = _outcome()
    first = ingest_outcome(store, outcome)
    second = ingest_outcome(store, outcome)

    assert first.recorded is True
    assert second.recorded is False
    assert first.outcome_id == second.outcome_id
    rows = _read_jsonl(store.outcomes_ledger_path)
    assert len(rows) == 1  # not appended twice


def test_ingest_replay_with_different_observed_at_still_drops(tmp_path: Path) -> None:
    # outcome_id() excludes observed_at (an administrative resend of the
    # exact same measurement keeps the same id -- outcomes.py
    # ResearchOutcome.outcome_id() docstring), so a resend with a genuinely
    # different observed_at is STILL treated as the same logical outcome.
    store = _store(tmp_path)
    first = ingest_outcome(store, _outcome(observed_at=T1))
    second = ingest_outcome(store, _outcome(observed_at=T2))

    assert first.outcome_id == second.outcome_id
    assert first.recorded is True
    assert second.recorded is False
    assert len(_read_jsonl(store.outcomes_ledger_path)) == 1


def test_ingest_genuinely_different_outcome_is_recorded_separately(tmp_path: Path) -> None:
    store = _store(tmp_path)
    a = ingest_outcome(store, _outcome(factor_id="FTR_A"))
    b = ingest_outcome(store, _outcome(factor_id="FTR_B"))

    assert a.recorded is True
    assert b.recorded is True
    assert a.outcome_id != b.outcome_id
    assert len(_read_jsonl(store.outcomes_ledger_path)) == 2


# ---------------------------------------------------------------------------
# observations + promotion
# ---------------------------------------------------------------------------


def test_ingest_records_one_observation_per_reason_code(tmp_path: Path) -> None:
    store = _store(tmp_path)
    outcome = _outcome(verdict="blocked", reason_codes=("SHARPE_BELOW_GATE", "TURNOVER_TOO_HIGH"))
    receipt = ingest_outcome(store, outcome)

    assert receipt.observation_count == 2
    observations = _read_jsonl(store.observations_path)
    assert len(observations) == 2
    signatures = {row["signature"] for row in observations}
    assert len(signatures) == 2  # one per reason code, distinct signatures


def test_ingest_unknown_verdict_records_envelope_but_zero_observations(tmp_path: Path) -> None:
    # Lifecycle-only submit bookkeeping (verdict="unknown") mints ZERO
    # observations by design (outcomes.outcome_to_observations) -- it is
    # ledger-only. Not reachable for the local "gate" stage producer, but
    # the shared sink must still handle it correctly for a future submit-
    # stage producer.
    store = _store(tmp_path)
    outcome = _outcome(
        stage="submit",
        verdict="unknown",
        reason_codes=(REASON_NONE,),
        lifecycle_status="submitted",
    )
    receipt = ingest_outcome(store, outcome)

    assert receipt.recorded is True
    assert receipt.observation_count == 0
    assert _read_jsonl(store.observations_path) == []
    assert len(_read_jsonl(store.outcomes_ledger_path)) == 1


def test_ingest_two_distinct_outcomes_same_signature_promotes_finding(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ingest_outcome(store, _outcome(factor_id="FTR_A", factor_fingerprint=FP_A))
    assert _read_jsonl(store.path_for("finding")) == []  # trace-only after one evidence run

    ingest_outcome(store, _outcome(factor_id="FTR_B", factor_fingerprint=FP_B))

    findings = _read_jsonl(store.path_for("finding"))
    assert findings
    assert findings[-1]["status"] == "active"
    assert findings[-1]["observation_count"] >= 2


def test_ingest_two_distinct_blocked_outcomes_same_signature_promotes_failure(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ingest_outcome(
        store, _outcome(factor_id="FTR_A", factor_fingerprint=FP_A, verdict="blocked", reason_codes=("SHARPE_BELOW_GATE",))
    )
    ingest_outcome(
        store, _outcome(factor_id="FTR_B", factor_fingerprint=FP_B, verdict="blocked", reason_codes=("SHARPE_BELOW_GATE",))
    )

    failures = _read_jsonl(store.path_for("failure"))
    assert failures
    assert failures[-1]["status"] == "active"


def test_ingest_replay_does_not_double_count_toward_promotion(tmp_path: Path) -> None:
    # A single evidence run replayed any number of times is still ONE
    # evidence run: memory.promote's evidence-unit cap (<=1 observation per
    # (signature, run_id) reaches the thresholds) absorbs the duplicate, so
    # replaying twice must not fake a second independent confirmation.
    store = _store(tmp_path)
    outcome = _outcome(factor_id="FTR_A", factor_fingerprint=FP_A)
    ingest_outcome(store, outcome)
    ingest_outcome(store, outcome)  # exact replay
    ingest_outcome(store, outcome)  # exact replay again

    assert _read_jsonl(store.path_for("finding")) == []
    # A genuinely second, distinct evidence run is still required to promote.
    ingest_outcome(store, _outcome(factor_id="FTR_B", factor_fingerprint=FP_B))
    assert _read_jsonl(store.path_for("finding"))


# ---------------------------------------------------------------------------
# crash-safety: unconditional observation recording
# ---------------------------------------------------------------------------


def test_ingest_records_observations_even_when_envelope_was_already_present(tmp_path: Path) -> None:
    # Simulates a prior crashed ingest that recorded the ledger envelope but
    # died before recording observations: record_outcome_envelope directly
    # (bypassing ingest_outcome) pre-populates the ledger, so a later
    # ingest_outcome call for the SAME outcome sees recorded=False, but MUST
    # still record its observations (module docstring "Unconditional
    # recording (crash-safety)").
    store = _store(tmp_path)
    outcome = _outcome(verdict="blocked", reason_codes=("SHARPE_BELOW_GATE",))
    store.record_outcome_envelope(outcome.to_record())
    assert _read_jsonl(store.observations_path) == []

    receipt = ingest_outcome(store, outcome)

    assert receipt.recorded is False
    assert receipt.observation_count == 1
    assert len(_read_jsonl(store.observations_path)) == 1


# ---------------------------------------------------------------------------
# as_of: monotonic across distinct outcomes, stable across a replay
# ---------------------------------------------------------------------------


def test_as_of_increases_for_distinct_outcomes_and_is_stable_on_replay(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = ingest_outcome(store, _outcome(factor_id="FTR_A"))
    assert first.as_of == 1

    replay = ingest_outcome(store, _outcome(factor_id="FTR_A"))
    assert replay.as_of == first.as_of  # stable: no new ledger row

    second = ingest_outcome(store, _outcome(factor_id="FTR_B"))
    assert second.as_of == first.as_of + 1

    assert store.outcomes_revision() == second.as_of


def test_known_outcome_ids_reflects_ledger_membership(tmp_path: Path) -> None:
    store = _store(tmp_path)
    a = ingest_outcome(store, _outcome(factor_id="FTR_A"))
    b = ingest_outcome(store, _outcome(factor_id="FTR_B"))

    known = store.known_outcome_ids()
    assert known == {a.outcome_id, b.outcome_id}


# ---------------------------------------------------------------------------
# dual-domain isolation (SE-i): store routing is entirely the caller's job
# ---------------------------------------------------------------------------


def test_two_store_roots_keep_independent_ledgers(tmp_path: Path) -> None:
    main_store = _store(tmp_path, "main_root")
    plugin_store = _store(tmp_path, "plugin_root")
    outcome = _outcome()

    ingest_outcome(main_store, outcome)

    assert main_store.known_outcome_ids() == {outcome.outcome_id()}
    assert plugin_store.known_outcome_ids() == frozenset()
    assert not plugin_store.outcomes_ledger_path.exists()

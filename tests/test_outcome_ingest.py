"""SE-P2 regression tests: the shared ResearchOutcome ingress sink.

Covers ``src/quant_forge/research_loop/outcome_ingest.py`` (and, through it,
the ADDITIVE outcome-ledger methods on ``ResearchMemoryStore``) against
DECISIONS.md "2026-07-13 -- Self-evolution engine CP0", rulings
SE-i/SE-ii/SE-ix, plus the SE-P2 review rework (P2-F5): a replay whose
``outcome_id`` is already on the ledger appends NOTHING (no envelope, no
observations -- ``observations.jsonl`` cannot grow on administrative
resends) but still runs promotion; for a new outcome the envelope lands
LAST as the completion marker, so a crash between observations and
envelope self-heals on retry; ``as_of`` monotonic across distinct outcomes
and stable across an exact replay.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

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
# crash-safety: replay short-circuit + envelope-last completion marker (P2-F5)
# ---------------------------------------------------------------------------


def test_ingest_ledger_known_id_appends_nothing(tmp_path: Path) -> None:
    # The envelope is the sink's COMPLETION MARKER (written last). An id
    # already on the ledger means a fully completed prior ingest, so a
    # replay appends nothing at all -- the pre-review behavior (re-append
    # every observation on every replay) let administrative resends grow
    # observations.jsonl without bound and let an earlier-observed_at resend
    # shift the pre-threshold representative row (P2-F5).
    store = _store(tmp_path)
    outcome = _outcome(verdict="blocked", reason_codes=("SHARPE_BELOW_GATE",))
    store.record_outcome_envelope(outcome.to_record())
    assert _read_jsonl(store.observations_path) == []

    receipt = ingest_outcome(store, outcome)

    assert receipt.recorded is False
    assert receipt.observation_count == 0
    assert _read_jsonl(store.observations_path) == []


def test_concurrent_same_outcome_ingests_append_exactly_once(tmp_path: Path) -> None:
    # RV2-F3: the replay check + observation appends + envelope marker are
    # ONE store-level critical section (ResearchMemoryStore.ingest_outcome_
    # rows), so racing ingests of the same outcome cannot both observe
    # "absent" and double-append. Exactly one thread records; the rest are
    # replays appending nothing.
    import threading

    store = _store(tmp_path)
    outcome = _outcome(verdict="blocked", reason_codes=("SHARPE_BELOW_GATE", "TURNOVER_TOO_HIGH"))
    receipts: list[Any] = []
    receipts_lock = threading.Lock()
    barrier = threading.Barrier(4)

    def _ingest() -> None:
        barrier.wait()
        receipt = ingest_outcome(store, outcome)
        with receipts_lock:
            receipts.append(receipt)

    threads = [threading.Thread(target=_ingest) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(receipt.recorded for receipt in receipts) == [False, False, False, True]
    assert sorted(receipt.observation_count for receipt in receipts) == [0, 0, 0, 2]
    assert len(_read_jsonl(store.observations_path)) == 2  # appended exactly once
    assert len(_read_jsonl(store.outcomes_ledger_path)) == 1  # one envelope
    assert all(receipt.as_of == 1 for receipt in receipts)


def test_ingest_replay_never_grows_observations_jsonl(tmp_path: Path) -> None:
    store = _store(tmp_path)
    outcome = _outcome(verdict="blocked", reason_codes=("SHARPE_BELOW_GATE", "TURNOVER_TOO_HIGH"))
    first = ingest_outcome(store, outcome)
    assert first.observation_count == 2
    assert len(_read_jsonl(store.observations_path)) == 2

    for _ in range(3):
        replay = ingest_outcome(store, outcome)
        assert replay.recorded is False
        assert replay.observation_count == 0
    assert len(_read_jsonl(store.observations_path)) == 2  # physically unchanged


def test_ingest_crash_between_observations_and_envelope_self_heals_on_retry(tmp_path: Path) -> None:
    # Envelope-last ordering: simulate a crash AFTER the observation rows
    # landed but BEFORE the completion-marker envelope, by recording the
    # observations directly (what a torn first attempt leaves behind). The
    # retry sees an unknown id, re-appends its observations (duplicates are
    # bounded to real crash windows and stay scientifically inert -- the
    # promote cap counts <=1 per (signature, run_id)), and completes the
    # envelope; a further replay then appends nothing.
    from quant_forge.research_loop.outcomes import outcome_to_observations

    store = _store(tmp_path)
    outcome = _outcome(verdict="blocked", reason_codes=("SHARPE_BELOW_GATE",))
    for observation in outcome_to_observations(outcome):
        store.record_observation(
            signature=observation.signature,
            statement=observation.statement,
            run_id=observation.run_id,
            data_window=observation.data_window,
            failure_class=observation.failure_class,
            evidence_ref=observation.evidence_ref,
            observed_at=observation.observed_at,
            scope=observation.scope,
        )
    assert store.known_outcome_ids() == frozenset()  # marker never landed

    retry = ingest_outcome(store, outcome)
    assert retry.recorded is True
    assert retry.observation_count == 1
    assert len(_read_jsonl(store.observations_path)) == 2  # torn attempt + retry
    assert store.known_outcome_ids() == {outcome.outcome_id()}

    replay = ingest_outcome(store, outcome)
    assert replay.recorded is False
    assert replay.observation_count == 0
    assert len(_read_jsonl(store.observations_path)) == 2  # replay adds nothing

    # The duplicate rows are one evidence unit: no self-promotion from a
    # crash retry, and a second DISTINCT evidence run still promotes.
    assert _read_jsonl(store.path_for("failure")) == []
    ingest_outcome(
        store,
        _outcome(factor_id="FTR_B", factor_fingerprint=FP_B, verdict="blocked", reason_codes=("SHARPE_BELOW_GATE",)),
    )
    assert _read_jsonl(store.path_for("failure"))


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


def test_ingest_rejects_origin_mismatch_in_both_directions_and_writes_nothing(tmp_path: Path) -> None:
    # F1: the origin-bound ingress guard (defense-in-depth, SE-i) rejects a
    # spoofed origin in BOTH directions and persists NOTHING -- no envelope, no
    # observations -- because the check runs inside the store's single critical
    # section, before any append.
    external_outcome = _outcome(origin="external_plugin")
    local_outcome = _outcome(origin="local")

    # Direction 1: a LOCAL store (expected_origin defaults to "local") refuses
    # an external_plugin outcome.
    local_store = _store(tmp_path, "local_root")
    with pytest.raises(ValueError, match="does not match this store's expected origin"):
        ingest_outcome(local_store, external_outcome)
    assert _read_jsonl(local_store.outcomes_ledger_path) == []
    assert _read_jsonl(local_store.observations_path) == []
    assert local_store.known_outcome_ids() == frozenset()

    # Direction 2: a plugin store (expected_origin="external_plugin") refuses a
    # local outcome.
    plugin_store = _store(tmp_path, "plugin_root")
    with pytest.raises(ValueError, match="does not match this store's expected origin"):
        ingest_outcome(plugin_store, local_outcome, expected_origin="external_plugin")
    assert _read_jsonl(plugin_store.outcomes_ledger_path) == []
    assert plugin_store.known_outcome_ids() == frozenset()

    # The MATCHING direction still ingests: an external_plugin outcome into an
    # external_plugin-expecting store is accepted and persisted.
    receipt = ingest_outcome(plugin_store, external_outcome, expected_origin="external_plugin")
    assert receipt.recorded is True
    assert plugin_store.known_outcome_ids() == {external_outcome.outcome_id()}


def test_ingress_survives_non_object_rows_in_ledger_and_observations(tmp_path: Path) -> None:
    # F12: a JSON-valid but non-object row (e.g. []) injected into the outcomes
    # ledger OR the observations file must not crash the ingress replay check
    # (_known_outcome_ids_unlocked) or the observation read (_read_observations).
    # The corrupt row is quarantined by exclusion, and a subsequent genuine
    # outcome still ingests.
    store = _store(tmp_path)
    first = ingest_outcome(store, _outcome(factor_id="FTR_A", factor_fingerprint=FP_A))
    assert first.recorded is True

    with store.outcomes_ledger_path.open("a", encoding="utf-8") as handle:
        handle.write("[]\n")
    with store.observations_path.open("a", encoding="utf-8") as handle:
        handle.write('"not an object"\n')

    # The next new outcome ingests without crashing on either corrupt row.
    second = ingest_outcome(store, _outcome(factor_id="FTR_B", factor_fingerprint=FP_B))
    assert second.recorded is True

    # Both real ids remain; the [] row never entered the replay set.
    assert store.known_outcome_ids() == {first.outcome_id, second.outcome_id}

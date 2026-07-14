"""SE-P4a regression tests: append-only review events (activate/deactivate/retire).

Covers ``research_loop/memory.py``'s :class:`MemoryReviewEvent` +
``ResearchMemoryStore`` review-event methods, and the ``qf memory rules`` CLI
parity commands. See DECISIONS.md "2026-07-13 -- Self-evolution engine CP0",
ruling SE-iii: promoted rule/finding/failure rows are NEVER mutated to express
a human governance decision; "effective" state is always a pure function of
the latest valid event per signature.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import threading

import pytest

import quant_forge.apps.cli.main as cli_main
from quant_forge.research_loop.memory import (
    RESEARCH_MEMORY_REVIEW_SCHEMA_VERSION,
    REVIEW_ACTIONS,
    MemoryReviewEvent,
    ResearchMemoryStore,
)

SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
T1 = "2026-07-01T00:00:00+00:00"
T2 = "2026-07-02T00:00:00+00:00"
T3 = "2026-07-03T00:00:00+00:00"
T4 = "2026-07-04T00:00:00+00:00"
WINDOW_A = "2024-01-01:2024-06-30"
WINDOW_B = "2024-07-01:2024-12-31"


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _promote_rule(store: ResearchMemoryStore, *, signature: str = "sig_rule", scope: str = "global") -> dict:
    """Mint one rule candidate row (3 obs, 2 windows, 2+ runs) and return it."""

    for run_id, observed_at, window in (
        (f"{signature}-1", T1, WINDOW_A),
        (f"{signature}-2", T2, WINDOW_A),
        (f"{signature}-3", T3, WINDOW_B),
    ):
        store.record_observation(
            signature=signature,
            statement=f"rule statement for {signature}",
            run_id=run_id,
            observed_at=observed_at,
            data_window=window,
            scope=scope,
        )
    store.promote_pending()
    return store.resolve_signature_prefix("rule", signature)


def _promote_finding(store: ResearchMemoryStore, *, signature: str = "sig_finding") -> dict:
    for run_id, observed_at in ((f"{signature}-1", T1), (f"{signature}-2", T2)):
        store.record_observation(
            signature=signature, statement=f"finding statement for {signature}", run_id=run_id, observed_at=observed_at
        )
    store.promote_pending()
    return store.resolve_signature_prefix("finding", signature)


# ---------------------------------------------------------------------------
# MemoryReviewEvent: validation, schema, event_id identity
# ---------------------------------------------------------------------------


def _event(**overrides) -> MemoryReviewEvent:
    payload = dict(
        target_kind="rule",
        target_signature="sig_x",
        reviewed_entry_id="entry_x",
        action="activate",
        actor="alice",
        decided_at=T1,
    )
    payload.update(overrides)
    return MemoryReviewEvent(**payload)


def test_review_actions_are_the_closed_four_action_vocabulary() -> None:
    assert REVIEW_ACTIONS == ("activate", "deactivate", "retire", "unretire")


def test_unknown_action_raises() -> None:
    with pytest.raises(ValueError, match="unknown review action"):
        _event(action="approve")


@pytest.mark.parametrize("action", ["activate", "deactivate"])
def test_rule_only_actions_reject_finding_and_failure_targets(action: str) -> None:
    for target_kind in ("finding", "failure"):
        with pytest.raises(ValueError, match="only valid for target_kind='rule'"):
            _event(action=action, target_kind=target_kind)


@pytest.mark.parametrize("action", ["retire", "unretire"])
def test_retire_actions_reject_rule_target(action: str) -> None:
    with pytest.raises(ValueError, match="only valid for target_kind"):
        _event(action=action, target_kind="rule")


@pytest.mark.parametrize("action", ["retire", "unretire"])
@pytest.mark.parametrize("target_kind", ["finding", "failure"])
def test_retire_actions_accept_finding_and_failure_targets(action: str, target_kind: str) -> None:
    event = _event(action=action, target_kind=target_kind)
    assert event.action == action
    assert event.target_kind == target_kind


def test_required_fields_reject_empty_values() -> None:
    with pytest.raises(ValueError, match="target_signature"):
        _event(target_signature="  ")
    with pytest.raises(ValueError, match="reviewed_entry_id"):
        _event(reviewed_entry_id="")
    with pytest.raises(ValueError, match="actor"):
        _event(actor="   ")


def test_decided_at_must_be_tz_aware_iso() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        _event(decided_at="2026-07-01T00:00:00")
    with pytest.raises(ValueError, match="ISO format"):
        _event(decided_at="not-a-timestamp")


def test_supersedes_must_be_a_sha256_hex_event_id_or_empty() -> None:
    _event(supersedes="")  # allowed
    _event(supersedes="a" * 64)  # allowed
    with pytest.raises(ValueError, match="supersedes must be a sha256"):
        _event(supersedes="not-a-hash")


def test_schema_version_is_pinned() -> None:
    event = _event()
    assert event.schema_version == RESEARCH_MEMORY_REVIEW_SCHEMA_VERSION == "qf.memory_review.v1"
    with pytest.raises(ValueError, match="schema_version"):
        _event(schema_version="qf.memory_review.v2")


def test_event_id_is_a_sha256_fingerprint_over_the_full_payload_excluding_itself() -> None:
    event = _event()
    assert SHA256_HEX.fullmatch(event.event_id())
    # event_id is deterministic and reproducible from to_dict() alone.
    from quant_forge.lineage.store import canonical_fingerprint

    assert event.event_id() == canonical_fingerprint(event.to_dict())
    assert "event_id" not in event.to_dict()


def test_event_id_changes_when_decided_at_changes() -> None:
    # Unlike outcome_id() (which excludes the timestamp), event_id() is an
    # EXACT-payload fingerprint: even the same decision re-decided a moment
    # later is a genuinely new event, not a dedup-eligible replay.
    first = _event(decided_at=T1)
    second = _event(decided_at=T2)
    assert first.event_id() != second.event_id()


# ---------------------------------------------------------------------------
# record_review_event: append-only, idempotent exact replay
# ---------------------------------------------------------------------------


def test_record_review_event_appends_one_row_with_expected_schema(tmp_path: Path) -> None:
    store = ResearchMemoryStore(tmp_path / "artifacts")
    row = _promote_rule(store)

    event = store.record_review_event(
        target_kind="rule",
        target_signature="sig_rule",
        reviewed_entry_id=row["entry_id"],
        action="activate",
        actor="alice",
        rationale="looks solid",
        decided_at=T3,
    )

    rows = _read_jsonl(store.review_events_path)
    assert len(rows) == 1
    stored = rows[0]
    assert stored["event_id"] == event.event_id()
    assert SHA256_HEX.fullmatch(stored["event_id"])
    assert stored["schema_version"] == RESEARCH_MEMORY_REVIEW_SCHEMA_VERSION
    assert stored["target_kind"] == "rule"
    assert stored["target_signature"] == "sig_rule"
    assert stored["reviewed_entry_id"] == row["entry_id"]
    assert stored["action"] == "activate"
    assert stored["actor"] == "alice"
    assert stored["rationale"] == "looks solid"
    assert stored["decided_at"] == T3
    assert stored["supersedes"] == ""


def test_event_replay_with_identical_payload_is_idempotent(tmp_path: Path) -> None:
    store = ResearchMemoryStore(tmp_path / "artifacts")
    row = _promote_rule(store)
    kwargs = dict(
        target_kind="rule",
        target_signature="sig_rule",
        reviewed_entry_id=row["entry_id"],
        action="activate",
        actor="alice",
        rationale="",
        decided_at=T2,
    )

    first = store.record_review_event(**kwargs)
    content_after_first = store.review_events_path.read_text(encoding="utf-8")
    second = store.record_review_event(**kwargs)
    content_after_second = store.review_events_path.read_text(encoding="utf-8")

    assert first.event_id() == second.event_id()
    assert content_after_first == content_after_second
    assert len(_read_jsonl(store.review_events_path)) == 1

    # A THIRD call with a materially different payload (new rationale) is a
    # genuinely new event and DOES append.
    store.record_review_event(**{**kwargs, "rationale": "reconsidered with more evidence"})
    assert len(_read_jsonl(store.review_events_path)) == 2


def test_actor_and_rationale_are_redacted_before_reaching_disk(tmp_path: Path) -> None:
    store = ResearchMemoryStore(tmp_path / "artifacts")
    row = _promote_rule(store)
    home = str(Path.home())

    store.record_review_event(
        target_kind="rule",
        target_signature="sig_rule",
        reviewed_entry_id=row["entry_id"],
        action="activate",
        actor="alice",
        rationale=f"verified against {home}/notes.md; QF_REVIEW_TOKEN=sekret123456",
        decided_at=T2,
    )

    raw = store.review_events_path.read_text(encoding="utf-8")
    assert home not in raw
    assert "sekret123456" not in raw
    stored = _read_jsonl(store.review_events_path)[0]
    assert "<redacted-path>" in stored["rationale"]
    assert "QF_REVIEW_TOKEN=<redacted>" in stored["rationale"]


# ---------------------------------------------------------------------------
# rule_states / retired_signatures: pure derivation, activate<->deactivate
# chains, kind-scoped retirement
# ---------------------------------------------------------------------------


def test_rule_states_default_absent_to_inactive(tmp_path: Path) -> None:
    store = ResearchMemoryStore(tmp_path / "artifacts")
    _promote_rule(store)
    assert store.rule_states() == {}
    assert store.rule_states().get("sig_rule", "inactive") == "inactive"


def test_activate_deactivate_activate_chain(tmp_path: Path) -> None:
    store = ResearchMemoryStore(tmp_path / "artifacts")
    row = _promote_rule(store)

    store.record_review_event(
        target_kind="rule", target_signature="sig_rule", reviewed_entry_id=row["entry_id"],
        action="activate", actor="alice", decided_at=T1,
    )
    assert store.rule_states() == {"sig_rule": "active"}

    store.record_review_event(
        target_kind="rule", target_signature="sig_rule", reviewed_entry_id=row["entry_id"],
        action="deactivate", actor="bob", decided_at=T2,
    )
    assert store.rule_states() == {"sig_rule": "inactive"}

    store.record_review_event(
        target_kind="rule", target_signature="sig_rule", reviewed_entry_id=row["entry_id"],
        action="activate", actor="carol", decided_at=T3,
    )
    assert store.rule_states() == {"sig_rule": "active"}

    # The full chain remains readable: three distinct events, none rewritten.
    events = _read_jsonl(store.review_events_path)
    assert len(events) == 3
    assert [row_["action"] for row_ in events] == ["activate", "deactivate", "activate"]
    assert [row_["actor"] for row_ in events] == ["alice", "bob", "carol"]


def test_rule_states_derivation_is_order_independent_of_decided_at_not_append_order(tmp_path: Path) -> None:
    # The LATEST event by decided_at wins, even if it was appended earlier in
    # wall-clock append order than a since-superseded one (defensive: the
    # store must sort by decided_at, not merely take the last-appended row).
    store = ResearchMemoryStore(tmp_path / "artifacts")
    row = _promote_rule(store)

    store.record_review_event(
        target_kind="rule", target_signature="sig_rule", reviewed_entry_id=row["entry_id"],
        action="activate", actor="alice", decided_at=T3,
    )
    store.record_review_event(
        target_kind="rule", target_signature="sig_rule", reviewed_entry_id=row["entry_id"],
        action="deactivate", actor="bob", decided_at=T1,
    )
    # T3 (activate) is chronologically LATER than T1 (deactivate) even though
    # deactivate was appended second; activate must still win.
    assert store.rule_states() == {"sig_rule": "active"}


def test_retired_signatures_is_kind_scoped(tmp_path: Path) -> None:
    store = ResearchMemoryStore(tmp_path / "artifacts")
    finding_row = _promote_finding(store, signature="sig_shared")
    assert store.retired_signatures("finding") == frozenset()
    assert store.retired_signatures("failure") == frozenset()

    store.record_review_event(
        target_kind="finding", target_signature="sig_shared", reviewed_entry_id=finding_row["entry_id"],
        action="retire", actor="dave", decided_at=T1,
    )
    assert store.retired_signatures("finding") == frozenset({"sig_shared"})
    # Retiring the FINDING must never retire an unrelated FAILURE row that
    # happens to share the same signature string.
    assert store.retired_signatures("failure") == frozenset()


def test_unretire_reverses_retirement(tmp_path: Path) -> None:
    store = ResearchMemoryStore(tmp_path / "artifacts")
    finding_row = _promote_finding(store)
    store.record_review_event(
        target_kind="finding", target_signature="sig_finding", reviewed_entry_id=finding_row["entry_id"],
        action="retire", actor="dave", decided_at=T1,
    )
    assert store.retired_signatures("finding") == frozenset({"sig_finding"})

    store.record_review_event(
        target_kind="finding", target_signature="sig_finding", reviewed_entry_id=finding_row["entry_id"],
        action="unretire", actor="erin", decided_at=T2,
    )
    assert store.retired_signatures("finding") == frozenset()


def test_retired_signatures_rejects_rule_target_kind(tmp_path: Path) -> None:
    store = ResearchMemoryStore(tmp_path / "artifacts")
    with pytest.raises(ValueError, match="finding.*failure|failure.*finding"):
        store.retired_signatures("rule")


# ---------------------------------------------------------------------------
# FP-2 regression: promotion structurally cannot mint activity; a hand-
# tampered row status is irrelevant because state derives from events only.
# ---------------------------------------------------------------------------


def test_promoted_rule_row_status_never_becomes_active(tmp_path: Path) -> None:
    store = ResearchMemoryStore(tmp_path / "artifacts")
    _promote_rule(store)
    rows = _read_jsonl(store.path_for("rule"))
    assert len(rows) == 1
    assert rows[0]["status"] == "needs_human_review"

    # Confirming observations keep arriving; the row is superseded but its
    # status is STILL needs_human_review -- promotion alone never flips it.
    store.record_observation(
        signature="sig_rule", statement="rule statement for sig_rule", run_id="sig_rule-4",
        observed_at=T4, data_window=WINDOW_B,
    )
    store.promote_pending()
    rows = _read_jsonl(store.path_for("rule"))
    assert len(rows) == 2
    assert all(row["status"] == "needs_human_review" for row in rows)


def test_direct_row_status_tamper_does_not_grant_effective_activation(tmp_path: Path) -> None:
    # A bug or a malicious actor could hand-edit rules.jsonl (append a row
    # claiming status="active" directly, bypassing PromotionDecision's
    # constructor guard entirely). rule_states() must still report this
    # signature as inactive: it derives PURELY from activations.jsonl, never
    # from anything rules.jsonl says.
    store = ResearchMemoryStore(tmp_path / "artifacts")
    row = _promote_rule(store, signature="sig_tamper")
    assert store.rule_states().get("sig_tamper", "inactive") == "inactive"

    tampered = dict(row)
    tampered["status"] = "active"
    tampered["supersedes"] = row["entry_id"]
    tampered_payload = {key: value for key, value in tampered.items() if key != "entry_id"}
    from quant_forge.lineage.store import canonical_fingerprint

    tampered["entry_id"] = canonical_fingerprint(tampered_payload)
    with store.path_for("rule").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(tampered, sort_keys=True) + "\n")

    # The tampered row is even the LATEST live row for the signature now...
    latest = store.list_promoted("rule")[0]
    assert latest["status"] == "active"
    # ...yet rule_states() (the ONLY path context_builder consults) still
    # reports inactive: no review event was ever recorded.
    assert store.rule_states().get("sig_tamper", "inactive") == "inactive"

    # Only a genuine event can flip it.
    store.record_review_event(
        target_kind="rule", target_signature="sig_tamper", reviewed_entry_id=latest["entry_id"],
        action="activate", actor="alice", decided_at=T2,
    )
    assert store.rule_states() == {"sig_tamper": "active"}


# ---------------------------------------------------------------------------
# Advisory-lock concurrency smoke: concurrent observation + review-event
# appends must never produce a torn JSONL line, and no write may be lost
# (no subprocess-based lock-concurrency pattern exists elsewhere in this
# suite to reuse; test_atomic_write_concurrency.py's threaded pattern is the
# closest existing precedent and is adapted here).
# ---------------------------------------------------------------------------


def test_concurrent_observation_and_review_event_appends_produce_no_torn_lines(tmp_path: Path) -> None:
    store = ResearchMemoryStore(tmp_path / "artifacts")
    row = _promote_rule(store, signature="sig_concurrent")

    writer_count = 8
    rounds = 15
    barrier = threading.Barrier(writer_count * 2)
    errors: list[str] = []

    def observation_writer(index: int) -> None:
        for round_index in range(rounds):
            try:
                barrier.wait(timeout=60)
            except threading.BrokenBarrierError:
                return
            try:
                store.record_observation(
                    signature=f"sig_concurrent_obs_{index}",
                    statement=f"concurrent observation {index}",
                    run_id=f"writer-{index}-round-{round_index}",
                    observed_at=T1,
                    data_window=WINDOW_A,
                )
            except Exception as exc:  # noqa: BLE001 - the raised race IS the finding
                errors.append(f"observation writer {index}: {exc!r}")

    def event_writer(index: int) -> None:
        for round_index in range(rounds):
            try:
                barrier.wait(timeout=60)
            except threading.BrokenBarrierError:
                return
            try:
                store.record_review_event(
                    target_kind="rule",
                    target_signature="sig_concurrent",
                    reviewed_entry_id=row["entry_id"],
                    action="activate" if round_index % 2 == 0 else "deactivate",
                    actor=f"reviewer-{index}",
                    # Unique decided_at per call so every event is a distinct,
                    # non-idempotent-deduped row (exercises the append path,
                    # not the replay-drop path).
                    decided_at=f"2026-07-01T00:00:{index:02d}.{round_index:06d}+00:00",
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(f"event writer {index}: {exc!r}")

    threads = [threading.Thread(target=observation_writer, args=(index,)) for index in range(writer_count)]
    threads += [threading.Thread(target=event_writer, args=(index,)) for index in range(writer_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)

    for thread in threads:
        assert not thread.is_alive(), "writer thread did not terminate"
    assert errors == []

    # No torn lines: every line in both files parses as JSON, and the line
    # count matches exactly what was successfully appended (nothing lost,
    # nothing duplicated beyond the intentionally-unique payloads above).
    obs_lines = store.observations_path.read_text(encoding="utf-8").splitlines()
    event_lines = store.review_events_path.read_text(encoding="utf-8").splitlines()
    for line in obs_lines:
        json.loads(line)  # raises on any torn/malformed line
    for line in event_lines:
        json.loads(line)
    # +3 for the observations _promote_rule() recorded before the concurrent
    # phase started; the concurrent phase itself contributes exactly one
    # observation line per (writer, round).
    assert len(obs_lines) == writer_count * rounds + 3
    assert len(event_lines) == writer_count * rounds
    assert len(store.rule_states()) == 1  # exactly sig_concurrent, no corruption


# ---------------------------------------------------------------------------
# CLI flows (list / activate / ambiguous-prefix error / deactivate / retire)
# ---------------------------------------------------------------------------


def _cli_json(capsys: pytest.CaptureFixture[str], argv: list[str], *, expect: int = 0):
    exit_code = cli_main.main(argv)
    output = capsys.readouterr().out
    assert exit_code == expect, output
    return json.loads(output) if expect == 0 else output


def test_cli_memory_rules_round_trip(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # Deliberately non-prefix-colliding signatures here: this test exercises
    # list/activate/deactivate output content, not prefix ambiguity (that is
    # covered separately below with signatures chosen to collide).
    artifact_root = tmp_path / "artifacts"
    root_arg = ["--artifact-root", str(artifact_root)]
    store = ResearchMemoryStore(artifact_root)
    _promote_rule(store, signature="sig_one_alpha")
    _promote_rule(store, signature="sig_two_beta")

    # list: both rules pending (no --active/--pending filter -> everything),
    # identified by their (untruncated) statement text since the displayed
    # "signature_prefix" column is intentionally truncated to 12 chars.
    exit_code = cli_main.main(["memory", "rules", "list", *root_arg])
    listed_all = capsys.readouterr().out
    assert exit_code == 0
    assert listed_all.count("inactive") == 2
    assert "rule statement for sig_one_alpha" in listed_all
    assert "rule statement for sig_two_beta" in listed_all

    # --active with nothing activated yet: honest empty message, not an
    # empty table.
    exit_code = cli_main.main(["memory", "rules", "list", "--active", *root_arg])
    assert exit_code == 0
    assert "no rules recorded" in capsys.readouterr().out

    # Activate one rule via its full (unambiguous) signature.
    activated = _cli_json(
        capsys,
        ["memory", "rules", "activate", "sig_one_alpha", "--actor", "alice", "--rationale", "solid", *root_arg],
    )
    assert activated["target_signature"] == "sig_one_alpha"
    assert activated["action"] == "activate"
    assert activated["actor"] == "alice"
    assert activated["rationale"] == "solid"

    # --active now shows exactly the activated rule; --pending shows only
    # the other one.
    exit_code = cli_main.main(["memory", "rules", "list", "--active", *root_arg])
    active_listing = capsys.readouterr().out
    assert exit_code == 0
    assert "rule statement for sig_one_alpha" in active_listing
    assert "sig_two_beta" not in active_listing

    exit_code = cli_main.main(["memory", "rules", "list", "--pending", *root_arg])
    pending_listing = capsys.readouterr().out
    assert exit_code == 0
    assert "rule statement for sig_two_beta" in pending_listing
    assert "sig_one_alpha" not in pending_listing

    # Deactivate it back.
    deactivated = _cli_json(capsys, ["memory", "rules", "deactivate", "sig_one_alpha", "--actor", "carol", *root_arg])
    assert deactivated["action"] == "deactivate"
    assert store.rule_states() == {"sig_one_alpha": "inactive"}

    # An unknown prefix (no candidates at all) errors cleanly, exit 2.
    missing_output = cli_main.main(["memory", "rules", "activate", "no-such-signature", "--actor", "dave", *root_arg])
    assert missing_output == 2
    assert "no rule signature matches prefix" in capsys.readouterr().out


def test_cli_memory_rules_ambiguous_prefix_errors_listing_candidates(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # This is the anti-fat-finger confirmation R3 wants: no interactive
    # prompt, an ambiguous prefix simply fails and names every candidate.
    artifact_root = tmp_path / "artifacts"
    root_arg = ["--artifact-root", str(artifact_root)]
    store = ResearchMemoryStore(artifact_root)
    _promote_rule(store, signature="sig_shared_alpha")
    _promote_rule(store, signature="sig_shared_beta")

    exit_code = cli_main.main(["memory", "rules", "activate", "sig_shared", "--actor", "alice", *root_arg])
    output = capsys.readouterr().out
    assert exit_code == 2
    assert "ambiguous" in output
    assert "sig_shared_alpha" in output and "sig_shared_beta" in output
    # Neither candidate was actually activated by the failed attempt.
    assert store.rule_states() == {}

    # A full, unambiguous signature still resolves and succeeds normally.
    resolved = _cli_json(capsys, ["memory", "rules", "activate", "sig_shared_alpha", "--actor", "alice", *root_arg])
    assert resolved["target_signature"] == "sig_shared_alpha"
    assert store.rule_states() == {"sig_shared_alpha": "active"}


def test_resolve_signature_prefix_exact_match_wins_over_prefix_ambiguity(tmp_path: Path) -> None:
    store = ResearchMemoryStore(tmp_path / "artifacts")
    _promote_rule(store, signature="sig_dup")
    _promote_rule(store, signature="sig_dup_extended")

    # "sig_dup" is BOTH an exact match for one signature AND a structural
    # prefix of the other ("sig_dup_extended"). The exact match must win
    # outright rather than raising ambiguous, so pasting a full signature
    # never trips over an unrelated signature that happens to extend it.
    resolved = store.resolve_signature_prefix("rule", "sig_dup")
    assert resolved["signature"] == "sig_dup"

    # A shorter prefix with no exact match is still correctly ambiguous.
    with pytest.raises(ValueError, match="ambiguous"):
        store.resolve_signature_prefix("rule", "sig_du")

    # An empty prefix is rejected outright (never resolves to "everything").
    with pytest.raises(ValueError, match="must not be empty"):
        store.resolve_signature_prefix("rule", "")

    # A prefix matching nothing at all is a clean "no match" error.
    with pytest.raises(ValueError, match="no rule signature matches"):
        store.resolve_signature_prefix("rule", "totally-unrelated")


def test_cli_memory_rules_retire(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    artifact_root = tmp_path / "artifacts"
    root_arg = ["--artifact-root", str(artifact_root)]
    store = ResearchMemoryStore(artifact_root)
    _promote_finding(store, signature="sig_retire_me")

    retired = _cli_json(
        capsys,
        ["memory", "rules", "retire", "finding", "sig_retire_me", "--actor", "erin", *root_arg],
    )
    assert retired["action"] == "retire"
    assert retired["target_kind"] == "finding"
    assert store.retired_signatures("finding") == frozenset({"sig_retire_me"})

    # retire only accepts finding|failure at the argparse level; anything
    # else fails argument parsing before the handler even runs.
    with pytest.raises(SystemExit):
        cli_main.main(["memory", "rules", "retire", "rule", "sig_retire_me", "--actor", "erin", *root_arg])


def test_cli_memory_rules_require_actor(tmp_path: Path) -> None:
    artifact_root = tmp_path / "artifacts"
    store = ResearchMemoryStore(artifact_root)
    _promote_rule(store)
    with pytest.raises(SystemExit):
        cli_main.main(["memory", "rules", "activate", "sig_rule", "--artifact-root", str(artifact_root)])

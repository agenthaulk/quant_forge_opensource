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
import os
from pathlib import Path
import re
import subprocess
import sys
import threading

import pytest

import quant_forge.apps.cli.main as cli_main
from quant_forge.lineage.store import canonical_fingerprint
from quant_forge.research_loop.memory import (
    RESEARCH_MEMORY_REVIEW_SCHEMA_VERSION,
    REVIEW_ACTIONS,
    MemoryReviewEvent,
    ResearchMemoryStore,
    _quarantine_path_for,
    _repair_torn_tail,
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


def test_latest_event_is_file_append_order_never_decided_at(tmp_path: Path) -> None:
    # P4a rework item 3 (opus probe C2 regression): "latest valid event" is
    # FILE APPEND ORDER under the lock, never decided_at. A deactivate
    # appended AFTER an activate must win even when its decided_at is NOT
    # chronologically later -- same-second timestamps, clock skew across
    # writers, or a caller-supplied timestamp that is flatly "earlier" must
    # never invert the true decision sequence.
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
    # T3 (activate) is chronologically LATER than T1 (deactivate) by
    # decided_at, but deactivate was appended SECOND -- append order wins,
    # so the rule is inactive. (Under the pre-rework decided_at-sorted
    # design this incorrectly asserted "active".)
    assert store.rule_states() == {"sig_rule": "inactive"}


def test_same_second_activate_then_deactivate_yields_inactive(tmp_path: Path) -> None:
    # The literal probe scenario: IDENTICAL decided_at values (the
    # same-second collision) must still resolve correctly via append order,
    # with no hash-based or otherwise arbitrary tiebreak.
    store = ResearchMemoryStore(tmp_path / "artifacts")
    row = _promote_rule(store)
    same_instant = "2026-07-01T00:00:00.500000+00:00"

    store.record_review_event(
        target_kind="rule", target_signature="sig_rule", reviewed_entry_id=row["entry_id"],
        action="activate", actor="alice", decided_at=same_instant,
    )
    store.record_review_event(
        target_kind="rule", target_signature="sig_rule", reviewed_entry_id=row["entry_id"],
        action="deactivate", actor="bob", decided_at=same_instant,
    )
    assert store.rule_states() == {"sig_rule": "inactive"}

    # A THIRD event with a decided_at that is even "earlier" on paper must
    # still win: it was appended last.
    store.record_review_event(
        target_kind="rule", target_signature="sig_rule", reviewed_entry_id=row["entry_id"],
        action="activate", actor="carol", decided_at="2026-01-01T00:00:00+00:00",
    )
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
# P4a rework items 2 + 3: row binding, supersedes integrity, and the row-
# content-change activation lapse.
# ---------------------------------------------------------------------------


def test_supersedes_auto_population_chains_correctly(tmp_path: Path) -> None:
    store = ResearchMemoryStore(tmp_path / "artifacts")
    row = _promote_rule(store)

    first = store.record_review_event(
        target_kind="rule", target_signature="sig_rule", reviewed_entry_id=row["entry_id"],
        action="activate", actor="alice", decided_at=T1,
    )
    second = store.record_review_event(
        target_kind="rule", target_signature="sig_rule", reviewed_entry_id=row["entry_id"],
        action="deactivate", actor="bob", decided_at=T2,
    )
    third = store.record_review_event(
        target_kind="rule", target_signature="sig_rule", reviewed_entry_id=row["entry_id"],
        action="activate", actor="carol", decided_at=T3,
    )

    assert first.supersedes == ""
    assert second.supersedes == first.event_id()
    assert third.supersedes == second.event_id()


def test_cross_signature_supersedes_is_rejected(tmp_path: Path) -> None:
    store = ResearchMemoryStore(tmp_path / "artifacts")
    row_a = _promote_rule(store, signature="sig_a")
    row_b = _promote_rule(store, signature="sig_b")

    event_a = store.record_review_event(
        target_kind="rule", target_signature="sig_a", reviewed_entry_id=row_a["entry_id"],
        action="activate", actor="alice", decided_at=T1,
    )
    # An explicit supersedes claim pointing at a DIFFERENT signature's event
    # must be rejected -- the chain is per-(kind, signature), never global.
    with pytest.raises(ValueError, match="same"):
        store.record_review_event(
            target_kind="rule", target_signature="sig_b", reviewed_entry_id=row_b["entry_id"],
            action="activate", actor="bob", decided_at=T2, supersedes=event_a.event_id(),
        )
    # A supersedes value that references NO event at all is equally rejected.
    with pytest.raises(ValueError, match="must reference an existing event"):
        store.record_review_event(
            target_kind="rule", target_signature="sig_b", reviewed_entry_id=row_b["entry_id"],
            action="activate", actor="bob", decided_at=T2, supersedes="f" * 64,
        )
    # Neither rejected attempt appended anything.
    assert store.rule_states().get("sig_b", "inactive") == "inactive"


def test_dangling_event_reviewed_entry_id_is_ignored_with_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # An event whose reviewed_entry_id never matched ANY row for its
    # signature (a forged or corrupted row, not merely a superseded one) is
    # ignored the same way a stale-superseded-row event is: it never counts
    # toward effective state, and the ignore is logged, not silent.
    store = ResearchMemoryStore(tmp_path / "artifacts")
    row = _promote_rule(store, signature="sig_dangling")
    bogus_entry_id = "0" * 64
    assert bogus_entry_id != row["entry_id"]

    store.record_review_event(
        target_kind="rule", target_signature="sig_dangling", reviewed_entry_id=bogus_entry_id,
        action="activate", actor="mallory", decided_at=T1,
    )
    with caplog.at_level("WARNING", logger="quant_forge.research_loop.memory"):
        states = store.rule_states()
    assert states.get("sig_dangling", "inactive") == "inactive"
    assert any("sig_dangling" in message and "no longer matches" in message for message in caplog.messages)

    # A genuine, correctly-bound event for the SAME signature still works
    # normally alongside the ignored dangling one.
    store.record_review_event(
        target_kind="rule", target_signature="sig_dangling", reviewed_entry_id=row["entry_id"],
        action="activate", actor="alice", decided_at=T2,
    )
    assert store.rule_states() == {"sig_dangling": "active"}


def test_row_content_change_lapses_activation_until_re_review(tmp_path: Path) -> None:
    # Item 2's headline scenario: once a promoted row is SUPERSEDED (new
    # observations promote a new version with a new entry_id), any event
    # bound to the PRIOR version stops counting -- the signature reverts to
    # unreviewed, not to whatever the old event said, until a human
    # re-reviews the new content.
    store = ResearchMemoryStore(tmp_path / "artifacts")
    row = _promote_rule(store, signature="sig_lapse")
    store.record_review_event(
        target_kind="rule", target_signature="sig_lapse", reviewed_entry_id=row["entry_id"],
        action="activate", actor="alice", decided_at=T1,
    )
    assert store.rule_states() == {"sig_lapse": "active"}

    # A new observation with a new window supersedes the row.
    store.record_observation(
        signature="sig_lapse", statement="rule statement for sig_lapse", run_id="sig_lapse-4",
        observed_at=T4, data_window="2025-01-01:2025-06-30",
    )
    store.promote_pending()
    new_row = store.resolve_signature_prefix("rule", "sig_lapse")
    assert new_row["entry_id"] != row["entry_id"]

    assert store.rule_states().get("sig_lapse", "inactive") == "inactive"

    # Re-reviewing against the NEW content re-activates it.
    store.record_review_event(
        target_kind="rule", target_signature="sig_lapse", reviewed_entry_id=new_row["entry_id"],
        action="activate", actor="bob", decided_at="2026-07-05T00:00:00+00:00",
    )
    assert store.rule_states() == {"sig_lapse": "active"}


# ---------------------------------------------------------------------------
# R3 rework item R3-2 (MINOR): honest dangling labels. rule_review_snapshot()
# (built from _rule_snapshot_unlocked()) must tell apart three genuinely
# different reasons an event fails to bind the CURRENT row: (i) it doesn't
# fail at all -- it binds the current row, active-eligible; (ii) it binds a
# REAL row this signature once had (rules.jsonl is append-only, so a
# superseded row's entry_id is still on disk) -- lapsed_pending_re_review,
# where the CLI's "row content changed" explanation is true by construction;
# (iii) it binds NO row this signature has EVER had -- a forged, corrupted,
# or wrong-signature copy-paste -- never_reviewed, not a false "content
# changed" claim, with a dangling-reference warning logged instead.
# ---------------------------------------------------------------------------


def test_snapshot_case_i_event_bound_to_current_row_is_active(tmp_path: Path) -> None:
    store = ResearchMemoryStore(tmp_path / "artifacts")
    row = _promote_rule(store, signature="sig_current")
    store.record_review_event(
        target_kind="rule", target_signature="sig_current", reviewed_entry_id=row["entry_id"],
        action="activate", actor="alice", decided_at=T1,
    )
    snapshot = store.rule_review_snapshot()
    assert snapshot["sig_current"]["state"] == "active"
    assert snapshot["sig_current"]["reviewed_entry_id"] == row["entry_id"]


def test_snapshot_case_ii_event_bound_to_a_superseded_row_is_lapsed(tmp_path: Path) -> None:
    # The event's reviewed_entry_id matches a REAL row this signature once
    # held, but a newer observation has since superseded it -- the content
    # genuinely changed since the review, so lapsed_pending_re_review (and
    # its "row content changed" label) is true by construction.
    store = ResearchMemoryStore(tmp_path / "artifacts")
    old_row = _promote_rule(store, signature="sig_lapsed")
    store.record_review_event(
        target_kind="rule", target_signature="sig_lapsed", reviewed_entry_id=old_row["entry_id"],
        action="activate", actor="alice", decided_at=T1,
    )
    assert store.rule_review_snapshot()["sig_lapsed"]["state"] == "active"

    store.record_observation(
        signature="sig_lapsed", statement="rule statement for sig_lapsed", run_id="sig_lapsed-4",
        observed_at=T4, data_window="2025-01-01:2025-06-30",
    )
    store.promote_pending()
    new_row = store.resolve_signature_prefix("rule", "sig_lapsed")
    assert new_row["entry_id"] != old_row["entry_id"]

    snapshot = store.rule_review_snapshot()
    assert snapshot["sig_lapsed"]["state"] == "lapsed_pending_re_review"


def test_snapshot_case_iii_event_bound_to_no_row_ever_is_never_reviewed_not_lapsed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # The event's reviewed_entry_id never matched ANY row this signature has
    # EVER had. Before R3-2 this was indistinguishable from case (ii) and
    # yielded lapsed_pending_re_review, falsely implying real content had
    # changed. It must instead read as never_reviewed -- no REAL review ever
    # bound to this signature -- with a warning logged for visibility.
    store = ResearchMemoryStore(tmp_path / "artifacts")
    row = _promote_rule(store, signature="sig_dangling_only")
    bogus_entry_id = "f" * 64
    assert bogus_entry_id != row["entry_id"]
    store.record_review_event(
        target_kind="rule", target_signature="sig_dangling_only", reviewed_entry_id=bogus_entry_id,
        action="activate", actor="mallory", decided_at=T1,
    )

    with caplog.at_level("WARNING", logger="quant_forge.research_loop.memory"):
        snapshot = store.rule_review_snapshot()
    assert snapshot["sig_dangling_only"]["state"] == "never_reviewed"
    assert any(
        "dangling" in message and "sig_dangling_only" in message and "never matched" in message
        for message in caplog.messages
    )


def test_snapshot_signature_with_both_a_lapsed_and_a_dangling_event_is_lapsed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # A signature can accumulate BOTH kinds of non-binding event over its
    # history: one genuinely bound to a row it once had (now superseded),
    # and, separately, a dangling one that never matched anything real. The
    # genuine review history wins the classification -- lapsed_pending_re_review,
    # which is true -- while the dangling event is still independently
    # flagged by its own warning; one bad reference does not erase a real
    # review's evidentiary weight, but it is not silently swallowed either.
    store = ResearchMemoryStore(tmp_path / "artifacts")
    old_row = _promote_rule(store, signature="sig_mixed")
    store.record_review_event(
        target_kind="rule", target_signature="sig_mixed", reviewed_entry_id=old_row["entry_id"],
        action="activate", actor="alice", decided_at=T1,
    )
    store.record_observation(
        signature="sig_mixed", statement="rule statement for sig_mixed", run_id="sig_mixed-4",
        observed_at=T4, data_window="2025-01-01:2025-06-30",
    )
    store.promote_pending()  # supersedes old_row: the activate event above is now lapsed, not current-bound

    bogus_entry_id = "e" * 64
    store.record_review_event(
        target_kind="rule", target_signature="sig_mixed", reviewed_entry_id=bogus_entry_id,
        action="deactivate", actor="mallory", decided_at=T2,
    )

    with caplog.at_level("WARNING", logger="quant_forge.research_loop.memory"):
        snapshot = store.rule_review_snapshot()
    assert snapshot["sig_mixed"]["state"] == "lapsed_pending_re_review"
    assert any("dangling" in message and "sig_mixed" in message for message in caplog.messages)


def test_cli_lists_honest_labels_for_lapsed_vs_dangling_never_reviewed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # End-to-end confirmation through the actual CLI surface (R3-2's stated
    # goal: "CLI labels must follow"): a genuinely lapsed signature shows the
    # "row content changed" wording, and a dangling-only signature shows the
    # plain pending wording -- never the reverse -- in the SAME listing.
    artifact_root = tmp_path / "artifacts"
    root_arg = ["--artifact-root", str(artifact_root)]
    store = ResearchMemoryStore(artifact_root)

    old_row = _promote_rule(store, signature="sig_cli_lapsed")
    store.record_review_event(
        target_kind="rule", target_signature="sig_cli_lapsed", reviewed_entry_id=old_row["entry_id"],
        action="activate", actor="alice", decided_at=T1,
    )
    store.record_observation(
        signature="sig_cli_lapsed", statement="rule statement for sig_cli_lapsed", run_id="sig_cli_lapsed-4",
        observed_at=T4, data_window="2025-01-01:2025-06-30",
    )
    store.promote_pending()  # lapses the activation above

    dangling_row = _promote_rule(store, signature="sig_cli_dangling")
    store.record_review_event(
        target_kind="rule", target_signature="sig_cli_dangling", reviewed_entry_id="d" * 64,
        action="activate", actor="mallory", decided_at=T1,
    )
    assert "d" * 64 != dangling_row["entry_id"]

    exit_code = cli_main.main(["memory", "rules", "list", *root_arg])
    listing = capsys.readouterr().out
    assert exit_code == 0
    assert "rule statement for sig_cli_lapsed" in listing
    assert "rule statement for sig_cli_dangling" in listing

    lapsed_line = next(line for line in listing.splitlines() if "sig_cli_lapsed" in line)
    dangling_line = next(line for line in listing.splitlines() if "sig_cli_dangling" in line)
    assert "row content changed" in lapsed_line
    assert "row content changed" not in dangling_line
    assert "pending -- lower tiers silenced" in dangling_line


# ---------------------------------------------------------------------------
# P4a rework item 7: trailing-line quarantine vs interior corruption.
# ---------------------------------------------------------------------------


def test_trailing_corrupt_line_is_quarantined_not_raised(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    store = ResearchMemoryStore(tmp_path / "artifacts")
    row = _promote_rule(store)
    store.record_review_event(
        target_kind="rule", target_signature="sig_rule", reviewed_entry_id=row["entry_id"],
        action="activate", actor="alice", decided_at=T1,
    )
    with store.review_events_path.open("a", encoding="utf-8") as handle:
        handle.write("}{ not valid json at all {\n")

    with caplog.at_level("WARNING", logger="quant_forge.research_loop.memory"):
        events = store._read_review_events_unlocked()  # noqa: SLF001
    assert len(events) == 1
    assert any("quarantining" in message for message in caplog.messages)
    # The public read path is equally tolerant.
    assert store.rule_states() == {"sig_rule": "active"}


def test_interior_corrupt_line_still_raises(tmp_path: Path) -> None:
    store = ResearchMemoryStore(tmp_path / "artifacts")
    row = _promote_rule(store)
    store.record_review_event(
        target_kind="rule", target_signature="sig_rule", reviewed_entry_id=row["entry_id"],
        action="activate", actor="alice", decided_at=T1,
    )
    good_content = store.review_events_path.read_text(encoding="utf-8")
    store.review_events_path.write_text("}{ not valid json at all {\n" + good_content, encoding="utf-8")

    with pytest.raises(ValueError, match="not the trailing line"):
        store._read_review_events_unlocked()  # noqa: SLF001


def test_semantically_invalid_line_is_skipped_with_a_warning(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    # Valid JSON, but fails MemoryReviewEvent's own schema/action/kind
    # validation (an unknown action here) -- skipped, never raised.
    store = ResearchMemoryStore(tmp_path / "artifacts")
    row = _promote_rule(store)
    store.record_review_event(
        target_kind="rule", target_signature="sig_rule", reviewed_entry_id=row["entry_id"],
        action="activate", actor="alice", decided_at=T1,
    )
    bad_row = {
        "event_id": "b" * 64,
        "schema_version": RESEARCH_MEMORY_REVIEW_SCHEMA_VERSION,
        "target_kind": "rule",
        "target_signature": "sig_rule",
        "reviewed_entry_id": row["entry_id"],
        "action": "not_a_real_action",
        "actor": "mallory",
        "rationale": "",
        "decided_at": T2,
        "supersedes": "",
    }
    with store.review_events_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(bad_row, sort_keys=True) + "\n")

    with caplog.at_level("WARNING", logger="quant_forge.research_loop.memory"):
        events = store._read_review_events_unlocked()  # noqa: SLF001
    assert len(events) == 1
    assert any("semantically-invalid" in message for message in caplog.messages)


# ---------------------------------------------------------------------------
# R2 rework item R2-2: readback integrity -- recomputed fingerprint mismatch
# and missing/non-current schema_version are rejected, never defaulted.
# ---------------------------------------------------------------------------


def test_wrong_event_id_line_is_ignored_with_a_warning(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    # A line that is valid JSON AND passes MemoryReviewEvent's own field
    # validation, but whose stored event_id does NOT match the recomputed
    # content fingerprint (a tampered or corrupted single field -- e.g. the
    # actor field silently altered without the id being recomputed), is
    # rejected rather than trusted.
    store = ResearchMemoryStore(tmp_path / "artifacts")
    row = _promote_rule(store)
    genuine = store.record_review_event(
        target_kind="rule", target_signature="sig_rule", reviewed_entry_id=row["entry_id"],
        action="activate", actor="alice", decided_at=T1,
    )
    rows = _read_jsonl(store.review_events_path)
    assert len(rows) == 1
    tampered = dict(rows[0])
    tampered["actor"] = "mallory"  # content changed...
    # ...but event_id is NOT recomputed, so it now mismatches.
    store.review_events_path.write_text(json.dumps(tampered, sort_keys=True) + "\n", encoding="utf-8")

    with caplog.at_level("WARNING", logger="quant_forge.research_loop.memory"):
        events = store._read_review_events_unlocked()  # noqa: SLF001
    assert events == ()
    assert any("event_id" in message and "does not match" in message for message in caplog.messages)
    # The public read path is equally protected: the tampered "activation"
    # is not merely rejected in isolation, it also means the signature
    # reverts to unreviewed rather than trusting the tampered actor's claim.
    assert store.rule_states().get("sig_rule", "inactive") == "inactive"
    assert genuine.event_id() == canonical_fingerprint(genuine.to_dict())  # sanity: the ORIGINAL id was correct


def test_missing_schema_version_line_is_ignored_never_defaulted(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store = ResearchMemoryStore(tmp_path / "artifacts")
    row = _promote_rule(store)
    store.record_review_event(
        target_kind="rule", target_signature="sig_rule", reviewed_entry_id=row["entry_id"],
        action="activate", actor="alice", decided_at=T1,
    )
    rows = _read_jsonl(store.review_events_path)
    stripped = dict(rows[0])
    del stripped["schema_version"]
    store.review_events_path.write_text(json.dumps(stripped, sort_keys=True) + "\n", encoding="utf-8")

    with caplog.at_level("WARNING", logger="quant_forge.research_loop.memory"):
        events = store._read_review_events_unlocked()  # noqa: SLF001
    assert events == ()
    assert any("schema_version" in message for message in caplog.messages)
    assert store.rule_states().get("sig_rule", "inactive") == "inactive"


def test_non_current_schema_version_line_is_ignored_never_defaulted(tmp_path: Path) -> None:
    store = ResearchMemoryStore(tmp_path / "artifacts")
    row = _promote_rule(store)
    store.record_review_event(
        target_kind="rule", target_signature="sig_rule", reviewed_entry_id=row["entry_id"],
        action="activate", actor="alice", decided_at=T1,
    )
    rows = _read_jsonl(store.review_events_path)
    stale = dict(rows[0])
    stale["schema_version"] = "qf.memory_review.v0"  # a real, but NOT current, version string
    store.review_events_path.write_text(json.dumps(stale, sort_keys=True) + "\n", encoding="utf-8")

    events = store._read_review_events_unlocked()  # noqa: SLF001
    assert events == ()


# ---------------------------------------------------------------------------
# R2 rework item R2-5: uniform tail tolerance across every memory JSONL file
# kind, not just review events -- trailing-only recovery, interior raises.
# ---------------------------------------------------------------------------


def test_observations_jsonl_trailing_corruption_is_tolerated(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    store = ResearchMemoryStore(tmp_path / "artifacts")
    store.record_observation(signature="sig_obs", statement="obs statement", run_id="rd-1", observed_at=T1)
    with store.observations_path.open("a", encoding="utf-8") as handle:
        handle.write("}{ not valid json at all {\n")

    with caplog.at_level("WARNING", logger="quant_forge.research_loop.memory"):
        observations = store._read_observations()  # noqa: SLF001
    assert len(observations) == 1
    assert any("quarantining" in message for message in caplog.messages)


def test_observations_jsonl_interior_corruption_still_raises(tmp_path: Path) -> None:
    store = ResearchMemoryStore(tmp_path / "artifacts")
    store.record_observation(signature="sig_obs", statement="obs statement", run_id="rd-1", observed_at=T1)
    good = store.observations_path.read_text(encoding="utf-8")
    store.observations_path.write_text("}{ not valid json at all {\n" + good, encoding="utf-8")

    with pytest.raises(ValueError, match="not the trailing line"):
        store._read_observations()  # noqa: SLF001


def test_rules_jsonl_trailing_corruption_is_tolerated(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    store = ResearchMemoryStore(tmp_path / "artifacts")
    _promote_rule(store)
    with store.path_for("rule").open("a", encoding="utf-8") as handle:
        handle.write("}{ not valid json at all {\n")

    with caplog.at_level("WARNING", logger="quant_forge.research_loop.memory"):
        rows = store.list_promoted("rule")
    assert len(rows) == 1
    assert any("quarantining" in message for message in caplog.messages)


def test_rules_jsonl_interior_corruption_still_raises(tmp_path: Path) -> None:
    store = ResearchMemoryStore(tmp_path / "artifacts")
    _promote_rule(store)
    good = store.path_for("rule").read_text(encoding="utf-8")
    store.path_for("rule").write_text("}{ not valid json at all {\n" + good, encoding="utf-8")

    with pytest.raises(ValueError, match="not the trailing line"):
        store.list_promoted("rule")


def test_findings_and_failures_jsonl_trailing_corruption_is_tolerated(tmp_path: Path) -> None:
    store = ResearchMemoryStore(tmp_path / "artifacts")
    _promote_finding(store, signature="sig_finding_tail")
    with store.path_for("finding").open("a", encoding="utf-8") as handle:
        handle.write("}{ not valid json at all {\n")
    assert len(store.list_promoted("finding")) == 1

    store.record_observation(
        signature="sig_failure_tail", statement="failure statement", run_id="rd-f1", observed_at=T1,
        failure_class="gate_blocked",
    )
    store.record_observation(
        signature="sig_failure_tail", statement="failure statement", run_id="rd-f2", observed_at=T2,
        failure_class="gate_blocked",
    )
    store.promote_pending()
    with store.path_for("failure").open("a", encoding="utf-8") as handle:
        handle.write("}{ not valid json at all {\n")
    assert len(store.list_promoted("failure")) == 1


# ---------------------------------------------------------------------------
# R3 rework item R3-1 (MAJOR): append-safe quarantine on the WRITE side.
# _read_jsonl's trailing tolerance (R2-5 above) leaves a torn tail ON DISK;
# without a write-side repair the very NEXT blind append either merges into
# the torn bytes (the new row is silently lost on the following read -- the
# reviewer's "lost retry" probe) or, once a further line follows, turns the
# merged garbage into INTERIOR corruption, which _read_jsonl() raises on (a
# promote_pending() outage -- the reviewer's "next-append-raises" probe).
# _repair_torn_tail() runs from _append_jsonl(), inside the caller's existing
# advisory-lock hold, before every append.
# ---------------------------------------------------------------------------


def test_torn_tail_is_repaired_before_the_next_append(tmp_path: Path) -> None:
    store = ResearchMemoryStore(tmp_path / "artifacts")
    store.record_observation(signature="sig_a", statement="first obs", run_id="rd-a", observed_at=T1)

    # A writer dies mid-append: the next line is only partially flushed --
    # no closing brace, no trailing newline.
    with store.observations_path.open("a", encoding="utf-8") as handle:
        handle.write('{"signature": "sig_torn", "statement": "incomplete wri')
    assert not store.observations_path.read_text(encoding="utf-8").endswith("\n")

    # The next append -- a brand-new, unrelated observation -- must repair
    # the torn tail FIRST, then land its own data cleanly.
    store.record_observation(
        signature="sig_retry", statement="the retry that must not be lost", run_id="rd-retry", observed_at=T2
    )

    content = store.observations_path.read_text(encoding="utf-8")
    assert content.endswith("\n")
    lines = [line for line in content.splitlines() if line.strip()]
    parsed = [json.loads(line) for line in lines]  # every remaining line parses cleanly
    assert [row["signature"] for row in parsed] == ["sig_a", "sig_retry"]

    # The torn fragment is preserved for forensics, not silently destroyed.
    sidecar = _quarantine_path_for(store.observations_path)
    assert sidecar.exists()
    assert "sig_torn" in sidecar.read_text(encoding="utf-8")

    # The read path every real caller uses is equally clean.
    observations = store._read_observations()  # noqa: SLF001
    assert [o.signature for o in observations] == ["sig_a", "sig_retry"]

    # promote_pending() succeeds cleanly -- no outage.
    store.record_observation(
        signature="sig_retry", statement="the retry that must not be lost", run_id="rd-retry-2", observed_at=T3
    )
    store.promote_pending()
    assert store.resolve_signature_prefix("finding", "sig_retry")["signature"] == "sig_retry"


def test_reviewers_two_failure_probe_sequence_is_dead(tmp_path: Path) -> None:
    """The exact two-failure probe from the R3 review: (1) "lost retry" -- a
    torn trailing line already on disk silently swallows the very next
    append (the torn fragment and the new line merge into ONE still-torn
    trailing line, so the new row never comes back on a subsequent read);
    (2) "next-append-raises" -- a FURTHER append turns that merged garbage
    into INTERIOR corruption, which _read_jsonl() raises on (a
    promote_pending()-equivalent outage for review events). Both must be
    dead: no exception anywhere in the sequence, and every appended
    signature is recoverable afterwards.
    """
    store = ResearchMemoryStore(tmp_path / "artifacts")
    row_a = _promote_rule(store, signature="sig_probe_a")
    store.record_review_event(
        target_kind="rule", target_signature="sig_probe_a", reviewed_entry_id=row_a["entry_id"],
        action="activate", actor="alice", decided_at=T1,
    )

    # A writer dies mid-append against review_events_path specifically.
    with store.review_events_path.open("a", encoding="utf-8") as handle:
        handle.write('{"event_id": "deadbeef", "action": "activate_but_never_fin')

    # Probe 1 ("lost retry"): the next append to THIS file must not be
    # silently swallowed into the torn bytes.
    row_b = _promote_rule(store, signature="sig_probe_b")
    store.record_review_event(
        target_kind="rule", target_signature="sig_probe_b", reviewed_entry_id=row_b["entry_id"],
        action="activate", actor="bob", decided_at=T2,
    )
    after_first_retry = store._read_review_events_unlocked()  # noqa: SLF001
    assert {event.target_signature for event in after_first_retry} == {"sig_probe_a", "sig_probe_b"}

    # Probe 2 ("next-append-raises"): a further append -- what would, pre-
    # fix, complete the merge into a genuinely interior-corrupt line -- must
    # not raise either.
    row_c = _promote_rule(store, signature="sig_probe_c")
    store.record_review_event(
        target_kind="rule", target_signature="sig_probe_c", reviewed_entry_id=row_c["entry_id"],
        action="activate", actor="carol", decided_at=T3,
    )
    after_second_retry = store._read_review_events_unlocked()  # noqa: SLF001
    assert {event.target_signature for event in after_second_retry} == {
        "sig_probe_a", "sig_probe_b", "sig_probe_c",
    }

    # And the read path every real caller uses (rule_states -> context
    # builder's steering channel) is equally healthy: no lost activations.
    assert store.rule_states() == {"sig_probe_a": "active", "sig_probe_b": "active", "sig_probe_c": "active"}


def test_repair_torn_tail_alone_leaves_a_clean_file_if_the_append_never_happens(tmp_path: Path) -> None:
    # _append_jsonl() calls _repair_torn_tail() and THEN performs its own
    # write as two separate filesystem operations. If the process dies in
    # between -- after the repair's truncate, before the caller's new data
    # is appended -- the file left behind must be immediately clean and
    # valid on its own, never torn, with the pre-existing good data intact.
    store = ResearchMemoryStore(tmp_path / "artifacts")
    store.record_observation(signature="sig_a", statement="obs a", run_id="rd-a", observed_at=T1)
    with store.observations_path.open("a", encoding="utf-8") as handle:
        handle.write('{"signature": "torn", "statem')

    _repair_torn_tail(store.observations_path)  # the "crash" lands exactly HERE -- no append follows

    content = store.observations_path.read_text(encoding="utf-8")
    assert content.endswith("\n")
    lines = [line for line in content.splitlines() if line.strip()]
    for line in lines:
        json.loads(line)  # every remaining line parses -- no torn or interior corruption
    assert len(lines) == 1  # only the pre-existing good row; the torn one never lands half-written

    # The store keeps working from this state with no special recovery step.
    observations = store._read_observations()  # noqa: SLF001
    assert [o.signature for o in observations] == ["sig_a"]


def test_valid_json_tail_missing_only_its_newline_is_healed_not_quarantined(tmp_path: Path) -> None:
    # A tail that IS valid JSON -- merely missing its trailing newline
    # terminator -- is not the corruption shape _read_jsonl()'s tolerance
    # exists for (splitlines() accepts it as a normal row either way). The
    # write-side repair must not misclassify and quarantine a perfectly
    # good row just because the writer stopped one byte early.
    store = ResearchMemoryStore(tmp_path / "artifacts")
    store.record_observation(signature="sig_x", statement="obs x", run_id="rd-x", observed_at=T1)
    content = store.observations_path.read_text(encoding="utf-8")
    assert content.endswith("\n")
    store.observations_path.write_text(content.rstrip("\n"), encoding="utf-8")  # strip ONLY the terminator

    store.record_observation(signature="sig_y", statement="obs y", run_id="rd-y", observed_at=T2)

    sidecar = _quarantine_path_for(store.observations_path)
    assert not sidecar.exists(), "a fully valid row must never be quarantined"
    observations = store._read_observations()  # noqa: SLF001
    assert [o.signature for o in observations] == ["sig_x", "sig_y"]


def test_repairing_the_same_torn_tail_twice_does_not_duplicate_the_sidecar_fragment(tmp_path: Path) -> None:
    # Idempotency: if the process dies AFTER the sidecar write but BEFORE
    # the truncate (or the repair helper otherwise runs twice against the
    # same on-disk state), the fragment must not be appended to the sidecar
    # a second time.
    store = ResearchMemoryStore(tmp_path / "artifacts")
    store.record_observation(signature="sig_y", statement="obs y", run_id="rd-y", observed_at=T1)
    with store.observations_path.open("a", encoding="utf-8") as handle:
        handle.write('{"signature": "torn_y", "bad')

    _repair_torn_tail(store.observations_path)
    sidecar = _quarantine_path_for(store.observations_path)
    first_sidecar_content = sidecar.read_text(encoding="utf-8")

    # Re-running the repair against the now-clean file is a pure no-op.
    _repair_torn_tail(store.observations_path)
    assert sidecar.read_text(encoding="utf-8") == first_sidecar_content

    # Re-appending the IDENTICAL torn fragment (simulating a crash that
    # re-lands the same bytes on retry) still does not duplicate it.
    with store.observations_path.open("ab") as handle:
        handle.write(b'{"signature": "torn_y", "bad')
    _repair_torn_tail(store.observations_path)
    assert sidecar.read_text(encoding="utf-8") == first_sidecar_content


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
    # "signature_prefix" column is intentionally truncated to 12 chars. A
    # pending row's state is spelled out (P4a rework item 1: reaching the
    # rule tier at all already silences lower tiers, not just activation).
    exit_code = cli_main.main(["memory", "rules", "list", *root_arg])
    listed_all = capsys.readouterr().out
    assert exit_code == 0
    assert listed_all.count("pending -- lower tiers silenced") == 2
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


def test_cli_memory_rules_unretire(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # P4a rework item 8: CLI parity for the store's unretire action (the
    # store method already existed; only the CLI surface was missing).
    artifact_root = tmp_path / "artifacts"
    root_arg = ["--artifact-root", str(artifact_root)]
    store = ResearchMemoryStore(artifact_root)
    _promote_finding(store, signature="sig_unretire_me")

    _cli_json(capsys, ["memory", "rules", "retire", "finding", "sig_unretire_me", "--actor", "erin", *root_arg])
    assert store.retired_signatures("finding") == frozenset({"sig_unretire_me"})

    unretired = _cli_json(
        capsys, ["memory", "rules", "unretire", "finding", "sig_unretire_me", "--actor", "frank", *root_arg]
    )
    assert unretired["action"] == "unretire"
    assert store.retired_signatures("finding") == frozenset()

    # unretire only accepts finding|failure at the argparse level, same as
    # retire.
    with pytest.raises(SystemExit):
        cli_main.main(["memory", "rules", "unretire", "rule", "sig_unretire_me", "--actor", "erin", *root_arg])


def test_cli_atomic_race_two_threads_activating_the_same_prefix(tmp_path: Path) -> None:
    # P4a rework item 8: resolve_validate_append (which the CLI now uses)
    # closes the resolve-then-append TOCTOU window. Two THREADS (see
    # test_cli_atomic_race_two_real_processes_activating_the_same_prefix
    # below for a genuine two-OS-process version, R2-7) racing to activate
    # the SAME unambiguous prefix must never raise, never leave a
    # torn/malformed line, and must leave the store in a single well-defined
    # end state decided by the store's own append-order lock -- not by
    # whichever thread the OS scheduler happened to favor at the Python
    # level producing inconsistent output.
    artifact_root = tmp_path / "artifacts"
    store = ResearchMemoryStore(artifact_root)
    row = _promote_rule(store, signature="sig_race")

    results: list[int] = []
    errors: list[str] = []
    barrier = threading.Barrier(2)

    def activate(actor: str) -> None:
        try:
            barrier.wait(timeout=30)
        except threading.BrokenBarrierError:
            return
        exit_code = cli_main.main(
            [
                "memory", "rules", "activate", "sig_race", "--actor", actor,
                "--artifact-root", str(artifact_root),
            ]
        )
        results.append(exit_code)

    threads = [threading.Thread(target=activate, args=(actor,)) for actor in ("alice", "bob")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    for thread in threads:
        assert not thread.is_alive()

    assert errors == []
    # Both CLI invocations complete cleanly (no crash from either side of
    # the race); the store resolves the ordering deterministically via its
    # own lock, not by which thread "wins" at the Python level.
    assert results == [0, 0]

    # No torn state: every line in activations.jsonl parses as JSON, and the
    # store lands in a single well-defined active state for sig_race.
    for line in store.review_events_path.read_text(encoding="utf-8").splitlines():
        json.loads(line)
    assert store.rule_states() == {"sig_race": "active"}
    # Both events are bound to the SAME (only) live row -- no fork.
    events = [event for event in store._read_review_events_unlocked() if event.target_signature == "sig_race"]  # noqa: SLF001
    assert all(event.reviewed_entry_id == row["entry_id"] for event in events)
    assert len(events) in (1, 2)  # 2 distinct actors -> 2 distinct events, unless a timestamp collision deduped them


def test_cli_atomic_race_two_real_processes_activating_the_same_prefix(tmp_path: Path) -> None:
    # R2 rework item R2-7: a REAL two-OS-process race, not threads, on
    # resolve_validate_append. The threaded test above proves the Python-
    # level call sequencing is correct; it does NOT prove the underlying
    # fcntl.flock actually serializes independent OS processes (two threads
    # share one process's file-descriptor table and GIL, which is a weaker
    # claim than "two processes"). This launches the ACTUAL `qf` CLI as two
    # separate subprocesses racing to activate the same unambiguous prefix:
    # no torn/malformed line, and the store lands in one well-defined final
    # state regardless of which process the OS scheduled first.
    artifact_root = tmp_path / "artifacts"
    store = ResearchMemoryStore(artifact_root)
    row = _promote_rule(store, signature="sig_real_race")

    repo_src = Path(__file__).resolve().parents[1] / "src"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(repo_src)

    def _cli_command(actor: str) -> list[str]:
        return [
            sys.executable,
            "-m",
            "quant_forge.apps.cli.main",
            "memory",
            "rules",
            "activate",
            "sig_real_race",
            "--actor",
            actor,
            "--artifact-root",
            str(artifact_root),
        ]

    processes = [
        subprocess.Popen(  # noqa: S603 - fixed argv, no shell, test-controlled interpreter path
            _cli_command(actor), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        for actor in ("alice", "bob")
    ]
    # communicate() (not wait()) avoids the classic PIPE-buffer deadlock if
    # either process writes enough output to fill the pipe before exiting.
    outcomes = [proc.communicate(timeout=30) for proc in processes]
    exit_codes = [proc.returncode for proc in processes]

    # Both real OS processes complete cleanly; the store's own lock (a real
    # fcntl.flock, not merely Python-level thread sequencing) resolves the
    # ordering deterministically regardless of which process the OS
    # scheduled first -- no crash on either side of the race.
    assert exit_codes == [0, 0], list(zip(exit_codes, outcomes, strict=True))

    # No torn state: every line in activations.jsonl parses as JSON.
    for line in store.review_events_path.read_text(encoding="utf-8").splitlines():
        json.loads(line)
    assert store.rule_states() == {"sig_real_race": "active"}
    events = [
        event for event in store._read_review_events_unlocked() if event.target_signature == "sig_real_race"  # noqa: SLF001
    ]
    assert all(event.reviewed_entry_id == row["entry_id"] for event in events)
    assert len(events) in (1, 2)  # 2 distinct actors -> 2 distinct events, unless a timestamp collision deduped them

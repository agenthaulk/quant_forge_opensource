"""Lane G regression tests: immutable research goal artifacts + audit log."""

from __future__ import annotations

import json
from pathlib import Path
import re

import pytest

import quant_forge.apps.cli.main as cli_main
from quant_forge.research_loop.goals import (
    GOAL_AUDIT_SCHEMA_VERSION,
    GOAL_SCHEMA_VERSION,
    GoalCompletionError,
    GoalCriterion,
    ResearchGoalStore,
)

CREATED_AT = "2026-07-07T00:00:00+00:00"
RECORDED_AT = "2026-07-07T01:00:00+00:00"
RUNTIME_HASH = "a" * 64


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _store(tmp_path: Path) -> ResearchGoalStore:
    return ResearchGoalStore(tmp_path / "artifacts")


def _create_goal(store: ResearchGoalStore, **overrides):
    payload = {
        "objective": "Improve small-cap momentum robustness",
        "criteria": (
            GoalCriterion(criterion_id="c1", text="OOS rank ICIR is reportable"),
            GoalCriterion(criterion_id="c2", text="No embargo violations in evaluation"),
            GoalCriterion(criterion_id="c3", text="Turnover stays within budget", required=False),
        ),
        "seed_factor_id": "FTR_DEMO_SMALL_CAP",
        "runtime_config_hash": RUNTIME_HASH,
        "created_at": CREATED_AT,
    }
    payload.update(overrides)
    return store.create_goal(**payload)


def _evidence(store: ResearchGoalStore, rel: str = "evaluations/evidence.json") -> str:
    path = store.artifact_root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"ok": true}\n', encoding="utf-8")
    return rel


# ---------------------------------------------------------------------------
# Artifact schema, goal_id shape, immutability
# ---------------------------------------------------------------------------


def test_goal_artifact_schema_and_immutability(tmp_path: Path) -> None:
    store = _store(tmp_path)
    goal = _create_goal(store)

    assert re.fullmatch(r"[a-z0-9][a-z0-9-]*-[0-9a-f]{12}", goal.goal_id)
    path = store.goal_path(goal.goal_id)
    assert path == store.artifact_root / "research_goals" / f"{goal.goal_id}.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["schema_version"] == GOAL_SCHEMA_VERSION
    assert raw["objective"] == "Improve small-cap momentum robustness"
    assert raw["seed_factor_id"] == "FTR_DEMO_SMALL_CAP"
    assert raw["runtime_config_hash"] == RUNTIME_HASH
    assert raw["created_at"] == CREATED_AT
    assert raw["status"] == "active"
    assert raw["evidence_refs"] == []
    assert [entry["criterion_id"] for entry in raw["criteria"]] == ["c1", "c2", "c3"]
    assert [entry["required"] for entry in raw["criteria"]] == [True, True, False]

    # Immutable: recreating the identical goal must refuse to overwrite.
    with pytest.raises(FileExistsError):
        _create_goal(store)

    # Audit activity never rewrites the goal artifact itself.
    bytes_before = path.read_bytes()
    rel = _evidence(store)
    store.append_audit(goal.goal_id, criterion_id="c1", result="satisfied", evidence_refs=(rel,), recorded_at=RECORDED_AT)
    assert path.read_bytes() == bytes_before


def test_goal_validation_rejects_bad_inputs(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(ValueError):
        _create_goal(store, objective="   ")
    with pytest.raises(ValueError):
        _create_goal(store, criteria=())
    with pytest.raises(ValueError):
        _create_goal(
            store,
            criteria=(
                GoalCriterion(criterion_id="c1", text="one"),
                GoalCriterion(criterion_id="c1", text="duplicate id"),
            ),
        )
    with pytest.raises(ValueError):
        _create_goal(store, runtime_config_hash="not-a-hash")
    with pytest.raises(ValueError):
        _create_goal(store, seed_factor_id="  ")
    # Terminal statuses are not creatable states.
    with pytest.raises(ValueError):
        _create_goal(store, status="complete")


def test_naive_created_at_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(ValueError, match="timezone-aware"):
        _create_goal(store, created_at="2026-07-07T00:00:00")
    goal = _create_goal(store)
    rel = _evidence(store)
    with pytest.raises(ValueError, match="timezone-aware"):
        store.append_audit(
            goal.goal_id,
            criterion_id="c1",
            result="satisfied",
            evidence_refs=(rel,),
            recorded_at="2026-07-07T01:00:00",
        )


# ---------------------------------------------------------------------------
# Audit rows: evidence existence, relative paths, append-only log
# ---------------------------------------------------------------------------


def test_satisfied_audit_requires_existing_evidence(tmp_path: Path) -> None:
    store = _store(tmp_path)
    goal = _create_goal(store)

    with pytest.raises(ValueError, match="at least one evidence ref"):
        store.append_audit(goal.goal_id, criterion_id="c1", result="satisfied", recorded_at=RECORDED_AT)
    with pytest.raises(ValueError, match="do not exist under artifact_root"):
        store.append_audit(
            goal.goal_id,
            criterion_id="c1",
            result="satisfied",
            evidence_refs=("evaluations/missing.json",),
            recorded_at=RECORDED_AT,
        )

    rel = _evidence(store)
    row = store.append_audit(
        goal.goal_id, criterion_id="c1", result="satisfied", evidence_refs=(rel,), recorded_at=RECORDED_AT
    )
    assert row["schema_version"] == GOAL_AUDIT_SCHEMA_VERSION
    assert row["row_type"] == "criterion_audit"
    assert row["evidence_refs"] == [rel]

    # Evidence refs must stay relative to artifact_root: no absolute, no traversal.
    with pytest.raises(ValueError):
        store.append_audit(
            goal.goal_id,
            criterion_id="c2",
            result="satisfied",
            evidence_refs=(str(store.artifact_root / rel),),
            recorded_at=RECORDED_AT,
        )
    with pytest.raises(ValueError):
        store.append_audit(
            goal.goal_id,
            criterion_id="c2",
            result="satisfied",
            evidence_refs=("../outside.json",),
            recorded_at=RECORDED_AT,
        )
    # Unknown criterion ids are rejected.
    with pytest.raises(ValueError, match="unknown criterion"):
        store.append_audit(
            goal.goal_id, criterion_id="c9", result="not_satisfied", recorded_at=RECORDED_AT
        )


def test_audit_log_is_append_only(tmp_path: Path) -> None:
    store = _store(tmp_path)
    goal = _create_goal(store)
    rel = _evidence(store)
    log_path = store.audit_log_path(goal.goal_id)

    store.append_audit(goal.goal_id, criterion_id="c1", result="satisfied", evidence_refs=(rel,), recorded_at=RECORDED_AT)
    content_after_first = log_path.read_text(encoding="utf-8")
    store.append_audit(goal.goal_id, criterion_id="c2", result="not_applicable_user_accepted", recorded_at=RECORDED_AT)
    content_after_second = log_path.read_text(encoding="utf-8")
    # Earlier content is a byte-for-byte prefix of the later file.
    assert content_after_second.startswith(content_after_first)

    store.complete_goal(goal.goal_id, recorded_at=RECORDED_AT)
    assert log_path.read_text(encoding="utf-8").startswith(content_after_second)
    rows = _read_jsonl(log_path)
    assert [row["row_type"] for row in rows] == ["criterion_audit", "criterion_audit", "status_transition"]
    assert rows[-1]["from_status"] == "active"
    assert rows[-1]["to_status"] == "complete"

    # A terminal goal's audit log is closed.
    with pytest.raises(ValueError, match="closed"):
        store.append_audit(goal.goal_id, criterion_id="c1", result="not_satisfied", recorded_at=RECORDED_AT)


# ---------------------------------------------------------------------------
# Completion rule
# ---------------------------------------------------------------------------


def test_completion_blocked_without_full_required_coverage(tmp_path: Path) -> None:
    store = _store(tmp_path)
    goal = _create_goal(store)
    rel = _evidence(store)
    goal_bytes = store.goal_path(goal.goal_id).read_bytes()

    # No audit rows at all.
    with pytest.raises(GoalCompletionError, match="c1"):
        store.complete_goal(goal.goal_id, recorded_at=RECORDED_AT)

    # One of two required criteria covered: still blocked, and the error names c2.
    store.append_audit(goal.goal_id, criterion_id="c1", result="satisfied", evidence_refs=(rel,), recorded_at=RECORDED_AT)
    with pytest.raises(GoalCompletionError, match="c2"):
        store.complete_goal(goal.goal_id, recorded_at=RECORDED_AT)

    # Latest row wins: a not_satisfied row on c2 keeps completion blocked.
    store.append_audit(goal.goal_id, criterion_id="c2", result="not_satisfied", recorded_at=RECORDED_AT)
    with pytest.raises(GoalCompletionError, match="not_satisfied"):
        store.complete_goal(goal.goal_id, recorded_at=RECORDED_AT)

    # The failed attempts changed nothing: no status row, goal file untouched.
    assert store.effective_status(goal.goal_id) == "active"
    assert store.goal_path(goal.goal_id).read_bytes() == goal_bytes

    # Cover c2 with a completion-eligible result; the optional c3 never needs one.
    store.append_audit(
        goal.goal_id, criterion_id="c2", result="satisfied_with_caveat", evidence_refs=(rel,), recorded_at=RECORDED_AT
    )
    row = store.complete_goal(goal.goal_id, recorded_at=RECORDED_AT)
    assert row["to_status"] == "complete"
    assert store.effective_status(goal.goal_id) == "complete"
    # Immutable base artifact still says "active"; status lives in the log.
    assert store.goal_path(goal.goal_id).read_bytes() == goal_bytes

    # Completing twice is refused.
    with pytest.raises(GoalCompletionError, match="already complete"):
        store.complete_goal(goal.goal_id, recorded_at=RECORDED_AT)


def test_completion_reverifies_evidence_on_disk(tmp_path: Path) -> None:
    store = _store(tmp_path)
    goal = _create_goal(store)
    rel = _evidence(store)
    store.append_audit(goal.goal_id, criterion_id="c1", result="satisfied", evidence_refs=(rel,), recorded_at=RECORDED_AT)
    store.append_audit(goal.goal_id, criterion_id="c2", result="not_applicable_user_accepted", recorded_at=RECORDED_AT)

    # Evidence disappearing after the audit row must block completion (FP-2).
    (store.artifact_root / rel).unlink()
    with pytest.raises(GoalCompletionError, match="exists under artifact_root"):
        store.complete_goal(goal.goal_id, recorded_at=RECORDED_AT)

    _evidence(store, rel)
    assert store.complete_goal(goal.goal_id, recorded_at=RECORDED_AT)["to_status"] == "complete"


def test_all_na_required_criteria_cannot_complete(tmp_path: Path) -> None:
    # FP-2 (softer vacuous-completion variant): a goal whose EVERY required
    # criterion is not_applicable_user_accepted has zero on-disk evidence and
    # must not complete, even though every latest result is completion-eligible.
    store = _store(tmp_path)
    goal = _create_goal(store)
    store.append_audit(
        goal.goal_id, criterion_id="c1", result="not_applicable_user_accepted", recorded_at=RECORDED_AT
    )
    store.append_audit(
        goal.goal_id, criterion_id="c2", result="not_applicable_user_accepted", recorded_at=RECORDED_AT
    )

    with pytest.raises(GoalCompletionError, match="no required criterion is backed by existing evidence"):
        store.complete_goal(goal.goal_id, recorded_at=RECORDED_AT)

    # The blocked attempt transitioned nothing.
    assert store.effective_status(goal.goal_id) == "active"


def test_one_evidence_backed_required_plus_na_completes(tmp_path: Path) -> None:
    # A single required criterion backed by existing evidence is enough for the
    # goal to complete even when the other required criterion is
    # not_applicable_user_accepted.
    store = _store(tmp_path)
    goal = _create_goal(store)
    rel = _evidence(store)
    store.append_audit(
        goal.goal_id, criterion_id="c1", result="satisfied", evidence_refs=(rel,), recorded_at=RECORDED_AT
    )
    store.append_audit(
        goal.goal_id, criterion_id="c2", result="not_applicable_user_accepted", recorded_at=RECORDED_AT
    )

    row = store.complete_goal(goal.goal_id, recorded_at=RECORDED_AT)
    assert row["to_status"] == "complete"
    assert store.effective_status(goal.goal_id) == "complete"


# ---------------------------------------------------------------------------
# Redaction and free-text safety
# ---------------------------------------------------------------------------


def test_goal_free_text_is_redacted_on_disk(tmp_path: Path) -> None:
    store = _store(tmp_path)
    home = str(Path.home())
    goal = _create_goal(
        store,
        objective=f"Reduce drawdown; panel loaded from {home}/research/panel.parquet",
        criteria=(
            GoalCriterion(criterion_id="c1", text=f"Report saved under {home}/reports with QF_GOAL_API_KEY=sekret123456"),
        ),
    )
    rel = _evidence(store)
    store.append_audit(
        goal.goal_id,
        criterion_id="c1",
        result="satisfied",
        evidence_refs=(rel,),
        notes=f"verified against {home}/notes.md; MY_ACCESS_TOKEN=tok98765",
        recorded_at=RECORDED_AT,
    )

    goal_text = store.goal_path(goal.goal_id).read_text(encoding="utf-8")
    audit_text = store.audit_log_path(goal.goal_id).read_text(encoding="utf-8")
    for text in (goal_text, audit_text):
        assert home not in text
    assert "sekret123456" not in goal_text
    assert "tok98765" not in audit_text
    assert "<redacted-path>" in goal_text
    assert "MY_ACCESS_TOKEN=<redacted>" in audit_text


# ---------------------------------------------------------------------------
# CLI round-trip on a tmp workspace
# ---------------------------------------------------------------------------


def _cli_json(capsys: pytest.CaptureFixture[str], argv: list[str], *, expect: int = 0):
    exit_code = cli_main.main(argv)
    output = capsys.readouterr().out
    assert exit_code == expect, output
    return json.loads(output) if expect == 0 else output


def test_cli_goal_round_trip(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    artifact_root = tmp_path / "artifacts"
    root_arg = ["--artifact-root", str(artifact_root)]

    created = _cli_json(
        capsys,
        [
            "goal",
            "create",
            "--objective",
            "Improve small-cap momentum robustness",
            "--criteria",
            "OOS rank ICIR is reportable",
            "--criteria",
            "No embargo violations in evaluation",
            "--optional-criteria",
            "Turnover stays within budget",
            "--seed",
            "FTR_DEMO_SMALL_CAP",
            *root_arg,
        ],
    )
    goal_id = created["goal"]["goal_id"]
    assert created["goal"]["schema_version"] == GOAL_SCHEMA_VERSION
    assert created["effective_status"] == "active"
    assert re.fullmatch(r"[0-9a-f]{64}", created["goal"]["runtime_config_hash"])
    assert [entry["criterion_id"] for entry in created["goal"]["criteria"]] == ["c1", "c2", "c3"]
    assert [entry["required"] for entry in created["goal"]["criteria"]] == [True, True, False]
    assert created["path_rel"] == f"research_goals/{goal_id}.json"
    assert (artifact_root / "research_goals" / f"{goal_id}.json").exists()

    listed = _cli_json(capsys, ["goal", "list", *root_arg])
    assert [row["goal_id"] for row in listed] == [goal_id]
    assert listed[0]["status"] == "active"
    assert listed[0]["required_criteria_count"] == 2

    # Completion before any audits is blocked with exit code 2.
    blocked = _cli_json(capsys, ["goal", "complete", goal_id, *root_arg], expect=2)
    assert "goal completion blocked" in blocked

    # Audit c1 as satisfied with an absolute evidence path under artifact_root:
    # the CLI stores it relative, and validates existence.
    evidence = artifact_root / "evaluations" / "eval_demo.json"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text('{"ok": true}\n', encoding="utf-8")
    row = _cli_json(
        capsys,
        ["goal", "audit", goal_id, "--criterion", "c1", "--result", "satisfied", "--evidence", str(evidence), *root_arg],
    )
    assert row["evidence_refs"] == ["evaluations/eval_demo.json"]

    # A missing evidence file is refused with a friendly exit(2), not a traceback
    # (O10: audit matches the show/complete error style).
    refused = _cli_json(
        capsys,
        [
            "goal",
            "audit",
            goal_id,
            "--criterion",
            "c2",
            "--result",
            "satisfied",
            "--evidence",
            "evaluations/never_written.json",
            *root_arg,
        ],
        expect=2,
    )
    assert "goal audit failed" in refused
    assert "do not exist under artifact_root" in refused

    _cli_json(
        capsys,
        ["goal", "audit", goal_id, "--criterion", "c2", "--result", "not_applicable_user_accepted", *root_arg],
    )
    completed = _cli_json(capsys, ["goal", "complete", goal_id, *root_arg])
    assert completed["to_status"] == "complete"

    shown = _cli_json(capsys, ["goal", "show", goal_id, *root_arg])
    assert shown["effective_status"] == "complete"
    assert [row["row_type"] for row in shown["audit_rows"]] == [
        "criterion_audit",
        "criterion_audit",
        "status_transition",
    ]
    # No absolute paths anywhere in the stored artifacts.
    for name in (f"{goal_id}.json", f"{goal_id}.audit.jsonl"):
        assert str(tmp_path) not in (artifact_root / "research_goals" / name).read_text(encoding="utf-8")

    missing = _cli_json(capsys, ["goal", "show", "no-such-goal-000000000000", *root_arg], expect=2)
    assert "goal not found" in missing


# ---------------------------------------------------------------------------
# O1: at least one required criterion, at both ends
# ---------------------------------------------------------------------------


def test_all_optional_goal_is_unrepresentable(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(ValueError, match="required criterion"):
        _create_goal(
            store,
            criteria=(
                GoalCriterion(criterion_id="c1", text="optional one", required=False),
                GoalCriterion(criterion_id="c2", text="optional two", required=False),
            ),
        )


def test_legacy_all_optional_goal_row_cannot_complete(tmp_path: Path) -> None:
    # Defense in depth: a hand-built legacy artifact (written before the
    # constructor rule) must still never complete vacuously. Loading it trips
    # the constructor guard; nothing is appended to the audit log.
    store = _store(tmp_path)
    goal_id = "legacy-goal-aaaaaaaaaaaa"
    payload = {
        "schema_version": GOAL_SCHEMA_VERSION,
        "goal_id": goal_id,
        "objective": "legacy all-optional goal",
        "criteria": [{"criterion_id": "c1", "text": "optional", "required": False}],
        "seed_factor_id": "FTR_DEMO_SMALL_CAP",
        "runtime_config_hash": RUNTIME_HASH,
        "created_at": CREATED_AT,
        "status": "active",
        "evidence_refs": [],
    }
    store.goals_root.mkdir(parents=True, exist_ok=True)
    store.goal_path(goal_id).write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises((GoalCompletionError, ValueError)):
        store.complete_goal(goal_id, recorded_at=RECORDED_AT)
    assert store.read_audit_rows(goal_id) == []


# ---------------------------------------------------------------------------
# O6: every status change routes through the transition table
# ---------------------------------------------------------------------------


def test_transition_table_legal_chain(tmp_path: Path) -> None:
    store = _store(tmp_path)
    goal = _create_goal(store, status="draft")
    rel = _evidence(store)

    # Draft goals accept no audits and cannot complete.
    with pytest.raises(ValueError, match="draft"):
        store.append_audit(goal.goal_id, criterion_id="c1", result="satisfied", evidence_refs=(rel,), recorded_at=RECORDED_AT)
    with pytest.raises(GoalCompletionError, match="not allowed"):
        store.complete_goal(goal.goal_id, recorded_at=RECORDED_AT)
    assert store.effective_status(goal.goal_id) == "draft"

    # Legal chain: draft -> active -> insufficient_evidence -> active -> complete.
    store.transition(goal.goal_id, "active", recorded_at=RECORDED_AT)
    assert store.effective_status(goal.goal_id) == "active"
    store.transition(goal.goal_id, "insufficient_evidence", recorded_at=RECORDED_AT)
    store.transition(goal.goal_id, "active", recorded_at=RECORDED_AT)
    store.append_audit(goal.goal_id, criterion_id="c1", result="satisfied", evidence_refs=(rel,), recorded_at=RECORDED_AT)
    store.append_audit(goal.goal_id, criterion_id="c2", result="not_applicable_user_accepted", recorded_at=RECORDED_AT)
    assert store.complete_goal(goal.goal_id, recorded_at=RECORDED_AT)["to_status"] == "complete"

    # Terminal states accept no further transitions.
    with pytest.raises(ValueError, match="not allowed"):
        store.transition(goal.goal_id, "active", recorded_at=RECORDED_AT)


def test_transition_table_rejects_illegal_jumps(tmp_path: Path) -> None:
    store = _store(tmp_path)
    goal = _create_goal(store, status="draft")

    # draft -> insufficient_evidence skips activation.
    with pytest.raises(ValueError, match="not allowed"):
        store.transition(goal.goal_id, "insufficient_evidence", recorded_at=RECORDED_AT)
    # 'complete' is never reachable through transition(), even from active.
    store.transition(goal.goal_id, "active", recorded_at=RECORDED_AT)
    with pytest.raises(ValueError, match="complete_goal"):
        store.transition(goal.goal_id, "complete", recorded_at=RECORDED_AT)
    # Unknown statuses are rejected outright.
    with pytest.raises(ValueError, match="status must be one of"):
        store.transition(goal.goal_id, "archived", recorded_at=RECORDED_AT)


# ---------------------------------------------------------------------------
# O10: evidence containment — symlinks may not escape artifact_root
# ---------------------------------------------------------------------------


def test_symlinked_evidence_escaping_artifact_root_is_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    goal = _create_goal(store)
    outside = tmp_path / "outside_evidence.json"
    outside.write_text('{"ok": true}\n', encoding="utf-8")
    link_rel = "evaluations/linked.json"
    link_path = store.artifact_root / link_rel
    link_path.parent.mkdir(parents=True, exist_ok=True)
    link_path.symlink_to(outside)

    # The audit path refuses the escaping ref even though the symlink resolves
    # to an existing file.
    with pytest.raises(ValueError, match="do not exist under artifact_root"):
        store.append_audit(
            goal.goal_id, criterion_id="c1", result="satisfied", evidence_refs=(link_rel,), recorded_at=RECORDED_AT
        )

    # Completion re-verification also refuses it: audit against a real file,
    # then swap the file for an escaping symlink before completing.
    real_rel = "evaluations/real.json"
    real_path = store.artifact_root / real_rel
    real_path.write_text('{"ok": true}\n', encoding="utf-8")
    store.append_audit(goal.goal_id, criterion_id="c1", result="satisfied", evidence_refs=(real_rel,), recorded_at=RECORDED_AT)
    store.append_audit(goal.goal_id, criterion_id="c2", result="not_applicable_user_accepted", recorded_at=RECORDED_AT)
    real_path.unlink()
    real_path.symlink_to(outside)
    with pytest.raises(GoalCompletionError, match="exists under artifact_root"):
        store.complete_goal(goal.goal_id, recorded_at=RECORDED_AT)

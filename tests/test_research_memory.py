"""Lane M regression tests: research memory promotion, append-only stores, context feed."""

from __future__ import annotations

import json
from pathlib import Path
import re

import pytest

from quant_forge.data.local import create_demo_workspace
from quant_forge.research_loop.context_builder import ResearchContextBuilder
from quant_forge.research_loop.contracts import ResearchTraceEntry
from quant_forge.research_loop.memory import (
    RESEARCH_MEMORY_SCHEMA_VERSION,
    RULE_CANDIDATE_STATUS,
    MemoryObservation,
    PromotionDecision,
    ResearchMemoryStore,
    promote,
)
from quant_forge.research_loop.trace_store import ResearchTraceStore, utc_timestamp

SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
T1 = "2026-07-01T00:00:00+00:00"
T2 = "2026-07-02T00:00:00+00:00"
T3 = "2026-07-03T00:00:00+00:00"
WINDOW_A = "2024-01-01:2024-06-30"
WINDOW_B = "2024-07-01:2024-12-31"


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _observation(
    *,
    signature: str = "sig_reversal_low_icir",
    statement: str = "5d reversal candidates show unstable OOS rank ICIR",
    run_id: str = "rd-1",
    observed_at: str = T1,
    data_window: str = WINDOW_A,
    failure_class: str = "",
    evidence_ref: str = "",
) -> MemoryObservation:
    return MemoryObservation(
        signature=signature,
        statement=statement,
        run_id=run_id,
        observed_at=observed_at,
        data_window=data_window,
        failure_class=failure_class,
        evidence_ref=evidence_ref,
    )


# ---------------------------------------------------------------------------
# promote(): pure deterministic promotion thresholds
# ---------------------------------------------------------------------------


def test_promote_single_observation_yields_no_decision() -> None:
    assert promote([_observation()]) == ()


def test_promote_two_observations_same_run_stays_trace_only() -> None:
    observations = [
        _observation(run_id="rd-1", observed_at=T1),
        _observation(run_id="rd-1", observed_at=T2),
    ]
    assert promote(observations) == ()


def test_promote_two_observations_two_runs_yields_active_finding() -> None:
    observations = [
        _observation(run_id="rd-1", observed_at=T1),
        _observation(run_id="rd-2", observed_at=T2),
    ]
    decisions = promote(observations)

    assert len(decisions) == 1
    decision = decisions[0]
    assert decision.kind == "finding"
    assert decision.status == "active"
    assert decision.observation_count == 2
    assert decision.run_ids == ("rd-1", "rd-2")
    assert decision.first_seen == T1
    assert decision.last_seen == T2


@pytest.mark.parametrize("failure_class", ["gate_blocked", "validation_error"])
def test_promote_gate_blocking_class_yields_failure(failure_class: str) -> None:
    observations = [
        _observation(run_id="rd-1", observed_at=T1, failure_class=failure_class),
        _observation(run_id="rd-2", observed_at=T2),
    ]
    decisions = promote(observations)

    assert len(decisions) == 1
    assert decisions[0].kind == "failure"
    assert decisions[0].status == "active"


def test_promote_three_observations_two_windows_yields_rule_candidate() -> None:
    observations = [
        _observation(run_id="rd-1", observed_at=T1, data_window=WINDOW_A),
        _observation(run_id="rd-2", observed_at=T2, data_window=WINDOW_A),
        _observation(run_id="rd-3", observed_at=T3, data_window=WINDOW_B),
    ]
    decisions = promote(observations)

    assert len(decisions) == 1
    decision = decisions[0]
    assert decision.kind == "rule"
    assert decision.status == RULE_CANDIDATE_STATUS
    assert decision.observation_count == 3
    assert decision.data_windows == (WINDOW_A, WINDOW_B)


def test_promote_three_observations_single_window_stays_finding() -> None:
    observations = [
        _observation(run_id="rd-1", observed_at=T1, data_window=WINDOW_A),
        _observation(run_id="rd-2", observed_at=T2, data_window=WINDOW_A),
        _observation(run_id="rd-3", observed_at=T3, data_window=WINDOW_A),
    ]
    decisions = promote(observations)

    assert len(decisions) == 1
    assert decisions[0].kind == "finding"
    assert decisions[0].observation_count == 3


def test_promote_ignores_empty_windows_as_unknowns() -> None:
    # FP-4: an unknown window is not a distinct window; no rule from unknowns.
    observations = [
        _observation(run_id="rd-1", observed_at=T1, data_window=""),
        _observation(run_id="rd-2", observed_at=T2, data_window=""),
        _observation(run_id="rd-3", observed_at=T3, data_window=WINDOW_A),
    ]
    decisions = promote(observations)

    assert len(decisions) == 1
    assert decisions[0].kind == "finding"


def test_rule_auto_activation_is_unrepresentable() -> None:
    with pytest.raises(ValueError, match="never auto-activate"):
        PromotionDecision(
            signature="sig",
            kind="rule",
            status="active",
            statement="rules cannot self-activate",
            scope="global",
            observation_count=3,
            first_seen=T1,
            last_seen=T3,
        )


# ---------------------------------------------------------------------------
# ResearchMemoryStore: append-only JSONL rows under research_memory/
# ---------------------------------------------------------------------------


def test_single_observation_leaves_trace_only_no_knowledge_row(tmp_path: Path) -> None:
    store = ResearchMemoryStore(tmp_path / "artifacts")
    store.record_observation(
        signature="sig_one",
        statement="one-off observation",
        run_id="rd-1",
        observed_at=T1,
        data_window=WINDOW_A,
    )

    assert store.promote_pending() == ()
    assert len(_read_jsonl(store.observations_path)) == 1
    for kind in ("rule", "finding", "failure"):
        assert _read_jsonl(store.path_for(kind)) == []


def test_two_runs_promote_to_finding_row_with_schema_and_hash_id(tmp_path: Path) -> None:
    store = ResearchMemoryStore(tmp_path / "artifacts")
    store.record_observation(
        signature="sig_find",
        statement="small-cap tilt persists after neutralization",
        run_id="rd-1",
        observed_at=T1,
        data_window=WINDOW_A,
        evidence_ref="runs/rd-1/report.md",
    )
    store.record_observation(
        signature="sig_find",
        statement="small-cap tilt persists after neutralization",
        run_id="rd-2",
        observed_at=T2,
        data_window=WINDOW_A,
    )

    appended = store.promote_pending()

    assert len(appended) == 1
    rows = _read_jsonl(store.path_for("finding"))
    assert len(rows) == 1
    row = rows[0]
    assert row["schema_version"] == RESEARCH_MEMORY_SCHEMA_VERSION
    assert SHA256_HEX.fullmatch(row["entry_id"])
    assert row["kind"] == "finding"
    assert row["status"] == "active"
    assert row["observation_count"] == 2
    assert row["first_seen"] == T1
    assert row["last_seen"] == T2
    assert row["supersedes"] is None
    assert sorted(row["evidence_refs"]) == ["rd-2", "runs/rd-1/report.md"]
    for ref in row["evidence_refs"]:
        assert not ref.startswith("/")
        assert ".." not in Path(ref).parts
    assert _read_jsonl(store.path_for("failure")) == []
    assert _read_jsonl(store.path_for("rule")) == []


def test_gate_blocking_signature_promotes_to_failures_file(tmp_path: Path) -> None:
    store = ResearchMemoryStore(tmp_path / "artifacts")
    store.record_observation(
        signature="sig_gate",
        statement="turnover cap gate blocks daily-rebalance variants",
        run_id="rd-1",
        observed_at=T1,
        failure_class="gate_blocked",
    )
    store.record_observation(
        signature="sig_gate",
        statement="turnover cap gate blocks daily-rebalance variants",
        run_id="rd-2",
        observed_at=T2,
        failure_class="gate_blocked",
    )

    store.promote_pending()

    rows = _read_jsonl(store.path_for("failure"))
    assert len(rows) == 1
    assert rows[0]["kind"] == "failure"
    assert rows[0]["status"] == "active"
    assert _read_jsonl(store.path_for("finding")) == []


def test_rule_candidate_row_needs_human_review_and_never_auto_activates(tmp_path: Path) -> None:
    store = ResearchMemoryStore(tmp_path / "artifacts")
    for run_id, observed_at, window in (("rd-1", T1, WINDOW_A), ("rd-2", T2, WINDOW_A), ("rd-3", T3, WINDOW_B)):
        store.record_observation(
            signature="sig_rule",
            statement="prefer rank over zscore for volume features",
            run_id=run_id,
            observed_at=observed_at,
            data_window=window,
        )

    store.promote_pending()
    rows = _read_jsonl(store.path_for("rule"))
    assert len(rows) == 1
    assert rows[0]["status"] == RULE_CANDIDATE_STATUS

    # A fourth confirming observation supersedes the row but the promotion
    # path still cannot flip the status to active.
    store.record_observation(
        signature="sig_rule",
        statement="prefer rank over zscore for volume features",
        run_id="rd-4",
        observed_at="2026-07-04T00:00:00+00:00",
        data_window=WINDOW_B,
    )
    store.promote_pending()
    rows = _read_jsonl(store.path_for("rule"))
    assert len(rows) == 2
    assert all(row["status"] == RULE_CANDIDATE_STATUS for row in rows)
    assert not any(row["status"] == "active" for row in rows)


def test_duplicate_submission_appends_superseding_row_never_rewrites(tmp_path: Path) -> None:
    store = ResearchMemoryStore(tmp_path / "artifacts")
    original_statement = "momentum decay accelerates past 10d horizon"
    store.record_observation(
        signature="sig_dup",
        statement=original_statement,
        run_id="rd-1",
        observed_at=T1,
        data_window=WINDOW_A,
    )
    store.record_observation(
        signature="sig_dup",
        statement=original_statement,
        run_id="rd-2",
        observed_at=T2,
        data_window=WINDOW_A,
    )
    store.promote_pending()
    findings_path = store.path_for("finding")
    content_after_first = findings_path.read_text(encoding="utf-8")
    first_row = _read_jsonl(findings_path)[0]

    store.record_observation(
        signature="sig_dup",
        statement="momentum decay accelerates past 10d horizon (reworded)",
        run_id="rd-3",
        observed_at=T3,
        data_window=WINDOW_A,
    )
    appended = store.promote_pending()

    # Append-only: earlier bytes are a strict prefix of the new file content.
    content_after_second = findings_path.read_text(encoding="utf-8")
    assert content_after_second.startswith(content_after_first)
    rows = _read_jsonl(findings_path)
    assert len(rows) == 2
    superseding = rows[1]
    assert len(appended) == 1
    assert superseding["supersedes"] == first_row["entry_id"]
    assert superseding["observation_count"] == 3
    assert superseding["last_seen"] == T3
    # Statements are never rewritten: the superseding row keeps the original.
    assert superseding["statement"] == first_row["statement"] == original_statement
    assert superseding["first_seen"] == first_row["first_seen"] == T1

    # read_recent collapses the chain to the live row only.
    recent = store.read_recent("finding", 5)
    assert len(recent) == 1
    assert recent[0]["entry_id"] == superseding["entry_id"]
    assert recent[0]["observation_count"] == 3

    # Idempotent: promoting again with no new observations appends nothing.
    assert store.promote_pending() == ()
    assert findings_path.read_text(encoding="utf-8") == content_after_second


def test_statements_are_redacted_before_reaching_disk(tmp_path: Path) -> None:
    store = ResearchMemoryStore(tmp_path / "artifacts")
    home = str(Path.home())
    leaky = f"panel loaded from {home}/research/panel.parquet with QF_DEMO_API_KEY=abc123def456ghi789"
    for run_id, observed_at in (("rd-1", T1), ("rd-2", T2)):
        store.record_observation(
            signature="sig_leak",
            statement=leaky,
            run_id=run_id,
            observed_at=observed_at,
            data_window=WINDOW_A,
        )
    store.promote_pending()

    for path in (store.observations_path, store.path_for("finding")):
        raw = path.read_text(encoding="utf-8")
        assert home not in raw
        assert "abc123def456ghi789" not in raw
    row = _read_jsonl(store.path_for("finding"))[0]
    assert "<redacted-path>" in row["statement"]
    assert "QF_DEMO_API_KEY=<redacted>" in row["statement"]


def test_evidence_refs_must_be_relative(tmp_path: Path) -> None:
    store = ResearchMemoryStore(tmp_path / "artifacts")
    for bad_ref in ("/absolute/evidence.json", "~/evidence.json", "runs/../../evidence.json", "C:\\evidence.json"):
        with pytest.raises(ValueError):
            store.record_observation(
                signature="sig_ref",
                statement="ref check",
                run_id="rd-1",
                observed_at=T1,
                evidence_ref=bad_ref,
            )
    assert _read_jsonl(store.observations_path) == []


def test_read_recent_orders_newest_first_and_caps(tmp_path: Path) -> None:
    store = ResearchMemoryStore(tmp_path / "artifacts")
    for index in range(7):
        for run_suffix in ("a", "b"):
            store.record_observation(
                signature=f"sig_{index}",
                statement=f"finding number {index}",
                run_id=f"rd-{index}-{run_suffix}",
                observed_at=f"2026-07-0{index + 1}T0{0 if run_suffix == 'a' else 1}:00:00+00:00",
                data_window=WINDOW_A,
            )
    store.promote_pending()

    recent = store.read_recent("finding", 5)
    assert len(recent) == 5
    seen = [row["last_seen"] for row in recent]
    assert seen == sorted(seen, reverse=True)
    assert recent[0]["statement"] == "finding number 6"


# ---------------------------------------------------------------------------
# Context feed: memory items appended with a source marker, caps respected
# ---------------------------------------------------------------------------


def _promote_pairs(store: ResearchMemoryStore, *, prefix: str, count: int, failure_class: str = "") -> None:
    for index in range(count):
        for run_suffix in ("a", "b"):
            store.record_observation(
                signature=f"{prefix}_{index}",
                statement=f"{prefix} statement {index}",
                run_id=f"rd-{prefix}-{index}-{run_suffix}",
                observed_at=f"2026-06-{index + 10}T0{0 if run_suffix == 'a' else 1}:00:00+00:00",
                data_window=WINDOW_A,
                failure_class=failure_class,
            )
    store.promote_pending()


def test_context_builder_appends_memory_items_with_source_marker_and_caps(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    memory = ResearchMemoryStore(paths["artifact_root"])
    _promote_pairs(memory, prefix="fail", count=7, failure_class="gate_blocked")
    _promote_pairs(memory, prefix="find", count=7)
    trace = ResearchTraceStore(tmp_path / "trace")
    trace.append_trace(
        ResearchTraceEntry(
            run_id="rd_ctx",
            lane_id="plan",
            phase="plan_blocked",
            timestamp=utc_timestamp(),
            formula_dsl="rank(close)",
        )
    )

    context = ResearchContextBuilder(
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        trace_store=trace,
        memory_store=memory,
    ).build()

    memory_failures = [item for item in context.recent_failures if item.get("source") == "research_memory"]
    memory_findings = [item for item in context.recent_successes if item.get("source") == "research_memory"]
    # Caps: at most 5 memory items per tuple even though 7 were promoted.
    assert len(memory_failures) == 5
    assert len(memory_findings) == 5
    assert all(item["kind"] == "failure" for item in memory_failures)
    assert all(item["kind"] == "finding" for item in memory_findings)
    assert all(item["statement"].startswith("fail statement") for item in memory_failures)
    assert all(item["observation_count"] == 2 for item in memory_findings)
    # Existing trace-derived entries keep their shape (no source marker) and
    # precede the appended memory items — ResearchContext is not restructured.
    trace_failures = [item for item in context.recent_failures if item.get("source") != "research_memory"]
    assert len(trace_failures) == 1
    assert trace_failures[0].get("phase") == "plan_blocked"
    assert list(context.recent_failures).index(trace_failures[0]) == 0


def test_context_builder_memory_statements_stay_redacted(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    memory = ResearchMemoryStore(paths["artifact_root"])
    home = str(Path.home())
    for run_id, observed_at in (("rd-1", T1), ("rd-2", T2)):
        memory.record_observation(
            signature="sig_ctx_leak",
            statement=f"failure while reading {home}/research/panel.parquet",
            run_id=run_id,
            observed_at=observed_at,
            failure_class="validation_error",
        )
    memory.promote_pending()

    context = ResearchContextBuilder(
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
        memory_store=memory,
    ).build()

    items = [item for item in context.recent_failures if item.get("source") == "research_memory"]
    assert len(items) == 1
    assert home not in items[0]["statement"]
    assert "<redacted-path>" in items[0]["statement"]


def test_context_builder_without_memory_store_is_unchanged(tmp_path: Path) -> None:
    paths = create_demo_workspace(tmp_path / "demo")
    context = ResearchContextBuilder(
        factor_root=paths["factor_root"],
        data_root=paths["data_root"],
    ).build()

    assert context.recent_failures == ()
    assert context.recent_successes == ()

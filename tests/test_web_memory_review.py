"""SE-P4b regression tests: the web memory-review tab payload/action module
and its additive routing handlers.

Covers ``apps/web/memory_review.py`` (pure payload + action functions over
the frozen ``research_loop.memory``/``research_loop.priors`` contracts),
``apps/web/routing.py``'s additive ``GET /api/memory/review`` and
``POST /api/memory/review/{rule,promoted}`` handlers, and the additive
``html.py`` tab mount. See DECISIONS.md "2026-07-13 -- Self-evolution engine
CP0", ruling SE-iii (rule governance = review surface) and owner rulings R3
(rules must pass review; findings/failures automatic + retirable; no
signatures, no popups) / R5 (plugin-domain read-only pane; priors ``as_of``
disclosure).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import quant_forge.apps.web.routing as web_routing
import quant_forge.apps.web.server as web_server
from quant_forge.apps.web.memory_review import (
    MEMORY_REVIEW_PAYLOAD_SCHEMA_VERSION,
    memory_review_payload,
    review_promoted,
    review_rule,
)
from quant_forge.apps.web.server import create_local_web_server
from quant_forge.config import QuantForgeConfig
from quant_forge.data.local import create_demo_workspace
from quant_forge.research_loop.memory import ResearchMemoryStore

JSON_CONTENT_TYPE = "application/json; charset=utf-8"

T1 = "2026-07-01T00:00:00+00:00"
T2 = "2026-07-02T00:00:00+00:00"
T3 = "2026-07-03T00:00:00+00:00"
T4 = "2026-07-04T00:00:00+00:00"
WINDOW_A = "2024-01-01:2024-06-30"
WINDOW_B = "2024-07-01:2024-12-31"


def _store(tmp_path: Path) -> ResearchMemoryStore:
    return ResearchMemoryStore(tmp_path / "artifacts")


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


def _promote_finding_or_failure(store: ResearchMemoryStore, *, signature: str, failure_class: str = "") -> dict:
    """Mint one finding (default) or failure (``failure_class`` set) row."""

    kind = "failure" if failure_class else "finding"
    for run_id, observed_at in ((f"{signature}-1", T1), (f"{signature}-2", T2)):
        store.record_observation(
            signature=signature,
            statement=f"statement for {signature}",
            run_id=run_id,
            observed_at=observed_at,
            failure_class=failure_class,
        )
    store.promote_pending()
    return store.resolve_signature_prefix(kind, signature)


# ---------------------------------------------------------------------------
# memory_review_payload: shape, 4-state rule labels, sort order
# ---------------------------------------------------------------------------


def test_payload_schema_version_and_empty_shape(tmp_path: Path) -> None:
    store = _store(tmp_path)
    payload = memory_review_payload(store)
    assert payload["schema_version"] == MEMORY_REVIEW_PAYLOAD_SCHEMA_VERSION == "qf.web.memory_review.v1"
    assert payload["rules"] == []
    assert payload["findings"] == []
    assert payload["failures"] == []
    assert payload["plugin"] is None
    assert payload["priors"]["as_of"] == 0


def test_rule_payload_carries_never_reviewed_state_and_eligibility(tmp_path: Path) -> None:
    store = _store(tmp_path)
    row = _promote_rule(store, signature="sig_a")
    payload = memory_review_payload(store)
    assert len(payload["rules"]) == 1
    entry = payload["rules"][0]
    assert entry["signature"] == "sig_a"
    assert entry["state"] == "never_reviewed"
    assert entry["can_activate"] is True
    assert entry["can_deactivate"] is True
    assert entry["event_id"] == ""
    assert entry["activation_seq"] is None
    assert entry["entry_id"] == row["entry_id"]
    # Promotion status stays needs_human_review forever (FP-2); "state" is
    # the review-derived field, never conflated with the row's own status.
    assert entry["status"] == "needs_human_review"


def test_rule_payload_active_state_after_activation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    row = _promote_rule(store, signature="sig_b")
    payload = review_rule(store, signature_prefix="sig_b", action="activate", actor="alice")
    entry = next(item for item in payload["rules"] if item["signature"] == "sig_b")
    assert entry["state"] == "active"
    assert entry["can_activate"] is False
    assert entry["can_deactivate"] is True
    assert entry["reviewed_entry_id"] == row["entry_id"]
    assert entry["event_id"]


def test_rule_payload_deactivated_state(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _promote_rule(store, signature="sig_deact")
    payload = review_rule(store, signature_prefix="sig_deact", action="deactivate", actor="alice")
    entry = payload["rules"][0]
    assert entry["state"] == "deactivated"
    assert entry["can_activate"] is True
    assert entry["can_deactivate"] is False


def test_rule_lapsed_pending_re_review_after_superseding_promotion(tmp_path: Path) -> None:
    # Workorder recipe: promote rule -> activate -> re-promote a superseding
    # row -> label lapsed_pending_re_review.
    store = _store(tmp_path)
    _promote_rule(store, signature="sig_lapse")
    payload = review_rule(store, signature_prefix="sig_lapse", action="activate", actor="alice")
    assert payload["rules"][0]["state"] == "active"

    store.record_observation(
        signature="sig_lapse",
        statement="rule statement for sig_lapse",
        run_id="sig_lapse-4",
        observed_at=T4,
        data_window="2025-01-01:2025-06-30",
    )
    store.promote_pending()

    payload = memory_review_payload(store)
    entry = next(item for item in payload["rules"] if item["signature"] == "sig_lapse")
    assert entry["state"] == "lapsed_pending_re_review"
    # A superseded row's content changed since the last review: both a
    # fresh activate and a fresh deactivate are meaningful re-review calls.
    assert entry["can_activate"] is True
    assert entry["can_deactivate"] is True


def test_rules_sort_needs_review_first_then_active_then_deactivated(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _promote_rule(store, signature="sig_active")
    review_rule(store, signature_prefix="sig_active", action="activate", actor="alice")
    _promote_rule(store, signature="sig_deactivated")
    review_rule(store, signature_prefix="sig_deactivated", action="deactivate", actor="alice")
    _promote_rule(store, signature="sig_never")

    payload = memory_review_payload(store)
    states = [entry["state"] for entry in payload["rules"]]
    assert states.index("never_reviewed") < states.index("active") < states.index("deactivated")


# ---------------------------------------------------------------------------
# findings / failures: review_state + retire/unretire eligibility
# ---------------------------------------------------------------------------


def test_finding_payload_active_then_retired_then_restored(tmp_path: Path) -> None:
    store = _store(tmp_path)
    original_row = _promote_finding_or_failure(store, signature="sig_finding")

    payload = memory_review_payload(store)
    entry = next(item for item in payload["findings"] if item["signature"] == "sig_finding")
    assert entry["review_state"] == "active"
    assert entry["can_retire"] is True
    assert entry["can_unretire"] is False

    payload = review_promoted(store, kind="finding", signature_prefix="sig_finding", action="retire", actor="bob")
    entry = next(item for item in payload["findings"] if item["signature"] == "sig_finding")
    assert entry["review_state"] == "retired"
    assert entry["can_retire"] is False
    assert entry["can_unretire"] is True

    payload = review_promoted(store, kind="finding", signature_prefix="sig_finding", action="unretire", actor="bob")
    entry = next(item for item in payload["findings"] if item["signature"] == "sig_finding")
    assert entry["review_state"] == "active"
    assert entry["can_retire"] is True
    assert entry["can_unretire"] is False
    assert entry["entry_id"] == original_row["entry_id"]


def test_failure_payload_shape_and_retire(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _promote_finding_or_failure(store, signature="sig_failure", failure_class="gate_blocked")
    payload = memory_review_payload(store)
    assert len(payload["failures"]) == 1
    entry = payload["failures"][0]
    assert entry["signature"] == "sig_failure"
    assert entry["review_state"] == "active"

    payload = review_promoted(store, kind="failure", signature_prefix="sig_failure", action="retire", actor="carol")
    entry = payload["failures"][0]
    assert entry["review_state"] == "retired"


# ---------------------------------------------------------------------------
# P4B-F2: promoted-row retirement survives a read-read race
# ---------------------------------------------------------------------------


def test_promoted_retirement_survives_a_supersession_race_between_reads(tmp_path: Path, monkeypatch) -> None:
    # list_promoted() and retired_signatures() are each independently
    # locked; a promote_pending() landing between them can supersede a row
    # exactly when its retire event's reviewed_entry_id no longer matches
    # the live row -- the frozen contract then makes that event lapse
    # (truthfully NOT retired relative to the new row). Simulate the
    # interleaving: list_promoted's FIRST call returns the STALE
    # pre-supersession row; every later call returns the real (post-
    # supersession) row. retired_signatures is left untouched -- it always
    # answers with the CURRENT truth. A naive "call both once" read would
    # zip the stale rows-read together with retired_signatures and could
    # only get lucky or unlucky depending on call order; the fix must
    # detect the mismatch and retry until the (signature -> entry_id)
    # mapping stabilizes.
    store = _store(tmp_path)
    old_row = _promote_finding_or_failure(store, signature="sig_race")
    review_promoted(store, kind="finding", signature_prefix="sig_race", action="retire", actor="alice")

    store.record_observation(
        signature="sig_race", statement="statement for sig_race", run_id="sig_race-3", observed_at=T3
    )
    store.promote_pending()
    new_row = store.resolve_signature_prefix("finding", "sig_race")
    assert new_row["entry_id"] != old_row["entry_id"]
    # The frozen contract's own truth: the retire event lapsed, since it is
    # bound to old_row's entry_id, not the new live row's.
    assert "sig_race" not in store.retired_signatures("finding")

    real_list_promoted = store.list_promoted
    calls = {"count": 0}

    def flaky_list_promoted(kind: str):
        # memory_review_payload() also reads kind="failure" (empty, always
        # stable in one extra pair) -- scope the staleness simulation and
        # the call count to "finding" only, so the count assertion below
        # is a precise, meaningful pin on THIS kind's retry behavior rather
        # than incidentally counting an unrelated kind's own reads too.
        if kind != "finding":
            return real_list_promoted(kind)
        calls["count"] += 1
        if calls["count"] == 1:
            return (old_row,)
        return real_list_promoted(kind)

    monkeypatch.setattr(store, "list_promoted", flaky_list_promoted)

    payload = memory_review_payload(store)
    entry = next(item for item in payload["findings"] if item["signature"] == "sig_race")
    # Converges on the NEW row with its correctly-lapsed (not retired)
    # status -- never the new row mislabeled retired from the stale first
    # read, and never stuck serving the stale old row either.
    assert entry["entry_id"] == new_row["entry_id"]
    assert entry["review_state"] == "active"
    assert calls["count"] == 3  # 1 stale + 1 mismatch-detected + 1 confirming match


def test_promoted_stable_read_is_bounded_and_uses_latest_pair_on_persistent_churn(
    tmp_path: Path, monkeypatch
) -> None:
    # Pathological case: the (signature -> entry_id) mapping never
    # stabilizes across the bounded attempt budget. The read must still
    # terminate (never hang/retry forever) and must return a
    # SELF-CONSISTENT pairing from its own last attempt, not some
    # undefined mix of two different attempts.
    store = _store(tmp_path)
    _promote_finding_or_failure(store, signature="sig_churn")

    real_list_promoted = store.list_promoted
    calls = {"count": 0}

    def churning_list_promoted(kind: str):
        # Scope to "finding" only -- see the identical note in
        # test_promoted_retirement_survives_a_supersession_race_between_reads;
        # the empty "failure" kind is also read by memory_review_payload()
        # and would otherwise pollute this count.
        if kind != "finding":
            return real_list_promoted(kind)
        calls["count"] += 1
        rows = real_list_promoted(kind)
        # A fabricated, ever-changing entry_id: the mapping is different on
        # every single call, so consecutive reads can never agree.
        return tuple({**row, "entry_id": f"{row['entry_id']}-{calls['count']}"} for row in rows)

    monkeypatch.setattr(store, "list_promoted", churning_list_promoted)

    payload = memory_review_payload(store)  # must not hang, must not raise
    assert calls["count"] == 3  # bounded: exactly _PROMOTED_STABLE_READ_ATTEMPTS, never unbounded
    entry = next(item for item in payload["findings"] if item["signature"] == "sig_churn")
    assert entry["entry_id"].endswith("-3")  # the LATEST attempt's pair, not the first or a mix


# ---------------------------------------------------------------------------
# closed action vocabulary per target kind (frozen store contract)
# ---------------------------------------------------------------------------


def test_review_rule_rejects_retire_and_unretire(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _promote_rule(store, signature="sig_c")
    for action in ("retire", "unretire"):
        with pytest.raises(ValueError, match="rule review action must be one of"):
            review_rule(store, signature_prefix="sig_c", action=action, actor="alice")


def test_review_promoted_rejects_activate_and_deactivate(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _promote_finding_or_failure(store, signature="sig_d")
    for action in ("activate", "deactivate"):
        with pytest.raises(ValueError, match="promoted review action must be one of"):
            review_promoted(store, kind="finding", signature_prefix="sig_d", action=action, actor="alice")


def test_review_promoted_rejects_unknown_kind(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(ValueError, match="kind must be one of"):
        review_promoted(store, kind="rule", signature_prefix="sig_e", action="retire", actor="alice")


# ---------------------------------------------------------------------------
# actor required (R3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("actor", ["", "   "])
def test_review_rule_requires_non_empty_actor(tmp_path: Path, actor: str) -> None:
    store = _store(tmp_path)
    _promote_rule(store, signature="sig_f")
    with pytest.raises(ValueError, match="actor is required"):
        review_rule(store, signature_prefix="sig_f", action="activate", actor=actor)


def test_review_promoted_requires_non_empty_actor(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _promote_finding_or_failure(store, signature="sig_g")
    with pytest.raises(ValueError, match="actor is required"):
        review_promoted(store, kind="finding", signature_prefix="sig_g", action="retire", actor="")


# ---------------------------------------------------------------------------
# R3 anti-fat-finger: ambiguous / absent signature prefix
# ---------------------------------------------------------------------------


def test_review_rule_ambiguous_prefix_lists_candidates(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _promote_rule(store, signature="sig_shared_one")
    _promote_rule(store, signature="sig_shared_two")
    with pytest.raises(ValueError, match="ambiguous rule signature prefix") as excinfo:
        review_rule(store, signature_prefix="sig_shared", action="activate", actor="alice")
    assert "sig_shared_one" in str(excinfo.value)
    assert "sig_shared_two" in str(excinfo.value)


def test_review_rule_absent_prefix_raises(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _promote_rule(store, signature="sig_only")
    with pytest.raises(ValueError, match="no rule signature matches prefix"):
        review_rule(store, signature_prefix="does_not_exist", action="activate", actor="alice")


def test_review_promoted_ambiguous_prefix_lists_candidates(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _promote_finding_or_failure(store, signature="sig_shared_find_one")
    _promote_finding_or_failure(store, signature="sig_shared_find_two")
    with pytest.raises(ValueError, match="ambiguous finding signature prefix") as excinfo:
        review_promoted(store, kind="finding", signature_prefix="sig_shared_find", action="retire", actor="alice")
    assert "sig_shared_find_one" in str(excinfo.value)
    assert "sig_shared_find_two" in str(excinfo.value)


# ---------------------------------------------------------------------------
# priors honesty counters (SE-v) flow through the payload verbatim
# ---------------------------------------------------------------------------


def test_priors_honesty_counters_flow_through_payload_with_one_invalid_row(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ledger = store.outcomes_ledger_path
    ledger.parent.mkdir(parents=True, exist_ok=True)
    # A JSON-valid but non-mapping row: read-path re-validation excludes it
    # and counts it, never crashes (mirrors test_research_priors.py's
    # non-mapping-row regression).
    with ledger.open("a", encoding="utf-8") as handle:
        handle.write("[]\n")

    payload = memory_review_payload(store)
    priors = payload["priors"]
    assert priors["invalid_rows"] == 1
    assert priors["as_of"] == 1
    assert priors["total_envelopes"] == 1
    assert priors["total_evidence_runs"] == 0
    assert priors["oos_excluded"] == 0
    assert priors["tables"]
    for table in priors["tables"]:
        assert table["unbucketed"] == 0
        assert table["cells"] == []


# ---------------------------------------------------------------------------
# plugin pane (R5): read-only, no action eligibility anywhere
# ---------------------------------------------------------------------------


def test_plugin_pane_omitted_when_absent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    payload = memory_review_payload(store)
    assert payload["plugin"] is None


def test_plugin_pane_present_and_read_only_when_plugin_store_passed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    plugin_store = ResearchMemoryStore(tmp_path / "plugin_artifacts")
    _promote_rule(plugin_store, signature="plugin_sig_rule")
    review_rule(plugin_store, signature_prefix="plugin_sig_rule", action="activate", actor="plugin-bot")
    _promote_finding_or_failure(plugin_store, signature="plugin_sig_finding")

    payload = memory_review_payload(store, plugin_store)
    plugin = payload["plugin"]
    assert plugin is not None
    assert "priors" in plugin
    assert len(plugin["rules"]) == 1
    rule_entry = plugin["rules"][0]
    assert rule_entry["signature"] == "plugin_sig_rule"
    assert rule_entry["state"] == "active"
    assert "can_activate" not in rule_entry
    assert "can_deactivate" not in rule_entry
    assert len(plugin["findings"]) == 1
    finding_entry = plugin["findings"][0]
    assert "can_retire" not in finding_entry
    assert "can_unretire" not in finding_entry
    assert plugin["failures"] == []
    assert "as_of" in plugin["priors"]

    # Dual-domain isolation (SE-i): the plugin pane never leaks into the
    # main store's own (action-eligible) rows.
    assert payload["rules"] == []


def test_review_actions_target_the_main_store_never_the_plugin_store(tmp_path: Path) -> None:
    store = _store(tmp_path)
    plugin_store = ResearchMemoryStore(tmp_path / "plugin_artifacts")
    _promote_rule(store, signature="sig_main")
    _promote_rule(plugin_store, signature="sig_plugin")

    payload = review_rule(store, plugin_store, signature_prefix="sig_main", action="activate", actor="alice")
    assert payload["rules"][0]["state"] == "active"
    assert payload["plugin"]["rules"][0]["signature"] == "sig_plugin"
    assert "can_activate" not in payload["plugin"]["rules"][0]
    # The plugin store itself was never mutated by an action scoped to store.
    assert plugin_store.rule_review_snapshot()["sig_plugin"]["state"] == "never_reviewed"


# ---------------------------------------------------------------------------
# routing handlers: GET /api/memory/review, POST .../rule, POST .../promoted
# ---------------------------------------------------------------------------


@pytest.fixture()
def web_config(tmp_path):
    create_demo_workspace(tmp_path / "demo")
    return QuantForgeConfig().resolve(tmp_path / "demo")


@pytest.fixture()
def web_app(web_config):
    server = create_local_web_server(host="127.0.0.1", port=0, config=web_config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield base_url
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def _get(base_url: str, path: str) -> tuple[int, str, bytes]:
    request = urllib.request.Request(base_url + path)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.headers.get("Content-Type", ""), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type", ""), exc.read()


def _post(base_url: str, path: str, body: dict) -> tuple[int, str, bytes]:
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        base_url + path, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.headers.get("Content-Type", ""), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type", ""), exc.read()


def test_get_memory_review_returns_empty_payload_over_http(web_app) -> None:
    status, content_type, body = _get(web_app, "/api/memory/review")
    assert status == 200
    assert content_type == JSON_CONTENT_TYPE
    payload = json.loads(body.decode("utf-8"))
    assert payload["schema_version"] == "qf.web.memory_review.v1"
    assert payload["rules"] == []
    assert payload["plugin"] is None


def test_post_memory_review_rule_round_trips_through_http(web_app, web_config) -> None:
    store = ResearchMemoryStore(web_config.paths.artifact_root)
    _promote_rule(store, signature="sig_http_rule")

    status, content_type, body = _post(
        web_app,
        "/api/memory/review/rule",
        {"signature_prefix": "sig_http_rule", "action": "activate", "actor": "alice", "rationale": "looks solid"},
    )
    assert status == 200
    assert content_type == JSON_CONTENT_TYPE
    payload = json.loads(body.decode("utf-8"))
    entry = next(item for item in payload["rules"] if item["signature"] == "sig_http_rule")
    assert entry["state"] == "active"


def test_post_memory_review_promoted_round_trips_through_http(web_app, web_config) -> None:
    store = ResearchMemoryStore(web_config.paths.artifact_root)
    _promote_finding_or_failure(store, signature="sig_http_finding")

    status, _, body = _post(
        web_app,
        "/api/memory/review/promoted",
        {"kind": "finding", "signature_prefix": "sig_http_finding", "action": "retire", "actor": "bob"},
    )
    assert status == 200
    payload = json.loads(body.decode("utf-8"))
    entry = next(item for item in payload["findings"] if item["signature"] == "sig_http_finding")
    assert entry["review_state"] == "retired"


def test_post_memory_review_rule_missing_actor_is_a_clean_400(web_app, web_config) -> None:
    store = ResearchMemoryStore(web_config.paths.artifact_root)
    _promote_rule(store, signature="sig_http_no_actor")

    status, content_type, body = _post(
        web_app,
        "/api/memory/review/rule",
        {"signature_prefix": "sig_http_no_actor", "action": "activate", "actor": ""},
    )
    assert status == 400
    assert content_type == JSON_CONTENT_TYPE
    assert "actor is required" in json.loads(body.decode("utf-8"))["error"]


def test_post_memory_review_rule_unknown_action_is_a_clean_400(web_app, web_config) -> None:
    store = ResearchMemoryStore(web_config.paths.artifact_root)
    _promote_rule(store, signature="sig_http_bad_action")

    status, _, body = _post(
        web_app,
        "/api/memory/review/rule",
        {"signature_prefix": "sig_http_bad_action", "action": "retire", "actor": "alice"},
    )
    assert status == 400
    assert "rule review action must be one of" in json.loads(body.decode("utf-8"))["error"]


# ---------------------------------------------------------------------------
# P4B-F1: the JSON-string boundary check on every POST field
# ---------------------------------------------------------------------------


def test_memory_review_str_field_accepts_strings_and_treats_absent_keys_as_empty() -> None:
    assert web_routing._memory_review_str_field({"a": "hello"}, "a") == "hello"
    assert web_routing._memory_review_str_field({"a": ""}, "a") == ""
    assert web_routing._memory_review_str_field({}, "a") == ""


@pytest.mark.parametrize("bad_value", [None, 42, 3.14, {}, [], True])
def test_memory_review_str_field_rejects_every_non_string_json_type(bad_value) -> None:
    with pytest.raises(ValueError, match="must be a string"):
        web_routing._memory_review_str_field({"a": bad_value}, "a")


@pytest.mark.parametrize("bad_actor", [None, 42, {}], ids=["null", "number", "object"])
def test_post_memory_review_rule_non_string_actor_is_400_with_no_event_appended(
    web_app, web_config, bad_actor
) -> None:
    store = ResearchMemoryStore(web_config.paths.artifact_root)
    _promote_rule(store, signature="sig_http_bad_actor_type")
    events_path = store.review_events_path
    before = events_path.read_text(encoding="utf-8") if events_path.exists() else ""

    status, content_type, body = _post(
        web_app,
        "/api/memory/review/rule",
        {"signature_prefix": "sig_http_bad_actor_type", "action": "activate", "actor": bad_actor},
    )
    assert status == 400
    assert content_type == JSON_CONTENT_TYPE
    assert "actor must be a string" in json.loads(body.decode("utf-8"))["error"]

    after = events_path.read_text(encoding="utf-8") if events_path.exists() else ""
    assert after == before  # the action was never called: zero bytes appended


@pytest.mark.parametrize("bad_actor", [None, 42, {}], ids=["null", "number", "object"])
def test_post_memory_review_promoted_non_string_actor_is_400_with_no_event_appended(
    web_app, web_config, bad_actor
) -> None:
    store = ResearchMemoryStore(web_config.paths.artifact_root)
    _promote_finding_or_failure(store, signature="sig_http_bad_actor_promoted")
    events_path = store.review_events_path
    before = events_path.read_text(encoding="utf-8") if events_path.exists() else ""

    status, content_type, body = _post(
        web_app,
        "/api/memory/review/promoted",
        {"kind": "finding", "signature_prefix": "sig_http_bad_actor_promoted", "action": "retire", "actor": bad_actor},
    )
    assert status == 400
    assert content_type == JSON_CONTENT_TYPE
    assert "actor must be a string" in json.loads(body.decode("utf-8"))["error"]

    after = events_path.read_text(encoding="utf-8") if events_path.exists() else ""
    assert after == before


@pytest.mark.parametrize(
    "field,bad_value",
    [
        ("action", None),
        ("action", 7),
        ("kind", None),
        ("kind", []),
        ("signature_prefix", None),
        ("signature_prefix", 5),
        ("rationale", 5),
        ("rationale", {}),
    ],
)
def test_post_memory_review_promoted_rejects_every_non_string_field_with_no_event_appended(
    web_app, web_config, field, bad_value
) -> None:
    store = ResearchMemoryStore(web_config.paths.artifact_root)
    _promote_finding_or_failure(store, signature="sig_http_bad_field")
    events_path = store.review_events_path
    before = events_path.read_text(encoding="utf-8") if events_path.exists() else ""

    body = {"kind": "finding", "signature_prefix": "sig_http_bad_field", "action": "retire", "actor": "alice"}
    body[field] = bad_value
    status, content_type, response_body = _post(web_app, "/api/memory/review/promoted", body)
    assert status == 400
    assert content_type == JSON_CONTENT_TYPE
    assert f"{field} must be a string" in json.loads(response_body.decode("utf-8"))["error"]

    after = events_path.read_text(encoding="utf-8") if events_path.exists() else ""
    assert after == before


def test_memory_review_paths_do_not_leak_into_unrelated_routes(web_app) -> None:
    status, content_type, body = _get(web_app, "/api/memory/reviewX")
    assert status == 404
    assert content_type == JSON_CONTENT_TYPE
    assert "unknown API path" in json.loads(body.decode("utf-8"))["error"]


# ---------------------------------------------------------------------------
# html.py additive tab mount
# ---------------------------------------------------------------------------


def test_index_page_hosts_the_memory_tab_panel_and_script_entry(web_config) -> None:
    html = web_server._index_html(web_config)
    assert 'id="lab-tab-memory" aria-controls="lab-panel-memory" aria-selected="false" tabindex="-1"' in html
    assert 'id="lab-panel-memory" aria-labelledby="lab-tab-memory" tabindex="0" hidden' in html
    assert 'id="memory-result"' in html
    assert "记忆治理" in html
    assert '<script type="module" src="/static/views/memory.js"></script>' in html
    # Additive-only: appended strictly after the pre-existing extensions tab
    # and panel, never interleaved (the FE-track union merge stays small).
    assert html.index('id="lab-tab-extensions"') < html.index('id="lab-tab-memory"')
    assert html.index('id="lab-panel-extensions"') < html.index('id="lab-panel-memory"')
    assert html.index('src="/static/app.js"') < html.index('src="/static/views/memory.js"')


# ---------------------------------------------------------------------------
# P4B-F3: DOM regression -- the memory tab must deactivate when another tab
# becomes active, via EITHER path the real controller uses (hashchange, or
# a programmatic activateTab() that only mutates the DOM). A stdlib-only
# Node harness (the pattern tests/test_web_synthesis_view.py uses for its
# pure-renderer fixtures), extended here with a small stateful DOM/window
# shim because this bug is specifically about DOM/event interaction, not a
# pure function.
# ---------------------------------------------------------------------------

MEMORY_JS_PATH = web_server.STATIC_ROOT / "views" / "memory.js"

_MEMORY_TAB_HARNESS = r"""
// Minimal, stateful DOM/window shim -- NOT jsdom, NOT a browser -- just
// enough to exercise memory.js's tab-activation logic under plain Node.
// A synchronous MutationObserver replica invokes its callback the instant
// an observed attribute changes; real MutationObserver batches into a
// microtask, but that only changes WHEN the callback runs, never WHETHER
// the mutation is captured, which is all this harness needs to prove.

class FakeElement {
  constructor(id) {
    this.id = id;
    this._attrs = {};
    this.hidden = false;
    this.tabIndex = 0;
    this._listeners = {};
    this._innerHTML = '';
  }
  setAttribute(name, value) {
    this._attrs[name] = String(value);
    for (const observer of ACTIVE_OBSERVERS) observer._notify(this, name);
  }
  getAttribute(name) {
    return Object.prototype.hasOwnProperty.call(this._attrs, name) ? this._attrs[name] : null;
  }
  removeAttribute(name) { delete this._attrs[name]; }
  addEventListener(type, handler) { (this._listeners[type] = this._listeners[type] || []).push(handler); }
  get innerHTML() { return this._innerHTML; }
  set innerHTML(value) {
    // Poor-man's HTML parse: memory.js's ensureShell()/renderPayload()
    // assign a full HTML string, then getElementById() its mount points
    // (#mem-tables-mount, #mem-actor, ...) later -- a plain string
    // property write (no parsing) would leave every one of those ids
    // unresolvable. Registering a placeholder element for every id="..."
    // this markup introduces is sufficient for what this harness checks
    // (id resolvability, never deeper structure/text content).
    this._innerHTML = value;
    const idPattern = /id="([^"]+)"/g;
    let match;
    while ((match = idPattern.exec(value))) {
      if (!ELEMENTS.has(match[1])) makeElement(match[1], this);
    }
  }
}

const ACTIVE_OBSERVERS = [];
class FakeMutationObserver {
  constructor(callback) { this._callback = callback; this._watching = []; }
  observe(target, options) { this._watching.push({ target, options }); ACTIVE_OBSERVERS.push(this); }
  _notify(element, attrName) {
    for (const { target, options } of this._watching) {
      if (options.attributeFilter && !options.attributeFilter.includes(attrName)) continue;
      const isSelf = element === target;
      const isTrackedChild = options.subtree && CHILD_PARENT.get(element) === target;
      if (isSelf || isTrackedChild) this._callback([{ target: element, attributeName: attrName }]);
    }
  }
}
globalThis.MutationObserver = FakeMutationObserver;

const ELEMENTS = new Map();
const CHILD_PARENT = new Map();
function makeElement(id, parent) {
  const el = new FakeElement(id);
  ELEMENTS.set(id, el);
  if (parent) CHILD_PARENT.set(el, parent);
  return el;
}

const tablist = makeElement('__tablist__', null);
// The seven real top-level tab ids (six pre-existing + SE-P4b's memory tab)
// laid out exactly like the real .lab-tabs container: every tab button is
// a direct child, so subtree observation needs only one level.
const ALL_TAB_IDS = [
  'lab-tab-factor', 'lab-tab-history', 'lab-tab-data',
  'lab-tab-registry', 'lab-tab-docs', 'lab-tab-extensions', 'lab-tab-memory'
];
ALL_TAB_IDS.forEach(id => makeElement(id, tablist));
ALL_TAB_IDS.forEach(id => makeElement(id.replace('lab-tab-', 'lab-panel-'), null));
makeElement('memory-result', null);

globalThis.document = {
  getElementById: id => ELEMENTS.get(id) || null,
  querySelector: selector => (selector === '.lab-tabs' ? tablist : null),
  createElement: () => new FakeElement(''),
  head: { appendChild: () => {} }
};

const windowListeners = {};
globalThis.window = {
  location: { hash: '' },
  history: { replaceState: () => {} },
  sessionStorage: { getItem: () => null, setItem: () => {} },
  addEventListener: (type, handler) => { (windowListeners[type] = windowListeners[type] || []).push(handler); },
  prompt: () => null
};
function fireHashChange() { (windowListeners['hashchange'] || []).forEach(handler => handler({})); }

globalThis.fetch = async () => ({
  ok: true,
  json: async () => ({
    schema_version: 'qf.web.memory_review.v1', rules: [], findings: [], failures: [],
    priors: {
      schema_version: 'qf.research_priors.v1', query: {}, query_fingerprint: '', as_of: 0,
      total_envelopes: 0, total_evidence_runs: 0, oos_excluded: 0, invalid_rows: 0, tables: []
    },
    plugin: null
  })
});

// Plays views/lab.js's part by hand (memory.js only READS lab.js, never
// imports it -- see the module docstring): exactly what activateTab(tabId)
// does to the DOM -- every one of lab.js's OWN six tab ids gets its
// aria-selected/tabIndex set and its panel's hidden toggled, target true,
// all others false. lab-tab-memory is deliberately OUTSIDE this loop,
// exactly like the real lab.js TAB_IDS array (that omission is the root
// cause this whole finding is about).
const LAB_JS_TAB_IDS = ['lab-tab-factor', 'lab-tab-history', 'lab-tab-data', 'lab-tab-registry', 'lab-tab-docs', 'lab-tab-extensions'];
function simulateLabJsActivateTab(tabId) {
  LAB_JS_TAB_IDS.forEach(id => {
    const tab = ELEMENTS.get(id);
    const panel = ELEMENTS.get(id.replace('lab-tab-', 'lab-panel-'));
    const selected = id === tabId;
    tab.setAttribute('aria-selected', selected ? 'true' : 'false');
    tab.tabIndex = selected ? 0 : -1;
    panel.hidden = !selected;
  });
}

function selectedTabIds() {
  return ALL_TAB_IDS.filter(id => ELEMENTS.get(id).getAttribute('aria-selected') === 'true');
}
function visiblePanelIds() {
  return ALL_TAB_IDS.map(id => id.replace('lab-tab-', 'lab-panel-')).filter(id => ELEMENTS.get(id).hidden === false);
}

let failed = 0;
function check(name, cond, detail) {
  if (cond) { console.log('PASS ' + name); }
  else { failed++; console.log('FAIL ' + name + (detail ? ': ' + detail : '')); }
}

// --- import the REAL served module under the shim -------------------------
window.location.hash = '#lab-tab-memory';
await import(process.env.QF_MEMORY_URL);

check('setup.memory_self_activates_on_matching_hash',
  ELEMENTS.get('lab-tab-memory').getAttribute('aria-selected') === 'true'
  && ELEMENTS.get('lab-panel-memory').hidden === false);

// --- Scenario A: reviewer's literal reproduction -- hash to memory
// (above), then hash to data. Exercises BOTH fix mechanisms together, as a
// real browser would (lab.js's activateTab mutates the DOM, which routes
// through the MutationObserver; the hashchange dispatch independently
// reaches memory.js's own listener too -- both converge on the same
// correct end state, proving the overlap is harmless).
window.location.hash = '#lab-tab-data';
simulateLabJsActivateTab('lab-tab-data');
fireHashChange();
check('scenario_a.exactly_one_selected', selectedTabIds().length === 1, JSON.stringify(selectedTabIds()));
check('scenario_a.selected_is_data', selectedTabIds()[0] === 'lab-tab-data');
check('scenario_a.exactly_one_visible_panel', visiblePanelIds().length === 1, JSON.stringify(visiblePanelIds()));
check('scenario_a.visible_is_data_panel', visiblePanelIds()[0] === 'lab-panel-data');

// --- Scenario B: isolate the MutationObserver mechanism -- a purely
// PROGRAMMATIC activation with NO hashchange at all (mirrors app.js's
// Parse/Validate/RD button handlers, which call activateTab() through
// history.replaceState -- fires neither 'hashchange' nor 'popstate'). If
// only the hashchange fix existed, this scenario would fail.
window.location.hash = '#lab-tab-memory';
fireHashChange();
check('scenario_b.setup_memory_reselected', selectedTabIds()[0] === 'lab-tab-memory');
simulateLabJsActivateTab('lab-tab-registry');  // DOM mutation only, no hash touched, no event fired
check('scenario_b.exactly_one_selected_with_no_hashchange_event', selectedTabIds().length === 1, JSON.stringify(selectedTabIds()));
check('scenario_b.selected_is_registry', selectedTabIds()[0] === 'lab-tab-registry');
check('scenario_b.memory_panel_hidden', ELEMENTS.get('lab-panel-memory').hidden === true);
check('scenario_b.memory_tab_deselected', ELEMENTS.get('lab-tab-memory').getAttribute('aria-selected') === 'false');

// --- Scenario C: isolate the hashchange mechanism -- the hash changes to
// something UNRECOGNIZED and lab.js's own applyHash does NOTHING (its
// documented behavior: "Unknown hashes are ignored; the server-rendered
// default tab stays") -- no other tab's DOM ever changes, so the
// MutationObserver never fires. If only the MutationObserver fix existed,
// this scenario would fail: memory would stay visually selected forever.
window.location.hash = '#lab-tab-memory';
fireHashChange();
check('scenario_c.setup_memory_reselected', selectedTabIds()[0] === 'lab-tab-memory');
window.location.hash = '#totally-unrecognized-garbage';
fireHashChange();  // no simulateLabJsActivateTab call on purpose
check('scenario_c.memory_deactivated_with_no_other_tab_ever_selected',
  ELEMENTS.get('lab-tab-memory').getAttribute('aria-selected') === 'false'
  && ELEMENTS.get('lab-panel-memory').hidden === true);
check('scenario_c.no_tab_left_selected', selectedTabIds().length === 0);

console.log('FIXTURE RESULT: ' + failed + ' failed');
if (failed) process.exit(1);
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node runtime not available")
def test_node_dom_harness_memory_tab_deactivates_on_every_switch_path(tmp_path: Path) -> None:
    harness = tmp_path / "memory_tab_harness.mjs"
    harness.write_text(_MEMORY_TAB_HARNESS, encoding="utf-8")
    env = {"QF_MEMORY_URL": MEMORY_JS_PATH.resolve().as_uri()}
    result = subprocess.run(
        ["node", str(harness)],
        capture_output=True,
        text=True,
        env={**dict(__import__("os").environ), **env},
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    assert "FIXTURE RESULT: 0 failed" in result.stdout
    for marker in (
        "PASS setup.memory_self_activates_on_matching_hash",
        "PASS scenario_a.exactly_one_selected",
        "PASS scenario_a.selected_is_data",
        "PASS scenario_a.exactly_one_visible_panel",
        "PASS scenario_a.visible_is_data_panel",
        "PASS scenario_b.exactly_one_selected_with_no_hashchange_event",
        "PASS scenario_b.selected_is_registry",
        "PASS scenario_b.memory_panel_hidden",
        "PASS scenario_b.memory_tab_deselected",
        "PASS scenario_c.memory_deactivated_with_no_other_tab_ever_selected",
        "PASS scenario_c.no_tab_left_selected",
    ):
        assert marker in result.stdout, result.stdout

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
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

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

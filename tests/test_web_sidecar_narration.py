"""Sidecar narration + clarify + journal (agent_sidecar_frontend.md §5.5/§5.2/§5.6/§11).

WORKORDER P2 pins + acceptance:

- ref resolution failure = fail loud;
- chat is never the sole carrier of a number (journal + assertion);
- blocking clarify questions unanswered ⇒ no execution (function + HTTP /confirm);
- a superseded clarify answer keeps BOTH in provenance;
- LLM readiness is a genuine tri-state (unknown / unavailable / ready);
- ACCEPTANCE: the sidecar journal lands under artifact_root and a replay
  reproduces the same rendered cards.
"""

from __future__ import annotations

import http.client
import json
import threading

import pytest

from quant_forge.apps.web.narration import (
    ClarifyBlockedError,
    ClarifySession,
    SidecarSessionStore,
    UnresolvedNarrationRefError,
    active_component_ids_for,
    assert_action_suggestion_allowlisted,
    assert_chat_not_sole_number_carrier,
    assert_clarify_unblocked,
    llm_readiness,
    replay_rendered_cards,
    resolve_ref,
)
from quant_forge.apps.web.server import create_local_web_server
from quant_forge.apps.web import server as web_server
from quant_forge.apps.web.tools import SidecarJournal
from quant_forge.config import LLMProviderSettings, LLMSettings, QuantForgeConfig
from quant_forge.data.local import create_demo_workspace
from quant_forge.specs.narration import (
    ClarifyQuestion,
    NarrationNode,
    NarrationOption,
    NarrationRef,
    NumericNarrationArgError,
)


PIPELINE_A = "PL_" + "a" * 32


# --- pin: ref resolution fails loud -----------------------------------------


def test_ref_resolves_to_an_active_known_component() -> None:
    ref = NarrationRef(component_id="factor-tape", artifact_ref="eval.json")
    assert resolve_ref(ref, active_component_ids=frozenset({"factor-tape"})) is ref


def test_ref_to_unknown_component_fails_loud() -> None:
    ref = NarrationRef(component_id="secret-console", artifact_ref="eval.json")
    with pytest.raises(UnresolvedNarrationRefError):
        resolve_ref(ref, active_component_ids=frozenset({"secret-console"}))


def test_ref_to_inactive_component_fails_loud() -> None:
    ref = NarrationRef(component_id="report-comparison", artifact_ref="bt.json")
    # Known component, but not currently rendered (report not ready yet).
    with pytest.raises(UnresolvedNarrationRefError):
        resolve_ref(ref, active_component_ids=frozenset({"pipeline-card"}))


def test_ref_to_unproduced_artifact_fails_loud() -> None:
    ref = NarrationRef(component_id="factor-tape", artifact_ref="ghost.json")
    with pytest.raises(UnresolvedNarrationRefError):
        resolve_ref(
            ref,
            active_component_ids=frozenset({"factor-tape"}),
            known_artifact_refs=frozenset({"eval.json", "bt.json"}),
        )


def test_active_components_track_pipeline_status() -> None:
    assert active_component_ids_for(None) == frozenset()
    assert active_component_ids_for({"status": "awaiting_confirm"}) == frozenset({"pipeline-card"})
    completed = active_component_ids_for({"status": "completed"})
    assert {"pipeline-card", "factor-tape", "report-comparison"} <= completed


# --- pin: chat is never the sole carrier of a number ------------------------


def test_status_node_with_a_number_is_rejected() -> None:
    with pytest.raises(NumericNarrationArgError):
        assert_chat_not_sole_number_carrier([{"kind": "status", "message_key": "report.ic", "args": ["0.05"]}])


def test_numbers_travel_only_via_a_ref_node() -> None:
    # To "say" IC=0.05 the sidecar emits a ref; the value is NOT in the node.
    ref_node = NarrationNode(
        kind="ref", message_key="narration.ref.see", args=["IC"], ref=NarrationRef("factor-tape", "eval.json")
    ).to_dict()
    assert_chat_not_sole_number_carrier([ref_node])  # passes: no numeric leaf
    assert "0.05" not in json.dumps(ref_node)


def test_action_suggestion_must_name_an_allowlisted_tool() -> None:
    ok = NarrationNode(kind="action_suggestion", message_key="suggest.confirm", action="confirm_pipeline")
    assert_action_suggestion_allowlisted(ok)  # allowlisted -> fine
    off_list = NarrationNode(kind="action_suggestion", message_key="suggest.promote", action="promote_factor")
    with pytest.raises(UnresolvedNarrationRefError):
        assert_action_suggestion_allowlisted(off_list)


# --- pin: blocking clarify questions gate execution -------------------------


def _blocking_question() -> ClarifyQuestion:
    return ClarifyQuestion(
        question_key="clarify.mktcap.basis",
        tier="blocking",
        options=(NarrationOption("float", "流通市值", is_default=True), NarrationOption("total", "总市值")),
    )


def test_blocking_question_blocks_execution_until_answered() -> None:
    session = ClarifySession(PIPELINE_A)
    session.pose([_blocking_question()])
    assert session.blocking_unanswered() == ["clarify.mktcap.basis"]
    assert not session.is_executable()
    with pytest.raises(ClarifyBlockedError):
        session.assert_executable()
    session.answer("clarify.mktcap.basis", "total")
    assert session.is_executable()
    session.assert_executable()  # no raise


def test_semantic_question_never_blocks() -> None:
    session = ClarifySession(PIPELINE_A)
    session.pose(
        [ClarifyQuestion("clarify.x", "semantic", options=(NarrationOption("a", "A", is_default=True), NarrationOption("b", "B")))]
    )
    assert session.is_executable()  # semantic has a safe default; skippable


def test_skip_accepts_the_default_and_is_recorded() -> None:
    session = ClarifySession(PIPELINE_A)
    session.pose([_blocking_question()])
    answer = session.answer("clarify.mktcap.basis", skipped=True)
    assert answer.skipped and answer.chosen_option_id == "float"  # the default
    assert session.is_executable()


def test_none_session_is_trivially_unblocked() -> None:
    assert_clarify_unblocked(None)  # no interview happened (no-LLM degradation)


# --- pin: a superseded answer keeps BOTH in provenance ----------------------


def test_superseded_answer_keeps_both_in_provenance() -> None:
    session = ClarifySession(PIPELINE_A)
    session.pose([_blocking_question()])
    session.answer("clarify.mktcap.basis", "float")
    session.answer("clarify.mktcap.basis", "total")  # supersedes
    effective = session.effective_answers()
    assert effective["clarify.mktcap.basis"].chosen_option_id == "total"
    entries = session.provenance_entries()
    assert len(entries) == 2
    first, second = entries
    assert first.value == "float" and first.superseded_by == "total"
    assert second.value == "total" and second.superseded_by is None
    assert all(entry.source == "user_answer" for entry in entries)


# --- pin: LLM readiness tri-state -------------------------------------------


def test_readiness_unknown_when_redacted() -> None:
    assert llm_readiness(QuantForgeConfig(), redacted=True) == "unknown"


def test_readiness_unavailable_for_rule_provider() -> None:
    assert llm_readiness(QuantForgeConfig()) == "unavailable"


def test_readiness_unavailable_when_key_missing() -> None:
    provider = LLMProviderSettings(
        provider="deepseek", model="deepseek-chat", base_url="x", api_key_env="QF_NO_SUCH_KEY_ENV_XYZ", api_key_required=True
    )
    cfg = QuantForgeConfig(
        llm=LLMSettings(provider="deepseek", model="deepseek-chat", base_url="x", api_key_env="QF_NO_SUCH_KEY_ENV_XYZ", providers={"deepseek": provider})
    )
    assert llm_readiness(cfg) == "unavailable"


def test_readiness_ready_for_local_no_key_endpoint() -> None:
    provider = LLMProviderSettings(
        provider="local", model="local-model", base_url="x", api_key_required=False
    )
    cfg = QuantForgeConfig(
        llm=LLMSettings(provider="local", model="local-model", base_url="x", api_key_required=False, providers={"local": provider})
    )
    assert llm_readiness(cfg) == "ready"


# --- ACCEPTANCE: journal lands under artifact_root + replay reproduces cards -


def test_journal_replay_reproduces_the_same_cards(tmp_path) -> None:
    journal = SidecarJournal(tmp_path)
    node_a = NarrationNode(kind="status", message_key="sidecar.tool.list_factors", args=["list_factors"]).to_dict()
    node_b = NarrationNode(
        kind="ref", message_key="narration.ref.see", args=["IC"], ref=NarrationRef("factor-tape", "eval.json")
    ).to_dict()
    journal.record(PIPELINE_A, tool="list_factors", objective="scan", request_hash="h1", narration=(node_a,))
    journal.record(PIPELINE_A, tool="get_factor", objective="inspect", request_hash="h2", narration=(node_b,))
    # The journal file physically lands under artifact_root/sidecar/.
    journal_file = tmp_path / "sidecar" / f"{PIPELINE_A}.journal.jsonl"
    assert journal_file.exists()
    # Replay reconstructs the EXACT same rendered cards, in order.
    replayed = replay_rendered_cards(journal.rows(PIPELINE_A))
    assert [card.to_dict() for card in replayed] == [node_a, node_b]


def test_journal_row_carries_the_acceptance_fields(tmp_path) -> None:
    journal = SidecarJournal(tmp_path)
    row = journal.record(
        PIPELINE_A,
        tool="get_factor",
        objective="inspect a factor",
        input_refs={"factor_id": "FTR_ABCD1234"},
        request_hash="h",
        artifact_refs=({"kind": "factor", "factor_id": "FTR_ABCD1234"},),
        nav_target="factor-tape",
    )
    for key in ("tool", "objective", "input_refs", "request_hash", "artifact_refs", "nav_target", "narration"):
        assert key in row


# --- HTTP: the blocking gate + journal over the wire ------------------------


@pytest.fixture()
def web_config(tmp_path):
    create_demo_workspace(tmp_path / "demo")
    return QuantForgeConfig().resolve(tmp_path / "demo")


@pytest.fixture()
def web_app(web_config):
    server = create_local_web_server(host="127.0.0.1", port=0, config=web_config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"127.0.0.1:{server.server_address[1]}"
    try:
        yield base, web_config
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def _http(base: str, method: str, path: str, *, body=None, headers=None):
    conn = http.client.HTTPConnection(base, timeout=15)
    try:
        payload = json.dumps(body) if body is not None else None
        conn.request(method, path, body=payload, headers=headers or {})
        response = conn.getresponse()
        raw = response.read().decode("utf-8")
        return response.status, (json.loads(raw) if raw else {})
    finally:
        conn.close()


def _create_pipeline_over_http(base: str) -> dict:
    status, job = _http(base, "POST", "/api/jobs/parse-idea", body={"text": "小市值 非ST", "parser_mode": "rule"})
    assert status == 202, job
    job_id = job["job_id"]
    for _ in range(100):
        status, snapshot = _http(base, "GET", f"/api/jobs/{job_id}")
        if snapshot.get("status") == "completed":
            break
        threading.Event().wait(0.05)
    else:  # pragma: no cover
        raise AssertionError("parse job did not complete")
    status, record = _http(base, "POST", "/api/pipelines", body={"parse_job_id": job_id, "kind": "factor_study"})
    assert status == 201, record
    return record


def test_http_blocking_clarify_gate_then_answer_then_confirm(web_app) -> None:
    base, config = web_app
    record = _create_pipeline_over_http(base)
    pipeline_id = record["pipeline_id"]

    # The sidecar posed a blocking question (persisted where the server reads it).
    store = SidecarSessionStore(config.paths.artifact_root)
    session = ClarifySession(pipeline_id)
    session.pose([_blocking_question()])
    store.save(session)

    # Confirm is refused server-side while the blocking question is open (409).
    confirm_body = {"nonce": record["confirm"]["nonce"], "version": record["confirm"]["version"]}
    status, body = _http(base, "POST", f"/api/pipelines/{pipeline_id}/confirm", body=confirm_body)
    assert status == 409, body

    # Answer over HTTP (both answers recorded on supersede -> provenance).
    status, answered = _http(
        base, "POST", f"/api/sidecar/pipelines/{pipeline_id}/clarify", body={"question_key": "clarify.mktcap.basis", "option_id": "total"}
    )
    assert status == 200 and answered["clarify"]["executable"] is True

    # Now confirm proceeds (the same nonce/version is still valid).
    status, body = _http(base, "POST", f"/api/pipelines/{pipeline_id}/confirm", body=confirm_body)
    assert status == 200, body
    assert body["status"] in {"running", "completed", "paused_failure"}


def test_http_sidecar_journal_and_replay(web_app) -> None:
    base, config = web_app
    record = _create_pipeline_over_http(base)
    pipeline_id = record["pipeline_id"]
    _http(base, "POST", f"/api/sidecar/pipelines/{pipeline_id}/authorize", body={})
    status, invoked = _http(base, "POST", f"/api/sidecar/pipelines/{pipeline_id}/tools/list_factors", body={"arguments": {}, "objective": "scan"})
    assert status == 200, invoked
    # The journal physically lands under artifact_root/sidecar/.
    journal_file = config.paths.artifact_root / "sidecar" / f"{pipeline_id}.journal.jsonl"
    assert journal_file.exists()
    # GET session exposes the journal; replay reproduces the same cards.
    status, session_payload = _http(base, "GET", f"/api/sidecar/pipelines/{pipeline_id}/session")
    assert status == 200
    served_narration = [node for row in session_payload["journal"] for node in row["narration"]]
    replayed = replay_rendered_cards(SidecarJournal(config.paths.artifact_root).rows(pipeline_id))
    assert [card.to_dict() for card in replayed] == served_narration
    assert served_narration[0]["kind"] == "status"

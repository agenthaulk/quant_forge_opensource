"""Server-owned pipeline aggregate tests (agent_sidecar_frontend.md §2.3, WORKORDER P1,
phase-review qf-fe-p1-review-20260714).

Exercises the aggregate directly (no HTTP) against a real demo workspace: the
rule parser (zero LLM key) and the real evaluate/backtest chain, mirroring
``tests/test_web_workbench.py``'s existing pattern for
``run_idea_parse_workflow`` / ``run_idea_validation_workflow``.

Organized by the phase-review's own clusters so each test's finding is easy
to cross-reference:
- Cluster A (F1/F2/F8): exactly-once launch, confirm-epoch rotation, retry
  expiry-under-lock.
- Cluster B (F3/F4/F5): genuine per-value provenance, immutable baseline,
  load-time assertion (F3's per-field unit coverage lives in
  test_web_provenance.py).
- Cluster C (F6): pipeline_id-scoped snapshot isolation, publish-on-success,
  G3 protection, delete-on-failure/abort.
- Cluster D (F9/F10): RunIndex-backed crash recovery, durable journal.
- Cluster E (F7/F14): failure-exit forks, closed kind vocabulary.
"""

from __future__ import annotations

import threading
import time

import pytest

import quant_forge.apps.web.server as web_server
from quant_forge.apps.web.jobs import _WebJobManager
from quant_forge.apps.web.pipeline import (
    PipelineConflictError,
    PipelineNotFoundError,
    PipelineStore,
    _working_factor_id_for,
    cancel_pipeline,
    confirm_pipeline,
    create_pipeline,
    create_pipeline_as_fallback,
    fork_pipeline_from_failure,
    get_pipeline,
    list_active_pipelines,
    retry_pipeline,
    update_pipeline_parameters,
)
from quant_forge.apps.web.provenance import InvalidProvenanceError, ProvenanceEntry, provenance_by_field
from quant_forge.apps.web.server import run_idea_parse_workflow
from quant_forge.config import QuantForgeConfig
from quant_forge.data.local import create_demo_workspace
from quant_forge.factor_library.repository import FactorRepository
from quant_forge.lineage.store import RunIndex
from quant_forge.research_loop.config import ResearchLoopConfig
from quant_forge.specs.pipeline import (
    FACTOR_STUDY_STAGE_IDS,
    LEGAL_TRANSITIONS,
    PIPELINE_KINDS,
    ConfirmState,
    PipelineRecord,
    can_transition,
    initial_stages_for,
)


IDEA_TEXT = "非ST的小市值股票未来表现更好"


def _rd_config() -> ResearchLoopConfig:
    return ResearchLoopConfig()


def _run_job_sync(job_manager: _WebJobManager, kind: str, runner, *, timeout: float = 20.0) -> dict:
    started = job_manager.start(kind, runner)
    return _wait_job(job_manager, started["job_id"], timeout=timeout)


def _wait_job(job_manager: _WebJobManager, job_id: str, *, timeout: float = 20.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = job_manager.get(job_id)
        if job["status"] in {"completed", "failed", "cancelled"}:
            return job
        time.sleep(0.01)
    raise TimeoutError(f"job {job_id} did not reach a terminal status within {timeout}s")


def _new_env(tmp_path):
    create_demo_workspace(tmp_path / "demo")
    config = QuantForgeConfig().resolve(tmp_path / "demo")
    store = PipelineStore(config.paths.artifact_root)
    job_manager = _WebJobManager()
    return config, store, job_manager


def _parsed_job(config, job_manager, *, text: str = IDEA_TEXT) -> dict:
    return _run_job_sync(
        job_manager,
        "parse_idea",
        lambda cancel_event: run_idea_parse_workflow(config, text, parser_mode="rule"),
    )


def _parsed_job_with_request(config, job_manager, *, text: str = IDEA_TEXT) -> dict:
    """Mirrors apps/web/routing.py's own /api/jobs/parse-idea handler: the
    job's own ``request`` must be recorded for phase-review F3's per-field
    provenance to have real text to check (create_pipeline reads it off the
    job, never off a client claim)."""

    started = job_manager.start(
        "parse_idea",
        lambda cancel_event: run_idea_parse_workflow(config, text, parser_mode="rule"),
        request={"text": text, "parser_mode": "rule"},
    )
    return _wait_job(job_manager, started["job_id"])


# ---------------------------------------------------------------------------
# Legal-transition table (pure, no I/O)
# ---------------------------------------------------------------------------


def test_legal_transitions_cover_every_status_and_terminal_statuses_are_closed() -> None:
    for status in ("draft", "awaiting_confirm", "running", "paused_failure", "completed", "aborted", "expired"):
        assert status in LEGAL_TRANSITIONS
    for terminal in ("completed", "aborted", "expired"):
        assert LEGAL_TRANSITIONS[terminal] == frozenset()
    assert can_transition("draft", "awaiting_confirm") is True
    assert can_transition("awaiting_confirm", "running") is True
    assert can_transition("running", "completed") is True
    assert can_transition("running", "paused_failure") is True
    assert can_transition("paused_failure", "awaiting_confirm") is True
    # No skipping straight from awaiting_confirm to completed, and no
    # resurrecting a terminal status.
    assert can_transition("awaiting_confirm", "completed") is False
    assert can_transition("completed", "running") is False
    assert can_transition("aborted", "awaiting_confirm") is False


# ---------------------------------------------------------------------------
# F14: closed kind vocabulary at the schema layer, not just an API-level check
# ---------------------------------------------------------------------------


def test_rd_optimize_is_not_in_the_p1_kind_vocabulary_at_all() -> None:
    assert PIPELINE_KINDS == frozenset({"factor_study"})
    with pytest.raises(ValueError, match="unknown pipeline kind"):
        initial_stages_for("rd_optimize")


def test_pipeline_record_construction_rejects_rd_optimize_kind() -> None:
    with pytest.raises(ValueError, match="invalid pipeline kind"):
        PipelineRecord(
            pipeline_id="PL_" + "a" * 32,
            kind="rd_optimize",
            created_at="2026-01-01T00:00:00Z",
            expires_at="2026-01-02T00:00:00Z",
            status="draft",
            stages=(),
            input_hash="deadbeef",
            confirm=ConfirmState(nonce="n"),
        )


def test_pipeline_record_from_dict_rejects_rd_optimize_kind() -> None:
    payload = {
        "pipeline_id": "PL_" + "a" * 32,
        "kind": "rd_optimize",
        "created_at": "2026-01-01T00:00:00Z",
        "expires_at": "2026-01-02T00:00:00Z",
        "status": "draft",
        "stages": [],
        "input_hash": "deadbeef",
        "confirm": {"nonce": "n", "version": 1},
    }
    with pytest.raises(ValueError, match="invalid pipeline kind"):
        PipelineRecord.from_dict(payload)


# ---------------------------------------------------------------------------
# Create: honest stage granularity (D11) + zero-LLM parse artifact capture
# ---------------------------------------------------------------------------


def test_create_pipeline_from_a_completed_rule_parse_job_lands_in_awaiting_confirm(tmp_path) -> None:
    config, store, job_manager = _new_env(tmp_path)
    parse_job = _parsed_job_with_request(config, job_manager)

    pipeline = create_pipeline(store, job_manager=job_manager, parse_job_id=parse_job["job_id"], rd_config=_rd_config())

    assert pipeline.kind == "factor_study"
    assert pipeline.status == "awaiting_confirm"
    assert tuple(stage.stage_id for stage in pipeline.stages) == FACTOR_STUDY_STAGE_IDS
    assert pipeline.stage("parse").status == "completed"
    assert pipeline.stage("confirm").status == "active"
    assert pipeline.stage("compute").status == "pending"
    assert pipeline.stage("report").status == "pending"
    assert pipeline.factor["formula"] == "-rank(market_cap)"
    assert pipeline.parser["source"] == "rule"
    assert pipeline.confirm.nonce
    assert pipeline.confirm.version == 1
    assert pipeline.confirm.confirmed_at is None
    assert pipeline.input_hash
    # SE-ix reserved slot: present, empty, and folded into the hash contract.
    assert pipeline.planning_influence_hash == ""
    # phase-review F4: the immutable baseline is captured at creation and
    # equals the fresh, unedited parameter set.
    assert pipeline.original_parameters == pipeline.parameters
    # phase-review F6: a pipeline-exclusive working id exists from creation,
    # scoped by pipeline_id, distinct from the bare parser id.
    assert pipeline.working_factor_id
    assert pipeline.working_factor_id != pipeline.factor["factor_id"]
    assert pipeline.published_factor_id is None
    assert pipeline.revision >= 1


def test_create_pipeline_rejects_rd_optimize_in_p1(tmp_path) -> None:
    config, store, job_manager = _new_env(tmp_path)
    parse_job = _parsed_job(config, job_manager)

    with pytest.raises(ValueError, match="rd_optimize"):
        create_pipeline(
            store, job_manager=job_manager, parse_job_id=parse_job["job_id"], rd_config=_rd_config(), kind="rd_optimize"
        )


def test_create_pipeline_never_trusts_a_client_supplied_parser_claim(tmp_path) -> None:
    # FE-L3: create_pipeline takes a job id, not a parser/factor payload --
    # there is no parameter through which a caller could inject a forged
    # provenance claim. This test documents the contract at the call-site
    # level: the only inputs are (store, job_manager, parse_job_id, rd_config,
    # kind, parent_run_id -- the last one only carries fork/fallback lineage,
    # phase-review F7), so a "fake" client cannot pass `parser={"source":
    # "llm"}` at all.
    import inspect

    signature = inspect.signature(create_pipeline)
    assert set(signature.parameters) == {"store", "job_manager", "parse_job_id", "rd_config", "kind", "parent_run_id"}


def test_create_pipeline_requires_a_completed_parse_idea_job(tmp_path) -> None:
    config, store, job_manager = _new_env(tmp_path)
    running_job = job_manager.start("parse_idea", lambda cancel_event: (_ for _ in ()).throw(RuntimeError("boom")))
    # Force the job into a still-running-looking state is awkward to fake
    # cleanly; simplest honest probe is an unknown job id and a wrong kind.
    with pytest.raises(KeyError):
        create_pipeline(store, job_manager=job_manager, parse_job_id="not-a-real-job", rd_config=_rd_config())

    other_job = _run_job_sync(job_manager, "validate_idea", lambda cancel_event: (_ for _ in ()).throw(RuntimeError("x")))
    with pytest.raises(ValueError, match="not a parse_idea job"):
        create_pipeline(store, job_manager=job_manager, parse_job_id=other_job["job_id"], rd_config=_rd_config())
    _wait_job(job_manager, running_job["job_id"])


# ---------------------------------------------------------------------------
# Cluster A -- exactly-once launch + confirm epoch (F1, F2, F8)
# ---------------------------------------------------------------------------


def test_double_confirm_with_the_same_token_returns_the_same_run(tmp_path) -> None:
    config, store, job_manager = _new_env(tmp_path)
    parse_job = _parsed_job_with_request(config, job_manager)
    pipeline = create_pipeline(store, job_manager=job_manager, parse_job_id=parse_job["job_id"], rd_config=_rd_config())

    first = confirm_pipeline(
        config, store, pipeline.pipeline_id,
        nonce=pipeline.confirm.nonce, version=pipeline.confirm.version,
        job_manager=job_manager, rd_config=_rd_config(),
    )
    assert first.status == "running"
    job_id = first.stage("compute").child_job_id
    assert job_id
    assert [entry["field"] for entry in first.provenance]  # badges present from the first confirm
    assert first.attempt.number == 1

    # Simulates a double click / a second tab / a retried HTTP request: the
    # SAME (pipeline_id, nonce, version) arrives again.
    second = confirm_pipeline(
        config, store, pipeline.pipeline_id,
        nonce=pipeline.confirm.nonce, version=pipeline.confirm.version,
        job_manager=job_manager, rd_config=_rd_config(),
    )
    assert second.stage("compute").child_job_id == job_id
    assert second.confirm.confirmed_at == first.confirm.confirmed_at
    assert second.attempt.number == 1  # replay, not a second launch
    assert [entry["field"] for entry in second.provenance] == [entry["field"] for entry in first.provenance]

    _wait_job(job_manager, job_id)


def test_double_confirm_does_not_start_a_second_job(tmp_path) -> None:
    config, store, job_manager = _new_env(tmp_path)
    parse_job = _parsed_job_with_request(config, job_manager)
    pipeline = create_pipeline(store, job_manager=job_manager, parse_job_id=parse_job["job_id"], rd_config=_rd_config())

    confirmed = confirm_pipeline(
        config, store, pipeline.pipeline_id,
        nonce=pipeline.confirm.nonce, version=pipeline.confirm.version,
        job_manager=job_manager, rd_config=_rd_config(),
    )
    job = _wait_job(job_manager, confirmed.stage("compute").child_job_id)
    assert job["status"] == "completed"

    # Idempotent replay after the job already finished must still return the
    # completed run, not start a fresh compute.
    replay = confirm_pipeline(
        config, store, pipeline.pipeline_id,
        nonce=pipeline.confirm.nonce, version=pipeline.confirm.version,
        job_manager=job_manager, rd_config=_rd_config(),
    )
    assert replay.status == "completed"
    assert replay.stage("compute").child_job_id == confirmed.stage("compute").child_job_id


def test_confirm_with_a_stale_token_is_rejected(tmp_path) -> None:
    config, store, job_manager = _new_env(tmp_path)
    parse_job = _parsed_job_with_request(config, job_manager)
    pipeline = create_pipeline(store, job_manager=job_manager, parse_job_id=parse_job["job_id"], rd_config=_rd_config())

    with pytest.raises(PipelineConflictError, match="stale confirm token"):
        confirm_pipeline(
            config, store, pipeline.pipeline_id,
            nonce="not-the-real-nonce", version=pipeline.confirm.version,
            job_manager=job_manager, rd_config=_rd_config(),
        )
    with pytest.raises(PipelineConflictError, match="stale confirm token"):
        confirm_pipeline(
            config, store, pipeline.pipeline_id,
            nonce=pipeline.confirm.nonce, version=pipeline.confirm.version + 1,
            job_manager=job_manager, rd_config=_rd_config(),
        )


def test_confirm_on_an_already_completed_pipeline_with_a_stale_token_is_rejected(tmp_path) -> None:
    config, store, job_manager = _new_env(tmp_path)
    parse_job = _parsed_job_with_request(config, job_manager)
    pipeline = create_pipeline(store, job_manager=job_manager, parse_job_id=parse_job["job_id"], rd_config=_rd_config())
    confirmed = confirm_pipeline(
        config, store, pipeline.pipeline_id,
        nonce=pipeline.confirm.nonce, version=pipeline.confirm.version,
        job_manager=job_manager, rd_config=_rd_config(),
    )
    _wait_job(job_manager, confirmed.stage("compute").child_job_id)

    with pytest.raises(PipelineConflictError, match="not awaiting confirmation"):
        confirm_pipeline(
            config, store, pipeline.pipeline_id,
            nonce="a-completely-different-nonce", version=1,
            job_manager=job_manager, rd_config=_rd_config(),
        )


def test_editing_the_draft_rotates_the_confirm_token_and_invalidates_the_old_one(tmp_path) -> None:
    # phase-review F2: a token held by a tab that missed a later edit must be
    # provably stale, not merely "the same nonce that happens to still work".
    config, store, job_manager = _new_env(tmp_path)
    parse_job = _parsed_job_with_request(config, job_manager)
    pipeline = create_pipeline(store, job_manager=job_manager, parse_job_id=parse_job["job_id"], rd_config=_rd_config())
    stale_nonce, stale_version = pipeline.confirm.nonce, pipeline.confirm.version

    edited = update_pipeline_parameters(
        store, pipeline.pipeline_id, {"holding_days": 9}, job_manager=job_manager, config=config
    )
    assert edited.confirm.nonce != stale_nonce
    assert edited.confirm.version == stale_version + 1

    with pytest.raises(PipelineConflictError, match="stale confirm token"):
        confirm_pipeline(
            config, store, pipeline.pipeline_id,
            nonce=stale_nonce, version=stale_version,
            job_manager=job_manager, rd_config=_rd_config(),
        )
    # The CURRENT token still works.
    confirmed = confirm_pipeline(
        config, store, pipeline.pipeline_id,
        nonce=edited.confirm.nonce, version=edited.confirm.version,
        job_manager=job_manager, rd_config=_rd_config(),
    )
    assert confirmed.status == "running"
    _wait_job(job_manager, confirmed.stage("compute").child_job_id)


def test_attempt_number_increments_only_on_a_durable_launch_never_on_a_bare_retry(tmp_path, monkeypatch) -> None:
    # phase-review F1: attempt.number must reflect actual launches, not
    # retry clicks. A retry that is itself never reconfirmed must not have
    # spent an attempt number.
    config, store, job_manager = _new_env(tmp_path)
    parse_job = _parsed_job_with_request(config, job_manager)
    pipeline = create_pipeline(store, job_manager=job_manager, parse_job_id=parse_job["job_id"], rd_config=_rd_config())

    def fail_evaluate(*args, **kwargs):
        raise RuntimeError("synthetic failure for attempt-number test")

    monkeypatch.setattr(web_server, "evaluate_factor", fail_evaluate)
    confirmed = confirm_pipeline(
        config, store, pipeline.pipeline_id,
        nonce=pipeline.confirm.nonce, version=pipeline.confirm.version,
        job_manager=job_manager, rd_config=_rd_config(),
    )
    assert confirmed.attempt.number == 1
    _wait_job(job_manager, confirmed.stage("compute").child_job_id)
    monkeypatch.undo()

    retried_once = retry_pipeline(store, pipeline.pipeline_id, job_manager=job_manager, config=config)
    assert retried_once.attempt.number == 1  # retry itself spends nothing

    # Retry again without ever reconfirming -- still nothing spent, and the
    # token keeps rotating (each retry issues a fresh epoch).
    monkeypatch.setattr(web_server, "evaluate_factor", fail_evaluate)
    reconfirmed = confirm_pipeline(
        config, store, pipeline.pipeline_id,
        nonce=retried_once.confirm.nonce, version=retried_once.confirm.version,
        job_manager=job_manager, rd_config=_rd_config(),
    )
    assert reconfirmed.attempt.number == 2  # the SECOND durable launch
    _wait_job(job_manager, reconfirmed.stage("compute").child_job_id)
    monkeypatch.undo()

    retried_again = retry_pipeline(store, pipeline.pipeline_id, job_manager=job_manager, config=config)
    reconfirmed_again = confirm_pipeline(
        config, store, pipeline.pipeline_id,
        nonce=retried_again.confirm.nonce, version=retried_again.confirm.version,
        job_manager=job_manager, rd_config=_rd_config(),
    )
    assert reconfirmed_again.attempt.number == 3
    _wait_job(job_manager, reconfirmed_again.stage("compute").child_job_id)


def test_retry_reconciles_expiry_under_the_lock_and_rejects_an_expired_paused_failure(tmp_path, monkeypatch) -> None:
    # phase-review F8: a direct /retry on an already-expired paused_failure
    # must not resurrect it, even though this call path (unlike GET/list)
    # would otherwise skip straight to the plain status check.
    config, store, job_manager = _new_env(tmp_path)
    parse_job = _parsed_job_with_request(config, job_manager)
    pipeline = create_pipeline(store, job_manager=job_manager, parse_job_id=parse_job["job_id"], rd_config=_rd_config())

    def fail_evaluate(*args, **kwargs):
        raise RuntimeError("synthetic failure for expiry-under-lock test")

    monkeypatch.setattr(web_server, "evaluate_factor", fail_evaluate)
    confirmed = confirm_pipeline(
        config, store, pipeline.pipeline_id,
        nonce=pipeline.confirm.nonce, version=pipeline.confirm.version,
        job_manager=job_manager, rd_config=_rd_config(),
    )
    _wait_job(job_manager, confirmed.stage("compute").child_job_id)
    monkeypatch.undo()
    failed = get_pipeline(store, pipeline.pipeline_id, job_manager=job_manager, config=config)
    assert failed.status == "paused_failure"

    backdated = failed.with_updates(expires_at="2000-01-01T00:00:00Z")
    store.save(backdated, event="test_backdate")

    with pytest.raises(PipelineConflictError, match="not paused"):
        retry_pipeline(store, pipeline.pipeline_id, job_manager=job_manager, config=config)
    # The record itself is now honestly "expired", not silently stuck as a
    # resurrectable paused_failure.
    after = get_pipeline(store, pipeline.pipeline_id, job_manager=job_manager, config=config)
    assert after.status == "expired"


def test_confirmed_parameters_freeze_and_later_edits_are_next_attempt_only(tmp_path, monkeypatch) -> None:
    # "A run is live" is exercised via paused_failure rather than a mid-flight
    # race on `running`: the demo workspace's rule-parsed compute finishes in
    # well under a second, so racing an edit against it would be flaky. A
    # deterministically-failed run is equally non-terminal (spec §2.3: the
    # freeze only lifts on a NEW confirm) and lets this test be deterministic.
    config, store, job_manager = _new_env(tmp_path)
    parse_job = _parsed_job_with_request(config, job_manager)
    pipeline = create_pipeline(store, job_manager=job_manager, parse_job_id=parse_job["job_id"], rd_config=_rd_config())

    def fail_evaluate(*args, **kwargs):
        raise RuntimeError("synthetic failure to reach a non-terminal paused state")

    monkeypatch.setattr(web_server, "evaluate_factor", fail_evaluate)
    confirmed = confirm_pipeline(
        config, store, pipeline.pipeline_id,
        nonce=pipeline.confirm.nonce, version=pipeline.confirm.version,
        job_manager=job_manager, rd_config=_rd_config(), parameters={"holding_days": 7},
    )
    _wait_job(job_manager, confirmed.stage("compute").child_job_id)
    monkeypatch.undo()
    paused = get_pipeline(store, pipeline.pipeline_id, job_manager=job_manager, config=config)
    assert paused.status == "paused_failure"
    assert paused.confirmed_parameters["holding_days"] == 7

    edited = update_pipeline_parameters(
        store, pipeline.pipeline_id, {"holding_days": 12}, job_manager=job_manager, config=config
    )
    # The live/completed run's frozen snapshot never moves...
    assert edited.confirmed_parameters["holding_days"] == 7
    # ...while the draft (labeled 「仅用于下次尝试」 by the card) reflects the edit.
    assert edited.parameters["holding_days"] == 12


def test_confirm_override_is_not_shadowed_by_a_stale_nested_parameter_mirror(tmp_path) -> None:
    # Regression: apps/web/api.py::_default_validation_parameters also emits
    # nested evaluation.simulation.decay_days / backtest.simulation.decay_days
    # mirrors of the same flat decay_days value; _idea_validation_settings
    # merges a NESTED override after a flat one, so a naive implementation
    # that stored/forwarded both representations could let a stale nested
    # mirror silently win over a fresher flat confirm-time override.
    # apps/web/pipeline.py::_flat_parameters_only closes this by never
    # carrying the nested mirrors past parse capture. Proven end to end here
    # via the compute job's own reported simulation_profile.
    config, store, job_manager = _new_env(tmp_path)
    parse_job = _parsed_job_with_request(config, job_manager)
    pipeline = create_pipeline(store, job_manager=job_manager, parse_job_id=parse_job["job_id"], rd_config=_rd_config())
    assert pipeline.parameters["decay_days"] == 0
    assert "evaluation" not in pipeline.parameters
    assert "backtest" not in pipeline.parameters
    assert "transaction_costs" not in pipeline.parameters

    confirmed = confirm_pipeline(
        config, store, pipeline.pipeline_id,
        nonce=pipeline.confirm.nonce, version=pipeline.confirm.version,
        job_manager=job_manager, rd_config=_rd_config(), parameters={"decay_days": 3},
    )
    job = _wait_job(job_manager, confirmed.stage("compute").child_job_id)
    assert job["status"] == "completed"
    assert confirmed.confirmed_parameters["decay_days"] == 3
    assert job["result"]["backtest"]["simulation_profile"]["decay_days"] == 3


# ---------------------------------------------------------------------------
# Cluster B -- genuine per-value provenance (F4, F5; F3's per-field unit
# coverage lives in test_web_provenance.py)
# ---------------------------------------------------------------------------


def test_immutable_baseline_editing_one_field_never_moves_anothers_badge(tmp_path) -> None:
    # phase-review F4: attribution compares against original_parameters
    # (frozen at creation), never the mutable current draft -- editing decay
    # must never revert (or otherwise move) holding_days's badge.
    config, store, job_manager = _new_env(tmp_path)
    parse_job = _parsed_job_with_request(config, job_manager)
    pipeline = create_pipeline(store, job_manager=job_manager, parse_job_id=parse_job["job_id"], rd_config=_rd_config())

    after_first_edit = update_pipeline_parameters(
        store, pipeline.pipeline_id, {"holding_days": 9}, job_manager=job_manager, config=config
    )
    by_field = provenance_by_field(tuple(ProvenanceEntry(**entry) for entry in after_first_edit.provenance))
    assert by_field["holding_days"].source == "human_override"
    assert by_field["holding_days"].parent_value == pipeline.original_parameters["holding_days"]

    after_second_edit = update_pipeline_parameters(
        store, pipeline.pipeline_id, {"decay_days": 3}, job_manager=job_manager, config=config
    )
    by_field_2 = provenance_by_field(tuple(ProvenanceEntry(**entry) for entry in after_second_edit.provenance))
    # holding_days's badge and parent_value are untouched by decay_days's edit.
    assert by_field_2["holding_days"].source == "human_override"
    assert by_field_2["holding_days"].parent_value == pipeline.original_parameters["holding_days"]
    assert by_field_2["holding_days"].value == 9
    assert by_field_2["decay_days"].source == "human_override"

    # Reverting holding_days to its EXACT original value correctly un-marks
    # it (proving the comparison target is the fixed baseline, not "the
    # value from one edit ago").
    reverted = update_pipeline_parameters(
        store,
        pipeline.pipeline_id,
        {"holding_days": pipeline.original_parameters["holding_days"]},
        job_manager=job_manager,
        config=config,
    )
    by_field_3 = provenance_by_field(tuple(ProvenanceEntry(**entry) for entry in reverted.provenance))
    assert by_field_3["holding_days"].source != "human_override"
    assert by_field_3["decay_days"].source == "human_override"  # unrelated field unaffected


def test_pipeline_store_load_rejects_a_provenance_array_with_a_stale_value(tmp_path) -> None:
    # phase-review F5: a GET must never serve a confirm card whose badges
    # could be lying about the numbers next to them.
    config, store, job_manager = _new_env(tmp_path)
    parse_job = _parsed_job_with_request(config, job_manager)
    pipeline = create_pipeline(store, job_manager=job_manager, parse_job_id=parse_job["job_id"], rd_config=_rd_config())

    tampered_provenance = tuple(
        {**entry, "value": "not-the-real-value"} if entry["field"] == "formula" else entry
        for entry in pipeline.provenance
    )
    tampered = pipeline.with_updates(provenance=tampered_provenance)
    store.save(tampered, event="test_tamper_stale_value")

    with pytest.raises(InvalidProvenanceError, match="stale provenance"):
        store.load(pipeline.pipeline_id)


def test_pipeline_store_load_rejects_a_provenance_array_missing_a_field(tmp_path) -> None:
    config, store, job_manager = _new_env(tmp_path)
    parse_job = _parsed_job_with_request(config, job_manager)
    pipeline = create_pipeline(store, job_manager=job_manager, parse_job_id=parse_job["job_id"], rd_config=_rd_config())

    truncated = pipeline.with_updates(provenance=pipeline.provenance[1:])
    store.save(truncated, event="test_tamper_missing_field")

    with pytest.raises(InvalidProvenanceError, match="missing provenance badge"):
        store.load(pipeline.pipeline_id)


def test_pipeline_store_load_rejects_a_duplicate_provenance_field(tmp_path) -> None:
    config, store, job_manager = _new_env(tmp_path)
    parse_job = _parsed_job_with_request(config, job_manager)
    pipeline = create_pipeline(store, job_manager=job_manager, parse_job_id=parse_job["job_id"], rd_config=_rd_config())

    duplicated = pipeline.with_updates(provenance=pipeline.provenance + (pipeline.provenance[0],))
    store.save(duplicated, event="test_tamper_duplicate_field")

    with pytest.raises(InvalidProvenanceError, match="duplicate provenance field"):
        store.load(pipeline.pipeline_id)


# ---------------------------------------------------------------------------
# Rejoin (refresh / server restart)
# ---------------------------------------------------------------------------


def test_list_active_pipelines_includes_awaiting_confirm_and_excludes_terminal(tmp_path) -> None:
    config, store, job_manager = _new_env(tmp_path)
    parse_job = _parsed_job_with_request(config, job_manager)
    awaiting = create_pipeline(store, job_manager=job_manager, parse_job_id=parse_job["job_id"], rd_config=_rd_config())
    completed_pipeline = create_pipeline(
        store, job_manager=job_manager, parse_job_id=parse_job["job_id"], rd_config=_rd_config()
    )
    confirmed = confirm_pipeline(
        config, store, completed_pipeline.pipeline_id,
        nonce=completed_pipeline.confirm.nonce, version=completed_pipeline.confirm.version,
        job_manager=job_manager, rd_config=_rd_config(), parameters={"holding_days": 9},
    )
    _wait_job(job_manager, confirmed.stage("compute").child_job_id)

    # rv2 semantics: the FIRST list observes the running->completed
    # transition (returned once, so the caller can fetch /report); every
    # later list excludes the now-terminal record.
    first = {record.pipeline_id: record for record in list_active_pipelines(store, job_manager=job_manager, config=config)}
    assert awaiting.pipeline_id in first
    assert first[completed_pipeline.pipeline_id].status == "completed"

    second = {record.pipeline_id for record in list_active_pipelines(store, job_manager=job_manager, config=config)}
    assert awaiting.pipeline_id in second
    assert completed_pipeline.pipeline_id not in second


def test_rejoin_after_a_simulated_server_restart_reconciles_to_paused_failure(tmp_path, monkeypatch) -> None:
    # A "server restart" wipes the in-memory _WebJobManager but the pipeline
    # snapshot on disk survives -- the durability contract under test. The
    # compute is deliberately still BLOCKED (never reached the point where
    # _record_validate_factor_runs would write anything to the RunIndex), so
    # F9's independent RunIndex check correctly finds nothing and this still
    # reconciles honestly to JOB_NOT_FOUND -- distinct from the F9
    # crash-recovery test below, which asserts the opposite outcome for a
    # genuinely-finished compute.
    config, store, job_manager = _new_env(tmp_path)
    parse_job = _parsed_job_with_request(config, job_manager)
    pipeline = create_pipeline(store, job_manager=job_manager, parse_job_id=parse_job["job_id"], rd_config=_rd_config())

    started = threading.Event()
    release = threading.Event()

    def blocking_evaluate(*args, **kwargs):
        started.set()
        release.wait(timeout=10)
        raise RuntimeError("stopped for test")

    monkeypatch.setattr(web_server, "evaluate_factor", blocking_evaluate)
    confirmed = confirm_pipeline(
        config, store, pipeline.pipeline_id,
        nonce=pipeline.confirm.nonce, version=pipeline.confirm.version,
        job_manager=job_manager, rd_config=_rd_config(),
    )
    assert confirmed.status == "running"
    assert started.wait(timeout=5)

    fresh_job_manager = _WebJobManager()  # simulates the restart: no jobs known
    fresh_store = PipelineStore(config.paths.artifact_root)  # simulates a fresh process reading the same disk state

    rejoined = get_pipeline(fresh_store, pipeline.pipeline_id, job_manager=fresh_job_manager, config=config)
    assert rejoined.status == "paused_failure"
    assert rejoined.failure is not None
    assert rejoined.failure.reason_code == "JOB_NOT_FOUND"
    # The card can still redisplay the original formula/params after rejoin
    # -- rejoin must never strand the user with an empty card.
    assert rejoined.factor["formula"] == "-rank(market_cap)"
    # F6: JOB_NOT_FOUND cleanup leaves zero registry residue.
    with pytest.raises(FileNotFoundError):
        FactorRepository(config.paths.factor_root).get(pipeline.working_factor_id)

    release.set()
    _wait_job(job_manager, confirmed.stage("compute").child_job_id)
    monkeypatch.undo()


def test_get_pipeline_reconciles_a_completed_job_into_the_report_stage(tmp_path) -> None:
    config, store, job_manager = _new_env(tmp_path)
    parse_job = _parsed_job_with_request(config, job_manager)
    pipeline = create_pipeline(store, job_manager=job_manager, parse_job_id=parse_job["job_id"], rd_config=_rd_config())
    confirmed = confirm_pipeline(
        config, store, pipeline.pipeline_id,
        nonce=pipeline.confirm.nonce, version=pipeline.confirm.version,
        job_manager=job_manager, rd_config=_rd_config(),
    )
    _wait_job(job_manager, confirmed.stage("compute").child_job_id)

    reconciled = get_pipeline(store, pipeline.pipeline_id, job_manager=job_manager, config=config)
    assert reconciled.status == "completed"
    assert reconciled.stage("compute").status == "completed"
    assert reconciled.stage("report").status == "completed"
    assert any(ref.kind == "report" for ref in reconciled.artifact_refs)
    assert reconciled.published_factor_id == pipeline.factor["factor_id"]


# ---------------------------------------------------------------------------
# Cluster D -- durable rejoin (F9)
# ---------------------------------------------------------------------------


def test_rejoin_after_restart_recognizes_a_genuinely_completed_job_via_completion_artifact(tmp_path) -> None:
    # phase-review F9 + re-verify RV-F4: a completed-but-pruned job, or any
    # job after a server restart, must NOT be rewritten as a JOB_NOT_FOUND
    # failure -- the ATTEMPT-SCOPED completion artifact the compute job
    # itself wrote at the moment of success is the independent proof (the
    # earlier unscoped RunIndex row-count probe could credit a prior
    # attempt's rows to the current attempt).
    config, store, job_manager = _new_env(tmp_path)
    parse_job = _parsed_job_with_request(config, job_manager)
    pipeline = create_pipeline(store, job_manager=job_manager, parse_job_id=parse_job["job_id"], rd_config=_rd_config())
    confirmed = confirm_pipeline(
        config, store, pipeline.pipeline_id,
        nonce=pipeline.confirm.nonce, version=pipeline.confirm.version,
        job_manager=job_manager, rd_config=_rd_config(),
    )
    job = _wait_job(job_manager, confirmed.stage("compute").child_job_id)
    assert job["status"] == "completed"

    # Nothing has reconciled this pipeline since confirm -- the on-disk
    # snapshot still says "running". Simulate the restart: a fresh job
    # manager that has never heard of this job id, reading the SAME disk
    # state (including the RunIndex rows the real compute already wrote).
    fresh_job_manager = _WebJobManager()
    fresh_store = PipelineStore(config.paths.artifact_root)
    still_running_on_disk = fresh_store.load(pipeline.pipeline_id)
    assert still_running_on_disk.status == "running"

    rejoined = get_pipeline(fresh_store, pipeline.pipeline_id, job_manager=fresh_job_manager, config=config)
    assert rejoined.status == "completed"
    assert rejoined.published_factor_id == pipeline.factor["factor_id"]
    # And the working row was still cleaned up via the crash-recovery path.
    with pytest.raises(FileNotFoundError):
        FactorRepository(config.paths.factor_root).get(pipeline.working_factor_id)


# ---------------------------------------------------------------------------
# Unknown pipeline id / containment
# ---------------------------------------------------------------------------


def test_unknown_pipeline_id_raises_not_found(tmp_path) -> None:
    config, store, job_manager = _new_env(tmp_path)
    with pytest.raises(PipelineNotFoundError):
        get_pipeline(store, "PL_" + "0" * 32, job_manager=job_manager, config=config)


@pytest.mark.parametrize(
    "probe",
    ["../../etc/passwd", "PL_short", "not-even-close", "PL_" + "z" * 32, ""],
)
def test_malformed_pipeline_id_is_rejected_before_touching_the_filesystem(tmp_path, probe: str) -> None:
    config, store, job_manager = _new_env(tmp_path)
    with pytest.raises(PipelineNotFoundError):
        get_pipeline(store, probe, job_manager=job_manager, config=config)


# ---------------------------------------------------------------------------
# Expiry
# ---------------------------------------------------------------------------


def test_expired_draft_reconciles_to_expired_on_read(tmp_path) -> None:
    config, store, job_manager = _new_env(tmp_path)
    parse_job = _parsed_job_with_request(config, job_manager)
    pipeline = create_pipeline(store, job_manager=job_manager, parse_job_id=parse_job["job_id"], rd_config=_rd_config())

    stale = pipeline.with_updates(expires_at="2000-01-01T00:00:00Z")
    store.save(stale, event="test_backdate")

    reconciled = get_pipeline(store, pipeline.pipeline_id, job_manager=job_manager, config=config)
    assert reconciled.status == "expired"


def test_expired_pipelines_are_reported_once_then_excluded_from_list_active(tmp_path) -> None:
    # rv2 semantics: a record that transitions to terminal INSIDE a list
    # call's own reconcile pass is returned that once (so the client that
    # triggered the discovery actually sees the transition -- the same rule
    # that lets a restart-recovered completion surface its report), and is
    # excluded from every later listing.
    config, store, job_manager = _new_env(tmp_path)
    parse_job = _parsed_job_with_request(config, job_manager)
    pipeline = create_pipeline(store, job_manager=job_manager, parse_job_id=parse_job["job_id"], rd_config=_rd_config())
    stale = pipeline.with_updates(expires_at="2000-01-01T00:00:00Z")
    store.save(stale, event="test_backdate")

    first = {record.pipeline_id: record for record in list_active_pipelines(store, job_manager=job_manager, config=config)}
    assert first[pipeline.pipeline_id].status == "expired"  # surfaced once, honestly terminal

    second = {record.pipeline_id for record in list_active_pipelines(store, job_manager=job_manager, config=config)}
    assert pipeline.pipeline_id not in second


# ---------------------------------------------------------------------------
# Cancel + retry
# ---------------------------------------------------------------------------


def test_cancel_awaiting_confirm_pipeline_is_terminal(tmp_path) -> None:
    config, store, job_manager = _new_env(tmp_path)
    parse_job = _parsed_job_with_request(config, job_manager)
    pipeline = create_pipeline(store, job_manager=job_manager, parse_job_id=parse_job["job_id"], rd_config=_rd_config())

    cancelled = cancel_pipeline(store, pipeline.pipeline_id, job_manager=job_manager, config=config)
    assert cancelled.status == "aborted"
    # Idempotent: cancelling an already-terminal pipeline is a no-op, not an error.
    again = cancel_pipeline(store, pipeline.pipeline_id, job_manager=job_manager, config=config)
    assert again.status == "aborted"


def test_cancel_while_running_synchronously_deletes_the_working_factor_row(tmp_path, monkeypatch) -> None:
    # phase-review F6: an aborted pipeline must leave zero registry residue,
    # and this must not depend on the background job's own eventual
    # cooperation -- cancel_pipeline deletes the row itself, synchronously,
    # before the still-blocked compute thread ever notices it was asked to
    # stop.
    config, store, job_manager = _new_env(tmp_path)
    parse_job = _parsed_job_with_request(config, job_manager)
    pipeline = create_pipeline(store, job_manager=job_manager, parse_job_id=parse_job["job_id"], rd_config=_rd_config())
    repo = FactorRepository(config.paths.factor_root)

    started = threading.Event()
    release = threading.Event()

    def blocking_evaluate(*args, **kwargs):
        started.set()
        release.wait(timeout=10)
        raise RuntimeError("stopped for test")

    monkeypatch.setattr(web_server, "evaluate_factor", blocking_evaluate)
    confirmed = confirm_pipeline(
        config, store, pipeline.pipeline_id,
        nonce=pipeline.confirm.nonce, version=pipeline.confirm.version,
        job_manager=job_manager, rd_config=_rd_config(),
    )
    assert started.wait(timeout=5)
    working_id = confirmed.working_factor_id
    assert repo.get(working_id) is not None  # registered while running

    cancelled = cancel_pipeline(store, pipeline.pipeline_id, job_manager=job_manager, config=config)
    assert cancelled.status == "aborted"
    with pytest.raises(FileNotFoundError):
        repo.get(working_id)

    release.set()
    _wait_job(job_manager, confirmed.stage("compute").child_job_id)
    monkeypatch.undo()


def test_retry_after_failure_reuses_parse_and_issues_a_new_confirm_token(tmp_path, monkeypatch) -> None:
    config, store, job_manager = _new_env(tmp_path)
    parse_job = _parsed_job_with_request(config, job_manager)
    pipeline = create_pipeline(store, job_manager=job_manager, parse_job_id=parse_job["job_id"], rd_config=_rd_config())

    def fail_evaluate(*args, **kwargs):
        raise RuntimeError("synthetic failure for retry test")

    monkeypatch.setattr(web_server, "evaluate_factor", fail_evaluate)
    confirmed = confirm_pipeline(
        config, store, pipeline.pipeline_id,
        nonce=pipeline.confirm.nonce, version=pipeline.confirm.version,
        job_manager=job_manager, rd_config=_rd_config(),
    )
    _wait_job(job_manager, confirmed.stage("compute").child_job_id)
    failed = get_pipeline(store, pipeline.pipeline_id, job_manager=job_manager, config=config)
    assert failed.status == "paused_failure"
    assert failed.failure is not None

    monkeypatch.undo()
    retried = retry_pipeline(store, pipeline.pipeline_id, job_manager=job_manager, config=config)
    assert retried.status == "awaiting_confirm"
    assert retried.stage("parse").status == "completed"  # reused, not re-run
    assert retried.stage("compute").status == "pending"
    assert retried.stage("compute").child_job_id is None
    assert retried.confirm.nonce != pipeline.confirm.nonce
    # phase-review F1: retry itself never spends an attempt number.
    assert retried.attempt.number == 1
    assert retried.failure is None

    reconfirmed = confirm_pipeline(
        config, store, pipeline.pipeline_id,
        nonce=retried.confirm.nonce, version=retried.confirm.version,
        job_manager=job_manager, rd_config=_rd_config(),
    )
    assert reconfirmed.attempt.number == 2  # the actual second launch
    job = _wait_job(job_manager, reconfirmed.stage("compute").child_job_id)
    assert job["status"] == "completed"


# ---------------------------------------------------------------------------
# Cluster C -- snapshot isolation redesign (F6)
# ---------------------------------------------------------------------------


def test_working_factor_id_is_scoped_by_pipeline_id_not_input_content() -> None:
    a = _working_factor_id_for("PL_" + "a" * 32, "FTR_ABCDEFGH")
    b = _working_factor_id_for("PL_" + "b" * 32, "FTR_ABCDEFGH")
    same_a_again = _working_factor_id_for("PL_" + "a" * 32, "FTR_ABCDEFGH")
    assert a != b  # different pipelines, same parsed factor id -> never share a row
    assert a == same_a_again  # same pipeline id -> deterministic, stable across retries
    import re

    assert re.fullmatch(r"[A-Za-z][A-Za-z0-9_=-]*", a)


def test_two_pipelines_from_the_same_idea_get_distinct_working_ids_from_creation(tmp_path) -> None:
    config, store, job_manager = _new_env(tmp_path)
    parse_job = _parsed_job_with_request(config, job_manager)
    pipeline_a = create_pipeline(store, job_manager=job_manager, parse_job_id=parse_job["job_id"], rd_config=_rd_config())
    pipeline_b = create_pipeline(store, job_manager=job_manager, parse_job_id=parse_job["job_id"], rd_config=_rd_config())
    # Distinct even though BOTH are still unedited drafts of the identical
    # parse result -- pipeline_id scoping never depends on confirm-time
    # parameters differing, unlike the input-hash scheme this replaces.
    assert pipeline_a.working_factor_id != pipeline_b.working_factor_id


def test_a_failing_pipelines_cleanup_does_not_clobber_a_parallel_pipelines_published_factor(tmp_path, monkeypatch) -> None:
    config, store, job_manager = _new_env(tmp_path)
    parse_job = _parsed_job_with_request(config, job_manager)
    repo = FactorRepository(config.paths.factor_root)
    canonical_id = str(parse_job["result"]["factor"]["factor_id"])

    pipeline_a = create_pipeline(store, job_manager=job_manager, parse_job_id=parse_job["job_id"], rd_config=_rd_config())
    pipeline_b = create_pipeline(store, job_manager=job_manager, parse_job_id=parse_job["job_id"], rd_config=_rd_config())
    assert pipeline_a.pipeline_id != pipeline_b.pipeline_id
    assert pipeline_a.working_factor_id != pipeline_b.working_factor_id

    confirmed_b = confirm_pipeline(
        config, store, pipeline_b.pipeline_id,
        nonce=pipeline_b.confirm.nonce, version=pipeline_b.confirm.version,
        job_manager=job_manager, rd_config=_rd_config(), parameters={"holding_days": 11},
    )
    job_b = _wait_job(job_manager, confirmed_b.stage("compute").child_job_id)
    assert job_b["status"] == "completed"
    completed_b = get_pipeline(store, pipeline_b.pipeline_id, job_manager=job_manager, config=config)
    assert completed_b.status == "completed"
    assert completed_b.published_factor_id == canonical_id
    row_b_after_publish = repo.get(canonical_id)

    def fail_evaluate(*args, **kwargs):
        raise RuntimeError("synthetic evaluate failure for isolation test")

    monkeypatch.setattr(web_server, "evaluate_factor", fail_evaluate)
    confirmed_a = confirm_pipeline(
        config, store, pipeline_a.pipeline_id,
        nonce=pipeline_a.confirm.nonce, version=pipeline_a.confirm.version,
        job_manager=job_manager, rd_config=_rd_config(), parameters={"holding_days": 4},
    )
    job_a = _wait_job(job_manager, confirmed_a.stage("compute").child_job_id)
    assert job_a["status"] == "failed"
    monkeypatch.undo()

    failed_a = get_pipeline(store, pipeline_a.pipeline_id, job_manager=job_manager, config=config)
    assert failed_a.status == "paused_failure"
    assert failed_a.published_factor_id is None

    # B's already-published canonical row is completely untouched by A's
    # failure -- A never shared a working_factor_id with B in the first
    # place (pipeline_id scoping from creation, not confirm-time input
    # hashing), so there is structurally nothing for A's cleanup to collide
    # with.
    assert repo.get(canonical_id) == row_b_after_publish
    # A's own working row leaves zero residue (#003-class defect closed).
    with pytest.raises(FileNotFoundError):
        repo.get(pipeline_a.working_factor_id)


def test_publish_declines_to_overwrite_an_already_promoted_canonical_factor(tmp_path) -> None:
    # G3: a completed background pipeline must never silently demote or
    # rewrite a candidate/active factor a human already promoted.
    config, store, job_manager = _new_env(tmp_path)
    parse_job = _parsed_job_with_request(config, job_manager)
    repo = FactorRepository(config.paths.factor_root)
    canonical_id = str(parse_job["result"]["factor"]["factor_id"])

    first = create_pipeline(store, job_manager=job_manager, parse_job_id=parse_job["job_id"], rd_config=_rd_config())
    confirmed_first = confirm_pipeline(
        config, store, first.pipeline_id,
        nonce=first.confirm.nonce, version=first.confirm.version,
        job_manager=job_manager, rd_config=_rd_config(),
    )
    _wait_job(job_manager, confirmed_first.stage("compute").child_job_id)
    completed_first = get_pipeline(store, first.pipeline_id, job_manager=job_manager, config=config)
    assert completed_first.published_factor_id == canonical_id

    repo.promote(canonical_id, "candidate", "human review")
    assert repo.get(canonical_id).status == "candidate"

    second = create_pipeline(store, job_manager=job_manager, parse_job_id=parse_job["job_id"], rd_config=_rd_config())
    confirmed_second = confirm_pipeline(
        config, store, second.pipeline_id,
        nonce=second.confirm.nonce, version=second.confirm.version,
        job_manager=job_manager, rd_config=_rd_config(), parameters={"holding_days": 8},
    )
    _wait_job(job_manager, confirmed_second.stage("compute").child_job_id)
    completed_second = get_pipeline(store, second.pipeline_id, job_manager=job_manager, config=config)

    assert completed_second.status == "completed"
    assert completed_second.published_factor_id is None  # publish declined
    assert repo.get(canonical_id).status == "candidate"  # untouched by the pipeline
    # The second pipeline's own working row still leaves zero residue even
    # though publish was declined.
    with pytest.raises(FileNotFoundError):
        repo.get(completed_second.working_factor_id)


# ---------------------------------------------------------------------------
# Cluster E -- failure exits (F7)
# ---------------------------------------------------------------------------


def test_fork_from_failure_creates_a_new_draft_with_parent_lineage_and_aborts_the_old(tmp_path, monkeypatch) -> None:
    config, store, job_manager = _new_env(tmp_path)
    parse_job = _parsed_job_with_request(config, job_manager)
    pipeline = create_pipeline(store, job_manager=job_manager, parse_job_id=parse_job["job_id"], rd_config=_rd_config())

    def fail_evaluate(*args, **kwargs):
        raise RuntimeError("synthetic failure for fork test")

    monkeypatch.setattr(web_server, "evaluate_factor", fail_evaluate)
    confirmed = confirm_pipeline(
        config, store, pipeline.pipeline_id,
        nonce=pipeline.confirm.nonce, version=pipeline.confirm.version,
        job_manager=job_manager, rd_config=_rd_config(), parameters={"holding_days": 6},
    )
    _wait_job(job_manager, confirmed.stage("compute").child_job_id)
    monkeypatch.undo()
    failed = get_pipeline(store, pipeline.pipeline_id, job_manager=job_manager, config=config)
    assert failed.status == "paused_failure"

    forked = fork_pipeline_from_failure(
        store, pipeline.pipeline_id, job_manager=job_manager, config=config, rd_config=_rd_config()
    )
    assert forked.pipeline_id != pipeline.pipeline_id
    assert forked.status == "awaiting_confirm"
    assert forked.attempt.number == 1
    assert forked.attempt.parent_run_id == pipeline.pipeline_id
    # The frozen (failed-attempt) parameters carry over as the new baseline.
    assert forked.parameters["holding_days"] == 6
    assert forked.original_parameters["holding_days"] == 6
    assert forked.working_factor_id != pipeline.working_factor_id

    old_after_fork = get_pipeline(store, pipeline.pipeline_id, job_manager=job_manager, config=config)
    assert old_after_fork.status == "aborted"

    # The forked draft is independently confirmable.
    reconfirmed = confirm_pipeline(
        config, store, forked.pipeline_id,
        nonce=forked.confirm.nonce, version=forked.confirm.version,
        job_manager=job_manager, rd_config=_rd_config(),
    )
    job = _wait_job(job_manager, reconfirmed.stage("compute").child_job_id)
    assert job["status"] == "completed"


def test_fork_from_failure_rejects_a_non_paused_pipeline(tmp_path) -> None:
    config, store, job_manager = _new_env(tmp_path)
    parse_job = _parsed_job_with_request(config, job_manager)
    pipeline = create_pipeline(store, job_manager=job_manager, parse_job_id=parse_job["job_id"], rd_config=_rd_config())

    with pytest.raises(PipelineConflictError, match="not paused"):
        fork_pipeline_from_failure(store, pipeline.pipeline_id, job_manager=job_manager, config=config, rd_config=_rd_config())


def test_fallback_to_rule_parse_creates_a_new_pipeline_with_parent_lineage_and_aborts_the_old(tmp_path, monkeypatch) -> None:
    config, store, job_manager = _new_env(tmp_path)
    parse_job = _parsed_job_with_request(config, job_manager)
    pipeline = create_pipeline(store, job_manager=job_manager, parse_job_id=parse_job["job_id"], rd_config=_rd_config())

    def fail_evaluate(*args, **kwargs):
        raise RuntimeError("synthetic failure for rule-fallback test")

    monkeypatch.setattr(web_server, "evaluate_factor", fail_evaluate)
    confirmed = confirm_pipeline(
        config, store, pipeline.pipeline_id,
        nonce=pipeline.confirm.nonce, version=pipeline.confirm.version,
        job_manager=job_manager, rd_config=_rd_config(),
    )
    _wait_job(job_manager, confirmed.stage("compute").child_job_id)
    monkeypatch.undo()
    failed = get_pipeline(store, pipeline.pipeline_id, job_manager=job_manager, config=config)
    assert failed.status == "paused_failure"

    # Re-verify RV-F10: the rule parse now runs SERVER-SIDE inside
    # create_pipeline_as_fallback, against the failed pipeline's own
    # persisted source_text -- no client-supplied parse job id exists in the
    # contract at all anymore.
    fallback = create_pipeline_as_fallback(
        store,
        job_manager=job_manager,
        rd_config=_rd_config(),
        parent_pipeline_id=pipeline.pipeline_id,
        config=config,
    )
    assert fallback.pipeline_id != pipeline.pipeline_id
    assert fallback.status == "awaiting_confirm"
    assert fallback.attempt.parent_run_id == pipeline.pipeline_id
    assert fallback.attempt.number == 1
    # The fallback's parse artifact is genuinely the RULE parser's output,
    # derived from the parent's persisted idea text.
    assert fallback.parser.get("source") == "rule"
    assert fallback.source_text == pipeline.source_text
    # Server-side synchronous parse: the parse ref carries no job id.
    parse_refs = [ref for ref in fallback.artifact_refs if ref.kind == "parse"]
    assert len(parse_refs) == 1
    assert parse_refs[0].job_id is None

    old_after_fallback = get_pipeline(store, pipeline.pipeline_id, job_manager=job_manager, config=config)
    assert old_after_fallback.status == "aborted"


def test_fallback_to_rule_parse_rejects_a_non_paused_parent(tmp_path) -> None:
    config, store, job_manager = _new_env(tmp_path)
    parse_job = _parsed_job_with_request(config, job_manager)
    pipeline = create_pipeline(store, job_manager=job_manager, parse_job_id=parse_job["job_id"], rd_config=_rd_config())

    with pytest.raises(PipelineConflictError, match="not paused"):
        create_pipeline_as_fallback(
            store,
            job_manager=job_manager,
            rd_config=_rd_config(),
            parent_pipeline_id=pipeline.pipeline_id,
            config=config,
        )


# ---------------------------------------------------------------------------
# Cluster D -- durable journal (F10)
# ---------------------------------------------------------------------------


def test_revision_increments_monotonically_across_saves(tmp_path) -> None:
    config, store, job_manager = _new_env(tmp_path)
    parse_job = _parsed_job_with_request(config, job_manager)
    pipeline = create_pipeline(store, job_manager=job_manager, parse_job_id=parse_job["job_id"], rd_config=_rd_config())
    first_revision = pipeline.revision
    assert first_revision >= 1

    edited = update_pipeline_parameters(
        store, pipeline.pipeline_id, {"holding_days": 9}, job_manager=job_manager, config=config
    )
    assert edited.revision == first_revision + 1

    edited_again = update_pipeline_parameters(
        store, pipeline.pipeline_id, {"holding_days": 10}, job_manager=job_manager, config=config
    )
    assert edited_again.revision == first_revision + 2


def test_store_load_recovers_entirely_from_the_journal_when_the_snapshot_is_missing(tmp_path) -> None:
    config, store, job_manager = _new_env(tmp_path)
    parse_job = _parsed_job_with_request(config, job_manager)
    pipeline = create_pipeline(store, job_manager=job_manager, parse_job_id=parse_job["job_id"], rd_config=_rd_config())
    edited = update_pipeline_parameters(
        store, pipeline.pipeline_id, {"holding_days": 9}, job_manager=job_manager, config=config
    )

    snapshot_path = store._snapshot_path(pipeline.pipeline_id)
    snapshot_path.unlink()

    recovered = store.load(pipeline.pipeline_id)
    assert recovered.parameters["holding_days"] == 9
    assert recovered.revision == edited.revision


def test_store_load_tolerates_a_torn_tail_journal_line(tmp_path) -> None:
    # phase-review F10: a malformed FINAL journal line is an expected torn
    # tail (a crash mid-append can only ever truncate the last write) and
    # must be dropped, not raised.
    config, store, job_manager = _new_env(tmp_path)
    parse_job = _parsed_job_with_request(config, job_manager)
    pipeline = create_pipeline(store, job_manager=job_manager, parse_job_id=parse_job["job_id"], rd_config=_rd_config())

    journal_path = store._journal_path(pipeline.pipeline_id)
    with journal_path.open("a", encoding="utf-8") as handle:
        handle.write('{"ts": "2026-01-01T00:00:00Z", "event": "torn", "record": {"pipeline_id": "PL_')  # truncated, no newline

    store._snapshot_path(pipeline.pipeline_id).unlink()  # force journal-only recovery
    recovered = store.load(pipeline.pipeline_id)
    assert recovered.pipeline_id == pipeline.pipeline_id  # recovered from the last GOOD row


def test_store_load_raises_on_interior_journal_corruption(tmp_path) -> None:
    # An interior corrupt line is not explicable by a torn-tail crash and
    # must surface as a real error, not be silently skipped.
    config, store, job_manager = _new_env(tmp_path)
    parse_job = _parsed_job_with_request(config, job_manager)
    pipeline = create_pipeline(store, job_manager=job_manager, parse_job_id=parse_job["job_id"], rd_config=_rd_config())
    update_pipeline_parameters(store, pipeline.pipeline_id, {"holding_days": 9}, job_manager=job_manager, config=config)

    journal_path = store._journal_path(pipeline.pipeline_id)
    lines = journal_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) >= 2
    lines[0] = "{not valid json at all"
    journal_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    store._snapshot_path(pipeline.pipeline_id).unlink()  # force journal-only recovery

    with pytest.raises(ValueError, match="corrupt pipeline journal"):
        store.load(pipeline.pipeline_id)


def test_store_load_prefers_the_journal_when_it_is_ahead_of_the_snapshot(tmp_path) -> None:
    # Simulates the exact crash window save() cannot avoid: the journal
    # append lands, then the process dies before the snapshot replace. Uses
    # a REAL, self-consistent edit (valid provenance included) rather than a
    # hand-built record, so this test isolates F10's journal-vs-snapshot
    # precedence from F5's independent stale-provenance guard -- a
    # hand-crafted record that bumps a parameter without updating its
    # provenance entry would (correctly) trip F5 instead of exercising F10.
    config, store, job_manager = _new_env(tmp_path)
    parse_job = _parsed_job_with_request(config, job_manager)
    pipeline = create_pipeline(store, job_manager=job_manager, parse_job_id=parse_job["job_id"], rd_config=_rd_config())
    stale_snapshot_bytes = store._snapshot_path(pipeline.pipeline_id).read_bytes()

    edited = update_pipeline_parameters(
        store, pipeline.pipeline_id, {"holding_days": 42}, job_manager=job_manager, config=config
    )
    assert edited.revision == pipeline.revision + 1
    assert edited.parameters["holding_days"] == 42

    # Roll the snapshot FILE back to the stale, lower-revision bytes --
    # exactly what a crash between the journal append and the snapshot
    # replace in `save()` would leave on disk. The journal already carries
    # the newer, self-consistent "edited" row from the update call above.
    store._snapshot_path(pipeline.pipeline_id).write_bytes(stale_snapshot_bytes)

    recovered = store.load(pipeline.pipeline_id)
    assert recovered.revision == edited.revision
    assert recovered.parameters["holding_days"] == 42


# ---------------------------------------------------------------------------
# Re-verify round 2 (RV-F1..RV-F6, RV-F9 + Cluster A payload semantics)
# ---------------------------------------------------------------------------

import json

import quant_forge.apps.web.pipeline as pipeline_module
from quant_forge.core.contracts import FactorDefinition as _FactorDefinition


def _confirm(config, store, pipeline, job_manager):
    return confirm_pipeline(
        config, store, pipeline.pipeline_id,
        nonce=pipeline.confirm.nonce, version=pipeline.confirm.version,
        job_manager=job_manager, rd_config=_rd_config(),
    )


def test_confirm_token_reuse_with_a_different_payload_is_a_conflict(tmp_path) -> None:
    # Cluster A (re-verify): the token is single-use PER PAYLOAD. Two tabs
    # holding the same (nonce, version): the first confirms with edits, the
    # second submits DIFFERENT edits with the now-consumed token -- the old
    # behavior silently treated it as an idempotent replay of the first.
    config, store, job_manager = _new_env(tmp_path)
    parse_job = _parsed_job_with_request(config, job_manager)
    pipeline = create_pipeline(store, job_manager=job_manager, parse_job_id=parse_job["job_id"], rd_config=_rd_config())

    first = confirm_pipeline(
        config, store, pipeline.pipeline_id,
        nonce=pipeline.confirm.nonce, version=pipeline.confirm.version,
        job_manager=job_manager, rd_config=_rd_config(),
        parameters={"holding_days": 7},
    )
    assert first.status == "running"
    assert first.confirmed_parameters["holding_days"] == 7

    # Same spent token, same payload: idempotent replay returns the run.
    replay = confirm_pipeline(
        config, store, pipeline.pipeline_id,
        nonce=pipeline.confirm.nonce, version=pipeline.confirm.version,
        job_manager=job_manager, rd_config=_rd_config(),
        parameters={"holding_days": 7},
    )
    assert replay.pipeline_id == first.pipeline_id
    assert replay.attempt.number == first.attempt.number

    # Same spent token, DIFFERENT payload: conflict, never a silent discard.
    with pytest.raises(PipelineConflictError, match="different parameter payload"):
        confirm_pipeline(
            config, store, pipeline.pipeline_id,
            nonce=pipeline.confirm.nonce, version=pipeline.confirm.version,
            job_manager=job_manager, rd_config=_rd_config(),
            parameters={"holding_days": 9},
        )
    _wait_job(job_manager, first.stage("compute").child_job_id)


def test_expiry_rotates_the_confirm_token(tmp_path) -> None:
    config, store, job_manager = _new_env(tmp_path)
    parse_job = _parsed_job_with_request(config, job_manager)
    pipeline = create_pipeline(store, job_manager=job_manager, parse_job_id=parse_job["job_id"], rd_config=_rd_config())
    expired_at = pipeline.with_updates(expires_at="2000-01-01T00:00:00Z")
    store.save(expired_at, event="test_forced_expiry_setup")

    reconciled = get_pipeline(store, pipeline.pipeline_id, job_manager=job_manager, config=config)
    assert reconciled.status == "expired"
    assert reconciled.confirm.nonce != pipeline.confirm.nonce
    assert reconciled.confirm.version > pipeline.confirm.version


def test_publish_cas_declines_when_canonical_row_changed_mid_run(tmp_path) -> None:
    # RV-F1: a canonical draft edited BETWEEN confirm and completion must be
    # preserved -- the completed pipeline reports publish_state="conflict"
    # instead of clobbering the newer content with its snapshot of the past.
    config, store, job_manager = _new_env(tmp_path)
    parse_job = _parsed_job_with_request(config, job_manager)
    pipeline = create_pipeline(store, job_manager=job_manager, parse_job_id=parse_job["job_id"], rd_config=_rd_config())
    confirmed = _confirm(config, store, pipeline, job_manager)
    _wait_job(job_manager, confirmed.stage("compute").child_job_id)

    # Concurrent actor edits (creates) the canonical row while compute ran.
    repo = FactorRepository(config.paths.factor_root)
    canonical_id = str(pipeline.factor["factor_id"])
    repo.save(
        _FactorDefinition(
            factor_id=canonical_id, name="edited_mid_run", formula="rank(volume)", status="draft",
            description="a concurrent human edit that must survive",
        )
    )

    done = get_pipeline(store, pipeline.pipeline_id, job_manager=job_manager, config=config)
    assert done.status == "completed"
    assert done.publish_state == "conflict"
    assert done.published_factor_id is None
    survived = repo.get(canonical_id)
    assert survived.name == "edited_mid_run"
    assert survived.formula == "rank(volume)"


def test_publish_cas_succeeds_when_canonical_row_unchanged(tmp_path) -> None:
    config, store, job_manager = _new_env(tmp_path)
    parse_job = _parsed_job_with_request(config, job_manager)
    pipeline = create_pipeline(store, job_manager=job_manager, parse_job_id=parse_job["job_id"], rd_config=_rd_config())
    confirmed = _confirm(config, store, pipeline, job_manager)
    _wait_job(job_manager, confirmed.stage("compute").child_job_id)

    done = get_pipeline(store, pipeline.pipeline_id, job_manager=job_manager, config=config)
    assert done.status == "completed"
    assert done.publish_state == "published"
    assert done.published_factor_id == pipeline.factor["factor_id"]


def test_terminal_cleanup_removes_cached_working_values_from_both_roots(tmp_path) -> None:
    # RV-F2: cancel/fail/abort must remove the working id's cached value
    # directories, not just the registry row.
    config, store, job_manager = _new_env(tmp_path)
    parse_job = _parsed_job_with_request(config, job_manager)
    pipeline = create_pipeline(store, job_manager=job_manager, parse_job_id=parse_job["job_id"], rd_config=_rd_config())

    # The demo env leaves both value roots unset; configure them the way a
    # real deployment does so the cleanup path has something to sweep.
    from dataclasses import replace as _dc_replace

    values_root = config.paths.artifact_root / "test_factor_values"
    overlay_root = config.paths.artifact_root / "test_overlay_values"
    config = _dc_replace(
        config,
        paths=_dc_replace(config.paths, factor_values_root=values_root, factor_values_overlay_root=overlay_root),
    )
    from quant_forge.factor_library.classification import FACTOR_CATEGORY_DIRS

    category_dir = next(iter(FACTOR_CATEGORY_DIRS.values()))
    orphan_dir = values_root / category_dir / f"factor_id={pipeline.working_factor_id.upper()}"
    orphan_dir.mkdir(parents=True, exist_ok=True)
    (orphan_dir / "2024.parquet").write_bytes(b"stub")
    # And the raw-id spelling under the overlay root itself.
    raw_dir = overlay_root / pipeline.working_factor_id
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / "2024.parquet").write_bytes(b"stub")

    cancelled = cancel_pipeline(store, pipeline.pipeline_id, job_manager=job_manager, config=config)
    assert cancelled.status == "aborted"
    assert cancelled.cleanup_pending is False
    assert not orphan_dir.exists()
    assert not raw_dir.exists()


def test_failed_cleanup_persists_cleanup_pending_and_reconcile_retries_it(tmp_path, monkeypatch) -> None:
    # RV-F3: a cleanup failure must not be swallowed once and forgotten --
    # the record carries cleanup_pending and the next load retries it.
    config, store, job_manager = _new_env(tmp_path)
    parse_job = _parsed_job_with_request(config, job_manager)
    pipeline = create_pipeline(store, job_manager=job_manager, parse_job_id=parse_job["job_id"], rd_config=_rd_config())

    original_delete = pipeline_module.FactorRepository.delete

    def _boom(self, factor_id):
        raise RuntimeError("simulated registry outage")

    monkeypatch.setattr(pipeline_module.FactorRepository, "delete", _boom)
    cancelled = cancel_pipeline(store, pipeline.pipeline_id, job_manager=job_manager, config=config)
    assert cancelled.status == "aborted"
    assert cancelled.cleanup_pending is True

    monkeypatch.setattr(pipeline_module.FactorRepository, "delete", original_delete)
    retried = get_pipeline(store, pipeline.pipeline_id, job_manager=job_manager, config=config)
    assert retried.cleanup_pending is False


def test_stale_completion_artifact_from_another_attempt_does_not_complete(tmp_path) -> None:
    # RV-F4 (attack side): an artifact keyed to a DIFFERENT child job /
    # attempt / input hash is not completion evidence for this record.
    config, store, job_manager = _new_env(tmp_path)
    parse_job = _parsed_job_with_request(config, job_manager)
    pipeline = create_pipeline(store, job_manager=job_manager, parse_job_id=parse_job["job_id"], rd_config=_rd_config())
    confirmed = _confirm(config, store, pipeline, job_manager)
    _wait_job(job_manager, confirmed.stage("compute").child_job_id)

    # Forge a stale artifact naming a different attempt + job id.
    pipeline_module._write_completion_artifact(
        store,
        pipeline_id=pipeline.pipeline_id,
        child_job_id="JOB_someone_else",
        attempt=99,
        input_hash="not-this-input",
        result={"forged": True},
    )
    fresh_job_manager = _WebJobManager()
    fresh_store = PipelineStore(config.paths.artifact_root)
    rejoined = get_pipeline(fresh_store, pipeline.pipeline_id, job_manager=fresh_job_manager, config=config)
    # The record was still "running" on disk; the mismatched artifact must
    # NOT complete it -- honest paused_failure with the three exits instead.
    assert rejoined.status == "paused_failure"
    assert rejoined.failure.reason_code == "JOB_NOT_FOUND"


def test_pipeline_report_serves_from_artifact_after_restart(tmp_path) -> None:
    # RV-F4 (recovery side): a completed pipeline still renders a report
    # after the in-memory job manager is wiped.
    config, store, job_manager = _new_env(tmp_path)
    parse_job = _parsed_job_with_request(config, job_manager)
    pipeline = create_pipeline(store, job_manager=job_manager, parse_job_id=parse_job["job_id"], rd_config=_rd_config())
    confirmed = _confirm(config, store, pipeline, job_manager)
    _wait_job(job_manager, confirmed.stage("compute").child_job_id)
    done = get_pipeline(store, pipeline.pipeline_id, job_manager=job_manager, config=config)
    assert done.status == "completed"

    live = pipeline_module.pipeline_report(store, pipeline.pipeline_id, job_manager=job_manager, config=config)
    assert live["source"] == "job"
    assert live["result"]

    fresh_job_manager = _WebJobManager()
    recovered = pipeline_module.pipeline_report(
        store, pipeline.pipeline_id, job_manager=fresh_job_manager, config=config
    )
    assert recovered["source"] == "artifact"
    assert recovered["result"]
    assert recovered["result"].keys() == live["result"].keys()


def test_journal_tolerates_a_torn_multibyte_utf8_tail(tmp_path) -> None:
    # RV-F5: a crash can truncate the tail mid-way through a multi-byte
    # codepoint; the reader must keep every complete earlier record.
    config, store, job_manager = _new_env(tmp_path)
    parse_job = _parsed_job_with_request(config, job_manager)
    pipeline = create_pipeline(store, job_manager=job_manager, parse_job_id=parse_job["job_id"], rd_config=_rd_config())
    journal_path = store._journal_path(pipeline.pipeline_id)

    torn = json.dumps({"event": "torn", "detail": "管线中文字段"}, ensure_ascii=False).encode("utf-8")
    with journal_path.open("ab") as handle:
        handle.write(torn[: len(torn) - 4])  # cut inside the multi-byte run

    loaded = store.load(pipeline.pipeline_id)
    assert loaded.pipeline_id == pipeline.pipeline_id
    assert loaded.revision == pipeline.revision


def test_journal_only_pipeline_is_discoverable_by_list_ids(tmp_path) -> None:
    # RV-F6: crash after the first journal fsync but before the first
    # snapshot replace leaves a journal-only pipeline; rejoin must find it.
    config, store, job_manager = _new_env(tmp_path)
    parse_job = _parsed_job_with_request(config, job_manager)
    pipeline = create_pipeline(store, job_manager=job_manager, parse_job_id=parse_job["job_id"], rd_config=_rd_config())
    snapshot_path = store._snapshot_path(pipeline.pipeline_id)
    snapshot_path.unlink()

    assert pipeline.pipeline_id in store.list_ids()
    records = list_active_pipelines(store, job_manager=job_manager, config=config)
    assert any(record.pipeline_id == pipeline.pipeline_id for record in records)


def test_fork_carries_pending_parameter_edits_as_human_override(tmp_path, monkeypatch) -> None:
    # RV-F9: the paused card's visible pending edits travel with the fork
    # and badge as human_override against the FROZEN inputs of the failed
    # attempt.
    config, store, job_manager = _new_env(tmp_path)
    parse_job = _parsed_job_with_request(config, job_manager)
    pipeline = create_pipeline(store, job_manager=job_manager, parse_job_id=parse_job["job_id"], rd_config=_rd_config())
    monkeypatch.setattr(
        pipeline_module,
        "run_idea_validation_workflow",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    confirmed = _confirm(config, store, pipeline, job_manager)
    _wait_job(job_manager, confirmed.stage("compute").child_job_id)
    monkeypatch.undo()
    failed = get_pipeline(store, pipeline.pipeline_id, job_manager=job_manager, config=config)
    assert failed.status == "paused_failure"

    forked = fork_pipeline_from_failure(
        store, pipeline.pipeline_id,
        job_manager=job_manager, config=config, rd_config=_rd_config(),
        parameters={"holding_days": 13},
    )
    assert forked.parameters["holding_days"] == 13
    assert forked.original_parameters["holding_days"] == failed.confirmed_parameters["holding_days"]
    badge = provenance_by_field(
        tuple(ProvenanceEntry(**entry) for entry in forked.provenance)
    )["holding_days"]
    assert badge.source == "human_override"
    assert badge.parent_value == failed.confirmed_parameters["holding_days"]


# ---------------------------------------------------------------------------
# Re-verify round 3 (rv2 sub-findings)
# ---------------------------------------------------------------------------


def test_retry_is_refused_while_cleanup_is_pending(tmp_path, monkeypatch) -> None:
    # RV2-F4: a pending old-attempt cleanup must never escape into a new
    # attempt that reuses the same working id.
    config, store, job_manager = _new_env(tmp_path)
    parse_job = _parsed_job_with_request(config, job_manager)
    pipeline = create_pipeline(store, job_manager=job_manager, parse_job_id=parse_job["job_id"], rd_config=_rd_config())
    monkeypatch.setattr(
        pipeline_module,
        "run_idea_validation_workflow",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    confirmed = _confirm(config, store, pipeline, job_manager)
    _wait_job(job_manager, confirmed.stage("compute").child_job_id)

    def _boom_delete(self, factor_id):
        raise RuntimeError("simulated registry outage")

    original_delete = pipeline_module.FactorRepository.delete
    monkeypatch.setattr(pipeline_module.FactorRepository, "delete", _boom_delete)
    failed = get_pipeline(store, pipeline.pipeline_id, job_manager=job_manager, config=config)
    assert failed.status == "paused_failure"
    assert failed.cleanup_pending is True

    with pytest.raises(PipelineConflictError, match="unfinished working-artifact cleanup"):
        retry_pipeline(store, pipeline.pipeline_id, job_manager=job_manager, config=config)

    # Once cleanup can succeed, the retry path clears the flag and proceeds.
    monkeypatch.setattr(pipeline_module.FactorRepository, "delete", original_delete)
    retried = retry_pipeline(store, pipeline.pipeline_id, job_manager=job_manager, config=config)
    assert retried.status == "awaiting_confirm"
    assert retried.cleanup_pending is False


def test_fork_preserves_durable_next_attempt_edits_on_empty_payload(tmp_path, monkeypatch) -> None:
    # RV2-F8: saved 「仅用于下次尝试」 edits (old.parameters) survive a fork
    # posted by a refreshed client with no local overrides.
    config, store, job_manager = _new_env(tmp_path)
    parse_job = _parsed_job_with_request(config, job_manager)
    pipeline = create_pipeline(store, job_manager=job_manager, parse_job_id=parse_job["job_id"], rd_config=_rd_config())
    monkeypatch.setattr(
        pipeline_module,
        "run_idea_validation_workflow",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    confirmed = _confirm(config, store, pipeline, job_manager)
    _wait_job(job_manager, confirmed.stage("compute").child_job_id)
    monkeypatch.undo()
    failed = get_pipeline(store, pipeline.pipeline_id, job_manager=job_manager, config=config)
    assert failed.status == "paused_failure"

    # Durable next-attempt edit saved while paused...
    update_pipeline_parameters(store, pipeline.pipeline_id, {"holding_days": 13}, job_manager=job_manager, config=config)
    # ...then a refreshed client forks with an empty payload.
    forked = fork_pipeline_from_failure(
        store, pipeline.pipeline_id, job_manager=job_manager, config=config, rd_config=_rd_config(), parameters={}
    )
    assert forked.parameters["holding_days"] == 13  # not silently reverted to frozen
    badge = provenance_by_field(tuple(ProvenanceEntry(**entry) for entry in forked.provenance))["holding_days"]
    assert badge.source == "human_override"


def test_completion_artifact_is_public_sanitized(tmp_path) -> None:
    # RV2-F7 (write side): the persisted artifact carries the same public
    # projection the live job endpoint serves -- no absolute local paths.
    config, store, job_manager = _new_env(tmp_path)
    parse_job = _parsed_job_with_request(config, job_manager)
    pipeline = create_pipeline(store, job_manager=job_manager, parse_job_id=parse_job["job_id"], rd_config=_rd_config())
    confirmed = _confirm(config, store, pipeline, job_manager)
    _wait_job(job_manager, confirmed.stage("compute").child_job_id)

    artifact_path = pipeline_module._completion_artifact_path(store, pipeline.pipeline_id)
    raw = artifact_path.read_text(encoding="utf-8")
    assert str(config.paths.artifact_root) not in raw
    assert str(tmp_path) not in raw

    # Serve side agrees (defensive re-projection).
    report = pipeline_module.pipeline_report(store, pipeline.pipeline_id, job_manager=_WebJobManager(), config=config)
    assert report["source"] == "artifact"
    assert str(tmp_path) not in json.dumps(report)


def test_completion_artifact_write_failure_pauses_instead_of_completing(tmp_path, monkeypatch) -> None:
    # RV2-F5: the artifact is the only durable result copy; failing to
    # write it must surface as a paused failure with retry, never a
    # completion that silently loses its report at the next restart.
    config, store, job_manager = _new_env(tmp_path)
    parse_job = _parsed_job_with_request(config, job_manager)
    pipeline = create_pipeline(store, job_manager=job_manager, parse_job_id=parse_job["job_id"], rd_config=_rd_config())
    monkeypatch.setattr(
        pipeline_module,
        "_write_completion_artifact",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )
    confirmed = _confirm(config, store, pipeline, job_manager)
    _wait_job(job_manager, confirmed.stage("compute").child_job_id)
    reconciled = get_pipeline(store, pipeline.pipeline_id, job_manager=job_manager, config=config)
    assert reconciled.status == "paused_failure"


def test_forged_artifact_with_malformed_attempt_is_non_evidence(tmp_path) -> None:
    # RV2-F9: schema garbage in the artifact degrades to the honest
    # JOB_NOT_FOUND pause, never an exception out of reconciliation.
    config, store, job_manager = _new_env(tmp_path)
    parse_job = _parsed_job_with_request(config, job_manager)
    pipeline = create_pipeline(store, job_manager=job_manager, parse_job_id=parse_job["job_id"], rd_config=_rd_config())
    confirmed = _confirm(config, store, pipeline, job_manager)
    _wait_job(job_manager, confirmed.stage("compute").child_job_id)

    artifact_path = pipeline_module._completion_artifact_path(store, pipeline.pipeline_id)
    payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    payload["attempt"] = "bad"
    artifact_path.write_text(json.dumps(payload), encoding="utf-8")

    rejoined = get_pipeline(
        PipelineStore(config.paths.artifact_root), pipeline.pipeline_id, job_manager=_WebJobManager(), config=config
    )
    assert rejoined.status == "paused_failure"
    assert rejoined.failure.reason_code == "JOB_NOT_FOUND"


def test_value_cleanup_skips_directories_contested_by_another_factor_id(tmp_path) -> None:
    # RV2-F2: the canonical dir naming is non-injective (case folding,
    # hyphen->underscore); a directory another registered id also derives
    # is skipped -- residue over deleting someone else's cache.
    from quant_forge.factor_engine.value_store import remove_stored_values_for_factor_id

    root = tmp_path / "values"
    ours = "Foo-Bar_PWaaaaaaaaaaaaaaaa"
    theirs = "FOO_BAR_PWAAAAAAAAAAAAAAAA"
    contested = root / "factor_id=FOO_BAR_PWAAAAAAAAAAAAAAAA"
    contested.mkdir(parents=True)
    (contested / "2024.parquet").write_bytes(b"stub")
    exclusive = root / ours
    exclusive.mkdir(parents=True)
    (exclusive / "2024.parquet").write_bytes(b"stub")

    removed = remove_stored_values_for_factor_id(root, ours, other_known_factor_ids=(theirs,))
    assert contested.is_dir()  # contested canonical spelling survives
    assert not exclusive.exists()  # our raw-id dir is gone
    assert removed == 1


def test_value_cleanup_claims_compare_casefolded(tmp_path) -> None:
    # RV3-F1: the default macOS volume is case-insensitive -- "foo" and
    # "FOO" alias one directory, so ownership claims must compare
    # casefolded or the cleanup deletes the other spelling's data.
    from quant_forge.factor_engine.value_store import remove_stored_values_for_factor_id

    root = tmp_path / "values"
    contested = root / "foo_PWaaaaaaaaaaaaaaaa"
    contested.mkdir(parents=True)
    (contested / "2024.parquet").write_bytes(b"stub")

    removed = remove_stored_values_for_factor_id(
        root, "foo_PWaaaaaaaaaaaaaaaa", other_known_factor_ids=("FOO_PWAAAAAAAAAAAAAAAA",)
    )
    assert removed == 0
    assert contested.is_dir()  # case-variant claim protects the alias


def test_completion_artifact_attempt_requires_exact_int_type(tmp_path) -> None:
    # RV3-F2: True / 1.9 / "1" coerce to a matching attempt via int();
    # strict typing keeps forged near-miss artifacts non-evidence.
    config, store, job_manager = _new_env(tmp_path)
    parse_job = _parsed_job_with_request(config, job_manager)
    pipeline = create_pipeline(store, job_manager=job_manager, parse_job_id=parse_job["job_id"], rd_config=_rd_config())
    confirmed = _confirm(config, store, pipeline, job_manager)
    _wait_job(job_manager, confirmed.stage("compute").child_job_id)
    running_on_disk = PipelineStore(config.paths.artifact_root).load(pipeline.pipeline_id)
    assert running_on_disk.status == "running"

    artifact_path = pipeline_module._completion_artifact_path(store, pipeline.pipeline_id)
    payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    for bad_attempt in (True, 1.9, "1"):
        forged = dict(payload)
        forged["attempt"] = bad_attempt
        assert pipeline_module._completion_matches_record(forged, running_on_disk) is False
    assert pipeline_module._completion_matches_record(payload, running_on_disk) is True


def test_byte_corrupt_completion_artifact_is_non_evidence(tmp_path) -> None:
    # RV3-F3: invalid UTF-8 in the artifact degrades to the honest pause,
    # never a UnicodeDecodeError out of reconciliation.
    config, store, job_manager = _new_env(tmp_path)
    parse_job = _parsed_job_with_request(config, job_manager)
    pipeline = create_pipeline(store, job_manager=job_manager, parse_job_id=parse_job["job_id"], rd_config=_rd_config())
    confirmed = _confirm(config, store, pipeline, job_manager)
    _wait_job(job_manager, confirmed.stage("compute").child_job_id)

    artifact_path = pipeline_module._completion_artifact_path(store, pipeline.pipeline_id)
    artifact_path.write_bytes(b"\xff\xfe broken bytes")

    rejoined = get_pipeline(
        PipelineStore(config.paths.artifact_root), pipeline.pipeline_id, job_manager=_WebJobManager(), config=config
    )
    assert rejoined.status == "paused_failure"
    assert rejoined.failure.reason_code == "JOB_NOT_FOUND"

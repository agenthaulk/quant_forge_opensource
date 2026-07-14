"""Server-owned pipeline aggregate tests (agent_sidecar_frontend.md §2.3, WORKORDER P1).

Exercises the aggregate directly (no HTTP) against a real demo workspace: the
rule parser (zero LLM key) and the real evaluate/backtest chain, mirroring
``tests/test_web_workbench.py``'s existing pattern for
``run_idea_parse_workflow`` / ``run_idea_validation_workflow``.

Covers the WORKORDER P1 pins:
- double-confirm returns the same run (nonce idempotency);
- refresh/server-restart rejoin;
- transition legality;
- expiry;
- snapshot isolation (a failing run's rollback must not clobber a parallel
  write, apps/web/pipeline.py's `_pipeline_scoped_factor_id` mechanism).
"""

from __future__ import annotations

import time

import pytest

import quant_forge.apps.web.server as web_server
from quant_forge.apps.web.jobs import _WebJobManager
from quant_forge.apps.web.pipeline import (
    PipelineConflictError,
    PipelineNotFoundError,
    PipelineStore,
    _pipeline_scoped_factor_id,
    cancel_pipeline,
    confirm_pipeline,
    create_pipeline,
    get_pipeline,
    list_active_pipelines,
    retry_pipeline,
    update_pipeline_parameters,
)
from quant_forge.apps.web.server import run_idea_parse_workflow
from quant_forge.config import QuantForgeConfig
from quant_forge.data.local import create_demo_workspace
from quant_forge.factor_library.repository import FactorRepository
from quant_forge.research_loop.config import ResearchLoopConfig
from quant_forge.specs.pipeline import FACTOR_STUDY_STAGE_IDS, LEGAL_TRANSITIONS, can_transition


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
    return _run_job_sync(job_manager, "parse_idea", lambda cancel_event: run_idea_parse_workflow(config, text, parser_mode="rule"))


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
# Create: honest stage granularity (D11) + zero-LLM parse artifact capture
# ---------------------------------------------------------------------------


def test_create_pipeline_from_a_completed_rule_parse_job_lands_in_awaiting_confirm(tmp_path) -> None:
    config, store, job_manager = _new_env(tmp_path)
    parse_job = _parsed_job(config, job_manager)

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
    # kind), so a "fake" client cannot pass `parser={"source": "llm"}` at all.
    import inspect

    signature = inspect.signature(create_pipeline)
    assert set(signature.parameters) == {"store", "job_manager", "parse_job_id", "rd_config", "kind"}


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
# Confirm idempotency (WORKORDER P1 pin)
# ---------------------------------------------------------------------------


def test_double_confirm_with_the_same_token_returns_the_same_run(tmp_path) -> None:
    config, store, job_manager = _new_env(tmp_path)
    parse_job = _parsed_job(config, job_manager)
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

    # Simulates a double click / a second tab / a retried HTTP request: the
    # SAME (pipeline_id, nonce, version) arrives again.
    second = confirm_pipeline(
        config, store, pipeline.pipeline_id,
        nonce=pipeline.confirm.nonce, version=pipeline.confirm.version,
        job_manager=job_manager, rd_config=_rd_config(),
    )
    assert second.stage("compute").child_job_id == job_id
    assert second.confirm.confirmed_at == first.confirm.confirmed_at
    assert [entry["field"] for entry in second.provenance] == [entry["field"] for entry in first.provenance]

    _wait_job(job_manager, job_id)


def test_double_confirm_does_not_start_a_second_job(tmp_path) -> None:
    config, store, job_manager = _new_env(tmp_path)
    parse_job = _parsed_job(config, job_manager)
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
    parse_job = _parsed_job(config, job_manager)
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
    parse_job = _parsed_job(config, job_manager)
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


def test_confirmed_parameters_freeze_and_later_edits_are_next_attempt_only(tmp_path, monkeypatch) -> None:
    # "A run is live" is exercised via paused_failure rather than a mid-flight
    # race on `running`: the demo workspace's rule-parsed compute finishes in
    # well under a second, so racing an edit against it would be flaky. A
    # deterministically-failed run is equally non-terminal (spec §2.3: the
    # freeze only lifts on a NEW confirm) and lets this test be deterministic.
    config, store, job_manager = _new_env(tmp_path)
    parse_job = _parsed_job(config, job_manager)
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
    paused = get_pipeline(store, pipeline.pipeline_id, job_manager=job_manager)
    assert paused.status == "paused_failure"
    assert paused.confirmed_parameters["holding_days"] == 7

    edited = update_pipeline_parameters(
        store, pipeline.pipeline_id, {"holding_days": 12}, job_manager=job_manager
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
    parse_job = _parsed_job(config, job_manager)
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
# Rejoin (refresh / server restart)
# ---------------------------------------------------------------------------


def test_list_active_pipelines_includes_awaiting_confirm_and_excludes_terminal(tmp_path) -> None:
    config, store, job_manager = _new_env(tmp_path)
    parse_job = _parsed_job(config, job_manager)
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

    active_ids = {record.pipeline_id for record in list_active_pipelines(store, job_manager=job_manager)}
    assert awaiting.pipeline_id in active_ids
    assert completed_pipeline.pipeline_id not in active_ids


def test_rejoin_after_a_simulated_server_restart_reconciles_to_paused_failure(tmp_path) -> None:
    # A "server restart" wipes the in-memory _WebJobManager but the pipeline
    # snapshot on disk survives -- the durability contract under test.
    config, store, job_manager = _new_env(tmp_path)
    parse_job = _parsed_job(config, job_manager)
    pipeline = create_pipeline(store, job_manager=job_manager, parse_job_id=parse_job["job_id"], rd_config=_rd_config())
    confirmed = confirm_pipeline(
        config, store, pipeline.pipeline_id,
        nonce=pipeline.confirm.nonce, version=pipeline.confirm.version,
        job_manager=job_manager, rd_config=_rd_config(),
    )
    assert confirmed.status == "running"

    fresh_job_manager = _WebJobManager()  # simulates the restart: no jobs known
    fresh_store = PipelineStore(config.paths.artifact_root)  # simulates a fresh process reading the same disk state

    rejoined = get_pipeline(fresh_store, pipeline.pipeline_id, job_manager=fresh_job_manager)
    assert rejoined.status == "paused_failure"
    assert rejoined.failure is not None
    assert rejoined.failure.reason_code == "JOB_NOT_FOUND"
    # The card can still redisplay the original formula/params after rejoin
    # -- rejoin must never strand the user with an empty card.
    assert rejoined.factor["formula"] == "-rank(market_cap)"


def test_get_pipeline_reconciles_a_completed_job_into_the_report_stage(tmp_path) -> None:
    config, store, job_manager = _new_env(tmp_path)
    parse_job = _parsed_job(config, job_manager)
    pipeline = create_pipeline(store, job_manager=job_manager, parse_job_id=parse_job["job_id"], rd_config=_rd_config())
    confirmed = confirm_pipeline(
        config, store, pipeline.pipeline_id,
        nonce=pipeline.confirm.nonce, version=pipeline.confirm.version,
        job_manager=job_manager, rd_config=_rd_config(),
    )
    _wait_job(job_manager, confirmed.stage("compute").child_job_id)

    reconciled = get_pipeline(store, pipeline.pipeline_id, job_manager=job_manager)
    assert reconciled.status == "completed"
    assert reconciled.stage("compute").status == "completed"
    assert reconciled.stage("report").status == "completed"
    assert any(ref.kind == "report" for ref in reconciled.artifact_refs)


# ---------------------------------------------------------------------------
# Unknown pipeline id / containment
# ---------------------------------------------------------------------------


def test_unknown_pipeline_id_raises_not_found(tmp_path) -> None:
    config, store, job_manager = _new_env(tmp_path)
    with pytest.raises(PipelineNotFoundError):
        get_pipeline(store, "PL_" + "0" * 32, job_manager=job_manager)


@pytest.mark.parametrize(
    "probe",
    ["../../etc/passwd", "PL_short", "not-even-close", "PL_" + "z" * 32, ""],
)
def test_malformed_pipeline_id_is_rejected_before_touching_the_filesystem(tmp_path, probe: str) -> None:
    config, store, job_manager = _new_env(tmp_path)
    with pytest.raises(PipelineNotFoundError):
        get_pipeline(store, probe, job_manager=job_manager)


# ---------------------------------------------------------------------------
# Expiry
# ---------------------------------------------------------------------------


def test_expired_draft_reconciles_to_expired_on_read(tmp_path) -> None:
    config, store, job_manager = _new_env(tmp_path)
    parse_job = _parsed_job(config, job_manager)
    pipeline = create_pipeline(store, job_manager=job_manager, parse_job_id=parse_job["job_id"], rd_config=_rd_config())

    stale = pipeline.with_updates(expires_at="2000-01-01T00:00:00Z")
    store.save(stale, event="test_backdate")

    reconciled = get_pipeline(store, pipeline.pipeline_id, job_manager=job_manager)
    assert reconciled.status == "expired"


def test_expired_pipelines_are_excluded_from_list_active(tmp_path) -> None:
    config, store, job_manager = _new_env(tmp_path)
    parse_job = _parsed_job(config, job_manager)
    pipeline = create_pipeline(store, job_manager=job_manager, parse_job_id=parse_job["job_id"], rd_config=_rd_config())
    stale = pipeline.with_updates(expires_at="2000-01-01T00:00:00Z")
    store.save(stale, event="test_backdate")

    active_ids = {record.pipeline_id for record in list_active_pipelines(store, job_manager=job_manager)}
    assert pipeline.pipeline_id not in active_ids


# ---------------------------------------------------------------------------
# Cancel + retry
# ---------------------------------------------------------------------------


def test_cancel_awaiting_confirm_pipeline_is_terminal(tmp_path) -> None:
    config, store, job_manager = _new_env(tmp_path)
    parse_job = _parsed_job(config, job_manager)
    pipeline = create_pipeline(store, job_manager=job_manager, parse_job_id=parse_job["job_id"], rd_config=_rd_config())

    cancelled = cancel_pipeline(store, pipeline.pipeline_id, job_manager=job_manager)
    assert cancelled.status == "aborted"
    # Idempotent: cancelling an already-terminal pipeline is a no-op, not an error.
    again = cancel_pipeline(store, pipeline.pipeline_id, job_manager=job_manager)
    assert again.status == "aborted"


def test_retry_after_failure_reuses_parse_and_issues_a_new_confirm_token(tmp_path, monkeypatch) -> None:
    config, store, job_manager = _new_env(tmp_path)
    parse_job = _parsed_job(config, job_manager)
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
    failed = get_pipeline(store, pipeline.pipeline_id, job_manager=job_manager)
    assert failed.status == "paused_failure"
    assert failed.failure is not None

    monkeypatch.undo()
    retried = retry_pipeline(store, pipeline.pipeline_id)
    assert retried.status == "awaiting_confirm"
    assert retried.stage("parse").status == "completed"  # reused, not re-run
    assert retried.stage("compute").status == "pending"
    assert retried.stage("compute").child_job_id is None
    assert retried.confirm.nonce != pipeline.confirm.nonce
    assert retried.attempt.number == 2
    assert retried.failure is None

    reconfirmed = confirm_pipeline(
        config, store, pipeline.pipeline_id,
        nonce=retried.confirm.nonce, version=retried.confirm.version,
        job_manager=job_manager, rd_config=_rd_config(),
    )
    job = _wait_job(job_manager, reconfirmed.stage("compute").child_job_id)
    assert job["status"] == "completed"


# ---------------------------------------------------------------------------
# Snapshot isolation regression (WORKORDER P1 pin)
# ---------------------------------------------------------------------------


def test_a_failing_pipelines_rollback_does_not_clobber_a_parallel_pipelines_write(tmp_path, monkeypatch) -> None:
    config, store, job_manager = _new_env(tmp_path)
    parse_job = _parsed_job(config, job_manager)
    repo = FactorRepository(config.paths.factor_root)

    pipeline_a = create_pipeline(store, job_manager=job_manager, parse_job_id=parse_job["job_id"], rd_config=_rd_config())
    pipeline_b = create_pipeline(store, job_manager=job_manager, parse_job_id=parse_job["job_id"], rd_config=_rd_config())
    assert pipeline_a.pipeline_id != pipeline_b.pipeline_id

    # B confirms first, with DIFFERENT parameters than A will use below --
    # different confirm-time input means a different input_hash, which is
    # exactly the property _pipeline_scoped_factor_id relies on.
    confirmed_b = confirm_pipeline(
        config, store, pipeline_b.pipeline_id,
        nonce=pipeline_b.confirm.nonce, version=pipeline_b.confirm.version,
        job_manager=job_manager, rd_config=_rd_config(), parameters={"holding_days": 11},
    )
    job_b = _wait_job(job_manager, confirmed_b.stage("compute").child_job_id)
    assert job_b["status"] == "completed"
    assert confirmed_b.input_hash != pipeline_a.input_hash
    factor_id_b = _pipeline_scoped_factor_id(pipeline_b.factor["factor_id"], confirmed_b.input_hash)
    row_b_before = repo.get(factor_id_b)

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

    # A's failure-triggered restore (apps/web/api.py::_restore_factor_after_failed_validation,
    # frozen, unchanged) deletes A's own brand-new row. B's row -- a
    # DIFFERENT factor_root entry by construction -- must be untouched.
    row_b_after = repo.get(factor_id_b)
    assert row_b_after == row_b_before
    factor_id_a = _pipeline_scoped_factor_id(pipeline_a.factor["factor_id"], confirmed_a.input_hash)
    assert factor_id_a != factor_id_b
    with pytest.raises(FileNotFoundError):
        repo.get(factor_id_a)


def test_scoped_factor_id_is_stable_for_identical_input_and_distinct_for_different_input() -> None:
    same_a = _pipeline_scoped_factor_id("FTR_ABCDEFGH", "0" * 64)
    same_b = _pipeline_scoped_factor_id("FTR_ABCDEFGH", "0" * 64)
    different = _pipeline_scoped_factor_id("FTR_ABCDEFGH", "1" * 64)
    assert same_a == same_b
    assert same_a != different
    # Always a legal FactorDefinition id: [A-Za-z][A-Za-z0-9_=-]*.
    import re

    assert re.fullmatch(r"[A-Za-z][A-Za-z0-9_=-]*", same_a)

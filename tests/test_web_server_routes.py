"""Characterization tests for the local web adapter's route surface.

These tests lock in the CP4 pre-decomposition behavior of
``quant_forge.apps.web.server``:

- GET route dispatch (status code + Content-Type) including the HTML
  fallback for unknown paths and the JSON 404 for unknown jobs.
- POST route dispatch reaching the workflow callables through the server
  module namespace (the monkeypatch seams used by test_web_workbench.py).
- The module attribute surface that tests/test_web_workbench.py imports,
  reads, or monkeypatches on ``quant_forge.apps.web.server``.

They must pass unchanged before and after the server.py module split.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

import quant_forge.apps.cli.main as cli_main
import quant_forge.apps.web.server as web_server
from quant_forge.apps.web.server import create_local_web_server
from quant_forge.config import QuantForgeConfig
from quant_forge.core.contracts import BacktestResult, EvaluationResult, FactorDefinition
from quant_forge.data.local import create_demo_workspace
from quant_forge.llm_factor_parser import ParsedFactor
from quant_forge.research_loop.service import (
    ResearchGate,
    ResearchLoopResult,
    ResearchObjectiveWeights,
)


# Names tests/test_web_workbench.py monkeypatches via the server module
# namespace. Each must remain patchable on quant_forge.apps.web.server and
# keep taking effect at call time after any refactor.
_MONKEYPATCH_SEAM_NAMES = sorted(
    [
        "DEFAULT_RD_CONFIG_PATH",
        "_run_research_once",
        "_web_public_json",
        "evaluate_factor",
        "parse_factor_idea",
        "run_factor_backtest",
        "run_idea_workflow",
        "run_local_web",
        "run_research_once_workflow",
    ]
)

# Additional attributes tests/test_web_workbench.py imports from or reads on
# the server module without patching them.
_MODULE_SURFACE_NAMES = sorted(
    [
        "FactorRepository",
        "MAX_RD_ITERATIONS",
        "MAX_REQUEST_BODY_BYTES",
        "UTC",
        "_WebJobCancelled",
        "_WebJobManager",
        "_client_error_message",
        "_evaluation_payload",
        "_idea_validation_settings",
        "_index_html",
        "_json_safe",
        "_validation_payload",
        "create_local_web_server",
        "datetime",
        "run_idea_parse_workflow",
        "run_idea_validation_workflow",
    ]
)


def test_server_module_exposes_monkeypatch_seams() -> None:
    for name in _MONKEYPATCH_SEAM_NAMES:
        assert hasattr(web_server, name), f"missing monkeypatch seam: {name}"


def test_server_module_exposes_test_surface() -> None:
    for name in _MODULE_SURFACE_NAMES:
        assert hasattr(web_server, name), f"missing module attribute: {name}"


@pytest.fixture()
def web_app(tmp_path):
    create_demo_workspace(tmp_path / "demo")
    config = QuantForgeConfig().resolve(tmp_path / "demo")
    server = create_local_web_server(host="127.0.0.1", port=0, config=config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield base_url
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def _get(url: str) -> tuple[int, str, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.status, response.headers.get("Content-Type", ""), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type", ""), exc.read()


def _post(url: str, payload: dict) -> tuple[int, str, bytes]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.headers.get("Content-Type", ""), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type", ""), exc.read()


def _wait_for_job(base_url: str, job_id: str, *, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status, _, body = _get(f"{base_url}/api/jobs/{job_id}")
        assert status == 200
        payload = json.loads(body.decode("utf-8"))
        if payload["status"] in {"completed", "failed", "cancelled"}:
            return payload
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish")


JSON_CONTENT_TYPE = "application/json; charset=utf-8"
HTML_CONTENT_TYPE = "text/html; charset=utf-8"


def test_get_health_route(web_app) -> None:
    status, content_type, body = _get(f"{web_app}/health")
    assert status == 200
    assert content_type == JSON_CONTENT_TYPE
    assert json.loads(body.decode("utf-8")) == {"ok": True}


def test_get_catalog_route(web_app) -> None:
    status, content_type, body = _get(f"{web_app}/catalog")
    assert status == 200
    assert content_type == JSON_CONTENT_TYPE
    payload = json.loads(body.decode("utf-8"))
    assert "fields" in payload
    assert "operators" in payload


def test_get_api_status_route(web_app) -> None:
    status, content_type, body = _get(f"{web_app}/api/status")
    assert status == 200
    assert content_type == JSON_CONTENT_TYPE
    payload = json.loads(body.decode("utf-8"))
    assert payload["name"] == "Quant Forge"
    assert set(payload["paths"]) == {
        "data_root",
        "factor_root",
        "factor_values_root",
        "factor_values_overlay_root",
        "factor_values_manifest_root",
        "artifact_root",
    }
    assert "provider" in payload["llm"]
    assert "providers" in payload["llm"]
    assert "research_stage" in payload["rd"]


def test_get_research_status_route(web_app) -> None:
    status, content_type, body = _get(f"{web_app}/api/research/status")
    assert status == 200
    assert content_type == JSON_CONTENT_TYPE
    payload = json.loads(body.decode("utf-8"))
    assert payload["enabled"] is False


def test_get_unknown_job_returns_404_json(web_app) -> None:
    status, content_type, body = _get(f"{web_app}/api/jobs/does-not-exist")
    assert status == 404
    assert content_type == JSON_CONTENT_TYPE
    payload = json.loads(body.decode("utf-8"))
    assert "unknown job" in payload["error"]


def test_get_root_serves_html_index(web_app) -> None:
    status, content_type, body = _get(f"{web_app}/")
    assert status == 200
    assert content_type == HTML_CONTENT_TYPE
    assert body.decode("utf-8").startswith("<!doctype html>")


def test_get_unknown_path_falls_back_to_html_index(web_app) -> None:
    """Non-API fallthrough to the index shell is intentional (F-007).

    The frontend is a hash-routed single page, so unknown GET paths outside
    the ``/api`` namespace deliberately serve the index (deep links and typos
    land in the app). Only the API namespace returns 404 JSON -- see
    ``test_get_unknown_api_path_returns_404_json``.
    """

    for path in ("/definitely-not-a-route", "/some/random/page"):
        status, content_type, body = _get(f"{web_app}{path}")
        assert status == 200
        assert content_type == HTML_CONTENT_TYPE
        assert body.decode("utf-8").startswith("<!doctype html>")


def test_get_unknown_api_path_returns_404_json(web_app) -> None:
    """Unknown GET paths in the API namespace return 404 JSON (F-007).

    Scripts and health probes hitting a typo'd endpoint must see a JSON
    error, never HTTP 200 with the HTML shell.
    """

    for path in ("/api/nonexistent", "/api/runtime", "/api", "/api/"):
        status, content_type, body = _get(f"{web_app}{path}")
        assert status == 404, path
        assert content_type == JSON_CONTENT_TYPE, path
        payload = json.loads(body.decode("utf-8"))
        assert "unknown API path" in payload["error"], path


def test_post_run_idea_dispatches_workflow_via_server_namespace(monkeypatch, web_app) -> None:
    captured: dict[str, object] = {}

    def fake_run_idea_workflow(config, text, *, parser_mode, llm_provider, rd_config, cancel_event=None):
        captured["text"] = text
        captured["parser_mode"] = parser_mode
        captured["llm_provider"] = llm_provider
        return {"echo": "run-idea"}

    monkeypatch.setattr(web_server, "run_idea_workflow", fake_run_idea_workflow)

    status, content_type, body = _post(
        f"{web_app}/api/run-idea",
        {"text": "小市值", "parser_mode": "rule"},
    )

    assert status == 200
    assert content_type == JSON_CONTENT_TYPE
    assert json.loads(body.decode("utf-8")) == {"echo": "run-idea"}
    assert captured == {"text": "小市值", "parser_mode": "rule", "llm_provider": None}


def test_post_parse_idea_dispatches_workflow_via_server_namespace(monkeypatch, web_app) -> None:
    captured: dict[str, object] = {}

    def fake_parse_workflow(config, text, *, parser_mode, llm_provider, rd_config, cancel_event=None):
        captured["text"] = text
        captured["parser_mode"] = parser_mode
        return {"echo": "parse-idea"}

    monkeypatch.setattr(web_server, "run_idea_parse_workflow", fake_parse_workflow)

    status, content_type, body = _post(
        f"{web_app}/api/parse-idea",
        {"text": "小市值", "parser_mode": "rule"},
    )

    assert status == 200
    assert content_type == JSON_CONTENT_TYPE
    assert json.loads(body.decode("utf-8")) == {"echo": "parse-idea"}
    assert captured == {"text": "小市值", "parser_mode": "rule"}


def test_post_validate_idea_dispatches_workflow_via_server_namespace(monkeypatch, web_app) -> None:
    captured: dict[str, object] = {}

    def fake_validation_workflow(config, factor, *, parser, parameters, rd_config, cancel_event=None):
        captured["factor_id"] = factor.factor_id
        captured["parser"] = parser
        captured["parameters"] = parameters
        return {"echo": "validate-idea"}

    monkeypatch.setattr(web_server, "run_idea_validation_workflow", fake_validation_workflow)

    status, content_type, body = _post(
        f"{web_app}/api/validate-idea",
        {
            "factor": {"factor_id": "FTR_ROUTE_TEST", "formula": "-rank(market_cap)"},
            "parameters": {"holding_days": 5},
        },
    )

    assert status == 200
    assert content_type == JSON_CONTENT_TYPE
    assert json.loads(body.decode("utf-8")) == {"echo": "validate-idea"}
    assert captured == {
        "factor_id": "FTR_ROUTE_TEST",
        "parser": None,
        "parameters": {"holding_days": 5},
    }


def test_post_staggered_entry_dispatches_workflow_via_server_namespace(monkeypatch, web_app) -> None:
    captured: dict[str, object] = {}

    def fake_staggered_workflow(config, factor_id, *, parameters, formation_trading_days, rd_config, cancel_event=None):
        captured["factor_id"] = factor_id
        captured["formation_trading_days"] = formation_trading_days
        return {"echo": "staggered-entry"}

    monkeypatch.setattr(web_server, "run_staggered_entry_workflow", fake_staggered_workflow)

    status, content_type, body = _post(
        f"{web_app}/api/staggered-entry",
        {"factor_id": "FTR_DEMO_SMALL_CAP", "formation_trading_days": 3},
    )

    assert status == 200
    assert content_type == JSON_CONTENT_TYPE
    assert json.loads(body.decode("utf-8")) == {"echo": "staggered-entry"}
    assert captured == {"factor_id": "FTR_DEMO_SMALL_CAP", "formation_trading_days": 3}


def test_post_research_run_once_dispatches_workflow_via_server_namespace(monkeypatch, web_app) -> None:
    captured: dict[str, object] = {}

    def fake_research_workflow(config, seed_factor_id, *, objective, max_candidates, iterations, rd_config, cancel_event=None):
        captured["seed_factor_id"] = seed_factor_id
        captured["objective"] = objective
        captured["max_candidates"] = max_candidates
        captured["iterations"] = iterations
        return {"echo": "research-run-once"}

    monkeypatch.setattr(web_server, "run_research_once_workflow", fake_research_workflow)

    status, content_type, body = _post(
        f"{web_app}/api/research/run-once",
        {"seed_factor_id": "FTR_DEMO_SMALL_CAP", "objective": "balanced", "max_candidates": 2, "iterations": 1},
    )

    assert status == 200
    assert content_type == JSON_CONTENT_TYPE
    assert json.loads(body.decode("utf-8")) == {"echo": "research-run-once"}
    assert captured == {
        "seed_factor_id": "FTR_DEMO_SMALL_CAP",
        "objective": "balanced",
        "max_candidates": 2,
        "iterations": 1,
    }


@pytest.mark.parametrize(
    ("route", "kind", "seam_name", "payload"),
    [
        ("/api/jobs/run-idea", "run_idea", "run_idea_workflow", {"text": "x", "parser_mode": "rule"}),
        ("/api/jobs/parse-idea", "parse_idea", "run_idea_parse_workflow", {"text": "x", "parser_mode": "rule"}),
        (
            "/api/jobs/validate-idea",
            "validate_idea",
            "run_idea_validation_workflow",
            {"factor": {"factor_id": "FTR_ROUTE_TEST", "formula": "-rank(market_cap)"}},
        ),
        (
            "/api/jobs/staggered-entry",
            "staggered_entry",
            "run_staggered_entry_workflow",
            {"factor_id": "FTR_DEMO_SMALL_CAP"},
        ),
        (
            "/api/jobs/research-run-once",
            "research_run_once",
            "run_research_once_workflow",
            {"seed_factor_id": "FTR_DEMO_SMALL_CAP"},
        ),
    ],
)
def test_post_job_routes_dispatch_workflows_via_server_namespace(
    monkeypatch, web_app, route: str, kind: str, seam_name: str, payload: dict
) -> None:
    invoked = threading.Event()

    def fake_workflow(*args, **kwargs):
        invoked.set()
        return {"echo": kind}

    monkeypatch.setattr(web_server, seam_name, fake_workflow)

    status, content_type, body = _post(f"{web_app}{route}", payload)

    assert status == 202
    assert content_type == JSON_CONTENT_TYPE
    started = json.loads(body.decode("utf-8"))
    assert started["kind"] == kind
    assert started["status"] in {"running", "completed"}

    completed = _wait_for_job(web_app, started["job_id"])
    assert invoked.is_set()
    assert completed["status"] == "completed"
    assert completed["result"] == {"echo": kind}


def test_post_cancel_unknown_job_returns_404(web_app) -> None:
    status, content_type, body = _post(f"{web_app}/api/jobs/does-not-exist/cancel", {})
    assert status == 404
    assert content_type == JSON_CONTENT_TYPE
    payload = json.loads(body.decode("utf-8"))
    assert "unknown job" in payload["error"]


def test_post_research_schedule_rejects_unknown_action(web_app) -> None:
    status, content_type, body = _post(f"{web_app}/api/research/schedule", {"action": "pause"})
    assert status == 400
    assert content_type == JSON_CONTENT_TYPE
    payload = json.loads(body.decode("utf-8"))
    assert payload["error"] == "action must be start or stop"


def test_post_research_schedule_stop_without_start_returns_disabled(web_app) -> None:
    status, content_type, body = _post(f"{web_app}/api/research/schedule", {"action": "stop"})
    assert status == 200
    assert content_type == JSON_CONTENT_TYPE
    payload = json.loads(body.decode("utf-8"))
    assert payload["enabled"] is False


def test_post_unknown_endpoint_returns_404(web_app) -> None:
    status, content_type, body = _post(f"{web_app}/api/not-a-route", {})
    assert status == 404
    assert content_type == JSON_CONTENT_TYPE
    payload = json.loads(body.decode("utf-8"))
    assert "unknown endpoint" in payload["error"]


# ---------------------------------------------------------------------------
# Deep-seam behavioral tests (CP4-2). The workflow-level seams above are
# exercised behaviorally; these cover the remaining monkeypatch seams
# (evaluate_factor, run_factor_backtest, parse_factor_idea,
# _run_research_once, DEFAULT_RD_CONFIG_PATH, _web_public_json,
# run_local_web). Each test patches the name on the server module namespace
# and asserts the fake is actually invoked through the route/function that
# uses it, so a future refactor that early-binds a seam fails loudly here.
# ---------------------------------------------------------------------------


def test_validate_idea_route_invokes_patched_evaluate_and_backtest_seams(monkeypatch, web_app) -> None:
    captured: dict[str, object] = {}

    def fake_evaluate_factor(
        factor_id,
        *,
        factor_root,
        data_root,
        artifact_root,
        horizon_days,
        horizon_days_matrix,
        sample_splits,
        simulation_profile,
        factor_values_root,
        factor_values_overlay_root,
        factor_values_manifest_root,
    ):
        captured["evaluated_factor_id"] = factor_id
        return EvaluationResult(
            factor_id=factor_id,
            observations=1,
            coverage=1.0,
            rank_ic_mean=0.1,
            rank_ic_std=0.0,
            rank_icir=0.0,
            ic_days=1,
            artifact_path=Path(artifact_root) / "evaluations" / f"{factor_id}.json",
            simulation_profile=simulation_profile,
        )

    def fake_run_factor_backtest(
        factor_id,
        *,
        factor_root,
        data_root,
        artifact_root,
        simulation_profile,
        holding_days,
        transaction_costs,
        sample_splits,
        factor_values_root,
        factor_values_overlay_root,
        factor_values_manifest_root,
        sample_role="external_oos_backtest",
        include_partial_final_period=False,
    ):
        captured.setdefault("backtest_sample_roles", []).append(sample_role)
        return BacktestResult(
            factor_id=factor_id,
            periods=1,
            holding_days=holding_days,
            cumulative_return=0.01,
            annualized_return=0.01,
            annualized_volatility=0.0,
            max_drawdown=0.0,
            artifact_path=Path(artifact_root) / "backtests" / f"{factor_id}.json",
            top_quantile=simulation_profile.top_quantile,
            transaction_costs=transaction_costs,
            simulation_profile=simulation_profile,
        )

    monkeypatch.setattr(web_server, "evaluate_factor", fake_evaluate_factor)
    monkeypatch.setattr(web_server, "run_factor_backtest", fake_run_factor_backtest)

    status, content_type, body = _post(
        f"{web_app}/api/validate-idea",
        {
            "factor": {"factor_id": "FTR_DEEP_SEAM", "formula": "-rank(market_cap)"},
            "parameters": {"holding_days": 5},
        },
    )

    assert status == 200
    assert content_type == JSON_CONTENT_TYPE
    payload = json.loads(body.decode("utf-8"))
    assert payload["evaluation"]["factor_id"] == "FTR_DEEP_SEAM"
    assert payload["backtest"]["factor_id"] == "FTR_DEEP_SEAM"
    assert captured["evaluated_factor_id"] == "FTR_DEEP_SEAM"
    assert captured["backtest_sample_roles"] == ["in_sample_backtest", "external_oos_backtest"]


def test_parse_idea_route_invokes_patched_parse_factor_idea_seam(monkeypatch, web_app) -> None:
    captured: dict[str, object] = {}

    def fake_parse_factor_idea(text, llm, *, mode):
        captured["text"] = text
        captured["mode"] = mode
        return ParsedFactor(
            factor=FactorDefinition(
                factor_id="FTR_PARSE_SEAM",
                name="parse_seam",
                formula="-rank(market_cap)",
            ),
            source="patched-seam",
            provider="patched-provider",
            model="patched-model",
        )

    monkeypatch.setattr(web_server, "parse_factor_idea", fake_parse_factor_idea)

    status, content_type, body = _post(
        f"{web_app}/api/parse-idea",
        {"text": "小市值", "parser_mode": "rule"},
    )

    assert status == 200
    assert content_type == JSON_CONTENT_TYPE
    payload = json.loads(body.decode("utf-8"))
    assert payload["parser"] == {
        "source": "patched-seam",
        "provider": "patched-provider",
        "model": "patched-model",
    }
    assert payload["factor"]["factor_id"] == "FTR_PARSE_SEAM"
    assert captured == {"text": "小市值", "mode": "rule"}


def test_research_run_once_route_invokes_patched_run_research_once_seam(
    monkeypatch, web_app, tmp_path
) -> None:
    captured: dict[str, object] = {}

    def fake_run_research_once(config, rd_config, seed_factor_id, *, objective, max_candidates, cancel_event=None):
        captured["seed_factor_id"] = seed_factor_id
        captured["objective"] = objective
        captured["max_candidates"] = max_candidates
        return ResearchLoopResult(
            rd_stage="research",
            seed_factor_id=seed_factor_id,
            objective=objective,
            objective_weights=ResearchObjectiveWeights(),
            gate=ResearchGate(),
            candidates=(),
            accepted_candidate_ids=(),
            report_path=tmp_path / "deep_seam_report.md",
        )

    monkeypatch.setattr(web_server, "_run_research_once", fake_run_research_once)

    status, content_type, body = _post(
        f"{web_app}/api/research/run-once",
        {"seed_factor_id": "FTR_SEED_SEAM", "objective": "balanced", "max_candidates": 3, "iterations": 1},
    )

    assert status == 200
    assert content_type == JSON_CONTENT_TYPE
    payload = json.loads(body.decode("utf-8"))
    assert payload["seed_factor_id"] == "FTR_SEED_SEAM"
    assert payload["candidates"] == []
    assert captured == {
        "seed_factor_id": "FTR_SEED_SEAM",
        "objective": "balanced",
        "max_candidates": 3,
    }


def test_default_rd_config_path_seam_flows_into_index_html_and_routing(monkeypatch, tmp_path) -> None:
    create_demo_workspace(tmp_path / "demo")
    config = QuantForgeConfig().resolve(tmp_path / "demo")
    rd_path = tmp_path / "rd.yaml"
    rd_path.write_text(
        """
objective: rank_icir
default_max_candidates: 7
default_interval_days: 42
allowed_interval_days: [42]
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(web_server, "DEFAULT_RD_CONFIG_PATH", rd_path)

    # _index_html resolves the seam when no rd_config is passed.
    html = web_server._index_html(config)
    assert '<option value="42" selected>42天</option>' in html
    assert 'id="rd-max" type="number" min="1" max="10" value="7"' in html

    # create_local_web_server resolves the seam again for routing.
    server = create_local_web_server(host="127.0.0.1", port=0, config=config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, content_type, body = _get(f"http://127.0.0.1:{server.server_address[1]}/")
        assert status == 200
        assert content_type == HTML_CONTENT_TYPE
        assert '<option value="42" selected>42天</option>' in body.decode("utf-8")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def test_web_public_json_seam_transforms_job_results(monkeypatch, web_app) -> None:
    seen: dict[str, object] = {}

    def fake_workflow(config, text, *, parser_mode, llm_provider, rd_config, cancel_event=None):
        return {"marker": "raw-result"}

    def fake_web_public_json(value):
        seen["raw"] = value
        return {"marker": "public-result", "via": "patched_web_public_json"}

    monkeypatch.setattr(web_server, "run_idea_workflow", fake_workflow)
    monkeypatch.setattr(web_server, "_web_public_json", fake_web_public_json)

    status, content_type, body = _post(f"{web_app}/api/jobs/run-idea", {"text": "x", "parser_mode": "rule"})

    assert status == 202
    assert content_type == JSON_CONTENT_TYPE
    started = json.loads(body.decode("utf-8"))
    completed = _wait_for_job(web_app, started["job_id"])
    assert completed["status"] == "completed"
    assert completed["result"] == {"marker": "public-result", "via": "patched_web_public_json"}
    assert seen["raw"] == {"marker": "raw-result"}


def test_run_local_web_seam_resolved_via_cli_lazy_import(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("QF_ROUTES_WEB_SEAM_KEY", raising=False)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
llm:
  provider: deepseek
  providers:
    deepseek:
      model: deepseek-chat
      base_url: https://api.deepseek.com
      api_key_env: QF_ROUTES_WEB_SEAM_KEY
""",
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    def fake_run_local_web(*, host, port, config, rd_config):
        captured["host"] = host
        captured["port"] = port
        captured["rd_objective"] = rd_config.objective

    monkeypatch.setattr(web_server, "run_local_web", fake_run_local_web)

    assert cli_main.main(["web", "--config", str(config_path), "--port", "8912"]) == 0
    assert captured == {"host": "127.0.0.1", "port": 8912, "rd_objective": "balanced"}


def test_get_api_status_internal_value_error_is_not_reflected(monkeypatch, web_app) -> None:
    """Pre-existing GET routes keep the generic error mapping.

    A ValueError raised inside an existing GET route (for example detailed
    LLM-config text from ``select_provider``) must surface as the generic
    400 ``request failed`` -- never as the reflected exception message that
    the two new read-only endpoints use for their limit validation.
    ``_active_llm`` is not one of the documented monkeypatch seams, so
    routing binds it at module import; the patch therefore targets the
    routing module binding (patching only the server namespace would not
    reach the route, by CP4-1 design).
    """

    import quant_forge.apps.web.routing as web_routing

    def raise_config_detail(config):
        raise ValueError("SENTINEL-CONFIG-DETAIL")

    monkeypatch.setattr(web_routing, "_active_llm", raise_config_detail)
    monkeypatch.setattr(web_server, "_active_llm", raise_config_detail)

    status, content_type, body = _get(f"{web_app}/api/status")

    assert status == 400
    assert content_type == JSON_CONTENT_TYPE
    text = body.decode("utf-8")
    assert "SENTINEL-CONFIG-DETAIL" not in text
    assert json.loads(text) == {"error": "request failed"}


def test_run_local_web_flushes_startup_url_line(monkeypatch) -> None:
    """The startup URL line must reach a redirected stdout immediately (F-004).

    With stdout redirected to a file or pipe (docker logs, shell
    redirection) the interpreter block-buffers, and ``serve_forever`` never
    returns, so a print without ``flush=True`` leaves the listening URL
    invisible while the server is healthy. Pin that ``run_local_web``
    flushes stdout after writing the line.
    """

    import quant_forge.apps.web.routing as web_routing

    class _RecordingStream(io.StringIO):
        def __init__(self) -> None:
            super().__init__()
            self.flushed_after_write = False

        def flush(self) -> None:  # pragma: no cover - trivial
            if self.getvalue():
                self.flushed_after_write = True
            super().flush()

    class _FakeServer:
        server_address = ("127.0.0.1", 8123)

        def serve_forever(self) -> None:
            return None

    captured_kwargs: dict[str, object] = {}

    def fake_create_local_web_server(**kwargs):
        captured_kwargs.update(kwargs)
        return _FakeServer()

    monkeypatch.setattr(web_routing, "create_local_web_server", fake_create_local_web_server)
    stream = _RecordingStream()
    monkeypatch.setattr(sys, "stdout", stream)

    web_routing.run_local_web(host="127.0.0.1", port=8123, config=QuantForgeConfig(), rd_config=None)

    assert captured_kwargs["host"] == "127.0.0.1"
    assert "Quant Forge local web listening on http://127.0.0.1:8123" in stream.getvalue()
    assert stream.flushed_after_write


def test_cmd_web_missing_control_token_prints_single_actionable_line(monkeypatch, tmp_path, capsys) -> None:
    """A predictable control-token misconfiguration must not raise a traceback (F-006).

    ``qf web`` bound to 0.0.0.0 without the control token env var set is a
    correct refusal; the CLI boundary must present it as one actionable line
    naming the env var and exit nonzero. Other exception types still
    propagate (no blanket catch) -- see the sibling unreadable-config path,
    which is out of scope here.
    """

    monkeypatch.delenv("QF_ROUTES_WEB_MISSING_TOKEN", raising=False)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
web:
  host: 0.0.0.0
  allow_docker_bind: true
  control_token_env: QF_ROUTES_WEB_MISSING_TOKEN
""",
        encoding="utf-8",
    )

    exit_code = cli_main.main(["web", "--config", str(config_path)])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "Traceback" not in captured.out
    assert "Traceback" not in captured.err
    lines = [line for line in captured.out.splitlines() if line.strip()]
    assert len(lines) == 1
    assert "QF_ROUTES_WEB_MISSING_TOKEN" in lines[0]
    assert "qf web startup blocked" in lines[0]

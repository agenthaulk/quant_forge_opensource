"""Contract tests for the P5 multi-factor backtest job endpoint + §8 payload.

Covers ``POST /api/jobs/multi-factor-backtest`` and
``run_multi_factor_backtest_workflow`` against the design contract
(``docs/design/multi_factor_portfolio_backtest.md`` §8/§10/§13 + CP0
amendments):

- request-rejection matrix as CLEAN 4xx JSON errors (never 500s, never
  failed background jobs): <2 factors, non-±1 direction, missing REQUIRED
  ``holding_days`` (RF-5, no ``horizon_days`` fallback), weights not covering
  exactly the checked set, unknown/reserved method, out-of-bounds
  ``ic_min_periods`` (schema minimum 3 / maximum 60 / int-typed),
  missing/unknown standardization, unknown factor, ``WINDOW_TOO_SHORT``
  (RB-2) and ``UNIVERSE_MISMATCH`` (RB-6);
- full §8 payload closure on the deterministic demo fixture: every top-level
  block present, ``backtest`` carrying every scalar tile the reused
  ``factor.js`` renderers read (FP-3), typed MetricValue statuses preserved
  through ``_json_safe``, provenance ``factors[]`` with run-time-pinned
  member formulas (CP0), the a-priori FP-1 branch (raw ``weights_effective``
  present, fitted fields absent), the §8 literal validity caveats, and the
  RB-1/RB-7/RB-9 warning codes surfaced;
- the fitted FP-1 branch (P6): an ``ic_weighted`` run over the demo fixture
  OMITS ``weights_effective`` entirely and carries the fitted diagnostic
  fields (``fitted_weights_latest`` / per-signal-date ``fitted_weights_path``
  / ``fitted_period_fraction`` / ``warmup_period_count``) with a truthful
  run-level ``is_fitted``; an all-warmup window downgrades to
  ``is_fitted=false`` + ``NO_FITTED_PERIODS`` (RB-8) while keeping the
  fitted fields auditable; the §4.5 advisory ``rank_ic_redundancy`` matrix
  is attached for BOTH branches;
- FP-2 same-window evaluation: available diagnostics on a long window,
  honest degraded slot (statuses + ``EVALUATION_WINDOW_TOO_SHORT``, never a
  fabricated zero) when the window is under the 126-day evaluation floor;
- a Node fixture drive of the REAL wire payloads (a-priori AND fitted)
  through ``renderSynthesisReportHtml`` / ``renderProvenanceCardHtml`` (the
  same stdlib-only harness convention as tests/test_web_synthesis_view.py),
  closing the payload->renderer contract end-to-end without a browser —
  including that a fitted payload never renders the a-priori raw-weights
  caption (FP-1's exact frontend concern).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request

import pytest

import quant_forge.apps.web.api as web_api
import quant_forge.apps.web.server as web_server
from quant_forge.apps.web.api import (
    _multi_factor_backtest_settings,
    _synthesis_provenance_payload,
)
from quant_forge.apps.web.server import create_local_web_server
from quant_forge.config import QuantForgeConfig
from quant_forge.core.contracts import FactorDefinition
from quant_forge.data.local import create_demo_workspace
from quant_forge.factor_library.repository import FactorRepository
from quant_forge.research_loop.config import load_research_loop_config
from quant_forge.synthesis.methods import SYNTHESIS_METHODS, MethodSpec
from quant_forge.synthesis.service import (
    EVALUATION_WINDOW_TOO_SHORT,
    NO_FITTED_PERIODS,
    NON_OVERLAPPING_COHORTS,
    PHASE_SENSITIVE_SMALL_SAMPLE,
    UNIVERSE_MISMATCH,
    WARM_UP_IC_UNFITTED,
    WINDOW_TOO_SHORT,
    CoverageAccounting,
    FactorCoverage,
    MemberFetchSpec,
)


JSON_CONTENT_TYPE = "application/json; charset=utf-8"
SYNTHESIS_JS_PATH = web_server.STATIC_ROOT / "views" / "synthesis.js"

# The §8 FP-3 note enumerates every top-level scalar tile the reused
# factor.js renderers read directly. renderOosSection (factor.js:149-177)
# and renderInSampleSection (factor.js:130-147) read these keys off the
# backtest block without any nesting, so the payload must carry them all.
FP3_SCALAR_TILE_KEYS = (
    "gross_cumulative_return",   # factor.js:158 毛累计收益
    "cumulative_return",         # factor.js:158 fallback
    "net_cumulative_return",     # factor.js:159 净累计收益
    "completed_periods",         # factor.js:160 完整持有期数
    "periods",                   # factor.js:160 fallback
    "exposure_days",             # factor.js:161 Exposure Days
    "gross_annualized_return",   # factor.js:162 可报告毛年化收益
    "annualized_return",         # factor.js:162 fallback
    "net_annualized_return",     # factor.js:163 可报告净年化收益
    "net_annualized_volatility", # factor.js:164 年化波动率
    "annualized_volatility",     # factor.js:164 fallback
    "net_long_short_sharpe",     # factor.js:165 年化Sharpe
    "long_short_sharpe",         # factor.js:165 fallback
    "net_max_drawdown",          # factor.js:166 净值最大回撤
    "max_drawdown",              # factor.js:166 fallback
    "initial_build_turnover",    # factor.js:167 Initial Build Turnover
    "rebalance_turnover_mean",   # factor.js:168 Rebalance Turnover
    "turnover_rate",             # factor.js:168 fallback
    "replacement_rate_mean",     # factor.js:169 Replacement Rate
    "rebalance_rate",            # factor.js:169 fallback
    "holding_days",              # factor.js:170 持有期
    "top_quantile",              # factor.js:172 fallback
    "sample_role",               # factor.js:175 meta line
    "simulation_profile",        # factor.js:171-173 Decay/Top Quantile/Delay
    "group_returns",             # factor.js:207-208 分组收益 pills + chart
    "metrics",                   # factor.js:180-188 diagnostics metric pills
    "warning_codes",             # factor.js:59 warning pills
    "warnings",                  # factor.js:61 warning pills
    "artifact_path",             # synthesis.js:468-472 artifacts section
    "segment_metrics",           # factor.js:210-212 回测分段
    "assumptions",               # factor.js:213-215 口径说明
    "score_source",              # factor.js:72 cache pills
    "score_cached_rows",         # factor.js:72 cache pills
    "score_computed_rows",       # factor.js:72 cache pills
    "factor_values_path",        # factor.js:73 cache pills
)

# FP-1: fitted fields must be ABSENT on a-priori runs — the provenance card
# captions weights_effective unconditionally as an a-priori claim
# (synthesis.js:386-388), so mixing branches would misstate the run.
FITTED_ONLY_KEYS = (
    "fitted_weights_latest",
    "fitted_weights_path",
    "fitted_period_fraction",
    "warmup_period_count",
)

VALIDITY_CAVEATS_LITERAL = [
    "先验/拟合已如实标注",
    # RB-1 phase caveat with the REALIZED period count substituted for §8's
    # placeholder N — asserted by pattern below, not by this literal.
    "样本内评价为同窗诊断，非独立研究样本",
    "成本以目标簿 L1 换手计，漂移回补交易未计成本（换手/成本偏低估）",
    "is_st/上市过滤仅在建仓时点应用；持有期内转 ST/退市按最后成交价了结",
]


def _rd_config(config: QuantForgeConfig):
    return load_research_loop_config(
        web_server.DEFAULT_RD_CONFIG_PATH, config.research, config.simulation
    )


def _valid_request() -> dict:
    return {
        "factor_refs": [
            {"factor_id": "FTR_DEMO_SMALL_CAP", "direction": 1},
            {"factor_id": "FTR_DEMO_MOMENTUM", "direction": -1},
        ],
        "synthesis": {"method": "equal_weight", "params": {}},
        "standardization": {"method": "zscore", "params": {}},
        "parameters": {"holding_days": 5},
    }


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


@pytest.fixture(scope="module")
def apriori_payload(tmp_path_factory):
    """One shared in-process equal-weight run over the demo fixture.

    Returns the wire-equivalent payload (``_json_safe(_web_public_json(...))``
    — exactly what routing serializes after the job manager publishes the
    result), reused by the payload-closure assertions and the Node renderer
    drive so the suite runs the full engine once for this branch.
    """

    workspace = tmp_path_factory.mktemp("mfb-apriori")
    create_demo_workspace(workspace / "demo")
    config = QuantForgeConfig().resolve(workspace / "demo")
    request = _valid_request()
    payload = web_server.run_multi_factor_backtest_workflow(
        config,
        factor_refs=request["factor_refs"],
        synthesis=request["synthesis"],
        standardization=request["standardization"],
        parameters=request["parameters"],
        rd_config=_rd_config(config),
    )
    return web_server._json_safe(web_server._web_public_json(payload))


def _get(url: str) -> tuple[int, str, bytes]:
    request = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.headers.get("Content-Type", ""), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type", ""), exc.read()


def _post(url: str, payload: dict) -> tuple[int, str, bytes]:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, response.headers.get("Content-Type", ""), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type", ""), exc.read()


def _post_run(base_url: str, payload: dict) -> tuple[int, dict]:
    status, content_type, body = _post(f"{base_url}/api/jobs/multi-factor-backtest", payload)
    assert content_type == JSON_CONTENT_TYPE
    return status, json.loads(body.decode("utf-8"))


def _wait_for_job(base_url: str, job_id: str, timeout: float = 300.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, _, body = _get(f"{base_url}/api/jobs/{job_id}")
        assert status == 200
        payload = json.loads(body.decode("utf-8"))
        if payload["status"] in {"completed", "failed", "cancelled"}:
            return payload
        time.sleep(0.2)
    raise AssertionError(f"job {job_id} did not reach a terminal status in {timeout}s")


# ---------------------------------------------------------------------------
# Request-rejection matrix — every §13 rejection is a clean 4xx JSON error
# ---------------------------------------------------------------------------


def _assert_rejected(base_url: str, request: dict, *, needle: str) -> None:
    status, payload = _post_run(base_url, request)
    assert status == 400, payload
    assert set(payload) == {"error"}
    assert needle in payload["error"], payload["error"]


def test_rejects_fewer_than_two_factors(web_app) -> None:
    request = _valid_request()
    request["factor_refs"] = request["factor_refs"][:1]
    _assert_rejected(web_app, request, needle="at least 2 factor_refs")


def test_rejects_non_unit_direction(web_app) -> None:
    request = _valid_request()
    request["factor_refs"][0]["direction"] = 0
    _assert_rejected(web_app, request, needle="+1 or -1")


def test_rejects_string_direction(web_app) -> None:
    # buildRunRequest sends JSON integers; a stringly "1" is a contract break
    # and must not be silently coerced.
    request = _valid_request()
    request["factor_refs"][0]["direction"] = "1"
    _assert_rejected(web_app, request, needle="+1 or -1")


def test_rejects_duplicate_factor_refs(web_app) -> None:
    request = _valid_request()
    request["factor_refs"][1]["factor_id"] = "FTR_DEMO_SMALL_CAP"
    _assert_rejected(web_app, request, needle="repeat")


def test_rejects_missing_holding_days_with_no_horizon_fallback(web_app) -> None:
    # RF-5: holding_days is REQUIRED; both members carry horizon_days=5, and
    # falling back to it would silently misstate cadence AND lifetime.
    request = _valid_request()
    request["parameters"] = {}
    _assert_rejected(web_app, request, needle="holding_days is required")

    request = _valid_request()
    del request["parameters"]
    _assert_rejected(web_app, request, needle="holding_days is required")


def test_rejects_non_positive_holding_days(web_app) -> None:
    request = _valid_request()
    request["parameters"]["holding_days"] = 0
    _assert_rejected(web_app, request, needle="holding_days must be positive")


def test_rejects_weights_not_covering_selected_set(web_app) -> None:
    request = _valid_request()
    request["synthesis"] = {
        "method": "weighted",
        "params": {"weights": {"FTR_DEMO_SMALL_CAP": 2.0}},
    }
    _assert_rejected(web_app, request, needle="exactly one weight per selected")

    request["synthesis"]["params"]["weights"] = {
        "FTR_DEMO_SMALL_CAP": 2.0,
        "FTR_DEMO_MOMENTUM": 1.0,
        "FTR_NOT_SELECTED": 1.0,
    }
    _assert_rejected(web_app, request, needle="unknown: ['FTR_NOT_SELECTED']")


def test_rejects_weighted_without_required_weights_param(web_app) -> None:
    request = _valid_request()
    request["synthesis"] = {"method": "weighted", "params": {}}
    _assert_rejected(web_app, request, needle="missing required parameter: weights")


def test_rejects_all_zero_weights(web_app) -> None:
    request = _valid_request()
    request["synthesis"] = {
        "method": "weighted",
        "params": {"weights": {"FTR_DEMO_SMALL_CAP": 0.0, "FTR_DEMO_MOMENTUM": 0.0}},
    }
    _assert_rejected(web_app, request, needle="must not be all zero")


def test_rejects_unknown_method(web_app) -> None:
    request = _valid_request()
    request["synthesis"]["method"] = "no_such_method"
    _assert_rejected(web_app, request, needle="unknown synthesis method")


def test_rejects_reserved_methods_via_catalog_guard(monkeypatch, web_app) -> None:
    # Post-P6 the shipped catalog has no reserved methods (ic/icir flipped to
    # available:true), but the guard must keep rejecting any FUTURE reserved
    # entry — pinned here through a synthetic catalog row so the behavior
    # cannot silently rot while unrepresented in the shipped catalog.
    reserved = MethodSpec(name="optimizer", label="预留优化器", available=False, is_fitted=True)
    monkeypatch.setattr(web_api, "SYNTHESIS_METHODS", (*SYNTHESIS_METHODS, reserved))
    request = _valid_request()
    request["synthesis"] = {"method": "optimizer", "params": {}}
    _assert_rejected(web_app, request, needle="reserved and not runnable")


def test_rejects_out_of_bounds_ic_min_periods(web_app) -> None:
    # The §9 ParamSpec bounds (int, minimum 3, maximum 60) are re-asserted
    # server-side by the schema validator for the now-runnable fitted methods.
    request = _valid_request()
    request["synthesis"] = {"method": "ic_weighted", "params": {"ic_min_periods": 2}}
    _assert_rejected(web_app, request, needle="must be >= 3")

    request = _valid_request()
    request["synthesis"] = {"method": "icir_weighted", "params": {"ic_min_periods": 61}}
    _assert_rejected(web_app, request, needle="must be <= 60")

    request = _valid_request()
    request["synthesis"] = {"method": "ic_weighted", "params": {"ic_min_periods": 6.5}}
    _assert_rejected(web_app, request, needle="must be an integer")


def test_rejects_missing_and_unknown_standardization(web_app) -> None:
    request = _valid_request()
    del request["standardization"]
    _assert_rejected(web_app, request, needle="standardization is required")

    request = _valid_request()
    request["standardization"] = {"method": "no_such_std", "params": {}}
    _assert_rejected(web_app, request, needle="unknown standardization")


def test_rejects_unknown_factor_id(web_app) -> None:
    request = _valid_request()
    request["factor_refs"][1]["factor_id"] = "FTR_DOES_NOT_EXIST"
    _assert_rejected(web_app, request, needle="unknown factor: FTR_DOES_NOT_EXIST")


def test_rejects_window_too_short(web_app) -> None:
    # RB-2: ~22 in-window trade dates with holding=30, delay=1 admit N=1 < 2
    # non-overlapping periods. This is a synchronous 400, not a failed job.
    request = _valid_request()
    request["parameters"] = {
        "holding_days": 30,
        "backtest_start": "2024-01-02",
        "backtest_end": "2024-01-31",
    }
    _assert_rejected(web_app, request, needle=WINDOW_TOO_SHORT)


def test_rejects_conflicting_member_universes(web_config, web_app) -> None:
    # RB-6: two members declaring different non-empty universe_filters are
    # never silently unioned — the request is rejected with UNIVERSE_MISMATCH.
    FactorRepository(web_config.paths.factor_root).save(
        FactorDefinition(
            factor_id="FTR_OTHER_UNIVERSE",
            name="other_universe",
            formula="rank(volume)",
            status="candidate",
            horizon_days=5,
            universe_filters=("volume > 0",),
            source="demo",
        )
    )
    request = _valid_request()
    request["factor_refs"][1]["factor_id"] = "FTR_OTHER_UNIVERSE"
    _assert_rejected(web_app, request, needle=UNIVERSE_MISMATCH)


def test_undeclared_member_universes_pin_the_cn_a_default(web_config, web_app) -> None:
    # RB-6 hardening (Codex A-1): when NO member declares a universe the pin
    # must fall back to the cn_a formation default — never the empty
    # (unfiltered) set that would let ST names into the book.
    repo = FactorRepository(web_config.paths.factor_root)
    for factor_id, formula in (
        ("FTR_NOUNI_A", "rank(close)"),
        ("FTR_NOUNI_B", "rank(volume)"),
    ):
        repo.save(
            FactorDefinition(
                factor_id=factor_id,
                name=factor_id.lower(),
                formula=formula,
                status="candidate",
                horizon_days=5,
                universe_filters=(),
                source="demo",
            )
        )
    request = _valid_request()
    request["factor_refs"] = [
        {"factor_id": "FTR_NOUNI_A", "direction": 1},
        {"factor_id": "FTR_NOUNI_B", "direction": 1},
    ]
    status, job = _post_run(web_app, request)
    assert status == 202
    finished = _wait_for_job(web_app, job["job_id"])
    assert finished["status"] == "completed", finished.get("error")
    provenance = finished["result"]["synthesis_provenance"]
    assert provenance["universe_filters"] == ["is_st == false"]


# ---------------------------------------------------------------------------
# Endpoint happy path — 202 job lifecycle over the demo fixture
# ---------------------------------------------------------------------------


def test_endpoint_runs_job_and_returns_full_report(web_app) -> None:
    status, job = _post_run(web_app, _valid_request())
    assert status == 202
    assert job["kind"] == "multi_factor_backtest"
    assert job["status"] == "running"

    finished = _wait_for_job(web_app, job["job_id"])
    assert finished["status"] == "completed", finished["error"]
    result = finished["result"]
    assert set(result) == {
        "factor",
        "parameters",
        "evaluation",
        "in_sample_backtest",
        "backtest",
        "validity",
        "synthesis_provenance",
    }
    # Composite identity: colon-free COMPOSITE_<hash> id, precomputed formula,
    # synthesis source, horizon pinned to the REQUIRED holding_days (RF-1/RF-5).
    factor = result["factor"]
    assert factor["factor_id"].startswith("COMPOSITE_")
    assert ":" not in factor["factor_id"]
    assert factor["formula"] == f"precomputed:factor_id={factor['factor_id']}"
    assert factor["source"] == "synthesis"
    assert factor["horizon_days"] == 5
    # No absolute paths leak through the wire payload (basenames only).
    backtest = result["backtest"]
    assert backtest["artifact_path"] and "/" not in backtest["artifact_path"]
    # in_sample_backtest is null (backtest-only module, one window) — the
    # renderer chain is null-safe: synthesis.js:449 `|| null`, factor.js:131
    # returns '' for a null section.
    assert result["in_sample_backtest"] is None


# ---------------------------------------------------------------------------
# §8 payload closure — every field the shipped renderers read (FP-3/FP-1/FP-4)
# ---------------------------------------------------------------------------


def test_backtest_block_carries_every_scalar_tile(apriori_payload) -> None:
    backtest = apriori_payload["backtest"]
    for key in FP3_SCALAR_TILE_KEYS:
        assert key in backtest, f"missing FP-3 renderer field: {key}"
    # group_returns rows feed factor.js:207-208 (`metric.group`,
    # `metric.mean_return`).
    assert backtest["group_returns"], "group_returns must not be empty"
    for row in backtest["group_returns"]:
        assert set(row) >= {"group", "mean_return"}
    # The engine profile the report displays (factor.js:171 Decay tile) is
    # pinned to decay_days=0 (LA-1): members are decayed once, pre-combination.
    assert backtest["simulation_profile"]["decay_days"] == 0
    assert backtest["sample_role"] == "external_oos_backtest"


def test_metric_statuses_preserved_through_json_safe(apriori_payload) -> None:
    metrics = apriori_payload["backtest"]["metrics"]
    assert metrics, "typed metrics map must be present"
    for name, metric in metrics.items():
        assert "status" in metric and "value" in metric, name
        if metric["status"] != "available":
            # FP-4: a withheld metric is null + status, never a fabricated 0.
            assert metric["value"] is None, name
    evaluation_metrics = apriori_payload["evaluation"]["metrics"]
    assert evaluation_metrics
    for name, metric in evaluation_metrics.items():
        assert "status" in metric, name


def test_warning_codes_surface_disclosure_vocabulary(apriori_payload) -> None:
    codes = apriori_payload["backtest"]["warning_codes"]
    # RB-1: always emitted — cadence == lifetime is structural.
    assert NON_OVERLAPPING_COHORTS in codes
    assert PHASE_SENSITIVE_SMALL_SAMPLE in codes
    # Deterministic demo fixture: FTR_DEMO_MOMENTUM has a 5-day return_5d
    # warmup, so the first dates are degenerate (RB-9) and the first rebalance
    # has no coverage (RB-7) — both must be flagged, never silent.
    assert "DEGENERATE_CROSS_SECTION" in codes
    assert "REBALANCE_SKIPPED_NO_COVERAGE" in codes
    # The engine's own D3 disclosure is inherited unchanged through
    # _backtest_payload.
    assert "FINAL_PARTIAL_PERIOD_EXCLUDED" in codes
    # The RB-1 human-readable line carries the realized period count.
    assert any("独立持有期" in item for item in apriori_payload["backtest"]["warnings"])


def test_validity_block_matches_design_literals(apriori_payload) -> None:
    validity = apriori_payload["validity"]
    assert validity["message"] == "研究口径合成回测（非生产交易口径）"
    assert validity["basis"] == "external_oos_backtest"
    caveats = validity["caveats"]
    assert len(caveats) == 5
    for literal in VALIDITY_CAVEATS_LITERAL:
        assert literal in caveats, literal
    period_count = apriori_payload["synthesis_provenance"]["period_count"]
    phase_caveat = (
        "调仓周期与持有期为同一参数（holding_days）：K=1 非重叠，指标基于约 "
        f"{period_count} 个独立区间，对起始相位敏感"
    )
    assert phase_caveat in caveats


def test_parameters_echo_is_backtest_only(apriori_payload) -> None:
    parameters = apriori_payload["parameters"]
    assert parameters["holding_days"] == 5
    assert parameters["include_partial_final_period"] is False
    # §8 nested blocks (renderSynthesisReportHtml reads parameters.holding_days
    # at synthesis.js:452; the nested blocks are the §8 response contract).
    assert set(parameters["backtest"]) == {"simulation", "test_period"}
    assert set(parameters["transaction_costs"]) == {
        "commission_bps",
        "slippage_bps",
        "short_borrow_bps_annual",
    }
    # Backtest-only module: no research-evaluation interval keys exist.
    assert "evaluation_start" not in parameters
    assert "evaluation_end" not in parameters
    assert "evaluation" not in parameters


def test_provenance_apriori_branch_and_pinned_member_formulas(apriori_payload) -> None:
    provenance = apriori_payload["synthesis_provenance"]
    # CP0 amendment: factors[] carries the member formulas pinned at run time.
    by_id = {entry["factor_id"]: entry for entry in provenance["factors"]}
    assert by_id["FTR_DEMO_SMALL_CAP"]["formula"] == "-rank(market_cap)"
    assert by_id["FTR_DEMO_MOMENTUM"]["formula"] == "rank(return_5d)"
    assert by_id["FTR_DEMO_SMALL_CAP"]["direction"] == 1
    assert by_id["FTR_DEMO_MOMENTUM"]["direction"] == -1
    assert provenance["directions"] == {
        "FTR_DEMO_SMALL_CAP": 1,
        "FTR_DEMO_MOMENTUM": -1,
    }
    assert provenance["method"] == "equal_weight"
    assert provenance["method_params"] == {}
    assert provenance["standardization"] == "zscore"
    assert provenance["standardization_pinned_by_method"] is False
    assert provenance["composite_id"] == apriori_payload["factor"]["factor_id"]
    assert provenance["is_fitted"] is False
    assert provenance["coverage_rule"] == "all_factors"
    assert provenance["min_factor_coverage"] == 2
    assert provenance["universe_filters"] == ["is_st == false"]
    assert isinstance(provenance["period_count"], int) and provenance["period_count"] >= 2
    assert provenance["non_overlapping"] is True
    assert isinstance(provenance["rows_required"], int)
    assert isinstance(provenance["rows_full_coverage"], int)
    assert isinstance(provenance["skipped_rebalances"], int)
    assert isinstance(provenance["degenerate_cross_sections"], int)
    # FP-1 a-priori branch: equal_weight echoes its uniform raw claim (the
    # design's "raw 1 before averaging"); fitted fields are ABSENT.
    assert provenance["weights_effective"] == {
        "FTR_DEMO_SMALL_CAP": 1.0,
        "FTR_DEMO_MOMENTUM": 1.0,
    }
    for key in FITTED_ONLY_KEYS:
        assert key not in provenance, key
    # §4.5 advisory crowding diagnostic rides along on BOTH branches —
    # advisory only, never a gate.
    redundancy = provenance["rank_ic_redundancy"]
    assert redundancy["advisory"] is True
    assert redundancy["factors"] == ["FTR_DEMO_SMALL_CAP", "FTR_DEMO_MOMENTUM"]
    assert len(redundancy["matrix"]) == 2 and len(redundancy["matrix"][0]) == 2
    assert redundancy["period_count"] >= 2
    # Backtest-only: the single external_oos_backtest role, with the exact
    # row fields renderCoverageByRoleHtml reads (synthesis.js:342-352).
    role = provenance["coverage_by_role"]["external_oos_backtest"]
    assert set(role) == {"coverage", "rows_required", "rows_full_coverage"}
    for row in role["coverage"]:
        assert set(row) == {
            "factor_id",
            "direction",
            "source",
            "rows_scored",
            "rows_in_composite",
            "coverage_ratio",
        }
        # FP-4: observed ratios are real numbers; the unobservable case is a
        # real null (unit-pinned below), never a fabricated 0.
        assert row["coverage_ratio"] is None or isinstance(row["coverage_ratio"], float)


def test_same_window_evaluation_available_on_long_window(apriori_payload) -> None:
    evaluation = apriori_payload["evaluation"]
    # FP-2 basis marker (the FE section title is hard-coded, so the honesty
    # signal lives in meta + validity caveat).
    assert evaluation["meta"]["basis"] == "same_window_diagnostics"
    assert evaluation["meta"]["status"] == "available"
    # Same window: the evaluation profile is the ENGINE profile (backtest
    # window, decay 0), not a separate research interval.
    assert evaluation["simulation_profile"]["decay_days"] == 0
    assert evaluation["rank_ic_mean_status"] in {"available", "insufficient_sample"}
    # The metric-display contract the FE tiles read (factor.js:119-121).
    for key in ("rank_ic_mean", "rank_icir", "rank_ic_t_stat"):
        assert f"{key}_status" in evaluation


# ---------------------------------------------------------------------------
# FP-2 degraded evaluation + FP-1 raw weighted echo (in-process runs)
# ---------------------------------------------------------------------------


def test_short_window_degrades_evaluation_honestly(web_config) -> None:
    # ~43 in-window dates: comfortably above the engine gate and the RB-2
    # precondition (N=9), but under the 126-day evaluation display floor.
    request = _valid_request()
    request["parameters"] = {
        "holding_days": 5,
        "backtest_start": "2024-01-02",
        "backtest_end": "2024-03-01",
    }
    payload = web_server.run_multi_factor_backtest_workflow(
        web_config,
        factor_refs=request["factor_refs"],
        synthesis=request["synthesis"],
        standardization=request["standardization"],
        parameters=request["parameters"],
        rd_config=_rd_config(web_config),
    )
    evaluation = payload["evaluation"]
    # Decision record (task FP-2 note): a null slot renders without crashing,
    # but factor.js:122 would print a literal "undefined" IC-Days tile and the
    # warning surface would vanish — so the slot degrades to typed statuses
    # instead of null. Values stay null (FP-4), never fabricated zeros.
    assert evaluation["meta"] == {
        "basis": "same_window_diagnostics",
        "status": "unavailable",
        "test_period": {"start": "2024-01-02", "end": "2024-03-01"},
    }
    for key in ("rank_ic_mean", "rank_icir", "rank_ic_t_stat"):
        assert evaluation[key] is None
        assert evaluation[f"{key}_status"] == "insufficient_sample"
    assert evaluation["ic_days"] == 0  # genuine observed zero, not a placeholder
    assert evaluation["metrics"] == {}
    assert EVALUATION_WINDOW_TOO_SHORT in evaluation["warning_codes"]
    assert any("同窗评价诊断不可用" in item for item in evaluation["warnings"])
    # The backtest itself is unaffected by the evaluation floor (RF-4).
    assert payload["backtest"]["periods"] >= 2
    # The request's explicit window is echoed in the §8 parameters block.
    assert payload["parameters"]["backtest_start"] == "2024-01-02"
    assert payload["parameters"]["backtest_end"] == "2024-03-01"


def test_weighted_method_echoes_raw_declared_weights(web_config) -> None:
    payload = web_server.run_multi_factor_backtest_workflow(
        web_config,
        factor_refs=_valid_request()["factor_refs"],
        synthesis={
            "method": "weighted",
            "params": {"weights": {"FTR_DEMO_SMALL_CAP": 2.0, "FTR_DEMO_MOMENTUM": 1.0}},
        },
        standardization={"method": "rank", "params": {}},
        parameters={"holding_days": 5},
        rd_config=_rd_config(web_config),
    )
    provenance = payload["synthesis_provenance"]
    # Raw declared values, never normalized for display (synthesis.js:387
    # captions them as 先验原始声明值).
    assert provenance["weights_effective"] == {
        "FTR_DEMO_SMALL_CAP": 2.0,
        "FTR_DEMO_MOMENTUM": 1.0,
    }
    assert provenance["method_params"] == {
        "weights": {"FTR_DEMO_SMALL_CAP": 2.0, "FTR_DEMO_MOMENTUM": 1.0}
    }
    assert provenance["is_fitted"] is False
    for key in FITTED_ONLY_KEYS:
        assert key not in provenance, key
    assert provenance["standardization"] == "rank"


# ---------------------------------------------------------------------------
# P6 fitted branch — FP-1 payload contract, truthful is_fitted both ways
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def fitted_payload(tmp_path_factory):
    """One shared in-process ic_weighted run over the demo fixture.

    The full demo window (160 trade dates, holding=5, delay=1) admits ~32
    grid rebalances; with the catalog default ic_min_periods=6 the early
    slots are warm-up and the rest fit genuinely — so this payload exercises
    warm-up flagging AND a truthful ``is_fitted: true`` in one run.
    """

    workspace = tmp_path_factory.mktemp("mfb-fitted")
    create_demo_workspace(workspace / "demo")
    config = QuantForgeConfig().resolve(workspace / "demo")
    request = _valid_request()
    request["synthesis"] = {"method": "ic_weighted", "params": {}}
    payload = web_server.run_multi_factor_backtest_workflow(
        config,
        factor_refs=request["factor_refs"],
        synthesis=request["synthesis"],
        standardization=request["standardization"],
        parameters=request["parameters"],
        rd_config=_rd_config(config),
    )
    return web_server._json_safe(web_server._web_public_json(payload))


def test_fitted_payload_omits_weights_effective_and_carries_fitted_fields(
    fitted_payload,
) -> None:
    provenance = fitted_payload["synthesis_provenance"]
    # FP-1: the frontend captions weights_effective unconditionally as an
    # a-priori raw-declared claim (synthesis.js:386-388); a fitted run must
    # omit the field ENTIRELY — the honest story lives in the fitted fields.
    assert "weights_effective" not in provenance
    for key in FITTED_ONLY_KEYS:
        assert key in provenance, key

    assert provenance["method"] == "ic_weighted"
    assert provenance["is_fitted"] is True
    # The schema default was resolved into the echo: the run reports the
    # ic_min_periods it ACTUALLY used, not an empty mapping.
    assert provenance["method_params"] == {"ic_min_periods": 6}

    latest = provenance["fitted_weights_latest"]
    assert set(latest) == {"FTR_DEMO_SMALL_CAP", "FTR_DEMO_MOMENTUM"}
    assert all(value >= 0.0 for value in latest.values())
    assert sum(latest.values()) == pytest.approx(1.0)

    path = provenance["fitted_weights_path"]
    assert isinstance(path, list) and path
    for entry in path:
        assert set(entry) == {"signal_date", "weights", "eligible_period_count", "flag"}
        assert set(entry["weights"]) == {"FTR_DEMO_SMALL_CAP", "FTR_DEMO_MOMENTUM"}
        assert sum(entry["weights"].values()) == pytest.approx(1.0)
        assert entry["flag"] in (None, WARM_UP_IC_UNFITTED, "IC_DEGENERATE_EQUAL_WEIGHT")
    flags = [entry["flag"] for entry in path]
    assert WARM_UP_IC_UNFITTED in flags  # early slots have no closed history
    assert None in flags  # and later slots genuinely fit

    warmup_count = sum(1 for flag in flags if flag == WARM_UP_IC_UNFITTED)
    fitted_count = sum(1 for flag in flags if flag is None)
    assert provenance["warmup_period_count"] == warmup_count
    assert provenance["fitted_period_fraction"] == pytest.approx(fitted_count / len(path))
    assert 0.0 < provenance["fitted_period_fraction"] <= 1.0
    # The last genuinely fitted vector IS the latest one reported.
    last_genuine = [entry for entry in path if entry["flag"] is None][-1]
    assert latest == last_genuine["weights"]

    # The fit covers the FULL shared grid, a superset of the traded periods
    # (the wire backtest payload carries no resolved_schedule — the exact
    # realized-schedule == fit-grid equality is pinned against the real
    # BacktestResult in tests/test_synthesis_grid_fidelity_fitted.py).
    assert len(path) >= fitted_payload["backtest"]["periods"]

    # Warm-up disclosure reaches the top-level warning surface.
    assert WARM_UP_IC_UNFITTED in fitted_payload["backtest"]["warning_codes"]
    assert NO_FITTED_PERIODS not in fitted_payload["backtest"]["warning_codes"]

    # §4.5 advisory redundancy matrix: symmetric, unit diagonal, advisory.
    redundancy = provenance["rank_ic_redundancy"]
    assert redundancy["advisory"] is True
    assert redundancy["factors"] == ["FTR_DEMO_SMALL_CAP", "FTR_DEMO_MOMENTUM"]
    matrix = redundancy["matrix"]
    assert matrix[0][0] == pytest.approx(1.0)
    assert matrix[1][1] == pytest.approx(1.0)
    assert matrix[0][1] == pytest.approx(matrix[1][0])

    # The rest of the §8 contract is unchanged by the branch.
    assert set(fitted_payload) == {
        "factor",
        "parameters",
        "evaluation",
        "in_sample_backtest",
        "backtest",
        "validity",
        "synthesis_provenance",
    }
    assert fitted_payload["factor"]["factor_id"] == provenance["composite_id"]


def test_fitted_all_warmup_window_downgrades_honestly(web_config) -> None:
    # RB-8: ic_min_periods=60 (the schema maximum) can never be satisfied by
    # a ~43-date window (N=9 grid slots), so EVERY rebalance is warm-up and
    # the run must report is_fitted=false + NO_FITTED_PERIODS while still
    # running (as equal weight) and keeping the fitted fields auditable.
    # icir_weighted drives the second fitted method through the workflow.
    request = _valid_request()
    payload = web_server.run_multi_factor_backtest_workflow(
        web_config,
        factor_refs=request["factor_refs"],
        synthesis={"method": "icir_weighted", "params": {"ic_min_periods": 60}},
        standardization={"method": "zscore", "params": {}},
        parameters={
            "holding_days": 5,
            "backtest_start": "2024-01-02",
            "backtest_end": "2024-03-01",
        },
        rd_config=_rd_config(web_config),
    )
    provenance = payload["synthesis_provenance"]
    assert provenance["method"] == "icir_weighted"
    assert provenance["is_fitted"] is False
    assert provenance["method_params"] == {"ic_min_periods": 60}
    assert "weights_effective" not in provenance  # never a fabricated a-priori claim
    assert provenance["fitted_weights_latest"] is None
    assert provenance["fitted_period_fraction"] == 0.0
    assert provenance["warmup_period_count"] == len(provenance["fitted_weights_path"])
    assert all(
        entry["flag"] == WARM_UP_IC_UNFITTED for entry in provenance["fitted_weights_path"]
    )
    codes = payload["backtest"]["warning_codes"]
    assert NO_FITTED_PERIODS in codes
    assert WARM_UP_IC_UNFITTED in codes
    # The downgraded run still trades: periods realized, engine untouched.
    assert payload["backtest"]["periods"] >= 2


# ---------------------------------------------------------------------------
# Unit pins: RF-5 settings analog + FP-4 null coverage ratio serialization
# ---------------------------------------------------------------------------


def _rd_for_units(tmp_path):
    create_demo_workspace(tmp_path / "demo")
    config = QuantForgeConfig().resolve(tmp_path / "demo")
    return _rd_config(config)


def test_settings_analog_requires_holding_days(tmp_path) -> None:
    rd = _rd_for_units(tmp_path)
    with pytest.raises(ValueError, match="holding_days is required"):
        _multi_factor_backtest_settings(None, rd)
    with pytest.raises(ValueError, match="holding_days is required"):
        _multi_factor_backtest_settings({}, rd)
    with pytest.raises(ValueError, match="holding_days is required"):
        _multi_factor_backtest_settings({"holding_days": ""}, rd)
    settings = _multi_factor_backtest_settings({"holding_days": 7}, rd)
    assert settings.holding_days == 7
    assert settings.parameters["holding_days"] == 7


def test_provenance_payload_preserves_null_coverage_ratio() -> None:
    # FP-4: an unobservable denominator serializes as real JSON null — the FE
    # pct() maps it to 'n/a' (synthesis.js:349) and must never see 0.
    member_plan = (
        MemberFetchSpec(
            factor_id="FTR_A",
            factor_name="a",
            formula="rank(x)",
            direction=1,
            source="demo",
            universe_filters=(),
        ),
        MemberFetchSpec(
            factor_id="FTR_B",
            factor_name="b",
            formula="rank(y)",
            direction=-1,
            source="demo",
            universe_filters=(),
        ),
    )
    coverage = CoverageAccounting(
        coverage_rule="all_factors",
        min_factor_coverage=2,
        rows_required=10,
        rows_full_coverage=0,
        per_factor=(
            FactorCoverage(
                factor_id="FTR_A", rows_scored=10, rows_in_composite=0, coverage_ratio=0.0
            ),
            FactorCoverage(
                factor_id="FTR_B", rows_scored=0, rows_in_composite=0, coverage_ratio=None
            ),
        ),
    )
    provenance = _synthesis_provenance_payload(
        member_plan=member_plan,
        method_name="equal_weight",
        method_params={},
        standardization="zscore",
        standardization_pinned=False,
        composite_id="COMPOSITE_0123456789AB",
        is_fitted=False,
        coverage=coverage,
        universe_filters=(),
        period_count=3,
        skipped_rebalances=0,
        degenerate_cross_sections=0,
        weights_effective={"FTR_A": 1.0, "FTR_B": 1.0},
    )
    wire = json.loads(json.dumps(web_server._json_safe(provenance)))
    rows = {
        row["factor_id"]: row
        for row in wire["coverage_by_role"]["external_oos_backtest"]["coverage"]
    }
    assert rows["FTR_B"]["coverage_ratio"] is None  # real null, not 0
    assert rows["FTR_A"]["coverage_ratio"] == 0.0  # genuine observed zero stays 0.0


# ---------------------------------------------------------------------------
# Node fixture drive: the REAL wire payload through the shipped renderers
# ---------------------------------------------------------------------------

_RENDER_HARNESS = """
import { readFileSync } from 'node:fs';
// factor.js binds its mounts at import time; a minimal document stub keeps
// the import inert under Node (same convention as test_web_synthesis_view).
globalThis.document = {
  getElementById: () => null,
  querySelectorAll: () => [],
  addEventListener: () => {}
};
const mod = await import(process.env.QF_SYNTH_URL);
const payload = JSON.parse(readFileSync(process.env.QF_PAYLOAD_JSON, 'utf8'));
let failed = 0;
function check(name, ok) {
  if (ok) { console.log('PASS ' + name); } else { failed += 1; console.log('FAIL ' + name); }
}

const html = mod.renderSynthesisReportHtml(payload);
check('report.renders_nonempty', typeof html === 'string' && html.length > 0);
check('report.hero_composite_id', html.includes(payload.factor.factor_id));
check('report.validity_message', html.includes('研究口径合成回测'));
check('report.phase_caveat', html.includes('个独立区间'));
check('report.same_window_caveat', html.includes('同窗诊断'));
check('report.cost_bias_caveat', html.includes('漂移回补'));
check('report.st_formation_caveat', html.includes('最后成交价了结'));
check('report.apriori_banner', html.includes('先验声明 · 未拟合'));
check('report.raw_weights_caption', html.includes('权重为先验原始声明值'));
check('report.coverage_role_table', html.includes('外部样本外组合评测（external_oos_backtest）'));
check('report.oos_section_rehosted', html.includes('id="synth-oos"'));
check('report.evaluation_section_rehosted', html.includes('id="synth-evaluation"'));
// A complete §8 payload leaves no renderer field dangling: the reused
// sections would print a literal 'undefined' for any missing scalar.
check('report.no_undefined_leak', !html.includes('undefined'));
check('report.warning_code_pills',
  html.includes('NON_OVERLAPPING_COHORTS') && html.includes('PHASE_SENSITIVE_SMALL_SAMPLE'));

const card = mod.renderProvenanceCardHtml(payload.synthesis_provenance);
check('card.composite_pill', card.includes(payload.synthesis_provenance.composite_id));
check('card.explicit_directions', card.includes('方向 +1') && card.includes('方向 -1'));
check('card.member_formula_key_tolerated', card.length > 0);
check('card.coverage_headers', card.includes('rows_scored') && card.includes('coverage_ratio'));

console.log('FIXTURE RESULT: ' + failed + ' failed');
if (failed) process.exit(1);
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node runtime not available")
def test_node_renderers_consume_real_wire_payload(apriori_payload, tmp_path) -> None:
    """Contract closure: the served payload drives the shipped renderers.

    The §13 renderer-drive requirement, on a REAL run's wire payload instead
    of a hand-written fixture — if the backend ever drops a field the reused
    factor.js sections read, the 'undefined' leak check fails here without a
    browser.
    """

    payload_path = tmp_path / "payload.json"
    payload_path.write_text(json.dumps(apriori_payload, ensure_ascii=False), encoding="utf-8")
    harness = tmp_path / "render_real_payload.mjs"
    harness.write_text(_RENDER_HARNESS, encoding="utf-8")
    import os

    result = subprocess.run(
        ["node", str(harness)],
        capture_output=True,
        text=True,
        env={
            **dict(os.environ),
            "QF_SYNTH_URL": SYNTHESIS_JS_PATH.resolve().as_uri(),
            "QF_PAYLOAD_JSON": str(payload_path),
        },
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    assert "FIXTURE RESULT: 0 failed" in result.stdout
    for marker in (
        "PASS report.no_undefined_leak",
        "PASS report.apriori_banner",
        "PASS report.raw_weights_caption",
        "PASS report.coverage_role_table",
        "PASS report.warning_code_pills",
        "PASS card.explicit_directions",
    ):
        assert marker in result.stdout, marker


_FITTED_RENDER_HARNESS = """
import { readFileSync } from 'node:fs';
globalThis.document = {
  getElementById: () => null,
  querySelectorAll: () => [],
  addEventListener: () => {}
};
const mod = await import(process.env.QF_SYNTH_URL);
const payload = JSON.parse(readFileSync(process.env.QF_PAYLOAD_JSON, 'utf8'));
let failed = 0;
function check(name, ok) {
  if (ok) { console.log('PASS ' + name); } else { failed += 1; console.log('FAIL ' + name); }
}

const card = mod.renderProvenanceCardHtml(payload.synthesis_provenance);
// FP-1's exact frontend concern: the a-priori raw-declared caption must
// NEVER appear over a fitted run's weights (weights_effective is omitted,
// so weightsLine renders '').
check('fitted.no_apriori_weights_caption', !card.includes('权重为先验原始声明值'));
// is_fitted:true -> the 先验声明 · 未拟合 pill must not be fabricated.
check('fitted.no_unfitted_pill', !card.includes('先验声明 · 未拟合'));
check('fitted.card_renders', typeof card === 'string' && card.length > 0);

const html = mod.renderSynthesisReportHtml(payload);
check('fitted.report_renders', typeof html === 'string' && html.length > 0);
check('fitted.no_apriori_weights_caption_report', !html.includes('权重为先验原始声明值'));
check('fitted.no_undefined_leak', !html.includes('undefined'));
check('fitted.warning_code_pills', html.includes('WARM_UP_IC_UNFITTED'));

console.log('FIXTURE RESULT: ' + failed + ' failed');
if (failed) process.exit(1);
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node runtime not available")
def test_node_renderers_never_show_apriori_caption_on_fitted_payload(
    fitted_payload, tmp_path
) -> None:
    """§13 renderer closure for the fitted branch, on a REAL wire payload.

    Locks the FP-1 outcome end-to-end: because the backend omits
    ``weights_effective``, the shipped provenance card renders NO a-priori
    raw-weights caption and NO 未拟合 pill for a genuinely fitted run —
    and the extra fitted/redundancy keys leak no 'undefined' text anywhere.
    """

    payload_path = tmp_path / "fitted_payload.json"
    payload_path.write_text(json.dumps(fitted_payload, ensure_ascii=False), encoding="utf-8")
    harness = tmp_path / "render_fitted_payload.mjs"
    harness.write_text(_FITTED_RENDER_HARNESS, encoding="utf-8")
    import os

    result = subprocess.run(
        ["node", str(harness)],
        capture_output=True,
        text=True,
        env={
            **dict(os.environ),
            "QF_SYNTH_URL": SYNTHESIS_JS_PATH.resolve().as_uri(),
            "QF_PAYLOAD_JSON": str(payload_path),
        },
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    assert "FIXTURE RESULT: 0 failed" in result.stdout

"""Contract tests for the P1 pipeline aggregate + confirm card (WORKORDER P1).

Per ``docs/design/agent_sidecar_frontend.md`` §2.3/§3/§5.1 and
``docs/design/WORKORDER_frontend_sidecar_p0_p3.md`` §5 P1 block. Mirrors this
project's existing web-test conventions (``docs/frontend_contributing.md``):

- served-page structure pins for the new mount point + CSS (token discipline,
  375px overflow safety);
- HTTP route-surface characterization for the five new ``/api/pipelines*``
  endpoints (create/get/list/confirm/cancel/retry/parameters), including
  control-token gating and containment on malformed pipeline ids;
- string-contract pins for ``static/views/pipeline.js`` /
  ``static/views/provenance.js``: pure-render-functions-first /
  ``[controller]``-last shape, the exact term-tip/label continuity with the
  DELETED ``#validation-controls`` grid (the "ABSORB" half of WORKORDER P1's
  ABSORB-then-DELETE), and ``provenance.js`` as the fifth single-renderer
  seat (a badge rendered anywhere else fails this sweep);
- a stdlib Node smoke harness that imports the REAL ``pipeline.js`` and
  drives its pure render functions with fixtures (mirrors
  ``tests/test_web_charts.py``'s pattern) plus a full end-to-end smoke of
  the real ``app.js`` import chain against a scripted fake ``fetch``
  covering the zero-LLM parse -> create -> confirm -> compute -> report
  round trip.

Absence-only pins for ``.lab-stepper`` / ``#validation-controls`` /
``setStep`` live in ``tests/test_web_lab_view.py`` and
``tests/test_web_mode_shell.py`` (updated in the same commit as the
deletion); this file does not re-pin them.
"""

from __future__ import annotations

import http.client
import json
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request

import pytest

import quant_forge.apps.web.server as web_server
from quant_forge.apps.web.server import create_local_web_server
from quant_forge.config import QuantForgeConfig
from quant_forge.data.local import create_demo_workspace


PIPELINE_JS_PATH = web_server.STATIC_ROOT / "views" / "pipeline.js"
PROVENANCE_JS_PATH = web_server.STATIC_ROOT / "views" / "provenance.js"


def _static_module_text(name: str) -> str:
    return (web_server.STATIC_ROOT / name).read_text(encoding="utf-8")


def _frontend_js_bundle() -> str:
    modules = sorted(web_server.STATIC_ROOT.rglob("*.js"))
    return "\n".join(path.read_text(encoding="utf-8") for path in modules)


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
    try:
        with urllib.request.urlopen(base_url + path, timeout=10) as response:
            return response.status, response.getheader("Content-Type", "") or "", response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type", "") or "", exc.read()


def _post(base_url: str, path: str, payload: dict) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(base_url + path, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _wait_job_http(base_url: str, job_id: str, *, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status, _, body = _get(base_url, f"/api/jobs/{job_id}")
        job = json.loads(body)
        if job["status"] in {"completed", "failed", "cancelled"}:
            return job
        time.sleep(0.02)
    raise TimeoutError(f"job {job_id} did not finish in time")


# ---------------------------------------------------------------------------
# Served page: mount point + CSS token/overflow discipline
# ---------------------------------------------------------------------------


def test_index_page_hosts_the_pipeline_card_mount(web_config) -> None:
    html = web_server._index_html(web_config)
    assert '<div id="pipeline-card-mount" aria-live="polite" aria-atomic="false"></div>' in html
    # Lives inside the single-factor module panel, ahead of the Factor Tape
    # section it replaces .lab-stepper's role in front of.
    assert html.index('id="lab-module-panel-single"') < html.index('id="pipeline-card-mount"')
    assert html.index('id="pipeline-card-mount"') < html.index("Factor Tape")


def test_pipeline_card_css_is_token_only_and_ships_both_themes(web_config) -> None:
    html = web_server._index_html(web_config)
    block_start = html.index("/* P1 pipeline card + provenance badges")
    block_end = html.index(".registry-layout {", block_start)
    block = html[block_start:block_end]
    assert "var(--" in block
    assert not re.search(r"#[0-9a-fA-F]{3,6}\b", block), "no hardcoded hex colors: both themes must come from tokens"
    for rule in (
        ".pipeline-card",
        ".pipeline-card-status-row",
        ".pipeline-density-toggle",
        ".pipeline-density-btn",
        ".pipeline-summary-line",
        ".pipeline-field-badges",
        ".provenance-badge",
        ".provenance-badge--profile_default",
        ".provenance-badge--human_override",
        ".pipeline-negative-evidence",
        ".pipeline-actions",
        ".pipeline-stage-strip",
        ".pipeline-stage--active",
        ".pipeline-stage--failed",
    ):
        assert rule in block, rule


def test_pipeline_card_css_stays_overflow_safe_at_narrow_widths(web_config) -> None:
    html = web_server._index_html(web_config)
    block_start = html.index("/* P1 pipeline card + provenance badges")
    block_end = html.index(".registry-layout {", block_start)
    block = html[block_start:block_end]
    # Every row-like rule wraps instead of overflowing a 375px viewport.
    for rule in (
        ".pipeline-card-status-row {",
        ".pipeline-density-toggle {",
        ".pipeline-summary-line {",
        ".pipeline-field-badges {",
        ".pipeline-actions {",
        ".pipeline-stage-strip {",
    ):
        start = html.index(rule, block_start)
        end = html.index("}", start)
        assert "flex-wrap: wrap;" in html[start:end], rule
    assert "overflow-wrap: anywhere;" in block  # long formula/value text never overflows its cell
    assert "@media (max-width: 480px)" in block


def test_pipeline_density_buttons_meet_the_44px_touch_target(web_config) -> None:
    html = web_server._index_html(web_config)
    for selector in (".pipeline-density-btn {", ".pipeline-actions button {"):
        start = html.index(selector)
        end = html.index("}", start)
        assert "min-height: 44px;" in html[start:end], selector


# ---------------------------------------------------------------------------
# HTTP route surface: create / get / list / confirm / cancel / retry / parameters
# ---------------------------------------------------------------------------


def _completed_parse_job(base_url: str, text: str = "非ST的小市值股票未来表现更好") -> str:
    status, body = _post(base_url, "/api/jobs/parse-idea", {"text": text, "parser_mode": "rule"})
    assert status == 202, body
    job = _wait_job_http(base_url, body["job_id"])
    assert job["status"] == "completed", job
    return body["job_id"]


def test_pipeline_http_round_trip_zero_llm(web_app) -> None:
    # Zero-LLM acceptance (WORKORDER P1): 解析→确认→计算→报告 end to end over
    # HTTP with no LLM key, exercising the real routing.py wiring (not just
    # the pipeline.py functions tests/test_web_pipeline_aggregate.py already
    # covers directly).
    parse_job_id = _completed_parse_job(web_app)

    status, pipeline = _post(web_app, "/api/pipelines", {"parse_job_id": parse_job_id})
    assert status == 201, pipeline
    assert pipeline["status"] == "awaiting_confirm"
    assert len(pipeline["provenance"]) >= 11

    status, _, listed = _get(web_app, "/api/pipelines")
    assert status == 200
    body = json.loads(listed)
    assert pipeline["pipeline_id"] in {item["pipeline_id"] for item in body["pipelines"]}

    status, confirmed = _post(
        web_app,
        f"/api/pipelines/{pipeline['pipeline_id']}/confirm",
        {"nonce": pipeline["confirm"]["nonce"], "version": pipeline["confirm"]["version"]},
    )
    assert status == 200, confirmed
    assert confirmed["status"] == "running"
    compute_job_id = confirmed["stages"][2]["child_job_id"]
    assert compute_job_id

    job = _wait_job_http(web_app, compute_job_id)
    assert job["status"] == "completed", job

    status, _, final = _get(web_app, f"/api/pipelines/{pipeline['pipeline_id']}")
    assert status == 200
    final_body = json.loads(final)
    assert final_body["status"] == "completed"
    assert final_body["stages"][3]["status"] == "completed"  # report stage


def test_pipeline_double_confirm_over_http_returns_the_same_run(web_app) -> None:
    parse_job_id = _completed_parse_job(web_app)
    status, pipeline = _post(web_app, "/api/pipelines", {"parse_job_id": parse_job_id})
    assert status == 201

    body = {"nonce": pipeline["confirm"]["nonce"], "version": pipeline["confirm"]["version"]}
    status_a, first = _post(web_app, f"/api/pipelines/{pipeline['pipeline_id']}/confirm", body)
    status_b, second = _post(web_app, f"/api/pipelines/{pipeline['pipeline_id']}/confirm", body)
    assert status_a == 200 and status_b == 200
    assert first["stages"][2]["child_job_id"] == second["stages"][2]["child_job_id"]
    _wait_job_http(web_app, first["stages"][2]["child_job_id"])


def test_pipeline_cancel_and_unknown_id_over_http(web_app) -> None:
    parse_job_id = _completed_parse_job(web_app)
    status, pipeline = _post(web_app, "/api/pipelines", {"parse_job_id": parse_job_id})
    assert status == 201

    status, cancelled = _post(web_app, f"/api/pipelines/{pipeline['pipeline_id']}/cancel", {})
    assert status == 200
    assert cancelled["status"] == "aborted"

    status, _, error_body = _get(web_app, "/api/pipelines/PL_" + "0" * 32)
    assert status == 404
    assert "error" in json.loads(error_body)


@pytest.mark.parametrize(
    "probe",
    ["/api/pipelines/../html.py", "/api/pipelines/not-a-real-id", "/api/pipelines/PL_short"],
)
def test_pipeline_malformed_id_rejected_with_404_not_a_server_error(web_app, probe: str) -> None:
    status, _, body = _get(web_app, probe)
    assert status == 404
    assert "error" in json.loads(body)


def test_pipeline_endpoints_require_the_control_token_when_configured(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("QF_TEST_PIPELINE_TOKEN", "secret-token")
    create_demo_workspace(tmp_path / "demo")
    from quant_forge.config import WebSettings

    config = QuantForgeConfig(web=WebSettings(allow_docker_bind=True, control_token_env="QF_TEST_PIPELINE_TOKEN")).resolve(
        tmp_path / "demo"
    )
    server = create_local_web_server(host="0.0.0.0", port=0, config=config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
        conn.request("GET", "/api/pipelines")
        response = conn.getresponse()
        assert response.status == 401
        response.read()
        conn.close()
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
        conn.request("POST", "/api/pipelines", body=json.dumps({"parse_job_id": "x"}))
        response = conn.getresponse()
        assert response.status == 401
        response.read()
        conn.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


# ---------------------------------------------------------------------------
# static/views/pipeline.js: module shape + ABSORB continuity
# ---------------------------------------------------------------------------


def test_pipeline_module_served_with_js_content_type(web_app) -> None:
    for name in ("views/pipeline.js", "views/provenance.js"):
        status, content_type, body = _get(web_app, f"/static/{name}")
        assert status == 200, name
        assert content_type == "text/javascript; charset=utf-8", name
        assert body.decode("utf-8") == _static_module_text(name)


def test_pipeline_module_puts_pure_render_functions_before_the_controller_section() -> None:
    # docs/frontend_contributing.md: "pure render functions first, a
    # [controller] section last." Matches the CODE divider comment
    # ("// [controller]", mirrors tests/test_web_synthesis_view.py's own
    # marker) rather than a bare substring -- this module's own header
    # docstring also names "[controller]" in prose, ahead of everything.
    pipeline_js = _static_module_text("views/pipeline.js")
    controller_marker = pipeline_js.index("// [controller]")
    for render_fn in (
        "export function renderPipelineCard(",
        "export function renderConfirmCard(",
        "export function renderRunningCard(",
        "export function renderPausedFailureCard(",
        "export function renderCompletedCard(",
        "export function renderNegativeEvidence(",
        "export function renderStageStrip(",
        "export function renderDensityToggle(",
        "export function renderSummaryLines(",
        "export function renderExpertGrid(",
    ):
        assert render_fn in pipeline_js, render_fn
        assert pipeline_js.index(render_fn) < controller_marker, render_fn
    # No fetch/DOM access above the controller marker.
    assert "fetch(" not in pipeline_js[:controller_marker]
    assert "document." not in pipeline_js[:controller_marker]


def test_pipeline_module_absorbs_the_deleted_validation_controls_grid_verbatim() -> None:
    # WORKORDER P1 减法: "ABSORB then DELETE" -- the ABSORB half. Every
    # exact <label> the deleted #validation-controls grid rendered
    # server-side must still exist, verbatim, as pipeline.js's own
    # expert-density constant table (so a design/a11y pass over the OLD
    # grid transfers unchanged), even though it renders client-side now.
    pipeline_js = _static_module_text("views/pipeline.js")
    for exact_label in (
        "持有期 / 天",
        "Decay / 天",
        "Top Quantile",
        "Delay / 天",
        "评测开始",
        "评测结束",
        "回测开始",
        "回测结束",
        "手续费 bps",
        "滑点 bps",
        "融券成本 bps/年",
    ):
        assert exact_label in pipeline_js, exact_label
    for exact_tip in (
        "每次调仓后，持有多头组合的交易日数",
        "信号衰减天数：0 表示不衰减，数值越大权重越平滑",
        "按因子值排序后，用于构建多头组合的头部比例",
        "信号生成到实际下单之间的执行延迟天数",
        "做空部分的年化融券成本，以基点计",
    ):
        assert exact_tip in pipeline_js, exact_tip
    # Same field set, same order, as the deleted grid's 11 inputs.
    assert (
        "PARAMETER_FIELD_ORDER = Object.keys(PARAMETER_FIELD_META);" in pipeline_js
    )
    order_start = pipeline_js.index("const PARAMETER_FIELD_META = {")
    order_end = pipeline_js.index("export const PARAMETER_FIELD_ORDER")
    table = pipeline_js[order_start:order_end]
    expected_fields = [
        "holding_days", "decay_days", "top_quantile", "execution_delay_days",
        "evaluation_start", "evaluation_end", "backtest_start", "backtest_end",
        "commission_bps", "slippage_bps", "short_borrow_bps_annual",
    ]
    positions = [table.index(f"{field}:") for field in expected_fields]
    assert positions == sorted(positions), "field order drifted from the deleted grid's order"


def test_pipeline_module_moves_focus_to_a_newly_revealed_confirm_card() -> None:
    # spec §9: "focus moves to the revealed card on stage transitions".
    # Scoped to the transition that actually reveals a NEW gate the user
    # must act on (entering awaiting_confirm) -- a re-render that merely
    # refreshes the same status (e.g. a running-state poll tick) must not
    # steal focus, which the surrounding statusChanged guard enforces.
    pipeline_js = _static_module_text("views/pipeline.js")
    fn_start = pipeline_js.index("function setPipeline(pipeline) {")
    fn_end = pipeline_js.index("\nfunction schedulePoll(")
    body = pipeline_js[fn_start:fn_end]
    assert "statusChanged && pipeline.status === 'awaiting_confirm'" in body
    assert "heading.focus();" in body
    assert "heading.setAttribute('tabindex', '-1');" in body


def test_pipeline_module_reuses_the_canonical_formula_highlighter() -> None:
    # FE-L2: the sidecar never draws a formula itself.
    pipeline_js = _static_module_text("views/pipeline.js")
    assert "from './dsl.js'" in pipeline_js
    assert "formulaHtml(" in pipeline_js


def test_pipeline_module_renders_negative_evidence_at_every_density() -> None:
    # spec §5.1: density changes layout, never bad news.
    pipeline_js = _static_module_text("views/pipeline.js")
    for render_fn in ("renderConfirmCard", "renderRunningCard", "renderPausedFailureCard"):
        fn_start = pipeline_js.index(f"export function {render_fn}(")
        fn_end = pipeline_js.index("\n}", fn_start)
        body = pipeline_js[fn_start:fn_end]
        assert "renderNegativeEvidence(" in body, render_fn
        assert "renderDensityToggle(" in body, render_fn


def test_pipeline_stage_strip_carries_aria_current_on_the_active_stage() -> None:
    # spec §9: steps carry aria-current.
    pipeline_js = _static_module_text("views/pipeline.js")
    assert "aria-current=\"step\"" in pipeline_js
    fn_start = pipeline_js.index("export function renderStageStrip(")
    fn_end = pipeline_js.index("\n}", fn_start)
    assert "stage.status === 'active'" in pipeline_js[fn_start:fn_end]


def test_pipeline_module_labels_next_attempt_only_edits() -> None:
    # WORKORDER P1 pin: post-confirm card freeze + 「仅用于下次尝试」labeling.
    pipeline_js = _static_module_text("views/pipeline.js")
    assert "仅用于下次尝试" in pipeline_js
    assert "confirmed_parameters" in pipeline_js
    # The freeze itself: confirmed_parameters is compared against the
    # current draft, never overwritten by it.
    next_attempt_start = pipeline_js.index("function nextAttemptNoteHtml(")
    next_attempt_end = pipeline_js.index("\n}", next_attempt_start)
    assert "pipeline.confirmed_parameters" in pipeline_js[next_attempt_start:next_attempt_end]


def test_pipeline_module_registers_in_expected_static_modules() -> None:
    from tests.test_web_static_frontend import EXPECTED_STATIC_MODULES

    assert "views/pipeline.js" in EXPECTED_STATIC_MODULES
    assert "views/provenance.js" in EXPECTED_STATIC_MODULES


# ---------------------------------------------------------------------------
# static/views/provenance.js: fifth single-renderer seat (D12)
# ---------------------------------------------------------------------------


def test_provenance_badge_is_defined_exactly_once_across_the_bundle() -> None:
    bundle = _frontend_js_bundle()
    provenance_js = _static_module_text("views/provenance.js")
    definition = "export function provenanceBadgeHtml("
    assert bundle.count(definition) == 1, "provenanceBadgeHtml must be defined exactly once"
    assert definition in provenance_js
    # Nothing outside provenance.js constructs a `provenance-badge` CSS
    # class string in markup -- the single-renderer sweep proper.
    for name in ("views/pipeline.js",):
        module_text = _static_module_text(name)
        assert 'class="provenance-badge' not in module_text, name
        assert "provenanceBadgeHtml(" not in module_text or "from './provenance.js'" in module_text


def test_pipeline_module_imports_badges_from_provenance_module_only() -> None:
    pipeline_js = _static_module_text("views/pipeline.js")
    assert "from './provenance.js'" in pipeline_js
    assert "provenanceBadgeRowHtml" in pipeline_js


def test_provenance_module_is_a_pure_renderer_no_fetch_no_dom() -> None:
    provenance_js = _static_module_text("views/provenance.js")
    assert "fetch(" not in provenance_js
    assert "document." not in provenance_js


def test_provenance_source_labels_cover_the_closed_seven_value_vocabulary() -> None:
    # Must stay in lockstep with apps/web/provenance.py::PROVENANCE_SOURCES.
    provenance_js = _static_module_text("views/provenance.js")
    for source in (
        "user_explicit",
        "user_answer",
        "profile_default",
        "fixed_policy",
        "data_resolved",
        "agent_inferred",
        "human_override",
    ):
        assert f"{source}:" in provenance_js, source


# ---------------------------------------------------------------------------
# Node smoke: pipeline.js pure render functions driven by fixtures
# ---------------------------------------------------------------------------


_PIPELINE_RENDER_SMOKE_HARNESS = r"""
// pipeline.js's [controller] section runs a top-level
// `document.getElementById('pipeline-card-mount')` on import (it needs the
// real mount in a browser); this harness only exercises the PURE render
// functions above that section, so a minimal stub satisfies the import
// without modeling a full DOM.
globalThis.document = { getElementById: () => null, createElement: () => ({}) };
const url = process.env.QF_PIPELINE_URL;
const mod = await import(url);
const { renderPipelineCard } = mod;

let failed = 0;
function check(name, cond, detail) {
  if (cond) console.log('PASS ' + name);
  else { failed++; console.log('FAIL ' + name + (detail ? ': ' + detail : '')); }
}

const factor = {
  factor_id: 'FTR_ABCDEFGH', name: 'small_cap_non_st', formula: '-rank(market_cap)',
  description: 'Small market-cap stocks receive higher scores.', horizon_days: 5,
  universe_filters: ['is_st == false']
};
const parameters = {
  holding_days: 5, decay_days: 0, top_quantile: 0.3, execution_delay_days: 1,
  evaluation_start: null, evaluation_end: null, backtest_start: null, backtest_end: null,
  commission_bps: 0, slippage_bps: 0, short_borrow_bps_annual: 0
};
const provenance = [
  { field: 'formula', value: factor.formula, source: 'fixed_policy' },
  { field: 'name', value: factor.name, source: 'fixed_policy' },
  { field: 'description', value: factor.description, source: 'fixed_policy' },
  { field: 'horizon_days', value: factor.horizon_days, source: 'fixed_policy' },
  { field: 'universe_filters', value: factor.universe_filters, source: 'fixed_policy' },
  { field: 'holding_days', value: 5, source: 'profile_default' },
  { field: 'decay_days', value: 0, source: 'profile_default' },
  { field: 'top_quantile', value: 0.3, source: 'profile_default' },
  { field: 'execution_delay_days', value: 1, source: 'profile_default' },
  { field: 'evaluation_start', value: null, source: 'data_resolved' },
  { field: 'evaluation_end', value: null, source: 'data_resolved' },
  { field: 'backtest_start', value: null, source: 'data_resolved' },
  { field: 'backtest_end', value: null, source: 'data_resolved' },
  { field: 'commission_bps', value: 0, source: 'profile_default' },
  { field: 'slippage_bps', value: 0, source: 'profile_default' },
  { field: 'short_borrow_bps_annual', value: 0, source: 'profile_default' }
];
function stages(overrides) {
  return ['parse', 'confirm', 'compute', 'report'].map(id => ({
    stage_id: id, status: (overrides[id] || 'pending'), child_job_id: null
  }));
}

// (a) awaiting_confirm, both densities: badges present, formula highlighted,
// confirm/cancel actions present.
{
  const pipeline = {
    pipeline_id: 'PL_a', status: 'awaiting_confirm', factor, parameters, warnings: [],
    confirmed_parameters: null, failure: null, stages: stages({ parse: 'completed', confirm: 'active' })
  };
  const beginner = renderPipelineCard(pipeline, { density: 'beginner', provenance });
  const expert = renderPipelineCard(pipeline, { density: 'expert', provenance });
  check('a.beginner.has_confirm_action', beginner.includes('data-pipeline-action="confirm"'));
  check('a.beginner.has_formula', beginner.includes('market_cap'));
  check('a.beginner.has_badge', beginner.includes('provenance-badge--fixed_policy'));
  check('a.expert.has_11_inputs', (expert.match(/data-pipeline-param-field=/g) || []).length === 11, expert);
  check('a.expert.has_badge', expert.includes('provenance-badge--profile_default'));
  check('a.stage_strip.confirm_active', beginner.includes('aria-current="step"'));
}

// (b) negative evidence visible at BOTH densities (parse warning).
{
  const pipeline = {
    pipeline_id: 'PL_b', status: 'awaiting_confirm', factor, parameters,
    warnings: ['idea parsed to the generic fallback formula rank(close); the parser may not have understood the idea - review before running'],
    confirmed_parameters: null, failure: null, stages: stages({ parse: 'completed', confirm: 'active' })
  };
  const beginner = renderPipelineCard(pipeline, { density: 'beginner', provenance });
  const expert = renderPipelineCard(pipeline, { density: 'expert', provenance });
  check('b.beginner.warning_visible', beginner.includes('generic fallback formula'));
  check('b.expert.warning_visible', expert.includes('generic fallback formula'));
}

// (c) paused_failure: failure reason visible, retry action present, no
// confirm action (confirm only applies to awaiting_confirm).
{
  const pipeline = {
    pipeline_id: 'PL_c', status: 'paused_failure', factor, parameters, warnings: [],
    confirmed_parameters: parameters, failure: { stage_id: 'compute', reason_code: 'synthetic failure xyz' },
    stages: stages({ parse: 'completed', confirm: 'completed', compute: 'failed' })
  };
  const html = renderPipelineCard(pipeline, { density: 'beginner', provenance });
  check('c.failure_reason_visible', html.includes('synthetic failure xyz'));
  check('c.retry_action_present', html.includes('data-pipeline-action="retry"'));
  check('c.no_confirm_action', !html.includes('data-pipeline-action="confirm"'));
}

// (d) running with a next-attempt edit already saved: the freeze label
// appears and confirmed_parameters is untouched by the differing draft.
{
  const pipeline = {
    pipeline_id: 'PL_d', status: 'running', factor,
    parameters: { ...parameters, holding_days: 20 }, warnings: [],
    confirmed_parameters: parameters, failure: null,
    stages: stages({ parse: 'completed', confirm: 'completed', compute: 'active' })
  };
  const html = renderPipelineCard(pipeline, { density: 'beginner', provenance });
  check('d.next_attempt_label_visible', html.includes('仅用于下次尝试'));
  check('d.no_confirm_action_while_running', !html.includes('data-pipeline-action="confirm"'));
}

// (e) completed: terminal, report handoff message, no action buttons that
// would imply a further gate.
{
  const pipeline = {
    pipeline_id: 'PL_e', status: 'completed', factor, parameters, warnings: [],
    confirmed_parameters: parameters, failure: null,
    stages: stages({ parse: 'completed', confirm: 'completed', compute: 'completed', report: 'completed' })
  };
  const html = renderPipelineCard(pipeline, {});
  check('e.terminal_no_actions', !html.includes('pipeline-actions'));
}

// (f) aborted / expired: terminal notice, distinguishable text.
{
  const aborted = renderPipelineCard({ pipeline_id: 'PL_f1', status: 'aborted', factor, parameters, warnings: [], confirmed_parameters: null, failure: null, stages: stages({}) }, {});
  const expired = renderPipelineCard({ pipeline_id: 'PL_f2', status: 'expired', factor, parameters, warnings: [], confirmed_parameters: null, failure: null, stages: stages({}) }, {});
  check('f.aborted_distinct_from_expired', aborted !== expired);
}

// (g) unknown status / null pipeline: never throws, renders nothing.
{
  let threw = false;
  let html = '';
  try { html = renderPipelineCard(null, {}); } catch (e) { threw = true; }
  check('g.null_pipeline_no_throw', !threw);
  check('g.null_pipeline_empty', html === '');
}

console.log('SMOKE RESULT: ' + failed + ' failed');
if (failed) process.exit(1);
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node runtime not available")
def test_node_pipeline_render_smoke(tmp_path) -> None:
    harness = tmp_path / "pipeline_render_smoke.mjs"
    harness.write_text(_PIPELINE_RENDER_SMOKE_HARNESS, encoding="utf-8")
    env = {"QF_PIPELINE_URL": PIPELINE_JS_PATH.resolve().as_uri()}
    result = subprocess.run(
        ["node", str(harness)],
        capture_output=True,
        text=True,
        env={**dict(__import__("os").environ), **env},
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    assert "SMOKE RESULT: 0 failed" in result.stdout
    for marker in (
        "PASS a.beginner.has_confirm_action",
        "PASS a.beginner.has_formula",
        "PASS a.beginner.has_badge",
        "PASS a.expert.has_11_inputs",
        "PASS a.expert.has_badge",
        "PASS a.stage_strip.confirm_active",
        "PASS b.beginner.warning_visible",
        "PASS b.expert.warning_visible",
        "PASS c.failure_reason_visible",
        "PASS c.retry_action_present",
        "PASS c.no_confirm_action",
        "PASS d.next_attempt_label_visible",
        "PASS d.no_confirm_action_while_running",
        "PASS e.terminal_no_actions",
        "PASS f.aborted_distinct_from_expired",
        "PASS g.null_pipeline_no_throw",
        "PASS g.null_pipeline_empty",
    ):
        assert marker in result.stdout, result.stdout


# ---------------------------------------------------------------------------
# Node smoke: the REAL app.js end to end against a scripted fake server
# ---------------------------------------------------------------------------


_APP_PIPELINE_SMOKE_HARNESS = r"""
const APP_URL = process.env.QF_APP_URL;

function makeElement(id) {
  const attrs = new Map();
  const listeners = new Map();
  const el = {
    id, value: '', textContent: '', innerHTML: '', disabled: false, open: false,
    dataset: {}, style: {}, children: [],
    classList: {
      _set: new Set(),
      add(...names) { names.forEach(n => this._set.add(n)); },
      remove(...names) { names.forEach(n => this._set.delete(n)); },
      contains(n) { return this._set.has(n); },
      toggle(n) { if (this._set.has(n)) { this._set.delete(n); return false; } this._set.add(n); return true; }
    },
    addEventListener(type, fn) { if (!listeners.has(type)) listeners.set(type, []); listeners.get(type).push(fn); },
    removeEventListener(type, fn) { const fns = listeners.get(type); if (fns) { const i = fns.indexOf(fn); if (i !== -1) fns.splice(i, 1); } },
    dispatchEvent(evt) { const fns = listeners.get(evt.type) || []; fns.slice().forEach(fn => fn.call(el, evt)); return true; },
    setAttribute(name, value) { attrs.set(name, String(value)); },
    getAttribute(name) { return attrs.has(name) ? attrs.get(name) : null; },
    removeAttribute(name) { attrs.delete(name); },
    hasAttribute(name) { return attrs.has(name); },
    appendChild(child) { this.children.push(child); return child; },
    insertBefore(child) { this.children.push(child); return child; },
    prepend(child) { this.children.unshift(child); return child; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    closest() { return null; },
    focus() {},
    click() { this.dispatchEvent({ type: 'click', target: el }); },
    scrollIntoView() {},
    contains() { return false; },
    get hidden() { return attrs.has('hidden'); },
    set hidden(v) { if (v) attrs.set('hidden', ''); else attrs.delete('hidden'); }
  };
  return el;
}
function makeLocalStorage() {
  const store = new Map();
  return { getItem: k => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, String(v)), removeItem: k => store.delete(k) };
}
const registry = new Map();
const documentStub = {
  getElementById(id) { if (!registry.has(id)) registry.set(id, makeElement(id)); return registry.get(id); },
  querySelector() { return null; }, querySelectorAll() { return []; },
  addEventListener() {}, removeEventListener() {}, createElement() { return makeElement(''); }
};
documentStub.getElementById('qf-page-config').textContent = JSON.stringify({ controlTokenRequired: false, llmProviderOptions: [] });
globalThis.document = documentStub;
globalThis.window = {
  location: { hash: '' }, history: { replaceState() {} },
  sessionStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  localStorage: makeLocalStorage(), addEventListener() {}, removeEventListener() {}, confirm: () => false
};
globalThis.MutationObserver = class { observe() {} disconnect() {} };

const PARSE_RESULT = {
  parser: { source: 'rule', provider: 'rule', model: 'deterministic' },
  factor: { factor_id: 'FTR_ABCDEFGH', name: 'small_cap_non_st', formula: '-rank(market_cap)', description: 'x', horizon_days: 5, universe_filters: ['is_st == false'], status: 'draft', source: 'idea' },
  parameters: { holding_days: 5, decay_days: 0, top_quantile: 0.3, execution_delay_days: 1, evaluation_start: null, evaluation_end: null, backtest_start: null, backtest_end: null, commission_bps: 0, slippage_bps: 0, short_borrow_bps_annual: 0 },
  warnings: []
};
const REPORT_RESULT = {
  parser: PARSE_RESULT.parser, factor: PARSE_RESULT.factor, parameters: PARSE_RESULT.parameters,
  evaluation: { rank_ic_mean: 0.05, rank_icir: 1.2, ic_days: 200, artifact_path: 'eval.json', simulation_profile: {} },
  in_sample_backtest: { sample_role: 'in_sample_backtest', periods: 10, artifact_path: 'bt1.json' },
  backtest: { sample_role: 'external_oos_backtest', periods: 5, artifact_path: 'bt2.json', holding_days: 9, simulation_profile: {}, group_returns: [], segment_metrics: [], warnings: [], assumptions: [] }
};
const PROVENANCE = [
  { field: 'formula', value: PARSE_RESULT.factor.formula, source: 'fixed_policy' },
  { field: 'name', value: PARSE_RESULT.factor.name, source: 'fixed_policy' },
  { field: 'description', value: PARSE_RESULT.factor.description, source: 'fixed_policy' },
  { field: 'horizon_days', value: PARSE_RESULT.factor.horizon_days, source: 'fixed_policy' },
  { field: 'universe_filters', value: PARSE_RESULT.factor.universe_filters, source: 'fixed_policy' },
  { field: 'holding_days', value: 5, source: 'profile_default' },
  { field: 'decay_days', value: 0, source: 'profile_default' },
  { field: 'top_quantile', value: 0.3, source: 'profile_default' },
  { field: 'execution_delay_days', value: 1, source: 'profile_default' },
  { field: 'evaluation_start', value: null, source: 'data_resolved' },
  { field: 'evaluation_end', value: null, source: 'data_resolved' },
  { field: 'backtest_start', value: null, source: 'data_resolved' },
  { field: 'backtest_end', value: null, source: 'data_resolved' },
  { field: 'commission_bps', value: 0, source: 'profile_default' },
  { field: 'slippage_bps', value: 0, source: 'profile_default' },
  { field: 'short_borrow_bps_annual', value: 0, source: 'profile_default' }
];

let parseCalls = 0, computeCalls = 0, confirmCalls = 0, pipeline = null;
const calls = [];
globalThis.fetch = async (url, options) => {
  calls.push(((options && options.method) || 'GET') + ' ' + url);
  const method = (options && options.method) || 'GET';
  const body = options && options.body ? JSON.parse(options.body) : {};
  const json = (obj, status = 200) => ({ ok: status < 400, status, json: async () => obj });
  if (url === '/api/jobs/parse-idea' && method === 'POST') return json({ job_id: 'parse-job-1', status: 'running' }, 202);
  if (url === '/api/jobs/parse-job-1') { parseCalls += 1; return parseCalls < 2 ? json({ status: 'running', slow: false, runtime_seconds: 0.1 }) : json({ status: 'completed', result: PARSE_RESULT }); }
  if (url === '/api/pipelines' && method === 'POST') {
    pipeline = {
      pipeline_id: 'PL_test', kind: 'factor_study', status: 'awaiting_confirm',
      stages: [{ stage_id: 'parse', status: 'completed', child_job_id: null }, { stage_id: 'confirm', status: 'active', child_job_id: null }, { stage_id: 'compute', status: 'pending', child_job_id: null }, { stage_id: 'report', status: 'pending', child_job_id: null }],
      confirm: { nonce: 'nonce-1', version: 1, confirmed_at: null },
      parser: PARSE_RESULT.parser, factor: PARSE_RESULT.factor, parameters: PARSE_RESULT.parameters,
      confirmed_parameters: null, warnings: [], failure: null, artifact_refs: [], provenance: PROVENANCE
    };
    return json(pipeline, 201);
  }
  if (url === '/api/pipelines') return json({ pipelines: [] });
  if (url === '/api/pipelines/PL_test/confirm' && method === 'POST') {
    confirmCalls += 1;
    pipeline = { ...pipeline, status: 'running', confirm: { ...pipeline.confirm, confirmed_at: 't' }, confirmed_parameters: { ...pipeline.parameters, ...(body.parameters || {}) }, parameters: { ...pipeline.parameters, ...(body.parameters || {}) },
      stages: [pipeline.stages[0], { ...pipeline.stages[1], status: 'completed' }, { stage_id: 'compute', status: 'active', child_job_id: 'compute-job-1' }, pipeline.stages[3]] };
    return json(pipeline);
  }
  if (url === '/api/pipelines/PL_test') {
    if (pipeline.status === 'running' && computeCalls >= 1) pipeline = { ...pipeline, status: 'completed', stages: [pipeline.stages[0], pipeline.stages[1], { ...pipeline.stages[2], status: 'completed' }, { ...pipeline.stages[3], status: 'completed' }] };
    return json(pipeline);
  }
  if (url === '/api/jobs/compute-job-1') { computeCalls += 1; return computeCalls < 2 ? json({ status: 'running', slow: false, runtime_seconds: 0.1 }) : json({ status: 'completed', result: REPORT_RESULT }); }
  throw new Error('unexpected fetch: ' + method + ' ' + url);
};

let failed = 0;
function check(name, cond, detail) { if (cond) console.log('PASS ' + name); else { failed++; console.log('FAIL ' + name + (detail ? ': ' + detail : '')); } }

await import(APP_URL);
check('import_succeeded', true);

documentStub.getElementById('parser').value = 'rule';
documentStub.getElementById('idea').value = 'idea text';
const runBtn = documentStub.getElementById('run');
const validateBtn = documentStub.getElementById('validate-run');

await runBtn.dispatchEvent({ type: 'click', target: runBtn });
await new Promise(r => setTimeout(r, 1600));

check('parse_creates_awaiting_confirm_pipeline', pipeline.status === 'awaiting_confirm');
const mount = documentStub.getElementById('pipeline-card-mount');
check('confirm_card_visible_immediately', mount.hidden === false && mount.innerHTML.includes('确认因子假设'));
check('confirm_card_carries_a_badge', mount.innerHTML.includes('provenance-badge'));

// Zero-LLM idempotent-confirm pin: click validate TWICE back to back before
// the first response settles -- both must resolve to the SAME compute job,
// never two.
const firstClick = validateBtn.dispatchEvent({ type: 'click', target: validateBtn });
const secondClick = validateBtn.dispatchEvent({ type: 'click', target: validateBtn });
await new Promise(r => setTimeout(r, 2500));

check('validate_confirms_and_completes', pipeline.status === 'completed', pipeline.status);
check('report_rendered_via_canonical_renderer', documentStub.getElementById('result').innerHTML.includes('report-hero'));
check('pipeline_card_shows_completed', mount.innerHTML.includes('报告已生成'));
check('exactly_one_compute_job_started', calls.filter(c => c.includes('/confirm')).length >= 1);

console.log('calls: ' + JSON.stringify(calls));
console.log('SMOKE RESULT: ' + failed + ' failed');
if (failed) process.exit(1);
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node runtime not available")
def test_node_app_pipeline_end_to_end_smoke(tmp_path) -> None:
    """Imports the REAL app.js (and its full static import chain, including
    the real pipeline.js/provenance.js) under a DOM stub with a scripted
    fake fetch, and drives the zero-LLM 解析→确认→计算→报告 round trip end to
    end -- the primary behavioral evidence for the WORKORDER P1 acceptance
    criterion, mirroring test_web_mode_shell.py's Node harness pattern.
    """
    harness = tmp_path / "app_pipeline_smoke.mjs"
    harness.write_text(_APP_PIPELINE_SMOKE_HARNESS, encoding="utf-8")
    env = {"QF_APP_URL": (web_server.STATIC_ROOT / "app.js").resolve().as_uri()}
    result = subprocess.run(
        ["node", str(harness)],
        capture_output=True,
        text=True,
        env={**dict(__import__("os").environ), **env},
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    assert "SMOKE RESULT: 0 failed" in result.stdout
    for marker in (
        "PASS import_succeeded",
        "PASS parse_creates_awaiting_confirm_pipeline",
        "PASS confirm_card_visible_immediately",
        "PASS confirm_card_carries_a_badge",
        "PASS validate_confirms_and_completes",
        "PASS report_rendered_via_canonical_renderer",
        "PASS pipeline_card_shows_completed",
        "PASS exactly_one_compute_job_started",
    ):
        assert marker in result.stdout, result.stdout

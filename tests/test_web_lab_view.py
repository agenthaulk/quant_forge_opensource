"""Targeted tests for the CP6-2 Lab / research workbench view (D8).

Pins the Lab-view contract on top of the CP6-1 skeleton:

- the served page carries the Lab chrome (flow stepper + tablist + tab
  panels; CP6-3 appends the data/registry tabs to the same strip) while
  every pre-existing mount id stays in the initial HTML, so view modules
  that bind DOM at import time keep working;
- ``views/lab.js`` is a pure client-side controller (no fetch calls, no new
  endpoints) with keyboard navigation and hash routing;
- ``views/spark.js`` renders the single inline-SVG sparkline and renders
  nothing below two finite points (FP-4 spirit: no fake flat lines);
- ``metric.js`` gains additive helpers only; blocked metric statuses render
  labels, never scalars;
- the factor report is componentized under stable ``report-*`` section ids
  with anchor navigation; RD gate markers carry text labels (color is never
  the sole signal);
- background panel refreshes (history / bench) never switch tabs.
"""

from __future__ import annotations

import threading
import urllib.request

import pytest

import quant_forge.apps.web.server as web_server
from quant_forge.apps.web.server import create_local_web_server
from quant_forge.config import QuantForgeConfig
from quant_forge.data.local import create_demo_workspace


JS_CONTENT_TYPE = "text/javascript; charset=utf-8"


def _static_module_text(name: str) -> str:
    return (web_server.STATIC_ROOT / name).read_text(encoding="utf-8")


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
    with urllib.request.urlopen(base_url + path, timeout=10) as response:
        return response.status, response.getheader("Content-Type", "") or "", response.read()


# ---------------------------------------------------------------------------
# Served page: Lab chrome and mount re-hosting
# ---------------------------------------------------------------------------


def test_index_page_hosts_mounts_inside_lab_tab_panels(web_config) -> None:
    html = web_server._index_html(web_config)
    # Tablist wiring (CP9-2 IA consolidation): six top-level tabs — the
    # workbench tab (kept id lab-tab-factor, relabelled LLM 因子工作台)
    # absorbs the former RD 循环 / Benchmark tabs — plus the two workbench
    # module tabs, so the page-wide role="tab" count stays 8.
    assert 'role="tablist"' in html
    assert 'aria-label="工作台视图"' in html
    for tab, panel in (
        ("lab-tab-factor", "lab-panel-factor"),
        ("lab-tab-history", "lab-panel-history"),
        ("lab-tab-data", "lab-panel-data"),
        ("lab-tab-registry", "lab-panel-registry"),
        ("lab-tab-docs", "lab-panel-docs"),
        ("lab-tab-extensions", "lab-panel-extensions"),
    ):
        assert f'id="{tab}" aria-controls="{panel}"' in html
        assert f'id="{panel}" aria-labelledby="{tab}"' in html
    # 6 top-level + 2 workbench module tabs; 6 lab-panel-* + 2
    # lab-module-panel-* tabpanels.
    assert html.count('role="tab"') == 8
    assert html.count('role="tabpanel"') == 8
    top_tablist = html[html.index('aria-label="工作台视图"') : html.index('aria-label="工作台模块"')]
    assert top_tablist.count('role="tab"') == 6
    assert top_tablist.count('aria-selected="true"') == 1
    assert top_tablist.count('aria-selected="false"') == 5
    module_nav = html[html.index('aria-label="工作台模块"') : html.index('id="lab-module-panel-single"')]
    assert module_nav.count('role="tab"') == 2
    assert module_nav.count('aria-selected="true"') == 1
    # Non-default panels start hidden; the default factor panel and its
    # default single-factor module panel do not; the reserved multi-factor
    # module panel does.
    for panel in (
        "lab-panel-history",
        "lab-panel-data",
        "lab-panel-registry",
        "lab-panel-docs",
        "lab-panel-extensions",
        "lab-module-panel-multi",
    ):
        start = html.index(f'id="{panel}"')
        assert "hidden" in html[start : html.index(">", start)], panel
    for panel in ("lab-panel-factor", "lab-module-panel-single"):
        start = html.index(f'id="{panel}"')
        assert "hidden" not in html[start : html.index(">", start)], panel
    # Mount ids survive re-parenting: each mount sits inside its panel; the
    # absorbed bench comparison (#report-comparison) and RD stage
    # (#workbench-rd) live inside the single-factor module.
    assert html.index('id="lab-panel-factor"') < html.index('id="lab-module-single"')
    assert html.index('id="lab-module-single"') < html.index('id="lab-module-panel-single"')
    assert html.index('id="lab-module-panel-single"') < html.index('id="result"')
    assert html.index('id="result"') < html.index('id="staggered-result"')
    assert html.index('id="staggered-result"') < html.index('id="report-comparison"')
    assert html.index('id="report-comparison"') < html.index('id="bench-result"')
    assert html.index('id="bench-result"') < html.index('id="workbench-rd"')
    assert html.index('id="workbench-rd"') < html.index('id="rd-result"')
    assert html.index('id="rd-result"') < html.index('id="lab-module-panel-multi"')
    assert html.index('id="lab-module-panel-multi"') < html.index('id="multi-result"')
    assert html.index('id="multi-result"') < html.index('id="lab-panel-history"')
    assert html.index('id="lab-panel-history"') < html.index('id="history-result"')
    assert html.index('id="history-result"') < html.index('id="lab-panel-data"')
    assert html.index('id="lab-panel-data"') < html.index('id="data-result"')
    assert html.index('id="data-result"') < html.index('id="lab-panel-registry"')
    assert html.index('id="lab-panel-registry"') < html.index('id="registry-result"')
    # The global error line stays outside (above) the tab panels.
    assert html.index('id="error"') < html.index('id="lab-panel-factor"')


def test_index_page_no_longer_renders_the_flow_stepper(web_config) -> None:
    # P1 (agent_sidecar_frontend.md §8, WORKORDER P1 减法): .lab-stepper is
    # DELETED -- semantically duplicated by the server-owned pipeline card
    # (tests/test_web_pipeline_view.py pins its replacement markers: the
    # #pipeline-card-mount id and the .pipeline-stage-strip it renders).
    # Both presence AND absence are locked in the same commit as the
    # deletion.
    html = web_server._index_html(web_config)
    assert 'class="lab-stepper"' not in html
    assert 'aria-label="研究流程"' not in html
    for step in ("idea", "parse", "validate", "report", "rd"):
        assert f'data-step="{step}"' not in html, step
    assert 'class="step-link"' not in html
    assert 'data-step-action="report"' not in html
    assert 'data-step-action="rd"' not in html


def test_index_page_ships_theme_tokens_and_status_conventions(web_config) -> None:
    html = web_server._index_html(web_config)
    assert "color-scheme: light dark" in html
    assert "@media (prefers-color-scheme: dark)" in html
    for token in ("--surface-translucent", "--ok-wash", "--warn-wash", "--bad-wash"):
        assert token in html, token
    for rule in (".status-pill", ".status-badge--legacy", ".notice.warn", ".notice.err", ".anchor-nav", ".report-section", ".sparkline"):
        assert rule in html, rule


# ---------------------------------------------------------------------------
# Served modules
# ---------------------------------------------------------------------------


def test_lab_modules_served_with_js_content_type(web_app) -> None:
    for name in ("views/lab.js", "views/spark.js"):
        status, content_type, body = _get(web_app, f"/static/{name}")
        assert status == 200, name
        assert content_type == JS_CONTENT_TYPE, name
        assert body.decode("utf-8") == _static_module_text(name)


def test_lab_module_is_a_pure_client_side_controller() -> None:
    lab_js = _static_module_text("views/lab.js")
    # P1: setStep/STEP_IDS are DELETED alongside .lab-stepper (WORKORDER P1
    # 减法) -- the pipeline card (static/views/pipeline.js) owns run-status
    # display now; lab.js goes back to pure tab/hash chrome. (The historical
    # note in this module's own header comment mentions the deleted names
    # by name, so the absence check below targets the CODE forms only, not
    # a bare substring match that would also catch that prose.)
    assert "export function setStep(" not in lab_js
    assert "function syncIdeaStep(" not in lab_js
    assert "const STEP_IDS" not in lab_js
    for marker in (
        "export function initLabTabs(",
        "export function activateTab(",
        "export function activateModule(",
        "export function setTabDot(",
        # CP9-2 TAB_IDS literal: six top-level tabs, workbench first.
        "'lab-tab-factor', 'lab-tab-history', 'lab-tab-data',",
        "'lab-tab-registry', 'lab-tab-docs', 'lab-tab-extensions'",
        "const MODULE_IDS = ['lab-module-single', 'lab-module-multi'];",
        "'report-hero'",
        "'report-staggered'",
        "'report-comparison'",
        "'workbench-rd'",
        # Legacy hash migration: removed RD/Benchmark tab hashes map to
        # their workbench anchors, never dead-ending.
        "const LEGACY_HASH_ALIASES = { 'lab-tab-rd': 'workbench-rd', 'lab-tab-bench': 'report-comparison' };",
        "hashchange",
        "'ArrowRight'",
        "'ArrowLeft'",
        "'Home'",
        "'End'",
    ):
        assert marker in lab_js, marker
    # Client-side state only: no requests, no new endpoints, no metric text.
    assert "fetch(" not in lab_js
    assert "/api/" not in lab_js


def test_spark_module_renders_nothing_below_two_finite_points() -> None:
    spark_js = _static_module_text("views/spark.js")
    assert "export function sparklineSvg(" in spark_js
    assert "if (finite.length < 2) return '';" in spark_js
    # Null-not-zero: null marks are skipped, not coerced into points.
    assert "typeof value === 'number' && Number.isFinite(value)" in spark_js
    assert "currentColor" in spark_js
    assert "<title>" in spark_js
    # Inline SVG only — no namespace URL, no external references.
    assert "xmlns" not in spark_js


def test_metric_module_gains_only_additive_status_helpers() -> None:
    metric_js = _static_module_text("metric.js")
    assert "export function statusBadgeHtml(" in metric_js
    assert "export function metricCellHtml(" in metric_js
    # Blocked statuses render their label; a missing entry renders
    # not_recorded; null is never coerced to a scalar.
    assert '<span class="metric-blocked" title="${esc(status)}">${esc(status)}</span>' in metric_js
    assert '<span class="metric-missing">not_recorded</span>' in metric_js
    assert "status-badge--legacy" in metric_js


# ---------------------------------------------------------------------------
# View wiring
# ---------------------------------------------------------------------------


def test_app_module_activates_tabs_from_existing_handlers_only() -> None:
    app_js = _static_module_text("app.js")
    assert "from './views/lab.js'" in app_js
    assert "initLabTabs({" in app_js
    # Every job flow lands on the workbench tab AND forces the single-factor
    # module active before the submit (CP9-2: results must never render into
    # a hidden module panel). RD flows land on the absorbed #workbench-rd
    # stage inside the same module.
    parse_click = app_js.index("button.addEventListener('click', async () => {")
    parse_activate = app_js.index("activateTab('lab-tab-factor');", parse_click)
    parse_submit = app_js.index("const payload = await submitParse(parserMode);", parse_click)
    assert parse_activate < parse_submit
    assert app_js.index("activateModule('lab-module-single');", parse_click) < parse_submit
    validate_click = app_js.index("validateButton.addEventListener('click', async () => {")
    validate_submit = app_js.index("const payload = await submitValidation();", validate_click)
    assert app_js.index("activateTab('lab-tab-factor');", validate_click) < validate_submit
    assert app_js.index("activateModule('lab-module-single');", validate_click) < validate_submit
    staggered_click = app_js.index("staggeredButton.addEventListener('click', async () => {")
    staggered_submit = app_js.index("const payload = await submitStaggeredEntry();", staggered_click)
    assert app_js.index("activateTab('lab-tab-factor');", staggered_click) < staggered_submit
    assert app_js.index("activateModule('lab-module-single');", staggered_click) < staggered_submit
    rd_click = app_js.index("rdRun.addEventListener('click', async () => {")
    rd_activate = app_js.index("activateTab('lab-tab-factor');", rd_click)
    rd_submit = app_js.index("const job = await postJson('/api/jobs/research-run-once', rdPayload());", rd_click)
    assert rd_activate < rd_submit
    assert app_js.index("activateModule('lab-module-single');", rd_click) < rd_submit
    # RD run brings the absorbed RD stage into view (a result event; the
    # schedule-start handler activates without scrolling).
    assert app_js.index("getElementById('workbench-rd')", rd_click) < rd_submit
    # Staggered completion returns the user to the robustness section.
    assert "getElementById('report-staggered')" in app_js


def test_background_panel_refreshes_never_switch_tabs() -> None:
    for name in ("views/history.js", "views/bench.js"):
        module_text = _static_module_text(name)
        assert "activateTab" not in module_text, name
        assert "lab.js" not in module_text, name


def test_control_token_storage_refreshes_token_gated_panels() -> None:
    # Token-protected runs: the history and bench panels skip their startup
    # fetch until a control token is stored, so storing the token must
    # refresh both panels alongside /api/status — no page reload needed.
    app_js = _static_module_text("app.js")
    start = app_js.index("onControlTokenStored(")
    block = app_js[start : app_js.index("llmProviderSelect.addEventListener", start)]
    assert "refreshRuntimeStatus()" in block
    assert "historyPanel.refresh()" in block
    assert "benchPanel.refresh()" in block


def test_tab_activation_lazy_refresh_wiring_lives_in_app_module() -> None:
    # A panel refreshes on tab activation when it has never rendered real
    # data OR a completed job marked it stale (integration finding F-008:
    # panels must not serve their first render forever). All fetch wiring
    # stays in app.js: lab.js only invokes the activation callback it was
    # handed.
    app_js = _static_module_text("app.js")
    lab_js = _static_module_text("views/lab.js")
    assert "initLabTabs({" in app_js
    assert "onActivate" in app_js
    # CP9-2: values are arrays so one tab can own several lazy panels — the
    # workbench tab owns the absorbed bench comparison panel.
    assert "'lab-tab-history': [historyPanel]" in app_js
    assert "'lab-tab-factor': [benchPanel]" in app_js
    assert "if (panel.isStale() || !panel.hasLoaded()) panel.refresh();" in app_js
    # lab.js purity is preserved: no requests, no panel refreshers.
    assert "fetch(" not in lab_js
    assert "/api/" not in lab_js
    assert "refreshHistoryPanel" not in lab_js
    assert "refreshBenchPanel" not in lab_js
    # The view refreshers report whether real data rendered, so app.js can
    # keep retrying lazily until the first successful load.
    for name in ("views/history.js", "views/bench.js"):
        module_text = _static_module_text(name)
        assert "if (!payload) return false;" in module_text, name
        assert "return true;" in module_text, name


def test_job_completion_invalidates_dependent_panels() -> None:
    # Integration finding F-008: a successfully completed job changes what
    # the history / bench / registry endpoints return, so those panels are
    # marked stale at every job-completion point instead of staying frozen
    # on their first render. The data console reads only the local data
    # root, which no job mutates, so it stays out of the dependent set.
    app_js = _static_module_text("app.js")
    # The tracked wrapper gains stale marking on top of the existing
    # in-flight de-dupe; starting a fetch is the only thing that clears it.
    assert "isStale: () => stale," in app_js
    assert "invalidate() {" in app_js
    refresh_start = app_js.index("refresh() {")
    assert app_js.index("stale = false;", refresh_start) < app_js.index(
        "inFlight = refreshPanel()", refresh_start
    )
    # Settle recheck: an invalidation that lands while a refresh is in
    # flight de-dupes into a response that may predate the job's writes,
    # so a success settle that still sees the stale mark chains exactly
    # one follow-up refresh. Gating on `rendered` keeps the error
    # semantics: a failed fetch (the refreshers resolve false, never
    # reject) chains nothing and leaves the mark for the next activation.
    settle_recheck = "if (rendered && stale) tracker.refresh();"
    assert app_js.count(settle_recheck) == 1
    # The recheck runs only after the in-flight slot clears, so the
    # chained call starts a real fetch instead of de-duping into the
    # promise that just settled.
    assert app_js.index("inFlight = null;", refresh_start) < app_js.index(
        settle_recheck, refresh_start
    )
    # CP9-2: tab/panel pairs — the bench panel moved onto the workbench tab
    # with the absorbed comparison section, so the pairing is explicit.
    assert "const JOB_DEPENDENT_PANELS = [" in app_js
    assert "['lab-tab-history', historyPanel]" in app_js
    assert "['lab-tab-factor', benchPanel]" in app_js
    assert "['lab-tab-registry', registryPanel]" in app_js
    # Immediate refresh when the dependent tab is already active: the user
    # is looking at the stale panel at the moment the job completes.
    assert "if (tab && tab.getAttribute('aria-selected') === 'true') panel.refresh();" in app_js
    # One invalidation call per success path, inside the try block (before
    # the handler's catch), never in finally: parse (primary + rule
    # fallback), validate, staggered, RD.
    parse_click = app_js.index("button.addEventListener('click', async () => {")
    validate_click = app_js.index("validateButton.addEventListener('click', async () => {")
    staggered_click = app_js.index("staggeredButton.addEventListener('click', async () => {")
    rd_click = app_js.index("rdRun.addEventListener('click', async () => {")
    call = "invalidateJobDependentPanels();"
    assert app_js.count(call, parse_click, validate_click) == 2
    assert app_js.count(call, validate_click, staggered_click) == 1
    assert app_js.count(call, staggered_click, rd_click) == 1
    assert app_js.count(call, rd_click) == 1
    for handler_start in (validate_click, staggered_click, rd_click):
        assert app_js.index(call, handler_start) < app_js.index("} catch (error) {", handler_start)
    # lab.js stays fetch-free: invalidation wiring lives in app.js only.
    lab_js = _static_module_text("views/lab.js")
    assert "invalidate" not in lab_js
    assert "isStale" not in lab_js


def test_job_failure_branches_surface_error_and_replace_stale_placeholders() -> None:
    # Integration finding F-011: when a job fails (e.g. RD requested on a
    # parse-only draft whose seed factor was never persisted), the job's
    # error field must reach the visible failure surface instead of the
    # result region keeping its stale "running" placeholder next to a
    # generic status line. Every job handler's failure branch renders a
    # design-system error notice (text label, never color alone) with the
    # escaped error message; an empty/missing message falls back to the
    # api-layer generic, so the notice never renders 'undefined'.
    app_js = _static_module_text("app.js")
    assert "function jobFailureReason(error) {" in app_js
    assert "return (error && error.message) || 'request failed';" in app_js
    assert "function showJobFailureNotice(mountId, reason) {" in app_js
    assert (
        '<div class="notice err"><span class="status-pill status-pill--fail">失败</span> ${esc(reason)}</div>'
        in app_js
    )
    parse_click = app_js.index("button.addEventListener('click', async () => {")
    validate_click = app_js.index("validateButton.addEventListener('click', async () => {")
    staggered_click = app_js.index("staggeredButton.addEventListener('click', async () => {")
    rd_click = app_js.index("rdRun.addEventListener('click', async () => {")
    # Every failure branch derives its message through the fallback helper:
    # parse (primary + rule fallback), validate, staggered, RD.
    assert app_js.count("const reason = jobFailureReason(") == 5
    assert app_js.count("const reason = jobFailureReason(fallbackError);", parse_click, validate_click) == 1
    assert app_js.count("const reason = jobFailureReason(error);", parse_click, validate_click) == 1
    # Parse failures (primary + rule fallback) replace the stale "解析中"
    # card in #result; validate replaces its stale placeholder in the same
    # mount; staggered replaces the running panel in #staggered-result; RD
    # replaces the stale "RD 运行中" placeholder in #rd-result.
    assert app_js.count("showJobFailureNotice('result', reason);", parse_click, validate_click) == 2
    assert app_js.count("showJobFailureNotice('result', reason);", validate_click, staggered_click) == 1
    assert app_js.count("showJobFailureNotice('staggered-result', reason);", staggered_click, rd_click) == 1
    assert app_js.count("showJobFailureNotice('rd-result', reason);", rd_click) == 1
    # The idea-flow handlers keep surfacing the reason on the global error
    # line; the RD handler surfaces it into #rd-status through the esc()
    # pattern (styled err span), never a raw template interpolation.
    assert app_js.count("errorEl.textContent = reason;") == 4
    assert 'rdStatusEl.innerHTML = `<span class="err">${esc(reason)}</span>`;' in app_js
    assert "rdStatusEl.textContent = error.message;" not in app_js[rd_click:app_js.index("rdCancel.addEventListener", rd_click)]


def test_report_anchor_deep_link_keeps_the_report_fragment() -> None:
    # A workbench anchor hash (#report-* / #workbench-rd) activates the
    # factor tab AND forces the single-factor module active without
    # rewriting the URL fragment, so reload / back / copy-link keep the
    # section anchor (CP9-2: the anchor branch spans WORKBENCH_ANCHOR_IDS =
    # report sections + the absorbed RD stage).
    lab_js = _static_module_text("views/lab.js")
    assert "const updateHash = !(options && options.updateHash === false);" in lab_js
    # A-MINOR-3: the workbench tab's canonical fragment reflects the ACTIVE
    # module, so returning to the workbench with the multi module selected
    # writes #lab-module-multi (copy-link fidelity) instead of always
    # #lab-tab-factor; every other tab still uses its own id, and the anchor
    # deep-link path below still passes {updateHash: false} to keep its
    # section fragment.
    assert "const canonical = tabId === 'lab-tab-factor' ? workbenchCanonicalHash() : `#${tabId}`;" in lab_js
    assert "if (window.location.hash !== canonical) {" in lab_js
    assert "return activeModuleId() === 'lab-module-multi' ? '#lab-module-multi' : '#lab-tab-factor';" in lab_js
    assert "const WORKBENCH_ANCHOR_IDS = [...REPORT_SECTION_IDS, 'workbench-rd'];" in lab_js
    anchor_branch = lab_js.index("WORKBENCH_ANCHOR_IDS.includes(target)")
    activate = lab_js.index("activateTab('lab-tab-factor', { updateHash: false });", anchor_branch)
    module = lab_js.index("activateModule('lab-module-single', { updateHash: false });", anchor_branch)
    scroll = lab_js.index("scrollToReportSection(target);", anchor_branch)
    assert activate < module < scroll


def test_missing_split_weighted_icir_renders_na_not_zero() -> None:
    # FP-4: a candidate card without split_weighted_icir must show n/a via
    # num() (which maps null/undefined to 'n/a'), never a fabricated 0.00.
    research_js = _static_module_text("views/research.js")
    assert "num(candidate.split_weighted_icir, 2)" in research_js
    assert "valueOr(candidate.split_weighted_icir" not in research_js


def test_factor_report_is_componentized_under_stable_section_ids() -> None:
    factor_js = _static_module_text("views/factor.js")
    for marker in (
        "export function renderReportHero(",
        "export function renderPendingParams(",
        "export function renderEvaluationSection(",
        "export function renderInSampleSection(",
        "export function renderOosSection(",
        "export function renderDiagnosticsSection(",
        "export function renderEvidenceSection(",
        "export function renderArtifactsSection(",
        "export function renderAnchorNav(",
        'id="report-hero"',
        'id="report-params"',
        'id="report-evaluation"',
        'id="report-insample"',
        'id="report-oos"',
        'id="report-diagnostics"',
        'id="report-evidence"',
        'id="report-artifacts"',
        'id="report-staggered"',
        'class="anchor-nav"',
        'class="eyebrow"',
        # CP9-1: the staggered #report-staggered NAV row is now an honest
        # inline-SVG line chart (charts.js) instead of the spark sparkline.
        "lineChart(",
        # CP9-2: the report hero formula renders through the structural DSL
        # highlighter, and the anchor nav reaches the absorbed bench
        # comparison section.
        "from './dsl.js'",
        "formulaHtml(factor.formula)",
        "'report-comparison', label: 'Benchmark 对比'",
    ):
        assert marker in factor_js, marker
    assert "from './charts.js'" in factor_js


def test_research_gate_markers_carry_text_labels() -> None:
    research_js = _static_module_text("views/research.js")
    for marker in (
        "export function renderRdSummary(",
        "export function renderCandidateCard(",
        'id="rd-summary"',
        "status-pill status-pill--ok",
        "status-pill status-pill--neutral",
        "candidate · pass",
        "draft · gate fail",
    ):
        assert marker in research_js, marker
    # The old color-only gate markers are gone from candidate cards.
    assert '<span class="ok">candidate</span>' not in research_js
    assert '<span class="err">draft</span>' not in research_js


# ---------------------------------------------------------------------------
# CP9-2 IA consolidation: legacy hash migration, module nav, DSL highlighting
# ---------------------------------------------------------------------------


def test_legacy_tab_hashes_migrate_to_workbench_anchors() -> None:
    # The removed RD 循环 / Benchmark tab hashes must never dead-end: the
    # alias map rewrites them to their workbench anchors and normalizes the
    # URL (replaceState) BEFORE the TAB_IDS branch, so the migrated target
    # then routes through the workbench-anchor branch.
    lab_js = _static_module_text("views/lab.js")
    alias_map = lab_js.index(
        "const LEGACY_HASH_ALIASES = { 'lab-tab-rd': 'workbench-rd', 'lab-tab-bench': 'report-comparison' };"
    )
    apply_hash = lab_js.index("function applyHash(hash) {")
    assert alias_map < apply_hash
    alias_lookup = lab_js.index("const alias = LEGACY_HASH_ALIASES[target];", apply_hash)
    normalize = lab_js.index("window.history.replaceState(null, '', `#${alias}`);", apply_hash)
    retarget = lab_js.index("target = alias;", apply_hash)
    tab_branch = lab_js.index("if (TAB_IDS.includes(target))", apply_hash)
    module_branch = lab_js.index("if (MODULE_IDS.includes(target))", apply_hash)
    anchor_branch = lab_js.index("if (WORKBENCH_ANCHOR_IDS.includes(target))", apply_hash)
    assert alias_lookup < normalize < retarget < tab_branch < module_branch < anchor_branch
    # #lab-module-* hashes activate the workbench tab plus that module while
    # keeping the fragment; activateModule owns the canonical hash (the
    # default single module maps back to the workbench tab hash).
    assert "activateModule(target, { updateHash: false });" in lab_js
    assert (
        "const canonical = moduleId === 'lab-module-single' ? '#lab-tab-factor' : `#${moduleId}`;"
        in lab_js
    )


def test_index_page_hosts_the_workbench_module_nav(web_config) -> None:
    html = web_server._index_html(web_config)
    # Exact module-nav markup (CP9-2 §1.2): tablist semantics over the two
    # workbench modules. CP10 filled the reserved multi slot: the 即将上线
    # pill and placeholder card were replaced by the module's
    # server-rendered skeleton (the mount id and nav hook stayed EXACTLY as
    # reserved; only the placeholder content moved).
    for marker in (
        '<div class="lab-module-nav" role="tablist" aria-label="工作台模块">',
        '<button class="lab-module-tab" role="tab" id="lab-module-single" aria-controls="lab-module-panel-single" aria-selected="true">单因子研究</button>',
        '<button class="lab-module-tab" role="tab" id="lab-module-multi" aria-controls="lab-module-panel-multi" aria-selected="false" tabindex="-1">多因子策略回测</button>',
        'id="lab-module-panel-single" aria-labelledby="lab-module-single" tabindex="0"',
        'id="lab-module-panel-multi" aria-labelledby="lab-module-multi" tabindex="0" hidden',
        # CP10 claims this mount and the lab-module-multi nav hook.
        "CP10 mount: the multi-factor module claims #multi-result",
        'id="multi-result"',
        # CP10 skeleton: form regions + result mount inside #multi-result.
        "合成配置",
        'id="synth-factors"',
        'id="synth-method-mount"',
        'id="synth-report"',
        "合成回测完成后，评价、样本内回测、外部样本外评测与合成 provenance 会展示在这里。",
    ):
        assert marker in html, marker
    # The absorbed sections carry report-section anchors inside the single
    # module so sticky-strip scroll clearance applies unchanged.
    assert '<section class="report-section" id="report-comparison">' in html
    assert '<section class="report-section" id="workbench-rd">' in html
    # Module-nav CSS ships with token-referencing declarations only.
    for rule in (".lab-module-nav", ".lab-module-tab", ".lab-module-panel"):
        assert rule in html, rule
    module_css_start = html.index("/* CP9-2 workbench module nav")
    module_css = html[module_css_start : html.index(".anchor-nav", module_css_start)]
    assert "#" not in module_css
    assert "var(--" in module_css
    # DSL formula-highlight CSS ships too, token-referencing only, so both
    # themes come from the variables.
    dsl_css_start = html.index("/* CP9-2 DSL formula highlighting")
    dsl_css = html[dsl_css_start : html.index(".evidence-grid", dsl_css_start)]
    for rule in (".dsl-fn", ".dsl-id", ".dsl-num", ".dsl-str", ".dsl-op", ".dsl-punct"):
        assert rule in dsl_css, rule
    assert "#" not in dsl_css
    assert "var(--" in dsl_css


def test_dsl_module_is_a_pure_structural_tokenizer() -> None:
    dsl_js = _static_module_text("views/dsl.js")
    for marker in (
        "export function tokenizeFormula(",
        "export function formulaHtml(",
        # FP-4 single-renderer rule: dsl.js imports esc, never defines it.
        "import { esc } from '../metric.js';",
        "'dsl-fn'",
        "'dsl-id'",
        "'dsl-num'",
        "'dsl-str'",
        "'dsl-op'",
        "'dsl-punct'",
    ):
        assert marker in dsl_js, marker
    # Structural and pure: no operator-catalog coupling, no dynamic code
    # evaluation, no requests.
    assert "eval(" not in dsl_js
    assert "new Function" not in dsl_js
    assert "fetch(" not in dsl_js
    assert "/api/" not in dsl_js
    assert "function esc(" not in dsl_js
    # Application sites: exactly the report hero and the registry detail
    # card highlight; the precomputed key and the ellipsized list rows stay
    # esc()-only (a key is not an expression).
    registry_js = _static_module_text("views/registry.js")
    assert "from './dsl.js'" in registry_js
    assert '`<div class="formula">${formulaHtml(formula)}</div>`' in registry_js
    assert "${esc(formula.slice(PRECOMPUTED_PREFIX.length))}" in registry_js
    assert '<span class="registry-row-formula">${esc(text)}</span>' in registry_js
    assert "formulaHtml" not in _static_module_text("views/research.js")


# ---------------------------------------------------------------------------
# CP9-2 follow-up regression pins (A-MINOR-1 dot priority, A-MINOR-2 nav)
# ---------------------------------------------------------------------------


def test_module_nav_click_and_keyboard_wiring_is_pinned() -> None:
    # A-MINOR-2: the workbench module-nav BEHAVIOR (click-to-activate +
    # roving-tabindex keyboard nav) was unpinned — markup/exports were pinned
    # but deleting the module click handler or the onModuleNavKeydown wiring
    # would still pass. Pin the string contract, consistent with the tablist
    # wiring pins.
    lab_js = _static_module_text("views/lab.js")
    # Click handler: a .lab-module-tab click activates that module.
    assert "const moduleNav = document.querySelector('.lab-module-nav');" in lab_js
    module_nav = lab_js.index("const moduleNav = document.querySelector('.lab-module-nav');")
    click_bind = lab_js.index("moduleNav.addEventListener('click'", module_nav)
    keydown_bind = lab_js.index("moduleNav.addEventListener('keydown', onModuleNavKeydown);", module_nav)
    assert "const tab = event.target.closest('.lab-module-tab');" in lab_js[click_bind:keydown_bind]
    assert lab_js.index("if (tab) activateModule(tab.id);", click_bind) < keydown_bind
    # Keydown routes to onModuleNavKeydown, which mirrors the tablist roving
    # tabindex over MODULE_IDS (Arrow / Home / End -> activateModule).
    assert "function onModuleNavKeydown(event) {" in lab_js
    keydown_fn = lab_js.index("function onModuleNavKeydown(event) {")
    body = lab_js[keydown_fn : lab_js.index("export function initLabTabs(", keydown_fn)]
    assert "event.target.closest('.lab-module-tab')" in body
    for key in ("'ArrowRight'", "'ArrowLeft'", "'Home'", "'End'"):
        assert key in body, key
    assert "focusModuleByOffset(" in body


def test_workbench_dot_reflects_job_family_priority_not_last_writer() -> None:
    # A-MINOR-1: the idea lane (parse/validate/staggered on activeIdeaJobId)
    # and the RD lane (activeRdJobId) run concurrently but share ONE workbench
    # dot. A completing job must not downgrade or clear a still-active family,
    # so the dot shows the highest-priority active state across both families
    # (error > running > done > idle) instead of last-writer-wins.
    app_js = _static_module_text("app.js")
    assert "const WORKBENCH_DOT_PRIORITY = { error: 3, running: 2, done: 1 };" in app_js
    assert "const workbenchDotState = { idea: null, rd: null };" in app_js
    assert "function setWorkbenchDot(family, state) {" in app_js
    # Per-family state is tracked; a state outside the priority map (e.g.
    # 'clear' on cancel) zeroes only that family, never the other lane.
    assert "workbenchDotState[family] = WORKBENCH_DOT_PRIORITY[state] ? state : null;" in app_js
    # Exactly one visible dot: the consolidated setter is the ONLY writer of
    # the workbench tab dot — no handler calls setTabDot('lab-tab-factor', ...)
    # directly any more (that was the last-writer-wins regression).
    assert app_js.count("setTabDot('lab-tab-factor',") == 1
    assert "setTabDot('lab-tab-factor', winner || 'clear');" in app_js
    # Both lanes route through the priority setter; the RD lane is the sole
    # 'rd' family user (run start / done / cancel / error), the idea lane
    # owns parse + validate + staggered.
    assert app_js.count("setWorkbenchDot('rd', ") == 4
    assert app_js.count("setWorkbenchDot('idea', ") == 14

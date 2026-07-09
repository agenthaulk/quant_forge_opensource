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
    # Tablist wiring: eight tabs (CP6-2 four + CP6-3 data/registry + CP6-4
    # docs/extensions appended so existing tab indices stay stable), factor
    # selected, panels labelled by tabs. The tablist spans non-Lab views
    # since CP6-3, hence the label.
    assert 'role="tablist"' in html
    assert 'aria-label="工作台视图"' in html
    for tab, panel in (
        ("lab-tab-factor", "lab-panel-factor"),
        ("lab-tab-rd", "lab-panel-rd"),
        ("lab-tab-history", "lab-panel-history"),
        ("lab-tab-bench", "lab-panel-bench"),
        ("lab-tab-data", "lab-panel-data"),
        ("lab-tab-registry", "lab-panel-registry"),
        ("lab-tab-docs", "lab-panel-docs"),
        ("lab-tab-extensions", "lab-panel-extensions"),
    ):
        assert f'id="{tab}" aria-controls="{panel}"' in html
        assert f'id="{panel}" aria-labelledby="{tab}"' in html
    assert html.count('role="tab"') == 8
    assert html.count('role="tabpanel"') == 8
    tablist = html[html.index('role="tablist"') : html.index('id="error"')]
    assert tablist.count('aria-selected="true"') == 1
    assert tablist.count('aria-selected="false"') == 7
    # Non-default panels start hidden; the default factor panel does not.
    for panel in (
        "lab-panel-rd",
        "lab-panel-history",
        "lab-panel-bench",
        "lab-panel-data",
        "lab-panel-registry",
        "lab-panel-docs",
        "lab-panel-extensions",
    ):
        start = html.index(f'id="{panel}"')
        assert "hidden" in html[start : html.index(">", start)], panel
    factor_start = html.index('id="lab-panel-factor"')
    assert "hidden" not in html[factor_start : html.index(">", factor_start)]
    # Mount ids survive re-parenting: each mount sits inside its panel.
    assert html.index('id="lab-panel-factor"') < html.index('id="result"')
    assert html.index('id="result"') < html.index('id="staggered-result"')
    assert html.index('id="staggered-result"') < html.index('id="lab-panel-rd"')
    assert html.index('id="lab-panel-rd"') < html.index('id="rd-result"')
    assert html.index('id="rd-result"') < html.index('id="lab-panel-history"')
    assert html.index('id="lab-panel-history"') < html.index('id="history-result"')
    assert html.index('id="history-result"') < html.index('id="lab-panel-bench"')
    assert html.index('id="lab-panel-bench"') < html.index('id="bench-result"')
    assert html.index('id="bench-result"') < html.index('id="lab-panel-data"')
    assert html.index('id="lab-panel-data"') < html.index('id="data-result"')
    assert html.index('id="data-result"') < html.index('id="lab-panel-registry"')
    assert html.index('id="lab-panel-registry"') < html.index('id="registry-result"')
    # The global error line stays outside (above) the tab panels.
    assert html.index('id="error"') < html.index('id="lab-panel-factor"')


def test_index_page_renders_the_flow_stepper(web_config) -> None:
    html = web_server._index_html(web_config)
    assert 'class="lab-stepper"' in html
    assert 'aria-label="研究流程"' in html
    for step, label in (
        ("idea", "想法"),
        ("parse", "解析"),
        ("validate", "验证"),
        ("report", "因子报告"),
        ("rd", "RD 循环"),
    ):
        assert f'data-step="{step}"' in html, step
        assert label in html, label
    # Only the report and rd steps are interactive; the report step-link
    # stays disabled until a full report exists.
    assert html.count('class="step-link"') == 2
    assert 'data-step-action="report" disabled' in html
    assert 'data-step-action="rd"' in html


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
    for marker in (
        "export function initLabTabs(",
        "export function activateTab(",
        "export function setTabDot(",
        "export function setStep(",
        "'lab-tab-factor', 'lab-tab-rd', 'lab-tab-history', 'lab-tab-bench'",
        "'report-hero'",
        "'report-staggered'",
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
    # Parse / validate / staggered flows land on the factor tab before the
    # submit; RD flows land on the RD tab.
    parse_click = app_js.index("button.addEventListener('click', async () => {")
    parse_activate = app_js.index("activateTab('lab-tab-factor');", parse_click)
    parse_submit = app_js.index("const payload = await submitParse(parserMode);", parse_click)
    assert parse_activate < parse_submit
    rd_click = app_js.index("rdRun.addEventListener('click', async () => {")
    rd_activate = app_js.index("activateTab('lab-tab-rd');", rd_click)
    rd_submit = app_js.index("const job = await postJson('/api/jobs/research-run-once', rdPayload());", rd_click)
    assert rd_activate < rd_submit
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
    assert "'lab-tab-history': historyPanel" in app_js
    assert "'lab-tab-bench': benchPanel" in app_js
    assert "if (panel && (panel.isStale() || !panel.hasLoaded())) panel.refresh();" in app_js
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
    assert (
        "const JOB_DEPENDENT_PANEL_TABS = ['lab-tab-history', 'lab-tab-bench', 'lab-tab-registry'];"
        in app_js
    )
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
    # A #report-* hash activates the factor tab without rewriting the URL
    # fragment, so reload / back / copy-link keep the section anchor.
    lab_js = _static_module_text("views/lab.js")
    assert "const updateHash = !(options && options.updateHash === false);" in lab_js
    assert "if (updateHash && window.location.hash !== `#${tabId}`)" in lab_js
    anchor_branch = lab_js.index("REPORT_SECTION_IDS.includes(target)")
    activate = lab_js.index("activateTab('lab-tab-factor', { updateHash: false });", anchor_branch)
    scroll = lab_js.index("scrollToReportSection(target);", anchor_branch)
    assert activate < scroll


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

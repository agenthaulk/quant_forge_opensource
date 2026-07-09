"""Targeted tests for the CP10 multi-factor synthesis module (D8).

Two layers, mirroring the charts.js convention:

- Python source-contract pins over the served page (the module's
  server-rendered skeleton inside the reserved ``#multi-result`` mount) and
  the served ``views/synthesis.js`` module: purity split (pure renderers
  before the ``[controller]`` section; fetch/DOM only after it), ZERO
  per-method hardcoding (no shipped method or standardizer name appears in
  the module — the dynamic params form renders purely from the catalog's
  ParamSpec JSON), the honest 方法目录不可用 degraded state while
  ``GET /api/synthesis/methods`` is absent on this deployment, esc()
  discipline, FP-4 sweeps (no toFixed / null-to-zero), and the lazy app.js
  activation wiring (module-panel ``hidden`` observation; lab.js untouched).
- A stdlib-only Node fixture harness that imports the real module and drives
  the pure renderers/builders with fixtures: a FICTIONAL method must render
  a working form (proving zero hardcoding), weights inputs must track the
  checked-factor set, direction toggles are explicit (+1 default), the
  request builder mirrors the §8.2 contract (>=2 factors, required
  holding_days, weights key-set, standardization OMITTED when the method
  pins one per the B3 wire report), and the report renderers pin the
  provenance/validity/coverage contract (raw a-priori weights echoed
  unnormalized, coverage_ratio null -> 'n/a' never 0, is_fitted surfacing
  only as the 先验声明 label, section ids re-hosted so the multi report
  never mints duplicate #report-* anchors).
"""

from __future__ import annotations

import shutil
import subprocess
import threading
import urllib.request

import pytest

import quant_forge.apps.web.server as web_server
from quant_forge.apps.web.server import create_local_web_server
from quant_forge.config import QuantForgeConfig
from quant_forge.data.local import create_demo_workspace


JS_CONTENT_TYPE = "text/javascript; charset=utf-8"
SYNTHESIS_PATH = web_server.STATIC_ROOT / "views" / "synthesis.js"

# Method/standardizer names shipped by the backend catalog. NONE of them may
# appear in the frontend module: the form must render purely from the §7
# ParamSpec JSON so a new method needs no frontend change.
BACKEND_CATALOG_NAMES = (
    "equal_weight",
    "custom_weight",
    "rank_average",
    "ic_weighted",
    "zscore",
    "cross_sectional_rank",
)


def _static_module_text(name: str) -> str:
    return (web_server.STATIC_ROOT / name).read_text(encoding="utf-8")


def _synthesis_src() -> str:
    return SYNTHESIS_PATH.read_text(encoding="utf-8")


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


# ---------------------------------------------------------------------------
# Served page: the module's server-rendered skeleton inside #multi-result
# ---------------------------------------------------------------------------


def test_index_page_hosts_the_synthesis_module_skeleton(web_config) -> None:
    html = web_server._index_html(web_config)
    # The reserved mount id and module nav hook stay EXACTLY as reserved;
    # only the placeholder content was replaced by the module skeleton.
    assert "CP10 mount: the multi-factor module claims #multi-result" in html
    assert 'id="multi-result"' in html
    assert 'id="lab-module-multi"' in html
    # The 即将上线 placeholder is gone everywhere (nav pill + card).
    assert "即将上线" not in html
    assert "多因子策略合成与回测模块将在后续版本提供" not in html
    # Form regions + result mount.
    for marker in (
        'id="synth-form"',
        "合成配置",
        'id="synth-factors"',
        'id="synth-method-mount"',
        'id="synth-standardization-mount"',
        'id="synth-params"',
        'id="synth-backtest-params"',
        'id="synth-param-holding-days"',
        'id="synth-param-decay-days"',
        'id="synth-param-top-quantile"',
        'id="synth-param-delay-days"',
        'id="synth-param-evaluation-start"',
        'id="synth-param-evaluation-end"',
        'id="synth-param-backtest-start"',
        'id="synth-param-backtest-end"',
        'id="synth-param-commission-bps"',
        'id="synth-param-slippage-bps"',
        'id="synth-param-short-borrow-bps"',
        'id="synth-run-hint"',
        'id="synth-run"',
        'id="synth-cancel"',
        'id="synth-status"',
        'id="synth-report"',
        "等待运行",
        "合成回测完成后，评价、样本内回测、外部样本外评测与合成 provenance 会展示在这里。",
    ):
        assert marker in html, marker
    # holding_days is REQUIRED; the prefilled 5 is a suggestion only and the
    # label says so.
    assert '<input id="synth-param-holding-days" type="number" min="1" step="1" value="5">' in html
    assert "持有期 / 天（必填）" in html
    assert "持有期为必填（预填 5 仅为建议值）" in html
    # The run control starts disabled until >=2 factors + a method exist —
    # and never without a stated reason: the why-disabled hint is a live
    # region the button points at, server-prefilled with the exact text the
    # pure runReadinessHintText builder emits for the initial state.
    assert '<button id="synth-run" disabled aria-describedby="synth-run-hint">合成并回测</button>' in html
    assert (
        '<p id="synth-run-hint" class="meta" aria-live="polite">'
        "已选 0 个因子，运行需至少勾选 2 个；方法目录尚未加载或不可用。</p>"
    ) in html
    assert '<p id="synth-status" class="meta" aria-live="polite"></p>' in html
    # A-priori discipline is stated on the form itself.
    assert "权重与方法为先验声明（非拟合）" in html
    # No inline script arrives with the skeleton (D8): still exactly the
    # JSON config block plus the module entry tag.
    assert html.count("<script") == 2
    assert "addEventListener" not in html
    # Skeleton lives inside the reserved multi module panel, before the
    # history panel; tab semantics unchanged (6 + 2 tabs).
    assert html.index('id="lab-module-panel-multi"') < html.index('id="multi-result"')
    assert html.index('id="multi-result"') < html.index('id="synth-form"')
    assert html.index('id="synth-form"') < html.index('id="synth-report"')
    assert html.index('id="synth-report"') < html.index('id="lab-panel-history"')
    assert html.count('role="tab"') == 8
    assert html.count('role="tabpanel"') == 8


def test_index_page_ships_cp10_css_with_theme_tokens_only(web_config) -> None:
    html = web_server._index_html(web_config)
    for rule in (
        ".synth-factor-list",
        ".synth-factor-row",
        ".synth-factor-name",
        ".synth-direction-label",
        ".synth-factor-formula",
        ".synth-param",
        ".synth-check-label",
        ".synth-weight-id",
    ):
        assert rule in html, rule
    block_start = html.index("/* CP10 multi-factor synthesis module")
    block_end = html.index("@media (prefers-color-scheme: dark)")
    block = html[block_start:block_end]
    # Zero new color literals and no id selectors: token references only.
    assert "#" not in block
    assert "var(--" in block


# ---------------------------------------------------------------------------
# Served module: delivery, purity split, zero hardcoding, sweeps
# ---------------------------------------------------------------------------


def test_synthesis_module_served_with_js_content_type(web_app) -> None:
    with urllib.request.urlopen(web_app + "/static/views/synthesis.js", timeout=10) as response:
        assert response.status == 200
        assert (response.getheader("Content-Type", "") or "") == JS_CONTENT_TYPE
        assert response.read().decode("utf-8") == _synthesis_src()


def test_synthesis_module_purity_split_fetch_after_render() -> None:
    src = _synthesis_src()
    marker = src.index("// [controller]")
    pure = src[:marker]
    # Every pure export (renderers + guards/builders) is defined before the
    # controller section, so fixtures can drive them without a DOM.
    for definition in (
        "export function renderFactorPickerHtml(",
        "export function renderMethodsUnavailableHtml(",
        "export function renderMethodSelectHtml(",
        "export function renderStandardizationHtml(",
        "export function renderParamsFormHtml(",
        "export function runReadinessHintText(",
        "export function renderValidityBannerHtml(",
        "export function renderCoverageByRoleHtml(",
        "export function renderProvenanceCardHtml(",
        "export function renderSynthesisReportHtml(",
        "export function validateHoldingDaysInput(",
        "export function buildRunRequest(",
    ):
        assert definition in pure, definition
    # The pure region performs no fetch, no DOM access, no event wiring
    # (imports carry the api.js names, but no call sites appear here).
    for call in (
        "fetchPanelJson(",
        "postJson(",
        "waitForJob(",
        "cancelJob(",
        "document.getElementById(",
        "document.querySelectorAll(",
        "addEventListener(",
        "innerHTML",
    ):
        assert call not in pure, call
    # The controller half owns exactly the module's fetch/job surfaces.
    controller = src[marker:]
    assert "fetchPanelJson('/api/registry/factors')" in controller
    assert "fetchPanelJson('/api/synthesis/methods')" in controller
    assert "postJson('/api/jobs/multi-factor-backtest', request)" in controller
    assert "waitForJob(" in controller
    assert "export async function refreshSynthesisPanel()" in controller
    assert "export function initSynthesisModule(" in controller


def test_synthesis_module_has_zero_per_method_hardcoding() -> None:
    src = _synthesis_src()
    for name in BACKEND_CATALOG_NAMES:
        assert name not in src, f"per-method hardcoding: {name}"
    # Availability and the reserved-entry affordance are generic payload
    # properties, never name checks.
    assert "method.available === true" in src
    assert "（预留）" in src
    assert "required_standardization" in src


def test_synthesis_module_renders_the_dynamic_form_from_paramspec_json() -> None:
    src = _synthesis_src()
    # One branch per ParamSpec type — and only these branches; the weights
    # branch keys one input per checked factor by factor_id.
    assert "spec.type === 'weights'" in src
    assert "spec.type === 'bool'" in src
    assert "spec.type === 'enum'" in src
    assert "spec.type === 'int' ? '1' : 'any'" in src
    assert 'data-weight-factor="${esc(factorId)}"' in src
    assert "data-param-name=" in src
    # min/max/default come from the spec, absent bounds render no attribute
    # (never a fabricated 0).
    assert "spec.minimum === undefined || spec.minimum === null ? ''" in src
    assert "spec.maximum === undefined || spec.maximum === null ? ''" in src


def test_synthesis_module_escapes_every_server_interpolation() -> None:
    src = _synthesis_src()
    for token in (
        "${factor.",
        "${method.",
        "${spec.",
        "${std.",
        "${provenance.",
        "${validity.",
        "${payload.",
        "${row.",
        "${ref.",
        "${entry.",
        "${roleMeta.",
        "${catalog.",
        "${error.",
        "${job.",
    ):
        assert token not in src, f"unescaped interpolation {token}"
    assert "from '../metric.js'" in src


def test_synthesis_module_never_formats_or_zero_fills_metrics() -> None:
    src = _synthesis_src()
    # FP-4: metric.js is the only number renderer; nulls are never coerced.
    assert ".toFixed(" not in src
    assert "|| 0" not in src
    assert "?? 0" not in src
    assert "function esc(" not in src
    assert "function pct(" not in src
    assert "function num(" not in src
    # Coverage ratios go through pct() (null -> 'n/a'); counts through
    # valueOr(..., 'n/a').
    assert "${pct(row.coverage_ratio)}" in src
    assert "valueOr(row.rows_scored, 'n/a')" in src
    assert "valueOr(row.rows_in_composite, 'n/a')" in src


def test_synthesis_module_degraded_methods_catalog_state() -> None:
    src = _synthesis_src()
    # The absent methods endpoint is an explicit, labeled state — never a
    # crash, never a silently empty select (FP-4 for capability surfaces).
    assert "方法目录不可用" in src
    assert "methodMount.innerHTML = renderMethodsUnavailableHtml(error.message);" in src
    # Degraded keeps the panel "not loaded" so the next activation retries;
    # a missing control token skips silently for the lazy retry.
    assert "return factorsOk && methodsOk;" in src
    assert "if (!payload) return false;" in src
    # The run guard needs both >=2 factors and an available method.
    assert "checkedSelection().length >= 2 && Boolean(selectedMethod())" in src


def test_synthesis_report_reuses_factor_sections_and_rehosts_ids() -> None:
    src = _synthesis_src()
    # Report blocks reuse the single-factor section renderers (metric.js
    # cells and charts.js gap semantics arrive through them) …
    for imported in (
        "renderArtifactsSection",
        "renderDiagnosticsSection",
        "renderEvaluationSection",
        "renderEvidenceSection",
        "renderInSampleSection",
        "renderOosSection",
    ):
        assert imported in src, imported
    assert "from './factor.js'" in src
    assert "from './dsl.js'" in src
    # … but every reused section id is re-hosted to a synth-* id so the
    # multi report never mints duplicate #report-* anchors.
    for pair in (
        "'report-evaluation', 'synth-evaluation'",
        "'report-insample', 'synth-insample'",
        "'report-oos', 'synth-oos'",
        "'report-diagnostics', 'synth-diagnostics'",
        "'report-evidence', 'synth-evidence'",
        "'report-artifacts', 'synth-artifacts'",
    ):
        assert pair in src, pair
    # The single-factor anchor nav is NOT reused: its #report-* hrefs would
    # route back to the single-factor module.
    assert "renderAnchorNav" not in src


def test_synthesis_module_contains_no_external_references() -> None:
    src = _synthesis_src()
    assert "http://" not in src
    assert "https://" not in src


def test_synthesis_frontend_followup_source_contracts() -> None:
    # CP10 frontend follow-up (B-MAJOR-1 / B-MINOR-1 / B-MINOR-2 / B-MINOR-4):
    # source-contract pins over the DOM-facing controller parts of
    # views/synthesis.js (the node harness drives the pure renderers).
    src = _synthesis_src()
    # B-MAJOR-1: the panel refresh captures the picker state BEFORE the
    # re-render, threads it back through renderFactorPickerHtml, then rebuilds
    # the params form from the restored selection — so a completed job never
    # wipes an in-progress selection + directions.
    assert "export function renderFactorPickerHtml(factors, preserved) {" in src
    assert "function captureFactorPickerState() {" in src
    capture = src.index("const preservedSelection = captureFactorPickerState();")
    rerender = src.index("renderFactorPickerHtml(payload.factors || [], preservedSelection);")
    assert capture < rerender
    assert (
        "regenerateParamsForm();\n  updateRunEnabled();\n  return factorsOk && methodsOk;"
        in src
    )
    # B-MINOR-1: an unrecognized spec.type renders a labeled unsupported
    # notice (escaped type), never a number input; collectMethodParams omits
    # any non-scalar/choice type so no fabricated value is sent.
    assert "不支持的参数类型" in src
    assert 'class="synth-param synth-param-unsupported"' in src
    assert (
        "if (spec.type !== 'bool' && spec.type !== 'enum' && spec.type !== 'float' && spec.type !== 'int') return;"
        in src
    )
    # B-MINOR-2: an untouched OPTIONAL weights param is omitted, not sent as
    # an empty {} (matches the enum/number blank-optional rule).
    assert "if (spec.required || Object.keys(weights).length) params[spec.name] = weights;" in src
    # B-MINOR-4: readiness also requires a usable standardization when the
    # chosen method needs one; otherwise the run stays disabled with the
    # stated reason.
    assert "function standardizationReadyForSelection() {" in src
    assert "if (method.required_standardization) return true;" in src
    assert "return Boolean(selectedStandardizationName());" in src
    assert "&& standardizationReady && !jobRunning;" in src
    assert "所选方法需要标准化，但标准化目录为空或不可用" in src


# ---------------------------------------------------------------------------
# app.js wiring: lazy activation via the module panel's hidden attribute
# ---------------------------------------------------------------------------


def test_app_module_wires_synthesis_lazily_via_hidden_observation() -> None:
    app_js = _static_module_text("app.js")
    assert "import { initSynthesisModule, refreshSynthesisPanel } from './views/synthesis.js';" in app_js
    assert "const synthesisPanel = trackedPanelRefresh(refreshSynthesisPanel);" in app_js
    # Job flow completion invalidates the shared job-dependent panels via
    # the callback — synthesis.js never reaches into app-level state.
    assert "initSynthesisModule({ onJobComplete: invalidateJobDependentPanels });" in app_js
    # Activation signal: the reserved module panel's hidden attribute (one
    # signal for nav clicks, keyboard activation, and deep links). lab.js
    # gains no callback and stays fetch-free.
    assert "new MutationObserver(refreshSynthesisPanelIfDue)" in app_js
    assert ".observe(multiModulePanel, { attributes: true, attributeFilter: ['hidden'] });" in app_js
    assert "if (!multiModulePanel || multiModulePanel.hidden) return;" in app_js
    assert (
        "if (synthesisPanel.isStale() || !synthesisPanel.hasLoaded()) synthesisPanel.refresh();"
        in app_js
    )
    # Strictly lazy: the only refresh call site is inside the visibility
    # guard — no eager startup fetch for the hidden module.
    assert app_js.count("synthesisPanel.refresh()") == 1
    startup_block = app_js[
        app_js.index("llmProviderSelect.addEventListener") : app_js.index("button.addEventListener('click'")
    ]
    assert "synthesisPanel" not in startup_block
    # Returning to the workbench tab with the multi module still selected
    # retries through the tab-activation callback (the hidden attribute
    # does not change on tab switches).
    assert "if (tabId === 'lab-tab-factor') refreshSynthesisPanelIfDue();" in app_js
    # Token arrival retries only when the module is visible (still lazy).
    token_block = app_js[
        app_js.index("onControlTokenStored(") : app_js.index("llmProviderSelect.addEventListener")
    ]
    assert "refreshSynthesisPanelIfDue();" in token_block
    # Completed jobs mark the picker stale (registry catalog may change).
    invalidate_start = app_js.index("function invalidateJobDependentPanels() {")
    invalidate_block = app_js[invalidate_start : app_js.index("initLabTabs({", invalidate_start)]
    assert "synthesisPanel.invalidate();" in invalidate_block
    assert "refreshSynthesisPanelIfDue();" in invalidate_block
    # lab.js purity untouched by CP10.
    lab_js = _static_module_text("views/lab.js")
    assert "synthesis" not in lab_js
    assert "fetch(" not in lab_js
    assert "/api/" not in lab_js


# ---------------------------------------------------------------------------
# Node fixture harness: fixtures drive the pure renderers/builders directly
# ---------------------------------------------------------------------------


_FIXTURE_HARNESS = r"""
// factor.js binds its mounts at import time; a minimal document stub keeps
// the import inert under Node (the pure renderers never touch it).
globalThis.document = {
  getElementById: () => null,
  querySelectorAll: () => [],
  addEventListener: () => {}
};
const mod = await import(process.env.QF_SYNTH_URL);

let failed = 0;
function check(name, cond, detail) {
  if (cond) { console.log('PASS ' + name); }
  else { failed++; console.log('FAIL ' + name + (detail ? ': ' + detail : '')); }
}
function throws(fn) {
  try { fn(); return null; } catch (error) { return error.message; }
}

// FICTIONAL method: never shipped by the backend. If the form renders from
// this fixture, the frontend provably has no per-method knowledge.
const FICTIONAL = {
  name: 'blend_x1', label: 'Fictional blend', required_standardization: null, available: true,
  params: [
    { name: 'alpha', label: 'Alpha', type: 'float', required: true, default: 0.25, minimum: 0, maximum: 1, choices: [], help: 'mixing share' },
    { name: 'lookback', label: 'Lookback', type: 'int', required: false, default: 20, minimum: 1, maximum: 250, choices: [], help: '' },
    { name: 'demean', label: 'Demean', type: 'bool', required: false, default: true, minimum: null, maximum: null, choices: [], help: '' },
    { name: 'bucket', label: 'Bucket', type: 'enum', required: false, default: 'med', minimum: null, maximum: null, choices: ['low', 'med', 'high'], help: '' },
    { name: 'w', label: 'W', type: 'weights', required: true, default: null, minimum: null, maximum: null, choices: [], help: 'one per factor' }
  ]
};
const PINNED = { ...FICTIONAL, name: 'blend_x2', required_standardization: 'std_b' };
const CATALOG = {
  standardizations: [
    { name: 'std_a', label: 'Std A', params: [] },
    { name: 'std_b', label: 'Std B', params: [] }
  ],
  methods: [FICTIONAL, PINNED, { name: 'reserved_x', label: 'Reserved later', required_standardization: null, available: false, params: [] }],
  coverage: { default_rule: 'complete_case_require_all', default_min_factor_coverage: null }
};

// --- dynamic form from a fictional method (zero hardcoding) ---------------
{
  const html = mod.renderParamsFormHtml(FICTIONAL, ['FTR_A', 'FTR_B'], {});
  check('form.float_bounds_default', html.includes('data-param-name="alpha"') && html.includes('min="0"') && html.includes('max="1"') && html.includes('value="0.25"') && html.includes('step="any"'));
  check('form.int_step_default', html.includes('data-param-name="lookback"') && html.includes('step="1"') && html.includes('value="20"'));
  check('form.bool_checkbox_default', html.includes('type="checkbox"') && html.includes('data-param-name="demean"') && html.includes('checked'));
  check('form.enum_choices_selected', html.includes('data-param-name="bucket"') && html.includes('<option value="low">') && html.includes('<option value="med" selected>') && html.includes('<option value="high">'));
  check('form.weights_per_checked_factor', (html.match(/data-weight-factor=/g) || []).length === 2 && html.includes('data-weight-factor="FTR_A"') && html.includes('data-weight-factor="FTR_B"'));
  check('form.help_text', html.includes('mixing share'));
  check('form.required_marked', html.includes('（必填）'));
}
// weights inputs TRACK the checked-factor set.
{
  const three = mod.renderParamsFormHtml(FICTIONAL, ['FTR_A', 'FTR_B', 'FTR_C'], {});
  check('form.weights_track_selection', (three.match(/data-weight-factor=/g) || []).length === 3 && three.includes('data-weight-factor="FTR_C"'));
  const zero = mod.renderParamsFormHtml(FICTIONAL, [], {});
  check('form.weights_empty_note', (zero.match(/data-weight-factor=/g) || []).length === 0 && zero.includes('先勾选参与合成的因子'));
}
// regeneration preserves already-typed values.
{
  const html = mod.renderParamsFormHtml(FICTIONAL, ['FTR_A', 'FTR_B'], { alpha: '0.9', w: { FTR_A: '2' } });
  check('form.preserves_values', html.includes('value="0.9"') && html.includes('data-weight-factor="FTR_A" value="2"') && html.includes('data-weight-factor="FTR_B" value=""'));
}
// weights inputs are labeled by factor NAME (picker refs), with the raw id
// kept visible only when it differs; the request key stays the factor_id.
{
  const named = mod.renderParamsFormHtml(FICTIONAL, [
    { factor_id: 'FTR_A', name: 'alpha one' },
    { factor_id: 'FTR_B', name: 'FTR_B' }
  ], {});
  check('form.weights_labeled_by_name', named.includes('W（必填） · alpha one') && named.includes('data-weight-factor="FTR_A"'));
  check('form.weights_id_note_only_when_name_differs',
    named.includes('<span class="synth-weight-id">FTR_A</span>') && !named.includes('<span class="synth-weight-id">FTR_B</span>'));
}
// an UNKNOWN ParamSpec.type renders a labeled unsupported notice (escaped
// type), never a fabricated number field (B-MINOR-1).
{
  const method = { name: 'blend_x3', label: 'X3', required_standardization: null, available: true, params: [
    { name: 'mystery', label: 'Mystery', type: 'matrix<&>', required: false, default: null, minimum: null, maximum: null, choices: [], help: 'opaque' }
  ]};
  const html = mod.renderParamsFormHtml(method, ['FTR_A', 'FTR_B'], {});
  check('form.unknown_type_notice', html.includes('不支持的参数类型') && html.includes('matrix&lt;&amp;&gt;') && !html.includes('matrix<&>'));
  check('form.unknown_type_no_number_input', !html.includes('type="number"') && !html.includes('data-param-type="matrix'));
  check('form.unknown_type_labeled', html.includes('Mystery'));
}

// --- run-button why-disabled honesty (single primary action) ---------------
{
  check('hint.initial_state', mod.runReadinessHintText({ checkedCount: 0, methodReady: false, hasCatalog: false, jobRunning: false })
    === '已选 0 个因子，运行需至少勾选 2 个；方法目录尚未加载或不可用。');
  check('hint.needs_more_factors', mod.runReadinessHintText({ checkedCount: 1, methodReady: true, hasCatalog: true, jobRunning: false })
    === '已选 1 个因子，运行需至少勾选 2 个。');
  check('hint.catalog_without_available_method', mod.runReadinessHintText({ checkedCount: 2, methodReady: false, hasCatalog: true, jobRunning: false })
    === '方法目录中没有可用的合成方法。');
  check('hint.job_running', mod.runReadinessHintText({ checkedCount: 2, methodReady: true, hasCatalog: true, jobRunning: true }).includes('运行中'));
  check('hint.ready_is_silent', mod.runReadinessHintText({ checkedCount: 2, methodReady: true, hasCatalog: true, jobRunning: false }) === '');
  // B-MINOR-4: an unpinned method with no usable standardization is a stated
  // not-ready reason, not a silently-enabled run.
  check('hint.standardization_missing', mod.runReadinessHintText({ checkedCount: 2, methodReady: true, standardizationReady: false, hasCatalog: true, jobRunning: false })
    === '所选方法需要标准化，但标准化目录为空或不可用。');
  check('hint.standardization_ready_silent', mod.runReadinessHintText({ checkedCount: 2, methodReady: true, standardizationReady: true, hasCatalog: true, jobRunning: false }) === '');
}

// --- method + standardization selects --------------------------------------
{
  const html = mod.renderMethodSelectHtml(CATALOG, null);
  check('method.first_available_selected', html.includes('<option value="blend_x1" selected>'));
  check('method.reserved_disabled', html.includes('value="reserved_x"') && / disabled>[^<]*（预留）/.test(html));
  const unpinned = mod.renderStandardizationHtml(CATALOG, FICTIONAL, null);
  check('std.unpinned_enabled', !unpinned.includes('disabled') && unpinned.includes('<option value="std_a" selected>') && unpinned.includes('value="std_b"'));
  const pinned = mod.renderStandardizationHtml(CATALOG, PINNED, null);
  check('std.pinned_disabled', pinned.includes('disabled') && pinned.includes('data-pinned="std_b"') && pinned.includes('（方法固定）'));
  check('std.pinned_omission_note', pinned.includes('省略 standardization'));
}

// --- factor picker: explicit direction, dsl formula, precomputed pill ------
{
  const html = mod.renderFactorPickerHtml([
    { factor_id: 'FTR_A', name: 'alpha one', formula: 'rank(volume)', status: 'active', horizon_days: 5 },
    { factor_id: 'FTR_B', name: 'pc two', formula: 'precomputed:pc_key', status: 'draft', horizon_days: 5 }
  ]);
  check('picker.checkbox_per_factor', (html.match(/synth-factor-check/g) || []).length >= 2);
  check('picker.direction_default_plus1', (html.match(/<option value="1" selected>/g) || []).length === 2);
  check('picker.direction_minus1_offered', (html.match(/<option value="-1">/g) || []).length === 2);
  check('picker.formula_highlighted', html.includes('dsl-fn'));
  check('picker.precomputed_key_not_highlighted', html.includes('<span class="pill">precomputed</span> pc_key'));
  check('picker.status_pills', html.includes('status-pill--ok') && html.includes('status-pill--neutral'));
  check('picker.checkbox_carries_name', html.includes('data-factor-name="alpha one"'));
  check('picker.direction_labeled_by_name', html.includes('aria-label="方向 alpha one"'));
}
// picker PRESERVES selection + directions across an invalidate/refresh
// (B-MAJOR-1): a completed job re-renders the list to surface new factors,
// but a prior {checked, direction} per factor_id is re-applied; a vanished
// factor_id is silently dropped; a factor with no prior entry keeps the
// defaults (unchecked, +1).
{
  const factors = [
    { factor_id: 'FTR_A', name: 'alpha one', formula: 'rank(volume)', status: 'active', horizon_days: 5 },
    { factor_id: 'FTR_B', name: 'pc two', formula: 'precomputed:pc_key', status: 'draft', horizon_days: 5 },
    { factor_id: 'FTR_C', name: 'gamma', formula: 'rank(close)', status: 'active', horizon_days: 5 }
  ];
  const preserved = {
    FTR_A: { checked: true, direction: -1 },
    FTR_B: { checked: false, direction: 1 },
    FTR_GONE: { checked: true, direction: -1 }
  };
  const html = mod.renderFactorPickerHtml(factors, preserved);
  const rowA = html.slice(html.indexOf('data-factor-id="FTR_A"'), html.indexOf('data-factor-id="FTR_B"'));
  check('picker.preserve_checked', rowA.includes('data-factor-name="alpha one" checked'));
  check('picker.preserve_direction_minus1', rowA.includes('<option value="-1" selected>') && !rowA.includes('<option value="1" selected>'));
  const rowC = html.slice(html.indexOf('data-factor-id="FTR_C"'));
  check('picker.default_when_no_prior', !/data-factor-name="gamma" checked/.test(rowC) && rowC.includes('<option value="1" selected>') && !rowC.includes('<option value="-1" selected>'));
  check('picker.vanished_factor_dropped', !html.includes('FTR_GONE'));
  const fresh = mod.renderFactorPickerHtml(factors);
  check('picker.no_preserved_all_default', (fresh.match(/<option value="1" selected>/g) || []).length === 3 && !fresh.includes(' checked'));
}

// --- degraded methods catalog: explicit, escaped, never empty --------------
{
  const html = mod.renderMethodsUnavailableHtml('unknown API path: /api/synthesis/methods <&>');
  check('degraded.labeled', html.includes('方法目录不可用'));
  check('degraded.reason_escaped', html.includes('unknown API path: /api/synthesis/methods &lt;&amp;&gt;') && !html.includes('<&>'));
}

// --- holding_days guard (required; prefill is a suggestion only) -----------
{
  check('holding.empty_rejected', throws(() => mod.validateHoldingDaysInput('')) !== null);
  check('holding.zero_rejected', throws(() => mod.validateHoldingDaysInput('0')) !== null);
  check('holding.fraction_rejected', throws(() => mod.validateHoldingDaysInput('2.5')) !== null);
  check('holding.accepts_positive_int', mod.validateHoldingDaysInput('5') === 5 && mod.validateHoldingDaysInput(5) === 5);
}

// --- request builder mirrors §8.2 + the B3 pinned-standardization rule -----
{
  const base = {
    factors: [{ factor_id: 'FTR_A', direction: 1 }, { factor_id: 'FTR_B', direction: -1 }],
    method: FICTIONAL,
    methodParams: { alpha: 0.25, w: { FTR_A: 2, FTR_B: 1 } },
    standardization: 'std_a',
    parameters: { holding_days: 5 }
  };
  const body = mod.buildRunRequest(base);
  check('request.shape', body.synthesis.method === 'blend_x1' && body.standardization.method === 'std_a' && body.parameters.holding_days === 5);
  check('request.directions_explicit', body.factor_refs[0].direction === 1 && body.factor_refs[1].direction === -1);
  check('request.raw_weights_passthrough', body.synthesis.params.w.FTR_A === 2 && body.synthesis.params.w.FTR_B === 1);
  const pinnedBody = mod.buildRunRequest({ ...base, method: PINNED, standardization: null });
  check('request.pinned_omits_standardization', !('standardization' in pinnedBody));
  check('request.min_two_factors', throws(() => mod.buildRunRequest({ ...base, factors: base.factors.slice(0, 1) })) !== null);
  check('request.direction_must_be_unit', throws(() => mod.buildRunRequest({ ...base, factors: [{ factor_id: 'FTR_A', direction: 0 }, { factor_id: 'FTR_B', direction: 1 }] })) !== null);
  check('request.holding_required', throws(() => mod.buildRunRequest({ ...base, parameters: {} })) !== null);
  check('request.weights_cover_selection', throws(() => mod.buildRunRequest({ ...base, methodParams: { alpha: 0.25, w: { FTR_A: 2 } } })) !== null);
  check('request.weights_finite', throws(() => mod.buildRunRequest({ ...base, methodParams: { alpha: 0.25, w: { FTR_A: 2, FTR_B: 'x' } } })) !== null);
  check('request.required_param_enforced', throws(() => mod.buildRunRequest({ ...base, methodParams: { w: { FTR_A: 2, FTR_B: 1 } } })) !== null);
  check('request.unpinned_needs_standardization', throws(() => mod.buildRunRequest({ ...base, standardization: null })) !== null);
}

// --- report render: provenance / validity / per-role coverage --------------
const REPORT = {
  factor: { factor_id: 'MFC_1A2B3C4D', name: 'multi_factor_composite', formula: 'composite: blend_x1(std_b) of 2 factors', horizon_days: 5, universe_filters: [], source: 'synthesis' },
  parameters: { holding_days: 5 },
  evaluation: {
    rank_ic_mean: 0.031, rank_ic_mean_status: 'available',
    rank_icir: null, rank_icir_status: 'insufficient_sample',
    rank_ic_t_stat: null, rank_ic_t_stat_status: 'insufficient_sample',
    ic_days: 120, coverage: 0.9,
    simulation_profile: { test_period_start: '2020-01-01', test_period_end: '2020-12-31', execution_delay_days: 1 },
    metrics: {}, split_metrics: [], horizon_metrics: [], warnings: [], warning_codes: [],
    coverage_lineage: {}, artifact_path: 'MFC_1A2B3C4D_evaluation.json'
  },
  in_sample_backtest: {
    net_cumulative_return: 0.04, completed_periods: 12, exposure_days: 240,
    sample_role: 'in_sample_backtest',
    simulation_profile: { test_period_start: '2020-01-01', test_period_end: '2020-12-31' },
    metrics: {}, group_returns: [], segment_metrics: [], assumptions: [], warnings: [], warning_codes: [],
    artifact_path: 'MFC_1A2B3C4D_backtest_is.json'
  },
  backtest: {
    periods: 3, completed_periods: 3, holding_days: 5, net_cumulative_return: 0.02, exposure_days: 60,
    sample_role: 'external_oos_backtest',
    simulation_profile: { test_period_start: '2021-01-01', test_period_end: '2021-06-30', top_quantile: 0.3, execution_delay_days: 1, decay_days: 0 },
    metrics: {}, group_returns: [], segment_metrics: [], assumptions: [], warnings: [], warning_codes: [],
    artifact_path: 'MFC_1A2B3C4D_backtest.json'
  },
  synthesis_provenance: {
    factors: [
      { factor_id: 'FTR_A', direction: 1, source: 'catalog' },
      { factor_id: 'FTR_B', direction: -1, source: 'catalog' }
    ],
    directions: { FTR_A: 1, FTR_B: -1 },
    method: 'blend_x1', method_params: { w: { FTR_A: 2, FTR_B: 1 } },
    standardization: 'std_b', standardization_params: {}, standardization_pinned_by_method: true,
    coverage_rule: 'complete_case_require_all', min_factor_coverage: 2,
    rows_required: 1920, rows_full_coverage: 1560,
    coverage_by_role: {
      research_evaluation: {
        rows_required: 1920, rows_full_coverage: 1560,
        coverage: [
          { factor_id: 'FTR_A', direction: 1, source: 'catalog', rows_scored: 1620, rows_in_composite: 1560, coverage_ratio: null },
          { factor_id: 'FTR_B', direction: -1, source: 'catalog', rows_scored: 1860, rows_in_composite: 1560, coverage_ratio: 0.8 }
        ]
      },
      external_oos_backtest: {
        rows_required: 400, rows_full_coverage: 380,
        coverage: [
          { factor_id: 'FTR_A', direction: 1, source: 'catalog', rows_scored: 390, rows_in_composite: 380, coverage_ratio: 0.95 },
          { factor_id: 'FTR_B', direction: -1, source: 'catalog', rows_scored: 400, rows_in_composite: 380, coverage_ratio: 0.97 }
        ]
      }
    },
    weights_effective: { FTR_A: 2, FTR_B: 1 },
    composite_id: 'MFC_1A2B3C4D', is_fitted: false
  },
  validity: {
    basis: 'a_priori_declared_weights',
    message: 'Weights and method were chosen a priori, not fitted; treat IS/OOS with the same discipline as any single factor.',
    caveats: ['coverage drop recorded for one factor', '<script>alert(1)</script>']
  }
};
{
  const html = mod.renderSynthesisReportHtml(REPORT);
  check('report.validity_banner', html.includes('先验声明') && html.includes('a_priori_declared_weights') && html.includes('chosen a priori'));
  check('report.caveats_rendered', html.includes('coverage drop recorded for one factor'));
  check('report.caveat_escaped', html.includes('&lt;script&gt;alert(1)&lt;/script&gt;') && !html.includes('<script>alert(1)</script>'));
  check('report.directions_explicit', html.includes('方向 +1') && html.includes('方向 -1'));
  check('report.method_and_pinned_note', html.includes('方法 blend_x1') && html.includes('标准化 std_b') && html.includes('方法固定标准化'));
  check('report.raw_weights_echoed', html.includes('FTR_A 权重 2') && html.includes('FTR_B 权重 1') && html.includes('未做归一化展示'));
  check('report.weights_not_normalized', !html.includes('0.6667') && !html.includes('0.3333') && !html.includes('66.67%'));
  // Role headings carry the reused sections' exact wording with the raw
  // server key kept visible; unknown keys would render as-is.
  check('report.coverage_roles',
    html.includes('覆盖 · 样本内研究评价（research_evaluation）')
    && html.includes('覆盖 · 外部样本外组合评测（external_oos_backtest）'));
  check('report.coverage_null_ratio_na', html.includes('<td>n/a</td>') && html.includes('80.00%'));
  check('report.coverage_counts', html.includes('rows_required 1920') && html.includes('rows_full_coverage 1560'));
  check('report.is_fitted_only_as_banner', html.includes('先验声明 · 未拟合') && !html.includes('is_fitted'));
  check('report.withheld_metric_label', html.includes('insufficient_sample'));
  check('report.sections_reused', html.includes('样本内研究评价') && html.includes('样本内组合回测') && html.includes('外部样本外组合评测'));
  check('report.section_ids_rehosted',
    html.includes('id="synth-evaluation"') && html.includes('id="synth-insample"') && html.includes('id="synth-oos"')
    && html.includes('id="synth-diagnostics"') && html.includes('id="synth-evidence"') && html.includes('id="synth-artifacts"'));
  check('report.no_duplicate_report_ids',
    !html.includes('id="report-evaluation"') && !html.includes('id="report-insample"') && !html.includes('id="report-oos"')
    && !html.includes('id="report-diagnostics"') && !html.includes('id="report-evidence"') && !html.includes('id="report-artifacts"'));
  check('report.composite_hero', html.includes('MFC_1A2B3C4D') && html.includes('2 因子'));
  check('report.artifact_basenames', html.includes('MFC_1A2B3C4D_evaluation.json') && html.includes('MFC_1A2B3C4D_backtest.json'));
}
// Coverage fallbacks: flat spec-§8.2 list under one table; absent coverage
// is stated as unobservable, never invented.
{
  const flat = mod.renderCoverageByRoleHtml(null, [
    { factor_id: 'FTR_A', direction: 1, source: 'catalog', rows_scored: 10, rows_in_composite: 8, coverage_ratio: null }
  ]);
  check('coverage.flat_fallback', flat.includes('覆盖 · 合成总体') && flat.includes('<td>n/a</td>'));
  const absent = mod.renderCoverageByRoleHtml(null, null);
  check('coverage.absent_is_na', absent.includes('覆盖明细不可观测'));
}

console.log('FIXTURE RESULT: ' + failed + ' failed');
if (failed) process.exit(1);
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node runtime not available")
def test_node_fixture_driven_renderers(tmp_path) -> None:
    harness = tmp_path / "synthesis_fixtures.mjs"
    harness.write_text(_FIXTURE_HARNESS, encoding="utf-8")
    env = {"QF_SYNTH_URL": SYNTHESIS_PATH.resolve().as_uri()}
    result = subprocess.run(
        ["node", str(harness)],
        capture_output=True,
        text=True,
        env={**dict(__import__("os").environ), **env},
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    assert "FIXTURE RESULT: 0 failed" in result.stdout
    # Spot-check the load-bearing lines actually ran (not skipped).
    for marker in (
        "PASS form.float_bounds_default",
        "PASS form.weights_per_checked_factor",
        "PASS form.weights_track_selection",
        "PASS form.preserves_values",
        "PASS form.weights_labeled_by_name",
        "PASS form.weights_id_note_only_when_name_differs",
        "PASS hint.initial_state",
        "PASS hint.ready_is_silent",
        "PASS picker.direction_labeled_by_name",
        "PASS method.reserved_disabled",
        "PASS std.pinned_disabled",
        "PASS picker.direction_default_plus1",
        "PASS degraded.labeled",
        "PASS holding.empty_rejected",
        "PASS request.pinned_omits_standardization",
        "PASS request.weights_cover_selection",
        "PASS report.raw_weights_echoed",
        "PASS report.coverage_null_ratio_na",
        "PASS report.is_fitted_only_as_banner",
        "PASS report.no_duplicate_report_ids",
        "PASS coverage.absent_is_na",
        # CP10 frontend follow-up pins.
        "PASS picker.preserve_checked",
        "PASS picker.preserve_direction_minus1",
        "PASS picker.vanished_factor_dropped",
        "PASS picker.no_preserved_all_default",
        "PASS form.unknown_type_notice",
        "PASS form.unknown_type_no_number_input",
        "PASS hint.standardization_missing",
        "PASS hint.standardization_ready_silent",
    ):
        assert marker in result.stdout, result.stdout

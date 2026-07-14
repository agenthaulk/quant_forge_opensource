"""Contract tests for the P0 agent-sidecar mode shell (simple landing +
expert workbench toggle), per ``docs/design/agent_sidecar_frontend.md`` §5.6
and ``docs/design/WORKORDER_frontend_sidecar_p0_p3.md`` §5 P0.

P0 is pure frontend (zero backend changes, zero new endpoints): a simple-mode
landing (idea box + example seeds + start button + one-line runtime status)
sits beside the existing six-tab expert workbench, which is now taggable and
toggled via the ``hidden`` attribute instead of being the only shell. Three
layers, mirroring the project's existing web-test conventions
(``docs/frontend_contributing.md``):

- served-page structure pins (``web_server._index_html``) for the new shells,
  the mode toggle, the advanced-params disclosure, and terminology tooltips;
- string-contract pins on ``app.js`` for the mode-precedence algorithm
  (recognized expert deep link > saved preference > default simple) and on
  ``views/lab.js`` for the read-only hash-recognition predicate it exports;
- a stdlib Node smoke test that imports the REAL ``views/lab.js`` module and
  exercises ``isRecognizedExpertHash`` against the full hash vocabulary
  ``applyHash`` already routes (tabs, modules, workbench anchors, legacy
  aliases, and the registry/docs/extensions per-item deep links).

No test in this module touches ``apps/web/api.py`` or adds a new endpoint;
P0's binding scope is ``html.py`` / ``app.js`` / ``views/lab.js`` only.
"""

from __future__ import annotations

import re
import shutil
import subprocess

import pytest

import quant_forge.apps.web.server as web_server
from quant_forge.config import QuantForgeConfig
from quant_forge.data.local import create_demo_workspace


LAB_JS_PATH = web_server.STATIC_ROOT / "views" / "lab.js"


def _static_module_text(name: str) -> str:
    return (web_server.STATIC_ROOT / name).read_text(encoding="utf-8")


@pytest.fixture()
def web_config(tmp_path):
    create_demo_workspace(tmp_path / "demo")
    return QuantForgeConfig().resolve(tmp_path / "demo")


# ---------------------------------------------------------------------------
# Served page: simple shell, expert shell, mode toggle, advanced-params,
# terminology tooltips
# ---------------------------------------------------------------------------


def test_index_page_hosts_the_simple_shell_and_mode_toggle(web_config) -> None:
    html = web_server._index_html(web_config)
    for marker in (
        'id="simple-shell"',
        'aria-label="简洁模式"',
        'class="mode-toggle" role="group" aria-label="界面模式"',
        'id="mode-simple-btn" class="mode-toggle-btn" aria-pressed="true"',
        'id="mode-expert-btn" class="mode-toggle-btn" aria-pressed="false"',
        '简洁模式',
        '专家模式',
        'id="simple-idea"',
        'for="simple-idea" class="sr-only"',
        'class="simple-seeds"',
        'class="simple-seed-btn"',
        'data-seed-text=',
        'id="simple-run"',
        'id="simple-runtime-status"',
    ):
        assert marker in html, marker
    # Example seeds: at least the three server-rendered starter ideas.
    assert html.count('class="simple-seed-btn"') == 3


def test_index_page_tags_the_existing_workbench_as_the_expert_shell(web_config) -> None:
    html = web_server._index_html(web_config)
    # The pre-existing app-shell markup is untouched (FE-L1 thin shell, no
    # rewrite) except for gaining an id and starting hidden: the six-tab
    # workbench, control rail, and every existing mount id still exist.
    assert '<main id="expert-shell" class="app-shell" hidden>' in html
    assert 'class="control-rail"' in html
    assert 'class="lab-tabs" role="tablist"' in html
    # Document order: simple shell first (default landing), expert shell
    # second, so the simple experience is what a no-JS-yet paint shows.
    assert html.index('id="simple-shell"') < html.index('id="expert-shell"')
    assert html.index('id="expert-shell"') < html.index('class="control-rail"')
    # Expert shell starts hidden server-side (mirrors the existing
    # non-default lab-panel-* pattern); JS decides the real landing mode.
    expert_start = html.index('id="expert-shell"')
    assert "hidden" in html[expert_start : html.index(">", expert_start)]
    simple_start = html.index('id="simple-shell"')
    assert "hidden" not in html[simple_start : html.index(">", simple_start)]


def test_simple_and_expert_idea_fields_share_the_same_seeded_default_text(web_config) -> None:
    html = web_server._index_html(web_config)
    # One Python source for the seed text (component contract 5.6: "one
    # shared draft"); both textareas render the identical string.
    default_text = "非ST的小市值股票未来表现更好"
    assert f'<textarea id="simple-idea" aria-describedby="simple-runtime-status">{default_text}</textarea>' in html
    assert f'<textarea id="idea">{default_text}</textarea>' in html


def test_advanced_params_details_wraps_the_11_parameter_grid(web_config) -> None:
    html = web_server._index_html(web_config)
    assert '<details class="advanced-params" id="advanced-params">' in html
    assert "<summary>高级参数" in html
    details_start = html.index('id="advanced-params"')
    controls_start = html.index('id="validation-controls"', details_start)
    details_end = html.index("</details>", controls_start)
    # The full existing 11-parameter grid lives INSIDE the disclosure, with
    # every input id and its disabled-by-default state unchanged (P0 does
    # no deletions; the grid is absorbed into a collapsed section, not
    # rewritten).
    for param_id in (
        "param-holding-days",
        "param-decay-days",
        "param-top-quantile",
        "param-delay-days",
        "param-evaluation-start",
        "param-evaluation-end",
        "param-backtest-start",
        "param-backtest-end",
        "param-commission-bps",
        "param-slippage-bps",
        "param-short-borrow-bps",
    ):
        marker_start = html.index(f'id="{param_id}"', controls_start)
        assert marker_start < details_end, param_id
        assert "disabled" in html[marker_start : html.index(">", marker_start)], param_id


def test_terminology_tooltips_cover_the_jargon_labelled_parameters(web_config) -> None:
    html = web_server._index_html(web_config)
    # A term-tip is a focusable span carrying a plain-language data-tip; it
    # wraps the SAME visible label text as before (no string-contract
    # regression on the label wording), just adds an explanation surface.
    # Each is asserted as one exact, distinct span so five separate
    # attachment points are proven (not one match re-found five times).
    exact_spans = (
        '<span class="term-tip" tabindex="0" data-tip="每次调仓后，持有多头组合的交易日数">持有期 / 天</span>',
        '<span class="term-tip" tabindex="0" data-tip="信号衰减天数：0 表示不衰减，数值越大权重越平滑">Decay / 天</span>',
        '<span class="term-tip" tabindex="0" data-tip="按因子值排序后，用于构建多头组合的头部比例">Top Quantile</span>',
        '<span class="term-tip" tabindex="0" data-tip="信号生成到实际下单之间的执行延迟天数">Delay / 天</span>',
        '<span class="term-tip" tabindex="0" data-tip="做空部分的年化融券成本，以基点计">融券成本 bps/年</span>',
    )
    assert len(set(exact_spans)) == 5, "test fixture bug: expected 5 distinct spans"
    for span in exact_spans:
        assert span in html, span
    assert html.count('class="term-tip"') == 5
    # Untouched labels (no jargon ambiguity) keep their plain <span>, never
    # gaining an unnecessary tooltip.
    for plain_label in ("评测开始", "评测结束", "回测开始", "回测结束", "手续费 bps", "滑点 bps"):
        assert f"<span>{plain_label}</span>" in html, plain_label


# ---------------------------------------------------------------------------
# No duplicate DOM ids anywhere on the served page (general regression pin)
# ---------------------------------------------------------------------------


def test_index_page_has_no_duplicate_dom_ids(web_config) -> None:
    html = web_server._index_html(web_config)
    ids = re.findall(r'\bid="([^"]+)"', html)
    assert ids, "expected at least one id= attribute on the served page"
    counts: dict[str, int] = {}
    for value in ids:
        counts[value] = counts.get(value, 0) + 1
    duplicates = {value: count for value, count in counts.items() if count > 1}
    assert duplicates == {}, f"duplicate DOM ids: {duplicates}"


# ---------------------------------------------------------------------------
# Theme-token discipline + 375px overflow-safety proxy for the new CSS
# ---------------------------------------------------------------------------


def test_mode_shell_css_is_token_only_and_ships_both_themes(web_config) -> None:
    html = web_server._index_html(web_config)
    block_start = html.index("/* P0 mode shell (agent_sidecar_frontend.md")
    block_end = html.index(".control-rail {", block_start)
    block = html[block_start:block_end]
    assert "var(--" in block
    # No hardcoded hex colors: every color comes from a token, so both the
    # light :root block and the prefers-color-scheme: dark block cover it.
    assert not re.search(r"#[0-9a-fA-F]{3,6}\b", block)
    for rule in (
        ".simple-shell",
        ".mode-toggle",
        ".mode-toggle-btn",
        ".simple-idea-panel",
        ".simple-seeds",
        ".simple-seed-btn",
        ".advanced-params",
        ".term-tip",
    ):
        assert rule in block, rule


def test_mode_shell_css_stays_overflow_safe_at_narrow_widths(web_config) -> None:
    html = web_server._index_html(web_config)
    block_start = html.index("/* P0 mode shell (agent_sidecar_frontend.md")
    block_end = html.index(".control-rail {", block_start)
    block = html[block_start:block_end]
    # Fluid width (never a fixed px wider than a mobile viewport) plus
    # wrapping rows so the mode toggle and seed chips reflow instead of
    # overflowing at 375px; the existing app-wide `* { box-sizing: border-box; }`
    # and `textarea, select, input { width: 100%; }` rules already cover the
    # inherited form controls.
    assert "max-width: 640px" in block
    assert "box-sizing: border-box" in block
    assert "flex-wrap: wrap" in block
    assert "@media (max-width: 480px)" in block


def test_hidden_attribute_override_beats_both_shells_own_display_grid(web_config) -> None:
    # Regression pin (found via live browser verification, not by any
    # existing test): #simple-shell and #expert-shell each declare their
    # own `display: grid`, which sits at the SAME specificity as the
    # browser's default `[hidden] { display: none; }` rule. Author styles
    # win over user-agent styles at equal specificity regardless of source
    # order, so without an explicit higher-specificity override the
    # `hidden` attribute app.js toggles would have NO visual effect and
    # both shells would render stacked at all times. This mirrors the
    # documented .lab-tab-dot[hidden] precedent elsewhere in this file.
    html = web_server._index_html(web_config)
    assert "#simple-shell[hidden], #expert-shell[hidden] {" in html
    override_start = html.index("#simple-shell[hidden], #expert-shell[hidden] {")
    override_end = html.index("}", override_start)
    assert "display: none;" in html[override_start:override_end]
    # Correctness here rests on SPECIFICITY, not source order (CSS origin
    # outranks source order: author `display: grid` beats the UA `[hidden]`
    # rule at equal 0-1-0 specificity regardless of which is declared
    # later) — the override selector must combine an id with the [hidden]
    # attribute (0-1-1 per compound, decisively above the plain 0-1-0
    # class rules), and the file avoids !important entirely, matching the
    # existing .lab-tab-dot[hidden] precedent's approach.
    assert "!important" not in html
    assert ".app-shell {" in html
    assert ".simple-shell {" in html


# ---------------------------------------------------------------------------
# app.js: mode-precedence algorithm (string-contract pins)
# ---------------------------------------------------------------------------


def test_app_module_imports_the_hash_recognizer_from_lab_module() -> None:
    app_js = _static_module_text("app.js")
    assert (
        "import { activateModule, activateTab, initLabTabs, isRecognizedExpertHash, setStep, setTabDot } "
        "from './views/lab.js';" in app_js
    )


def test_mode_shell_dom_refs_bind_to_the_new_html_elements() -> None:
    app_js = _static_module_text("app.js")
    for ref in (
        "const simpleShell = document.getElementById('simple-shell');",
        "const expertShell = document.getElementById('expert-shell');",
        "const modeSimpleBtn = document.getElementById('mode-simple-btn');",
        "const modeExpertBtn = document.getElementById('mode-expert-btn');",
        "const ideaEl = document.getElementById('idea');",
        "const simpleIdeaEl = document.getElementById('simple-idea');",
        "const simpleRunButton = document.getElementById('simple-run');",
        "const advancedParamsDetails = document.getElementById('advanced-params');",
    ):
        assert ref in app_js, ref


def test_mode_precedence_deep_link_beats_saved_preference_beats_default() -> None:
    # Component contract 5.6: "recognized expert deep link > saved mode
    # preference > default simple. A deep link wins that navigation without
    # rewriting the saved preference."
    app_js = _static_module_text("app.js")
    assert "const MODE_STORAGE_KEY = 'qf_ui_mode';" in app_js
    assert "function readSavedMode() {" in app_js
    assert "function writeSavedMode(mode) {" in app_js
    assert "function applyMode(mode) {" in app_js
    assert "function setMode(mode) {" in app_js
    # setMode both applies AND persists; applyMode alone never persists.
    set_mode_start = app_js.index("function setMode(mode) {")
    set_mode_end = app_js.index("\n}", set_mode_start)
    set_mode_body = app_js[set_mode_start:set_mode_end]
    assert "applyMode(mode);" in set_mode_body
    assert "writeSavedMode(mode);" in set_mode_body
    apply_mode_start = app_js.index("function applyMode(mode) {")
    apply_mode_end = app_js.index("\n}", apply_mode_start)
    apply_mode_body = app_js[apply_mode_start:apply_mode_end]
    assert "writeSavedMode" not in apply_mode_body
    assert "localStorage" not in apply_mode_body
    # The initial landing decision calls applyMode directly (never setMode)
    # when the hash is a recognized expert deep link, so that path can never
    # write the saved preference; readSavedMode only otherwise, defaulting
    # to 'simple'.
    assert "const deepLinkIsExpert = isRecognizedExpertHash(window.location.hash);" in app_js
    assert (
        "applyMode(deepLinkIsExpert ? 'expert' : (readSavedMode() || 'simple'));" in app_js
    )
    initial_landing = app_js.index("const deepLinkIsExpert = isRecognizedExpertHash(window.location.hash);")
    assert "setMode(" not in app_js[initial_landing : app_js.index(
        "applyMode(deepLinkIsExpert ? 'expert' : (readSavedMode() || 'simple'));"
    ) + 1]


def test_hashchange_reevaluates_deep_link_precedence_without_a_full_reload() -> None:
    # Regression pin (found via live browser verification): the initial
    # applyMode(...) call above runs ONCE at module top-level evaluation.
    # Editing the URL fragment while the page is already open (or any other
    # same-document hash navigation) fires 'hashchange' WITHOUT re-running
    # that top-level code, so a later recognized expert hash would activate
    # the right TAB inside the expert shell (views/lab.js's own hashchange
    # listener still runs) while the SHELL itself stayed on whichever mode
    # was already showing. A second, independent hashchange listener here
    # re-applies the SAME precedence rule (applyMode only, never setMode,
    # so it still never rewrites the saved preference) whenever a later
    # hash change is itself a recognized expert deep link.
    app_js = _static_module_text("app.js")
    assert (
        "window.addEventListener('hashchange', () => {\n"
        "  if (isRecognizedExpertHash(window.location.hash)) applyMode('expert');\n"
        "});" in app_js
    )
    # Placed after the initial applyMode call, and still never calls
    # setMode/writeSavedMode.
    initial_landing = app_js.index("applyMode(deepLinkIsExpert ? 'expert' : (readSavedMode() || 'simple'));")
    listener_start = app_js.index("window.addEventListener('hashchange', () => {", initial_landing)
    listener_end = app_js.index("});", listener_start)
    assert initial_landing < listener_start
    assert "setMode(" not in app_js[listener_start:listener_end]


def test_local_storage_access_is_guarded_against_failure() -> None:
    # A Storage exception (privacy mode, quota, disabled storage) must
    # degrade gracefully rather than break the whole module: both the read
    # and the write path are wrapped in try/catch.
    app_js = _static_module_text("app.js")
    read_start = app_js.index("function readSavedMode() {")
    read_end = app_js.index("function writeSavedMode(mode) {")
    read_body = app_js[read_start:read_end]
    assert "try {" in read_body
    assert "} catch (error) {" in read_body
    assert "window.localStorage.getItem(MODE_STORAGE_KEY)" in read_body
    write_start = read_end
    write_end = app_js.index("function applyMode(mode) {")
    write_body = app_js[write_start:write_end]
    assert "try {" in write_body
    assert "} catch (error) {" in write_body
    assert "window.localStorage.setItem(MODE_STORAGE_KEY, mode);" in write_body


def test_mode_toggle_buttons_and_seed_buttons_are_wired() -> None:
    app_js = _static_module_text("app.js")
    assert "modeSimpleBtn.addEventListener('click', () => setMode('simple'));" in app_js
    assert "modeExpertBtn.addEventListener('click', () => setMode('expert'));" in app_js
    assert "document.querySelectorAll('.simple-seed-btn').forEach(seedButton => {" in app_js
    assert "simpleIdeaEl.value = seedButton.dataset.seedText || '';" in app_js


def test_simple_run_delegates_to_the_existing_run_handler_without_duplicating_it() -> None:
    # FE-L1 (thin shell, no rewrite): the simple entry point must not
    # duplicate #run's parse/activate/error/panel wiring; it reuses the
    # SAME handler via button.click() after choosing an honest parser mode
    # and switching to the expert view (so the delegated run's output is
    # visible through the existing renderers, never a second canvas).
    app_js = _static_module_text("app.js")
    simple_run_start = app_js.index("simpleRunButton.addEventListener('click', () => {")
    simple_run_end = app_js.index("});", simple_run_start)
    body = app_js[simple_run_start:simple_run_end]
    assert "llmProviderOptions.some(option => option.runtimeReady === 'true')" in body
    assert "document.getElementById('parser').value = anyProviderReady ? 'llm' : 'rule';" in body
    assert "setMode('expert');" in body
    assert "button.click();" in body
    # It must NOT reimplement the #run handler's own body inline (no second
    # copy of its activate/submit/render sequence).
    assert "submitParse(" not in body
    assert "renderParsed(" not in body
    # The disabled state mirrors the delegated button instead of managing
    # its own try/finally bookkeeping.
    assert (
        "new MutationObserver(() => { simpleRunButton.disabled = button.disabled; })" in app_js
    )
    assert ".observe(button, { attributes: true, attributeFilter: ['disabled'] });" in app_js


def test_hydrate_runtime_status_keeps_the_simple_status_line_in_sync() -> None:
    app_js = _static_module_text("app.js")
    hydrate_start = app_js.index("function hydrateRuntimeStatus(status) {")
    hydrate_end = app_js.index("function refreshRuntimeStatus(", hydrate_start)
    body = app_js[hydrate_start:hydrate_end]
    assert "setRuntimeText('simple-runtime-status', `LLM ${llmLabel} · RD ${rdLabel}`);" in body


def test_advanced_params_auto_opens_once_parse_defaults_are_enabled() -> None:
    app_js = _static_module_text("app.js")
    fn_start = app_js.index("function setValidationInputsEnabled(enabled) {")
    fn_end = app_js.index("\n}", fn_start)
    body = app_js[fn_start:fn_end]
    assert "if (enabled) advancedParamsDetails.open = true;" in body


# ---------------------------------------------------------------------------
# app.js: existing pinned handlers stay untouched (P0 must not rewrite them)
# ---------------------------------------------------------------------------


def test_existing_run_handler_body_is_unmodified_by_the_mode_shell() -> None:
    # Regression guard for the delegation design above: the ORIGINAL #run
    # click handler must still contain its own full activate/submit/render
    # sequence verbatim (test_web_lab_view.py already pins the ordering;
    # this just confirms the mode-shell change didn't fork or shadow it).
    app_js = _static_module_text("app.js")
    parse_click = app_js.index("button.addEventListener('click', async () => {")
    validate_click = app_js.index("validateButton.addEventListener('click', async () => {")
    handler = app_js[parse_click:validate_click]
    assert "const payload = await submitParse(parserMode);" in handler
    assert "renderParsed(payload);" in handler
    assert "activateTab('lab-tab-factor');" in handler
    assert "activateModule('lab-module-single');" in handler


# ---------------------------------------------------------------------------
# views/lab.js: read-only hash-recognition predicate (string-contract +
# stdlib Node smoke, mirroring test_web_charts.py's harness pattern)
# ---------------------------------------------------------------------------


def test_lab_module_exports_a_pure_read_only_hash_recognizer() -> None:
    lab_js = _static_module_text("views/lab.js")
    assert "export function isRecognizedExpertHash(hash) {" in lab_js
    fn_start = lab_js.index("export function isRecognizedExpertHash(hash) {")
    fn_end = lab_js.index("\n}", fn_start)
    body = lab_js[fn_start:fn_end]
    # Read-only: reuses applyHash's own recognition surface (tabs, modules,
    # workbench anchors, legacy aliases, registry/docs/extensions per-item
    # hashes) without any of applyHash's navigation side effects.
    assert "TAB_IDS.includes(target)" in body
    assert "MODULE_IDS.includes(target)" in body
    assert "WORKBENCH_ANCHOR_IDS.includes(target)" in body
    assert "REGISTRY_FACTOR_HASH.test(target)" in body
    assert "DOCS_DOC_HASH.test(target)" in body
    assert "EXTENSIONS_MANIFEST_HASH.test(target)" in body
    assert "LEGACY_HASH_ALIASES[target]" in body
    assert "history.replaceState" not in body
    assert "activateTab(" not in body
    assert "activateModule(" not in body
    assert "scrollToReportSection(" not in body
    # applyHash's own pinned contract (test_web_lab_view.py) is untouched:
    # the new function is purely additive, placed after applyHash returns.
    apply_hash_start = lab_js.index("function applyHash(hash) {")
    assert apply_hash_start < fn_start


@pytest.mark.skipif(shutil.which("node") is None, reason="node runtime not available")
def test_node_is_recognized_expert_hash_smoke(tmp_path) -> None:
    harness = tmp_path / "lab_hash_smoke.mjs"
    harness.write_text(_HASH_SMOKE_HARNESS, encoding="utf-8")
    env = {"QF_LAB_URL": LAB_JS_PATH.resolve().as_uri()}
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
        "PASS tab_id",
        "PASS module_id",
        "PASS workbench_anchor",
        "PASS workbench_rd_anchor",
        "PASS legacy_alias_rd",
        "PASS legacy_alias_bench",
        "PASS registry_factor",
        "PASS docs_doc",
        "PASS extensions_manifest",
        "PASS empty_string",
        "PASS hash_only",
        "PASS unknown",
        "PASS undefined_input",
        "PASS no_hash_prefix_still_recognized",
    ):
        assert marker in result.stdout, result.stdout


_HASH_SMOKE_HARNESS = r"""
const url = process.env.QF_LAB_URL;
const mod = await import(url);
const { isRecognizedExpertHash } = mod;

let failed = 0;
function check(name, cond, detail) {
  if (cond) { console.log('PASS ' + name); }
  else { failed++; console.log('FAIL ' + name + (detail ? ': ' + detail : '')); }
}

// Recognized: every hash kind applyHash routes into the expert workbench.
check('tab_id', isRecognizedExpertHash('#lab-tab-registry') === true);
check('module_id', isRecognizedExpertHash('#lab-module-multi') === true);
check('workbench_anchor', isRecognizedExpertHash('#report-hero') === true);
check('workbench_rd_anchor', isRecognizedExpertHash('#workbench-rd') === true);
check('legacy_alias_rd', isRecognizedExpertHash('#lab-tab-rd') === true);
check('legacy_alias_bench', isRecognizedExpertHash('#lab-tab-bench') === true);
check('registry_factor', isRecognizedExpertHash('#registry-factor-FTR_DEMO') === true);
check('docs_doc', isRecognizedExpertHash('#docs-doc-architecture.md') === true);
check('extensions_manifest', isRecognizedExpertHash('#extensions-manifest-my_ext') === true);
// A leading '#' is not required (callers may pass window.location.hash's
// raw fragment either way); the function strips at most one.
check('no_hash_prefix_still_recognized', isRecognizedExpertHash('lab-tab-registry') === true);

// Not recognized: falls through to the saved-preference/default chain.
check('empty_string', isRecognizedExpertHash('') === false);
check('hash_only', isRecognizedExpertHash('#') === false);
check('unknown', isRecognizedExpertHash('#not-a-real-hash') === false);
check('undefined_input', isRecognizedExpertHash(undefined) === false);

console.log('SMOKE RESULT: ' + failed + ' failed');
if (failed) process.exit(1);
"""

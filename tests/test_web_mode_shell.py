"""Contract tests for the P0 agent-sidecar mode shell (simple landing +
expert workbench toggle), per ``docs/design/agent_sidecar_frontend.md`` §5.6
and ``docs/design/WORKORDER_frontend_sidecar_p0_p3.md`` §5 P0.

P0 is pure frontend (zero backend changes, zero new endpoints): a simple-mode
landing (idea box + example seeds + start button + one-line runtime status)
sits beside the existing six-tab expert workbench, which is now taggable and
toggled via the ``hidden`` attribute instead of being the only shell. A
persistent header (outside both shells) hosts the mode toggle so switching
to expert mode never hides the only way back (FE0, phase review). Four
layers, mirroring the project's existing web-test conventions
(``docs/frontend_contributing.md``) plus one added for this review round:

- served-page structure pins (``web_server._index_html``) for the new shells,
  the persistent toggle header, the advanced-params disclosure, and
  terminology tooltips;
- a stdlib ``html.parser`` ancestor-chain check (not a string search) that
  the toggle buttons are real DOM descendants of the header and NOT of
  either shell — the structural half of the FE0 regression, which no flat
  string match or JS-only stub could prove;
- a stdlib Node DOM-stub harness that imports the REAL ``app.js`` (and its
  full static import chain) under an auto-vivifying element/window/
  localStorage stub and DRIVES the real mode-shell lifecycle: default
  landing, toggle round-trips, the simple-run handoff's non-persistence,
  deep-link precedence (fresh load and same-document hashchange), and
  localStorage-throwing resilience. This is the primary behavioral
  evidence for the mode-precedence and persistence contracts; the stub
  models state/event wiring faithfully but NOT CSS layout/paint, so it
  cannot see ancestor-`display:none` cascades (covered by the parser check
  above) or real viewport geometry (covered by live-browser verification,
  recorded in the phase commit message, for the FE2/FE3 pixel claims);
- a handful of string-contract pins for wiring the CSS proxy tests and the
  lab.js hash-recognition predicate cannot otherwise cover (kept minimal
  after the phase review's "most pins are string tautologies" finding —
  see the trimmed set below and the harness above for what replaced them).

No test in this module touches ``apps/web/api.py`` or adds a new endpoint;
P0's binding scope is ``html.py`` / ``app.js`` / ``views/lab.js`` only.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from html.parser import HTMLParser

import pytest

import quant_forge.apps.web.server as web_server
from quant_forge.config import QuantForgeConfig
from quant_forge.data.local import create_demo_workspace


LAB_JS_PATH = web_server.STATIC_ROOT / "views" / "lab.js"
APP_JS_PATH = web_server.STATIC_ROOT / "app.js"

_VOID_ELEMENTS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}


class _ContainmentParser(HTMLParser):
    """Stdlib-only ancestor-chain tracker: for every ``id="..."`` attribute
    encountered, records the set of ids of its OPEN ancestor elements at
    that point. Used to prove real DOM containment (FE0: the mode-toggle
    buttons must be descendants of the persistent header and NEVER of
    either `[hidden]`-toggled shell) without a browser and without a
    string-position heuristic, which cannot distinguish "before this tag
    in the source" from "inside this tag"."""

    def __init__(self) -> None:
        super().__init__()
        self._open_ids: list[str] = []
        self.ancestors_by_id: dict[str, set[str]] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        element_id = dict(attrs).get("id")
        if element_id:
            self.ancestors_by_id[element_id] = set(self._open_ids)
        if tag not in _VOID_ELEMENTS:
            self._open_ids.append(element_id or "")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        element_id = dict(attrs).get("id")
        if element_id:
            self.ancestors_by_id[element_id] = set(self._open_ids)

    def handle_endtag(self, tag: str) -> None:
        if tag not in _VOID_ELEMENTS and self._open_ids:
            self._open_ids.pop()


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
        'id="mode-header"',
        'class="mode-toggle" role="group" aria-label="界面模式"',
        'id="mode-simple-btn" class="mode-toggle-btn" aria-pressed="true"',
        'id="mode-expert-btn" class="mode-toggle-btn" aria-pressed="false"',
        '简洁模式',
        '专家模式',
        'id="simple-shell"',
        'aria-label="简洁模式"',
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
    # Document order: persistent header first (FE0: outside both shells,
    # never hidden), then simple shell (default landing), then expert
    # shell, so the simple experience plus a reachable toggle is what a
    # no-JS-yet paint shows.
    assert html.index('id="mode-header"') < html.index('id="simple-shell"')
    assert html.index('id="simple-shell"') < html.index('id="expert-shell"')
    assert html.index('id="expert-shell"') < html.index('class="control-rail"')
    # The header itself is never `[hidden]` — it's the one thing visible in
    # both modes.
    header_start = html.index('id="mode-header"')
    assert "hidden" not in html[header_start : html.index(">", header_start)]
    # Expert shell starts hidden server-side (mirrors the existing
    # non-default lab-panel-* pattern); JS decides the real landing mode.
    expert_start = html.index('id="expert-shell"')
    assert "hidden" in html[expert_start : html.index(">", expert_start)]
    simple_start = html.index('id="simple-shell"')
    assert "hidden" not in html[simple_start : html.index(">", simple_start)]


def test_mode_toggle_is_a_descendant_of_the_header_never_of_either_shell(web_config) -> None:
    # FE0 (BLOCKING, phase review): the toggle used to be a child of
    # #simple-shell, so applyMode('expert') hid the only way back — a dead
    # end until reload. A REAL ancestor-chain check (stdlib html.parser,
    # not a string/index heuristic that can't distinguish "appears earlier
    # in the source" from "is nested inside") proves the buttons are
    # structurally outside both `[hidden]`-toggled containers.
    html = web_server._index_html(web_config)
    parser = _ContainmentParser()
    parser.feed(html)
    for toggle_id in ("mode-simple-btn", "mode-expert-btn"):
        ancestors = parser.ancestors_by_id.get(toggle_id)
        assert ancestors is not None, f"{toggle_id} never appeared in the parsed document"
        assert "simple-shell" not in ancestors, f"{toggle_id} is nested inside #simple-shell"
        assert "expert-shell" not in ancestors, f"{toggle_id} is nested inside #expert-shell"
        assert "mode-header" in ancestors, f"{toggle_id} is not nested inside #mode-header"
    # Sanity check the parser itself against KNOWN-nested ids, so a bug in
    # the parser (e.g. failing to track nesting at all, which would make
    # the negative assertions above pass vacuously) is caught here instead.
    idea_ancestors = parser.ancestors_by_id.get("idea")
    assert idea_ancestors is not None
    assert "expert-shell" in idea_ancestors, "parser sanity check failed: #idea should be nested inside #expert-shell"
    simple_idea_ancestors = parser.ancestors_by_id.get("simple-idea")
    assert simple_idea_ancestors is not None
    assert "simple-shell" in simple_idea_ancestors, "parser sanity check failed: #simple-idea should be nested inside #simple-shell"


def test_simple_and_expert_idea_fields_share_the_same_seeded_default_text(web_config) -> None:
    html = web_server._index_html(web_config)
    # One Python source for the seed text (component contract 5.6: "one
    # shared draft"); both textareas render the identical string.
    default_text = "非ST的小市值股票未来表现更好"
    assert f'<textarea id="simple-idea" aria-describedby="simple-runtime-status">{default_text}</textarea>' in html
    assert f'<textarea id="idea">{default_text}</textarea>' in html


def test_simple_shell_subtitle_is_chinese_first(web_config) -> None:
    # FE4 (MINOR, phase review): the NEW simple-shell subtitle must be
    # CN-first (D8). Scoped to the element P0 actually introduces — the
    # pre-existing expert control-rail brand block predates this phase and
    # is untouched, so its subtitle is deliberately not asserted here.
    html = web_server._index_html(web_config)
    simple_start = html.index('id="simple-shell"')
    expert_start = html.index('id="expert-shell"')
    simple_shell_html = html[simple_start:expert_start]
    assert "<p class=\"brand-subtitle\">因子研究控制台</p>" in simple_shell_html
    assert "Factor research console" not in simple_shell_html


def test_advanced_params_grid_is_no_longer_server_rendered(web_config) -> None:
    # P1 (agent_sidecar_frontend.md §5.1/§8, WORKORDER P1 减法): the resident
    # #validation-controls grid (and its #advanced-params <details> wrapper)
    # is ABSORBED into the pipeline confirm card's expert density and
    # DELETED from the server-rendered shell. The card is rendered
    # client-side by static/views/pipeline.js, which reuses the SAME
    # .param-grid / .term-tip CSS classes (still defined in html.py, see
    # test_mode_shell_css_is_token_only_and_ships_both_themes) -- the
    # absorbed markers themselves are pinned in
    # tests/test_web_pipeline_view.py, not on this server-rendered page.
    html = web_server._index_html(web_config)
    assert 'id="advanced-params"' not in html
    assert 'id="validation-controls"' not in html
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
        assert f'id="{param_id}"' not in html, param_id


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
        ".mode-header",
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


def test_touch_targets_meet_the_44px_minimum(web_config) -> None:
    # FE2 (MAJOR, phase review, spec §9): .mode-toggle-btn, .simple-seed-btn,
    # and .advanced-params summary were all below the 44px minimum touch
    # target. Pin the min-height rule on each selector's OWN declaration
    # block specifically (not just "appears somewhere in the file") so a
    # min-height added to an unrelated rule cannot satisfy this vacuously.
    # Real pixel confirmation (getBoundingClientRect == 44 for all three,
    # including the resident #advanced-params summary) is recorded in the
    # phase commit message; computed layout is out of a stdlib-only Python
    # test's reach.
    html = web_server._index_html(web_config)
    for selector in (".mode-toggle-btn {", ".simple-seed-btn {", ".advanced-params summary {"):
        start = html.index(selector)
        end = html.index("}", start)
        assert "min-height: 44px;" in html[start:end], selector


def test_term_tip_popover_stays_inside_narrow_viewports(web_config) -> None:
    # FE3/R1 (MAJOR, phase review, two rounds). Round 1: `left: 0` anchored
    # the popover to the term-tip's OWN left edge, overflowing the
    # viewport's RIGHT edge for a right-column label. Round 1's fix
    # (`left: 50%; transform: translateX(-50%)`) was found to rest on an
    # implicit, fragile dependency: it only stayed inside the LEFT edge
    # today because the pre-existing `.param-grid span { display: block; }`
    # rule (unrelated to this phase) happens to make `.term-tip` stretch to
    # the full column width. R1 removes that dependency: column-aware
    # anchoring (the reviewer's own suggestion) — a left-column popover
    # (`:nth-child(odd)`, param-grid fills row-major so odd = column 1)
    # anchors at its own LEFT edge and extends rightward (always positive);
    # a right-column popover (`:nth-child(even)`) anchors at its own RIGHT
    # edge and extends leftward (always inside the viewport) — neither
    # depends on the anchor's rendered width. The viewport-relative
    # max-width stays as a second safety net. Real geometry for all 5
    # tooltips, BOTH edges (rectLeft >= 0 AND rectRight <= 375 at a true
    # 375px viewport, via two independently-constructed measurement
    # techniques that agreed exactly) is recorded in the phase commit
    # message; computed/rendered box geometry is out of a stdlib-only
    # Python test's reach.
    html = web_server._index_html(web_config)
    base_start = html.index(".term-tip:hover::after, .term-tip:focus-visible::after {")
    base_end = html.index("}", base_start)
    base_block = html[base_start:base_end]
    # The base rule carries no horizontal anchor of its own any more —
    # positioning is column-aware only, so a stray `left`/`right`/
    # `transform` re-added here would silently re-couple both columns to
    # one anchor and reopen exactly this bug.
    assert "left:" not in base_block
    assert "right:" not in base_block
    assert "transform:" not in base_block
    assert "max-width: min(180px, calc(100vw - 40px));" in base_block

    odd_marker = (
        ".param-grid label:nth-child(odd) .term-tip:hover::after,\n"
        "    .param-grid label:nth-child(odd) .term-tip:focus-visible::after {"
    )
    even_marker = (
        ".param-grid label:nth-child(even) .term-tip:hover::after,\n"
        "    .param-grid label:nth-child(even) .term-tip:focus-visible::after {"
    )
    assert odd_marker in html
    assert even_marker in html
    odd_start = html.index(odd_marker)
    odd_end = html.index("}", odd_start)
    assert "left: 0;" in html[odd_start:odd_end]
    even_start = html.index(even_marker)
    even_end = html.index("}", even_start)
    assert "right: 0;" in html[even_start:even_end]
    # Column-aware rules must outrank the base rule on SPECIFICITY (no
    # !important anywhere in the file, confirmed elsewhere), not source
    # order alone — each compounds an extra class (.param-grid) and
    # pseudo-class (:nth-child) onto the base selector, so they are
    # strictly higher regardless of where they sit in the stylesheet.
    assert "!important" not in html


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


def test_sticky_elements_offset_below_the_persistent_header(web_config) -> None:
    # R2 (MINOR, phase review): .mode-header (sticky, top:0, z-index:50)
    # and .control-rail (ALSO sticky, top:0) both try to pin at the
    # viewport's y:0 once scrolled — the header wins on z-index, so the
    # rail's own top content (the QF/Quant Forge brand block) painted
    # UNDER it. Fix: a single shared --mode-header-height custom property
    # (defined once in :root, the only common ancestor — .mode-header sits
    # outside .app-shell entirely, so no closer scope exists) that every
    # OTHER `position: sticky; top: 0` element offsets by by. The reviewer
    # additionally asked to audit for the SAME collision elsewhere:
    # .lab-tabs (sticky inside .workbench) had it too and is fixed the
    # same way. Real getBoundingClientRect() evidence (rail/tab-strip top
    # never overlaps the header's bottom edge across several scroll
    # positions) is recorded in the phase commit message; scroll-driven
    # sticky geometry is out of a stdlib-only Python test's reach.
    html = web_server._index_html(web_config)
    assert "--mode-header-height: 71px;" in html
    root_start = html.index(":root {")
    root_end = html.index("}", root_start)
    assert "--mode-header-height: 71px;" in html[root_start:root_end]

    header_start = html.index(".mode-header {")
    header_end = html.index("}", header_start)
    assert "position: sticky;" in html[header_start:header_end]
    assert "top: 0;" in html[header_start:header_end]

    rail_start = html.index(".control-rail {")
    rail_end = html.index("}", rail_start)
    rail_block = html[rail_start:rail_end]
    assert "position: sticky;" in rail_block
    assert "top: var(--mode-header-height);" in rail_block
    assert "height: calc(100vh - var(--mode-header-height));" in rail_block
    # The old collision values must be gone from THIS rule specifically
    # (not a global sweep — `top: 0;` / `height: 100vh;` legitimately
    # appear elsewhere, e.g. on .mode-header itself).
    assert "top: 0;" not in rail_block
    assert "height: 100vh;" not in rail_block

    tabs_start = html.index(".lab-tabs {")
    tabs_end = html.index("}", tabs_start)
    tabs_block = html[tabs_start:tabs_end]
    assert "position: sticky;" in tabs_block
    assert "top: var(--mode-header-height);" in tabs_block
    assert "top: 0;" not in tabs_block

    # No OTHER `position: sticky` rule was missed by this audit: exactly
    # these three own that declaration on the served page.
    assert html.count("position: sticky;") == 3


# ---------------------------------------------------------------------------
# app.js: mode-precedence algorithm (string-contract pins)
# ---------------------------------------------------------------------------


def test_app_module_imports_the_hash_recognizer_from_lab_module() -> None:
    # P1: setStep is DELETED from lab.js's export surface alongside
    # .lab-stepper (WORKORDER P1 减法); app.js's import line drops it too.
    app_js = _static_module_text("app.js")
    assert (
        "import { activateModule, activateTab, initLabTabs, isRecognizedExpertHash, setTabDot } "
        "from './views/lab.js';" in app_js
    )
    assert "setStep" not in app_js


def test_mode_shell_dom_refs_bind_to_the_new_html_elements() -> None:
    # P1: #advanced-params is DELETED from the served page (WORKORDER P1
    # 减法); app.js no longer binds a DOM ref to it.
    app_js = _static_module_text("app.js")
    assert "getElementById('advanced-params')" not in app_js
    for ref in (
        "const simpleShell = document.getElementById('simple-shell');",
        "const expertShell = document.getElementById('expert-shell');",
        "const modeSimpleBtn = document.getElementById('mode-simple-btn');",
        "const modeExpertBtn = document.getElementById('mode-expert-btn');",
        "const ideaEl = document.getElementById('idea');",
        "const simpleIdeaEl = document.getElementById('simple-idea');",
        "const simpleRunButton = document.getElementById('simple-run');",
    ):
        assert ref in app_js, ref


def test_mode_shell_function_shapes_exist_for_the_harness_to_drive() -> None:
    # Minimal structural sanity (function signatures + the storage-guard
    # try/catch shape) kept as a cheap first-failure localizer; the BEHAVIOR
    # these functions implement — precedence, persistence-vs-not,
    # hashchange re-evaluation, and throwing-storage resilience — is proven
    # by actually EXECUTING the real module in the Node DOM-stub harness
    # below (phase review FE5: "most pins are string tautologies" — this
    # replaces the three former precedence/hashchange/storage-guard tests
    # that only read text and asserted it existed).
    app_js = _static_module_text("app.js")
    for marker in (
        "const MODE_STORAGE_KEY = 'qf_ui_mode';",
        "function readSavedMode() {",
        "function writeSavedMode(mode) {",
        "function applyMode(mode) {",
        "function setMode(mode) {",
        "const deepLinkIsExpert = isRecognizedExpertHash(window.location.hash);",
        "window.addEventListener('hashchange', () => {",
    ):
        assert marker in app_js, marker
    read_start = app_js.index("function readSavedMode() {")
    read_end = app_js.index("function writeSavedMode(mode) {")
    assert "try {" in app_js[read_start:read_end]
    write_start = read_end
    write_end = app_js.index("function applyMode(mode) {")
    assert "try {" in app_js[write_start:write_end]


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
    # FE1 (MAJOR, phase review): this handoff must use applyMode (never
    # setMode) — running the guided form once must not silently overwrite
    # the saved mode preference; only an explicit toggle click is a real
    # preference choice. (The former version of this assertion pinned
    # `setMode('expert')`, i.e. pinned the bug; the Node harness below now
    # additionally proves this behaviorally via scenario (c).)
    assert "applyMode('expert');" in body
    assert "setMode(" not in body
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


def test_confirm_card_appears_immediately_after_parse_without_an_extra_click() -> None:
    # P1 supersedes the deleted disclosure-auto-open behavior this test used
    # to pin: the confirm card (static/views/pipeline.js) is not a collapsed
    # <details> a completed parse has to reveal -- createPipelineFromParseJob
    # renders it directly into #pipeline-card-mount right after the parse
    # payload arrives, in the SAME #run success path, so real values (and
    # their provenance badges) are visible the moment parsing finishes.
    app_js = _static_module_text("app.js")
    parse_click = app_js.index("button.addEventListener('click', async () => {")
    validate_click = app_js.index("validateButton.addEventListener('click', async () => {")
    handler = app_js[parse_click:validate_click]
    assert "renderParsed(payload);" in handler
    assert "await createPipelineFromParseJob(payload.job_id);" in handler
    assert handler.index("renderParsed(payload);") < handler.index("await createPipelineFromParseJob(payload.job_id);")


# ---------------------------------------------------------------------------
# app.js: existing pinned handlers stay untouched (P0 must not rewrite them)
# ---------------------------------------------------------------------------


def test_run_handler_keeps_its_core_activate_submit_render_sequence() -> None:
    # Regression guard for the delegation design above: the #run click
    # handler still contains its core activate/submit/render sequence
    # (test_web_lab_view.py pins the ordering in more detail) so the
    # simple-mode handoff's delegated click keeps landing on real output.
    # P1 (agent_sidecar_frontend.md §2.3) adds pipeline-aggregate creation
    # to this SAME handler (test_web_mode_shell.py's own
    # test_confirm_card_appears_immediately_after_parse_without_an_extra_click
    # pins that addition) -- this test only protects the part that must
    # keep working underneath it, not a claim that the handler is frozen.
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


# ---------------------------------------------------------------------------
# app.js: real mode-shell LIFECYCLE, driven by importing the actual module
# (phase review FE5 — the primary behavioral evidence for FE0/FE1/precedence)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("node") is None, reason="node runtime not available")
def test_node_app_mode_shell_lifecycle_smoke(tmp_path) -> None:
    """Imports the REAL app.js (and its full static dependency chain —
    api.js and every static/views/*.js module it wires) under a minimal,
    auto-vivifying document/window/localStorage stub, with no npm/jsdom,
    and drives the actual mode-shell lifecycle end to end:

    (a) default simple on first load; (b) the FE0 regression — an expert
    toggle click, then a return click, then a THIRD switch, proving the
    toggle buttons keep working rather than trapping the state machine
    after one switch; (c) the FE1 regression — the simple-run handoff
    resolves to the expert shell WITHOUT writing the saved preference;
    (d) a recognized deep link wins navigation without writing storage, for
    both a fresh load with the hash already set and a same-document
    hashchange on an already-open page; (e) a throwing localStorage
    (private-mode style) degrades gracefully — import survives, the
    default landing still resolves, and the toggle keeps working for that
    page view.

    Scope note: this stub models DOM state and event wiring faithfully
    (getElementById is memoized per id, so every call site touching the
    same id shares one object; `hidden` and `open` behave like the real
    IDL attributes) but it is NOT a layout/paint engine — it cannot see a
    `[hidden]` ancestor visually cascading over a nested descendant. That
    structural half of FE0 is covered by
    ``test_mode_toggle_is_a_descendant_of_the_header_never_of_either_shell``
    (a real ancestor-chain parse of the server-rendered HTML) plus the
    live-browser verification recorded in the phase commit message.
    """
    harness = tmp_path / "app_mode_shell_smoke.mjs"
    harness.write_text(_APP_MODE_SHELL_HARNESS, encoding="utf-8")
    env = {"QF_APP_URL": APP_JS_PATH.resolve().as_uri()}
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
        "PASS a.default_simple.expert_hidden",
        "PASS a.default_simple.simple_visible",
        "PASS a.default_simple.no_write",
        "PASS b.after_expert_click.expert_visible",
        "PASS b.after_expert_click.simple_hidden",
        "PASS b.after_expert_click.persisted",
        "PASS b.back_to_simple.simple_visible",
        "PASS b.back_to_simple.expert_hidden",
        "PASS b.back_to_simple.persisted",
        "PASS b.third_switch.expert_visible_again",
        "PASS c.handoff.expert_shell_visible",
        "PASS c.handoff.no_write",
        "PASS d.fresh_load_with_hash.expert_visible",
        "PASS d.fresh_load_with_hash.no_write",
        "PASS d.same_doc.initial_simple",
        "PASS d.same_doc.hashchange_reveals_expert",
        "PASS d.same_doc.hashchange_no_write",
        "PASS e.import_survived_throwing_storage",
        "PASS e.default_simple_despite_throwing_storage",
        "PASS e.toggle_still_works_this_pageview",
    ):
        assert marker in result.stdout, result.stdout


_APP_MODE_SHELL_HARNESS = r"""
const APP_URL = process.env.QF_APP_URL;

// Minimal, auto-vivifying DOM/window/localStorage stub: enough for the
// REAL app.js (and its full static import chain: api.js, every
// static/views/*.js module it wires, lab.js) to execute top-level module
// code under plain Node, no npm/jsdom. getElementById returns a generic
// fake element for ANY id on first request and memoizes it, so every call
// site touching the SAME id shares one consistent object. This models
// STATE and EVENT WIRING faithfully; it does NOT model real layout/paint,
// so it cannot see CSS-driven visibility cascades (an ancestor's
// `display:none` hiding a structurally-nested descendant) — that aspect of
// FE0 is covered separately by a real ancestor-chain check over the
// server-rendered HTML plus live-browser verification, not by this stub.
function makeElement(id) {
  const attrs = new Map();
  const listeners = new Map();
  const el = {
    id,
    value: '',
    textContent: '',
    innerHTML: '',
    disabled: false,
    open: false,
    dataset: {},
    style: {},
    children: [],
    classList: {
      _set: new Set(),
      add(...names) { names.forEach(n => this._set.add(n)); },
      remove(...names) { names.forEach(n => this._set.delete(n)); },
      contains(n) { return this._set.has(n); },
      toggle(n) { if (this._set.has(n)) { this._set.delete(n); return false; } this._set.add(n); return true; }
    },
    addEventListener(type, fn) {
      if (!listeners.has(type)) listeners.set(type, []);
      listeners.get(type).push(fn);
    },
    removeEventListener(type, fn) {
      const fns = listeners.get(type);
      if (fns) { const i = fns.indexOf(fn); if (i !== -1) fns.splice(i, 1); }
    },
    dispatchEvent(evt) {
      const fns = listeners.get(evt.type) || [];
      fns.slice().forEach(fn => fn.call(el, evt));
      return true;
    },
    setAttribute(name, value) { attrs.set(name, String(value)); },
    getAttribute(name) { return attrs.has(name) ? attrs.get(name) : null; },
    removeAttribute(name) { attrs.delete(name); },
    hasAttribute(name) { return attrs.has(name); },
    appendChild(child) { this.children.push(child); return child; },
    insertBefore(child) { this.children.push(child); return child; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    closest() { return null; },
    focus() {},
    click() { this.dispatchEvent({ type: 'click', target: el }); },
    scrollIntoView() {},
    get hidden() { return attrs.has('hidden'); },
    set hidden(v) { if (v) attrs.set('hidden', ''); else attrs.delete('hidden'); }
  };
  return el;
}

function makeLocalStorage({ throwing = false, initial = {} } = {}) {
  const store = new Map(Object.entries(initial));
  return {
    getItem(key) {
      if (throwing) throw new Error('SecurityError: storage disabled');
      return store.has(key) ? store.get(key) : null;
    },
    setItem(key, value) {
      if (throwing) throw new Error('SecurityError: storage disabled');
      store.set(key, String(value));
    },
    removeItem(key) { store.delete(key); },
    _dump() { return Object.fromEntries(store); }
  };
}

function setupGlobals({ hash = '', localStorageOpts = {} } = {}) {
  const registry = new Map();
  const documentStub = {
    getElementById(id) {
      if (!registry.has(id)) registry.set(id, makeElement(id));
      return registry.get(id);
    },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    addEventListener() {},
    removeEventListener() {},
    createElement() { return makeElement(''); }
  };
  const pageConfigEl = documentStub.getElementById('qf-page-config');
  pageConfigEl.textContent = JSON.stringify({ controlTokenRequired: false, llmProviderOptions: [] });

  let currentHash = hash;
  const windowListeners = new Map();
  function fireHashChange() {
    (windowListeners.get('hashchange') || []).slice().forEach(fn => fn({ type: 'hashchange' }));
  }
  const localStorageStub = makeLocalStorage(localStorageOpts);
  const windowStub = {
    location: {
      get hash() { return currentHash; },
      set hash(v) { currentHash = v.startsWith('#') ? v : '#' + v; fireHashChange(); }
    },
    history: { replaceState() {} },
    sessionStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    localStorage: localStorageStub,
    addEventListener(type, fn) {
      if (!windowListeners.has(type)) windowListeners.set(type, []);
      windowListeners.get(type).push(fn);
    },
    removeEventListener() {},
    confirm: () => false
  };

  globalThis.document = documentStub;
  globalThis.window = windowStub;
  globalThis.MutationObserver = class { observe() {} disconnect() {} };
  globalThis.fetch = async () => { throw new Error('network disabled in stub'); };

  return { documentStub, windowStub, localStorageStub, setHash: v => { windowStub.location.hash = v; } };
}

let failed = 0;
function check(name, cond, detail) {
  if (cond) console.log('PASS ' + name);
  else { failed++; console.log('FAIL ' + name + (detail ? ': ' + detail : '')); }
}

// (a) default simple on first load: no hash, no saved preference.
{
  const { documentStub, localStorageStub } = setupGlobals({ hash: '' });
  await import(APP_URL + '?scenario=a');
  const expertShell = documentStub.getElementById('expert-shell');
  const simpleShell = documentStub.getElementById('simple-shell');
  check('a.default_simple.expert_hidden', expertShell.hidden === true, 'hidden=' + expertShell.hidden);
  check('a.default_simple.simple_visible', simpleShell.hidden === false, 'hidden=' + simpleShell.hidden);
  check('a.default_simple.no_write', localStorageStub._dump().qf_ui_mode === undefined, JSON.stringify(localStorageStub._dump()));
}

// (b) FE0 regression: expert toggle -> toggle stays reachable/functional
// -> simple toggle returns. (The CSS-cascade half of FE0 — proving the
// buttons are not a DESCENDANT of a `[hidden]` shell — is proven
// separately over the real server-rendered HTML; this proves the STATE
// MACHINE keeps working correctly across repeated mode switches on the
// SAME button elements, i.e. no listener gets lost or state corrupted.)
{
  const { documentStub, localStorageStub } = setupGlobals({ hash: '' });
  await import(APP_URL + '?scenario=b');
  const expertBtn = documentStub.getElementById('mode-expert-btn');
  const simpleBtn = documentStub.getElementById('mode-simple-btn');
  const expertShell = documentStub.getElementById('expert-shell');
  const simpleShell = documentStub.getElementById('simple-shell');

  expertBtn.click();
  check('b.after_expert_click.expert_visible', expertShell.hidden === false, 'hidden=' + expertShell.hidden);
  check('b.after_expert_click.simple_hidden', simpleShell.hidden === true, 'hidden=' + simpleShell.hidden);
  check('b.after_expert_click.persisted', localStorageStub._dump().qf_ui_mode === 'expert', JSON.stringify(localStorageStub._dump()));
  // The critical "not trapped" assertion: the SAME toggle-simple button is
  // still wired and flips the shells back.
  simpleBtn.click();
  check('b.back_to_simple.simple_visible', simpleShell.hidden === false, 'hidden=' + simpleShell.hidden);
  check('b.back_to_simple.expert_hidden', expertShell.hidden === true, 'hidden=' + expertShell.hidden);
  check('b.back_to_simple.persisted', localStorageStub._dump().qf_ui_mode === 'simple', JSON.stringify(localStorageStub._dump()));
  // And it keeps working for a third switch (no one-shot listener bugs).
  expertBtn.click();
  check('b.third_switch.expert_visible_again', expertShell.hidden === false, 'hidden=' + expertShell.hidden);
}

// (c) FE1 regression: the simple-run handoff must NOT write the saved
// preference — only explicit toggle clicks are a real preference choice.
{
  const { documentStub, localStorageStub } = setupGlobals({ hash: '' });
  await import(APP_URL + '?scenario=c');
  const simpleRunBtn = documentStub.getElementById('simple-run');
  const expertShell = documentStub.getElementById('expert-shell');
  simpleRunBtn.click();
  check('c.handoff.expert_shell_visible', expertShell.hidden === false, 'hidden=' + expertShell.hidden);
  check('c.handoff.no_write', localStorageStub._dump().qf_ui_mode === undefined, JSON.stringify(localStorageStub._dump()));
}

// (d) recognized deep link wins navigation without writing storage — both
// a fresh load WITH the hash already set, and a same-document hashchange
// on a page that loaded without one.
{
  const { documentStub, localStorageStub } = setupGlobals({ hash: '#lab-tab-registry' });
  await import(APP_URL + '?scenario=d1');
  const expertShell = documentStub.getElementById('expert-shell');
  check('d.fresh_load_with_hash.expert_visible', expertShell.hidden === false, 'hidden=' + expertShell.hidden);
  check('d.fresh_load_with_hash.no_write', localStorageStub._dump().qf_ui_mode === undefined, JSON.stringify(localStorageStub._dump()));
}
{
  const { documentStub, localStorageStub, setHash } = setupGlobals({ hash: '' });
  await import(APP_URL + '?scenario=d2');
  const expertShell = documentStub.getElementById('expert-shell');
  check('d.same_doc.initial_simple', expertShell.hidden === true);
  setHash('#lab-tab-registry');
  check('d.same_doc.hashchange_reveals_expert', expertShell.hidden === false, 'hidden=' + expertShell.hidden);
  check('d.same_doc.hashchange_no_write', localStorageStub._dump().qf_ui_mode === undefined, JSON.stringify(localStorageStub._dump()));
}

// (e) localStorage throwing (private-mode style) degrades gracefully: the
// import itself must not throw, the default landing still resolves, and
// the toggle still works for THIS page view even though it cannot persist.
{
  const { documentStub } = setupGlobals({ hash: '', localStorageOpts: { throwing: true } });
  let importThrew = false;
  try {
    await import(APP_URL + '?scenario=e');
  } catch (err) {
    importThrew = true;
    console.log('e.import_threw detail: ' + (err && err.stack));
  }
  check('e.import_survived_throwing_storage', importThrew === false);
  const expertShell = documentStub.getElementById('expert-shell');
  const simpleShell = documentStub.getElementById('simple-shell');
  check('e.default_simple_despite_throwing_storage', expertShell.hidden === true && simpleShell.hidden === false);
  const expertBtn = documentStub.getElementById('mode-expert-btn');
  expertBtn.click();
  check('e.toggle_still_works_this_pageview', expertShell.hidden === false, 'hidden=' + expertShell.hidden);
}

console.log('SMOKE RESULT: ' + failed + ' failed');
if (failed) process.exit(1);
"""

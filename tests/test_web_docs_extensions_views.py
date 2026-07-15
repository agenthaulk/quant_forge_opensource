"""Targeted tests for the CP6-4 Docs view + Extensions browse panel (D7/D7a, D8).

String-contract pins over the served page and static modules:

- the served page hosts the 文档 / 扩展 tabs and panels (appended after the
  CP6-3 tabs so existing indices stay stable) with server-rendered empty
  states, and the CP6-4 CSS additions reference theme tokens only (zero new
  color literals);
- ``views/docs.js`` renders the docs index + one rendered document: the
  SINGLE innerHTML insertion of unescaped server HTML in the whole app is
  the document payload's ``html`` field (safety owned by the server-side
  escape-first renderer), every other interpolation goes through ``esc()``,
  document fetch URLs encode each path segment separately so slashes stay
  path separators, and the ``#docs-doc-<relpath>`` hash syncs through
  ``replaceState`` with internal-link click delegation;
- ``views/extensions.js`` renders the declarative extension registry (D7):
  validation statuses render as the literal ``valid``/``rejected`` label
  pills, contribution-point statuses as literal ``supported``/``reserved``
  labels, and rejection issue codes verbatim in ``<code>`` with no
  client-side re-interpretation table;
- both new modules carry zero external references and no metric
  formatting or zero-fill;
- ``app.js`` wires both panels through the token-gated tracked lazy
  refresh.
"""

from __future__ import annotations

import pytest

import quant_forge.apps.web.server as web_server
from quant_forge.config import QuantForgeConfig
from quant_forge.data.local import create_demo_workspace


def _static_module_text(name: str) -> str:
    return (web_server.STATIC_ROOT / name).read_text(encoding="utf-8")


@pytest.fixture()
def web_config(tmp_path):
    create_demo_workspace(tmp_path / "demo")
    return QuantForgeConfig().resolve(tmp_path / "demo")


# ---------------------------------------------------------------------------
# Served page: tabs, panels, CSS additions
# ---------------------------------------------------------------------------


def test_index_page_hosts_docs_and_extensions_tabs_and_panels(web_config) -> None:
    html = web_server._index_html(web_config)
    for tab, panel in (
        ("lab-tab-docs", "lab-panel-docs"),
        ("lab-tab-extensions", "lab-panel-extensions"),
    ):
        assert f'id="{tab}" aria-controls="{panel}" aria-selected="false" tabindex="-1"' in html
        assert f'id="{panel}" aria-labelledby="{tab}" tabindex="0" hidden' in html
    for marker in (
        "文档",
        "仓库 docs/ 只读渲染",
        "扩展",
        "声明式扩展注册表（只读）",
        'id="docs-result"',
        'id="extensions-result"',
        "打开本页签后，文档目录与渲染内容会展示在这里。",
        "扩展清单加载后，manifest 校验状态与贡献点会展示在这里。",
    ):
        assert marker in html, marker
    # Appended after the CP6-3 tabs so existing tab indices stay stable.
    assert html.index('id="lab-tab-registry"') < html.index('id="lab-tab-docs"')
    assert html.index('id="lab-tab-docs"') < html.index('id="lab-tab-extensions"')
    assert html.index('id="registry-result"') < html.index('id="lab-panel-docs"')
    assert html.index('id="lab-panel-docs"') < html.index('id="docs-result"')
    assert html.index('id="docs-result"') < html.index('id="lab-panel-extensions"')
    assert html.index('id="lab-panel-extensions"') < html.index('id="extensions-result"')
    # 7 top-level tabs (CP9-2 IA consolidation + SE-P4b's 记忆治理) + 2
    # workbench module tabs.
    assert html.count('role="tab"') == 9
    assert html.count('role="tabpanel"') == 9


def test_index_page_ships_cp64_css_with_theme_tokens_only(web_config) -> None:
    html = web_server._index_html(web_config)
    for rule in (
        ".docs-layout",
        ".docs-nav",
        ".docs-nav-section",
        ".docs-row",
        '.docs-row[aria-current="true"]',
        ".docs-row:focus-visible",
        ".docs-detail",
        ".docs-article",
        ".docs-article pre",
        ".docs-article blockquote",
        ".docs-article .docs-table",
        ".docs-link",
        ".docs-external-url",
        ".docs-image-alt",
        ".ext-grid",
        ".ext-card",
        ".ext-card-head",
        ".ext-version",
        ".ext-contribs",
        ".ext-points",
    ):
        assert rule in html, rule
    # Zero new color literals: every CP6-4 declaration references theme
    # variables, so the dark block needs no new overrides.
    block_start = html.index("/* CP6-4 Docs + Extensions")
    block_end = html.index("@media (prefers-color-scheme: dark)")
    block = html[block_start:block_end]
    assert "#" not in block
    assert "rgb" not in block
    assert "var(--" in block
    # The docs master-detail collapses to one column on narrow screens, the
    # rendered doc tables keep a readable minimum width inside the
    # renderer's .table-scroll wrapper, and the anchor clearance covers the
    # taller wrapped tab strip (8 tabs).
    media_start = html.index("@media (max-width: 900px)")
    assert ".docs-layout { grid-template-columns: 1fr; }" in html[media_start:]
    assert ".docs-article .docs-table { min-width: 480px; }" in html[media_start:]
    assert ".report-section { scroll-margin-top: 156px; }" in html[media_start:]


# ---------------------------------------------------------------------------
# views/docs.js — the single unescaped HTML insertion, fetch + hash contract
# ---------------------------------------------------------------------------


def test_docs_module_has_exactly_one_unescaped_server_html_insertion() -> None:
    docs_js = _static_module_text("views/docs.js")
    # The single insertion site for server-rendered HTML, guarded by the
    # module-header justification (server escape-first renderer owns its
    # safety); no second unescaped payload path exists.
    assert docs_js.count("${articleHtml}") == 1
    assert "ONLY unescaped insertion of server HTML" in docs_js
    assert "escape-first" in docs_js
    assert "const articleHtml = String(payload.html || '');" in docs_js
    assert '<div class="docs-article">${articleHtml}</div>' in docs_js
    # Every server field outside the html payload is esc()-routed; a bare
    # `${payload.field}` inside a template literal would bypass esc().
    for token in ("${payload.", "${doc.", "${sectionEntry.", "${error.", "${href"):
        assert token not in docs_js, f"unescaped interpolation {token}"
    assert "from '../metric.js'" in docs_js


def test_docs_module_fetch_hash_and_degradation_contract() -> None:
    docs_js = _static_module_text("views/docs.js")
    assert "fetchPanelJson('/api/docs')" in docs_js
    # Per-segment encoding keeps slashes as path separators in the doc URL.
    assert "relpath.split('/').map(encodeURIComponent).join('/')" in docs_js
    assert "fetchPanelJson(`/api/docs/${encoded}`)" in docs_js
    # Selection travels via data-relpath (never DOM ids minted from
    # relpaths) and syncs the URL through replaceState.
    assert 'data-relpath="' in docs_js
    assert 'id="docs-doc-' not in docs_js
    assert "const DOCS_HASH_PREFIX = '#docs-doc-';" in docs_js
    assert "const DOCS_DOC_HASH = /^#docs-doc-([A-Za-z0-9_][A-Za-z0-9_/.-]*)$/;" in docs_js
    assert "window.history.replaceState(null, '', DOCS_HASH_PREFIX + relpath)" in docs_js
    assert "window.addEventListener('hashchange'" in docs_js
    assert "scrollIntoView({ block: 'start' })" in docs_js
    # Internal doc links navigate in-view via delegated clicks; unknown
    # targets degrade in the detail pane.
    assert "closest('a.docs-link')" in docs_js
    assert "event.preventDefault();" in docs_js
    assert "listedRelpaths.includes(target)" in docs_js
    # Degradation states stay in-pane: no selection, loading, missing root,
    # empty index, token cleared mid-session, document fetch error.
    assert "未选择文档" in docs_js
    assert "正在读取并渲染文档。" in docs_js
    assert "文档不可用" in docs_js
    assert "暂无文档" in docs_js
    assert "需要控制令牌后重试。" in docs_js
    assert "文档可能不存在或已被移动" in docs_js
    # The index is the panel's primary content: refresh reports loaded only
    # after a successful index render; a missing token skips silently.
    assert "if (!payload) return false;" in docs_js
    assert "return true;" in docs_js


def test_docs_hash_charset_mirrors_server_relpath_rule() -> None:
    """The doc-name rule has ONE server-side definition; both frontend hash
    patterns use the same alphabet (segment charset plus '/' separators), so
    every relpath the server can serve deep-links cleanly. The frontend does
    not replicate segment-level dot rules: the server stays authoritative
    and 404s hashes it rejects."""

    from quant_forge.apps.web.api import _DOCS_RELPATH_SEGMENT_RE

    assert _DOCS_RELPATH_SEGMENT_RE.pattern == r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$"
    docs_js = _static_module_text("views/docs.js")
    lab_js = _static_module_text("views/lab.js")
    assert "const DOCS_DOC_HASH = /^#docs-doc-([A-Za-z0-9_][A-Za-z0-9_/.-]*)$/;" in docs_js
    assert "const DOCS_DOC_HASH = /^docs-doc-[A-Za-z0-9_][A-Za-z0-9_/.-]*$/;" in lab_js


# ---------------------------------------------------------------------------
# views/extensions.js — literal labels, verbatim issue codes, escaping
# ---------------------------------------------------------------------------


def test_extensions_module_renders_literal_labels_and_verbatim_codes() -> None:
    extensions_js = _static_module_text("views/extensions.js")
    assert "fetchPanelJson('/api/extensions')" in extensions_js
    # Manifest statuses are the literal closed-set labels with the CP6-4
    # tone classes (valid=ok, rejected=fail); contribution-point statuses
    # stay capability taxonomy (supported=ok, reserved=neutral).
    assert '<span class="status-pill status-pill--ok">valid</span>' in extensions_js
    assert '<span class="status-pill status-pill--fail">rejected</span>' in extensions_js
    assert "label === 'supported' ? 'status-pill--ok' : 'status-pill--neutral'" in extensions_js
    assert '<span class="status-pill status-pill--neutral">reserved</span>' in extensions_js
    # Issue codes render verbatim in <code> with their field locator — no
    # client-side re-interpretation table in v1 (no code literal appears in
    # the module).
    assert "<code>${esc(issue.code || '')}</code>" in extensions_js
    assert "executable_contribution_rejected" not in extensions_js
    assert "network_access_rejected" not in extensions_js
    # The permanent declarative-registry notice and empty states.
    assert "声明式注册表：manifest 仅作元数据展示，不加载、不执行任何代码。" in extensions_js
    assert "扩展目录不存在" in extensions_js
    assert "未安装扩展" in extensions_js
    assert "注册表为声明式（D7）：不支持任何可执行贡献。" in extensions_js
    assert "extensions/&lt;name&gt;/extension.json" in extensions_js
    # Deep-link targeting travels via data-extension-id, never DOM ids.
    assert 'data-extension-id="' in extensions_js
    assert 'id="extensions-manifest-' not in extensions_js
    assert (
        "const EXTENSIONS_MANIFEST_HASH = /^#extensions-manifest-([a-z][a-z0-9]*(?:[._-][a-z0-9]+)*)$/;"
        in extensions_js
    )
    assert "window.addEventListener('hashchange'" in extensions_js
    assert "scrollIntoView({ block: 'start' })" in extensions_js
    # Optional manifest fields absent -> omitted, never defaulted.
    assert "ext.description ? " in extensions_js
    # Refresh contract: token missing skips silently; registry render loads.
    assert "if (!payload) return false;" in extensions_js
    assert "return true;" in extensions_js


def test_extensions_module_escapes_every_server_interpolation() -> None:
    extensions_js = _static_module_text("views/extensions.js")
    for token in (
        "${payload.",
        "${ext.",
        "${issue.",
        "${point.",
        "${contribution.",
        "${permissions.",
        "${data.",
        "${error.",
    ):
        assert token not in extensions_js, f"unescaped interpolation {token}"
    assert "from '../metric.js'" in extensions_js


# ---------------------------------------------------------------------------
# Shared sweeps and app.js wiring
# ---------------------------------------------------------------------------


def test_new_modules_contain_no_external_references() -> None:
    for name in ("views/docs.js", "views/extensions.js"):
        text = _static_module_text(name)
        assert "http://" not in text, name
        assert "https://" not in text, name


def test_new_modules_never_format_or_zero_fill_metrics() -> None:
    # FP-4: no local number formatting (metric.js is the only renderer), no
    # null-to-zero coercion, and no client-side reordering of server lists.
    for name in ("views/docs.js", "views/extensions.js"):
        text = _static_module_text(name)
        assert ".toFixed(" not in text, name
        assert "|| 0" not in text, name
        assert "?? 0" not in text, name
        assert ".sort(" not in text, name


def test_app_module_wires_docs_and_extensions_lazy_panels() -> None:
    app_js = _static_module_text("app.js")
    assert "from './views/docs.js'" in app_js
    assert "from './views/extensions.js'" in app_js
    assert "const docsPanel = trackedPanelRefresh(refreshDocsPanel);" in app_js
    assert "const extensionsPanel = trackedPanelRefresh(refreshExtensionsPanel);" in app_js
    # CP9-2: lazyPanelsByTab values are arrays so one tab can own several
    # lazy panels (the workbench tab owns the absorbed bench panel).
    assert "'lab-tab-docs': [docsPanel]" in app_js
    assert "'lab-tab-extensions': [extensionsPanel]" in app_js
    # Storing the control token refreshes all six token-gated panels.
    token_block_start = app_js.index("onControlTokenStored(")
    token_block = app_js[token_block_start : app_js.index("llmProviderSelect.addEventListener", token_block_start)]
    for marker in ("docsPanel.refresh()", "extensionsPanel.refresh()"):
        assert marker in token_block, marker
    # Startup refresh outside the callback also covers both new panels.
    startup_block = app_js[
        app_js.index("llmProviderSelect.addEventListener") : app_js.index("button.addEventListener('click'")
    ]
    for marker in ("docsPanel.refresh();", "extensionsPanel.refresh();"):
        assert marker in startup_block, marker

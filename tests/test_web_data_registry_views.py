"""Targeted tests for the CP6-3 Data console + Registry views (D8).

String-contract pins over the served page and static modules:

- the served page hosts the 数据 / 注册表 tabs and panels (appended after the
  CP6-2 tabs so existing indices stay stable) with server-rendered empty
  states, and the CP6-3 CSS additions reference theme tokens only (zero new
  color literals);
- ``views/data.js`` renders GET /api/data/catalog + GET /api/data/status
  with FP-4 discipline: coverage counts fall back to ``n/a`` (never 0),
  gate/availability values render as literal label pills, and quality
  tokens render verbatim next to mapped neutral explanations (unknown
  tokens are never re-interpreted);
- ``views/tags.js`` is the single qf.research_tags.v1 chip renderer and
  preserves the ``columns_required`` null-vs-empty distinction;
- ``views/registry.js`` renders the master-detail registry over the
  factor-catalog and per-factor evidence endpoints: selection travels via
  ``data-factor-id`` (never DOM ids minted from factor ids), the URL hash
  syncs through ``replaceState``, the kind filter mirrors ``RUN_KINDS``,
  and metric cells render only via ``metric.js`` helpers;
- ``views/lab.js`` gains only tab ids and hash-prefix rules (fetch-free
  purity intact; CP6-4 appends the docs/extensions ids and prefixes);
  ``app.js`` wires both panels through the token-gated tracked lazy
  refresh.
"""

from __future__ import annotations

import inspect

import pytest

import quant_forge.apps.web.server as web_server
from quant_forge.config import QuantForgeConfig
from quant_forge.core import contracts as core_contracts
from quant_forge.data.local import create_demo_workspace
from quant_forge.lineage.store import RUN_KINDS


def _static_module_text(name: str) -> str:
    return (web_server.STATIC_ROOT / name).read_text(encoding="utf-8")


@pytest.fixture()
def web_config(tmp_path):
    create_demo_workspace(tmp_path / "demo")
    return QuantForgeConfig().resolve(tmp_path / "demo")


# ---------------------------------------------------------------------------
# Served page: tabs, panels, CSS additions
# ---------------------------------------------------------------------------


def test_index_page_hosts_data_and_registry_tabs_and_panels(web_config) -> None:
    html = web_server._index_html(web_config)
    for tab, panel in (
        ("lab-tab-data", "lab-panel-data"),
        ("lab-tab-registry", "lab-panel-registry"),
    ):
        assert f'id="{tab}" aria-controls="{panel}" aria-selected="false" tabindex="-1"' in html
        assert f'id="{panel}" aria-labelledby="{tab}" tabindex="0" hidden' in html
    for marker in (
        "数据控制台",
        "字段目录、覆盖范围与质量门",
        "因子定义与证据链",
        'id="data-result"',
        'id="registry-result"',
        "打开本页签后，字段目录、覆盖范围和质量门结果会展示在这里。",
        "因子目录加载后，定义详情与关联运行记录会展示在这里。",
    ):
        assert marker in html, marker
    # Tab order after the CP9-2 IA consolidation: workbench, history, data,
    # registry, docs, extensions. The bench mount now precedes the history
    # panel from inside the workbench panel's single-factor module.
    assert html.index('id="lab-tab-history"') < html.index('id="lab-tab-data"')
    assert html.index('id="lab-tab-data"') < html.index('id="lab-tab-registry"')
    assert html.index('id="bench-result"') < html.index('id="lab-panel-history"')
    assert html.index('id="lab-panel-data"') < html.index('id="data-result"')
    assert html.index('id="data-result"') < html.index('id="lab-panel-registry"')
    assert html.index('id="lab-panel-registry"') < html.index('id="registry-result"')
    # 6 top-level tabs + 2 workbench module tabs; 6 + 2 tabpanels.
    assert html.count('role="tab"') == 8
    assert html.count('role="tabpanel"') == 8


def test_index_page_ships_cp63_css_with_theme_tokens_only(web_config) -> None:
    html = web_server._index_html(web_config)
    for rule in (
        ".notice.ok",
        ".pill.muted",
        ".tile-range",
        ".tag-chips",
        ".registry-layout",
        ".registry-list",
        ".registry-row",
        '.registry-row[aria-current="true"]',
        ".registry-row:focus-visible",
        ".registry-row-name",
        ".registry-row-formula",
        ".registry-detail",
        ".registry-runs-toolbar",
        ".nowrap",
        ".table-scroll",
    ):
        assert rule in html, rule
    # Zero new color literals: every CP6-3 declaration references theme
    # variables, so the dark block needs no new overrides.
    block_start = html.index("/* CP6-3 Data console + Registry")
    block_end = html.index("@media (prefers-color-scheme: dark)")
    block = html[block_start:block_end]
    assert "#" not in block
    assert "var(--" in block
    # The master-detail layout collapses to one column on narrow screens,
    # and the dense tables scroll sideways instead of crushing cells.
    media_start = html.index("@media (max-width: 900px)")
    assert ".registry-layout { grid-template-columns: 1fr; }" in html[media_start:]
    assert ".data-fields-table { min-width: 640px; }" in html[media_start:]
    assert ".registry-runs-table { min-width: 520px; }" in html[media_start:]


# ---------------------------------------------------------------------------
# views/data.js — fetch contract and FP-4 rendering
# ---------------------------------------------------------------------------


def test_data_module_fetch_contract_and_partial_render() -> None:
    data_js = _static_module_text("views/data.js")
    assert "fetchPanelJson('/api/data/catalog')" in data_js
    assert "fetchPanelJson('/api/data/status').catch(error => ({ __error: error.message }))" in data_js
    # No stored token yet -> silent skip so the lazy retry stays alive.
    assert "if (!catalog) return false;" in data_js
    # A status failure keeps the fields table (availability shows n/a) plus
    # one error notice, and reports not-loaded so the next activation
    # retries the status fetch.
    assert "return Boolean(status) && !status.__error;" in data_js
    assert "数据校验状态不可用" in data_js
    # The view only reads through the token-gated panel fetch helper.
    assert "fetch(" not in data_js.replace("fetchPanelJson(", "")


def test_data_module_renders_labels_and_na_never_zero() -> None:
    data_js = _static_module_text("views/data.js")
    # Gate flag renders as a labelled pill inside a notice, never a scalar.
    assert '质量门 <span class="status-pill status-pill--ok">ok</span>' in data_js
    assert '质量门 <span class="status-pill status-pill--fail">fail</span>' in data_js
    assert '质量门 <span class="status-pill status-pill--neutral">n/a</span>' in data_js
    # Coverage counts fall back to n/a via valueOr — never coerced to 0.
    assert "valueOr(coverage.rows, 'n/a')" in data_js
    assert "valueOr(coverage.instruments, 'n/a')" in data_js
    assert "valueOr(coverage.date_count, 'n/a')" in data_js
    assert "coverage.start_date || 'n/a'" in data_js
    assert "coverage.end_date || 'n/a'" in data_js
    # Availability labels: literal statuses with honest severity tones; a
    # missing optional field is not a failure; keys are skipped by contract.
    assert '<span class="status-pill status-pill--ok">available</span>' in data_js
    assert '<span class="status-pill status-pill--running">synthesized</span>' in data_js
    assert "field.role === 'required' ? 'status-pill--fail' : 'status-pill--neutral'" in data_js
    assert '<span class="status-pill status-pill--neutral">key</span>' in data_js
    assert '<span class="metric-missing">n/a</span>' in data_js
    # Row order is the server catalog order; no client-side sorting.
    assert ".sort(" not in data_js


def test_data_module_quality_tokens_render_verbatim_with_neutral_text() -> None:
    data_js = _static_module_text("views/data.js")
    assert "'duplicate_keys'" in data_js
    assert "面板存在重复的 (trade_date, instrument) 键" in data_js
    assert "'null:'" in data_js
    assert "存在空值" in data_js
    assert "'dtype:'" in data_js
    assert "数据类型与目录声明不符" in data_js
    # Unknown tokens render raw with a generic marker note, never an
    # invented explanation.
    assert "数据校验发现的问题标记" in data_js
    assert "目录声明的必需列缺失" in data_js
    assert "由加载器合成，缺少完整源数据支撑" in data_js
    assert "存在于数据中但未在目录声明" in data_js
    assert "数据校验未发现阻塞问题" in data_js


# ---------------------------------------------------------------------------
# views/tags.js — the single research-tag chip renderer
# ---------------------------------------------------------------------------


def test_tags_module_is_the_single_research_tag_renderer() -> None:
    definition = "function researchTagChipsHtml("
    modules = sorted(web_server.STATIC_ROOT.rglob("*.js"))
    total = sum(path.read_text(encoding="utf-8").count(definition) for path in modules)
    assert total == 1
    tags_js = _static_module_text("views/tags.js")
    assert "export function researchTagChipsHtml(" in tags_js
    for name in ("views/data.js", "views/registry.js"):
        text = _static_module_text(name)
        assert "from './tags.js'" in text, name
        assert "researchTagChipsHtml(" in text, name


def test_tags_module_preserves_null_vs_empty_and_absent_values() -> None:
    tags_js = _static_module_text("views/tags.js")
    # tags: null renders an explicit no-tags chip, never an empty tag set.
    assert "if (!tags) return '<span class=\"pill muted\">无研究标签</span>';" in tags_js
    # columns_required: null (unobservable) vs [] (observably none) render
    # differently and are never collapsed (FP-4).
    assert "if (tags.columns_required == null) chips.push('<span class=\"pill muted\">inputs n/a</span>');" in tags_js
    assert "else if (!tags.columns_required.length) chips.push(chip('inputs 无'));" in tags_js
    # Absent backend facts render nothing — no guessed defaults.
    assert "if (tags.frequency != null)" in tags_js
    assert "if (tags.min_warmup_bars != null)" in tags_js
    assert "if (tags.decay_horizon_days != null)" in tags_js


def test_tags_module_labels_horizon_value_as_horizon_not_decay() -> None:
    # Integration finding F-009: the qf.research_tags.v1 payload key stays
    # `decay_horizon_days` (the key is frozen by the payload-shape pins in
    # test_web_data_registry_api / test_data_catalog_expansion), but for
    # factor subjects the backend populates it from
    # FactorDefinition.horizon_days — the holding/signal horizon — because
    # no measured decay estimate exists (research_tags.py, FP-4). The chip
    # label must state what the value holds; a "decay" label would claim a
    # decay parameter the run never had.
    tags_js = _static_module_text("views/tags.js")
    assert "chip('horizon ' + tags.decay_horizon_days + 'd')" in tags_js
    assert "'decay '" not in tags_js


# ---------------------------------------------------------------------------
# views/registry.js — master-detail, hash sync, kind filter, FP-4
# ---------------------------------------------------------------------------


def test_registry_module_master_detail_selection_and_hash_sync() -> None:
    registry_js = _static_module_text("views/registry.js")
    assert "fetchPanelJson('/api/registry/factors')" in registry_js
    assert "`/api/registry/factors/${encodeURIComponent(factorId)}${query}`" in registry_js
    # Selection travels via data-factor-id on real buttons with aria-current;
    # no DOM element ever gets an id derived from a factor id, so native
    # fragment scrolling cannot fight the hash routing.
    assert 'data-factor-id="' in registry_js
    assert "aria-current" in registry_js
    assert 'id="registry-factor-' not in registry_js
    assert "const REGISTRY_HASH_PREFIX = '#registry-factor-';" in registry_js
    assert "window.history.replaceState(null, '', REGISTRY_HASH_PREFIX + factorId)" in registry_js
    assert "window.addEventListener('hashchange'" in registry_js
    assert "scrollIntoView({ block: 'start' })" in registry_js
    # Degradation states stay in-pane: empty catalog, no selection, zero
    # runs, token cleared mid-session, detail fetch error.
    assert "暂无注册因子" in registry_js
    assert "选择左侧因子查看定义与证据链。" in registry_js
    assert "该因子暂无运行记录" in registry_js
    assert "需要控制令牌后重试。" in registry_js
    assert "列表可能已过期" in registry_js
    # The catalog is the panel's primary content: refresh reports loaded
    # after a successful list render.
    assert "if (!payload) return false;" in registry_js
    assert "return true;" in registry_js


def test_registry_kind_filter_mirrors_run_kinds() -> None:
    registry_js = _static_module_text("views/registry.js")
    expected = "const RUN_KIND_OPTIONS = [" + ", ".join(f"'{kind}'" for kind in RUN_KINDS) + "];"
    assert expected in registry_js
    assert '<option value="">全部</option>' in registry_js
    assert "encodeURIComponent(currentKind)" in registry_js


def test_registry_metric_cells_render_only_via_metric_helpers() -> None:
    registry_js = _static_module_text("views/registry.js")
    assert "import { esc, metricCellHtml, metricStatusSuffix, valueOr } from '../metric.js';" in registry_js
    # CP9-2: the detail-card formula highlighter arrives on its own import
    # line — the pinned metric.js import stays byte-identical.
    assert "import { formulaHtml } from './dsl.js';" in registry_js
    # The value part renders only when metric.js itself would render a
    # number (available/legacy); otherwise metricStatusSuffix carries the
    # single status label — never the same label twice in one pill.
    assert "status === 'available' || status === 'legacy'" in registry_js
    assert "`${metricCellHtml(entry)} · `" in registry_js
    assert "${valueHtml}${metricStatusSuffix(entry)}" in registry_js
    # Redacted / fingerprint fields are never rendered (matches history.js).
    assert "artifact_paths_rel" not in registry_js
    assert "config_fingerprint" not in registry_js
    # Lifecycle labels render literally; only active/candidate change tone.
    assert "label === 'active'" in registry_js
    assert "'candidate' ? 'status-pill--running' : 'status-pill--neutral'" in registry_js
    assert "valueOr(factor.horizon_days, 'n/a')" in registry_js
    assert "run.warnings_count ?? 'n/a'" in registry_js
    # Precomputed formulas render the key behind a literal pill — never a
    # path, never a fake expression.
    assert "const PRECOMPUTED_PREFIX = 'precomputed:';" in registry_js
    assert "公式不在本仓库，输入字段不可观测。" in registry_js


def test_registry_row_and_detail_flag_unavailable_precomputed_values() -> None:
    """A dangling composite's row + detail header render a 值不可用 pill.

    Additive honesty pill (FP-4): a precomputed factor's DEFINITION can
    persist while its VALUES were only ever written to a past run's overlay.
    Strictly ``=== false`` renders the pill — ``null`` (not precomputed, or
    the presence probe itself failed) and ``true`` render unchanged, and both
    the list row and the detail header share ONE helper.
    """

    registry_js = _static_module_text("views/registry.js")
    assert "factor.precomputed_values_present === false" in registry_js
    assert "值不可用" in registry_js
    # One helper definition + exactly two call sites (list row, detail card);
    # additive next to the existing lifecycle pill, never replacing it.
    assert registry_js.count("valuesUnavailablePillHtml(factor)") == 3
    assert registry_js.count(
        "${factorStatusPillHtml(factor.status)}${valuesUnavailablePillHtml(factor)}"
    ) == 2


# ---------------------------------------------------------------------------
# Escaping and FP-4 sweeps across the new modules
# ---------------------------------------------------------------------------

# Server-derived values must be escaped at their interpolation sites; a bare
# `${payload.field}` inside a template literal would bypass esc().
RAW_INTERPOLATION_TOKENS = {
    "views/data.js": (
        "${field.",
        "${coverage.",
        "${catalog.",
        "${status.",
        "${tags.",
        "${quality.",
        "${entry.",
    ),
    "views/registry.js": (
        "${factor.",
        "${run.",
        "${payload.",
        "${dataWindow.",
        "${tags.",
    ),
    "views/tags.js": ("${tags.",),
}


def test_new_modules_escape_every_server_interpolation() -> None:
    for name, tokens in RAW_INTERPOLATION_TOKENS.items():
        text = _static_module_text(name)
        for token in tokens:
            assert token not in text, f"{name}: unescaped interpolation {token}"
        assert "from '../metric.js'" in text, name


def test_new_modules_never_format_or_zero_fill_metrics() -> None:
    # FP-4: no local number formatting (metric.js is the only renderer) and
    # no null-to-zero coercion anywhere in the new modules.
    for name in ("views/data.js", "views/registry.js", "views/tags.js"):
        text = _static_module_text(name)
        assert ".toFixed(" not in text, name
        assert "|| 0" not in text, name
        assert "?? 0" not in text, name


# ---------------------------------------------------------------------------
# lab.js purity and app.js wiring
# ---------------------------------------------------------------------------


def test_lab_module_adds_only_tab_ids_and_registry_hash_prefix() -> None:
    lab_js = _static_module_text("views/lab.js")
    # CP9-2 TAB_IDS literal: six top-level tabs, workbench (kept id
    # lab-tab-factor) first; the RD/Benchmark ids left the tab strip and
    # survive only as LEGACY_HASH_ALIASES keys.
    assert "'lab-tab-factor', 'lab-tab-history', 'lab-tab-data'," in lab_js
    assert "'lab-tab-registry', 'lab-tab-docs', 'lab-tab-extensions'" in lab_js
    # The hash rules pin each entity charset client-side and keep the
    # anchor in the URL (same discipline as #report-* links). lab.js only
    # activates the owning tab; the view modules own selection.
    assert "const REGISTRY_FACTOR_HASH = /^registry-factor-[A-Za-z][A-Za-z0-9_=-]*$/;" in lab_js
    assert "activateTab('lab-tab-registry', { updateHash: false });" in lab_js
    assert "[A-Za-z][A-Za-z0-9_=-]*" in inspect.getsource(core_contracts)
    assert "const DOCS_DOC_HASH = /^docs-doc-[A-Za-z0-9_][A-Za-z0-9_/.-]*$/;" in lab_js
    assert "activateTab('lab-tab-docs', { updateHash: false });" in lab_js
    assert (
        "const EXTENSIONS_MANIFEST_HASH = /^extensions-manifest-[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$/;"
        in lab_js
    )
    assert "activateTab('lab-tab-extensions', { updateHash: false });" in lab_js
    # Purity intact: no fetch, no endpoints, no panel refreshers, and no
    # selection logic (each view owns what its anchor selects).
    assert "fetch(" not in lab_js
    assert "/api/" not in lab_js
    assert "refreshDataPanel" not in lab_js
    assert "refreshRegistryPanel" not in lab_js
    assert "refreshDocsPanel" not in lab_js
    assert "refreshExtensionsPanel" not in lab_js
    assert "data-factor-id" not in lab_js
    assert "data-relpath" not in lab_js
    assert "data-extension-id" not in lab_js
    # P1 (WORKORDER P1 减法): the former research-flow stepper (STEP_IDS)
    # is DELETED from lab.js entirely -- superseded by the pipeline card
    # (tests/test_web_pipeline_view.py). data/registry/docs/extensions were
    # never stepper concerns either way. (lab.js's own header comment names
    # the deleted identifier in prose, so this checks the CODE form only.)
    assert "const STEP_IDS" not in lab_js


def test_app_module_wires_data_and_registry_lazy_panels() -> None:
    app_js = _static_module_text("app.js")
    assert "from './views/data.js'" in app_js
    assert "from './views/registry.js'" in app_js
    assert "const dataPanel = trackedPanelRefresh(refreshDataPanel);" in app_js
    assert "const registryPanel = trackedPanelRefresh(refreshRegistryPanel);" in app_js
    # CP9-2: lazyPanelsByTab values are arrays so one tab can own several
    # lazy panels (the workbench tab owns the absorbed bench panel).
    assert "'lab-tab-data': [dataPanel]" in app_js
    assert "'lab-tab-registry': [registryPanel]" in app_js
    # Storing the control token refreshes all four token-gated panels.
    token_block_start = app_js.index("onControlTokenStored(")
    token_block = app_js[token_block_start : app_js.index("llmProviderSelect.addEventListener", token_block_start)]
    for marker in (
        "historyPanel.refresh()",
        "benchPanel.refresh()",
        "dataPanel.refresh()",
        "registryPanel.refresh()",
    ):
        assert marker in token_block, marker
    # Startup refresh outside the callback also covers both new panels.
    startup_block = app_js[
        app_js.index("llmProviderSelect.addEventListener") : app_js.index("button.addEventListener('click'")
    ]
    for marker in ("dataPanel.refresh();", "registryPanel.refresh();"):
        assert marker in startup_block, marker


def test_new_modules_contain_no_external_references() -> None:
    for name in ("views/data.js", "views/registry.js", "views/tags.js"):
        text = _static_module_text(name)
        assert "http://" not in text, name
        assert "https://" not in text, name

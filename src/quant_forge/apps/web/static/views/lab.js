/* Lab workbench chrome (CP6-2, D8; CP9-2 IA consolidation): surface-tab
 * controller, workbench module nav, research-flow stepper state, hash
 * routing, and per-tab status dots.
 *
 * Pure client-side state over the existing panels — no fetch calls, no new
 * endpoints. The tab panels only host per-view mounts (#result,
 * #staggered-result, #rd-result, #history-result, #bench-result,
 * #data-result, #registry-result, #docs-result, #extensions-result, plus
 * the CP10-reserved #multi-result placeholder);
 * hidden panels use the `hidden` attribute
 * so the existing render functions keep writing into their mounts while a
 * panel is inactive. Tab activation only notifies the optional `onActivate`
 * callback wired by app.js — all panel data loading stays out of this
 * module.
 *
 * CP9-2: the workbench tab (kept id lab-tab-factor, relabelled
 * 「LLM 因子工作台」) absorbs the former RD 循环 and Benchmark tabs as the
 * #workbench-rd and #report-comparison sections of its 单因子研究 module;
 * the 多因子策略回测 module is a reserved CP10 slot. Legacy #lab-tab-rd /
 * #lab-tab-bench hashes migrate through LEGACY_HASH_ALIASES — no deep link
 * dead-ends.
 *
 * Known flow-step gaps (documented, intentionally not wired to new
 * endpoints): there is no job re-attach after reload and no
 * research-history artifact detail endpoint. The factor-catalog listing and
 * per-factor evidence chain live in the CP6-3 registry view over their
 * GET-only endpoints.
 */

const TAB_IDS = [
  'lab-tab-factor', 'lab-tab-history', 'lab-tab-data',
  'lab-tab-registry', 'lab-tab-docs', 'lab-tab-extensions'
];
const MODULE_IDS = ['lab-module-single', 'lab-module-multi'];
const STEP_IDS = ['idea', 'parse', 'validate', 'report', 'rd'];
const REPORT_SECTION_IDS = [
  'report-hero',
  'report-params',
  'report-evaluation',
  'report-insample',
  'report-oos',
  'report-diagnostics',
  'report-evidence',
  'report-artifacts',
  'report-staggered',
  'report-comparison'
];
// Workbench anchors the hash router scrolls to inside the single-factor
// module: every report section plus the absorbed RD stage.
const WORKBENCH_ANCHOR_IDS = [...REPORT_SECTION_IDS, 'workbench-rd'];
// CP9-2 IA consolidation: the removed RD 循环 / Benchmark top-level tabs
// migrate to their workbench sections; applyHash normalizes the URL so
// reload / copy-link carry the new canonical fragment.
const LEGACY_HASH_ALIASES = { 'lab-tab-rd': 'workbench-rd', 'lab-tab-bench': 'report-comparison' };
// FactorDefinition id charset (core/contracts.py) pinned client-side so a
// #registry-factor-<id> anchor can activate the owning tab. Only the hash
// prefix is known here; which factor the anchor selects is applied by the
// registry view module (this module stays fetch-free and selection-free).
const REGISTRY_FACTOR_HASH = /^registry-factor-[A-Za-z][A-Za-z0-9_=-]*$/;
// Docs relpath charset — mirrors the server-side single definition
// (_DOCS_RELPATH_SEGMENT_RE in apps/web/api.py, segments joined by '/') so
// a #docs-doc-<relpath> anchor can activate the owning tab; which document
// the anchor selects is applied by the docs view module.
const DOCS_DOC_HASH = /^docs-doc-[A-Za-z0-9_][A-Za-z0-9_/.-]*$/;
// Extension id charset (extensions/manifest.py) — same discipline for
// #extensions-manifest-<id> anchors; card targeting lives in the
// extensions view module.
const EXTENSIONS_MANIFEST_HASH = /^extensions-manifest-[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$/;
const DOT_STATE_LABELS = { running: '运行中', done: '已完成', error: '出错' };

let onTabActivate = null;

function tabElement(tabId) {
  return TAB_IDS.includes(tabId) ? document.getElementById(tabId) : null;
}

function panelElement(tabId) {
  return document.getElementById(tabId.replace('lab-tab-', 'lab-panel-'));
}

function tabBaseLabel(tab) {
  if (!tab.dataset.baseLabel) tab.dataset.baseLabel = tab.textContent.trim();
  return tab.dataset.baseLabel;
}

// The active workbench module decides the workbench tab's canonical hash:
// the multi module keeps its own #lab-module-multi fragment (mirroring
// activateModule's canonical mapping) so reload / copy-link return to it;
// the single module (the default) maps back to the tab id.
function activeModuleId() {
  return MODULE_IDS.find(id => {
    const moduleTab = document.getElementById(id);
    return moduleTab && moduleTab.getAttribute('aria-selected') === 'true';
  }) || 'lab-module-single';
}

function workbenchCanonicalHash() {
  return activeModuleId() === 'lab-module-multi' ? '#lab-module-multi' : '#lab-tab-factor';
}

export function activateTab(tabId, options) {
  const target = tabElement(tabId);
  if (!target) return;
  TAB_IDS.forEach(id => {
    const tab = document.getElementById(id);
    const panel = panelElement(id);
    if (!tab || !panel) return;
    const selected = id === tabId;
    tab.setAttribute('aria-selected', selected ? 'true' : 'false');
    tab.tabIndex = selected ? 0 : -1;
    panel.hidden = !selected;
  });
  // Deep-linkable tab state without a scroll jump or history spam: the
  // URL fragment tracks the active tab via replaceState (tab ids double
  // as hash targets, so location.hash assignment would scroll to the
  // tab button itself). Callers pass `{updateHash: false}` when the
  // current fragment must survive as-is — e.g. a `#report-*` deep link
  // that activates the factor tab but keeps its section anchor in the
  // URL for reload / back / copy-link.
  const updateHash = !(options && options.updateHash === false);
  if (updateHash) {
    // The workbench tab's canonical fragment reflects the ACTIVE module
    // (A-MINOR-3): module state persists across top-tab switches, so
    // returning to the workbench while the multi module is showing must
    // write #lab-module-multi — its canonical form — instead of always
    // #lab-tab-factor, or reload / copy-link would land on the single
    // module. Every other tab keeps its own id.
    const canonical = tabId === 'lab-tab-factor' ? workbenchCanonicalHash() : `#${tabId}`;
    if (window.location.hash !== canonical) {
      window.history.replaceState(null, '', canonical);
    }
  }
  if (onTabActivate) onTabActivate(tabId);
}

export function activateModule(moduleId, options) {
  if (!MODULE_IDS.includes(moduleId)) return;
  MODULE_IDS.forEach(id => {
    const tab = document.getElementById(id);
    const panel = document.getElementById(id.replace('lab-module-', 'lab-module-panel-'));
    if (!tab || !panel) return;
    const selected = id === moduleId;
    tab.setAttribute('aria-selected', selected ? 'true' : 'false');
    tab.tabIndex = selected ? 0 : -1;
    panel.hidden = !selected;
  });
  // Canonical hash: the single module is the default, so it maps back to
  // the workbench tab hash; only the reserved multi module gets its own.
  const updateHash = !(options && options.updateHash === false);
  const canonical = moduleId === 'lab-module-single' ? '#lab-tab-factor' : `#${moduleId}`;
  if (updateHash && window.location.hash !== canonical) {
    window.history.replaceState(null, '', canonical);
  }
}

export function setTabDot(tabId, state) {
  const tab = tabElement(tabId);
  if (!tab) return;
  const dot = tab.querySelector('.lab-tab-dot');
  if (!dot) return;
  dot.classList.remove('is-running', 'is-done', 'is-error');
  const label = DOT_STATE_LABELS[state];
  if (!label) {
    dot.hidden = true;
    tab.removeAttribute('aria-label');
    return;
  }
  dot.hidden = false;
  dot.classList.add(`is-${state}`);
  tab.setAttribute('aria-label', `${tabBaseLabel(tab)}，${label}`);
}

export function setStep(stepId, state) {
  if (!STEP_IDS.includes(stepId)) return;
  const step = document.querySelector(`.lab-stepper .step[data-step="${stepId}"]`);
  if (!step) return;
  step.classList.remove('is-done', 'is-active', 'is-pending');
  step.classList.add(state === 'done' ? 'is-done' : state === 'active' ? 'is-active' : 'is-pending');
  // 因子报告 is the only step whose affordance depends on its state: its
  // step-link only becomes clickable once a full report exists.
  const link = step.querySelector('.step-link');
  if (link && stepId === 'report') link.disabled = state !== 'done';
}

function scrollToReportSection(sectionId) {
  const section = document.getElementById(sectionId);
  if (!section) return;
  section.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function applyHash(hash) {
  let target = (hash || '').replace(/^#/, '');
  const alias = LEGACY_HASH_ALIASES[target];
  if (alias) {
    // Legacy tab hashes migrate to their workbench anchors; the URL is
    // normalized so reload / copy-link carry the new canonical fragment.
    window.history.replaceState(null, '', `#${alias}`);
    target = alias;
  }
  if (TAB_IDS.includes(target)) {
    activateTab(target);
    return;
  }
  if (MODULE_IDS.includes(target)) {
    activateTab('lab-tab-factor', { updateHash: false });
    activateModule(target, { updateHash: false });
    return;
  }
  if (WORKBENCH_ANCHOR_IDS.includes(target)) {
    activateTab('lab-tab-factor', { updateHash: false });
    activateModule('lab-module-single', { updateHash: false });
    scrollToReportSection(target);
    return;
  }
  if (REGISTRY_FACTOR_HASH.test(target)) {
    // Mirrors the #report-* rule: activate the owning tab, keep the anchor
    // in the URL for reload / back / copy-link.
    activateTab('lab-tab-registry', { updateHash: false });
    return;
  }
  if (DOCS_DOC_HASH.test(target)) {
    // Same registry discipline: this module only activates the owning tab;
    // the docs view applies which document the anchor selects.
    activateTab('lab-tab-docs', { updateHash: false });
    return;
  }
  if (EXTENSIONS_MANIFEST_HASH.test(target)) {
    activateTab('lab-tab-extensions', { updateHash: false });
  }
  // Unknown hashes are ignored; the server-rendered default tab stays.
}

// P0 mode-shell precedence (agent_sidecar_frontend.md §5.6): "recognized
// expert deep link" reuses the SAME hash vocabulary applyHash already
// routes into the six-tab expert workbench (tabs, modules, report/RD
// anchors, and the registry/docs/extensions per-item deep links) — kept as
// an independent READ-ONLY predicate (no history/DOM mutation) so app.js
// can decide the landing mode BEFORE any navigation side effect runs, and
// so a recognized link wins only that navigation without ever touching the
// saved mode preference. Deliberately does not call applyHash or mutate
// anything; applyHash's own routing/normalization behavior stays untouched.
export function isRecognizedExpertHash(hash) {
  let target = (hash || '').replace(/^#/, '');
  target = LEGACY_HASH_ALIASES[target] || target;
  return (
    TAB_IDS.includes(target) ||
    MODULE_IDS.includes(target) ||
    WORKBENCH_ANCHOR_IDS.includes(target) ||
    REGISTRY_FACTOR_HASH.test(target) ||
    DOCS_DOC_HASH.test(target) ||
    EXTENSIONS_MANIFEST_HASH.test(target)
  );
}

function focusTabByOffset(currentId, offset) {
  const index = TAB_IDS.indexOf(currentId);
  if (index === -1) return;
  const next = TAB_IDS[(index + offset + TAB_IDS.length) % TAB_IDS.length];
  const tab = document.getElementById(next);
  if (!tab) return;
  tab.focus();
  activateTab(next);
}

function onTablistKeydown(event) {
  const tab = event.target.closest('.lab-tab');
  if (!tab) return;
  if (event.key === 'ArrowRight') {
    event.preventDefault();
    focusTabByOffset(tab.id, 1);
  } else if (event.key === 'ArrowLeft') {
    event.preventDefault();
    focusTabByOffset(tab.id, -1);
  } else if (event.key === 'Home') {
    event.preventDefault();
    focusTabByOffset(tab.id, -TAB_IDS.indexOf(tab.id));
  } else if (event.key === 'End') {
    event.preventDefault();
    focusTabByOffset(tab.id, TAB_IDS.length - 1 - TAB_IDS.indexOf(tab.id));
  }
}

// Module-nav roving tabindex: mirrors onTablistKeydown over MODULE_IDS
// (same Arrow / Home / End semantics, activateModule instead of
// activateTab).
function focusModuleByOffset(currentId, offset) {
  const index = MODULE_IDS.indexOf(currentId);
  if (index === -1) return;
  const next = MODULE_IDS[(index + offset + MODULE_IDS.length) % MODULE_IDS.length];
  const tab = document.getElementById(next);
  if (!tab) return;
  tab.focus();
  activateModule(next);
}

function onModuleNavKeydown(event) {
  const tab = event.target.closest('.lab-module-tab');
  if (!tab) return;
  if (event.key === 'ArrowRight') {
    event.preventDefault();
    focusModuleByOffset(tab.id, 1);
  } else if (event.key === 'ArrowLeft') {
    event.preventDefault();
    focusModuleByOffset(tab.id, -1);
  } else if (event.key === 'Home') {
    event.preventDefault();
    focusModuleByOffset(tab.id, -MODULE_IDS.indexOf(tab.id));
  } else if (event.key === 'End') {
    event.preventDefault();
    focusModuleByOffset(tab.id, MODULE_IDS.length - 1 - MODULE_IDS.indexOf(tab.id));
  }
}

function syncIdeaStep(ideaEl) {
  setStep('idea', ideaEl.value.trim() ? 'done' : 'active');
}

export function initLabTabs(options) {
  onTabActivate = (options && options.onActivate) || null;
  const tablist = document.querySelector('.lab-tabs');
  if (tablist) {
    tablist.addEventListener('click', event => {
      const tab = event.target.closest('.lab-tab');
      if (tab) activateTab(tab.id);
    });
    tablist.addEventListener('keydown', onTablistKeydown);
  }
  // Workbench module nav (CP9-2): module state persists across top-tab
  // switches — activateTab never resets it.
  const moduleNav = document.querySelector('.lab-module-nav');
  if (moduleNav) {
    moduleNav.addEventListener('click', event => {
      const tab = event.target.closest('.lab-module-tab');
      if (tab) activateModule(tab.id);
    });
    moduleNav.addEventListener('keydown', onModuleNavKeydown);
  }
  window.addEventListener('hashchange', () => applyHash(window.location.hash));
  applyHash(window.location.hash);
  const ideaEl = document.getElementById('idea');
  if (ideaEl) {
    syncIdeaStep(ideaEl);
    ideaEl.addEventListener('input', () => syncIdeaStep(ideaEl));
  }
  const stepper = document.querySelector('.lab-stepper');
  if (stepper) {
    stepper.addEventListener('click', event => {
      const link = event.target.closest('.step-link');
      if (!link || link.disabled) return;
      const action = link.dataset.stepAction;
      if (action === 'report') {
        activateTab('lab-tab-factor');
        activateModule('lab-module-single');
        scrollToReportSection('report-hero');
      } else if (action === 'rd') {
        activateTab('lab-tab-factor');
        activateModule('lab-module-single');
        scrollToReportSection('workbench-rd');
      }
    });
  }
}

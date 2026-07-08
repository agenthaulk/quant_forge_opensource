/* Lab workbench chrome (CP6-2, D8): surface-tab controller, research-flow
 * stepper state, hash routing, and per-tab status dots.
 *
 * Pure client-side state over the existing panels — no fetch calls, no new
 * endpoints. The tab panels only host per-view mounts (#result,
 * #staggered-result, #rd-result, #history-result, #bench-result,
 * #data-result, #registry-result, #docs-result, #extensions-result);
 * hidden panels use the `hidden` attribute
 * so the existing render functions keep writing into their mounts while a
 * panel is inactive. Tab activation only notifies the optional `onActivate`
 * callback wired by app.js — all panel data loading stays out of this
 * module.
 *
 * Known flow-step gaps (documented, intentionally not wired to new
 * endpoints): there is no job re-attach after reload and no
 * research-history artifact detail endpoint. The factor-catalog listing and
 * per-factor evidence chain live in the CP6-3 registry view over their
 * GET-only endpoints.
 */

const TAB_IDS = [
  'lab-tab-factor', 'lab-tab-rd', 'lab-tab-history', 'lab-tab-bench',
  'lab-tab-data', 'lab-tab-registry', 'lab-tab-docs', 'lab-tab-extensions'
];
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
  'report-staggered'
];
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
  if (updateHash && window.location.hash !== `#${tabId}`) {
    window.history.replaceState(null, '', `#${tabId}`);
  }
  if (onTabActivate) onTabActivate(tabId);
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
  const target = (hash || '').replace(/^#/, '');
  if (TAB_IDS.includes(target)) {
    activateTab(target);
    return;
  }
  if (REPORT_SECTION_IDS.includes(target)) {
    activateTab('lab-tab-factor', { updateHash: false });
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
        scrollToReportSection('report-hero');
      } else if (action === 'rd') {
        activateTab('lab-tab-rd');
      }
    });
  }
}

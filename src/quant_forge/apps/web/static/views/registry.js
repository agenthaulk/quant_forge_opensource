/* Registry view over GET /api/registry/factors (G3) and
 * GET /api/registry/factors/{factor_id} (G4) — CP6-3 master–detail.
 *
 * Selection is client-side state keyed by data-factor-id attributes — no DOM
 * element ever gets an id derived from a factor id, so native fragment
 * scrolling can never fight the #registry-factor-<id> replaceState routing.
 * views/lab.js owns activating the tab for that hash prefix; this module
 * owns which factor the anchor selects (applied on refresh and on
 * hashchange while the tab is already active).
 *
 * FP-4 discipline: factor status is a lifecycle label pill (the literal
 * draft/candidate/active/inactive/archived string); metric highlights render
 * only via metric.js metricCellHtml/metricStatusSuffix; horizon/source/total
 * fall back to 'n/a' (never 0); precomputed formulas render the key behind
 * an explicit precomputed pill instead of pretending to be an expression.
 */

import { esc, metricCellHtml, metricStatusSuffix, valueOr } from '../metric.js';
import { fetchPanelJson } from '../api.js';
import { formulaHtml } from './dsl.js';
import { researchTagChipsHtml } from './tags.js';

const registryResultEl = document.getElementById('registry-result');

// Mirrors RUN_KINDS in lineage/store.py: the filter can only request kinds
// the run index can contain, so an invalid kind query is impossible by
// construction.
const RUN_KIND_OPTIONS = ['evaluate', 'backtest', 'bench', 'rd', 'falsification'];
const REGISTRY_HASH_PREFIX = '#registry-factor-';
// FactorDefinition id charset (core/contracts.py) — the same rule lab.js
// pins for tab activation, here with a capture for the selected id.
const REGISTRY_FACTOR_HASH = /^#registry-factor-([A-Za-z][A-Za-z0-9_=-]*)$/;
const PRECOMPUTED_PREFIX = 'precomputed:';

let selectedFactorId = null;
let currentKind = '';
let listedFactorIds = [];
let detailRequestSeq = 0;
let hashScrollApplied = false;

function factorIdFromHash(hash) {
  const match = REGISTRY_FACTOR_HASH.exec(hash || '');
  return match ? match[1] : null;
}

function detailElement() {
  return document.getElementById('registry-detail');
}

function factorStatusPillHtml(status) {
  const label = status ? String(status) : 'n/a';
  // Lifecycle tone, not health: active is the only "live" state, candidate
  // is in-flight; draft/inactive/archived (and unknown labels) stay neutral.
  const tone = label === 'active'
    ? 'status-pill--ok'
    : label === 'candidate' ? 'status-pill--running' : 'status-pill--neutral';
  return `<span class="status-pill ${tone}">${esc(label)}</span>`;
}

// Additive honesty pill (never replaces the lifecycle pill above): a
// precomputed factor's DEFINITION can persist while its VALUES were only
// ever written to a past run's overlay directory this deployment does not
// read. Strictly === false renders it — null (not precomputed, or the
// presence probe itself failed) and true render exactly as before (FP-4:
// unobservable stays silent, never a guess).
function valuesUnavailablePillHtml(factor) {
  return factor.precomputed_values_present === false
    ? ' <span class="status-pill status-pill--running">值不可用</span>'
    : '';
}

function rowFormulaHtml(formula) {
  const text = formula ? String(formula) : '';
  if (text.startsWith(PRECOMPUTED_PREFIX)) {
    const key = text.slice(PRECOMPUTED_PREFIX.length);
    return `<span class="registry-row-formula"><span class="pill">precomputed</span> ${esc(key)}</span>`;
  }
  return `<span class="registry-row-formula">${esc(text)}</span>`;
}

function registryRowHtml(factor) {
  const factorId = factor.factor_id || '';
  const current = factorId && factorId === selectedFactorId ? ' aria-current="true"' : '';
  return `
      <button type="button" class="registry-row" data-factor-id="${esc(factorId)}"${current}>
        <span class="registry-row-name">${esc(factor.name || factorId)} ${factorStatusPillHtml(factor.status)}${valuesUnavailablePillHtml(factor)}</span>
        <span class="meta">${esc(factorId)} · 持有 ${esc(valueOr(factor.horizon_days, 'n/a'))} 天</span>
        ${rowFormulaHtml(factor.formula)}
      </button>`;
}

function detailPlaceholderHtml() {
  return `
      <div class="panel empty-state">
        <h3>未选择因子</h3>
        <p class="meta">选择左侧因子查看定义与证据链。</p>
      </div>`;
}

function definitionCardHtml(factor) {
  const formula = factor.formula ? String(factor.formula) : '';
  const precomputed = formula.startsWith(PRECOMPUTED_PREFIX);
  // The precomputed branch keeps esc(key) behind the precomputed pill — a
  // key is not an expression, so it is never syntax-highlighted (CP9-2).
  const formulaBlock = precomputed
    ? `<div class="formula"><span class="pill">precomputed</span> ${esc(formula.slice(PRECOMPUTED_PREFIX.length))}</div>`
    : `<div class="formula">${formulaHtml(formula)}</div>`;
  const precomputedNote = precomputed
    ? '<p class="meta"><span class="pill">precomputed</span> 公式不在本仓库，输入字段不可观测。</p>'
    : '';
  // The research-tag chips already render tags.universe_filters; a
  // definition-level filter with the identical literal value would appear
  // as a duplicate chip, so only definition-only values render here (the
  // tag copy stays; nothing is hidden, one chip per distinct value).
  const tagFilterValues = (factor.tags && factor.tags.universe_filters) || [];
  const filterChips = (factor.universe_filters || [])
    .filter(filterName => tagFilterValues.indexOf(filterName) === -1)
    .map(filterName => `<span class="pill">filter ${esc(filterName)}</span>`)
    .join(' ');
  return `
      <div class="panel hero-panel">
        <div>
          <p class="eyebrow">Registry · Factor</p>
          <h3>${esc(factor.name || factor.factor_id || '')} ${factorStatusPillHtml(factor.status)}${valuesUnavailablePillHtml(factor)}</h3>
          ${formulaBlock}
          <p>${esc(factor.description || '')}</p>
          <p class="meta">持有 ${esc(valueOr(factor.horizon_days, 'n/a'))} 天 · source ${esc(factor.source || 'n/a')}</p>
          ${precomputedNote}
          <div class="tag-chips">${filterChips ? filterChips + ' ' : ''}${researchTagChipsHtml(factor.tags || null)}</div>
        </div>
        <div class="formula-badge">factor_id<br>${esc(factor.factor_id || '')}</div>
      </div>`;
}

function kindFilterHtml() {
  const options = ['<option value="">全部</option>'].concat(
    RUN_KIND_OPTIONS.map(kind => {
      const selected = kind === currentKind ? ' selected' : '';
      return `<option value="${kind}"${selected}>${kind}</option>`;
    })
  ).join('');
  return `
        <div class="registry-runs-toolbar">
          <label for="registry-kind-filter">类型</label>
          <select id="registry-kind-filter">${options}</select>
        </div>`;
}

function runCreatedAtHtml(createdAt) {
  const text = createdAt ? String(createdAt) : '';
  const timeSplit = text.indexOf('T');
  if (timeSplit <= 0) return esc(text);
  // Same timestamp, presented as date line + muted time line; both parts
  // stay unbroken (the runs table scrolls inside .table-scroll instead of
  // wrapping timestamps mid-token).
  return '<span class="nowrap">' + esc(text.slice(0, timeSplit)) + '</span><br>'
    + '<span class="meta nowrap">' + esc(text.slice(timeSplit + 1)) + '</span>';
}

function runMetricPillHtml(name, entry) {
  const status = entry && entry.status ? String(entry.status) : '';
  // When the value is withheld (FP-4), metricCellHtml renders the status
  // label that metricStatusSuffix already carries; keep a single label by
  // adding the value part only when metric.js would render a number.
  const valueHtml = status === 'available' || status === 'legacy'
    ? `${metricCellHtml(entry)} · `
    : '';
  return `<span class="pill">${esc(name)} ${valueHtml}${metricStatusSuffix(entry)}</span>`;
}

function runRowHtml(run) {
  const dataWindow = run.data_window || {};
  const windowText = dataWindow.status === 'available'
    ? (dataWindow.start_date || '') + ' .. ' + (dataWindow.end_date || '')
    : (dataWindow.status || 'unavailable');
  // Multi-factor runs (e.g. bench) reference more than the selected factor;
  // surface that as a count pill instead of repeating the factor column.
  const extraFactorCount = (run.factor_ids || []).filter(id => id !== selectedFactorId).length;
  const extraPill = extraFactorCount > 0 ? ` <span class="pill">+${extraFactorCount} 因子</span>` : '';
  const highlights = Object.entries(run.metric_highlights || {})
    .map(([name, entry]) => runMetricPillHtml(name, entry))
    .join(' ');
  return `
          <tr>
            <td>${esc(run.kind || '')}${extraPill}<br><span class="meta">${esc(run.run_id || '')}</span></td>
            <td>${runCreatedAtHtml(run.created_at)}</td>
            <td>${esc(windowText)}<br><span class="meta">warnings ${esc(run.warnings_count ?? 'n/a')}</span></td>
            <td>${highlights || '<span class="pill">无指标摘要</span>'}</td>
          </tr>`;
}

function evidencePanelHtml(payload) {
  const runs = (payload && payload.runs) || [];
  const body = runs.map(runRowHtml).join('')
    || '<tr><td colspan="4">该因子暂无运行记录</td></tr>';
  return `
      <div class="panel">
        <h3>证据链 · 最近 ${esc(valueOr(payload.count, runs.length))} 条 / 共 ${esc(valueOr(payload.total, 'n/a'))} 条</h3>
        ${kindFilterHtml()}
        <div class="table-scroll">
          <table class="comparison-table registry-runs-table">
            <thead>
              <tr><th>类型 / run_id</th><th>时间</th><th>数据窗口 / 状态</th><th>指标摘要</th></tr>
            </thead>
            <tbody>${body}</tbody>
          </table>
        </div>
      </div>`;
}

function markSelectedRow() {
  registryResultEl.querySelectorAll('.registry-row').forEach(row => {
    if (selectedFactorId && row.dataset.factorId === selectedFactorId) {
      row.setAttribute('aria-current', 'true');
    } else {
      row.removeAttribute('aria-current');
    }
  });
}

async function loadFactorDetail(factorId, options) {
  const mount = detailElement();
  if (!mount) return;
  const seq = ++detailRequestSeq;
  mount.innerHTML = `
      <div class="panel empty-state">
        <h3>加载中</h3>
        <p class="meta">正在读取因子定义与关联运行记录。</p>
      </div>`;
  let payload = null;
  try {
    const query = currentKind ? `?kind=${encodeURIComponent(currentKind)}` : '';
    payload = await fetchPanelJson(`/api/registry/factors/${encodeURIComponent(factorId)}${query}`);
  } catch (error) {
    if (seq !== detailRequestSeq) return;
    const errorMount = detailElement();
    if (!errorMount) return;
    // The server error text arrives verbatim (missing factor -> 404 body,
    // reflected input validation -> 400 body); the list stays usable.
    errorMount.innerHTML = `<div class="notice err">${esc(error.message)}<br>`
      + '<span class="meta">因子可能不存在或已被移除；列表可能已过期，重新打开本页签可刷新。</span></div>';
    return;
  }
  if (seq !== detailRequestSeq) return;
  const liveMount = detailElement();
  if (!liveMount) return;
  if (!payload) {
    liveMount.innerHTML = '<div class="notice warn">需要控制令牌后重试。</div>';
    return;
  }
  liveMount.innerHTML = definitionCardHtml(payload.factor || {}) + evidencePanelHtml(payload);
  if (options && options.scrollIntoView) liveMount.scrollIntoView({ block: 'start' });
}

function selectFactor(factorId, options) {
  selectedFactorId = factorId;
  markSelectedRow();
  if (!(options && options.updateHash === false) && window.location.hash !== REGISTRY_HASH_PREFIX + factorId) {
    // Same replaceState-no-scroll discipline as activateTab and the
    // #report-* anchors: the URL tracks selection without history spam.
    window.history.replaceState(null, '', REGISTRY_HASH_PREFIX + factorId);
  }
  loadFactorDetail(factorId, { scrollIntoView: Boolean(options && options.scrollIntoView) });
}

export function renderRegistry(payload) {
  const factors = (payload && payload.factors) || [];
  listedFactorIds = factors.map(factor => factor.factor_id).filter(Boolean);
  if (!factors.length) {
    selectedFactorId = null;
    registryResultEl.innerHTML = `
      <div class="panel empty-state">
        <h3>暂无注册因子</h3>
        <p class="meta">在 Lab 页解析并验证一个因子后，它会出现在这里。</p>
      </div>`;
    return;
  }
  registryResultEl.innerHTML = `
    <div class="registry-layout">
      <div class="registry-list" aria-label="因子目录">${factors.map(registryRowHtml).join('')}</div>
      <div class="registry-detail" id="registry-detail">${detailPlaceholderHtml()}</div>
    </div>`;
}

// Resolves true only after a successful catalog render (never rejects); the
// detail pane degrades in place, so a detail error does not block "loaded".
export async function refreshRegistryPanel() {
  try {
    const payload = await fetchPanelJson('/api/registry/factors');
    if (!payload) return false;
    const previousSelection = selectedFactorId;
    renderRegistry(payload);
    if (!listedFactorIds.length) return true;
    const hashId = factorIdFromHash(window.location.hash);
    if (hashId && listedFactorIds.includes(hashId)) {
      const scrollOnce = !hashScrollApplied;
      hashScrollApplied = true;
      selectFactor(hashId, { updateHash: false, scrollIntoView: scrollOnce });
    } else if (previousSelection && listedFactorIds.includes(previousSelection)) {
      selectFactor(previousSelection, { updateHash: false });
    } else {
      selectedFactorId = null;
    }
    return true;
  } catch (error) {
    registryResultEl.innerHTML = `<div class="panel"><h3>已注册因子</h3><p class="meta err">${esc(error.message)}</p></div>`;
    return false;
  }
}

registryResultEl.addEventListener('click', event => {
  const row = event.target.closest('.registry-row');
  if (!row || !registryResultEl.contains(row)) return;
  const factorId = row.dataset.factorId || '';
  if (factorId) selectFactor(factorId, { updateHash: true });
});

registryResultEl.addEventListener('change', event => {
  const target = event.target;
  if (!target || target.id !== 'registry-kind-filter') return;
  currentKind = target.value || '';
  if (selectedFactorId) loadFactorDetail(selectedFactorId);
});

// Pasting a new #registry-factor-<id> anchor while the tab is already
// active: lab.js only activates the tab; selection application lives here.
window.addEventListener('hashchange', () => {
  const factorId = factorIdFromHash(window.location.hash);
  if (!factorId || !listedFactorIds.includes(factorId)) return;
  if (factorId === selectedFactorId) return;
  selectFactor(factorId, { updateHash: false, scrollIntoView: true });
});

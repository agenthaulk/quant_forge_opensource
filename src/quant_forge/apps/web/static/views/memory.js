/* Memory-review tab (SE-P4b, D8): rule governance, promoted findings/
 * failures, the priors view, and the read-only plugin-domain pane (R5).
 *
 * Self-contained by necessity, not by choice: html.py's additive budget for
 * this surface is one nav tab entry + one section container div + this
 * module's own <script type="module"> entry (SE-P4b workorder) -- it does
 * NOT extend views/lab.js's hardcoded TAB_IDS array or app.js's central
 * onActivate/lazyPanelsByTab wiring, both of which are FE-track-owned files
 * outside this module's scope. So this module additively layers its OWN
 * tablist participation (click delegation on the existing .lab-tabs
 * container; a hashchange listener that activates OR deactivates this tab
 * depending on the target; a MutationObserver on the tablist that
 * deactivates this tab the moment any other tab's aria-selected becomes
 * "true", catching programmatic activation that never touches the URL at
 * all) beside the existing controller instead of inside it -- lab.js and
 * app.js are read, never written, by this file. One known, accepted gap
 * from that split: lab.js's
 * ArrowRight/ArrowLeft/Home/End roving-tabindex handler does not know about
 * lab-tab-memory, so arrow-key cycling skips this tab; native Tab/Shift+Tab
 * and Enter/Space activation both still work (plain <button> semantics).
 *
 * The SERVER decides truth here: every action-eligibility flag
 * (can_activate/can_deactivate/can_retire/can_unretire) is server-derived
 * (apps/web/memory_review.py) and rendered as-is -- this module never
 * re-derives eligibility from state client-side.
 */

import { esc, num, valueOr } from '../metric.js';
import { controlHeaders, postJson } from '../api.js';

const TAB_ID = 'lab-tab-memory';
const PANEL_ID = 'lab-panel-memory';
const MOUNT_ID = 'memory-result';
// The other six top-level tabs (views/lab.js TAB_IDS) this module must ALSO
// deactivate when lab-tab-memory is chosen, and whose own activation must
// hide this tab's panel -- see the module docstring.
const OTHER_TAB_IDS = [
  'lab-tab-factor', 'lab-tab-history', 'lab-tab-data',
  'lab-tab-registry', 'lab-tab-docs', 'lab-tab-extensions'
];

const RULE_STATE_LABELS = {
  active: '生效中',
  never_reviewed: '待审核',
  deactivated: '已停用',
  lapsed_pending_re_review: '内容已变更 · 待复审'
};
const RULE_STATE_TONE = {
  active: 'status-pill--ok',
  never_reviewed: 'status-pill--running',
  deactivated: 'status-pill--neutral',
  lapsed_pending_re_review: 'status-pill--running'
};

const mountEl = document.getElementById(MOUNT_ID);
let loaded = false;

// ---------------------------------------------------------------------
// [pure renderers] -- no fetch, no DOM writes below this section header.
// ---------------------------------------------------------------------

function signatureHtml(signature) {
  const text = String(signature || '');
  // Full signature always ships to the client (server never truncates);
  // only the DISPLAY is clipped, with the full value in the title attr.
  return `<code class="mem-sig" title="${esc(text)}">${esc(text)}</code>`;
}

function actionButtonHtml(action, kind, signature, label) {
  const shortSig = String(signature || '').slice(0, 12);
  return (
    `<button type="button" class="mem-action-btn secondary" data-mem-action="${esc(action)}" ` +
    `data-mem-kind="${esc(kind)}" data-mem-signature="${esc(signature)}" ` +
    `aria-label="${esc(label)} ${esc(shortSig)}">${esc(label)}</button>`
  );
}

function ruleRowHtml(row, includeActions) {
  const state = String(row.state || '');
  const label = RULE_STATE_LABELS[state] || state || 'n/a';
  const tone = RULE_STATE_TONE[state] || 'status-pill--neutral';
  let actionsCell = '';
  if (includeActions) {
    const actions = [];
    if (row.can_activate) actions.push(actionButtonHtml('activate', 'rule', row.signature, '激活'));
    if (row.can_deactivate) actions.push(actionButtonHtml('deactivate', 'rule', row.signature, '停用'));
    actionsCell = `<td class="mem-actions">${actions.join(' ') || '<span class="meta">无可用操作</span>'}</td>`;
  }
  return `
    <tr>
      <td>${signatureHtml(row.signature)}</td>
      <td><span class="status-pill ${tone}">${esc(label)}</span></td>
      <td>${esc(row.statement || '')}</td>
      <td>${esc(valueOr(row.observation_count, 'n/a'))}</td>
      <td>${esc(valueOr(row.last_seen, 'n/a'))}</td>
      <td>${esc(valueOr(row.decided_at || null, 'n/a'))}</td>
      ${actionsCell}
    </tr>`;
}

function rulesTableHtml(rows, includeActions) {
  if (!rows.length) {
    return '<div class="panel empty-state"><h3>暂无规则</h3><p class="meta">尚未产生规则候选。</p></div>';
  }
  const head = includeActions ? '<th>操作</th>' : '';
  const body = rows.map(row => ruleRowHtml(row, includeActions)).join('');
  return `
    <div class="table-scroll">
      <table class="comparison-table mem-table">
        <thead><tr><th>signature</th><th>状态</th><th>statement</th><th>观测数</th><th>last_seen</th><th>decided_at</th>${head}</tr></thead>
        <tbody>${body}</tbody>
      </table>
    </div>`;
}

function promotedRowHtml(kind, row, includeActions) {
  const retired = row.review_state === 'retired';
  const tone = retired ? 'status-pill--neutral' : 'status-pill--ok';
  const label = retired ? '已退休' : '生效中';
  let actionsCell = '';
  if (includeActions) {
    const actions = [];
    if (row.can_retire) actions.push(actionButtonHtml('retire', kind, row.signature, '退休'));
    if (row.can_unretire) actions.push(actionButtonHtml('unretire', kind, row.signature, '恢复'));
    actionsCell = `<td class="mem-actions">${actions.join(' ') || '<span class="meta">无可用操作</span>'}</td>`;
  }
  return `
    <tr>
      <td>${signatureHtml(row.signature)}</td>
      <td><span class="status-pill ${tone}">${esc(label)}</span></td>
      <td>${esc(row.statement || '')}</td>
      <td>${esc(valueOr(row.observation_count, 'n/a'))}</td>
      <td>${esc(valueOr(row.first_seen, 'n/a'))}</td>
      <td>${esc(valueOr(row.last_seen, 'n/a'))}</td>
      ${actionsCell}
    </tr>`;
}

function promotedTableHtml(kind, title, rows, includeActions) {
  if (!rows.length) {
    return `<div class="panel empty-state"><h3>暂无${esc(title)}</h3><p class="meta">尚未产生${esc(title)}。</p></div>`;
  }
  const head = includeActions ? '<th>操作</th>' : '';
  const body = rows.map(row => promotedRowHtml(kind, row, includeActions)).join('');
  return `
    <div class="table-scroll">
      <table class="comparison-table mem-table">
        <thead><tr><th>signature</th><th>状态</th><th>statement</th><th>观测数</th><th>first_seen</th><th>last_seen</th>${head}</tr></thead>
        <tbody>${body}</tbody>
      </table>
    </div>`;
}

function priorCellHtml(cell) {
  const counts = cell.verdict_counts || {};
  return `
    <tr>
      <td>${esc(cell.bucket)}</td>
      <td>${esc(valueOr(cell.evidence_runs, 'n/a'))}</td>
      <td>${esc(valueOr(counts.passed, 'n/a'))}</td>
      <td>${esc(valueOr(counts.blocked, 'n/a'))}</td>
      <td>${esc(valueOr(counts.unknown, 'n/a'))}</td>
      <td>${esc(valueOr(counts.not_applicable, 'n/a'))}</td>
      <td>${num(cell.pass_rate, 2)}</td>
      <td>${num(cell.weighted_pass_rate, 2)}</td>
      <td>${cell.insufficient_sample ? '<span class="status-pill status-pill--neutral">样本不足</span>' : ''}</td>
    </tr>`;
}

function priorsTableHtml(table) {
  const rows = (table.cells || []).map(priorCellHtml).join('');
  const body = rows || '<tr><td colspan="9" class="meta">无分桶数据</td></tr>';
  return `
    <div class="panel">
      <h3>${esc(table.dimension)}</h3>
      <div class="table-scroll">
        <table class="comparison-table mem-table">
          <thead><tr><th>分桶</th><th>evidence_runs</th><th>passed</th><th>blocked</th><th>unknown</th><th>not_applicable</th><th>pass_rate</th><th>weighted</th><th></th></tr></thead>
          <tbody>${body}</tbody>
        </table>
      </div>
      <p class="meta">unbucketed（无法归入任何分桶的观测数）: ${esc(valueOr(table.unbucketed, 'n/a'))}</p>
    </div>`;
}

function priorsSectionHtml(priors, headingLevel) {
  if (!priors) {
    return '<div class="panel empty-state"><h3>暂无先验数据</h3><p class="meta">尚未产生可计算先验的结果台账。</p></div>';
  }
  const tables = (priors.tables || []).map(priorsTableHtml).join('');
  return `
    <div class="notice">
      as_of ${esc(valueOr(priors.as_of, 'n/a'))} ·
      invalid_rows（读取时判定无效并剔除的行数） ${esc(valueOr(priors.invalid_rows, 'n/a'))} ·
      oos_excluded（样本外、不参与统计的行数） ${esc(valueOr(priors.oos_excluded, 'n/a'))} ·
      total_envelopes ${esc(valueOr(priors.total_envelopes, 'n/a'))} ·
      total_evidence_runs ${esc(valueOr(priors.total_evidence_runs, 'n/a'))}
    </div>
    ${tables || '<div class="panel empty-state"><h3>暂无分桶维度</h3></div>'}`;
}

function pluginSectionHtml(plugin) {
  if (!plugin) {
    return `
      <div class="panel empty-state">
        <h3>未配置插件域</h3>
        <p class="meta">当前部署未提供插件（如 BRAIN 适配器）自有 artifact root 的配置入口，插件域面板在本版本省略；插件自身的 research memory 完全独立、只影响插件自己的工作。</p>
      </div>`;
  }
  return `
    <div class="notice">插件域数据仅供查看：规则/发现/失败均不可在本面板操作，任何决策都只能在插件自己的审核入口完成。</div>
    <div class="panel"><h3>插件 · 规则治理</h3>${rulesTableHtml(plugin.rules || [], false)}</div>
    <div class="panel"><h3>插件 · 发现</h3>${promotedTableHtml('finding', '发现', plugin.findings || [], false)}</div>
    <div class="panel"><h3>插件 · 失败</h3>${promotedTableHtml('failure', '失败', plugin.failures || [], false)}</div>
    ${priorsSectionHtml(plugin.priors)}`;
}

function tablesHtml(payload) {
  const rules = payload.rules || [];
  const findings = payload.findings || [];
  const failures = payload.failures || [];
  return `
    <div class="section-title"><h2>规则治理</h2><p>规则必须经过人工审核才会进入 steering；四态：待审核 / 生效中 / 已停用 / 内容已变更·待复审</p></div>
    ${rulesTableHtml(rules, true)}
    <div class="section-title"><h2>发现 / 失败</h2><p>自动产生、只读；可在此退休 / 恢复</p></div>
    <div class="panel"><h3>发现</h3>${promotedTableHtml('finding', '发现', findings, true)}</div>
    <div class="panel"><h3>失败</h3>${promotedTableHtml('failure', '失败', failures, true)}</div>
    <div class="section-title"><h2>先验视图</h2><p>只读、计算得出的研究先验；不进入 steering（SE-v）</p></div>
    ${priorsSectionHtml(payload.priors)}
    <div class="section-title"><h2>插件域（只读）</h2><p>R5：外部插件（例如 BRAIN）自有 research memory 的只读镜像，本面板不提供任何操作入口</p></div>
    ${pluginSectionHtml(payload.plugin)}`;
}

// ---------------------------------------------------------------------
// [controller] -- fetch, DOM writes, and event wiring below this header.
// ---------------------------------------------------------------------

function ensureStyles() {
  if (document.getElementById('mem-styles')) return;
  const style = document.createElement('style');
  style.id = 'mem-styles';
  // Additive-only budget for html.py's shared <style> block meant this
  // module owns its own presentation instead of extending that block.
  // Token-referencing declarations only (zero new color literals), so both
  // themes still come from the existing CSS variables.
  style.textContent = `
    .mem-actor-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 10px; margin: 10px 0; }
    .mem-actor-row label { margin: 0 0 4px; }
    .mem-table { min-width: 720px; }
    .mem-sig { display: inline-block; max-width: 220px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; vertical-align: bottom; }
    .mem-actions { display: flex; flex-wrap: wrap; gap: 6px; }
    .mem-action-btn { width: auto; min-height: 44px; min-width: 44px; margin: 0; padding: 8px 12px; font-size: 12px; }
    @media (max-width: 480px) { .mem-sig { max-width: 140px; } }
  `;
  document.head.appendChild(style);
}

function ensureShell() {
  ensureStyles();
  if (document.getElementById('mem-tables-mount')) return;
  mountEl.innerHTML = `
    <div class="panel">
      <h3>操作面板</h3>
      <p class="meta">规则治理、发现/失败的激活 / 停用 / 退休 / 恢复共用这里填写的审核人与理由；插件域面板没有操作入口。</p>
      <div class="mem-actor-row">
        <label for="mem-actor">审核人 actor（必填）</label>
        <input id="mem-actor" type="text" placeholder="actor" autocomplete="off">
        <label for="mem-rationale">理由 rationale（可选）</label>
        <input id="mem-rationale" type="text" placeholder="rationale" autocomplete="off">
      </div>
      <p id="mem-action-status" class="meta" role="status" aria-live="polite"></p>
    </div>
    <div id="mem-tables-mount">
      <div class="panel empty-state"><h3>加载中...</h3></div>
    </div>`;
}

function renderPayload(payload) {
  ensureShell();
  const tablesMount = document.getElementById('mem-tables-mount');
  tablesMount.innerHTML = tablesHtml(payload || {});
}

async function fetchPayload() {
  const response = await fetch('/api/memory/review', { headers: controlHeaders() });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || 'request failed');
  return body;
}

export async function refreshMemoryPanel() {
  ensureShell();
  const tablesMount = document.getElementById('mem-tables-mount');
  try {
    const payload = await fetchPayload();
    loaded = true;
    renderPayload(payload);
    return true;
  } catch (error) {
    tablesMount.innerHTML = `<div class="panel"><p class="meta err">${esc(error.message)}</p></div>`;
    return false;
  }
}

async function handleActionClick(button) {
  const action = button.dataset.memAction;
  const kind = button.dataset.memKind;
  const signature = button.dataset.memSignature;
  const actorInput = document.getElementById('mem-actor');
  const rationaleInput = document.getElementById('mem-rationale');
  const statusEl = document.getElementById('mem-action-status');
  const actor = actorInput ? actorInput.value.trim() : '';
  const rationale = rationaleInput ? rationaleInput.value.trim() : '';
  if (!actor) {
    if (statusEl) statusEl.innerHTML = '<span class="err">请先填写审核人 actor，再执行操作。</span>';
    return;
  }
  button.disabled = true;
  if (statusEl) statusEl.innerHTML = '运行中...';
  try {
    const url = kind === 'rule' ? '/api/memory/review/rule' : '/api/memory/review/promoted';
    const body = kind === 'rule'
      ? { signature_prefix: signature, action, actor, rationale }
      : { kind, signature_prefix: signature, action, actor, rationale };
    const payload = await postJson(url, body);
    if (statusEl) {
      statusEl.innerHTML = `<span class="ok">已记录 ${esc(action)} · ${esc(String(signature).slice(0, 12))}</span>`;
    }
    renderPayload(payload);
    // renderPayload replaced #mem-tables-mount, not the actor/rationale
    // shell, so the values the reviewer just typed survive this refresh.
  } catch (error) {
    if (statusEl) statusEl.innerHTML = `<span class="err">${esc(error.message)}</span>`;
    button.disabled = false;
  }
}

if (mountEl) {
  mountEl.addEventListener('click', event => {
    const button = event.target.closest('[data-mem-action]');
    if (!button) return;
    handleActionClick(button);
  });
}

// ---- Self-contained tablist participation (see module docstring) --------

function activateMemoryTab() {
  const tab = document.getElementById(TAB_ID);
  const panel = document.getElementById(PANEL_ID);
  if (!tab || !panel) return;
  OTHER_TAB_IDS.forEach(id => {
    const otherTab = document.getElementById(id);
    const otherPanel = document.getElementById(id.replace('lab-tab-', 'lab-panel-'));
    if (otherTab) {
      otherTab.setAttribute('aria-selected', 'false');
      otherTab.tabIndex = -1;
    }
    if (otherPanel) otherPanel.hidden = true;
  });
  tab.setAttribute('aria-selected', 'true');
  tab.tabIndex = 0;
  panel.hidden = false;
  if ((window.location.hash || '').replace(/^#/, '') !== TAB_ID) {
    window.history.replaceState(null, '', `#${TAB_ID}`);
  }
  if (!loaded) refreshMemoryPanel();
}

function deactivateMemoryTab() {
  const tab = document.getElementById(TAB_ID);
  const panel = document.getElementById(PANEL_ID);
  if (tab) {
    tab.setAttribute('aria-selected', 'false');
    tab.tabIndex = -1;
  }
  if (panel) panel.hidden = true;
}

const tablist = document.querySelector('.lab-tabs');
if (tablist) {
  tablist.addEventListener('click', event => {
    const tab = event.target.closest('.lab-tab');
    if (!tab) return;
    if (tab.id === TAB_ID) activateMemoryTab();
    else deactivateMemoryTab();
  });
}

// P4B-F3: two independent, complementary mechanisms close "leaves both
// tabs selected" -- neither alone covers every path the existing
// controller (views/lab.js, read but never written by this module) uses to
// switch tabs:
//
// 1. hashchange: covers user-driven navigation (address bar, back/forward,
//    an <a href="#..."> link). The ORIGINAL bug was here -- this listener
//    only ever handled "the new hash IS lab-tab-memory" and did nothing on
//    every other target, so switching to any other tab via the hash left
//    this tab's aria-selected/hidden state untouched. Now it deactivates
//    on ANY target that is not lab-tab-memory, unconditionally -- a
//    recognized TAB_IDS entry, a workbench anchor (#report-*), an unknown
//    hash, all of it.
// 2. MutationObserver on the tablist: covers PROGRAMMATIC activation that
//    never touches the URL hash at all -- e.g. app.js's Parse/Validate/RD
//    button handlers call activateTab('lab-tab-factor') directly, and
//    activateTab's default updateHash path writes the URL via
//    history.replaceState, which fires NEITHER 'hashchange' NOR
//    'popstate'. Watching the other six tabs' aria-selected attribute is
//    the only reliable, controller-agnostic signal that "some other tab
//    just became the active one" -- it fires even when nothing else would.
//
// Both call deactivateMemoryTab(), which is idempotent (a no-op if this
// tab is already inactive), so the two mechanisms overlapping on the same
// real navigation (as they do for a plain hash-driven tab switch) is
// harmless, not a double-fire bug.
window.addEventListener('hashchange', () => {
  const target = (window.location.hash || '').replace(/^#/, '');
  if (target === TAB_ID) activateMemoryTab();
  else deactivateMemoryTab();
});

if (tablist) {
  new MutationObserver(mutations => {
    for (const mutation of mutations) {
      const target = mutation.target;
      if (!target || target.id === TAB_ID) continue;
      if (OTHER_TAB_IDS.includes(target.id) && target.getAttribute('aria-selected') === 'true') {
        deactivateMemoryTab();
        return;
      }
    }
  }).observe(tablist, { attributes: true, attributeFilter: ['aria-selected'], subtree: true });
}

if ((window.location.hash || '').replace(/^#/, '') === TAB_ID) activateMemoryTab();

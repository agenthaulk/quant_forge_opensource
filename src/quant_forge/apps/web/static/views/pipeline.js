/* views/pipeline.js — pipeline A (factor_study) state machine + card
 * (agent_sidecar_frontend.md §2/§3/§5.1, WORKORDER P1).
 *
 * Pure render functions first, [controller] section last
 * (docs/frontend_contributing.md). The card embeds the canonical DSL
 * highlighter (views/dsl.js) for the formula and THE provenance badge
 * renderer (views/provenance.js) for every value -- this module never
 * invents a second way to render either (FE-L1/FE-L2). All pipeline STATE
 * (status, stages, confirm token, frozen vs. draft parameters) is exactly
 * what the server last returned; this module never guesses or locally
 * advances a status the server hasn't confirmed (FE-L3).
 *
 * Confirm-card params are ONE hashable object (apps/web/pipeline.py's
 * `parameters`) rendered at two densities -- beginner (grouped plain-
 * language lines) and expert (the 11-field grid absorbed from the deleted
 * #validation-controls) -- never two independent payloads.
 */

import { esc } from '../metric.js';
import { formulaHtml } from './dsl.js';
import { provenanceBadgeRowHtml, provenanceEntryByField } from './provenance.js';

// The 11 simulation/backtest fields absorbed from the deleted
// apps/web/html.py#validation-controls grid (WORKORDER P1 减法).
// `gridLabel` is the EXACT original <label> text (byte-for-byte, including
// each field's own " / 天" vs " bps" separator convention) so the confirm
// card's expert density reads exactly like the resident grid it replaces;
// `shortLabel` is the shorter form used by the beginner-density grouped
// lines, which already append the unit onto the VALUE via fieldDisplayValue
// and would otherwise double it up.
const PARAMETER_FIELD_META = {
  holding_days: { shortLabel: '持有期', gridLabel: '持有期 / 天', unit: '天', tip: '每次调仓后，持有多头组合的交易日数', type: 'number', min: 1, step: 1 },
  decay_days: { shortLabel: 'Decay', gridLabel: 'Decay / 天', unit: '天', tip: '信号衰减天数：0 表示不衰减，数值越大权重越平滑', type: 'number', min: 0, step: 1 },
  top_quantile: { shortLabel: 'Top Quantile', gridLabel: 'Top Quantile', unit: '', tip: '按因子值排序后，用于构建多头组合的头部比例', type: 'number', min: 0.01, max: 0.5, step: 0.01 },
  execution_delay_days: { shortLabel: 'Delay', gridLabel: 'Delay / 天', unit: '天', tip: '信号生成到实际下单之间的执行延迟天数', type: 'number', min: 1, step: 1 },
  evaluation_start: { shortLabel: '评测开始', gridLabel: '评测开始', unit: '', tip: '', type: 'date' },
  evaluation_end: { shortLabel: '评测结束', gridLabel: '评测结束', unit: '', tip: '', type: 'date' },
  backtest_start: { shortLabel: '回测开始', gridLabel: '回测开始', unit: '', tip: '', type: 'date' },
  backtest_end: { shortLabel: '回测结束', gridLabel: '回测结束', unit: '', tip: '', type: 'date' },
  commission_bps: { shortLabel: '手续费', gridLabel: '手续费 bps', unit: 'bps', tip: '', type: 'number', min: 0, step: 0.1 },
  slippage_bps: { shortLabel: '滑点', gridLabel: '滑点 bps', unit: 'bps', tip: '', type: 'number', min: 0, step: 0.1 },
  short_borrow_bps_annual: { shortLabel: '融券成本', gridLabel: '融券成本 bps/年', unit: 'bps/年', tip: '做空部分的年化融券成本，以基点计', type: 'number', min: 0, step: 1 }
};
export const PARAMETER_FIELD_ORDER = Object.keys(PARAMETER_FIELD_META);

const SUMMARY_GROUPS = [
  { label: '因子定义', fields: ['formula', 'description'] },
  { label: '股票池', fields: ['universe_filters'] },
  { label: '持有与执行', fields: ['holding_days', 'execution_delay_days', 'decay_days', 'top_quantile'] },
  { label: '评测区间', fields: ['evaluation_start', 'evaluation_end'] },
  { label: '回测区间', fields: ['backtest_start', 'backtest_end'] },
  { label: '交易成本', fields: ['commission_bps', 'slippage_bps', 'short_borrow_bps_annual'] }
];

const FACTOR_STUDY_STAGE_LABELS = { parse: '解析', confirm: '假设确认', compute: '计算', report: '报告' };

const STATUS_META = {
  draft: { pill: 'status-pill--neutral', text: '草稿' },
  awaiting_confirm: { pill: 'status-pill--running', text: '待确认' },
  running: { pill: 'status-pill--running', text: '计算中' },
  paused_failure: { pill: 'status-pill--fail', text: '已暂停' },
  completed: { pill: 'status-pill--ok', text: '已完成' },
  aborted: { pill: 'status-pill--neutral', text: '已放弃' },
  expired: { pill: 'status-pill--neutral', text: '已过期' }
};

function fieldLabel(field) {
  if (field === 'formula') return '公式';
  if (field === 'description') return '说明';
  if (field === 'universe_filters') return '股票池筛选';
  const meta = PARAMETER_FIELD_META[field];
  return meta ? meta.shortLabel : field;
}

function fieldDisplayValue(field, value) {
  if (field === 'formula') return `<span class="formula" style="font-size:15px;margin:0;">${formulaHtml(value || '')}</span>`;
  if (field === 'universe_filters') {
    const list = value || [];
    return list.length ? esc(list.join(' · ')) : '<span class="pill muted">无筛选（全市场）</span>';
  }
  if (field === 'description') return esc(value || '（无说明）');
  const meta = PARAMETER_FIELD_META[field];
  if (!meta) return esc(value === undefined || value === null ? '' : String(value));
  if (value === null || value === undefined || value === '') {
    return '<span class="metric-missing">未设置（按可用数据自动解析）</span>';
  }
  return `${esc(value)}${meta.unit ? esc(meta.unit) : ''}`;
}

/* Effective parameter set for rendering: server parameters overlaid with
 * any not-yet-saved local edits (draftOverrides). Never mutates either
 * argument. */
export function effectivePipelineParameters(pipeline, draftOverrides) {
  return Object.assign({}, pipeline.parameters || {}, draftOverrides || {});
}

/* Lossless negative-evidence projection (spec §5.1): parse-time warnings
 * (e.g. the generic-fallback-formula warning) and, once paused, the
 * failure record. Rendered ONCE, above the density split, so it is
 * visible regardless of which density the user is looking at -- density
 * changes layout, never bad news. */
export function renderNegativeEvidence(pipeline) {
  const items = [];
  (pipeline.warnings || []).forEach(warning => {
    items.push(`<div class="notice warn"><span class="status-pill status-pill--running">警告</span> ${esc(warning)}</div>`);
  });
  if (pipeline.failure) {
    items.push(
      `<div class="notice err"><span class="status-pill status-pill--fail">失败</span> ` +
      `${esc(FACTOR_STUDY_STAGE_LABELS[pipeline.failure.stage_id] || pipeline.failure.stage_id)} · ${esc(pipeline.failure.reason_code)}</div>`
    );
  }
  if (!items.length) return '';
  return `<div class="pipeline-negative-evidence">${items.join('')}</div>`;
}

export function renderDensityToggle(density) {
  return `
    <div class="pipeline-density-toggle" role="group" aria-label="确认卡展示密度">
      <button type="button" class="pipeline-density-btn" data-pipeline-density="beginner" aria-pressed="${density !== 'expert'}">简洁</button>
      <button type="button" class="pipeline-density-btn" data-pipeline-density="expert" aria-pressed="${density === 'expert'}">专家</button>
    </div>`;
}

export function renderSummaryLines(pipeline, provenanceByField, draftOverrides) {
  const parameters = effectivePipelineParameters(pipeline, draftOverrides);
  const factor = pipeline.factor || {};
  const lines = SUMMARY_GROUPS.map(group => {
    const parts = group.fields.map(field => {
      const value = Object.prototype.hasOwnProperty.call(parameters, field) ? parameters[field] : factor[field];
      const badge = provenanceBadgeRowHtml(provenanceByField[field]);
      return (
        `<span class="pipeline-summary-value">${esc(fieldLabel(field))}` +
        `${field === 'formula' || field === 'description' ? '' : ' '}${fieldDisplayValue(field, value)}</span>${badge}`
      );
    });
    return `<div class="pipeline-summary-line"><span class="pipeline-summary-label">${esc(group.label)}</span>${parts.join('')}</div>`;
  }).join('');
  return `<div class="pipeline-summary-lines">${lines}</div>`;
}

function expertFieldHtml(field, value, entry, disabled) {
  const meta = PARAMETER_FIELD_META[field];
  const labelSpan = meta.tip
    ? `<span class="term-tip" tabindex="0" data-tip="${esc(meta.tip)}">${esc(meta.gridLabel)}</span>`
    : `<span>${esc(meta.gridLabel)}</span>`;
  const attrs = [`type="${meta.type}"`, meta.min !== undefined ? `min="${meta.min}"` : '', meta.max !== undefined ? `max="${meta.max}"` : '', meta.step !== undefined ? `step="${meta.step}"` : ''].filter(Boolean).join(' ');
  const displayValue = value === null || value === undefined ? '' : esc(String(value));
  return (
    `<label>${labelSpan}<input ${attrs} data-pipeline-param-field="${field}" value="${displayValue}"${disabled ? ' disabled' : ''}>` +
    `${provenanceBadgeRowHtml(entry)}</label>`
  );
}

export function renderExpertGrid(pipeline, provenanceByField, draftOverrides, disabled) {
  const parameters = effectivePipelineParameters(pipeline, draftOverrides);
  const factorFields = ['formula', 'universe_filters'].map(field => {
    const badge = provenanceBadgeRowHtml(provenanceByField[field]);
    return `<div class="pipeline-summary-line"><span class="pipeline-summary-label">${esc(fieldLabel(field))}</span><span class="pipeline-summary-value">${fieldDisplayValue(field, pipeline.factor[field])}</span>${badge}</div>`;
  }).join('');
  const grid = PARAMETER_FIELD_ORDER.map(field => expertFieldHtml(field, parameters[field], provenanceByField[field], disabled)).join('');
  return `<div class="pipeline-summary-lines">${factorFields}</div><div class="param-grid pipeline-expert-grid" id="pipeline-expert-params">${grid}</div>`;
}

function renderDensityBody(pipeline, density, provenanceByField, draftOverrides, disabled) {
  return density === 'expert'
    ? renderExpertGrid(pipeline, provenanceByField, draftOverrides, disabled)
    : renderSummaryLines(pipeline, provenanceByField, draftOverrides);
}

export function renderStageStrip(pipeline) {
  const items = (pipeline.stages || []).map(stage => {
    const label = FACTOR_STUDY_STAGE_LABELS[stage.stage_id] || stage.stage_id;
    const current = stage.status === 'active' ? ' aria-current="step"' : '';
    return `<li class="pipeline-stage pipeline-stage--${esc(stage.status)}"${current}>${esc(label)}</li>`;
  }).join('');
  return `<ol class="pipeline-stage-strip" aria-label="管线阶段">${items}</ol>`;
}

function nextAttemptNoteHtml(pipeline) {
  const confirmed = pipeline.confirmed_parameters;
  if (!confirmed) return '';
  const current = pipeline.parameters || {};
  const changed = Object.keys(current).some(key => current[key] !== confirmed[key]);
  return changed ? '<span class="pipeline-frozen-note">已保存的编辑仅用于下次尝试</span>' : '';
}

function statusRowHtml(pipeline, title) {
  const meta = STATUS_META[pipeline.status] || { pill: 'status-pill--neutral', text: pipeline.status };
  return `
      <div class="pipeline-card-status-row">
        <span class="status-pill ${meta.pill}">${esc(meta.text)}</span>
        <h3 class="pipeline-card-title">${esc(title)}</h3>
        ${nextAttemptNoteHtml(pipeline)}
      </div>`;
}

export function renderConfirmCard(pipeline, density, provenance, draftOverrides) {
  const provenanceByField = provenanceEntryByField(provenance);
  const body = renderDensityBody(pipeline, density, provenanceByField, draftOverrides, false);
  return `
    <div class="panel pipeline-card" id="pipeline-card">
      ${statusRowHtml(pipeline, '确认因子假设')}
      ${renderStageStrip(pipeline)}
      ${renderNegativeEvidence(pipeline)}
      ${renderDensityToggle(density)}
      ${body}
      <div class="pipeline-actions">
        <button type="button" id="pipeline-confirm-btn" data-pipeline-action="confirm">确认并计算</button>
        <button type="button" class="secondary danger" data-pipeline-action="cancel">放弃本次研究</button>
      </div>
    </div>`;
}

export function renderRunningCard(pipeline, density, provenance, draftOverrides) {
  const provenanceByField = provenanceEntryByField(provenance);
  const body = renderDensityBody(pipeline, density, provenanceByField, draftOverrides, false);
  return `
    <div class="panel pipeline-card" id="pipeline-card">
      ${statusRowHtml(pipeline, '正在评测与回测')}
      ${renderStageStrip(pipeline)}
      ${renderNegativeEvidence(pipeline)}
      <p class="meta">本次运行使用确认时冻结的参数；下方编辑只会保存为下一次尝试，不会改变本次运行。</p>
      ${renderDensityToggle(density)}
      ${body}
      <div class="pipeline-actions">
        <button type="button" data-pipeline-action="save-next-attempt">保存修改（仅用于下次尝试）</button>
        <button type="button" class="secondary danger" data-pipeline-action="cancel">中断本次运行</button>
      </div>
    </div>`;
}

export function renderPausedFailureCard(pipeline, density, provenance, draftOverrides) {
  const provenanceByField = provenanceEntryByField(provenance);
  const body = renderDensityBody(pipeline, density, provenanceByField, draftOverrides, false);
  return `
    <div class="panel pipeline-card" id="pipeline-card">
      ${statusRowHtml(pipeline, '本次尝试失败')}
      ${renderStageStrip(pipeline)}
      ${renderNegativeEvidence(pipeline)}
      <p class="meta">原始输入已冻结保留。可编辑下方参数后重试（编辑仅作用于下一次尝试），或放弃本次研究。</p>
      ${renderDensityToggle(density)}
      ${body}
      <div class="pipeline-actions">
        <button type="button" data-pipeline-action="retry">重试</button>
        <button type="button" class="secondary danger" data-pipeline-action="cancel">放弃本次研究</button>
      </div>
    </div>`;
}

export function renderCompletedCard(pipeline) {
  return `
    <div class="panel pipeline-card" id="pipeline-card">
      ${statusRowHtml(pipeline, '报告已生成')}
      ${renderStageStrip(pipeline)}
      <p class="meta">报告是本次管线的终点；下方 Factor Tape 展开完整评测与回测证据。是否开始 RD 优化由你决定。</p>
    </div>`;
}

export function renderTerminalCard(pipeline) {
  const meta = STATUS_META[pipeline.status] || { pill: 'status-pill--neutral', text: pipeline.status };
  const title = pipeline.status === 'expired' ? '本次管线已过期' : '本次研究已放弃';
  return `
    <div class="panel pipeline-card" id="pipeline-card">
      <div class="pipeline-card-status-row">
        <span class="status-pill ${meta.pill}">${esc(meta.text)}</span>
        <h3 class="pipeline-card-title">${esc(title)}</h3>
      </div>
    </div>`;
}

/* Top-level dispatcher: the ONLY function callers outside this module need
 * for rendering (state -> card). `pipeline` is exactly the server's last
 * returned aggregate snapshot; `provenance` is the array the confirm/create
 * response carries (or `[]` before the first render has any). */
export function renderPipelineCard(pipeline, options) {
  if (!pipeline) return '';
  const opts = options || {};
  const density = opts.density === 'expert' ? 'expert' : 'beginner';
  const provenance = opts.provenance || [];
  const draftOverrides = opts.draftOverrides || {};
  switch (pipeline.status) {
    case 'draft':
    case 'awaiting_confirm':
      return renderConfirmCard(pipeline, density, provenance, draftOverrides);
    case 'running':
      return renderRunningCard(pipeline, density, provenance, draftOverrides);
    case 'paused_failure':
      return renderPausedFailureCard(pipeline, density, provenance, draftOverrides);
    case 'completed':
      return renderCompletedCard(pipeline);
    case 'aborted':
    case 'expired':
      return renderTerminalCard(pipeline);
    default:
      return '';
  }
}

// -----------------------------------------------------------------------
// [controller] Everything below touches fetch / DOM / events; everything
// above is a pure payload -> HTML function a design lane can drive with
// fixtures alone.
// -----------------------------------------------------------------------

import { getPipeline, listActivePipelines, postJson, sleep } from '../api.js';

const mount = document.getElementById('pipeline-card-mount');

let currentPipeline = null;
let currentProvenance = [];
let currentDensity = 'beginner';
let draftOverrides = {};
let pollToken = 0;
let onCompletedCallback = null;
let lastRenderedHtml = null;

function parametersFromExpertGrid() {
  if (!mount) return {};
  const values = {};
  mount.querySelectorAll('[data-pipeline-param-field]').forEach(input => {
    const field = input.dataset.pipelineParamField;
    const meta = PARAMETER_FIELD_META[field];
    if (!field || !meta) return;
    const raw = input.value;
    if (raw === '') {
      values[field] = null;
      return;
    }
    values[field] = meta.type === 'number' ? Number(raw) : raw;
  });
  return values;
}

/* Renders the current state into the mount. Skips the DOM write when the
 * markup is unchanged so the aria-live region only actually announces on a
 * real status/content change (spec §9 "throttled aria-live"), not on every
 * poll tick that happens to see the same snapshot. */
function renderCurrent() {
  if (!mount) return;
  if (!currentPipeline) {
    if (lastRenderedHtml !== '') {
      mount.innerHTML = '';
      lastRenderedHtml = '';
    }
    mount.hidden = true;
    return;
  }
  mount.hidden = false;
  const html = renderPipelineCard(currentPipeline, {
    density: currentDensity,
    provenance: currentProvenance,
    draftOverrides
  });
  if (html !== lastRenderedHtml) {
    mount.innerHTML = html;
    lastRenderedHtml = html;
    // Focus follows the revealed card on a stage transition (spec §9); a
    // fresh confirm card is the one case worth moving focus to, since it is
    // a genuine new gate the user must act on. Re-renders that merely
    // refresh the SAME status (e.g. a running-state badge tick) do not
    // steal focus away from whatever the user is doing.
  }
}

// The pipeline record itself is the single source for provenance (it is a
// stored field the server recomputes on every write that can change a
// value's source -- create / confirm / a next-attempt parameter edit; see
// specs/pipeline.py's `provenance` field comment). Deriving it HERE from
// whatever record setPipeline was just handed, rather than threading a
// second argument through every caller, means a caller can never
// accidentally re-render the card against a stale provenance array.
function setPipeline(pipeline) {
  const isNewPipeline = !currentPipeline || currentPipeline.pipeline_id !== pipeline.pipeline_id;
  const statusChanged = !currentPipeline || currentPipeline.status !== pipeline.status;
  currentPipeline = pipeline;
  currentProvenance = pipeline.provenance || [];
  if (isNewPipeline) draftOverrides = {};
  renderCurrent();
  if (statusChanged && pipeline.status === 'awaiting_confirm') {
    const heading = mount && mount.querySelector('.pipeline-card-title');
    if (heading) heading.setAttribute('tabindex', '-1');
    if (heading) heading.focus();
  }
  if (pipeline.status === 'running') {
    schedulePoll(pipeline.pipeline_id);
  } else if (pipeline.status === 'completed' && statusChanged && onCompletedCallback) {
    onCompletedCallback(pipeline);
  }
}

function schedulePoll(pipelineId) {
  const token = ++pollToken;
  (async () => {
    while (token === pollToken) {
      await sleep(750);
      if (token !== pollToken) return;
      let record;
      try {
        record = await getPipeline(pipelineId);
      } catch (error) {
        return;
      }
      if (token !== pollToken) return;
      setPipeline(record);
      if (record.status !== 'running') return;
    }
  })();
}

/* Creates a pipeline from an already-completed parse_idea job (FE-L3: the
 * server reads its OWN stored parse result for parseJobId; this call never
 * carries a client-claimed parser/factor payload). */
export async function createPipelineFromParseJob(parseJobId) {
  const record = await postJson('/api/pipelines', { parse_job_id: parseJobId, kind: 'factor_study' });
  currentDensity = 'beginner';
  setPipeline(record);
  return record;
}

export async function confirmCurrentPipeline() {
  if (!currentPipeline) throw new Error('没有待确认的管线');
  const overrides = currentDensity === 'expert' ? parametersFromExpertGrid() : draftOverrides;
  const body = {
    nonce: currentPipeline.confirm.nonce,
    version: currentPipeline.confirm.version,
    parameters: Object.keys(overrides).length ? overrides : undefined
  };
  const record = await postJson(`/api/pipelines/${encodeURIComponent(currentPipeline.pipeline_id)}/confirm`, body);
  draftOverrides = {};
  setPipeline(record);
  return record;
}

export async function cancelCurrentPipeline() {
  if (!currentPipeline) return null;
  const record = await postJson(`/api/pipelines/${encodeURIComponent(currentPipeline.pipeline_id)}/cancel`, {});
  setPipeline(record);
  return record;
}

export async function retryCurrentPipeline() {
  if (!currentPipeline) return null;
  const record = await postJson(`/api/pipelines/${encodeURIComponent(currentPipeline.pipeline_id)}/retry`, {});
  setPipeline(record);
  return record;
}

export async function saveNextAttemptParameters() {
  if (!currentPipeline) return null;
  const overrides = currentDensity === 'expert' ? parametersFromExpertGrid() : draftOverrides;
  const record = await postJson(`/api/pipelines/${encodeURIComponent(currentPipeline.pipeline_id)}/parameters`, {
    parameters: overrides
  });
  draftOverrides = {};
  setPipeline(record);
  return record;
}

/* Rejoin (spec §2.3): on page load, query active pipelines and re-attach
 * to whichever one is most recent -- refresh, Back, and a server restart
 * must never silently strand a running computation. A no-op (mount stays
 * hidden) when there is nothing active. */
export async function rejoinActivePipelines() {
  let pipelines;
  try {
    pipelines = await listActivePipelines();
  } catch (error) {
    return null;
  }
  if (!pipelines.length) return null;
  const [mostRecent] = pipelines;
  setPipeline(mostRecent);
  return mostRecent;
}

export function currentPipelineParameters() {
  if (!currentPipeline) return null;
  return effectivePipelineParameters(currentPipeline, draftOverrides);
}

export function currentPipelineFactorId() {
  return currentPipeline && currentPipeline.factor ? currentPipeline.factor.factor_id : null;
}

export function hasActivePipeline() {
  return Boolean(currentPipeline);
}

export function resetPipelineCard() {
  pollToken += 1;
  currentPipeline = null;
  currentProvenance = [];
  draftOverrides = {};
  renderCurrent();
}

const ACTION_HANDLERS = {
  confirm: confirmCurrentPipeline,
  cancel: cancelCurrentPipeline,
  retry: retryCurrentPipeline,
  'save-next-attempt': saveNextAttemptParameters
};

function showPipelineActionError(message) {
  if (!mount) return;
  const banner = document.createElement('div');
  banner.className = 'notice err';
  banner.innerHTML = `<span class="status-pill status-pill--fail">操作失败</span> ${esc(message)}`;
  mount.prepend(banner);
}

export function initPipelineModule(options) {
  onCompletedCallback = (options && options.onCompleted) || null;
  if (!mount) return;
  // Explicitly hidden from the very first paint (not just "empty"): a page
  // load with nothing to rejoin never calls renderCurrent() otherwise,
  // which would leave the server-rendered markup's un-hidden (but content-
  // free) mount as the only state -- harmless visually, but this makes the
  // "[hidden] until a pipeline exists" contract in html.py's mount comment
  // actually true rather than incidentally true.
  renderCurrent();
  mount.addEventListener('click', event => {
    const densityBtn = event.target.closest('[data-pipeline-density]');
    if (densityBtn) {
      // Switching density must never lose an in-progress edit: capture the
      // expert grid's current values into draftOverrides before the
      // re-render replaces those inputs.
      if (currentDensity === 'expert') Object.assign(draftOverrides, parametersFromExpertGrid());
      currentDensity = densityBtn.dataset.pipelineDensity === 'expert' ? 'expert' : 'beginner';
      renderCurrent();
      return;
    }
    const actionBtn = event.target.closest('[data-pipeline-action]');
    if (!actionBtn) return;
    const handler = ACTION_HANDLERS[actionBtn.dataset.pipelineAction];
    if (!handler) return;
    actionBtn.disabled = true;
    (async () => {
      try {
        await handler();
        // A successful action always re-renders through setPipeline, which
        // replaces this exact button's DOM node -- nothing left to re-enable.
      } catch (error) {
        actionBtn.disabled = false;
        showPipelineActionError((error && error.message) || '请求失败');
      }
    })();
  });
  mount.addEventListener('input', event => {
    const input = event.target.closest('[data-pipeline-param-field]');
    if (!input) return;
    const field = input.dataset.pipelineParamField;
    const meta = PARAMETER_FIELD_META[field];
    if (!meta) return;
    draftOverrides[field] = input.value === '' ? null : (meta.type === 'number' ? Number(input.value) : input.value);
  });
}

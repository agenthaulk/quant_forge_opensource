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
import { provenanceBadgeHtml, provenanceBadgeRowHtml, provenanceEntryByField } from './provenance.js';

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

// phase-review F11: every server-declared confirm-card field renders with
// its badge at BOTH densities -- name/horizon_days were previously absent
// from the beginner-density summary (only formula/description appeared).
const SUMMARY_GROUPS = [
  { label: '因子定义', fields: ['name', 'formula', 'description', 'horizon_days'] },
  { label: '股票池', fields: ['universe_filters'] },
  { label: '持有与执行', fields: ['holding_days', 'execution_delay_days', 'decay_days', 'top_quantile'] },
  { label: '评测区间', fields: ['evaluation_start', 'evaluation_end'] },
  { label: '回测区间', fields: ['backtest_start', 'backtest_end'] },
  { label: '交易成本', fields: ['commission_bps', 'slippage_bps', 'short_borrow_bps_annual'] }
];

const FACTOR_STUDY_STAGE_LABELS = { parse: '解析', confirm: '假设确认', compute: '计算', report: '报告' };
// Pipeline B (rd_optimize, spec §2.1): RD 确认 → RD 运行 → 排行榜. Three
// truth-mapped stages (the N rounds run inside ONE job, D11), never one light
// per round.
const RD_OPTIMIZE_STAGE_LABELS = { confirm: 'RD 确认', run: 'RD 运行', leaderboard: '排行榜' };
const STAGE_LABELS_BY_KIND = { factor_study: FACTOR_STUDY_STAGE_LABELS, rd_optimize: RD_OPTIMIZE_STAGE_LABELS };

// Objective options for the pipeline B confirm card, mirroring the resident
// #rd-objective select in apps/web/html.py (kept in lockstep by
// tests/test_web_pipeline_view.py).
const RD_OBJECTIVE_OPTIONS = [
  ['balanced', 'IC / ICIR 优先'],
  ['rank_ic', 'Rank IC'],
  ['rank_icir', 'ICIR'],
  ['annualized_return', '回测收益']
];
// Server-enforced rounds ceiling (apps/web/api.py::MAX_RD_ITERATIONS). This is
// only a UI hint: create_rd_pipeline AND _confirm_rd_pipeline both re-validate
// rounds server-side, so an edited-past-the-max value is rejected there, never
// merely here (WORKORDER pin: out-of-range rounds rejected server-side).
const RD_MAX_ITERATIONS = 5;

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
  if (field === 'name') return '因子名称';
  if (field === 'description') return '说明';
  if (field === 'horizon_days') return '预测周期';
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
  if (field === 'name') return esc(value || '（未命名）');
  if (field === 'description') return esc(value || '（无说明）');
  if (field === 'horizon_days') {
    return value === null || value === undefined ? '<span class="metric-missing">未设置</span>' : `${esc(value)}天`;
  }
  const meta = PARAMETER_FIELD_META[field];
  if (!meta) return esc(value === undefined || value === null ? '' : String(value));
  if (value === null || value === undefined || value === '') {
    return '<span class="metric-missing">未设置（按可用数据自动解析）</span>';
  }
  return `${esc(value)}${meta.unit ? esc(meta.unit) : ''}`;
}

/* phase-review F4: a field with an unsaved local edit must never display a
 * stale server-derived badge -- see views/provenance.js's
 * provenanceUnverifiedBadgeHtml. Strictly value-based (not merely "has a
 * draftOverrides entry"): editing a field back to the value the server
 * already classified is genuinely no longer "unverified". */
function isFieldDirty(pipeline, draftOverrides, field) {
  if (!draftOverrides || !Object.prototype.hasOwnProperty.call(draftOverrides, field)) return false;
  const serverValue = (pipeline.parameters || {})[field];
  return draftOverrides[field] !== serverValue;
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
    const labels = STAGE_LABELS_BY_KIND[pipeline.kind] || FACTOR_STUDY_STAGE_LABELS;
    items.push(
      `<div class="notice err"><span class="status-pill status-pill--fail">失败</span> ` +
      `${esc(labels[pipeline.failure.stage_id] || pipeline.failure.stage_id)} · ${esc(pipeline.failure.reason_code)}</div>`
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
      const badge = provenanceBadgeRowHtml(provenanceByField[field], isFieldDirty(pipeline, draftOverrides, field));
      return (
        `<span class="pipeline-summary-value">${esc(fieldLabel(field))}` +
        `${field === 'formula' || field === 'description' ? '' : ' '}${fieldDisplayValue(field, value)}</span>${badge}`
      );
    });
    return `<div class="pipeline-summary-line"><span class="pipeline-summary-label">${esc(group.label)}</span>${parts.join('')}</div>`;
  }).join('');
  return `<div class="pipeline-summary-lines">${lines}</div>`;
}

function expertFieldHtml(field, value, entry, disabled, isDirty) {
  const meta = PARAMETER_FIELD_META[field];
  const labelSpan = meta.tip
    ? `<span class="term-tip" tabindex="0" data-tip="${esc(meta.tip)}">${esc(meta.gridLabel)}</span>`
    : `<span>${esc(meta.gridLabel)}</span>`;
  const attrs = [`type="${meta.type}"`, meta.min !== undefined ? `min="${meta.min}"` : '', meta.max !== undefined ? `max="${meta.max}"` : '', meta.step !== undefined ? `step="${meta.step}"` : ''].filter(Boolean).join(' ');
  const displayValue = value === null || value === undefined ? '' : esc(String(value));
  return (
    `<label>${labelSpan}<input ${attrs} data-pipeline-param-field="${field}" value="${displayValue}"${disabled ? ' disabled' : ''}>` +
    `${provenanceBadgeRowHtml(entry, isDirty)}</label>`
  );
}

// phase-review F11: expert density previously showed only formula and
// universe_filters -- name/description/horizon_days carried badges nowhere
// on this density even though the server derives them for every pipeline.
const EXPERT_FACTOR_FIELDS = ['name', 'formula', 'description', 'horizon_days', 'universe_filters'];

export function renderExpertGrid(pipeline, provenanceByField, draftOverrides, disabled) {
  const parameters = effectivePipelineParameters(pipeline, draftOverrides);
  const factor = pipeline.factor || {};
  const factorFields = EXPERT_FACTOR_FIELDS.map(field => {
    const badge = provenanceBadgeRowHtml(provenanceByField[field]);
    return `<div class="pipeline-summary-line"><span class="pipeline-summary-label">${esc(fieldLabel(field))}</span><span class="pipeline-summary-value">${fieldDisplayValue(field, factor[field])}</span>${badge}</div>`;
  }).join('');
  const grid = PARAMETER_FIELD_ORDER.map(field => (
    expertFieldHtml(field, parameters[field], provenanceByField[field], disabled, isFieldDirty(pipeline, draftOverrides, field))
  )).join('');
  return `<div class="pipeline-summary-lines">${factorFields}</div><div class="param-grid pipeline-expert-grid" id="pipeline-expert-params">${grid}</div>`;
}

function renderDensityBody(pipeline, density, provenanceByField, draftOverrides, disabled) {
  return density === 'expert'
    ? renderExpertGrid(pipeline, provenanceByField, draftOverrides, disabled)
    : renderSummaryLines(pipeline, provenanceByField, draftOverrides);
}

export function renderStageStrip(pipeline) {
  const labels = STAGE_LABELS_BY_KIND[pipeline.kind] || FACTOR_STUDY_STAGE_LABELS;
  const items = (pipeline.stages || []).map(stage => {
    const label = labels[stage.stage_id] || stage.stage_id;
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

// -----------------------------------------------------------------------
// Pipeline B (rd_optimize) cards. Its confirm card is its OWN three-field
// gate (rounds / candidates-per-round / objective) — NOT the factor_study
// 11-parameter density grid — plus the R3.1 fixed_policy disclosure (the
// evaluation interval/sample contract is INHERITED, never re-parameterized)
// and a cost preview. The leaderboard is TERMINAL; the candidate ranking
// itself renders below via the canonical research.js renderer (FE-L2).
// -----------------------------------------------------------------------

function rdCostText(rounds, candidates) {
  const total = Number(rounds) * Number(candidates);
  const totalText = Number.isFinite(total) ? total : 'n/a';
  return `成本预告：约 ${esc(rounds)} 轮 × ${esc(candidates)} 候选 = ${esc(totalText)} 次候选评测（研究口径；真实 LLM + 全量数据耗时可能很长）。`;
}

/* F2 (compare-loop disclosure, spec §5.4): the SERVER-authoritative attempt
 * lineage + multiple-comparison honesty for pipeline B, rendered on every RD
 * card. `attempt.number` and `failed_attempts` are durable on the aggregate --
 * `failed_attempts` is NEVER cleared by a retry (unlike `failure`), so the
 * disclosure "attempt N; M prior attempts failed" survives leaving the failure
 * state. `input_hash` is the RD input fingerprint (seed + rounds/candidates/
 * objective + config); `attempt.parent_run_id` is the seed lineage. All are
 * read straight off the record — never client-computed. */
export function renderRdAttemptDisclosure(pipeline) {
  const attempt = pipeline.attempt || {};
  const attemptNo = attempt.number || 1;
  const failed = pipeline.failed_attempts || 0;
  const fingerprint = pipeline.input_hash ? String(pipeline.input_hash).slice(0, 12) : 'n/a';
  const parts = [`第 ${esc(attemptNo)} 次尝试`];
  if (failed > 0) parts.push(`此前 ${esc(failed)} 次尝试失败（多重比较披露）`);
  parts.push(`输入指纹 ${esc(fingerprint)}`);
  if (attempt.parent_run_id) parts.push(`源自 ${esc(attempt.parent_run_id)}`);
  return `<p class="meta pipeline-rd-attempts">${parts.join(' · ')}</p>`;
}

export function renderRdConfirmCard(pipeline, provenance) {
  const provenanceByField = provenanceEntryByField(provenance || []);
  const params = pipeline.parameters || {};
  const rounds = params.rounds === undefined || params.rounds === null ? 1 : params.rounds;
  const candidates = params.candidates_per_round === undefined || params.candidates_per_round === null ? 1 : params.candidates_per_round;
  const objective = params.objective || 'balanced';
  const seedFactorId = (pipeline.factor && pipeline.factor.factor_id) || '';
  const objectiveOptions = RD_OBJECTIVE_OPTIONS.map(([value, label]) =>
    `<option value="${esc(value)}"${value === objective ? ' selected' : ''}>${esc(label)}</option>`
  ).join('');
  return `
    <div class="panel pipeline-card" id="pipeline-card">
      ${statusRowHtml(pipeline, '确认 RD 优化（管线 B）')}
      ${renderStageStrip(pipeline)}
      ${renderNegativeEvidence(pipeline)}
      ${renderRdAttemptDisclosure(pipeline)}
      <p class="meta">从因子 <code>${esc(seedFactorId)}</code> 出发做 RD 优化。RD 是一条由你显式发起的独立管线——报告不会自动进入 RD（无 A→B 自动桥）。</p>
      <div class="pipeline-rd-disclosure">
        ${provenanceBadgeHtml({ source: 'fixed_policy' })}
        <span class="meta">周期与样本区间继承因子评测设置，不在此重新配置（R3.1：RD 无独立 interval 参数）。</span>
      </div>
      <div class="pipeline-rd-grid">
        <label><span>迭代轮数</span>
          <input type="number" min="1" max="${RD_MAX_ITERATIONS}" step="1" data-pipeline-rd-field="rounds" value="${esc(rounds)}">
          ${provenanceBadgeRowHtml(provenanceByField['rounds'])}</label>
        <label><span>每轮候选数</span>
          <input type="number" min="1" step="1" data-pipeline-rd-field="candidates_per_round" value="${esc(candidates)}">
          ${provenanceBadgeRowHtml(provenanceByField['candidates_per_round'])}</label>
        <label><span>目标优先级</span>
          <select data-pipeline-rd-field="objective">${objectiveOptions}</select>
          ${provenanceBadgeRowHtml(provenanceByField['objective'])}</label>
      </div>
      <p class="meta pipeline-rd-cost" id="pipeline-rd-cost">${rdCostText(rounds, candidates)}</p>
      <div class="pipeline-actions">
        <button type="button" id="pipeline-rd-confirm-btn" data-pipeline-action="confirm">确认并运行 RD</button>
        <button type="button" class="secondary danger" data-pipeline-action="cancel">放弃本次 RD</button>
      </div>
    </div>`;
}

export function renderRdRunningCard(pipeline) {
  return `
    <div class="panel pipeline-card" id="pipeline-card">
      ${statusRowHtml(pipeline, '正在运行 RD 优化')}
      ${renderStageStrip(pipeline)}
      ${renderNegativeEvidence(pipeline)}
      ${renderRdAttemptDisclosure(pipeline)}
      <p class="meta">RD 的 N 轮在同一个后台任务内运行（不拆分每轮灯）；完成后候选排行榜会在下方 RD 循环区展开。</p>
      <div class="pipeline-actions">
        <button type="button" class="secondary danger" data-pipeline-action="cancel">中断本次 RD</button>
      </div>
    </div>`;
}

export function renderRdPausedFailureCard(pipeline) {
  return `
    <div class="panel pipeline-card" id="pipeline-card">
      ${statusRowHtml(pipeline, 'RD 尝试失败')}
      ${renderStageStrip(pipeline)}
      ${renderNegativeEvidence(pipeline)}
      ${renderRdAttemptDisclosure(pipeline)}
      <p class="meta">seed 与 RD 参数已冻结保留。可沿用原参数重试，或放弃本次 RD。重试会保留此前失败尝试的计数披露。</p>
      <div class="pipeline-actions">
        <button type="button" data-pipeline-action="retry">重试（沿用原参数）</button>
        <button type="button" class="secondary danger" data-pipeline-action="cancel">放弃本次 RD</button>
      </div>
    </div>`;
}

export function renderRdLeaderboardCard(pipeline) {
  return `
    <div class="panel pipeline-card" id="pipeline-card">
      ${statusRowHtml(pipeline, '排行榜已生成')}
      ${renderStageStrip(pipeline)}
      ${renderRdAttemptDisclosure(pipeline)}
      <p class="meta">RD 优化完成，候选排行榜在下方 RD 循环区展开：external OOS 列仅供审计（不参与 winner 选择），并按 执行 / 执行后判定重复 / 执行前跳过 披露去重处置。</p>
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

// phase-review F7: paused_failure offers all three spec §2.3 exits, plus
// the pre-existing lightweight retry (Cluster A/F1/F8 -- same pipeline_id,
// same attempt lineage, the common "transient failure, try again" path).
// "edit" and "fall back to rule parse" are both heavier: each forks the
// frozen inputs into a brand-new pipeline_id with its own attempt-1
// lineage (parent_run_id pointing back here) and terminalizes THIS
// aggregate as aborted -- so unlike retry, this pipeline's own failed
// history stays put, inspectable under its own id, rather than being
// reused for the next attempt.
export function renderPausedFailureCard(pipeline, density, provenance, draftOverrides) {
  const provenanceByField = provenanceEntryByField(provenance);
  const body = renderDensityBody(pipeline, density, provenanceByField, draftOverrides, false);
  return `
    <div class="panel pipeline-card" id="pipeline-card">
      ${statusRowHtml(pipeline, '本次尝试失败')}
      ${renderStageStrip(pipeline)}
      ${renderNegativeEvidence(pipeline)}
      <p class="meta">原始输入已冻结保留。可直接重试、编辑后另起一次新尝试、改用规则解析重新开始，或放弃本次研究。</p>
      ${renderDensityToggle(density)}
      ${body}
      <div class="pipeline-actions">
        <button type="button" data-pipeline-action="retry">重试（沿用原参数）</button>
        <button type="button" data-pipeline-action="fork">编辑后另起新尝试</button>
        <button type="button" data-pipeline-action="fallback-rule-parse">改用规则解析重新开始</button>
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
  if (pipeline.kind === 'rd_optimize') {
    // Pipeline B has its own three-field confirm gate and a terminal
    // leaderboard; it shares only the shared status machine, not the
    // factor_study density body (spec §2.1 / §5.4).
    switch (pipeline.status) {
      case 'draft':
      case 'awaiting_confirm':
        return renderRdConfirmCard(pipeline, provenance);
      case 'running':
        return renderRdRunningCard(pipeline);
      case 'paused_failure':
        return renderRdPausedFailureCard(pipeline);
      case 'completed':
        return renderRdLeaderboardCard(pipeline);
      case 'aborted':
      case 'expired':
        return renderTerminalCard(pipeline);
      default:
        return '';
    }
  }
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
// P2 (agent_sidecar_frontend.md §5.5): the sidecar narration/clarify drawer
// attaches reactively to whatever pipeline this module owns. pipeline.js stays
// the single owner of pipeline state (FE-L3); narration.js is a pure consumer
// notified only on a NEW pipeline or a status change (never on every poll tick).
let onPipelineCallback = null;
let lastRenderedHtml = null;
// phase-review F12: "focus the revealed heading on meaningful transitions,
// restore focus on dismissal" -- remembers whatever had focus right before
// a card first grabbed it, so leaving the card (a terminal status, or an
// explicit reset) can hand focus back instead of silently dropping it to
// <body> once the heading's DOM node is replaced/removed.
let preCardFocusElement = null;

// F7 (durable report rejoin): persist the active pipeline id so a reload can
// re-attach even to a completed report that has fallen out of the bounded
// active listing. sessionStorage (not localStorage): scoped to this tab like
// the control token; access is guarded so a Storage exception (privacy mode,
// disabled storage) degrades to "no persistence" rather than breaking rejoin.
const ACTIVE_PIPELINE_STORAGE_KEY = 'qf_active_pipeline_id';
function persistActivePipelineId(id) {
  try {
    if (id) window.sessionStorage.setItem(ACTIVE_PIPELINE_STORAGE_KEY, id);
    else window.sessionStorage.removeItem(ACTIVE_PIPELINE_STORAGE_KEY);
  } catch (error) {
    // Storage unavailable: the id still tracks in-memory for this page view,
    // it just cannot survive a reload.
  }
}
function readActivePipelineId() {
  try {
    return window.sessionStorage.getItem(ACTIVE_PIPELINE_STORAGE_KEY) || null;
  } catch (error) {
    return null;
  }
}

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
    // Focus management for this new markup happens in setPipeline (below
    // renderCurrent's only caller path that also knows statusChanged) --
    // see the FOCUS_REVEAL_STATUSES/FOCUS_RESTORE_STATUSES comment there.
  }
}

// phase-review F12: "focus the revealed heading on meaningful transitions,
// restore focus on dismissal." A fresh confirm card (awaiting_confirm) and
// a fresh failure card (paused_failure, now presenting three new exits per
// F7) are both genuine new gates the user must act on; `running` and
// `completed` are not (see the comment above renderCurrent -- a background
// poll tick must never yank focus away from whatever the user is doing
// elsewhere on the page). `aborted`/`expired` are the "dismissal" side:
// the card's actionable content just emptied out, so focus returns to
// wherever it was BEFORE the card first took it, rather than silently
// dropping to <body> once the heading node is replaced.
const FOCUS_REVEAL_STATUSES = new Set(['awaiting_confirm', 'paused_failure']);
const FOCUS_RESTORE_STATUSES = new Set(['aborted', 'expired']);

function focusRevealedHeading() {
  const heading = mount && mount.querySelector('.pipeline-card-title');
  if (!heading) return;
  if (!preCardFocusElement) preCardFocusElement = document.activeElement;
  heading.setAttribute('tabindex', '-1');
  heading.focus();
}

function restoreFocusFromCard() {
  const target = preCardFocusElement;
  preCardFocusElement = null;
  if (target && typeof target.focus === 'function' && document.contains(target)) {
    target.focus();
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
  // F7: remember this pipeline so a reload can re-attach -- including a
  // completed report that has fallen out of the bounded active listing. A
  // reportless terminal (aborted/expired) has nothing to rejoin, so forget it.
  persistActivePipelineId(
    pipeline.status === 'aborted' || pipeline.status === 'expired' ? null : pipeline.pipeline_id
  );
  currentProvenance = pipeline.provenance || [];
  if (isNewPipeline) draftOverrides = {};
  renderCurrent();
  if ((isNewPipeline || statusChanged) && onPipelineCallback) onPipelineCallback(pipeline);
  if (statusChanged && FOCUS_REVEAL_STATUSES.has(pipeline.status)) {
    focusRevealedHeading();
  } else if (statusChanged && FOCUS_RESTORE_STATUSES.has(pipeline.status)) {
    restoreFocusFromCard();
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

/* Reads the pipeline B confirm card's own three fields (rounds /
 * candidates_per_round / objective) straight from the DOM at confirm time.
 * The server re-validates rounds against 1..MAX_RD_ITERATIONS regardless, so
 * a hand-edited out-of-range value is rejected server-side, never merely by
 * the input's own max attribute (WORKORDER pin). */
function parametersFromRdConfirm() {
  if (!mount) return {};
  const values = {};
  mount.querySelectorAll('[data-pipeline-rd-field]').forEach(input => {
    const field = input.dataset.pipelineRdField;
    if (!field) return;
    if (field === 'objective') {
      values[field] = input.value;
    } else {
      values[field] = input.value === '' ? null : Number(input.value);
    }
  });
  return values;
}

export async function confirmCurrentPipeline() {
  if (!currentPipeline) throw new Error('没有待确认的管线');
  const overrides = currentPipeline.kind === 'rd_optimize'
    ? parametersFromRdConfirm()
    : (currentDensity === 'expert' ? parametersFromExpertGrid() : draftOverrides);
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

/* Pipeline B (rd_optimize) creation, seeded from an EXPLICIT factor id (spec
 * §2.1): a completed report's factor or a registry factor. There is NO
 * automatic A->B bridge — only an explicit user action ever reaches here, and
 * nothing in the aggregate calls it on A's completion. Replaces whatever
 * terminal pipeline the card was showing with the fresh RD confirm gate. */
export async function createRdPipeline(seedFactorId, options) {
  const opts = options || {};
  const body = { kind: 'rd_optimize', seed_factor_id: seedFactorId };
  if (opts.rounds !== undefined) body.rounds = opts.rounds;
  if (opts.candidates_per_round !== undefined) body.candidates_per_round = opts.candidates_per_round;
  if (opts.objective !== undefined) body.objective = opts.objective;
  const record = await postJson('/api/pipelines', body);
  currentDensity = 'beginner';
  setPipeline(record);
  return record;
}

/* F2d (editable-formula compare loop, spec §5.3/§5.4): run a validated formula
 * edit as a NEW immutable factor_study run branched from the CURRENT pipeline.
 * `edited_by=human` is derived SERVER-side by fingerprint comparison inside
 * create_pipeline_from_edited_formula (the request asserts no edited_by). The
 * server pre-validates the edit and refuses a non-runnable one; on success the
 * card is replaced with the new run's confirm gate, while the parent (a
 * completed report) stays intact server-side for side-by-side comparison. */
export async function createEditedFormulaRun(formula, options) {
  const opts = options || {};
  // F12 (formula editor parent binding): branch from the pipeline the editor
  // was OPENED on (its id captured at open time and passed here), NOT whatever
  // `currentPipeline` has since become. Fall back to the current pipeline only
  // when no captured id was supplied. The server validates the parent exists
  // and returns a clear error if it is gone (surfaced by the caller), instead
  // of this module silently rebinding to a different pipeline.
  const parentId = opts.parentPipelineId || (currentPipeline && currentPipeline.pipeline_id);
  if (!parentId) throw new Error('没有可编辑的管线');
  const body = { formula };
  if (opts.universe_filters !== undefined) body.universe_filters = opts.universe_filters;
  if (opts.horizon_days !== undefined) body.horizon_days = opts.horizon_days;
  const record = await postJson(
    `/api/pipelines/${encodeURIComponent(parentId)}/edit-formula`,
    body
  );
  currentDensity = 'beginner';
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

/* phase-review F7 "edit" exit: forks the failed pipeline's frozen inputs
 * into a brand-new draft (own attempt-1 lineage, parent_run_id set
 * server-side) and terminalizes this one as aborted -- a single endpoint
 * call, since apps/web/pipeline.py::fork_pipeline_from_failure does both
 * atomically under its own lock. The user's PENDING edits on the paused
 * card travel with the fork (re-verify RV-F9): the server validates them
 * and badges each one human_override against the frozen baseline --
 * posting {} here used to silently discard edits the user could see. */
export async function forkCurrentPipeline() {
  if (!currentPipeline) return null;
  const overrides = currentDensity === 'expert' ? parametersFromExpertGrid() : draftOverrides;
  const body = Object.keys(overrides).length ? { parameters: overrides } : {};
  const record = await postJson(`/api/pipelines/${encodeURIComponent(currentPipeline.pipeline_id)}/fork`, body);
  draftOverrides = {};
  currentDensity = 'beginner';
  setPipeline(record);
  return record;
}

/* phase-review F7 "fall back to rule parse" exit, fully server-side
 * (re-verify RV-F10): one endpoint call. The server re-parses the failed
 * pipeline's own PERSISTED idea text in rule mode -- this module no longer
 * reads the text back, starts a parse job, or hands over a job id, so a
 * pruned parse job can't strand the exit and no client-chosen job can
 * substitute a different parse. */
export async function fallbackToRuleParseForCurrentPipeline() {
  if (!currentPipeline) return null;
  const parentId = currentPipeline.pipeline_id;
  const record = await postJson(`/api/pipelines/${encodeURIComponent(parentId)}/fallback-rule-parse`, {});
  draftOverrides = {};
  currentDensity = 'beginner';
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
  const persistedId = readActivePipelineId();
  if (pipelines.length) {
    // F7: prefer the pipeline the user was actually working on when it is still
    // in the listing (running OR recently-completed), else the most recent.
    const persisted = persistedId && pipelines.find(record => record.pipeline_id === persistedId);
    const target = persisted || pipelines[0];
    setPipeline(target);
    return target;
  }
  // F7: nothing in the active listing. If a pipeline id was persisted (e.g. a
  // completed report that has since fallen out of the bounded listing window),
  // re-attach to it DIRECTLY so a reload never strands the user's own
  // just-finished run -- fetching the record renders its card and, via the
  // completion callback, its /report. A genuinely-gone id (pruned/expired) is
  // forgotten instead of retried every load.
  if (persistedId) {
    try {
      const record = await getPipeline(persistedId);
      setPipeline(record);
      return record;
    } catch (error) {
      persistActivePipelineId(null);
      return null;
    }
  }
  return null;
}

export function currentPipelineParameters() {
  if (!currentPipeline) return null;
  return effectivePipelineParameters(currentPipeline, draftOverrides);
}

export function currentPipelineFactorId() {
  return currentPipeline && currentPipeline.factor ? currentPipeline.factor.factor_id : null;
}

export function currentPipelineId() {
  return currentPipeline ? currentPipeline.pipeline_id : null;
}

export function hasActivePipeline() {
  return Boolean(currentPipeline);
}

export function resetPipelineCard() {
  pollToken += 1;
  currentPipeline = null;
  currentProvenance = [];
  draftOverrides = {};
  persistActivePipelineId(null); // F7: an explicit reset forgets the rejoin target
  renderCurrent();
  if (onPipelineCallback) onPipelineCallback(null); // detach the narration drawer
  restoreFocusFromCard(); // F12: an explicit reset is also a "dismissal"
}

const ACTION_HANDLERS = {
  confirm: confirmCurrentPipeline,
  cancel: cancelCurrentPipeline,
  retry: retryCurrentPipeline,
  fork: forkCurrentPipeline,
  'fallback-rule-parse': fallbackToRuleParseForCurrentPipeline,
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
  onPipelineCallback = (options && options.onPipeline) || null;
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
    // Pipeline B confirm card: refresh the cost preview live as rounds /
    // candidates change (the confirmed values are still read fresh from the
    // DOM at confirm time by parametersFromRdConfirm).
    const rdInput = event.target.closest('[data-pipeline-rd-field]');
    if (rdInput) {
      const costEl = mount.querySelector('#pipeline-rd-cost');
      if (costEl) {
        const values = parametersFromRdConfirm();
        const rounds = values.rounds === undefined || values.rounds === null ? 1 : values.rounds;
        const candidates = values.candidates_per_round === undefined || values.candidates_per_round === null ? 1 : values.candidates_per_round;
        costEl.innerHTML = rdCostText(rounds, candidates);
      }
      return;
    }
    const input = event.target.closest('[data-pipeline-param-field]');
    if (!input) return;
    const field = input.dataset.pipelineParamField;
    const meta = PARAMETER_FIELD_META[field];
    if (!meta) return;
    draftOverrides[field] = input.value === '' ? null : (meta.type === 'number' ? Number(input.value) : input.value);
  });
}

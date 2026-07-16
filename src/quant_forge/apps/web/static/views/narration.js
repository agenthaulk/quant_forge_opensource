/* views/narration.js — THE narration renderer (agent_sidecar_frontend.md §5.5/§5.2/§9).
 *
 * Pure render functions first, [controller] section last
 * (docs/frontend_contributing.md). This is the ONLY place narration becomes
 * pixels. It renders typed NarrationNodes (status / question / ref /
 * action_suggestion) as an event stream ATTACHED to the pipeline card, plus
 * the tiered clarify Q&A card. It NEVER draws a metric, chart, formula, or any
 * number (FE-L2): a `ref` node links to the canonical component that already
 * shows the value; narration text carries only labels and ids.
 *
 * i18n (spec §9): stable `message_key` enums resolve to Chinese labels HERE,
 * from MESSAGE_CATALOG — the LLM narration is never the translation catalog.
 * An unknown key degrades to a neutral label (never a crash, never leaking the
 * raw key as if it were prose).
 *
 * There is NO standalone chat column (spec §5.5): the drawer sits beside the
 * pipeline card on wide viewports and collapses to a ≤375px drawer/inline
 * surface, driven entirely by CSS in html.py.
 */

import { esc } from '../metric.js';

// Stable message-key -> Chinese label. Keys are enums the server/LLM emit;
// labels live only here. `{0}`,`{1}` interpolate non-numeric arg tokens.
const MESSAGE_CATALOG = {
  'sidecar.tool.list_factors': '已查询因子列表',
  'sidecar.tool.get_factor': '已读取因子定义',
  'sidecar.tool.search_runs': '已检索研究历史',
  'sidecar.tool.get_run': '已读取运行记录',
  'sidecar.tool.get_data_summary': '已读取数据概要',
  'sidecar.tool.search_docs': '已检索文档',
  'sidecar.tool.parse_idea': '已解析因子想法',
  'sidecar.tool.validate_draft_formula': '已校验草稿公式',
  'sidecar.tool.create_pipeline': '已创建研究管线',
  'sidecar.tool.confirm_pipeline': '已确认并开始计算',
  'sidecar.tool.cancel_pipeline': '已取消管线',
  'narration.parse.completed': '解析完成，等待确认',
  'narration.report.ready': '报告已就绪',
  'narration.ref.see': '结果见',
  'clarify.title': '澄清问题',
  'clarify.blocking_pending': '有阻塞级问题未回答，暂不能开始计算'
};

const READINESS_LABEL = {
  ready: 'LLM 副驾可用',
  unavailable: '无 LLM 副驾，已降级为规则解析（证据不受影响）',
  unknown: '副驾状态未知'
};

const TIER_LABEL = { blocking: '阻塞级', semantic: '可跳过' };

const COMPONENT_LABEL = {
  'pipeline-card': '管线卡',
  'factor-tape': 'Factor Tape',
  'report-comparison': '对比报告',
  'staggered-result': '稳健性回测',
  'rd-result': 'RD 结果'
};

/* Resolve a NarrationNode to a Chinese label. args are ALWAYS non-numeric
 * tokens (the server schema rejects numbers), so this never prints a metric. */
export function narrationLabel(node) {
  const template = MESSAGE_CATALOG[node.message_key];
  const args = (node.args || []).map(a => esc(String(a)));
  if (!template) {
    // Unknown key: neutral fallback, never the bare key masquerading as prose.
    return args.length ? `${esc(node.message_key)}（${args.join(' · ')}）` : esc(node.message_key);
  }
  return template.replace(/\{(\d+)\}/g, (whole, index) => args[Number(index)] ?? whole);
}

export function renderStatusNode(node) {
  return `<li class="narration-node narration-node--status">${narrationLabel(node)}</li>`;
}

/* A ref node NEVER carries the value; it links to the component that renders
 * it (FE-L2). data-narration-ref lets the controller scroll/highlight it. */
export function renderRefNode(node) {
  const ref = node.ref || {};
  const componentLabel = COMPONENT_LABEL[ref.component_id] || esc(ref.component_id || '');
  return (
    `<li class="narration-node narration-node--ref">${narrationLabel(node)} ` +
    `<button type="button" class="narration-ref-link" data-narration-ref="${esc(ref.component_id || '')}">` +
    `${esc(componentLabel)}</button></li>`
  );
}

export function renderActionSuggestionNode(node) {
  return (
    `<li class="narration-node narration-node--action">${narrationLabel(node)} ` +
    `<button type="button" class="narration-action-btn" data-narration-action="${esc(node.action || '')}">` +
    `${esc(node.action || '')}</button></li>`
  );
}

export function renderNarrationNode(node) {
  if (node.kind === 'ref') return renderRefNode(node);
  if (node.kind === 'action_suggestion') return renderActionSuggestionNode(node);
  // question nodes render inside the clarify card, not the event stream.
  return renderStatusNode(node);
}

/* The narration event stream, attached to the pipeline card (spec §5.5). */
export function renderNarrationStream(nodes) {
  const list = (nodes || []).filter(node => node.kind !== 'question');
  if (!list.length) return '';
  const items = list.map(renderNarrationNode).join('');
  return `<ol class="narration-stream" aria-label="副驾叙述">${items}</ol>`;
}

/* One clarify question as an accessible fieldset/legend group (spec §9). The
 * default option is pre-checked; a blocking question is badged so its gate is
 * visible; skip = accept the default, recorded server-side. */
export function renderClarifyQuestion(question, answer) {
  const chosen = answer && answer.chosen_option_id;
  const options = (question.options || []).map((option, index) => {
    const isChecked = chosen ? option.id === chosen : option.is_default;
    const suffix = option.is_default ? ' <span class="clarify-default">默认</span>' : '';
    const inputId = `clarify-${esc(question.question_key)}-${index}`;
    return (
      `<label class="clarify-option" for="${inputId}">` +
      `<input type="radio" id="${inputId}" name="clarify-${esc(question.question_key)}" ` +
      `value="${esc(option.id)}"${isChecked ? ' checked' : ''}>` +
      `<span>${esc(option.label)}${suffix}</span></label>`
    );
  }).join('');
  const tierClass = question.tier === 'blocking' ? 'clarify-tier--blocking' : 'clarify-tier--semantic';
  const answered = chosen ? '<span class="clarify-answered">已回答</span>' : '';
  return (
    `<fieldset class="clarify-question" data-clarify-key="${esc(question.question_key)}">` +
    `<legend>${esc(narrationLabelForQuestion(question))} ` +
    `<span class="clarify-tier ${tierClass}">${esc(TIER_LABEL[question.tier] || question.tier)}</span>${answered}</legend>` +
    `${options}` +
    `<div class="clarify-actions">` +
    `<button type="button" class="clarify-submit" data-clarify-key="${esc(question.question_key)}">提交</button>` +
    `<button type="button" class="clarify-skip secondary" data-clarify-key="${esc(question.question_key)}">跳过（用默认）</button>` +
    `</div></fieldset>`
  );
}

function narrationLabelForQuestion(question) {
  const template = MESSAGE_CATALOG[question.question_key];
  if (template) return template;
  // Unknown clarify key: neutral, never leaking it as if it were the prompt.
  return question.question_key;
}

/* The clarify card: all posed questions + a blocking-gate banner. Rendered
 * ONCE above the narration stream inside the drawer. */
export function renderClarifyCard(clarify) {
  const questions = (clarify && clarify.questions) || [];
  if (!questions.length) return '';
  const answersByKey = {};
  ((clarify && clarify.answers) || []).forEach(a => {
    if (!a.superseded_by) answersByKey[a.question_key] = a;
  });
  const blocking = (clarify && clarify.blocking_unanswered) || [];
  const banner = blocking.length
    ? `<div class="notice warn" role="status"><span class="status-pill status-pill--running">阻塞</span> ` +
      `${esc(MESSAGE_CATALOG['clarify.blocking_pending'])}</div>`
    : '';
  const body = questions.map(question => renderClarifyQuestion(question, answersByKey[question.question_key])).join('');
  return (
    `<section class="clarify-card" aria-label="${esc(MESSAGE_CATALOG['clarify.title'])}">` +
    `<h3 class="clarify-card-title">${esc(MESSAGE_CATALOG['clarify.title'])}</h3>${banner}${body}</section>`
  );
}

export function renderReadinessLine(readiness) {
  const label = READINESS_LABEL[readiness] || READINESS_LABEL.unknown;
  return `<p class="narration-readiness" data-readiness="${esc(readiness || 'unknown')}">${esc(label)}</p>`;
}

/* Top-level drawer render (state -> HTML). `state` = {readiness, clarify,
 * narration}. The ONLY function the controller needs to turn a fetched sidecar
 * session into the drawer's markup. */
export function renderNarrationDrawer(state) {
  const opts = state || {};
  return (
    renderReadinessLine(opts.readiness) +
    renderClarifyCard(opts.clarify) +
    renderNarrationStream(opts.narration)
  );
}

// -----------------------------------------------------------------------
// [controller] Everything below touches fetch / DOM / events; everything
// above is a pure state -> HTML function a design lane can drive with
// fixtures alone.
// -----------------------------------------------------------------------

import { fetchPanelJson, postJson } from '../api.js';

const drawer = document.getElementById('narration-drawer');

let currentPipelineId = null;
let currentReadiness = 'unknown';
let lastRenderedHtml = null;

function renderInto(state) {
  if (!drawer) return;
  const html = renderNarrationDrawer(state);
  if (html === lastRenderedHtml) return;
  drawer.innerHTML = html;
  lastRenderedHtml = html;
  drawer.hidden = !html;
}

/* Readiness tri-state (spec §5.6). The pre-fetch default is `unknown` so a
 * token-redacted boot never pre-judges; a later real read upgrades in place. */
export async function refreshReadiness() {
  const payload = await fetchPanelJson('/api/sidecar/readiness');
  if (!payload) return currentReadiness; // no token yet: stay unknown
  currentReadiness = payload.readiness || 'unknown';
  renderInto({ readiness: currentReadiness, ...(await currentSessionState()) });
  return currentReadiness;
}

async function currentSessionState() {
  if (!currentPipelineId) return { clarify: null, narration: [] };
  const payload = await fetchPanelJson(`/api/sidecar/pipelines/${encodeURIComponent(currentPipelineId)}/session`);
  if (!payload) return { clarify: null, narration: [] };
  const narration = [];
  (payload.journal || []).forEach(row => (row.narration || []).forEach(node => narration.push(node)));
  return { clarify: payload.clarify, narration };
}

/* Attach the drawer to a pipeline (its narration event stream + clarify).
 * Called reactively whenever the pipeline card changes (app.js wires this to
 * the pipeline module's onPipeline hook), so narration never outruns the DOM. */
export async function attachToPipeline(pipelineId) {
  currentPipelineId = pipelineId || null;
  if (!currentPipelineId) {
    renderInto({ readiness: currentReadiness, clarify: null, narration: [] });
    return;
  }
  const state = await currentSessionState();
  renderInto({ readiness: currentReadiness, ...state });
}

async function submitClarify(questionKey, skipped) {
  if (!currentPipelineId) return;
  const optionInput = drawer.querySelector(`input[name="clarify-${cssEscape(questionKey)}"]:checked`);
  const body = skipped
    ? { question_key: questionKey, skipped: true }
    : { question_key: questionKey, option_id: optionInput ? optionInput.value : null };
  await postJson(`/api/sidecar/pipelines/${encodeURIComponent(currentPipelineId)}/clarify`, body);
  const state = await currentSessionState();
  renderInto({ readiness: currentReadiness, ...state });
}

/* Minimal attribute-safe escape for the radio group name in a querySelector
 * (question keys are dotted-snake enums, but stay defensive). */
function cssEscape(value) {
  return String(value).replace(/[^a-zA-Z0-9_.-]/g, '\\$&');
}

export function initNarrationModule() {
  if (!drawer) return;
  drawer.hidden = true;
  drawer.addEventListener('click', event => {
    const submit = event.target.closest('.clarify-submit');
    if (submit) { submitClarify(submit.dataset.clarifyKey, false).catch(() => {}); return; }
    const skip = event.target.closest('.clarify-skip');
    if (skip) { submitClarify(skip.dataset.clarifyKey, true).catch(() => {}); return; }
    const refLink = event.target.closest('[data-narration-ref]');
    if (refLink) {
      // A ref link scrolls the canonical component into view — the value lives
      // THERE (FE-L2), never here.
      const target = document.getElementById(refLink.dataset.narrationRef === 'pipeline-card' ? 'pipeline-card-mount' : 'result');
      if (target && typeof target.scrollIntoView === 'function') target.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  });
}

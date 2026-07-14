/* Entry module: control wiring for the Quant Forge single-page workbench.
 *
 * Server-rendered dynamic values (control-token flag, LLM provider options)
 * arrive through the #qf-page-config JSON block; everything executable lives
 * in these static ES modules (decision D8: no build step, no external
 * resources, no inline application script).
 */

import { esc, valueOr } from './metric.js';
import {
  cancelJob,
  configureApi,
  onControlTokenStored,
  postJson,
  waitForJob
} from './api.js';
import {
  render,
  renderParsed,
  renderStaggered,
  renderStaggeredRunning,
  resetIdeaResult,
  resetStaggeredResult
} from './views/factor.js';
import { renderResearch, resetRdResult } from './views/research.js';
import { refreshHistoryPanel } from './views/history.js';
import { refreshBenchPanel } from './views/bench.js';
import { refreshDataPanel } from './views/data.js';
import { refreshRegistryPanel } from './views/registry.js';
import { refreshDocsPanel } from './views/docs.js';
import { refreshExtensionsPanel } from './views/extensions.js';
import { activateModule, activateTab, initLabTabs, isRecognizedExpertHash, setStep, setTabDot } from './views/lab.js';
import { initSynthesisModule, refreshSynthesisPanel } from './views/synthesis.js';

const pageConfig = JSON.parse(document.getElementById('qf-page-config').textContent || '{}');
const controlTokenRequired = Boolean(pageConfig.controlTokenRequired);
configureApi({ controlTokenRequired });

const button = document.getElementById('run');
const validateButton = document.getElementById('validate-run');
const staggeredButton = document.getElementById('staggered-run');
const cancelButton = document.getElementById('cancel-run');
const statusEl = document.getElementById('status');
const errorEl = document.getElementById('error');
const llmProviderSelect = document.getElementById('llm-provider');
const llmApiKeyMode = document.getElementById('llm-api-key-mode');
const llmApiKeyInput = document.getElementById('llm-api-key');
const llmApiKeyStatus = document.getElementById('llm-api-key-status');
let llmProviderOptions = pageConfig.llmProviderOptions || [];
const validationInputs = {
  holding_days: document.getElementById('param-holding-days'),
  decay_days: document.getElementById('param-decay-days'),
  top_quantile: document.getElementById('param-top-quantile'),
  execution_delay_days: document.getElementById('param-delay-days'),
  evaluation_start: document.getElementById('param-evaluation-start'),
  evaluation_end: document.getElementById('param-evaluation-end'),
  backtest_start: document.getElementById('param-backtest-start'),
  backtest_end: document.getElementById('param-backtest-end'),
  commission_bps: document.getElementById('param-commission-bps'),
  slippage_bps: document.getElementById('param-slippage-bps'),
  short_borrow_bps_annual: document.getElementById('param-short-borrow-bps')
};
const rdRun = document.getElementById('rd-run');
const rdStart = document.getElementById('rd-start');
const rdStop = document.getElementById('rd-stop');
const rdCancel = document.getElementById('rd-cancel');
const rdStatusEl = document.getElementById('rd-status');
let activeIdeaJobId = null;
let activeRdJobId = null;
let parsedIdea = null;
let validatedFactorId = null;

// P0 mode shell (agent_sidecar_frontend.md §5.6): simple landing + expert
// workbench toggle. #idea stays the single source of truth for the idea
// text; #simple-idea is a separate presentational surface synced on every
// mode switch (never a duplicate id, never a second parse pipeline).
const simpleShell = document.getElementById('simple-shell');
const expertShell = document.getElementById('expert-shell');
const modeSimpleBtn = document.getElementById('mode-simple-btn');
const modeExpertBtn = document.getElementById('mode-expert-btn');
const ideaEl = document.getElementById('idea');
const simpleIdeaEl = document.getElementById('simple-idea');
const simpleRunButton = document.getElementById('simple-run');
const advancedParamsDetails = document.getElementById('advanced-params');

// Mode precedence (component contract 5.6): recognized expert deep link >
// saved preference > default simple. localStorage access is guarded: a
// Storage exception (privacy mode, quota, disabled storage) degrades to
// "no saved preference" / "cannot persist" rather than breaking the module.
const MODE_STORAGE_KEY = 'qf_ui_mode';
function readSavedMode() {
  try {
    const saved = window.localStorage.getItem(MODE_STORAGE_KEY);
    return saved === 'expert' || saved === 'simple' ? saved : null;
  } catch (error) {
    return null;
  }
}
function writeSavedMode(mode) {
  try {
    window.localStorage.setItem(MODE_STORAGE_KEY, mode);
  } catch (error) {
    // Storage unavailable: the mode still applies for this page view, it
    // just cannot persist across reloads.
  }
}
// Pure DOM application, no persistence: used for the deep-link-forced
// landing so a shared/bookmarked expert link wins ONLY that navigation
// without rewriting the saved preference (component contract 5.6). #idea
// is the shared draft; whichever surface is becoming hidden hands its
// current text to the surface becoming visible so mode switches never
// destroy state.
function applyMode(mode) {
  const simple = mode !== 'expert';
  if (simple) {
    simpleIdeaEl.value = ideaEl.value;
  } else {
    ideaEl.value = simpleIdeaEl.value;
    // Programmatic .value assignment fires no native 'input' event; the
    // idea-step listener (views/lab.js syncIdeaStep) needs one to stay
    // honest about whether the stepper's first step is done.
    ideaEl.dispatchEvent(new Event('input', { bubbles: true }));
  }
  simpleShell.hidden = !simple;
  expertShell.hidden = simple;
  modeSimpleBtn.setAttribute('aria-pressed', String(simple));
  modeExpertBtn.setAttribute('aria-pressed', String(!simple));
}
// Applies AND persists: used for every explicit mode decision (toggle
// clicks, the simple-run handoff into the expert view) but never for the
// deep-link-forced initial landing.
function setMode(mode) {
  applyMode(mode);
  writeSavedMode(mode);
}
// Initial landing. A recognized expert deep link (views/lab.js hash
// vocabulary) wins this navigation via applyMode (no write); otherwise the
// saved preference wins, defaulting to simple when nothing is saved yet.
const deepLinkIsExpert = isRecognizedExpertHash(window.location.hash);
applyMode(deepLinkIsExpert ? 'expert' : (readSavedMode() || 'simple'));
// Same-document hash navigation (e.g. the address bar's fragment edited
// while the page is already open) fires 'hashchange' WITHOUT re-running
// the module's top-level code above, so the initial-landing precedence
// never re-evaluates on its own. A later recognized expert hash still
// wins THAT navigation via applyMode (never setMode, same no-write rule);
// an unrecognized/cleared hash intentionally does nothing here — it must
// not force a user who deliberately toggled modes back to simple.
window.addEventListener('hashchange', () => {
  if (isRecognizedExpertHash(window.location.hash)) applyMode('expert');
});

// The workbench tab hosts two concurrent job families that share one status
// dot: the idea lane (parse / validate / staggered, all on activeIdeaJobId)
// and the RD lane (activeRdJobId). A completing job in one family must never
// downgrade or clear the other still-active family (A-MINOR-1: the earlier
// consolidation regressed to last-writer-wins). Each family tracks its own
// current state and the single dot shows the highest-priority active state
// across both — error > running > done > idle. Any state outside the
// priority map (e.g. 'clear' on cancel) means that family has no active
// state, so the dot falls back to the other family or hides.
const WORKBENCH_DOT_PRIORITY = { error: 3, running: 2, done: 1 };
const workbenchDotState = { idea: null, rd: null };
function setWorkbenchDot(family, state) {
  workbenchDotState[family] = WORKBENCH_DOT_PRIORITY[state] ? state : null;
  const winner = [workbenchDotState.idea, workbenchDotState.rd].reduce(
    (best, current) =>
      current && (!best || WORKBENCH_DOT_PRIORITY[current] > WORKBENCH_DOT_PRIORITY[best])
        ? current
        : best,
    null
  );
  // One visible dot only; setTabDot mirrors the shown state into aria-label.
  setTabDot('lab-tab-factor', winner || 'clear');
}

function clearGlobalError() {
  errorEl.textContent = '';
}
// Failed-run notices (integration finding F-011): a failed job must not
// leave its result region on the stale "running" placeholder, and the
// job's error field must reach the visible failure surface. The card
// carries a text label (never color alone), matching the parse-warning
// notice pattern; an empty error message falls back to the api-layer
// generic so the notice never renders 'undefined'.
function jobFailureReason(error) {
  return (error && error.message) || 'request failed';
}
function showJobFailureNotice(mountId, reason) {
  const mount = document.getElementById(mountId);
  if (!mount) return;
  mount.innerHTML = `<div class="notice err"><span class="status-pill status-pill--fail">失败</span> ${esc(reason)}</div>`;
}
function setValidationInputsEnabled(enabled) {
  Object.values(validationInputs).forEach(input => {
    input.disabled = !enabled;
  });
  validateButton.disabled = !enabled;
  // Reveal the advanced-params disclosure once parsing has filled it with
  // real defaults, so the (now-enabled) values are not hidden behind an
  // extra click right when they first become relevant.
  if (enabled) advancedParamsDetails.open = true;
}
function setStaggeredEnabled(enabled) {
  staggeredButton.disabled = !enabled;
}
function currentProviderOption() {
  return llmProviderOptions.find(option => option.provider === llmProviderSelect.value) || null;
}
function providerReadinessLabel(option) {
  if (option.runtimeReady === 'true') {
    return option.apiKeyEnv ? ` · env ${option.apiKeyEnv}` : ' · no auth';
  }
  return option.apiKeyEnv ? ` · missing env ${option.apiKeyEnv}` : ' · not ready';
}
function setRuntimeText(id, value) {
  const element = document.getElementById(id);
  if (element) element.textContent = value || '未配置';
}
function hydrateRuntimeStatus(status) {
  const llm = status.llm || {};
  const rd = status.rd || {};
  const paths = status.paths || {};
  const llmLabel = `${llm.provider || '未配置'} / ${llm.model || '未配置'}`;
  const rdMode = `${rd.hypothesis_mode || 'unknown'}/${rd.review_mode || 'unknown'}`;
  const rdLabel = `${rd.research_stage || 'research'} ${rdMode} ${rd.provider || ''} ${rd.model || ''}`.trim();
  setRuntimeText('runtime-llm', llmLabel);
  setRuntimeText('runtime-rd', rdLabel);
  setRuntimeText('runtime-data-root', paths.data_root || '');
  setRuntimeText('runtime-factor-root', paths.factor_root || '');
  setRuntimeText('runtime-factor-values-root', paths.factor_values_root || '');
  setRuntimeText('runtime-factor-values-overlay-root', paths.factor_values_overlay_root || '');
  setRuntimeText('runtime-artifact-root', paths.artifact_root || '');
  setRuntimeText('runtime-llm-sr', `LLM parser: ${llmLabel}`);
  setRuntimeText('runtime-rd-sr', `RD optimizer: ${rdLabel}`);
  setRuntimeText('simple-runtime-status', `LLM ${llmLabel} · RD ${rdLabel}`);
  llmProviderOptions = (llm.providers || []).map(option => ({
    provider: option.provider || '',
    model: option.model || '',
    apiKeyEnv: option['api' + '_key_env'] || '',
    runtimeReady: option['runtime' + '_ready'] || 'false'
  }));
  if (llmProviderOptions.length) {
    llmProviderSelect.innerHTML = llmProviderOptions.map(option => {
      const selected = option.provider === llm.provider ? ' selected' : '';
      return `<option value="${esc(option.provider)}"${selected}>${esc(option.provider)} / ${esc(option.model)}${esc(providerReadinessLabel(option))}</option>`;
    }).join('');
  }
  const parserOption = document.querySelector('#parser option[value="llm"]');
  if (parserOption) parserOption.textContent = `LLM 语义解析: ${llm.provider || '未配置 LLM provider'}`;
  syncLlmApiKeyControls();
}
async function refreshRuntimeStatus() {
  if (!controlTokenRequired) return;
  const token = window.sessionStorage.getItem('qf_control_token') || '';
  if (!token) return;
  const response = await fetch('/api/status', {
    headers: {Authorization: `Bearer ${token}`}
  });
  if (!response.ok) return;
  hydrateRuntimeStatus(await response.json());
}
function syncLlmApiKeyControls() {
  const option = currentProviderOption();
  const keyEnv = option && option.apiKeyEnv ? option.apiKeyEnv : '';
  const configReady = option && option.runtimeReady === 'true';
  const manual = llmApiKeyMode.value === 'manual';
  llmApiKeyInput.disabled = !manual;
  if (!manual) llmApiKeyInput.value = '';
  if (manual) {
    llmApiKeyInput.placeholder = '仅前端联调，不提交后端';
    llmApiKeyStatus.textContent = keyEnv
      ? `手动输入不会保存或提交；后端正式调用仍读取 ${keyEnv}`
      : '手动输入不会保存或提交；请在 local config 中配置 API key 环境变量名后运行';
    return;
  }
  llmApiKeyInput.placeholder = configReady
    ? `已通过 ${keyEnv || 'provider config'} 加载`
    : (keyEnv ? `未检测到 ${keyEnv}` : '当前 provider 未配置 API key 环境变量名');
  llmApiKeyStatus.textContent = configReady
    ? 'API key 已由配置文件 / 环境变量加载，前端不展示密钥'
    : 'LLM 运行前需要在本地配置 API key 环境变量名并设置对应环境变量';
}
function fillValidationInputs(parameters) {
  const values = parameters || {};
  const evaluationPeriod = ((values.evaluation || {}).test_period) || {};
  const backtest = values.backtest || {};
  const backtestSimulation = backtest.simulation || {};
  const backtestPeriod = backtest.test_period || {};
  const costs = values.transaction_costs || {};
  const resolved = {
    holding_days: values.holding_days,
    decay_days: valueOr(values.decay_days, backtestSimulation.decay_days),
    top_quantile: valueOr(values.top_quantile, backtestSimulation.top_quantile),
    execution_delay_days: valueOr(values.execution_delay_days, backtestSimulation.execution_delay_days),
    evaluation_start: valueOr(values.evaluation_start, evaluationPeriod.start),
    evaluation_end: valueOr(values.evaluation_end, evaluationPeriod.end),
    backtest_start: valueOr(values.backtest_start, backtestPeriod.start),
    backtest_end: valueOr(values.backtest_end, backtestPeriod.end),
    commission_bps: valueOr(values.commission_bps, costs.commission_bps),
    slippage_bps: valueOr(values.slippage_bps, costs.slippage_bps),
    short_borrow_bps_annual: valueOr(values.short_borrow_bps_annual, costs.short_borrow_bps_annual)
  };
  Object.entries(validationInputs).forEach(([name, input]) => {
    const value = resolved[name];
    input.value = value === undefined || value === null ? '' : value;
  });
}
function currentEvaluationSimulation() {
  const source = (parsedIdea && parsedIdea.parameters && parsedIdea.parameters.evaluation) || {};
  const simulation = source.simulation || {};
  return {
    decay_days: simulation.decay_days,
    top_quantile: simulation.top_quantile,
    execution_delay_days: simulation.execution_delay_days
  };
}
function validationParameters() {
  const evaluationStart = validationInputs.evaluation_start.value || null;
  const evaluationEnd = validationInputs.evaluation_end.value || null;
  const backtestStart = validationInputs.backtest_start.value || null;
  const backtestEnd = validationInputs.backtest_end.value || null;
  const decayDays = Number(validationInputs.decay_days.value);
  const topQuantile = Number(validationInputs.top_quantile.value);
  const executionDelayDays = Number(validationInputs.execution_delay_days.value);
  const commissionBps = Number(validationInputs.commission_bps.value);
  const slippageBps = Number(validationInputs.slippage_bps.value);
  const shortBorrowBpsAnnual = Number(validationInputs.short_borrow_bps_annual.value);
  const payload = {
    holding_days: Number(validationInputs.holding_days.value),
    decay_days: decayDays,
    top_quantile: topQuantile,
    execution_delay_days: executionDelayDays,
    evaluation_start: evaluationStart,
    evaluation_end: evaluationEnd,
    backtest_start: backtestStart,
    backtest_end: backtestEnd,
    commission_bps: commissionBps,
    slippage_bps: slippageBps,
    short_borrow_bps_annual: shortBorrowBpsAnnual,
    evaluation: {
      test_period: { start: evaluationStart, end: evaluationEnd }
    },
    backtest: {
      simulation: {
        decay_days: decayDays,
        top_quantile: topQuantile,
        execution_delay_days: executionDelayDays
      },
      test_period: { start: backtestStart, end: backtestEnd }
    },
    transaction_costs: {
      commission_bps: commissionBps,
      slippage_bps: slippageBps,
      short_borrow_bps_annual: shortBorrowBpsAnnual
    }
  };
  const evaluationSimulation = currentEvaluationSimulation();
  if (
    evaluationSimulation.decay_days !== undefined ||
    evaluationSimulation.top_quantile !== undefined ||
    evaluationSimulation.execution_delay_days !== undefined
  ) {
    payload.evaluation.simulation = evaluationSimulation;
  }
  return payload;
}
function rdPayload() {
  return {
    seed_factor_id: document.getElementById('rd-seed').value,
    objective: document.getElementById('rd-objective').value,
    max_candidates: Number(document.getElementById('rd-max').value),
    iterations: Number(document.getElementById('rd-iterations').value)
  };
}
async function submitParse(parserMode) {
  const job = await postJson('/api/jobs/parse-idea', {
      text: document.getElementById('idea').value,
      parser_mode: parserMode,
      llm_provider: llmProviderSelect.value
  });
  activeIdeaJobId = job.job_id;
  cancelButton.disabled = false;
  return waitForJob(
    job.job_id,
    statusEl,
    '已运行超过10秒，LLM 仍在解析因子',
    jobId => activeIdeaJobId === jobId
  );
}
async function submitValidation() {
  if (!parsedIdea) throw new Error('请先解析因子');
  const job = await postJson('/api/jobs/validate-idea', {
      factor: parsedIdea.factor,
      parser: parsedIdea.parser,
      parameters: validationParameters()
  });
  activeIdeaJobId = job.job_id;
  cancelButton.disabled = false;
  return waitForJob(
    job.job_id,
    statusEl,
    '已运行超过10秒，系统仍在计算因子或回测',
    jobId => activeIdeaJobId === jobId
  );
}
async function submitStaggeredEntry() {
  const factorId = validatedFactorId || (parsedIdea && parsedIdea.factor && parsedIdea.factor.factor_id);
  if (!factorId) throw new Error('请先完成验证并评测');
  const job = await postJson('/api/jobs/staggered-entry', {
      factor_id: factorId,
      parameters: validationParameters()
  });
  activeIdeaJobId = job.job_id;
  cancelButton.disabled = false;
  return waitForJob(
    job.job_id,
    statusEl,
    '已运行超过10秒，系统仍在执行首月逐日建仓稳健性回测',
    jobId => activeIdeaJobId === jobId
  );
}
// Token-gated panel refreshes skip silently until a control token is
// stored, so each panel remembers whether it has rendered real data yet.
// The tracked wrapper de-duplicates in-flight refreshes; the underlying
// view refreshers resolve true/false and never reject. `invalidate()`
// marks already-rendered data stale (integration finding F-008): a
// completed job can change what the panel endpoint returns, so a loaded
// panel must not keep serving its first render. A refresh clears the
// stale mark only when it actually starts a fetch; an invalidation that
// lands while a refresh is in flight therefore survives it, because the
// in-flight response may predate the job's writes. Surviving is not
// enough while the tab stays active: the settle recheck below chains the
// follow-up fetch that actually replaces the pre-job data.
function trackedPanelRefresh(refreshPanel) {
  let loaded = false;
  let stale = false;
  let inFlight = null;
  const tracker = {
    hasLoaded: () => loaded,
    isStale: () => stale,
    invalidate() {
      stale = true;
    },
    refresh() {
      if (!inFlight) {
        stale = false;
        inFlight = refreshPanel().then(rendered => {
          inFlight = null;
          if (rendered) loaded = true;
          // Settle recheck: an invalidation that landed while this fetch
          // was in flight means the response it de-duped into may predate
          // the job's writes, so chain exactly one follow-up refresh. The
          // follow-up clears the stale mark when it starts, so repeated
          // invalidations converge on one trailing fetch instead of
          // storming. Only a success settle chains: a failed fetch
          // (rendered=false, the refreshers never reject) keeps the stale
          // mark for the next activation rather than retry-looping here.
          if (rendered && stale) tracker.refresh();
          return rendered;
        });
      }
      return inFlight;
    }
  };
  return tracker;
}
const historyPanel = trackedPanelRefresh(refreshHistoryPanel);
const benchPanel = trackedPanelRefresh(refreshBenchPanel);
const dataPanel = trackedPanelRefresh(refreshDataPanel);
const registryPanel = trackedPanelRefresh(refreshRegistryPanel);
const docsPanel = trackedPanelRefresh(refreshDocsPanel);
const extensionsPanel = trackedPanelRefresh(refreshExtensionsPanel);
// CP10 multi-factor module: fetch/render wiring lives in views/synthesis.js;
// app.js owns activation and job-dependent invalidation. lab.js stays
// fetch-free and gains no module callback, so activation is observed
// through the reserved module panel's `hidden` attribute — one signal that
// covers nav clicks, keyboard activation, and #lab-module-multi deep links
// uniformly. The refresh stays lazy: nothing fetches while the multi module
// panel is hidden. That gate is the panel's `hidden` attribute, which tracks
// module SELECTION, not true parent-tab visibility — a selected module can
// prefetch while its parent workbench tab is hidden. The prefetch is
// harmless (it fetches exactly what the module will show), so the gate is
// "module-panel not hidden", not "the module is actually visible".
const synthesisPanel = trackedPanelRefresh(refreshSynthesisPanel);
initSynthesisModule({ onJobComplete: invalidateJobDependentPanels });
const multiModulePanel = document.getElementById('lab-module-panel-multi');
function refreshSynthesisPanelIfDue() {
  if (!multiModulePanel || multiModulePanel.hidden) return;
  if (synthesisPanel.isStale() || !synthesisPanel.hasLoaded()) synthesisPanel.refresh();
}
if (multiModulePanel) {
  new MutationObserver(refreshSynthesisPanelIfDue)
    .observe(multiModulePanel, { attributes: true, attributeFilter: ['hidden'] });
}
// CP9-2 IA consolidation: values are arrays so one tab can own several
// lazy panels — the workbench tab hosts the absorbed bench comparison
// section (#report-comparison) alongside the factor tape.
const lazyPanelsByTab = {
  'lab-tab-factor': [benchPanel],
  'lab-tab-history': [historyPanel],
  'lab-tab-data': [dataPanel],
  'lab-tab-registry': [registryPanel],
  'lab-tab-docs': [docsPanel],
  'lab-tab-extensions': [extensionsPanel]
};
// Panels whose endpoints a successfully completed job can change (F-008):
// validate/staggered/RD append run-index records (history, bench filters
// the same index) and save factor definitions (registry); parse completes
// through the same handlers, so it invalidates uniformly rather than
// encoding per-job server knowledge here. The data console reads only the
// local data root, which no job mutates, so it is not invalidated. The
// bench panel now lives on the workbench tab, so jobs that finish with
// the workbench active refresh the comparison section immediately.
const JOB_DEPENDENT_PANELS = [
  ['lab-tab-history', historyPanel],
  ['lab-tab-factor', benchPanel],
  ['lab-tab-registry', registryPanel]
];
function invalidateJobDependentPanels() {
  JOB_DEPENDENT_PANELS.forEach(([tabId, panel]) => {
    panel.invalidate();
    const tab = document.getElementById(tabId);
    // If the dependent tab is already active the user is looking at the
    // stale panel right now, so it refreshes immediately instead of
    // waiting for the next activation.
    if (tab && tab.getAttribute('aria-selected') === 'true') panel.refresh();
  });
  // CP10: the multi-factor picker lists the registry catalog, which a
  // completed validate/RD job can extend. Same stale rule, but gated on
  // module visibility instead of tab selection.
  synthesisPanel.invalidate();
  refreshSynthesisPanelIfDue();
}
initLabTabs({
  // Lazy refresh on tab activation: a panel refreshes when it has never
  // rendered real data (e.g. the startup refresh skipped because the
  // control token was missing) OR a completed job marked it stale
  // (F-008). The refresh never switches tabs; it only fills the
  // already-active panel.
  onActivate: tabId => {
    (lazyPanelsByTab[tabId] || []).forEach(panel => {
      if (panel.isStale() || !panel.hasLoaded()) panel.refresh();
    });
    // CP10: returning to the workbench tab with the multi module still
    // selected must retry its lazy load — the module panel's hidden
    // attribute does not change on tab switches, so the attribute
    // observer alone cannot see this path.
    if (tabId === 'lab-tab-factor') refreshSynthesisPanelIfDue();
  }
});
onControlTokenStored(() => {
  refreshRuntimeStatus().catch(() => {});
  historyPanel.refresh();
  benchPanel.refresh();
  dataPanel.refresh();
  registryPanel.refresh();
  docsPanel.refresh();
  extensionsPanel.refresh();
  // CP10 stays lazy even on token arrival: only fetch when the multi module
  // panel is not hidden; otherwise the next activation retries.
  refreshSynthesisPanelIfDue();
});
llmProviderSelect.addEventListener('change', syncLlmApiKeyControls);
llmApiKeyMode.addEventListener('change', syncLlmApiKeyControls);
syncLlmApiKeyControls();
refreshRuntimeStatus().catch(() => {});
historyPanel.refresh();
benchPanel.refresh();
dataPanel.refresh();
registryPanel.refresh();
docsPanel.refresh();
extensionsPanel.refresh();
button.addEventListener('click', async () => {
  button.disabled = true;
  validateButton.disabled = true;
  cancelButton.disabled = true;
  activateTab('lab-tab-factor');
  activateModule('lab-module-single');
  setWorkbenchDot('idea', 'running');
  setStep('parse', 'active');
  setStep('validate', 'pending');
  setStep('report', 'pending');
  clearGlobalError();
  resetIdeaResult('解析中', '因子解析完成后，公式和默认评测参数会在这里刷新。');
  statusEl.textContent = '解析中...';
  parsedIdea = null;
  validatedFactorId = null;
  fillValidationInputs({});
  setValidationInputsEnabled(false);
  setStaggeredEnabled(false);
  resetStaggeredResult();
  const parserMode = document.getElementById('parser').value;
  try {
    const payload = await submitParse(parserMode);
    parsedIdea = payload;
    fillValidationInputs(payload.parameters);
    setValidationInputsEnabled(true);
    renderParsed(payload);
    setStep('parse', 'done');
    setWorkbenchDot('idea', 'done');
    invalidateJobDependentPanels();
    statusEl.innerHTML = '<span class="ok">解析完成，等待确认参数</span>';
  } catch (error) {
    if (error.message === '运行已中断') {
      setStep('parse', 'pending');
      setWorkbenchDot('idea', 'clear');
      statusEl.innerHTML = '<span class="warn">运行已中断</span>';
      return;
    }
    if (parserMode === 'llm') {
      const fallback = window.confirm(`LLM 无法使用：${error.message}\n\n是否改用本地规则解析？`);
      if (fallback) {
        try {
          const payload = await submitParse('rule');
          parsedIdea = payload;
          fillValidationInputs(payload.parameters);
          setValidationInputsEnabled(true);
          renderParsed(payload);
          setStep('parse', 'done');
          setWorkbenchDot('idea', 'done');
          invalidateJobDependentPanels();
          statusEl.innerHTML = '<span class="ok">已使用本地规则解析，等待确认参数</span>';
          return;
        } catch (fallbackError) {
          const reason = jobFailureReason(fallbackError);
          setStep('parse', 'pending');
          setWorkbenchDot('idea', 'error');
          showJobFailureNotice('result', reason);
          errorEl.textContent = reason;
          statusEl.textContent = '运行失败';
          return;
        }
      }
    }
    const reason = jobFailureReason(error);
    setStep('parse', 'pending');
    setWorkbenchDot('idea', 'error');
    showJobFailureNotice('result', reason);
    errorEl.textContent = reason;
    statusEl.textContent = '运行失败';
  } finally {
    activeIdeaJobId = null;
    button.disabled = false;
    cancelButton.disabled = true;
  }
});
validateButton.addEventListener('click', async () => {
  validateButton.disabled = true;
  button.disabled = true;
  cancelButton.disabled = true;
  activateTab('lab-tab-factor');
  activateModule('lab-module-single');
  setWorkbenchDot('idea', 'running');
  setStep('validate', 'active');
  clearGlobalError();
  resetIdeaResult('验证与评测中', '评测完成后，IC、回测收益和 artifact 路径会在这里刷新。');
  statusEl.textContent = '验证与评测中...';
  try {
    const payload = await submitValidation();
    render(payload);
    parsedIdea = {
      parser: payload.parser,
      factor: payload.factor,
      parameters: payload.parameters || validationParameters()
    };
    validatedFactorId = payload.factor.factor_id;
    fillValidationInputs(parsedIdea.parameters);
    document.getElementById('rd-seed').value = payload.factor.factor_id;
    setStaggeredEnabled(true);
    setStep('validate', 'done');
    setStep('report', 'done');
    setWorkbenchDot('idea', 'done');
    invalidateJobDependentPanels();
    statusEl.innerHTML = '<span class="ok">验证完成</span>';
  } catch (error) {
    if (error.message === '运行已中断') {
      setStep('validate', 'pending');
      setWorkbenchDot('idea', 'clear');
      statusEl.innerHTML = '<span class="warn">运行已中断</span>';
      return;
    }
    const reason = jobFailureReason(error);
    setStep('validate', 'pending');
    setWorkbenchDot('idea', 'error');
    showJobFailureNotice('result', reason);
    errorEl.textContent = reason;
    statusEl.textContent = '验证失败';
  } finally {
    activeIdeaJobId = null;
    button.disabled = false;
    cancelButton.disabled = true;
    setValidationInputsEnabled(Boolean(parsedIdea));
    setStaggeredEnabled(Boolean(validatedFactorId));
  }
});
staggeredButton.addEventListener('click', async () => {
  staggeredButton.disabled = true;
  validateButton.disabled = true;
  button.disabled = true;
  cancelButton.disabled = true;
  activateTab('lab-tab-factor');
  activateModule('lab-module-single');
  setWorkbenchDot('idea', 'running');
  clearGlobalError();
  renderStaggeredRunning();
  statusEl.textContent = '首月逐日建仓稳健性回测中...';
  try {
    const payload = await submitStaggeredEntry();
    renderStaggered(payload);
    setWorkbenchDot('idea', 'done');
    invalidateJobDependentPanels();
    const staggeredSection = document.getElementById('report-staggered');
    if (staggeredSection) staggeredSection.scrollIntoView({ behavior: 'smooth', block: 'start' });
    statusEl.innerHTML = '<span class="ok">首月逐日建仓稳健性回测完成</span>';
  } catch (error) {
    if (error.message === '运行已中断') {
      setWorkbenchDot('idea', 'clear');
      statusEl.innerHTML = '<span class="warn">运行已中断</span>';
      return;
    }
    const reason = jobFailureReason(error);
    setWorkbenchDot('idea', 'error');
    showJobFailureNotice('staggered-result', reason);
    errorEl.textContent = reason;
    statusEl.textContent = '稳健性回测失败';
  } finally {
    activeIdeaJobId = null;
    button.disabled = false;
    cancelButton.disabled = true;
    setValidationInputsEnabled(Boolean(parsedIdea));
    setStaggeredEnabled(Boolean(validatedFactorId));
  }
});
cancelButton.addEventListener('click', async () => {
  const jobId = activeIdeaJobId;
  if (!jobId) return;
  cancelButton.disabled = true;
  clearGlobalError();
  resetIdeaResult('中断中', '已请求取消当前运行，等待后端安全停止。');
  statusEl.innerHTML = '<span class="warn">已请求中断本次运行；当前安全阶段结束后停止</span>';
  try {
    await cancelJob(jobId);
  } catch (error) {
    errorEl.textContent = error.message;
    cancelButton.disabled = false;
  }
});
rdRun.addEventListener('click', async () => {
  rdRun.disabled = true;
  rdCancel.disabled = true;
  activateTab('lab-tab-factor');
  activateModule('lab-module-single');
  const rdSection = document.getElementById('workbench-rd');
  if (rdSection) rdSection.scrollIntoView({ behavior: 'smooth', block: 'start' });
  setWorkbenchDot('rd', 'running');
  setStep('rd', 'active');
  clearGlobalError();
  resetRdResult('RD 运行中', 'RD 候选、gate、report path 和分段证据会在本次运行完成后刷新。');
  rdStatusEl.textContent = 'RD 运行中...';
  try {
    const job = await postJson('/api/jobs/research-run-once', rdPayload());
    activeRdJobId = job.job_id;
    rdCancel.disabled = false;
    const payload = await waitForJob(
      job.job_id,
      rdStatusEl,
      '已运行超过10秒，RD 仍在生成、评价或回测',
      jobId => activeRdJobId === jobId
    );
    renderResearch(payload);
    setStep('rd', 'done');
    setWorkbenchDot('rd', 'done');
    invalidateJobDependentPanels();
    clearGlobalError();
    rdStatusEl.innerHTML = '<span class="ok">RD 完成</span>';
  } catch (error) {
    if (error.message === '运行已中断') {
      setStep('rd', 'pending');
      setWorkbenchDot('rd', 'clear');
      resetRdResult('RD 已中断', '本次 RD 已取消，未产生新的候选结果。');
      rdStatusEl.textContent = 'RD 已中断';
    } else {
      const reason = jobFailureReason(error);
      setStep('rd', 'pending');
      setWorkbenchDot('rd', 'error');
      showJobFailureNotice('rd-result', reason);
      rdStatusEl.innerHTML = `<span class="err">${esc(reason)}</span>`;
    }
  } finally {
    activeRdJobId = null;
    rdRun.disabled = false;
    rdCancel.disabled = true;
  }
});
rdCancel.addEventListener('click', async () => {
  const jobId = activeRdJobId;
  if (!jobId) return;
  rdCancel.disabled = true;
  clearGlobalError();
  resetRdResult('RD 中断中', '已请求取消当前 RD，等待后端安全停止。');
  rdStatusEl.innerHTML = '<span class="warn">已请求中断本次RD；当前安全阶段结束后停止</span>';
  try {
    await cancelJob(jobId);
  } catch (error) {
    rdStatusEl.textContent = error.message;
    rdCancel.disabled = false;
  }
});
rdStart.addEventListener('click', async () => {
  rdStart.disabled = true;
  activateTab('lab-tab-factor');
  activateModule('lab-module-single');
  clearGlobalError();
  resetRdResult('调度启动中', '调度开启后，最近一次 RD 结果会在这里刷新。');
  rdStatusEl.textContent = '调度启动中...';
  try {
    const payload = rdPayload();
    payload.action = 'start';
    payload.interval_days = Number(document.getElementById('rd-interval').value);
    const status = await postJson('/api/research/schedule', payload);
    rdStatusEl.innerHTML = '<span class="ok">调度已开启</span>';
    if (status.last_result) {
      renderResearch(status.last_result);
      setStep('rd', 'done');
    }
  } catch (error) {
    rdStatusEl.textContent = error.message;
  } finally {
    rdStart.disabled = false;
  }
});
rdStop.addEventListener('click', async () => {
  rdStop.disabled = true;
  clearGlobalError();
  try {
    const status = await postJson('/api/research/schedule', {action: 'stop'});
    rdStatusEl.textContent = status.run_count ? `调度已停止，累计运行 ${status.run_count} 次` : '调度已停止';
  } catch (error) {
    rdStatusEl.textContent = error.message;
  } finally {
    rdStop.disabled = false;
  }
});
modeSimpleBtn.addEventListener('click', () => setMode('simple'));
modeExpertBtn.addEventListener('click', () => setMode('expert'));
document.querySelectorAll('.simple-seed-btn').forEach(seedButton => {
  seedButton.addEventListener('click', () => {
    simpleIdeaEl.value = seedButton.dataset.seedText || '';
    simpleIdeaEl.focus();
  });
});
// The simple-mode entry point delegates to the EXISTING #run handler
// (button.click()) instead of duplicating its parse/activate/error/panel
// wiring, so every pin on that handler's behavior stays intact. Switching
// to expert mode first (FE-L1: no second canvas) reveals the real
// renderers the delegated run writes into; a no-provider-ready runtime
// forces the rule parser (no-LLM-key degradation, spec §10) instead of
// attempting an LLM call the user never chose.
simpleRunButton.addEventListener('click', () => {
  const anyProviderReady = llmProviderOptions.some(option => option.runtimeReady === 'true');
  document.getElementById('parser').value = anyProviderReady ? 'llm' : 'rule';
  setMode('expert');
  button.click();
});
// Mirrors the CP10 attribute-observer pattern above (MutationObserver on
// #lab-module-panel-multi's `hidden`): the simple button tracks the
// delegated #run button's disabled state instead of duplicating its
// try/finally bookkeeping.
new MutationObserver(() => { simpleRunButton.disabled = button.disabled; })
  .observe(button, { attributes: true, attributeFilter: ['disabled'] });

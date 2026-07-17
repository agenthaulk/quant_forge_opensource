/* Entry module: control wiring for the Quant Forge single-page workbench.
 *
 * Server-rendered dynamic values (control-token flag, LLM provider options)
 * arrive through the #qf-page-config JSON block; everything executable lives
 * in these static ES modules (decision D8: no build step, no external
 * resources, no inline application script).
 */

import { esc } from './metric.js';
import {
  cancelJob,
  configureApi,
  getJob,
  getPipelineReport,
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
import { activateModule, activateTab, initLabTabs, isRecognizedExpertHash, setTabDot } from './views/lab.js';
import { initSynthesisModule, refreshSynthesisPanel } from './views/synthesis.js';
import {
  confirmCurrentPipeline,
  createEditedFormulaRun,
  createPipelineFromParseJob,
  createRdPipeline,
  currentPipelineId,
  currentPipelineParameters,
  hasActivePipeline,
  initPipelineModule,
  rejoinActivePipelines,
  resetPipelineCard
} from './views/pipeline.js';
import { attachToPipeline, initNarrationModule, refreshReadiness } from './views/narration.js';
import { initFormulaModule, openFormulaCard } from './views/formula.js';

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
const llmModelLabel = document.getElementById('llm-model-label');
const llmModelInput = document.getElementById('llm-model');
const llmBaseUrlLabel = document.getElementById('llm-base-url-label');
const llmBaseUrlInput = document.getElementById('llm-base-url');
const llmSettingsSave = document.getElementById('llm-settings-save');
let llmProviderOptions = (pageConfig.llmProviderOptions || []).map(normalizeProviderOption);
const rdRun = document.getElementById('rd-run');
// R3.1 (owner-ruled, spec §8): the rd-interval 自动周期 select and the
// 开启/停止 timer-loop controls are DELETED. RD inherits the factor evaluation
// interval/sample contract (no independent RD interval parameter); an explicit
// pipeline B (rd_optimize), started from the report follow-up #rd-entry below,
// replaces implicit timed RD. CLI `research run-once` and its scheduler stay.
const rdCancel = document.getElementById('rd-cancel');
const rdStatusEl = document.getElementById('rd-status');
// P3 (spec §2.1 / §8): the post-report follow-up bar. #report-followups is
// hidden until pipeline A produces a report; #rd-entry starts pipeline B
// seeded from THIS report's factor (the ONLY A→B path — no automatic bridge);
// #formula-edit opens the expert editable-formula card (pre-validation).
const reportFollowups = document.getElementById('report-followups');
const reportFollowupsReason = document.getElementById('report-followups-reason');
const rdEntryButton = document.getElementById('rd-entry');
const formulaEditButton = document.getElementById('formula-edit');
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
function setStaggeredEnabled(enabled) {
  staggeredButton.disabled = !enabled;
}
function currentProviderOption() {
  return llmProviderOptions.find(option => option.provider === llmProviderSelect.value) || null;
}
// Accepts both option shapes: the server-rendered page config (camelCase,
// URL reduced to a hasBaseUrl flag per the D8 no-external-reference sweep)
// and the /api/status // settings-save payloads (snake_case, full URL —
// still reduced to the flag; the frontend never needs the URL itself).
function normalizeProviderOption(option) {
  const rawBaseUrl = option['base' + '_url'] || '';
  return {
    provider: option.provider || '',
    model: option.model || '',
    hasBaseUrl: rawBaseUrl ? 'true' : (option.hasBaseUrl || 'false'),
    apiKeyEnv: option['api' + '_key_env'] || option.apiKeyEnv || '',
    runtimeReady: option['runtime' + '_ready'] || option.runtimeReady || 'false',
    configured: option.configured || 'true'
  };
}
function providerReadinessLabel(option) {
  if (option.configured === 'false') {
    return ' · 预设未启用';
  }
  if (option.runtimeReady === 'true') {
    return option.apiKeyEnv ? ` · env ${option.apiKeyEnv}` : ' · no auth';
  }
  return option.apiKeyEnv ? ` · missing env ${option.apiKeyEnv}` : ' · not ready';
}
function setRuntimeText(id, value) {
  const element = document.getElementById(id);
  if (element) element.textContent = value || '未配置';
}
function hydrateLlmRuntime(llm) {
  const llmLabel = `${llm.provider || '未配置'} / ${llm.model || '未配置'}`;
  setRuntimeText('runtime-llm', llmLabel);
  setRuntimeText('runtime-llm-sr', `LLM parser: ${llmLabel}`);
  llmProviderOptions = (llm.providers || []).map(normalizeProviderOption);
  if (llmProviderOptions.length) {
    llmProviderSelect.innerHTML = llmProviderOptions.map(option => {
      const selected = option.provider === llm.provider ? ' selected' : '';
      return `<option value="${esc(option.provider)}"${selected}>${esc(option.provider)} / ${esc(option.model)}${esc(providerReadinessLabel(option))}</option>`;
    }).join('');
  }
  const parserOption = document.querySelector('#parser option[value="llm"]');
  if (parserOption) parserOption.textContent = `LLM 语义解析: ${llm.provider || '未配置 LLM provider'}`;
  syncLlmApiKeyControls();
  return llmLabel;
}
function hydrateRdRuntime(rd) {
  const rdMode = `${rd.hypothesis_mode || 'unknown'}/${rd.review_mode || 'unknown'}`;
  const rdLabel = `${rd.research_stage || 'research'} ${rdMode} ${rd.provider || ''} ${rd.model || ''}`.trim();
  setRuntimeText('runtime-rd', rdLabel);
  setRuntimeText('runtime-rd-sr', `RD optimizer: ${rdLabel}`);
  return rdLabel;
}
function hydrateRuntimeStatus(status) {
  const paths = status.paths || {};
  const llmLabel = hydrateLlmRuntime(status.llm || {});
  const rdLabel = hydrateRdRuntime(status.rd || {});
  setRuntimeText('runtime-data-root', paths.data_root || '');
  setRuntimeText('runtime-factor-root', paths.factor_root || '');
  setRuntimeText('runtime-factor-values-root', paths.factor_values_root || '');
  setRuntimeText('runtime-factor-values-overlay-root', paths.factor_values_overlay_root || '');
  setRuntimeText('runtime-artifact-root', paths.artifact_root || '');
  setRuntimeText('simple-runtime-status', `LLM ${llmLabel} · RD ${rdLabel}`);
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
  const isPreset = Boolean(option && option.configured === 'false');
  const manual = llmApiKeyMode.value === 'manual';
  llmApiKeyInput.disabled = !manual;
  llmSettingsSave.hidden = !manual;
  // Presets without a default model (openai/glm/claude) and the custom
  // openai_compatible entry need the missing fields typed in before save.
  const needsModel = manual && Boolean(option) && !option.model;
  const needsBaseUrl = manual && Boolean(option) && option.hasBaseUrl !== 'true';
  llmModelLabel.hidden = !needsModel;
  llmModelInput.hidden = !needsModel;
  llmBaseUrlLabel.hidden = !needsBaseUrl;
  llmBaseUrlInput.hidden = !needsBaseUrl;
  if (!manual) {
    llmApiKeyInput.value = '';
    llmModelInput.value = '';
    llmBaseUrlInput.value = '';
  }
  if (manual) {
    llmApiKeyInput.placeholder = keyEnv ? `将注入 ${keyEnv}（仅本次运行内存）` : '当前 provider 未声明 API key 环境变量名';
    llmModelInput.placeholder = '模型名称（该预设无默认值，必填）';
    llmBaseUrlInput.placeholder = 'OpenAI 兼容服务地址（以 http 或 https 协议开头）';
    llmApiKeyStatus.textContent = isPreset
      ? '内置预设：输入密钥后点击「保存并启用」，注册到本次运行并即时生效'
      : '密钥仅注入本次运行内存，不写入磁盘，重启后失效；如需持久保存请写入 configs/default.local.env';
    return;
  }
  llmApiKeyInput.placeholder = configReady
    ? `已通过 ${keyEnv || 'provider config'} 加载`
    : (keyEnv ? `未检测到 ${keyEnv}` : '当前 provider 未配置 API key 环境变量名');
  if (isPreset) {
    llmApiKeyStatus.textContent = '内置预设未启用：切换到「前端输入」保存密钥即可启用';
    return;
  }
  llmApiKeyStatus.textContent = configReady
    ? 'API key 已由配置文件 / 环境变量加载，前端不展示密钥'
    : 'LLM 运行前需要在本地配置 API key 环境变量名并设置对应环境变量，或切换到「前端输入」直接保存密钥';
}
// P1 (agent_sidecar_frontend.md §2.3/§5.1, WORKORDER P1 减法): the resident
// 11-input #validation-controls grid this used to read is deleted; the
// confirm card (static/views/pipeline.js) is now the single source for
// these 11 values (its own expert-density inputs, or the server-resolved
// defaults when the user hasn't touched anything). Staggered-entry is the
// remaining caller -- it reuses whatever the pipeline card currently shows.
function validationParameters() {
  return currentPipelineParameters() || {};
}
function rdPayload() {
  return {
    seed_factor_id: document.getElementById('rd-seed').value,
    objective: document.getElementById('rd-objective').value,
    max_candidates: Number(document.getElementById('rd-max').value),
    iterations: Number(document.getElementById('rd-iterations').value)
  };
}
// WORKORDER P2 §8 deletion item — DEFERRED / ESCALATED (steward to adjudicate).
// The design calls for this parse/validate orchestration to converge into
// pipeline.js so app.js returns to "read config + assemble + route". It is NOT
// done here: the honest, safe extraction (a pipeline.js `parseIntoPipeline`
// that app.js routes to) is entangled with app.js-owned concerns this security
// commit should not destabilize -- the shared activeIdeaJobId cancel tracking,
// the two-family workbench-dot priority, the simple/expert mode handoff, the
// job-dependent panel invalidation -- and the exact parse->create fetch
// sequence is pinned by tests/test_web_pipeline_view.py's end-to-end node
// smoke. Deferring the orthogonal refactor (no P2 pin or acceptance depends on
// it) keeps the P1 green baseline intact; recommend a focused follow-up (or
// folding it into P3, where pipeline.js gains the rd_optimize kind and this
// wiring re-homes naturally). The sidecar narration wiring above is additive
// and does not deepen the coupling.
async function submitParse(parserMode) {
  if (parserMode === 'llm') {
    const option = currentProviderOption();
    if (option && option.configured === 'false') {
      throw new Error('该 Provider 是内置预设，尚未启用：请在 LLM API Key 处选择「前端输入」，保存密钥后再解析');
    }
  }
  const job = await postJson('/api/jobs/parse-idea', {
      text: document.getElementById('idea').value,
      parser_mode: parserMode,
      llm_provider: llmProviderSelect.value
  });
  activeIdeaJobId = job.job_id;
  cancelButton.disabled = false;
  const result = await waitForJob(
    job.job_id,
    statusEl,
    '已运行超过10秒，LLM 仍在解析因子',
    jobId => activeIdeaJobId === jobId
  );
  // job_id rides along so the caller can hand this SAME completed job to
  // the pipeline aggregate (createPipelineFromParseJob) without a second
  // parse -- FE-L3: the pipeline reads its parser/factor from the job
  // manager's own stored result for this id, never from this object.
  return { ...result, job_id: job.job_id };
}
// P1 (agent_sidecar_frontend.md §2.3, WORKORDER P1 pin: idempotent confirm):
// the resident #validate-run button now triggers the pipeline aggregate's
// own confirm action (see pipeline.js) instead of starting a bare compute
// job directly, so a double click / a reload-then-click carries the SAME
// server-issued nonce and resolves to the SAME run rather than starting a
// second compute. The confirm response's compute-stage child_job_id is a
// real job id, so the rest of this function (activeIdeaJobId + waitForJob)
// is unchanged.
async function submitValidation() {
  if (!parsedIdea) throw new Error('请先解析因子');
  if (!hasActivePipeline()) throw new Error('请先解析因子');
  const confirmed = await confirmCurrentPipeline();
  const computeStage = confirmed.stages.find(stage => stage.stage_id === 'compute');
  const jobId = computeStage && computeStage.child_job_id;
  if (!jobId) throw new Error('管线未能启动计算任务');
  activeIdeaJobId = jobId;
  cancelButton.disabled = false;
  return waitForJob(
    jobId,
    statusEl,
    '已运行超过10秒，系统仍在计算因子或回测',
    activeJobId => activeIdeaJobId === activeJobId
  );
}
async function submitStaggeredEntry() {
  // F1: only the PUBLISHED canonical id (validatedFactorId) is a valid seed —
  // never parsedIdea.factor.factor_id, which post-completion is the deleted
  // working (_PW…) id.
  const factorId = validatedFactorId;
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
// P1 rejoin (agent_sidecar_frontend.md §2.3): a pipeline can reach
// `completed` without this page view ever calling submitValidation() itself
// -- e.g. the compute stage finished while a REJOINED card (after a
// refresh) was still polling. The canonical report renderer (render(), the
// same one submitValidation()'s own success path uses) still owns turning
// that job's result into pixels (FE-L2); this callback only fetches the
// already-completed job and hands it over.
// P3: the report follow-up bar (pipeline B entry + expert formula edit +
// staggered check) is hidden until pipeline A yields a factor to seed from.
function revealReportFollowups(hasFactor) {
  if (!reportFollowups) return;
  reportFollowups.hidden = !hasFactor;
  if (!hasFactor && reportFollowupsReason) {
    reportFollowupsReason.textContent = '';
    reportFollowupsReason.hidden = true;
  }
}
// F1: follow-ups (RD optimize + staggered) need a REAL registered factor id —
// the pipeline record's `published_factor_id`, set ONLY when the canonical
// factor was actually published on completion (publish_state === 'published').
// The completion result's `factor.factor_id` is the pipeline WORKING id
// (`…_PW…`), which `_cleanup_working_artifacts` deletes — so it must NEVER seed
// a follow-up. When publishing did not succeed, the id-dependent follow-ups are
// refused with a visible reason and `validatedFactorId` stays null.
const PUBLISH_UNAVAILABLE_REASONS = {
  conflict: '因子未发布：规范因子在本次运行期间被并发修改（发布冲突）。RD 优化 / 稳健性回测暂不可用；可重新运行本因子。',
  declined_promoted: '因子未覆盖：同名规范因子已被人工提升（promoted）。RD 优化 / 稳健性回测请从「已注册因子」中的该因子发起。'
};
function applyPublishedFollowups(pipeline) {
  const publishedId = pipeline && pipeline.published_factor_id;
  const publishState = pipeline && pipeline.publish_state;
  if (reportFollowups) reportFollowups.hidden = false; // formula-edit stays reachable regardless
  if (publishState === 'published' && publishedId) {
    validatedFactorId = publishedId;
    if (rdEntryButton) rdEntryButton.disabled = false;
    document.getElementById('rd-seed').value = publishedId;
    setStaggeredEnabled(true);
    if (reportFollowupsReason) { reportFollowupsReason.textContent = ''; reportFollowupsReason.hidden = true; }
    return;
  }
  // Not published: refuse the id-dependent follow-ups, never fall back to the
  // deleted working id.
  validatedFactorId = null;
  setStaggeredEnabled(false);
  if (rdEntryButton) rdEntryButton.disabled = true;
  if (reportFollowupsReason) {
    reportFollowupsReason.textContent =
      PUBLISH_UNAVAILABLE_REASONS[publishState] || '因子未成功发布，RD 优化 / 稳健性回测不可用。';
    reportFollowupsReason.hidden = false;
  }
}
async function onPipelineCompleted(pipeline) {
  try {
    // Restart-proof (re-verify RV-F4): the /report endpoint serves the live
    // job result while the job manager remembers it and falls back to the
    // durable completion artifact after a restart — the old direct getJob()
    // fetch rendered nothing for a legitimately recovered pipeline.
    const report = await getPipelineReport(pipeline.pipeline_id);
    const result = report && report.result;
    if (!result) return;
    if (pipeline.kind === 'rd_optimize') {
      // Pipeline B terminal (spec §2.1/§5.4): the candidate leaderboard
      // renders through the canonical research.js renderer (FE-L2) — external
      // OOS labelled audit-only, dedup disposition executed/duplicate/skipped —
      // into the existing RD result area, never a bespoke second renderer. The
      // pipeline card itself already shows the terminal leaderboard status, so
      // this path does not also drive the workbench RD dot (pipeline B started
      // via #rd-entry never set it 'running', so a bare 'done' would be a
      // running-less writer — the legacy 运行一次 lane stays its sole owner).
      renderResearch(result);
      invalidateJobDependentPanels();
      const rdSection = document.getElementById('workbench-rd');
      if (rdSection && rdSection.scrollIntoView) rdSection.scrollIntoView({ behavior: 'smooth', block: 'start' });
      return;
    }
    render(result);
    // parsedIdea keeps the formula for the editable-formula card; its
    // factor_id is the DELETED working id post-completion, so follow-ups are
    // seeded from published_factor_id (below), never from result.factor.
    parsedIdea = { parser: result.parser, factor: result.factor, parameters: result.parameters };
    applyPublishedFollowups(pipeline);
    invalidateJobDependentPanels();
  } catch (error) {
    // Best-effort mirror of the legacy Factor Tape: the pipeline card itself
    // already shows "completed" truthfully. Log instead of vanishing so a
    // genuinely missing report is at least diagnosable.
    console.warn('pipeline report mirror failed', error);
  }
}
// P2 sidecar (agent_sidecar_frontend.md §5.5/§5.6): the narration/clarify
// drawer attaches to whatever pipeline the pipeline module owns, via its
// onPipeline hook (pipeline.js stays the single state owner, FE-L3). Readiness
// is tri-state and refreshed on load + on token arrival; the pre-fetch default
// stays `unknown` so a token-redacted boot never pre-judges (spec §5.6).
initNarrationModule();
// F2d: the editable-formula card's "run edited formula" action creates a NEW
// immutable factor_study run branched from the current pipeline (edited_by
// derived server-side). app.js owns the pipeline-card handoff + scroll; the
// formula module stays a pure editor.
initFormulaModule({
  onRunEditedFormula: async (formula, parentPipelineId) => {
    // F12: the run targets the pipeline the editor was OPENED on (captured by
    // the formula module at open time), not whatever currentPipeline is now.
    await createEditedFormulaRun(formula, { parentPipelineId });
    const mount = document.getElementById('pipeline-card-mount');
    if (mount && mount.scrollIntoView) mount.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
});
initPipelineModule({
  onCompleted: onPipelineCompleted,
  onPipeline: pipeline => { attachToPipeline(pipeline && pipeline.pipeline_id).catch(() => {}); }
});
refreshReadiness().catch(() => {});
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
// P1 rejoin (spec §2.3): mirrors refreshRuntimeStatus()'s own token guard --
// skip silently (never prompt) until a token is already stored, so a fresh
// page load with no rejoin candidate never surprises the user with an
// unrelated auth prompt; onControlTokenStored below retries once one exists.
async function maybeRejoinActivePipelines() {
  if (controlTokenRequired) {
    const token = window.sessionStorage.getItem('qf_control_token') || '';
    if (!token) return;
  }
  await rejoinActivePipelines();
}
onControlTokenStored(() => {
  refreshRuntimeStatus().catch(() => {});
  refreshReadiness().catch(() => {});
  historyPanel.refresh();
  benchPanel.refresh();
  dataPanel.refresh();
  registryPanel.refresh();
  docsPanel.refresh();
  extensionsPanel.refresh();
  maybeRejoinActivePipelines().catch(() => {});
  // CP10 stays lazy even on token arrival: only fetch when the multi module
  // panel is not hidden; otherwise the next activation retries.
  refreshSynthesisPanelIfDue();
});
llmProviderSelect.addEventListener('change', syncLlmApiKeyControls);
llmApiKeyMode.addEventListener('change', syncLlmApiKeyControls);
llmSettingsSave.addEventListener('click', async () => {
  const provider = llmProviderSelect.value;
  if (!provider) return;
  llmSettingsSave.disabled = true;
  try {
    const body = { provider };
    const credential = llmApiKeyInput.value.trim();
    if (credential) body['api' + '_key'] = credential;
    if (!llmModelInput.hidden && llmModelInput.value.trim()) body.model = llmModelInput.value.trim();
    if (!llmBaseUrlInput.hidden && llmBaseUrlInput.value.trim()) body['base' + '_url'] = llmBaseUrlInput.value.trim();
    const response = await postJson('/api/settings/llm', body);
    // Clear the secret from the DOM immediately; it lives only in the
    // server process's environment now.
    llmApiKeyInput.value = '';
    llmApiKeyMode.value = 'config';
    const llmLabel = hydrateLlmRuntime(response.llm || {});
    const rdLabel = response.rd ? hydrateRdRuntime(response.rd) : '';
    if (rdLabel) setRuntimeText('simple-runtime-status', `LLM ${llmLabel} · RD ${rdLabel}`);
    llmApiKeyStatus.textContent = response['key' + '_updated']
      ? '已保存：密钥注入本次运行进程，Provider 已启用（不落盘，重启后需重新输入）'
      : '已保存：Provider 已启用';
  } catch (error) {
    llmApiKeyStatus.textContent = `保存失败：${errorMessage(error)}`;
  } finally {
    llmSettingsSave.disabled = false;
  }
});
syncLlmApiKeyControls();
refreshRuntimeStatus().catch(() => {});
maybeRejoinActivePipelines().catch(() => {});
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
  clearGlobalError();
  resetIdeaResult('解析中', '因子解析完成后，公式和默认评测参数会在这里刷新。');
  resetPipelineCard();
  statusEl.textContent = '解析中...';
  parsedIdea = null;
  validatedFactorId = null;
  setStaggeredEnabled(false);
  revealReportFollowups(false);
  resetStaggeredResult();
  const parserMode = document.getElementById('parser').value;
  try {
    const payload = await submitParse(parserMode);
    parsedIdea = payload;
    renderParsed(payload);
    // P1: wraps the just-completed parse job into a server-owned pipeline
    // (awaiting_confirm) and renders the confirm card -- FE-L3, the
    // pipeline reads its own parser/factor from this job id, never from
    // `payload` itself (see createPipelineFromParseJob / apps/web/pipeline.py).
    await createPipelineFromParseJob(payload.job_id);
    setWorkbenchDot('idea', 'done');
    invalidateJobDependentPanels();
    statusEl.innerHTML = '<span class="ok">解析完成，等待确认参数</span>';
  } catch (error) {
    if (error.message === '运行已中断') {
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
          renderParsed(payload);
          await createPipelineFromParseJob(payload.job_id);
          setWorkbenchDot('idea', 'done');
          invalidateJobDependentPanels();
          statusEl.innerHTML = '<span class="ok">已使用本地规则解析，等待确认参数</span>';
          return;
        } catch (fallbackError) {
          const reason = jobFailureReason(fallbackError);
          setWorkbenchDot('idea', 'error');
          showJobFailureNotice('result', reason);
          errorEl.textContent = reason;
          statusEl.textContent = '运行失败';
          return;
        }
      }
    }
    const reason = jobFailureReason(error);
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
  clearGlobalError();
  resetIdeaResult('验证与评测中', '评测完成后，IC、回测收益和 artifact 路径会在这里刷新。');
  statusEl.textContent = '验证与评测中...';
  try {
    const payload = await submitValidation();
    render(payload);
    // parsedIdea keeps the formula for the editable-formula card only; its
    // factor_id here is the pipeline WORKING id (…_PW…). Follow-up seeding
    // (validatedFactorId / rd-seed / staggered / the follow-up bar) is owned
    // SOLELY by onPipelineCompleted, which reads the reconciled record's
    // published_factor_id — never this working id (F1).
    parsedIdea = {
      parser: payload.parser,
      factor: payload.factor,
      parameters: payload.parameters || validationParameters()
    };
    setWorkbenchDot('idea', 'done');
    invalidateJobDependentPanels();
    statusEl.innerHTML = '<span class="ok">验证完成</span>';
  } catch (error) {
    if (error.message === '运行已中断') {
      setWorkbenchDot('idea', 'clear');
      statusEl.innerHTML = '<span class="warn">运行已中断</span>';
      return;
    }
    const reason = jobFailureReason(error);
    setWorkbenchDot('idea', 'error');
    showJobFailureNotice('result', reason);
    errorEl.textContent = reason;
    statusEl.textContent = '验证失败';
  } finally {
    activeIdeaJobId = null;
    button.disabled = false;
    cancelButton.disabled = true;
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
    setWorkbenchDot('rd', 'done');
    invalidateJobDependentPanels();
    clearGlobalError();
    rdStatusEl.innerHTML = '<span class="ok">RD 完成</span>';
  } catch (error) {
    if (error.message === '运行已中断') {
      setWorkbenchDot('rd', 'clear');
      resetRdResult('RD 已中断', '本次 RD 已取消，未产生新的候选结果。');
      rdStatusEl.textContent = 'RD 已中断';
    } else {
      const reason = jobFailureReason(error);
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
// P3 (spec §2.1): the ONLY A→B path. Clicking #rd-entry creates pipeline B
// (rd_optimize) seeded from THIS report's factor and hands it to the pipeline
// card, which opens on the RD confirm gate. There is NO automatic bridge —
// nothing here fires unless the user clicks this report follow-up button.
if (rdEntryButton) {
  rdEntryButton.addEventListener('click', async () => {
    // F1: seed pipeline B from the PUBLISHED canonical id only — never the
    // deleted working id that result.factor would carry post-completion.
    const seed = validatedFactorId;
    if (!seed) return;
    rdEntryButton.disabled = true;
    activateTab('lab-tab-factor');
    activateModule('lab-module-single');
    clearGlobalError();
    try {
      await createRdPipeline(seed);
      const mount = document.getElementById('pipeline-card-mount');
      if (mount && mount.scrollIntoView) mount.scrollIntoView({ behavior: 'smooth', block: 'start' });
    } catch (error) {
      errorEl.textContent = jobFailureReason(error);
    } finally {
      rdEntryButton.disabled = false;
    }
  });
}
// P3 (spec §5.3): open the expert editable-formula card seeded with the
// current factor's formula. The card runs read-only pre-validation
// (/api/pipelines/pre-validate: no persist / eval / backtest).
if (formulaEditButton) {
  formulaEditButton.addEventListener('click', () => {
    const formula = (parsedIdea && parsedIdea.factor && parsedIdea.factor.formula) || '';
    // F12: capture the owning pipeline id NOW (open time) so a later run edits
    // THIS pipeline even if currentPipeline changes before the user runs it.
    openFormulaCard(formula, currentPipelineId());
    const mount = document.getElementById('formula-card-mount');
    if (mount && mount.scrollIntoView) mount.scrollIntoView({ behavior: 'smooth', block: 'start' });
  });
}
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
// FE1 fix (phase review, binding): this handoff uses applyMode, NEVER
// setMode — running the guided form once must not silently overwrite the
// saved preference. Only an explicit toggle click is a real preference
// choice (component contract 5.6); a first-time beginner who runs once
// and reloads must still land back on the simple default.
simpleRunButton.addEventListener('click', () => {
  const anyProviderReady = llmProviderOptions.some(option => option.runtimeReady === 'true');
  document.getElementById('parser').value = anyProviderReady ? 'llm' : 'rule';
  applyMode('expert');
  button.click();
});
// Mirrors the CP10 attribute-observer pattern above (MutationObserver on
// #lab-module-panel-multi's `hidden`): the simple button tracks the
// delegated #run button's disabled state instead of duplicating its
// try/finally bookkeeping.
new MutationObserver(() => { simpleRunButton.disabled = button.disabled; })
  .observe(button, { attributes: true, attributeFilter: ['disabled'] });

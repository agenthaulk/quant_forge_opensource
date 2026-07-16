/* views/formula.js — THE editable formula card (agent_sidecar_frontend.md §5.3).
 *
 * One face, one module (D12 / spec §7): the expert formula editor is a
 * distinct reused surface — a <textarea> single source of truth with an
 * aria-hidden highlight overlay driven by the canonical highlighter
 * (views/dsl.js, FE-L2: the sidecar never draws a formula itself) — so it
 * lives in its own module rather than folding into pipeline.js's state
 * machine. Its only backend touch is the read-only pre-validation endpoint
 * (/api/pipelines/pre-validate: canonicalize + ValidationGate, NO persist /
 * eval / backtest); it holds no pipeline-lifecycle state, so there is no
 * hidden coupling back into pipeline.js.
 *
 * contenteditable is deliberately rejected (spec §5.3): a <textarea> keeps
 * CN IME composition, undo, cursor, and paste behaviour correct, and the
 * overlay is repainted ONLY between compositions — never mid-composition,
 * where a sibling innerHTML write could disturb the IME candidate window.
 */

import { esc } from '../metric.js';
import { formulaHtml } from './dsl.js';

// Pure render functions first; the [controller] section (fetch / DOM / events)
// is last, so a design lane can drive these with fixtures alone.

/* The aria-hidden highlight overlay content. dsl.js is the ONLY formula
 * highlighter (FE-L2 / single-renderer discipline): this module never
 * tokenizes or colours a formula itself, it defers to formulaHtml. A trailing
 * newline keeps the overlay's last line height in sync with the textarea. */
export function renderFormulaOverlay(formula) {
  return formulaHtml(formula) + '\n';
}

/* The pre-validation verdict (spec §5.3). Every branch states, in the UI,
 * that pre-validation neither ran nor persisted the formula. An unknown
 * operator is rendered as a review-packet disclosure (operator_drafts), never
 * as an executable result. */
export function renderPreValidationResult(result) {
  if (!result) return '';
  const status = result.status || 'unknown';
  const statusMeta = {
    ready: { pill: 'status-pill--ok', text: '可用' },
    review_required: { pill: 'status-pill--neutral', text: '需算子审阅' },
    blocked: { pill: 'status-pill--fail', text: '未通过' }
  }[status] || { pill: 'status-pill--neutral', text: status };
  const lines = [];
  lines.push(
    `<div class="formula-prevalidate-status">`
    + `<span class="status-pill ${statusMeta.pill}">${esc(statusMeta.text)}</span>`
    + `<span class="meta">预验证：不执行、不落盘（executed=${result.executed === true ? 'true' : 'false'} · `
    + `persisted=${result.persisted === true ? 'true' : 'false'}）</span>`
    + `</div>`
  );
  if (result.fingerprint) {
    lines.push(`<p class="meta">canonical fingerprint: <code>${esc(result.fingerprint)}</code></p>`);
  }
  if (status === 'ready') {
    // F2d (compare loop, spec §5.3/§5.4): only a runnable ("ready") edit may
    // create a run. Running it branches a NEW immutable factor_study run from
    // the current pipeline; edited_by=human is derived SERVER-side by
    // fingerprint comparison (never asserted here).
    lines.push(
      `<div class="formula-run-edited">`
      + `<button type="button" data-formula-action="run-edited">运行编辑后的公式（新建对比运行）</button>`
      + `<span class="meta">新建一次不可变运行；edited_by 由服务端按指纹比对判定（human），非客户端声明。</span>`
      + `</div>`
    );
  }
  const blocking = result.blocking_reasons || [];
  if (blocking.length) {
    lines.push(`<p class="meta">阻塞原因：${esc(blocking.join('；'))}</p>`);
  }
  const unresolvedFields = result.unresolved_fields || [];
  if (unresolvedFields.length) {
    lines.push(`<p class="meta">未知字段：${esc(unresolvedFields.join('、'))}</p>`);
  }
  const warnings = result.warnings || [];
  if (warnings.length) {
    lines.push(`<p class="meta">warning：${esc(warnings.join('；'))}</p>`);
  }
  const packet = result.review_packet;
  if (packet) {
    // Unknown operator: an operator_drafts review-packet disclosure. The
    // words hot_executed=false are shown verbatim so the surface itself
    // states the operator was never executed (WORKORDER pin / X3).
    lines.push(
      `<div class="formula-review-packet">`
      + `<span class="status-pill status-pill--neutral">审阅包 · ${esc(packet.channel || 'operator_drafts')}</span>`
      + `<p class="meta">未知算子：${esc((packet.unresolved_operators || []).join('、'))}</p>`
      + `<p class="meta">未知算子只生成算子审阅包（Codex/开发者审计），绝不热执行，`
      + `不落盘（hot_executed=${packet.hot_executed === true ? 'true' : 'false'}）。</p>`
      + `</div>`
    );
  } else {
    const unresolvedOps = result.unresolved_operators || [];
    if (unresolvedOps.length) {
      lines.push(`<p class="meta">未知算子：${esc(unresolvedOps.join('、'))}</p>`);
    }
  }
  return `<div class="formula-prevalidate-body">${lines.join('')}</div>`;
}

/* The static shell of the card: a header, the honest scope disclosure, the
 * editor (overlay + textarea, stacked), the pre-validate action, and a live
 * result slot. Rendered ONCE into the mount by the controller; edits only ever
 * mutate the overlay / result slots, never re-render the textarea (which would
 * lose IME state and cursor). */
export function renderFormulaCard(options) {
  const formula = (options && options.formula) || '';
  return `
    <div class="panel formula-card" id="formula-card">
      <div class="formula-card-header">
        <h3 class="formula-card-title" tabindex="-1">编辑并预验证公式</h3>
        <button type="button" class="secondary" data-formula-action="close">收起</button>
      </div>
      <p class="meta">预验证只做规范化与算子/字段校验，不落盘、不评测、不回测；未知算子只生成算子审阅包（operator_drafts），绝不执行。</p>
      <div class="formula-editor">
        <pre class="formula-overlay" id="formula-overlay" aria-hidden="true">${renderFormulaOverlay(formula)}</pre>
        <textarea id="formula-input" class="formula-input" spellcheck="false" autocomplete="off"
          autocapitalize="off" autocorrect="off" aria-label="因子公式">${esc(formula)}</textarea>
      </div>
      <div class="formula-actions">
        <button type="button" id="formula-prevalidate-btn" data-formula-action="pre-validate">预验证公式</button>
      </div>
      <div id="formula-prevalidate-result" class="formula-prevalidate-result" aria-live="polite"></div>
    </div>`;
}

// -----------------------------------------------------------------------
// [controller] Everything below touches fetch / DOM / events; everything
// above is a pure payload -> HTML function a design lane can drive with
// fixtures alone.
// -----------------------------------------------------------------------

import { postJson } from '../api.js';

const mount = document.getElementById('formula-card-mount');

let composing = false;
let inputEl = null;
let overlayEl = null;
let resultEl = null;
// F2d: app.js wires this to pipeline.js::createEditedFormulaRun so the module
// stays a pure formula editor with no direct pipeline-lifecycle coupling.
let onRunEditedFormula = null;
// F12 (formula editor parent binding): the pipeline this editor was OPENED on,
// captured at open time and forwarded verbatim when the user runs the edit --
// so a run always targets the pipeline the expert was editing, never whatever
// `currentPipeline` (which this module deliberately cannot see) has since
// become. An opaque token to this module; app.js supplies and consumes it.
let capturedParentPipelineId = null;
// F14 (stale pre-validation verdict): a monotonic generation counter. Every
// pre-validation captures the current generation; any later edit (or a fresh
// open) bumps it, so an out-of-generation response is dropped instead of
// overwriting a fresher verdict, and the displayed verdict is cleared the
// moment the formula changes -- the run button is thereby gated on a FRESH
// validation, never a stale "ready" from a formula the user has since edited.
let validationGeneration = 0;

/* Repaint the aria-hidden highlight overlay from the textarea's CURRENT value.
 * The IME guard (below) is the ONLY thing that gates this: it is a no-op while
 * a composition is in flight. */
function syncOverlay() {
  if (!inputEl || !overlayEl) return;
  overlayEl.innerHTML = renderFormulaOverlay(inputEl.value);
}

/* F15 (IME preedit visibility): while a CJK composition is in flight the raw
 * textarea text is what the user is actively editing, but `.formula-input` is
 * `color: transparent` (the aria-hidden overlay draws the highlighted copy),
 * so the preedit / candidate text would be INVISIBLE. Toggling `is-composing`
 * restores the textarea's own text colour AND hides the overlay for the
 * duration of the composition; compositionend/blur restore overlay mode and
 * resync. Both target elements carry an id, so the toggle is observable. */
function setComposingVisible(on) {
  if (inputEl && inputEl.classList) inputEl.classList.toggle('is-composing', on);
  if (overlayEl && overlayEl.classList) overlayEl.classList.toggle('is-composing', on);
}

/* F14: an edit after a verdict makes that verdict (and its run-edited button,
 * if any) stale. Bump the generation so any in-flight validation response is
 * dropped, and clear the displayed verdict so the run action is gated on a
 * fresh pre-validation. */
function invalidatePreValidation() {
  validationGeneration += 1;
  if (resultEl && resultEl.innerHTML !== '') resultEl.innerHTML = '';
}

/* Render the shell once and wire the persistent child elements. Idempotent:
 * a second call after the first is a no-op, so listeners are never doubled. */
function ensureRendered() {
  if (!mount || inputEl) return;
  mount.innerHTML = renderFormulaCard({ formula: '' });
  inputEl = document.getElementById('formula-input');
  overlayEl = document.getElementById('formula-overlay');
  resultEl = document.getElementById('formula-prevalidate-result');
  if (!inputEl) return;
  // IME composition guard (spec §5.3, WORKORDER pin "no repaint
  // mid-composition"): CJK IME fires `input` events for every intermediate
  // candidate DURING a composition; repainting the sibling overlay then can
  // disturb the candidate window / cursor. So compositionstart latches a flag
  // that makes `input` a no-op, and the overlay is repainted exactly ONCE on
  // compositionend (plus on ordinary, non-composed input).
  inputEl.addEventListener('compositionstart', () => { composing = true; setComposingVisible(true); });
  inputEl.addEventListener('compositionend', () => { composing = false; setComposingVisible(false); syncOverlay(); });
  // F14: any edit clears a now-stale verdict and drops an in-flight validation.
  inputEl.addEventListener('input', () => { if (!composing) syncOverlay(); invalidatePreValidation(); });
  // F15: some IMEs end a composition with a blur (tab/click away) rather than a
  // compositionend; reset the composing state so the overlay is never left
  // hidden with the textarea stuck in preedit-visible mode.
  inputEl.addEventListener('blur', () => {
    if (!composing) return;
    composing = false;
    setComposingVisible(false);
    syncOverlay();
  });
  // Keep the overlay scroll-aligned with the textarea so the highlight tracks
  // the visible text on long formulas.
  inputEl.addEventListener('scroll', () => {
    if (!overlayEl) return;
    overlayEl.scrollTop = inputEl.scrollTop;
    overlayEl.scrollLeft = inputEl.scrollLeft;
  });
  mount.addEventListener('click', event => {
    const btn = event.target.closest && event.target.closest('[data-formula-action]');
    if (!btn) return;
    const action = btn.dataset.formulaAction;
    if (action === 'close') { closeFormulaCard(); return; }
    if (action === 'pre-validate') { runPreValidation(btn); return; }
    if (action === 'run-edited') { runEditedFormula(btn); return; }
  });
}

/* F2d: hand the validated edit to app.js's wired runner (pipeline.js). This
 * module never touches the pipeline lifecycle directly; it only forwards the
 * current textarea formula once the user asks to run it. */
async function runEditedFormula(btn) {
  if (!inputEl || typeof onRunEditedFormula !== 'function') return;
  const formula = inputEl.value;
  if (!formula.trim()) return;
  if (btn) btn.disabled = true;
  try {
    // F12: run against the pipeline this editor was OPENED on (captured at
    // open time), never whatever the pipeline module's current pipeline has
    // since become.
    await onRunEditedFormula(formula, capturedParentPipelineId);
    closeFormulaCard();
  } catch (error) {
    if (resultEl) {
      resultEl.innerHTML = renderPreValidationResult({
        status: 'blocked', executed: false, persisted: false,
        blocking_reasons: [(error && error.message) || '运行编辑后的公式失败']
      });
    }
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function runPreValidation(btn) {
  if (!inputEl || !resultEl) return;
  const formula = inputEl.value;
  if (!formula.trim()) {
    resultEl.innerHTML = renderPreValidationResult({
      status: 'blocked', executed: false, persisted: false, blocking_reasons: ['公式不能为空']
    });
    return;
  }
  // F14: capture this request's generation; a later edit (or another
  // pre-validate) bumps the counter, so an out-of-generation response below is
  // dropped rather than overwriting a fresher verdict.
  const generation = ++validationGeneration;
  if (btn) btn.disabled = true;
  resultEl.innerHTML = '<p class="meta">预验证中...</p>';
  try {
    // Read-only: /api/pipelines/pre-validate canonicalizes + gates the formula
    // and NEVER persists / evaluates / backtests (apps/web/pipeline.py::
    // pre_validate_formula). An unknown operator comes back as a review packet.
    const result = await postJson('/api/pipelines/pre-validate', { formula });
    if (generation !== validationGeneration) return;  // F14: superseded -> drop
    resultEl.innerHTML = renderPreValidationResult(result);
  } catch (error) {
    if (generation !== validationGeneration) return;  // F14: drop a stale error too
    resultEl.innerHTML = renderPreValidationResult({
      status: 'blocked', executed: false, persisted: false,
      blocking_reasons: [(error && error.message) || '预验证失败']
    });
  } finally {
    if (btn) btn.disabled = false;
  }
}

/* Reveal the card seeded with `formula`, binding it to `parentPipelineId` (the
 * pipeline the expert is editing FROM, captured now so a later run targets it
 * regardless of what the pipeline module's current pipeline becomes -- F12).
 * Safe to call repeatedly; the shell is rendered lazily on first open. */
export function openFormulaCard(formula, parentPipelineId) {
  if (!mount) return;
  ensureRendered();
  capturedParentPipelineId = parentPipelineId || null;  // F12: bind to the owning pipeline NOW
  if (inputEl) inputEl.value = formula || '';
  syncOverlay();
  if (resultEl) resultEl.innerHTML = '';
  validationGeneration += 1;  // F14: a fresh open starts a new verdict epoch
  mount.hidden = false;
  const heading = mount.querySelector && mount.querySelector('.formula-card-title');
  if (heading && typeof heading.focus === 'function') heading.focus();
}

export function closeFormulaCard() {
  if (!mount) return;
  mount.hidden = true;
}

export function currentFormula() {
  return inputEl ? inputEl.value : '';
}

export function initFormulaModule(options) {
  onRunEditedFormula = (options && options.onRunEditedFormula) || null;
  if (!mount) return;
  // "[hidden] until the expert opens it" (html.py mount comment): hidden from
  // the first paint, rendered lazily on the first openFormulaCard().
  mount.hidden = true;
}

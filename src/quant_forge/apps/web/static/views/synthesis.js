/* views/synthesis.js — CP10 multi-factor synthesis + backtest module.
 *
 * Layout discipline (pinned by tests/test_web_synthesis_view.py): PURE
 * RENDERERS AND BUILDERS FIRST — every render* function is payload -> html
 * string with no DOM access, no fetch, no module state, so tests and the
 * design lane drive them with fixtures — then the [controller] section,
 * which is the only part that touches fetch / DOM / events.
 *
 * Contract surfaces:
 *   - GET  /api/registry/factors            factor picker catalog
 *   - GET  /api/synthesis/methods           method + standardization catalog
 *                                           (CP10 spec §7 ParamSpec JSON)
 *   - POST /api/jobs/multi-factor-backtest  run (spec §8.2 request/response)
 *
 * The dynamic params form renders purely from the chosen method's params[]
 * ParamSpec list — ZERO per-method hardcoding: no method or standardizer
 * name appears in this module, so a new method arriving in the payload
 * renders a working form with no frontend change.
 *
 * Honest degraded state: while GET /api/synthesis/methods is absent or
 * failing on this deployment, the module states 方法目录不可用 explicitly
 * and keeps the run disabled — it never crashes and never presents the
 * missing catalog as an empty one (FP-4 discipline applied to capability
 * surfaces, not just metrics).
 *
 * FP-4 rendering: metric cells render only through metric.js (via the
 * reused factor.js section renderers); coverage ratios null -> 'n/a', never
 * 0; a-priori weights echo RAW and unnormalized; is_fitted=false surfaces
 * only as the 先验声明 label — nothing here fabricates a fitted claim or a
 * zero.
 *
 * Disabled-state honesty: the single primary action (#synth-run) never sits
 * disabled without a stated reason — runReadinessHintText names the missing
 * prerequisite (factor count / catalog availability / job in flight) in the
 * #synth-run-hint live region; weights inputs are labeled by factor NAME
 * with the raw factor_id kept visible when it differs.
 */

import { esc, pct, valueOr } from '../metric.js';
import { cancelJob, fetchPanelJson, postJson, waitForJob } from '../api.js';
import {
  renderArtifactsSection,
  renderDiagnosticsSection,
  renderEvaluationSection,
  renderEvidenceSection,
  renderInSampleSection,
  renderOosSection
} from './factor.js';
import { formulaHtml } from './dsl.js';

const PRECOMPUTED_PREFIX = 'precomputed:';

/* A precomputed key is not an expression: pill + escaped key, never
 * syntax-highlighted (registry.js discipline). */
function pickerFormulaHtml(formula) {
  const text = formula ? String(formula) : '';
  if (text.startsWith(PRECOMPUTED_PREFIX)) {
    return `<span class="pill">precomputed</span> ${esc(text.slice(PRECOMPUTED_PREFIX.length))}`;
  }
  return formulaHtml(text);
}

/* Lifecycle tone, not health (mirrors the registry view): active is the only
 * "live" state, candidate is in-flight; every other label stays neutral. */
function factorStatusPillHtml(status) {
  const label = status ? String(status) : 'n/a';
  const tone = label === 'active'
    ? 'status-pill--ok'
    : label === 'candidate' ? 'status-pill--running' : 'status-pill--neutral';
  return `<span class="status-pill ${tone}">${esc(label)}</span>`;
}

function directionText(direction) {
  if (direction === undefined || direction === null) return 'n/a';
  const value = Number(direction);
  return value > 0 ? `+${value}` : String(value);
}

/* Factor picker: checkbox list over the registry catalog with an EXPLICIT
 * per-factor direction control (+1 default = "use as defined"; -1 = declared
 * inversion). No silent sign flips anywhere. */
export function renderFactorPickerHtml(factors, preserved) {
  const rows = factors || [];
  if (!rows.length) {
    return `
      <div class="panel empty-state">
        <h3>暂无注册因子</h3>
        <p class="meta">先在单因子模块解析并验证因子，注册后即可参与合成。</p>
      </div>`;
  }
  // Restore prior per-factor state (B-MAJOR-1): a background job completion
  // refreshes this list so newly-created factors appear, but it must NOT
  // reset an in-progress config. `preserved` maps factor_id -> {checked,
  // direction}; a factor_id no longer in the list is silently dropped, and a
  // factor with no prior entry keeps the defaults (unchecked, +1).
  const restore = preserved || {};
  const items = rows.map(factor => {
    const factorId = factor.factor_id ? String(factor.factor_id) : '';
    const factorName = factor.name ? String(factor.name) : factorId;
    const prior = restore[factorId] || null;
    // Strictly === false: a dangling composite's DEFINITION is registered
    // but its VALUES were only ever written to a past run's overlay, so
    // picking it here would fail deep in a later run. null/undefined/true
    // (not precomputed, or values confirmed present) render exactly as
    // before — this is additive, never a guess (FP-4).
    const valuesUnavailable = factor.precomputed_values_present === false;
    const checkedAttr = !valuesUnavailable && prior && prior.checked ? ' checked' : '';
    const negative = Boolean(prior) && Number(prior.direction) === -1;
    const posSelected = negative ? '' : ' selected';
    const negSelected = negative ? ' selected' : '';
    const disabledAttr = valuesUnavailable ? ' disabled' : '';
    const unavailablePill = valuesUnavailable
      ? ' <span class="status-pill status-pill--running">值不可用</span>'
      : '';
    const titleAttr = valuesUnavailable
      ? ' title="该合成因子的数值产物属于历史运行，当前部署未持有；需重新运行合成后方可复用。"'
      : '';
    return `
      <div class="synth-factor-row" data-factor-id="${esc(factorId)}"${titleAttr}>
        <label class="synth-factor-name">
          <input type="checkbox" class="synth-factor-check" data-factor-id="${esc(factorId)}" data-factor-name="${esc(factorName)}"${checkedAttr}${disabledAttr}>
          <span>${esc(factorName)}</span>
          ${factorStatusPillHtml(factor.status)}${unavailablePill}
        </label>
        <label class="synth-direction-label">
          <span>方向</span>
          <select class="synth-direction" data-factor-id="${esc(factorId)}" aria-label="方向 ${esc(factorName)}">
            <option value="1"${posSelected}>+1 按定义使用</option>
            <option value="-1"${negSelected}>-1 反向使用</option>
          </select>
        </label>
        <span class="synth-factor-formula">${pickerFormulaHtml(factor.formula)}</span>
      </div>`;
  }).join('');
  return `<div class="synth-factor-list">${items}</div>
    <p class="meta">已选因子数需 ≥ 2 才能运行；方向 +1 表示按公式定义使用，-1 表示显式反向。</p>`;
}

/* Honest degraded state for the methods catalog: absent is stated, never
 * treated as empty, never a crash. */
export function renderMethodsUnavailableHtml(reason) {
  return `
    <div class="notice warn" id="synth-methods-unavailable">
      <span class="status-pill status-pill--running">方法目录不可用</span> ${esc(reason || 'request failed')}
      <br><span class="meta">GET /api/synthesis/methods 未返回方法目录；目录可用前无法运行合成回测。目录不可用是明确状态，不会被当作空目录处理。</span>
    </div>`;
}

/* Method select rendered purely from methods[]: available:true entries are
 * selectable; available:false entries render as disabled 预留 options (the
 * reserved-method rule is generic — no method name is special-cased). */
export function renderMethodSelectHtml(catalog, selectedName) {
  const methods = (catalog && catalog.methods) || [];
  if (!methods.length) return renderMethodsUnavailableHtml('方法目录为空');
  const availableNames = methods.filter(method => method.available === true).map(method => method.name);
  if (!availableNames.length) {
    return renderMethodsUnavailableHtml('方法目录中没有可用方法');
  }
  const effective = availableNames.indexOf(selectedName) === -1 ? availableNames[0] : selectedName;
  const options = methods.map(method => {
    const available = method.available === true;
    const selected = available && method.name === effective ? ' selected' : '';
    const disabled = available ? '' : ' disabled';
    const suffix = available ? '' : '（预留）';
    return `<option value="${esc(method.name)}"${selected}${disabled}>${esc(method.name)} · ${esc(method.label || method.name)}${suffix}</option>`;
  }).join('');
  return `
    <label for="synth-method">合成方法</label>
    <select id="synth-method">${options}</select>`;
}

/* Standardization select. When the chosen method declares
 * required_standardization the control renders disabled + pinned with an
 * explicit note; the run request then OMITS the standardization block (B3
 * deviation #6) instead of sending a conflicting choice. */
export function renderStandardizationHtml(catalog, method, selectedName) {
  const standardizations = (catalog && catalog.standardizations) || [];
  const pinned = method && method.required_standardization ? String(method.required_standardization) : '';
  if (pinned) {
    const entry = standardizations.find(std => std.name === pinned) || null;
    const labelText = entry ? pinned + ' · ' + (entry.label || pinned) : pinned;
    return `
    <label for="synth-standardization">标准化</label>
    <select id="synth-standardization" disabled data-pinned="${esc(pinned)}">
      <option value="${esc(pinned)}" selected>${esc(labelText)}（方法固定）</option>
    </select>
    <p class="meta">该方法固定使用 ${esc(pinned)}；请求中省略 standardization 字段，由后端按方法钉住。</p>`;
  }
  if (!standardizations.length) {
    return '<div class="notice warn">标准化目录为空，无法运行合成回测。</div>';
  }
  const effective = standardizations.some(std => std.name === selectedName)
    ? selectedName
    : standardizations[0].name;
  const options = standardizations.map(std => {
    const selected = std.name === effective ? ' selected' : '';
    return `<option value="${esc(std.name)}"${selected}>${esc(std.name)} · ${esc(std.label || std.name)}</option>`;
  }).join('');
  return `
    <label for="synth-standardization">标准化</label>
    <select id="synth-standardization">${options}</select>`;
}

/* A checked-factor entry is either a bare factor_id string or a
 * {factor_id, name} ref from the picker; weights inputs label by the
 * human factor name (id shown alongside only when it differs). */
function checkedFactorEntry(item) {
  if (typeof item === 'string') return { factorId: item, name: item };
  const factorId = item && item.factor_id ? String(item.factor_id) : '';
  const name = item && item.name ? String(item.name) : factorId;
  return { factorId, name };
}

/* One ParamSpec -> one form control. float/int -> number input honoring
 * min/max/default, bool -> checkbox, enum -> select over choices, weights ->
 * one number input per currently-checked factor keyed by factor_id and
 * labeled by the factor's name. */
function renderParamSpecInputHtml(spec, checkedFactors, preservedValue) {
  const name = spec.name ? String(spec.name) : '';
  const requiredMark = spec.required ? '（必填）' : '';
  const labelText = (spec.label || name) + requiredMark;
  const help = spec.help ? `<p class="meta">${esc(spec.help)}</p>` : '';
  if (spec.type === 'weights') {
    const entries = (checkedFactors || []).map(checkedFactorEntry);
    const inputs = entries.map(entry => {
      const factorId = entry.factorId;
      const preserved = preservedValue && typeof preservedValue === 'object' ? preservedValue[factorId] : undefined;
      const value = preserved === undefined || preserved === null ? '' : String(preserved);
      const idNote = entry.name === factorId ? '' : ` <span class="synth-weight-id">${esc(factorId)}</span>`;
      return `
        <label><span>${esc(labelText)} · ${esc(entry.name)}${idNote}</span>
          <input type="number" step="any" data-param-name="${esc(name)}" data-param-type="weights" data-weight-factor="${esc(factorId)}" value="${esc(value)}"></label>`;
    }).join('');
    const emptyNote = entries.length
      ? ''
      : `<p class="meta">先勾选参与合成的因子，再为每个因子填写 ${esc(labelText)}。</p>`;
    return `<div class="synth-param synth-param-weights" data-param-name="${esc(name)}">${emptyNote}<div class="param-grid">${inputs}</div>${help}</div>`;
  }
  if (spec.type === 'bool') {
    const current = preservedValue === undefined ? spec.default === true : preservedValue === true || preservedValue === 'true';
    const checked = current ? ' checked' : '';
    return `<div class="synth-param"><label class="synth-check-label"><input type="checkbox" data-param-name="${esc(name)}" data-param-type="bool"${checked}> <span>${esc(labelText)}</span></label>${help}</div>`;
  }
  if (spec.type === 'enum') {
    const fallback = spec.default === undefined || spec.default === null ? '' : String(spec.default);
    const current = preservedValue === undefined || preservedValue === null ? fallback : String(preservedValue);
    const blank = current === '' ? '<option value="" selected>请选择</option>' : '';
    const options = (spec.choices || []).map(choice => {
      const selected = String(choice) === current ? ' selected' : '';
      return `<option value="${esc(choice)}"${selected}>${esc(choice)}</option>`;
    }).join('');
    return `<div class="synth-param"><label><span>${esc(labelText)}</span><select data-param-name="${esc(name)}" data-param-type="enum">${blank}${options}</select></label>${help}</div>`;
  }
  if (spec.type === 'float' || spec.type === 'int') {
    // float / int -> number input honoring the declared bounds and default.
    const step = spec.type === 'int' ? '1' : 'any';
    const min = spec.minimum === undefined || spec.minimum === null ? '' : ` min="${esc(String(spec.minimum))}"`;
    const max = spec.maximum === undefined || spec.maximum === null ? '' : ` max="${esc(String(spec.maximum))}"`;
    const fallback = spec.default === undefined || spec.default === null ? '' : String(spec.default);
    const current = preservedValue === undefined || preservedValue === null ? fallback : String(preservedValue);
    return `<div class="synth-param"><label><span>${esc(labelText)}</span><input type="number" step="${step}" data-param-name="${esc(name)}" data-param-type="${esc(spec.type || 'float')}"${min}${max} value="${esc(current)}"></label>${help}</div>`;
  }
  // Unrecognized ParamSpec.type (B-MINOR-1): honest-degraded. Never ship a
  // fabricated number field for a type the form can't render — name the
  // unsupported type (escaped) so the gap is explicit, and mark the wrapper
  // so collectMethodParams omits it (no invented value is sent downstream).
  const unsupportedType = spec.type === undefined || spec.type === null ? 'n/a' : String(spec.type);
  return `<div class="synth-param synth-param-unsupported" data-param-name="${esc(name)}" data-param-type="unsupported"><label><span>${esc(labelText)}</span></label><div class="notice warn"><span class="status-pill status-pill--running">不支持的参数类型</span> ${esc(unsupportedType)}</div>${help}</div>`;
}

/* Dynamic params form: rendered PURELY from the chosen method's params[]
 * ParamSpec JSON. `checkedFactors` entries are factor_id strings or
 * {factor_id, name} picker refs; `preserved` carries current user entries so
 * regenerating the form (e.g. when the checked-factor set changes) keeps
 * them. */
export function renderParamsFormHtml(method, checkedFactors, preserved) {
  if (!method) return '';
  const specs = method.params || [];
  if (!specs.length) return '<p class="meta">该方法无额外参数（以方法目录声明为准）。</p>';
  const values = preserved || {};
  const blocks = specs
    .map(spec => renderParamSpecInputHtml(spec, checkedFactors || [], values[spec.name]))
    .join('');
  return `<div class="synth-params-list">${blocks}</div>`;
}

/* Why-disabled honesty for the single primary action: the run button never
 * sits disabled without a stated reason. Returns '' when the run is ready
 * (the hint then stays silent) and a plain-text reason list otherwise —
 * static wording plus a count, so no esc() is needed and the controller can
 * assign via textContent. */
export function runReadinessHintText(state) {
  const s = state || {};
  if (s.jobRunning === true) return '合成回测运行中；完成或中断本次运行后可再次发起。';
  const reasons = [];
  const rawCount = Number(s.checkedCount);
  const count = Number.isFinite(rawCount) ? rawCount : 0;
  if (count < 2) reasons.push(`已选 ${count} 个因子，运行需至少勾选 2 个`);
  if (s.hasCatalog !== true) reasons.push('方法目录尚未加载或不可用');
  else if (s.methodReady !== true) reasons.push('方法目录中没有可用的合成方法');
  else if (s.standardizationReady === false) reasons.push('所选方法需要标准化，但标准化目录为空或不可用');
  if (!reasons.length) return '';
  return reasons.join('；') + '。';
}

/* Validity banner: the a-priori discipline statement plus server caveats
 * (coverage drops, pinned-standardization override notes). */
export function renderValidityBannerHtml(validity, isFitted) {
  if (!validity) return '';
  const caveats = (validity.caveats || []).map(item =>
    `<div class="notice warn"><span class="status-pill status-pill--running">注意</span> ${esc(item)}</div>`
  ).join('');
  /* The pill states the weight regime truthfully: a fitted run (is_fitted
   * true in provenance) must not wear the a-priori badge. Absent provenance
   * keeps neutral research-basis wording rather than guessing. */
  const pill = isFitted === true ? '拟合权重（时变）' : (isFitted === false ? '先验声明' : '研究口径');
  return `
    <div class="notice" id="synth-validity">
      <span class="status-pill status-pill--neutral">${esc(pill)}</span> ${esc(validity.message || '')}
      <br><span class="meta">basis: ${esc(validity.basis || 'n/a')}</span>
    </div>` + caveats;
}

function paramPillsHtml(params) {
  const entries = Object.entries(params || {});
  if (!entries.length) return '<span class="pill">无参数</span>';
  return entries.map(([key, value]) =>
    `<span class="pill">${esc(key)} ${esc(JSON.stringify(value))}</span>`
  ).join(' ');
}

/* Sample-role display labels mirror the reused report sections' wording
 * exactly, with the raw server key kept visible beside them; an unknown
 * role key renders as-is (never guessed). */
const SAMPLE_ROLE_LABELS = {
  research_evaluation: '样本内研究评价',
  in_sample_backtest: '样本内组合回测',
  external_oos_backtest: '外部样本外组合评测'
};

function roleDisplayText(role) {
  const key = String(role);
  const label = SAMPLE_ROLE_LABELS[key];
  return label ? `${label}（${key}）` : key;
}

/* Per-role coverage tables (B3: coverage_by_role keys research_evaluation /
 * external_oos_backtest). Ratios render via pct() — null stays 'n/a', never
 * 0; counts fall back to 'n/a' via valueOr. A missing coverage_by_role falls
 * back to the spec §8.2 flat coverage list under one 合成总体 table. */
export function renderCoverageByRoleHtml(coverageByRole, fallbackCoverage) {
  const roles = coverageByRole && typeof coverageByRole === 'object'
    ? Object.entries(coverageByRole)
    : (fallbackCoverage ? [['合成总体', fallbackCoverage]] : []);
  if (!roles.length) return '<p class="meta">覆盖明细不可观测（n/a）。</p>';
  return roles.map(([role, entry]) => {
    const roleRows = Array.isArray(entry) ? entry : ((entry && entry.coverage) || []);
    const roleMeta = !Array.isArray(entry) && entry ? entry : null;
    const body = roleRows.map(row => `
          <tr>
            <td>${esc(row.factor_id || '')}</td>
            <td>${esc(directionText(row.direction))}</td>
            <td>${esc(row.source || 'n/a')}</td>
            <td>${esc(valueOr(row.rows_scored, 'n/a'))}</td>
            <td>${esc(valueOr(row.rows_in_composite, 'n/a'))}</td>
            <td>${pct(row.coverage_ratio)}</td>
          </tr>`).join('') || '<tr><td colspan="6">暂无覆盖记录</td></tr>';
    const metaLine = roleMeta
      ? `<p class="meta">rows_required ${esc(valueOr(roleMeta.rows_required, 'n/a'))} · rows_full_coverage ${esc(valueOr(roleMeta.rows_full_coverage, 'n/a'))}</p>`
      : '';
    return `
      <h3>覆盖 · ${esc(roleDisplayText(role))}</h3>
      ${metaLine}
      <div class="table-scroll">
        <table class="comparison-table">
          <thead><tr><th>因子</th><th>方向</th><th>来源</th><th>rows_scored</th><th>rows_in_composite</th><th>coverage_ratio</th></tr></thead>
          <tbody>${body}</tbody>
        </table>
      </div>`;
  }).join('');
}

/* Synthesis-provenance card: factors + explicit directions + method +
 * params + standardization (+ pinned note) + RAW a-priori weights (echoed
 * unnormalized) + coverage rule/counts + per-role coverage tables.
 * is_fitted=false renders ONLY as the 先验声明 label — no other rendering
 * of that flag exists, and a fitted claim is never fabricated. */
export function renderProvenanceCardHtml(provenance) {
  if (!provenance) return '';
  const factorPills = (provenance.factors || []).map(ref => {
    const declared = (provenance.directions || {})[ref.factor_id];
    return `<span class="pill">${esc(ref.factor_id)} · 方向 ${esc(directionText(valueOr(declared, ref.direction)))} · ${esc(ref.source || 'n/a')}</span>`;
  }).join(' ') || '<span class="pill">暂无因子记录</span>';
  const pinnedNote = provenance.standardization_pinned_by_method === true
    ? ' <span class="pill">方法固定标准化</span>'
    : '';
  const weights = provenance.weights_effective;
  const weightPills = weights
    ? Object.entries(weights).map(([factorId, value]) =>
        `<span class="pill">${esc(factorId)} 权重 ${esc(valueOr(value, 'n/a'))}</span>`
      ).join(' ')
    : '';
  const weightsLine = weights
    ? `<p>${weightPills}</p><p class="meta">权重为先验原始声明值，未做归一化展示。</p>`
    : '';
  const aPrioriPill = provenance.is_fitted === false
    ? ' <span class="pill">先验声明 · 未拟合</span>'
    : '';
  return `
    <div class="panel report-section" id="synth-provenance">
      <h3>合成 Provenance</h3>
      <p>${factorPills}</p>
      <p>
        <span class="pill">方法 ${esc(provenance.method || 'n/a')}</span>
        <span class="pill">标准化 ${esc(provenance.standardization || 'n/a')}</span>${pinnedNote}
        <span class="pill">覆盖规则 ${esc(provenance.coverage_rule || 'n/a')}</span>
        <span class="pill">min_factor_coverage ${esc(valueOr(provenance.min_factor_coverage, 'n/a'))}</span>
        <span class="pill">composite ${esc(provenance.composite_id || 'n/a')}</span>${aPrioriPill}
      </p>
      <p>${paramPillsHtml(provenance.method_params)}</p>
      ${weightsLine}
      <p class="meta">rows_required ${esc(valueOr(provenance.rows_required, 'n/a'))} · rows_full_coverage ${esc(valueOr(provenance.rows_full_coverage, 'n/a'))}</p>
      ${renderCoverageByRoleHtml(provenance.coverage_by_role || null, provenance.coverage || null)}
    </div>`;
}

function synthesisHeroHtml(factor, provenance) {
  const factorCount = provenance && provenance.factors ? provenance.factors.length : null;
  const badgeCount = factorCount === null ? 'n/a' : `${factorCount} 因子`;
  return `
    <div class="panel hero-panel report-section" id="synth-hero">
      <div>
        <p class="eyebrow">Multi-Factor Composite Report</p>
        <h3>${esc(factor.factor_id || '')} · ${esc(factor.source || 'n/a')}</h3>
        <div class="formula">${formulaHtml(factor.formula)}</div>
        <p class="meta">研究口径，不是生产交易口径。</p>
      </div>
      <div class="formula-badge">
        H${esc(valueOr(factor.horizon_days, 'n/a'))}<br>
        ${esc(badgeCount)}
      </div>
    </div>`;
}

function profileOf(result) {
  return (result && result.simulation_profile) || {};
}

/* The reused factor.js section renderers carry the single-factor report's
 * section ids; re-host each to a synth-* id so the multi report never mints
 * duplicate #report-* anchors (which would collide with the single-factor
 * module's hash routing and anchor nav). One replacement — the id appears
 * exactly once per section. */
function rehostSectionId(html, fromId, toId) {
  return html.replace(`id="${fromId}"`, `id="${toId}"`);
}

/* Full report over the §8.2 response: validity banner, composite hero,
 * provenance card, then the evaluation / in-sample / external-OOS /
 * diagnostics / evidence / artifacts sections reused 1:1 from factor.js
 * (metric.js cells and charts.js gap semantics come with them). */
export function renderSynthesisReportHtml(payload) {
  const factor = payload.factor || {};
  const provenance = payload.synthesis_provenance || null;
  const evaluation = payload.evaluation || {};
  const inSampleBacktest = payload.in_sample_backtest || null;
  const backtest = payload.backtest || {};
  const parameters = payload.parameters || {};
  const effectiveHoldingDays = parameters.holding_days || backtest.holding_days || factor.horizon_days;
  const evaluationProfile = profileOf(evaluation);
  const inSampleProfile = profileOf(inSampleBacktest);
  const backtestProfile = profileOf(backtest);
  const profile = Object.keys(backtestProfile).length ? backtestProfile : evaluationProfile;
  const coverage = evaluation.coverage_lineage || {};
  return renderValidityBannerHtml(payload.validity, provenance ? provenance.is_fitted === true : undefined)
    + synthesisHeroHtml(factor, provenance)
    + (provenance ? renderProvenanceCardHtml(provenance) : '')
    + rehostSectionId(
        renderEvaluationSection(evaluation, coverage, { effectiveHoldingDays, evaluationProfile, profile }),
        'report-evaluation', 'synth-evaluation')
    + rehostSectionId(renderInSampleSection(inSampleBacktest, inSampleProfile), 'report-insample', 'synth-insample')
    + rehostSectionId(renderOosSection(backtest, profile, backtestProfile), 'report-oos', 'synth-oos')
    + rehostSectionId(renderDiagnosticsSection(evaluation, backtest), 'report-diagnostics', 'synth-diagnostics')
    + rehostSectionId(renderEvidenceSection(evaluation, backtest), 'report-evidence', 'synth-evidence')
    + rehostSectionId(renderArtifactsSection([
        evaluation.artifact_path,
        ...(inSampleBacktest ? [inSampleBacktest.artifact_path] : []),
        backtest.artifact_path
      ]), 'report-artifacts', 'synth-artifacts');
}

/* holding_days is REQUIRED for a composite (no single-factor horizon exists
 * to fall back to; the server enforces the same rule). Pure so tests drive
 * it directly. */
export function validateHoldingDaysInput(raw) {
  const text = raw === undefined || raw === null ? '' : String(raw).trim();
  if (!text) throw new Error('持有期 holding_days 为必填参数（合成组合没有单一因子 horizon 可回退）');
  const value = Number(text);
  if (!Number.isFinite(value) || !Number.isInteger(value) || value < 1) {
    throw new Error('持有期 holding_days 必须为正整数');
  }
  return value;
}

/* Pure request builder over the §8.2 shape. Client-side guards mirror the
 * contract (>=2 factors, explicit ±1 directions, required params present,
 * finite weights covering exactly the checked set, required holding_days);
 * the backend re-validates everything. When the method pins a
 * standardization the block is OMITTED entirely (B3 deviation #6). */
export function buildRunRequest(input) {
  const factors = (input && input.factors) || [];
  if (factors.length < 2) throw new Error('请至少勾选 2 个因子再运行合成回测');
  factors.forEach(ref => {
    const direction = Number(ref.direction);
    if (direction !== 1 && direction !== -1) {
      throw new Error('因子 ' + ref.factor_id + ' 的方向必须为 +1 或 -1');
    }
  });
  const method = input.method;
  if (!method || method.available !== true) {
    throw new Error('请选择可用的合成方法（方法目录不可用时无法运行）');
  }
  const methodParams = input.methodParams || {};
  const factorIds = factors.map(ref => ref.factor_id);
  (method.params || []).forEach(spec => {
    const value = methodParams[spec.name];
    if (spec.type === 'weights') {
      const weights = value || {};
      const keys = Object.keys(weights);
      const missing = factorIds.filter(factorId => !(factorId in weights));
      const extra = keys.filter(key => factorIds.indexOf(key) === -1);
      if (spec.required && (missing.length || extra.length)) {
        throw new Error('参数 ' + (spec.label || spec.name) + ' 需要为每个已选因子提供一个权重');
      }
      keys.forEach(key => {
        if (!Number.isFinite(Number(weights[key]))) {
          throw new Error('因子 ' + key + ' 的权重必须为有限数值');
        }
      });
      return;
    }
    if (spec.required && (value === undefined || value === null || value === '')) {
      throw new Error('参数 ' + (spec.label || spec.name) + ' 为必填');
    }
    if ((spec.type === 'float' || spec.type === 'int')
        && value !== undefined && value !== null && value !== ''
        && !Number.isFinite(Number(value))) {
      throw new Error('参数 ' + (spec.label || spec.name) + ' 必须为有限数值');
    }
  });
  const parameters = input.parameters || {};
  validateHoldingDaysInput(parameters.holding_days);
  const body = {
    factor_refs: factors.map(ref => ({ factor_id: ref.factor_id, direction: Number(ref.direction) })),
    synthesis: { method: method.name, params: methodParams },
    parameters
  };
  if (!method.required_standardization) {
    if (!input.standardization) throw new Error('请选择标准化方法');
    body.standardization = { method: input.standardization, params: {} };
  }
  return body;
}

// ---------------------------------------------------------------------------
// [controller] Everything below touches fetch / DOM / events; everything
// above is pure (payload -> html string, or values -> values).
// ---------------------------------------------------------------------------

let methodsCatalog = null;
let activeSynthJobId = null;
let jobCompleteCallback = null;
/* True from run-click until the job settles: keeps the run button (and its
 * why-disabled hint) honest during the pre-202 window before a job id
 * exists. */
let runInFlight = false;

function synthMount(id) {
  return document.getElementById(id);
}

/* The DOM is the source of truth for the checked set and directions — no
 * duplicated selection state to fall out of sync. */
function checkedSelection() {
  const directions = {};
  document.querySelectorAll('#synth-factors .synth-direction').forEach(select => {
    directions[select.dataset.factorId] = Number(select.value);
  });
  const selection = [];
  document.querySelectorAll('#synth-factors .synth-factor-check').forEach(check => {
    if (!check.checked) return;
    const factorId = check.dataset.factorId;
    selection.push({
      factor_id: factorId,
      name: check.dataset.factorName || factorId,
      direction: valueOr(directions[factorId], 1)
    });
  });
  return selection;
}

/* Snapshot of every picker row's checkbox + direction, captured before the
 * factor list is re-rendered (B-MAJOR-1) so a background refresh preserves an
 * in-progress config. Keyed by factor_id -> {checked, direction}; rows that
 * survive the refresh are restored, rows that vanish are dropped. */
function captureFactorPickerState() {
  const state = {};
  document.querySelectorAll('#synth-factors .synth-factor-row').forEach(row => {
    const factorId = row.dataset.factorId;
    if (!factorId) return;
    const check = row.querySelector('.synth-factor-check');
    const direction = row.querySelector('.synth-direction');
    state[factorId] = {
      checked: Boolean(check && check.checked),
      direction: direction ? Number(direction.value) : 1
    };
  });
  return state;
}

function selectedMethod() {
  if (!methodsCatalog) return null;
  const select = synthMount('synth-method');
  if (!select) return null;
  return (methodsCatalog.methods || []).find(
    method => method.name === select.value && method.available === true
  ) || null;
}

function selectedStandardizationName() {
  const select = synthMount('synth-standardization');
  return select && !select.disabled ? (select.value || null) : null;
}

/* Standardization readiness for the run guard (B-MINOR-4): a method that
 * pins its own standardization is always satisfied on that axis (the backend
 * applies it); an unpinned method needs a usable standardization from the
 * catalog, so an empty/absent standardization catalog is a stated not-ready
 * reason rather than a run that only fails at submit. */
function standardizationReadyForSelection() {
  const method = selectedMethod();
  if (!method) return false;
  if (method.required_standardization) return true;
  return Boolean(selectedStandardizationName());
}

/* Current user entries, captured before a form regeneration so switching the
 * checked-factor set never wipes already-typed values. */
function preservedParamValues() {
  const preserved = {};
  document.querySelectorAll('#synth-params [data-param-name]').forEach(el => {
    const name = el.dataset.paramName;
    if (el.dataset.paramType === 'weights') {
      const factorId = el.dataset.weightFactor;
      if (!factorId) return;
      const bucket = preserved[name] && typeof preserved[name] === 'object' ? preserved[name] : {};
      if (el.value !== '') bucket[factorId] = el.value;
      preserved[name] = bucket;
    } else if (el.dataset.paramType === 'bool') {
      preserved[name] = el.checked;
    } else if (el.value !== undefined && el.value !== '') {
      preserved[name] = el.value;
    }
  });
  return preserved;
}

function collectMethodParams(method) {
  const params = {};
  if (!method) return params;
  (method.params || []).forEach(spec => {
    if (spec.type === 'weights') {
      const weights = {};
      document.querySelectorAll('#synth-params [data-param-type="weights"]').forEach(input => {
        if (input.dataset.paramName !== spec.name) return;
        if (input.value === '') return;
        weights[input.dataset.weightFactor] = Number(input.value);
      });
      // B-MINOR-2: an untouched OPTIONAL weights param stays omitted (the
      // same blank-optional rule enum/number already apply), so a bare `{}`
      // is never sent; a required one is always sent so the backend can
      // reject an incomplete set explicitly.
      if (spec.required || Object.keys(weights).length) params[spec.name] = weights;
      return;
    }
    // B-MINOR-1: only the recognized scalar/choice types contribute a value;
    // an unsupported spec.type rendered a notice, not an input, so it is
    // omitted here rather than fabricating a number.
    if (spec.type !== 'bool' && spec.type !== 'enum' && spec.type !== 'float' && spec.type !== 'int') return;
    let captured = null;
    document.querySelectorAll('#synth-params [data-param-name]').forEach(el => {
      if (el.dataset.paramName !== spec.name || el.dataset.paramType === 'weights') return;
      if (el.dataset.paramType) captured = el;
    });
    if (!captured) return;
    if (spec.type === 'bool') params[spec.name] = captured.checked;
    else if (spec.type === 'enum') { if (captured.value !== '') params[spec.name] = captured.value; }
    else if (captured.value !== '') params[spec.name] = Number(captured.value);
  });
  return params;
}

const BACKTEST_NUMBER_FIELDS = [
  ['decay_days', 'synth-param-decay-days'],
  ['top_quantile', 'synth-param-top-quantile'],
  ['execution_delay_days', 'synth-param-delay-days'],
  ['commission_bps', 'synth-param-commission-bps'],
  ['slippage_bps', 'synth-param-slippage-bps'],
  ['short_borrow_bps_annual', 'synth-param-short-borrow-bps']
];
/* Multi-factor module is backtest-only: it carries no research-evaluation
 * date interval (owner directive) — only the backtest window is collected. */
const BACKTEST_DATE_FIELDS = [
  ['backtest_start', 'synth-param-backtest-start'],
  ['backtest_end', 'synth-param-backtest-end']
];

/* Flat §8.2 parameters: holding_days always (required, validated); every
 * other field only when the user filled it, so backend profile defaults
 * apply to omitted values instead of the frontend inventing them. */
function backtestParametersFromForm() {
  const holdingInput = synthMount('synth-param-holding-days');
  const parameters = {
    holding_days: validateHoldingDaysInput(holdingInput ? holdingInput.value : '')
  };
  BACKTEST_NUMBER_FIELDS.forEach(([name, id]) => {
    const input = synthMount(id);
    if (input && input.value !== '') parameters[name] = Number(input.value);
  });
  BACKTEST_DATE_FIELDS.forEach(([name, id]) => {
    const input = synthMount(id);
    if (input && input.value) parameters[name] = input.value;
  });
  return parameters;
}

function updateRunEnabled() {
  const runButton = synthMount('synth-run');
  if (!runButton) return;
  const jobRunning = runInFlight || activeSynthJobId !== null;
  const standardizationReady = standardizationReadyForSelection();
  const ready = checkedSelection().length >= 2 && Boolean(selectedMethod()) && standardizationReady && !jobRunning;
  runButton.disabled = !ready;
  const hint = synthMount('synth-run-hint');
  if (hint) {
    hint.textContent = runReadinessHintText({
      checkedCount: checkedSelection().length,
      methodReady: Boolean(selectedMethod()),
      standardizationReady,
      hasCatalog: methodsCatalog !== null,
      jobRunning
    });
  }
}

function renderStandardizationArea() {
  const mount = synthMount('synth-standardization-mount');
  if (!mount) return;
  const previous = selectedStandardizationName();
  mount.innerHTML = renderStandardizationHtml(methodsCatalog, selectedMethod(), previous);
}

function regenerateParamsForm() {
  const mount = synthMount('synth-params');
  if (!mount) return;
  const preserved = preservedParamValues();
  mount.innerHTML = renderParamsFormHtml(selectedMethod(), checkedSelection(), preserved);
}

function renderMethodArea() {
  const methodMount = synthMount('synth-method-mount');
  if (!methodMount) return;
  const currentSelect = synthMount('synth-method');
  const previous = currentSelect ? currentSelect.value : null;
  methodMount.innerHTML = renderMethodSelectHtml(methodsCatalog, previous);
  renderStandardizationArea();
  regenerateParamsForm();
}

const RUNNING_PLACEHOLDER = `
    <div class="panel">
      <h3>合成与回测运行中</h3>
      <p class="meta">完成后会显示评价、样本内回测、外部样本外评测与合成 provenance。</p>
    </div>`;

async function runSynthesisBacktest() {
  const runButton = synthMount('synth-run');
  const cancelButton = synthMount('synth-cancel');
  const statusEl = synthMount('synth-status');
  const reportMount = synthMount('synth-report');
  if (!runButton || !cancelButton || !statusEl || !reportMount) return;
  const method = selectedMethod();
  let request;
  try {
    request = buildRunRequest({
      factors: checkedSelection(),
      method,
      methodParams: collectMethodParams(method),
      standardization: selectedStandardizationName(),
      parameters: backtestParametersFromForm()
    });
  } catch (error) {
    statusEl.innerHTML = `<span class="err">${esc(error.message)}</span>`;
    return;
  }
  runInFlight = true;
  updateRunEnabled();
  cancelButton.disabled = true;
  statusEl.textContent = '合成与回测中...';
  reportMount.innerHTML = RUNNING_PLACEHOLDER;
  try {
    const job = await postJson('/api/jobs/multi-factor-backtest', request);
    activeSynthJobId = job.job_id;
    cancelButton.disabled = false;
    const payload = await waitForJob(
      job.job_id,
      statusEl,
      '已运行超过10秒，系统仍在合成与回测',
      jobId => activeSynthJobId === jobId
    );
    reportMount.innerHTML = renderSynthesisReportHtml(payload);
    statusEl.innerHTML = '<span class="ok">合成回测完成</span>';
    if (jobCompleteCallback) jobCompleteCallback();
  } catch (error) {
    if (error.message === '运行已中断') {
      reportMount.innerHTML = `
    <div class="panel empty-state">
      <h3>运行已中断</h3>
      <p class="meta">本次合成回测已取消，未产生新的结果。</p>
    </div>`;
      statusEl.innerHTML = '<span class="warn">运行已中断</span>';
    } else {
      const reasonText = (error && error.message) || 'request failed';
      reportMount.innerHTML = `<div class="notice err"><span class="status-pill status-pill--fail">失败</span> ${esc(reasonText)}</div>`;
      statusEl.innerHTML = `<span class="err">${esc(reasonText)}</span>`;
    }
  } finally {
    activeSynthJobId = null;
    runInFlight = false;
    cancelButton.disabled = true;
    updateRunEnabled();
  }
}

async function cancelSynthesisRun() {
  const jobId = activeSynthJobId;
  if (!jobId) return;
  const cancelButton = synthMount('synth-cancel');
  const statusEl = synthMount('synth-status');
  if (cancelButton) cancelButton.disabled = true;
  if (statusEl) statusEl.innerHTML = '<span class="warn">已请求中断本次运行；当前安全阶段结束后停止</span>';
  try {
    await cancelJob(jobId);
  } catch (error) {
    if (statusEl) statusEl.innerHTML = `<span class="err">${esc(error.message)}</span>`;
    if (cancelButton) cancelButton.disabled = false;
  }
}

/* Panel refresher (trackedPanelRefresh contract): resolves true only when
 * BOTH catalogs rendered real data, so a degraded methods catalog keeps the
 * panel "not loaded" and the next module activation retries it. Never
 * rejects. A null fetchPanelJson result (no control token yet) skips
 * silently so the lazy retry stays alive. */
export async function refreshSynthesisPanel() {
  const factorsMount = synthMount('synth-factors');
  const methodMount = synthMount('synth-method-mount');
  if (!factorsMount || !methodMount) return false;
  let factorsOk = false;
  let methodsOk = false;
  try {
    const payload = await fetchPanelJson('/api/registry/factors');
    if (!payload) return false;
    // Capture the user's picker state right before the re-render so a job
    // completion that refreshes the list (to surface new factors) does not
    // wipe an in-progress selection + directions (B-MAJOR-1).
    const preservedSelection = captureFactorPickerState();
    factorsMount.innerHTML = renderFactorPickerHtml(payload.factors || [], preservedSelection);
    factorsOk = true;
  } catch (error) {
    factorsMount.innerHTML = `<div class="notice err"><span class="status-pill status-pill--fail">失败</span> ${esc(error.message)}</div>`;
  }
  try {
    const catalog = await fetchPanelJson('/api/synthesis/methods');
    if (catalog) {
      methodsCatalog = catalog;
      renderMethodArea();
      methodsOk = true;
    }
  } catch (error) {
    // Honest degraded state: the methods endpoint is absent or failing on
    // this deployment. State it explicitly; keep the run disabled; never
    // present the missing catalog as an empty one.
    methodsCatalog = null;
    methodMount.innerHTML = renderMethodsUnavailableHtml(error.message);
    const stdMount = synthMount('synth-standardization-mount');
    if (stdMount) stdMount.innerHTML = '';
    const paramsMount = synthMount('synth-params');
    if (paramsMount) paramsMount.innerHTML = '';
  }
  // Rebuild the params form from the restored selection so the weights
  // inputs reappear for the factors that survived the refresh (B-MAJOR-1).
  // The method branch already regenerates on the happy path; this also
  // covers the degraded-methods path, where it clears to empty (no method)
  // rather than leaving a stale form.
  regenerateParamsForm();
  updateRunEnabled();
  return factorsOk && methodsOk;
}

/* One-time event wiring, delegated on the reserved #multi-result mount.
 * app.js owns activation (lazy refresh) and passes onJobComplete so this
 * module never reaches back into app-level panel state. */
export function initSynthesisModule(options) {
  jobCompleteCallback = (options && options.onJobComplete) || null;
  const container = document.getElementById('multi-result');
  if (!container) return;
  container.addEventListener('change', event => {
    const target = event.target;
    if (!target) return;
    if (target.classList && target.classList.contains('synth-factor-check')) {
      regenerateParamsForm();
      updateRunEnabled();
    } else if (target.id === 'synth-method') {
      renderStandardizationArea();
      regenerateParamsForm();
      updateRunEnabled();
    }
  });
  container.addEventListener('click', event => {
    const target = event.target;
    if (!target) return;
    if (target.id === 'synth-run') runSynthesisBacktest();
    else if (target.id === 'synth-cancel') cancelSynthesisRun();
  });
}

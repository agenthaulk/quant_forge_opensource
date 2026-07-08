/* Data console panel over GET /api/data/catalog (G1) and GET /api/data/status
 * (G2) — CP6-3. Layout top to bottom: quality-gate banner, coverage tiles,
 * catalog fields table (availability joined from G2 by field name), and the
 * quality-gate result cards.
 *
 * FP-4 discipline: gate/availability values render as literal label pills
 * (never scalars), coverage counts fall back to 'n/a' (never 0), and quality
 * tokens render verbatim in <code> next to a mapped neutral explanation —
 * unknown tokens are shown raw, never re-interpreted. Every server-derived
 * value goes through metric.js esc() at its interpolation site.
 *
 * Partial degradation: a G1 failure replaces the panel with an error card;
 * a G2 failure keeps the fields table (availability column shows n/a) plus
 * one error notice, and the refresh reports not-loaded so the token-gated
 * lazy retry stays alive.
 */

import { esc, valueOr } from '../metric.js';
import { fetchPanelJson } from '../api.js';
import { researchTagChipsHtml } from './tags.js';

const dataResultEl = document.getElementById('data-result');

function gateBannerHtml(ok) {
  if (ok === true) {
    return '<div class="notice ok">质量门 <span class="status-pill status-pill--ok">ok</span> '
      + '<span class="meta">面板通过数据校验；下方为覆盖范围与字段可用性。</span></div>';
  }
  if (ok === false) {
    return '<div class="notice err">质量门 <span class="status-pill status-pill--fail">fail</span> '
      + '<span class="meta">数据校验发现阻塞问题，详见下方质量门结果。</span></div>';
  }
  return '<div class="notice">质量门 <span class="status-pill status-pill--neutral">n/a</span></div>';
}

function coverageGridHtml(coverage) {
  const startDate = coverage.start_date || 'n/a';
  const endDate = coverage.end_date || 'n/a';
  return `
    <div class="grid">
      <div class="tile">行数<b>${esc(valueOr(coverage.rows, 'n/a'))}</b></div>
      <div class="tile">标的数<b>${esc(valueOr(coverage.instruments, 'n/a'))}</b></div>
      <div class="tile">交易日数<b>${esc(valueOr(coverage.date_count, 'n/a'))}</b></div>
      <div class="tile">数据窗口<span class="tile-range"><span class="nowrap">${esc(startDate)}</span> → <span class="nowrap">${esc(endDate)}</span></span></div>
    </div>`;
}

function rolePillHtml(role) {
  // Role is taxonomy, not health: always the neutral tone, label literal.
  return `<span class="status-pill status-pill--neutral">${esc(role || 'n/a')}</span>`;
}

function availabilityCellHtml(field, availabilityByName, statusLoaded) {
  if ((field.role || '') === 'key') {
    // G2 skips key columns by contract; the role itself is the label.
    return '<span class="status-pill status-pill--neutral">key</span>';
  }
  if (!statusLoaded) return '<span class="metric-missing">n/a</span>';
  const entry = availabilityByName[field.name];
  const label = entry && entry.status ? String(entry.status) : '';
  if (!label) return '<span class="metric-missing">n/a</span>';
  if (label === 'available') return '<span class="status-pill status-pill--ok">available</span>';
  if (label === 'synthesized') return '<span class="status-pill status-pill--running">synthesized</span>';
  if (label === 'missing') {
    // An absent optional field is not a failure (the gate can still pass);
    // only required fields escalate to the fail tone. The label stays the
    // literal status either way.
    const tone = field.role === 'required' ? 'status-pill--fail' : 'status-pill--neutral';
    return `<span class="status-pill ${tone}">missing</span>`;
  }
  // Unknown availability labels render verbatim with the neutral tone —
  // never re-interpreted client-side.
  return `<span class="status-pill status-pill--neutral">${esc(label)}</span>`;
}

function fieldRowHtml(field, availabilityByName, statusLoaded) {
  const tags = field.tags || null;
  const notes = tags && tags.notes ? `<br><span class="meta">${esc(tags.notes)}</span>` : '';
  return `
          <tr>
            <td><code>${esc(field.name || '')}</code></td>
            <td>${rolePillHtml(field.role)}</td>
            <td>${availabilityCellHtml(field, availabilityByName, statusLoaded)}</td>
            <td>${esc(field.description || '')}${notes}</td>
            <td>${researchTagChipsHtml(tags)}</td>
          </tr>`;
}

function qualityTokenText(token) {
  if (token === 'duplicate_keys') return '面板存在重复的 (trade_date, instrument) 键';
  if (token.startsWith('null:')) return '列 ' + token.slice('null:'.length) + ' 存在空值';
  if (token.startsWith('dtype:')) return '列 ' + token.slice('dtype:'.length) + ' 数据类型与目录声明不符';
  return '数据校验发现的问题标记';
}

function qualityNoticesHtml(quality) {
  const notices = [];
  (quality.missing_columns || []).forEach(name => {
    notices.push('<div class="notice err"><span class="status-pill status-pill--fail">blocking</span> '
      + `<code>${esc(name)}</code> 目录声明的必需列缺失</div>`);
  });
  (quality.problems || []).forEach(token => {
    const raw = String(token);
    notices.push('<div class="notice err"><span class="status-pill status-pill--fail">blocking</span> '
      + `<code>${esc(raw)}</code> ${esc(qualityTokenText(raw))}</div>`);
  });
  (quality.undeclared_columns || []).forEach(name => {
    notices.push('<div class="notice warn"><span class="status-pill status-pill--running">undeclared</span> '
      + `<code>${esc(name)}</code> 存在于数据中但未在目录声明</div>`);
  });
  (quality.synthesized_columns || []).forEach(name => {
    notices.push('<div class="notice warn"><span class="status-pill status-pill--running">synthesized</span> '
      + `<code>${esc(name)}</code> 由加载器合成，缺少完整源数据支撑</div>`);
  });
  if (!notices.length) {
    notices.push('<div class="notice ok"><span class="status-pill status-pill--ok">ok</span> 数据校验未发现阻塞问题</div>');
  }
  return notices.join('');
}

export function renderData(catalog, status) {
  const statusError = status && status.__error ? String(status.__error) : '';
  const live = status && !statusError ? status : null;
  const coverage = (live && live.coverage) || {};
  const availabilityByName = {};
  ((live && live.fields) || []).forEach(entry => {
    if (entry && entry.name) availabilityByName[entry.name] = entry;
  });
  const banner = live
    ? gateBannerHtml(live.ok)
    : (statusError
      ? `<div class="notice err">数据校验状态不可用 <span class="meta">${esc(statusError)}</span></div>`
      : '');
  const fields = (catalog && catalog.fields) || [];
  // Row order = server catalog declaration order (keys first); no client
  // sorting.
  const rows = fields.map(field => fieldRowHtml(field, availabilityByName, Boolean(live))).join('')
    || '<tr><td colspan="5">暂无目录字段</td></tr>';
  const fieldsPanel = `
    <div class="panel">
      <h3>字段目录 · ${esc(valueOr(catalog && catalog.count, fields.length))} 个字段</h3>
      <div class="table-scroll">
        <table class="comparison-table data-fields-table">
          <thead>
            <tr><th>字段</th><th>角色</th><th>可用性</th><th>描述</th><th>研究标签</th></tr>
          </thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
    </div>`;
  const qualityPanel = live
    ? `
    <div class="panel">
      <h3>质量门结果</h3>
      ${qualityNoticesHtml(live.quality || {})}
    </div>`
    : '';
  dataResultEl.innerHTML = banner + coverageGridHtml(coverage) + fieldsPanel + qualityPanel;
}

// Resolves true only after a full render (never rejects), so app.js can
// retry token-gated panels lazily until both endpoints have loaded once.
export async function refreshDataPanel() {
  try {
    const [catalog, status] = await Promise.all([
      fetchPanelJson('/api/data/catalog'),
      fetchPanelJson('/api/data/status').catch(error => ({ __error: error.message }))
    ]);
    if (!catalog) return false;
    renderData(catalog, status);
    return Boolean(status) && !status.__error;
  } catch (error) {
    dataResultEl.innerHTML = `<div class="panel"><h3>数据控制台</h3><p class="meta err">${esc(error.message)}</p></div>`;
    return false;
  }
}

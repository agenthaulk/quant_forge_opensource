/* Research history panel over GET /api/research/history.
 * Rendering moved 1:1 from the former inline script. */

import { esc, metricStatusSuffix, metricValueText } from '../metric.js';
import { fetchPanelJson } from '../api.js';

const historyResultEl = document.getElementById('history-result');

export function renderHistory(payload) {
  const rows = (payload && payload.runs) || [];
  if (!rows.length) {
    historyResultEl.innerHTML = `
      <div class="panel empty-state">
        <h3>暂无研究历史</h3>
        <p class="meta">评价、回测、bench、RD 运行记录到 run index 后会展示在这里。</p>
      </div>`;
    return;
  }
  const body = rows.map(row => {
    const dataWindow = row.data_window || {};
    const windowText = dataWindow.status === 'available'
      ? `${dataWindow.start_date} .. ${dataWindow.end_date}`
      : (dataWindow.status || 'unavailable');
    const highlights = Object.entries(row.metric_highlights || {}).map(([name, entry]) =>
      `<span class="pill">${esc(name)} ${metricValueText(entry)} · ${metricStatusSuffix(entry)}</span>`
    ).join(' ');
    return `
      <tr>
        <td>${esc(row.kind || '')}<br><span class="meta">${esc(row.run_id || '')}</span></td>
        <td>${esc(row.created_at || '')}</td>
        <td><code>${esc((row.factor_ids || []).join(', '))}</code></td>
        <td>${esc(windowText)}<br><span class="meta">warnings ${esc(row.warnings_count ?? 'n/a')}</span></td>
        <td>${highlights || '<span class="pill">无指标摘要</span>'}</td>
      </tr>`;
  }).join('');
  historyResultEl.innerHTML = `
    <div class="panel">
      <h3>研究历史 · 最近 ${rows.length} 条</h3>
      <table class="comparison-table">
        <thead>
          <tr>
            <th>类型 / run_id</th>
            <th>时间</th>
            <th>因子</th>
            <th>数据窗口 / 状态</th>
            <th>指标摘要</th>
          </tr>
        </thead>
        <tbody>${body}</tbody>
      </table>
    </div>`;
}

// Resolves true only after a successful render (never rejects), so app.js
// can retry token-gated panels lazily until the first load succeeds.
export async function refreshHistoryPanel() {
  try {
    const payload = await fetchPanelJson('/api/research/history');
    if (!payload) return false;
    renderHistory(payload);
    return true;
  } catch (error) {
    historyResultEl.innerHTML = `<div class="panel"><h3>研究历史</h3><p class="meta err">${esc(error.message)}</p></div>`;
    return false;
  }
}

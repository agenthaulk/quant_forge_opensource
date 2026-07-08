/* Benchmark panel over GET /api/bench (qf factor bench artifacts).
 * Rendering moved 1:1 from the former inline script. */

import { esc, metricStatusSuffix, metricValueText } from '../metric.js';
import { fetchPanelJson } from '../api.js';

const benchResultEl = document.getElementById('bench-result');

export function renderBench(payload) {
  const latest = payload && payload.latest;
  if (!latest) {
    benchResultEl.innerHTML = `
      <div class="panel empty-state">
        <h3>暂无 bench 结果</h3>
        <p class="meta">运行 qf factor bench 后，多因子指标状态表会展示在这里。</p>
      </div>`;
    return;
  }
  if (!latest.available) {
    benchResultEl.innerHTML = `
      <div class="panel">
        <h3>Benchmark · ${esc(latest.run_id || '')}</h3>
        <p class="meta">${esc(latest.reason || 'bench artifact 不可用')}</p>
      </div>`;
    return;
  }
  const factors = latest.factors || [];
  const metricNames = Array.from(new Set(factors.flatMap(row => Object.keys(row.metrics || {}))));
  const head = metricNames.map(name => `<th>${esc(name)}</th>`).join('');
  const body = factors.map(row => {
    const statusCell = row.status === 'error'
      ? `<span class="err">error</span><br><span class="meta">${esc(row.error || '')}</span>`
      : `${esc(row.status || '')}<br><span class="meta">warnings ${esc(row.warnings_count ?? 'n/a')}</span>`;
    const cells = metricNames.map(name => {
      const entry = (row.metrics || {})[name];
      if (!entry) return '<td><span class="meta">not_recorded</span></td>';
      return `<td>${metricValueText(entry)}<br><span class="meta">${metricStatusSuffix(entry)}</span></td>`;
    }).join('');
    return `
      <tr>
        <td><code>${esc(row.factor_id || '')}</code></td>
        <td>${statusCell}</td>
        ${cells}
      </tr>`;
  }).join('');
  const summary = latest.summary || {};
  benchResultEl.innerHTML = `
    <div class="panel">
      <h3>Benchmark · ${esc(latest.run_id || '')}</h3>
      <p class="meta">${esc(latest.created_at || '')} · evaluated ${esc(summary.evaluated_factor_count ?? 'n/a')} · errors ${esc(summary.error_factor_count ?? 'n/a')} · 指标不可用时展示状态标签，不显示为 0。</p>
      <table class="comparison-table">
        <thead>
          <tr>
            <th>因子</th>
            <th>状态</th>
            ${head}
          </tr>
        </thead>
        <tbody>${body || '<tr><td colspan="2">暂无因子行</td></tr>'}</tbody>
      </table>
    </div>`;
}

export async function refreshBenchPanel() {
  try {
    const payload = await fetchPanelJson('/api/bench');
    if (payload) renderBench(payload);
  } catch (error) {
    benchResultEl.innerHTML = `<div class="panel"><h3>Benchmark</h3><p class="meta err">${esc(error.message)}</p></div>`;
  }
}

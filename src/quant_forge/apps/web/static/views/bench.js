/* Benchmark panel over GET /api/bench (qf factor bench artifacts).
 * Rendering moved 1:1 from the former inline script; the CP6-2 design pass
 * adopts the shared metricCellHtml status classes for metric cells and the
 * status-pill convention for factor error rows (text label always present —
 * color is never the sole signal). */

import { esc, metricCellHtml, metricStatusSuffix, num } from '../metric.js';
import { barChart, emptyState } from './charts.js';
import { fetchPanelJson } from '../api.js';

// C8: the canonical factor-quality metric to chart across factors, in priority
// order. The first metric that at least one factor reports as available is
// charted; a factor whose entry is missing or blocked becomes an "n/a" tick,
// never a 0 bar. A run where NO factor reports any canonical metric as
// available renders an explicit empty-state chart instead — a silent
// omission would hide that every metric was withheld (FP-4-adjacent).
const BENCH_CHART_METRIC_PRIORITY = ['rank_ic_mean', 'rank_icir', 'rank_ic_t_stat'];

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
      ? `<span class="status-pill status-pill--fail">error</span><br><span class="meta">${esc(row.error || '')}</span>`
      : `${esc(row.status || '')}<br><span class="meta">warnings ${esc(row.warnings_count ?? 'n/a')}</span>`;
    const cells = metricNames.map(name => {
      const entry = (row.metrics || {})[name];
      const suffix = entry ? `<br><span class="meta">${metricStatusSuffix(entry)}</span>` : '';
      return `<td>${metricCellHtml(entry)}${suffix}</td>`;
    }).join('');
    return `
      <tr>
        <td><code>${esc(row.factor_id || '')}</code></td>
        <td>${statusCell}</td>
        ${cells}
      </tr>`;
  }).join('');
  const summary = latest.summary || {};
  const chartMetric = BENCH_CHART_METRIC_PRIORITY.find(name =>
    factors.some(row => {
      const entry = (row.metrics || {})[name];
      return entry && entry.status === 'available';
    })
  );
  const metricChart = `<div class="qf-chart-row">${chartMetric
    ? barChart(
        factors.map(row => {
          const entry = (row.metrics || {})[chartMetric];
          const status = entry && entry.status;
          const value = entry && (status === 'available' || status === 'legacy') ? entry.value : null;
          return { label: row.factor_id || '', value };
        }),
        { ariaLabel: `Benchmark ${chartMetric} 对比`, yFormat: value => num(value, 4), emptyMessage: '暂无对比' }
      )
    : emptyState('Benchmark 指标对比', { message: '无可用指标 / metrics withheld' })
  }</div>`;
  benchResultEl.innerHTML = `
    <div class="panel">
      <h3>Benchmark · ${esc(latest.run_id || '')}</h3>
      <p class="meta">${esc(latest.created_at || '')} · evaluated ${esc(summary.evaluated_factor_count ?? 'n/a')} · errors ${esc(summary.error_factor_count ?? 'n/a')} · 指标不可用时展示状态标签，不显示为 0。</p>
      ${metricChart}
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

// Resolves true only after a successful render (never rejects), so app.js
// can retry token-gated panels lazily until the first load succeeds.
export async function refreshBenchPanel() {
  try {
    const payload = await fetchPanelJson('/api/bench');
    if (!payload) return false;
    renderBench(payload);
    return true;
  } catch (error) {
    benchResultEl.innerHTML = `<div class="panel"><h3>Benchmark</h3><p class="meta err">${esc(error.message)}</p></div>`;
    return false;
  }
}

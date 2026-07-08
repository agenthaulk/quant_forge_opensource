/* Quant Forge web frontend — THE shared MetricValue renderer (CP6-1, D8).
 *
 * FP-4 discipline lives in this module: a null metric value is never coerced
 * to 0, and a metric whose status is not available/legacy renders its status
 * label instead of a bare scalar. Every metric cell in every view must go
 * through these helpers; this file is the single definition site.
 */

export function esc(value) {
  return String(value).replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
}
export function pct(value) {
  if (value === undefined || value === null || Number.isNaN(Number(value))) return 'n/a';
  return (Number(value) * 100).toFixed(2) + '%';
}
export function num(value, digits = 4) {
  if (value === undefined || value === null || Number.isNaN(Number(value))) return 'n/a';
  return Number(value).toFixed(digits);
}
export function metricNum(value, status, digits = 4) {
  if (status && status !== 'available' && status !== 'legacy') return esc(status);
  return num(value, digits);
}
export function valueOr(value, fallback) {
  return value === undefined || value === null ? fallback : value;
}
export function hasStableDispersion(periods) {
  return Number(periods || 0) > 1;
}
export function numIfStable(value, periods, digits = 2) {
  return hasStableDispersion(periods) ? num(value, digits) : 'n/a';
}
export function pctIfStable(value, periods) {
  return hasStableDispersion(periods) ? pct(value) : 'n/a';
}
export function metricPill(label, metric) {
  if (!metric) return '';
  const status = metric.status || 'unknown';
  const method = metric.method ? ` · ${metric.method}` : '';
  const n = metric.observation_count !== undefined ? ` · N=${metric.observation_count}` : '';
  const value = metric.value === undefined || metric.value === null ? 'n/a' : num(metric.value, 4);
  return `<span class="pill">${esc(label)} ${esc(value)} · ${esc(status)}${esc(method)}${esc(n)}</span>`;
}
export function pctMetric(metric) {
  if (!metric || metric.value === undefined || metric.value === null) return 'n/a';
  return pct(metric.value);
}
export function metricValueText(entry, digits = 4) {
  if (!entry) return 'not_recorded';
  const status = entry.status || 'unknown';
  if (status === 'available') return num(entry.value, digits);
  return esc(status);
}
export function metricStatusSuffix(entry) {
  if (!entry) return '';
  const status = entry.status || 'unknown';
  const n = entry.observation_count === undefined || entry.observation_count === null ? '' : ` · N=${entry.observation_count}`;
  return `${esc(status)}${n}`;
}

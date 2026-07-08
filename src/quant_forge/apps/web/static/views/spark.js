/* Inline-SVG sparkline helper (CP6-2, D8: inline SVG only, no external
 * resources). The only chart in the Lab view: the staggered-entry daily NAV
 * series. FP-4 spirit: fewer than 2 finite points renders nothing at all —
 * never a fake flat line standing in for missing data.
 */

import { esc } from '../metric.js';

export function sparklineSvg(values, options) {
  const settings = options || {};
  const width = settings.width || 220;
  const height = settings.height || 40;
  const label = settings.label || 'sparkline';
  // Null marks (non-finite values nulled by the server) are skipped, never
  // coerced to 0: only real numeric observations become points.
  const finite = (values || [])
    .filter(value => typeof value === 'number' && Number.isFinite(value));
  if (finite.length < 2) return '';
  const pad = 2;
  const min = Math.min(...finite);
  const max = Math.max(...finite);
  const span = max - min;
  const innerWidth = width - 2 * pad;
  const innerHeight = height - 2 * pad;
  const points = finite.map((value, index) => {
    const x = pad + (index / (finite.length - 1)) * innerWidth;
    // A genuinely flat series of >=2 real points draws its midline.
    const y = span === 0
      ? pad + innerHeight / 2
      : pad + (1 - (value - min) / span) * innerHeight;
    return `${x.toFixed(2)},${y.toFixed(2)}`;
  }).join(' ');
  return `<svg class="sparkline" viewBox="0 0 ${width} ${height}" role="img" aria-label="${esc(label)}">`
    + `<title>${esc(label)}</title>`
    + `<polyline fill="none" stroke="currentColor" stroke-width="1.5" points="${points}"/></svg>`;
}

/* Honest inline-SVG charting module (CP9-1, D8: inline SVG only, no external
 * resources). This is "spark.js grown up": pure string-returning functions,
 * no DOM builder, no state, consistent with the innerHTML render architecture.
 *
 * FP-4 is the whole point of this module:
 *   - a missing / null / non-finite point is a GAP in the path, never plotted
 *     as 0 (each contiguous finite run is its own subpath);
 *   - a fully-absent series (or empty x domain) renders an explicit
 *     empty-state box, never a flat line at 0;
 *   - line axes are honest: the domain is the data range (padded), a truncated
 *     axis is labeled, and a zero reference line is drawn only when 0 is in
 *     range; bars are ALWAYS zero-based (omitting 0 there is the misleading
 *     case);
 *   - a lone finite point (missing on both sides) still renders a visible dot.
 *
 * Colors come only from existing CSS variables and currentColor, so charts
 * read in both light and dark; every referenced var is defined in both the
 * :root and the prefers-color-scheme: dark blocks in html.py. Each chart is a
 * single role="img" SVG with a descriptive aria-label and a <desc> that
 * discloses the missing-data count; the visible legend carries a swatch AND
 * the series name, so color is never the sole signal.
 */

import { esc, num } from '../metric.js';

// Ordered palette. Semantics mirror the Studio chart choices: strategy=green,
// benchmark=blue, negative/drawdown=red. Every var flips with the theme.
export const DEFAULT_SERIES_COLORS = [
  'var(--accent)', 'var(--blue)', 'var(--bad)', 'var(--warn)', 'var(--accent-2)'
];

// Plot gutters: y-label column (L), right margin (R), legend/note band (T),
// x-label band (B).
const L = 48, R = 12, T = 24, B = 28;

function isFiniteNumber(value) {
  return typeof value === 'number' && Number.isFinite(value);
}

function f(value) {
  return Number(value).toFixed(2);
}

// Explicit empty-state box: a dashed border and a muted message. No <path>,
// no bars, no numeric axis — never a flat line at 0, never a fabricated range.
// Same a11y disclosure contract as the populated charts: role="img" +
// <title> + a <desc> that states there is no plottable data and repeats the
// caller's reason text (escaped).
export function emptyState(label, options) {
  const opts = options || {};
  const width = opts.width || 680;
  const height = opts.height || 140;
  const message = opts.message || 'no data';
  const name = `${label || 'chart'}: no data`;
  return `<svg class="qf-chart qf-chart--empty" viewBox="0 0 ${width} ${height}" role="img" aria-label="${esc(name)}">`
    + `<title>${esc(name)}</title>`
    + `<desc>${esc(`no plottable data: ${message}`)}</desc>`
    + `<rect x="0.5" y="0.5" width="${width - 1}" height="${height - 1}" fill="none" stroke="var(--line)" stroke-dasharray="4 4"/>`
    + `<text x="${f(width / 2)}" y="${f(height / 2 + 4)}" text-anchor="middle" fill="var(--muted)">${esc(message)}</text>`
    + `</svg>`;
}

// Multi-series line/area over a SHARED x domain. Returns an SVG string.
export function lineChart(series, opts) {
  const options = opts || {};
  const x = options.x || [];
  const label = options.ariaLabel || 'chart';
  const width = options.width || 680;
  const height = options.height || 260;
  const yBaseline = options.yBaseline === 0 ? 0 : 'auto';
  const yFormat = options.yFormat || (value => num(value, 4));
  const xFormat = options.xFormat || (value => String(value));
  const maxXTicks = Math.max(2, options.maxXTicks || 6);
  const emptyMessage = options.emptyMessage || 'no series data';

  const list = (series || []).map((entry, index) => ({
    name: entry.name,
    values: entry.values || [],
    color: entry.color || DEFAULT_SERIES_COLORS[index % DEFAULT_SERIES_COLORS.length],
    area: Boolean(entry.area)
  }));
  const count = x.length;
  const everyFinite = list.flatMap(entry => entry.values).filter(isFiniteNumber);
  // Empty-state: no x domain, no series, or not one finite point anywhere.
  if (count === 0 || list.length === 0 || everyFinite.length === 0) {
    return emptyState(label, { width, height, message: emptyMessage });
  }

  const dataLo = Math.min(...everyFinite);
  const dataHi = Math.max(...everyFinite);
  let lo = dataLo;
  let hi = dataHi;
  if (yBaseline === 0) { lo = Math.min(0, lo); hi = Math.max(0, hi); }
  const flat = hi === lo;
  if (!flat) { const pad = (hi - lo) * 0.04; lo -= pad; hi += pad; }

  const innerW = width - L - R;
  const innerH = height - T - B;
  const plotBottom = T + innerH;
  const px = index => count === 1 ? L + innerW / 2 : L + (index / (count - 1)) * innerW;
  const py = value => flat ? (T + innerH / 2) : (T + (1 - (value - lo) / (hi - lo)) * innerH);

  // Honest y ticks: label the true rendered domain, never a fabricated 0.
  const yTicks = [];
  if (flat) {
    yTicks.push({ y: T + innerH / 2, text: yFormat(dataLo) });
  } else {
    const divisions = 4;
    for (let k = 0; k <= divisions; k++) {
      const value = lo + (k / divisions) * (hi - lo);
      yTicks.push({ y: py(value), text: yFormat(value) });
    }
  }
  const gridSvg = yTicks.map(tick =>
    `<line class="qf-grid" x1="${L}" y1="${f(tick.y)}" x2="${width - R}" y2="${f(tick.y)}" stroke="var(--line)"/>`
    + `<text class="qf-tick" x="${L - 4}" y="${f(tick.y + 3)}" text-anchor="end" fill="var(--muted)">${esc(tick.text)}</text>`
  ).join('');

  const axesSvg =
    `<line x1="${L}" y1="${T}" x2="${L}" y2="${f(plotBottom)}" stroke="var(--line-strong)"/>`
    + `<line x1="${L}" y1="${f(plotBottom)}" x2="${width - R}" y2="${f(plotBottom)}" stroke="var(--line-strong)"/>`;

  // Zero reference line: drawn (and labeled) only when 0 is inside the
  // rendered domain; the truncation note is drawn only when 0 is excluded.
  const zeroInDomain = !flat && lo <= 0 && hi >= 0;
  const zeroSvg = zeroInDomain
    ? `<line class="qf-zero" x1="${L}" y1="${f(py(0))}" x2="${width - R}" y2="${f(py(0))}" stroke="var(--muted)" stroke-dasharray="3 3"/>`
      + `<text class="qf-tick" x="${L - 4}" y="${f(py(0) + 3)}" text-anchor="end" fill="var(--muted)">${esc(yFormat(0))}</text>`
    : '';
  const zeroExcluded = flat ? (dataLo !== 0) : !zeroInDomain;
  const noteSvg = zeroExcluded
    ? `<text class="qf-note" x="${width - R}" y="${T - 4}" text-anchor="end" fill="var(--muted)">axis ${esc(yFormat(dataLo))}–${esc(yFormat(dataHi))}, not zero-based</text>`
    : '';

  // Series: each contiguous finite run is a subpath (a new M command). A
  // missing point never joins two runs and never becomes 0. A run of a single
  // finite point also gets a visible <circle> so a lone observation shows.
  let seriesSvg = '';
  list.forEach(entry => {
    const runs = [];
    let current = [];
    entry.values.forEach((value, index) => {
      if (isFiniteNumber(value)) {
        current.push({ index, value });
      } else if (current.length) {
        runs.push(current);
        current = [];
      }
    });
    if (current.length) runs.push(current);

    if (entry.area) {
      runs.forEach(run => {
        if (run.length < 2) return;
        const d = `M${f(px(run[0].index))},${f(plotBottom)}`
          + run.map(point => ` L${f(px(point.index))},${f(py(point.value))}`).join('')
          + ` L${f(px(run[run.length - 1].index))},${f(plotBottom)} Z`;
        seriesSvg += `<path class="qf-area" fill="${entry.color}" fill-opacity="0.12" stroke="none" d="${d}"/>`;
      });
    }
    // One path per series carrying every finite point as M/L; a missing index
    // ends the current subpath so the next finite point starts a fresh M.
    const d = runs.map(run =>
      run.map((point, position) =>
        `${position === 0 ? 'M' : 'L'}${f(px(point.index))},${f(py(point.value))}`
      ).join(' ')
    ).join(' ');
    seriesSvg += `<path fill="none" stroke="${entry.color}" stroke-width="1.75" d="${d}"/>`;
    runs.filter(run => run.length === 1).forEach(run => {
      seriesSvg += `<circle cx="${f(px(run[0].index))}" cy="${f(py(run[0].value))}" r="2.2" fill="${entry.color}"/>`;
    });
  });

  // x ticks: first and last are always the true endpoints; interior ticks are
  // evenly spaced indices, deduped. Horizontal, no rotation.
  const tickIndices = [];
  if (count === 1) {
    tickIndices.push(0);
  } else {
    const seen = new Set([0, count - 1]);
    for (let k = 1; k < maxXTicks - 1; k++) {
      seen.add(Math.round(k * (count - 1) / (maxXTicks - 1)));
    }
    Array.from(seen).sort((a, b) => a - b).forEach(index => tickIndices.push(index));
  }
  const xLabelY = height - 8;
  const xTicksSvg = tickIndices.map(index => {
    const anchor = index === 0 ? 'start' : (index === count - 1 ? 'end' : 'middle');
    return `<text class="qf-tick" x="${f(px(index))}" y="${xLabelY}" text-anchor="${anchor}" fill="var(--muted)">${esc(xFormat(x[index]))}</text>`;
  }).join('');

  // Legend: swatch + series name (color never the sole signal).
  let legendSvg = '';
  let legendX = L;
  list.forEach(entry => {
    const name = String(entry.name === undefined || entry.name === null ? '' : entry.name);
    legendSvg += `<rect x="${f(legendX)}" y="8" width="10" height="10" fill="${entry.color}"/>`
      + `<text x="${f(legendX + 14)}" y="17" fill="var(--ink)">${esc(name)}</text>`;
    legendX += 24 + name.length * 6.5 + 16;
  });

  const first = esc(xFormat(x[0]));
  const last = esc(xFormat(x[count - 1]));
  const xRange = count === 1 ? first : `${first}–${last}`;
  const name = `${esc(label)} — line chart, ${list.length} series, ${xRange}`;
  const desc = list.map(entry => {
    const total = entry.values.length;
    const finite = entry.values.filter(isFiniteNumber).length;
    return `${esc(entry.name)}: ${finite}/${total} points (${total - finite} missing)`;
  }).join('; ');

  return `<svg class="qf-chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="${name}">`
    + `<title>${name}</title>`
    + `<desc>${desc}</desc>`
    + gridSvg + zeroSvg + axesSvg + noteSvg + seriesSvg + xTicksSvg + legendSvg
    + `</svg>`;
}

// Categorical bars, ALWAYS zero-based. Returns an SVG string. A null/non-finite
// value renders an "n/a" tick and NO rect; a real 0 renders a zero-height rect
// at the baseline (distinct from n/a). Negatives grow downward from zero.
export function barChart(bars, opts) {
  const options = opts || {};
  const label = options.ariaLabel || 'chart';
  const width = options.width || 680;
  const height = options.height || 240;
  const yFormat = options.yFormat || (value => num(value, 4));
  const emptyMessage = options.emptyMessage || 'no data';

  const list = bars || [];
  if (list.length === 0) {
    return emptyState(label, { width, height, message: emptyMessage });
  }

  const values = list.map(bar => bar.value);
  const finiteVals = values.filter(isFiniteNumber);
  // Zero is always in the domain; omitting it for bars is the misleading case.
  let lo = 0;
  let hi = 0;
  if (finiteVals.length) { lo = Math.min(0, ...finiteVals); hi = Math.max(0, ...finiteVals); }
  if (hi === lo) hi = lo + 1; // degenerate → nominal unit so the zero line renders

  const innerW = width - L - R;
  const innerH = height - T - B;
  const plotBottom = T + innerH;
  const py = value => T + (1 - (value - lo) / (hi - lo)) * innerH;
  const zeroY = py(0);
  const bandW = innerW / list.length;
  const barW = Math.max(2, Math.min(bandW * 0.6, 56));

  let gridSvg = '';
  [lo, hi].forEach(level => {
    if (level === 0) return; // 0 handled by the dedicated zero line below
    gridSvg += `<line class="qf-grid" x1="${L}" y1="${f(py(level))}" x2="${width - R}" y2="${f(py(level))}" stroke="var(--line)"/>`
      + `<text class="qf-tick" x="${L - 4}" y="${f(py(level) + 3)}" text-anchor="end" fill="var(--muted)">${esc(yFormat(level))}</text>`;
  });

  const axesSvg =
    `<line x1="${L}" y1="${T}" x2="${L}" y2="${f(plotBottom)}" stroke="var(--line-strong)"/>`
    + `<line x1="${L}" y1="${f(plotBottom)}" x2="${width - R}" y2="${f(plotBottom)}" stroke="var(--line-strong)"/>`;

  const zeroSvg = `<line class="qf-zero" x1="${L}" y1="${f(zeroY)}" x2="${width - R}" y2="${f(zeroY)}" stroke="var(--muted)" stroke-dasharray="3 3"/>`
    + `<text class="qf-tick" x="${L - 4}" y="${f(zeroY + 3)}" text-anchor="end" fill="var(--muted)">0</text>`;

  let barsSvg = '';
  let catSvg = '';
  list.forEach((bar, index) => {
    const cx = L + (index + 0.5) * bandW;
    const value = bar.value;
    if (isFiniteNumber(value)) {
      const yv = py(value);
      const top = Math.min(zeroY, yv);
      const barH = Math.abs(yv - zeroY);
      const color = bar.color || (value < 0 ? 'var(--bad)' : 'var(--accent)');
      barsSvg += `<rect x="${f(cx - barW / 2)}" y="${f(top)}" width="${f(barW)}" height="${f(barH)}" fill="${color}"/>`;
    } else {
      barsSvg += `<text class="qf-na" x="${f(cx)}" y="${f(zeroY - 4)}" text-anchor="middle" fill="var(--muted)">n/a</text>`;
    }
    const barLabel = String(bar.label === undefined || bar.label === null ? '' : bar.label);
    catSvg += `<text class="qf-tick" x="${f(cx)}" y="${height - 8}" text-anchor="middle" fill="var(--muted)">${esc(barLabel)}</text>`;
  });

  const naCount = values.filter(value => !isFiniteNumber(value)).length;
  const range = finiteVals.length
    ? `${esc(yFormat(Math.min(...finiteVals)))}–${esc(yFormat(Math.max(...finiteVals)))}`
    : 'no values';
  const name = `${esc(label)} — bar chart, ${list.length} categories, ${range}`;
  const desc = `${list.length} categories; ${naCount} n/a`;

  return `<svg class="qf-chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="${name}">`
    + `<title>${name}</title>`
    + `<desc>${desc}</desc>`
    + gridSvg + axesSvg + zeroSvg + barsSvg + catSvg
    + `</svg>`;
}

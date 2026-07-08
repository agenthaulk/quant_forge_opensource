/* Research-tag chips: THE shared qf.research_tags.v1 renderer (CP6-3).
 *
 * Single definition site (metric.js discipline): every research-tag chip in
 * every view renders through this helper. FP-4: columns_required null
 * (unobservable) and [] (observably none) render differently and are never
 * collapsed; absent frequency/warmup/decay values render nothing (no guessed
 * defaults); a null tag set renders an explicit "无研究标签" chip, never a
 * fabricated empty tag set. Chip text is escaped here, so callers pass raw
 * server values. `notes` is free text rendered by callers as a meta line,
 * never as a chip.
 */

import { esc } from '../metric.js';

function chip(text) {
  return `<span class="pill">${esc(text)}</span>`;
}

export function researchTagChipsHtml(tags) {
  if (!tags) return '<span class="pill muted">无研究标签</span>';
  const chips = [];
  (tags.themes || []).forEach(theme => chips.push(chip(theme)));
  (tags.universe_filters || []).forEach(filterName => chips.push(chip('filter ' + filterName)));
  if (tags.frequency != null) chips.push(chip('freq ' + tags.frequency));
  if (tags.min_warmup_bars != null) chips.push(chip('warmup ' + tags.min_warmup_bars));
  if (tags.decay_horizon_days != null) chips.push(chip('decay ' + tags.decay_horizon_days + 'd'));
  if (tags.columns_required == null) chips.push('<span class="pill muted">inputs n/a</span>');
  else if (!tags.columns_required.length) chips.push(chip('inputs 无'));
  else chips.push(chip('inputs ' + tags.columns_required.join(', ')));
  return chips.join(' ');
}

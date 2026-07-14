/* views/provenance.js — THE per-value provenance badge renderer (P1, D12).
 *
 * Fifth single-renderer seat (docs/frontend_contributing.md discipline,
 * agent_sidecar_frontend.md §7): a provenance badge rendered anywhere else
 * fails the sweep (tests/test_web_pipeline_view.py). Pure render functions
 * only -- this module never fetches and never decides a badge's SOURCE.
 * apps/web/provenance.py derives every entry server-side from the parse
 * artifact and confirm-time value fingerprints (FE-L3: never a client
 * claim); this module's only job is turning an already-derived
 * ``{field, value, source, parent_value, evidence_ref, superseded_by}``
 * entry into HTML.
 */

import { esc } from '../metric.js';

// CN labels for the closed 7-value vocabulary
// (apps/web/provenance.py::PROVENANCE_SOURCES). The vocabulary is a stable
// identifier server-side; only the display label lives here (i18n
// discipline, spec §9 -- LLM narration is never the translation catalog,
// and neither is this map's KEY set, which is asserted against the
// server's own list in tests/test_web_pipeline_view.py).
const SOURCE_LABELS = {
  user_explicit: '用户明确指定',
  user_answer: '用户澄清作答',
  profile_default: '默认配置',
  fixed_policy: '固定策略',
  data_resolved: '数据侧解析',
  agent_inferred: 'AI 推断',
  human_override: '人工修改'
};

export const PROVENANCE_SOURCES = Object.keys(SOURCE_LABELS);

export function provenanceSourceLabel(source) {
  return SOURCE_LABELS[source] || source;
}

/* One badge for one provenance entry. `title` surfaces the pre-edit value
 * for a human_override entry (parent_value) without cluttering the visible
 * label -- the badge stays a short pill, the superseded value is a hover/
 * focus detail, matching the existing .term-tip discoverability pattern. */
export function provenanceBadgeHtml(entry) {
  if (!entry) return '';
  const label = provenanceSourceLabel(entry.source);
  const hasParent = entry.parent_value !== undefined && entry.parent_value !== null && entry.parent_value !== '';
  const title = hasParent ? ` title="${esc('曾为 ' + String(entry.parent_value))}"` : '';
  return `<span class="provenance-badge provenance-badge--${esc(entry.source)}"${title}>${esc(label)}</span>`;
}

/* A confirm-card VALUE can carry more than one badge (spec §5.1: "mixed-
 * origin grouped lines carry multiple badges -- badges are per value, not
 * per row"). Accepts one entry or a list so a single-value row and a
 * grouped beginner-density line share one call; a missing/undefined entry
 * renders nothing here -- callers are expected to have already asserted
 * full coverage server-side (missing badge = fail, WORKORDER P1 pin), so a
 * gap reaching this renderer is a display of that fact, not a silent patch. */
export function provenanceBadgeRowHtml(entries) {
  const list = Array.isArray(entries) ? entries : [entries];
  const html = list.filter(Boolean).map(provenanceBadgeHtml).join('');
  return `<span class="pipeline-field-badges">${html}</span>`;
}

export function provenanceEntryByField(entries) {
  const byField = {};
  (entries || []).forEach(entry => {
    if (entry && entry.field) byField[entry.field] = entry;
  });
  return byField;
}

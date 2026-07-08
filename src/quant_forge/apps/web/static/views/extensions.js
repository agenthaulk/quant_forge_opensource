/* Extensions browse panel over GET /api/extensions — CP6-4 (D7/D7a).
 *
 * Read-only view of the declarative extension registry: manifests render as
 * metadata cards, validation statuses as literal label pills (FP-4), and
 * rejection issue codes verbatim in <code> — no client-side
 * re-interpretation table in v1. The registry loads and executes nothing;
 * rejected manifests arrive as directory + issues only (the server never
 * echoes content that failed safety validation). Every server-derived value
 * goes through esc() at its interpolation site.
 *
 * Deep link: #extensions-manifest-<extension-id> targets the card carrying
 * that data-extension-id (never a DOM id derived from the id, so native
 * fragment scrolling never fights the hash routing); a one-time
 * scrollIntoView applies on the first refresh with a matching anchor.
 */

import { esc, valueOr } from '../metric.js';
import { fetchPanelJson } from '../api.js';

const extensionsResultEl = document.getElementById('extensions-result');

const EXTENSIONS_HASH_PREFIX = '#extensions-manifest-';
// Extension id charset (extensions/manifest.py) — the same rule lab.js
// pins for tab activation, here with a capture for the targeted card.
const EXTENSIONS_MANIFEST_HASH = /^#extensions-manifest-([a-z][a-z0-9]*(?:[._-][a-z0-9]+)*)$/;

let hashScrollApplied = false;

function extensionIdFromHash(hash) {
  const match = EXTENSIONS_MANIFEST_HASH.exec(hash || '');
  return match ? match[1] : null;
}

function summaryPanelHtml(payload) {
  return `
    <div class="panel">
      <h3>扩展清单 · ${esc(valueOr(payload.count, 'n/a'))} 个 · valid ${esc(valueOr(payload.valid_count, 'n/a'))} · rejected ${esc(valueOr(payload.rejected_count, 'n/a'))}</h3>
      <div class="notice">声明式注册表：manifest 仅作元数据展示，不加载、不执行任何代码。</div>
    </div>`;
}

function pointStatusPillHtml(status) {
  const label = status ? String(status) : 'n/a';
  // Capability taxonomy, not health: supported is the only live state;
  // reserved (and any unknown label) stays neutral. Labels render literally.
  const tone = label === 'supported' ? 'status-pill--ok' : 'status-pill--neutral';
  return `<span class="status-pill ${tone}">${esc(label)}</span>`;
}

function pointsPanelHtml(points) {
  // Each point chip and its status pill form one non-breaking flex item so
  // a wrap never separates a label from its status.
  const chips = points.map(point =>
    `<span class="ext-point"><span class="pill">${esc(point.point || '')}</span>${pointStatusPillHtml(point.status)}</span>`
  ).join(' ');
  // Reserved points carry their inert-by-design note as one muted line per
  // point; supported points need no prose beyond the label.
  const reservedNotes = points
    .filter(point => point.status === 'reserved')
    .map(point => `<p class="meta">${esc(point.point || '')} — ${esc(point.note || '')}</p>`)
    .join('');
  return `
    <div class="panel">
      <h3>贡献点</h3>
      <div class="ext-points">${chips}</div>
      ${reservedNotes}
    </div>`;
}

function contributionChipHtml(contribution) {
  const reservedPill = contribution.reserved === true
    ? ' <span class="status-pill status-pill--neutral">reserved</span>'
    : '';
  return `<span class="pill">${esc(contribution.point || '')} · ${esc(contribution.id || '')}</span>${reservedPill}`;
}

function validCardHtml(ext) {
  // Optional manifest fields absent -> omitted entirely (FP-4): no
  // fabricated description, no defaulted permission values.
  const description = ext.description ? `<p>${esc(ext.description)}</p>` : '';
  const contribs = (ext.contributions || []).map(contributionChipHtml).join(' ');
  const permissions = ext.permissions || {};
  const scopePills = (permissions.data_scopes || [])
    .map(scope => ` <span class="pill">${esc(scope)}</span>`)
    .join('');
  return `
      <div class="panel ext-card" data-extension-id="${esc(ext.id || '')}">
        <div class="ext-card-head">
          <h3>${esc(ext.name || '')}</h3>
          <span class="ext-version">v${esc(ext.version || '')}</span>
          <span class="status-pill status-pill--ok">valid</span>
        </div>
        <p class="meta">${esc(ext.id || '')} · ${esc(ext.kind || '')} · 目录 ${esc(ext.directory || '')}</p>
        ${description}
        <div class="ext-contribs">${contribs}</div>
        <p class="meta">network_access ${esc(valueOr(permissions.network_access, 'n/a'))} · secret_access ${esc(valueOr(permissions.secret_access, 'n/a'))}${scopePills}</p>
      </div>`;
}

function rejectedCardHtml(ext) {
  // Issue codes are closed-set server labels; they render verbatim with
  // their field locator — never re-interpreted client-side.
  const issues = (ext.issues || []).map(issue => {
    const fieldLabel = issue.field ? ` <span class="meta">${esc(issue.field)}</span>` : '';
    return `<div class="notice err"><code>${esc(issue.code || '')}</code>${fieldLabel}</div>`;
  }).join('');
  return `
      <div class="panel ext-card">
        <div class="ext-card-head">
          <h3>${esc(ext.directory || '')}</h3>
          <span class="status-pill status-pill--fail">rejected</span>
        </div>
        ${issues}
      </div>`;
}

export function renderExtensions(payload) {
  const data = payload || {};
  const points = data.points || [];
  const extensions = data.extensions || [];
  let cardsHtml;
  if (data.available === false) {
    cardsHtml = `
      <div class="panel empty-state">
        <h3>扩展目录不存在</h3>
        <p class="meta">在仓库根目录创建 extensions/&lt;name&gt;/extension.json 后会展示在这里。</p>
      </div>`;
  } else if (!extensions.length) {
    cardsHtml = `
      <div class="panel empty-state">
        <h3>未安装扩展</h3>
        <p class="meta">在仓库根目录创建 extensions/&lt;name&gt;/extension.json 后会展示在这里。</p>
        <p class="meta">注册表为声明式（D7）：不支持任何可执行贡献。</p>
      </div>`;
  } else {
    const cards = extensions
      .map(ext => (ext.status === 'valid' ? validCardHtml(ext) : rejectedCardHtml(ext)))
      .join('');
    cardsHtml = `<div class="ext-grid">${cards}</div>`;
  }
  extensionsResultEl.innerHTML = summaryPanelHtml(data) + pointsPanelHtml(points) + cardsHtml;
}

function applyManifestHash(options) {
  const extensionId = extensionIdFromHash(window.location.hash);
  if (!extensionId) return;
  const cards = extensionsResultEl.querySelectorAll('.ext-card[data-extension-id]');
  let target = null;
  cards.forEach(card => {
    if (card.dataset.extensionId === extensionId) target = card;
  });
  cards.forEach(card => {
    if (card === target) card.setAttribute('aria-current', 'true');
    else card.removeAttribute('aria-current');
  });
  if (target && options && options.scroll) target.scrollIntoView({ block: 'start' });
}

// Resolves true only after the registry rendered (never rejects). A missing
// control token skips silently so the token-gated lazy retry stays alive.
export async function refreshExtensionsPanel() {
  try {
    const payload = await fetchPanelJson('/api/extensions');
    if (!payload) return false;
    renderExtensions(payload);
    if (extensionIdFromHash(window.location.hash)) {
      const scrollOnce = !hashScrollApplied;
      hashScrollApplied = true;
      applyManifestHash({ scroll: scrollOnce });
    }
    return true;
  } catch (error) {
    extensionsResultEl.innerHTML = `<div class="panel"><h3>扩展</h3><p class="meta err">${esc(error.message)}</p></div>`;
    return false;
  }
}

// Pasting a new #extensions-manifest-<id> anchor while the tab is already
// active: lab.js only activates the tab; card targeting lives here.
window.addEventListener('hashchange', () => {
  applyManifestHash({ scroll: true });
});

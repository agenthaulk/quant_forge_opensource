/* Docs view over GET /api/docs (index) and GET /api/docs/{relpath} (one
 * rendered document) — CP6-4 master–detail.
 *
 * This module contains the ONLY unescaped insertion of server HTML in the
 * app: the detail pane inserts the document payload's `html` field as-is
 * (the single articleHtml site below). Its safety is owned by the server-side
 * renderer's escape-first contract: apps/web/markdown.py escapes the whole
 * markdown source before any parsing and only wraps already-escaped text in
 * a test-enforced output tag whitelist, so no raw source text can reach the
 * DOM through it. Every OTHER interpolation in this module — titles,
 * relpaths, section labels, error messages — goes through esc() at its
 * interpolation site.
 *
 * Selection is client-side state keyed by data-relpath attributes — no DOM
 * element ever gets an id derived from a relpath, so native fragment
 * scrolling never fights the #docs-doc-<relpath> replaceState routing.
 * views/lab.js owns activating the tab for that hash prefix; this module
 * owns which document the anchor selects (applied on refresh and on
 * hashchange while the tab is already active).
 */

import { esc } from '../metric.js';
import { fetchPanelJson } from '../api.js';

const docsResultEl = document.getElementById('docs-result');

const DOCS_HASH_PREFIX = '#docs-doc-';
// Docs relpath charset — mirrors the server-side single definition
// (_DOCS_RELPATH_SEGMENT_RE in apps/web/api.py, segments joined by '/');
// the same alphabet lab.js pins for tab activation, here with a capture
// for the selected document. The server stays authoritative: hashes it
// rejects (e.g. dot-leading segments) simply 404 on fetch.
const DOCS_DOC_HASH = /^#docs-doc-([A-Za-z0-9_][A-Za-z0-9_/.-]*)$/;

let selectedRelpath = null;
let listedRelpaths = [];
let docRequestSeq = 0;
let hashScrollApplied = false;

function relpathFromHash(hash) {
  const match = DOCS_DOC_HASH.exec(hash || '');
  return match ? match[1] : null;
}

function detailElement() {
  return document.getElementById('docs-detail');
}

function docsRowHtml(doc) {
  const relpath = doc.relpath || '';
  const current = relpath && relpath === selectedRelpath ? ' aria-current="true"' : '';
  return `
      <button type="button" class="docs-row" data-relpath="${esc(relpath)}"${current}>
        ${esc(doc.title || relpath)}
        <span class="meta">${esc(relpath)}</span>
      </button>`;
}

function docsNavHtml(sections) {
  return sections.map(sectionEntry => {
    const rows = (sectionEntry.docs || []).map(docsRowHtml).join('');
    return `<p class="docs-nav-section">${esc(sectionEntry.section || '')}</p>${rows}`;
  }).join('');
}

function detailPlaceholderHtml() {
  return `
      <div class="panel empty-state">
        <h3>未选择文档</h3>
        <p class="meta">选择左侧文档查看渲染内容。</p>
      </div>`;
}

function markSelectedRow() {
  docsResultEl.querySelectorAll('.docs-row').forEach(row => {
    if (selectedRelpath && row.dataset.relpath === selectedRelpath) {
      row.setAttribute('aria-current', 'true');
    } else {
      row.removeAttribute('aria-current');
    }
  });
}

function internalLinkErrorHtml(target) {
  return `<div class="notice err">未找到内部链接目标 <code>${esc(target)}</code><br>`
    + '<span class="meta">文档可能不存在或已被移动；重新打开本页签可刷新目录。</span></div>';
}

async function loadDocDetail(relpath, options) {
  const mount = detailElement();
  if (!mount) return;
  const seq = ++docRequestSeq;
  mount.innerHTML = `
      <div class="panel empty-state">
        <h3>加载中</h3>
        <p class="meta">正在读取并渲染文档。</p>
      </div>`;
  let payload = null;
  try {
    // Encode each path segment separately and rejoin with '/', so slashes
    // stay path separators for the server-side relpath rule.
    const encoded = relpath.split('/').map(encodeURIComponent).join('/');
    payload = await fetchPanelJson(`/api/docs/${encoded}`);
  } catch (error) {
    if (seq !== docRequestSeq) return;
    const errorMount = detailElement();
    if (!errorMount) return;
    // The server error text arrives verbatim (unknown doc -> 404 body);
    // the index nav stays usable.
    errorMount.innerHTML = `<div class="notice err">${esc(error.message)}<br>`
      + '<span class="meta">文档可能不存在或已被移动；重新打开本页签可刷新目录。</span></div>';
    return;
  }
  if (seq !== docRequestSeq) return;
  const liveMount = detailElement();
  if (!liveMount) return;
  if (!payload) {
    liveMount.innerHTML = '<div class="notice warn">需要控制令牌后重试。</div>';
    return;
  }
  const section = payload.section || '';
  const title = payload.title || '';
  const relpathLabel = payload.relpath || relpath;
  // The single unescaped insertion (see the module header): payload.html is
  // the server renderer's escape-first output; every other field is esc()-ed.
  const articleHtml = String(payload.html || '');
  liveMount.innerHTML = `
      <div class="panel">
        <p class="eyebrow">Docs · ${esc(section)}</p>
        <h3>${esc(title)}</h3>
        <p class="meta">${esc(relpathLabel)}</p>
        <hr>
        <div class="docs-article">${articleHtml}</div>
      </div>`;
  if (options && options.scrollIntoView) liveMount.scrollIntoView({ block: 'start' });
}

function selectDoc(relpath, options) {
  selectedRelpath = relpath;
  markSelectedRow();
  if (!(options && options.updateHash === false) && window.location.hash !== DOCS_HASH_PREFIX + relpath) {
    // Same replaceState-no-scroll discipline as activateTab and the
    // #registry-factor-* anchors: the URL tracks selection without
    // history spam.
    window.history.replaceState(null, '', DOCS_HASH_PREFIX + relpath);
  }
  loadDocDetail(relpath, { scrollIntoView: Boolean(options && options.scrollIntoView) });
}

export function renderDocsIndex(payload) {
  if (!payload || payload.available === false) {
    selectedRelpath = null;
    listedRelpaths = [];
    docsResultEl.innerHTML = `
      <div class="panel empty-state">
        <h3>文档不可用</h3>
        <p class="meta">未找到仓库 docs/ 目录；请在源码检出环境中运行。</p>
      </div>`;
    return;
  }
  const sections = payload.sections || [];
  listedRelpaths = [];
  sections.forEach(sectionEntry => {
    (sectionEntry.docs || []).forEach(doc => {
      if (doc && doc.relpath) listedRelpaths.push(doc.relpath);
    });
  });
  if (!listedRelpaths.length) {
    selectedRelpath = null;
    docsResultEl.innerHTML = `
      <div class="panel empty-state">
        <h3>暂无文档</h3>
        <p class="meta">docs/ 目录中还没有可渲染的 Markdown 文档。</p>
      </div>`;
    return;
  }
  docsResultEl.innerHTML = `
    <div class="docs-layout">
      <div class="docs-nav" aria-label="文档目录">${docsNavHtml(sections)}</div>
      <div class="docs-detail" id="docs-detail">${detailPlaceholderHtml()}</div>
    </div>`;
}

// Resolves true only after the index rendered (never rejects); the detail
// pane degrades in place, so a document fetch error does not block
// "loaded". A missing control token skips silently so the token-gated lazy
// retry stays alive.
export async function refreshDocsPanel() {
  try {
    const payload = await fetchPanelJson('/api/docs');
    if (!payload) return false;
    const previousSelection = selectedRelpath;
    renderDocsIndex(payload);
    if (!listedRelpaths.length) return true;
    const hashRelpath = relpathFromHash(window.location.hash);
    if (hashRelpath && listedRelpaths.includes(hashRelpath)) {
      const scrollOnce = !hashScrollApplied;
      hashScrollApplied = true;
      selectDoc(hashRelpath, { updateHash: false, scrollIntoView: scrollOnce });
    } else if (previousSelection && listedRelpaths.includes(previousSelection)) {
      selectDoc(previousSelection, { updateHash: false });
    } else {
      selectedRelpath = null;
    }
    return true;
  } catch (error) {
    docsResultEl.innerHTML = `<div class="panel"><h3>文档</h3><p class="meta err">${esc(error.message)}</p></div>`;
    return false;
  }
}

docsResultEl.addEventListener('click', event => {
  const link = event.target.closest('a.docs-link');
  if (link && docsResultEl.contains(link)) {
    // Internal doc links render as #docs-doc-<relpath> anchors; navigation
    // stays inside the view (no native fragment jump, no page scroll).
    event.preventDefault();
    const href = link.getAttribute('href') || '';
    const target = href.startsWith(DOCS_HASH_PREFIX) ? href.slice(DOCS_HASH_PREFIX.length) : href;
    if (target && listedRelpaths.includes(target)) {
      selectDoc(target, { updateHash: true });
    } else {
      const mount = detailElement();
      if (mount) mount.innerHTML = internalLinkErrorHtml(target);
    }
    return;
  }
  const row = event.target.closest('.docs-row');
  if (!row || !docsResultEl.contains(row)) return;
  const relpath = row.dataset.relpath || '';
  if (relpath) selectDoc(relpath, { updateHash: true });
});

// Pasting a new #docs-doc-<relpath> anchor while the tab is already
// active: lab.js only activates the tab; selection application lives here.
window.addEventListener('hashchange', () => {
  const relpath = relpathFromHash(window.location.hash);
  if (!relpath || !listedRelpaths.includes(relpath)) return;
  if (relpath === selectedRelpath) return;
  selectDoc(relpath, { updateHash: false, scrollIntoView: true });
});

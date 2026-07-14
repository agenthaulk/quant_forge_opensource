/* Fetch client and control-token header handling for the web frontend.
 *
 * The API key / control token is only ever read from window.sessionStorage
 * or prompted interactively; nothing in this module persists secrets.
 */

import { esc } from './metric.js';

let controlTokenRequired = false;
let controlTokenStoredListener = null;

export function configureApi(options) {
  controlTokenRequired = Boolean(options && options.controlTokenRequired);
}

export function onControlTokenStored(listener) {
  controlTokenStoredListener = listener;
}

export function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

export function controlHeaders() {
  const headers = {'Content-Type': 'application/json'};
  if (!controlTokenRequired) return headers;
  let token = window.sessionStorage.getItem('qf_control_token') || '';
  if (!token) {
    token = window.prompt('请输入本次 Web 控制令牌') || '';
    if (token) {
      window.sessionStorage.setItem('qf_control_token', token);
      if (controlTokenStoredListener) setTimeout(() => controlTokenStoredListener(), 0);
    }
  }
  if (!token) throw new Error('需要 Web 控制令牌');
  headers.Authorization = `Bearer ${token}`;
  return headers;
}

export function storedControlHeaders() {
  if (!controlTokenRequired) return {};
  const token = window.sessionStorage.getItem('qf_control_token') || '';
  if (!token) return null;
  return {Authorization: `Bearer ${token}`};
}

export async function postJson(url, payload) {
  const response = await fetch(url, {
    method: 'POST',
    headers: controlHeaders(),
    body: JSON.stringify(payload)
  });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || 'request failed');
  return body;
}

export async function getJob(jobId) {
  const response = await fetch(`/api/jobs/${encodeURIComponent(jobId)}`, {
    headers: controlHeaders()
  });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || 'request failed');
  return body;
}

export async function cancelJob(jobId) {
  return postJson(`/api/jobs/${encodeURIComponent(jobId)}/cancel`, {});
}

// P1 pipeline aggregate reads (agent_sidecar_frontend.md §2.3). Mutations
// (create/confirm/cancel/retry/parameter edits) are plain postJson calls to
// /api/pipelines* -- only the two GET shapes need their own helper, mirroring
// getJob above.
export async function getPipeline(pipelineId) {
  const response = await fetch(`/api/pipelines/${encodeURIComponent(pipelineId)}`, {
    headers: controlHeaders()
  });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || 'request failed');
  return body;
}

export async function listActivePipelines() {
  const response = await fetch('/api/pipelines', {headers: controlHeaders()});
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || 'request failed');
  return body.pipelines || [];
}

export async function fetchPanelJson(url) {
  const headers = storedControlHeaders();
  if (headers === null) return null;
  const response = await fetch(url, {headers});
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || 'request failed');
  return body;
}

export async function waitForJob(jobId, statusEl, slowText, isActive) {
  const slowTimer = setTimeout(() => {
    if (isActive(jobId)) {
      statusEl.innerHTML = `<span class="warn">${esc(slowText)}</span>`;
    }
  }, 10000);
  try {
    while (isActive(jobId)) {
      const job = await getJob(jobId);
      if (job.status === 'completed') return job.result;
      if (job.status === 'failed') throw new Error(job.error || 'request failed');
      if (job.status === 'cancelled') throw new Error('运行已中断');
      if (job.slow) {
        statusEl.innerHTML = `<span class="warn">${esc(slowText)} · ${Math.round(job.runtime_seconds)}s</span>`;
      }
      await sleep(750);
    }
    throw new Error('运行已中断');
  } finally {
    clearTimeout(slowTimer);
  }
}

/* obsdemo hosted UX — PKCE auth + chat + telemetry panel.
 * Tokens live in sessionStorage/memory only. All rendering uses textContent.
 * Deploy-time config comes from config.js (window.OBSDEMO_CONFIG).
 */
'use strict';

const CFG = window.OBSDEMO_CONFIG || {};
const REDIRECT_URI = window.location.origin + '/';

// ---------- PKCE helpers (Web Crypto) ----------
function b64url(bytes) {
  return btoa(String.fromCharCode(...new Uint8Array(bytes)))
    .replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}
function randomString(len) {
  const bytes = new Uint8Array(len);
  crypto.getRandomValues(bytes);
  return b64url(bytes).slice(0, len);
}
async function sha256(str) {
  return crypto.subtle.digest('SHA-256', new TextEncoder().encode(str));
}

async function startLogin() {
  const verifier = randomString(64);
  const state = randomString(24);
  sessionStorage.setItem('pkce_verifier', verifier);
  sessionStorage.setItem('oauth_state', state);
  const challenge = b64url(await sha256(verifier));
  const url = new URL(`https://${CFG.cognitoDomain}/oauth2/authorize`);
  url.searchParams.set('response_type', 'code');
  url.searchParams.set('client_id', CFG.clientId);
  url.searchParams.set('redirect_uri', REDIRECT_URI);
  url.searchParams.set('scope', 'openid email');
  url.searchParams.set('state', state);
  url.searchParams.set('code_challenge_method', 'S256');
  url.searchParams.set('code_challenge', challenge);
  window.location.assign(url.toString());
}

async function exchangeCode(code) {
  const verifier = sessionStorage.getItem('pkce_verifier');
  if (!verifier) throw new Error('missing PKCE verifier — start login again');
  const body = new URLSearchParams({
    grant_type: 'authorization_code',
    client_id: CFG.clientId,
    code,
    redirect_uri: REDIRECT_URI,
    code_verifier: verifier,
  });
  const res = await fetch(`https://${CFG.cognitoDomain}/oauth2/token`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body,
  });
  if (!res.ok) throw new Error('token exchange failed: HTTP ' + res.status);
  const tok = await res.json();
  sessionStorage.setItem('id_token', tok.id_token);
  if (tok.access_token) sessionStorage.setItem('access_token', tok.access_token);
  sessionStorage.removeItem('pkce_verifier');
  sessionStorage.removeItem('oauth_state');
}

function logout() {
  sessionStorage.clear();
  const url = new URL(`https://${CFG.cognitoDomain}/logout`);
  url.searchParams.set('client_id', CFG.clientId);
  url.searchParams.set('logout_uri', REDIRECT_URI);
  window.location.assign(url.toString());
}

function idToken() { return sessionStorage.getItem('id_token'); }

function tokenEmail() {
  try {
    const payload = JSON.parse(atob(idToken().split('.')[1].replace(/-/g, '+').replace(/_/g, '/')));
    return payload.email || payload['cognito:username'] || 'signed in';
  } catch { return 'signed in'; }
}

// ---------- API ----------
async function api(path, opts = {}) {
  const res = await fetch(CFG.apiUrl + path, {
    ...opts,
    headers: {
      Authorization: 'Bearer ' + idToken(),
      'Content-Type': 'application/json',
      ...(opts.headers || {}),
    },
  });
  if (res.status === 401) {
    addStatus('Session expired — please sign in again.');
    sessionStorage.removeItem('id_token');
    showLogin();
    throw new Error('unauthorized');
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
  return data;
}

// ---------- UI ----------
const chat = document.getElementById('chat');
const promptEl = document.getElementById('prompt');
let sessionLabel = newSessionLabel();

function newSessionLabel() {
  return 's' + crypto.randomUUID().replace(/-/g, '').slice(0, 12);
}
function updateSessionLabel() {
  document.getElementById('session-label').textContent = 'session: ' + sessionLabel;
}

function addStatus(text) {
  const div = document.createElement('div');
  div.className = 'status';
  div.textContent = text;
  chat.appendChild(div);
  chat.scrollTop = chat.scrollHeight;
  return div;
}

function dtdd(dl, k, v) {
  if (v === undefined || v === null || v === '') return;
  const dt = document.createElement('dt');
  dt.textContent = k;
  const dd = document.createElement('dd');
  dd.textContent = String(v);
  dl.appendChild(dt);
  dl.appendChild(dd);
}

function addMessage(role, text, telemetry) {
  const wrap = document.createElement('div');
  wrap.className = 'msg ' + role;
  const bubble = document.createElement('div');
  bubble.className = 'bubble';
  bubble.textContent = text;
  wrap.appendChild(bubble);
  if (telemetry) {
    const btn = document.createElement('button');
    btn.className = 'meta-toggle';
    btn.textContent = '▸ Telemetry';
    const panel = document.createElement('dl');
    panel.className = 'telemetry';
    renderTelemetry(panel, telemetry);
    btn.addEventListener('click', () => {
      panel.classList.toggle('open');
      btn.textContent = (panel.classList.contains('open') ? '▾' : '▸') + ' Telemetry';
    });
    wrap.appendChild(btn);
    wrap.appendChild(panel);
  }
  chat.appendChild(wrap);
  chat.scrollTop = chat.scrollHeight;
}

function renderTelemetry(dl, t) {
  dtdd(dl, 'session_id', t.session_id);
  dtdd(dl, 'actor_id', t.actor_id);
  dtdd(dl, 'runtime_session_id', t.runtime_session_id);
  dtdd(dl, 'scenario', t.scenario);
  dtdd(dl, 'trace_id', t.trace_id);
  const mp = t.memory_provenance;
  if (mp) {
    dtdd(dl, 'STM events used', mp.stm_events_used);
    dtdd(dl, 'LTM records retrieved', mp.ltm_record_count);
    (mp.ltm_records_retrieved || []).forEach((r, i) => {
      dtdd(dl, 'LTM record ' + (i + 1), r.record_id + ' — "' + r.snippet + '"');
    });
  }
  if (t.trace_id) {
    const dt = document.createElement('dt');
    dt.textContent = 'observability';
    dl.appendChild(dt);
    const dd = document.createElement('dd');
    const traceBtn = document.createElement('button');
    traceBtn.className = 'meta-toggle';
    traceBtn.textContent = '[fetch trace spans]';
    traceBtn.addEventListener('click', () => fetchTrace(t.trace_id, dl));
    const memBtn = document.createElement('button');
    memBtn.className = 'meta-toggle';
    memBtn.textContent = '[memory service logs]';
    memBtn.style.marginLeft = '10px';
    memBtn.addEventListener('click', () => fetchMemoryLogs(dl));
    dd.appendChild(traceBtn);
    dd.appendChild(memBtn);
    dl.appendChild(dd);
  }
}

function appendResultBlock(dl, title, lines) {
  const dt = document.createElement('dt');
  dt.textContent = title;
  dl.appendChild(dt);
  const dd = document.createElement('dd');
  const pre = document.createElement('pre');
  pre.textContent = lines.join('\n');
  dd.appendChild(pre);
  dl.appendChild(dd);
}

async function fetchTrace(traceId, dl) {
  const traveler = document.getElementById('traveler').value;
  appendResultBlock(dl, 'trace spans', ['fetching… (span ingestion can lag ~1-2 min)']);
  try {
    const data = await api(`/api/telemetry/trace?trace_id=${encodeURIComponent(traceId)}&traveler=${encodeURIComponent(traveler)}`);
    const lines = data.spans.length ? data.spans.map(s => {
      const dur = s.durationNano ? (Number(s.durationNano) / 1e6).toFixed(0) + 'ms' : '';
      const extras = [];
      if (s['attributes.gen_ai.request.model']) extras.push('model=' + s['attributes.gen_ai.request.model']);
      if (s['attributes.gen_ai.tool.status']) extras.push('tool.status=' + s['attributes.gen_ai.tool.status']);
      return `${s.name || '(span)'}  status=${s['status.code'] || '-'}  ${dur}  ${extras.join(' ')}`;
    }) : ['(no spans yet — ingestion lag, retry in a minute)'];
    appendResultBlock(dl, `trace spans (${data.span_count})`, lines);
  } catch (e) {
    appendResultBlock(dl, 'trace spans', ['error: ' + e.message]);
  }
}

async function fetchMemoryLogs(dl) {
  const traveler = document.getElementById('traveler').value;
  try {
    const data = await api(`/api/telemetry/memory-logs?traveler=${encodeURIComponent(traveler)}&session=${encodeURIComponent(sessionLabel)}`);
    const lines = data.events.length ? data.events.map(e =>
      new Date(e.timestamp).toISOString() + '  ' + e.message.slice(0, 300)
    ) : ['(no memory service-log events in window yet — extraction is async)'];
    appendResultBlock(dl, `memory service logs (${data.event_count})`, lines);
  } catch (e) {
    appendResultBlock(dl, 'memory service logs', ['error: ' + e.message]);
  }
}

async function send() {
  const text = promptEl.value.trim();
  if (!text) return;
  promptEl.value = '';
  const traveler = document.getElementById('traveler').value;
  const scenario = document.getElementById('scenario').value;
  addMessage('user', text);
  const statusEl = addStatus('Thinking… (scenario: ' + scenario + ')');
  try {
    const data = await api('/api/chat', {
      method: 'POST',
      body: JSON.stringify({ prompt: text, traveler, session: sessionLabel, scenario }),
    });
    statusEl.remove();
    addMessage('agent', data.response || JSON.stringify(data), data);
  } catch (e) {
    statusEl.remove();
    addMessage('agent', 'Request failed: ' + e.message);
  }
}

function showLogin() {
  document.getElementById('login-overlay').classList.remove('hidden');
}

function showApp() {
  document.getElementById('login-overlay').classList.add('hidden');
  document.getElementById('app-header').classList.remove('hidden');
  document.getElementById('app-footer').classList.remove('hidden');
  chat.classList.remove('hidden');
  document.getElementById('user-label').textContent = tokenEmail();
  updateSessionLabel();
  addStatus('Signed in. New session: ' + sessionLabel);
}

async function init() {
  document.getElementById('login-btn').addEventListener('click', startLogin);
  document.getElementById('logout').addEventListener('click', logout);
  document.getElementById('send').addEventListener('click', send);
  promptEl.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
  });
  document.getElementById('new-session').addEventListener('click', () => {
    sessionLabel = newSessionLabel();
    updateSessionLabel();
    addStatus('New session started: ' + sessionLabel);
  });
  document.getElementById('traveler').addEventListener('change', () => {
    sessionLabel = newSessionLabel();
    updateSessionLabel();
    addStatus('Traveler switched — new session: ' + sessionLabel);
  });

  const qs = new URLSearchParams(window.location.search);
  if (qs.get('code')) {
    const expected = sessionStorage.getItem('oauth_state');
    if (qs.get('state') !== expected) {
      document.getElementById('login-status').textContent = 'State mismatch — please retry sign-in.';
      window.history.replaceState({}, '', '/');
      return;
    }
    try {
      await exchangeCode(qs.get('code'));
      window.history.replaceState({}, '', '/');
    } catch (e) {
      document.getElementById('login-status').textContent = e.message;
      window.history.replaceState({}, '', '/');
      return;
    }
  }
  if (idToken()) showApp();
}

init();

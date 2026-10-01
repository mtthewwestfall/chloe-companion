/* ============================================================
   Assist — "her hands": web search, reminders, email drafts, job lookup.

   CREDENTIAL RULE (from Thoughtspace, non-negotiable):
   The user's email login (Gmail OAuth token) lives ONLY on this device,
   in IndexedDB. It is NEVER sent to our server. Gmail API calls go
   straight from this browser to googleapis.com. The server only ever
   composes draft text — this file creates the actual Gmail draft.

   Plain script (no modules), exposes window.Assist.
   ============================================================ */
window.Assist = (() => {
  'use strict';

  /* ---------- Gmail provider constants (ported from Thoughtspace) ---------- */
  const GMAIL_API = 'https://gmail.googleapis.com/gmail/v1/users/me';
  const GOOGLE_USERINFO = 'https://www.googleapis.com/oauth2/v2/userinfo';
  const GIS_SCRIPT = 'https://accounts.google.com/gsi/client';
  const GMAIL_SCOPES = [
    'https://www.googleapis.com/auth/gmail.readonly',
    'https://www.googleapis.com/auth/gmail.compose',
    'https://www.googleapis.com/auth/userinfo.email',
  ].join(' ');

  /* ---------- on-device token vault (IndexedDB, ported) ---------- */
  const DB_NAME = 'companion-email';
  const STORE = 'session';
  const KEY = 'account';
  const mem = { account: null };

  function hasIdb() { return typeof indexedDB !== 'undefined'; }
  function openDb() {
    return new Promise((resolve, reject) => {
      const req = indexedDB.open(DB_NAME, 1);
      req.onupgradeneeded = () => {
        if (!req.result.objectStoreNames.contains(STORE)) {
          req.result.createObjectStore(STORE);
        }
      };
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error || new Error('email store failed'));
    });
  }
  function sanitizeAccount(a) {
    if (!a || typeof a !== 'object') return null;
    return {
      provider: 'gmail',
      address: String(a.address || '').trim().toLowerCase(),
      accessToken: String(a.accessToken || ''),
      expiresAt: Number(a.expiresAt) || 0,
      connectedAt: String(a.connectedAt || new Date().toISOString()),
    };
  }
  async function loadAccount() {
    if (!hasIdb()) return mem.account;
    try {
      const db = await openDb();
      return await new Promise((resolve, reject) => {
        const tx = db.transaction(STORE, 'readonly');
        const rq = tx.objectStore(STORE).get(KEY);
        rq.onsuccess = () => resolve(rq.result || null);
        rq.onerror = () => reject(rq.error);
      });
    } catch (e) { return mem.account; }
  }
  async function saveAccount(a) {
    const row = sanitizeAccount(a);
    if (!row || !row.accessToken) throw new Error('No email session to save');
    mem.account = row;
    if (!hasIdb()) return row;
    const db = await openDb();
    await new Promise((resolve, reject) => {
      const tx = db.transaction(STORE, 'readwrite');
      tx.objectStore(STORE).put(row, KEY);
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error);
    });
    return row;
  }
  async function clearAccount() {
    mem.account = null;
    if (!hasIdb()) return;
    try {
      const db = await openDb();
      await new Promise((resolve, reject) => {
        const tx = db.transaction(STORE, 'readwrite');
        tx.objectStore(STORE).delete(KEY);
        tx.oncomplete = () => resolve();
        tx.onerror = () => reject(tx.error);
      });
    } catch (e) { /* already gone */ }
  }
  function tokenValid(a, skewMs) {
    skewMs = skewMs == null ? 60000 : skewMs;
    if (!a || !a.accessToken) return false;
    if (!a.expiresAt) return true;
    return Date.now() + skewMs < Number(a.expiresAt);
  }
  function publicAccount(a) {
    if (!a) return { connected: false, provider: null, address: null };
    return {
      connected: Boolean(a.accessToken),
      provider: 'gmail',
      address: a.address || null,
      connectedAt: a.connectedAt || null,
    };
  }

  /* ---------- Google Identity Services ---------- */
  let gisClient = null;
  let gisReady = null;
  let oauthClientId = null;

  function loadGis() {
    if (typeof document === 'undefined') {
      return Promise.reject(new Error('Connect email needs a browser'));
    }
    if (window.google && window.google.accounts && window.google.accounts.oauth2) {
      return Promise.resolve(window.google);
    }
    if (gisReady) return gisReady;
    gisReady = new Promise((resolve, reject) => {
      const s = document.createElement('script');
      s.src = GIS_SCRIPT;
      s.async = true;
      s.onload = () => {
        if (window.google && window.google.accounts && window.google.accounts.oauth2) {
          resolve(window.google);
        } else reject(new Error('Google sign-in loaded without OAuth'));
      };
      s.onerror = () => reject(new Error("Couldn't load Google sign-in"));
      document.head.appendChild(s);
    });
    return gisReady;
  }

  async function gmailFetch(url, token, opts) {
    opts = opts || {};
    const res = await fetch(url, {
      method: opts.method || 'GET',
      headers: Object.assign(
        { authorization: 'Bearer ' + token },
        opts.body ? { 'content-type': 'application/json' } : {}
      ),
      body: opts.body ? JSON.stringify(opts.body) : undefined,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const msg = (data.error && (data.error.message || data.error)) ||
        ('Gmail request failed (' + res.status + ')');
      const err = new Error(typeof msg === 'string' ? msg : 'Gmail request failed');
      err.status = res.status;
      throw err;
    }
    return data;
  }

  async function getClientId() {
    if (oauthClientId) return oauthClientId;
    const cfg = await apiGet('/api/v1/assist/config');
    oauthClientId = cfg.google_oauth_client_id || null;
    return oauthClientId;
  }

  async function connectEmail(prompt) {
    const clientId = await getClientId();
    if (!clientId) {
      throw new Error('Email connect isn\'t set up on this host yet — tell him.');
    }
    const google = await loadGis();
    return new Promise((resolve, reject) => {
      try {
        gisClient = google.accounts.oauth2.initTokenClient({
          client_id: clientId,
          scope: GMAIL_SCOPES,
          callback: async (resp) => {
            try {
              if (resp && resp.error) {
                throw new Error(resp.error_description || resp.error);
              }
              const accessToken = resp && resp.access_token;
              if (!accessToken) throw new Error("Couldn't connect email");
              const me = await gmailFetch(GOOGLE_USERINFO, accessToken);
              const session = {
                address: String(me.email || '').toLowerCase(),
                accessToken,
                expiresAt: Date.now() + Number(resp.expires_in || 3600) * 1000,
                connectedAt: new Date().toISOString(),
              };
              await saveAccount(session);
              resolve(publicAccount(session));
            } catch (err) { reject(err); }
          },
          error_callback: (err) => {
            reject(new Error((err && err.message) || 'Connect email was cancelled'));
          },
        });
        gisClient.requestAccessToken({ prompt: prompt == null ? 'consent' : prompt });
      } catch (err) { reject(err); }
    });
  }

  async function requireToken() {
    const existing = await loadAccount();
    if (tokenValid(existing)) return existing;
    await connectEmail('');
    const next = await loadAccount();
    if (!tokenValid(next)) throw new Error('Connect email first');
    return next;
  }

  async function disconnectEmail() {
    await clearAccount();
    try {
      if (gisClient && window.google && window.google.accounts.oauth2.revoke) {
        /* best-effort; the session is already gone from this device */
      }
    } catch (e) {}
    return publicAccount(null);
  }

  /* ---------- drafts: composed by her (server), created here ---------- */
  function b64urlEncode(text) {
    const bytes = new TextEncoder().encode(String(text || ''));
    let bin = '';
    for (const b of bytes) bin += String.fromCharCode(b);
    const b64 = btoa(bin);
    return b64.replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/g, '');
  }
  function encodeRfc822(d) {
    d = d || {};
    const clean = (v) => String(v || '').replace(/[\r\n]+/g, ' ').trim();
    if (!clean(d.to)) throw new Error('Who is this going to?');
    const lines = [
      'From: ' + clean(d.from),
      'To: ' + clean(d.to),
      'Subject: ' + clean(d.subject),
      'MIME-Version: 1.0',
      'Content-Type: text/plain; charset=UTF-8',
      '',
      String(d.body || '').replace(/\r\n/g, '\n'),
    ];
    return b64urlEncode(lines.join('\r\n'));
  }

  async function createDraft(draft) {
    const account = await requireToken();
    const raw = encodeRfc822({
      from: account.address,
      to: draft.to,
      subject: draft.subject,
      body: draft.body,
    });
    const res = await gmailFetch(GMAIL_API + '/drafts', account.accessToken, {
      method: 'POST',
      body: { message: { raw } },
    });
    return { id: res.id, threadId: res.threadId };
  }

  /* ---------- server API (authed, keyless) ---------- */
  async function apiGet(path) {
    const tok = (window.ApiClient && ApiClient.getToken()) || null;
    const headers = {};
    if (tok) headers['Authorization'] = 'Bearer ' + tok;
    const res = await fetch(CONFIG.API_BASE_URL + path, { headers });
    if (res.status === 401) throw Object.assign(new Error('unauthorized'), { status: 401 });
    if (!res.ok) throw new Error('request failed (' + res.status + ')');
    return res.json();
  }
  async function apiPost(path, body) {
    const tok = (window.ApiClient && ApiClient.getToken()) || null;
    const headers = { 'Content-Type': 'application/json' };
    if (tok) headers['Authorization'] = 'Bearer ' + tok;
    const res = await fetch(CONFIG.API_BASE_URL + path, {
      method: 'POST', headers, body: JSON.stringify(body || {}),
    });
    if (res.status === 401) throw Object.assign(new Error('unauthorized'), { status: 401 });
    if (!res.ok) throw new Error('request failed (' + res.status + ')');
    return res.json();
  }

  async function getCapabilities() {
    try { return await apiGet('/api/v1/assist/capabilities'); }
    catch (e) { return { enabled: false }; }
  }
  async function listReminders() {
    return apiGet('/api/v1/assist/reminders');
  }
  async function cancelReminder(id) {
    const tok = (window.ApiClient && ApiClient.getToken()) || null;
    const headers = {};
    if (tok) headers['Authorization'] = 'Bearer ' + tok;
    const res = await fetch(CONFIG.API_BASE_URL + '/api/v1/assist/reminders/' + id, {
      method: 'DELETE', headers,
    });
    if (!res.ok) throw new Error('cancel failed');
    return res.json();
  }

  /* ---------- reminder delivery: poll, then hand to the chat UI ---------- */
  let pollTimer = null;
  const reminderListeners = new Set();
  function onReminder(fn) { reminderListeners.add(fn); }

  async function pollDue() {
    let due = [];
    try {
      const data = await apiGet('/api/v1/assist/reminders/due');
      due = (data && data.reminders) || [];
    } catch (e) { return; }
    for (const r of due) {
      reminderListeners.forEach((fn) => {
        try { fn(r); } catch (e) {}
      });
      window.dispatchEvent(new CustomEvent('assist:reminder', { detail: r }));
    }
  }
  function startPolling() {
    if (pollTimer) return;
    pollDue();
    pollTimer = setInterval(pollDue, 60000);
  }
  function stopPolling() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }

  /* ---------- chat hook: her email drafts get created on-device ---------- */
  async function handleChatResponse(res) {
    if (res && res.email_draft) {
      try {
        const created = await createDraft(res.email_draft);
        return { ok: true, draftId: created.id };
      } catch (e) {
        return { ok: false, error: e.message || 'draft failed' };
      }
    }
    return null;
  }

  return {
    /* email vault */
    connectEmail, disconnectEmail, requireToken,
    emailStatus: async () => publicAccount(await loadAccount()),
    createDraft,
    /* server */
    getCapabilities, getConfig: getClientId,
    listReminders, cancelReminder,
    /* reminders */
    onReminder, startPolling, stopPolling,
    /* chat */
    handleChatResponse,
  };
})();

/* Proactive messaging client (2026-10-16, feat/proactive-live).
 *
 * Polls GET /api/v1/proactive/pending for queued website messages,
 * renders them into the chat, and acks them (which writes the turn
 * into history exactly once). Also syncs the in-app proactive toggle
 * to the server and manages the Web Push subscription.
 *
 * Loaded after app.js; hooks into the DOM directly so no app.js
 * changes are required.
 */
(function () {
  'use strict';

  const API_BASE = (window.CONFIG && window.CONFIG.API_BASE_URL) || '';
  const POLL_MS = 60000;

  function token() {
    return localStorage.getItem('whispr_token') || '';
  }

  async function req(method, path, body) {
    const headers = { 'Content-Type': 'application/json' };
    const t = token();
    if (t) headers['Authorization'] = 'Bearer ' + t;
    const resp = await fetch(API_BASE + path, {
      method: method,
      headers: headers,
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!resp.ok) throw new Error('proactive ' + method + ' ' + path +
                                  ' -> ' + resp.status);
    return resp.json();
  }

  const Proactive = {
    pending: () => req('GET', '/api/v1/proactive/pending'),
    ack: (ids) => req('POST', '/api/v1/proactive/ack', { ids: ids }),
    config: () => req('GET', '/api/v1/proactive/config'),
    pushSubscribe: (sub) => req('POST', '/api/v1/proactive/push-subscribe', sub),
    getPreference: () => req('GET', '/api/v1/proactive/preference'),
    setPreference: (enabled) =>
      req('POST', '/api/v1/proactive/preference', { enabled: !!enabled }),
  };
  window.Proactive = Proactive;

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');
  }

  // Render a proactive message into the open chat, or toast otherwise.
  function renderMessage(m) {
    const log = document.getElementById('chatLog');
    const nameEl = document.getElementById('chatName');
    const currentName = nameEl ? nameEl.textContent.trim() : '';
    const isOpen = log && currentName &&
      currentName.toLowerCase() ===
        String(m.character_name || '').toLowerCase();
    if (isOpen) {
      const div = document.createElement('div');
      div.className = 'bubble her';
      div.innerHTML = esc(m.text);
      log.appendChild(div);
      log.scrollTop = log.scrollHeight;
    } else if (window.showToast) {
      window.showToast((m.character_name || 'Chloe') + ': ' +
                       String(m.text).slice(0, 80));
    }
  }

  async function poll() {
    if (!token()) return;
    try {
      const data = await Proactive.pending();
      const msgs = (data && data.messages) || [];
      if (!msgs.length) return;
      const ids = [];
      for (const m of msgs) {
        renderMessage(m);
        if (m.id != null) ids.push(m.id);
      }
      if (ids.length) {
        try { await Proactive.ack(ids); } catch (e) { /* retry next poll */ }
      }
    } catch (e) { /* offline or signed out — try again next poll */ }
  }

  function urlBase64ToUint8Array(base64) {
    const pad = '='.repeat((4 - base64.length % 4) % 4);
    const b64 = (base64 + pad).replace(/-/g, '+').replace(/_/g, '/');
    const raw = atob(b64);
    const out = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
    return out;
  }

  async function ensurePushSubscription() {
    try {
      if (!('serviceWorker' in navigator) || !('PushManager' in window))
        return;
      if (Notification.permission === 'denied') return;
      const cfg = await Proactive.config();
      const key = cfg && cfg.vapid_public_key;
      if (!key) return;
      if (Notification.permission === 'default') {
        try { await Notification.requestPermission(); } catch (e) { return; }
      }
      if (Notification.permission !== 'granted') return;
      const reg = await navigator.serviceWorker.ready;
      let sub = await reg.pushManager.getSubscription();
      if (!sub) {
        sub = await reg.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: urlBase64ToUint8Array(key),
        });
      }
      const json = sub.toJSON();
      await Proactive.pushSubscribe({
        endpoint: json.endpoint,
        p256dh: (json.keys && json.keys.p256dh) || '',
        auth: (json.keys && json.keys.auth) || '',
      });
    } catch (e) { /* push is best-effort */ }
  }

  // Sync the in-app toggle to the server (app.js owns the localStorage
  // behavior; we add the server call).
  function wireToggle() {
    const t = document.getElementById('proactiveToggle');
    if (!t || t.dataset.proactiveWired) return;
    t.dataset.proactiveWired = '1';
    t.addEventListener('change', () => {
      Proactive.setPreference(t.checked).catch(() => {});
    });
    // Initialize from the server when signed in.
    if (token()) {
      Proactive.getPreference().then((d) => {
        if (d && typeof d.enabled === 'boolean') t.checked = d.enabled;
      }).catch(() => {});
    }
  }

  function start() {
    wireToggle();
    if (token()) {
      poll();
      ensurePushSubscription();
    }
    setInterval(() => { wireToggle(); poll(); }, POLL_MS);
    // Re-wire after login (app.js shows the app without a reload).
    document.addEventListener('click', function h(e) {
      if (e.target && e.target.id === 'loginBtn') {
        setTimeout(() => {
          wireToggle();
          poll();
          ensurePushSubscription();
        }, 1500);
      }
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})();

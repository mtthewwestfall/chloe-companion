/* ============================================================
   API client for the real companion backend.

   Contract (implemented by the future companion server):
     POST /api/v1/chat {user_id, message}
       -> {reply_text, voice_note_url|null, photo_url|null, messages_remaining}
     GET  /api/v1/me?user_id=
       -> {user_id, name, tier, messages_remaining, voice_notes_remaining, proactive_enabled}
     GET  /api/v1/shows
       -> {shows:[{id,title,starts_at,ticket_url}]}

   Graceful fallback: if the server is unreachable (timeout/error),
   every call falls back to MockEngine (DEMO) so the app stays
   previewable. `connected` tracks the last known server state and
   the UI can listen via onStatus().
   ============================================================ */
const ApiClient = (() => {
  // Per-device identity (2026-09-29): the server keys banks, memory, and
  // Stripe fulfillment on user_id. First launch mints a stable id in
  // localStorage; installs that predate identity keep the shared demo_user
  // row so existing history/memories carry over.
  function userId() {
    try {
      let id = localStorage.getItem('chloe_uid');
      if (!id) {
        if (localStorage.getItem('chloe_seen')) {
          id = 'demo_user';
        } else {
          const b = new Uint8Array(8);
          if (crypto.getRandomValues) crypto.getRandomValues(b);
          else for (let i = 0; i < 8; i++) b[i] = Math.floor(Math.random() * 256);
          id = 'u_' + Array.from(b, (x) => x.toString(16).padStart(2, '0')).join('');
        }
        localStorage.setItem('chloe_uid', id);
      }
      return id;
    } catch (e) {
      return 'demo_user';
    }
  }

  let connected = false;
  const listeners = new Set();

  function setConnected(v) {
    if (v !== connected) {
      connected = v;
      listeners.forEach((fn) => { try { fn(v); } catch (e) {} });
    }
  }
  function onStatus(fn) { listeners.add(fn); }

  function withTimeout(promise, ms) {
    const t = new Promise((_, rej) => setTimeout(() => rej(new Error('timeout')), ms));
    return Promise.race([promise, t]);
  }

  async function request(method, path, body) {
    const url = CONFIG.API_BASE_URL + path;
    const opts = {
      method,
      headers: { 'Content-Type': 'application/json' },
      body: body ? JSON.stringify(body) : undefined,
    };
    const resp = await withTimeout(fetch(url, opts), CONFIG.REQUEST_TIMEOUT_MS);
    if (!resp.ok) throw new Error('http ' + resp.status);
    return resp.json();
  }

  // Probe the server once (called at boot and on manual retry).
  async function probe() {
    try {
      await request('GET', '/api/v1/me?user_id=' + encodeURIComponent(userId()));
      setConnected(true);
    } catch (e) {
      setConnected(false); // DEMO fallback — server not up yet
    }
    return connected;
  }

  async function chat(message, characterId) {
    try {
      const data = await request('POST', '/api/v1/chat', {
        user_id: userId(),
        message,
        character_id: characterId || 'chloe',
      });
      setConnected(true);
      return data;
    } catch (e) {
      setConnected(false);
      return MockEngine.chat(message); // DEMO fallback
    }
  }

  async function me() {
    try {
      const data = await request('GET', '/api/v1/me?user_id=' + encodeURIComponent(userId()));
      setConnected(true);
      return data;
    } catch (e) {
      setConnected(false);
      return MockEngine.me(); // DEMO fallback
    }
  }

  async function shows() {
    try {
      const data = await request('GET', '/api/v1/shows');
      setConnected(true);
      return data;
    } catch (e) {
      setConnected(false);
      return MockEngine.shows(); // DEMO fallback
    }
  }

  // Subscription plans from the live server (names, prices, benefits,
  // Stripe checkout links). Null when offline — plans UI renders a
  // "connect to subscribe" note instead.
  async function tiers() {
    try {
      const data = await request('GET', '/api/v1/tiers');
      setConnected(true);
      return data;
    } catch (e) {
      setConnected(false);
      return null;
    }
  }

  // Character roster from the live server (admin-managed Keyhole list,
  // kh_companion only). Chloe first, always present. Null when offline —
  // the app falls back to a Chloe-only roster.
  async function characters() {
    try {
      const data = await request('GET', '/api/v1/characters');
      setConnected(true);
      return data && data.characters ? data.characters : null;
    } catch (e) {
      setConnected(false);
      return null;
    }
  }

  return { probe, chat, me, shows, tiers, characters, userId, onStatus, isConnected: () => connected };
})();

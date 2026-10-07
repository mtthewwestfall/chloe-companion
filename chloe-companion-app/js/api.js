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
    const token = localStorage.getItem('whispr_token') || '';
    const headers = { 'Content-Type': 'application/json' };
    if (token) headers['Authorization'] = 'Bearer ' + token;
    const opts = {
      method,
      headers,
      body: body ? JSON.stringify(body) : undefined,
    };
    const resp = await withTimeout(fetch(url, opts), CONFIG.REQUEST_TIMEOUT_MS);
    if (!resp.ok) throw new Error('http ' + resp.status);
    return resp.json();
  }

  // Probe the server once (called at boot and on manual retry).
  async function probe() {
    try {
      await request('GET', '/api/v1/me?user_id=' + encodeURIComponent(CONFIG.DEMO_USER_ID));
      setConnected(true);
    } catch (e) {
      setConnected(false); // DEMO fallback — server not up yet
    }
    return connected;
  }

  async function chat(message, characterId) {
    // Real chat — requires login token. No demo fallback: if it fails,
    // the caller shows the reconnect message.
    const data = await request('POST', '/api/v1/chat', {
      message,
      character_id: characterId || 'chloe',
    });
    setConnected(true);
    return data;
  }

  async function me() {
    try {
      const data = await request('GET', '/api/v1/me?user_id=' + encodeURIComponent(CONFIG.DEMO_USER_ID));
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

  async function requestPreset(girlId, presetName) {
    const data = await request('POST', '/api/v1/preset-video', {
      girl_id: girlId,
      preset: presetName,
    });
    setConnected(true);
    return data;
  }

  async function requestCustomVideo(girlId, description) {
    const data = await request('POST', '/api/v1/custom-videos', {
      girl_id: girlId,
      description,
    });
    setConnected(true);
    return data;
  }

  async function customVideos() {
    const data = await request('GET', '/api/v1/custom-videos');
    setConnected(true);
    return data.requests || [];
  }

  async function herDay(girlId) {
    const q = girlId ? '?girl_id=' + encodeURIComponent(girlId) : '';
    const data = await request('GET', '/api/v1/her-day' + q);
    setConnected(true);
    return data.posts || [];
  }

  async function herDayView(postId) {
    try {
      await request('POST', '/api/v1/her-day/view', { post_id: postId });
    } catch (e) { /* non-critical */ }
  }

  async function herDayGenerate(girlId) {
    const data = await request('POST', '/api/v1/her-day/generate', { girl_id: girlId });
    setConnected(true);
    return data;
  }

  return { probe, chat, me, shows, requestPreset, requestCustomVideo, customVideos, herDay, herDayView, herDayGenerate, onStatus, isConnected: () => connected };
})();

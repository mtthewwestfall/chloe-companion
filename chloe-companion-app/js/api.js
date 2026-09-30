/* ============================================================
   API client — email-login session client for the companion backend.

   Auth (2026-09-30): every authed call sends
   `Authorization: Bearer <token>`. The token lives in localStorage
   (`chloe_auth_token`). There is NO demo fallback and NO anonymous
   device id — without a valid session the app shows the login screen.

   Public (no token needed): tiers, shows, characters.
   ============================================================ */
const ApiClient = (() => {
  const TOKEN_KEY = 'chloe_auth_token';

  let connected = false;
  let cachedUser = null;
  const listeners = new Set();
  const authListeners = new Set();

  function getToken() {
    try { return localStorage.getItem(TOKEN_KEY); } catch (e) { return null; }
  }
  function setToken(t) {
    try {
      if (t) localStorage.setItem(TOKEN_KEY, t);
      else localStorage.removeItem(TOKEN_KEY);
    } catch (e) {}
    if (!t) cachedUser = null;
  }
  function clearToken() { setToken(null); }

  function setConnected(v) {
    if (v !== connected) {
      connected = v;
      listeners.forEach((fn) => { try { fn(v); } catch (e) {} });
    }
  }
  function onStatus(fn) { listeners.add(fn); }
  function onAuthRequired(fn) { authListeners.add(fn); }
  function fireAuthRequired() {
    authListeners.forEach((fn) => { try { fn(); } catch (e) {} });
  }

  function withTimeout(promise, ms) {
    const t = new Promise((_, rej) => setTimeout(() => rej(new Error('timeout')), ms));
    return Promise.race([promise, t]);
  }

  // detail strings from the server for auth failures we surface in the UI
  const AUTH_DETAILS = {
    wrong_email_or_password: 'Wrong email or password.',
    email_unverified: 'Please verify your email first — check your inbox.',
    email_taken: 'That email already has an account. Try logging in.',
    invalid_email: 'That email doesn’t look right.',
    login_required: 'Please log in.',
    too_many_attempts: 'Too many attempts — wait a few minutes and try again.',
    email_failed: 'Couldn’t send the email. Try again in a minute.',
    email_unavailable: 'Email is temporarily unavailable. Try again soon.',
    invalid_or_expired_link: 'That link is invalid or expired.',
  };
  function friendlyDetail(d) {
    return AUTH_DETAILS[d] || 'Something went wrong. Try again.';
  }

  async function request(method, path, body, opts) {
    opts = opts || {};
    const headers = { 'Content-Type': 'application/json' };
    const tok = getToken();
    if (tok) headers['Authorization'] = 'Bearer ' + tok;
    const resp = await withTimeout(
      fetch(CONFIG.API_BASE_URL + path, {
        method,
        headers,
        body: body ? JSON.stringify(body) : undefined,
      }),
      opts.timeoutMs || CONFIG.REQUEST_TIMEOUT_MS
    );
    if (resp.status === 401) {
      const data = await resp.json().catch(() => ({}));
      const err = new Error('unauthorized');
      err.detail = data.detail || 'login_required';
      err.status = 401;
      // Only a 401 on a request that CARRIED a token means the session died
      // (expired/revoked) — clear it and bounce to login. A 401 on a
      // tokenless call (e.g. wrong password on /auth/login) must NOT wipe
      // anything or fire the auth screen bounce.
      if (tok) {
        clearToken();
        setConnected(false);
        fireAuthRequired();
      }
      throw err;
    }
    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}));
      const err = new Error('http ' + resp.status);
      err.detail = data.detail || '';
      err.status = resp.status;
      throw err;
    }
    setConnected(true);
    return resp.json();
  }

  /* ---------- auth ---------- */

  async function signup(email, password, name) {
    // Signup sends a verification email via Resend — allow a longer timeout
    // than the normal API budget.
    const data = await request('POST', '/api/v1/auth/signup',
      { email, password, name: name || '' },
      { timeoutMs: CONFIG.AUTH_EMAIL_TIMEOUT_MS || 30000 });
    return data; // {ok, verify_sent} — NOT logged in until email verified
  }

  async function login(email, password) {
    const data = await request('POST', '/api/v1/auth/login', { email, password });
    if (data.token) {
      setToken(data.token);
      cachedUser = data;
    }
    return data;
  }

  async function logout() {
    try { await request('POST', '/api/v1/auth/logout'); } catch (e) {}
    clearToken();
    setConnected(false);
  }

  async function resendVerification(email) {
    // Same longer budget as signup — it also sends a Resend email.
    return request('POST', '/api/v1/auth/resend-verification', { email },
      { timeoutMs: CONFIG.AUTH_EMAIL_TIMEOUT_MS || 30000 });
  }

  async function setName(name) {
    const data = await request('POST', '/api/v1/auth/name', { name });
    if (cachedUser) cachedUser.name = data.name;
    return data;
  }

  async function me() {
    const data = await request('GET', '/api/v1/auth/me');
    cachedUser = data;
    return data;
  }

  function userId() {
    return cachedUser ? cachedUser.user_id : null;
  }

  /* ---------- app ---------- */

  async function chat(message, characterId) {
    return request('POST', '/api/v1/chat',
      { message, character_id: characterId || 'chloe' },
      { timeoutMs: 30000 });
  }

  async function history(characterId, limit) {
    // Saved conversation, oldest first — restores the chat on login so it
    // never resets to a blank screen.
    const q = '?character_id=' + encodeURIComponent(characterId || 'chloe') +
              '&limit=' + (limit || 50);
    const data = await request('GET', '/api/v1/chat/history' + q);
    return (data && data.turns) ? data.turns : [];
  }

  async function shows() {
    return request('GET', '/api/v1/shows');
  }

  async function tiers() {
    return request('GET', '/api/v1/tiers');
  }

  async function characters() {
    try {
      const data = await request('GET', '/api/v1/characters');
      return data && data.characters ? data.characters : null;
    } catch (e) {
      return null;
    }
  }

  return {
    getToken, setToken, clearToken,
    signup, login, logout, resendVerification, setName, me, userId,
    chat, history, shows, tiers, characters,
    onStatus, onAuthRequired, isConnected: () => connected,
    friendlyDetail,
  };
})();

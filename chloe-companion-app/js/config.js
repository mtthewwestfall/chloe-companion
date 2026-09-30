/* ============================================================
   CONFIG — single source of truth for environment values.

   Auth era (2026-09-30): the app requires an email login — there is no
   demo user and no anonymous device id, so DEMO_USER_ID is gone.
   ============================================================ */
const CONFIG = {
  // Companion backend base URL. When the app is served over HTTP(S) — e.g.
  // mounted on the companion server itself, or opened via 127.0.0.1 / a LAN
  // IP — the API lives on the same origin, so default to the page's own
  // origin. Falls back to the local dev server for non-http contexts
  // (file:// previews).
  API_BASE_URL: (typeof location !== 'undefined' && /^https?:$/.test(location.protocol))
    ? location.origin
    : 'http://localhost:8471',

  // Deep-link target for the "Shows" button / ticket links fallback.
  WEBSITE_URL: 'https://keyhole.lockeddoor.ai',

  APP_NAME: 'Chloe — AI Companion',
  REQUEST_TIMEOUT_MS: 2500,

  // Signup / resend-verification send an email through Resend — allow a
  // longer budget than the normal API timeout.
  AUTH_EMAIL_TIMEOUT_MS: 30000
};

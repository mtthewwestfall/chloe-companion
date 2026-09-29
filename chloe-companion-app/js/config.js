/* ============================================================
   CONFIG — single source of truth for environment values.
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

  // *** REPLACE BEFORE SHIPPING ***
  // Deep-link target for the "Shows" button / ticket links.
  // Placeholder only — NOT wired to any live site.
  WEBSITE_URL: 'https://example.com',

  // Fixed identity used while the real backend is unreachable.
  DEMO_USER_ID: 'demo_user',

  APP_NAME: 'Chloe — AI Companion',
  REQUEST_TIMEOUT_MS: 2500
};

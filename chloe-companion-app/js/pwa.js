/* ============================================================
   PWA helpers: Add-to-Home-Screen prompt + Web Push registration.

   - Captures beforeinstallprompt and shows a subtle install banner
     (mobile only). On iOS, where beforeinstallprompt never fires,
     the banner shows Share -> Add to Home Screen instructions.
   - Registers for Web Push via VAPID: fetches the server's public key
     from /api/v1/push/vapid-public-key, subscribes with PushManager,
     and POSTs the subscription to /api/v1/push/subscribe so proactive
     messages (good mornings, missed-you nudges) can wake the app.

   Load order: after js/config.js (needs CONFIG.API_BASE_URL).
   ============================================================ */
const Pwa = (() => {
  const DISMISS_KEY = 'whispr_install_banner_dismissed_v1';
  const SUBSCRIBED_KEY = 'whispr_push_subscribed';
  const TOKEN_KEY = 'whispr_token';
  let deferredPrompt = null;

  const isStandalone = () =>
    window.matchMedia('(display-mode: standalone)').matches ||
    window.navigator.standalone === true;
  const isIos = () => /iphone|ipad|ipod/i.test(navigator.userAgent || '');
  const isMobile = () =>
    /android|iphone|ipad|ipod|mobile/i.test(navigator.userAgent || '');

  /* ---------------- install banner ---------------- */

  function dismissBanner() {
    try { localStorage.setItem(DISMISS_KEY, '1'); } catch (e) {}
    const el = document.getElementById('pwaInstallBanner');
    if (el) el.remove();
  }

  function showBanner() {
    if (isStandalone()) return;
    if (!isMobile()) return;
    try { if (localStorage.getItem(DISMISS_KEY)) return; } catch (e) {}
    if (document.getElementById('pwaInstallBanner')) return;

    const canInstall = !!deferredPrompt;
    // iOS Safari never fires beforeinstallprompt — show manual steps.
    const instructions = (isIos() && !canInstall)
      ? 'Tap <b>Share</b> then <b>Add to Home Screen</b> to install Whispr.'
      : 'Install Whispr for the full-screen app experience.';
    const banner = document.createElement('div');
    banner.id = 'pwaInstallBanner';
    banner.className = 'pwa-install-banner';
    banner.setAttribute('role', 'dialog');
    banner.setAttribute('aria-label', 'Install Whispr AI');
    banner.innerHTML =
      '<div class="pwa-install-icon">\uD83D\uDCAC</div>' +
      '<div class="pwa-install-text"><b>Get the Whispr app</b><span>' +
      instructions + '</span></div>' +
      (canInstall
        ? '<button class="pwa-install-cta" id="pwaInstallGo">Install</button>'
        : '') +
      '<button class="pwa-install-close" id="pwaInstallClose" ' +
      'aria-label="Dismiss">\u2715</button>';
    document.body.appendChild(banner);

    document.getElementById('pwaInstallClose')
      .addEventListener('click', dismissBanner);
    const go = document.getElementById('pwaInstallGo');
    if (go) go.addEventListener('click', async () => {
      try {
        deferredPrompt.prompt();
        await deferredPrompt.userChoice;
      } catch (e) {}
      deferredPrompt = null;
      dismissBanner();
    });
  }

  window.addEventListener('beforeinstallprompt', (e) => {
    e.preventDefault();
    deferredPrompt = e;
    // Don't fight the onboarding card — surface the banner shortly after.
    setTimeout(showBanner, 4000);
  });
  window.addEventListener('appinstalled', () => {
    deferredPrompt = null;
    dismissBanner();
  });

  /* ---------------- web push ---------------- */

  function urlBase64ToUint8Array(base64String) {
    const padding = '='.repeat((4 - (base64String.length % 4)) % 4);
    const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
    const raw = window.atob(base64);
    const out = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; ++i) out[i] = raw.charCodeAt(i);
    return out;
  }

  function authHeaders(json) {
    const token = localStorage.getItem(TOKEN_KEY) || '';
    const h = {};
    if (json) h['Content-Type'] = 'application/json';
    if (token) h['Authorization'] = 'Bearer ' + token;
    return h;
  }

  async function vapidPublicKey() {
    const resp = await fetch(CONFIG.API_BASE_URL + '/api/v1/push/vapid-public-key',
      { headers: authHeaders(false) });
    if (!resp.ok) throw new Error('vapid key http ' + resp.status);
    const data = await resp.json();
    if (!data.public_key) throw new Error('no vapid public key');
    return data.public_key;
  }

  // Subscribe (or re-sync) this device for push. Never prompts for
  // notification permission unless `interactive` is true — boot re-sync
  // stays silent.
  async function subscribePush(interactive) {
    if (!('serviceWorker' in navigator) || !('PushManager' in window)) {
      return { ok: false, reason: 'unsupported' };
    }
    if (Notification.permission === 'denied') {
      return { ok: false, reason: 'denied' };
    }
    if (!localStorage.getItem(TOKEN_KEY)) {
      return { ok: false, reason: 'signed_out' };
    }
    const reg = await navigator.serviceWorker.ready;
    let sub = await reg.pushManager.getSubscription();
    if (!sub) {
      if (Notification.permission !== 'granted') {
        if (!interactive) return { ok: false, reason: 'not_granted' };
        const perm = await Notification.requestPermission();
        if (perm !== 'granted') return { ok: false, reason: 'dismissed' };
      }
      const key = await vapidPublicKey();
      sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(key),
      });
    }
    const resp = await fetch(CONFIG.API_BASE_URL + '/api/v1/push/subscribe', {
      method: 'POST',
      headers: authHeaders(true),
      body: JSON.stringify({ subscription: sub.toJSON() }),
    });
    if (!resp.ok) throw new Error('subscribe http ' + resp.status);
    try { localStorage.setItem(SUBSCRIBED_KEY, '1'); } catch (e) {}
    return { ok: true };
  }

  async function unsubscribePush() {
    try {
      if ('serviceWorker' in navigator && 'PushManager' in window) {
        const reg = await navigator.serviceWorker.ready;
        const sub = await reg.pushManager.getSubscription();
        if (sub) {
          if (localStorage.getItem(TOKEN_KEY)) {
            await fetch(CONFIG.API_BASE_URL + '/api/v1/push/subscribe', {
              method: 'DELETE',
              headers: authHeaders(true),
              body: JSON.stringify({ endpoint: sub.endpoint }),
            }).catch(() => {});
          }
          await sub.unsubscribe().catch(() => {});
        }
      }
    } finally {
      try { localStorage.removeItem(SUBSCRIBED_KEY); } catch (e) {}
    }
  }

  function toast(msg) {
    const t = document.getElementById('toast');
    if (!t) return;
    t.textContent = msg;
    t.classList.remove('hidden');
    setTimeout(() => t.classList.add('hidden'), 4000);
  }

  // Profile-sheet toggle: "Push notifications — she can reach you anytime".
  function wirePushToggle() {
    const toggle = document.getElementById('pushToggle');
    if (!toggle) return;
    let subscribed = false;
    try { subscribed = localStorage.getItem(SUBSCRIBED_KEY) === '1'; } catch (e) {}
    toggle.checked = subscribed && Notification.permission === 'granted';
    toggle.addEventListener('change', async () => {
      if (toggle.checked) {
        toggle.disabled = true;
        let r = { ok: false };
        try { r = await subscribePush(true); } catch (e) {}
        toggle.disabled = false;
        toggle.checked = !!r.ok;
        if (!r.ok) {
          toast(r.reason === 'denied'
            ? 'Notifications are blocked — allow them in your browser settings first.'
            : 'Could not enable push notifications yet.');
        } else {
          toast('Push notifications on — she can reach you anytime.');
        }
      } else {
        await unsubscribePush();
        toggle.checked = false;
        toast('Push notifications off.');
      }
    });
  }

  async function init() {
    // iOS install hint (no beforeinstallprompt on iOS).
    if (isIos() && !isStandalone()) setTimeout(showBanner, 6000);
    wirePushToggle();
    // Silent re-sync of an existing subscription on boot (key rotation,
    // re-login). Never prompts — subscribePush(false) bails when the
    // permission isn't already granted.
    try {
      if (Notification.permission === 'granted' &&
          localStorage.getItem(TOKEN_KEY)) {
        await subscribePush(false);
      }
    } catch (e) {}
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  return { showBanner, dismissBanner, subscribePush, unsubscribePush };
})();

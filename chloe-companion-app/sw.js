/* Service worker: cache-first app shell + Web Push notifications.
   All assets are local, so the app works offline-ish once installed.
   Version the cache name on changes. */
const CACHE = 'whispr-pwa-v24-20261018';
const SHELL = [
  './index.html',
  './manifest.json',
  './css/app.css',
  './css/pwa.css',
  './js/config.js',
  './js/mock.js',
  './js/api.js',
  './js/app.js',
  './js/pwa.js',
  './js/proactive.js',
  './icons/icon-192.png',
  './icons/icon-512.png',
  './icons/apple-touch-icon.png',
];
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) => cache.addAll(SHELL))
      .then(() => self.skipWaiting())
  );
});
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});
self.addEventListener('fetch', (event) => {
  // Never cache or serve API traffic — balances and chat must always be live.
  if (event.request.method !== 'GET') return;
  if (new URL(event.request.url).pathname.startsWith('/api/')) return;
  event.respondWith(
    caches.match(event.request, { ignoreSearch: true }).then((cached) => {
      if (cached) return cached;
      return fetch(event.request).then((resp) => {
        // Cache same-origin GETs for offline use.
        if (resp && resp.ok &&
            new URL(event.request.url).origin === self.location.origin) {
          const copy = resp.clone();
          caches.open(CACHE).then((cache) => cache.put(event.request, copy));
        }
        return resp;
      }).catch(() => {
        // Offline fallback for navigations.
        if (event.request.mode === 'navigate')
          return caches.match('./index.html');
        throw new Error('offline');
      });
    })
  );
});

/* ---- Web Push: proactive messages (good mornings, missed-you nudges,
   show hype) arrive here even when the app is closed. Payload shape:
   {title, body, url}. Tapping a notification opens/focuses the app. ---- */
self.addEventListener('push', (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch (e) { /* non-JSON push — use defaults */ }
  const title = data.title || 'Whispr AI';
  const options = {
    body: data.body || 'She sent you a message.',
    icon: './icons/icon-192.png',
    badge: './icons/icon-192.png',
    image: data.image || undefined,
    data: { url: data.url || './' },
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || './';
  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then((wins) => {
      for (const w of wins) {
        if ('focus' in w) {
          if (url && url !== './' && 'navigate' in w) { try { w.navigate(url); } catch (e) {} }
          return w.focus();
        }
      }
      if (clients.openWindow) return clients.openWindow(url);
    })
  );
});

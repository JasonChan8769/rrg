// Offline support: keeps the app and the last prices on the phone.
// App files and prices: try the network first (so updates show), fall back to the saved copy.
// Fonts: saved copy first.
const SHELL = 'rrg-shell-v1', DATA = 'rrg-data-v1', FONTS = 'rrg-fonts-v1';
const SHELL_FILES = ['./', 'index.html', 'manifest.webmanifest', 'icons/icon-192.png', 'icons/icon-512.png', 'icons/apple-touch-icon.png'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(SHELL).then((c) => c.addAll(SHELL_FILES)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', (e) => {
  e.waitUntil(caches.keys()
    .then((keys) => Promise.all(keys.filter((k) => ![SHELL, DATA, FONTS].includes(k)).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

function networkFirst(req, cacheName, key) {
  return fetch(req).then((res) => {
    if (res.ok) { const copy = res.clone(); caches.open(cacheName).then((c) => c.put(key, copy)); }
    return res;
  }).catch(() => caches.match(key, { ignoreSearch: true }).then((hit) => hit || Promise.reject(new Error('offline'))));
}

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.hostname === 'fonts.googleapis.com' || url.hostname === 'fonts.gstatic.com') {
    e.respondWith(caches.match(req).then((hit) => hit || fetch(req).then((res) => {
      const copy = res.clone(); caches.open(FONTS).then((c) => c.put(req, copy)); return res;
    })));
    return;
  }
  if (url.origin !== location.origin) return;
  const key = url.origin + url.pathname;
  if (url.pathname.includes('/data/')) { e.respondWith(networkFirst(req, DATA, key)); return; }
  e.respondWith(networkFirst(req, SHELL, key).catch(() => caches.match('index.html')));
});

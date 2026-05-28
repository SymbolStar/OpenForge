/* OpenForge service worker — installable PWA shell only.
 *
 * Strategy:
 *   - /api/*           → network-only, never cached (live data, SSE, etc.)
 *   - same-origin GET  → stale-while-revalidate for a small allow-list of
 *                        static shell assets; everything else falls through
 *                        to network without touching the cache.
 *
 * Bump CACHE_VERSION whenever you change cached files or this file itself
 * so old clients pull fresh copies on activate.
 */
const CACHE_VERSION = 'openforge-shell-v1';

// Allow-list of path prefixes we are willing to cache. Anything outside this
// list (HTML pages, /api/*, ad-hoc endpoints) is left to the network.
const SHELL_PREFIXES = [
  '/style.css',
  '/xiaof.css',
  '/app.js',
  '/xiaof.js',
  '/src/',
  '/branding/',
  '/assets/',
  '/favicon.ico',
  '/manifest.webmanifest',
];

self.addEventListener('install', (event) => {
  // Activate the new SW immediately on next load.
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    const names = await caches.keys();
    await Promise.all(
      names.filter((n) => n !== CACHE_VERSION).map((n) => caches.delete(n))
    );
    await self.clients.claim();
  })());
});

function isShellAsset(url) {
  if (url.origin !== self.location.origin) return false;
  const p = url.pathname;
  if (p.startsWith('/api/')) return false;
  return SHELL_PREFIXES.some((prefix) =>
    prefix.endsWith('/') ? p.startsWith(prefix) : p === prefix
  );
}

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return; // never cache non-GET

  const url = new URL(req.url);

  // Hard rule: /api/* is always network-only. Belt-and-suspenders even
  // though isShellAsset() already excludes it.
  if (url.origin === self.location.origin && url.pathname.startsWith('/api/')) {
    return; // let the browser do its normal thing
  }

  if (!isShellAsset(url)) return;

  event.respondWith((async () => {
    const cache = await caches.open(CACHE_VERSION);
    const cached = await cache.match(req);
    const network = fetch(req)
      .then((res) => {
        // Only cache successful, basic (same-origin) responses.
        if (res && res.ok && res.type === 'basic') {
          cache.put(req, res.clone()).catch(() => {});
        }
        return res;
      })
      .catch(() => cached); // offline: fall back to cache if we have it
    return cached || network;
  })());
});

/* OpenForge service worker — installable PWA shell only.
 *
 * Strategy:
 *   - /api/*           → network-only, never cached (live data, SSE, etc.)
 *   - shell allow-list → network-first with cache fallback. Fresh code
 *                        wins on every load; cache is only used when the
 *                        network actually fails (offline). This is what
 *                        keeps a single hard refresh (Cmd+Shift+R) enough
 *                        to pick up new app.js / style.css — the older
 *                        stale-while-revalidate strategy meant the user
 *                        always saw the previous version on the first
 *                        load after a deploy.
 *   - everything else → falls through to the network without touching
 *                        the cache.
 *
 * Bump CACHE_VERSION whenever you change cached files or this file itself
 * so old clients pull fresh copies on activate.
 */
const CACHE_VERSION = 'openforge-shell-v2';

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
    try {
      const res = await fetch(req);
      // Only cache successful, basic (same-origin) responses.
      if (res && res.ok && res.type === 'basic') {
        cache.put(req, res.clone()).catch(() => {});
      }
      return res;
    } catch (_e) {
      // Network failed (offline / DNS / etc.) → serve from cache if we
      // have it; otherwise let the browser surface the network error.
      const cached = await cache.match(req);
      if (cached) return cached;
      throw _e;
    }
  })());
});

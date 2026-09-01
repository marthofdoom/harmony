"use strict";
// Harmony service worker.
//
// Strategy: NETWORK-FIRST for the app shell (HTML/JS/CSS). The app is a client
// of a live instance, so it's almost always online — fetching fresh code on
// every load means a new release is picked up on the next reload with no manual
// "unregister the service worker" dance. The cache is only a fallback for when
// the instance is unreachable (true offline), so the shell still opens.
//
// Static images (icons) are cache-first — they never change within a release
// and there's no reason to re-fetch them. API/stream/health responses are never
// cached; they must always hit the live instance.

const CACHE = "harmony-shell";
const SHELL = [
  "/",
  "/index.html",
  "/style.css",
  "/app.js",
  "/manifest.webmanifest",
  "/icon.svg",
  "/icon-192.png",
  "/icon-512.png",
];

self.addEventListener("install", (event) => {
  // Warm the cache for offline fallback, then take over immediately.
  event.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  // Drop any caches from older versions of this worker (e.g. the old
  // "harmony-shell-vN" names) and control open pages right away.
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

const isImage = (p) => /\.(png|svg|jpg|jpeg|webp|ico|gif)$/.test(p);

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;
  // Live data must never be served from cache.
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/stream/") || url.pathname === "/healthz") return;

  // Icons/images: cache-first (immutable within a release).
  if (isImage(url.pathname)) {
    event.respondWith(
      caches.match(req).then((cached) =>
        cached || fetch(req).then((resp) => {
          if (resp.ok) { const copy = resp.clone(); caches.open(CACHE).then((c) => c.put(req, copy)); }
          return resp;
        })
      )
    );
    return;
  }

  // App shell (HTML/JS/CSS): network-first — always get the latest code when the
  // instance is reachable; fall back to cache only when the fetch fails.
  event.respondWith(
    fetch(req).then((resp) => {
      if (resp.ok) { const copy = resp.clone(); caches.open(CACHE).then((c) => c.put(req, copy)); }
      return resp;
    }).catch(() => caches.match(req).then((cached) => cached || caches.match("/index.html")))
  );
});

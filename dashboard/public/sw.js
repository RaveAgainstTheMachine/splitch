/* Minimal pass-through Service Worker for desktop PWA installability.
   Never caches API, streaming, or WebSocket traffic. */
self.addEventListener("install", () => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener("fetch", (event) => {
  if (event.request.method !== "GET") return;
  const url = new URL(event.request.url);
  if (
    url.origin !== self.location.origin ||
    url.pathname.startsWith("/v1/") ||
    url.pathname.startsWith("/api/") ||
    url.pathname === "/health" ||
    url.pathname === "/metrics"
  ) {
    return;
  }
  event.respondWith(fetch(event.request));
});

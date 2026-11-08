// Pantry service worker. Static files: cache-first. Pages: network-first, falling back to the
// last copy so the list still opens with weak signal in the store. Never caches POSTs, and never
// caches a response that isn't a same-origin 200 (so a Cloudflare sign-in redirect passes through
// to the browser instead of being stored).
const V = "pantry-__VERSION__";
const SHELL = ["/static/app.css", "/static/app.js", "/static/icon.svg", "/static/icon-192.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(V).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== V).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});
self.addEventListener("fetch", (e) => {
  const req = e.request;
  const url = new URL(req.url);
  if (req.method !== "GET" || url.origin !== location.origin || url.pathname.startsWith("/api/")) return;

  if (url.pathname.startsWith("/static/") || url.pathname.startsWith("/img/")) {
    e.respondWith(caches.match(req).then((hit) => hit || fetch(req).then((r) => {
      if (r.ok && r.type === "basic") { const copy = r.clone(); caches.open(V).then((c) => c.put(req, copy)); }
      return r;
    })));
    return;
  }
  if (req.mode === "navigate") {
    e.respondWith(fetch(req).then((r) => {
      if (r.ok && r.type === "basic" && !r.redirected) { const copy = r.clone(); caches.open(V).then((c) => c.put(req, copy)); }
      return r;
    }).catch(() => caches.match(req).then((hit) => hit || caches.match("/"))));
  }
});

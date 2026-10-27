// Guarda la app para que abra rápido; los datos siempre se piden en vivo.
const C = "fabrica-v1";
const ARCHIVOS = ["./", "index.html", "manifest.json", "icono.svg", "icono-192.png", "icono-512.png"];
self.addEventListener("install", e => { e.waitUntil(caches.open(C).then(c => c.addAll(ARCHIVOS))); self.skipWaiting(); });
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== C).map(k => caches.delete(k)))));
  self.clients.claim();
});
self.addEventListener("fetch", e => {
  const u = new URL(e.request.url);
  if (u.origin !== location.origin || e.request.method !== "GET") return;
  e.respondWith(fetch(e.request).then(r => { const copia = r.clone(); caches.open(C).then(c => c.put(e.request, copia)); return r; })
    .catch(() => caches.match(e.request)));
});

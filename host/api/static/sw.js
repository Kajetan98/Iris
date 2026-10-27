// Service worker klienta IRIS: pliki aplikacji z pamięci podręcznej, żeby dało się ją zainstalować
// i otworzyć bez sieci (połączenie z hostem i tak wymaga sieci). API i /ws nie są buforowane.
const CACHE = "iris-app-v1";
const SHELL = [
  "./",
  "index.html",
  "app.js",
  "protocol.js",
  "mic-worklet.js",
  "style.css",
  "manifest.json",
  "icon.svg",
  "icon-192.png",
  "icon-512.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))),
  );
  self.clients.claim();
});

// Najpierw sieć (nowa wersja po aktualizacji hosta), pamięć podręczna bez sieci.
self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.origin !== location.origin || !url.pathname.startsWith("/app/")) {
    return;
  }
  event.respondWith(
    fetch(event.request)
      .then((response) => {
        const copy = response.clone();
        caches.open(CACHE).then((cache) => cache.put(event.request, copy));
        return response;
      })
      .catch(() => caches.match(event.request)),
  );
});

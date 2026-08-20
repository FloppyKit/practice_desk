/* Practice desk shell cache. Never caches API or note bodies. */
var CACHE = "psycharts-desk-v14";
var SHELL = [
  "/desk",
  "/static/desk.webmanifest",
  "/static/css/booker.css",
  "/static/css/client-tile.css",
  "/static/js/theme.js",
  "/static/js/desk-auth.js?v=1",
  "/static/js/desk-vault.js",
  "/static/js/desk-books.js",
  "/static/js/desk-services.js",
  "/static/js/desk-phrases.js",
  "/static/js/proton-drive.js",
  "/static/js/pa-router.js",
  "/static/js/staff-agent.js",
  "/static/js/desk-call.js",
  "/static/logo.png",
];

self.addEventListener("install", function (e) {
  e.waitUntil(
    caches
      .open(CACHE)
      .then(function (c) {
        return c.addAll(SHELL);
      })
      .then(function () {
        return self.skipWaiting();
      })
  );
});

self.addEventListener("activate", function (e) {
  e.waitUntil(
    caches
      .keys()
      .then(function (keys) {
        return Promise.all(
          keys.filter(function (k) {
            return k !== CACHE;
          }).map(function (k) {
            return caches.delete(k);
          })
        );
      })
      .then(function () {
        return self.clients.claim();
      })
  );
});

self.addEventListener("fetch", function (e) {
  var req = e.request;
  if (req.method !== "GET") return;
  var url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.indexOf("/api/") === 0) return;

  if (url.pathname.indexOf("/static/") === 0) {
    e.respondWith(
      caches.match(req).then(function (hit) {
        return (
          hit ||
          fetch(req).then(function (res) {
            if (res && res.ok) {
              var copy = res.clone();
              caches.open(CACHE).then(function (c) {
                c.put(req, copy);
              });
            }
            return res;
          })
        );
      })
    );
    return;
  }

  if (url.pathname === "/desk" || url.pathname === "/desk/") {
    e.respondWith(
      fetch(req)
        .then(function (res) {
          if (res && res.ok) {
            var copy = res.clone();
            caches.open(CACHE).then(function (c) {
              c.put(req, copy);
            });
          }
          return res;
        })
        .catch(function () {
          return caches.match("/desk");
        })
    );
  }
});

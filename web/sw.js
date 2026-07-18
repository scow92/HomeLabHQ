// HomelabHQ service worker: PWA install, static-shell caching + web-push
// handling.
const SHELL_CACHE = "hlhq-shell-v1";

self.addEventListener("install", (e) => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil((async () => {
  const keys = await caches.keys();
  await Promise.all(keys.filter((k) => k !== SHELL_CACHE).map((k) => caches.delete(k)));
  await self.clients.claim();
})()));

// Static app shell (index.html, css, js modules, icons, manifest) is cached
// stale-while-revalidate: an installed PWA renders instantly from cache —
// even offline — while a background fetch refreshes the cache for next
// time. /api/* is deliberately excluded and always goes straight to the
// network; caching live device/session data here would be actively wrong.
function isShellRequest(url) {
  return url.origin === self.location.origin &&
    !url.pathname.startsWith("/api/") && url.pathname !== "/sw.js";
}

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (!isShellRequest(url)) return;

  e.respondWith((async () => {
    const cache = await caches.open(SHELL_CACHE);
    const cached = await cache.match(req);
    const network = fetch(req).then((res) => {
      if (res && res.ok) cache.put(req, res.clone());
      return res;
    }).catch(() => null);
    if (cached) { network; return cached; }  // serve stale, refresh in background
    return (await network) || Response.error();
  })());
});

// Push: show the notification the poller sent.
self.addEventListener("push", (e) => {
  let d = { title: "HomelabHQ", body: "" };
  try { d = e.data.json(); } catch (_) { if (e.data) d.body = e.data.text(); }
  e.waitUntil(self.registration.showNotification(d.title || "HomelabHQ", {
    body: d.body || "",
    icon: "/icon-192.png",
    badge: "/icon-192.png",
    data: d.data || {},
    tag: (d.data && d.data.deviceId) || undefined,
  }));
});

// Focus/open the app when a notification is clicked.
self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  e.waitUntil((async () => {
    const all = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const c of all) { if ("focus" in c) return c.focus(); }
    if (self.clients.openWindow) return self.clients.openWindow("/");
  })());
});

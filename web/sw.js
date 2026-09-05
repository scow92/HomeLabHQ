// HomelabHQ service worker: PWA install, static-shell caching + web-push
// handling.
const SHELL_CACHE = "hlhq-shell-v10";

self.addEventListener("install", (e) => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil((async () => {
  const keys = await caches.keys();
  await Promise.all(keys.filter((k) => k !== SHELL_CACHE).map((k) => caches.delete(k)));
  await self.clients.claim();
})()));

// Static app shell (index.html, css, js modules, icons, manifest) is cached
// network-first: an online reload must use the currently deployed frontend,
// while the last successful response remains available as an offline fallback.
// /api/* is deliberately excluded and always goes straight to the network;
// caching live device/session data here would be actively wrong.
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
    try {
      const response = await fetch(req);
      if (response && response.ok) await cache.put(req, response.clone());
      return response;
    } catch (_) {
      return (await cache.match(req)) || Response.error();
    }
  })());
});

async function setAuthoritativeBadge(fallbackCount) {
  let count = Number(fallbackCount);
  try {
    const response = await fetch("/api/notifications?limit=1", {
      credentials: "same-origin", cache: "no-store",
    });
    if (response.ok) count = Number((await response.json()).unreadCount);
  } catch (_) {}
  if (!Number.isFinite(count) || count < 0) return;
  try {
    if (count > 0) await self.navigator.setAppBadge?.(count);
    else await self.navigator.clearAppBadge?.();
  } catch (_) {}
  const clients = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
  clients.forEach((client) => client.postMessage({
    type: "homelabhq-notification", unreadCount: count,
  }));
}

// Push: show the already-persisted notification and reconcile its backend count.
self.addEventListener("push", (e) => {
  let d = { title: "HomelabHQ", body: "" };
  try { d = e.data.json(); } catch (_) { if (e.data) d.body = e.data.text(); }
  e.waitUntil(Promise.all([
    self.registration.showNotification(d.title || "HomelabHQ", {
      body: d.body || "",
      icon: "/icon-192.png",
      badge: "/icon-192.png",
      data: d.data || {},
      tag: (d.data && (d.data.tag || d.data.deviceId)) || undefined,
    }),
    setAuthoritativeBadge(d.data?.unreadCount),
  ]));
});

// Focus/open the app when a notification is clicked.
self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  e.waitUntil((async () => {
    const target = new URL((e.notification.data && e.notification.data.url) || "/",
      self.location.origin).href;
    const all = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const c of all) {
      if ("navigate" in c) await c.navigate(target);
      if ("focus" in c) return c.focus();
    }
    if (self.clients.openWindow) return self.clients.openWindow(target);
  })());
});

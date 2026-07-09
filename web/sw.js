// NetManager service worker: PWA install + web-push handling.
self.addEventListener("install", (e) => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));

// Network-first pass-through; no aggressive caching so shell updates land.
self.addEventListener("fetch", (e) => {});

// Push: show the notification the poller sent.
self.addEventListener("push", (e) => {
  let d = { title: "NetManager", body: "" };
  try { d = e.data.json(); } catch (_) { if (e.data) d.body = e.data.text(); }
  e.waitUntil(self.registration.showNotification(d.title || "NetManager", {
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

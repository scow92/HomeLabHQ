// Minimal service worker so NetManager is installable as a PWA.
// Milestone 1 keeps it network-first with no aggressive caching, so shell
// updates always land; web-push handlers arrive with the poller (Milestone 4).
self.addEventListener("install", (e) => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", (e) => {
  // Pass through; let the network serve everything for now.
});

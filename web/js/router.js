// Tab switching + hash routing. Feature modules never import this file (or
// each other) for navigation — they dispatch "hlhq:navigate" /
// "hlhq:open-device" CustomEvents, which this module listens for. That keeps
// the import graph a DAG: router.js is the only module that reaches into
// devices.js, detail.js, clients.js, wizard.js, users.js, logs.js and
// settings.js for cross-tab orchestration.
"use strict";
import { $$, SESSION, onSessionChange } from "./api.js";
import { requestOwner } from "./request-owner.js";
import { loadDevices, loadDriverNames, ALL_DEVICES } from "./devices.js";
import { openDevice, closeDevice } from "./detail/index.js";
import { loadClients, startAccessBadge } from "./clients/index.js";
import { initWizard } from "./wizard.js";
import { loadUsers } from "./users.js";
import { loadLogs, stopLogsTimer } from "./logs.js";
import { loadNacConfig } from "./settings.js";
import { loadCompute, openCompute, closeCompute } from "./compute.js";

// Tabs carry their own URL (#/devices, #/access, …) and the device detail
// modal carries #/device/<id>, so the browser/Android back gesture closes a
// modal or returns to the previous tab instead of exiting the installed PWA —
// and a tab or a specific device is linkable / survives a refresh.
const TAB_HASH = { clients: "access" };
const HASH_TAB = { access: "clients" };

function tabFromHash() {
  const h = location.hash.replace(/^#\/?/, "");
  if (h.startsWith("device/")) return { tab: "devices", deviceId: decodeURIComponent(h.slice(7)) };
  if (h.startsWith("compute/")) return { tab: "compute", computeId: decodeURIComponent(h.slice(8)) };
  const seg = h.split("/")[0];
  const known = new Set(["devices", "compute", "clients", "add", "users", "logs", "settings"]);
  const tab = HASH_TAB[seg] || (known.has(seg) ? seg : "devices");
  return { tab };
}

const routeRequests = requestOwner();
let activatedHash = null;
onSessionChange(() => { routeRequests.invalidate(); activatedHash = null; });

export function switchTab(name, opts = {}) {
  if (!SESSION) return;
  if (!opts.fromHash) {
    routeRequests.invalidate();
    closeDevice({ fromRoute: true }); closeCompute({ fromRoute: true });
  }
  $$(".tab").forEach((t) => {
    const active = t.dataset.tab === name;
    t.classList.toggle("active", active);
    t.setAttribute("aria-selected", String(active));
    t.tabIndex = active ? 0 : -1;
  });
  $$("[data-panel]").forEach((p) => { p.hidden = p.dataset.panel !== name; });
  if (name !== "logs") stopLogsTimer();
  if (name === "devices" && !opts.detail) loadDevices();
  if (name === "compute" && !opts.detail) loadCompute();
  if (name === "clients") loadClients();
  if (name === "users") loadUsers();
  if (name === "logs") loadLogs();
  if (name === "add") initWizard();
  if (name === "settings") loadNacConfig();
  if (!opts.fromHash) {
    const target = "#/" + (TAB_HASH[name] || name);
    if (location.hash !== target) history.pushState(null, "", target);
    activatedHash = location.hash;
  }
}

async function routeFromHash({ force = false, resource = null } = {}) {
  if (!SESSION) return;
  const hash = location.hash;
  // A history traversal emits both popstate and hashchange. Activate once.
  if (!force && activatedHash === hash) return;
  activatedHash = hash;
  const request = routeRequests.begin(() => location.hash === hash);
  const { tab, deviceId, computeId } = tabFromHash();
  // Invalidate the old presentation before awaiting an inventory lookup.
  closeDevice({ fromRoute: true }); closeCompute({ fromRoute: true });
  switchTab(tab, { fromHash: true, detail: !!(deviceId || computeId) });
  if (deviceId) {
    if (!resource) await loadDevices(request);
    if (!request.current()) return;
    const device = resource || ALL_DEVICES.find(item => item.id === deviceId);
    if (device) return openDevice(device);
    history.replaceState(null, "", "#/devices");
  }
  if (computeId) {
    const instances = resource ? [resource] : await loadCompute(request);
    if (!request.current()) return;
    const instance = instances.find(item => item.id === computeId);
    if (instance) return openCompute(instance);
    history.replaceState(null, "", "#/compute");
  }
}

export function initialRoute() {
  loadDriverNames();
  startAccessBadge();
  return routeFromHash({ force: true });
}

function openResource(kind, resource) {
  const hash = `#/${kind}/${encodeURIComponent(resource.id)}`;
  if (location.hash !== hash) history.pushState({ detailReturn: true }, "", hash);
  return routeFromHash({ force: true, resource });
}

window.addEventListener("popstate", routeFromHash);
window.addEventListener("hashchange", routeFromHash);

// Feature modules dispatch these instead of importing switchTab/openDevice
// directly, which would otherwise recreate the app.js<->clients.js and
// devices.js<->detail.js import cycles this file exists to remove.
document.addEventListener("hlhq:navigate", (e) => switchTab(e.detail.tab));
document.addEventListener("hlhq:open-device", (e) => openResource("device", e.detail));
document.addEventListener("hlhq:open-compute", (e) => openResource("compute", e.detail));
document.addEventListener("hlhq:view-compute", (e) => {
  switchTab("compute");
  document.dispatchEvent(new CustomEvent("hlhq:compute-parent", { detail: e.detail }));
});

// Tab switching + hash routing. Feature modules never import this file (or
// each other) for navigation — they dispatch "hlhq:navigate" /
// "hlhq:open-device" CustomEvents, which this module listens for. That keeps
// the import graph a DAG: router.js is the only module that reaches into
// devices.js, detail.js, clients.js, wizard.js, users.js, logs.js and
// settings.js for cross-tab orchestration (see refactor.md 2.3).
"use strict";
import { $, $$, SESSION } from "./api.js";
import { loadDevices, loadDriverNames, ALL_DEVICES } from "./devices.js";
import { openDevice, closeDevice } from "./detail/index.js";
import { loadClients, startAccessBadge } from "./clients/index.js";
import { initWizard } from "./wizard.js";
import { loadUsers } from "./users.js";
import { loadLogs, stopLogsTimer } from "./logs.js";
import { loadNacConfig } from "./settings.js";

// Tabs carry their own URL (#/devices, #/access, …) and the device detail
// modal carries #/device/<id>, so the browser/Android back gesture closes a
// modal or returns to the previous tab instead of exiting the installed PWA —
// and a tab or a specific device is linkable / survives a refresh.
const TAB_HASH = { clients: "access" };
const HASH_TAB = { access: "clients" };

function tabFromHash() {
  const h = location.hash.replace(/^#\/?/, "");
  if (h.startsWith("device/")) return { tab: "devices", deviceId: decodeURIComponent(h.slice(7)) };
  const seg = h.split("/")[0];
  const known = new Set(["devices", "clients", "add", "users", "logs", "settings"]);
  const tab = HASH_TAB[seg] || (known.has(seg) ? seg : "devices");
  return { tab };
}

async function openDeviceById(id) {
  await loadDevices();
  const d = ALL_DEVICES.find((x) => x.id === id);
  if (d) openDevice(d);
  else history.replaceState(null, "", "#/devices");
}

export function switchTab(name, opts = {}) {
  $$(".tab").forEach((t) => {
    const active = t.dataset.tab === name;
    t.classList.toggle("active", active);
    t.setAttribute("aria-selected", String(active));
    t.tabIndex = active ? 0 : -1;
  });
  $$("[data-panel]").forEach((p) => { p.hidden = p.dataset.panel !== name; });
  if (name !== "logs") stopLogsTimer();
  if (name === "devices") loadDevices();
  if (name === "clients") loadClients();
  if (name === "users") loadUsers();
  if (name === "logs") loadLogs();
  if (name === "add") initWizard();
  if (name === "settings") loadNacConfig();
  if (!opts.fromHash) {
    const target = "#/" + (TAB_HASH[name] || name);
    if (location.hash !== target) history.pushState(null, "", target);
  }
}

function routeFromHash() {
  if (!SESSION) return;  // initialRoute() routes once login completes
  const { tab, deviceId } = tabFromHash();
  const modal = $("#device-modal");
  if (!deviceId && modal && !modal.hidden) {
    // Back-navigated out of a device deep link — close the modal in place
    // rather than leaving the app on whatever tab it lands on underneath.
    closeDevice();
  }
  switchTab(tab, { fromHash: true });
  if (deviceId) openDeviceById(deviceId);
}

// Called once, right after login, to route to whatever tab/device the URL
// (or a fresh boot) points at.
export function initialRoute() {
  loadDriverNames();
  startAccessBadge();
  const { tab, deviceId } = tabFromHash();
  switchTab(tab, { fromHash: true });
  if (deviceId) openDeviceById(deviceId);
}

window.addEventListener("popstate", routeFromHash);
window.addEventListener("hashchange", routeFromHash);

// Feature modules dispatch these instead of importing switchTab/openDevice
// directly, which would otherwise recreate the app.js<->clients.js and
// devices.js<->detail.js import cycles this file exists to remove.
document.addEventListener("hlhq:navigate", (e) => switchTab(e.detail.tab));
document.addEventListener("hlhq:open-device", (e) => openDevice(e.detail));

// Tab switching + hash routing. Feature modules never import this file (or
// each other) for navigation — they dispatch "hlhq:navigate" /
// "hlhq:open-device" CustomEvents, which this module listens for. That keeps
// the import graph a DAG: router.js is the only module that reaches into
// devices.js, detail.js, clients.js, wizard.js, users.js, logs.js and
// settings.js for cross-tab orchestration.
"use strict";
import { $, $$, SESSION, onSessionChange } from "./api.js";
import { requestOwner } from "./request-owner.js";
import { loadDevices, activateDevices, stopDevices, loadDriverNames, ALL_DEVICES,
         applyDeviceRouteContext, deviceRouteParams } from "./devices.js";
import { openDevice, closeDevice } from "./detail/index.js";
import { loadClients, stopClients, startAccessBadge } from "./clients/index.js";
import { initWizard } from "./wizard.js";
import { loadUsers } from "./users.js";
import { activateLogs, stopLogsTimer } from "./logs.js";
import { loadNacConfig, stopSettingsReads } from "./settings.js";
import { loadCompute, stopComputeReads, openCompute, closeCompute,
         applyComputeRouteContext, computeRouteParams } from "./compute.js";

// Tabs carry their own URL (#/devices, #/access, …) and the device detail
// modal carries #/device/<id>, so the browser/Android back gesture closes a
// modal or returns to the previous tab instead of exiting the installed PWA —
// and a tab or a specific device is linkable / survives a refresh.
const ROUTES = {
  devices: { tab: "devices", path: "devices", title: "Devices" },
  compute: { tab: "compute", path: "compute", title: "Compute" },
  access: { tab: "clients", path: "access", title: "Network Access" },
  add: { tab: "add", path: "add", title: "Add device" },
  users: { tab: "users", path: "users", title: "Users", admin: true },
  logs: { tab: "logs", path: "logs", title: "Logs", admin: true },
  settings: { tab: "settings", path: "settings", title: "Settings" },
};
const ROUTE_BY_TAB = Object.fromEntries(Object.values(ROUTES).map(route => [route.tab, route]));

function safeDecode(value) {
  try { return { value: decodeURIComponent(value) }; }
  catch (_) { return { error: "invalid" }; }
}

export function parseRoute(hash, role = SESSION?.role) {
  const raw = String(hash || "").replace(/^#\/?/, "");
  const queryAt = raw.indexOf("?");
  const rawPath = queryAt < 0 ? raw : raw.slice(0, queryAt);
  const params = new URLSearchParams(queryAt < 0 ? "" : raw.slice(queryAt + 1));
  const segments = (rawPath || "devices").split("/");
  let route;
  if ((segments[0] === "device" || segments[0] === "compute") && segments.length === 2 && segments[1]) {
    const decoded = safeDecode(segments[1]);
    if (decoded.error || decoded.value.length > 256 || /[\u0000-\u001f\u007f]/.test(decoded.value)) {
      return { kind: "invalid" };
    }
    route = { kind: "resource", tab: segments[0] === "device" ? "devices" : "compute",
      resourceKind: segments[0], resourceId: decoded.value, params };
  } else if (segments.length === 1) {
    const key = segments[0] === "clients" ? "access" : segments[0];
    const entry = ROUTES[key];
    if (entry) route = { kind: "panel", tab: entry.tab, params,
      normalize: segments[0] === "clients" };
  }
  if (!route) return { kind: "notfound" };
  const entry = ROUTE_BY_TAB[route.tab];
  if (entry.admin && role !== "admin") return { kind: "forbidden", tab: route.tab };
  return route;
}

function routeHash(tab, params = new URLSearchParams()) {
  const route = ROUTE_BY_TAB[tab];
  const query = params.toString();
  return `#/${route.path}${query ? `?${query}` : ""}`;
}

function currentParams(tab) {
  if (tab === "devices") return deviceRouteParams();
  if (tab === "compute") return computeRouteParams();
  return new URLSearchParams();
}

const routeRequests = requestOwner();
let activatedHash = null;
onSessionChange(() => { routeRequests.invalidate(); activatedHash = null; });

function stopPresentations() {
  stopDevices(); stopLogsTimer(); stopClients(); stopComputeReads(); stopSettingsReads();
}

function focusHeading(container) {
  const heading = $("h1", container);
  if (!heading) return;
  heading.tabIndex = -1;
  requestAnimationFrame(() => heading.focus({ preventScroll: false }));
}

function showRouteFeedback(kind) {
  routeRequests.invalidate();
  closeDevice({ fromRoute: true }); closeCompute({ fromRoute: true });
  stopPresentations();
  const tabs = $$(".tab"), firstVisible = tabs.find(tab => !tab.hidden);
  tabs.forEach(tab => {
    tab.classList.remove("active"); tab.setAttribute("aria-selected", "false");
    tab.tabIndex = tab === firstVisible ? 0 : -1;
  });
  $$("[data-panel]").forEach(panel => { panel.hidden = true; });
  const feedback = $("#route-feedback"); feedback.hidden = false;
  $(".route-feedback-message", feedback).textContent = kind === "forbidden"
    ? "This page is not available for your account."
    : kind === "invalid" ? "This is not a valid link. Check the address or return to Devices."
      : "The requested page could not be found.";
  document.title = "Page unavailable · HomelabHQ";
  focusHeading(feedback);
}

export function switchTab(name, opts = {}) {
  if (!SESSION) return;
  const route = ROUTE_BY_TAB[name];
  if (!route || (route.admin && SESSION.role !== "admin")) {
    if (!opts.fromHash && route) {
      history.pushState(null, "", routeHash(name)); activatedHash = location.hash;
    }
    showRouteFeedback(route ? "forbidden" : "notfound"); return;
  }
  if (!opts.fromHash) {
    routeRequests.invalidate();
    closeDevice({ fromRoute: true }); closeCompute({ fromRoute: true });
  }
  $("#route-feedback").hidden = true;
  $$(".tab").forEach((t) => {
    const active = t.dataset.tab === name;
    t.classList.toggle("active", active);
    t.setAttribute("aria-selected", String(active));
    t.tabIndex = active ? 0 : -1;
  });
  $$("[data-panel]").forEach((p) => { p.hidden = p.dataset.panel !== name; });
  stopPresentations();
  if (name === "devices" && !opts.detail) activateDevices();
  if (name === "compute" && !opts.detail) loadCompute();
  if (name === "clients") loadClients();
  if (name === "users") loadUsers();
  if (name === "logs") activateLogs();
  if (name === "add") initWizard();
  if (name === "settings") loadNacConfig();
  document.title = `${route.title} · HomelabHQ`;
  if (!opts.fromHash) {
    const target = routeHash(name, currentParams(name));
    if (location.hash !== target) history[opts.replace ? "replaceState" : "pushState"](null, "", target);
    activatedHash = location.hash;
  }
}

async function routeFromHash({ force = false, resource = null } = {}) {
  if (!SESSION) return;
  let hash = location.hash;
  // A history traversal emits both popstate and hashchange. Activate once.
  if (!force && activatedHash === hash) return;
  const parsed = parseRoute(hash);
  if (parsed.normalize) {
    history.replaceState(null, "", routeHash(parsed.tab, parsed.params));
    hash = location.hash;
  }
  activatedHash = hash;
  if (["forbidden", "invalid", "notfound"].includes(parsed.kind)) {
    showRouteFeedback(parsed.kind); return;
  }
  const request = routeRequests.begin(() => location.hash === hash);
  const { tab, params, resourceKind, resourceId } = parsed;
  if (tab === "devices" && (parsed.kind === "panel" || params.toString())) applyDeviceRouteContext(params);
  if (tab === "compute" && (parsed.kind === "panel" || params.toString())) applyComputeRouteContext(params);
  // Invalidate the old presentation before awaiting an inventory lookup.
  closeDevice({ fromRoute: true }); closeCompute({ fromRoute: true });
  switchTab(tab, { fromHash: true, detail: parsed.kind === "resource" });
  if (parsed.kind === "panel") focusHeading($(`[data-panel="${tab}"]`));
  if (resourceKind === "device") {
    if (!resource) await loadDevices(request);
    if (!request.current()) return;
    const device = resource || ALL_DEVICES.find(item => item.id === resourceId);
    if (device) { document.title = `${device.name || "Device"} · HomelabHQ`; return openDevice(device); }
    showRouteFeedback("notfound"); return;
  }
  if (resourceKind === "compute") {
    const instances = resource ? [resource] : await loadCompute(request);
    if (!request.current()) return;
    const instance = instances.find(item => item.id === resourceId);
    if (instance) { document.title = `${instance.name || "Compute"} · HomelabHQ`; return openCompute(instance); }
    showRouteFeedback("notfound");
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
document.addEventListener("hlhq:navigate", (e) => switchTab(e.detail.tab, { replace: !!e.detail.replace }));
document.addEventListener("hlhq:route-context", (event) => {
  if (!SESSION || !ROUTE_BY_TAB[event.detail.tab]) return;
  const target = routeHash(event.detail.tab, event.detail.params);
  if (location.hash === target) return;
  history[event.detail.replace ? "replaceState" : "pushState"](null, "", target);
  activatedHash = location.hash;
});
document.addEventListener("hlhq:open-device", (e) => openResource("device", e.detail));
document.addEventListener("hlhq:open-compute", (e) => openResource("compute", e.detail));
document.addEventListener("hlhq:view-compute", (e) => {
  switchTab("compute");
  document.dispatchEvent(new CustomEvent("hlhq:compute-parent", { detail: e.detail }));
});

// Devices tab: dashboard tabs, the device card grid, search, drag-to-reorder /
// drag-to-move, and the compact per-card live state rendering.
"use strict";
import { $, $$, api, timeAgo, fmtBytes, fmtNum, fmtUptime, effectiveOnline, labelFor } from "./api.js";
import { toastErr, toastOk, promptDialog, confirmDialog, pickDialog,
         ICON_INFO, ICON_SYNC, ICON_EDIT, ICON_TRASH, ICON_UP, ICON_DOWN,
         visiblePoll, reconcileList } from "./ui.js";

// Opening the detail modal is handled by router.js (which imports detail.js);
// dispatching an event here instead of importing openDevice directly avoids
// a devices.js <-> detail.js import cycle (refactor.md 2.3).
function openDevice(d) {
  document.dispatchEvent(new CustomEvent("hlhq:open-device", { detail: d }));
}

export let DASHBOARDS = [];             // [{id,name,order,...}]
export let ALL_DEVICES = [];            // last-loaded device list (unfiltered)
export let currentDashboard = "all";    // "all" | "unassigned" | <dashboardId>
let DRAG_ID = null;             // device id currently being dragged

// Driver ids → short, human names for the overview cards (the wire id like
// "keeplink.switch" reads poorly). Fetched once from the server (which already
// owns display_name per driver) so a new driver never needs a matching
// frontend edit; unknown/unfetched ids fall back to a humanized id. Called
// post-login (every /api/* route needs a session) rather than at module load.
let DRIVER_NAMES = {};
export async function loadDriverNames() {
  try {
    const { drivers } = await api("/api/drivers");
    const map = {};
    for (const d of drivers || []) map[d.id] = d.displayName;
    DRIVER_NAMES = map;
  } catch (_) { /* fall back to humanized ids until the next load */ }
}
export function driverName(id) {
  if (DRIVER_NAMES[id]) return DRIVER_NAMES[id];
  return (id || "").split(/[.\-_]/).map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ") || "device";
}

// The recurring 15s refresh only starts once the Devices tab has actually been
// loaded at least once (i.e. after login — switchTab("devices") is what calls
// loadDevices() first). Starting visiblePoll eagerly at module-import time
// would fire an authenticated-only request while the auth screen is still up.
const DEVICES_POLL_MS = 15000;
let devPollStop = null;
function ensureDevPoll() {
  if (!devPollStop) devPollStop = visiblePoll("devices", () => { if (!DRAG_ID) loadDevices(); }, DEVICES_POLL_MS);
}

export async function loadDevices() {
  try {
    const [dRes, devRes] = await Promise.all([
      api("/api/dashboards"), api("/api/devices"),
    ]);
    DASHBOARDS = dRes.dashboards || [];
    ALL_DEVICES = devRes.devices || [];
  } catch (ex) {
    // Don't wipe a good view on a transient refresh error — just surface it
    // (mirrors loadClients()). Only show the empty/error state on the very
    // first load, when there's nothing on screen yet.
    if (ALL_DEVICES.length) toastErr("Couldn't refresh devices: " + ex.message);
    else {
      $("#devices-list").innerHTML = "";
      const empty = $("#devices-empty");
      empty.hidden = false;
      $(".de-msg", empty).textContent = "Couldn't load devices.";
      $(".de-sub", empty).textContent = ex.message;
    }
    ensureDevPoll();
    return;
  }
  // If the selected dashboard vanished (deleted elsewhere), fall back to All.
  if (currentDashboard !== "all" && currentDashboard !== "unassigned" &&
      !DASHBOARDS.some((d) => d.id === currentDashboard)) {
    currentDashboard = "all";
  }
  renderDashTabs();
  renderDeviceList();
  ensureDevPoll();
}

export function devicesIn(id) {
  if (id === "all") return ALL_DEVICES;
  if (id === "unassigned") return ALL_DEVICES.filter((d) => !d.dashboardId);
  return ALL_DEVICES.filter((d) => d.dashboardId === id);
}

function renderDashTabs() {
  const bar = $("#dashboard-tabs");
  bar.innerHTML = "";
  const tabs = [{ id: "all", name: "All" }];
  if (devicesIn("unassigned").length) tabs.push({ id: "unassigned", name: "Unassigned" });
  for (const d of DASHBOARDS) tabs.push({ id: d.id, name: d.name });
  for (const t of tabs) {
    const el = document.createElement("button");
    el.className = "dash-tab" + (t.id === currentDashboard ? " active" : "");
    el.innerHTML = `<span class="nm"></span><span class="count"></span>`;
    $(".nm", el).textContent = t.name;
    $(".count", el).textContent = devicesIn(t.id).length;
    el.onclick = () => { currentDashboard = t.id; renderDashTabs(); renderDeviceList(); };
    // Drop a dragged device onto a tab to move it there ("All" is a no-op view).
    if (t.id !== "all") {
      el.addEventListener("dragover", (e) => {
        if (!DRAG_ID) return;
        e.preventDefault();
        el.classList.add("drop-target");
      });
      el.addEventListener("dragleave", () => el.classList.remove("drop-target"));
      el.addEventListener("drop", (e) => {
        el.classList.remove("drop-target");
        if (!DRAG_ID) return;
        e.preventDefault();
        moveDeviceToDashboard(DRAG_ID, t.id === "unassigned" ? null : t.id);
      });
    }
    bar.appendChild(el);
  }
}

let SEARCH_Q = "";  // device search filter (name / host / driver)

function matchesSearch(d) {
  if (!SEARCH_Q) return true;
  const hay = `${d.name || ""} ${d.host} ${d.driverId} ${driverName(d.driverId)} ${d.transport}`.toLowerCase();
  return SEARCH_Q.split(/\s+/).every((term) => hay.includes(term));
}

const DEV_CARDS = new Map();  // device id -> {el, patch} — reconciled in place

function renderDeviceList() {
  const list = $("#devices-list");
  const empty = $("#devices-empty");
  const inDash = devicesIn(currentDashboard);
  const devs = inDash.filter(matchesSearch);
  // Summary strip for the current dashboard tab — mirrors the Access tab's.
  const summary = $("#devices-summary");
  const polled = inDash.filter((d) => d.state);
  const online = polled.filter((d) => effectiveOnline(d.state)).length;
  const offline = polled.length - online;
  summary.hidden = !inDash.length;
  if (inDash.length) {
    summary.textContent = `${inDash.length} device${inDash.length === 1 ? "" : "s"} · ${online} online` +
      (offline ? ` · ${offline} offline` : "");
  }
  // Patch existing cards in place and only add/remove what actually changed,
  // so a background refresh can't yank a card out from under an in-progress
  // tap or drag (§4.2).
  reconcileList(list, DEV_CARDS, devs, (d) => d.id, buildDeviceCard,
    (entry, d) => entry.patch(d, { first: d === devs[0], last: d === devs[devs.length - 1] }));
  // Show the search box once there's a meaningful number of devices to sift.
  $("#dev-search").hidden = ALL_DEVICES.length < 5;
  empty.hidden = devs.length > 0;
  if (!devs.length) {
    const none = ALL_DEVICES.length === 0;
    const filtered = inDash.length > 0 && SEARCH_Q;
    $(".de-msg", empty).textContent = filtered ? "No matching devices."
      : none ? "No devices yet." : "No devices in this dashboard.";
    $(".de-sub", empty).textContent = filtered ? `Nothing matches “${SEARCH_Q}”.`
      : none ? "Add a router, switch, AP or firewall to start monitoring it."
      : "Add one here, or use “Move to…” on a device card to bring it in.";
  }
}
export { renderDeviceList };

(function bindDeviceSearch() {
  const input = $("#dev-search-input");
  const clear = $("#dev-search-clear");
  if (!input) return;
  input.addEventListener("input", () => {
    SEARCH_Q = input.value.trim().toLowerCase();
    clear.hidden = !input.value;
    renderDeviceList();
  });
  clear.addEventListener("click", () => {
    input.value = ""; SEARCH_Q = ""; clear.hidden = true;
    renderDeviceList(); input.focus();
  });
})();

// ---- drag to reorder (within the list) / move (onto a dashboard tab) ----
// Grid-aware: pick the card whose centre is nearest the pointer, insert
// before it when the pointer is above-or-left of that centre, else after.
function dragAfterElement(container, x, y) {
  const cards = [...container.querySelectorAll(".card:not(.dragging)")];
  let best = { dist: Infinity, el: null, before: true };
  for (const el of cards) {
    const b = el.getBoundingClientRect();
    const cx = b.left + b.width / 2, cy = b.top + b.height / 2;
    const dist = Math.hypot(x - cx, y - cy);
    if (dist < best.dist) {
      const before = y < cy - 1 || (Math.abs(y - cy) <= b.height / 2 && x < cx);
      best = { dist, el, before };
    }
  }
  if (!best.el) return null;
  return best.before ? best.el : best.el.nextElementSibling;
}

(function bindListDnD() {
  const list = $("#devices-list");
  list.addEventListener("dragover", (e) => {
    if (!DRAG_ID) return;
    e.preventDefault();
    const dragging = $(".card.dragging", list);
    if (!dragging) return;
    const after = dragAfterElement(list, e.clientX, e.clientY);
    if (after == null) list.appendChild(dragging);
    else if (after !== dragging) list.insertBefore(dragging, after);
  });
  list.addEventListener("drop", (e) => {
    if (!DRAG_ID) return;
    e.preventDefault();
    persistOrder();
  });
})();

async function persistOrder() {
  const ids = [...$("#devices-list").querySelectorAll(".card")]
    .map((c) => c.dataset.deviceId).filter(Boolean);
  ids.forEach((id, i) => { const d = ALL_DEVICES.find((x) => x.id === id); if (d) d.order = i; });
  try {
    await api("/api/devices/reorder", { method: "POST", body: JSON.stringify({ ids }) });
  } catch (_) { /* next auto-refresh will re-sync from the server */ }
}

// Move a card one slot up/down within the current view and persist — the
// touch-friendly alternative to drag-and-drop, which HTML5 DnD doesn't
// support on mobile (the primary platform this app targets).
export async function moveDeviceOrder(d, delta) {
  const list = $("#devices-list");
  const card = list.querySelector(`.card[data-device-id="${CSS.escape(d.id)}"]`);
  if (!card) return;
  const sib = delta < 0 ? card.previousElementSibling : card.nextElementSibling;
  if (!sib) return;
  if (delta < 0) list.insertBefore(card, sib);
  else list.insertBefore(sib, card);
  await persistOrder();
}

export async function moveDeviceToDashboard(devId, dashboardId) {
  try {
    await api(`/api/devices/${devId}`, {
      method: "PATCH", body: JSON.stringify({ dashboardId: dashboardId || null }) });
    await loadDevices();
  } catch (ex) { toastErr(ex.message); }
}

// Dashboard create / rename / delete, reached from the "⋯" menu so the tab row
// stays a clean horizontal scroll area (no action buttons to fat-finger).
async function dashCreate() {
  const name = (await promptDialog({ title: "New dashboard",
    placeholder: "e.g. Network, Proxmox", okLabel: "Create" }) || "").trim();
  if (!name) return;
  try {
    const { dashboard } = await api("/api/dashboards", {
      method: "POST", body: JSON.stringify({ name }) });
    currentDashboard = dashboard.id;
    await loadDevices();
    toastOk(`Dashboard “${name}” created.`);
  } catch (ex) { toastErr(ex.message); }
}
async function dashRename() {
  const cur = DASHBOARDS.find((d) => d.id === currentDashboard);
  if (!cur) return;
  const name = (await promptDialog({ title: "Rename dashboard",
    value: cur.name }) || "").trim();
  if (!name || name === cur.name) return;
  try {
    await api(`/api/dashboards/${cur.id}`, { method: "PATCH", body: JSON.stringify({ name }) });
    await loadDevices();
    toastOk("Dashboard renamed.");
  } catch (ex) { toastErr(ex.message); }
}
async function dashDelete() {
  const cur = DASHBOARDS.find((d) => d.id === currentDashboard);
  if (!cur) return;
  const n = devicesIn(cur.id).length;
  const ok = await confirmDialog({ title: `Delete “${cur.name}”?`,
    message: n ? `Its ${n} device(s) will become Unassigned (not deleted).` : "",
    okLabel: "Delete", danger: true });
  if (!ok) return;
  try {
    await api(`/api/dashboards?id=${encodeURIComponent(cur.id)}`, { method: "DELETE" });
    currentDashboard = "all";
    await loadDevices();
    toastOk("Dashboard deleted.");
  } catch (ex) { toastErr(ex.message); }
}

$("#dash-menu").addEventListener("click", async () => {
  const isReal = currentDashboard !== "all" && currentDashboard !== "unassigned";
  const items = [{ value: "new", label: "New dashboard" }];
  if (isReal) items.push(
    { value: "rename", label: "Rename this dashboard" },
    { value: "delete", label: "Delete this dashboard" });
  const pick = await pickDialog({ title: "Dashboards", items });
  if (pick === "new") dashCreate();
  else if (pick === "rename") dashRename();
  else if (pick === "delete") dashDelete();
});

// Keys whose sensor value is a percentage (CPU busy, memory used, pool used …).
// The compact card carries no unit metadata, so we tag them by name to add "%".
const PCT_KEY = /^cpu(_usage|_load)?$|^mem$|_used$/;

// Sensors shown in the detail view but too noisy for the compact card: the raw
// mem_total byte count is redundant with ram_used's percentage.
const CARD_SKIP = new Set(["mem_total"]);

// Format a raw sensor value for the compact device card (the detail view has
// its own richer formatting). Keeps big byte counters and uptimes readable.
function fmtCardValue(key, v) {
  if (v == null || v === "") return "–";
  if (typeof v === "number") {
    if (/octet|_bytes$|^bytes/i.test(key)) return fmtBytes(v);
    if (key === "uptime") return fmtUptime(v);
    if (PCT_KEY.test(key)) return `${fmtNum(v)}%`;
    return fmtNum(v);
  }
  return String(v);
}

export function renderState(container, res) {
  container.hidden = false;
  container.innerHTML = "";
  const entries = Object.entries(res.values || {});
  if (!entries.length && !Object.keys(res.errors || {}).length) {
    container.innerHTML = `<span class="muted">no values</span>`;
  }
  for (const [k, v] of entries) {
    if (CARD_SKIP.has(k)) continue;
    const kEl = document.createElement("span"); kEl.className = "k";
    kEl.textContent = labelFor(k);
    const vEl = document.createElement("span"); vEl.textContent = fmtCardValue(k, v);
    container.append(kEl, vEl);
  }
  for (const [k, msg] of Object.entries(res.errors || {})) {
    const kEl = document.createElement("span"); kEl.className = "k"; kEl.textContent = labelFor(k);
    const vEl = document.createElement("span"); vEl.className = "dev-err"; vEl.textContent = msg;
    container.append(kEl, vEl);
  }
}

// Builds a device card once; returns {el, patch(d)}. `patch` updates the same
// DOM node in place on every subsequent render (§4.2) so a background refresh
// never yanks a card out from under an in-progress tap/drag, and CSS
// transitions / focus aren't lost. `cur` is the device the card's action
// buttons currently act on — patch() keeps it current via closure.
function buildDeviceCard(d) {
  let cur = d;
  const el = document.createElement("div");
  el.className = "card clickable";
  el.title = "Drag to reorder, or onto a dashboard tab to move · click for details";
  el.draggable = true;
  el.innerHTML = `
    <div class="card-row"><h2><span class="dot"></span><span class="sr-only status-text"></span><span class="dname"></span></h2><span class="pill"></span></div>
    <div class="muted host"></div>
    <div class="muted offline-since" hidden></div>
    <div class="dev-state" hidden></div>
    <div class="muted updated"></div>
    <div class="dev-actions">
      <button class="icon-btn details" title="Details" aria-label="Details">${ICON_INFO}</button>
      <button class="icon-btn check" title="Sync now" aria-label="Sync now">${ICON_SYNC}</button>
      <button class="icon-btn move-up" title="Move up" aria-label="Move up">${ICON_UP}</button>
      <button class="icon-btn move-down" title="Move down" aria-label="Move down">${ICON_DOWN}</button>
      <button class="icon-btn rename" title="Rename" aria-label="Rename">${ICON_EDIT}</button>
      <button class="icon-btn icon-btn-danger del" title="Remove" aria-label="Remove">${ICON_TRASH}</button>
    </div>`;
  // Clicking the card body (but not its action buttons) opens the detail view.
  el.addEventListener("click", (e) => {
    if (e.target.closest(".dev-actions")) return;
    openDevice(cur);
  });
  // Drag to reorder (within the list) or onto a dashboard tab (to move).
  el.addEventListener("dragstart", (e) => {
    DRAG_ID = cur.id;
    el.classList.add("dragging");
    e.dataTransfer.effectAllowed = "move";
    try { e.dataTransfer.setData("text/plain", cur.id); } catch (_) {}
  });
  el.addEventListener("dragend", () => {
    el.classList.remove("dragging");
    DRAG_ID = null;
    $$(".dash-tab.drop-target").forEach((t) => t.classList.remove("drop-target"));
  });
  const dname = $(".dname", el), pill = $(".pill", el), host = $(".host", el);
  const state = $(".dev-state", el);
  const dot = $(".dot", el);
  const statusText = $(".status-text", el);
  const offlineSince = $(".offline-since", el);
  const updated = $(".updated", el);
  updated.dataset.tsPrefix = "updated ";  // read by the 30s relative-time ticker

  const applyState = (s) => {
    if (!s) {
      dot.className = "dot unknown"; statusText.textContent = "Not polled yet";
      offlineSince.hidden = true;
      updated.textContent = "not polled yet"; updated.removeAttribute("data-ts");
      return;
    }
    const up = effectiveOnline(s);
    dot.className = "dot " + (up ? "up" : "down");
    statusText.textContent = up ? "Online" : "Offline";
    dot.title = s.miss ? `${s.miss} missed poll${s.miss === 1 ? "" : "s"} in a row` : "";
    // "offline for 3h" reads much better than a grey dot alone (refactor.md
    // 3.4) — `since` is the last confirmed online/offline transition.
    if (!up && s.since) {
      offlineSince.hidden = false;
      offlineSince.textContent = "Offline for " +
        fmtUptime(Math.max(0, Math.floor(Date.now() / 1000) - s.since));
    } else {
      offlineSince.hidden = true;
    }
    renderState(state, s);
    updated.textContent = "updated " + timeAgo(s.ts);
    if (s.ts) updated.dataset.ts = s.ts; else updated.removeAttribute("data-ts");
  };

  $(".check", el).onclick = async (e) => {
    const btn = e.currentTarget; btn.disabled = true; btn.classList.add("spinning");
    try {
      const r = await api(`/api/devices/${cur.id}/state`);
      // The /state call succeeding means the device is up — even if every
      // selected entity happened to error, or none are numeric sensors.
      dot.className = "dot up"; statusText.textContent = "Online";
      renderState(state, r);
      updated.textContent = "updated just now";
      updated.dataset.ts = String(Math.floor(Date.now() / 1000));
    } catch (ex) {
      dot.className = "dot down"; statusText.textContent = "Offline";
      state.hidden = false;
      state.innerHTML = "";
      const errEl = document.createElement("span");
      errEl.className = "dev-err"; errEl.textContent = ex.message;
      state.appendChild(errEl);
    } finally { btn.disabled = false; btn.classList.remove("spinning"); }
  };

  $(".details", el).onclick = () => openDevice(cur);

  // Touch-friendly reorder fallback — HTML5 drag-and-drop doesn't exist on
  // mobile, the primary platform this app targets.
  $(".move-up", el).onclick = (e) => { e.stopPropagation(); moveDeviceOrder(cur, -1); };
  $(".move-down", el).onclick = (e) => { e.stopPropagation(); moveDeviceOrder(cur, 1); };

  $(".rename", el).onclick = () => renameDevice(cur);

  $(".del", el).onclick = async () => {
    const ok = await confirmDialog({ title: `Remove “${cur.name || cur.host}”?`,
      message: "This stops monitoring it and deletes its stored history.",
      okLabel: "Remove", danger: true });
    if (!ok) return;
    try {
      await api(`/api/devices?id=${encodeURIComponent(cur.id)}`, { method: "DELETE" });
      loadDevices();
      toastOk("Device removed.");
    }
    catch (ex) { toastErr(ex.message); }
  };

  function patch(d, { first, last } = {}) {
    cur = d;
    el.dataset.deviceId = d.id;
    dname.textContent = d.name || d.host;
    pill.textContent = driverName(d.driverId);
    pill.title = d.driverId;
    host.textContent = `${d.host}${d.port ? ":" + d.port : ""}`;
    applyState(d.state);
    $(".move-up", el).disabled = !!first;
    $(".move-down", el).disabled = !!last;
  }
  // reconcileList() always calls patch() itself, right after building — no
  // need to prime it here too.
  return { el, patch };
}

// Rename a device (from a card or the detail modal). Empty clears the custom
// name so it falls back to the host. Refreshes the overview after saving.
export async function renameDevice(d, onRenamed) {
  const next = await promptDialog({ title: "Rename device",
    message: `Currently “${d.name || d.host}”. Leave blank to use the host.`,
    value: d.name || "", placeholder: d.host });
  if (next == null) return;
  try {
    const r = await api(`/api/devices/${d.id}`, {
      method: "PATCH", body: JSON.stringify({ name: next.trim() }) });
    d.name = (r && r.device ? r.device.name : next.trim()) || null;
    if (onRenamed) onRenamed(d);
    loadDevices();
    toastOk("Device renamed.");
  } catch (ex) { toastErr(ex.message); }
}

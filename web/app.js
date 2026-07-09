// HomelabHQ SPA shell. Milestone 1: auth (first-run setup + login), multi-user
// management, and the empty tabbed layout. Devices/wizard land in later milestones.
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

let SESSION = null; // { id, username, role }

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    ...opts,
  });
  let data = {};
  try { data = await res.json(); } catch (_) {}
  if (!res.ok) throw Object.assign(new Error(data.error || res.statusText), { status: res.status, data });
  return data;
}

// ---- auth screen -----------------------------------------------------------
function showAuth(needsSetup) {
  $("#app").hidden = true;
  const screen = $("#auth-screen");
  screen.hidden = false;
  $("#auth-sub").textContent = needsSetup ? "Create the first admin account" : "Sign in";
  $("#auth-submit").textContent = needsSetup ? "Create admin" : "Sign in";
  $("#auth-confirm-field").hidden = !needsSetup;
  $("#auth-pass").autocomplete = needsSetup ? "new-password" : "current-password";
  $("#auth-form").dataset.mode = needsSetup ? "setup" : "login";
  $("#auth-err").hidden = true;
  $("#auth-user").focus();
}

$("#auth-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const mode = e.target.dataset.mode;
  const username = $("#auth-user").value.trim();
  const password = $("#auth-pass").value;
  const err = $("#auth-err");
  err.hidden = true;
  if (mode === "setup" && password !== $("#auth-confirm").value) {
    err.textContent = "Passwords do not match"; err.hidden = false; return;
  }
  try {
    await api(mode === "setup" ? "/api/setup" : "/api/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    });
    $("#auth-pass").value = "";
    await boot();
  } catch (ex) {
    err.textContent = ex.message || "Failed"; err.hidden = false;
  }
});

// ---- app shell -------------------------------------------------------------
function showApp() {
  $("#auth-screen").hidden = true;
  $("#app").hidden = false;
  $("#whoami").textContent = `${SESSION.username} · ${SESSION.role}`;
  $$("[data-admin]").forEach((el) => { el.hidden = SESSION.role !== "admin"; });
  switchTab("devices");
}

function switchTab(name) {
  $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  $$("[data-panel]").forEach((p) => { p.hidden = p.dataset.panel !== name; });
  if (name === "devices") loadDevices();
  if (name === "users") loadUsers();
  if (name === "add") initWizard();
}

document.addEventListener("click", (e) => {
  const tab = e.target.closest(".tab");
  if (tab) return switchTab(tab.dataset.tab);
  const goto = e.target.closest("[data-goto]");
  if (goto) return switchTab(goto.dataset.goto);
});

$("#logout-btn").addEventListener("click", async () => {
  try { await api("/api/logout", { method: "POST" }); } catch (_) {}
  SESSION = null;
  showAuth(false);
});

// ---- devices ----------------------------------------------------------------
let devicesTimer = null;
let DASHBOARDS = [];             // [{id,name,order,...}]
let ALL_DEVICES = [];           // last-loaded device list (unfiltered)
let currentDashboard = "all";   // "all" | "unassigned" | <dashboardId>
let DRAG_ID = null;             // device id currently being dragged

async function loadDevices() {
  const list = $("#devices-list");
  const empty = $("#devices-empty");
  try {
    const [dRes, devRes] = await Promise.all([
      api("/api/dashboards"), api("/api/devices"),
    ]);
    DASHBOARDS = dRes.dashboards || [];
    ALL_DEVICES = devRes.devices || [];
  } catch (ex) {
    list.innerHTML = "";
    empty.hidden = false;
    return scheduleDevRefresh();
  }
  // If the selected dashboard vanished (deleted elsewhere), fall back to All.
  if (currentDashboard !== "all" && currentDashboard !== "unassigned" &&
      !DASHBOARDS.some((d) => d.id === currentDashboard)) {
    currentDashboard = "all";
  }
  renderDashTabs();
  renderDeviceList();
  scheduleDevRefresh();
}

function scheduleDevRefresh() {
  clearInterval(devicesTimer);
  devicesTimer = setInterval(() => {
    if (DRAG_ID) return;  // don't yank cards out from under an in-progress drag
    if (!$('[data-panel="devices"]').hidden) loadDevices();
    else { clearInterval(devicesTimer); devicesTimer = null; }
  }, 15000);
}

function devicesIn(id) {
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
  const isReal = currentDashboard !== "all" && currentDashboard !== "unassigned";
  $("#dash-rename").hidden = !isReal;
  $("#dash-delete").hidden = !isReal;
}

function renderDeviceList() {
  const list = $("#devices-list");
  const empty = $("#devices-empty");
  const devs = devicesIn(currentDashboard);
  list.innerHTML = "";
  for (const d of devs) list.appendChild(deviceCard(d));
  empty.hidden = devs.length > 0;
  if (!devs.length) {
    const none = ALL_DEVICES.length === 0;
    $(".de-msg", empty).textContent = none ? "No devices yet." : "No devices in this dashboard.";
    $(".de-sub", empty).textContent = none
      ? "Add a router, switch, AP or firewall to start monitoring it."
      : "Add one here, or use “Move to…” on a device card to bring it in.";
  }
}

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

async function moveDeviceToDashboard(devId, dashboardId) {
  try {
    await api(`/api/devices/${devId}`, {
      method: "PATCH", body: JSON.stringify({ dashboardId: dashboardId || null }) });
    await loadDevices();
  } catch (ex) { alert(ex.message); }
}

// Dashboard create / rename / delete (bound once; elements are static).
$("#dash-new").addEventListener("click", async () => {
  const name = (prompt("New dashboard name (e.g. Network, Proxmox):") || "").trim();
  if (!name) return;
  try {
    const { dashboard } = await api("/api/dashboards", {
      method: "POST", body: JSON.stringify({ name }) });
    currentDashboard = dashboard.id;
    await loadDevices();
  } catch (ex) { alert(ex.message); }
});
$("#dash-rename").addEventListener("click", async () => {
  const cur = DASHBOARDS.find((d) => d.id === currentDashboard);
  if (!cur) return;
  const name = (prompt("Rename dashboard:", cur.name) || "").trim();
  if (!name || name === cur.name) return;
  try {
    await api(`/api/dashboards/${cur.id}`, { method: "PATCH", body: JSON.stringify({ name }) });
    await loadDevices();
  } catch (ex) { alert(ex.message); }
});
$("#dash-delete").addEventListener("click", async () => {
  const cur = DASHBOARDS.find((d) => d.id === currentDashboard);
  if (!cur) return;
  const n = devicesIn(cur.id).length;
  if (!confirm(`Delete dashboard "${cur.name}"?` +
      (n ? ` Its ${n} device(s) will become Unassigned (not deleted).` : ""))) return;
  try {
    await api(`/api/dashboards?id=${encodeURIComponent(cur.id)}`, { method: "DELETE" });
    currentDashboard = "all";
    await loadDevices();
  } catch (ex) { alert(ex.message); }
});

function timeAgo(ts) {
  if (!ts) return "never";
  const s = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (s < 60) return s + "s ago";
  if (s < 3600) return Math.floor(s / 60) + "m ago";
  return Math.floor(s / 3600) + "h ago";
}

function renderState(container, res) {
  container.hidden = false;
  container.innerHTML = "";
  const entries = Object.entries(res.values || {});
  if (!entries.length && !Object.keys(res.errors || {}).length) {
    container.innerHTML = `<span class="muted">no values</span>`;
  }
  for (const [k, v] of entries) {
    const kEl = document.createElement("span"); kEl.className = "k"; kEl.textContent = k;
    const vEl = document.createElement("span"); vEl.textContent = String(v);
    container.append(kEl, vEl);
  }
  for (const [k, msg] of Object.entries(res.errors || {})) {
    const kEl = document.createElement("span"); kEl.className = "k"; kEl.textContent = k;
    const vEl = document.createElement("span"); vEl.style.color = "var(--red)"; vEl.textContent = msg;
    container.append(kEl, vEl);
  }
}

function deviceCard(d) {
  const el = document.createElement("div");
  el.className = "card clickable";
  el.title = "Drag to reorder, or onto a dashboard tab to move · click for details";
  el.draggable = true;
  el.dataset.deviceId = d.id;
  el.innerHTML = `
    <div class="card-row"><h2><span class="dot"></span><span class="dname"></span></h2><span class="pill"></span></div>
    <div class="muted host"></div>
    <div class="dev-state" hidden></div>
    <div class="muted updated"></div>
    <div class="dev-actions">
      <button class="btn btn-ghost btn-sm details">Details →</button>
      <button class="btn btn-ghost btn-sm check">Sync now</button>
      <button class="btn btn-danger btn-sm del">Remove</button>
    </div>`;
  // Clicking the card body (but not its action buttons) opens the detail view.
  el.addEventListener("click", (e) => {
    if (e.target.closest(".dev-actions")) return;
    openDevice(d);
  });
  // Drag to reorder (within the list) or onto a dashboard tab (to move).
  el.addEventListener("dragstart", (e) => {
    DRAG_ID = d.id;
    el.classList.add("dragging");
    e.dataTransfer.effectAllowed = "move";
    try { e.dataTransfer.setData("text/plain", d.id); } catch (_) {}
  });
  el.addEventListener("dragend", () => {
    el.classList.remove("dragging");
    DRAG_ID = null;
    $$(".dash-tab.drop-target").forEach((t) => t.classList.remove("drop-target"));
  });
  $(".dname", el).textContent = d.name || d.host;
  $(".pill", el).textContent = d.transport;
  $(".host", el).textContent = `${d.host}${d.port ? ":" + d.port : ""} · ${d.driverId}`;
  const state = $(".dev-state", el);
  const dot = $(".dot", el);
  const updated = $(".updated", el);

  const applyState = (s) => {
    if (!s) { dot.className = "dot unknown"; updated.textContent = "not polled yet"; return; }
    dot.className = "dot " + (s.online ? "up" : "down");
    renderState(state, s);
    updated.textContent = "updated " + timeAgo(s.ts);
  };
  applyState(d.state);

  $(".check", el).onclick = async (e) => {
    const btn = e.target; btn.disabled = true; btn.textContent = "Syncing…";
    try {
      const r = await api(`/api/devices/${d.id}/state`);
      dot.className = "dot " + (Object.keys(r.values || {}).length ? "up" : "down");
      renderState(state, r);
      updated.textContent = "updated just now";
    } catch (ex) {
      dot.className = "dot down";
      state.hidden = false;
      state.innerHTML = `<span style="color:var(--red)">${ex.message}</span>`;
    } finally { btn.disabled = false; btn.textContent = "Sync now"; }
  };

  $(".details", el).onclick = () => openDevice(d);

  $(".del", el).onclick = async () => {
    if (!confirm(`Remove "${d.name || d.host}"?`)) return;
    try { await api(`/api/devices?id=${encodeURIComponent(d.id)}`, { method: "DELETE" }); loadDevices(); }
    catch (ex) { alert(ex.message); }
  };
  return el;
}

// ---- device detail modal ----------------------------------------------------
// Known entity keys → nicer labels; anything else is humanized from the key.
const ENTITY_LABELS = {
  cpu: "CPU", mem: "Memory", uptime: "Uptime", load1: "Load (1m)",
  clients: "Clients", clients_24: "Clients 2.4 GHz", clients_5: "Clients 5 GHz",
  in_octets: "Traffic in", out_octets: "Traffic out", if_count: "Interfaces",
  in_errors: "In errors", out_errors: "Out errors", mac_count: "Learned MACs",
  ports_up: "Ports up", poe_total: "PoE draw", gateways_online: "Gateways online",
  mem_used: "Memory used", channel_24: "Channel 2.4 GHz", channel_5: "Channel 5 GHz",
};
// Keys whose stored history is a monotonic byte counter — charted as a rate.
const RATE_KEY_RE = /octet|_bytes$|^bytes|throughput|rx_bytes|tx_bytes/i;
// Identity entities that belong under "Device details", never a metric graph.
const DETAIL_KEYS = new Set(["uptime", "model", "firmware", "version", "product",
  "release", "hostname", "kernel", "board", "board_name"]);
let ifEdit = false;  // interfaces "Edit" (remove/restore) toggle, per open

function fmtUptime(sec) {
  sec = Math.floor(sec);
  const d = Math.floor(sec / 86400); sec %= 86400;
  const h = Math.floor(sec / 3600); sec %= 3600;
  const m = Math.floor(sec / 60);
  const parts = [];
  if (d) parts.push(d + "d");
  if (h) parts.push(h + "h");
  if (!d) parts.push(m + "m");
  return parts.join(" ") || "0m";
}

function labelFor(key) {
  if (ENTITY_LABELS[key]) return ENTITY_LABELS[key];
  return key.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

function fmtBytes(n, perSec = false) {
  if (n == null || isNaN(n)) return "–";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0, v = Math.abs(n);
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return `${(n < 0 ? -v : v).toFixed(v >= 100 || i === 0 ? 0 : 1)} ${u[i]}${perSec ? "/s" : ""}`;
}

function fmtNum(n) {
  if (n == null || isNaN(n)) return "–";
  return Math.abs(n) >= 1000 ? Math.round(n).toLocaleString() : String(Math.round(n * 10) / 10);
}

// Turn a monotonic counter series into a per-second rate series.
function toRate(points) {
  const out = [];
  for (let i = 1; i < points.length; i++) {
    const dt = points[i][0] - points[i - 1][0];
    let dv = points[i][1] - points[i - 1][1];
    if (dt <= 0) continue;
    if (dv < 0) dv = 0; // counter reset / reboot — don't draw a negative spike
    out.push([points[i][0], dv / dt]);
  }
  return out;
}

let DM = null;  // current detail-modal state {device, entities, detail, history}

async function openDevice(d) {
  const modal = $("#device-modal");
  modal.hidden = false;
  document.body.style.overflow = "hidden";
  $("#dm-title").textContent = d.name || d.host;
  $("#dm-sub").textContent = `${d.host}${d.port ? ":" + d.port : ""} · ${d.transport} · ${d.driverId}`;
  // Dashboard move control (works on touch, where drag isn't available).
  const dsel = $("#dm-dashboard");
  dsel.innerHTML = "";
  dsel.appendChild(new Option("Unassigned", ""));
  for (const dash of DASHBOARDS) dsel.appendChild(new Option(dash.name, dash.id));
  dsel.value = d.dashboardId || "";
  dsel.onchange = async () => {
    try {
      await api(`/api/devices/${d.id}`, {
        method: "PATCH", body: JSON.stringify({ dashboardId: dsel.value || null }) });
      d.dashboardId = dsel.value || null;
      loadDevices();
    } catch (ex) { alert(ex.message); dsel.value = d.dashboardId || ""; }
  };
  $("#dm-customize").hidden = true;
  const dot = $("#dm-dot");
  dot.className = "dot " + (d.state ? (d.state.online ? "up" : "down") : "unknown");
  const body = $("#dm-body");
  body.innerHTML = `<p class="muted">Loading device details…</p>`;
  try {
    const data = await api(`/api/devices/${d.id}/detail`);
    DM = { device: data.device || d, entities: data.entities || [],
           detail: data.detail || {}, history: data.history || {},
           ifHistory: data.ifHistory || {} };
    ifEdit = false;
    const anyVal = DM.entities.some((e) => "value" in e && !e.error);
    dot.className = "dot " + (DM.device.state && DM.device.state.online ? "up"
      : anyVal ? "up" : "down");
    $("#dm-customize").hidden = false;
    $("#dm-customize").textContent = "Customize";
    renderDetail(body);
  } catch (ex) {
    DM = null;
    body.innerHTML = `<p class="auth-err">Couldn't load details: ${ex.message}</p>`;
  }
}

function closeDevice() {
  $("#device-modal").hidden = true;
  document.body.style.overflow = "";
  DM = null;
}

document.addEventListener("click", (e) => {
  if (e.target.closest("[data-close]")) closeDevice();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("#device-modal").hidden) closeDevice();
});
$("#dm-customize").addEventListener("click", () => toggleCustomize());

function section(title) {
  const s = document.createElement("div");
  s.className = "detail-section";
  s.innerHTML = `<h3></h3>`;
  $("h3", s).textContent = title;
  return s;
}

function renderDetail(body) {
  body.innerHTML = "";
  const { entities, detail, history } = DM;
  const enabled = entities.filter((e) => e.enabled && e.kind === "sensor");

  // Partition enabled sensors: identity keys (uptime, model, …) are always
  // "device details"; otherwise numbers/booleans are metrics (value + chart)
  // and strings are details.
  const details = [];  // {label, value}
  const metrics = [];  // entity records
  for (const e of enabled) {
    if (DETAIL_KEYS.has(e.key)) {
      let v = e.value;
      if (e.key === "uptime" && typeof v === "number") v = fmtUptime(v);
      details.push({ label: e.name, value: v == null ? "–" : String(v) });
    } else if (e.error || typeof e.value === "number" || typeof e.value === "boolean") {
      metrics.push(e);
    } else if (e.value == null) {
      details.push({ label: e.name, value: "–" });
    } else {
      details.push({ label: e.name, value: String(e.value) });
    }
  }
  for (const [k, v] of Object.entries(detail.info || {})) {
    details.push({ label: k, value: v == null ? "–" : String(v) });
  }
  if (detail.error) details.push({ label: "Detail error", value: detail.error });

  // --- Device details (identity) ---
  if (details.length) {
    const s = section("Device details");
    const grid = document.createElement("div");
    grid.className = "info-grid";
    for (const { label, value } of details) {
      const chip = document.createElement("div");
      chip.className = "info-chip";
      const isErr = label === "Detail error";
      chip.innerHTML = `<div class="k"></div><div class="v${isErr ? " err" : ""}"></div>`;
      $(".k", chip).textContent = label;
      $(".v", chip).textContent = value === "" ? "–" : value;
      grid.appendChild(chip);
    }
    s.appendChild(grid);
    body.appendChild(s);
  }

  // --- Metrics (CPU / memory / clients / traffic …) ---
  if (metrics.length) {
    const s = section("Metrics");
    const grid = document.createElement("div");
    grid.className = "charts";
    for (const e of metrics) grid.appendChild(metricCard(e, history));
    s.appendChild(grid);
    body.appendChild(s);
  }

  // --- Driver tables (interfaces / clients / radios …) ---
  for (const t of detail.tables || []) {
    if (t.interfaces) {
      body.appendChild(interfacesSection(t));
    } else {
      const s = section(t.title || "Details");
      s.appendChild(detailTable(t));
      body.appendChild(s);
    }
  }

  if (!details.length && !metrics.length && !(detail.tables || []).length) {
    body.appendChild(Object.assign(document.createElement("p"), {
      className: "detail-empty",
      textContent: "No entities enabled. Use Customize to choose what to display.",
    }));
  }

  // --- Customize panel (hidden until toggled) ---
  body.appendChild(buildCustomize());
}

// A metric renders as a history chart when it has a numeric series, otherwise a
// value-only card.
function metricCard(e, history) {
  const pts = history[e.key] || [];
  const numericHist = pts.length >= 2 && pts.every((p) => typeof p[1] === "number");
  if (numericHist && !e.error) return chartCard(e, pts);

  const card = document.createElement("div");
  card.className = "metric-card";
  card.innerHTML = `<div class="m-label"></div><div class="m-val"></div>`;
  $(".m-label", card).textContent = e.name;
  const val = $(".m-val", card);
  if (e.error) {
    val.classList.add("err");
    val.textContent = e.error;
  } else if (e.value == null) {
    val.textContent = "–";
  } else if (typeof e.value === "boolean") {
    val.textContent = e.value ? "Yes" : "No";
  } else {
    val.textContent = fmtNum(e.value);
    if (e.unit) {
      const u = document.createElement("span");
      u.className = "m-unit";
      u.textContent = e.unit;
      val.appendChild(u);
    }
  }
  return card;
}

// ---- customize (edit displayed entities) ----
function buildCustomize() {
  const wrap = document.createElement("div");
  wrap.className = "dm-customize";
  wrap.id = "dm-customize-panel";
  wrap.hidden = true;
  wrap.innerHTML = `
    <h3>Customize this device</h3>
    <p class="cz-sub">Choose which entities are displayed and tracked. Unchecked
      entities stop being polled and charted.</p>
    <div class="ent-list" id="cz-list"></div>
    <div class="cz-actions">
      <button class="btn btn-ghost btn-sm" id="cz-cancel">Cancel</button>
      <button class="btn btn-primary btn-sm" id="cz-save">Save</button>
    </div>`;
  const list = $("#cz-list", wrap);
  for (const e of DM.entities.filter((x) => x.kind === "sensor")) {
    const item = document.createElement("label");
    item.className = "ent-item";
    const cb = document.createElement("input");
    cb.type = "checkbox"; cb.dataset.key = e.key; cb.checked = !!e.enabled;
    const label = document.createElement("span");
    label.textContent = e.name;
    item.append(cb, label);
    if (e.unit) {
      const u = document.createElement("span");
      u.className = "e-unit"; u.textContent = `(${e.unit})`;
      item.append(u);
    }
    list.appendChild(item);
  }
  $("#cz-cancel", wrap).onclick = () => toggleCustomize(false);
  $("#cz-save", wrap).onclick = () => saveCustomize(wrap);
  return wrap;
}

function toggleCustomize(force) {
  const panel = $("#dm-customize-panel");
  if (!panel) return;
  const show = force !== undefined ? force : panel.hidden;
  panel.hidden = !show;
  $("#dm-customize").textContent = show ? "Done" : "Customize";
  if (show) panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

async function saveCustomize(wrap) {
  const keys = $$("#cz-list input:checked", wrap).map((c) => ({ key: c.dataset.key }));
  if (!keys.length) {
    alert("Select at least one entity to display.");
    return;
  }
  const btn = $("#cz-save", wrap);
  btn.disabled = true; btn.textContent = "Saving…";
  try {
    await api(`/api/devices/${DM.device.id}`, {
      method: "PATCH", body: JSON.stringify({ entities: keys }) });
    await openDevice(DM.device);  // re-fetch so newly enabled entities read live
    loadDevices();                // refresh card entity lists in the background
  } catch (ex) {
    btn.disabled = false; btn.textContent = "Save";
    alert(ex.message);
  }
}

// `e` is an entity record ({key,name,unit,value}); `rawPoints` its history.
function chartCard(e, rawPoints) {
  const key = e.key;
  const isRate = RATE_KEY_RE.test(key);
  const points = isRate ? toRate(rawPoints) : rawPoints;
  const card = document.createElement("div");
  card.className = "chart-card";
  const vals = points.map((p) => p[1]);
  const lo = vals.length ? Math.min(...vals) : null;
  const hi = vals.length ? Math.max(...vals) : null;
  const fmt = isRate ? (v) => fmtBytes(v, true) : (v) => fmtNum(v);
  // Current value: the live read for plain metrics; the latest rate for counters.
  const now = isRate ? (vals.length ? vals[vals.length - 1] : null)
    : (typeof e.value === "number" ? e.value : (vals.length ? vals[vals.length - 1] : null));
  const unit = !isRate && e.unit ? " " + e.unit : "";
  card.innerHTML = `
    <div class="c-head"><span class="c-title"></span><span class="c-now"></span></div>
    <canvas></canvas>
    <div class="c-foot"><span class="lo"></span><span class="hi"></span></div>`;
  $(".c-title", card).textContent = e.name || labelFor(key);
  $(".c-now", card).textContent = now == null ? "–" : fmt(now) + unit;
  $(".lo", card).textContent = "min " + fmt(lo);
  $(".hi", card).textContent = "max " + fmt(hi);
  // Draw after layout so the canvas has its CSS width.
  requestAnimationFrame(() => drawChart($("canvas", card), points));
  return card;
}

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || "#4aa8ff";
}

function drawChart(canvas, points) {
  const dpr = window.devicePixelRatio || 1;
  const cssW = canvas.clientWidth || 240;
  const cssH = canvas.clientHeight || 56;
  canvas.width = Math.round(cssW * dpr);
  canvas.height = Math.round(cssH * dpr);
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, cssW, cssH);
  if (points.length < 2) return;

  const pad = 3;
  const w = cssW - pad * 2, h = cssH - pad * 2;
  const xs = points.map((p) => p[0]);
  const ys = points.map((p) => p[1]);
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  let y0 = Math.min(...ys), y1 = Math.max(...ys);
  if (y1 === y0) { y1 += 1; y0 -= 1; } // flat line — give it room
  const px = (t) => pad + ((t - x0) / (x1 - x0 || 1)) * w;
  const py = (v) => pad + h - ((v - y0) / (y1 - y0)) * h;

  const accent = cssVar("--accent");
  // Area fill under the line.
  ctx.beginPath();
  ctx.moveTo(px(xs[0]), py(ys[0]));
  for (let i = 1; i < points.length; i++) ctx.lineTo(px(xs[i]), py(ys[i]));
  ctx.lineTo(px(xs[xs.length - 1]), pad + h);
  ctx.lineTo(px(xs[0]), pad + h);
  ctx.closePath();
  ctx.fillStyle = accent + "22";
  ctx.fill();
  // The line.
  ctx.beginPath();
  ctx.moveTo(px(xs[0]), py(ys[0]));
  for (let i = 1; i < points.length; i++) ctx.lineTo(px(xs[i]), py(ys[i]));
  ctx.strokeStyle = accent;
  ctx.lineWidth = 1.5;
  ctx.lineJoin = "round";
  ctx.stroke();
  // Marker on the latest point.
  ctx.beginPath();
  ctx.arc(px(xs[xs.length - 1]), py(ys[ys.length - 1]), 2.5, 0, Math.PI * 2);
  ctx.fillStyle = accent;
  ctx.fill();
}

function detailTable(t) {
  const cols = t.columns || [];
  const rows = t.rows || [];
  if (!rows.length) {
    const p = document.createElement("p");
    p.className = "detail-empty";
    p.textContent = "None.";
    return p;
  }
  const wrap = document.createElement("div");
  wrap.className = "detail-table-wrap";
  const table = document.createElement("table");
  table.className = "detail-table";
  const thead = document.createElement("thead");
  const htr = document.createElement("tr");
  for (const c of cols) {
    const th = document.createElement("th");
    th.textContent = c.label + (c.unit ? ` (${c.unit})` : "");
    htr.appendChild(th);
  }
  thead.appendChild(htr);
  table.appendChild(thead);
  const tbody = document.createElement("tbody");
  for (const row of rows) {
    const tr = document.createElement("tr");
    for (const c of cols) {
      const td = document.createElement("td");
      const v = row[c.key];
      td.textContent = v == null || v === "" ? "–" : String(v);
      if (/mac|rssi|tx|rx|channel|clients/i.test(c.key)) td.className = "mono";
      tr.appendChild(td);
    }
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  wrap.appendChild(table);
  return wrap;
}

// ---- interfaces: clickable per-interface history + edit/remove --------------
function interfacesSection(t) {
  const idKey = t.idKey || "device";
  const hidden = new Set((DM.device.hiddenInterfaces || []).map(String));
  const s = document.createElement("div");
  s.className = "detail-section";
  s.innerHTML = `
    <div class="sec-head"><h3></h3>
      <button class="btn btn-ghost btn-sm if-edit"></button></div>
    <div class="if-chart" hidden></div>
    <div class="if-table"></div>
    <div class="if-hidden" hidden></div>`;
  $("h3", s).textContent = t.title || "Interfaces";
  const chartBox = $(".if-chart", s);
  const tableBox = $(".if-table", s);
  const hiddenBox = $(".if-hidden", s);
  const editBtn = $(".if-edit", s);

  async function saveHidden() {
    DM.device.hiddenInterfaces = [...hidden];
    try {
      await api(`/api/devices/${DM.device.id}`, {
        method: "PATCH", body: JSON.stringify({ hiddenInterfaces: [...hidden] }) });
    } catch (ex) { alert(ex.message); }
  }

  function render() {
    editBtn.textContent = ifEdit ? "Done" : "Edit";
    const rows = t.rows || [];
    const visible = rows.filter((r) => !hidden.has(String(r[idKey])));
    tableBox.innerHTML = "";
    tableBox.appendChild(ifTable(t, visible, idKey, hidden, chartBox, saveHidden, render));
    // Hidden interfaces (restorable) — only shown while editing.
    hiddenBox.innerHTML = "";
    const hiddenRows = rows.filter((r) => hidden.has(String(r[idKey])));
    if (ifEdit && hiddenRows.length) {
      hiddenBox.hidden = false;
      hiddenBox.append(Object.assign(document.createElement("span"),
        { className: "if-hidden-lbl", textContent: "Hidden — tap to restore:" }));
      for (const r of hiddenRows) {
        const chip = document.createElement("button");
        chip.className = "btn btn-ghost btn-sm";
        chip.textContent = "+ " + (r.name || r[idKey]);
        chip.onclick = () => { hidden.delete(String(r[idKey])); saveHidden(); render(); };
        hiddenBox.appendChild(chip);
      }
    } else {
      hiddenBox.hidden = true;
    }
  }
  editBtn.onclick = () => { ifEdit = !ifEdit; render(); };
  render();
  return s;
}

function ifTable(t, rows, idKey, hidden, chartBox, saveHidden, rerender) {
  const cols = t.columns || [];
  if (!rows.length) {
    return Object.assign(document.createElement("p"),
      { className: "detail-empty", textContent: "No interfaces shown." });
  }
  const wrap = document.createElement("div");
  wrap.className = "detail-table-wrap";
  const table = document.createElement("table");
  table.className = "detail-table if-table-el" + (ifEdit ? "" : " rows-clickable");
  const thead = document.createElement("thead");
  const htr = document.createElement("tr");
  for (const c of cols) {
    const th = document.createElement("th");
    th.textContent = c.label + (c.unit ? ` (${c.unit})` : "");
    htr.appendChild(th);
  }
  if (ifEdit) htr.appendChild(document.createElement("th"));
  thead.appendChild(htr);
  table.appendChild(thead);
  const tbody = document.createElement("tbody");
  for (const row of rows) {
    const id = String(row[idKey]);
    const ifh = (DM.ifHistory || {})[id];
    const hasHist = ifh && ((ifh.rx || []).length >= 2 || (ifh.tx || []).length >= 2);
    const tr = document.createElement("tr");
    if (hasHist && !ifEdit) tr.classList.add("has-history");
    for (const c of cols) {
      const td = document.createElement("td");
      const v = row[c.key];
      td.textContent = v == null || v === "" ? "–" : String(v);
      if (/mac|tx|rx|status/i.test(c.key)) td.className = "mono";
      tr.appendChild(td);
    }
    if (ifEdit) {
      const td = document.createElement("td");
      const x = document.createElement("button");
      x.className = "if-remove"; x.textContent = "✕"; x.title = "Remove this interface";
      x.onclick = (e) => { e.stopPropagation(); hidden.add(id); saveHidden(); rerender(); };
      td.appendChild(x);
      tr.appendChild(td);
    }
    tr.onclick = () => {
      if (ifEdit) return;
      showIfChart(chartBox, id, row.name || id);
      [...tbody.children].forEach((r) => r.classList.remove("sel"));
      tr.classList.add("sel");
    };
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  wrap.appendChild(table);
  return wrap;
}

function showIfChart(container, id, name) {
  const ifh = (DM.ifHistory || {})[id] || {};
  const rx = ifh.rx || [], tx = ifh.tx || [];
  container.hidden = false;
  container.innerHTML = "";
  if (rx.length < 2 && tx.length < 2) {
    container.innerHTML = `<p class="detail-empty">No traffic history yet for ${name}` +
      ` — it builds up as the device is polled (every ~60s).</p>`;
    return;
  }
  container.appendChild(dualChartCard(name, rx, tx));
}

// Upload/download history for one interface (raw byte counters -> rate).
function dualChartCard(name, rxRaw, txRaw) {
  const rx = toRate(rxRaw), tx = toRate(txRaw);
  const card = document.createElement("div");
  card.className = "chart-card if-chart-card";
  const dNow = rx.length ? rx[rx.length - 1][1] : null;
  const uNow = tx.length ? tx[tx.length - 1][1] : null;
  card.innerHTML = `
    <div class="c-head"><span class="c-title"></span>
      <span class="c-legend"><span class="dl">&#8595; download <b class="dv"></b></span>
        <span class="ul">&#8593; upload <b class="uv"></b></span></span></div>
    <canvas></canvas>`;
  $(".c-title", card).textContent = name + " — traffic";
  $(".dv", card).textContent = dNow == null ? "–" : fmtBytes(dNow, true);
  $(".uv", card).textContent = uNow == null ? "–" : fmtBytes(uNow, true);
  requestAnimationFrame(() => drawDualChart($("canvas", card), rx, tx));
  return card;
}

function drawDualChart(canvas, rxPts, txPts) {
  const dpr = window.devicePixelRatio || 1;
  const cssW = canvas.clientWidth || 240;
  const cssH = canvas.clientHeight || 72;
  canvas.width = Math.round(cssW * dpr);
  canvas.height = Math.round(cssH * dpr);
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, cssW, cssH);
  const all = [...rxPts, ...txPts];
  if (all.length < 2) return;
  const pad = 3, w = cssW - pad * 2, h = cssH - pad * 2;
  const xs = all.map((p) => p[0]);
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  let y1 = Math.max(0, ...all.map((p) => p[1]));
  if (y1 <= 0) y1 = 1;
  const px = (t) => pad + ((t - x0) / (x1 - x0 || 1)) * w;
  const py = (v) => pad + h - (v / y1) * h;
  for (const [pts, color] of [[rxPts, cssVar("--accent")], [txPts, cssVar("--green")]]) {
    if (pts.length < 2) continue;
    ctx.beginPath();
    ctx.moveTo(px(pts[0][0]), py(pts[0][1]));
    for (let i = 1; i < pts.length; i++) ctx.lineTo(px(pts[i][0]), py(pts[i][1]));
    ctx.strokeStyle = color; ctx.lineWidth = 1.5; ctx.lineJoin = "round"; ctx.stroke();
    ctx.beginPath();
    ctx.arc(px(pts[pts.length - 1][0]), py(pts[pts.length - 1][1]), 2.5, 0, Math.PI * 2);
    ctx.fillStyle = color; ctx.fill();
  }
}

// ---- web push ---------------------------------------------------------------
function urlB64ToUint8Array(base64) {
  const pad = "=".repeat((4 - (base64.length % 4)) % 4);
  const b64 = (base64 + pad).replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(b64);
  return Uint8Array.from([...raw].map((c) => c.charCodeAt(0)));
}

async function enablePush() {
  const msg = $("#push-msg"); msg.hidden = false;
  if (!("serviceWorker" in navigator) || !("PushManager" in window)) {
    msg.textContent = "Push isn't supported by this browser."; return;
  }
  if (!window.isSecureContext) {
    msg.textContent = "Alerts need HTTPS (or localhost). Put HomelabHQ behind TLS to enable push.";
    return;
  }
  try {
    const perm = await Notification.requestPermission();
    if (perm !== "granted") { msg.textContent = "Notification permission denied."; return; }
    const reg = await navigator.serviceWorker.ready;
    const { publicKey } = await api("/api/push/vapid");
    const sub = await reg.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlB64ToUint8Array(publicKey),
    });
    await api("/api/push/subscribe", { method: "POST", body: JSON.stringify({ subscription: sub }) });
    msg.textContent = "Alerts enabled on this device.";
    $("#push-test").hidden = false;
  } catch (ex) {
    msg.textContent = "Couldn't enable alerts: " + ex.message;
  }
}

$("#push-enable").addEventListener("click", enablePush);
$("#push-test").addEventListener("click", async () => {
  const msg = $("#push-msg"); msg.hidden = false;
  try { const r = await api("/api/push/test", { method: "POST" }); msg.textContent = `Test sent (${r.sent}).`; }
  catch (ex) { msg.textContent = "Test failed: " + ex.message; }
});

// ---- add-device wizard ------------------------------------------------------
const TRANSPORTS = {
  ssh:  { label: "SSH", sub: "Shell login", defaultPort: 22, fields: [
    { k: "username", label: "Username" },
    { k: "password", label: "Password", type: "password" },
    { k: "privateKey", label: "Private key (optional, instead of password)", type: "textarea", full: true },
  ]},
  http: { label: "HTTP web UI", sub: "User + password", defaultPort: 80, fields: [
    { k: "username", label: "Username" },
    { k: "password", label: "Password", type: "password" },
    { k: "scheme", label: "Scheme", type: "select", options: ["http", "https"], default: "http" },
    { k: "probePath", label: "Probe path", default: "/" },
    { k: "metricsPath", label: "Prometheus /metrics path (optional — OpenWrt switch SFP data)", full: true },
    { k: "verifyTls", label: "Verify TLS certificate", type: "checkbox", default: true },
  ]},
  api:  { label: "HTTP API", sub: "Key + secret", defaultPort: "", fields: [
    { k: "apiKey", label: "API key" },
    { k: "apiSecret", label: "API secret", type: "password" },
    { k: "authStyle", label: "Auth style", type: "select", options: ["basic", "bearer", "header"], default: "basic" },
    { k: "scheme", label: "Scheme", type: "select", options: ["https", "http"], default: "https" },
    { k: "basePath", label: "Base path (optional)" },
    { k: "probePath", label: "Probe path", default: "/" },
    { k: "verifyTls", label: "Verify TLS certificate", type: "checkbox", default: true },
  ]},
  snmp: { label: "SNMP", sub: "Community string", defaultPort: 161, fields: [
    { k: "community", label: "Community", default: "public" },
    { k: "version", label: "Version", type: "select", options: ["2c", "1"], default: "2c" },
  ]},
};
const TRANSPORT_ORDER = ["ssh", "http", "api", "snmp"];

// Device presets: choosing one pre-fills the transport, auth style, port and a
// hint so the user enters credentials in the exact shape a driver expects.
const PRESETS = [
  { id: "opnsense", label: "OPNsense firewall", transport: "api", port: "",
    set: { authStyle: "basic", scheme: "https" },
    hint: "OPNsense: create an API key + secret (System ▸ Access ▸ Users). Enter the key as “API key” and secret as “API secret”." },
  { id: "pfsense", label: "pfSense firewall (REST API v2)", transport: "api", port: "",
    set: { authStyle: "header", keyHeader: "X-API-Key", scheme: "https" },
    hint: "Requires the pfSense REST API v2 package. Paste your API key as “API key”." },
  { id: "unifi", label: "UniFi Network controller", transport: "api", port: 443,
    set: { authStyle: "header", keyHeader: "X-API-KEY", scheme: "https" },
    hint: "UniFi Network 9+: create an API key, then paste it as “API key”." },
  { id: "proxmox", label: "Proxmox VE", transport: "api", port: 8006,
    set: { authStyle: "header", keyHeader: "Authorization", scheme: "https" },
    hint: "Proxmox: create an API token, then paste the WHOLE “PVEAPIToken=user@realm!tokenid=secret” string as “API key”." },
  { id: "truenas", label: "TrueNAS", transport: "api", port: "",
    set: { authStyle: "bearer", scheme: "https" },
    hint: "TrueNAS: create an API key (Settings ▸ API Keys) and paste it as “API key”." },
  { id: "firewalla", label: "Firewalla (MSP)", transport: "api", port: "",
    set: { authStyle: "header", keyHeader: "Authorization", scheme: "https" },
    hint: "Host is your MSP domain (xxx.firewalla.net). Paste “Token <your-token>” as “API key”." },
  { id: "mikrotik", label: "MikroTik RouterOS", transport: "api", port: "",
    set: { authStyle: "basic", scheme: "https" },
    hint: "RouterOS REST API: enter your username as “API key” and password as “API secret”." },
  { id: "openwrt", label: "OpenWrt router / AP / switch", transport: "http", port: 80,
    set: { scheme: "http", metricsPath: "/metrics" },
    hint: "Enter your LuCI (web UI) username and password. If the device exposes a Prometheus /metrics page (e.g. an OpenWrt-flashed switch with SFP telemetry), leave the metrics path set to pull SFP/optics data." },
  { id: "synology", label: "Synology DSM NAS", transport: "http", port: 5000,
    set: { scheme: "http" }, hint: "Enter your DSM username and password (DSM is usually on port 5000/5001)." },
  { id: "qnap", label: "QNAP NAS", transport: "http", port: 8080,
    set: { scheme: "http" }, hint: "Enter your QTS username and password (QTS is usually on port 8080/443)." },
  { id: "keeplink", label: "Keeplink web-smart switch", transport: "http", port: 80,
    set: { scheme: "http" }, hint: "Enter the switch web-UI username and password." },
  { id: "zyxel", label: "Zyxel WiFi access point (NWA/WAX)", transport: "http", port: 443,
    set: { scheme: "https", verifyTls: false },
    hint: "Enter the AP web-UI admin username and password. Zyxel APs use HTTPS with a self-signed certificate, so TLS verification is off." },
];

let WIZ = null;

async function initWizard() {
  WIZ = { transport: null, candidates: [], driverId: null, entities: [] };
  wizGoto(1);
  $("#wiz-err1").hidden = true;
  $("#wiz-host").value = ""; $("#wiz-port").value = "";
  $("#wiz-hint").hidden = true;
  // Only offer transports the server actually has drivers for.
  let available = TRANSPORT_ORDER;
  try {
    const { transports } = await api("/api/drivers");
    available = TRANSPORT_ORDER.filter((t) => transports.includes(t));
  } catch (_) {}
  const grid = $("#wiz-transports");
  grid.innerHTML = "";
  for (const t of available) {
    const meta = TRANSPORTS[t];
    const el = document.createElement("div");
    el.className = "transport-opt";
    el.dataset.transport = t;
    el.innerHTML = `<div class="t-name">${meta.label}</div><div class="t-sub">${meta.sub}</div>`;
    el.onclick = () => {           // manual pick clears any preset
      $("#wiz-preset").value = "auto";
      $("#wiz-hint").hidden = true;
      selectTransport(t);
    };
    grid.appendChild(el);
  }
  // populate the device-type preset dropdown
  const sel = $("#wiz-preset");
  sel.innerHTML = "";
  sel.append(new Option("Auto-detect / custom", "auto"));
  for (const p of PRESETS) sel.append(new Option(p.label, p.id));
  sel.value = "auto";
  sel.onchange = () => {
    const p = PRESETS.find((x) => x.id === sel.value);
    if (p) applyPreset(p);
    else { $("#wiz-hint").hidden = true; }
  };
  $("#wiz-creds").innerHTML = `<p class="muted">Pick a device type above, or choose a connection method.</p>`;
}

function applyPreset(p) {
  selectTransport(p.transport);
  $("#wiz-port").value = (p.port === undefined || p.port === "") ? "" : p.port;
  for (const [k, v] of Object.entries(p.set || {})) {
    const el = $("#cred-" + k);
    if (!el) continue;
    if (el.type === "checkbox") el.checked = !!v;
    else el.value = v;
  }
  const hint = $("#wiz-hint");
  hint.textContent = p.hint || "";
  hint.hidden = !p.hint;
}

function selectTransport(t) {
  WIZ.transport = t;
  $$("#wiz-transports .transport-opt").forEach((n) => n.classList.toggle("selected", n.dataset.transport === t));
  const meta = TRANSPORTS[t];
  $("#wiz-port").placeholder = meta.defaultPort ? `default ${meta.defaultPort}` : "(none)";
  const box = $("#wiz-creds");
  box.innerHTML = "";
  for (const f of meta.fields) {
    const wrap = document.createElement("label");
    wrap.className = "field" + (f.full ? " full" : "") + (f.type === "checkbox" ? " check" : "");
    if (f.type === "checkbox") {
      const cb = document.createElement("input"); cb.type = "checkbox"; cb.id = "cred-" + f.k;
      cb.checked = f.default !== false;
      wrap.append(cb, Object.assign(document.createElement("span"), { textContent: f.label }));
    } else {
      wrap.append(Object.assign(document.createElement("span"), { textContent: f.label }));
      let input;
      if (f.type === "textarea") input = document.createElement("textarea");
      else if (f.type === "select") {
        input = document.createElement("select");
        for (const o of f.options) input.append(new Option(o, o));
        input.value = f.default;
      } else { input = document.createElement("input"); input.type = f.type || "text"; }
      input.id = "cred-" + f.k;
      if (f.default && f.type !== "select") input.value = f.default;
      wrap.append(input);
    }
    box.appendChild(wrap);
  }
}

function collectCreds() {
  const creds = {};
  for (const f of TRANSPORTS[WIZ.transport].fields) {
    const el = $("#cred-" + f.k);
    if (!el) continue;
    if (f.type === "checkbox") creds[f.k] = el.checked;
    else if (el.value !== "") creds[f.k] = el.value;
  }
  return creds;
}

function wizGoto(step) {
  WIZ.step = step;
  $$(".wiz-step").forEach((s) => { s.hidden = Number(s.dataset.wstep) !== step; });
  $$("#wiz-steps li").forEach((li) => {
    const n = Number(li.dataset.step);
    li.classList.toggle("active", n === step);
    li.classList.toggle("done", n < step);
  });
}

$("#wiz-detect").addEventListener("click", async () => {
  const err = $("#wiz-err1"); err.hidden = true;
  if (!WIZ.transport) { err.textContent = "Choose a connection method."; err.hidden = false; return; }
  const host = $("#wiz-host").value.trim();
  if (!host) { err.textContent = "Enter a host or IP."; err.hidden = false; return; }
  WIZ.host = host;
  WIZ.port = $("#wiz-port").value.trim() ? Number($("#wiz-port").value.trim()) : null;
  WIZ.credentials = collectCreds();
  const btn = $("#wiz-detect"); btn.disabled = true; btn.textContent = "Detecting…";
  try {
    const r = await api("/api/devices/detect", { method: "POST", body: JSON.stringify({
      transport: WIZ.transport, host: WIZ.host, port: WIZ.port, credentials: WIZ.credentials }) });
    WIZ.candidates = r.candidates || [];
    if (!WIZ.candidates.length) {
      err.textContent = "Connected, but no driver recognised this device.";
      err.hidden = false; return;
    }
    renderCandidates(r.banner);
    wizGoto(2);
  } catch (ex) {
    err.textContent = ex.status === 502
      ? `Couldn't reach or authenticate: ${ex.message}` : ex.message;
    err.hidden = false;
  } finally { btn.disabled = false; btn.textContent = "Detect device →"; }
});

function renderCandidates(banner) {
  $("#wiz-banner").textContent = banner ? `Banner: ${banner}` : "";
  const box = $("#wiz-candidates"); box.innerHTML = "";
  WIZ.driverId = WIZ.candidates[0].driverId; // default to best match
  WIZ.candidates.forEach((c, i) => {
    const el = document.createElement("div");
    el.className = "candidate" + (i === 0 ? " selected" : "");
    const pct = Math.round(c.confidence * 100);
    el.innerHTML = `<span class="c-name">${c.displayName}</span>
      <span class="conf"><span class="conf-bar"><i style="width:${pct}%"></i></span>${pct}%</span>`;
    el.onclick = () => {
      WIZ.driverId = c.driverId;
      $$("#wiz-candidates .candidate").forEach((n) => n.classList.toggle("selected", n === el));
    };
    box.appendChild(el);
  });
}

$("#wiz-back2").addEventListener("click", () => wizGoto(1));
$("#wiz-back3").addEventListener("click", () => wizGoto(2));

$("#wiz-choose").addEventListener("click", async () => {
  const err = $("#wiz-err2"); err.hidden = true;
  const btn = $("#wiz-choose"); btn.disabled = true; btn.textContent = "Loading…";
  try {
    const r = await api("/api/devices/entities", { method: "POST", body: JSON.stringify({
      transport: WIZ.transport, host: WIZ.host, port: WIZ.port,
      credentials: WIZ.credentials, driverId: WIZ.driverId }) });
    WIZ.entities = r.entities || [];
    renderEntities();
    wizGoto(3);
  } catch (ex) {
    err.textContent = ex.message; err.hidden = false;
  } finally { btn.disabled = false; btn.textContent = "Choose entities →"; }
});

function renderEntities() {
  const cand = WIZ.candidates.find((c) => c.driverId === WIZ.driverId);
  $("#wiz-name").value = cand ? cand.displayName.replace(/\s*\(.*\)$/, "") : WIZ.host;
  // Dashboard picker — default to the one the user is currently viewing.
  const dsel = $("#wiz-dashboard");
  dsel.innerHTML = "";
  dsel.appendChild(new Option("Unassigned", ""));
  for (const dash of DASHBOARDS) dsel.appendChild(new Option(dash.name, dash.id));
  dsel.value = (currentDashboard !== "all" && currentDashboard !== "unassigned")
    ? currentDashboard : "";
  const sensors = $("#wiz-sensors"); const controls = $("#wiz-controls");
  sensors.innerHTML = ""; controls.innerHTML = "";
  let hasControls = false;
  for (const e of WIZ.entities) {
    const item = document.createElement("label");
    item.className = "ent-item";
    const cb = document.createElement("input"); cb.type = "checkbox";
    cb.dataset.key = e.key;
    cb.checked = !e.controllable; // sensors on by default, controls opt-in
    const label = document.createElement("span");
    label.textContent = e.name + (e.unit ? " " : "");
    item.append(cb, label);
    if (e.unit) { const u = document.createElement("span"); u.className = "e-unit"; u.textContent = `(${e.unit})`; item.append(u); }
    if (e.controllable) { controls.appendChild(item); hasControls = true; }
    else sensors.appendChild(item);
  }
  $("#wiz-controls-group").hidden = !hasControls;
}

$("#wiz-save").addEventListener("click", async () => {
  const err = $("#wiz-err3"); err.hidden = true;
  const keys = $$("#wiz-sensors input:checked, #wiz-controls input:checked").map((c) => ({ key: c.dataset.key }));
  const btn = $("#wiz-save"); btn.disabled = true; btn.textContent = "Adding…";
  try {
    await api("/api/devices", { method: "POST", body: JSON.stringify({
      transport: WIZ.transport, host: WIZ.host, port: WIZ.port,
      credentials: WIZ.credentials, driverId: WIZ.driverId,
      name: $("#wiz-name").value.trim() || WIZ.host, entities: keys,
      dashboardId: $("#wiz-dashboard").value || null }) });
    $("#wiz-done-msg").textContent = `${$("#wiz-name").value.trim() || WIZ.host} added with ${keys.length} entities.`;
    wizGoto(4);
  } catch (ex) {
    err.textContent = ex.message; err.hidden = false;
  } finally { btn.disabled = false; btn.textContent = "Add device"; }
});

$("#wiz-another").addEventListener("click", initWizard);

// ---- users -----------------------------------------------------------------
async function loadUsers() {
  const list = $("#users-list");
  try {
    const { users } = await api("/api/users");
    list.innerHTML = "";
    for (const u of users) {
      const el = document.createElement("div");
      el.className = "card";
      el.innerHTML = `<div class="card-row">
          <h2></h2><span class="pill"></span></div>
        <div class="card-row"><span class="muted id"></span></div>`;
      $("h2", el).textContent = u.username;
      const pill = $(".pill", el);
      pill.textContent = u.role;
      if (u.role === "admin") pill.classList.add("admin");
      if (u.id !== SESSION.id) {
        const del = document.createElement("button");
        del.className = "btn btn-danger btn-sm";
        del.textContent = "Remove";
        del.onclick = () => removeUser(u.id, u.username);
        $(".card-row:last-child", el).appendChild(del);
      } else {
        $(".id", el).textContent = "you";
      }
      list.appendChild(el);
    }
  } catch (ex) {
    list.innerHTML = `<p class="muted">${ex.message}</p>`;
  }
}

async function removeUser(id, name) {
  if (!confirm(`Remove user "${name}"?`)) return;
  const err = $("#users-err");
  err.hidden = true;
  try { await api("/api/users?id=" + encodeURIComponent(id), { method: "DELETE" }); loadUsers(); }
  catch (ex) { err.textContent = ex.message; err.hidden = false; }
}

$("#add-user-btn").addEventListener("click", () => {
  $("#add-user-form").hidden = false;
  $("#nu-user").focus();
});
$("#nu-cancel").addEventListener("click", () => { $("#add-user-form").hidden = true; });
$("#add-user-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const err = $("#users-err"); err.hidden = true;
  try {
    await api("/api/users", { method: "POST", body: JSON.stringify({
      username: $("#nu-user").value.trim(),
      password: $("#nu-pass").value,
      role: $("#nu-role").value,
    })});
    $("#nu-user").value = ""; $("#nu-pass").value = "";
    $("#add-user-form").hidden = true;
    loadUsers();
  } catch (ex) { err.textContent = ex.message; err.hidden = false; }
});

// ---- settings --------------------------------------------------------------
$("#pw-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const msg = $("#pw-msg");
  const pw = $("#pw-new").value;
  if (!pw) return;
  try {
    await api("/api/account/password", { method: "POST", body: JSON.stringify({ password: pw }) });
    $("#pw-new").value = "";
    msg.textContent = "Password updated."; msg.hidden = false;
  } catch (ex) { msg.textContent = ex.message; msg.hidden = false; }
});

// ---- boot ------------------------------------------------------------------
async function boot() {
  try {
    const s = await api("/api/session");
    if (s.authenticated) { SESSION = s.user; showApp(); }
    else showAuth(s.needsSetup);
  } catch (ex) {
    showAuth(false);
  }
}

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch(() => {});
}
boot();

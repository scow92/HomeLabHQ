// HomelabHQ SPA shell. Milestone 1: auth (first-run setup + login), multi-user
// management, and the empty tabbed layout. Devices/wizard land in later milestones.
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

let SESSION = null; // { id, username, role }

// ---- toasts (non-blocking notifications, replacing alert()) -----------------
function toast(msg, type = "info", ms = 4200) {
  const box = $("#toasts");
  if (!box) { if (type === "error") console.error(msg); return; }
  const el = document.createElement("div");
  el.className = "toast toast-" + type;
  el.setAttribute("role", type === "error" ? "alert" : "status");
  const text = document.createElement("span");
  text.className = "toast-msg";
  text.textContent = msg;
  const close = document.createElement("button");
  close.className = "toast-x";
  close.setAttribute("aria-label", "Dismiss");
  close.textContent = "×";
  const dismiss = () => {
    el.classList.add("leaving");
    el.addEventListener("animationend", () => el.remove(), { once: true });
    setTimeout(() => el.remove(), 400);
  };
  close.onclick = dismiss;
  el.append(text, close);
  box.appendChild(el);
  if (ms) setTimeout(dismiss, ms);
  return el;
}
const toastOk = (m) => toast(m, "ok");
const toastErr = (m) => toast(m, "error", 7000);

// ---- theme (dark default, light option, persisted) --------------------------
function applyTheme(theme) {
  // theme: "dark" | "light" | "auto". "auto" defers to the OS preference.
  const root = document.documentElement;
  if (theme === "auto") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", theme);
  try { localStorage.setItem("hlhq-theme", theme); } catch (_) {}
  const meta = $('meta[name="theme-color"]');
  if (meta) {
    const dark = theme === "dark" ||
      (theme === "auto" && matchMedia("(prefers-color-scheme: dark)").matches);
    meta.setAttribute("content", dark ? "#0b0f14" : "#f4f6f9");
  }
}
function initTheme() {
  let t = "auto";
  try { t = localStorage.getItem("hlhq-theme") || "auto"; } catch (_) {}
  applyTheme(t);
}
function cycleTheme() {
  let t = "auto";
  try { t = localStorage.getItem("hlhq-theme") || "auto"; } catch (_) {}
  const next = { auto: "dark", dark: "light", light: "auto" }[t] || "dark";
  applyTheme(next);
  const btn = $("#theme-btn");
  if (btn) btn.textContent = THEME_ICON[next];
  toast(`Theme: ${next}`, "info", 1500);
}
const THEME_ICON = { auto: "◐", dark: "☾", light: "☀" };
initTheme();

// ---- promise-based prompt/confirm dialog (replaces native prompt/confirm) ---
let _dialogResolve = null;
function _dialogClose(result) {
  const dlg = $("#dialog");
  if (dlg) dlg.hidden = true;
  document.body.style.removeProperty("overflow");
  // Reset transient state so the shared dialog is clean for its next use.
  const listBox = $("#dialog-list");
  if (listBox) { listBox.hidden = true; listBox.innerHTML = ""; }
  const ok = $("#dialog-ok");
  if (ok) { ok.hidden = false; ok.classList.remove("btn-danger-solid"); }
  const r = _dialogResolve; _dialogResolve = null;
  if (r) r(result);
}
function promptDialog({ title, message, value = "", placeholder = "", okLabel = "Save" }) {
  return new Promise((resolve) => {
    _dialogResolve = resolve;
    $("#dialog-title").textContent = title || "";
    const msg = $("#dialog-msg");
    msg.textContent = message || ""; msg.hidden = !message;
    $("#dialog-field").hidden = false;
    const input = $("#dialog-input");
    input.value = value; input.placeholder = placeholder;
    $("#dialog-ok").textContent = okLabel;
    $("#dialog-cancel").hidden = false;
    const dlg = $("#dialog"); dlg.hidden = false;
    document.body.style.overflow = "hidden";
    setTimeout(() => { input.focus(); input.select(); }, 30);
  });
}
function confirmDialog({ title, message, okLabel = "Confirm", danger = false }) {
  return new Promise((resolve) => {
    _dialogResolve = resolve;
    $("#dialog-title").textContent = title || "Are you sure?";
    const msg = $("#dialog-msg");
    msg.textContent = message || ""; msg.hidden = !message;
    $("#dialog-field").hidden = true;
    const ok = $("#dialog-ok");
    ok.textContent = okLabel;
    ok.classList.toggle("btn-danger-solid", danger);
    $("#dialog-cancel").hidden = false;
    const dlg = $("#dialog"); dlg.hidden = false;
    document.body.style.overflow = "hidden";
    setTimeout(() => ok.focus(), 30);
  });
}
// List picker: choose one item from a list of {value,label,sub}. Resolves the
// chosen value, or null on cancel.
function pickDialog({ title, message, items, current }) {
  return new Promise((resolve) => {
    _dialogResolve = resolve;
    $("#dialog-title").textContent = title || "Choose";
    const msg = $("#dialog-msg");
    msg.textContent = message || ""; msg.hidden = !message;
    $("#dialog-field").hidden = true;
    const listBox = $("#dialog-list");
    listBox.hidden = false;
    listBox.innerHTML = "";
    for (const it of items) {
      const row = document.createElement("button");
      row.type = "button";
      row.className = "dialog-pick" + (it.value === current ? " current" : "");
      row.innerHTML = `<span class="dp-label"></span>` +
        (it.sub ? `<span class="dp-sub"></span>` : "");
      $(".dp-label", row).textContent = it.label +
        (it.value === current ? "  (current)" : "");
      if (it.sub) $(".dp-sub", row).textContent = it.sub;
      row.onclick = () => { listBox.hidden = true; _dialogClose(it.value); };
      listBox.appendChild(row);
    }
    $("#dialog-ok").hidden = true;
    $("#dialog-cancel").hidden = false;
    const dlg = $("#dialog"); dlg.hidden = false;
    document.body.style.overflow = "hidden";
  });
}

(function bindDialog() {
  const form = $("#dialog-form");
  if (!form) return;
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const withInput = !$("#dialog-field").hidden;
    _dialogClose(withInput ? $("#dialog-input").value.trim() : true);
    $("#dialog-ok").classList.remove("btn-danger-solid");
  });
  $$("[data-dialog-cancel]").forEach((el) =>
    el.addEventListener("click", () => {
      const withInput = !$("#dialog-field").hidden;
      _dialogClose(withInput ? null : false);
      $("#dialog-ok").classList.remove("btn-danger-solid");
    }));
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("#dialog").hidden) {
      const withInput = !$("#dialog-field").hidden;
      _dialogClose(withInput ? null : false);
    }
  });
})();

async function api(path, opts = {}) {
  // Every call is bounded by a timeout so a stalled request (an unreachable
  // firewall behind a save, a wedged proxy) surfaces as an error instead of
  // leaving a button stuck on "Saving…" forever. Callers can override.
  const { timeoutMs = 30000, ...rest } = opts;
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  let res;
  try {
    res = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      signal: ctrl.signal,
      ...rest,
    });
  } catch (ex) {
    if (ex.name === "AbortError")
      throw new Error("Timed out — the server didn't respond in time.");
    throw ex;
  } finally {
    clearTimeout(timer);
  }
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
  if (name !== "logs") stopLogsTimer();
  if (name === "devices") loadDevices();
  if (name === "clients") loadClients();
  if (name === "users") loadUsers();
  if (name === "logs") loadLogs();
  if (name === "add") initWizard();
  if (name === "settings") loadNacConfig();
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

(function initThemeBtn() {
  const btn = $("#theme-btn");
  if (!btn) return;
  let t = "auto";
  try { t = localStorage.getItem("hlhq-theme") || "auto"; } catch (_) {}
  btn.textContent = THEME_ICON[t] || "◐";
  btn.addEventListener("click", cycleTheme);
})();

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
}

let SEARCH_Q = "";  // device search filter (name / host / driver)

function matchesSearch(d) {
  if (!SEARCH_Q) return true;
  const hay = `${d.name || ""} ${d.host} ${d.driverId} ${driverName(d.driverId)} ${d.transport}`.toLowerCase();
  return SEARCH_Q.split(/\s+/).every((term) => hay.includes(term));
}

function renderDeviceList() {
  const list = $("#devices-list");
  const empty = $("#devices-empty");
  const inDash = devicesIn(currentDashboard);
  const devs = inDash.filter(matchesSearch);
  list.innerHTML = "";
  for (const d of devs) list.appendChild(deviceCard(d));
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

// ---- network-wide clients view ----------------------------------------------
let CLIENTS = null;      // last-loaded {clients, sources}
let CLIENTS_Q = "";      // search filter
let CLIENTS_SORT = "hostname";  // card sort order (needs-approval always floats to top)
try { CLIENTS_SORT = localStorage.getItem("hlhq-clients-sort") || "hostname"; } catch (_) {}
// "status" was the old default (a dedicated sort mode); approval now always
// sorts to the top regardless of mode, so fold the legacy value into hostname.
if (CLIENTS_SORT === "status") CLIENTS_SORT = "hostname";

async function loadClients() {
  const body = $("#clients-body");
  if (!CLIENTS) body.innerHTML = `<p class="muted">Loading clients…</p>`;
  try {
    CLIENTS = await api("/api/clients");
    renderClients();
  } catch (ex) {
    // Don't wipe a good view on a transient refresh error — just surface it.
    if (CLIENTS) toastErr("Couldn't refresh clients: " + ex.message);
    else body.innerHTML = `<p class="auth-err">Couldn't load clients: ${ex.message}</p>`;
  }
}

function clientMatches(c) {
  if (!CLIENTS_Q) return true;
  const hay = `${c.hostname} ${c.ip} ${c.mac} ${c.kind} ${c.vendor || ""} ` +
    c.seen.map((s) => `${s.via} ${s.where}`).join(" ");
  return CLIENTS_Q.split(/\s+/).every((t) => hay.toLowerCase().includes(t));
}

function renderClients() {
  const { clients, sources, nac } = CLIENTS;
  const rows = clients.filter(clientMatches);
  const wifi = clients.filter((c) => c.kind === "wifi").length;
  const summary = $("#clients-summary");
  const errs = sources.filter((s) => s.error);
  const configured = nac && nac.configured;
  const approved = configured
    ? clients.filter((c) => c.nac === "approved").length : null;
  const needsApproval = configured
    ? clients.filter((c) => c.nac !== "approved").length : 0;
  summary.hidden = false;
  summary.textContent =
    `${clients.length} clients · ${wifi} Wi-Fi · ${clients.length - wifi} wired · ` +
    `from ${sources.length} device${sources.length === 1 ? "" : "s"}` +
    (approved != null ? ` · ${approved} approved` : "") +
    (needsApproval ? ` · ${needsApproval} need approval` : "") +
    (errs.length ? ` · ${errs.length} source(s) unreachable` : "");

  const body = $("#clients-body");
  body.innerHTML = "";
  const banner = nacBanner(nac);
  if (banner) body.appendChild(banner);

  if (!clients.length) {
    summary.hidden = true;
    body.appendChild(clientsEmptyState(sources.length));
    return;
  }
  if (CLIENTS_Q && !rows.length) {
    const p = document.createElement("p");
    p.className = "muted";
    p.textContent = `No clients match “${CLIENTS_Q}”.`;
    body.appendChild(p);
    return;
  }
  // NAC configured → a card per device (Approve/Revoke). Otherwise the classic
  // read-only table.
  body.appendChild(nac && nac.configured
    ? clientCardGrid(rows, nac) : clientsTable(rows));
}

// Read-only aggregated table (the view before access control is set up).
function clientsTable(rows) {
  const cols = [
    { key: "client", label: "Client" }, { key: "ip", label: "IP" },
    { key: "mac", label: "MAC" }, { key: "kind", label: "Type" },
    { key: "signal", label: "Signal" }, { key: "seen", label: "Seen on" },
  ];
  const wrap = document.createElement("div");
  wrap.className = "detail-table-wrap tall";
  const table = document.createElement("table");
  table.className = "detail-table clients-table";
  table.innerHTML = "<thead><tr>" +
    cols.map(() => `<th></th>`).join("") + "</tr></thead>";
  $$("th", table).forEach((th, i) => (th.textContent = cols[i].label));
  const tbody = document.createElement("tbody");
  for (const c of rows) {
    const tr = document.createElement("tr");
    for (const col of cols) {
      const td = document.createElement("td");
      if (col.key === "seen") {
        td.appendChild(seenBadges(c));
      } else {
        const cells = {
          client: c.hostname || c.ip || c.vendor || "—",
          ip: c.ip || "–", mac: c.mac,
          kind: c.kind === "wifi" ? "Wi-Fi" : "Wired",
          signal: c.signal == null ? "–" : `${c.signal} dBm`,
        };
        td.textContent = cells[col.key];
        const cls = [];
        if (/mac|ip|signal/.test(col.key)) cls.push("mono");
        if (col.key === "signal") { const s = cellSeverity("signal", c.signal); if (s) cls.push(s); }
        if (col.key === "kind" && c.kind === "wifi") cls.push("sev-accent");
        if (cls.length) td.className = cls.join(" ");
      }
      tr.appendChild(td);
    }
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  wrap.appendChild(table);
  return wrap;
}

// "Seen on" badges shared by the table and the cards.
function seenBadges(c) {
  const box = document.createElement("div");
  box.className = "seen-badges";
  for (const s of c.seen) {
    const b = document.createElement("span");
    b.className = "seen-badge";
    b.textContent = s.via + (s.where ? ` · ${s.where}` : "");
    box.appendChild(b);
  }
  return box;
}

// ---- NAC: one card per client (Approve / Revoke / Ignore) -------------------
const _cName = (c) => (c.hostname || c.ip || c.mac).toLowerCase();
const _cIpKey = (c) => (c.ip || "").split(".").map((n) => String(n).padStart(3, "0")).join(".");

function sortClients(rows, mode) {
  const arr = rows.slice();
  // Whatever the chosen field, devices needing approval always float to the top
  // so they can't be missed; the selected mode orders within each group.
  const byField = (a, b) => {
    if (mode === "ip") return _cIpKey(a).localeCompare(_cIpKey(b)) || _cName(a).localeCompare(_cName(b));
    if (mode === "mac") return a.mac.localeCompare(b.mac);
    // strongest signal first; wired (no signal) sink to the bottom.
    if (mode === "signal") return (b.signal ?? -999) - (a.signal ?? -999) || _cName(a).localeCompare(_cName(b));
    return _cName(a).localeCompare(_cName(b));  // "hostname" (default)
  };
  arr.sort((a, b) => {
    const aOk = a.nac === "approved", bOk = b.nac === "approved";
    if (aOk !== bOk) return aOk ? 1 : -1;  // needs-approval first, always
    return byField(a, b);
  });
  return arr;
}

function clientCardGrid(rows, nac) {
  const grid = document.createElement("div");
  grid.className = "cards client-cards";
  for (const c of sortClients(rows, CLIENTS_SORT)) grid.appendChild(clientCard(c, nac));
  return grid;
}

// The access point a Wi-Fi client is associated with: the "seen on" source that
// reported the (strongest) Wi-Fi signal. Falls back to any Wi-Fi source.
function clientAp(c) {
  const wifi = (c.seen || []).filter((s) => s.kind === "wifi");
  if (!wifi.length) return "";
  const withSig = wifi.filter((s) => s.signal != null);
  const pick = withSig.length
    ? withSig.reduce((a, b) => (b.signal > a.signal ? b : a))
    : wifi[0];
  return pick.via || "";
}

// Wi-Fi RSSI → severity class (matches the table's signal colouring).
function signalTone(dbm) {
  if (dbm == null) return "";
  if (dbm >= -60) return "sev-good";
  if (dbm >= -72) return "sev-warn";
  return "sev-bad";
}

function clientCard(c, nac) {
  const member = c.nac === "approved";
  const needs = !member;
  const el = document.createElement("div");
  el.className = "card client-card clickable" +
    (needs ? " needs-approval" : "") + (c.new ? " is-new" : "");
  el.title = "Click for details";
  const title = c.name || c.hostname || c.ip || c.vendor || c.mac;
  // Every client in this list is currently seen (present in ARP / associated),
  // so the dot always shows "present" (green) — grey read as offline. The
  // wired/Wi-Fi distinction is carried by the signal bar + detail panel instead.
  el.innerHTML = `
    <div class="card-row">
      <h2><span class="dot up" title="Currently connected"></span><span class="cc-name"></span></h2>
      <span class="pill nac-pill"></span>
    </div>
    <div class="muted cc-meta"></div>
    <div class="muted cc-vendor" hidden></div>
    <div class="cc-signal" hidden></div>
    <div class="cc-detail" hidden></div>
    <div class="dev-actions cc-actions"></div>`;
  $(".cc-name", el).textContent = title;

  // Status pill: Approved (green) / New (accent) / Needs approval (red).
  const pill = $(".nac-pill", el);
  if (member) { pill.textContent = "Approved"; pill.classList.add("nac-ok"); }
  else if (c.new) { pill.textContent = "New"; pill.classList.add("nac-new"); }
  else { pill.textContent = "Needs approval"; pill.classList.add("nac-blocked"); }

  // Core details on the face: IP + MAC on one line, vendor on the row below.
  $(".cc-meta", el).textContent = (c.ip ? c.ip + " · " : "") + c.mac;
  if (c.vendor) {
    const v = $(".cc-vendor", el);
    v.hidden = false;
    v.textContent = c.vendor;
  }

  // (Firewall-alias membership is shown only in the expanded detail now, not on
  // the card face — see fillClientDetail.)

  // Wi-Fi signal strength, colour-coded with a little bar, plus the AP the
  // device is associated with (the source that reported the strongest signal).
  if (c.kind === "wifi" && c.signal != null) {
    const sig = $(".cc-signal", el);
    sig.hidden = false;
    const tone = signalTone(c.signal);
    const pct = Math.max(0, Math.min(100, Math.round((c.signal + 90) / 60 * 100)));
    sig.innerHTML = `<span class="cc-sig-bar"><i></i></span>` +
      `<span class="cc-sig-val mono ${tone}"></span>` +
      `<span class="cc-sig-ap muted" hidden></span>`;
    $(".cc-sig-val", sig).textContent = `${c.signal} dBm`;
    const bar = $(".cc-sig-bar i", sig);
    bar.style.width = pct + "%";
    bar.className = tone;
    const ap = clientAp(c);
    if (ap) {
      const apEl = $(".cc-sig-ap", sig);
      apEl.hidden = false;
      apEl.textContent = ap;
      apEl.title = "Connected via " + ap;
    }
  }

  // Clicking the card body (not a button) expands more detail.
  const detail = $(".cc-detail", el);
  el.addEventListener("click", (e) => {
    if (e.target.closest(".cc-actions")) return;
    if (detail.hidden) fillClientDetail(detail, c);
    detail.hidden = !detail.hidden;
    el.classList.toggle("expanded", !detail.hidden);
  });

  // Actions (icon-only to keep the cards compact): Approve/Revoke, plus Ignore
  // for anything not yet approved, and Edit.
  const acts = $(".cc-actions", el);
  // Approve opens the edit modal so the hostname + aliases are set at approval
  // time; revoke stays a one-click action.
  const btn = iconBtn(
    member ? ICON_REVOKE : ICON_CHECK,
    member ? "Revoke access" : "Approve",
    member ? () => approveClient(c, nac, false, btn)
           : () => openClientEdit(c, { approve: true }),
    member ? "icon-btn-danger" : "icon-btn-primary");
  acts.appendChild(btn);
  if (needs) {
    const ig = iconBtn(ICON_IGNORE, "Ignore — hide until this device connects again");
    ig.onclick = () => ignoreClient(c, ig);
    acts.appendChild(ig);
  }
  acts.appendChild(iconBtn(ICON_EDIT,
    "Edit — rename, add notes, sync DNS / firewall aliases",
    () => openClientEdit(c)));
  return el;
}

// Expanded detail: where the device was seen (per-location signal), first seen.
function fillClientDetail(box, c) {
  box.innerHTML = "";
  const kv = document.createElement("div");
  kv.className = "cc-kv";
  const add = (k, v) => {
    if (v == null || v === "") return;
    const kk = document.createElement("span"); kk.className = "cc-k"; kk.textContent = k;
    const vv = document.createElement("span"); vv.className = "cc-v"; vv.textContent = v;
    kv.append(kk, vv);
  };
  add("Hostname", c.hostname);
  add("IP", c.ip);
  add("MAC", c.mac);
  add("Vendor", c.vendor);
  add("Type", c.kind === "wifi" ? "Wi-Fi" : "Wired");
  if (c.firstSeen) add("First seen", timeAgo(c.firstSeen));
  add("Notes", c.notes);
  box.appendChild(kv);

  // Firewall aliases this device belongs to (from the client scan).
  if (c.aliases && c.aliases.length) {
    const at = document.createElement("div");
    at.className = "cc-seen-title muted"; at.textContent = "Firewall aliases";
    box.appendChild(at);
    const av = document.createElement("div");
    av.className = "cc-aliases";
    for (const a of c.aliases) {
      const p = document.createElement("span");
      p.className = "pill alias-pill"; p.textContent = a.name;
      av.appendChild(p);
    }
    box.appendChild(av);
  }

  const t = document.createElement("div");
  t.className = "cc-seen-title muted"; t.textContent = "Seen on";
  box.appendChild(t);
  box.appendChild(seenBadges(c));
}

async function approveClient(c, nac, approve, btn) {
  btn.disabled = true; btn.classList.add("spinning");
  try {
    await api(`/api/devices/${nac.deviceId}/nac/approve`, {
      method: "POST", body: JSON.stringify({ mac: c.mac, approved: approve }) });
    c.nac = approve ? "approved" : "blocked";
    toastOk(approve ? `${c.hostname || c.mac} approved.`
                    : `${c.hostname || c.mac} revoked.`);
    renderClients();
  } catch (ex) {
    toastErr(ex.message);
    btn.disabled = false; btn.classList.remove("spinning");
  }
}

async function ignoreClient(c, btn) {
  btn.disabled = true; btn.classList.add("spinning");
  try {
    await api("/api/nac/ignore", { method: "POST",
      body: JSON.stringify({ mac: c.mac }) });
    CLIENTS.clients = CLIENTS.clients.filter((x) => x.mac !== c.mac);
    toastOk(`${c.hostname || c.mac} ignored — it'll reappear if it connects again.`);
    renderClients();
  } catch (ex) { toastErr(ex.message); btn.disabled = false; btn.classList.remove("spinning"); }
}

// ---- edit / approve client modal (hostname, notes, firewall aliases) --------
// One box sets the hostname: it's the device's name in the list AND, when saved,
// its static dnsmasq reservation. Approving a client opens this same modal so
// the hostname + aliases are set at approval time.
let _editClient = null;      // the client being edited
let _editAliases = [];       // [{uuid,name,type,member}] original membership
let _ceApproving = false;    // modal opened from Approve → also add to allow-list
let _ceDnsDomain = "";       // domain suffix for the dnsmasq entry (from Settings)

// Turn a typed name into a valid DNS label (lower-case, hyphen-separated).
function slugHost(s) {
  return (s || "").trim().toLowerCase()
    .replace(/[^a-z0-9-]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 63);
}

function closeClientModal() {
  $("#client-modal").hidden = true;
  _editClient = null; _editAliases = [];
}

// Render the alias tick boxes; `aliases` is [{uuid,name,member}]. Empty →
// a hint pointing to Settings.
function renderCeAliases(aliases) {
  const group = $("#ce-aliases-group"), box = $("#ce-aliases");
  group.hidden = false; box.innerHTML = "";
  if (!aliases.length) {
    const p = document.createElement("p");
    p.className = "muted"; p.style.fontSize = "12px"; p.style.margin = "0";
    p.textContent = "No aliases managed yet — add them in Settings → Network access.";
    box.appendChild(p);
    return;
  }
  for (const a of aliases) {
    const lbl = document.createElement("label"); lbl.className = "ent-item";
    const cb = document.createElement("input");
    cb.type = "checkbox"; cb.dataset.uuid = a.uuid; cb.checked = !!a.member;
    const sp = document.createElement("span"); sp.textContent = a.name || a.uuid;
    lbl.append(cb, sp); box.appendChild(lbl);
  }
}

// Open the edit modal. `opts.approve` means it was opened from the Approve
// button — saving also adds the client to the allow-list.
async function openClientEdit(c, opts = {}) {
  _editClient = c;
  _ceApproving = !!opts.approve;
  $("#ce-title").textContent = (_ceApproving ? "Approve " : "Edit ") +
    (c.hostname || c.name || c.mac);
  $("#ce-sub").textContent = (c.ip ? c.ip + " · " : "") + c.mac;
  // Single box: any name the user already set, else the name seen in the ARP /
  // DHCP scan (slugged to a valid hostname). The user can override it.
  $("#ce-host").value = c.name || slugHost(c.hostname) || "";
  $("#ce-notes").value = c.notes || "";
  $("#ce-err").hidden = true;
  $("#ce-save").textContent = _ceApproving ? "Approve" : "Save";
  $("#ce-aliases-group").hidden = true;
  $("#ce-aliases").innerHTML = "";
  $("#client-modal").hidden = false;
  $("#ce-host").focus(); $("#ce-host").select();

  const nac = (CLIENTS && CLIENTS.nac) || {};
  _ceDnsDomain = (nac.dnsSync && nac.dnsSync.domain) || "";
  updateHostHint();

  // Render alias ticks IMMEDIATELY from the scan data already in memory (each
  // client carries the aliases it belongs to), so membership shows with no delay.
  if (nac.configured) {
    const memberUuids = new Set((c.aliases || []).map((a) => a.uuid));
    _editAliases = (nac.managedAliases || []).map((a) => ({
      uuid: a.uuid, name: a.name, type: a.type, member: memberUuids.has(a.uuid) }));
    renderCeAliases(_editAliases);
  }

  // Then reconcile alias membership against the firewall (authoritative).
  try {
    const m = await api("/api/nac/client/membership", { method: "POST",
      body: JSON.stringify({ mac: c.mac, ip: c.ip || "" }) });
    if (_editClient !== c) return;  // modal closed/re-opened meanwhile
    if (m.configured) {
      _editAliases = m.aliases || _editAliases;
      renderCeAliases(_editAliases);
    }
    if (m.dnsSync && m.dnsSync.domain != null) { _ceDnsDomain = m.dnsSync.domain; updateHostHint(); }
  } catch (ex) {
    // Hostname + aliases still work if the firewall read fails; only warn.
    if (_editClient === c) toastErr("Couldn't refresh alias state: " + ex.message);
  }
}

// Live preview of the DNS name the hostname will be published as.
function updateHostHint() {
  const hint = $("#ce-host-hint");
  const host = slugHost($("#ce-host").value);
  if (!host) { hint.hidden = true; return; }
  const fqdn = _ceDnsDomain ? `${host}.${_ceDnsDomain}` : host;
  hint.textContent = `Saved as a static DNS reservation: ${fqdn} → ${_editClient && _editClient.ip || "this device"}`;
  hint.hidden = false;
}

$("#ce-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const c = _editClient;
  if (!c) return;
  const approving = _ceApproving;
  const err = $("#ce-err"); err.hidden = true;
  const save = $("#ce-save"); save.disabled = true;
  save.textContent = approving ? "Approving…" : "Saving…";
  try {
    const host = slugHost($("#ce-host").value);
    const nac = (CLIENTS && CLIENTS.nac) || {};
    // 1) Approve first (add the MAC to the allow-list) when opened from Approve.
    if (approving && nac.deviceId) {
      await api(`/api/devices/${nac.deviceId}/nac/approve`, {
        method: "POST", body: JSON.stringify({ mac: c.mac, approved: true }) });
      c.nac = "approved";
    }
    // 2) Save the hostname (published as a static dnsmasq reservation), notes,
    //    and any alias-membership changes.
    const aliasChanges = {};
    $$("#ce-aliases input[data-uuid]").forEach((cb) => {
      const orig = _editAliases.find((a) => a.uuid === cb.dataset.uuid);
      if (orig && !!orig.member !== cb.checked) aliasChanges[cb.dataset.uuid] = cb.checked;
    });
    const body = {
      mac: c.mac, ip: c.ip || "",
      name: host, notes: $("#ce-notes").value,
      hostname: host,
      syncDns: host ? true : null,   // publish the hostname; leave DNS alone if blank
      aliasChanges,
    };
    const r = await api("/api/nac/client", { method: "POST", body: JSON.stringify(body) });
    // Reflect saved values locally so the list updates without a full reload.
    c.name = r.name; c.notes = r.notes;
    closeClientModal();
    toastOk(approving ? `${host || c.mac} approved.` : "Saved.");
    renderClients();
  } catch (ex) {
    err.textContent = ex.message; err.hidden = false;
  } finally {
    // Always restore the button so it can never stick on "Saving…" — whether
    // the save succeeded, errored, or timed out.
    save.disabled = false; save.textContent = approving ? "Approve" : "Save";
  }
});

// Live DNS-name preview as the hostname is typed.
$("#ce-host").addEventListener("input", updateHostHint);

document.addEventListener("click", (e) => {
  if (e.target.closest("[data-close-client]")) closeClientModal();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("#client-modal").hidden) closeClientModal();
});

// Banner above the list: the setup CTA when NAC isn't configured, or the
// enforcement master switch once it is. Returns null when there's nothing to
// show (no NAC-capable device on the network).
function nacBanner(nac) {
  if (!nac || !nac.deviceId) return null;
  const box = document.createElement("div");
  box.className = "nac-banner card";
  if (!nac.configured) {
    box.innerHTML = `
      <div class="nac-b-main">
        <h2>Set up Network Access Control</h2>
        <p class="muted">Turn this list into an allow-list: approve the devices
          you trust, and (when you switch enforcement on) everything else is
          blocked at <strong class="nac-dev"></strong>. Nothing changes until you
          approve devices and enable enforcement.</p>
      </div>
      <button class="btn btn-primary nac-setup-btn">Set up</button>`;
    $(".nac-dev", box).textContent = nac.deviceName || "the firewall";
    $(".nac-setup-btn", box).onclick = () => nacSetup(nac);
    return box;
  }
  // Configured against an existing alias (membership-only, the user's own rule
  // enforces): no status box — it carried no actionable info. Cards alone show
  // approved/needs-approval state.
  if (nac.managedExternally) return null;
  // Managed mode → we own the deny rule, so show the enforcement toggle.
  box.classList.toggle("enforcing", !!nac.enforced);
  box.innerHTML = `
    <div class="nac-b-main">
      <h2>Access control <span class="nac-alias pill"></span></h2>
      <p class="muted nac-b-sub"></p>
    </div>
    <div class="nac-b-switch">
      <span class="nac-sw-label"></span>
      <button type="button" class="fw-switch nac-enforce" role="switch"><span class="fw-knob"></span></button>
    </div>`;
  $(".nac-alias", box).textContent = nac.alias || "";
  $(".nac-b-sub", box).textContent = nac.enforced
    ? "Enforcement is ON — only approved devices have network access."
    : "Enforcement is OFF — every device is allowed. Approve your devices, then turn it on.";
  $(".nac-sw-label", box).textContent = nac.enforced ? "Enforcing" : "Off";
  const sw = $(".nac-enforce", box);
  sw.classList.toggle("on", !!nac.enforced);
  sw.setAttribute("aria-checked", String(!!nac.enforced));
  sw.onclick = () => toggleEnforcement(nac, !nac.enforced, sw);
  return box;
}

async function toggleEnforcement(nac, on, sw) {
  if (on) {
    const ok = await confirmDialog({ title: "Turn on enforcement?",
      message: "Default-deny goes live: any device that isn't approved loses " +
        "network access immediately. Make sure everything you rely on is " +
        "approved first.", okLabel: "Turn on", danger: true });
    if (!ok) return;
  }
  sw.disabled = true;
  try {
    const r = await api(`/api/devices/${nac.deviceId}/nac/enforcement`, {
      method: "POST", body: JSON.stringify({ enabled: on }) });
    CLIENTS.nac.enforced = !!(r.device && r.device.nac && r.device.nac.enforced);
    toastOk(CLIENTS.nac.enforced
      ? "Enforcement on — only approved devices have access."
      : "Enforcement off — all devices allowed.");
    renderClients();
  } catch (ex) {
    toastErr(ex.message);
    sw.disabled = false;
  }
}

// Setup entry point: choose whether to reuse an existing firewall alias (safest
// when you already run one, e.g. Network Manager) or create a fresh one.
async function nacSetup(nac, deviceId) {
  const devId = deviceId || (nac && nac.deviceId);
  const mode = await pickDialog({ title: "Set up Network Access Control",
    message: "Control which devices are allowed on your network.",
    items: [
      { value: "existing", label: "Use an existing firewall alias",
        sub: "recommended if you already run one (e.g. Network Manager)" },
      { value: "create", label: "Create a new allow-list",
        sub: "fresh setup — creates the alias and rules for you" },
    ] });
  if (!mode) return;
  return mode === "existing" ? nacSetupExisting(devId) : nacSetupCreate(devId);
}

// Reuse an existing alias — membership-only. HomeLabHQ adds/removes devices in
// the alias you pick; your own firewall rule keeps enforcing it, so nothing can
// be cut off by turning this on.
async function nacSetupExisting(devId) {
  let aliases;
  try {
    aliases = (await api(`/api/devices/${devId}/nac/aliases`)).aliases || [];
  } catch (ex) { toastErr("Couldn't read aliases: " + ex.message); return; }
  if (!aliases.length) {
    toastErr("No aliases found on the firewall — choose “Create a new allow-list” instead.");
    return;
  }
  const pick = await pickDialog({ title: "Choose the alias to manage",
    message: "HomeLabHQ will add or remove devices in this alias. Everything " +
      "already in it stays approved.",
    items: aliases.map((a) => ({ value: a.uuid, label: a.name,
      sub: [a.type, a.description].filter(Boolean).join(" · ") })) });
  if (!pick) return;
  try {
    await api(`/api/devices/${devId}/nac/setup`, { method: "POST",
      body: JSON.stringify({ mode: "existing", existingUuid: pick }) });
    toastOk("Access control linked to your existing alias.");
    CLIENTS = null; loadClients();
    switchTab("clients");
  } catch (ex) { toastErr(ex.message); }
}

// Guided setup for a brand-new allow-list: name the alias, pick the interface,
// choose whether to seed it with everything currently online.
async function nacSetupCreate(devId) {
  const alias = await promptDialog({ title: "Create a new allow-list",
    message: "Name the firewall alias that will hold your approved devices " +
      "(letters, digits and underscore).",
    value: "HLHQ_NAC", okLabel: "Next" });
  if (alias == null) return;
  if (!/^[A-Za-z][A-Za-z0-9_]{0,31}$/.test(alias.trim())) {
    toastErr("Alias must start with a letter; letters, digits and underscore only.");
    return;
  }
  let ifaces;
  try {
    ifaces = (await api(`/api/devices/${devId}/nac/interfaces`)).interfaces || [];
  } catch (ex) { toastErr("Couldn't read interfaces: " + ex.message); return; }
  if (!ifaces.length) { toastErr("No interfaces available on the firewall."); return; }
  const iface = await pickDialog({ title: "Which network to protect?",
    message: "The access rule attaches to this interface — usually your LAN.",
    items: ifaces.map((i) => ({ value: i.value, label: i.label, sub: i.value })) });
  if (!iface) return;
  const seedChoice = await pickDialog({ title: "Seed the allow-list?",
    message: "Approving the devices already online means turning enforcement on " +
      "later won't cut anyone off.",
    items: [
      { value: "seed", label: "Approve all current devices", sub: "recommended" },
      { value: "empty", label: "Start empty", sub: "you'll approve devices yourself" },
    ] });
  if (!seedChoice) return;
  try {
    const r = await api(`/api/devices/${devId}/nac/setup`, { method: "POST",
      body: JSON.stringify({ alias: alias.trim(), interface: iface,
        seedExisting: seedChoice === "seed" }) });
    toastOk(`Access control ready${r.seeded ? ` — ${r.seeded} devices approved` : ""}. ` +
      "Enforcement is off until you turn it on.");
    CLIENTS = null; loadClients();
    switchTab("clients");
  } catch (ex) { toastErr(ex.message); }
}

// Onboarding for the Clients view — this is generic, so anyone running the tool
// on their own network populates it just by adding an AP or managed switch.
function clientsEmptyState(sourceCount) {
  const box = document.createElement("div");
  box.className = "empty";
  box.innerHTML = `
    <div class="empty-mark">◎</div>
    <p><strong>No clients to show yet.</strong></p>
    <p class="muted">The Clients view automatically aggregates every device
      seen by the access points and managed switches you add — hostname, IP,
      MAC, signal and where each one is connected.</p>
    <p class="muted">${sourceCount
      ? "Your client sources are reachable but reported nothing yet — try Refresh in a moment."
      : "Add a Wi-Fi access point or a managed switch to get started."}</p>
    <button class="btn btn-primary" data-goto="add">Add a device</button>`;
  return box;
}

(function bindClients() {
  const input = $("#clients-search");
  const clear = $("#clients-search-clear");
  if (input) {
    input.addEventListener("input", () => {
      CLIENTS_Q = input.value.trim().toLowerCase();
      clear.hidden = !input.value;
      if (CLIENTS) renderClients();
    });
    clear.addEventListener("click", () => {
      input.value = ""; CLIENTS_Q = ""; clear.hidden = true;
      if (CLIENTS) renderClients(); input.focus();
    });
  }
  const sort = $("#clients-sort");
  if (sort) {
    sort.value = CLIENTS_SORT;
    sort.addEventListener("change", () => {
      CLIENTS_SORT = sort.value;
      try { localStorage.setItem("hlhq-clients-sort", CLIENTS_SORT); } catch (_) {}
      if (CLIENTS) renderClients();
    });
  }
  const refresh = $("#clients-refresh");
  if (refresh) refresh.addEventListener("click", async () => {
    // Keep the current cards on screen and just swap in fresh data (a fresh ARP
    // + DHCP-lease read of the firewall), so refresh never blanks the view.
    const t = refresh.textContent;
    refresh.disabled = true; refresh.textContent = "↻ Scanning…";
    try { await loadClients(); } finally {
      refresh.disabled = false; refresh.textContent = t;
    }
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

async function moveDeviceToDashboard(devId, dashboardId) {
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

function timeAgo(ts) {
  if (!ts) return "never";
  const s = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (s < 60) return s + "s ago";
  if (s < 3600) return Math.floor(s / 60) + "m ago";
  return Math.floor(s / 3600) + "h ago";
}

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

function renderState(container, res) {
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
      <button class="icon-btn details" title="Details" aria-label="Details">${ICON_INFO}</button>
      <button class="icon-btn check" title="Sync now" aria-label="Sync now">${ICON_SYNC}</button>
      <button class="icon-btn rename" title="Rename" aria-label="Rename">${ICON_EDIT}</button>
      <button class="icon-btn icon-btn-danger del" title="Remove" aria-label="Remove">${ICON_TRASH}</button>
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
  $(".pill", el).textContent = driverName(d.driverId);
  $(".pill", el).title = d.driverId;
  $(".host", el).textContent = `${d.host}${d.port ? ":" + d.port : ""}`;
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
    const btn = e.currentTarget; btn.disabled = true; btn.classList.add("spinning");
    try {
      const r = await api(`/api/devices/${d.id}/state`);
      dot.className = "dot " + (Object.keys(r.values || {}).length ? "up" : "down");
      renderState(state, r);
      updated.textContent = "updated just now";
    } catch (ex) {
      dot.className = "dot down";
      state.hidden = false;
      state.innerHTML = `<span class="dev-err">${ex.message}</span>`;
    } finally { btn.disabled = false; btn.classList.remove("spinning"); }
  };

  $(".details", el).onclick = () => openDevice(d);

  $(".rename", el).onclick = () => renameDevice(d);

  $(".del", el).onclick = async () => {
    const ok = await confirmDialog({ title: `Remove “${d.name || d.host}”?`,
      message: "This stops monitoring it and deletes its stored history.",
      okLabel: "Remove", danger: true });
    if (!ok) return;
    try {
      await api(`/api/devices?id=${encodeURIComponent(d.id)}`, { method: "DELETE" });
      loadDevices();
      toastOk("Device removed.");
    }
    catch (ex) { toastErr(ex.message); }
  };
  return el;
}

// ---- device detail modal ----------------------------------------------------
// Driver ids → short, human names for the overview cards (the wire id like
// "keeplink.switch" reads poorly). Unknown ids fall back to a humanized id.
const DRIVER_NAMES = {
  "keeplink.switch": "Keeplink switch", "openwrt.ubus": "OpenWrt",
  "zyxel.ap": "Zyxel AP", "opnsense.firewall": "OPNsense",
  "pfsense.firewall": "pfSense", "unifi.network": "UniFi",
  "proxmox.ve": "Proxmox", "synology.dsm": "Synology", "truenas.system": "TrueNAS",
  "firewalla.msp": "Firewalla", "qnap.qts": "QNAP", "mikrotik.routeros": "MikroTik",
  "generic.http": "HTTP device", "generic.api": "API device",
  "generic.linux-ssh": "SSH host", "generic.snmp": "SNMP device",
  "snmp.switch": "SNMP switch",
};
function driverName(id) {
  if (DRIVER_NAMES[id]) return DRIVER_NAMES[id];
  return (id || "").split(/[.\-_]/).map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ") || "device";
}

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
  "release", "hostname", "kernel", "board", "board_name", "ports_up"]);
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

// Format a throughput given in BYTES/second as a bits/second rate — the network
// convention (a "100 Mbps" link, not "12.5 MB/s"). Decimal (1000) units.
function fmtBitsRate(bytesPerSec) {
  if (bytesPerSec == null || isNaN(bytesPerSec)) return "–";
  let bits = Math.abs(bytesPerSec) * 8;
  const u = ["bps", "Kbps", "Mbps", "Gbps", "Tbps"];
  let i = 0;
  while (bits >= 1000 && i < u.length - 1) { bits /= 1000; i++; }
  const v = bytesPerSec < 0 ? -bits : bits;
  return `${v.toFixed(v >= 100 || i === 0 ? 0 : 1)} ${u[i]}`;
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
  const sub = $("#dm-sub");
  sub.textContent = `${d.host}${d.port ? ":" + d.port : ""} · ${d.transport} · `;
  const drvLink = document.createElement("button");
  drvLink.className = "linkish";
  drvLink.textContent = driverName(d.driverId);
  drvLink.title = "Change driver (" + d.driverId + ")";
  drvLink.onclick = () => changeDriver(d);
  sub.appendChild(drvLink);
  $("#dm-rename").onclick = () => renameDevice(d);
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
    } catch (ex) { toastErr(ex.message); dsel.value = d.dashboardId || ""; }
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
           ifHistory: data.ifHistory || {}, actions: data.actions || [],
           supportsBinding: !!data.supportsBinding };
    ifEdit = false;
    const anyVal = DM.entities.some((e) => "value" in e && !e.error);
    dot.className = "dot " + (DM.device.state && DM.device.state.online ? "up"
      : anyVal ? "up" : "down");
    $("#dm-customize").hidden = false;
    $("#dm-customize").textContent = "Customize";
    renderDetail(body);
    startDetailLive(d.id);
  } catch (ex) {
    DM = null;
    body.innerHTML = `<p class="auth-err">Couldn't load details: ${ex.message}</p>`;
  }
}

// Real-time: while the detail modal is open, re-fetch the device every 20s and
// repaint its charts in place (no DOM rebuild) so throughput/values stay live.
let LIVE_TIMER = null;
function stopDetailLive() { clearInterval(LIVE_TIMER); LIVE_TIMER = null; }
function startDetailLive(id) {
  stopDetailLive();
  LIVE_TIMER = setInterval(async () => {
    const modal = $("#device-modal");
    if (!DM || !DM.device || DM.device.id !== id || !modal || modal.hidden) {
      return stopDetailLive();
    }
    try {
      const data = await api(`/api/devices/${id}/detail`);
      DM.history = data.history || DM.history;
      DM.ifHistory = data.ifHistory || DM.ifHistory;
      DM.entities = data.entities || DM.entities;
      DM.detail = data.detail || DM.detail;
      refreshCharts();
    } catch (_) { /* transient; try again next tick */ }
  }, 20000);
}

// Rename a device (from a card or the detail modal). Empty clears the custom
// name so it falls back to the host. Refreshes the overview after saving.
async function renameDevice(d) {
  const next = await promptDialog({ title: "Rename device",
    message: `Currently “${d.name || d.host}”. Leave blank to use the host.`,
    value: d.name || "", placeholder: d.host });
  if (next == null) return;
  try {
    const r = await api(`/api/devices/${d.id}`, {
      method: "PATCH", body: JSON.stringify({ name: next.trim() }) });
    d.name = (r && r.device ? r.device.name : next.trim()) || null;
    const title = $("#dm-title");
    if (title && DM && DM.device && DM.device.id === d.id) {
      DM.device.name = d.name;
      title.textContent = d.name || d.host;
    }
    loadDevices();
    toastOk("Device renamed.");
  } catch (ex) { toastErr(ex.message); }
}

// Re-point a device at a different curated driver — for a device that was
// mis-detected (e.g. a Keeplink switch added as generic.http). Works even when
// the device is offline, since it only rewrites the stored driver id.
async function changeDriver(d) {
  let list;
  try {
    list = (await api(`/api/drivers?transport=${encodeURIComponent(d.transport)}`)).drivers;
  } catch (ex) { toastErr(ex.message); return; }
  const chosenId = await pickDialog({
    title: "Change driver",
    message: `How should “${d.name || d.host}” (${d.transport}) be read?`,
    current: d.driverId,
    items: list.map((x) => ({ value: x.id, label: x.displayName, sub: x.id })),
  });
  if (chosenId == null || chosenId === d.driverId) return;
  const chosen = list.find((x) => x.id === chosenId);
  try {
    await api(`/api/devices/${d.id}`, {
      method: "PATCH", body: JSON.stringify({ driverId: chosen.id }) });
    d.driverId = chosen.id;
    toastOk(`Driver changed to ${chosen.displayName}.`);
    loadDevices();
    openDevice(d);
  } catch (ex) { toastErr(ex.message); }
}

function closeDevice() {
  stopDetailLive();
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

// Device-level actions (reboot, …) as buttons. Each POSTs to the action
// endpoint; danger actions confirm first and use the destructive style.
function actionsSection() {
  const s = section("Actions");
  const row = document.createElement("div");
  row.className = "action-row";
  for (const a of DM.actions) {
    const btn = document.createElement("button");
    btn.className = "btn btn-sm " + (a.danger ? "btn-danger" : "btn-ghost");
    btn.textContent = a.label || a.name;
    btn.onclick = async () => {
      if (a.confirm) {
        const ok = await confirmDialog({ title: `${a.label || a.name}?`,
          message: `On “${DM.device.name || DM.device.host}”.`,
          okLabel: a.label || "Confirm", danger: !!a.danger });
        if (!ok) return;
      }
      const orig = btn.textContent; btn.disabled = true; btn.textContent = "Working…";
      try {
        const r = await api(`/api/devices/${DM.device.id}/action`, {
          method: "POST", body: JSON.stringify({ action: a.name, args: {} }) });
        toastOk((r && r.message) || `${a.label || a.name} done.`);
      } catch (ex) { toastErr(ex.message); }
      finally { btn.disabled = false; btn.textContent = orig; }
    };
    row.appendChild(btn);
  }
  s.appendChild(row);
  return s;
}

// Compact inline-SVG icons for the firewall row actions (saves the width two
// text buttons took). Stroke uses currentColor so hover recolours them.
const ICON_EDIT = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4 12.5-12.5z"/></svg>`;
const ICON_TRASH = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><line x1="10" y1="11" x2="10" y2="17"/><line x1="14" y1="11" x2="14" y2="17"/></svg>`;
// Card-action icons (device + client cards), same feather-style stroke set.
const ICON_INFO = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>`;
const ICON_SYNC = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>`;
const ICON_CHECK = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>`;
const ICON_REVOKE = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="4.93" y1="4.93" x2="19.07" y2="19.07"/></svg>`;
const ICON_IGNORE = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>`;
// Compact icon-only action button used on both device and client cards.
function iconBtn(svg, label, onclick, extra) {
  const b = document.createElement("button");
  b.type = "button";
  b.className = "icon-btn" + (extra ? " " + extra : "");
  b.innerHTML = svg;
  b.title = label;
  b.setAttribute("aria-label", label);
  if (onclick) b.onclick = onclick;
  return b;
}
function fwIconBtn(svg, label, onclick, extra) {
  const b = document.createElement("button");
  b.type = "button";
  b.className = "fw-icon" + (extra ? " " + extra : "");
  b.innerHTML = svg;
  b.title = label;
  b.setAttribute("aria-label", label);
  b.onclick = onclick;
  return b;
}

// OPNsense firewall rules, mirroring Network Manager's toggle list: enable /
// disable a filter rule (applied live), rename its label, add rules from the
// full firewall list, or remove them from this section. Never deletes a rule
// on the firewall.
function firewallSection() {
  const s = section("Firewall rules");
  const dev = DM.device;
  const fw = DM.detail.firewall || {};
  let rules = (fw.rules || []).map((r) => ({ ...r }));

  const sub = document.createElement("p");
  sub.className = "cz-sub";
  sub.textContent = "Enable or disable OPNsense filter rules (applied live). " +
    "Rename changes the label here only; rules are never deleted.";
  s.appendChild(sub);

  const list = document.createElement("div");
  list.className = "fw-list";
  s.appendChild(list);

  async function saveManaged(next) {
    const res = await api(`/api/devices/${dev.id}/firewall/rules`, {
      method: "POST",
      body: JSON.stringify({ rules: next.map((r) =>
        ({ uuid: r.uuid, name: r.name, renamed: !!r.renamed })) }),
    });
    rules = res.rules || next;
    if (DM.detail.firewall) DM.detail.firewall.rules = rules;
    renderList();
  }

  async function toggleRule(r, sw) {
    const desired = !r.enabled;
    sw.disabled = true;
    try {
      const res = await api(`/api/devices/${dev.id}/firewall/toggle`, {
        method: "POST", body: JSON.stringify({ uuid: r.uuid, enabled: desired }) });
      r.enabled = !!res.enabled;
      toastOk(`Rule ${r.enabled ? "enabled" : "disabled"}.`);
    } catch (ex) { toastErr(ex.message); }
    finally { sw.disabled = false; renderList(); }
  }

  // The name shown for a rule: the live OPNsense rule name by default, or the
  // user's own label once they've renamed it here.
  function ruleTitle(r) {
    return (r.renamed && r.name) ? r.name : (r.descr || r.name);
  }

  async function renameRule(i) {
    const name = await promptDialog({ title: "Rename rule", value: ruleTitle(rules[i]),
      okLabel: "Save",
      message: "This label is stored here only — the rule on the firewall keeps its own name." });
    if (name == null) return;
    const label = name.trim();
    // A blank label clears the override and falls back to the live rule name.
    const next = rules.map((r, j) => j === i
      ? { ...r, name: label || r.descr || r.name, renamed: !!label }
      : r);
    try { await saveManaged(next); toastOk(label ? "Renamed." : "Reset to firewall name."); }
    catch (ex) { toastErr(ex.message); }
  }

  async function removeRule(i) {
    const ok = await confirmDialog({ title: "Remove from list?",
      message: `“${rules[i].name}” stays on the firewall — this only removes it from this section.`,
      okLabel: "Remove" });
    if (!ok) return;
    const next = rules.filter((_, j) => j !== i);
    try { await saveManaged(next); toastOk("Removed."); } catch (ex) { toastErr(ex.message); }
  }

  function renderList() {
    list.innerHTML = "";
    if (fw.error) {
      list.innerHTML = `<p class="muted" style="margin:0;font-size:12px"></p>`;
      $(".muted", list).textContent = "Couldn't read rules: " + fw.error;
      return;
    }
    if (!rules.length) {
      list.innerHTML = `<p class="muted" style="margin:0;font-size:12px">No rules yet. Add one below.</p>`;
      return;
    }
    for (const [i, r] of rules.entries()) {
      const row = document.createElement("div");
      row.className = "fw-row";
      const sw = document.createElement("button");
      sw.type = "button";
      sw.className = "fw-switch" + (r.enabled ? " on" : "") +
        (r.enabled == null ? " unknown" : "");
      sw.setAttribute("role", "switch");
      sw.setAttribute("aria-checked", String(!!r.enabled));
      sw.disabled = r.enabled == null;
      sw.title = r.enabled == null ? "State unknown"
        : (r.enabled ? "Enabled — click to disable" : "Disabled — click to enable");
      sw.innerHTML = `<span class="fw-knob"></span>`;
      sw.onclick = () => toggleRule(r, sw);

      const nm = document.createElement("div");
      nm.className = "fw-name";
      const title = document.createElement("span");
      title.className = "fw-title"; title.textContent = ruleTitle(r);
      nm.appendChild(title);
      if (r.error) {
        const e = document.createElement("span");
        e.className = "fw-sub err"; e.textContent = r.error;
        nm.appendChild(e);
      } else if (r.renamed && r.descr && r.descr !== r.name) {
        // User gave it a custom label — show the real firewall name underneath
        // so it's clear which rule this maps to.
        const d = document.createElement("span");
        d.className = "fw-sub";
        const tag = document.createElement("span");
        tag.className = "fw-src"; tag.textContent = "firewall";
        d.append(tag, document.createTextNode(r.descr));
        nm.appendChild(d);
      }

      const acts = document.createElement("div");
      acts.className = "fw-acts";
      const ren = fwIconBtn(ICON_EDIT, "Rename", () => renameRule(i));
      const rm = fwIconBtn(ICON_TRASH, "Remove from list",
        () => removeRule(i), "fw-icon-danger");
      acts.append(ren, rm);

      row.append(sw, nm, acts);
      list.appendChild(row);
    }
  }

  const addRow = document.createElement("div");
  addRow.className = "fw-add";
  const addBtn = document.createElement("button");
  addBtn.className = "btn btn-primary btn-sm"; addBtn.textContent = "Add rule";
  addBtn.onclick = async () => {
    const orig = addBtn.textContent;
    addBtn.disabled = true; addBtn.textContent = "Loading…";
    try {
      const data = await api(`/api/devices/${dev.id}/firewall/all`);
      const have = new Set(rules.map((r) => r.uuid));
      const items = (data.rules || []).map((r) => ({
        value: r.uuid, label: r.label,
        sub: (r.enabled ? "enabled" : "disabled") +
          (have.has(r.uuid) ? " · already added" : ""),
      }));
      const pick = await pickDialog({ title: "Add a firewall rule",
        message: "Pick a rule to manage in this section.", items });
      if (!pick) return;
      if (have.has(pick)) { toast("Already in the list.", "warn"); return; }
      const chosen = (data.rules || []).find((r) => r.uuid === pick);
      await saveManaged([...rules, { uuid: pick, name: chosen ? chosen.label : pick }]);
      toastOk("Rule added.");
    } catch (ex) { toastErr(ex.message); }
    finally { addBtn.disabled = false; addBtn.textContent = orig; }
  };
  addRow.appendChild(addBtn);

  renderList();
  s.appendChild(addRow);
  return s;
}

// Threshold-alert editor for a device: list existing rules and add new ones.
// Rules fire a push notification when a numeric sensor crosses the threshold.
function alertsSection() {
  const s = section("Alerts");
  const dev = DM.device;
  dev.alerts = dev.alerts || [];
  // Numeric sensors are the alertable entities; prefer ones with a live value.
  const numeric = DM.entities.filter((e) =>
    e.kind === "sensor" && !DETAIL_KEYS.has(e.key) &&
    (typeof e.value === "number" || /cpu|mem|clients|ports_up|poe|signal|rssi|load|temp|errors|count/i.test(e.key)));
  const nameFor = (k) => {
    const e = DM.entities.find((x) => x.key === k);
    return e ? e.name : labelFor(k);
  };

  const list = document.createElement("div");
  list.className = "alerts-list";
  const renderList = () => {
    list.innerHTML = "";
    if (!dev.alerts.length) {
      list.innerHTML = `<p class="muted" style="margin:0;font-size:12px">No alerts. Add one below to get a push notification when a value crosses a threshold.</p>`;
      return;
    }
    for (const [i, r] of dev.alerts.entries()) {
      const row = document.createElement("div");
      row.className = "alert-row";
      const sign = r.op === "above" ? ">" : "<";
      row.innerHTML = `<span class="a-txt"></span><button class="btn btn-ghost btn-sm a-del">Remove</button>`;
      $(".a-txt", row).textContent = `${nameFor(r.key)} ${sign} ${r.value}`;
      $(".a-del", row).onclick = async () => {
        const next = dev.alerts.filter((_, j) => j !== i);
        await saveAlerts(next);
      };
      list.appendChild(row);
    }
  };
  renderList();
  s.appendChild(list);

  // Add-rule form.
  const form = document.createElement("div");
  form.className = "alert-add";
  const entSel = document.createElement("select");
  if (!numeric.length) entSel.appendChild(new Option("(no numeric sensors)", ""));
  for (const e of numeric) entSel.appendChild(new Option(e.name, e.key));
  const opSel = document.createElement("select");
  opSel.appendChild(new Option("rises above", "above"));
  opSel.appendChild(new Option("drops below", "below"));
  const valIn = document.createElement("input");
  valIn.type = "number"; valIn.step = "any"; valIn.placeholder = "value";
  const addBtn = document.createElement("button");
  addBtn.className = "btn btn-primary btn-sm"; addBtn.textContent = "Add alert";
  addBtn.onclick = async () => {
    const key = entSel.value;
    if (!key) return toast("No numeric sensor to alert on.", "warn");
    if (valIn.value === "") return toast("Enter a threshold value.", "warn");
    const next = [...dev.alerts, { key, op: opSel.value,
      value: Number(valIn.value), label: nameFor(key) }];
    await saveAlerts(next);
    valIn.value = "";
  };
  form.append(entSel, opSel, valIn, addBtn);
  s.appendChild(form);

  async function saveAlerts(next) {
    try {
      const r = await api(`/api/devices/${dev.id}`, {
        method: "PATCH", body: JSON.stringify({ alerts: next }) });
      dev.alerts = (r.device && r.device.alerts) || next;
      renderList();
      toastOk("Alerts updated.");
    } catch (ex) { toastErr(ex.message); }
  }
  return s;
}

function renderDetail(body) {
  body.innerHTML = "";
  resetCharts();  // drop chart registrations from the previous render
  const { entities, detail, history } = DM;
  // Entities the driver surfaces elsewhere (e.g. Zyxel client counts/channels
  // live in the Radios table) are hidden from the generic details/metrics.
  const hide = new Set(detail.hideEntities || []);
  // Numeric sensors a driver wants shown as a plain number under Device details
  // (not a metric graph) — e.g. Proxmox node counts / uptime.
  const asDetail = new Set(detail.detailKeys || []);
  const enabled = entities.filter(
    (e) => e.enabled && e.kind === "sensor" && !hide.has(e.key));

  // Partition enabled sensors: identity keys (uptime, model, …) are always
  // "device details"; otherwise numbers/booleans are metrics (value + chart)
  // and strings are details.
  const details = [];  // {label, value}
  const metrics = [];  // entity records
  for (const e of enabled) {
    if (DETAIL_KEYS.has(e.key) || asDetail.has(e.key)) {
      let v = e.value;
      if (/uptime/.test(e.key) && typeof v === "number") v = fmtUptime(v);
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

  // --- Usage donuts (memory breakdown, pool capacity …) ---
  if ((detail.charts || []).length) {
    const s = section("Usage");
    const grid = document.createElement("div");
    grid.className = "donuts";
    for (const spec of detail.charts) grid.appendChild(donutCard(spec));
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
      s.appendChild(
        t.layout === "clients" ? clientsList(t)
        : t.layout === "radios" ? radiosTable(t)
        : detailTable(t));
      body.appendChild(s);
    }
  }

  // --- Firewall rules (OPNsense: toggle / rename / add, never delete) ---
  if (detail.firewall && detail.firewall.supported) {
    body.appendChild(firewallSection());
  }

  // --- Roam-binding toggle (APs that can pin clients) ---
  if (DM.supportsBinding) body.appendChild(bindingSection());

  // --- Device actions (reboot, …) ---
  if ((DM.actions || []).length) body.appendChild(actionsSection());

  // --- Alerts (threshold rules → push notifications) ---
  body.appendChild(alertsSection());

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

// ---- usage donuts (pie charts) ----------------------------------------------
// A driver's detail.charts[] entries render here as SVG donuts: slices sized by
// value, a used-% in the middle, and a legend with each slice's size + share.
const DONUT_TONE = { used: "--accent", cache: "--amber", free: "--muted" };
const SVG_NS = "http://www.w3.org/2000/svg";

function donutColor(tone) {
  return cssVar(DONUT_TONE[tone] || "--accent");
}

// Re-read this spec's latest values from live detail data (matched by title) so
// the 20s refresh repaints memory/pool donuts in place as usage shifts.
function liveDonutSpec(spec) {
  const list = (DM && DM.detail && DM.detail.charts) || [];
  return list.find((c) => c.title === spec.title) || spec;
}

function donutCard(spec) {
  const card = document.createElement("div");
  card.className = "donut-card";
  const render = () => {
    const s = liveDonutSpec(spec);
    card.innerHTML = "";
    const head = document.createElement("div");
    head.className = "donut-head";
    head.textContent = s.title;
    const wrap = document.createElement("div");
    wrap.className = "donut-wrap";
    wrap.append(donutSvg(s), donutLegend(s));
    card.append(head, wrap);
  };
  render();
  CHART_REG.push({ refresh: render });  // repaint on the live tick
  return card;
}

function donutSvg(s) {
  const size = 120, r = 48, cx = size / 2, cy = size / 2, sw = 18;
  const C = 2 * Math.PI * r;
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", `0 0 ${size} ${size}`);
  svg.setAttribute("class", "donut-svg");
  const circle = (stroke, dash, offset, extra = {}) => {
    const c = document.createElementNS(SVG_NS, "circle");
    c.setAttribute("cx", cx); c.setAttribute("cy", cy); c.setAttribute("r", r);
    c.setAttribute("fill", "none");
    c.setAttribute("stroke", stroke);
    c.setAttribute("stroke-width", sw);
    if (dash != null) c.setAttribute("stroke-dasharray", dash);
    if (offset != null) c.setAttribute("stroke-dashoffset", offset);
    for (const [k, v] of Object.entries(extra)) c.setAttribute(k, v);
    return c;
  };
  // Faint full-ring track under the slices.
  svg.appendChild(circle(cssVar("--border"), null, null, { opacity: "0.35" }));
  const slices = (s.slices || []).filter((x) => (x.value || 0) > 0);
  const total = slices.reduce((a, x) => a + (x.value || 0), 0) || 1;
  let acc = 0;
  for (const sl of slices) {
    const frac = (sl.value || 0) / total;
    const arc = circle(donutColor(sl.tone), `${frac * C} ${C}`, -acc * C,
      { transform: `rotate(-90 ${cx} ${cy})`, "stroke-linecap": "butt" });
    svg.appendChild(arc);
    acc += frac;
  }
  if (s.center) {
    const txt = (y, cls, str) => {
      const t = document.createElementNS(SVG_NS, "text");
      t.setAttribute("x", cx); t.setAttribute("y", y);
      t.setAttribute("text-anchor", "middle");
      t.setAttribute("class", cls);
      t.textContent = str;
      return t;
    };
    svg.appendChild(txt(cy - 1, "donut-center", s.center));
    if (s.centerLabel) svg.appendChild(txt(cy + 13, "donut-center-sub", s.centerLabel));
  }
  return svg;
}

function donutLegend(s) {
  const slices = s.slices || [];
  const total = slices.reduce((a, x) => a + (x.value || 0), 0) || 1;
  const box = document.createElement("div");
  box.className = "donut-legend";
  for (const sl of slices) {
    const row = document.createElement("div");
    row.className = "dl-row";
    const dot = document.createElement("span");
    dot.className = "dl-dot";
    dot.style.background = donutColor(sl.tone);
    const lab = document.createElement("span");
    lab.className = "dl-lab"; lab.textContent = sl.label;
    const val = document.createElement("span");
    val.className = "dl-val";
    const pct = Math.round((sl.value || 0) / total * 100);
    val.textContent = `${sl.text || "–"} · ${pct}%`;
    row.append(dot, lab, val);
    box.appendChild(row);
  }
  if (s.totalText) {
    const foot = document.createElement("div");
    foot.className = "dl-total";
    foot.textContent = s.totalText;
    box.appendChild(foot);
  }
  return box;
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
    toast("Select at least one entity to display.", "warn");
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
    toastErr(ex.message);
  }
}

// ---- interactive charts -----------------------------------------------------
// Charts in the open detail modal register here so the real-time refresh can
// recompute their data and repaint in place — no DOM rebuild, no lost hover.
let CHART_REG = [];
// Live cells (e.g. interface rate columns) that recompute from DM data on each
// real-time tick without a DOM rebuild.
let LIVE_CELLS = [];
function resetCharts() { CHART_REG = []; LIVE_CELLS = []; }
function refreshCharts() {
  for (const c of CHART_REG) { try { c.refresh(); } catch (_) {} }
  for (const f of LIVE_CELLS) { try { f(); } catch (_) {} }
}

// Current per-interface throughput (bytes/sec) from the last two counter
// samples in DM.ifHistory. Returns {down, up} (null when no history yet).
function ifRate(id) {
  const ifh = (DM.ifHistory || {})[id] || {};
  const rate = (arr) => {
    const a = arr || [];
    if (a.length < 2) return null;
    const [t0, v0] = a[a.length - 2], [t1, v1] = a[a.length - 1];
    const dt = t1 - t0;
    if (dt <= 0) return null;
    return Math.max(0, v1 - v0) / dt;  // clamp counter resets to 0
  };
  return { down: rate(ifh.rx), up: rate(ifh.tx) };
}

function fmtClock(ts) {
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

// Build an interactive line chart on the <canvas> inside `card`.
//   seriesFn() -> [{points:[[ts,val]], color, label}]  (recomputed on refresh)
//   fmt(v) -> string ; headFn(card, series) updates header/legend readouts.
function makeChart({ card, seriesFn, fmt, headFn, fromZero }) {
  const canvas = $("canvas", card);
  const tip = document.createElement("div");
  tip.className = "chart-tip"; tip.hidden = true;
  card.appendChild(tip);
  const state = { series: [], fmt, hover: null, fromZero: !!fromZero };

  function recompute() {
    state.series = seriesFn() || [];
    if (headFn) headFn(card, state.series);
  }
  function nearestTs(cx, width) {
    const xs = state.series.flatMap((s) => s.points.map((p) => p[0]));
    if (!xs.length) return null;
    const x0 = Math.min(...xs), x1 = Math.max(...xs);
    const t = x0 + Math.max(0, Math.min(1, cx / width)) * (x1 - x0 || 1);
    let best = null, bd = Infinity;
    for (const s of state.series) for (const p of s.points) {
      const d = Math.abs(p[0] - t); if (d < bd) { bd = d; best = p[0]; }
    }
    return best;
  }
  function onMove(ev) {
    const rect = canvas.getBoundingClientRect();
    const cx = (ev.touches ? ev.touches[0].clientX : ev.clientX) - rect.left;
    const ts = nearestTs(cx, rect.width);
    if (ts == null) return;
    state.hover = ts;
    paintChart(canvas, state);
    showChartTip(card, tip, canvas, state, ts);
    if (ev.cancelable) ev.preventDefault();
  }
  function onLeave() { state.hover = null; tip.hidden = true; paintChart(canvas, state); }
  canvas.addEventListener("pointerdown", onMove);
  canvas.addEventListener("pointermove", onMove);
  canvas.addEventListener("pointerleave", onLeave);
  canvas.addEventListener("pointercancel", onLeave);
  canvas.style.touchAction = "pan-y";

  recompute();
  requestAnimationFrame(() => paintChart(canvas, state));
  CHART_REG.push({ refresh() {
    if (!canvas.isConnected) return;      // a superseded interface chart
    recompute();
    if (state.hover == null) paintChart(canvas, state);  // don't fight a hover
  } });
}

function valueAt(points, ts) {
  let best = null, bd = Infinity;
  for (const p of points) { const d = Math.abs(p[0] - ts); if (d < bd) { bd = d; best = p[1]; } }
  return best;
}

function chartGeom(canvas, state) {
  const pad = 3;
  const cssW = canvas.clientWidth || 240, cssH = canvas.clientHeight || 56;
  const xs = state.series.flatMap((s) => s.points.map((p) => p[0]));
  const ys = state.series.flatMap((s) => s.points.map((p) => p[1]));
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  let y0 = state.fromZero ? 0 : Math.min(...ys), y1 = Math.max(...ys);
  if (!(y1 > y0)) y1 = y0 + 1;
  const w = cssW - pad * 2, h = cssH - pad * 2;
  return { pad, w, h, x0, x1, y0, y1,
    px: (t) => pad + ((t - x0) / (x1 - x0 || 1)) * w,
    py: (v) => pad + h - ((v - y0) / (y1 - y0)) * h };
}

function paintChart(canvas, state) {
  const dpr = window.devicePixelRatio || 1;
  const cssW = canvas.clientWidth || 240, cssH = canvas.clientHeight || 56;
  canvas.width = Math.round(cssW * dpr); canvas.height = Math.round(cssH * dpr);
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr); ctx.clearRect(0, 0, cssW, cssH);
  if (state.series.reduce((n, s) => n + s.points.length, 0) < 2) return;
  const g = chartGeom(canvas, state);
  const single = state.series.length === 1;
  for (const s of state.series) {
    const pts = s.points;
    if (pts.length < 2) continue;
    if (single) {
      ctx.beginPath(); ctx.moveTo(g.px(pts[0][0]), g.py(pts[0][1]));
      for (let i = 1; i < pts.length; i++) ctx.lineTo(g.px(pts[i][0]), g.py(pts[i][1]));
      ctx.lineTo(g.px(pts[pts.length - 1][0]), g.pad + g.h);
      ctx.lineTo(g.px(pts[0][0]), g.pad + g.h); ctx.closePath();
      ctx.fillStyle = s.color + "22"; ctx.fill();
    }
    ctx.beginPath(); ctx.moveTo(g.px(pts[0][0]), g.py(pts[0][1]));
    for (let i = 1; i < pts.length; i++) ctx.lineTo(g.px(pts[i][0]), g.py(pts[i][1]));
    ctx.strokeStyle = s.color; ctx.lineWidth = 1.5; ctx.lineJoin = "round"; ctx.stroke();
    const last = pts[pts.length - 1];
    ctx.beginPath(); ctx.arc(g.px(last[0]), g.py(last[1]), 2.5, 0, Math.PI * 2);
    ctx.fillStyle = s.color; ctx.fill();
  }
  if (state.hover != null) {
    const hx = g.px(state.hover);
    ctx.save();
    ctx.beginPath(); ctx.moveTo(hx, g.pad); ctx.lineTo(hx, g.pad + g.h);
    ctx.strokeStyle = cssVar("--muted"); ctx.globalAlpha = 0.5; ctx.stroke();
    ctx.restore();
    for (const s of state.series) {
      const v = valueAt(s.points, state.hover);
      if (v == null) continue;
      ctx.beginPath(); ctx.arc(hx, g.py(v), 3.4, 0, Math.PI * 2);
      ctx.fillStyle = s.color; ctx.fill();
      ctx.strokeStyle = cssVar("--bg-elev"); ctx.lineWidth = 1.5; ctx.stroke();
    }
  }
}

function showChartTip(card, tip, canvas, state, ts) {
  const rows = state.series.map((s) => {
    const v = valueAt(s.points, ts);
    return `<span class="ct-row"><i style="background:${s.color}"></i>` +
      `${state.series.length > 1 ? s.label + " " : ""}<b>${v == null ? "–" : state.fmt(v)}</b></span>`;
  }).join("");
  tip.innerHTML = `<div class="ct-time">${fmtClock(ts)}</div>${rows}`;
  tip.hidden = false;
  const g = chartGeom(canvas, state);
  const tw = tip.offsetWidth || 90;
  tip.style.left = Math.max(2, Math.min(g.px(ts) - tw / 2, canvas.clientWidth - tw - 2)) + "px";
}

// `e` is an entity record ({key,name,unit,value}); `rawPoints` its history.
function chartCard(e, rawPoints) {
  const key = e.key;
  const isRate = RATE_KEY_RE.test(key);
  const unit = !isRate && e.unit ? " " + e.unit : "";
  const fmt = isRate ? (v) => fmtBitsRate(v) : (v) => fmtNum(v) + unit;
  const card = document.createElement("div");
  card.className = "chart-card";
  card.innerHTML = `
    <div class="c-head"><span class="c-title"></span><span class="c-now"></span></div>
    <canvas></canvas>
    <div class="c-foot"><span class="lo"></span><span class="hi"></span></div>`;
  $(".c-title", card).textContent = e.name || labelFor(key);
  const seriesFn = () => {
    const hist = (DM.history && DM.history[key]) || rawPoints || [];
    const pts = isRate ? toRate(hist) : hist;
    return [{ points: pts, color: cssVar("--accent"), label: e.name || labelFor(key) }];
  };
  const headFn = (c, series) => {
    const vals = series[0].points.map((p) => p[1]);
    const liveVal = DM.entities.find((x) => x.key === key);
    const now = isRate ? (vals.length ? vals[vals.length - 1] : null)
      : (liveVal && typeof liveVal.value === "number" ? liveVal.value
        : (vals.length ? vals[vals.length - 1] : null));
    $(".c-now", c).textContent = now == null ? "–" : fmt(now);
    $(".lo", c).textContent = "min " + (vals.length ? fmt(Math.min(...vals)) : "–");
    $(".hi", c).textContent = "peak " + (vals.length ? fmt(Math.max(...vals)) : "–");
  };
  makeChart({ card, seriesFn, fmt, headFn, fromZero: isRate });
  return card;
}

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || "#4aa8ff";
}

// Colour a table cell by meaning, matching Network Manager's cues: WiFi signal
// green/amber/red by dBm, link/status up=green down=red, error counts red.
function cellSeverity(key, v) {
  if (v == null || v === "" || v === "–") return "";
  const k = String(key).toLowerCase();
  const s = String(v).toLowerCase();
  if (/rssi|signal/.test(k)) {
    const dbm = parseInt(String(v).replace(/[^\-0-9]/g, ""), 10);
    if (isNaN(dbm)) return "";
    if (dbm >= -60) return "sev-good";
    if (dbm >= -72) return "sev-warn";
    return "sev-bad";
  }
  if (/link|status|up|carrier/.test(k)) {
    if (/^(up|yes|true|online|connected)$/.test(s)) return "sev-good";
    if (/^(down|no|false|offline)$/.test(s)) return "sev-bad";
  }
  if (/error|discard|crc|drop|bad/.test(k)) {
    const n = parseInt(String(v).replace(/[^0-9]/g, ""), 10);
    if (!isNaN(n)) return n > 0 ? "sev-bad" : "sev-good";
  }
  if (k === "type") {
    if (s === "fibre" || s === "fiber") return "sev-accent";
  }
  return "";
}

// A per-row action button (e.g. "Force roam" on an AP client). POSTs the
// action + the row's arg value (a MAC) to the device action endpoint.
function rowActionButton(a, row) {
  const btn = document.createElement("button");
  btn.className = "btn btn-ghost btn-sm";
  btn.textContent = a.label || a.action;
  const arg = a.argKey ? row[a.argKey] : null;
  if (a.argKey && (arg == null || arg === "" || arg === "–")) btn.disabled = true;
  btn.onclick = async () => {
    if (a.confirm) {
      const ok = await confirmDialog({ title: `${a.label || a.action}?`,
        message: arg ? `Target: ${arg}` : "", okLabel: a.label || "Confirm",
        danger: !!a.danger });
      if (!ok) return;
    }
    const orig = btn.textContent;
    btn.disabled = true;
    btn.textContent = "Working…";
    try {
      const args = a.argKey ? { [a.argKey]: arg } : {};
      const r = await api(`/api/devices/${DM.device.id}/action`, {
        method: "POST",
        body: JSON.stringify({ action: a.action, args }),
      });
      btn.textContent = "Done ✓";
      toastOk((r && r.message) || `${a.label || a.action} done.`);
    } catch (ex) {
      btn.textContent = orig;
      btn.disabled = false;
      toastErr(ex.message);
    }
  };
  return btn;
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
  const rowActions = t.rowActions || [];
  for (const c of cols) {
    const th = document.createElement("th");
    th.textContent = c.label + (c.unit ? ` (${c.unit})` : "");
    htr.appendChild(th);
  }
  if (rowActions.length) {
    table.classList.add("has-actions");
    const ath = document.createElement("th");
    ath.className = "col-actions";
    htr.appendChild(ath);
  }
  thead.appendChild(htr);
  table.appendChild(thead);
  const cellChart = t.cellChart;
  const cellPie = t.cellPie;
  const tbody = document.createElement("tbody");
  for (const row of rows) {
    const tr = document.createElement("tr");
    for (const c of cols) {
      const td = document.createElement("td");
      const v = row[c.key];
      td.textContent = v == null || v === "" ? "–" : String(v);
      const cls = [];
      if (/mac|rssi|tx|rx|channel|clients|ip|speed/i.test(c.key)) cls.push("mono");
      const sev = cellSeverity(c.key, v);
      if (sev) cls.push(sev);
      // A cell the driver marked chartable (e.g. a disk's Temp) opens a
      // time-series popup on click.
      if (cellChart && c.key === cellChart.col && v != null && v !== "" &&
          String(v) !== "–" && row[cellChart.idKey] != null) {
        cls.push("cell-chart");
        const ident = String(row[cellChart.idKey]);
        td.tabIndex = 0;
        td.title = "Click for history";
        td.addEventListener("click", () => openSeriesChart(cellChart, ident));
        td.addEventListener("keydown", (ev) => {
          if (ev.key === "Enter" || ev.key === " ") {
            ev.preventDefault(); openSeriesChart(cellChart, ident);
          }
        });
      }
      // A cell the driver marked with a per-row pie spec (e.g. a node's memory)
      // opens a donut breakdown on click.
      if (cellPie && c.key === cellPie.col && row[cellPie.specKey]) {
        cls.push("cell-chart");
        const spec = row[cellPie.specKey];
        td.tabIndex = 0;
        td.title = "Click for breakdown";
        const open = () => openPieModal(spec);
        td.addEventListener("click", open);
        td.addEventListener("keydown", (ev) => {
          if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); open(); }
        });
      }
      if (cls.length) td.className = cls.join(" ");
      tr.appendChild(td);
    }
    if (rowActions.length) {
      const td = document.createElement("td");
      td.className = "row-actions";
      for (const a of rowActions) td.appendChild(rowActionButton(a, row));
      tr.appendChild(td);
    }
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  wrap.appendChild(table);
  return wrap;
}

// Popup: fetch and chart a table cell's time-series (e.g. a disk's temperature
// history). `cfg` is the table's cellChart spec; `ident` the row's id value.
async function openSeriesChart(cfg, ident) {
  const overlay = document.createElement("div");
  overlay.className = "modal series-modal";
  overlay.innerHTML = `
    <div class="modal-backdrop"></div>
    <div class="modal-card series-card">
      <div class="modal-head">
        <h2><span></span></h2>
        <div class="modal-head-actions">
          <button class="btn btn-ghost btn-sm sc-close">Close</button>
        </div>
      </div>
      <div class="series-body"><p class="muted">Loading…</p></div>
    </div>`;
  $(".modal-head h2 span", overlay).textContent =
    `${cfg.title || "History"}: ${ident}`;
  document.body.appendChild(overlay);
  document.body.style.overflow = "hidden";
  const close = () => {
    overlay.remove();
    document.body.style.overflow = DM ? "hidden" : "";
    document.removeEventListener("keydown", onEsc);
  };
  const onEsc = (ev) => { if (ev.key === "Escape") close(); };
  $(".modal-backdrop", overlay).onclick = close;
  $(".sc-close", overlay).onclick = close;
  document.addEventListener("keydown", onEsc);

  const body = $(".series-body", overlay);
  try {
    const q = `metric=${encodeURIComponent(cfg.metric)}&id=${encodeURIComponent(ident)}`;
    const data = await api(`/api/devices/${DM.device.id}/series?${q}`);
    const pts = (data && data.series) || [];
    if (pts.length < 2) {
      body.innerHTML = `<p class="muted">Not enough history yet to chart.</p>`;
      return;
    }
    body.innerHTML = "";
    body.appendChild(seriesChartCard({
      name: `${cfg.title || "Value"} · ${ident}`, unit: cfg.unit,
    }, pts));
  } catch (ex) {
    body.innerHTML = `<p class="auth-err">Couldn't load history: ${ex.message}</p>`;
  }
}

// Popup: render a donut breakdown for a table row (e.g. a node's memory pie)
// in the same modal shell as the series chart.
function openPieModal(spec) {
  const overlay = document.createElement("div");
  overlay.className = "modal series-modal";
  overlay.innerHTML = `
    <div class="modal-backdrop"></div>
    <div class="modal-card series-card">
      <div class="modal-head">
        <h2><span></span></h2>
        <div class="modal-head-actions">
          <button class="btn btn-ghost btn-sm sc-close">Close</button>
        </div>
      </div>
      <div class="series-body"></div>
    </div>`;
  $(".modal-head h2 span", overlay).textContent = spec.title || "Breakdown";
  document.body.appendChild(overlay);
  document.body.style.overflow = "hidden";
  const close = () => {
    overlay.remove();
    document.body.style.overflow = DM ? "hidden" : "";
    document.removeEventListener("keydown", onEsc);
  };
  const onEsc = (ev) => { if (ev.key === "Escape") close(); };
  $(".modal-backdrop", overlay).onclick = close;
  $(".sc-close", overlay).onclick = close;
  document.addEventListener("keydown", onEsc);
  const wrap = document.createElement("div");
  wrap.className = "donut-wrap";
  wrap.append(donutSvg(spec), donutLegend(spec));
  $(".series-body", overlay).appendChild(wrap);
}

// A standalone (not history-backed) line chart over a fixed point array, for
// the cell-chart popup. Mirrors chartCard but reads its own `pts`.
function seriesChartCard(e, pts) {
  const unit = e.unit ? " " + e.unit : "";
  const fmt = (v) => fmtNum(v) + unit;
  const card = document.createElement("div");
  card.className = "chart-card series-chart";
  card.innerHTML = `
    <div class="c-head"><span class="c-title"></span><span class="c-now"></span></div>
    <canvas></canvas>
    <div class="c-foot"><span class="lo"></span><span class="hi"></span></div>`;
  $(".c-title", card).textContent = e.name;
  const seriesFn = () => [{ points: pts, color: cssVar("--accent"), label: e.name }];
  const headFn = (c, series) => {
    const vals = series[0].points.map((p) => p[1]);
    const now = vals.length ? vals[vals.length - 1] : null;
    $(".c-now", c).textContent = now == null ? "–" : fmt(now);
    $(".lo", c).textContent = "min " + (vals.length ? fmt(Math.min(...vals)) : "–");
    $(".hi", c).textContent = "peak " + (vals.length ? fmt(Math.max(...vals)) : "–");
  };
  makeChart({ card, seriesFn, fmt, headFn, fromZero: false });
  return card;
}

// Normalize a cell value for display: null / empty string -> "–".
function fld(v) {
  return v == null || v === "" ? "–" : String(v);
}

// A wireless-station table (layout: "clients") rendered as a mobile-friendly
// expandable list — one line per client (name · band/SSID · signal · rate),
// with IP/MAC and the remaining columns revealed on tap. Mirrors Network
// Manager's WiFi station cards, which read far better on a phone than a wide
// horizontal-scrolling table.
function clientsList(t) {
  const rows = t.rows || [];
  if (!rows.length) {
    const p = document.createElement("p");
    p.className = "detail-empty";
    p.textContent = "None.";
    return p;
  }
  const cols = t.columns || [];
  const labelOf = {};
  for (const c of cols) labelOf[c.key] = c.label + (c.unit ? ` (${c.unit})` : "");
  const rowActions = t.rowActions || [];
  // Keys shown on the collapsed line; everything else drops into the expander.
  const inlineKeys = new Set(["client", "band", "ssid", "rssi", "signal", "tx", "rx"]);

  const list = document.createElement("div");
  list.className = "client-list";

  for (const row of rows) {
    const item = document.createElement("div");
    item.className = "client-item";

    // --- collapsed header (tap to expand) ---
    const head = document.createElement("button");
    head.type = "button";
    head.className = "client-head";

    const main = document.createElement("div");
    main.className = "client-main";
    const name = document.createElement("div");
    name.className = "client-name";
    const nameText = document.createElement("span");
    nameText.className = "client-name-txt";
    nameText.textContent = fld(row.client);
    const chev = document.createElement("span");
    chev.className = "chev";
    chev.textContent = "▸";
    name.append(nameText, chev);
    const sub = document.createElement("div");
    sub.className = "client-sub";
    sub.textContent =
      [row.band, row.ssid].map(fld).filter((x) => x !== "–").join(" · ") || "—";
    main.append(name, sub);

    const metrics = document.createElement("div");
    metrics.className = "client-metrics";
    const rssiVal = row.rssi != null && row.rssi !== "" ? row.rssi : row.signal;
    if (rssiVal != null && rssiVal !== "" && rssiVal !== "–") {
      const r = document.createElement("div");
      const sev = cellSeverity("rssi", rssiVal);
      r.className = "client-rssi" + (sev ? " " + sev : "");
      r.textContent = `${rssiVal} dBm`;
      metrics.appendChild(r);
    }
    const tx = fld(row.tx), rx = fld(row.rx);
    if (tx !== "–" || rx !== "–") {
      const rate = document.createElement("div");
      rate.className = "client-rate";
      rate.textContent = `↑${tx} ↓${rx}`;
      metrics.appendChild(rate);
    }
    head.append(main, metrics);

    // --- AP-lock circle (only when this AP can enforce a binding) ---
    // Green = locked to this AP, amber = locked to another AP, grey = unlocked.
    // Tapping toggles the lock; a background poller then pins the client. Sits
    // inside the header button, so the click must not also expand the row.
    if (t.bindable && row.mac && row.mac !== "–") {
      head.appendChild(lockCircle(row));
    }

    // --- expanded detail (IP, MAC, PHY, …) ---
    const detail = document.createElement("div");
    detail.className = "client-detail";
    const kv = document.createElement("dl");
    kv.className = "client-kv";
    for (const c of cols) {
      if (inlineKeys.has(c.key)) continue;
      const dt = document.createElement("dt");
      dt.textContent = labelOf[c.key] || c.key;
      const dd = document.createElement("dd");
      dd.textContent = fld(row[c.key]);
      kv.append(dt, dd);
    }
    detail.appendChild(kv);
    if (rowActions.length) {
      const acts = document.createElement("div");
      acts.className = "row-actions";
      for (const a of rowActions) acts.appendChild(rowActionButton(a, row));
      detail.appendChild(acts);
    }

    head.onclick = () => item.classList.toggle("open");
    item.append(head, detail);
    list.appendChild(item);
  }
  return list;
}

// The Radios table (layout: "radios"): a normal table whose rows are clickable
// when they carry a historyKey — tapping a band expands its client-count history
// chart. Replaces the standalone per-band client charts in Metrics.
function radiosTable(t) {
  const cols = t.columns || [];
  const wrap = document.createElement("div");
  const table = detailTable(t);   // reuse the generic table renderer + styling
  wrap.appendChild(table);
  const bodyRows = $$("tbody tr", table);
  (t.rows || []).forEach((row, i) => {
    const tr = bodyRows[i];
    const key = row.historyKey;
    if (!tr || !key) return;
    tr.classList.add("radio-row");
    const chartRow = document.createElement("tr");
    chartRow.className = "radio-chart-row";
    chartRow.hidden = true;
    const td = document.createElement("td");
    td.colSpan = cols.length;
    chartRow.appendChild(td);
    tr.after(chartRow);
    let built = false;
    tr.onclick = () => {
      const show = chartRow.hidden;
      chartRow.hidden = !show;
      tr.classList.toggle("open", show);
      if (show && !built) {
        built = true;
        const label = (row.band ? row.band + " " : "") + "clients";
        td.appendChild(chartCard({ key, name: label, unit: "" },
                                 (DM.history && DM.history[key]) || []));
      }
    };
  });
  return wrap;
}

// Enable/disable roam-binding on an already-added AP (the wizard sets it at add
// time; this lets you turn it on/off later). Enabling re-verifies SSH server-side.
function bindingSection() {
  const s = section("Roam-binding");
  const on = !!DM.device.apBinding;
  const row = document.createElement("div");
  row.className = "binding-row";
  const desc = document.createElement("p");
  desc.className = "cz-sub";
  desc.innerHTML = on
    ? "On — lock any client in the list to pin it to this AP. Uses SSH to the AP."
    : "Off — turn on to pin clients to this access point. <strong>Uses SSH</strong> "
      + "to the AP (same admin login); it's checked when you enable it.";
  const btn = document.createElement("button");
  btn.className = "btn btn-sm " + (on ? "btn-ghost" : "btn-primary");
  btn.textContent = on ? "Turn off" : "Turn on";
  btn.onclick = async () => {
    const locks = (DM.device.boundClients || []).length;
    if (on && locks) {
      const ok = await confirmDialog({ title: "Turn off roam-binding?",
        message: `This clears ${locks} client lock${locks > 1 ? "s" : ""} on this AP.`,
        okLabel: "Turn off", danger: true });
      if (!ok) return;
    }
    btn.disabled = true; btn.textContent = on ? "Turning off…" : "Checking SSH…";
    try {
      const r = await api(`/api/devices/${DM.device.id}/binding`,
        { method: "POST", body: JSON.stringify({ enabled: !on }) });
      if (!on && r.bindingWarning) {
        toastErr("Couldn't enable roam-binding — " + r.bindingWarning);
        btn.disabled = false; btn.textContent = "Turn on";
        return;
      }
      toastOk(r.device.apBinding ? "Roam-binding on" : "Roam-binding off");
      await openDevice(DM.device);   // re-fetch so client locks appear/vanish
      loadDevices();                 // refresh card in the background
    } catch (ex) {
      toastErr(ex.message);
      btn.disabled = false; btn.textContent = on ? "Turn off" : "Turn on";
    }
  };
  row.append(desc, btn);
  s.appendChild(row);
  return s;
}

const LOCK_TITLE = {
  here: "Locked to this AP — tap to unlock",
  elsewhere: "Locked to another AP — tap to lock here",
  "": "Tap to lock this client to this AP",
};

// The bind circle for one client row. Colour reflects row.lock; tap toggles the
// binding via the API and recolours in place (no full re-render).
function lockCircle(row) {
  const dot = document.createElement("span");
  const paint = () => {
    dot.className = "client-lock lock-" + (row.lock || "none");
    dot.title = LOCK_TITLE[row.lock || ""];
  };
  paint();
  dot.onclick = async (e) => {
    e.stopPropagation();           // don't also toggle the row expander
    if (dot.classList.contains("busy")) return;
    const bound = row.lock !== "here";   // locked here already -> unlock
    dot.classList.add("busy");
    try {
      await api(`/api/devices/${DM.device.id}/bind-client`,
        { method: "POST", body: JSON.stringify({ mac: row.mac, bound }) });
      row.lock = bound ? "here" : "";
      paint();
      toastOk(bound ? "Locked to this AP" : "Lock removed");
    } catch (ex) {
      toastErr(ex.message);
    } finally {
      dot.classList.remove("busy");
    }
  };
  return dot;
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
    } catch (ex) { toastErr(ex.message); }
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
  const rateTh = document.createElement("th");
  rateTh.textContent = "Rate ↓↑";
  htr.appendChild(rateTh);
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
    // Live rate cell — current down/up throughput, updated on each real-time
    // tick from DM.ifHistory without rebuilding the table.
    const rateTd = document.createElement("td");
    rateTd.className = "if-rate mono";
    rateTd.innerHTML = `<span class="r-dn"></span><span class="r-up"></span>`;
    tr.appendChild(rateTd);
    const updateRate = () => {
      if (!rateTd.isConnected) return;
      const { down, up } = ifRate(id);
      $(".r-dn", rateTd).textContent = down == null ? "–" : "↓ " + fmtBitsRate(down);
      $(".r-up", rateTd).textContent = up == null ? "" : "↑ " + fmtBitsRate(up);
    };
    updateRate();
    LIVE_CELLS.push(updateRate);
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
  DM.selectedIf = id;  // remembered so real-time refresh keeps this chart live
  container.hidden = false;
  container.innerHTML = "";
  if (rx.length < 2 && tx.length < 2) {
    container.innerHTML = `<p class="detail-empty">No traffic history yet for ${name}` +
      ` — it builds up as the device is polled (every ~60s).</p>`;
    return;
  }
  container.appendChild(dualChartCard(name, id));
}

// Interactive upload/download throughput for one interface. Reads fresh history
// from DM.ifHistory[id] on each (real-time) refresh.
function dualChartCard(name, id) {
  const card = document.createElement("div");
  card.className = "chart-card if-chart-card";
  card.innerHTML = `
    <div class="c-head"><span class="c-title"></span>
      <span class="c-legend"><span class="dl">&#8595; <b class="dv"></b></span>
        <span class="ul">&#8593; <b class="uv"></b></span></span></div>
    <canvas></canvas>
    <div class="c-foot"><span class="lo"></span><span class="hi"></span></div>`;
  $(".c-title", card).textContent = name + " — traffic";
  const seriesFn = () => {
    const ifh = (DM.ifHistory || {})[id] || {};
    return [
      { points: toRate(ifh.rx || []), color: cssVar("--accent"), label: "Download" },
      { points: toRate(ifh.tx || []), color: cssVar("--green"), label: "Upload" },
    ];
  };
  const headFn = (c, series) => {
    const d = series[0].points, u = series[1].points;
    const peak = Math.max(0, ...d.map((p) => p[1]), ...u.map((p) => p[1]));
    $(".dv", c).textContent = d.length ? fmtBitsRate(d[d.length - 1][1]) : "–";
    $(".uv", c).textContent = u.length ? fmtBitsRate(u[u.length - 1][1]) : "–";
    $(".lo", c).textContent = "";
    $(".hi", c).textContent = "peak " + fmtBitsRate(peak);
  };
  makeChart({ card, seriesFn, fmt: (v) => fmtBitsRate(v), headFn, fromZero: true });
  return card;
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
  try {
    const r = await api("/api/push/test", { method: "POST" });
    if (r.sent) msg.textContent = `Test sent (${r.sent}).`;
    else if (r.failed) msg.textContent = `Test failed on ${r.failed} device(s): ${r.error || "push rejected"}`;
    else msg.textContent = "No device is subscribed — tap “Enable notifications” first.";
  } catch (ex) { msg.textContent = "Test failed: " + ex.message; }
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
    driverId: "opnsense.firewall", set: { authStyle: "basic", scheme: "https" },
    hint: "OPNsense: create an API key + secret (System ▸ Access ▸ Users). Enter the key as “API key” and secret as “API secret”." },
  { id: "pfsense", label: "pfSense firewall (REST API v2)", transport: "api", port: "",
    driverId: "pfsense.firewall", set: { authStyle: "header", keyHeader: "X-API-Key", scheme: "https" },
    hint: "Requires the pfSense REST API v2 package. Paste your API key as “API key”." },
  { id: "unifi", label: "UniFi Network controller", transport: "api", port: 443,
    driverId: "unifi.network", set: { authStyle: "header", keyHeader: "X-API-KEY", scheme: "https" },
    hint: "UniFi Network 9+: create an API key, then paste it as “API key”." },
  { id: "proxmox", label: "Proxmox VE", transport: "api", port: 8006,
    driverId: "proxmox.ve",
    hint: "Proxmox: Datacenter ▸ Permissions ▸ API Tokens ▸ Add. Enter the token ID (user@realm!tokenid) and the secret value shown once on creation. Give the token a read role (e.g. PVEAuditor) or uncheck Privilege Separation so it can read the API.",
    fields: [
      { k: "tokenId", label: "API token ID (user@realm!tokenid)", placeholder: "monitor@pve!diag" },
      { k: "tokenSecret", label: "Token secret", type: "password" },
      { k: "verifyTls", label: "Verify TLS certificate (Proxmox is self-signed by default)", type: "checkbox", default: false },
    ],
    assemble: (r) => ({
      apiKey: `PVEAPIToken=${(r.tokenId || "").trim()}=${(r.tokenSecret || "").trim()}`,
      authStyle: "header", keyHeader: "Authorization", scheme: "https",
      verifyTls: !!r.verifyTls,
    }) },
  { id: "truenas", label: "TrueNAS", transport: "api", port: "",
    driverId: "truenas.system", set: { authStyle: "bearer", scheme: "https" },
    hint: "TrueNAS: create an API key (Settings ▸ API Keys) and paste it as “API key”." },
  { id: "firewalla", label: "Firewalla (MSP)", transport: "api", port: "",
    driverId: "firewalla.msp", set: { authStyle: "header", keyHeader: "Authorization", scheme: "https" },
    hint: "Host is your MSP domain (xxx.firewalla.net). Paste “Token <your-token>” as “API key”." },
  { id: "mikrotik", label: "MikroTik RouterOS", transport: "api", port: "",
    driverId: "mikrotik.routeros", set: { authStyle: "basic", scheme: "https" },
    hint: "RouterOS REST API: enter your username as “API key” and password as “API secret”." },
  { id: "openwrt", label: "OpenWrt router / AP / switch", transport: "http", port: 80,
    driverId: "openwrt.ubus", set: { scheme: "http", metricsPath: "/metrics" },
    hint: "Enter your LuCI (web UI) username and password. If the device exposes a Prometheus /metrics page (e.g. an OpenWrt-flashed switch with SFP telemetry), leave the metrics path set to pull SFP/optics data." },
  { id: "synology", label: "Synology DSM NAS", transport: "http", port: 5000,
    driverId: "synology.dsm", set: { scheme: "http" }, hint: "Enter your DSM username and password (DSM is usually on port 5000/5001)." },
  { id: "qnap", label: "QNAP NAS", transport: "http", port: 8080,
    driverId: "qnap.qts", set: { scheme: "http" }, hint: "Enter your QTS username and password (QTS is usually on port 8080/443)." },
  { id: "keeplink", label: "Keeplink web-smart switch", transport: "http", port: 80,
    driverId: "keeplink.switch", set: { scheme: "http" }, hint: "Enter the switch web-UI username and password." },
  { id: "zyxel", label: "Zyxel WiFi access point (NWA/WAX)", transport: "http", port: 443,
    driverId: "zyxel.ap", set: { scheme: "https", verifyTls: false },
    hint: "Enter the AP web-UI admin username and password. Zyxel APs use HTTPS with a self-signed certificate, so TLS verification is off." },
];

let WIZ = null;

async function initWizard() {
  WIZ = { transport: null, candidates: [], driverId: null, entities: [],
          presetDriver: null, presetLabel: null, supportsBinding: false,
          nacSupported: false, newDeviceId: null };
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
      WIZ.presetDriver = null; WIZ.presetLabel = null;
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
    else {
      $("#wiz-hint").hidden = true; WIZ.presetDriver = null; WIZ.presetLabel = null;
      WIZ.presetAssemble = null;
      if (WIZ.transport) selectTransport(WIZ.transport);  // restore default inputs
    }
  };
  $("#wiz-creds").innerHTML = `<p class="muted">Pick a device type above, or choose a connection method.</p>`;
}

function applyPreset(p) {
  // Remember the driver this preset implies so detection prefers it (and we can
  // still offer it if the login fails and it isn't auto-detected).
  WIZ.presetDriver = p.driverId || null;
  WIZ.presetLabel = p.label;
  selectTransport(p.transport);
  // A preset may replace the credential inputs entirely (Proxmox's token setup
  // differs from the generic API key/secret) and supply an assemble() that maps
  // them back to the transport's credential shape.
  if (p.fields) {
    renderCredFields(p.fields);
    WIZ.presetAssemble = p.assemble || null;
  }
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
  WIZ.presetAssemble = null;   // a manual transport pick uses the default fields
  $$("#wiz-transports .transport-opt").forEach((n) => n.classList.toggle("selected", n.dataset.transport === t));
  const meta = TRANSPORTS[t];
  $("#wiz-port").placeholder = meta.defaultPort ? `default ${meta.defaultPort}` : "(none)";
  renderCredFields(meta.fields);
}

// Render a set of credential inputs and remember which fields are active so
// collectCreds() reads back exactly what was shown. `fields` is either a
// transport's default fields or a preset's custom override (e.g. Proxmox).
function renderCredFields(fields) {
  WIZ.fields = fields;
  const box = $("#wiz-creds");
  box.innerHTML = "";
  for (const f of fields) {
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
      if (f.placeholder) input.placeholder = f.placeholder;
      if (f.default && f.type !== "select") input.value = f.default;
      wrap.append(input);
    }
    box.appendChild(wrap);
  }
}

function collectCreds() {
  const raw = {};
  for (const f of WIZ.fields || TRANSPORTS[WIZ.transport].fields) {
    const el = $("#cred-" + f.k);
    if (!el) continue;
    if (f.type === "checkbox") raw[f.k] = el.checked;
    else if (el.value !== "") raw[f.k] = el.value;
  }
  // Presets with custom inputs map their raw fields back into the transport's
  // credential shape (e.g. Proxmox token id + secret -> a PVEAPIToken header).
  return WIZ.presetAssemble ? WIZ.presetAssemble(raw) : raw;
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
    // If a device type was chosen from the dropdown, honour it: use its driver
    // even when detection didn't confirm it (a wrong password makes the device
    // look generic). Inject it as the pre-selected, unconfirmed choice.
    if (WIZ.presetDriver &&
        !WIZ.candidates.some((c) => c.driverId === WIZ.presetDriver)) {
      WIZ.candidates.unshift({ driverId: WIZ.presetDriver,
        displayName: WIZ.presetLabel || WIZ.presetDriver,
        confidence: null, unconfirmed: true });
    }
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
  // Pre-select the chosen device type if one was picked, else the best match.
  const preferred = WIZ.presetDriver &&
    WIZ.candidates.find((c) => c.driverId === WIZ.presetDriver);
  WIZ.driverId = (preferred || WIZ.candidates[0]).driverId;
  WIZ.candidates.forEach((c) => {
    const el = document.createElement("div");
    const selected = c.driverId === WIZ.driverId;
    el.className = "candidate" + (selected ? " selected" : "");
    let confHtml;
    if (c.confidence == null) {
      confHtml = `<span class="conf muted">your choice</span>`;
    } else {
      const pct = Math.round(c.confidence * 100);
      confHtml = `<span class="conf"><span class="conf-bar"><i style="width:${pct}%"></i></span>${pct}%</span>`;
    }
    el.innerHTML = `<span class="c-name">${c.displayName}</span>${confHtml}`;
    el.onclick = () => {
      WIZ.driverId = c.driverId;
      $$("#wiz-candidates .candidate").forEach((n) => n.classList.toggle("selected", n === el));
    };
    box.appendChild(el);
  });
  renderDetectHint();
}

// Warn when the chosen/expected driver wasn't actually confirmed by probing —
// almost always a wrong web-UI username/password. Also nudge when only a
// generic driver matched a credentialed connection.
function renderDetectHint() {
  const hint = $("#wiz-detecthint");
  const chosen = WIZ.candidates.find((c) => c.driverId === WIZ.driverId);
  const hadPassword = !!(WIZ.credentials &&
    (WIZ.credentials.password || WIZ.credentials.apiSecret || WIZ.credentials.apiKey));
  let msg = "";
  if (WIZ.presetLabel && chosen && (chosen.unconfirmed || (chosen.confidence != null && chosen.confidence < 0.5))) {
    msg = `We couldn't confirm this is a ${WIZ.presetLabel}. That usually means ` +
      `the username or password is wrong — go Back and re-check them, or ` +
      `continue with ${WIZ.presetLabel} anyway (it'll start working once the login is correct).`;
  } else if (!WIZ.presetDriver && hadPassword &&
             WIZ.candidates.every((c) => c.driverId.startsWith("generic."))) {
    msg = "Only a generic driver matched. If this is a specific device (switch, " +
      "AP, NAS…), the login probably failed — check the credentials, or pick the " +
      "exact device type from the dropdown on the previous step.";
  }
  hint.textContent = msg;
  hint.hidden = !msg;
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
    WIZ.supportsBinding = !!r.supportsBinding;
    WIZ.nacSupported = !!r.nacSupported;
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
  // Roam-binding opt-in: only for drivers that can enforce it (e.g. Zyxel AP).
  $("#wiz-binding-group").hidden = !WIZ.supportsBinding;
  $("#wiz-binding").checked = false;
}

$("#wiz-save").addEventListener("click", async () => {
  const err = $("#wiz-err3"); err.hidden = true;
  const keys = $$("#wiz-sensors input:checked, #wiz-controls input:checked").map((c) => ({ key: c.dataset.key }));
  const btn = $("#wiz-save"); btn.disabled = true; btn.textContent = "Adding…";
  const wantBinding = WIZ.supportsBinding && $("#wiz-binding").checked;
  try {
    const r = await api("/api/devices", { method: "POST", body: JSON.stringify({
      transport: WIZ.transport, host: WIZ.host, port: WIZ.port,
      credentials: WIZ.credentials, driverId: WIZ.driverId,
      name: $("#wiz-name").value.trim() || WIZ.host, entities: keys,
      apBinding: wantBinding,
      dashboardId: $("#wiz-dashboard").value || null }) });
    const nm = $("#wiz-name").value.trim() || WIZ.host;
    let msg = `${nm} added with ${keys.length} entities.`;
    if (wantBinding) {
      msg += r.bindingWarning
        ? ` Roam-binding couldn't be enabled — ${r.bindingWarning} You can retry once SSH is reachable.`
        : " Roam-binding is on: use the lock on each client to pin it here.";
    }
    WIZ.newDeviceId = r.device && r.device.id;
    // Offer NAC setup right after adding a capable device (e.g. OPNsense).
    const nacBtn = $("#wiz-nac");
    const nacReady = WIZ.nacSupported && !(r.device && r.device.nac && r.device.nac.configured);
    nacBtn.hidden = !nacReady;
    if (nacReady) {
      msg += " Want to control which devices get network access? Set it up now.";
      nacBtn.onclick = () => nacSetup(null, WIZ.newDeviceId);
    }
    $("#wiz-done-msg").textContent = msg;
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
  const ok = await confirmDialog({ title: `Remove user “${name}”?`,
    okLabel: "Remove", danger: true });
  if (!ok) return;
  try { await api("/api/users?id=" + encodeURIComponent(id), { method: "DELETE" }); loadUsers(); toastOk("User removed."); }
  catch (ex) { toastErr(ex.message); }
}

// ---- logs (admin diagnostics) ----------------------------------------------
let logsTimer = null;
let LOG_ENTRIES = [];

function stopLogsTimer() {
  if (logsTimer) { clearInterval(logsTimer); logsTimer = null; }
}

async function loadLogs() {
  try {
    const { logs } = await api("/api/logs");
    LOG_ENTRIES = logs || [];
    renderLogs();
  } catch (ex) {
    $("#logs-table").innerHTML = `<p class="muted">${ex.message}</p>`;
  }
  // Refresh while the Logs tab stays open (only when auto-refresh is ticked).
  stopLogsTimer();
  if ($("#logs-auto").checked) {
    logsTimer = setInterval(() => {
      if ($('[data-panel="logs"]').hidden) return stopLogsTimer();
      loadLogs();
    }, 3000);
  }
}

function renderLogs() {
  const box = $("#logs-table");
  const empty = $("#logs-empty");
  const q = ($("#logs-search").value || "").trim().toLowerCase();
  const errsOnly = $("#logs-errors-only").checked;
  const rows = LOG_ENTRIES.filter((e) => {
    const noteProblem = e.level === "error" || e.level === "warn";
    if (errsOnly && !(e.error || (e.status && e.status >= 400) || noteProblem)) return false;
    if (!q) return true;
    return [e.method, e.path, e.status, e.error, e.message, e.source]
      .filter((x) => x != null).join(" ").toLowerCase().includes(q);
  });
  box.innerHTML = "";
  empty.hidden = rows.length > 0;
  for (const e of rows) box.appendChild(logRow(e));
}

function logRow(e) {
  const row = document.createElement("div");
  row.className = "log-row";
  const t = new Date((e.ts || 0) * 1000);
  const time = document.createElement("span");
  time.className = "log-time";
  time.textContent = isNaN(t) ? "" : t.toLocaleTimeString([], { hour12: false }) +
    "." + String(t.getMilliseconds()).padStart(3, "0");
  row.appendChild(time);

  if (e.method || e.path) {
    // A request entry: METHOD, status, duration, path (+ error line if any).
    const status = e.status || 0;
    const meth = document.createElement("span");
    meth.className = "log-method"; meth.textContent = e.method || "";
    const st = document.createElement("span");
    st.className = "log-status " +
      (status >= 500 ? "s5" : status >= 400 ? "s4" : status >= 300 ? "s3" : "s2");
    st.textContent = status || "—";
    const ms = document.createElement("span");
    ms.className = "log-ms" + (e.ms != null && e.ms >= 3000 ? " slow" : "");
    ms.textContent = e.ms != null ? e.ms + "ms" : "";
    const path = document.createElement("span");
    path.className = "log-path"; path.textContent = e.path || "";
    row.append(meth, st, ms, path);
    if (e.ip) { const ip = document.createElement("span"); ip.className = "log-ip"; ip.textContent = e.ip; row.appendChild(ip); }
    if (e.error) {
      const err = document.createElement("div");
      err.className = "log-err"; err.textContent = e.error;
      if (e.trace) { err.title = e.trace; err.classList.add("has-trace"); }
      row.appendChild(err);
    }
  } else {
    // A free-form note (startup, background task).
    const lvl = document.createElement("span");
    lvl.className = "log-status " +
      (e.level === "error" ? "s5" : e.level === "warn" ? "s4" : "s2");
    lvl.textContent = (e.level || "info").toUpperCase();
    const src = document.createElement("span");
    src.className = "log-method"; src.textContent = e.source || "";
    const msg = document.createElement("span");
    msg.className = "log-path"; msg.textContent = e.message || "";
    row.append(lvl, src, msg);
  }
  return row;
}

$("#logs-refresh").addEventListener("click", loadLogs);
$("#logs-auto").addEventListener("change", loadLogs);
$("#logs-search").addEventListener("input", renderLogs);
$("#logs-errors-only").addEventListener("change", renderLogs);
$("#logs-clear").addEventListener("click", async () => {
  try { await api("/api/logs", { method: "DELETE" }); LOG_ENTRIES = []; renderLogs(); toastOk("Logs cleared."); }
  catch (ex) { toastErr(ex.message); }
});

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

// ---- settings: network access (managed aliases + DNS sync) -----------------
async function loadNacConfig() {
  const card = $("#nac-access-card");
  let cfg;
  try { cfg = await api("/api/nac/config"); }
  catch (_) { card.hidden = true; return; }
  if (!cfg.configured) { card.hidden = true; return; }  // needs NAC setup first
  card.hidden = false;
  $("#na-dns").checked = !!(cfg.dnsSync && cfg.dnsSync.enabled);
  $("#na-domain").value = (cfg.dnsSync && cfg.dnsSync.domain) || "";
  $("#na-domain-field").hidden = !$("#na-dns").checked;
  const chosen = new Set((cfg.managedAliases || []).map((a) => a.uuid));
  const box = $("#na-aliases");
  box.innerHTML = "<p class='muted'>Loading…</p>";
  let aliases = [];
  try {
    aliases = (await api(`/api/devices/${cfg.deviceId}/nac/aliases`)).aliases || [];
  } catch (ex) { box.innerHTML = ""; box.textContent = "Couldn't read aliases: " + ex.message; return; }
  box.innerHTML = "";
  if (!aliases.length) { box.textContent = "No firewall aliases found."; return; }
  for (const a of aliases) {
    const lbl = document.createElement("label"); lbl.className = "ent-item";
    const cb = document.createElement("input");
    cb.type = "checkbox"; cb.dataset.uuid = a.uuid;
    cb.dataset.name = a.name || ""; cb.dataset.atype = a.type || "";
    cb.checked = chosen.has(a.uuid);
    const sp = document.createElement("span");
    sp.textContent = (a.name || a.uuid) + (a.type ? ` · ${a.type}` : "");
    lbl.append(cb, sp); box.appendChild(lbl);
  }
}

$("#na-dns").addEventListener("change", () => {
  $("#na-domain-field").hidden = !$("#na-dns").checked;
});

$("#na-add").addEventListener("click", async () => {
  const name = $("#na-new-name").value.trim();
  if (!name) { $("#na-new-name").focus(); return; }
  const msg = $("#na-msg"); msg.hidden = false; msg.textContent = "Creating alias…";
  try {
    const r = await api("/api/nac/alias", { method: "POST",
      body: JSON.stringify({ name, type: $("#na-new-type").value }) });
    $("#na-new-name").value = "";
    msg.textContent = r.alias && r.alias.existed
      ? `“${name}” already existed — now managed.` : `Alias “${name}” created.`;
    await loadNacConfig();  // re-render with the new alias checked
  } catch (ex) { msg.textContent = ex.message; }
});

$("#na-save").addEventListener("click", async () => {
  const msg = $("#na-msg"); msg.hidden = false; msg.textContent = "Saving…";
  const managedAliases = $$("#na-aliases input[data-uuid]:checked").map((cb) => ({
    uuid: cb.dataset.uuid, name: cb.dataset.name, type: cb.dataset.atype }));
  const dnsSync = { enabled: $("#na-dns").checked, domain: $("#na-domain").value.trim() };
  try {
    await api("/api/nac/config", { method: "POST",
      body: JSON.stringify({ managedAliases, dnsSync }) });
    msg.textContent = "Saved.";
  } catch (ex) { msg.textContent = ex.message; }
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

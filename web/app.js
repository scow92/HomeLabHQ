// NetManager SPA shell. Milestone 1: auth (first-run setup + login), multi-user
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

async function loadDevices() {
  const list = $("#devices-list");
  const empty = $("#devices-empty");
  try {
    const { devices } = await api("/api/devices");
    list.innerHTML = "";
    empty.hidden = devices.length > 0;
    for (const d of devices) list.appendChild(deviceCard(d));
  } catch (ex) {
    list.innerHTML = "";
    empty.hidden = false;
  }
  // Auto-refresh the latest polled state while this tab is open.
  clearInterval(devicesTimer);
  devicesTimer = setInterval(() => {
    if (!$('[data-panel="devices"]').hidden) loadDevices();
    else { clearInterval(devicesTimer); devicesTimer = null; }
  }, 15000);
}

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
  el.className = "card";
  el.innerHTML = `
    <div class="card-row"><h2><span class="dot"></span><span class="dname"></span></h2><span class="pill"></span></div>
    <div class="muted host"></div>
    <div class="dev-state" hidden></div>
    <div class="muted updated"></div>
    <div class="dev-actions">
      <button class="btn btn-ghost btn-sm check">Check now</button>
      <button class="btn btn-danger btn-sm del">Remove</button>
    </div>`;
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
    const btn = e.target; btn.disabled = true; btn.textContent = "Checking…";
    try {
      const r = await api(`/api/devices/${d.id}/state`);
      dot.className = "dot " + (Object.keys(r.values || {}).length ? "up" : "down");
      renderState(state, r);
      updated.textContent = "updated just now";
    } catch (ex) {
      dot.className = "dot down";
      state.hidden = false;
      state.innerHTML = `<span style="color:var(--red)">${ex.message}</span>`;
    } finally { btn.disabled = false; btn.textContent = "Check now"; }
  };

  $(".del", el).onclick = async () => {
    if (!confirm(`Remove "${d.name || d.host}"?`)) return;
    try { await api(`/api/devices?id=${encodeURIComponent(d.id)}`, { method: "DELETE" }); loadDevices(); }
    catch (ex) { alert(ex.message); }
  };
  return el;
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
    msg.textContent = "Alerts need HTTPS (or localhost). Put NetManager behind TLS to enable push.";
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

let WIZ = null;

async function initWizard() {
  WIZ = { transport: null, candidates: [], driverId: null, entities: [] };
  wizGoto(1);
  $("#wiz-err1").hidden = true;
  $("#wiz-host").value = ""; $("#wiz-port").value = "";
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
    el.innerHTML = `<div class="t-name">${meta.label}</div><div class="t-sub">${meta.sub}</div>`;
    el.onclick = () => selectTransport(t, el);
    grid.appendChild(el);
  }
  $("#wiz-creds").innerHTML = `<p class="muted">Pick a connection method above.</p>`;
}

function selectTransport(t, el) {
  WIZ.transport = t;
  $$("#wiz-transports .transport-opt").forEach((n) => n.classList.toggle("selected", n === el));
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
      name: $("#wiz-name").value.trim() || WIZ.host, entities: keys }) });
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

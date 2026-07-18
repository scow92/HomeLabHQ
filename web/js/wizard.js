// Add-device wizard: connect → detect → choose entities → done.
"use strict";
import { $, $$, api } from "./api.js";
import { withBusy } from "./ui.js";
import { DASHBOARDS, currentDashboard } from "./devices.js";
import { nacSetup } from "./clients/nac-setup.js";

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

export async function initWizard() {
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

// Completed steps stay clickable so you can jump back without losing what
// you've already entered (WIZ retains every step's state).
$("#wiz-steps").addEventListener("click", (e) => {
  const li = e.target.closest("li.done");
  if (!li || !WIZ) return;
  wizGoto(Number(li.dataset.step));
});

$("#wiz-detect").addEventListener("click", async () => {
  const err = $("#wiz-err1"); err.hidden = true;
  if (!WIZ.transport) { err.textContent = "Choose a connection method."; err.hidden = false; return; }
  const host = $("#wiz-host").value.trim();
  if (!host) { err.textContent = "Enter a host or IP."; err.hidden = false; return; }
  WIZ.host = host;
  WIZ.port = $("#wiz-port").value.trim() ? Number($("#wiz-port").value.trim()) : null;
  WIZ.credentials = collectCreds();
  const btn = $("#wiz-detect");
  await withBusy(btn, "Detecting…", async () => {
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
    }
  });
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
  const btn = $("#wiz-choose");
  await withBusy(btn, "Loading…", async () => {
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
    }
  });
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
  const btn = $("#wiz-save");
  const wantBinding = WIZ.supportsBinding && $("#wiz-binding").checked;
  await withBusy(btn, "Adding…", async () => {
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
    }
  });
});

$("#wiz-another").addEventListener("click", initWizard);

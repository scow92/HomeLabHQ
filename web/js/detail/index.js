// Device detail modal: overview chips, usage donuts, metric charts, driver
// tables (interfaces / clients / radios), firewall rules, actions, alerts and
// the customize (entity picker) panel. Owns the modal shell + live tick and
// dispatches rendering to the detail/* submodules, passing the current `dm`
// snapshot into each builder rather than having them import mutable state
// back from here (see refactor.md 2.1/2.3).
"use strict";
import { $, $$, api, fmtUptime, effectiveOnline, DETAIL_ENTITY_KEYS } from "../api.js";
import { toast, toastErr, toastOk, confirmDialog, pickDialog, withBusy,
         renderError, pushModal, popModal, visiblePoll, skeletonRows,
         detailSection } from "../ui.js";
import { resetCharts, refreshCharts } from "../charts.js";
import { DASHBOARDS, driverName, renameDevice, loadDevices } from "../devices.js";
import { metricCard, donutCard } from "./metrics.js";
import { detailTable, clientsList, radiosTable } from "./tables.js";
import { interfacesSection, resetIfEdit } from "./interfaces.js";
import { firewallSection } from "./firewall.js";
import { alertsSection } from "./alerts.js";

let DM = null;  // current detail-modal state {device, entities, detail, history}

export async function openDevice(d) {
  const modal = $("#device-modal");
  // Re-opened in place (saveCustomize / changeDriver re-call this while the
  // modal is already up) — don't push a second stack entry for the same modal.
  const reopening = !modal.hidden;
  modal.hidden = false;
  document.body.style.overflow = "hidden";
  location.hash = "#/device/" + encodeURIComponent(d.id);
  $("#dm-title").textContent = d.name || d.host;
  const sub = $("#dm-sub");
  sub.textContent = `${d.host}${d.port ? ":" + d.port : ""} · ${d.transport} · `;
  const drvLink = document.createElement("button");
  drvLink.className = "linkish";
  drvLink.textContent = driverName(d.driverId);
  drvLink.title = "Change driver (" + d.driverId + ")";
  drvLink.onclick = () => changeDriver(d);
  sub.appendChild(drvLink);
  $("#dm-rename").onclick = () => renameDevice(d, (renamed) => {
    if (DM && DM.device && DM.device.id === renamed.id) {
      DM.device.name = renamed.name;
      $("#dm-title").textContent = renamed.name || renamed.host;
    }
  });
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
  const dotText = $("#dm-dot-text");
  const setDot = (up) => {
    dot.className = "dot " + (up == null ? "unknown" : up ? "up" : "down");
    if (dotText) dotText.textContent = up == null ? "Status unknown" : up ? "Online" : "Offline";
  };
  setDot(d.state ? effectiveOnline(d.state) : null);
  const body = $("#dm-body");
  body.innerHTML = "";
  body.appendChild(skeletonRows(6));
  if (!reopening) pushModal(modal, { onEscape: closeDevice });
  try {
    const data = await api(`/api/devices/${d.id}/detail`);
    DM = { device: data.device || d, entities: data.entities || [],
           detail: data.detail || {}, history: data.history || {},
           ifHistory: data.ifHistory || {}, actions: data.actions || [],
           supportsBinding: !!data.supportsBinding };
    resetIfEdit();
    const anyVal = DM.entities.some((e) => "value" in e && !e.error);
    setDot(DM.device.state && DM.device.state.online ? true : anyVal ? true : false);
    $("#dm-customize").hidden = false;
    $("#dm-customize").textContent = "Customize";
    renderDetail(body);
    startDetailLive(d.id);
  } catch (ex) {
    DM = null;
    renderError(body, "Couldn't load details: " + ex.message);
  }
}

// Real-time: while the detail modal is open, re-fetch the device every 20s and
// repaint its charts in place (no DOM rebuild) so throughput/values stay live.
const DETAIL_REFRESH_MS = 20000;
let stopDetailLive = () => {};
function startDetailLive(id) {
  stopDetailLive();
  stopDetailLive = visiblePoll(
    () => !!(DM && DM.device && DM.device.id === id) && !$("#device-modal").hidden,
    async () => {
      try {
        const data = await api(`/api/devices/${id}/detail`);
        DM.history = data.history || DM.history;
        DM.ifHistory = data.ifHistory || DM.ifHistory;
        DM.entities = data.entities || DM.entities;
        DM.detail = data.detail || DM.detail;
        refreshCharts();
      } catch (_) { /* transient; try again next tick */ }
    }, DETAIL_REFRESH_MS);
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

export function closeDevice() {
  stopDetailLive();
  $("#device-modal").hidden = true;
  document.body.style.overflow = "";
  DM = null;
  popModal();
  if (location.hash.startsWith("#/device/")) history.replaceState(null, "", "#/devices");
}

document.addEventListener("click", (e) => {
  if (e.target.closest("[data-close]")) closeDevice();
});
// Escape is handled by ui.js's shared modal-stack router (topmost modal
// first), via the onEscape passed to pushModal in openDevice().
$("#dm-customize").addEventListener("click", () => toggleCustomize());

// Device-level actions (reboot, …) as buttons. Each POSTs to the action
// endpoint; danger actions confirm first and use the destructive style.
function actionsSection() {
  const s = detailSection("Actions");
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
      await withBusy(btn, "Working…", async () => {
        try {
          const r = await api(`/api/devices/${DM.device.id}/action`, {
            method: "POST", body: JSON.stringify({ action: a.name, args: {} }) });
          toastOk((r && r.message) || `${a.label || a.name} done.`);
        } catch (ex) { toastErr(ex.message); }
      });
    };
    row.appendChild(btn);
  }
  s.appendChild(row);
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
    if (DETAIL_ENTITY_KEYS.has(e.key) || asDetail.has(e.key)) {
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
    const s = detailSection("Device details");
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
    const s = detailSection("Usage");
    const grid = document.createElement("div");
    grid.className = "donuts";
    for (const spec of detail.charts) grid.appendChild(donutCard(spec, DM));
    s.appendChild(grid);
    body.appendChild(s);
  }

  // --- Metrics (CPU / memory / clients / traffic …) ---
  if (metrics.length) {
    const s = detailSection("Metrics");
    const grid = document.createElement("div");
    grid.className = "charts";
    for (const e of metrics) grid.appendChild(metricCard(e, history, DM));
    s.appendChild(grid);
    body.appendChild(s);
  }

  // --- Driver tables (interfaces / clients / radios …) ---
  for (const t of detail.tables || []) {
    if (t.interfaces) {
      body.appendChild(interfacesSection(t, DM));
    } else {
      const s = detailSection(t.title || "Details");
      s.appendChild(
        t.layout === "clients" ? clientsList(t, DM)
        : t.layout === "radios" ? radiosTable(t, DM)
        : detailTable(t, DM));
      body.appendChild(s);
    }
  }

  // --- Firewall rules (OPNsense: toggle / rename / add, never delete) ---
  if (detail.firewall && detail.firewall.supported) {
    body.appendChild(firewallSection(DM));
  }

  // --- Roam-binding toggle (APs that can pin clients) ---
  if (DM.supportsBinding) body.appendChild(bindingSection());

  // --- Device actions (reboot, …) ---
  if ((DM.actions || []).length) body.appendChild(actionsSection());

  // --- Alerts (threshold rules → push notifications) ---
  body.appendChild(alertsSection(DM));

  if (!details.length && !metrics.length && !(detail.tables || []).length) {
    body.appendChild(Object.assign(document.createElement("p"), {
      className: "detail-empty",
      textContent: "No entities enabled. Use Customize to choose what to display.",
    }));
  }

  // --- Customize panel (hidden until toggled) ---
  body.appendChild(buildCustomize());
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
  await withBusy(btn, "Saving…", async () => {
    try {
      await api(`/api/devices/${DM.device.id}`, {
        method: "PATCH", body: JSON.stringify({ entities: keys }) });
      await openDevice(DM.device);  // re-fetch so newly enabled entities read live
      loadDevices();                // refresh card entity lists in the background
    } catch (ex) {
      toastErr(ex.message);
    }
  });
}

// Enable/disable roam-binding on an already-added AP (the wizard sets it at add
// time; this lets you turn it on/off later). Enabling re-verifies SSH server-side.
function bindingSection() {
  const s = detailSection("Roam-binding");
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
    await withBusy(btn, on ? "Turning off…" : "Checking SSH…", async () => {
      try {
        const r = await api(`/api/devices/${DM.device.id}/binding`,
          { method: "POST", body: JSON.stringify({ enabled: !on }) });
        if (!on && r.bindingWarning) {
          toastErr("Couldn't enable roam-binding — " + r.bindingWarning);
          return;
        }
        toastOk(r.device.apBinding ? "Roam-binding on" : "Roam-binding off");
        await openDevice(DM.device);   // re-fetch so client locks appear/vanish
        loadDevices();                 // refresh card in the background
      } catch (ex) {
        toastErr(ex.message);
      }
    });
  };
  row.append(desc, btn);
  s.appendChild(row);
  return s;
}

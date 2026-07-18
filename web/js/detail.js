// Device detail modal: overview chips, usage donuts, metric charts, driver
// tables (interfaces / clients / radios), firewall rules, actions, alerts and
// the customize (entity picker) panel.
"use strict";
import { $, $$, api, fmtNum, fmtUptime, fmtBitsRate, effectiveOnline, labelFor,
         cellSeverity } from "./api.js";
import { toast, toastErr, toastOk, promptDialog, confirmDialog, pickDialog, withBusy,
         renderError, openOverlay, fwIconBtn, ICON_EDIT, ICON_TRASH, pushModal, popModal,
         visiblePoll, skeletonRows } from "./ui.js";
import { makeChart, cssVar, toRate, donutSvg, donutLegend, openPieModal,
         seriesChartCard, resetCharts, refreshCharts, registerChart, registerLiveCell } from "./charts.js";
import { DASHBOARDS, driverName, renameDevice, loadDevices } from "./devices.js";

// Identity entities that belong under "Device details", never a metric graph.
const DETAIL_KEYS = new Set(["uptime", "model", "firmware", "version", "product",
  "release", "hostname", "kernel", "board", "board_name", "ports_up"]);
// Keys whose stored history is a monotonic byte counter — charted as a rate.
const RATE_KEY_RE = /octet|_bytes$|^bytes|throughput|rx_bytes|tx_bytes/i;
let ifEdit = false;  // interfaces "Edit" (remove/restore) toggle, per open

export let DM = null;  // current detail-modal state {device, entities, detail, history}

export async function openDevice(d) {
  const modal = $("#device-modal");
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
  pushModal(modal);
  try {
    const data = await api(`/api/devices/${d.id}/detail`);
    DM = { device: data.device || d, entities: data.entities || [],
           detail: data.detail || {}, history: data.history || {},
           ifHistory: data.ifHistory || {}, actions: data.actions || [],
           supportsBinding: !!data.supportsBinding };
    ifEdit = false;
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
    }, 20000);
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
    await withBusy(sw, null, async () => {
      try {
        const res = await api(`/api/devices/${dev.id}/firewall/toggle`, {
          method: "POST", body: JSON.stringify({ uuid: r.uuid, enabled: desired }) });
        r.enabled = !!res.enabled;
        toastOk(`Rule ${r.enabled ? "enabled" : "disabled"}.`);
      } catch (ex) { toastErr(ex.message); }
    });
    renderList();
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
      const p = document.createElement("p");
      p.className = "muted"; p.style.margin = "0"; p.style.fontSize = "12px";
      p.textContent = "Couldn't read rules: " + fw.error;
      list.appendChild(p);
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
  addBtn.onclick = () => withBusy(addBtn, "Loading…", async () => {
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
  });
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
      const txt = document.createElement("span");
      txt.className = "a-txt"; txt.textContent = `${nameFor(r.key)} ${sign} ${r.value}`;
      const del = document.createElement("button");
      del.className = "btn btn-ghost btn-sm"; del.textContent = "Remove";
      del.onclick = async () => {
        const next = dev.alerts.filter((_, j) => j !== i);
        await saveAlerts(next);
      };
      row.append(txt, del);
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

// `e` is an entity record ({key,name,unit,value}); `rawPoints` its history.
// Reads DM.history/DM.entities live on each refresh so the real-time tick
// repaints without a DOM rebuild.
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
    <div class="c-foot"><span class="lo"></span><span class="hi"></span></div>
    <div class="c-range"></div>`;
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
  registerChart({ refresh: render });  // repaint on the live tick
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
  const { body } = openOverlay({ title: `${cfg.title || "History"}: ${ident}` });
  body.innerHTML = `<p class="muted">Loading…</p>`;
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
    renderError(body, "Couldn't load history: " + ex.message);
  }
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
    registerLiveCell(updateRate);
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
    const p = document.createElement("p");
    p.className = "detail-empty";
    // `name` is a driver-reported interface name — device-supplied, so it must
    // go through textContent, not an innerHTML template (see REVIEW.md §5.1).
    p.textContent = `No traffic history yet for ${name} — it builds up as the ` +
      `device is polled (every ~60s).`;
    container.appendChild(p);
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
    <div class="c-foot"><span class="lo"></span><span class="hi"></span></div>
    <div class="c-range"></div>`;
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

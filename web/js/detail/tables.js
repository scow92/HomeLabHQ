// Device-detail driver tables: the generic table, the wireless-clients list
// (+ AP-lock), the radios table, and the shared per-row action button.
// `dm` (current detail-modal state) is passed in by the caller.
"use strict";
import { $$, api, cellSeverity } from "../api.js";
import { toastErr, toastOk, confirmDialog, openOverlay, renderError, buildTable } from "../ui.js";
import { openPieModal, seriesChartCard } from "../charts.js";
import { chartCard } from "./metrics.js";

// A per-row action button (e.g. "Force roam" on an AP client). POSTs the
// action + the row's arg value (a MAC) to the device action endpoint.
export function rowActionButton(a, row, dm) {
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
      const r = await api(`/api/devices/${dm.device.id}/action`, {
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

export function detailTable(t, dm) {
  const rows = t.rows || [];
  if (!rows.length) {
    const p = document.createElement("p");
    p.className = "detail-empty";
    p.textContent = "None.";
    return p;
  }
  const rowActions = t.rowActions || [];
  const cellChart = t.cellChart;
  const cellPie = t.cellPie;
  const cellFn = (td, row, c) => {
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
      td.addEventListener("click", () => openSeriesChart(cellChart, ident, dm));
      td.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter" || ev.key === " ") {
          ev.preventDefault(); openSeriesChart(cellChart, ident, dm);
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
  };
  const rowExtra = rowActions.length ? (tr, row) => {
    const td = document.createElement("td");
    td.className = "row-actions";
    for (const a of rowActions) td.appendChild(rowActionButton(a, row, dm));
    tr.appendChild(td);
  } : undefined;
  const { wrap } = buildTable({
    cols: t.columns || [], rows, cellFn, rowExtra,
    extraHeadCols: rowActions.length ? [{ className: "col-actions" }] : [],
    tableClass: rowActions.length ? "has-actions" : "",
  });
  return wrap;
}

// Popup: fetch and chart a table cell's time-series (e.g. a disk's temperature
// history). `cfg` is the table's cellChart spec; `ident` the row's id value.
async function openSeriesChart(cfg, ident, dm) {
  const { body } = openOverlay({ title: `${cfg.title || "History"}: ${ident}` });
  body.innerHTML = `<p class="muted">Loading…</p>`;
  try {
    const q = `metric=${encodeURIComponent(cfg.metric)}&id=${encodeURIComponent(ident)}`;
    const data = await api(`/api/devices/${dm.device.id}/series?${q}`);
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
export function clientsList(t, dm) {
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
      head.appendChild(lockCircle(row, dm));
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
      for (const a of rowActions) acts.appendChild(rowActionButton(a, row, dm));
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
export function radiosTable(t, dm) {
  const cols = t.columns || [];
  const wrap = document.createElement("div");
  const table = detailTable(t, dm);   // reuse the generic table renderer + styling
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
                                 (dm.history && dm.history[key]) || [], dm));
      }
    };
  });
  return wrap;
}

const LOCK_TITLE = {
  here: "Locked to this AP — tap to unlock",
  elsewhere: "Locked to another AP — tap to lock here",
  "": "Tap to lock this client to this AP",
};

// The bind circle for one client row. Colour reflects row.lock; tap toggles the
// binding via the API and recolours in place (no full re-render).
function lockCircle(row, dm) {
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
      await api(`/api/devices/${dm.device.id}/bind-client`,
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

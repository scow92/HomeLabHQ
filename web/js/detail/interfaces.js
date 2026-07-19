// Device-detail interfaces section: the interfaces table with per-row
// throughput + click-to-expand history chart, and the edit/remove-restore
// toggle. `dm` (current detail-modal state) is passed in by the caller.
"use strict";
import { $, api, fmtBitsRate } from "../api.js";
import { toastErr, buildTable } from "../ui.js";
import { makeChart, cssVar, toRate, registerLiveCell } from "../charts.js";

let ifEdit = false;  // interfaces "Edit" (remove/restore) toggle, per open

// Current per-interface throughput (bytes/sec) from the last two counter
// samples in dm.ifHistory. Returns {down, up} (null when no history yet).
function ifRate(id, dm) {
  const ifh = (dm.ifHistory || {})[id] || {};
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

// Reset the "Edit" toggle — call when a fresh device is opened.
export function resetIfEdit() {
  ifEdit = false;
}

export function interfacesSection(t, dm) {
  const idKey = t.idKey || "device";
  const hidden = new Set((dm.device.hiddenInterfaces || []).map(String));
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
    dm.device.hiddenInterfaces = [...hidden];
    try {
      await api(`/api/devices/${dm.device.id}`, {
        method: "PATCH", body: JSON.stringify({ hiddenInterfaces: [...hidden] }) });
    } catch (ex) { toastErr(ex.message); }
  }

  function render() {
    editBtn.textContent = ifEdit ? "Done" : "Edit";
    const rows = t.rows || [];
    const visible = rows.filter((r) => !hidden.has(String(r[idKey])));
    tableBox.innerHTML = "";
    tableBox.appendChild(ifTable(t, visible, idKey, hidden, chartBox, saveHidden, render, dm));
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

function ifTable(t, rows, idKey, hidden, chartBox, saveHidden, rerender, dm) {
  const cols = t.columns || [];
  if (!rows.length) {
    return Object.assign(document.createElement("p"),
      { className: "detail-empty", textContent: "No interfaces shown." });
  }
  const cellFn = (td, row, c) => {
    const v = row[c.key];
    td.textContent = v == null || v === "" ? "–" : String(v);
    if (/mac|tx|rx|status/i.test(c.key)) td.className = "mono";
  };
  const rowExtra = (tr, row) => {
    const id = String(row[idKey]);
    // Live rate cell — current down/up throughput, updated on each real-time
    // tick from dm.ifHistory without rebuilding the table.
    const rateTd = document.createElement("td");
    rateTd.className = "if-rate mono";
    rateTd.innerHTML = `<span class="r-dn"></span><span class="r-up"></span>`;
    tr.appendChild(rateTd);
    const updateRate = () => {
      if (!rateTd.isConnected) return;
      const { down, up } = ifRate(id, dm);
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
  };
  const { wrap, tbody } = buildTable({
    cols, rows, cellFn, rowExtra,
    extraHeadCols: [{ label: "Rate ↓↑" }, ...(ifEdit ? [{ label: "" }] : [])],
    tableClass: "if-table-el" + (ifEdit ? "" : " rows-clickable"),
  });
  // has-history styling and the click-to-chart/select behaviour are keyed off
  // the row's id, so wire them up positionally against `rows` after the
  // shared table shell has built the <tr>s.
  [...tbody.children].forEach((tr, i) => {
    const row = rows[i];
    const id = String(row[idKey]);
    const ifh = (dm.ifHistory || {})[id];
    const hasHist = ifh && ((ifh.rx || []).length >= 2 || (ifh.tx || []).length >= 2);
    if (hasHist && !ifEdit) tr.classList.add("has-history");
    tr.onclick = () => {
      if (ifEdit) return;
      showIfChart(chartBox, id, row.name || id, dm);
      [...tbody.children].forEach((r) => r.classList.remove("sel"));
      tr.classList.add("sel");
    };
  });
  return wrap;
}

function showIfChart(container, id, name, dm) {
  const ifh = (dm.ifHistory || {})[id] || {};
  const rx = ifh.rx || [], tx = ifh.tx || [];
  dm.selectedIf = id;  // remembered so real-time refresh keeps this chart live
  container.hidden = false;
  container.innerHTML = "";
  if (rx.length < 2 && tx.length < 2) {
    const p = document.createElement("p");
    p.className = "detail-empty";
    // `name` is a driver-reported interface name — device-supplied, so it must
    // go through textContent, not an innerHTML template.
    p.textContent = `No traffic history yet for ${name} — it builds up as the ` +
      `device is polled (every ~60s).`;
    container.appendChild(p);
    return;
  }
  container.appendChild(dualChartCard(name, id, dm));
}

// Interactive upload/download throughput for one interface. Reads fresh history
// from dm.ifHistory[id] on each (real-time) refresh.
function dualChartCard(name, id, dm) {
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
    const ifh = (dm.ifHistory || {})[id] || {};
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

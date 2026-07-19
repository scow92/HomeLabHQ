// Device-detail metric cards: value-only cards, history-chart cards, and the
// usage-donut cards. `dm` (the current detail-modal state) is passed in
// rather than imported, so this module never needs a back-reference to
// detail/index.js.
"use strict";
import { $, $$, api, fmtNum, fmtBitsRate, labelFor } from "../api.js";
import { makeChart, cssVar, toRate, donutSvg, donutLegend, registerChart } from "../charts.js";

// Keys whose stored history is a monotonic byte counter — charted as a rate.
const RATE_KEY_RE = /octet|_bytes$|^bytes|throughput|rx_bytes|tx_bytes/i;

// A metric renders as a history chart when it has a numeric series, otherwise a
// value-only card.
export function metricCard(e, history, dm) {
  const pts = history[e.key] || [];
  const numericHist = pts.length >= 2 && pts.every((p) => typeof p[1] === "number");
  if (numericHist && !e.error) return chartCard(e, pts, dm);

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

// How often an open 24h/7d chart re-fetches its long series. Matches the
// server's downsample interval (history.LONG_INTERVAL) — refetching faster
// can never show a new point.
const LONG_RANGE_REFRESH_MS = 5 * 60 * 1000;

// `e` is an entity record ({key,name,unit,value}); `rawPoints` its history.
// Reads dm.history/dm.entities live on each refresh so the real-time tick
// repaints without a DOM rebuild — safe because startDetailLive() mutates the
// same `dm` object in place rather than replacing it. The 2h range charts the
// live full-resolution series; 24h/7d fetch the server's downsampled long
// series on demand.
export function chartCard(e, rawPoints, dm) {
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
    <div class="c-rangebar"><span class="c-range"></span><span class="c-ranges"></span></div>`;
  $(".c-title", card).textContent = e.name || labelFor(key);
  let range = "2h";        // "2h" (live series) | "24h" | "7d" (fetched)
  let longPts = [];        // last-fetched long series for the current range
  let lastFetch = 0;
  const seriesFn = () => {
    const hist = range === "2h"
      ? (dm.history && dm.history[key]) || rawPoints || []
      : longPts;
    const pts = isRate ? toRate(hist) : hist;
    return [{ points: pts, color: cssVar("--accent"), label: e.name || labelFor(key) }];
  };
  const headFn = (c, series) => {
    const vals = series[0].points.map((p) => p[1]);
    const liveVal = dm.entities.find((x) => x.key === key);
    const now = isRate ? (vals.length ? vals[vals.length - 1] : null)
      : (liveVal && typeof liveVal.value === "number" ? liveVal.value
        : (vals.length ? vals[vals.length - 1] : null));
    $(".c-now", c).textContent = now == null ? "–" : fmt(now);
    $(".lo", c).textContent = "min " + (vals.length ? fmt(Math.min(...vals)) : "–");
    $(".hi", c).textContent = "peak " + (vals.length ? fmt(Math.max(...vals)) : "–");
  };
  const chart = makeChart({ card, seriesFn, fmt, headFn, fromZero: isRate });

  async function fetchLong(r) {
    try {
      const res = await api(`/api/devices/${dm.device.id}/history` +
        `?key=${encodeURIComponent(key)}&range=${r}`);
      if (range !== r || !card.isConnected) return;  // switched away meanwhile
      longPts = res.series || [];
      lastFetch = Date.now();
      chart.refresh();
    } catch (_) { /* keep whatever's on screen; retried on the next tick */ }
  }
  const ranges = $(".c-ranges", card);
  for (const r of ["2h", "24h", "7d"]) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "c-range-btn" + (r === range ? " active" : "");
    b.textContent = r;
    b.setAttribute("aria-pressed", String(r === range));
    b.onclick = () => {
      range = r;
      longPts = []; lastFetch = 0;
      $$(".c-range-btn", card).forEach((n) => {
        const active = n === b;
        n.classList.toggle("active", active);
        n.setAttribute("aria-pressed", String(active));
      });
      if (r === "2h") chart.refresh();
      else fetchLong(r);
    };
    ranges.appendChild(b);
  }
  // The long series only gains a point every LONG_INTERVAL, so an open
  // 24h/7d chart refetches lazily on the live tick instead of every 20s.
  registerChart({ refresh() {
    if (range !== "2h" && card.isConnected &&
        Date.now() - lastFetch > LONG_RANGE_REFRESH_MS) fetchLong(range);
  } });
  return card;
}

// Re-read this spec's latest values from live detail data (matched by title) so
// the 20s refresh repaints memory/pool donuts in place as usage shifts.
function liveDonutSpec(spec, dm) {
  const list = (dm && dm.detail && dm.detail.charts) || [];
  return list.find((c) => c.title === spec.title) || spec;
}

export function donutCard(spec, dm) {
  const card = document.createElement("div");
  card.className = "donut-card";
  const render = () => {
    const s = liveDonutSpec(spec, dm);
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

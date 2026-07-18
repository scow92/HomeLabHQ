// Device-detail metric cards: value-only cards, history-chart cards, and the
// usage-donut cards. `dm` (the current detail-modal state) is passed in
// rather than imported, so this module never needs a back-reference to
// detail/index.js (see refactor.md 2.1/2.3).
"use strict";
import { $, fmtNum, fmtBitsRate, labelFor } from "../api.js";
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

// `e` is an entity record ({key,name,unit,value}); `rawPoints` its history.
// Reads dm.history/dm.entities live on each refresh so the real-time tick
// repaints without a DOM rebuild — safe because startDetailLive() mutates the
// same `dm` object in place rather than replacing it.
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
    <div class="c-range"></div>`;
  $(".c-title", card).textContent = e.name || labelFor(key);
  const seriesFn = () => {
    const hist = (dm.history && dm.history[key]) || rawPoints || [];
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
  makeChart({ card, seriesFn, fmt, headFn, fromZero: isRate });
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

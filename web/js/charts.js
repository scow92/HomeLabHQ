// Generic, state-free chart rendering: the canvas line-chart engine and the
// SVG donut primitives. Nothing here reads device/detail state (DM) — the
// detail module builds device-aware cards (chartCard, donutCard, …) on top of
// these primitives.
"use strict";
import { $, fmtClock, fmtNum } from "./api.js";
import { openOverlay } from "./ui.js";

// Turn a monotonic counter series into a per-second rate series.
export function toRate(points) {
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

export function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || "#4aa8ff";
}

// ---- chart registry -----------------------------------------------------
// Charts/donuts open in the detail modal register here so the real-time
// refresh can recompute their data and repaint in place — no DOM rebuild, no
// lost hover. Kept private; callers use register*/reset/refresh.
let CHART_REG = [];
let LIVE_CELLS = [];
export function resetCharts() { CHART_REG = []; LIVE_CELLS = []; }
export function refreshCharts() {
  for (const c of CHART_REG) { try { c.refresh(); } catch (_) {} }
  for (const f of LIVE_CELLS) { try { f(); } catch (_) {} }
}
export function registerChart(entry) { CHART_REG.push(entry); }
export function registerLiveCell(fn) { LIVE_CELLS.push(fn); }

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

// First/last timestamp of the data currently on the chart — the "can't tell if
// a spike was 2 minutes or 2 hours ago" cheap fix from the review: a range
// readout next to the hover tooltip's per-point time.
function updateRange(card, state) {
  const el = $(".c-range", card);
  if (!el) return;
  const xs = state.series.flatMap((s) => s.points.map((p) => p[0]));
  if (!xs.length) { el.textContent = ""; return; }
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  el.textContent = x1 > x0 ? `${fmtClock(x0)} – ${fmtClock(x1)}` : fmtClock(x0);
}

// Build an interactive line chart on the <canvas> inside `card`.
//   seriesFn() -> [{points:[[ts,val]], color, label}]  (recomputed on refresh)
//   fmt(v) -> string ; headFn(card, series) updates header/legend readouts.
export function makeChart({ card, seriesFn, fmt, headFn, fromZero }) {
  const canvas = $("canvas", card);
  const tip = document.createElement("div");
  tip.className = "chart-tip"; tip.hidden = true;
  card.appendChild(tip);
  const state = { series: [], fmt, hover: null, fromZero: !!fromZero };

  function recompute() {
    state.series = seriesFn() || [];
    if (headFn) headFn(card, state.series);
    updateRange(card, state);
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
  const entry = { refresh() {
    if (!canvas.isConnected) return;      // a superseded interface chart
    recompute();
    if (state.hover == null) paintChart(canvas, state);  // don't fight a hover
  } };
  registerChart(entry);
  // Handed back so a caller can repaint after changing what seriesFn returns
  // (e.g. the chart card's time-range switch), without waiting for the tick.
  return entry;
}

// ---- usage donuts (SVG pie charts) -------------------------------------------
const DONUT_TONE = { used: "--accent", cache: "--amber", free: "--muted" };
const SVG_NS = "http://www.w3.org/2000/svg";

function donutColor(tone) {
  return cssVar(DONUT_TONE[tone] || "--accent");
}

export function donutSvg(s) {
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

export function donutLegend(s) {
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

// Popup: render a donut breakdown for a table row (e.g. a node's memory pie).
// Pure — takes the spec directly, no device-detail state involved.
export function openPieModal(spec) {
  const { body } = openOverlay({ title: spec.title || "Breakdown" });
  const wrap = document.createElement("div");
  wrap.className = "donut-wrap";
  wrap.append(donutSvg(spec), donutLegend(spec));
  body.appendChild(wrap);
}

// A standalone (not history-backed) line chart over a fixed point array, used
// by the cell-chart popup (e.g. a disk's temperature history).
export function seriesChartCard(e, pts) {
  const unit = e.unit ? " " + e.unit : "";
  const fmt = (v) => fmtNum(v) + unit;
  const card = document.createElement("div");
  card.className = "chart-card series-chart";
  card.innerHTML = `
    <div class="c-head"><span class="c-title"></span><span class="c-now"></span></div>
    <canvas></canvas>
    <div class="c-foot"><span class="lo"></span><span class="hi"></span></div>
    <div class="c-range"></div>`;
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

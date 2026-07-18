// Foundational module: DOM shorthands, the session-aware fetch wrapper, and
// the value/time formatters shared across every other module. Every other
// module in web/js/ imports from here — this one imports nothing.
"use strict";

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

export let SESSION = null; // { id, username, role }
export function setSession(s) { SESSION = s; }

export async function api(path, opts = {}) {
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

export function timeAgo(ts) {
  if (!ts) return "never";
  const s = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (s < 60) return s + "s ago";
  if (s < 3600) return Math.floor(s / 60) + "m ago";
  if (s < 86400) return Math.floor(s / 3600) + "h ago";
  return Math.floor(s / 86400) + "d ago";
}

export function fmtBytes(n, perSec = false) {
  if (n == null || isNaN(n)) return "–";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0, v = Math.abs(n);
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return `${(n < 0 ? -v : v).toFixed(v >= 100 || i === 0 ? 0 : 1)} ${u[i]}${perSec ? "/s" : ""}`;
}

export function fmtNum(n) {
  if (n == null || isNaN(n)) return "–";
  return Math.abs(n) >= 1000 ? Math.round(n).toLocaleString() : String(Math.round(n * 10) / 10);
}

// Format a throughput given in BYTES/second as a bits/second rate — the network
// convention (a "100 Mbps" link, not "12.5 MB/s"). Decimal (1000) units.
export function fmtBitsRate(bytesPerSec) {
  if (bytesPerSec == null || isNaN(bytesPerSec)) return "–";
  let bits = Math.abs(bytesPerSec) * 8;
  const u = ["bps", "Kbps", "Mbps", "Gbps", "Tbps"];
  let i = 0;
  while (bits >= 1000 && i < u.length - 1) { bits /= 1000; i++; }
  const v = bytesPerSec < 0 ? -bits : bits;
  return `${v.toFixed(v >= 100 || i === 0 ? 0 : 1)} ${u[i]}`;
}

export function fmtUptime(sec) {
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

export function fmtClock(ts) {
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
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
export function labelFor(key) {
  if (ENTITY_LABELS[key]) return ENTITY_LABELS[key];
  return key.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

// Identity entities that belong under device-detail "Device details", never a
// metric graph or an alert target. Shared by detail/index.js (partitioning)
// and detail/alerts.js (excluding them from the alertable-sensor list).
export const DETAIL_ENTITY_KEYS = new Set(["uptime", "model", "firmware", "version", "product",
  "release", "hostname", "kernel", "board", "board_name", "ports_up"]);

// Colour a table cell by meaning, matching Network Manager's cues: WiFi signal
// green/amber/red by dBm, link/status up=green down=red, error counts red.
// Shared by the clients table and the device-detail driver tables.
export function cellSeverity(key, v) {
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

// The poller debounces reachability (confirmedOnline flips only after several
// consecutive missed polls) so notifications don't flap on slow management
// planes. Render that debounced state, not the raw per-poll `online`, so the
// UI agrees with what actually triggers a notification. Falls back to `online`
// for state records from before confirmedOnline existed.
export function effectiveOnline(s) {
  return s.confirmedOnline !== undefined ? s.confirmedOnline : s.online;
}

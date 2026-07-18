// Logs (admin) tab: recent API requests + errors, for diagnostics.
"use strict";
import { $, api } from "./api.js";
import { toastOk, toastErr, visiblePoll } from "./ui.js";

let LOG_ENTRIES = [];
let stopLive = () => {};

export async function loadLogs() {
  try {
    const { logs } = await api("/api/logs");
    LOG_ENTRIES = logs || [];
    renderLogs();
  } catch (ex) {
    $("#logs-table").innerHTML = "";
    const p = document.createElement("p");
    p.className = "muted"; p.textContent = ex.message;
    $("#logs-table").appendChild(p);
  }
  // Refresh while the Logs tab stays open (only when auto-refresh is ticked).
  stopLive();
  if ($("#logs-auto").checked) {
    stopLive = visiblePoll("logs", loadLogs, 3000);
  }
}

export function stopLogsTimer() { stopLive(); }

function renderLogs() {
  const box = $("#logs-table");
  const empty = $("#logs-empty");
  const q = ($("#logs-search").value || "").trim().toLowerCase();
  const errsOnly = $("#logs-errors-only").checked;
  const rows = LOG_ENTRIES.filter((e) => {
    const noteProblem = e.level === "error" || e.level === "warn";
    if (errsOnly && !(e.error || (e.status && e.status >= 400) || noteProblem)) return false;
    if (!q) return true;
    return [e.method, e.path, e.status, e.error, e.message, e.source]
      .filter((x) => x != null).join(" ").toLowerCase().includes(q);
  });
  // Pin the scroll position across a background refresh instead of snapping the
  // page back to the top mid-read — unless the user was already at the top, in
  // which case staying there keeps the newest entries in view.
  const scrollY = window.scrollY;
  box.innerHTML = "";
  empty.hidden = rows.length > 0;
  if (!rows.length) {
    const filtered = LOG_ENTRIES.length > 0 && (q || errsOnly);
    $(".le-msg", empty).textContent = filtered
      ? "No log entries match the current filter." : "No log entries yet.";
  }
  for (const e of rows) box.appendChild(logRow(e));
  if (scrollY > 40) window.scrollTo(0, scrollY);
}

function logRow(e) {
  const row = document.createElement("div");
  row.className = "log-row";
  const t = new Date((e.ts || 0) * 1000);
  const time = document.createElement("span");
  time.className = "log-time";
  time.textContent = isNaN(t) ? "" : t.toLocaleTimeString([], { hour12: false }) +
    "." + String(t.getMilliseconds()).padStart(3, "0");
  row.appendChild(time);

  if (e.method || e.path) {
    // A request entry: METHOD, status, duration, path (+ error line if any).
    const status = e.status || 0;
    const meth = document.createElement("span");
    meth.className = "log-method"; meth.textContent = e.method || "";
    const st = document.createElement("span");
    st.className = "log-status " +
      (status >= 500 ? "s5" : status >= 400 ? "s4" : status >= 300 ? "s3" : "s2");
    st.textContent = status || "—";
    const ms = document.createElement("span");
    ms.className = "log-ms" + (e.ms != null && e.ms >= 3000 ? " slow" : "");
    ms.textContent = e.ms != null ? e.ms + "ms" : "";
    const path = document.createElement("span");
    path.className = "log-path"; path.textContent = e.path || "";
    row.append(meth, st, ms, path);
    if (e.ip) { const ip = document.createElement("span"); ip.className = "log-ip"; ip.textContent = e.ip; row.appendChild(ip); }
    if (e.error) {
      const err = document.createElement("div");
      err.className = "log-err"; err.textContent = e.error;
      if (e.trace) { err.title = e.trace; err.classList.add("has-trace"); }
      row.appendChild(err);
    }
  } else {
    // A free-form note (startup, background task).
    const lvl = document.createElement("span");
    lvl.className = "log-status " +
      (e.level === "error" ? "s5" : e.level === "warn" ? "s4" : "s2");
    lvl.textContent = (e.level || "info").toUpperCase();
    const src = document.createElement("span");
    src.className = "log-method"; src.textContent = e.source || "";
    const msg = document.createElement("span");
    msg.className = "log-path"; msg.textContent = e.message || "";
    row.append(lvl, src, msg);
  }
  return row;
}

$("#logs-refresh").addEventListener("click", loadLogs);
$("#logs-auto").addEventListener("change", loadLogs);
$("#logs-search").addEventListener("input", renderLogs);
$("#logs-errors-only").addEventListener("change", renderLogs);
$("#logs-clear").addEventListener("click", async () => {
  try { await api("/api/logs", { method: "DELETE" }); LOG_ENTRIES = []; renderLogs(); toastOk("Logs cleared."); }
  catch (ex) { toastErr(ex.message); }
});

// Filter state belongs to this module; rendering remains owned by index.js.
"use strict";
import { onSessionChange } from "../api.js";
let query = "";
let status = "all";
let view = null;
let sort = "hostname";
try { sort = localStorage.getItem("hlhq-clients-sort") || sort; } catch (_) {}
if (sort === "status") sort = "hostname";
onSessionChange(() => {
  query = ""; status = "all"; view = null;
  document.querySelector("#clients-search").value = "";
  document.querySelector("#clients-status").value = "all";
  document.querySelector("#clients-search-clear").hidden = true;
});

export const isOnline = (client) => client.online !== false;
export const getFilters = () => ({ query, status, sort, view });

// IPv4 first, then IPv6, then missing/invalid addresses. URL parsing validates
// IPv6 (including embedded IPv4); expand its canonical form for numeric order.
function addressKey(value = "") {
  if (/^\d+\.\d+\.\d+\.\d+$/.test(value)) {
    const bytes = value.split(".").map(Number);
    if (bytes.every(n => n <= 255)) return "0" + bytes.map(n => n.toString(16).padStart(2, "0")).join("");
  }
  try {
    const host = new URL(`http://[${value}]/`).hostname.slice(1, -1);
    const [left, right] = host.split("::");
    const a = left ? left.split(":") : [], b = right ? right.split(":") : [];
    const words = right === undefined ? a : [...a, ...Array(8 - a.length - b.length).fill("0"), ...b];
    return "1" + words.map(word => word.padStart(4, "0")).join("");
  } catch (_) { return "2"; }
}
const compare = (a, b) => a < b ? -1 : a > b ? 1 : 0;
const clientName = client => (client.hostname || client.ip || client.mac || "").toLowerCase();
export function sortClients(rows, selectedSort = sort) {
  return rows.slice().sort((a, b) => {
    const tie = () => compare(clientName(a), clientName(b)) || compare(a.mac || "", b.mac || "");
    if (selectedSort === "ip") return compare(addressKey(a.ip), addressKey(b.ip)) || tie();
    if (selectedSort === "mac") return compare(a.mac || "", b.mac || "") || tie();
    if (selectedSort === "signal") return (b.signal ?? -Infinity) - (a.signal ?? -Infinity) || tie();
    if (selectedSort === "lastseen") return (b.lastSeen ?? 0) - (a.lastSeen ?? 0) || tie();
    return tie();
  });
}
export function matchesClient(client) {
  if (status === "online" && !isOnline(client)) return false;
  if (status === "offline" && isOnline(client)) return false;
  if (!query) return true;
  const text = `${client.name || ""} ${client.hostname || ""} ${client.ip || ""} ${client.mac || ""} ` +
    `${client.kind || ""} ${client.vendor || ""} ${client.via || ""} ` +
    (client.seen || []).map((seen) => `${seen.via || ""} ${seen.where || ""}`).join(" ");
  return query.split(/\s+/).every((term) => text.toLowerCase().includes(term));
}

export function bindFilters({ hasClients, render }) {
  document.querySelector("#clients-view").addEventListener("change", event => {
    view = event.target.value; if (hasClients()) render();
  });
  const input = document.querySelector("#clients-search");
  const clear = document.querySelector("#clients-search-clear");
  if (input) {
    input.addEventListener("input", () => {
      query = input.value.trim().toLowerCase(); clear.hidden = !input.value;
      if (hasClients()) render();
    });
    clear.addEventListener("click", () => {
      input.value = ""; query = ""; clear.hidden = true;
      if (hasClients()) render(); input.focus();
    });
  }
  const statusSelect = document.querySelector("#clients-status");
  if (statusSelect) statusSelect.addEventListener("change", () => {
    status = statusSelect.value; if (hasClients()) render();
  });
  const sortSelect = document.querySelector("#clients-sort");
  if (sortSelect) {
    sortSelect.value = sort;
    sortSelect.addEventListener("change", () => {
      sort = sortSelect.value;
      try { localStorage.setItem("hlhq-clients-sort", sort); } catch (_) {}
      if (hasClients()) render();
    });
  }
}

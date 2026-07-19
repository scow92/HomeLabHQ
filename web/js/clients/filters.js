// Filter state belongs to this module; rendering remains owned by index.js.
"use strict";
let query = "";
let status = "all";
let sort = "hostname";
try { sort = localStorage.getItem("hlhq-clients-sort") || sort; } catch (_) {}
if (sort === "status") sort = "hostname";

export const isOnline = (client) => client.online !== false;
export const getFilters = () => ({ query, status, sort });
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

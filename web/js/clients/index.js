// Clients feature coordinator. It is the only module allowed to combine
// client state, transport, rendering, and feature actions.
"use strict";
import { $, SESSION } from "../api.js";
import { visiblePoll, skeletonCards, renderError, toastErr, withBusy } from "../ui.js";
import { fetchClients, fetchClientEventCount, refreshClients } from "./api.js";
import { getClients, setClients, invalidateClients, removeClient } from "./store.js";
import { bindFilters } from "./filters.js";
import { renderClientGrid } from "./grid.js";
import { approveClient, forgetClient, ignoreOneClient, bulkActions, toggleEnforcement } from "./actions.js";
import { openClientEdit } from "./edit-modal.js";
import { nacSetup } from "./nac-setup.js";

export { invalidateClients } from "./store.js";

export function renderClients() {
  const roster = getClients();
  if (!roster) return;
  renderClientGrid(roster, {
    approve: (client, nac, approved, button) => approveClient(client, nac, approved, button, renderClients),
    forget: (client, button) => forgetClient(client, button, { remove: removeClient, render: renderClients }),
    ignore: (client, button) => ignoreOneClient(client, button, { remove: removeClient, render: renderClients }),
    edit: (client, options) => openClientEdit(client, { ...options, onComplete: renderClients }),
    setup: (nac) => nacSetup(nac, null, { onComplete: reloadAfterSetup }),
    enforcement: (nac, enabled, button) => toggleEnforcement(nac, enabled, button, renderClients),
  });
}

export async function loadClients() {
  const body = $("#clients-body");
  if (!getClients()) { body.innerHTML = ""; body.appendChild(skeletonCards(4)); }
  try {
    setClients(await fetchClients()); renderClients(); markAccessSeen();
  } catch (error) {
    if (getClients()) toastErr(`Couldn't refresh clients: ${error.message}`);
    else renderError(body, `Couldn't load clients: ${error.message}`);
  }
}

async function reloadAfterSetup() {
  invalidateClients(); await loadClients();
  document.dispatchEvent(new CustomEvent("hlhq:navigate", { detail: { tab: "clients" } }));
}

const accessSeenKeyPrefix = "hlhq-access-seen:";
const accessBadgePollMs = 60000;
let accessBadgeGeneration = 0;

// Access activity belongs to the signed-in owner.  Do not share a “last seen”
// timestamp between accounts that happen to use the same browser profile.
function accessSeenKey() { return accessSeenKeyPrefix + (SESSION?.id || "unknown"); }
function accessSeenTs() { try { return Number(localStorage.getItem(accessSeenKey())) || 0; } catch (_) { return 0; } }
function markAccessSeen() {
  accessBadgeGeneration += 1;
  try { localStorage.setItem(accessSeenKey(), String(Math.floor(Date.now() / 1000))); } catch (_) {}
  renderAccessBadge(0);
}
function renderAccessBadge(count) {
  const tab = $('.tab[data-tab="clients"]'); if (!tab) return;
  let badge = $(".tab-badge", tab);
  if (!count) { if (badge) badge.remove(); return; }
  if (!badge) { badge = document.createElement("span"); badge.className = "tab-badge"; tab.appendChild(badge); }
  badge.textContent = count > 99 ? "99+" : String(count);
  badge.title = `${count} connection event${count === 1 ? "" : "s"} since you last looked`;
}
async function pollAccessBadge() {
  const panel = $('[data-panel="clients"]');
  if (panel && !panel.hidden) { markAccessSeen(); return; }
  const since = accessSeenTs();
  // A browser that has never opened Access has no meaningful “unread since”
  // point. Establish one now instead of presenting the entire retained event
  // history as a fresh notification count.
  if (!since) { markAccessSeen(); return; }
  const generation = accessBadgeGeneration;
  try {
    const { count } = await fetchClientEventCount(since);
    // A navigation to Access while the request was pending marks events seen.
    // Do not let that older response recreate the badge afterward.
    if (generation === accessBadgeGeneration && panel?.hidden) renderAccessBadge(count || 0);
  } catch (_) {}
}
let stopAccessBadge = null;
export function startAccessBadge() {
  if (stopAccessBadge) stopAccessBadge();
  accessBadgeGeneration += 1;
  pollAccessBadge();
  stopAccessBadge = visiblePoll(() => !$("#app").hidden, pollAccessBadge, accessBadgePollMs);
}

bindFilters({ hasClients: () => !!getClients(), render: renderClients });
const refresh = $("#clients-refresh");
if (refresh) refresh.addEventListener("click", () => withBusy(refresh, "↻ Scanning…", async () => {
  setClients(await refreshClients()); renderClients(); markAccessSeen();
}));
const menu = $("#clients-menu");
if (menu) menu.addEventListener("click", () => bulkActions(getClients(), loadClients));
// Other flows (such as the add-device wizard) can request a roster reload
// without importing this feature's mutable state.
document.addEventListener("hlhq:clients-changed", reloadAfterSetup);

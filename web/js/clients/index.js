// Clients feature coordinator. It is the only module allowed to combine
// client state, transport, rendering, and feature actions.
"use strict";
import { $, SESSION, onSessionChange, getSessionGeneration, isCurrentSession } from "../api.js";
import { refreshState } from "../refresh-state.js";
import { requestOwner } from "../request-owner.js";
import { visiblePoll, skeletonCards, renderError, toastErr, withBusy } from "../ui.js";
import { fetchClients, fetchClientEventSummary, refreshClients } from "./api.js";
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

const rosterRequests = requestOwner();
const rosterState = refreshState("clients-refresh-state", $("#clients-body"), "Clients", loadClients);
const scanState = refreshState("clients-scan-state", $("#clients-body"), "Client scan", scanClients);
export function stopClients() { rosterRequests.invalidate(); }
onSessionChange(stopClients);
export async function loadClients() {
  if (!SESSION) return;
  const request = rosterRequests.begin(() => !$('[data-panel="clients"]').hidden);
  rosterState.start();
  const body = $("#clients-body");
  if (!getClients()) { body.innerHTML = ""; body.appendChild(skeletonCards(4)); }
  try {
    const roster = await fetchClients(request);
    if (!request.current()) return;
    setClients(roster); renderClients(); markAccessSeen(); rosterState.success();
  } catch (error) {
    if (!request.current()) return;
    rosterState.fail(error);
    if (!getClients()) body.replaceChildren();
  }
}

async function reloadAfterSetup() {
  invalidateClients(); await loadClients();
  document.dispatchEvent(new CustomEvent("hlhq:navigate", { detail: { tab: "clients" } }));
}

const accessSeenKeyPrefix = "hlhq-access-seen:";
const accessBadgePollMs = 60000;
let accessBadgeGeneration = 0;
const badgeRequests = requestOwner();
const badgeState = refreshState("access-activity-refresh-state", $("#clients-body"), "Access activity", pollAccessBadge);
let badgeRead = null;

// Access activity belongs to the signed-in owner.  Do not share a “last seen”
// timestamp between accounts that happen to use the same browser profile.
function accessSeenKey() { return accessSeenKeyPrefix + (SESSION?.id || "unknown"); }
function accessSeenTs() { try { return Number(localStorage.getItem(accessSeenKey())) || 0; } catch (_) { return 0; } }
function markAccessSeen() {
  badgeState.reset(); delete $('.tab[data-tab="clients"]').dataset.degraded;
  badgeRequests.invalidate();
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
  badge.title = `${count} new device${count === 1 ? "" : "s"} since you last looked`;
}
async function pollAccessBadge() {
  if (badgeRead?.current()) return;
  const panel = $('[data-panel="clients"]');
  if (panel && !panel.hidden) { markAccessSeen(); return; }
  const since = accessSeenTs();
  // A browser that has never opened Access has no meaningful “unread since”
  // point. Establish one now instead of presenting the entire retained event
  // history as a fresh notification count.
  if (!since) { markAccessSeen(); return; }
  const generation = accessBadgeGeneration;
  const request = badgeRequests.begin(() => generation === accessBadgeGeneration && !!panel?.hidden);
  badgeRead = request;
  try {
    const { newCount } = await fetchClientEventSummary(since, request);
    // A navigation to Access while the request was pending marks events seen.
    // Do not let that older response recreate the badge afterward.
    if (request.current()) { renderAccessBadge(newCount || 0); badgeState.success(); delete $('.tab[data-tab="clients"]').dataset.degraded; }
  } catch (error) { if (request.current()) { badgeState.start(); badgeState.fail(error); $('.tab[data-tab="clients"]').dataset.degraded = "true"; } }
  finally { if (badgeRead === request) badgeRead = null; }
}
let stopAccessBadge = null;
onSessionChange(() => {
  stopAccessBadge?.(); stopAccessBadge = null;
  badgeRequests.invalidate();
  accessBadgeGeneration += 1;
  renderAccessBadge(0);
});
export function startAccessBadge() {
  if (stopAccessBadge) stopAccessBadge();
  accessBadgeGeneration += 1;
  pollAccessBadge();
  stopAccessBadge = visiblePoll(() => !$("#app").hidden, pollAccessBadge, accessBadgePollMs, { onStop: badgeRequests.invalidate });
}

bindFilters({ hasClients: () => !!getClients(), render: renderClients });
const refresh = $("#clients-refresh");
async function scanClients() {
  const request = rosterRequests.begin(() => !$('[data-panel="clients"]').hidden);
  scanState.start();
  await withBusy(refresh, "↻ Scanning…", async () => {
    try {
      const roster = await refreshClients(request);
      if (!request.current()) return;
      setClients(roster); renderClients(); markAccessSeen(); rosterState.success(); scanState.success();
    } catch (error) { if (request.current()) { scanState.fail(error); rosterState.fail(error); } }
  });
}
if (refresh) refresh.addEventListener("click", scanClients);
const menu = $("#clients-menu");
if (menu) menu.addEventListener("click", () => bulkActions(getClients(), loadClients));
// Other flows (such as the add-device wizard) can request a roster reload
// without importing this feature's mutable state.
document.addEventListener("hlhq:clients-changed", reloadAfterSetup);

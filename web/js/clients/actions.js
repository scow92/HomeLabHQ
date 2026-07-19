// Mutating client workflows. State and re-rendering are supplied by index.js.
"use strict";
import { toastErr, toastOk, confirmDialog, pickDialog, withBusy } from "../ui.js";
import { forgetClients, ignoreClient, setClientApproval, setEnforcement } from "./api.js";
import { isOnline, matchesClient } from "./filters.js";

export async function approveClient(client, nac, approved, button, render) {
  await withBusy(button, null, async () => {
    try {
      await setClientApproval(nac.deviceId, client.mac, approved);
      client.nac = approved ? "approved" : "blocked";
      toastOk(approved ? `${client.hostname || client.mac} approved.` : `${client.hostname || client.mac} revoked.`);
      render();
    } catch (error) { toastErr(error.message); }
  });
}

export async function forgetClient(client, button, { remove, render }) {
  const label = client.name || client.hostname || client.mac;
  const ok = await confirmDialog({ title: `Forget “${label}”?`,
    message: "Removes its saved name, notes and connection history. If it ever connects again it shows up as a brand-new device.",
    okLabel: "Forget", danger: true });
  if (!ok) return;
  await withBusy(button, null, async () => {
    try { await forgetClients(client.mac); remove(client.mac); toastOk(`${label} forgotten.`); render(); }
    catch (error) { toastErr(error.message); }
  });
}

export async function ignoreOneClient(client, button, { remove, render }) {
  await withBusy(button, null, async () => {
    try {
      await ignoreClient(client.mac); remove(client.mac);
      toastOk(`${client.hostname || client.mac} ignored — it'll reappear if it connects again.`); render();
    } catch (error) { toastErr(error.message); }
  });
}

export async function bulkActions(roster, reload) {
  if (!roster) return;
  const shown = roster.clients.filter(matchesClient);
  const { nac } = roster;
  const configured = nac && nac.configured && nac.deviceId;
  const unapproved = configured ? shown.filter((client) => client.nac !== "approved") : [];
  const offline = shown.filter((client) => !isOnline(client));
  const items = [];
  if (unapproved.length) items.push({ value: "approve", label: `Approve all shown (${unapproved.length})`, sub: "Adds every unapproved device in the current view to the allow-list" });
  if (offline.length) items.push({ value: "forget", label: `Forget offline shown (${offline.length})`, sub: "Deletes their saved names, notes and connection history" });
  items.push({ value: "csv", label: "Export roster as CSV", sub: "Spreadsheet-friendly snapshot of every device" },
    { value: "json", label: "Export roster as JSON", sub: "Full snapshot including connection history" });
  const pick = await pickDialog({ title: "Bulk actions", items });
  if (pick === "csv" || pick === "json") {
    const link = document.createElement("a"); link.href = `/api/clients/export?format=${pick}`;
    link.download = ""; document.body.appendChild(link); link.click(); link.remove(); return;
  }
  const targets = pick === "approve" ? unapproved : pick === "forget" ? offline : null;
  if (!targets) return;
  const ok = await confirmDialog({ title: `${pick === "approve" ? "Approve" : "Forget"} ${targets.length} devices?`,
    message: pick === "approve" ? "Every unapproved device in the current view is added to the allow-list." : "Removes their saved names, notes and connection history. Any that connect again show up as brand-new devices.",
    okLabel: pick === "approve" ? "Approve all" : "Forget all", danger: pick === "forget" });
  if (!ok) return;
  try {
    if (pick === "approve") await setClientApproval(nac.deviceId, targets.map((client) => client.mac), true);
    else await forgetClients(targets.map((client) => client.mac));
    toastOk(`${targets.length} devices ${pick === "approve" ? "approved" : "forgotten"}.`); await reload();
  } catch (error) { toastErr(error.message); }
}

export async function toggleEnforcement(nac, enabled, button, render) {
  if (enabled && !await confirmDialog({ title: "Turn on enforcement?", message: "Default-deny goes live: any device that isn't approved loses network access immediately. Make sure everything you rely on is approved first.", okLabel: "Turn on", danger: true })) return;
  button.disabled = true;
  try {
    const result = await setEnforcement(nac.deviceId, enabled);
    nac.enforced = !!(result.device && result.device.nac && result.device.nac.enforced);
    toastOk(nac.enforced ? "Enforcement on — only approved devices have access." : "Enforcement off — all devices allowed.");
    render();
  } catch (error) { toastErr(error.message); button.disabled = false; }
}

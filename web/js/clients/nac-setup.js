// Guided Network Access Control (NAC) setup: reuse an existing firewall
// alias, or create a fresh allow-list (name, interface, seed). Never touches
// the client grid — this + edit-modal.js are the ~300 lines of clients.js
// that don't (refactor.md 2.2).
"use strict";
import { api } from "../api.js";
import { toastErr, toastOk, promptDialog, pickDialog } from "../ui.js";
import { loadClients, invalidateClients } from "../clients.js";

// Switching tabs is handled by router.js; dispatching an event here instead
// of importing switchTab directly avoids an import cycle (refactor.md 2.3).
function switchTab(name) {
  document.dispatchEvent(new CustomEvent("hlhq:navigate", { detail: { tab: name } }));
}

// Setup entry point: choose whether to reuse an existing firewall alias (safest
// when you already run one, e.g. Network Manager) or create a fresh one.
export async function nacSetup(nac, deviceId) {
  const devId = deviceId || (nac && nac.deviceId);
  const mode = await pickDialog({ title: "Set up Network Access Control",
    message: "Control which devices are allowed on your network.",
    items: [
      { value: "existing", label: "Use an existing firewall alias",
        sub: "recommended if you already run one (e.g. Network Manager)" },
      { value: "create", label: "Create a new allow-list",
        sub: "fresh setup — creates the alias and rules for you" },
    ] });
  if (!mode) return;
  return mode === "existing" ? nacSetupExisting(devId) : nacSetupCreate(devId);
}

// Reuse an existing alias — membership-only. HomeLabHQ adds/removes devices in
// the alias you pick; your own firewall rule keeps enforcing it, so nothing can
// be cut off by turning this on.
async function nacSetupExisting(devId) {
  let aliases;
  try {
    aliases = (await api(`/api/devices/${devId}/nac/aliases`)).aliases || [];
  } catch (ex) { toastErr("Couldn't read aliases: " + ex.message); return; }
  if (!aliases.length) {
    toastErr("No aliases found on the firewall — choose “Create a new allow-list” instead.");
    return;
  }
  const pick = await pickDialog({ title: "Choose the alias to manage",
    message: "HomeLabHQ will add or remove devices in this alias. Everything " +
      "already in it stays approved.",
    items: aliases.map((a) => ({ value: a.uuid, label: a.name,
      sub: [a.type, a.description].filter(Boolean).join(" · ") })) });
  if (!pick) return;
  try {
    await api(`/api/devices/${devId}/nac/setup`, { method: "POST",
      body: JSON.stringify({ mode: "existing", existingUuid: pick }) });
    toastOk("Access control linked to your existing alias.");
    invalidateClients(); loadClients();
    switchTab("clients");
  } catch (ex) { toastErr(ex.message); }
}

// Guided setup for a brand-new allow-list: name the alias, pick the interface,
// choose whether to seed it with everything currently online.
async function nacSetupCreate(devId) {
  const alias = await promptDialog({ title: "Create a new allow-list",
    message: "Name the firewall alias that will hold your approved devices " +
      "(letters, digits and underscore).",
    value: "HLHQ_NAC", okLabel: "Next" });
  if (alias == null) return;
  if (!/^[A-Za-z][A-Za-z0-9_]{0,31}$/.test(alias.trim())) {
    toastErr("Alias must start with a letter; letters, digits and underscore only.");
    return;
  }
  let ifaces;
  try {
    ifaces = (await api(`/api/devices/${devId}/nac/interfaces`)).interfaces || [];
  } catch (ex) { toastErr("Couldn't read interfaces: " + ex.message); return; }
  if (!ifaces.length) { toastErr("No interfaces available on the firewall."); return; }
  const iface = await pickDialog({ title: "Which network to protect?",
    message: "The access rule attaches to this interface — usually your LAN.",
    items: ifaces.map((i) => ({ value: i.value, label: i.label, sub: i.value })) });
  if (!iface) return;
  const seedChoice = await pickDialog({ title: "Seed the allow-list?",
    message: "Approving the devices already online means turning enforcement on " +
      "later won't cut anyone off.",
    items: [
      { value: "seed", label: "Approve all current devices", sub: "recommended" },
      { value: "empty", label: "Start empty", sub: "you'll approve devices yourself" },
    ] });
  if (!seedChoice) return;
  try {
    const r = await api(`/api/devices/${devId}/nac/setup`, { method: "POST",
      body: JSON.stringify({ alias: alias.trim(), interface: iface,
        seedExisting: seedChoice === "seed" }) });
    toastOk(`Access control ready${r.seeded ? ` — ${r.seeded} devices approved` : ""}. ` +
      "Enforcement is off until you turn it on.");
    invalidateClients(); loadClients();
    switchTab("clients");
  } catch (ex) { toastErr(ex.message); }
}

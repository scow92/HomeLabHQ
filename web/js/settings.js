// Settings tab: account password, web push, certificate download, and the
// Network Access (managed aliases + DNS sync) admin config.
"use strict";
import { $, $$, api } from "./api.js";
import { toastOk, toastErr, withBusy } from "./ui.js";

// ---- password ---------------------------------------------------------------
$("#pw-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const pw = $("#pw-new").value;
  if (!pw) return;
  try {
    await api("/api/account/password", { method: "POST", body: JSON.stringify({ password: pw }) });
    $("#pw-new").value = "";
    toastOk("Password updated.");
  } catch (ex) { toastErr(ex.message); }
});

// ---- web push -----------------------------------------------------------------
function urlB64ToUint8Array(base64) {
  const pad = "=".repeat((4 - (base64.length % 4)) % 4);
  const b64 = (base64 + pad).replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(b64);
  return Uint8Array.from([...raw].map((c) => c.charCodeAt(0)));
}

async function enablePush() {
  if (!("serviceWorker" in navigator) || !("PushManager" in window)) {
    toastErr("Push isn't supported by this browser."); return;
  }
  if (!window.isSecureContext) {
    toastErr("Alerts need HTTPS (or localhost). Put HomelabHQ behind TLS to enable push.");
    return;
  }
  try {
    const perm = await Notification.requestPermission();
    if (perm !== "granted") { toastErr("Notification permission denied."); return; }
    const reg = await navigator.serviceWorker.ready;
    const { publicKey } = await api("/api/push/vapid");
    const sub = await reg.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlB64ToUint8Array(publicKey),
    });
    await api("/api/push/subscribe", { method: "POST", body: JSON.stringify({ subscription: sub }) });
    toastOk("Alerts enabled on this device.");
    $("#push-test").hidden = false;
  } catch (ex) {
    toastErr("Couldn't enable alerts: " + ex.message);
  }
}

$("#push-enable").addEventListener("click", enablePush);
$("#push-test").addEventListener("click", async () => {
  try {
    const r = await api("/api/push/test", { method: "POST" });
    if (r.sent) toastOk(`Test sent (${r.sent}).`);
    else if (r.failed) toastErr(`Test failed on ${r.failed} device(s): ${r.error || "push rejected"}`);
    else toastErr("No device is subscribed — tap “Enable notifications” first.");
  } catch (ex) { toastErr("Test failed: " + ex.message); }
});

// ---- network access (managed aliases + DNS sync) -----------------------------
export async function loadNacConfig() {
  const card = $("#nac-access-card");
  let cfg;
  try { cfg = await api("/api/nac/config"); }
  catch (_) { card.hidden = true; return; }
  if (!cfg.configured) { card.hidden = true; return; }  // needs NAC setup first
  card.hidden = false;
  $("#na-dns").checked = !!(cfg.dnsSync && cfg.dnsSync.enabled);
  $("#na-domain").value = (cfg.dnsSync && cfg.dnsSync.domain) || "";
  $("#na-domain-field").hidden = !$("#na-dns").checked;
  const chosen = new Set((cfg.managedAliases || []).map((a) => a.uuid));
  const box = $("#na-aliases");
  box.innerHTML = "";
  box.appendChild(Object.assign(document.createElement("p"),
    { className: "muted", textContent: "Loading…" }));
  let aliases = [];
  try {
    aliases = (await api(`/api/devices/${cfg.deviceId}/nac/aliases`)).aliases || [];
  } catch (ex) { box.innerHTML = ""; box.textContent = "Couldn't read aliases: " + ex.message; return; }
  box.innerHTML = "";
  if (!aliases.length) { box.textContent = "No firewall aliases found."; return; }
  for (const a of aliases) {
    const lbl = document.createElement("label"); lbl.className = "ent-item";
    const cb = document.createElement("input");
    cb.type = "checkbox"; cb.dataset.uuid = a.uuid;
    cb.dataset.name = a.name || ""; cb.dataset.atype = a.type || "";
    cb.checked = chosen.has(a.uuid);
    const sp = document.createElement("span");
    sp.textContent = (a.name || a.uuid) + (a.type ? ` · ${a.type}` : "");
    lbl.append(cb, sp); box.appendChild(lbl);
  }
}

$("#na-dns").addEventListener("change", () => {
  $("#na-domain-field").hidden = !$("#na-dns").checked;
});

$("#na-add").addEventListener("click", async () => {
  const name = $("#na-new-name").value.trim();
  if (!name) { $("#na-new-name").focus(); return; }
  await withBusy($("#na-add"), "Creating…", async () => {
    try {
      const r = await api("/api/nac/alias", { method: "POST",
        body: JSON.stringify({ name, type: $("#na-new-type").value }) });
      $("#na-new-name").value = "";
      toastOk(r.alias && r.alias.existed
        ? `“${name}” already existed — now managed.` : `Alias “${name}” created.`);
      await loadNacConfig();  // re-render with the new alias checked
    } catch (ex) { toastErr(ex.message); }
  });
});

$("#na-save").addEventListener("click", async () => {
  const managedAliases = $$("#na-aliases input[data-uuid]:checked").map((cb) => ({
    uuid: cb.dataset.uuid, name: cb.dataset.name, type: cb.dataset.atype }));
  const dnsSync = { enabled: $("#na-dns").checked, domain: $("#na-domain").value.trim() };
  await withBusy($("#na-save"), "Saving…", async () => {
    try {
      await api("/api/nac/config", { method: "POST",
        body: JSON.stringify({ managedAliases, dnsSync }) });
      toastOk("Network access settings saved.");
    } catch (ex) { toastErr(ex.message); }
  });
});

// Settings tab: account password, web push, certificate download, and the
// Network Access (managed aliases + DNS sync) admin config.
"use strict";
import { $, $$, api, SESSION } from "./api.js";
import { toastOk, toastErr, withBusy } from "./ui.js";

// ---- password ---------------------------------------------------------------
$("#pw-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const currentPassword = $("#pw-current").value;
  const pw = $("#pw-new").value;
  if (pw !== $("#pw-confirm").value) {
    toastErr("New passwords do not match.");
    return;
  }
  try {
    await api("/api/account/password", {
      method: "POST", body: JSON.stringify({ currentPassword, password: pw }),
    });
    $("#pw-current").value = "";
    $("#pw-new").value = "";
    $("#pw-confirm").value = "";
    toastOk("Password updated. Other sessions were signed out.");
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
  if (SESSION && SESSION.role === "admin") loadAnsibleConfig();
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

// ---- Ansible controller ----------------------------------------------------
const ANSIBLE_OPERATIONS = [
  ["os_check", "OS update check"], ["os_update", "OS update"],
  ["docker_check", "Docker update check"], ["docker_discovery", "Docker discovery"],
  ["docker_update_pull", "Docker update · pull and recreate"],
  ["docker_update_local_build", "Docker update · local build and recreate"],
];
let ansibleController = null;

function setValue(selector, value) { const el = $(selector); if (el) el.value = value ?? ""; }

async function loadAnsibleConfig() {
  const card = $("#ansible-settings-card");
  if (!card) return;
  card.hidden = false;
  try {
    ansibleController = (await api("/api/settings/ansible")).controller;
  } catch (error) { toastErr("Couldn't load Ansible settings: " + error.message); return; }
  const c = ansibleController || {};
  $("#ans-enabled").checked = !!c.enabled;
  setValue("#ans-name", c.displayName || "Ansible");
  setValue("#ans-host", c.host); setValue("#ans-port", c.sshPort || 22);
  setValue("#ans-user", c.sshUsername); setValue("#ans-auth", c.authMethod || "private_key");
  setValue("#ans-project", c.projectDirectory); setValue("#ans-inventory", c.inventoryPath);
  setValue("#ans-playbooks", c.playbooksDirectory);
  setValue("#ans-connect-timeout", c.connectionTimeout || 12);
  setValue("#ans-exec-timeout", c.executionTimeout || 1800);
  $("#ans-secret").value = "";
  $("#ans-secret").placeholder = c.credentialConfigured
    ? "Saved — leave blank to keep it" : "Required";
  $("#ansible-state").textContent = c.enabled ? "Enabled" : "Disabled";
  const inventory = c.inventory || {};
  $("#ans-inventory-summary").textContent = inventory.discoveredAt
    ? `${(inventory.hosts || []).length} hosts · ${(inventory.groups || []).length} groups`
    : "Inventory has not been discovered.";
  renderPlaybookOperations();
  updateAnsibleSecretLabel();
}

function updateAnsibleSecretLabel() {
  const password = $("#ans-auth").value === "password";
  $("#ans-secret-label").textContent = password ? "Password" : "Private key";
}
$("#ans-auth").addEventListener("change", updateAnsibleSecretLabel);

function ansiblePayload() {
  const authMethod = $("#ans-auth").value;
  const payload = {
    enabled: $("#ans-enabled").checked, displayName: $("#ans-name").value.trim(),
    host: $("#ans-host").value.trim(), sshPort: Number($("#ans-port").value),
    sshUsername: $("#ans-user").value.trim(), authMethod,
    projectDirectory: $("#ans-project").value.trim(),
    inventoryPath: $("#ans-inventory").value.trim(),
    playbooksDirectory: $("#ans-playbooks").value.trim(),
    connectionTimeout: Number($("#ans-connect-timeout").value),
    executionTimeout: Number($("#ans-exec-timeout").value),
  };
  if ($("#ans-secret").value) payload[authMethod === "password" ? "password" : "privateKey"] = $("#ans-secret").value;
  return payload;
}

$("#ansible-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  await withBusy($("#ans-save"), "Saving…", async () => {
    try {
      ansibleController = (await api("/api/settings/ansible", {
        method: "POST", body: JSON.stringify(ansiblePayload()),
      })).controller;
      toastOk("Ansible controller settings saved."); await loadAnsibleConfig();
    } catch (error) { toastErr(error.message); }
  });
});

function renderTestStatus(status) {
  const box = $("#ans-test-result"); box.hidden = false; box.innerHTML = "";
  for (const [label, key] of [["Controller", "controller"], ["Project", "project"],
    ["ansible-playbook", "ansiblePlaybook"], ["ansible-inventory", "ansibleInventory"],
    ["Inventory", "inventory"]]) {
    const row = document.createElement("div");
    const name = document.createElement("span"); name.textContent = label;
    const value = document.createElement("strong");
    const item = status[key] || {};
    value.className = item.ok ? "sev-good" : "sev-bad";
    value.textContent = item.ok ? (item.version || (key === "inventory"
      ? `${item.hosts} hosts · ${item.groups} groups` : "OK")) : (item.error || "Failed");
    row.append(name, value); box.appendChild(row);
  }
}

$("#ans-test").addEventListener("click", () => withBusy($("#ans-test"), "Testing…", async () => {
  try { renderTestStatus((await api("/api/settings/ansible/test", { method: "POST", timeoutMs: 130000 })).status); }
  catch (error) { toastErr(error.message); }
}));

$("#ans-discover").addEventListener("click", () => withBusy($("#ans-discover"), "Discovering…", async () => {
  try { await api("/api/settings/ansible/inventory", { method: "POST", timeoutMs: 130000 }); await loadAnsibleConfig(); toastOk("Ansible inventory refreshed."); }
  catch (error) { toastErr(error.message); }
}));

$("#ans-find-playbooks").addEventListener("click", () => withBusy($("#ans-find-playbooks"), "Discovering…", async () => {
  try { await api("/api/settings/ansible/playbooks", { method: "POST", timeoutMs: 130000 }); await loadAnsibleConfig(); toastOk("Playbook list refreshed."); }
  catch (error) { toastErr(error.message); }
}));

function renderPlaybookOperations() {
  const area = $("#ans-playbook-config"), list = $("#ans-operation-list");
  const discovered = ansibleController.discoveredPlaybooks || [];
  area.hidden = !discovered.length; list.innerHTML = "";
  for (const [operation, label] of ANSIBLE_OPERATIONS) {
    const current = (ansibleController.playbooks || {})[operation] || {};
    const row = document.createElement("div"); row.className = "ans-operation";
    const title = document.createElement("strong"); title.textContent = label;
    const select = document.createElement("select");
    select.innerHTML = '<option value="">Not approved</option>';
    for (const playbook of discovered) {
      const option = document.createElement("option"); option.value = playbook;
      option.textContent = playbook; option.selected = current.playbook === playbook;
      select.appendChild(option);
    }
    const metadata = document.createElement("input"); metadata.placeholder = operation === "os_update"
      ? "Reboot variable (optional)" : operation.startsWith("docker_update")
        ? "Project variable (required)" : "";
    metadata.hidden = !metadata.placeholder;
    metadata.value = operation === "os_update" ? (current.rebootVariable || "") : (current.projectVariable || "");
    const save = document.createElement("button"); save.className = "btn btn-ghost btn-sm";
    save.textContent = "Approve";
    save.onclick = async () => {
      const body = { operation, playbook: select.value, approved: !!select.value };
      if (operation === "os_update" && metadata.value.trim()) Object.assign(body, { supportsReboot: true, rebootVariable: metadata.value.trim() });
      if (operation.startsWith("docker_update")) Object.assign(body, {
        projectVariable: metadata.value.trim(),
        updateStrategy: operation.endsWith("pull") ? "pull" : "local_build",
      });
      try { await api("/api/settings/ansible/playbooks/approve", { method: "POST", body: JSON.stringify(body) }); await loadAnsibleConfig(); toastOk(`${label} approval saved.`); }
      catch (error) { toastErr(error.message); }
    };
    row.append(title, select, metadata, save); list.appendChild(row);
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

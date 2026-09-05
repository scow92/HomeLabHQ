// Settings tab: account password, web push, certificate download, and the
// Network Access (managed aliases + DNS sync) admin config.
"use strict";
import { $, $$, api, SESSION, onSessionChange, getSessionGeneration, isCurrentSession } from "./api.js";
import { refreshState } from "./refresh-state.js";
import { requestOwner } from "./request-owner.js";
import { toastOk, toastErr, withBusy, fieldError } from "./ui.js";

const settingsReads = [requestOwner(), requestOwner(), requestOwner()];
const settingsCurrent = () => !!SESSION && !$('[data-panel="settings"]').hidden;
export function stopSettingsReads() { settingsReads.forEach(owner => owner.invalidate()); }
onSessionChange(stopSettingsReads);
const morningState = refreshState("morning-settings-refresh-state", $("#morning-update-form"), "Morning settings", loadMorningUpdateSettings);
const nacState = refreshState("nac-settings-refresh-state", $("#nac-access-card"), "Network Access settings", loadNacConfig);
const aliasesState = refreshState("aliases-refresh-state", $("#na-aliases"), "Firewall aliases", loadNacConfig);
const ansibleState = refreshState("ansible-settings-refresh-state", $("#ansible-settings-card"), "Ansible settings", loadAnsibleConfig);

// ---- password ---------------------------------------------------------------
$("#pw-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const currentPassword = $("#pw-current").value;
  const pw = $("#pw-new").value;
  if (pw !== $("#pw-confirm").value) {
    fieldError($("#pw-confirm"), "New passwords do not match.");
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
  } catch (ex) { fieldError($("#pw-current"), ex.message); }
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
    await refreshPushState();
  } catch (ex) {
    toastErr("Couldn't enable alerts: " + ex.message);
  }
}

$("#push-enable").addEventListener("click", enablePush);
$("#push-disable").addEventListener("click", async () => {
  try {
    const reg = await navigator.serviceWorker.ready;
    const sub = await reg.pushManager.getSubscription();
    if (sub) {
      await api("/api/push/unsubscribe", {
        method: "POST", body: JSON.stringify({ endpoint: sub.endpoint }),
      });
      await sub.unsubscribe();
    }
    toastOk("Notifications disabled on this browser.");
    await refreshPushState();
  } catch (ex) { toastErr("Couldn't disable notifications: " + ex.message); }
});
$("#push-test").addEventListener("click", async () => {
  try {
    const r = await api("/api/push/test", { method: "POST" });
    if (r.sent) toastOk(`Test sent (${r.sent}).`);
    else if (r.failed) toastErr(`Test failed on ${r.failed} device(s): ${r.error || "push rejected"}`);
    else toastErr("No device is subscribed — tap “Enable notifications” first.");
  } catch (ex) { toastErr("Test failed: " + ex.message); }
});

async function refreshPushState(serverStatus = null) {
  const generation = getSessionGeneration();
  let local = null;
  if ("serviceWorker" in navigator && "PushManager" in window && window.isSecureContext) {
    try {
      const reg = await navigator.serviceWorker.ready;
      local = await reg.pushManager.getSubscription();
    } catch (_) { /* status text below remains useful */ }
  }
  const subscribed = !!local;
  if (!isCurrentSession(generation)) return;
  const count = serverStatus?.subscriptionCount;
  $("#push-enable").hidden = subscribed;
  $("#push-disable").hidden = !subscribed;
  $("#push-test").hidden = !(subscribed || count > 0);
  $("#push-status").textContent = subscribed
    ? "Notification subscription status: enabled on this browser."
    : count > 0
      ? `Notification subscription status: ${count} other subscribed browser${count === 1 ? "" : "s"}.`
      : "Notification subscription status: disabled.";
}

async function loadMorningUpdateSettings() {
  const request = settingsReads[0].begin(settingsCurrent); morningState.start();
  let data;
  try { data = await api("/api/settings/morning-updates", request); }
  catch (ex) { if (request.current()) morningState.fail(ex); return; }
  if (!request.current()) return;
  morningState.success();
  const config = data.config || {};
  const notifications = data.notifications || {};
  $("#morning-enabled").checked = !!config.enabled;
  $("#morning-time").value = config.runTime || "07:00";
  $("#morning-timezone").value = config.timezone || "Europe/London";
  $("#morning-timeout").value = config.deviceTimeoutSeconds || 30;
  $("#morning-ansible").checked = !!config.runAnsibleChecks;
  $("#morning-native").checked = !!config.runDeviceNativeChecks;
  $("#morning-notify-updates").checked = notifications.notifyUpdates !== false;
  $("#morning-notify-failures").checked = notifications.notifyFailures !== false;
  $("#morning-notify-success").checked = notifications.notifySuccess !== false;
  $("#morning-update-state").textContent = config.enabled ? "Enabled" : "Disabled";
  const last = data.lastRun;
  $("#morning-last-run").textContent = last
    ? `Last run: ${last.status} · ${new Date(last.completedAt || last.startedAt).toLocaleString()} · ` +
      `${last.devicesRequiringUpdates || 0} need updates · ${last.failedChecks || 0} failed checks`
    : "Last run: never";
  await refreshPushState(data.subscription);
}

$("#morning-update-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const payload = {
    notifications: {
      notifyUpdates: $("#morning-notify-updates").checked,
      notifyFailures: $("#morning-notify-failures").checked,
      notifySuccess: $("#morning-notify-success").checked,
    },
  };
  if (SESSION?.role === "admin") payload.config = {
    enabled: $("#morning-enabled").checked,
    runTime: $("#morning-time").value,
    timezone: $("#morning-timezone").value.trim(),
    runAnsibleChecks: $("#morning-ansible").checked,
    runDeviceNativeChecks: $("#morning-native").checked,
    deviceTimeoutSeconds: Number($("#morning-timeout").value),
  };
  await withBusy($("#morning-save"), "Saving…", async () => {
    try {
      await api("/api/settings/morning-updates", {
        method: "POST", body: JSON.stringify(payload),
      });
      toastOk("Morning update settings saved.");
      await loadMorningUpdateSettings();
    } catch (ex) { toastErr(ex.message); }
  });
});

$("#morning-run-now").addEventListener("click", async () => {
  await withBusy($("#morning-run-now"), "Starting…", async () => {
    try {
      await api("/api/morning-updates/run", { method: "POST" });
      toastOk("Morning update check started.");
      $("#morning-last-run").textContent = "Last run: running now…";
    } catch (ex) { toastErr(ex.message); }
  });
});

// ---- network access (managed aliases + DNS sync) -----------------------------
export async function loadNacConfig() {
  loadMorningUpdateSettings();
  if (SESSION && SESSION.role === "admin") loadAnsibleConfig();
  const card = $("#nac-access-card");
  const request = settingsReads[1].begin(settingsCurrent); nacState.start();
  let cfg;
  try { cfg = await api("/api/nac/config", request); }
  catch (error) { if (request.current()) nacState.fail(error); return; }
  if (!request.current()) return;
  nacState.success();
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
  aliasesState.start();
  let aliases = [];
  try {
    aliases = (await api(`/api/devices/${cfg.deviceId}/nac/aliases`, request)).aliases || [];
  } catch (ex) { if (request.current()) aliasesState.fail(ex); return; }
  if (!request.current()) return;
  aliasesState.success();
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
  ["appliance_health", "Appliance health check"],
  ["os_check", "OS update check"], ["os_update", "OS update"],
  ["docker_discovery", "Docker discovery"], ["docker_check", "Docker update check"],
  ["docker_update", "Docker update"],
];
const LEGACY_DOCKER_OPERATIONS = [
  ["docker_update_pull", "Pull and recreate playbook"],
  ["docker_update_local_build", "Local build and recreate playbook"],
];
const ANSIBLE_REQUIRED_GROUPS = {
  appliance_health: "appliances",
  os_check: "debian_hosts", os_update: "debian_hosts",
  docker_discovery: "docker_hosts", docker_check: "docker_hosts",
  docker_update: "docker_hosts", docker_update_pull: "docker_hosts",
  docker_update_local_build: "docker_hosts",
};
let ansibleController = null;
onSessionChange(() => {
  ansibleController = null;
  $$('[data-panel="settings"] form').forEach(form => form.reset());
  for (const selector of ["#na-aliases", "#ans-test-result", "#ans-inventory-summary",
    "#ans-operation-list", "#morning-update-state", "#morning-last-run", "#push-status"]) {
    $(selector)?.replaceChildren();
  }
  $("#ans-playbook-config").hidden = true;
  $("#nac-access-card").hidden = true;
});

function setValue(selector, value) { const el = $(selector); if (el) el.value = value ?? ""; }

async function loadAnsibleConfig() {
  const card = $("#ansible-settings-card");
  if (!card) return;
  card.hidden = false;
  const request = settingsReads[2].begin(settingsCurrent); ansibleState.start();
  let response;
  try { response = await api("/api/settings/ansible", request); }
  catch (error) { if (request.current()) ansibleState.fail(error); return; }
  if (!request.current()) return;
  ansibleController = response.controller || {}; ansibleState.success();
  const c = ansibleController || {};
  $("#ans-enabled").checked = !!c.enabled;
  setValue("#ans-name", c.displayName || "Ansible");
  setValue("#ans-host", c.host); setValue("#ans-port", c.sshPort || 22);
  setValue("#ans-user", c.sshUsername); setValue("#ans-auth", c.authMethod || "private_key");
  setValue("#ans-project", c.projectDirectory); setValue("#ans-inventory", c.inventoryPath);
  setValue("#ans-playbooks", c.playbooksDirectory);
  setValue("#ans-playbook-executable", c.ansiblePlaybookExecutable);
  setValue("#ans-inventory-executable", c.ansibleInventoryExecutable);
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
    ansiblePlaybookExecutable: $("#ans-playbook-executable").value.trim(),
    ansibleInventoryExecutable: $("#ans-inventory-executable").value.trim(),
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
    ["Ansible Playbook", "ansiblePlaybook"], ["Ansible Inventory", "ansibleInventory"],
    ["Inventory", "inventory"]]) {
    const row = document.createElement("div");
    const name = document.createElement("span"); name.textContent = label;
    const value = document.createElement("strong");
    const item = status[key] || {};
    value.className = item.ok ? "sev-good" : "sev-bad";
    value.textContent = item.ok ? (item.path || item.version || (key === "inventory"
      ? `${item.hosts} hosts · ${item.groups} groups` : "OK")) : (item.error || "Failed");
    row.append(name, value); box.appendChild(row);
  }
}

$("#ans-test").addEventListener("click", () => withBusy($("#ans-test"), "Testing…", async () => {
  try {
    const status = (await api("/api/settings/ansible/test", { method: "POST", timeoutMs: 130000 })).status;
    renderTestStatus(status);
    let discovered = false;
    for (const [key, selector] of [["ansiblePlaybook", "#ans-playbook-executable"],
      ["ansibleInventory", "#ans-inventory-executable"]]) {
      if (status[key]?.ok && status[key]?.discovered && status[key]?.path) {
        setValue(selector, status[key].path); discovered = true;
      }
    }
    if (discovered) toastOk("Ansible executable paths discovered. Review and Save them.");
  }
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

  const commaList = (value) => value.split(",").map((item) => item.trim()).filter(Boolean);
  const field = (label, input) => {
    const wrapper = document.createElement("label"); wrapper.className = "ans-metadata-field";
    const caption = document.createElement("span"); caption.textContent = label;
    wrapper.append(caption, input); return wrapper;
  };

  const renderOperation = (operation, label, parent) => {
    const current = (ansibleController.playbooks || {})[operation] || {};
    const row = document.createElement("div"); row.className = "ans-operation";
    const heading = document.createElement("div"); heading.className = "ans-operation-heading";
    const title = document.createElement("strong"); title.textContent = label;
    const state = document.createElement("span"); state.className = "muted";
    state.textContent = current.approved ? (current.label || "Approved") : "Not approved";
    heading.append(title, state);
    const select = document.createElement("select");
    select.innerHTML = '<option value="">Not approved</option>';
    for (const playbook of discovered) {
      const option = document.createElement("option"); option.value = playbook;
      option.textContent = playbook; option.selected = current.playbook === playbook;
      select.appendChild(option);
    }
    select.setAttribute("aria-label", `${label} playbook`);

    const metadata = document.createElement("details"); metadata.className = "ans-operation-metadata";
    const metadataSummary = document.createElement("summary"); metadataSummary.textContent = "Approval restrictions";
    const metadataGrid = document.createElement("div"); metadataGrid.className = "ans-metadata-grid";
    const friendly = document.createElement("input"); friendly.value = current.label || label;
    friendly.placeholder = "Friendly label";
    const targets = document.createElement("input"); targets.value = (current.allowedTargets || []).join(", ");
    targets.placeholder = "host-a, host-b (blank allows all)";
    const groups = document.createElement("input"); groups.value = (current.allowedGroups || []).join(", ");
    groups.placeholder = ANSIBLE_REQUIRED_GROUPS[operation]
      ? `Additional restriction (${ANSIBLE_REQUIRED_GROUPS[operation]} is always required)`
      : "group-a, group-b (blank allows all)";
    const variables = document.createElement("input"); variables.value = (current.allowedExtraVariables || []).join(", ");
    variables.placeholder = "Approved variable names only";
    const checkMode = document.createElement("input"); checkMode.type = "checkbox";
    checkMode.checked = !!current.checkModeSupported;
    metadataGrid.append(field("Friendly label", friendly), field("Allowed inventory hosts", targets),
      field("Allowed inventory groups", groups), field("Other approved variable names", variables),
      field("Supports Ansible check mode", checkMode));

    let rebootVariable = null; let projectVariable = null; let modeVariable = null;
    let pullMode = null; let buildMode = null;
    if (operation === "os_update") {
      rebootVariable = document.createElement("input"); rebootVariable.value = current.rebootVariable || "";
      rebootVariable.placeholder = "e.g. maintenance_reboot";
      metadataGrid.append(field("Optional reboot Boolean variable", rebootVariable));
    }
    if (operation === "docker_check" || operation.startsWith("docker_update")) {
      projectVariable = document.createElement("input");
      projectVariable.value = "docker_project"; projectVariable.readOnly = true;
      projectVariable.placeholder = "docker_project";
      metadataGrid.append(field("Project name variable", projectVariable));
    }
    if (operation === "docker_update") {
      modeVariable = document.createElement("input"); modeVariable.value = current.modeVariable || "";
      modeVariable.placeholder = "Required when both modes are enabled";
      metadataGrid.append(field("Update mode variable", modeVariable));
      const modes = document.createElement("div"); modes.className = "ans-mode-options";
      pullMode = document.createElement("input"); pullMode.type = "checkbox";
      pullMode.checked = (current.supportedModes || ["pull"]).includes("pull");
      buildMode = document.createElement("input"); buildMode.type = "checkbox";
      buildMode.checked = (current.supportedModes || []).includes("build");
      for (const [input, text] of [[pullMode, "Pull"], [buildMode, "Build"]]) {
        const option = document.createElement("label"); option.append(input, document.createTextNode(text)); modes.append(option);
      }
      metadataGrid.append(field("Supported Docker modes", modes));
    }
    metadata.append(metadataSummary, metadataGrid);

    const save = document.createElement("button"); save.className = "btn btn-ghost btn-sm";
    save.textContent = "Save approval";
    save.onclick = async () => {
      const body = {
        operation, playbook: select.value, approved: !!select.value,
        label: friendly.value.trim(), checkModeSupported: checkMode.checked,
        allowedTargets: commaList(targets.value), allowedGroups: commaList(groups.value),
        allowedExtraVariables: commaList(variables.value),
      };
      if (rebootVariable?.value.trim()) Object.assign(body, {
        supportsReboot: true, rebootVariable: rebootVariable.value.trim(),
      });
      if (projectVariable) body.projectVariable = projectVariable.value.trim();
      if (operation === "docker_update") Object.assign(body, {
        modeVariable: modeVariable.value.trim(),
        supportedModes: [[pullMode, "pull"], [buildMode, "build"]]
          .filter(([input]) => input.checked).map(([, mode]) => mode),
      });
      try { await api("/api/settings/ansible/playbooks/approve", { method: "POST", body: JSON.stringify(body) }); await loadAnsibleConfig(); toastOk(`${label} approval saved.`); }
      catch (error) { toastErr(error.message); }
    };
    row.append(heading, select, save, metadata); parent.appendChild(row);
  };

  for (const [operation, label] of ANSIBLE_OPERATIONS) renderOperation(operation, label, list);
  const legacy = document.createElement("details"); legacy.className = "ans-legacy-operations";
  const summary = document.createElement("summary");
  summary.textContent = "Separate Docker update playbooks (compatibility)";
  const note = document.createElement("p"); note.className = "muted";
  note.textContent = "Use these only when pull and build genuinely require different playbooks. New setups can approve one Docker update playbook with supported modes above.";
  legacy.append(summary, note);
  for (const [operation, label] of LEGACY_DOCKER_OPERATIONS) renderOperation(operation, label, legacy);
  list.appendChild(legacy);
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

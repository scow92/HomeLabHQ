// Compute workload cards, filtering, detail, mappings, Docker hierarchy, and jobs.
"use strict";
import { $, $$, api, SESSION, fmtBytes, fmtUptime, timeAgo } from "./api.js";
import { toastErr, toastOk, withBusy, confirmDialog, pushModal, popModal } from "./ui.js";

let INSTANCES = [];
let FILTER = "all";
let PARENT_FILTER = null;
let ACTIVE_INSTANCE = null;
let pollTimer = null;

function attention(instance) {
  return ["updates_available", "failed", "unreachable", "reboot_required"]
    .includes((instance.updateState || {}).state) || instance.discoveryState !== "current";
}

function matches(instance) {
  if (PARENT_FILTER && instance.parentDeviceId !== PARENT_FILTER) return false;
  if (FILTER === "vm" || FILTER === "lxc") return instance.type === FILTER;
  if (FILTER === "docker") return !!(instance.docker && instance.docker.available);
  if (FILTER === "attention") return attention(instance);
  return true;
}

function updateLabel(instance) {
  const state = instance.updateState || {};
  if (state.state === "updates_available") return state.updateCount == null
    ? "Available" : String(state.updateCount);
  return ({ up_to_date: "Up to date", checking: "Checking…", updating: "Updating…",
    failed: "Failed", unreachable: "Unreachable", reboot_required: "Reboot required",
    successful: "Successful" })[state.state] || "Unknown";
}

function dockerLabel(instance) {
  const docker = instance.docker;
  if (!docker || docker.available == null) return "Unknown";
  if (!docker.available) return "Unavailable";
  const containers = (docker.projects || []).flatMap((project) => project.containers || []);
  if (!containers.length) return "Available";
  const healthy = containers.filter((container) => container.health === "healthy").length;
  const running = containers.filter((container) => container.state === "running").length;
  const restarting = containers.filter((container) => container.state === "restarting").length;
  if (healthy === containers.length) return `${healthy}/${containers.length} healthy`;
  return `${running} running${restarting ? ` · ${restarting} restarting` : ""}`;
}

function valueRow(grid, key, value) {
  if (value == null || value === "") return;
  const k = document.createElement("span"); k.className = "k"; k.textContent = key;
  const v = document.createElement("span"); v.textContent = String(value);
  grid.append(k, v);
}

function buildCard(instance) {
  const card = document.createElement("article"); card.className = "card clickable compute-card";
  card.dataset.computeId = instance.id;
  const top = document.createElement("div"); top.className = "card-row";
  const title = document.createElement("h2");
  const dot = document.createElement("span"); dot.className = "dot " +
    (instance.status === "running" ? "up" : instance.status === "stopped" ? "down" : "unknown");
  const name = document.createElement("span"); name.textContent = instance.name;
  title.append(dot, name);
  const pill = document.createElement("span"); pill.className = "pill";
  pill.textContent = instance.type === "vm" ? "VM" : "LXC";
  top.append(title, pill);
  const parent = document.createElement("button"); parent.className = "linkish compute-parent";
  parent.textContent = instance.parentDevice ? `Hosted on ${instance.parentDevice.name}` : "Parent unavailable";
  parent.disabled = !instance.parentDevice;
  parent.onclick = (event) => {
    event.stopPropagation();
    if (instance.parentDevice) document.dispatchEvent(new CustomEvent("hlhq:open-device", { detail: instance.parentDevice }));
  };
  const stats = document.createElement("div"); stats.className = "dev-state";
  valueRow(stats, "Status", instance.status);
  valueRow(stats, "CPU", instance.cpuCores != null ? `${instance.cpuCores} cores` : null);
  valueRow(stats, "Memory", instance.memoryBytes != null ? fmtBytes(instance.memoryBytes) : null);
  valueRow(stats, "Updates", updateLabel(instance));
  valueRow(stats, "Docker", dockerLabel(instance));
  const last = document.createElement("div"); last.className = "muted updated";
  last.textContent = instance.lastDiscoveredAt ? `discovered ${timeAgo(instance.lastDiscoveredAt)}` : "not discovered";
  const actions = document.createElement("div"); actions.className = "compute-actions";
  const details = document.createElement("button"); details.className = "btn btn-ghost btn-sm";
  details.textContent = "Details"; details.onclick = (event) => { event.stopPropagation(); openCompute(instance); };
  actions.appendChild(details);
  if (instance.ansible && instance.ansible.enabled) {
    const check = document.createElement("button"); check.className = "btn btn-ghost btn-sm";
    check.textContent = "Check Updates";
    check.onclick = (event) => { event.stopPropagation(); startCardJob(instance, check, "updates/check", "Checking…"); };
    actions.appendChild(check);
    if (SESSION.role === "admin") {
      const update = document.createElement("button"); update.className = "btn btn-primary btn-sm";
      update.textContent = "Update";
      update.onclick = async (event) => {
        event.stopPropagation();
        const confirmed = await confirmDialog({ title: `Update “${instance.name}”?`,
          message: "The approved OS update playbook will run without permission to reboot.",
          okLabel: "Update", danger: true });
        if (confirmed) startCardJob(instance, update, "updates", "Starting…", { allowReboot: false });
      };
      actions.appendChild(update);
    }
  }
  card.append(top, parent, stats, last, actions);
  card.onclick = () => openCompute(instance);
  return card;
}

async function startCardJob(instance, button, endpoint, busy, body = {}) {
  await withBusy(button, busy, async () => {
    try {
      const { job } = await api(`/api/compute/${instance.id}/${endpoint}`, {
        method: "POST", body: JSON.stringify(body),
      });
      await pollJob(job.id, async (finished) => {
        if (finished.state === "successful") toastOk(finished.summary);
        else toastErr(finished.summary || "Maintenance failed.");
        await loadCompute();
      });
    } catch (error) { toastErr(error.message); }
  });
}

function render() {
  const visible = INSTANCES.filter(matches);
  const list = $("#compute-list"); list.innerHTML = "";
  visible.forEach((instance) => list.appendChild(buildCard(instance)));
  const summary = $("#compute-summary"); summary.hidden = !INSTANCES.length;
  if (INSTANCES.length) {
    const running = visible.filter((item) => item.status === "running").length;
    summary.textContent = `${visible.length} workload${visible.length === 1 ? "" : "s"} · ${running} running` +
      (PARENT_FILTER ? " · filtered by host" : "");
  }
  const empty = $("#compute-empty"); empty.hidden = !!visible.length;
  $(".compute-empty-title", empty).textContent = INSTANCES.length ? "No matching workloads." : "No compute workloads discovered.";
  $(".compute-empty-sub", empty).textContent = INSTANCES.length
    ? "Choose another filter." : "Add a Proxmox Device, then refresh Compute.";
}

export async function loadCompute() {
  try {
    const response = await api("/api/compute"); INSTANCES = response.instances || []; render();
  } catch (error) {
    if (INSTANCES.length) toastErr("Couldn't refresh Compute: " + error.message);
    else { $("#compute-empty").hidden = false; $(".compute-empty-title").textContent = "Couldn't load Compute."; $(".compute-empty-sub").textContent = error.message; }
  }
  return INSTANCES;
}

$("#compute-filters").addEventListener("click", (event) => {
  const button = event.target.closest("[data-compute-filter]"); if (!button) return;
  FILTER = button.dataset.computeFilter; PARENT_FILTER = null;
  $$("[data-compute-filter]", $("#compute-filters")).forEach((item) => item.classList.toggle("active", item === button));
  render();
});

document.addEventListener("hlhq:compute-parent", (event) => {
  PARENT_FILTER = event.detail.deviceId; FILTER = "all";
  $$("[data-compute-filter]", $("#compute-filters")).forEach((item) => item.classList.toggle("active", item.dataset.computeFilter === "all"));
  render();
});

$("#compute-refresh").addEventListener("click", () => withBusy($("#compute-refresh"), "Refreshing…", async () => {
  try { await api("/api/compute/refresh", { method: "POST", body: "{}", timeoutMs: 130000 }); await loadCompute(); toastOk("Compute discovery refreshed."); }
  catch (error) { toastErr(error.message); }
}));

function section(title) {
  const el = document.createElement("section"); el.className = "detail-section";
  const heading = document.createElement("h3"); heading.textContent = title; el.appendChild(heading);
  return el;
}

function chip(grid, label, value) {
  if (value == null || value === "") return;
  const el = document.createElement("div"); el.className = "info-chip";
  const key = document.createElement("div"); key.className = "k"; key.textContent = label;
  const val = document.createElement("div"); val.className = "v"; val.textContent = String(value);
  el.append(key, val); grid.appendChild(el);
}

function infoSection(instance) {
  const el = section("Workload"); const grid = document.createElement("div"); grid.className = "info-grid";
  chip(grid, "Type", instance.type === "vm" ? "Virtual machine" : "LXC container");
  chip(grid, "Provider", instance.provider); chip(grid, "ID", instance.providerInstanceId);
  chip(grid, "Status", instance.status); chip(grid, "Node", instance.node);
  chip(grid, "CPU", instance.cpuCores != null ? `${instance.cpuCores} cores` : null);
  chip(grid, "Memory", instance.memoryBytes != null ? fmtBytes(instance.memoryBytes) : null);
  chip(grid, "Disk", instance.diskBytes != null ? fmtBytes(instance.diskBytes) : null);
  chip(grid, "IP", (instance.ipAddresses || []).join(", "));
  chip(grid, "Uptime", instance.uptimeSeconds != null ? fmtUptime(instance.uptimeSeconds) : null);
  chip(grid, "OS", instance.os); chip(grid, "Discovery", instance.discoveryState);
  el.appendChild(grid);
  if (instance.parentDevice) {
    const parent = document.createElement("button"); parent.className = "linkish detail-parent";
    parent.textContent = `View parent Device · ${instance.parentDevice.name}`;
    parent.onclick = () => document.dispatchEvent(new CustomEvent("hlhq:open-device", { detail: instance.parentDevice }));
    el.appendChild(parent);
  }
  return el;
}

async function managementSection(instance, controller) {
  const el = section("Maintenance");
  const mapping = instance.ansible || {};
  const status = document.createElement("p"); status.className = "muted";
  status.textContent = mapping.enabled ? `Managed as ${mapping.inventoryHost}` : "Not managed by Ansible.";
  el.appendChild(status);
  if (SESSION.role === "admin" && controller) {
    const form = document.createElement("div"); form.className = "inline-form compute-mapping";
    const enabled = document.createElement("input"); enabled.type = "checkbox"; enabled.checked = !!mapping.enabled;
    enabled.setAttribute("aria-label", "Enable Ansible management");
    const select = document.createElement("select");
    const placeholder = document.createElement("option"); placeholder.value = ""; placeholder.textContent = "Choose inventory host"; select.appendChild(placeholder);
    for (const host of (controller.inventory || {}).hosts || []) {
      const option = document.createElement("option"); option.value = host.name;
      option.textContent = `${host.name}${host.address && host.address !== host.name ? ` · ${host.address}` : ""}`;
      option.selected = mapping.inventoryHost === host.name; select.appendChild(option);
    }
    if (!mapping.enabled && (instance.suggestedMappings || []).length) select.value = instance.suggestedMappings[0].inventoryHost;
    const save = document.createElement("button"); save.className = "btn btn-ghost btn-sm"; save.textContent = "Save mapping";
    save.onclick = async () => {
      try { await api(`/api/compute/${instance.id}/ansible`, { method: "POST", body: JSON.stringify({ enabled: enabled.checked, controllerId: controller.id, inventoryHost: select.value }) }); toastOk("Ansible mapping saved."); await refreshOpen(instance.id); }
      catch (error) { toastErr(error.message); }
    };
    form.append(enabled, select, save); el.appendChild(form);
    if ((instance.suggestedMappings || []).length && !mapping.enabled) {
      const suggestion = document.createElement("p"); suggestion.className = "hint";
      suggestion.textContent = `Suggested: ${instance.suggestedMappings.map((item) => item.inventoryHost).join(", ")} — confirm before saving.`;
      el.appendChild(suggestion);
    }
  }
  if (mapping.enabled) {
    const actions = document.createElement("div"); actions.className = "action-row";
    const check = document.createElement("button"); check.className = "btn btn-ghost btn-sm"; check.textContent = "Check Updates";
    check.onclick = () => runDetailJob(instance, check, "updates/check", {});
    actions.appendChild(check);
    if (SESSION.role === "admin") {
      const rebootLabel = document.createElement("label"); rebootLabel.className = "ent-item compact-check";
      const reboot = document.createElement("input"); reboot.type = "checkbox"; reboot.checked = false;
      const text = document.createElement("span"); text.textContent = "Allow reboot if required"; rebootLabel.append(reboot, text);
      const update = document.createElement("button"); update.className = "btn btn-primary btn-sm"; update.textContent = "Update";
      update.onclick = async () => {
        const confirmed = await confirmDialog({ title: `Update “${instance.name}”?`,
          message: reboot.checked ? "The approved playbook may reboot this workload if required." : "Reboot permission is OFF.",
          okLabel: reboot.checked ? "Update and permit reboot" : "Update", danger: true });
        if (confirmed) runDetailJob(instance, update, "updates", { allowReboot: reboot.checked, rebootConfirmed: reboot.checked });
      };
      actions.append(rebootLabel, update);
    }
    el.appendChild(actions);
  }
  const state = instance.updateState || {};
  const summary = document.createElement("p"); summary.className = "muted";
  summary.textContent = `Update status: ${updateLabel(instance)}` + (state.lastCheckedAt ? ` · checked ${timeAgo(state.lastCheckedAt)}` : "");
  el.appendChild(summary);
  return el;
}

function dockerSection(instance) {
  const el = section("Docker"); const docker = instance.docker;
  if (!docker) {
    const p = document.createElement("p"); p.className = "muted"; p.textContent = "Docker status is unknown."; el.appendChild(p);
  } else {
    const p = document.createElement("p"); p.className = "muted";
    p.textContent = docker.available ? `Docker ${docker.version || "available"} · Compose ${docker.composeAvailable ? "available" : "unavailable"}` : "Docker unavailable";
    el.appendChild(p);
    for (const project of docker.projects || []) {
      const box = document.createElement("div"); box.className = "compose-project";
      const head = document.createElement("div"); head.className = "card-row";
      const title = document.createElement("strong"); title.textContent = project.name;
      const count = document.createElement("span"); count.className = "pill"; count.textContent = `${(project.containers || []).length} containers`;
      head.append(title, count); box.appendChild(head);
      const list = document.createElement("div"); list.className = "container-list";
      for (const container of project.containers || []) {
        const row = document.createElement("div"); const name = document.createElement("span"); name.textContent = container.name;
        const state = document.createElement("span"); state.className = container.health === "healthy" ? "sev-good" : container.state === "running" ? "" : "sev-bad";
        state.textContent = container.health || container.state; row.append(name, state); list.appendChild(row);
      }
      box.appendChild(list);
      if (SESSION.role === "admin") {
        const controls = document.createElement("div"); controls.className = "inline-form";
        const strategy = document.createElement("select");
        for (const [value, label] of [["unmanaged", "Read-only"], ["pull", "Pull and recreate"], ["local_build", "Local build and recreate"]]) {
          const option = document.createElement("option"); option.value = value; option.textContent = label; option.selected = project.updateStrategy === value; strategy.appendChild(option);
        }
        strategy.onchange = async () => {
          try {
            await api(`/api/compute/${instance.id}/docker/projects/${project.id}/strategy`, { method: "POST", body: JSON.stringify({ strategy: strategy.value }) });
            project.updateStrategy = strategy.value;
            update.disabled = strategy.value === "unmanaged";
            update.textContent = strategy.value === "local_build" ? "Rebuild & Deploy" : "Update Stack";
            toastOk("Docker update method saved.");
          }
          catch (error) { toastErr(error.message); }
        };
        const update = document.createElement("button"); update.className = "btn btn-ghost btn-sm";
        update.textContent = project.updateStrategy === "local_build" ? "Rebuild & Deploy" : "Update Stack";
        update.disabled = project.updateStrategy === "unmanaged";
        update.onclick = async () => {
          const confirmed = await confirmDialog({ title: `Update “${project.name}”?`, message: `Run its approved ${strategy.options[strategy.selectedIndex].text.toLowerCase()} playbook?`, okLabel: "Update Stack", danger: true });
          if (confirmed) runDetailJob(instance, update, `docker/projects/${project.id}/update`, {});
        };
        controls.append(strategy, update); box.appendChild(controls);
      }
      el.appendChild(box);
    }
  }
  if (instance.ansible && instance.ansible.enabled) {
    const actions = document.createElement("div"); actions.className = "action-row";
    const discover = document.createElement("button"); discover.className = "btn btn-ghost btn-sm"; discover.textContent = "Refresh Docker";
    discover.onclick = () => runDetailJob(instance, discover, "docker/discover", {});
    const check = document.createElement("button"); check.className = "btn btn-ghost btn-sm"; check.textContent = "Check Docker Updates";
    check.onclick = () => runDetailJob(instance, check, "docker/check", {});
    actions.append(discover, check); el.appendChild(actions);
  }
  const dockerUpdates = instance.dockerUpdateState || {};
  if (dockerUpdates.state) {
    const status = document.createElement("p"); status.className = "muted";
    const count = dockerUpdates.updateCount == null ? "" : ` · ${dockerUpdates.updateCount} update${dockerUpdates.updateCount === 1 ? "" : "s"}`;
    status.textContent = `Docker update status: ${dockerUpdates.state.replaceAll("_", " ")}${count}`;
    el.appendChild(status);
  }
  return el;
}

function historySection(jobs) {
  const el = section("Recent maintenance");
  if (!jobs.length) { const p = document.createElement("p"); p.className = "muted"; p.textContent = "No maintenance jobs yet."; el.appendChild(p); return el; }
  for (const job of jobs) {
    const box = document.createElement("details"); box.className = "job-history";
    const summary = document.createElement("summary");
    summary.textContent = `${job.operation.replaceAll("_", " ")} · ${job.state} · ${timeAgo(job.createdAt)}`;
    const recap = document.createElement("p"); recap.className = "muted";
    const totals = Object.values(job.recap || {}).reduce((acc, value) => { for (const key of ["ok", "changed", "failed", "unreachable"]) acc[key] += value[key] || 0; return acc; }, { ok: 0, changed: 0, failed: 0, unreachable: 0 });
    recap.textContent = `${job.summary || ""} · changed ${totals.changed} · failed ${totals.failed} · unreachable ${totals.unreachable}`;
    const logs = document.createElement("pre"); logs.textContent = [job.stdout, job.stderr].filter(Boolean).join("\n");
    box.append(summary, recap, logs); el.appendChild(box);
  }
  return el;
}

async function renderDetail(instance) {
  const body = $("#cm-body"); body.innerHTML = "";
  body.appendChild(infoSection(instance));
  let controller = null;
  if (SESSION.role === "admin") {
    try { controller = (await api("/api/settings/ansible")).controller; } catch (_) {}
  }
  body.appendChild(await managementSection(instance, controller));
  body.appendChild(dockerSection(instance));
  let jobs = [];
  try { jobs = (await api(`/api/compute/${instance.id}/jobs`)).jobs || []; } catch (_) {}
  body.appendChild(historySection(jobs));
}

async function refreshOpen(id) {
  try {
    const { instance } = await api(`/api/compute/${id}`); ACTIVE_INSTANCE = instance;
    $("#cm-title").textContent = instance.name; $("#cm-sub").textContent = `${instance.type.toUpperCase()} · ${instance.parentDevice ? `Hosted on ${instance.parentDevice.name}` : "Parent unavailable"}`;
    $("#cm-dot").className = "dot " + (instance.status === "running" ? "up" : instance.status === "stopped" ? "down" : "unknown");
    await renderDetail(instance);
  } catch (error) { toastErr(error.message); }
}

export function openCompute(instance) {
  ACTIVE_INSTANCE = instance; const modal = $("#compute-modal");
  if (!modal.hidden) {
    refreshOpen(instance.id);
    return;
  }
  modal.hidden = false; document.body.style.overflow = "hidden";
  $("#cm-title").textContent = instance.name; $("#cm-body").textContent = "Loading…";
  pushModal(modal, { onEscape: closeCompute });
  const hash = `#/compute/${encodeURIComponent(instance.id)}`;
  if (location.hash !== hash) history.pushState(null, "", hash);
  refreshOpen(instance.id);
}

export function closeCompute() {
  const modal = $("#compute-modal"); if (modal.hidden) return;
  modal.hidden = true; document.body.style.overflow = ""; ACTIVE_INSTANCE = null;
  clearTimeout(pollTimer); pollTimer = null; popModal();
}

$$('[data-close-compute]').forEach((button) => button.addEventListener("click", () => {
  if (location.hash.startsWith("#/compute/")) history.back(); else closeCompute();
}));

async function runDetailJob(instance, button, path, body) {
  await withBusy(button, "Starting…", async () => {
    try {
      const { job } = await api(`/api/compute/${instance.id}/${path}`, { method: "POST", body: JSON.stringify(body) });
      toastOk("Maintenance job queued.");
      pollJob(job.id, async (finished) => {
        if (finished.state === "successful") toastOk(finished.summary); else toastErr(finished.summary || "Maintenance failed.");
        await loadCompute(); if (ACTIVE_INSTANCE) await refreshOpen(instance.id);
      });
    } catch (error) { toastErr(error.message); }
  });
}

async function pollJob(jobId, done) {
  clearTimeout(pollTimer);
  try {
    const { job } = await api(`/api/compute/jobs/${jobId}`);
    if (["queued", "running"].includes(job.state)) {
      pollTimer = setTimeout(() => pollJob(jobId, done), 1500); return;
    }
    await done(job);
  } catch (error) { toastErr("Couldn't read job progress: " + error.message); }
}

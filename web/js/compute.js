// Compute workload cards, filtering, detail, mappings, Docker hierarchy, and jobs.
"use strict";
import { $, $$, api, SESSION, effectiveOnline, fmtBytes, fmtUptime, timeAgo } from "./api.js";
import { toastErr, toastOk, withBusy, confirmDialog, pushModal, popModal } from "./ui.js";

let INSTANCES = [];
let FILTER = "all";
let PARENT_FILTER = null;
let ACTIVE_INSTANCE = null;
let ANSIBLE_ENABLED = false;
let pollTimer = null;
let BULK_UPDATE_ACTIVE = false;

const BULK_UPDATE_CONCURRENCY = 3;

function managedByAnsible(instance) {
  const mapping = instance.ansible || {};
  return mapping.enabled === true && !!mapping.controllerId && !!mapping.inventoryHost;
}

function updateCheckEligible(instance) {
  const mapping = instance.ansible || {};
  return mapping.updateCheckEligible ?? managedByAnsible(instance);
}

function dockerDiscoveryEligible(instance) {
  const mapping = instance.ansible || {};
  return mapping.dockerDiscoveryEligible ?? managedByAnsible(instance);
}

function dockerCheckEligible(instance) {
  return !!(instance.ansible || {}).dockerUpdateCheckEligible;
}

function osUpdateEligible(instance) {
  return !!(instance.ansible || {}).updateEligible;
}

function updateAvailable(instance) {
  return (instance.updateState || {}).state === "updates_available";
}

function bulkUpdateEligible(instance) {
  const parentState = instance.parentDevice?.state;
  return updateAvailable(instance) && osUpdateEligible(instance) &&
    instance.status === "running" && !!parentState && effectiveOnline(parentState) === true &&
    !(instance.ansible || {}).maintenanceActive;
}

function openAnsibleSettings() {
  closeCompute();
  document.dispatchEvent(new CustomEvent("hlhq:navigate", { detail: { tab: "settings" } }));
}

function attention(instance) {
  const containers = dockerContainers(instance.docker);
  const docker = containerSummary(containers, dockerDataCurrent(instance));
  return ["updates_available", "failed", "unreachable", "reboot_required"]
    .includes((instance.updateState || {}).state) || instance.discoveryState !== "current" ||
    ["bad", "warn", "unknown"].includes(docker.tone);
}

function matches(instance) {
  if (PARENT_FILTER && instance.parentDeviceId !== PARENT_FILTER) return false;
  if (FILTER === "vm" || FILTER === "lxc") return instance.type === FILTER;
  if (FILTER === "docker") return hasDockerContainers(instance);
  if (FILTER === "attention") return attention(instance);
  return true;
}

function updateLabel(instance) {
  const state = instance.updateState || {};
  if (!managedByAnsible(instance)) {
    return ANSIBLE_ENABLED ? "Not managed" : "Set up Ansible";
  }
  if (!state.state || state.state === "unknown") {
    return "Unknown";
  }
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
  const containers = dockerContainers(docker);
  if (!containers.length) return "Available";
  return containerSummary(containers, dockerDataCurrent(instance)).label;
}

function dockerContainers(docker) {
  return [...(docker?.projects || []).flatMap((project) => project.containers || []),
    ...(docker?.containers || [])];
}

function hasDockerContainers(instance) {
  return dockerContainers(instance.docker).length > 0;
}

function dockerHealth(containers) {
  const result = { healthy: 0, unhealthy: 0, starting: 0, noHealthcheck: 0,
    unknown: 0, running: 0, restarting: 0, stopped: 0, paused: 0 };
  for (const container of containers) {
    const status = containerStatus(container);
    const configured = healthcheckConfigured(container);
    if (status.state === "running") result.running += 1;
    else if (status.state === "restarting") result.restarting += 1;
    else if (["stopped", "exited", "dead"].includes(status.state)) result.stopped += 1;
    else if (status.state === "paused") result.paused += 1;
    if (configured === false) result.noHealthcheck += 1;
    if (status.kind === "healthy") result.healthy += 1;
    else if (status.kind === "unhealthy") result.unhealthy += 1;
    else if (status.kind === "starting") result.starting += 1;
    else if (status.kind === "unknown") result.unknown += 1;
  }
  return result;
}

function dockerDataCurrent(instance) {
  const discovery = (instance.dockerDiscoveryState || {}).state;
  return !["failed", "unreachable", "unknown", "incomplete"].includes(discovery) &&
    instance.discoveryState !== "stale" && instance.discoveryState !== "unavailable";
}

function healthcheckConfigured(container) {
  if (typeof container.hasHealthcheck === "boolean") return container.hasHealthcheck;
  if (["healthy", "unhealthy", "starting"].includes(container.health)) return true;
  if (["no_healthcheck", "none"].includes(container.health)) return false;
  return null;
}

function containerStatus(container, current = true) {
  const state = String(container.state || "unknown").toLowerCase();
  if (!current) return { state, label: "Unknown", tone: "unknown", kind: "unknown" };
  if (state !== "running") {
    const states = {
      restarting: ["Restarting", "warn"], paused: ["Paused", "warn"],
      stopped: ["Stopped", "bad"], exited: ["Exited", "bad"], dead: ["Dead", "bad"],
      created: ["Created", "neutral"], removing: ["Removing", "warn"],
    };
    const [label, tone] = states[state] || ["Unknown", "unknown"];
    return { state, label, tone, kind: state === "unknown" ? "unknown" : "lifecycle" };
  }
  const configured = healthcheckConfigured(container);
  if (configured === false) {
    return { state, label: "Running", secondary: "No healthcheck", tone: "good",
      secondaryTone: "neutral", kind: "no_healthcheck" };
  }
  if (configured === true && container.health === "healthy") {
    return { state, label: "Healthy", tone: "good", kind: "healthy" };
  }
  if (configured === true && container.health === "unhealthy") {
    return { state, label: "Unhealthy", tone: "bad", kind: "unhealthy" };
  }
  if (configured === true && container.health === "starting") {
    return { state, label: "Starting", tone: "warn", kind: "starting" };
  }
  return { state, label: "Unknown", tone: "unknown", kind: "unknown" };
}

function containerSummary(containers, current = true) {
  if (!current) return { label: "Unknown", tone: "unknown" };
  if (!containers.length) return { label: "No containers", tone: "neutral" };
  const count = dockerHealth(containers);
  if (count.unhealthy) return { label: `${count.unhealthy} unhealthy`, tone: "bad" };
  if (count.restarting) return { label: `${count.restarting} restarting`, tone: "warn" };
  if (count.stopped) return { label: `${count.stopped} stopped`, tone: "bad" };
  if (count.unknown) return { label: "Unknown", tone: "unknown" };
  if (count.starting) return { label: `${count.starting} starting`, tone: "warn" };
  if (count.paused) return { label: `${count.paused} paused`, tone: "warn" };
  if (count.running === containers.length) {
    if (count.healthy === containers.length) return { label: "Healthy", tone: "good" };
    if (count.healthy) return { label: "Operational", tone: "good" };
    if (count.noHealthcheck === containers.length) return { label: "Running", tone: "good" };
  }
  return { label: `${count.running}/${containers.length} running`, tone: "warn" };
}

function projectStatus(project, current = true) {
  const containers = project.containers || [];
  if (!containers.length) return { label: project.status || "No containers", tone: "neutral" };
  return containerSummary(containers, current);
}

const NO_HEALTHCHECK_EXPLANATION = "This container is running, but its image or Compose configuration does not define a Docker healthcheck.";

function statusBadge(label, tone = "neutral", title = "") {
  const badge = document.createElement("span");
  badge.className = `compute-status compute-status-${tone}`;
  const icon = document.createElement("span"); icon.setAttribute("aria-hidden", "true");
  icon.textContent = ({ good: "✓", bad: "!", warn: "◷", unknown: "?", neutral: "–" })[tone];
  const text = document.createElement("span"); text.textContent = label;
  badge.append(icon, text); if (title) badge.title = title;
  return badge;
}

function appendContainerRow(list, container, current = true) {
  const row = document.createElement("div"); row.className = "container-row";
  const identity = document.createElement("span"); identity.className = "container-identity";
  const name = document.createElement("strong"); name.textContent = container.name;
  const detail = document.createElement("small"); detail.className = "muted";
  detail.textContent = container.composeService || container.image || "Docker container";
  identity.append(name, detail);
  const state = document.createElement("span"); state.className = "container-state";
  const status = containerStatus(container, current);
  const healthOutput = status.kind === "unhealthy" ? container.healthDetails?.output : "";
  state.appendChild(statusBadge(status.label, status.tone, healthOutput || ""));
  if (status.secondary) {
    state.appendChild(statusBadge(status.secondary, status.secondaryTone,
      NO_HEALTHCHECK_EXPLANATION));
  }
  row.append(identity, state); list.appendChild(row);
}

function dockerUpdateLabel(instance) {
  const state = instance.dockerUpdateState || {};
  if (!state.state) return null;
  if (state.state === "updates_available") return state.updateCount == null
    ? "Available" : `${state.updateCount} available`;
  return ({ up_to_date: "Up to date", checking: "Checking…", updating: "Updating…",
    failed: "Failed", unreachable: "Unreachable", unknown: "Unknown" })[state.state]
    || state.state.replaceAll("_", " ");
}

function cardDockerLabel(instance, containers) {
  const count = `${containers.length} container${containers.length === 1 ? "" : "s"}`;
  const parts = [count, dockerLabel(instance)];
  const updateState = (instance.dockerUpdateState || {}).state;
  if (["updates_available", "checking", "updating", "failed", "unreachable"].includes(updateState)) {
    parts.push(`Updates: ${dockerUpdateLabel(instance)}`);
  }
  return parts.filter(Boolean).join(" · ");
}

function appendContainerPreview(card, containers, current = true) {
  const preview = document.createElement("div"); preview.className = "compute-container-preview";
  for (const container of containers.slice(0, 5)) {
    const item = document.createElement("span"); item.className = "compute-container-chip";
    const status = containerStatus(container, current);
    const state = document.createElement("span"); state.className = `compute-chip-icon compute-status-${status.tone}`;
    state.textContent = ({ good: "✓", bad: "!", warn: "◷", unknown: "?", neutral: "–" })[status.tone];
    state.setAttribute("aria-label", status.label);
    const name = document.createElement("span"); name.textContent = container.name;
    item.title = [status.label, status.secondary].filter(Boolean).join(" · ");
    item.append(state, name); preview.appendChild(item);
  }
  if (containers.length > 5) {
    const more = document.createElement("span"); more.className = "muted compute-container-more";
    more.textContent = `+${containers.length - 5} more`; preview.appendChild(more);
  }
  card.appendChild(preview);
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
  card.tabIndex = 0;
  card.setAttribute("role", "button");
  card.setAttribute("aria-label", `View ${instance.name} details`);
  const top = document.createElement("div"); top.className = "card-row";
  const title = document.createElement("h2");
  const name = document.createElement("span"); name.textContent = instance.name;
  title.append(name);
  const badges = document.createElement("span"); badges.className = "compute-card-badges";
  const pill = document.createElement("span"); pill.className = "pill";
  pill.textContent = instance.type === "vm" ? "VM" : "LXC";
  const workloadStatus = ({ running: ["Running", "good"], stopped: ["Stopped", "bad"],
    paused: ["Paused", "warn"] })[instance.status] || ["Unknown", "unknown"];
  badges.append(pill, statusBadge(...workloadStatus)); top.append(title, badges);
  const parent = document.createElement("div"); parent.className = "muted compute-parent";
  parent.textContent = instance.node ? `Node ${instance.node}` :
    instance.parentDevice ? `Hosted on ${instance.parentDevice.name}` : "Parent unavailable";
  const stats = document.createElement("div"); stats.className = "dev-state";
  valueRow(stats, "CPU", instance.cpuCores != null ? `${instance.cpuCores} cores` : null);
  valueRow(stats, "Memory", instance.memoryBytes != null ? fmtBytes(instance.memoryBytes) : null);
  valueRow(stats, "Updates", updateLabel(instance));
  const containers = dockerContainers(instance.docker);
  if (containers.length) valueRow(stats, "Docker", cardDockerLabel(instance, containers));
  const last = document.createElement("div"); last.className = "muted updated";
  last.textContent = instance.lastDiscoveredAt ? `discovered ${timeAgo(instance.lastDiscoveredAt)}` : "not discovered";
  card.append(top, parent, stats);
  if (containers.length) appendContainerPreview(card, containers, dockerDataCurrent(instance));
  card.appendChild(last);
  card.onclick = () => openCompute(instance);
  card.onkeydown = (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault(); openCompute(instance);
    }
  };
  return card;
}

function render() {
  const attentionFilter = $("[data-compute-filter='attention']", $("#compute-filters"));
  attentionFilter.textContent = `Need Attention ${INSTANCES.filter(attention).length}`;
  const dockerFilter = $("[data-compute-filter='docker']", $("#compute-filters"));
  const hasDocker = INSTANCES.some(hasDockerContainers);
  dockerFilter.hidden = !hasDocker;
  if (!hasDocker && FILTER === "docker") {
    FILTER = "all"; PARENT_FILTER = null;
    $$("[data-compute-filter]", $("#compute-filters")).forEach((item) =>
      item.classList.toggle("active", item.dataset.computeFilter === "all"));
  }
  const visible = INSTANCES.filter(matches);
  const list = $("#compute-list"); list.innerHTML = "";
  const hosts = new Map();
  for (const instance of visible) {
    const key = instance.parentDeviceId || `unavailable-${instance.id}`;
    if (!hosts.has(key)) hosts.set(key, []);
    hosts.get(key).push(instance);
  }
  for (const workloads of hosts.values()) list.appendChild(buildHostGroup(workloads));
  const summary = $("#compute-summary"); summary.hidden = !INSTANCES.length;
  if (INSTANCES.length) {
    renderComputeSummary(summary, visible);
  }
  $("#compute-ansible-setup").hidden = SESSION.role !== "admin" || ANSIBLE_ENABLED || !INSTANCES.length;
  renderBulkUpdateButton();
  const empty = $("#compute-empty"); empty.hidden = !!visible.length;
  $(".compute-empty-title", empty).textContent = INSTANCES.length ? "No matching workloads." : "No compute workloads discovered.";
  $(".compute-empty-sub", empty).textContent = INSTANCES.length
    ? "Choose another filter." : "Add a Proxmox Device, then refresh Compute.";
}

function renderBulkUpdateButton() {
  const button = $("#compute-update-all");
  const count = INSTANCES.filter(bulkUpdateEligible).length;
  button.disabled = BULK_UPDATE_ACTIVE || count === 0;
  if (!BULK_UPDATE_ACTIVE) {
    button.textContent = "Update All";
    button.removeAttribute("aria-label");
    $("#compute-update-all-description").textContent = count
      ? `${count} eligible Compute device${count === 1 ? "" : "s"}`
      : "No eligible Compute devices";
  }
}

function buildHostGroup(workloads) {
  const parent = workloads[0].parentDevice;
  const group = document.createElement("section"); group.className = "compute-host";
  const header = document.createElement("header"); header.className = "compute-host-header";
  const identity = document.createElement("div"); identity.className = "compute-host-identity";
  const eyebrow = document.createElement("span"); eyebrow.className = "compute-eyebrow";
  eyebrow.textContent = "Compute host";
  const title = document.createElement("h2"); title.textContent = parent?.name || "Unavailable host";
  const address = document.createElement("span"); address.className = "muted";
  address.textContent = parent?.host || "Parent device is unavailable";
  identity.append(eyebrow, title, address);
  const states = document.createElement("div"); states.className = "compute-host-status";
  const online = parent?.state ? effectiveOnline(parent.state) : null;
  states.appendChild(statusBadge(online === true ? "Online" : online === false ? "Offline" : "Unknown",
    online === true ? "good" : online === false ? "bad" : "unknown"));
  const containers = workloads.flatMap((instance) => dockerContainers(instance.docker));
  if (containers.length) {
    const current = workloads.every(dockerDataCurrent);
    const docker = containerSummary(containers, current);
    states.appendChild(statusBadge(`Docker · ${docker.label}`, docker.tone));
  }
  const count = document.createElement("span"); count.className = "pill";
  count.textContent = `${workloads.length} workload${workloads.length === 1 ? "" : "s"}`;
  states.appendChild(count); header.append(identity, states);
  const cards = document.createElement("div"); cards.className = "cards compute-cards compute-host-workloads";
  workloads.forEach((instance) => cards.appendChild(buildCard(instance)));
  group.append(header, cards); return group;
}

function renderComputeSummary(summary, instances) {
  summary.innerHTML = ""; summary.className = "compute-summary-grid";
  const parents = new Map(instances.map((instance) => [instance.parentDeviceId, instance.parentDevice]));
  const online = [...parents.values()].filter((parent) => parent?.state && effectiveOnline(parent.state) === true).length;
  const offline = [...parents.values()].filter((parent) => parent?.state && effectiveOnline(parent.state) === false).length;
  const unknownHosts = parents.size - online - offline;
  const running = instances.filter((item) => item.status === "running").length;
  const stopped = instances.filter((item) => ["stopped", "exited"].includes(item.status)).length;
  const freshContainers = instances.filter(dockerDataCurrent)
    .flatMap((instance) => dockerContainers(instance.docker));
  const staleContainers = instances.filter((instance) => !dockerDataCurrent(instance))
    .flatMap((instance) => dockerContainers(instance.docker));
  const health = dockerHealth(freshContainers);
  health.unknown += staleContainers.length;
  health.noHealthcheck += staleContainers.filter(
    (container) => healthcheckConfigured(container) === false).length;
  const values = [
    ["Hosts", `${online} online · ${offline} offline${unknownHosts ? ` · ${unknownHosts} unknown` : ""}`],
    ["Workloads", `${running} running · ${stopped} stopped`],
    ["Healthchecks", `${health.healthy} healthy · ${health.unhealthy} unhealthy${health.starting ? ` · ${health.starting} starting` : ""}`],
    ["Not monitored", `${health.noHealthcheck} no healthcheck${health.unknown ? ` · ${health.unknown} unknown` : ""}`],
  ];
  for (const [label, value] of values) {
    const item = document.createElement("div"); item.className = "compute-summary-item";
    const key = document.createElement("span"); key.textContent = label;
    const detail = document.createElement("strong"); detail.textContent = value;
    item.append(key, detail); summary.appendChild(item);
  }
  if (PARENT_FILTER) {
    const filtered = document.createElement("span"); filtered.className = "compute-summary-filtered";
    filtered.textContent = "Filtered by host"; summary.appendChild(filtered);
  }
}

export async function loadCompute() {
  const list = $("#compute-list"); list.setAttribute("aria-busy", "true");
  if (!INSTANCES.length) {
    const empty = $("#compute-empty"); empty.hidden = false;
    $(".compute-empty-title", empty).textContent = "Loading compute inventory…";
    $(".compute-empty-sub", empty).textContent = "Reading hosts, workloads, and Docker status.";
  }
  try {
    const response = await api("/api/compute");
    INSTANCES = response.instances || [];
    ANSIBLE_ENABLED = !!response.ansibleEnabled;
    render();
  } catch (error) {
    if (INSTANCES.length) toastErr("Couldn't refresh Compute: " + error.message);
    else { $("#compute-empty").hidden = false; $(".compute-empty-title").textContent = "Couldn't load Compute."; $(".compute-empty-sub").textContent = error.message; }
  } finally { list.removeAttribute("aria-busy"); }
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

async function waitForRefreshJob(jobId) {
  while (true) {
    const { job } = await api(`/api/compute/jobs/${jobId}`);
    if (!["queued", "running"].includes(job.state)) return job;
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
}

function updateBulkProgress(completed, total) {
  const button = $("#compute-update-all");
  const progress = $("#compute-update-all-progress");
  const label = `Updating ${completed} of ${total} Compute device${total === 1 ? "" : "s"}…`;
  button.textContent = `Updating ${completed}/${total}…`;
  button.setAttribute("aria-label", label);
  button.setAttribute("aria-busy", "true");
  button.classList.add("spinning");
  $("span", progress).textContent = label;
  const meter = $("progress", progress);
  meter.max = total;
  meter.value = completed;
  meter.setAttribute("aria-label", label);
  progress.hidden = false;
}

async function runBounded(items, limit, task, progress) {
  const results = new Array(items.length);
  let next = 0;
  let completed = 0;
  async function worker() {
    while (next < items.length) {
      const index = next++;
      results[index] = await task(items[index]);
      completed += 1;
      progress(completed, items.length);
    }
  }
  await Promise.all(Array.from(
    { length: Math.min(limit, items.length) }, () => worker()));
  return results;
}

async function updateOneForBulk(instance) {
  try {
    const { job } = await api(`/api/compute/${instance.id}/updates`, {
      method: "POST",
      body: JSON.stringify({ allowReboot: false, rebootConfirmed: false }),
    });
    const finished = await waitForRefreshJob(job.id);
    return finished.state === "successful" ? "succeeded" : "failed";
  } catch (error) {
    return error.status === 409 ? "skipped" : "failed";
  }
}

$("#compute-update-all").addEventListener("click", async () => {
  if (BULK_UPDATE_ACTIVE) return;
  const available = INSTANCES.filter(updateAvailable);
  const eligible = available.filter(bulkUpdateEligible);
  if (!eligible.length) { renderBulkUpdateButton(); return; }
  BULK_UPDATE_ACTIVE = true;
  renderBulkUpdateButton();
  const initiallySkipped = available.length - eligible.length;
  const skipMessage = initiallySkipped
    ? ` ${initiallySkipped} other device${initiallySkipped === 1 ? "" : "s"} with available updates will be skipped because they are offline, unsupported, or busy.`
    : "";
  const confirmed = await confirmDialog({
    title: `Update ${eligible.length} Compute device${eligible.length === 1 ? "" : "s"}?`,
    message: `Each device will run its approved OS update playbook. Reboot permission is OFF.${skipMessage}`,
    okLabel: "Update All",
    danger: true,
  });
  if (!confirmed) {
    BULK_UPDATE_ACTIVE = false;
    renderBulkUpdateButton();
    return;
  }
  updateBulkProgress(0, eligible.length);
  let results = [];
  try {
    results = await runBounded(
      eligible, BULK_UPDATE_CONCURRENCY, updateOneForBulk, updateBulkProgress);
  } finally {
    await loadCompute();
    BULK_UPDATE_ACTIVE = false;
    const button = $("#compute-update-all");
    button.removeAttribute("aria-busy");
    button.classList.remove("spinning");
    $("#compute-update-all-progress").hidden = true;
    renderBulkUpdateButton();
  }
  const succeeded = results.filter((result) => result === "succeeded").length;
  const failed = results.filter((result) => result === "failed").length;
  const skipped = initiallySkipped +
    results.filter((result) => result === "skipped").length;
  const summary = `Bulk update complete: ${succeeded} succeeded, ${failed} failed, ${skipped} skipped.`;
  if (failed) toastErr(summary); else toastOk(summary);
});

$("#compute-refresh").addEventListener("click", () => withBusy(
  $("#compute-refresh"), "Refreshing all…", async () => {
    const progress = $("#compute-refresh-progress"); progress.hidden = false;
    try {
      const result = await api("/api/compute/refresh", {
        method: "POST", body: "{}", timeoutMs: 130000,
      });
      const entries = result.maintenanceJobs || [];
      const queuedJobs = entries.filter((entry) => entry.queued)
        .flatMap((entry) => entry.jobs || []);
      const finished = await Promise.all(
        queuedJobs.map((job) => waitForRefreshJob(job.jobId)));
      await loadCompute();
      const failures = finished.filter((job) => job.state !== "successful").length +
        entries.filter((entry) => !entry.queued).length;
      if (failures) {
        toastErr(`Refresh completed with ${failures} maintenance check${failures === 1 ? "" : "s"} needing attention.`);
      } else if (queuedJobs.length) {
        toastOk("Compute, OS updates, and Docker updates refreshed.");
      } else {
        toastOk("Compute refreshed; no maintenance checks were eligible.");
      }
    } catch (error) { toastErr(error.message); }
    finally { progress.hidden = true; }
  }));

function section(title) {
  const el = document.createElement("section"); el.className = "detail-section";
  const heading = document.createElement("h3"); heading.textContent = title; el.appendChild(heading);
  return el;
}

function workloadDetail(list, label, value) {
  if (value == null || value === "") return;
  const row = document.createElement("div"); row.className = "workload-detail";
  const key = document.createElement("dt"); key.textContent = label;
  const val = document.createElement("dd"); val.textContent = String(value);
  row.append(key, val); list.appendChild(row);
}

function infoSection(instance) {
  const el = section("Workload");
  const details = document.createElement("dl"); details.className = "workload-details";
  workloadDetail(details, "Type", instance.type === "vm" ? "Virtual machine" : "LXC container");
  workloadDetail(details, "Provider", instance.provider);
  workloadDetail(details, "ID", instance.providerInstanceId);
  workloadDetail(details, "Status", instance.status);
  workloadDetail(details, "Node", instance.node);
  workloadDetail(details, "CPU", instance.cpuCores != null ? `${instance.cpuCores} cores` : null);
  workloadDetail(details, "Memory", instance.memoryBytes != null ? fmtBytes(instance.memoryBytes) : null);
  workloadDetail(details, "Disk", instance.diskBytes != null ? fmtBytes(instance.diskBytes) : null);
  workloadDetail(details, "IP", (instance.ipAddresses || []).join(", "));
  workloadDetail(details, "Uptime", instance.uptimeSeconds != null ? fmtUptime(instance.uptimeSeconds) : null);
  workloadDetail(details, "OS", instance.os);
  workloadDetail(details, "Discovery", instance.discoveryState);
  el.appendChild(details);
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
  const managed = managedByAnsible(instance);
  const status = document.createElement("p"); status.className = "muted";
  status.textContent = managed
    ? `Managed by Ansible as ${mapping.inventoryHost}.`
    : "Ansible management is off. Confirm an inventory host to enable update and Docker checks.";
  el.appendChild(status);
  if (SESSION.role === "admin" && controller) {
    const form = document.createElement("div"); form.className = "inline-form compute-mapping";
    const field = document.createElement("label"); field.className = "compute-mapping-field";
    const fieldName = document.createElement("span"); fieldName.textContent = "Ansible inventory host";
    const select = document.createElement("select");
    select.setAttribute("aria-label", "Ansible inventory host");
    const placeholder = document.createElement("option"); placeholder.value = ""; placeholder.textContent = "Not managed by Ansible"; select.appendChild(placeholder);
    for (const host of (controller.inventory || {}).hosts || []) {
      const option = document.createElement("option"); option.value = host.name;
      option.textContent = `${host.name}${host.address && host.address !== host.name ? ` · ${host.address}` : ""}`;
      option.selected = mapping.inventoryHost === host.name; select.appendChild(option);
    }
    const suggestions = instance.suggestedMappings || [];
    if (!managed && suggestions.length) select.value = suggestions[0].inventoryHost;
    field.append(fieldName, select);

    const persistMapping = async (inventoryHost, button) => {
      await withBusy(button, "Saving…", async () => {
        try {
          const enabled = !!inventoryHost;
          const body = enabled
            ? { enabled: true, controllerId: controller.id, inventoryHost }
            : { enabled: false };
          await api(`/api/compute/${instance.id}/ansible`, {
            method: "POST", body: JSON.stringify(body),
          });
          await loadCompute();
          toastOk("Ansible mapping saved.");
          await refreshOpen(instance.id);
        }
        catch (error) { toastErr(error.message); }
      });
    };

    const save = document.createElement("button");
    save.className = `btn ${managed ? "btn-ghost" : "btn-primary"} btn-sm`;
    save.onclick = () => persistMapping(select.value, save);
    const updateSaveButton = () => {
      save.disabled = !managed && !select.value;
      save.textContent = select.value
        ? (managed ? "Save selected host" : "Manage selected host")
        : (managed ? "Stop managing with Ansible" : "Choose a host");
    };
    select.onchange = updateSaveButton;
    updateSaveButton();
    form.append(field, save); el.appendChild(form);

    if (suggestions.length && !managed) {
      const suggestion = document.createElement("div"); suggestion.className = "hint mapping-suggestion";
      const suggestedHost = suggestions[0].inventoryHost;
      const copy = document.createElement("span");
      copy.textContent = suggestions.length === 1
        ? `Ansible found a likely inventory match: ${suggestedHost}.`
        : `Ansible found likely matches: ${suggestions.map((item) => item.inventoryHost).join(", ")}.`;
      const confirm = document.createElement("button"); confirm.className = "btn btn-primary btn-sm";
      confirm.textContent = `Manage with Ansible as ${suggestedHost}`;
      confirm.onclick = () => persistMapping(suggestedHost, confirm);
      suggestion.append(copy, confirm); el.appendChild(suggestion);
    }
  }
  if (managed) {
    const actions = document.createElement("div"); actions.className = "action-row maintenance-actions";
    const check = document.createElement("button"); check.className = "btn btn-ghost btn-sm"; check.textContent = "Check Updates";
    check.disabled = !updateCheckEligible(instance);
    check.onclick = () => runDetailJob(instance, check, "updates/check", {});
    actions.appendChild(check);
    if (SESSION.role === "admin") {
      const rebootLabel = document.createElement("label"); rebootLabel.className = "ent-item compact-check";
      const reboot = document.createElement("input"); reboot.type = "checkbox"; reboot.checked = false;
      const text = document.createElement("span"); text.textContent = "Allow reboot if required"; rebootLabel.append(reboot, text);
      const update = document.createElement("button"); update.className = "btn btn-primary btn-sm"; update.textContent = "Update";
      update.disabled = !osUpdateEligible(instance);
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
  if (managed && SESSION.role === "admin" && controller) {
    const required = [
      [updateCheckEligible(instance), "OS update check"],
      [dockerDiscoveryEligible(instance), "Docker discovery"],
    ].filter(([ready]) => !ready).map(([, label]) => label);
    if (required.length) {
      const notice = document.createElement("div"); notice.className = "hint warn maintenance-readiness";
      const copy = document.createElement("span");
      copy.textContent = `Mapping saved. To enable checks, approve ${required.join(" and ")} playbooks in Settings → Ansible.`;
      const settings = document.createElement("button"); settings.className = "btn btn-ghost btn-sm";
      settings.textContent = "Open Ansible settings"; settings.onclick = openAnsibleSettings;
      notice.append(copy, settings); el.appendChild(notice);
    }
  }
  const state = instance.updateState || {};
  const summary = document.createElement("p"); summary.className = "muted";
  summary.textContent = `Update status: ${updateLabel(instance)}` + (state.lastCheckedAt ? ` · checked ${timeAgo(state.lastCheckedAt)}` : "");
  el.appendChild(summary);
  if (["checking", "updating"].includes(state.state)) {
    appendMaintenanceProgress(el, state.state === "updating" ? "Updating OS…" : "Checking OS updates…");
  }
  return el;
}

function appendMaintenanceProgress(parent, label) {
  const status = document.createElement("div"); status.className = "maintenance-progress";
  status.setAttribute("role", "status");
  const text = document.createElement("span"); text.textContent = label;
  const progress = document.createElement("progress");
  progress.setAttribute("aria-label", label);
  status.append(text, progress); parent.appendChild(status);
  return status;
}

function dockerSection(instance, controller) {
  const el = section("Docker"); const docker = instance.docker;
  const dockerUpdates = instance.dockerUpdateState || {};
  const discovery = instance.dockerDiscoveryState || {};
  if (dockerUpdates.state) {
    const status = document.createElement("div"); status.className = "maintenance-summary";
    const copy = document.createElement("div");
    const title = document.createElement("strong"); title.textContent = "Docker updates";
    const detail = document.createElement("span"); detail.className = "muted";
    detail.textContent = dockerUpdates.summary || (dockerUpdates.state === "unknown"
      ? "No structured update result was returned by the playbook."
      : "Last approved check result.");
    copy.append(title, detail);
    const value = document.createElement("span"); value.className = "pill";
    const label = ({ updates_available: "Available", up_to_date: "Up to date",
      checking: "Checking…", updating: "Updating…", failed: "Failed",
      unreachable: "Unreachable", unknown: "Unknown" })[dockerUpdates.state]
      || dockerUpdates.state.replaceAll("_", " ");
    const count = dockerUpdates.updateCount == null ? ""
      : ` · ${dockerUpdates.updateCount} update${dockerUpdates.updateCount === 1 ? "" : "s"}`;
    value.textContent = `${label}${count}`;
    status.append(copy, value); el.appendChild(status);
    if (["checking", "updating"].includes(dockerUpdates.state)) {
      appendMaintenanceProgress(status, dockerUpdates.state === "updating"
        ? "Updating Docker project…" : "Checking Docker updates…");
    }
  }
  if (["unknown", "failed", "unreachable"].includes(discovery.state)) {
    const status = document.createElement("div"); status.className = "maintenance-summary";
    const copy = document.createElement("div");
    const title = document.createElement("strong"); title.textContent = "Docker discovery";
    const detail = document.createElement("span"); detail.className = "muted";
    detail.textContent = discovery.summary || discovery.lastErrorSummary ||
      "The last discovery did not return Docker inventory data.";
    copy.append(title, detail);
    const value = document.createElement("span"); value.className = "pill";
    value.textContent = discovery.state === "unknown" ? "Incomplete"
      : discovery.state.charAt(0).toUpperCase() + discovery.state.slice(1);
    status.append(copy, value); el.appendChild(status);
  }
  if (!docker) {
    const p = document.createElement("p"); p.className = "muted";
    p.textContent = discovery.state
      ? "No Docker inventory has been received yet."
      : "Docker has not been discovered yet.";
    el.appendChild(p);
  } else {
    const p = document.createElement("p"); p.className = "docker-versions";
    const compose = docker.composeAvailable == null ? "unknown"
      : docker.composeAvailable ? (docker.composeVersion || "available") : "unavailable";
    p.textContent = docker.available == null ? "Docker availability is unknown"
      : docker.available ? `Docker ${docker.version || "available"} · Compose ${compose}` : "Docker unavailable";
    el.appendChild(p);
    const allContainers = dockerContainers(docker);
    if (docker.available && allContainers.length) {
      const health = dockerHealth(allContainers);
      const overview = document.createElement("div"); overview.className = "docker-overview";
      const values = [
        [`${allContainers.length} container${allContainers.length === 1 ? "" : "s"}`, "neutral", ""],
        [health.healthy ? `${health.healthy} healthy` : "", "good", ""],
        [health.unhealthy ? `${health.unhealthy} unhealthy` : "", "bad", ""],
        [health.starting ? `${health.starting} starting` : "", "warn", ""],
        [health.noHealthcheck ? `${health.noHealthcheck} no healthcheck` : "", "neutral",
          NO_HEALTHCHECK_EXPLANATION],
        [health.unknown ? `${health.unknown} unknown` : "", "unknown", ""],
      ];
      for (const [value, tone, title] of values.filter(([value]) => value)) {
        overview.appendChild(statusBadge(value, tone, title));
      }
      el.appendChild(overview);
    }
    if (docker.summary) {
      const summary = document.createElement("p"); summary.className = "muted";
      summary.textContent = docker.summary; el.appendChild(summary);
    }
    if ((docker.projects || []).length) {
      const heading = document.createElement("h4"); heading.className = "docker-subheading";
      heading.textContent = "Compose projects"; el.appendChild(heading);
    }
    for (const project of docker.projects || []) {
      const box = document.createElement("div"); box.className = "compose-project";
      const head = document.createElement("div"); head.className = "compose-project-header";
      const heading = document.createElement("div");
      const kind = document.createElement("span"); kind.className = "compute-eyebrow";
      kind.textContent = "Compose project";
      const title = document.createElement("strong"); title.textContent = project.name;
      heading.append(kind, title);
      const summaryStatus = projectStatus(project, dockerDataCurrent(instance));
      const status = statusBadge(summaryStatus.label, summaryStatus.tone);
      head.append(heading, status); box.appendChild(head);
      const projectSummary = document.createElement("p"); projectSummary.className = "muted";
      const projectHealth = dockerHealth(project.containers || []);
      projectSummary.textContent = `${(project.containers || []).length} container${(project.containers || []).length === 1 ? "" : "s"}` +
        (projectHealth.noHealthcheck ? ` · ${projectHealth.noHealthcheck} without healthcheck` : "");
      box.appendChild(projectSummary);
      const list = document.createElement("div"); list.className = "container-list";
      for (const container of project.containers || []) {
        appendContainerRow(list, container, dockerDataCurrent(instance));
      }
      box.appendChild(list);
      if ((project.images || []).length) {
        const images = document.createElement("p"); images.className = "muted";
        images.textContent = `Images: ${project.images.map((image) => image.name || (image.tags || []).join(", ") || image.id).join(", ")}`;
        box.appendChild(images);
      }
      if (project.updateState) {
        const updates = document.createElement("p"); updates.className = "muted";
        const available = project.updateState.updatesAvailable;
        updates.textContent = `Updates: ${available == null ? "unknown" : available ? "available" : "up to date"}` +
          (project.updateState.summary ? ` · ${project.updateState.summary}` : "");
        box.appendChild(updates);
      }
      const controls = document.createElement("div"); controls.className = "inline-form";
      if (managedByAnsible(instance)) {
        const check = document.createElement("button"); check.className = "btn btn-ghost btn-sm";
        check.textContent = "Check updates"; check.disabled = !dockerCheckEligible(instance);
        check.onclick = () => runDetailJob(instance, check, "docker/check", {
          projectName: project.name,
        });
        controls.appendChild(check);
      }
      if (SESSION.role === "admin") {
        const strategy = document.createElement("select");
        const currentMode = project.updateMode || ({ local_build: "build", unmanaged: "read_only" })[project.updateStrategy] || project.updateStrategy || "read_only";
        for (const [value, label] of [["read_only", "Read-only"], ["pull", "Pull and recreate"], ["build", "Local build and recreate"]]) {
          const option = document.createElement("option"); option.value = value; option.textContent = label; option.selected = currentMode === value; strategy.appendChild(option);
        }
        strategy.onchange = async () => {
          try {
            const saved = (await api(`/api/compute/${instance.id}/docker/projects/${encodeURIComponent(project.name)}/strategy`, { method: "POST", body: JSON.stringify({ mode: strategy.value }) })).project;
            project.managed = saved.managed; project.updateMode = saved.updateMode;
            const supported = (instance.ansible?.dockerUpdateModes || []).includes(strategy.value);
            update.disabled = !saved.managed || strategy.value === "read_only" || !supported;
            update.textContent = strategy.value === "build" ? "Rebuild & Deploy" : "Update Stack";
            toastOk("Docker update method saved.");
          }
          catch (error) { toastErr(error.message); }
        };
        const update = document.createElement("button"); update.className = "btn btn-ghost btn-sm";
        update.textContent = currentMode === "build" ? "Rebuild & Deploy" : "Update Stack";
        update.disabled = !project.managed || currentMode === "read_only" ||
          !(instance.ansible?.dockerUpdateModes || []).includes(currentMode);
        update.onclick = async () => {
          const confirmed = await confirmDialog({ title: `Update “${project.name}”?`, message: `Run its approved ${strategy.options[strategy.selectedIndex].text.toLowerCase()} playbook?`, okLabel: "Update Stack", danger: true });
          if (confirmed) runDetailJob(instance, update,
            `docker/projects/${encodeURIComponent(project.name)}/update`, {});
        };
        controls.append(strategy, update);
      }
      if (controls.children.length) box.appendChild(controls);
      el.appendChild(box);
    }
    if ((docker.containers || []).length) {
      const heading = document.createElement("h4"); heading.className = "docker-subheading";
      heading.textContent = "Other containers"; el.appendChild(heading);
      const list = document.createElement("div"); list.className = "container-list direct-containers";
      for (const container of docker.containers) {
        appendContainerRow(list, container, dockerDataCurrent(instance));
      }
      el.appendChild(list);
    }
    if ((docker.images || []).length) {
      const images = document.createElement("p"); images.className = "muted";
      images.textContent = `${docker.images.length} host image${docker.images.length === 1 ? "" : "s"}`;
      el.appendChild(images);
    }
  }
  if (managedByAnsible(instance)) {
    const actions = document.createElement("div"); actions.className = "action-row maintenance-actions";
    const discover = document.createElement("button"); discover.className = "btn btn-ghost btn-sm";
    discover.textContent = instance.docker ? "Refresh Docker" : "Discover";
    discover.setAttribute("aria-label", instance.docker ? "Refresh Docker" : "Discover Docker");
    discover.disabled = !dockerDiscoveryEligible(instance);
    discover.onclick = () => runDetailJob(instance, discover, "docker/discover", {});
    actions.appendChild(discover); el.appendChild(actions);
  }
  return el;
}

function historySection(jobs) {
  const el = section("Recent maintenance");
  if (!jobs.length) { const p = document.createElement("p"); p.className = "muted"; p.textContent = "No maintenance jobs yet."; el.appendChild(p); return el; }
  for (const job of jobs) {
    const box = document.createElement("details"); box.className = "job-history";
    const summary = document.createElement("summary");
    summary.textContent = `${job.operation.replaceAll("_", " ")}` +
      (job.projectName ? ` · ${job.projectName}` : "") +
      ` · ${job.state} · ${timeAgo(job.createdAt)}`;
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
  body.appendChild(dockerSection(instance, controller));
  let jobs = [];
  try { jobs = (await api(`/api/compute/${instance.id}/jobs`)).jobs || []; } catch (_) {}
  body.appendChild(historySection(jobs));
}

async function refreshOpen(id) {
  try {
    const { instance } = await api(`/api/compute/${id}`); ACTIVE_INSTANCE = instance;
    $("#cm-title").textContent = instance.name; $("#cm-sub").textContent = `${instance.type.toUpperCase()} · ${instance.parentDevice ? `Hosted on ${instance.parentDevice.name}` : "Parent unavailable"}`;
    $("#cm-dot").className = "dot " + (instance.status === "running" ? "up" : instance.status === "stopped" ? "down" : "unknown");
    $("#cm-status-text").textContent = `${instance.status || "unknown"} · `;
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
  let progress = null;
  await withBusy(button, "Starting…", async () => {
    try {
      const { job } = await api(`/api/compute/${instance.id}/${path}`, { method: "POST", body: JSON.stringify(body) });
      toastOk("Maintenance job queued.");
      progress = appendMaintenanceProgress(
        button.closest(".detail-section") || button.parentElement,
        `${job.operation.includes("update") ? "Updating" : "Checking"} ${job.projectName || instance.name}…`);
      pollJob(job.id, async (finished) => {
        progress?.remove();
        if (finished.state === "successful") toastOk(finished.summary);
        else toastErr(finished.summary || "Maintenance did not complete.");
        await loadCompute(); if (ACTIVE_INSTANCE) await refreshOpen(instance.id);
      }, () => progress?.remove());
    } catch (error) { progress?.remove(); toastErr(error.message); }
  });
}

async function pollJob(jobId, done, failed = null) {
  clearTimeout(pollTimer);
  try {
    const { job } = await api(`/api/compute/jobs/${jobId}`);
    if (["queued", "running"].includes(job.state)) {
      pollTimer = setTimeout(() => pollJob(jobId, done, failed), 1500); return;
    }
    await done(job);
  } catch (error) {
    failed?.(); toastErr("Couldn't read job progress: " + error.message);
  }
}

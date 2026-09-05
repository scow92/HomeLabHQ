// Compute workload cards, filtering, detail, mappings, Docker hierarchy, and jobs.
"use strict";
import { $, $$, api, SESSION, onSessionChange, getSessionGeneration, isCurrentSession,
         SessionChangedError, effectiveOnline, fmtBytes, fmtUptime, timeAgo } from "./api.js";
import { refreshState } from "./refresh-state.js";
import { requestOwner } from "./request-owner.js";
import { renderError, toastErr, toastOk, withBusy, confirmDialog, promptDialog, pushModal, popModal, closeModalChildren } from "./ui.js";

let INSTANCES = [];
let HOSTS = [];
let FILTER = "all";
let PARENT_FILTER = null;

export function applyComputeRouteContext(params = new URLSearchParams()) {
  const filter = params.get("filter");
  FILTER = ["vm", "lxc", "docker", "attention"].includes(filter) ? filter : "all";
  const parent = params.get("parent");
  PARENT_FILTER = parent && parent.length <= 128 ? parent : null;
  $$("[data-compute-filter]", $("#compute-filters")).forEach(item =>
    item.classList.toggle("active", item.dataset.computeFilter === FILTER));
}

export function computeRouteParams() {
  const params = new URLSearchParams();
  if (FILTER !== "all") params.set("filter", FILTER);
  if (PARENT_FILTER) params.set("parent", PARENT_FILTER);
  return params;
}

function syncComputeRoute(replace = false) {
  document.dispatchEvent(new CustomEvent("hlhq:route-context", {
    detail: { tab: "compute", params: computeRouteParams(), replace },
  }));
}
let ACTIVE_INSTANCE = null;
const inventoryRequests = requestOwner();
const inventoryState = refreshState("compute-refresh-state", $("#compute-list"), "Compute", loadCompute);
export function stopComputeReads() { inventoryRequests.invalidate(); }
const detailRequests = requestOwner();
const detailViews = requestOwner();
let computeView = null;
let ANSIBLE_ENABLED = false;
let pollTimer = null;
let BULK_UPDATE_ACTIVE = false;
const PROXMOX_CATALOGUES = new Map();
const PROXMOX_POLL_TIMERS = new Map();
const PROXMOX_NODE_OPERATIONS = new Map();
const PROXMOX_CLUSTER_OPERATIONS = new Map();
const PROXMOX_EXPANDED = new Set();
const PROXMOX_REFRESHING = new Set();
const PROXMOX_REFRESH_ERRORS = new Map();

onSessionChange(() => {
  inventoryRequests.invalidate();
  INSTANCES = []; HOSTS = []; FILTER = "all"; PARENT_FILTER = null;
  detailViews.invalidate(); detailRequests.invalidate(); computeView = null;
  $("#cm-body").removeAttribute("aria-busy");
  ACTIVE_INSTANCE = null; ANSIBLE_ENABLED = false; BULK_UPDATE_ACTIVE = false;
  clearTimeout(pollTimer); pollTimer = null;
  for (const timer of PROXMOX_POLL_TIMERS.values()) clearTimeout(timer);
  for (const cache of [PROXMOX_CATALOGUES, PROXMOX_POLL_TIMERS, PROXMOX_NODE_OPERATIONS,
    PROXMOX_CLUSTER_OPERATIONS, PROXMOX_EXPANDED, PROXMOX_REFRESHING, PROXMOX_REFRESH_ERRORS]) cache.clear();
  for (const selector of ["#compute-list", "#compute-summary", "#cm-title", "#cm-sub",
    "#cm-body", "#cm-status-text", "#compute-refresh-detail-list", "#compute-update-all-detail-list"]) {
    $(selector).replaceChildren();
  }
  $("#compute-modal").hidden = true;
  $("#compute-refresh-progress").hidden = true;
  $("#compute-update-all-progress").hidden = true;
  $$("[data-compute-filter]").forEach(item => item.classList.toggle("active", item.dataset.computeFilter === "all"));
});

const BULK_UPDATE_CONCURRENCY = 3;

function managedByAnsible(instance) {
  const mapping = instance.ansible || {};
  return mapping.enabled === true && !!mapping.controllerId && !!mapping.inventoryHost;
}

function osMaintenanceCapable(instance) {
  const mapping = instance.ansible || {};
  return mapping.capabilities?.osMaintenance ??
    !!(mapping.updateCheckEligible || mapping.updateEligible);
}

function dockerMaintenanceCapable(instance) {
  const mapping = instance.ansible || {};
  return mapping.capabilities?.dockerMaintenance ??
    !!(mapping.dockerDiscoveryEligible || mapping.dockerUpdateCheckEligible);
}

function applianceHealthCapable(instance) {
  return !!(instance.ansible || {}).capabilities?.applianceHealth;
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

function bulkUpdateSkipReason(instance) {
  if (!osUpdateEligible(instance)) return "OS updates are not supported";
  if ((instance.ansible || {}).maintenanceActive) return "Maintenance is already running";
  if (instance.status !== "running") return "Workload is not running";
  const parentState = instance.parentDevice?.state;
  if (!parentState || effectiveOnline(parentState) !== true) {
    return "Host is offline or unreachable";
  }
  return null;
}

function bulkUpdateEligible(instance) {
  return updateAvailable(instance) && !bulkUpdateSkipReason(instance);
}

function openAnsibleSettings() {
  closeCompute();
  document.dispatchEvent(new CustomEvent("hlhq:navigate", { detail: { tab: "settings" } }));
}

function attention(instance) {
  const containers = dockerContainers(instance.docker);
  const docker = containerSummary(containers, dockerDataCurrent(instance));
  const applianceHealth = (instance.applianceHealthState || {}).state;
  return (osMaintenanceCapable(instance) &&
    ["updates_available", "failed", "unreachable", "reboot_required"]
      .includes((instance.updateState || {}).state)) ||
    (applianceHealthCapable(instance) &&
      ["failed", "unreachable"].includes(applianceHealth)) ||
    instance.discoveryState !== "current" ||
    ["bad", "warn", "unknown"].includes(docker.tone);
}

function fallbackHost(instance) {
  return {
    id: computeHostKey(instance), node: workloadNode(instance) || null,
    parentDevice: instance.parentDevice, maintenance: null,
    sshConfigured: false, maintenanceCheckedAt: null,
  };
}

function proxmoxNodeKey(deviceId, node) {
  return `${deviceId}\u0000${node || "parent"}`;
}

function proxmoxNodeState(host) {
  const deviceId = host.parentDevice?.id;
  const catalogue = deviceId ? PROXMOX_CATALOGUES.get(deviceId) : null;
  const live = (catalogue?.nodes || []).find((item) => item.node === host.node);
  const nodeKey = proxmoxNodeKey(deviceId, host.node);
  const packages = live?.packages || host.maintenance?.packages || [];
  return {
    status: live?.status || host.maintenance?.status || "unknown",
    updateCount: live ? packages.length : host.maintenance?.updateCount,
    packages,
    reboot: live?.reboot || host.maintenance?.reboot || null,
    sshConfigured: catalogue ? !!catalogue.sshConfigured : !!host.sshConfigured,
    operation: PROXMOX_NODE_OPERATIONS.get(nodeKey) || null,
    clusterOperation: deviceId ? PROXMOX_CLUSTER_OPERATIONS.get(deviceId) || null : null,
    refreshing: PROXMOX_REFRESHING.has(nodeKey),
    refreshError: PROXMOX_REFRESH_ERRORS.get(nodeKey) || host.maintenanceRefreshError || null,
    refreshFailedAt: host.maintenanceRefreshFailedAt || null,
  };
}

function hostNeedsAttention(host) {
  const status = proxmoxNodeState(host).reboot?.rebootStatus;
  return status === "required" || status === "unknown" || !status;
}

function hostMatches(host, workloads) {
  if (PARENT_FILTER && host.parentDevice?.id !== PARENT_FILTER) return false;
  if (FILTER === "vm" || FILTER === "lxc") return workloads.some((item) => item.type === FILTER);
  if (FILTER === "docker") return workloads.some(hasDockerContainers);
  if (FILTER === "attention") return hostNeedsAttention(host) || workloads.some(attention);
  return true;
}

function matches(instance) {
  if (PARENT_FILTER && instance.parentDeviceId !== PARENT_FILTER) return false;
  if (FILTER === "vm" || FILTER === "lxc") return instance.type === FILTER;
  if (FILTER === "docker") return hasDockerContainers(instance);
  if (FILTER === "attention") return attention(instance);
  return true;
}

function workloadNode(instance) {
  return typeof instance.node === "string" ? instance.node.trim() : "";
}

function computeHostKey(instance) {
  const node = workloadNode(instance);
  const provider = instance.parentDeviceId || instance.provider || `unavailable-${instance.id}`;
  return node ? `${provider}\u0000node\u0000${node}` : `${provider}\u0000parent`;
}

function workloadLocation(instance) {
  const node = workloadNode(instance);
  if (node) return `Node ${node}`;
  return instance.parentDevice ? `Hosted on ${instance.parentDevice.name}` : "Parent unavailable";
}

function updateLabel(instance) {
  if (!osMaintenanceCapable(instance)) return null;
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
    unknown: 0, running: 0, restarting: 0, stopped: 0, paused: 0,
    completed: 0, failed: 0 };
  for (const container of containers) {
    const status = containerStatus(container);
    if (status.state === "running") result.running += 1;
    if (status.kind === "restarting") result.restarting += 1;
    else if (status.kind === "stopped") result.stopped += 1;
    else if (status.kind === "paused") result.paused += 1;
    else if (status.kind === "completed") result.completed += 1;
    else if (status.kind === "failed") result.failed += 1;
    if (status.kind === "no_healthcheck") result.noHealthcheck += 1;
    if (status.kind === "healthy") result.healthy += 1;
    else if (status.kind === "unhealthy") result.unhealthy += 1;
    else if (status.kind === "starting") result.starting += 1;
    else if (status.kind === "unknown") result.unknown += 1;
  }
  return result;
}

function dockerDataCurrent(instance) {
  if ((instance.ansible || {}).capabilities?.dockerMaintenance === false) return true;
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
  const lifecycle = String(
    (container.labels || {})["com.homelabhq.lifecycle"] || ""
  ).trim().toLowerCase();
  const oneShot = lifecycle === "oneshot" ? true : container.oneShot;
  if (!current) return { state, label: "Unknown", tone: "unknown", kind: "unknown" };
  if (state !== "running") {
    const exitCode = Number.isInteger(container.exitCode) ? container.exitCode : null;
    if (["stopped", "exited"].includes(state)) {
      if (exitCode != null && exitCode !== 0) {
        return { state, label: "Failed", tone: "bad", kind: "failed" };
      }
      if (oneShot === true && exitCode === 0) {
        return { state, label: "Completed", tone: "good", kind: "completed" };
      }
      if (oneShot !== false) {
        return { state, label: "Expected state unknown", tone: "unknown", kind: "unknown",
          explanation: "Discovery did not report whether this container is expected to stop or provide a conclusive exit result." };
      }
      return { state, label: state === "exited" ? "Exited unexpectedly" : "Stopped unexpectedly",
        tone: "bad", kind: "stopped" };
    }
    const states = {
      restarting: ["Restarting", "warn", "restarting"],
      paused: ["Paused", "warn", "paused"], dead: ["Dead", "bad", "stopped"],
      created: ["Created", "neutral", "lifecycle"],
      removing: ["Removing", "warn", "lifecycle"],
    };
    const [label, tone, kind] = states[state] || ["Unknown", "unknown", "unknown"];
    return { state, label, tone, kind };
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
  if (count.failed) return { label: `${count.failed} failed`, tone: "bad" };
  if (count.restarting) return { label: `${count.restarting} restarting`, tone: "warn" };
  if (count.stopped) return { label: `${count.stopped} stopped`, tone: "bad" };
  if (count.unknown) return { label: "Unknown", tone: "unknown" };
  if (count.starting) return { label: `${count.starting} starting`, tone: "warn" };
  if (count.paused) return { label: `${count.paused} paused`, tone: "warn" };
  if (count.completed === containers.length) return { label: "Completed", tone: "good" };
  if (count.running + count.completed === containers.length) {
    if (count.completed) return { label: "Operational", tone: "good" };
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
  state.appendChild(statusBadge(
    status.label, status.tone, healthOutput || status.explanation || ""));
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
    failed: "Failed", unreachable: "Unreachable", incomplete: "Incomplete",
    not_applicable: "Not applicable", read_only: "Read-only",
    check_recommended: "Check recommended", not_checked: "Not checked",
    unknown: "Undetermined" })[state.state]
    || state.state.replaceAll("_", " ");
}

function dockerProjectUpdateStatus(project) {
  const state = project.updateState || { state: "not_checked" };
  const presentations = {
    not_checked: ["Not checked", "Run Check updates to compare this project's images."],
    checking: ["Checking…", "The approved project check is running."],
    updating: ["Updating…", "The approved project update is running."],
    updates_available: ["Update available", "A newer image is available."],
    up_to_date: ["Up to date", "No newer image was found."],
    failed: ["Check failed", "The project check failed."],
    unreachable: ["Host unreachable", "The inventory host could not be reached."],
    incomplete: ["Check incomplete", "The playbook did not return a usable project result."],
    unknown: ["Undetermined", "The check did not determine update availability."],
    check_recommended: ["Check recommended", "The project was updated; run a new check."],
    not_applicable: ["Not applicable", "Registry checks do not apply to locally built projects."],
    read_only: ["Read-only", "Inventory does not permit updates for this project."],
    unmanaged: ["Unmanaged", "Not listed in docker_compose_projects for this inventory host."],
  };
  const [label, fallback] = presentations[state.state] || ["Undetermined", "No update status is available."];
  return { state: state.state, label, detail: state.summary || state.lastErrorSummary || fallback };
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
  parent.textContent = workloadLocation(instance);
  const stats = document.createElement("div"); stats.className = "dev-state";
  valueRow(stats, "CPU", instance.cpuCores != null ? `${instance.cpuCores} cores` : null);
  valueRow(stats, "Memory", instance.memoryBytes != null ? fmtBytes(instance.memoryBytes) : null);
  valueRow(stats, "Updates", updateLabel(instance));
  if (applianceHealthCapable(instance)) {
    const health = (instance.applianceHealthState || {}).state;
    valueRow(stats, "Appliance", ({ available: "Available", checking: "Checking…",
      failed: "Unavailable", unreachable: "Unavailable" })[health] || "Unknown");
  }
  const containers = dockerContainers(instance.docker);
  if (containers.length) valueRow(stats, "Docker", cardDockerLabel(instance, containers));
  const last = document.createElement("div"); last.className = "muted updated";
  last.textContent = instance.lastDiscoveredAt ? `discovered ${timeAgo(instance.lastDiscoveredAt)}` : "not discovered";
  card.append(top, parent, stats);
  if (containers.length) appendContainerPreview(card, containers, dockerDataCurrent(instance));
  card.appendChild(last);
  const open = () => document.dispatchEvent(new CustomEvent("hlhq:open-compute", { detail: instance }));
  card.onclick = open;
  card.onkeydown = (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault(); open();
    }
  };
  return card;
}

function render() {
  if (!SESSION) return;
  const attentionFilter = $("[data-compute-filter='attention']", $("#compute-filters"));
  const attentionIds = new Set([
    ...INSTANCES.filter(attention).map((instance) => instance.id),
    ...HOSTS.filter(hostNeedsAttention).map((host) => host.id),
  ]);
  attentionFilter.textContent = `Need Attention ${attentionIds.size}`;
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
  const hosts = new Map(HOSTS.map((host) => [
    host.node ? `${host.parentDevice?.id}\u0000node\u0000${host.node}`
      : `${host.parentDevice?.id}\u0000parent`,
    { host, workloads: [] },
  ]));
  for (const instance of visible) {
    const key = computeHostKey(instance);
    if (!hosts.has(key)) hosts.set(key, { host: fallbackHost(instance), workloads: [] });
    hosts.get(key).workloads.push(instance);
  }
  for (const entry of hosts.values()) {
    if (hostMatches(entry.host, entry.workloads)) list.appendChild(
      buildHostGroup(entry.host, entry.workloads));
  }
  const renderedHosts = [...hosts.values()].filter((entry) =>
    hostMatches(entry.host, entry.workloads));
  const summary = $("#compute-summary"); summary.hidden = !renderedHosts.length;
  if (renderedHosts.length) {
    renderComputeSummary(summary, visible, renderedHosts);
  }
  $("#compute-ansible-setup").hidden = SESSION.role !== "admin" || ANSIBLE_ENABLED || !INSTANCES.length;
  renderBulkUpdateButton();
  const empty = $("#compute-empty"); empty.hidden = !!renderedHosts.length;
  $(".compute-empty-title", empty).textContent = (INSTANCES.length || HOSTS.length)
    ? "No matching workloads or hosts." : "No compute workloads discovered.";
  $(".compute-empty-sub", empty).textContent = (INSTANCES.length || HOSTS.length)
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

function buildHostGroup(host, workloads) {
  const node = host.node || workloadNode(workloads[0] || {});
  const parent = host.parentDevice || workloads[0]?.parentDevice;
  const group = document.createElement("section"); group.className = "compute-host";
  const header = document.createElement("header"); header.className = "compute-host-header";
  const identity = document.createElement("div"); identity.className = "compute-host-identity";
  const eyebrow = document.createElement("span"); eyebrow.className = "compute-eyebrow";
  eyebrow.textContent = "Compute host";
  const title = document.createElement("h2"); title.textContent = node || parent?.name || "Unavailable host";
  const address = document.createElement("span"); address.className = "muted";
  address.textContent = node && parent
    ? `Discovered via ${parent.name}${parent.host ? ` · ${parent.host}` : ""}`
    : parent?.host || "Parent device is unavailable";
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
  group.append(header);
  if (parent?.driverId === "proxmox.ve") group.appendChild(buildProxmoxMaintenance(host));
  if (workloads.length) group.appendChild(cards);
  return group;
}

function operationVersion(operation) {
  return Number(operation?.updatedAt || operation?.finishedAt || operation?.startedAt || 0);
}

function clearProxmoxNodeOperations(deviceId, taskId) {
  let changed = false;
  const currentCluster = PROXMOX_CLUSTER_OPERATIONS.get(deviceId);
  if (currentCluster?.id === taskId) {
    PROXMOX_CLUSTER_OPERATIONS.delete(deviceId);
    changed = true;
  }
  for (const [key, operation] of PROXMOX_NODE_OPERATIONS) {
    if (operation.deviceId === deviceId && operation.taskId === taskId) {
      PROXMOX_NODE_OPERATIONS.delete(key);
      changed = true;
    }
  }
  return changed;
}

function reconcileProxmoxOperation(deviceId, operation, { forceNew = false } = {}) {
  if (!operation?.id) return false;
  const currentCluster = PROXMOX_CLUSTER_OPERATIONS.get(deviceId);
  if (currentCluster && currentCluster.id !== operation.id && !forceNew) {
    const olderStart = Number(operation.startedAt || 0) < Number(currentCluster.startedAt || 0);
    const olderUpdate = operationVersion(operation) < operationVersion(currentCluster);
    if (olderStart || olderUpdate) return false;
  }
  if (currentCluster?.id === operation.id) {
    if (operationVersion(operation) < operationVersion(currentCluster)) return false;
    if (["completed", "failed", "cancelled"].includes(currentCluster.state) &&
        operation.state === "running") return false;
  }
  if (operation.operationType === "reboot" && operation.state === "completed") {
    return clearProxmoxNodeOperations(deviceId, operation.id);
  }
  PROXMOX_CLUSTER_OPERATIONS.set(deviceId, operation);
  for (const node of operation.nodes || []) {
    if (!node.node || (node.taskId && node.taskId !== operation.id)) continue;
    const key = proxmoxNodeKey(deviceId, node.node);
    const current = PROXMOX_NODE_OPERATIONS.get(key);
    if (current && current.taskId !== operation.id && !forceNew) {
      const olderStart = Number(operation.startedAt || 0) < Number(current.startedAt || 0);
      const olderUpdate = operationVersion(operation) < Number(current.updatedAt || 0);
      if (olderStart || olderUpdate) continue;
    }
    if (current?.taskId === operation.id) {
      if (operationVersion(operation) < Number(current.updatedAt || 0)) continue;
      if (["completed", "failed", "cancelled"].includes(current.status) &&
          node.state === "running") continue;
    }
    PROXMOX_NODE_OPERATIONS.set(key, {
      taskId: operation.id, deviceId, nodeId: node.node,
      status: node.state || operation.state, stage: node.stage || operation.stage,
      progressMode: node.progressMode || operation.progressMode,
      progress: node.percent, currentPackage: node.currentPackage || null,
      message: node.message || operation.message,
      startedAt: operation.startedAt, completedAt: operation.finishedAt,
      updatedAt: operationVersion(operation), error: node.state === "failed"
        ? node.message || operation.message : null,
      rebootRequired: node.rebootRequired, rebootStatus: node.rebootStatus,
      operationType: operation.operationType || "update",
    });
  }
  return true;
}

async function loadProxmoxCatalogue(deviceId, node = null) {
  const generation = getSessionGeneration();
  const key = proxmoxNodeKey(deviceId, node);
  PROXMOX_REFRESHING.add(key); PROXMOX_REFRESH_ERRORS.delete(key); render();
  try {
    const catalogue = await api(`/api/devices/${deviceId}/updates`);
    PROXMOX_CATALOGUES.set(deviceId, catalogue);
    reconcileProxmoxOperation(deviceId, catalogue.operation);
    for (const refreshKey of [...PROXMOX_REFRESH_ERRORS.keys()]) {
      if (refreshKey.startsWith(`${deviceId}\u0000`)) PROXMOX_REFRESH_ERRORS.delete(refreshKey);
    }
    await loadCompute();
    return catalogue;
  } catch (error) {
    if (!isCurrentSession(generation)) throw error;
    PROXMOX_REFRESH_ERRORS.set(key, error.message);
    render();
    throw error;
  } finally {
    if (isCurrentSession(generation)) { PROXMOX_REFRESHING.delete(key); render(); }
  }
}

function stopProxmoxPolling(deviceId) {
  const timer = PROXMOX_POLL_TIMERS.get(deviceId);
  if (timer) clearTimeout(timer);
  PROXMOX_POLL_TIMERS.delete(deviceId);
}

function trackProxmoxOperation(deviceId, state, operation) {
  const current = PROXMOX_CATALOGUES.get(deviceId) || {};
  PROXMOX_CATALOGUES.set(deviceId, { ...current, sshConfigured: state.sshConfigured });
  reconcileProxmoxOperation(deviceId, operation, { forceNew: true });
  render();
  pollProxmoxOperation(deviceId, operation.id);
}

async function pollProxmoxOperation(deviceId, expectedTaskId) {
  const generation = getSessionGeneration();
  stopProxmoxPolling(deviceId);
  try {
    const response = await api(`/api/devices/${deviceId}/updates/status`);
    const operation = response.operation;
    if (!operation) return;
    const accepted = reconcileProxmoxOperation(deviceId, operation);
    if (accepted) render();
    if (operation.id !== expectedTaskId) {
      const expected = PROXMOX_CLUSTER_OPERATIONS.get(deviceId);
      if (expected?.id === expectedTaskId && expected.state === "running") {
        PROXMOX_POLL_TIMERS.set(deviceId, setTimeout(
          () => pollProxmoxOperation(deviceId, expectedTaskId), 1500));
      }
      return;
    }
    if (operation.state === "running") {
      PROXMOX_POLL_TIMERS.set(deviceId, setTimeout(
        () => pollProxmoxOperation(deviceId, expectedTaskId), 1500));
      return;
    }
    if (operation) {
      const reboot = operation.operationType === "reboot";
      if (reboot) {
        PROXMOX_CATALOGUES.delete(deviceId);
        await loadCompute();
        if (!isCurrentSession(generation)) return;
        if (operation.state === "completed") toastOk(operation.message);
        else toastErr(operation.message || "Proxmox node reboot failed.");
        return;
      }
      try {
        await loadProxmoxCatalogue(deviceId, operation.requestedNode);
      } catch (refreshError) {
        if (!isCurrentSession(generation)) return;
        await loadCompute();
        toastErr(`Updates finished, but package metadata could not be refreshed: ${refreshError.message}`);
      }
      if (!isCurrentSession(generation)) return;
      if (operation.state === "completed") toastOk(operation.message);
      else toastErr(operation.message || "Proxmox update installation failed.");
    }
  } catch (error) {
    if (!isCurrentSession(generation) || !SESSION) return;
    toastErr(`Couldn't read Proxmox maintenance progress: ${error.message}`);
    PROXMOX_POLL_TIMERS.set(deviceId, setTimeout(
      () => pollProxmoxOperation(deviceId, expectedTaskId), 3000));
  }
}

const PROXMOX_STAGE_LABELS = {
  preparing: "Preparing", downloading: "Downloading package metadata",
  installing: "Installing", configuring: "Configuring",
  cleaning_up: "Cleaning up", checking_reboot_status: "Checking reboot status",
  rebooting: "Rebooting", completed: "Completed", failed: "Failed",
  cancelled: "Cancelled", interrupted: "Interrupted",
};

function buildProxmoxProgress(operation) {
  if (!operation) return null;
  const box = document.createElement("section");
  box.className = `proxmox-live-progress ${operation.status || "unknown"}`;
  box.setAttribute("role", "status");
  box.setAttribute("aria-live", "polite");
  const heading = document.createElement("div"); heading.className = "proxmox-progress-heading";
  const title = document.createElement("strong");
  title.textContent = operation.operationType === "reboot" ? "Node reboot" : "Node update";
  const state = document.createElement("span"); state.className = "pill";
  state.textContent = PROXMOX_STAGE_LABELS[operation.stage] ||
    String(operation.status || "unknown").replaceAll("_", " ");
  heading.append(title, state); box.appendChild(heading);

  if (operation.status === "running" || operation.status === "pending") {
    const meter = document.createElement("progress"); meter.max = 100;
    const trustworthy = operation.progressMode === "exact" && Number.isFinite(operation.progress);
    if (trustworthy) meter.value = operation.progress;
    meter.setAttribute("aria-label", trustworthy
      ? `${operation.progress}% complete` : `${state.textContent}, progress is indeterminate`);
    box.appendChild(meter);
    if (trustworthy) {
      const percent = document.createElement("span"); percent.className = "proxmox-progress-percent";
      percent.textContent = `${operation.progress}%`; box.appendChild(percent);
    }
  }
  if (operation.currentPackage) {
    const current = document.createElement("p"); current.className = "proxmox-current-package";
    current.textContent = `Current package: ${operation.currentPackage}`; box.appendChild(current);
  }
  const message = document.createElement("p"); message.className = "muted";
  message.textContent = operation.message || "Proxmox maintenance operation";
  box.appendChild(message);
  if (operation.rebootRequired === true) {
    box.appendChild(statusBadge("Reboot required", "warn"));
  } else if (operation.status === "completed" && operation.rebootRequired === false) {
    box.appendChild(statusBadge("No reboot required", "good"));
  }
  return box;
}

function proxmoxResultReconciled(state) {
  const operation = state.operation;
  return operation?.operationType === "update" && operation.status === "completed" &&
    operation.rebootRequired === false && state.updateCount === 0 &&
    state.reboot?.rebootStatus === "not_required" && !state.refreshError;
}

function buildProxmoxUpdateList(host, state) {
  const deviceId = host.parentDevice.id;
  const key = proxmoxNodeKey(deviceId, host.node);
  const count = state.updateCount;
  const updates = document.createElement("details"); updates.className = "proxmox-updates";
  updates.open = PROXMOX_EXPANDED.has(key);
  updates.ontoggle = () => {
    if (updates.open) PROXMOX_EXPANDED.add(key); else PROXMOX_EXPANDED.delete(key);
  };
  const summary = document.createElement("summary");
  summary.textContent = state.refreshing ? "Checking updates…"
    : count == null ? "Update details unavailable"
    : count === 0 ? "Up to date"
    : `${count} update${count === 1 ? "" : "s"} available`;
  summary.setAttribute("aria-label", `${host.node || "Proxmox node"}: ${summary.textContent}`);
  updates.appendChild(summary);
  const body = document.createElement("div"); body.className = "proxmox-update-list";
  if (state.refreshError) {
    const error = document.createElement("p"); error.className = "proxmox-refresh-error";
    error.textContent = `Refresh failed: ${state.refreshError}` +
      (state.packages.length ? " Showing the latest successful update list." : "");
    body.appendChild(error);
  }
  if (state.refreshing) {
    const loading = document.createElement("p"); loading.className = "muted";
    loading.textContent = "Refreshing this node’s package information…"; body.appendChild(loading);
  }
  if (!state.packages.length) {
    const empty = document.createElement("p"); empty.className = "muted";
    empty.textContent = count === 0 ? "This node is up to date." :
      "No successful package list is available yet.";
    body.appendChild(empty);
  } else {
    for (const item of state.packages) {
      const row = document.createElement("article"); row.className = "proxmox-package";
      const name = document.createElement("strong"); name.textContent = item.name || "Unnamed package";
      const versions = document.createElement("div"); versions.className = "proxmox-package-versions";
      const installed = document.createElement("span");
      installed.textContent = `Current: ${item.installed || "Not reported"}`;
      const candidate = document.createElement("span");
      candidate.textContent = `New: ${item.available || "Not reported"}`;
      versions.append(installed, candidate); row.append(name, versions);
      if (item.source) {
        const source = document.createElement("small"); source.textContent = `Source: ${item.source}`;
        row.appendChild(source);
      }
      if (item.security === true) row.appendChild(statusBadge("Security update", "warn"));
      body.appendChild(row);
    }
  }
  updates.appendChild(body);
  return updates;
}

function buildProxmoxMaintenance(host) {
  const state = proxmoxNodeState(host);
  const reboot = state.reboot || {};
  const wrap = document.createElement("div");
  wrap.className = `compute-host-maintenance reboot-${reboot.rebootStatus || "unknown"}`;
  const details = document.createElement("div"); details.className = "compute-host-maintenance-details";
  const updateCount = state.updateCount;
  details.appendChild(buildProxmoxUpdateList(host, state));
  if (reboot.rebootStatus !== "not_required") {
    const heading = document.createElement("div"); heading.className = "compute-host-maintenance-heading";
    const labels = { required: ["Reboot required", "warn"],
      unknown: ["Reboot status unknown", "unknown"] };
    const [label, tone] = labels[reboot.rebootStatus] || labels.unknown;
    heading.appendChild(statusBadge(label, tone));
    const reason = document.createElement("p"); reason.className = "muted";
    reason.textContent = reboot.reason || "Run a Compute refresh to check this Proxmox node.";
    details.append(heading, reason);
    if (reboot.runningKernel || reboot.targetKernel) {
      const kernels = document.createElement("div"); kernels.className = "compute-host-kernels";
      if (reboot.runningKernel) {
        const running = document.createElement("span");
        running.textContent = `Running ${reboot.runningKernel}`; kernels.appendChild(running);
      }
      if (reboot.targetKernel) {
        const target = document.createElement("span");
        target.textContent = `Next boot ${reboot.targetKernel}`; kernels.appendChild(target);
      }
      details.appendChild(kernels);
    }
  }
  const progress = proxmoxResultReconciled(state) ? null : buildProxmoxProgress(state.operation);
  if (progress) details.appendChild(progress);

  const clusterRunning = state.clusterOperation?.state === "running";
  const localRunning = clusterRunning && state.operation?.taskId === state.clusterOperation.id &&
    ["pending", "running"].includes(state.operation.status);
  if (clusterRunning && !localRunning) {
    const waiting = document.createElement("p"); waiting.className = "proxmox-update-waiting";
    const activeNode = state.clusterOperation.requestedNode || state.clusterOperation.currentNode ||
      state.clusterOperation.nodes?.[0]?.node || "another node";
    waiting.textContent = `Waiting — update running on ${activeNode}`; details.appendChild(waiting);
  }

  const actions = document.createElement("div"); actions.className = "compute-host-maintenance-actions";
  const checkButton = Object.assign(document.createElement("button"), {
    className: "btn btn-sm btn-ghost", textContent: state.refreshing ? "Checking…" : "Check updates",
    disabled: clusterRunning || state.refreshing,
  });
  checkButton.onclick = () => withBusy(checkButton, "Checking…", async () => {
    try { await loadProxmoxCatalogue(host.parentDevice.id, host.node); }
    catch (error) { toastErr(error.message); }
  });
  actions.appendChild(checkButton);
  if (SESSION.role === "admin" && !state.sshConfigured) {
    const sshButton = Object.assign(document.createElement("button"), {
      className: "btn btn-sm btn-ghost", textContent: "Configure root SSH",
    });
    sshButton.onclick = async () => {
      const password = await promptDialog({
        title: "Configure root SSH",
        message: "Enter the Proxmox root password. It will be verified now, encrypted at rest, and used only for host updates and reboot checks.",
        placeholder: "Root password", okLabel: "Verify and save", inputType: "password",
      });
      if (!password) return;
      await withBusy(sshButton, "Verifying…", async () => {
        try {
          await api(`/api/devices/${host.parentDevice.id}/updates/credentials`, {
            method: "POST", body: JSON.stringify({ username: "root", password, port: 22 }),
          });
          toastOk("Root SSH credentials verified and saved.");
          await loadProxmoxCatalogue(host.parentDevice.id, host.node);
        } catch (error) { toastErr(error.message); }
      });
    };
    actions.appendChild(sshButton);
  }
  if (SESSION.role === "admin" && Number(updateCount) > 0) {
    const installButton = Object.assign(document.createElement("button"), {
      className: "btn btn-sm btn-primary",
      textContent: localRunning ? "Installing…" : `Install ${updateCount} update${updateCount === 1 ? "" : "s"}`,
      disabled: clusterRunning || !host.node,
    });
    installButton.onclick = async () => {
      const ok = await confirmDialog({
        title: `Update Proxmox node ${host.node}?`,
        message: "HomelabHQ will refresh package lists and run a non-interactive dist-upgrade on this node. Services may restart; the node will not be rebooted.",
        okLabel: "Install updates", danger: true,
      });
      if (!ok) return;
      await withBusy(installButton, "Starting…", async () => {
        try {
          const response = await api(`/api/devices/${host.parentDevice.id}/updates/install`, {
            method: "POST", body: JSON.stringify({ node: host.node }),
          });
          trackProxmoxOperation(host.parentDevice.id, state, response.operation);
        } catch (error) { toastErr(error.message); }
      });
    };
    actions.appendChild(installButton);
  }
  if (SESSION.role === "admin" && reboot.rebootStatus === "required") {
    const rebootButton = Object.assign(document.createElement("button"), {
      className: "btn btn-sm btn-danger",
      textContent: localRunning && state.operation.operationType === "reboot" ? "Rebooting…" : "Reboot node",
      disabled: clusterRunning || state.status !== "online" || !state.sshConfigured || !host.node,
    });
    rebootButton.onclick = async () => {
      const ok = await confirmDialog({
        title: `Reboot Proxmox node ${host.node}?`,
        message: "The node and its workloads will be unavailable while it restarts. HomelabHQ will send the reboot command immediately; refresh Compute after the node returns to verify its kernel.",
        okLabel: "Reboot node", danger: true,
      });
      if (!ok) return;
      await withBusy(rebootButton, "Starting…", async () => {
        try {
          const response = await api(`/api/devices/${host.parentDevice.id}/updates/reboot`, {
            method: "POST", body: JSON.stringify({ node: host.node, confirmed: true }),
          });
          trackProxmoxOperation(host.parentDevice.id, state, response.operation);
        } catch (error) { toastErr(error.message); }
      });
    };
    actions.appendChild(rebootButton);
  }
  wrap.append(details, actions);
  return wrap;
}

function renderComputeSummary(summary, instances, hostEntries) {
  summary.innerHTML = ""; summary.className = "compute-summary-grid";
  const parents = hostEntries.map((entry) => entry.host.parentDevice);
  const online = parents.filter((parent) => parent?.state && effectiveOnline(parent.state) === true).length;
  const offline = parents.filter((parent) => parent?.state && effectiveOnline(parent.state) === false).length;
  const unknownHosts = parents.length - online - offline;
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

export async function loadCompute(routeRequest = null) {
  if (!SESSION || $('[data-panel="compute"]').hidden) return INSTANCES;
  const request = inventoryRequests.begin(() => !$('[data-panel="compute"]').hidden && (!routeRequest || routeRequest.current()));
  inventoryState.start();
  const generation = getSessionGeneration();
  const list = $("#compute-list"); list.setAttribute("aria-busy", "true");
  if (!inventoryState.hasData) {
    const empty = $("#compute-empty"); empty.hidden = false;
    $(".compute-empty-title", empty).textContent = "Loading compute inventory…";
    $(".compute-empty-sub", empty).textContent = "Reading hosts, workloads, and Docker status.";
  }
  try {
    const response = await api("/api/compute", request || {});
    if (request && !request.current()) return [];
    INSTANCES = response.instances || [];
    HOSTS = response.hosts || [];
    ANSIBLE_ENABLED = !!response.ansibleEnabled;
    const operations = new Map();
    for (const host of HOSTS) {
      const deviceId = host.parentDevice?.id;
      if (deviceId && host.operation?.id) operations.set(deviceId, host.operation);
    }
    for (const [deviceId, operation] of operations) {
      reconcileProxmoxOperation(deviceId, operation);
      if (operation.state === "running" && !PROXMOX_POLL_TIMERS.has(deviceId)) {
        pollProxmoxOperation(deviceId, operation.id);
      }
    }
    render(); inventoryState.success();
  } catch (error) {
    if (!isCurrentSession(generation) || (request && !request.current())) return [];
    inventoryState.fail(error);
    if (!inventoryState.hasData) $("#compute-empty").hidden = true;
  } finally {
    if (isCurrentSession(generation) && (!request || request.current())) list.removeAttribute("aria-busy");
  }
  return INSTANCES;
}

$("#compute-filters").addEventListener("click", (event) => {
  const button = event.target.closest("[data-compute-filter]"); if (!button) return;
  FILTER = button.dataset.computeFilter; PARENT_FILTER = null;
  $$("[data-compute-filter]", $("#compute-filters")).forEach((item) => item.classList.toggle("active", item === button));
  render(); syncComputeRoute(false);
});

document.addEventListener("hlhq:compute-parent", (event) => {
  PARENT_FILTER = event.detail.deviceId; FILTER = "all";
  $$("[data-compute-filter]", $("#compute-filters")).forEach((item) => item.classList.toggle("active", item.dataset.computeFilter === "all"));
  render(); syncComputeRoute(false);
});

const OPERATION_LABELS = {
  docker_discovery: "Docker discovery",
  os_check: "OS update check",
  os_update: "OS update",
  docker_check: "Docker update check",
  docker_project_update: "Docker project update",
};

function operationLabel(operation, projectName = null) {
  const label = OPERATION_LABELS[operation] || String(operation || "Maintenance")
    .replaceAll("_", " ");
  return projectName ? `${label} · ${projectName}` : label;
}

function operationState(state) {
  return ({
    waiting: ["Waiting", "neutral"], queued: ["Queued", "neutral"],
    starting: ["Starting", "warn"], running: ["Running", "warn"],
    succeeded: ["Succeeded", "good"], successful: ["Succeeded", "good"],
    skipped: ["Skipped", "neutral"], incomplete: ["Incomplete", "warn"],
    failed: ["Failed", "bad"], unreachable: ["Unreachable", "bad"],
  })[state] || [String(state || "Unknown"), "unknown"];
}

function renderOperationDetails(id, items) {
  const details = $(`#${id}-details`);
  const list = $(`#${id}-detail-list`);
  // A newly activated service worker can briefly pair current JavaScript with
  // an older cached document. Keep the operation itself usable while that
  // document is replaced on the next navigation.
  if (!details || !list) return;
  const totals = { running: 0, waiting: 0, succeeded: 0, failed: 0, skipped: 0 };
  for (const item of items) {
    if (["starting", "running"].includes(item.state)) totals.running += 1;
    else if (["waiting", "queued"].includes(item.state)) totals.waiting += 1;
    else if (["succeeded", "successful"].includes(item.state)) totals.succeeded += 1;
    else if (item.state === "skipped") totals.skipped += 1;
    else totals.failed += 1;
  }
  const summary = ["More details"];
  for (const [key, label] of [["running", "running"], ["waiting", "waiting"],
    ["succeeded", "succeeded"], ["failed", "failed"], ["skipped", "skipped"]]) {
    if (totals[key]) summary.push(`${totals[key]} ${label}`);
  }
  $("summary", details).textContent = summary.join(" · ");
  list.innerHTML = "";
  for (const item of items) {
    const row = document.createElement("div"); row.className = "compute-operation-detail";
    const main = document.createElement("div"); main.className = "compute-operation-detail-main";
    const target = document.createElement("strong"); target.textContent = item.target;
    const operation = document.createElement("small"); operation.textContent = item.operation;
    main.append(target, operation);
    const result = document.createElement("div"); result.className = "compute-operation-detail-result";
    result.appendChild(statusBadge(...operationState(item.state)));
    if (item.summary) {
      const detail = document.createElement("small"); detail.textContent = item.summary;
      result.appendChild(detail);
    }
    row.append(main, result); list.appendChild(row);
  }
  details.hidden = false;
}

function updateOperationDetail(id, items, key, state, summary = null) {
  const item = items.find((candidate) => candidate.key === key);
  if (!item) return;
  item.state = state;
  item.summary = summary;
  renderOperationDetails(id, items);
}

async function waitForRefreshJob(jobId, onUpdate = null) {
  const generation = getSessionGeneration();
  while (true) {
    if (!isCurrentSession(generation)) throw new SessionChangedError();
    const { job } = await api(`/api/compute/jobs/${jobId}`);
    onUpdate?.(job);
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
  const generation = getSessionGeneration();
  const results = new Array(items.length);
  let next = 0;
  let completed = 0;
  async function worker() {
    while (next < items.length) {
      if (!isCurrentSession(generation)) throw new SessionChangedError();
      const index = next++;
      results[index] = await task(items[index]);
      if (!isCurrentSession(generation)) throw new SessionChangedError();
      completed += 1;
      progress(completed, items.length);
    }
  }
  await Promise.all(Array.from(
    { length: Math.min(limit, items.length) }, () => worker()));
  return results;
}

async function updateOneForBulk(instance, onUpdate) {
  onUpdate("starting", "Sending update request");
  try {
    const { job } = await api(`/api/compute/${instance.id}/updates`, {
      method: "POST",
      body: JSON.stringify({ allowReboot: false, rebootConfirmed: false }),
    });
    onUpdate(job.state || "queued", "Update job queued");
    const finished = await waitForRefreshJob(job.id, (current) =>
      onUpdate(current.state, current.summary));
    const state = finished.state === "successful" ? "succeeded" : "failed";
    onUpdate(state, finished.summary);
    return state;
  } catch (error) {
    if (error instanceof SessionChangedError) throw error;
    const state = error.status === 409 ? "skipped" : "failed";
    onUpdate(state, error.message);
    return state;
  }
}

$("#compute-update-all").addEventListener("click", async () => {
  const generation = getSessionGeneration();
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
  if (!isCurrentSession(generation)) return;
  if (!confirmed) {
    BULK_UPDATE_ACTIVE = false;
    renderBulkUpdateButton();
    return;
  }
  const detailItems = available.map((instance) => {
    const reason = bulkUpdateSkipReason(instance);
    return {
      key: instance.id,
      target: instance.name || instance.id,
      operation: "OS update",
      state: reason ? "skipped" : "waiting",
      summary: reason,
    };
  });
  renderOperationDetails("compute-update-all", detailItems);
  updateBulkProgress(0, eligible.length);
  let results = [];
  try {
    results = await runBounded(
      eligible, BULK_UPDATE_CONCURRENCY,
      (instance) => updateOneForBulk(instance, (state, summary) =>
        updateOperationDetail(
          "compute-update-all", detailItems, instance.id, state, summary)),
      updateBulkProgress);
  } catch (error) {
    if (!(error instanceof SessionChangedError)) throw error;
  } finally {
    if (!isCurrentSession(generation)) return;
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

const computeRefreshButton = $("#compute-refresh");
computeRefreshButton?.addEventListener("click", () => withBusy(
  computeRefreshButton, "Refreshing all…", async () => {
    const generation = getSessionGeneration();
    const progress = $("#compute-refresh-progress");
    if (progress) progress.hidden = false;
    let detailItems = [{
      key: "refresh", target: "Compute inventory", operation: "Discover workloads",
      state: "running", summary: "Reading compatible infrastructure devices",
    }];
    renderOperationDetails("compute-refresh", detailItems);
    try {
      const result = await api("/api/compute/refresh", {
        method: "POST", body: "{}", timeoutMs: 130000,
      });
      const entries = result.maintenanceJobs || [];
      const providers = result.providers || [];
      detailItems = providers.map((provider) => ({
        key: `provider-${provider.deviceId}`,
        target: provider.deviceName || provider.deviceId,
        operation: "Discover workloads",
        state: provider.ok ? "succeeded" : "failed",
        summary: provider.ok
          ? `${provider.discovered || 0} discovered · ${provider.created || 0} new · ${provider.stale || 0} stale`
          : provider.error || "Provider refresh failed",
      }));
      if (!providers.length) detailItems.push({
        key: "providers-none", target: "Compute providers", operation: "Discover workloads",
        state: "skipped", summary: "No compatible infrastructure devices",
      });
      const inventory = result.ansibleInventory || {};
      detailItems.push({
        key: "inventory", target: "Ansible inventory", operation: "Refresh inventory",
        state: inventory.ok ? "succeeded" : inventory.skipped ? "skipped" : "failed",
        summary: inventory.ok
          ? `${inventory.hosts || 0} hosts · ${inventory.groups || 0} groups`
          : inventory.error || inventory.skipped || "Inventory refresh failed",
      });
      const queuedJobs = entries.filter((entry) => entry.queued)
        .flatMap((entry) => (entry.jobs || []).map((job) => ({
          ...job,
          computeInstanceName: entry.computeInstanceName || entry.computeInstanceId,
        })));
      for (const entry of entries.filter((candidate) => !candidate.queued)) {
        detailItems.push({
          key: `entry-${entry.computeInstanceId}`,
          target: entry.computeInstanceName || entry.computeInstanceId,
          operation: (entry.operations || []).map((operation) =>
            operationLabel(operation)).join(", ") || "Maintenance checks",
          state: entry.reason ? "skipped" : "failed",
          summary: entry.error || entry.reason || "Checks could not be queued",
        });
      }
      for (const job of queuedJobs) detailItems.push({
        key: job.jobId,
        target: job.computeInstanceName,
        operation: operationLabel(job.operation, job.projectName),
        state: "queued",
        summary: "Waiting to run",
      });
      if (!entries.length) detailItems.push({
        key: "maintenance-none", target: "Compute workloads",
        operation: "Maintenance checks", state: "skipped",
        summary: "No eligible checks",
      });
      renderOperationDetails("compute-refresh", detailItems);
      const finished = await Promise.all(
        queuedJobs.map(async (job) => {
          try {
            return await waitForRefreshJob(job.jobId, (current) =>
              updateOperationDetail(
                "compute-refresh", detailItems, job.jobId,
                current.state, current.summary));
          } catch (error) {
            if (!isCurrentSession(generation)) throw error;
            updateOperationDetail(
              "compute-refresh", detailItems, job.jobId, "failed",
              `Couldn't read progress: ${error.message}`);
            return { state: "failed", summary: error.message };
          }
        }));
      for (const provider of providers) PROXMOX_CATALOGUES.delete(provider.deviceId);
      await loadCompute();
      const issues = finished.filter((job) => job.state !== "successful").length +
        entries.filter((entry) => !entry.queued).length +
        providers.filter((provider) => !provider.ok).length +
        Number(inventory.ok === false && !!inventory.error);
      if (issues) {
        toastErr(`Refresh completed with ${issues} issue${issues === 1 ? "" : "s"} needing attention. Open More details to see what failed.`);
      } else if (queuedJobs.length) {
        toastOk("Compute, OS updates, and Docker updates refreshed.");
      } else {
        toastOk("Compute refreshed; no maintenance checks were eligible.");
      }
    } catch (error) {
      if (!isCurrentSession(generation)) return;
      updateOperationDetail(
        "compute-refresh", detailItems, detailItems[0].key, "failed", error.message);
      toastErr(error.message);
    }
    finally { if (isCurrentSession(generation) && progress) progress.hidden = true; }
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

async function managementSection(instance, controller, view) {
  const el = section("Maintenance");
  const mapping = instance.ansible || {};
  const managed = managedByAnsible(instance);
  const status = document.createElement("p"); status.className = "muted compute-management-copy";
  status.textContent = managed
    ? `Managed by Ansible as ${mapping.inventoryHost}.`
    : "Ansible management is off. Confirm an inventory host to enable compatible checks.";
  el.appendChild(status);
  const state = instance.updateState || {};
  if (osMaintenanceCapable(instance)) {
    const summary = document.createElement("div");
    summary.className = "maintenance-summary os-maintenance-summary";
    const summaryCopy = document.createElement("div");
    const summaryTitle = document.createElement("strong");
    summaryTitle.textContent = "Operating system updates";
    const checked = document.createElement("span"); checked.className = "muted";
    checked.textContent = state.lastCheckedAt
      ? `Last checked ${timeAgo(state.lastCheckedAt)}` : "No completed update check yet";
    summaryCopy.append(summaryTitle, checked);
    const tones = { updates_available: "warn", up_to_date: "good", reboot_required: "warn",
      failed: "bad", unreachable: "bad", checking: "warn", updating: "warn",
      successful: "good" };
    summary.append(summaryCopy,
      statusBadge(updateLabel(instance), tones[state.state] || "unknown"));
    el.appendChild(summary);
  }
  if (applianceHealthCapable(instance)) {
    const health = instance.applianceHealthState || {};
    const summary = document.createElement("div");
    summary.className = "maintenance-summary appliance-health-summary";
    const copy = document.createElement("div");
    const title = document.createElement("strong"); title.textContent = "Appliance API health";
    const detail = document.createElement("span"); detail.className = "muted";
    detail.textContent = health.summary || (health.lastCheckedAt
      ? `Last checked ${timeAgo(health.lastCheckedAt)}`
      : "No authenticated API health check yet");
    copy.append(title, detail);
    const presentation = ({ available: ["Available", "good"],
      checking: ["Checking…", "warn"], failed: ["Health check failed", "bad"],
      unreachable: ["Health check failed", "bad"] })[health.state] || ["Unknown", "unknown"];
    summary.append(copy, statusBadge(...presentation)); el.appendChild(summary);
    if (managed && mapping.applianceHealthEligible) {
      const actions = document.createElement("div");
      actions.className = "action-row maintenance-actions appliance-health-actions";
      const check = document.createElement("button"); check.className = "btn btn-ghost btn-sm";
      check.textContent = "Check appliance health";
      check.disabled = health.state === "checking";
      check.onclick = () => runDetailJob(instance, check, "health/check", {});
      actions.appendChild(check); el.appendChild(actions);
    }
  }
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
          await refreshOpen(instance.id, view);
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
  if (managed && osMaintenanceCapable(instance)) {
    const actions = document.createElement("div");
    actions.className = "action-row maintenance-actions maintenance-action-bar";
    const check = document.createElement("button"); check.className = "btn btn-ghost btn-sm";
    check.textContent = "Check OS updates";
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
    const required = [];
    if (osMaintenanceCapable(instance) && !updateCheckEligible(instance)) {
      required.push("OS update check");
    }
    if (dockerMaintenanceCapable(instance) && !dockerDiscoveryEligible(instance)) {
      required.push("Docker discovery");
    }
    if (applianceHealthCapable(instance) && !mapping.applianceHealthEligible) {
      required.push("appliance health check");
    }
    if (required.length) {
      const notice = document.createElement("div"); notice.className = "hint warn maintenance-readiness";
      const copy = document.createElement("span");
      copy.textContent = `Mapping saved. To enable checks, approve ${required.join(" and ")} playbooks in Settings → Ansible.`;
      const settings = document.createElement("button"); settings.className = "btn btn-ghost btn-sm";
      settings.textContent = "Open Ansible settings"; settings.onclick = openAnsibleSettings;
      notice.append(copy, settings); el.appendChild(notice);
    }
  }
  if (osMaintenanceCapable(instance) && ["checking", "updating"].includes(state.state)) {
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
  if (dockerUpdates.state && !(docker?.projects || []).length &&
      dockerUpdates.state !== "not_checked") {
    const status = document.createElement("div"); status.className = "maintenance-summary";
    const copy = document.createElement("div");
    const title = document.createElement("strong"); title.textContent = "Docker updates";
    const detail = document.createElement("span"); detail.className = "muted";
    detail.textContent = dockerUpdates.summary || dockerUpdates.lastErrorSummary ||
      (dockerUpdates.state === "unknown"
      ? "The last check could not determine update availability."
      : "Last approved check result.");
    copy.append(title, detail);
    const value = document.createElement("span"); value.className = "pill";
    const label = ({ updates_available: "Available", up_to_date: "Up to date",
      checking: "Checking…", updating: "Updating…", failed: "Failed",
      unreachable: "Unreachable", incomplete: "Incomplete",
      not_applicable: "Not applicable", read_only: "Read-only",
      check_recommended: "Check recommended", unknown: "Unknown" })[dockerUpdates.state]
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
        [health.failed ? `${health.failed} failed` : "", "bad", ""],
        [health.restarting ? `${health.restarting} restarting` : "", "warn", ""],
        [health.stopped ? `${health.stopped} stopped` : "", "bad", ""],
        [health.starting ? `${health.starting} starting` : "", "warn", ""],
        [health.completed ? `${health.completed} completed` : "", "good", ""],
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
    const approvedProjects = (docker.projects || []).filter(
      (project) => project.approved === true);
    if (managedByAnsible(instance) && approvedProjects.length &&
        !dockerCheckEligible(instance)) {
      const notice = document.createElement("div");
      notice.className = "hint warn docker-check-approval-notice";
      const copy = document.createElement("span");
      copy.textContent = SESSION.role === "admin"
        ? "Docker update checks are unavailable. Approve a Docker update-check playbook " +
          "with the required docker_project variable in Settings → Ansible."
        : "Docker update checks are unavailable because the required Ansible playbook " +
          "has not been approved.";
      notice.appendChild(copy);
      if (SESSION.role === "admin") {
        const settings = document.createElement("button");
        settings.className = "btn btn-ghost btn-sm";
        settings.textContent = "Open Ansible settings";
        settings.onclick = openAnsibleSettings;
        notice.appendChild(settings);
      }
      el.appendChild(notice);
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
        (projectHealth.completed ? ` · ${projectHealth.completed} completed` : "") +
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
      if (project.updateState && project.updateState.state !== "unmanaged") {
        const presentation = dockerProjectUpdateStatus(project);
        const updates = document.createElement("div");
        updates.className = "maintenance-summary project-update-state";
        const copy = document.createElement("div");
        const title = document.createElement("strong"); title.textContent = "Image updates";
        const detail = document.createElement("span"); detail.className = "muted";
        detail.textContent = presentation.detail; copy.append(title, detail);
        const value = document.createElement("span"); value.className = "pill";
        value.textContent = presentation.label; updates.append(copy, value);
        box.appendChild(updates);
        if (["checking", "updating"].includes(presentation.state)) {
          appendMaintenanceProgress(box, presentation.state === "updating"
            ? `Updating ${project.name}…` : `Checking ${project.name}…`);
        }
      }
      const controls = document.createElement("div");
      controls.className = "inline-form compose-project-actions";
      if (managedByAnsible(instance) && project.approved === true &&
          dockerCheckEligible(instance)) {
        const check = document.createElement("button"); check.className = "btn btn-ghost btn-sm";
        check.textContent = "Check updates";
        check.disabled = ["checking", "updating"].includes(project.updateState?.state);
        check.onclick = () => runDetailJob(instance, check, "docker/check", {
          projectName: project.name,
        });
        controls.appendChild(check);
      }
      const currentMode = project.updateMode ||
        ({ local_build: "build", unmanaged: "read_only" })[project.updateStrategy] ||
        project.updateStrategy || "read_only";
      if (SESSION.role === "admin" && project.approved === true &&
          project.managed && currentMode !== "read_only") {
        const update = document.createElement("button"); update.className = "btn btn-ghost btn-sm";
        update.textContent = currentMode === "build" ? "Rebuild & Deploy" : "Update Stack";
        update.disabled = !(instance.ansible?.dockerUpdateModes || []).includes(currentMode) ||
          ["checking", "updating"].includes(project.updateState?.state);
        update.onclick = async () => {
          const method = currentMode === "build" ? "local build and recreate" : "pull and recreate";
          const confirmed = await confirmDialog({ title: `Update “${project.name}”?`, message: `Run its approved ${method} playbook?`, okLabel: "Update Stack", danger: true });
          if (confirmed) runDetailJob(instance, update,
            `docker/projects/${encodeURIComponent(project.name)}/update`, {});
        };
        controls.append(update);
      }
      if (controls.children.length) box.appendChild(controls);
      el.appendChild(box);
    }
    const unmanagedProjects = (docker.projects || []).filter((project) => project.approved === false);
    if (managedByAnsible(instance) && unmanagedProjects.length) {
      const notice = document.createElement("div");
      notice.className = "hint docker-inventory-notice";
      const names = unmanagedProjects.map((project) => project.name).join(", ");
      const inventoryLocation = SESSION.role === "admin" && controller?.inventoryPath
        ? controller.inventoryPath : "the configured Ansible inventory file";
      const copy = document.createElement("span");
      copy.textContent = `HomeLabHQ discovered ${names}, but inventory host ` +
        `${instance.ansible.inventoryHost} does not approve ${unmanagedProjects.length === 1
          ? "this Compose project" : "these Compose projects"}. Edit ${inventoryLocation} ` +
        "on the Ansible controller; HomeLabHQ Settings do not edit inventory contents. " +
        `Add ${unmanagedProjects.length === 1 ? "an entry" : "entries"} under ` +
        "docker_compose_projects with the exact name, Compose path, and update_mode set to " +
        "pull, build, or read-only. Then refresh inventory and containers.";
      notice.appendChild(copy);
      el.appendChild(notice);
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
    const actions = document.createElement("div");
    actions.className = "action-row maintenance-actions docker-section-actions";
    const discover = document.createElement("button"); discover.className = "btn btn-ghost btn-sm";
    discover.textContent = instance.docker
      ? "Refresh inventory & containers" : "Discover inventory & containers";
    discover.setAttribute("aria-label", instance.docker
      ? "Refresh Ansible inventory and Docker containers"
      : "Discover Ansible inventory and Docker containers");
    discover.disabled = !dockerDiscoveryEligible(instance);
    discover.onclick = () => runDetailJob(instance, discover, "docker/discover", {});
    actions.appendChild(discover); el.appendChild(actions);
  }
  return el;
}

function historySection(jobs) {
  const el = document.createElement("details");
  el.className = "detail-section compute-history";
  const trigger = document.createElement("summary");
  const title = document.createElement("span"); title.textContent = "Recent maintenance";
  const count = document.createElement("span"); count.className = "pill";
  count.textContent = `${jobs.length} job${jobs.length === 1 ? "" : "s"}`;
  trigger.append(title, count); el.appendChild(trigger);
  const body = document.createElement("div"); body.className = "compute-history-body";
  if (!jobs.length) {
    const p = document.createElement("p"); p.className = "muted";
    p.textContent = "No maintenance jobs yet."; body.appendChild(p);
    el.appendChild(body); return el;
  }
  for (const job of jobs) {
    const box = document.createElement("details"); box.className = "job-history";
    const summary = document.createElement("summary");
    summary.textContent = `${job.operation.replaceAll("_", " ")}` +
      (job.projectName ? ` · ${job.projectName}` : "") +
      ` · ${job.state} · ${timeAgo(job.createdAt)}`;
    const recap = document.createElement("p"); recap.className = "muted";
    const totals = Object.values(job.recap || {}).reduce((acc, value) => { for (const key of ["ok", "changed", "failed", "unreachable"]) acc[key] += value[key] || 0; return acc; }, { ok: 0, changed: 0, failed: 0, unreachable: 0 });
    recap.textContent = `${job.summary || ""} · changed ${totals.changed} · failed ${totals.failed} · unreachable ${totals.unreachable}`;
    const logs = document.createElement(job.detailsRetained === false ? "p" : "pre");
    logs.textContent = job.detailsRetained === false
      ? "Detailed output was compacted after a newer successful Docker discovery."
      : [job.stdout, job.stderr].filter(Boolean).join("\n");
    if (job.detailsRetained === false) logs.className = "muted";
    box.append(summary, recap, logs); body.appendChild(box);
  }
  el.appendChild(body);
  return el;
}

async function renderDetail(instance, request, view) {
  const body = $("#cm-body"); body.innerHTML = "";
  body.classList.add("compute-detail-body");
  body.appendChild(infoSection(instance));
  let controller = null;
  if (SESSION.role === "admin") {
    try { controller = (await api("/api/settings/ansible", request)).controller; }
    catch (error) {
      if (!request.current()) return;
      const warning = document.createElement("div");
      renderError(warning, "Couldn't load management settings: " + error.message);
      body.appendChild(warning);
    }
  }
  if (!request.current()) return;
  const management = await managementSection(instance, controller, view);
  if (!request.current()) return;
  body.appendChild(management);
  if (dockerMaintenanceCapable(instance) || instance.docker ||
      instance.dockerDiscoveryState || instance.dockerUpdateState) {
    body.appendChild(dockerSection(instance, controller));
  }
  try {
    const { jobs = [] } = await api(`/api/compute/${instance.id}/jobs`, request);
    if (!request.current()) return;
    body.appendChild(historySection(jobs));
  } catch (error) {
    if (!request.current()) return;
    const warning = document.createElement("div");
    renderError(warning, "Couldn't load maintenance history: " + error.message);
    body.appendChild(warning);
  }
}

async function refreshOpen(id, view = computeView) {
  if (!view || view !== computeView || view.id !== id || !view.current()) return;
  const request = detailRequests.begin(view.current);
  const body = $("#cm-body");
  body.setAttribute("aria-busy", "true");
  try {
    const { instance } = await api(`/api/compute/${id}`, request);
    if (!request.current()) return;
    ACTIVE_INSTANCE = instance;
    $("#cm-title").textContent = instance.name; $("#cm-sub").textContent = `${instance.type.toUpperCase()} · ${workloadLocation(instance)}`;
    $("#cm-dot").className = "dot " + (instance.status === "running" ? "up" : instance.status === "stopped" ? "down" : "unknown");
    $("#cm-status-text").textContent = `${instance.status || "unknown"} · `;
    await renderDetail(instance, request, view);
  } catch (error) {
    if (!request.current()) return;
    ACTIVE_INSTANCE = null;
    renderError(body, "Couldn't load details: " + error.message);
  } finally {
    if (request.current()) body.removeAttribute("aria-busy");
  }
}

export function openCompute(instance) {
  if (!SESSION) return;
  const modal = $("#compute-modal"), body = $("#cm-body");
  const reopening = !modal.hidden;
  closeModalChildren(modal);
  ACTIVE_INSTANCE = null;
  modal.hidden = false;
  const hash = `#/compute/${encodeURIComponent(instance.id)}`;
  if (location.hash !== hash) history.pushState({ detailReturn: true }, "", hash);
  const view = detailViews.begin(() => !modal.hidden && modal.isConnected &&
    $("#cm-body") === body && location.hash === hash);
  view.id = instance.id;
  computeView = view;
  $("#cm-title").textContent = instance.name; body.textContent = "Loading…";
  $("#cm-sub").textContent = ""; $("#cm-status-text").textContent = "";
  $("#cm-dot").className = "dot unknown";
  if (!reopening) pushModal(modal, { onEscape: dismissCompute });
  return refreshOpen(instance.id, view);
}

export function closeCompute({ fromRoute = false } = {}) {
  detailViews.invalidate(); detailRequests.invalidate(); computeView = null;
  const modal = $("#compute-modal"); if (modal.hidden) return;
  closeModalChildren(modal);
  $("#cm-body").removeAttribute("aria-busy");
  modal.hidden = true; ACTIVE_INSTANCE = null;
  clearTimeout(pollTimer); pollTimer = null; popModal();
  if (!fromRoute && location.hash.startsWith("#/compute/")) {
    document.dispatchEvent(new CustomEvent("hlhq:navigate", {
      detail: { tab: "compute", replace: true },
    }));
  }
}

function dismissCompute() {
  if (location.hash.startsWith("#/compute/") && history.state?.detailReturn) history.back();
  else closeCompute();
}
$$('[data-close-compute]').forEach(button => button.addEventListener("click", dismissCompute));

async function runDetailJob(instance, button, path, body) {
  const view = computeView;
  let progress = null;
  await withBusy(button, "Starting…", async () => {
    try {
      const { job } = await api(`/api/compute/${instance.id}/${path}`, { method: "POST", body: JSON.stringify(body) });
      toastOk("Maintenance job queued.");
      progress = appendMaintenanceProgress(
        button.closest(".compose-project") || button.closest(".detail-section") || button.parentElement,
        `${job.operation.includes("update") ? "Updating" : "Checking"} ${job.projectName || instance.name}…`);
      pollJob(job.id, async (finished) => {
        progress?.remove();
        if (finished.state === "successful") toastOk(finished.summary);
        else toastErr(finished.summary || "Maintenance did not complete.");
        await loadCompute(); if (view?.current()) await refreshOpen(instance.id, view);
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

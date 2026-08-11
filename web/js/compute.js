// Compute workload cards, filtering, detail, mappings, Docker hierarchy, and jobs.
"use strict";
import { $, $$, api, SESSION, fmtBytes, fmtUptime, timeAgo } from "./api.js";
import { toastErr, toastOk, withBusy, confirmDialog, pushModal, popModal } from "./ui.js";

let INSTANCES = [];
let FILTER = "all";
let PARENT_FILTER = null;
let ACTIVE_INSTANCE = null;
let ANSIBLE_ENABLED = false;
let pollTimer = null;

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

function openAnsibleSettings() {
  closeCompute();
  document.dispatchEvent(new CustomEvent("hlhq:navigate", { detail: { tab: "settings" } }));
}

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
  if (!managedByAnsible(instance)) {
    return ANSIBLE_ENABLED ? "Not managed" : "Set up Ansible";
  }
  if (!docker || docker.available == null) return "Unknown";
  if (!docker.available) return "Unavailable";
  const containers = dockerContainers(docker);
  if (!containers.length) return "Available";
  const health = dockerHealth(containers);
  const running = containers.filter((container) => container.state === "running").length;
  const restarting = containers.filter((container) => container.state === "restarting").length;
  if (health.unhealthy) return `${health.unhealthy} unhealthy`;
  if (health.starting) return `${health.starting} starting`;
  if (running === containers.length && !health.unknown) {
    const details = [health.healthy ? `${health.healthy} healthy` : "",
      health.noHealthcheck ? `${health.noHealthcheck} running` : ""].filter(Boolean).join(" · ");
    return `Healthy${details ? ` · ${details}` : ""}`;
  }
  return `${running} running${restarting ? ` · ${restarting} restarting` : ""}`;
}

function dockerContainers(docker) {
  return [...(docker?.projects || []).flatMap((project) => project.containers || []),
    ...(docker?.containers || [])];
}

function dockerHealth(containers) {
  const result = { healthy: 0, unhealthy: 0, starting: 0, noHealthcheck: 0, unknown: 0 };
  for (const container of containers) {
    if (container.health === "healthy") result.healthy += 1;
    else if (container.health === "unhealthy") result.unhealthy += 1;
    else if (container.health === "starting") result.starting += 1;
    else if (container.health === "no_healthcheck" || container.health === "none" || container.health == null) result.noHealthcheck += 1;
    else result.unknown += 1;
  }
  return result;
}

function projectStatus(project) {
  const containers = project.containers || [];
  if (!containers.length) return project.status || "No containers";
  const health = dockerHealth(containers);
  if (health.unhealthy) return `${health.unhealthy} unhealthy`;
  if (health.starting) return `${health.starting} starting`;
  const running = containers.filter((container) => container.state === "running").length;
  if (running === containers.length && !health.unknown) return "Healthy";
  return `${running}/${containers.length} running`;
}

function appendContainerRow(list, container) {
  const row = document.createElement("div");
  const name = document.createElement("span"); name.textContent = container.name;
  const state = document.createElement("span"); state.className = "container-state";
  const runtime = document.createElement("span");
  runtime.textContent = (container.state || "unknown").replaceAll("_", " ");
  const health = document.createElement("small");
  health.textContent = ({ healthy: "Healthy", unhealthy: "Unhealthy", starting: "Healthcheck starting",
    no_healthcheck: "No healthcheck", none: "No healthcheck" })[container.health] || "Health unknown";
  if (container.health === "healthy") health.className = "sev-good";
  else if (container.health === "unhealthy") health.className = "sev-bad";
  else health.className = "muted";
  state.append(runtime, health); row.append(name, state); list.appendChild(row);
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
  valueRow(stats, "Docker updates", dockerUpdateLabel(instance));
  const last = document.createElement("div"); last.className = "muted updated";
  last.textContent = instance.lastDiscoveredAt ? `discovered ${timeAgo(instance.lastDiscoveredAt)}` : "not discovered";
  const actions = document.createElement("div"); actions.className = "compute-actions";
  const details = document.createElement("button"); details.className = "btn btn-ghost btn-sm";
  details.textContent = "Details"; details.onclick = (event) => { event.stopPropagation(); openCompute(instance); };
  actions.appendChild(details);
  if (managedByAnsible(instance)) {
    const check = document.createElement("button"); check.className = "btn btn-ghost btn-sm";
    check.textContent = "Check Updates";
    check.disabled = !updateCheckEligible(instance);
    check.onclick = (event) => { event.stopPropagation(); startCardJob(instance, check, "updates/check", "Checking…"); };
    actions.appendChild(check);
    if (SESSION.role === "admin") {
      const update = document.createElement("button"); update.className = "btn btn-primary btn-sm";
      update.textContent = "Update";
      update.disabled = !osUpdateEligible(instance);
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
  if (managedByAnsible(instance)) {
    const discover = document.createElement("button"); discover.className = "btn btn-ghost btn-sm";
    discover.textContent = instance.docker ? "Refresh Docker" : "Discover";
    discover.setAttribute("aria-label", instance.docker ? "Refresh Docker" : "Discover Docker");
    discover.disabled = !dockerDiscoveryEligible(instance);
    discover.onclick = (event) => {
      event.stopPropagation();
      startCardJob(instance, discover, "docker/discover", "Starting…");
    };
    actions.appendChild(discover);
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
  $("#compute-ansible-setup").hidden = SESSION.role !== "admin" || ANSIBLE_ENABLED || !INSTANCES.length;
  const empty = $("#compute-empty"); empty.hidden = !!visible.length;
  $(".compute-empty-title", empty).textContent = INSTANCES.length ? "No matching workloads." : "No compute workloads discovered.";
  $(".compute-empty-sub", empty).textContent = INSTANCES.length
    ? "Choose another filter." : "Add a Proxmox Device, then refresh Compute.";
}

export async function loadCompute() {
  try {
    const response = await api("/api/compute");
    INSTANCES = response.instances || [];
    ANSIBLE_ENABLED = !!response.ansibleEnabled;
    render();
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
  return el;
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
      for (const value of [`${allContainers.length} container${allContainers.length === 1 ? "" : "s"}`,
        health.healthy ? `${health.healthy} healthy` : "",
        health.unhealthy ? `${health.unhealthy} unhealthy` : "",
        health.starting ? `${health.starting} starting` : "",
        health.noHealthcheck ? `${health.noHealthcheck} without healthcheck` : ""].filter(Boolean)) {
        const item = document.createElement("span"); item.className = "pill"; item.textContent = value;
        overview.appendChild(item);
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
      const head = document.createElement("div"); head.className = "card-row";
      const title = document.createElement("strong"); title.textContent = project.name;
      const status = document.createElement("span"); status.className = "pill";
      status.textContent = projectStatus(project);
      head.append(title, status); box.appendChild(head);
      const projectSummary = document.createElement("p"); projectSummary.className = "muted";
      projectSummary.textContent = `${(project.containers || []).length} container${(project.containers || []).length === 1 ? "" : "s"}` +
        (project.status ? ` · ${project.status}` : ""); box.appendChild(projectSummary);
      if ((project.configFiles || []).length) {
        const paths = document.createElement("p"); paths.className = "muted";
        paths.textContent = `Compose config: ${project.configFiles.join(", ")}`; box.appendChild(paths);
      }
      const list = document.createElement("div"); list.className = "container-list";
      for (const container of project.containers || []) appendContainerRow(list, container);
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
      if (SESSION.role === "admin") {
        const controls = document.createElement("div"); controls.className = "inline-form";
        const strategy = document.createElement("select");
        const currentMode = project.updateMode || ({ local_build: "build", unmanaged: "read_only" })[project.updateStrategy] || project.updateStrategy || "read_only";
        for (const [value, label] of [["read_only", "Read-only"], ["pull", "Pull and recreate"], ["build", "Local build and recreate"]]) {
          const option = document.createElement("option"); option.value = value; option.textContent = label; option.selected = currentMode === value; strategy.appendChild(option);
        }
        strategy.onchange = async () => {
          try {
            const saved = (await api(`/api/compute/${instance.id}/docker/projects/${project.id}/strategy`, { method: "POST", body: JSON.stringify({ mode: strategy.value }) })).project;
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
          if (confirmed) runDetailJob(instance, update, `docker/projects/${project.id}/update`, {});
        };
        controls.append(strategy, update); box.appendChild(controls);
      }
      el.appendChild(box);
    }
    if ((docker.containers || []).length) {
      const heading = document.createElement("h4"); heading.className = "docker-subheading";
      heading.textContent = "Other containers"; el.appendChild(heading);
      const list = document.createElement("div"); list.className = "container-list direct-containers";
      for (const container of docker.containers) appendContainerRow(list, container);
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
    const check = document.createElement("button"); check.className = "btn btn-ghost btn-sm"; check.textContent = "Check Docker Updates";
    check.disabled = !dockerCheckEligible(instance);
    check.onclick = () => runDetailJob(instance, check, "docker/check", {});
    actions.append(discover, check); el.appendChild(actions);
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
        if (finished.state === "successful") toastOk(finished.summary);
        else toastErr(finished.summary || "Maintenance did not complete.");
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

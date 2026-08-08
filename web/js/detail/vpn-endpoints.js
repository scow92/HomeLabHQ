// OPNsense NordVPN endpoint manager. Owns only its fetched snapshot and local
// disclosure/dialog state; the parent detail module supplies device identity.
"use strict";
import { api, timeAgo } from "../api.js";
import { seriesChartCard } from "../charts.js";
import { confirmDialog, detailSection, openOverlay, toastErr, toastOk, withBusy } from "../ui.js";

const VALIDATION_STATES = ["Verified", "Failed", "Assumed", "Unknown"];

function node(tag, className = "", text = "") {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (text) el.textContent = text;
  return el;
}

function object(value) { return value && typeof value === "object" ? value : {}; }
function list(value) { return Array.isArray(value) ? value.filter((item) => item && typeof item === "object") : []; }
function text(value) { return typeof value === "string" ? value.trim() : ""; }

function validTimestamp(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric) || numeric <= 0) return null;
  const date = new Date(numeric < 1e12 ? numeric * 1000 : numeric);
  return Number.isNaN(date.getTime()) ? null : date;
}

function exactDate(value) {
  const date = validTimestamp(value);
  return date ? date.toLocaleString() : "";
}

function utilizationPoints(value) {
  const raw = object(value).history;
  if (!Array.isArray(raw)) return [];
  return raw.map((point) => {
    if (!Array.isArray(point) || point.length < 2) return null;
    const timestamp = Number(point[0]);
    const percent = Number(point[1]);
    return Number.isFinite(timestamp) && timestamp > 0 && Number.isFinite(percent)
      && percent >= 0 && percent <= 100 ? [timestamp, percent] : null;
  }).filter(Boolean);
}

function endpointText(value) {
  const item = object(value);
  const address = text(item.endpointIp);
  if (!address) return "";
  const port = Number(item.endpointPort);
  return Number.isInteger(port) && port > 0 ? `${address}:${port}` : address;
}

function ownerText(value) {
  const item = object(value);
  const owner = text(item.organisation) || text(item.owner) || text(item.asnName);
  const asn = text(item.asn) || (Number.isInteger(item.asn) ? String(item.asn) : "");
  return [owner, asn ? `AS${asn.replace(/^AS/i, "")}` : ""].filter(Boolean).join(" · ");
}

function validationSummary(targets) {
  const values = list(targets);
  if (!values.length) return "";
  const counts = new Map();
  for (const target of values) {
    const state = VALIDATION_STATES.includes(target.state) ? target.state : "Unknown";
    counts.set(state, (counts.get(state) || 0) + 1);
  }
  return VALIDATION_STATES.filter((state) => counts.has(state))
    .map((state) => `${counts.get(state)} ${state.toLowerCase()}`).join(" · ");
}

function pill(label, kind = "") {
  const el = node("span", `vpn-pill${kind ? ` ${kind}` : ""}`, label);
  return el;
}

function technicalError(parent, summary, detail) {
  const box = node("div", "vpn-message error");
  box.appendChild(node("p", "", summary));
  if (text(detail)) {
    const disclosure = document.createElement("details");
    disclosure.appendChild(node("summary", "", "Technical details"));
    disclosure.appendChild(node("pre", "vpn-technical", text(detail)));
    box.appendChild(disclosure);
  }
  parent.appendChild(box);
}

function formField(label, value = "", type = "text", options = {}) {
  const wrap = node("label", "field");
  wrap.appendChild(node("span", "", label));
  const input = document.createElement(options.multiline ? "textarea" : "input");
  if (!options.multiline) input.type = type;
  input.value = value == null ? "" : String(value);
  if (options.min != null) input.min = String(options.min);
  if (options.max != null) input.max = String(options.max);
  if (options.required) input.required = true;
  if (options.placeholder) input.placeholder = options.placeholder;
  wrap.appendChild(input);
  if (options.help) wrap.appendChild(node("small", "muted", options.help));
  return [wrap, input];
}

function checkboxField(label, checked) {
  const wrap = node("label", "ent-item vpn-check");
  const input = document.createElement("input");
  input.type = "checkbox";
  input.checked = !!checked;
  wrap.append(input, node("span", "", label));
  return [wrap, input];
}

function group(title) {
  const fieldset = node("fieldset", "vpn-settings-group");
  fieldset.appendChild(node("legend", "", title));
  return fieldset;
}

export function vpnEndpointsSection(dm) {
  const section = detailSection("VPN Endpoint");
  section.classList.add("vpn-endpoint-section");
  const content = node("div", "vpn-endpoint-content");
  section.appendChild(content);

  const baseEndpoint = `/api/devices/${dm.device.id}/vpn-endpoints`;
  let snapshot = null;
  let snapshots = [];
  let activeProfileId = "";
  let loadError = "";
  let candidateOpen = false;
  let showAll = false;
  let discoveryLoading = false;
  let settingsOpening = false;
  let settingsOverlay = null;
  let validationOverlay = null;
  let historyOverlay = null;

  function profileEndpoint() {
    const profileId = text(object(object(snapshot).profile).id);
    return profileId ? `${baseEndpoint}/${encodeURIComponent(profileId)}` : baseEndpoint;
  }

  function updateSnapshot(value) {
    snapshot = object(value);
    const profileId = text(object(snapshot.profile).id);
    if (!profileId) return;
    activeProfileId = profileId;
    const index = snapshots.findIndex((item) => text(object(item.profile).id) === profileId);
    if (index >= 0) snapshots[index] = snapshot;
    else snapshots.push(snapshot);
  }

  function profileNavigation() {
    const nav = node("div", "vpn-profile-navigation");
    const tabs = node("div", "vpn-profile-tabs");
    tabs.setAttribute("role", "tablist");
    tabs.setAttribute("aria-label", "Managed VPN endpoints");
    for (const item of snapshots) {
      const profile = object(item.profile);
      const profileId = text(profile.id);
      if (!profileId) continue;
      const label = text(profile.name) || text(profile.country) || "VPN endpoint";
      const tab = node("button", "btn btn-sm btn-ghost vpn-profile-tab", label);
      tab.type = "button";
      tab.setAttribute("role", "tab");
      tab.setAttribute("aria-selected", String(profileId === activeProfileId));
      tab.tabIndex = profileId === activeProfileId ? 0 : -1;
      tab.onclick = (event) => {
        activeProfileId = profileId;
        snapshot = item;
        candidateOpen = false;
        showAll = false;
        render();
        if (event.detail === 0) {
          const selected = content.querySelector('[role="tab"][aria-selected="true"]');
          if (selected) selected.focus();
        }
      };
      tabs.appendChild(tab);
    }
    const tabButtons = [...tabs.querySelectorAll('[role="tab"]')];
    for (const [index, tab] of tabButtons.entries()) {
      tab.onkeydown = (event) => {
        if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
        event.preventDefault();
        let next = index;
        if (event.key === 'ArrowLeft') next = (index - 1 + tabButtons.length) % tabButtons.length;
        if (event.key === 'ArrowRight') next = (index + 1) % tabButtons.length;
        if (event.key === 'Home') next = 0;
        if (event.key === 'End') next = tabButtons.length - 1;
        tabButtons[next].focus();
        tabButtons[next].click();
      };
    }
    const add = node("button", "btn btn-sm btn-ghost vpn-profile-add", "+");
    add.type = "button";
    add.setAttribute("aria-label", "Add VPN endpoint");
    add.title = "Add VPN endpoint";
    add.onclick = () => openSettings(true);
    nav.append(tabs, add);
    return nav;
  }

  function currentDiagnostics(current, discovery) {
    const details = document.createElement("details");
    details.className = "vpn-details";
    details.appendChild(node("summary", "", "Details"));
    const values = [];
    const runtime = object(current.status);
    const add = (label, value) => { if (value !== "" && value != null) values.push([label, String(value)]); };
    add("Server ID", Number.isInteger(current.serverId) ? current.serverId : text(current.serverId));
    add("Associated gateway", current.gateway ? ownerText(current.gateway) || text(current.gateway.name) : "");
    add("Latest handshake", exactDate(runtime.latestHandshake));
    add("Handshake age", Number.isFinite(Number(runtime.handshakeAge)) ? `${runtime.handshakeAge} seconds` : "");
    const endpointState = current.runtimeClassification === "Stale"
      ? "Not in latest discovery"
      : text(current.runtimeClassification) || (current.appearsInDiscovery ? "Active" : "");
    add("Endpoint state", endpointState);
    add("Discovery timestamp", exactDate(discovery.discoveredAt || discovery.at));
    add("Candidate ID", text(current.candidateId));
    if (current.gateway && text(current.gateway.status)) add("Gateway status", text(current.gateway.status));
    const dl = node("dl", "vpn-diagnostics");
    for (const [label, value] of values) {
      dl.append(node("dt", "", label), node("dd", "", value));
    }
    details.appendChild(dl);
    return details;
  }

  function currentCard(profile, current, discovery) {
    const card = node("article", "vpn-current-card");
    const badges = node("div", "vpn-pills");
    const health = ["Healthy", "Warning", "Offline", "Unknown"].includes(current.health)
      ? current.health : "Unknown";
    badges.appendChild(pill(health, health.toLowerCase()));
    card.appendChild(badges);

    if (text(current.hostname)) card.appendChild(node("h4", "vpn-hostname", text(current.hostname)));
    const address = endpointText(current);
    if (address) card.appendChild(node("div", "vpn-address", address));
    const owner = ownerText(current);
    if (owner) card.appendChild(node("div", "vpn-owner muted", owner));

    const runtime = object(current.status);
    let handshake = "No authenticated handshake";
    if (validTimestamp(runtime.latestHandshake)) handshake = `Handshake ${timeAgo(runtime.latestHandshake)}`;
    card.appendChild(node("p", "vpn-handshake", handshake));
    const utilization = object(current.utilization);
    const utilizationPercent = Number(utilization.percent);
    if (Number.isFinite(utilizationPercent) && utilizationPercent >= 0 && utilizationPercent <= 100) {
      const points = utilizationPoints(utilization);
      const chart = seriesChartCard(
        { name: "Server utilization", unit: "%" },
        points.length ? points : [[Number(utilization.observedAt), utilizationPercent]]);
      chart.classList.add("vpn-utilization-chart");
      const observed = validTimestamp(utilization.observedAt);
      const source = text(utilization.source) || "Provider";
      const note = node("p", "muted vpn-utilization-note",
        observed ? `${source} observation ${timeAgo(utilization.observedAt)}` : `${source} observation`);
      chart.appendChild(note);
      card.appendChild(chart);
    }
    const checks = validationSummary(current.compatibilityTargets);
    if (checks) card.appendChild(node("p", "vpn-validation-summary", checks));
    if (text(current.error)) technicalError(
      card, "Live WireGuard status could not be read.", current.error);
    card.appendChild(currentDiagnostics(current, discovery));
    return card;
  }

  function candidateCard(candidate, allowSwitch = true) {
    const card = node("article", "vpn-candidate-card");
    const body = node("div", "vpn-candidate-body");
    const title = text(candidate.hostname) || endpointText(candidate) || "Candidate endpoint";
    body.appendChild(node("h5", "", title));
    const metadata = [];
    if (text(candidate.city)) metadata.push(text(candidate.city));
    if (Number.isFinite(Number(candidate.load))) metadata.push(`${candidate.load}% load`);
    const classification = ["Preferred", "Eligible", "Excluded", "Unknown"].includes(candidate.classification)
      ? candidate.classification : "Unknown";
    if (classification !== "Eligible") metadata.push(classification);
    if (metadata.length) body.appendChild(node("div", "vpn-candidate-meta", metadata.join(" · ")));
    const owner = ownerText(candidate);
    if (owner) body.appendChild(node("div", "muted", owner));
    const checks = validationSummary(candidate.compatibilityTargets);
    if (checks) body.appendChild(node("div", "vpn-validation-summary", checks));
    const address = endpointText(candidate);
    if (address && address !== title) body.appendChild(node("div", "vpn-address secondary", address));

    const actions = node("div", "vpn-candidate-actions");
    if (list(candidate.compatibilityTargets).length) {
      const checksButton = node("button", "btn btn-sm btn-ghost", "View checks");
      checksButton.type = "button";
      checksButton.onclick = () => openValidation(candidate);
      actions.appendChild(checksButton);
    }
    if (allowSwitch && !candidate.active && ["Preferred", "Eligible"].includes(classification)) {
      const use = node("button", "btn btn-sm btn-primary", "Use");
      use.type = "button";
      use.onclick = () => switchEndpoint(candidate, use);
      actions.appendChild(use);
    }
    card.append(body, actions);
    return card;
  }

  function candidatePanel(discovery) {
    const panel = node("section", "vpn-candidates");
    const profileId = text(object(object(snapshot).profile).id) || "new";
    const headingId = `vpn-candidates-${dm.device.id}-${profileId}`;
    panel.setAttribute("aria-labelledby", headingId);
    panel.appendChild(node("h4", "", "Replacement candidates"));
    panel.lastChild.id = headingId;
    if (discoveryLoading) {
      const loading = node("p", "vpn-message", "Discovering candidates…");
      loading.setAttribute("role", "status");
      panel.appendChild(loading);
      return panel;
    }
    if (discovery.status === "error") {
      technicalError(panel, "NordVPN candidate discovery is temporarily unavailable.", discovery.error);
    }
    const candidates = list(discovery.candidates).filter((candidate) => !candidate.active);
    const selectable = candidates.filter(
      (candidate) => ["Preferred", "Eligible"].includes(candidate.classification));
    if (!selectable.length) {
      panel.appendChild(node("p", "vpn-message", "No replacement candidates are available."));
    } else {
      const visible = showAll ? selectable : selectable.slice(0, 3);
      const cards = node("div", "vpn-candidate-list");
      for (const candidate of visible) cards.appendChild(candidateCard(candidate));
      panel.appendChild(cards);
      if (!showAll && selectable.length > 3) {
        const all = node("button", "btn btn-sm btn-ghost", "Show all candidates");
        all.type = "button";
        all.onclick = () => { showAll = true; render(); };
        panel.appendChild(all);
      }
    }
    const otherCandidates = candidates.filter(
      (candidate) => !["Preferred", "Eligible"].includes(candidate.classification));
    if (otherCandidates.length) {
      const other = document.createElement("details");
      other.className = "vpn-other-candidates";
      other.appendChild(node("summary", "", `Other candidates (${otherCandidates.length})`));
      const cards = node("div", "vpn-candidate-list");
      for (const candidate of otherCandidates) cards.appendChild(candidateCard(candidate));
      other.appendChild(cards);
      panel.appendChild(other);
    }
    const unknownOwners = candidates.filter((candidate) => candidate.lookupStatus === "unknown").length;
    if (unknownOwners) {
      const note = document.createElement("details");
      note.className = "vpn-rdap-note";
      note.appendChild(node("summary", "",
        `Ownership unavailable for ${unknownOwners} candidate${unknownOwners === 1 ? "" : "s"}`));
      note.appendChild(node("p", "muted",
        "The registry did not return ownership details. These endpoints remain available as Unknown."));
      panel.appendChild(note);
    }
    return panel;
  }

  function actions(profile) {
    const wrap = node("div", "vpn-actions-wrap");
    const row = node("div", "vpn-primary-actions");
    const find = node("button", "btn btn-sm btn-primary", "Find replacement");
    find.type = "button";
    find.disabled = !profile.enabled;
    find.onclick = async () => {
      candidateOpen = true;
      showAll = false;
      render();
      await refreshCandidates();
    };
    const more = node("button", "btn btn-sm btn-ghost", "More");
    more.type = "button";
    more.setAttribute("aria-expanded", "false");
    const actionMenuId = `vpn-endpoint-action-menu-${text(profile.id) || "new"}`;
    more.setAttribute("aria-controls", actionMenuId);
    const menu = node("div", "vpn-action-menu");
    menu.id = actionMenuId;
    menu.hidden = true;
    const closeMenu = () => {
      menu.hidden = true;
      more.setAttribute("aria-expanded", "false");
    };
    more.onclick = () => {
      const opening = menu.hidden;
      menu.hidden = !opening;
      more.setAttribute("aria-expanded", String(opening));
    };
    const refresh = node("button", "btn btn-sm btn-ghost", "Refresh from OPNsense");
    refresh.type = "button";
    refresh.onclick = () => { closeMenu(); more.focus(); syncCurrent(); };
    const settings = node("button", "btn btn-sm btn-ghost", "Settings");
    settings.type = "button";
    settings.onclick = () => { closeMenu(); more.focus(); openSettings(); };
    const history = node("button", "btn btn-sm btn-ghost", "View history");
    history.type = "button";
    history.onclick = () => { closeMenu(); more.focus(); openHistory(); };
    menu.append(refresh, settings, history);
    row.append(find, more);
    wrap.append(row, menu);
    return wrap;
  }

  function render() {
    content.replaceChildren();
    content.appendChild(profileNavigation());
    if (!snapshot && !loadError) {
      const loading = node("p", "vpn-message", "Loading current endpoint…");
      loading.setAttribute("role", "status");
      content.appendChild(loading);
      return;
    }
    if (loadError) {
      technicalError(content, "VPN endpoint status is temporarily unavailable.", loadError);
      const settings = node("button", "btn btn-sm btn-ghost", "Settings");
      settings.type = "button";
      settings.onclick = () => openSettings();
      content.appendChild(settings);
      return;
    }
    const state = object(snapshot);
    const profile = object(state.profile);
    const current = object(state.current);
    const discovery = object(state.discovery);
    if (!state.profileConfigured) {
      const empty = node("div", "vpn-message");
      empty.append(node("h4", "", "No VPN endpoint profile configured"),
        node("p", "muted", "Choose an existing OPNsense WireGuard instance and peer to begin."));
      const settings = node("button", "btn btn-sm btn-primary", "Open settings");
      settings.type = "button";
      settings.onclick = () => openSettings(true);
      empty.appendChild(settings);
      content.appendChild(empty);
      return;
    }
    if (!profile.enabled) {
      content.appendChild(node("p", "vpn-message", "Endpoint management is disabled."));
    }
    if (!current.configured) {
      content.appendChild(node("p", "vpn-message", "No current endpoint is selected."));
    } else {
      content.appendChild(currentCard(profile, current, discovery));
    }
    content.appendChild(actions(profile));
    if (candidateOpen) content.appendChild(candidatePanel(discovery));
  }

  async function load(force = false) {
    try {
      const result = object(await api(
        baseEndpoint + (force ? "?refresh=1" : ""), { timeoutMs: 30000 }));
      const received = list(result.profiles);
      snapshots = received.length ? received : (result.profileConfigured ? [result] : []);
      const selected = snapshots.find(
        (item) => text(object(item.profile).id) === activeProfileId) || snapshots[0];
      snapshot = selected || result;
      activeProfileId = text(object(object(snapshot).profile).id);
      loadError = "";
    } catch (error) {
      loadError = error.message || "Request failed";
    }
    render();
  }

  async function refreshCandidates() {
    if (discoveryLoading) return;
    discoveryLoading = true;
    render();
    try {
      const result = await api(profileEndpoint() + "?refresh=1", { timeoutMs: 30000 });
      updateSnapshot(result);
      loadError = "";
    } catch (error) {
      loadError = "";
      snapshot = snapshot || {};
      snapshot.discovery = { ...object(snapshot.discovery), status: "error", error: error.message };
      toastErr("Candidate discovery could not be refreshed.");
    } finally {
      discoveryLoading = false;
      render();
    }
  }

  async function syncCurrent() {
    try {
      updateSnapshot(await api(profileEndpoint(), { timeoutMs: 30000 }));
      loadError = "";
      render();
      toastOk("Current endpoint refreshed from OPNsense.");
    } catch (error) {
      toastErr(error.message || "Current endpoint could not be refreshed from OPNsense.");
    }
  }

  async function switchEndpoint(candidate, button) {
    const current = object(object(snapshot).current);
    const currentOwner = ownerText(current) || "Ownership unknown";
    const replacementOwner = ownerText(candidate) || "Ownership unknown";
    const replacementMeta = [text(candidate.city),
      Number.isFinite(Number(candidate.load)) ? `${candidate.load}% load` : "",
      text(candidate.classification)].filter(Boolean).join(" · ");
    const message = [
      "Current", currentOwner, endpointText(current) || "No endpoint configured", "",
      "Replacement", replacementOwner, endpointText(candidate), replacementMeta, "",
      "HomeLabHQ will apply the replacement endpoint, regenerate its WireGuard configuration, restart the selected instance, wait for an authenticated handshake and restore the previous configuration automatically if verification fails. Restarting briefly interrupts that tunnel.",
    ].filter((value, index, values) => value !== "" || values[index - 1] !== "").join("\n");
    const confirmed = await confirmDialog({
      title: "Change VPN endpoint?", message, okLabel: "Apply and verify", danger: false,
    });
    if (!confirmed) return;
    await withBusy(button, "Applying…", async () => {
      try {
        const result = await api(profileEndpoint() + "/switch", {
          method: "POST", timeoutMs: 45000,
          body: JSON.stringify({ candidateId: candidate.candidateId, confirmed: true }),
        });
        if (result.ok) {
          candidateOpen = false;
          showAll = false;
          toastOk(result.message || "Endpoint applied and verified.");
        } else if (result.rollback === null || result.rollback === undefined) {
          toastErr(result.message || "Endpoint change was not applied.");
        } else if (result.rollback) {
          toastErr(result.message
            || "Endpoint verification failed; the previous configuration was restored.");
        } else {
          toastErr(result.message
            || "Endpoint verification and rollback failed. Use OPNsense for manual recovery.");
        }
        await load();
      } catch (error) {
        toastErr(error.message || "Endpoint change failed.");
      }
    });
  }

  function openValidation(candidate) {
    if (validationOverlay && validationOverlay.isConnected) return;
    const targets = list(candidate.compatibilityTargets);
    if (!targets.length) return;
    const modal = openOverlay({ title: "Update validation" });
    validationOverlay = modal.overlay;
    modal.overlay.classList.add("vpn-dialog");
    const form = node("form", "vpn-validation-form");
    const targetLabel = node("label", "field");
    targetLabel.appendChild(node("span", "", "Validation target"));
    const targetSelect = document.createElement("select");
    for (const target of targets) targetSelect.appendChild(new Option(text(target.name), target.id));
    targetLabel.appendChild(targetSelect);
    const stateLabel = node("label", "field");
    stateLabel.appendChild(node("span", "", "State"));
    const stateSelect = document.createElement("select");
    for (const state of VALIDATION_STATES) stateSelect.appendChild(new Option(state, state));
    stateLabel.appendChild(stateSelect);
    const [noteWrap, noteInput] = formField("Optional note", "", "text", { multiline: true });
    const timestamp = node("p", "muted vpn-validation-time");
    const actions = node("div", "dialog-actions");
    const cancel = node("button", "btn btn-ghost", "Cancel");
    cancel.type = "button";
    cancel.onclick = modal.close;
    const save = node("button", "btn btn-primary", "Save validation");
    save.type = "submit";
    actions.append(cancel, save);
    form.append(targetLabel, stateLabel, noteWrap, timestamp, actions);
    modal.body.appendChild(form);
    const selectTarget = () => {
      const selected = targets.find((target) => target.id === targetSelect.value) || targets[0];
      stateSelect.value = VALIDATION_STATES.includes(selected.state) ? selected.state : "Unknown";
      noteInput.value = text(selected.note);
      const when = exactDate(selected.lastValidatedAt);
      timestamp.textContent = when ? `Last updated ${when}` : "Not yet validated";
    };
    targetSelect.onchange = selectTarget;
    selectTarget();
    form.onsubmit = async (event) => {
      event.preventDefault();
      await withBusy(save, "Saving…", async () => {
        try {
          await api(profileEndpoint() + "/compatibility", {
            method: "POST",
            body: JSON.stringify({ candidateId: candidate.candidateId,
              targetId: targetSelect.value, state: stateSelect.value, note: noteInput.value }),
          });
          modal.close();
          toastOk("Validation saved.");
          await load();
        } catch (error) { toastErr(error.message); }
      });
    };
  }

  async function openSettings(create = false) {
    if (settingsOpening || (settingsOverlay && settingsOverlay.isConnected)) return;
    settingsOpening = true;
    let choices;
    try {
      choices = object(await api(baseEndpoint + "/choices"));
    } catch (error) {
      toastErr(error.message || "WireGuard choices could not be loaded.");
      settingsOpening = false;
      return;
    }
    settingsOpening = false;
    const profile = create ? {} : object(object(snapshot).profile);
    const modal = openOverlay({ title: create ? "Add VPN endpoint" : "VPN endpoint settings" });
    settingsOverlay = modal.overlay;
    modal.overlay.classList.add("vpn-dialog", "vpn-settings-dialog");
    const form = node("form", "vpn-settings-form");

    const tunnel = group("Tunnel");
    const [nameWrap, profileName] = formField(
      "Profile name", profile.name || "VPN endpoint", "text", { required: true });
    const [enabledWrap, enabled] = checkboxField("Enable endpoint management", profile.enabled);
    const selectField = (label, values, selected) => {
      const wrap = node("label", "field");
      wrap.appendChild(node("span", "", label));
      const select = document.createElement("select");
      select.appendChild(new Option("Select…", ""));
      for (const item of list(values)) {
        const value = text(item.uuid);
        if (value) select.appendChild(new Option(text(item.name) || value, value));
      }
      select.value = text(selected);
      wrap.appendChild(select);
      return [wrap, select];
    };
    const [instanceWrap, instance] = selectField("WireGuard instance", choices.instances, profile.instanceUuid);
    const [peerWrap, peer] = selectField("WireGuard peer", choices.peers, profile.peerUuid);
    const [gatewayWrap, gateway] = formField("Gateway association", profile.gatewayUuid, "text",
      { help: "Optional OPNsense gateway UUID when the endpoint address is used by that gateway." });
    tunnel.append(nameWrap, enabledWrap, instanceWrap, peerWrap, gatewayWrap);
    tunnel.appendChild(node("p", "muted", "Each manager must use a different WireGuard peer."));

    const discovery = group("Discovery");
    const locations = list(choices.locations);
    const locationSelect = (label) => {
      const wrap = node("label", "field");
      wrap.appendChild(node("span", "", label));
      const select = document.createElement("select");
      wrap.appendChild(select);
      return [wrap, select];
    };
    const [countryWrap, country] = locationSelect("Country");
    const [cityWrap, city] = locationSelect("City");
    const selectedCountry = text(profile.country) || "United Kingdom";
    const renderCities = (selected = "") => {
      city.replaceChildren(new Option("Any city", ""));
      const location = locations.find((item) => text(item.name) === country.value);
      for (const item of list(object(location).cities)) {
        const name = text(item.name);
        if (name) city.appendChild(new Option(name, name));
      }
      if (selected && ![...city.options].some((option) => option.value === selected)) {
        city.appendChild(new Option(selected, selected));
      }
      city.value = selected;
    };
    for (const item of locations) {
      const name = text(item.name);
      if (name) country.appendChild(new Option(name, name));
    }
    if (selectedCountry && ![...country.options].some((option) => option.value === selectedCountry)) {
      country.appendChild(new Option(selectedCountry, selectedCountry));
    }
    country.value = selectedCountry;
    country.required = true;
    renderCities(text(profile.city));
    country.onchange = () => renderCities();
    const [limitWrap, limit] = formField("Maximum candidates", profile.maxCandidates ?? 20,
      "number", { min: 1, max: 50 });
    const [intervalWrap, interval] = formField("Discovery interval (seconds)",
      profile.discoveryIntervalSeconds ?? 3600, "number", { min: 300, max: 604800 });
    discovery.append(countryWrap, cityWrap, limitWrap, intervalWrap);
    if (text(choices.locationsError)) {
      discovery.appendChild(node("p", "muted",
        "NordVPN locations could not be refreshed. Saved location values remain available."));
    }

    const monitoring = group("Monitoring");
    const [warningWrap, warning] = formField("Handshake warning threshold (seconds)",
      profile.handshakeWarningSeconds ?? 300, "number", { min: 60, max: 86400 });
    monitoring.appendChild(warningWrap);

    const notesGroup = group("Notes");
    const [notesWrap, notes] = formField("Profile notes", profile.notes || "", "text", { multiline: true });
    notesGroup.appendChild(notesWrap);

    const formActions = node("div", "dialog-actions");
    const cancel = node("button", "btn btn-ghost", "Cancel");
    cancel.type = "button";
    cancel.onclick = modal.close;
    const save = node("button", "btn btn-primary", "Save settings");
    save.type = "submit";
    if (!create) {
      const remove = node("button", "btn btn-ghost", "Remove manager");
      remove.type = "button";
      remove.onclick = async () => {
        const confirmed = await confirmDialog({
          title: `Remove “${text(profile.name) || "VPN endpoint"}”?`,
          message: "This removes HomeLabHQ’s profile, candidates and validation history. It does not change the WireGuard peer in OPNsense.",
          okLabel: "Remove manager", danger: false,
        });
        if (!confirmed) return;
        try {
          await api(profileEndpoint(), {
            method: "DELETE", body: JSON.stringify({ confirmed: true }),
          });
          modal.close();
          activeProfileId = "";
          toastOk("VPN endpoint manager removed. OPNsense was not changed.");
          await load();
        } catch (error) { toastErr(error.message); }
      };
      formActions.appendChild(remove);
    }
    formActions.append(cancel, save);
    form.append(tunnel, discovery, monitoring, notesGroup, formActions);
    modal.body.appendChild(form);
    form.onsubmit = async (event) => {
      event.preventDefault();
      await withBusy(save, "Saving…", async () => {
        try {
          const payload = {
            name: profileName.value.trim(),
            enabled: enabled.checked,
            peerUuid: peer.value,
            instanceUuid: instance.value,
            gatewayUuid: gateway.value,
            country: country.value,
            city: city.value,
            maxCandidates: Number(limit.value),
            discoveryIntervalSeconds: Number(interval.value),
            handshakeWarningSeconds: Number(warning.value),
            notes: notes.value,
          };
          const result = await api(create ? baseEndpoint : profileEndpoint(), {
            method: create ? "POST" : "PATCH", body: JSON.stringify(payload),
          });
          const savedProfile = object(result).profile || payload;
          activeProfileId = text(savedProfile.id);
          modal.close();
          toastOk(create ? "VPN endpoint manager added." : "VPN endpoint settings saved.");
          await load();
        } catch (error) { toastErr(error.message); }
      });
    };
  }

  function openHistory() {
    if (historyOverlay && historyOverlay.isConnected) return;
    const modal = openOverlay({ title: "VPN endpoint history" });
    historyOverlay = modal.overlay;
    modal.overlay.classList.add("vpn-dialog");
    const history = list(object(snapshot).history);
    if (!history.length) {
      modal.body.appendChild(node("p", "vpn-message", "No endpoint history has been recorded."));
      return;
    }
    const cards = node("div", "vpn-candidate-list");
    for (const candidate of history) cards.appendChild(candidateCard(candidate, false));
    modal.body.appendChild(cards);
  }

  render();
  load();
  return section;
}

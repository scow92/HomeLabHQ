// OPNsense NordVPN endpoint manager. Owns only its fetched snapshot and local
// disclosure/dialog state; the parent detail module supplies device identity.
"use strict";
import { api, timeAgo } from "../api.js";
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
  const operation = node("div", "vpn-operation");
  operation.setAttribute("role", "status");
  operation.setAttribute("aria-live", "polite");
  operation.hidden = true;
  section.append(content, operation);

  const endpoint = `/api/devices/${dm.device.id}/vpn-endpoints`;
  let snapshot = null;
  let loadError = "";
  let candidateOpen = false;
  let showAll = false;
  let discoveryLoading = false;
  let settingsOpening = false;
  let settingsOverlay = null;
  let validationOverlay = null;
  let historyOverlay = null;

  function setOperation(message, kind = "") {
    operation.textContent = message;
    operation.className = `vpn-operation${kind ? ` ${kind}` : ""}`;
    operation.hidden = !message;
  }

  function currentDiagnostics(current, discovery) {
    const details = document.createElement("details");
    details.className = "vpn-details";
    details.appendChild(node("summary", "", "Details"));
    const values = [];
    const runtime = object(current.status);
    const add = (label, value) => { if (value !== "" && value != null) values.push([label, String(value)]); };
    add("WireGuard peer", text(current.peerUuid));
    add("WireGuard instance", text(current.instanceUuid));
    add("Associated gateway", current.gateway ? ownerText(current.gateway) || text(current.gateway.name) : "");
    add("Received bytes", Number.isFinite(Number(runtime.receivedBytes)) ? runtime.receivedBytes : "");
    add("Sent bytes", Number.isFinite(Number(runtime.transmittedBytes)) ? runtime.transmittedBytes : "");
    add("Latest handshake", exactDate(runtime.latestHandshake));
    add("Handshake age", Number.isFinite(Number(runtime.handshakeAge)) ? `${runtime.handshakeAge} seconds` : "");
    add("Ownership classification", text(current.classification));
    add("Endpoint state", text(current.runtimeClassification) || (current.appearsInDiscovery ? "Active" : ""));
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
    const classification = ["Preferred", "Eligible", "Excluded", "Unknown"].includes(current.classification)
      ? current.classification : "Unknown";
    badges.append(pill(health, health.toLowerCase()), pill(classification, classification.toLowerCase()));
    if (current.runtimeClassification === "Stale") badges.appendChild(pill("Stale", "warning"));
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
    metadata.push(classification);
    body.appendChild(node("div", "vpn-candidate-meta", metadata.join(" · ")));
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
    panel.setAttribute("aria-labelledby", `vpn-candidates-${dm.device.id}`);
    panel.appendChild(node("h4", "", "Replacement candidates"));
    panel.lastChild.id = `vpn-candidates-${dm.device.id}`;
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
    const eligible = candidates.filter((candidate) => ["Preferred", "Eligible"].includes(candidate.classification));
    if (!eligible.length) {
      panel.appendChild(node("p", "vpn-message", "No preferred or eligible candidates are available."));
    } else {
      const visible = showAll ? eligible : eligible.slice(0, 3);
      const cards = node("div", "vpn-candidate-list");
      for (const candidate of visible) cards.appendChild(candidateCard(candidate));
      panel.appendChild(cards);
      if (!showAll && eligible.length > 3) {
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
      if (!candidateOpen) {
        candidateOpen = true;
        showAll = false;
        render();
        await refreshCandidates();
      } else {
        const panel = content.querySelector(".vpn-candidates");
        if (panel) panel.focus({ preventScroll: false });
      }
    };
    const more = node("button", "btn btn-sm btn-ghost", "More");
    more.type = "button";
    more.setAttribute("aria-expanded", "false");
    more.setAttribute("aria-controls", "vpn-endpoint-action-menu");
    const menu = node("div", "vpn-action-menu");
    menu.id = "vpn-endpoint-action-menu";
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
    const refresh = node("button", "btn btn-sm btn-ghost", "Refresh");
    refresh.type = "button";
    refresh.onclick = () => { closeMenu(); more.focus(); refreshCandidates(); };
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
      settings.onclick = () => openSettings();
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
      const result = await api(endpoint + (force ? "?refresh=1" : ""), { timeoutMs: 30000 });
      snapshot = object(result);
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
      const result = await api(endpoint + "?refresh=1", { timeoutMs: 30000 });
      snapshot = object(result);
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
      "HomeLabHQ will apply the replacement endpoint, wait for an authenticated WireGuard handshake and restore the previous configuration automatically if verification fails.",
    ].filter((value, index, values) => value !== "" || values[index - 1] !== "").join("\n");
    const confirmed = await confirmDialog({
      title: "Change VPN endpoint?", message, okLabel: "Apply and verify", danger: false,
    });
    if (!confirmed) return;
    await withBusy(button, "Applying…", async () => {
      const timers = [];
      setOperation("Applying configuration…");
      timers.push(setTimeout(() => setOperation("Waiting for authenticated handshake…"), 500));
      timers.push(setTimeout(() => setOperation("Verifying endpoint…"), 1800));
      try {
        const result = await api(endpoint + "/switch", {
          method: "POST", timeoutMs: 45000,
          body: JSON.stringify({ candidateId: candidate.candidateId, confirmed: true }),
        });
        timers.forEach(clearTimeout);
        if (result.ok) {
          setOperation("Endpoint verified.", "success");
          toastOk(result.message || "Endpoint applied and verified.");
        } else {
          setOperation("Rolling back…", "warning");
          await new Promise((resolve) => setTimeout(resolve, 100));
          if (result.rollback) {
            setOperation("Rollback succeeded.", "success");
            toastErr("Endpoint verification failed; the previous configuration was restored.");
          } else {
            setOperation("Rollback failed.", "error");
            toastErr("Endpoint verification and rollback failed. Use OPNsense for manual recovery.");
          }
        }
        await load();
      } catch (error) {
        timers.forEach(clearTimeout);
        setOperation("Endpoint change failed.", "error");
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
          await api(endpoint + "/compatibility", {
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

  function targetHasHistory(targetId) {
    const state = object(snapshot);
    return [...list(object(state.discovery).candidates), ...list(state.history)].some((candidate) =>
      list(candidate.compatibilityTargets).some((target) => target.id === targetId
        && (target.state !== "Unknown" || !!target.lastValidatedAt || !!text(target.note))));
  }

  function targetSummary(targetId) {
    const state = object(snapshot);
    const current = object(state.current);
    const candidates = [current, ...list(object(state.discovery).candidates), ...list(state.history)];
    const checks = candidates.flatMap((candidate) =>
      list(candidate.compatibilityTargets).filter((target) => target.id === targetId));
    return validationSummary(checks);
  }

  async function openSettings() {
    if (settingsOpening || (settingsOverlay && settingsOverlay.isConnected)) return;
    settingsOpening = true;
    let choices;
    try {
      choices = object(await api(endpoint + "/choices"));
    } catch (error) {
      toastErr(error.message || "WireGuard choices could not be loaded.");
      settingsOpening = false;
      return;
    }
    settingsOpening = false;
    const profile = object(object(snapshot).profile);
    const modal = openOverlay({ title: "VPN endpoint settings" });
    settingsOverlay = modal.overlay;
    modal.overlay.classList.add("vpn-dialog", "vpn-settings-dialog");
    const form = node("form", "vpn-settings-form");
    let confirmedRemoval = false;

    const tunnel = group("Tunnel");
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
    tunnel.append(enabledWrap, instanceWrap, peerWrap, gatewayWrap);

    const discovery = group("Discovery");
    const [countryWrap, country] = formField("Country", profile.country || "United Kingdom");
    const [cityWrap, city] = formField("Preferred city", profile.city || "");
    const [limitWrap, limit] = formField("Maximum candidates", profile.maxCandidates ?? 20,
      "number", { min: 1, max: 50 });
    const [intervalWrap, interval] = formField("Discovery interval (seconds)",
      profile.discoveryIntervalSeconds ?? 3600, "number", { min: 300, max: 604800 });
    discovery.append(countryWrap, cityWrap, limitWrap, intervalWrap);

    const preferences = group("Network preferences");
    const [preferredWrap, preferred] = formField("Preferred owner patterns",
      Array.isArray(profile.preferredOwners) ? profile.preferredOwners.join(", ") : "", "text",
      { help: "Comma-separated, case-insensitive ownership patterns." });
    const [excludedWrap, excluded] = formField("Excluded owner patterns",
      Array.isArray(profile.excludedOwners) ? profile.excludedOwners.join(", ") : "");
    const [unknownWrap, unknown] = checkboxField("Include unknown owners", profile.includeUnknownOwners);
    preferences.append(preferredWrap, excludedWrap, unknownWrap);

    const monitoring = group("Monitoring");
    const [warningWrap, warning] = formField("Handshake warning threshold (seconds)",
      profile.handshakeWarningSeconds ?? 300, "number", { min: 60, max: 86400 });
    monitoring.appendChild(warningWrap);

    const targetsGroup = group("Compatibility targets");
    targetsGroup.appendChild(node("p", "muted",
      "Optional manual checks for services, applications or operational requirements."));
    const targetList = node("div", "vpn-target-list");
    const targets = list(profile.compatibilityTargets).map((target) => ({
      id: text(target.id), name: text(target.name), description: text(target.description),
    }));
    const renderTargets = () => {
      targetList.replaceChildren();
      for (const target of targets) {
        const row = node("div", "vpn-target-editor");
        const [nameWrap, nameInput] = formField("Name", target.name, "text", { required: true });
        const [descriptionWrap, descriptionInput] = formField("Description", target.description);
        nameInput.oninput = () => { target.name = nameInput.value; };
        descriptionInput.oninput = () => { target.description = descriptionInput.value; };
        const remove = node("button", "btn btn-sm btn-ghost", "Remove target");
        remove.type = "button";
        remove.onclick = async () => {
          if (targetHasHistory(target.id)) {
            const ok = await confirmDialog({
              title: `Remove “${target.name || "validation target"}”?`,
              message: "This target has saved validation history. Removing it also removes those saved checks.",
              okLabel: "Remove target", danger: false,
            });
            if (!ok) return;
            confirmedRemoval = true;
          }
          targets.splice(targets.indexOf(target), 1);
          renderTargets();
        };
        row.append(nameWrap, descriptionWrap, remove);
        const summary = targetSummary(target.id);
        if (summary) row.appendChild(node("div", "muted vpn-target-summary", summary));
        targetList.appendChild(row);
      }
    };
    const addTarget = node("button", "btn btn-sm btn-ghost", "Add target");
    addTarget.type = "button";
    addTarget.onclick = () => {
      const id = globalThis.crypto && typeof globalThis.crypto.randomUUID === "function"
        ? globalThis.crypto.randomUUID() : `target-${Date.now()}-${targets.length}`;
      targets.push({ id, name: "", description: "" });
      renderTargets();
      const names = targetList.querySelectorAll("input");
      if (names.length) names[names.length - 2].focus();
    };
    targetsGroup.append(targetList, addTarget);
    renderTargets();

    const notesGroup = group("Notes");
    const [notesWrap, notes] = formField("Profile notes", profile.notes || "", "text", { multiline: true });
    notesGroup.appendChild(notesWrap);

    const formActions = node("div", "dialog-actions");
    const cancel = node("button", "btn btn-ghost", "Cancel");
    cancel.type = "button";
    cancel.onclick = modal.close;
    const save = node("button", "btn btn-primary", "Save settings");
    save.type = "submit";
    formActions.append(cancel, save);
    form.append(tunnel, discovery, preferences, monitoring, targetsGroup, notesGroup, formActions);
    modal.body.appendChild(form);
    form.onsubmit = async (event) => {
      event.preventDefault();
      await withBusy(save, "Saving…", async () => {
        try {
          const payload = {
            enabled: enabled.checked,
            peerUuid: peer.value,
            instanceUuid: instance.value,
            gatewayUuid: gateway.value,
            country: country.value,
            city: city.value,
            maxCandidates: Number(limit.value),
            discoveryIntervalSeconds: Number(interval.value),
            preferredOwners: preferred.value.split(",").map((value) => value.trim()).filter(Boolean),
            excludedOwners: excluded.value.split(",").map((value) => value.trim()).filter(Boolean),
            includeUnknownOwners: unknown.checked,
            handshakeWarningSeconds: Number(warning.value),
            compatibilityTargets: targets.map((target) => ({
              id: target.id, name: target.name.trim(), description: target.description.trim(),
            })),
            notes: notes.value,
            confirmTargetRemoval: confirmedRemoval,
          };
          const result = await api(endpoint, {
            method: "PATCH", body: JSON.stringify(payload),
          });
          snapshot = { ...object(snapshot), profileConfigured: true,
            profile: object(result).profile || payload };
          modal.close();
          toastOk("VPN endpoint settings saved.");
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

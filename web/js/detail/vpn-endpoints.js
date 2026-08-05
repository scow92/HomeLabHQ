// OPNsense NordVPN endpoint manager. Owns only its own fetched snapshot and
// receives the detail-modal state from index.js; it never mutates another
// feature module's state or DOM.
"use strict";
import { api, timeAgo } from "../api.js";
import { confirmDialog, detailSection, toastErr, toastOk, withBusy } from "../ui.js";

const field = (label, value, type = "text") => {
  const wrap = document.createElement("label"); wrap.className = "ent-item";
  const text = document.createElement("span"); text.textContent = label;
  const input = document.createElement("input"); input.type = type; input.value = value || "";
  wrap.append(text, input); return [wrap, input];
};

export function vpnEndpointsSection(dm) {
  const section = detailSection("VPN Endpoints");
  const description = document.createElement("p"); description.className = "cz-sub";
  description.textContent = "NordVPN WireGuard candidates are ownership-classified with RDAP. " +
    "A recent authenticated handshake is the primary health signal; gateway state is supporting information.";
  const content = document.createElement("div");
  const actions = document.createElement("div"); actions.className = "action-row";
  const refresh = Object.assign(document.createElement("button"), { className: "btn btn-sm btn-ghost", textContent: "Refresh candidates" });
  const configure = Object.assign(document.createElement("button"), { className: "btn btn-sm btn-ghost", textContent: "Configure" });
  actions.append(refresh, configure); section.append(description, actions, content);

  let snapshot = null;
  const endpoint = `/api/devices/${dm.device.id}/vpn-endpoints`;
  const addText = (parent, text, cls = "muted") => {
    const p = document.createElement("p"); p.className = cls; p.textContent = text; parent.appendChild(p);
  };
  const render = () => {
    content.innerHTML = "";
    if (!snapshot) { addText(content, "Loading endpoint status…"); return; }
    const current = snapshot.current || {};
    const card = document.createElement("div"); card.className = "info-grid";
    const health = current.status || {};
    const pairs = [
      ["Current endpoint", current.endpointIp ? `${current.endpointIp}:${current.endpointPort || 51820}` : "Not configured"],
      ["Current owner", current.owner || current.classification || "Unknown"],
      ["Handshake", health.latestHandshake ? `${timeAgo(health.latestHandshake)} (${health.handshakeAge || 0}s old)` : "No authenticated handshake"],
      ["Transfer", health.receivedBytes == null ? "–" : `${health.receivedBytes} received · ${health.transmittedBytes || 0} sent`],
      ["WireGuard status", health.status || "Unknown"],
      ["Gateway", current.gateway ? `${current.gateway.name || "Gateway"} · ${current.gateway.status || "unknown"}` : "Not associated"],
      ["Discovery", snapshot.discovery.status || "Unknown"],
    ];
    for (const [label, value] of pairs) {
      const chip = document.createElement("div"); chip.className = "info-chip";
      chip.innerHTML = "<div class='k'></div><div class='v'></div>";
      chip.children[0].textContent = label; chip.children[1].textContent = value; card.appendChild(chip);
    }
    content.appendChild(card);
    if (snapshot.discovery.error) addText(content, snapshot.discovery.error, "err");
    const makeTable = (title, rows) => {
      const wrap = document.createElement("div");
      const h = document.createElement("h3"); h.textContent = title; wrap.appendChild(h);
      if (!rows.length) { addText(wrap, "None."); return wrap; }
      const table = document.createElement("table"); table.className = "detail-table";
      table.innerHTML = "<thead><tr><th>Server</th><th>Owner / ASN</th><th>City</th><th>Load</th><th>Ring</th><th>Status</th><th></th></tr></thead>";
      const body = document.createElement("tbody");
      for (const candidate of rows) {
        const tr = document.createElement("tr");
        const owner = [candidate.owner || "Unknown", candidate.asn ? `AS${candidate.asn}` : ""].filter(Boolean).join(" · ");
        for (const value of [`${candidate.hostname}\n${candidate.endpointIp}`, owner, candidate.city || "–",
                              candidate.load == null ? "–" : `${candidate.load}%`, candidate.compatibility || "Unknown",
                              candidate.active ? "Active" : candidate.classification]) {
          const td = document.createElement("td"); td.textContent = value; tr.appendChild(td);
        }
        const act = document.createElement("td");
        if (candidate.classification === "Preferred" && !candidate.active) {
          const btn = Object.assign(document.createElement("button"), { className: "btn btn-sm btn-primary", textContent: "Test and switch" });
          btn.onclick = () => testAndSwitch(candidate, btn); act.appendChild(btn);
        }
        const verified = Object.assign(document.createElement("button"), { className: "btn btn-sm btn-ghost", textContent: "Ring result" });
        verified.onclick = () => markRing(candidate); act.appendChild(verified); tr.appendChild(act); body.appendChild(tr);
      }
      table.appendChild(body); wrap.appendChild(table); return wrap;
    };
    const candidates = snapshot.discovery.candidates || [];
    content.appendChild(makeTable("Preferred candidates", candidates.filter((x) => x.classification === "Preferred")));
    const other = document.createElement("details");
    const summary = document.createElement("summary"); summary.textContent = "Rejected and unknown candidates"; other.appendChild(summary);
    other.appendChild(makeTable("Other candidates", candidates.filter((x) => x.classification !== "Preferred"))); content.appendChild(other);
  };
  async function load(force = false) {
    try { snapshot = await api(endpoint + (force ? "?refresh=1" : ""), { timeoutMs: 30000 }); render(); }
    catch (error) { content.innerHTML = ""; addText(content, "Could not load VPN endpoints: " + error.message, "err"); }
  }
  async function testAndSwitch(candidate, button) {
    const current = snapshot.current || {};
    const ok = await confirmDialog({ title: "Test and switch VPN endpoint?",
      message: `Current: ${current.endpointIp || "not configured"}:${current.endpointPort || 51820}\nNew: ${candidate.endpointIp}:${candidate.endpointPort}\n\nHomelabHQ will apply the peer, wait for an authenticated handshake, and restore the complete prior configuration if verification fails.`,
      okLabel: "Test and switch", danger: true });
    if (!ok) return;
    await withBusy(button, "Switching…", async () => {
      try {
        const result = await api(endpoint + "/switch", { method: "POST", timeoutMs: 45000,
          body: JSON.stringify({ candidateId: candidate.candidateId, confirmed: true }) });
        result.ok ? toastOk(result.message) : toastErr(`${result.message} Rollback: ${result.rollback ? "verified" : "failed or unverified"}.`);
        await load(true);
      } catch (error) { toastErr(error.message); }
    });
  }
  async function markRing(candidate) {
    const state = prompt("Ring compatibility (Verified, Failed, Assumed from provider, or Unknown):", candidate.compatibility || "Unknown");
    if (state == null) return;
    const note = prompt("Optional compatibility note:", "");
    if (note == null) return;
    try {
      await api(endpoint + "/compatibility", { method: "POST", body: JSON.stringify({ candidateId: candidate.candidateId, state, note }) });
      toastOk("Ring compatibility saved."); await load();
    } catch (error) { toastErr(error.message); }
  }
  async function openConfigure() {
    try {
      const choices = await api(endpoint + "/choices");
      const profile = snapshot ? snapshot.profile : {};
      const form = document.createElement("div"); form.className = "ent-list";
      const enabled = document.createElement("input"); enabled.type = "checkbox"; enabled.checked = !!profile.enabled;
      const enabledLabel = document.createElement("label"); enabledLabel.className = "ent-item"; enabledLabel.append(enabled, document.createTextNode(" Enable endpoint management")); form.appendChild(enabledLabel);
      const [countryWrap, country] = field("Country", profile.country || "United Kingdom");
      const [cityWrap, city] = field("City preference", profile.city || "London");
      const [limitWrap, limit] = field("Candidate limit", profile.maxCandidates || 20, "number");
      const [intervalWrap, interval] = field("Discovery interval seconds", profile.discoveryIntervalSeconds || 3600, "number");
      const [warningWrap, warning] = field("Handshake warning seconds", profile.handshakeWarningSeconds || 300, "number");
      const [preferredWrap, preferred] = field("Preferred owner patterns (comma separated)", (profile.preferredOwners || []).join(", "));
      const [rejectedWrap, rejected] = field("Rejected owner patterns (comma separated)", (profile.rejectedOwners || []).join(", "));
      const [notesWrap, notes] = field("Notes", profile.notes || "");
      form.append(countryWrap, cityWrap, limitWrap, intervalWrap, warningWrap, preferredWrap, rejectedWrap, notesWrap);
      const peer = document.createElement("select"); for (const item of choices.peers || []) peer.append(new Option(item.name, item.uuid)); peer.value = profile.peerUuid || "";
      const peerWrap = document.createElement("label"); peerWrap.className = "ent-item"; peerWrap.append(document.createTextNode("WireGuard peer"), peer); form.appendChild(peerWrap);
      const instance = document.createElement("select"); for (const item of choices.instances || []) instance.append(new Option(item.name, item.uuid)); instance.value = profile.instanceUuid || "";
      const instanceWrap = document.createElement("label"); instanceWrap.className = "ent-item"; instanceWrap.append(document.createTextNode("WireGuard instance"), instance); form.appendChild(instanceWrap);
      const gateway = field("Gateway UUID (optional)", profile.gatewayUuid || ""); form.appendChild(gateway[0]);
      const unknown = document.createElement("input"); unknown.type = "checkbox"; unknown.checked = !!profile.includeUnknownOwners;
      const unknownWrap = document.createElement("label"); unknownWrap.className = "ent-item"; unknownWrap.append(unknown, document.createTextNode(" Include unknown owners in discovery")); form.appendChild(unknownWrap);
      const save = Object.assign(document.createElement("button"), { className: "btn btn-primary btn-sm", textContent: "Save VPN profile" });
      form.appendChild(save); content.prepend(form);
      save.onclick = async () => withBusy(save, "Saving…", async () => {
        try {
          await api(endpoint, { method: "PUT", body: JSON.stringify({ ...profile, enabled: enabled.checked, country: country.value,
            city: city.value, maxCandidates: limit.value, discoveryIntervalSeconds: interval.value,
            handshakeWarningSeconds: warning.value, peerUuid: peer.value, instanceUuid: instance.value,
            gatewayUuid: gateway[1].value, preferredOwners: preferred.value.split(",").map((x) => x.trim()).filter(Boolean),
            rejectedOwners: rejected.value.split(",").map((x) => x.trim()).filter(Boolean), notes: notes.value,
            includeUnknownOwners: unknown.checked }) });
          toastOk("VPN endpoint profile saved."); await load();
        } catch (error) { toastErr(error.message); }
      });
    } catch (error) { toastErr(error.message); }
  }
  refresh.onclick = () => withBusy(refresh, "Refreshing…", () => load(true));
  configure.onclick = openConfigure;
  load(); return section;
}

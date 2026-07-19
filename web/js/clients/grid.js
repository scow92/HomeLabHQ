// Client list rendering only. User intents are emitted through callbacks so
// this module owns no feature state and has no dependency on index.js.
"use strict";
import { $, timeAgo, cellSeverity } from "../api.js";
import { iconBtn, reconcileList, buildTable,
  ICON_EDIT, ICON_CHECK, ICON_REVOKE, ICON_IGNORE, ICON_TRASH } from "../ui.js";
import { fetchClientHistory } from "./api.js";
import { getFilters, isOnline, matchesClient } from "./filters.js";

const sectionCards = { needs: new Map(), online: new Map(), offline: new Map() };
const clientName = (client) => (client.hostname || client.ip || client.mac).toLowerCase();
const ipKey = (client) => (client.ip || "").split(".").map((part) => part.padStart(3, "0")).join(".");

export function renderClientGrid(roster, actions) {
  const { clients = [], sources = [], nac } = roster;
  const { query, status } = getFilters();
  const rows = clients.filter(matchesClient);
  const online = clients.filter(isOnline).length;
  const wifi = clients.filter((client) => client.kind === "wifi" && isOnline(client)).length;
  const configured = nac && nac.configured;
  const approved = configured ? clients.filter((client) => client.nac === "approved").length : null;
  const needsApproval = configured ? clients.filter((client) => client.nac !== "approved" && isOnline(client)).length : 0;
  const errors = sources.filter((source) => source.error);
  if ("setAppBadge" in navigator) {
    const pending = needsApproval ? navigator.setAppBadge(needsApproval) : navigator.clearAppBadge();
    if (pending && pending.catch) pending.catch(() => {});
  }
  const summary = $("#clients-summary");
  summary.hidden = false;
  summary.textContent = `${clients.length} devices · ${online} online` +
    (clients.length - online ? ` · ${clients.length - online} offline` : "") +
    ` · ${wifi} Wi-Fi · ${online - wifi} wired · from ${sources.length} device${sources.length === 1 ? "" : "s"}` +
    (approved != null ? ` · ${approved} approved` : "") +
    (needsApproval ? ` · ${needsApproval} need approval` : "") +
    (errors.length ? ` · ${errors.length} source(s) unreachable` : "");
  const body = $("#clients-body"); body.innerHTML = "";
  const banner = nacBanner(nac, actions);
  if (banner) body.appendChild(banner);
  if (!clients.length) { summary.hidden = true; body.appendChild(emptyState(sources.length)); return; }
  if ((query || status !== "all") && !rows.length) {
    const message = document.createElement("p"); message.className = "muted";
    message.textContent = query ? `No clients match “${query}”.` : `No ${status} devices.`;
    body.appendChild(message); return;
  }
  body.appendChild(configured ? clientCards(rows, nac, actions) : clientsTable(rows));
}

function clientsTable(rows) {
  const cols = [{ key: "client", label: "Client" }, { key: "status", label: "Status" }, { key: "ip", label: "IP" },
    { key: "mac", label: "MAC" }, { key: "kind", label: "Type" }, { key: "signal", label: "Signal" }, { key: "seen", label: "Seen on" }];
  const { wrap } = buildTable({ cols, rows, wrapClass: "detail-table-wrap tall", tableClass: "clients-table", cellFn(td, client, col) {
    if (col.key === "seen") { td.appendChild(seenBadges(client)); return; }
    const online = isOnline(client);
    td.textContent = ({ client: client.name || client.hostname || client.ip || client.vendor || "—", status: online ? "Online" : `Offline · ${timeAgo(client.lastSeen)}`,
      ip: client.ip || "–", mac: client.mac, kind: client.kind === "wifi" ? "Wi-Fi" : "Wired", signal: client.signal == null ? "–" : `${client.signal} dBm` })[col.key];
    const classes = [];
    if (/mac|ip|signal/.test(col.key)) classes.push("mono");
    if (col.key === "status") classes.push(online ? "sev-good" : "sev-bad");
    if (col.key === "signal") { const tone = cellSeverity("signal", client.signal); if (tone) classes.push(tone); }
    if (col.key === "kind" && client.kind === "wifi") classes.push("sev-accent");
    td.className = classes.join(" ");
  } });
  return wrap;
}

function seenBadges(client) {
  const box = document.createElement("div"); box.className = "seen-badges";
  const seen = client.seen || [];
  if (!seen.length && client.via) { const badge = document.createElement("span"); badge.className = "seen-badge"; badge.textContent = client.via; badge.title = "Last seen here"; box.appendChild(badge); return box; }
  for (const source of seen) { const badge = document.createElement("span"); badge.className = "seen-badge"; badge.textContent = source.via + (source.where ? ` · ${source.where}` : ""); box.appendChild(badge); }
  return box;
}

function sortClients(rows) {
  const { sort } = getFilters();
  return rows.slice().sort((a, b) => {
    if (sort === "ip") return ipKey(a).localeCompare(ipKey(b)) || clientName(a).localeCompare(clientName(b));
    if (sort === "mac") return a.mac.localeCompare(b.mac);
    if (sort === "signal") return (b.signal ?? -999) - (a.signal ?? -999) || clientName(a).localeCompare(clientName(b));
    if (sort === "lastseen") return (b.lastSeen ?? 0) - (a.lastSeen ?? 0) || clientName(a).localeCompare(clientName(b));
    return clientName(a).localeCompare(clientName(b));
  });
}

function clientCards(rows, nac, actions) {
  const box = document.createElement("div"); box.className = "client-sections";
  const sections = [
    { key: "needs", title: "Needs approval", rows: rows.filter((client) => isOnline(client) && client.nac !== "approved"), cls: "needs" },
    { key: "online", title: "Connected", rows: rows.filter((client) => isOnline(client) && client.nac === "approved"), cls: "" },
    { key: "offline", title: "Offline", rows: rows.filter((client) => !isOnline(client)), cls: "off" },
  ];
  for (const section of sections) {
    const cache = sectionCards[section.key];
    if (!section.rows.length) { reconcileList(document.createElement("div"), cache, [], (client) => client.mac, () => {}, () => {}); continue; }
    const title = document.createElement("h3"); title.className = "cc-section-title" + (section.cls ? ` ${section.cls}` : ""); title.textContent = section.title;
    const count = document.createElement("span"); count.className = "cc-section-count"; count.textContent = section.rows.length; title.appendChild(count);
    const grid = document.createElement("div"); grid.className = "cards client-cards";
    reconcileList(grid, cache, sortClients(section.rows), (client) => client.mac,
      (client) => buildCard(client, nac, actions), (entry, client) => entry.patch(client, nac));
    box.append(title, grid);
  }
  return box;
}

function clientAp(client) {
  const wifi = (client.seen || []).filter((source) => source.kind === "wifi");
  if (!wifi.length) return "";
  const signal = wifi.filter((source) => source.signal != null);
  return (signal.length ? signal.reduce((a, b) => b.signal > a.signal ? b : a) : wifi[0]).via || "";
}
function signalTone(dbm) { return dbm == null ? "" : dbm >= -60 ? "sev-good" : dbm >= -72 ? "sev-warn" : "sev-bad"; }

function buildCard(client, nac, actions) {
  let current = client, currentNac = nac;
  const el = document.createElement("div"); el.className = "card client-card clickable"; el.title = "Click for details";
  el.innerHTML = `<div class="card-row"><h2><span class="dot up"></span><span class="sr-only cc-status"></span><span class="cc-name"></span></h2><span class="pill nac-pill"></span></div><div class="muted cc-meta"></div><div class="muted cc-vendor" hidden></div><div class="muted cc-last" hidden></div><div class="cc-signal" hidden></div><div class="cc-detail" hidden></div><div class="dev-actions cc-actions"></div>`;
  const dot = $(".dot", el), status = $(".cc-status", el), name = $(".cc-name", el), pill = $(".nac-pill", el), meta = $(".cc-meta", el), vendor = $(".cc-vendor", el), last = $(".cc-last", el), signal = $(".cc-signal", el), detail = $(".cc-detail", el), buttons = $(".cc-actions", el);
  last.dataset.tsPrefix = "Last seen ";
  el.addEventListener("click", (event) => { if (event.target.closest(".cc-actions")) return; const opening = detail.hidden; if (opening) fillDetail(detail, current); detail.hidden = !opening; el.classList.toggle("expanded", opening); });
  function patch(next, nextNac) {
    current = next; currentNac = nextNac;
    const online = isOnline(next), member = next.nac === "approved", needs = !member;
    el.classList.toggle("needs-approval", needs && online); el.classList.toggle("is-new", !!next.new); el.classList.toggle("offline", !online);
    name.textContent = next.name || next.hostname || next.ip || next.vendor || next.mac;
    dot.className = `dot ${online ? "up" : "unknown"}`; dot.title = online ? "Currently connected" : `Offline — last seen ${timeAgo(next.lastSeen)}`; status.textContent = online ? "Connected" : "Offline";
    last.hidden = online || !next.lastSeen; if (!last.hidden) { last.textContent = `Last seen ${timeAgo(next.lastSeen)}`; last.dataset.ts = next.lastSeen; } else last.removeAttribute("data-ts");
    pill.className = "pill nac-pill"; if (member) { pill.textContent = "Approved"; pill.classList.add("nac-ok"); } else if (next.new) { pill.textContent = "New"; pill.classList.add("nac-new"); } else { pill.textContent = "Needs approval"; pill.classList.add("nac-blocked"); }
    meta.textContent = (next.ip ? `${next.ip} · ` : "") + next.mac; vendor.hidden = !next.vendor; if (next.vendor) vendor.textContent = next.vendor;
    signal.hidden = !(online && next.kind === "wifi" && next.signal != null);
    if (!signal.hidden) { const tone = signalTone(next.signal), pct = Math.max(0, Math.min(100, Math.round((next.signal + 90) / 60 * 100))); signal.innerHTML = `<span class="cc-sig-bar"><i></i></span><span class="cc-sig-val mono ${tone}"></span><span class="cc-sig-ap muted" hidden></span>`; $(".cc-sig-val", signal).textContent = `${next.signal} dBm`; const bar = $(".cc-sig-bar i", signal); bar.style.width = `${pct}%`; bar.className = tone; const ap = clientAp(next); if (ap) { const apEl = $(".cc-sig-ap", signal); apEl.hidden = false; apEl.textContent = ap; apEl.title = `Connected via ${ap}`; } }
    if (!detail.hidden) fillDetail(detail, next);
    buttons.innerHTML = "";
    const approval = iconBtn(member ? ICON_REVOKE : ICON_CHECK, member ? "Revoke access" : "Approve", member ? () => actions.approve(current, currentNac, false, approval) : () => actions.edit(current, { approve: true, nac: currentNac }), member ? "icon-btn-danger" : "icon-btn-primary"); buttons.appendChild(approval);
    if (needs && online) { const ignore = iconBtn(ICON_IGNORE, "Ignore — hide until this device connects again"); ignore.onclick = () => actions.ignore(current, ignore); buttons.appendChild(ignore); }
    buttons.appendChild(iconBtn(ICON_EDIT, "Edit — rename, add notes, sync DNS / firewall aliases", () => actions.edit(current, { nac: currentNac })));
    if (!online) { const forget = iconBtn(ICON_TRASH, "Forget — delete this device's saved history", () => actions.forget(current, forget), "icon-btn-danger"); buttons.appendChild(forget); }
  }
  patch(client, nac); return { el, patch };
}

function fillDetail(box, client) {
  box.innerHTML = ""; const values = document.createElement("div"); values.className = "cc-kv";
  const add = (key, value, timestamp) => { if (value == null || value === "") return; const label = document.createElement("span"); label.className = "cc-k"; label.textContent = key; const content = document.createElement("span"); content.className = "cc-v"; content.textContent = value; if (timestamp) content.dataset.ts = timestamp; values.append(label, content); };
  add("Hostname", client.hostname); add("IP", client.ip); add("MAC", client.mac); add("Vendor", client.vendor); add("Type", client.kind === "wifi" ? "Wi-Fi" : "Wired"); if (client.firstSeen) add("First seen", timeAgo(client.firstSeen), client.firstSeen); if (!isOnline(client) && client.lastSeen) add("Last seen", timeAgo(client.lastSeen), client.lastSeen); add("Notes", client.notes); if (client.notify) add("Notifications", "On — connect/disconnect alerts"); box.appendChild(values);
  if (client.aliases && client.aliases.length) { const heading = document.createElement("div"); heading.className = "cc-seen-title muted"; heading.textContent = "Firewall aliases"; const aliases = document.createElement("div"); aliases.className = "cc-aliases"; for (const alias of client.aliases) { const pill = document.createElement("span"); pill.className = "pill alias-pill"; pill.textContent = alias.name; aliases.appendChild(pill); } box.append(heading, aliases); }
  if ((client.seen || []).length || client.via) { const heading = document.createElement("div"); heading.className = "cc-seen-title muted"; heading.textContent = (client.seen || []).length ? "Seen on" : "Last seen on"; box.append(heading, seenBadges(client)); }
  const heading = document.createElement("div"); heading.className = "cc-seen-title muted"; heading.textContent = "Connection history"; const history = document.createElement("div"); history.className = "cc-history muted"; history.textContent = "Loading…"; box.append(heading, history);
  fetchClientHistory(client.mac).then((result) => { if (!history.isConnected) return; history.classList.remove("muted"); history.innerHTML = ""; const events = (result.events || []).slice(-12).reverse(); if (!events.length) { history.innerHTML = `<span class="muted">No events recorded yet — history builds up as the network is scanned.</span>`; return; } for (const event of events) { const row = document.createElement("div"); row.className = "cc-ev"; const marker = document.createElement("span"); marker.className = `cc-ev-dot ${event.ev === "up" ? "up" : "down"}`; const what = document.createElement("span"); what.textContent = event.ev === "up" ? `Connected${event.via ? ` via ${event.via}` : ""}` : "Disconnected"; const when = document.createElement("span"); when.className = "cc-ev-when muted"; when.textContent = timeAgo(event.ts); when.dataset.ts = event.ts; row.append(marker, what, when); history.appendChild(row); } }).catch((error) => { if (history.isConnected) history.textContent = `Couldn't load history: ${error.message}`; });
}

function nacBanner(nac, actions) {
  if (!nac || !nac.deviceId) return null;
  const box = document.createElement("div"); box.className = "nac-banner card";
  if (!nac.configured) { box.innerHTML = `<div class="nac-b-main"><h2>Set up Network Access Control</h2><p class="muted">Turn this list into an allow-list: approve the devices you trust, and (when you switch enforcement on) everything else is blocked at <strong class="nac-dev"></strong>. Nothing changes until you approve devices and enable enforcement.</p></div><button class="btn btn-primary nac-setup-btn">Set up</button>`; $(".nac-dev", box).textContent = nac.deviceName || "the firewall"; $(".nac-setup-btn", box).onclick = () => actions.setup(nac); return box; }
  if (nac.managedExternally) return null;
  box.classList.toggle("enforcing", !!nac.enforced); box.innerHTML = `<div class="nac-b-main"><h2>Access control <span class="nac-alias pill"></span></h2><p class="muted nac-b-sub"></p></div><div class="nac-b-switch"><span class="nac-sw-label"></span><button type="button" class="fw-switch nac-enforce" role="switch"><span class="fw-knob"></span></button></div>`;
  $(".nac-alias", box).textContent = nac.alias || ""; $(".nac-b-sub", box).textContent = nac.enforced ? "Enforcement is ON — only approved devices have network access." : "Enforcement is OFF — every device is allowed. Approve your devices, then turn it on."; $(".nac-sw-label", box).textContent = nac.enforced ? "Enforcing" : "Off";
  const toggle = $(".nac-enforce", box); toggle.classList.toggle("on", !!nac.enforced); toggle.setAttribute("aria-checked", String(!!nac.enforced)); toggle.onclick = () => actions.enforcement(nac, !nac.enforced, toggle); return box;
}

function emptyState(sourceCount) {
  const box = document.createElement("div"); box.className = "empty"; box.innerHTML = `<div class="empty-mark">◎</div><p><strong>No clients to show yet.</strong></p><p class="muted">The Clients view automatically aggregates every device seen by the access points and managed switches you add — hostname, IP, MAC, signal and where each one is connected.</p><p class="muted">${sourceCount ? "Your client sources are reachable but reported nothing yet — try Refresh in a moment." : "Add a Wi-Fi access point or a managed switch to get started."}</p><button class="btn btn-primary" data-goto="add">Add a device</button>`; return box;
}

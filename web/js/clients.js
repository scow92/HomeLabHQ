// Access tab: the network-wide client list, Network Access Control (NAC) setup
// + enforcement toggle, and the edit/approve client modal.
"use strict";
import { $, $$, api, timeAgo, cellSeverity } from "./api.js";
import { toastErr, toastOk, promptDialog, confirmDialog, pickDialog, withBusy,
         renderError, iconBtn, pushModal, popModal, reconcileList, skeletonCards,
         ICON_EDIT, ICON_CHECK, ICON_REVOKE, ICON_IGNORE, ICON_TRASH } from "./ui.js";

// Switching tabs is handled by router.js; dispatching an event here instead
// of importing switchTab directly avoids an app.js <-> clients.js import
// cycle (refactor.md 2.3).
function switchTab(name) {
  document.dispatchEvent(new CustomEvent("hlhq:navigate", { detail: { tab: name } }));
}

export let CLIENTS = null;      // last-loaded {clients, sources}
let CLIENTS_Q = "";      // search filter
let CLIENTS_STATUS = "all";     // status filter: all | online | offline
let CLIENTS_SORT = "hostname";  // card sort order (needs-approval always floats to top)
try { CLIENTS_SORT = localStorage.getItem("hlhq-clients-sort") || "hostname"; } catch (_) {}
// "status" was the old default (a dedicated sort mode); approval now always
// sorts to the top regardless of mode, so fold the legacy value into hostname.
if (CLIENTS_SORT === "status") CLIENTS_SORT = "hostname";

export async function loadClients() {
  const body = $("#clients-body");
  if (!CLIENTS) { body.innerHTML = ""; body.appendChild(skeletonCards(4)); }
  try {
    CLIENTS = await api("/api/clients");
    renderClients();
  } catch (ex) {
    // Don't wipe a good view on a transient refresh error — just surface it.
    if (CLIENTS) toastErr("Couldn't refresh clients: " + ex.message);
    else renderError(body, "Couldn't load clients: " + ex.message);
  }
}

// A client record with no `online` field (older server) is treated as online —
// everything in the pre-roster list was, by definition, currently connected.
const isOnline = (c) => c.online !== false;

function clientMatches(c) {
  if (CLIENTS_STATUS === "online" && !isOnline(c)) return false;
  if (CLIENTS_STATUS === "offline" && isOnline(c)) return false;
  if (!CLIENTS_Q) return true;
  const hay = `${c.name || ""} ${c.hostname} ${c.ip} ${c.mac} ${c.kind} ` +
    `${c.vendor || ""} ${c.via || ""} ` +
    (c.seen || []).map((s) => `${s.via} ${s.where}`).join(" ");
  return CLIENTS_Q.split(/\s+/).every((t) => hay.toLowerCase().includes(t));
}

function renderClients() {
  const { clients, sources, nac } = CLIENTS;
  const rows = clients.filter(clientMatches);
  const online = clients.filter(isOnline).length;
  const offline = clients.length - online;
  const wifi = clients.filter((c) => c.kind === "wifi" && isOnline(c)).length;
  const summary = $("#clients-summary");
  const errs = sources.filter((s) => s.error);
  const configured = nac && nac.configured;
  const approved = configured
    ? clients.filter((c) => c.nac === "approved").length : null;
  const needsApproval = configured
    ? clients.filter((c) => c.nac !== "approved" && isOnline(c)).length : 0;
  summary.hidden = false;
  summary.textContent =
    `${clients.length} devices · ${online} online` +
    (offline ? ` · ${offline} offline` : "") +
    ` · ${wifi} Wi-Fi · ${online - wifi} wired · ` +
    `from ${sources.length} device${sources.length === 1 ? "" : "s"}` +
    (approved != null ? ` · ${approved} approved` : "") +
    (needsApproval ? ` · ${needsApproval} need approval` : "") +
    (errs.length ? ` · ${errs.length} source(s) unreachable` : "");

  const body = $("#clients-body");
  body.innerHTML = "";
  const banner = nacBanner(nac);
  if (banner) body.appendChild(banner);

  if (!clients.length) {
    summary.hidden = true;
    body.appendChild(clientsEmptyState(sources.length));
    return;
  }
  if ((CLIENTS_Q || CLIENTS_STATUS !== "all") && !rows.length) {
    const p = document.createElement("p");
    p.className = "muted";
    p.textContent = CLIENTS_Q ? `No clients match “${CLIENTS_Q}”.`
      : `No ${CLIENTS_STATUS} devices.`;
    body.appendChild(p);
    return;
  }
  // NAC configured → a card per device (Approve/Revoke). Otherwise the classic
  // read-only table.
  body.appendChild(nac && nac.configured
    ? clientCardGrid(rows, nac) : clientsTable(rows));
}

// Read-only aggregated table (the view before access control is set up).
function clientsTable(rows) {
  const cols = [
    { key: "client", label: "Client" }, { key: "status", label: "Status" },
    { key: "ip", label: "IP" },
    { key: "mac", label: "MAC" }, { key: "kind", label: "Type" },
    { key: "signal", label: "Signal" }, { key: "seen", label: "Seen on" },
  ];
  const wrap = document.createElement("div");
  wrap.className = "detail-table-wrap tall";
  const table = document.createElement("table");
  table.className = "detail-table clients-table";
  table.innerHTML = "<thead><tr>" +
    cols.map(() => `<th></th>`).join("") + "</tr></thead>";
  $$("th", table).forEach((th, i) => (th.textContent = cols[i].label));
  const tbody = document.createElement("tbody");
  for (const c of rows) {
    const tr = document.createElement("tr");
    for (const col of cols) {
      const td = document.createElement("td");
      if (col.key === "seen") {
        td.appendChild(seenBadges(c));
      } else {
        const on = isOnline(c);
        const cells = {
          client: c.name || c.hostname || c.ip || c.vendor || "—",
          status: on ? "Online" : `Offline · ${timeAgo(c.lastSeen)}`,
          ip: c.ip || "–", mac: c.mac,
          kind: c.kind === "wifi" ? "Wi-Fi" : "Wired",
          signal: c.signal == null ? "–" : `${c.signal} dBm`,
        };
        td.textContent = cells[col.key];
        const cls = [];
        if (/mac|ip|signal/.test(col.key)) cls.push("mono");
        if (col.key === "status") cls.push(on ? "sev-good" : "sev-bad");
        if (col.key === "signal") { const s = cellSeverity("signal", c.signal); if (s) cls.push(s); }
        if (col.key === "kind" && c.kind === "wifi") cls.push("sev-accent");
        if (cls.length) td.className = cls.join(" ");
      }
      tr.appendChild(td);
    }
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  wrap.appendChild(table);
  return wrap;
}

// "Seen on" badges shared by the table and the cards. An offline client has no
// live sources — fall back to the location remembered at its last sighting.
function seenBadges(c) {
  const box = document.createElement("div");
  box.className = "seen-badges";
  const seen = c.seen || [];
  if (!seen.length && c.via) {
    const b = document.createElement("span");
    b.className = "seen-badge";
    b.textContent = c.via;
    b.title = "Last seen here";
    box.appendChild(b);
    return box;
  }
  for (const s of seen) {
    const b = document.createElement("span");
    b.className = "seen-badge";
    b.textContent = s.via + (s.where ? ` · ${s.where}` : "");
    box.appendChild(b);
  }
  return box;
}

// ---- NAC: one card per client (Approve / Revoke / Ignore) -------------------
const _cName = (c) => (c.hostname || c.ip || c.mac).toLowerCase();
const _cIpKey = (c) => (c.ip || "").split(".").map((n) => String(n).padStart(3, "0")).join(".");

function sortClients(rows, mode) {
  const arr = rows.slice();
  // Whatever the chosen field, connected devices needing approval always float
  // to the top so they can't be missed, and online devices sort before offline
  // history entries; the selected mode orders within each group.
  const byField = (a, b) => {
    if (mode === "ip") return _cIpKey(a).localeCompare(_cIpKey(b)) || _cName(a).localeCompare(_cName(b));
    if (mode === "mac") return a.mac.localeCompare(b.mac);
    // strongest signal first; wired (no signal) sink to the bottom.
    if (mode === "signal") return (b.signal ?? -999) - (a.signal ?? -999) || _cName(a).localeCompare(_cName(b));
    // most recently seen first (offline history browsing).
    if (mode === "lastseen") return (b.lastSeen ?? 0) - (a.lastSeen ?? 0) || _cName(a).localeCompare(_cName(b));
    return _cName(a).localeCompare(_cName(b));  // "hostname" (default)
  };
  arr.sort((a, b) => {
    const aNeeds = a.nac !== "approved" && isOnline(a);
    const bNeeds = b.nac !== "approved" && isOnline(b);
    if (aNeeds !== bNeeds) return aNeeds ? -1 : 1;  // needs-approval first, always
    if (isOnline(a) !== isOnline(b)) return isOnline(a) ? -1 : 1;
    return byField(a, b);
  });
  return arr;
}

// Reused across renderClients() calls (keyed by MAC) so an expanded card
// survives the ~60s refresh instead of collapsing under the user (§4.2). The
// grid <div> itself is still rebuilt each render (cheap, and it holds no
// state of its own) — only the card elements inside it are reused; moving an
// already-built card into a fresh grid preserves its DOM state (expanded
// detail, focus) because it's the same node, not a new one.
const CLIENT_CARDS = new Map();

function clientCardGrid(rows, nac) {
  const grid = document.createElement("div");
  grid.className = "cards client-cards";
  const sorted = sortClients(rows, CLIENTS_SORT);
  reconcileList(grid, CLIENT_CARDS, sorted, (c) => c.mac, buildClientCard,
    (entry, c) => entry.patch(c, nac));
  return grid;
}

// The access point a Wi-Fi client is associated with: the "seen on" source that
// reported the (strongest) Wi-Fi signal. Falls back to any Wi-Fi source.
function clientAp(c) {
  const wifi = (c.seen || []).filter((s) => s.kind === "wifi");
  if (!wifi.length) return "";
  const withSig = wifi.filter((s) => s.signal != null);
  const pick = withSig.length
    ? withSig.reduce((a, b) => (b.signal > a.signal ? b : a))
    : wifi[0];
  return pick.via || "";
}

// Wi-Fi RSSI → severity class (matches the table's signal colouring).
function signalTone(dbm) {
  if (dbm == null) return "";
  if (dbm >= -60) return "sev-good";
  if (dbm >= -72) return "sev-warn";
  return "sev-bad";
}

// Builds a client card once; returns {el, patch(c, nac)}. See CLIENT_CARDS
// above for why: patching in place (instead of the old full rebuild) keeps an
// expanded card's detail panel open across the ~60s background refresh.
function buildClientCard(c, nac) {
  let cur = c, curNac = nac;
  const el = document.createElement("div");
  el.className = "card client-card clickable";
  el.title = "Click for details";
  // The roster keeps every device ever seen, so the dot carries real state:
  // green = currently connected, grey = offline (with a "last seen" line).
  el.innerHTML = `
    <div class="card-row">
      <h2><span class="dot up"></span><span class="sr-only cc-status"></span><span class="cc-name"></span></h2>
      <span class="pill nac-pill"></span>
    </div>
    <div class="muted cc-meta"></div>
    <div class="muted cc-vendor" hidden></div>
    <div class="muted cc-last" hidden></div>
    <div class="cc-signal" hidden></div>
    <div class="cc-detail" hidden></div>
    <div class="dev-actions cc-actions"></div>`;
  const dot = $(".dot", el);
  const statusText = $(".cc-status", el);
  const nameEl = $(".cc-name", el);
  const pill = $(".nac-pill", el);
  const meta = $(".cc-meta", el);
  const vendor = $(".cc-vendor", el);
  const lastSeen = $(".cc-last", el);
  lastSeen.dataset.tsPrefix = "Last seen ";  // kept fresh by the 30s ticker
  const sig = $(".cc-signal", el);
  const detail = $(".cc-detail", el);
  const acts = $(".cc-actions", el);

  // Clicking the card body (not a button) expands more detail. Refreshes the
  // detail content on every open (not just the first) so aliases/notes stay
  // current; collapsing never touches its content, so a fast toggle can't
  // show a stale flash.
  el.addEventListener("click", (e) => {
    if (e.target.closest(".cc-actions")) return;
    const opening = detail.hidden;
    if (opening) fillClientDetail(detail, cur);
    detail.hidden = !opening;
    el.classList.toggle("expanded", opening);
  });

  function patch(c, nac) {
    cur = c; curNac = nac;
    const on = isOnline(c);
    const member = c.nac === "approved";
    const needs = !member;
    el.classList.toggle("needs-approval", needs && on);
    el.classList.toggle("is-new", !!c.new);
    el.classList.toggle("offline", !on);
    nameEl.textContent = c.name || c.hostname || c.ip || c.vendor || c.mac;

    // Connection state: green dot = currently connected, grey = offline.
    dot.className = "dot " + (on ? "up" : "unknown");
    dot.title = on ? "Currently connected"
      : "Offline — last seen " + timeAgo(c.lastSeen);
    statusText.textContent = on ? "Connected" : "Offline";
    lastSeen.hidden = on || !c.lastSeen;
    if (!lastSeen.hidden) {
      lastSeen.textContent = "Last seen " + timeAgo(c.lastSeen);
      lastSeen.dataset.ts = c.lastSeen;
    } else {
      lastSeen.removeAttribute("data-ts");
    }

    // Status pill: Approved (green) / New (accent) / Needs approval (red).
    pill.className = "pill nac-pill";
    if (member) { pill.textContent = "Approved"; pill.classList.add("nac-ok"); }
    else if (c.new) { pill.textContent = "New"; pill.classList.add("nac-new"); }
    else { pill.textContent = "Needs approval"; pill.classList.add("nac-blocked"); }

    // Core details on the face: IP + MAC on one line, vendor on the row below.
    meta.textContent = (c.ip ? c.ip + " · " : "") + c.mac;
    vendor.hidden = !c.vendor;
    if (c.vendor) vendor.textContent = c.vendor;

    // Wi-Fi signal strength, colour-coded with a little bar, plus the AP the
    // device is associated with (the strongest-signal source). Offline devices
    // have no live signal.
    sig.hidden = !(on && c.kind === "wifi" && c.signal != null);
    if (!sig.hidden) {
      const tone = signalTone(c.signal);
      const pct = Math.max(0, Math.min(100, Math.round((c.signal + 90) / 60 * 100)));
      sig.innerHTML = `<span class="cc-sig-bar"><i></i></span>` +
        `<span class="cc-sig-val mono ${tone}"></span>` +
        `<span class="cc-sig-ap muted" hidden></span>`;
      $(".cc-sig-val", sig).textContent = `${c.signal} dBm`;
      const bar = $(".cc-sig-bar i", sig);
      bar.style.width = pct + "%";
      bar.className = tone;
      const ap = clientAp(c);
      if (ap) {
        const apEl = $(".cc-sig-ap", sig);
        apEl.hidden = false;
        apEl.textContent = ap;
        apEl.title = "Connected via " + ap;
      }
    }

    // If the card is already expanded, refresh the detail content too so it
    // doesn't go stale while left open; a collapsed card just waits for its
    // next open (see the click handler above).
    if (!detail.hidden) fillClientDetail(detail, c);

    // Actions (icon-only to keep the cards compact): Approve/Revoke, plus
    // Ignore for anything not yet approved, and Edit. Rebuilt each patch —
    // cheap (a couple of icon buttons) and simplest to keep correct as
    // membership flips between Approve/Revoke.
    acts.innerHTML = "";
    const btn = iconBtn(
      member ? ICON_REVOKE : ICON_CHECK,
      member ? "Revoke access" : "Approve",
      member ? () => approveClient(cur, curNac, false, btn)
             : () => openClientEdit(cur, { approve: true }),
      member ? "icon-btn-danger" : "icon-btn-primary");
    acts.appendChild(btn);
    if (needs && on) {
      const ig = iconBtn(ICON_IGNORE, "Ignore — hide until this device connects again");
      ig.onclick = () => ignoreClient(cur, ig);
      acts.appendChild(ig);
    }
    acts.appendChild(iconBtn(ICON_EDIT,
      "Edit — rename, add notes, sync DNS / firewall aliases",
      () => openClientEdit(cur)));
    if (!on) {
      const fg = iconBtn(ICON_TRASH,
        "Forget — delete this device's saved history",
        () => forgetClient(cur, fg), "icon-btn-danger");
      acts.appendChild(fg);
    }
  }
  return { el, patch };
}

// Expanded detail: where the device was seen (per-location signal), first seen.
function fillClientDetail(box, c) {
  box.innerHTML = "";
  const kv = document.createElement("div");
  kv.className = "cc-kv";
  const add = (k, v, ts) => {
    if (v == null || v === "") return;
    const kk = document.createElement("span"); kk.className = "cc-k"; kk.textContent = k;
    const vv = document.createElement("span"); vv.className = "cc-v"; vv.textContent = v;
    if (ts) vv.dataset.ts = ts;  // kept fresh by the 30s relative-time ticker
    kv.append(kk, vv);
  };
  add("Hostname", c.hostname);
  add("IP", c.ip);
  add("MAC", c.mac);
  add("Vendor", c.vendor);
  add("Type", c.kind === "wifi" ? "Wi-Fi" : "Wired");
  if (c.firstSeen) add("First seen", timeAgo(c.firstSeen), c.firstSeen);
  if (!isOnline(c) && c.lastSeen) add("Last seen", timeAgo(c.lastSeen), c.lastSeen);
  add("Notes", c.notes);
  box.appendChild(kv);

  // Firewall aliases this device belongs to (from the client scan).
  if (c.aliases && c.aliases.length) {
    const at = document.createElement("div");
    at.className = "cc-seen-title muted"; at.textContent = "Firewall aliases";
    box.appendChild(at);
    const av = document.createElement("div");
    av.className = "cc-aliases";
    for (const a of c.aliases) {
      const p = document.createElement("span");
      p.className = "pill alias-pill"; p.textContent = a.name;
      av.appendChild(p);
    }
    box.appendChild(av);
  }

  if ((c.seen || []).length || c.via) {
    const t = document.createElement("div");
    t.className = "cc-seen-title muted";
    t.textContent = (c.seen || []).length ? "Seen on" : "Last seen on";
    box.appendChild(t);
    box.appendChild(seenBadges(c));
  }

  // Connection history: the stored connect/disconnect events, fetched on
  // demand so the client list payload stays small.
  const ht = document.createElement("div");
  ht.className = "cc-seen-title muted"; ht.textContent = "Connection history";
  box.appendChild(ht);
  const hist = document.createElement("div");
  hist.className = "cc-history muted";
  hist.textContent = "Loading…";
  box.appendChild(hist);
  api(`/api/clients/history?mac=${encodeURIComponent(c.mac)}`).then((r) => {
    if (!hist.isConnected) return;  // panel collapsed/re-rendered meanwhile
    hist.classList.remove("muted");
    hist.innerHTML = "";
    const evs = (r.events || []).slice(-12).reverse();  // newest first
    if (!evs.length) {
      hist.innerHTML = `<span class="muted">No events recorded yet — history builds up as the network is scanned.</span>`;
      return;
    }
    for (const e of evs) {
      const row = document.createElement("div");
      row.className = "cc-ev";
      const d = document.createElement("span");
      d.className = "cc-ev-dot " + (e.ev === "up" ? "up" : "down");
      const what = document.createElement("span");
      what.textContent = e.ev === "up"
        ? "Connected" + (e.via ? ` via ${e.via}` : "") : "Disconnected";
      const when = document.createElement("span");
      when.className = "cc-ev-when muted";
      when.textContent = timeAgo(e.ts);
      when.dataset.ts = e.ts;  // kept fresh by the 30s relative-time ticker
      row.append(d, what, when);
      hist.appendChild(row);
    }
  }).catch((ex) => {
    if (hist.isConnected) hist.textContent = "Couldn't load history: " + ex.message;
  });
}

async function approveClient(c, nac, approve, btn) {
  await withBusy(btn, null, async () => {
    try {
      await api(`/api/devices/${nac.deviceId}/nac/approve`, {
        method: "POST", body: JSON.stringify({ mac: c.mac, approved: approve }) });
      c.nac = approve ? "approved" : "blocked";
      toastOk(approve ? `${c.hostname || c.mac} approved.`
                      : `${c.hostname || c.mac} revoked.`);
      renderClients();
    } catch (ex) {
      toastErr(ex.message);
    }
  });
}

async function forgetClient(c, btn) {
  const label = c.name || c.hostname || c.mac;
  const ok = await confirmDialog({ title: `Forget “${label}”?`,
    message: "Removes its saved name, notes and connection history. If it " +
      "ever connects again it shows up as a brand-new device.",
    okLabel: "Forget", danger: true });
  if (!ok) return;
  await withBusy(btn, null, async () => {
    try {
      await api("/api/clients/forget", { method: "POST",
        body: JSON.stringify({ mac: c.mac }) });
      CLIENTS.clients = CLIENTS.clients.filter((x) => x.mac !== c.mac);
      toastOk(`${label} forgotten.`);
      renderClients();
    } catch (ex) { toastErr(ex.message); }
  });
}

async function ignoreClient(c, btn) {
  await withBusy(btn, null, async () => {
    try {
      await api("/api/nac/ignore", { method: "POST",
        body: JSON.stringify({ mac: c.mac }) });
      CLIENTS.clients = CLIENTS.clients.filter((x) => x.mac !== c.mac);
      toastOk(`${c.hostname || c.mac} ignored — it'll reappear if it connects again.`);
      renderClients();
    } catch (ex) { toastErr(ex.message); }
  });
}

// ---- edit / approve client modal (hostname, notes, firewall aliases) --------
// One box sets the hostname: it's the device's name in the list AND, when saved,
// its static dnsmasq reservation. Approving a client opens this same modal so
// the hostname + aliases are set at approval time.
let _editClient = null;      // the client being edited
let _editAliases = [];       // [{uuid,name,type,member}] original membership
let _ceApproving = false;    // modal opened from Approve → also add to allow-list
let _ceDnsDomain = "";       // domain suffix for the dnsmasq entry (from Settings)

// Turn a typed name into a valid DNS label (lower-case, hyphen-separated).
function slugHost(s) {
  return (s || "").trim().toLowerCase()
    .replace(/[^a-z0-9-]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 63);
}

function closeClientModal() {
  $("#client-modal").hidden = true;
  popModal();
  _editClient = null; _editAliases = [];
}

// Render the alias tick boxes; `aliases` is [{uuid,name,member}]. Empty →
// a hint pointing to Settings.
function renderCeAliases(aliases) {
  const group = $("#ce-aliases-group"), box = $("#ce-aliases");
  group.hidden = false; box.innerHTML = "";
  if (!aliases.length) {
    const p = document.createElement("p");
    p.className = "muted"; p.style.fontSize = "12px"; p.style.margin = "0";
    p.textContent = "No aliases managed yet — add them in Settings → Network access.";
    box.appendChild(p);
    return;
  }
  for (const a of aliases) {
    const lbl = document.createElement("label"); lbl.className = "ent-item";
    const cb = document.createElement("input");
    cb.type = "checkbox"; cb.dataset.uuid = a.uuid; cb.checked = !!a.member;
    const sp = document.createElement("span"); sp.textContent = a.name || a.uuid;
    lbl.append(cb, sp); box.appendChild(lbl);
  }
}

// Open the edit modal. `opts.approve` means it was opened from the Approve
// button — saving also adds the client to the allow-list.
async function openClientEdit(c, opts = {}) {
  _editClient = c;
  _ceApproving = !!opts.approve;
  $("#ce-title").textContent = (_ceApproving ? "Approve " : "Edit ") +
    (c.hostname || c.name || c.mac);
  $("#ce-sub").textContent = (c.ip ? c.ip + " · " : "") + c.mac;
  // Single box: any name the user already set, else the name seen in the ARP /
  // DHCP scan (slugged to a valid hostname). The user can override it.
  $("#ce-host").value = c.name || slugHost(c.hostname) || "";
  $("#ce-notes").value = c.notes || "";
  $("#ce-err").hidden = true;
  $("#ce-save").textContent = _ceApproving ? "Approve" : "Save";
  $("#ce-aliases-group").hidden = true;
  $("#ce-aliases").innerHTML = "";
  $("#client-modal").hidden = false;
  pushModal($("#client-modal"));
  $("#ce-host").focus(); $("#ce-host").select();

  const nac = (CLIENTS && CLIENTS.nac) || {};
  _ceDnsDomain = (nac.dnsSync && nac.dnsSync.domain) || "";
  updateHostHint();

  // Render alias ticks IMMEDIATELY from the scan data already in memory (each
  // client carries the aliases it belongs to), so membership shows with no delay.
  if (nac.configured) {
    const memberUuids = new Set((c.aliases || []).map((a) => a.uuid));
    _editAliases = (nac.managedAliases || []).map((a) => ({
      uuid: a.uuid, name: a.name, type: a.type, member: memberUuids.has(a.uuid) }));
    renderCeAliases(_editAliases);
  }

  // Then reconcile alias membership against the firewall (authoritative).
  try {
    const m = await api("/api/nac/client/membership", { method: "POST",
      body: JSON.stringify({ mac: c.mac, ip: c.ip || "" }) });
    if (_editClient !== c) return;  // modal closed/re-opened meanwhile
    if (m.configured) {
      _editAliases = m.aliases || _editAliases;
      renderCeAliases(_editAliases);
    }
    if (m.dnsSync && m.dnsSync.domain != null) { _ceDnsDomain = m.dnsSync.domain; updateHostHint(); }
  } catch (ex) {
    // Hostname + aliases still work if the firewall read fails; only warn.
    if (_editClient === c) toastErr("Couldn't refresh alias state: " + ex.message);
  }
}

// Live preview of the DNS name the hostname will be published as.
function updateHostHint() {
  const hint = $("#ce-host-hint");
  const host = slugHost($("#ce-host").value);
  if (!host) { hint.hidden = true; return; }
  const fqdn = _ceDnsDomain ? `${host}.${_ceDnsDomain}` : host;
  hint.textContent = `Saved as a static DNS reservation: ${fqdn} → ${_editClient && _editClient.ip || "this device"}`;
  hint.hidden = false;
}

$("#ce-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const c = _editClient;
  if (!c) return;
  const approving = _ceApproving;
  const err = $("#ce-err"); err.hidden = true;
  const save = $("#ce-save"); save.disabled = true;
  save.textContent = approving ? "Approving…" : "Saving…";
  try {
    const host = slugHost($("#ce-host").value);
    const nac = (CLIENTS && CLIENTS.nac) || {};
    // 1) Approve first (add the MAC to the allow-list) when opened from Approve.
    if (approving && nac.deviceId) {
      await api(`/api/devices/${nac.deviceId}/nac/approve`, {
        method: "POST", body: JSON.stringify({ mac: c.mac, approved: true }) });
      c.nac = "approved";
    }
    // 2) Save the hostname (published as a static dnsmasq reservation), notes,
    //    and any alias-membership changes.
    const aliasChanges = {};
    $$("#ce-aliases input[data-uuid]").forEach((cb) => {
      const orig = _editAliases.find((a) => a.uuid === cb.dataset.uuid);
      if (orig && !!orig.member !== cb.checked) aliasChanges[cb.dataset.uuid] = cb.checked;
    });
    const body = {
      mac: c.mac, ip: c.ip || "",
      name: host, notes: $("#ce-notes").value,
      hostname: host,
      syncDns: host ? true : null,   // publish the hostname; leave DNS alone if blank
      aliasChanges,
    };
    const r = await api("/api/nac/client", { method: "POST", body: JSON.stringify(body) });
    // Reflect saved values locally so the list updates without a full reload.
    c.name = r.name; c.notes = r.notes;
    closeClientModal();
    toastOk(approving ? `${host || c.mac} approved.` : "Saved.");
    renderClients();
  } catch (ex) {
    err.textContent = ex.message; err.hidden = false;
  } finally {
    // Always restore the button so it can never stick on "Saving…" — whether
    // the save succeeded, errored, or timed out.
    save.disabled = false; save.textContent = approving ? "Approve" : "Save";
  }
});

// Live DNS-name preview as the hostname is typed.
$("#ce-host").addEventListener("input", updateHostHint);

document.addEventListener("click", (e) => {
  if (e.target.closest("[data-close-client]")) closeClientModal();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("#client-modal").hidden) closeClientModal();
});

// Banner above the list: the setup CTA when NAC isn't configured, or the
// enforcement master switch once it is. Returns null when there's nothing to
// show (no NAC-capable device on the network).
function nacBanner(nac) {
  if (!nac || !nac.deviceId) return null;
  const box = document.createElement("div");
  box.className = "nac-banner card";
  if (!nac.configured) {
    box.innerHTML = `
      <div class="nac-b-main">
        <h2>Set up Network Access Control</h2>
        <p class="muted">Turn this list into an allow-list: approve the devices
          you trust, and (when you switch enforcement on) everything else is
          blocked at <strong class="nac-dev"></strong>. Nothing changes until you
          approve devices and enable enforcement.</p>
      </div>
      <button class="btn btn-primary nac-setup-btn">Set up</button>`;
    $(".nac-dev", box).textContent = nac.deviceName || "the firewall";
    $(".nac-setup-btn", box).onclick = () => nacSetup(nac);
    return box;
  }
  // Configured against an existing alias (membership-only, the user's own rule
  // enforces): no status box — it carried no actionable info. Cards alone show
  // approved/needs-approval state.
  if (nac.managedExternally) return null;
  // Managed mode → we own the deny rule, so show the enforcement toggle.
  box.classList.toggle("enforcing", !!nac.enforced);
  box.innerHTML = `
    <div class="nac-b-main">
      <h2>Access control <span class="nac-alias pill"></span></h2>
      <p class="muted nac-b-sub"></p>
    </div>
    <div class="nac-b-switch">
      <span class="nac-sw-label"></span>
      <button type="button" class="fw-switch nac-enforce" role="switch"><span class="fw-knob"></span></button>
    </div>`;
  $(".nac-alias", box).textContent = nac.alias || "";
  $(".nac-b-sub", box).textContent = nac.enforced
    ? "Enforcement is ON — only approved devices have network access."
    : "Enforcement is OFF — every device is allowed. Approve your devices, then turn it on.";
  $(".nac-sw-label", box).textContent = nac.enforced ? "Enforcing" : "Off";
  const sw = $(".nac-enforce", box);
  sw.classList.toggle("on", !!nac.enforced);
  sw.setAttribute("aria-checked", String(!!nac.enforced));
  sw.onclick = () => toggleEnforcement(nac, !nac.enforced, sw);
  return box;
}

async function toggleEnforcement(nac, on, sw) {
  if (on) {
    const ok = await confirmDialog({ title: "Turn on enforcement?",
      message: "Default-deny goes live: any device that isn't approved loses " +
        "network access immediately. Make sure everything you rely on is " +
        "approved first.", okLabel: "Turn on", danger: true });
    if (!ok) return;
  }
  sw.disabled = true;
  try {
    const r = await api(`/api/devices/${nac.deviceId}/nac/enforcement`, {
      method: "POST", body: JSON.stringify({ enabled: on }) });
    CLIENTS.nac.enforced = !!(r.device && r.device.nac && r.device.nac.enforced);
    toastOk(CLIENTS.nac.enforced
      ? "Enforcement on — only approved devices have access."
      : "Enforcement off — all devices allowed.");
    renderClients();
  } catch (ex) {
    toastErr(ex.message);
    sw.disabled = false;
  }
}

// Setup entry point: choose whether to reuse an existing firewall alias (safest
// when you already run one, e.g. Network Manager) or create a fresh one.
async function nacSetup(nac, deviceId) {
  const devId = deviceId || (nac && nac.deviceId);
  const mode = await pickDialog({ title: "Set up Network Access Control",
    message: "Control which devices are allowed on your network.",
    items: [
      { value: "existing", label: "Use an existing firewall alias",
        sub: "recommended if you already run one (e.g. Network Manager)" },
      { value: "create", label: "Create a new allow-list",
        sub: "fresh setup — creates the alias and rules for you" },
    ] });
  if (!mode) return;
  return mode === "existing" ? nacSetupExisting(devId) : nacSetupCreate(devId);
}

// Reuse an existing alias — membership-only. HomeLabHQ adds/removes devices in
// the alias you pick; your own firewall rule keeps enforcing it, so nothing can
// be cut off by turning this on.
async function nacSetupExisting(devId) {
  let aliases;
  try {
    aliases = (await api(`/api/devices/${devId}/nac/aliases`)).aliases || [];
  } catch (ex) { toastErr("Couldn't read aliases: " + ex.message); return; }
  if (!aliases.length) {
    toastErr("No aliases found on the firewall — choose “Create a new allow-list” instead.");
    return;
  }
  const pick = await pickDialog({ title: "Choose the alias to manage",
    message: "HomeLabHQ will add or remove devices in this alias. Everything " +
      "already in it stays approved.",
    items: aliases.map((a) => ({ value: a.uuid, label: a.name,
      sub: [a.type, a.description].filter(Boolean).join(" · ") })) });
  if (!pick) return;
  try {
    await api(`/api/devices/${devId}/nac/setup`, { method: "POST",
      body: JSON.stringify({ mode: "existing", existingUuid: pick }) });
    toastOk("Access control linked to your existing alias.");
    CLIENTS = null; loadClients();
    switchTab("clients");
  } catch (ex) { toastErr(ex.message); }
}

// Guided setup for a brand-new allow-list: name the alias, pick the interface,
// choose whether to seed it with everything currently online.
async function nacSetupCreate(devId) {
  const alias = await promptDialog({ title: "Create a new allow-list",
    message: "Name the firewall alias that will hold your approved devices " +
      "(letters, digits and underscore).",
    value: "HLHQ_NAC", okLabel: "Next" });
  if (alias == null) return;
  if (!/^[A-Za-z][A-Za-z0-9_]{0,31}$/.test(alias.trim())) {
    toastErr("Alias must start with a letter; letters, digits and underscore only.");
    return;
  }
  let ifaces;
  try {
    ifaces = (await api(`/api/devices/${devId}/nac/interfaces`)).interfaces || [];
  } catch (ex) { toastErr("Couldn't read interfaces: " + ex.message); return; }
  if (!ifaces.length) { toastErr("No interfaces available on the firewall."); return; }
  const iface = await pickDialog({ title: "Which network to protect?",
    message: "The access rule attaches to this interface — usually your LAN.",
    items: ifaces.map((i) => ({ value: i.value, label: i.label, sub: i.value })) });
  if (!iface) return;
  const seedChoice = await pickDialog({ title: "Seed the allow-list?",
    message: "Approving the devices already online means turning enforcement on " +
      "later won't cut anyone off.",
    items: [
      { value: "seed", label: "Approve all current devices", sub: "recommended" },
      { value: "empty", label: "Start empty", sub: "you'll approve devices yourself" },
    ] });
  if (!seedChoice) return;
  try {
    const r = await api(`/api/devices/${devId}/nac/setup`, { method: "POST",
      body: JSON.stringify({ alias: alias.trim(), interface: iface,
        seedExisting: seedChoice === "seed" }) });
    toastOk(`Access control ready${r.seeded ? ` — ${r.seeded} devices approved` : ""}. ` +
      "Enforcement is off until you turn it on.");
    CLIENTS = null; loadClients();
    switchTab("clients");
  } catch (ex) { toastErr(ex.message); }
}
export { nacSetup };

// Onboarding for the Clients view — this is generic, so anyone running the tool
// on their own network populates it just by adding an AP or managed switch.
function clientsEmptyState(sourceCount) {
  const box = document.createElement("div");
  box.className = "empty";
  box.innerHTML = `
    <div class="empty-mark">◎</div>
    <p><strong>No clients to show yet.</strong></p>
    <p class="muted">The Clients view automatically aggregates every device
      seen by the access points and managed switches you add — hostname, IP,
      MAC, signal and where each one is connected.</p>
    <p class="muted">${sourceCount
      ? "Your client sources are reachable but reported nothing yet — try Refresh in a moment."
      : "Add a Wi-Fi access point or a managed switch to get started."}</p>
    <button class="btn btn-primary" data-goto="add">Add a device</button>`;
  return box;
}

(function bindClients() {
  const input = $("#clients-search");
  const clear = $("#clients-search-clear");
  if (input) {
    input.addEventListener("input", () => {
      CLIENTS_Q = input.value.trim().toLowerCase();
      clear.hidden = !input.value;
      if (CLIENTS) renderClients();
    });
    clear.addEventListener("click", () => {
      input.value = ""; CLIENTS_Q = ""; clear.hidden = true;
      if (CLIENTS) renderClients(); input.focus();
    });
  }
  const status = $("#clients-status");
  if (status) {
    status.addEventListener("change", () => {
      CLIENTS_STATUS = status.value;
      if (CLIENTS) renderClients();
    });
  }
  const sort = $("#clients-sort");
  if (sort) {
    sort.value = CLIENTS_SORT;
    sort.addEventListener("change", () => {
      CLIENTS_SORT = sort.value;
      try { localStorage.setItem("hlhq-clients-sort", CLIENTS_SORT); } catch (_) {}
      if (CLIENTS) renderClients();
    });
  }
  const refresh = $("#clients-refresh");
  if (refresh) refresh.addEventListener("click", () => withBusy(refresh, "↻ Scanning…", async () => {
    // Keep the current cards on screen and just swap in fresh data (a fresh ARP
    // + DHCP-lease read of the firewall), so refresh never blanks the view.
    await loadClients();
  }));
})();

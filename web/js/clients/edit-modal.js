// Edit/approve-client modal: hostname (published as a static dnsmasq
// reservation), notes, and firewall-alias membership. Opened either from a
// card's Edit button or its Approve button (approving also adds the MAC to
// the allow-list on save). Never touches the client grid — this + nac-setup.js
// are the ~300 lines of clients.js that don't (refactor.md 2.2).
"use strict";
import { $, $$, api } from "../api.js";
import { toastErr, toastOk, pushModal, popModal } from "../ui.js";

let _editClient = null;      // the client being edited
let _editAliases = [];       // [{uuid,name,type,member}] original membership
let _ceApproving = false;    // modal opened from Approve → also add to allow-list
let _ceDnsDomain = "";       // domain suffix for the dnsmasq entry (from Settings)
let _editNac = {};            // injected for the current modal run
let _onComplete = () => {};   // injected completion callback

// Turn a typed name into a valid DNS label (lower-case, hyphen-separated).
function slugHost(s) {
  return (s || "").trim().toLowerCase()
    .replace(/[^a-z0-9-]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 63);
}

function closeClientModal() {
  $("#client-modal").hidden = true;
  popModal();
  _editClient = null; _editAliases = []; _editNac = {}; _onComplete = () => {};
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
// `onComplete` is supplied by the owning client module. Keeping it explicit
// prevents this modal from importing mutable roster state and recreating a
// module cycle.
export async function openClientEdit(c, { approve = false, nac = {}, onComplete = () => {} } = {}) {
  _editClient = c;
  _ceApproving = approve;
  _editNac = nac;
  _onComplete = onComplete;
  $("#ce-title").textContent = (_ceApproving ? "Approve " : "Edit ") +
    (c.hostname || c.name || c.mac);
  $("#ce-sub").textContent = (c.ip ? c.ip + " · " : "") + c.mac;
  // Single box: any name the user already set, else the name seen in the ARP /
  // DHCP scan (slugged to a valid hostname). The user can override it.
  $("#ce-host").value = c.name || slugHost(c.hostname) || "";
  $("#ce-notes").value = c.notes || "";
  $("#ce-notify").checked = !!c.notify;
  $("#ce-err").hidden = true;
  $("#ce-save").textContent = _ceApproving ? "Approve" : "Save";
  $("#ce-aliases-group").hidden = true;
  $("#ce-aliases").innerHTML = "";
  $("#client-modal").hidden = false;
  pushModal($("#client-modal"), { onEscape: closeClientModal });
  $("#ce-host").focus(); $("#ce-host").select();

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
    const nac = _editNac;
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
      notify: $("#ce-notify").checked,
      hostname: host,
      syncDns: host ? true : null,   // publish the hostname; leave DNS alone if blank
      aliasChanges,
    };
    const r = await api("/api/nac/client", { method: "POST", body: JSON.stringify(body) });
    // Reflect saved values locally so the list updates without a full reload.
    c.name = r.name; c.notes = r.notes; c.notify = !!r.notify;
    const complete = _onComplete;
    closeClientModal();
    toastOk(approving ? `${host || c.mac} approved.` : "Saved.");
    complete(c);
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
// Escape is handled by ui.js's shared modal-stack router (topmost modal
// first), via the onEscape passed to pushModal in openClientEdit().

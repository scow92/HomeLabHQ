// OPNsense firewall rules section, mirroring Network Manager's toggle list:
// enable/disable a filter rule (applied live), rename its label, add rules
// from the full firewall list, or remove them from this section. Never
// deletes a rule on the firewall. `dm` (current detail-modal state) is
// passed in by the caller.
"use strict";
import { api } from "../api.js";
import { toast, toastErr, toastOk, promptDialog, confirmDialog, pickDialog, withBusy,
         fwIconBtn, ICON_EDIT, ICON_TRASH, detailSection } from "../ui.js";

export function firewallSection(dm) {
  const s = detailSection("Firewall rules");
  const dev = dm.device;
  const fw = dm.detail.firewall || {};
  let rules = (fw.rules || []).map((r) => ({ ...r }));

  const sub = document.createElement("p");
  sub.className = "cz-sub";
  sub.textContent = "Enable or disable OPNsense filter rules (applied live). " +
    "Rename changes the label here only; rules are never deleted.";
  s.appendChild(sub);

  const list = document.createElement("div");
  list.className = "fw-list";
  s.appendChild(list);

  async function saveManaged(next) {
    const res = await api(`/api/devices/${dev.id}/firewall/rules`, {
      method: "POST",
      body: JSON.stringify({ rules: next.map((r) =>
        ({ uuid: r.uuid, name: r.name, renamed: !!r.renamed })) }),
    });
    rules = res.rules || next;
    if (dm.detail.firewall) dm.detail.firewall.rules = rules;
    renderList();
  }

  async function toggleRule(r, sw) {
    const desired = !r.enabled;
    await withBusy(sw, null, async () => {
      try {
        const res = await api(`/api/devices/${dev.id}/firewall/toggle`, {
          method: "POST", body: JSON.stringify({ uuid: r.uuid, enabled: desired }) });
        r.enabled = !!res.enabled;
        toastOk(`Rule ${r.enabled ? "enabled" : "disabled"}.`);
      } catch (ex) { toastErr(ex.message); }
    });
    renderList();
  }

  // The name shown for a rule: the live OPNsense rule name by default, or the
  // user's own label once they've renamed it here.
  function ruleTitle(r) {
    return (r.renamed && r.name) ? r.name : (r.descr || r.name);
  }

  async function renameRule(i) {
    const name = await promptDialog({ title: "Rename rule", value: ruleTitle(rules[i]),
      okLabel: "Save",
      message: "This label is stored here only — the rule on the firewall keeps its own name." });
    if (name == null) return;
    const label = name.trim();
    // A blank label clears the override and falls back to the live rule name.
    const next = rules.map((r, j) => j === i
      ? { ...r, name: label || r.descr || r.name, renamed: !!label }
      : r);
    try { await saveManaged(next); toastOk(label ? "Renamed." : "Reset to firewall name."); }
    catch (ex) { toastErr(ex.message); }
  }

  async function removeRule(i) {
    const ok = await confirmDialog({ title: "Remove from list?",
      message: `“${rules[i].name}” stays on the firewall — this only removes it from this section.`,
      okLabel: "Remove" });
    if (!ok) return;
    const next = rules.filter((_, j) => j !== i);
    try { await saveManaged(next); toastOk("Removed."); } catch (ex) { toastErr(ex.message); }
  }

  function renderList() {
    list.innerHTML = "";
    if (fw.error) {
      const p = document.createElement("p");
      p.className = "muted"; p.style.margin = "0"; p.style.fontSize = "12px";
      p.textContent = "Couldn't read rules: " + fw.error;
      list.appendChild(p);
      return;
    }
    if (!rules.length) {
      list.innerHTML = `<p class="muted" style="margin:0;font-size:12px">No rules yet. Add one below.</p>`;
      return;
    }
    for (const [i, r] of rules.entries()) {
      const row = document.createElement("div");
      row.className = "fw-row";
      const sw = document.createElement("button");
      sw.type = "button";
      sw.className = "fw-switch" + (r.enabled ? " on" : "") +
        (r.enabled == null ? " unknown" : "");
      sw.setAttribute("role", "switch");
      sw.setAttribute("aria-checked", String(!!r.enabled));
      sw.disabled = r.enabled == null;
      sw.title = r.enabled == null ? "State unknown"
        : (r.enabled ? "Enabled — click to disable" : "Disabled — click to enable");
      sw.innerHTML = `<span class="fw-knob"></span>`;
      sw.onclick = () => toggleRule(r, sw);

      const nm = document.createElement("div");
      nm.className = "fw-name";
      const title = document.createElement("span");
      title.className = "fw-title"; title.textContent = ruleTitle(r);
      nm.appendChild(title);
      if (r.error) {
        const e = document.createElement("span");
        e.className = "fw-sub err"; e.textContent = r.error;
        nm.appendChild(e);
      } else if (r.renamed && r.descr && r.descr !== r.name) {
        // User gave it a custom label — show the real firewall name underneath
        // so it's clear which rule this maps to.
        const d = document.createElement("span");
        d.className = "fw-sub";
        const tag = document.createElement("span");
        tag.className = "fw-src"; tag.textContent = "firewall";
        d.append(tag, document.createTextNode(r.descr));
        nm.appendChild(d);
      }

      const acts = document.createElement("div");
      acts.className = "fw-acts";
      const ren = fwIconBtn(ICON_EDIT, "Rename", () => renameRule(i));
      const rm = fwIconBtn(ICON_TRASH, "Remove from list",
        () => removeRule(i), "fw-icon-danger");
      acts.append(ren, rm);

      row.append(sw, nm, acts);
      list.appendChild(row);
    }
  }

  const addRow = document.createElement("div");
  addRow.className = "fw-add";
  const addBtn = document.createElement("button");
  addBtn.className = "btn btn-primary btn-sm"; addBtn.textContent = "Add rule";
  addBtn.onclick = () => withBusy(addBtn, "Loading…", async () => {
    try {
      const data = await api(`/api/devices/${dev.id}/firewall/all`);
      const have = new Set(rules.map((r) => r.uuid));
      const items = (data.rules || []).map((r) => ({
        value: r.uuid, label: r.label,
        sub: (r.enabled ? "enabled" : "disabled") +
          (have.has(r.uuid) ? " · already added" : ""),
      }));
      const pick = await pickDialog({ title: "Add a firewall rule",
        message: "Pick a rule to manage in this section.", items });
      if (!pick) return;
      if (have.has(pick)) { toast("Already in the list.", "warn"); return; }
      const chosen = (data.rules || []).find((r) => r.uuid === pick);
      await saveManaged([...rules, { uuid: pick, name: chosen ? chosen.label : pick }]);
      toastOk("Rule added.");
    } catch (ex) { toastErr(ex.message); }
  });
  addRow.appendChild(addBtn);

  renderList();
  s.appendChild(addRow);
  return s;
}

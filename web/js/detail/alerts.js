// Threshold-alert editor for a device: list existing rules and add new ones.
// Rules fire a push notification when a numeric sensor crosses the threshold.
// `dm` (current detail-modal state) is passed in by the caller.
"use strict";
import { api, labelFor, DETAIL_ENTITY_KEYS } from "../api.js";
import { toast, toastErr, toastOk, detailSection } from "../ui.js";

export function alertsSection(dm) {
  const s = detailSection("Alerts");
  const dev = dm.device;
  dev.alerts = dev.alerts || [];
  // Numeric sensors are the alertable entities; prefer ones with a live value.
  const numeric = dm.entities.filter((e) =>
    e.kind === "sensor" && !DETAIL_ENTITY_KEYS.has(e.key) &&
    (typeof e.value === "number" || /cpu|mem|clients|ports_up|poe|signal|rssi|load|temp|errors|count/i.test(e.key)));
  const nameFor = (k) => {
    const e = dm.entities.find((x) => x.key === k);
    return e ? e.name : labelFor(k);
  };

  const list = document.createElement("div");
  list.className = "alerts-list";
  const renderList = () => {
    list.innerHTML = "";
    if (!dev.alerts.length) {
      list.innerHTML = `<p class="muted" style="margin:0;font-size:12px">No alerts. Add one below to get a push notification when a value crosses a threshold.</p>`;
      return;
    }
    for (const [i, r] of dev.alerts.entries()) {
      const row = document.createElement("div");
      row.className = "alert-row";
      const sign = r.op === "above" ? ">" : "<";
      const txt = document.createElement("span");
      txt.className = "a-txt"; txt.textContent = `${nameFor(r.key)} ${sign} ${r.value}`;
      const del = document.createElement("button");
      del.className = "btn btn-ghost btn-sm"; del.textContent = "Remove";
      del.onclick = async () => {
        const next = dev.alerts.filter((_, j) => j !== i);
        await saveAlerts(next);
      };
      row.append(txt, del);
      list.appendChild(row);
    }
  };
  renderList();
  s.appendChild(list);

  // Add-rule form.
  const form = document.createElement("div");
  form.className = "alert-add";
  const entSel = document.createElement("select");
  if (!numeric.length) entSel.appendChild(new Option("(no numeric sensors)", ""));
  for (const e of numeric) entSel.appendChild(new Option(e.name, e.key));
  const opSel = document.createElement("select");
  opSel.appendChild(new Option("rises above", "above"));
  opSel.appendChild(new Option("drops below", "below"));
  const valIn = document.createElement("input");
  valIn.type = "number"; valIn.step = "any"; valIn.placeholder = "value";
  const addBtn = document.createElement("button");
  addBtn.className = "btn btn-primary btn-sm"; addBtn.textContent = "Add alert";
  addBtn.onclick = async () => {
    const key = entSel.value;
    if (!key) return toast("No numeric sensor to alert on.", "warn");
    if (valIn.value === "") return toast("Enter a threshold value.", "warn");
    const next = [...dev.alerts, { key, op: opSel.value,
      value: Number(valIn.value), label: nameFor(key) }];
    await saveAlerts(next);
    valIn.value = "";
  };
  form.append(entSel, opSel, valIn, addBtn);
  s.appendChild(form);

  async function saveAlerts(next) {
    try {
      const r = await api(`/api/devices/${dev.id}`, {
        method: "PATCH", body: JSON.stringify({ alerts: next }) });
      dev.alerts = (r.device && r.device.alerts) || next;
      renderList();
      toastOk("Alerts updated.");
    } catch (ex) { toastErr(ex.message); }
  }
  return s;
}

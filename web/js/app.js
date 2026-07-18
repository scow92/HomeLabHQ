// Boot + tab routing. The entry point loaded by index.html; every other
// module is reached (directly or transitively) from here.
"use strict";
import { $, $$, api, SESSION, setSession } from "./api.js";
import { initTheme, initThemeBtn } from "./theme.js";
import { startRelativeTimeTicker } from "./ui.js";
import { loadDevices, loadDriverNames, ALL_DEVICES } from "./devices.js";
import { openDevice, closeDevice } from "./detail.js";
import { loadClients } from "./clients.js";
import { initWizard } from "./wizard.js";
import { loadUsers } from "./users.js";
import { loadLogs, stopLogsTimer } from "./logs.js";
import { loadNacConfig } from "./settings.js";

initTheme();
startRelativeTimeTicker();

// ---- auth screen -------------------------------------------------------------
function showAuth(needsSetup) {
  $("#app").hidden = true;
  const screen = $("#auth-screen");
  screen.hidden = false;
  $("#auth-sub").textContent = needsSetup ? "Create the first admin account" : "Sign in";
  $("#auth-submit").textContent = needsSetup ? "Create admin" : "Sign in";
  $("#auth-confirm-field").hidden = !needsSetup;
  $("#auth-pass").autocomplete = needsSetup ? "new-password" : "current-password";
  $("#auth-form").dataset.mode = needsSetup ? "setup" : "login";
  $("#auth-err").hidden = true;
  $("#auth-user").focus();
}

$("#auth-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const mode = e.target.dataset.mode;
  const username = $("#auth-user").value.trim();
  const password = $("#auth-pass").value;
  const err = $("#auth-err");
  err.hidden = true;
  if (mode === "setup" && password !== $("#auth-confirm").value) {
    err.textContent = "Passwords do not match"; err.hidden = false; return;
  }
  const submit = $("#auth-submit");
  const orig = submit.textContent;
  submit.disabled = true;
  submit.textContent = mode === "setup" ? "Creating…" : "Signing in…";
  try {
    await api(mode === "setup" ? "/api/setup" : "/api/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    });
    $("#auth-pass").value = "";
    await boot();
  } catch (ex) {
    err.textContent = ex.message || "Failed"; err.hidden = false;
  } finally {
    submit.disabled = false; submit.textContent = orig;
  }
});

// Show/hide password toggle on the auth screen (login + first-run setup).
(function bindShowPassword() {
  const btn = $("#auth-pass-toggle");
  if (!btn) return;
  const fields = [$("#auth-pass"), $("#auth-confirm")].filter(Boolean);
  btn.addEventListener("click", () => {
    const showing = fields[0].type === "text";
    for (const f of fields) f.type = showing ? "password" : "text";
    btn.setAttribute("aria-pressed", String(!showing));
    btn.textContent = showing ? "Show" : "Hide";
  });
})();

// ---- app shell -------------------------------------------------------------
function showApp() {
  $("#auth-screen").hidden = true;
  $("#app").hidden = false;
  $("#whoami").textContent = `${SESSION.username} · ${SESSION.role}`;
  $$("[data-admin]").forEach((el) => { el.hidden = SESSION.role !== "admin"; });
  loadDriverNames();
  const { tab, deviceId } = tabFromHash();
  switchTab(tab, { fromHash: true });
  if (deviceId) openDeviceById(deviceId);
}

// ---- tabs + hash routing -----------------------------------------------------
// Tabs carry their own URL (#/devices, #/access, …) and the device detail
// modal carries #/device/<id>, so the browser/Android back gesture closes a
// modal or returns to the previous tab instead of exiting the installed PWA —
// and a tab or a specific device is linkable / survives a refresh.
const TAB_HASH = { clients: "access" };
const HASH_TAB = { access: "clients" };

function tabFromHash() {
  const h = location.hash.replace(/^#\/?/, "");
  if (h.startsWith("device/")) return { tab: "devices", deviceId: decodeURIComponent(h.slice(7)) };
  const seg = h.split("/")[0];
  const known = new Set(["devices", "clients", "add", "users", "logs", "settings"]);
  const tab = HASH_TAB[seg] || (known.has(seg) ? seg : "devices");
  return { tab };
}

async function openDeviceById(id) {
  await loadDevices();
  const d = ALL_DEVICES.find((x) => x.id === id);
  if (d) openDevice(d);
  else history.replaceState(null, "", "#/devices");
}

export function switchTab(name, opts = {}) {
  $$(".tab").forEach((t) => {
    const active = t.dataset.tab === name;
    t.classList.toggle("active", active);
    t.setAttribute("aria-selected", String(active));
    t.tabIndex = active ? 0 : -1;
  });
  $$("[data-panel]").forEach((p) => { p.hidden = p.dataset.panel !== name; });
  if (name !== "logs") stopLogsTimer();
  if (name === "devices") loadDevices();
  if (name === "clients") loadClients();
  if (name === "users") loadUsers();
  if (name === "logs") loadLogs();
  if (name === "add") initWizard();
  if (name === "settings") loadNacConfig();
  if (!opts.fromHash) {
    const target = "#/" + (TAB_HASH[name] || name);
    if (location.hash !== target) history.pushState(null, "", target);
  }
}

window.addEventListener("popstate", routeFromHash);
window.addEventListener("hashchange", routeFromHash);
function routeFromHash() {
  if (!SESSION) return;  // boot() routes once login completes
  const { tab, deviceId } = tabFromHash();
  const modal = $("#device-modal");
  if (!deviceId && modal && !modal.hidden) {
    // Back-navigated out of a device deep link — close the modal in place
    // rather than leaving the app on whatever tab it lands on underneath.
    closeDevice();
  }
  switchTab(tab, { fromHash: true });
  if (deviceId) openDeviceById(deviceId);
}

document.addEventListener("click", (e) => {
  const tab = e.target.closest(".tab");
  if (tab) return switchTab(tab.dataset.tab);
  const goto = e.target.closest("[data-goto]");
  if (goto) return switchTab(goto.dataset.goto);
});

// Arrow-key navigation across the tablist (standard tab-widget keyboard
// pattern): Left/Right move + activate, Home/End jump to the ends.
$("#tabs").addEventListener("keydown", (e) => {
  if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) return;
  const tabs = $$(".tab", $("#tabs"));
  const i = tabs.indexOf(document.activeElement);
  if (i === -1) return;
  e.preventDefault();
  const next = e.key === "ArrowRight" ? tabs[(i + 1) % tabs.length]
    : e.key === "ArrowLeft" ? tabs[(i - 1 + tabs.length) % tabs.length]
    : e.key === "Home" ? tabs[0] : tabs[tabs.length - 1];
  next.focus();
  switchTab(next.dataset.tab);
});

$("#logout-btn").addEventListener("click", async () => {
  try { await api("/api/logout", { method: "POST" }); } catch (_) {}
  setSession(null);
  showAuth(false);
});

initThemeBtn();

// ---- boot ------------------------------------------------------------------
async function boot() {
  try {
    const s = await api("/api/session");
    if (s.authenticated) { setSession(s.user); showApp(); }
    else showAuth(s.needsSetup);
  } catch (ex) {
    showAuth(false);
  }
}

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch(() => {});
}
boot();

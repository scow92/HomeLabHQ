// Boot + tab routing. The entry point loaded by index.html; every other
// module is reached (directly or transitively) from here.
"use strict";
import { $, $$, api, SESSION, setSession, onSessionChange, SessionChangedError } from "./api.js";
import { refreshState } from "./refresh-state.js";
import { initTheme, initThemeBtn } from "./theme.js";
import { startRelativeTimeTicker } from "./ui.js";
import { switchTab, initialRoute } from "./router.js";
import { initNotifications, stopNotifications } from "./notifications.js";

initTheme();
startRelativeTimeTicker();

// ---- auth screen -------------------------------------------------------------
function showAuth(needsSetup) {
  $("#auth-form").hidden = false;
  bootState?.reset();
  $("#app").hidden = true;
  $("#whoami").textContent = "";
  const screen = $("#auth-screen");
  screen.hidden = false;
  $("#auth-sub").textContent = needsSetup
    ? "Create the first admin account · use 15+ characters" : "Sign in";
  $("#auth-submit").textContent = needsSetup ? "Create admin" : "Sign in";
  $("#auth-confirm-field").hidden = !needsSetup;
  $("#auth-pass").autocomplete = needsSetup ? "new-password" : "current-password";
  $("#auth-pass").minLength = needsSetup ? 15 : 0;
  $("#auth-form").dataset.mode = needsSetup ? "setup" : "login";
  document.title = `${needsSetup ? "Create admin" : "Sign in"} · HomelabHQ`;
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
  initialRoute();
  initNotifications();
}

// ---- tabs + hash routing -----------------------------------------------------
// Tab switching and hash routing live in router.js; this file just wires the
// tab widget's DOM events to it.
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
  const tabs = $$(".tab", $("#tabs")).filter(tab => !tab.hidden);
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
  setSession(null);
  // Serialize login behind logout so a late logout cannot clear a new cookie.
  $("#auth-submit").disabled = true;
  try { await api("/api/logout", { method: "POST" }); } catch (_) {}
  $("#auth-submit").disabled = false;
});

onSessionChange(() => {
  stopNotifications();
  for (const selector of ["#auth-user", "#auth-pass", "#auth-confirm"]) $(selector).value = "";
  if (SESSION) showApp();
  else showAuth(false);
});

initThemeBtn();

const bootState = refreshState("boot-refresh-state", $("#auth-form"), "Connection", boot);
document.addEventListener("hlhq:session-expired", () => { $("#auth-sub").textContent = "Session expired. Sign in again."; });
// ---- boot ------------------------------------------------------------------
async function boot() {
  bootState.start();
  try {
    const s = await api("/api/session");
    if (s.authenticated) setSession(s.user);
    else { setSession(null); showAuth(s.needsSetup); }
  } catch (ex) {
    if (ex instanceof SessionChangedError) return;
    setSession(null);
    if (ex.status === 401) { showAuth(false); $("#auth-sub").textContent = "Session expired. Sign in again."; return; }
    $("#auth-form").hidden = true;
    $("#auth-sub").textContent = "Unable to connect";
    bootState.start(); bootState.fail(ex);
  }
}

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch(() => {});
}
boot();

// A document restored from the browser's page cache must authenticate again;
// the hidden/cached document must not retain its previous protected snapshot.
window.addEventListener("pagehide", () => setSession(null));
window.addEventListener("pageshow", (event) => { if (event.persisted) boot(); });

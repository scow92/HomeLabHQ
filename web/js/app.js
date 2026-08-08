// Boot + tab routing. The entry point loaded by index.html; every other
// module is reached (directly or transitively) from here.
"use strict";
import { $, $$, api, SESSION, setSession } from "./api.js";
import { initTheme, initThemeBtn } from "./theme.js";
import { startRelativeTimeTicker } from "./ui.js";
import { switchTab, initialRoute } from "./router.js";

initTheme();
startRelativeTimeTicker();

// ---- auth screen -------------------------------------------------------------
function showAuth(needsSetup) {
  $("#app").hidden = true;
  const screen = $("#auth-screen");
  screen.hidden = false;
  $("#auth-sub").textContent = needsSetup
    ? "Create the first admin account · use 15+ characters" : "Sign in";
  $("#auth-submit").textContent = needsSetup ? "Create admin" : "Sign in";
  $("#auth-confirm-field").hidden = !needsSetup;
  $("#auth-pass").autocomplete = needsSetup ? "new-password" : "current-password";
  $("#auth-pass").minLength = needsSetup ? 15 : 0;
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
  initialRoute();
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

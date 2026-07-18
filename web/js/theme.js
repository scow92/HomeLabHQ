// Theme: dark default, light option, auto (OS preference); persisted to
// localStorage. The topbar button opens a 3-option picker instead of silently
// cycling, so the choice is discoverable.
"use strict";
import { $ } from "./api.js";
import { pickDialog, toast } from "./ui.js";

export const THEME_ICON = { auto: "◐", dark: "☾", light: "☀" };
const THEME_LABEL = { auto: "Auto (follow system)", dark: "Dark", light: "Light" };

export function currentTheme() {
  try { return localStorage.getItem("hlhq-theme") || "auto"; } catch (_) { return "auto"; }
}

export function applyTheme(theme) {
  // theme: "dark" | "light" | "auto". "auto" defers to the OS preference.
  const root = document.documentElement;
  if (theme === "auto") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", theme);
  try { localStorage.setItem("hlhq-theme", theme); } catch (_) {}
  const meta = $('meta[name="theme-color"]');
  if (meta) {
    const dark = theme === "dark" ||
      (theme === "auto" && matchMedia("(prefers-color-scheme: dark)").matches);
    meta.setAttribute("content", dark ? "#0b0f14" : "#f4f6f9");
  }
  const btn = $("#theme-btn");
  if (btn) btn.textContent = THEME_ICON[theme] || "◐";
}

export function initTheme() {
  applyTheme(currentTheme());
}

async function openThemeMenu() {
  const current = currentTheme();
  const next = await pickDialog({
    title: "Theme",
    items: ["auto", "dark", "light"].map((v) => ({ value: v, label: THEME_LABEL[v] })),
    current,
  });
  if (next == null || next === current) return;
  applyTheme(next);
  toast(`Theme: ${THEME_LABEL[next]}`, "info", 1500);
}

export function initThemeBtn() {
  const btn = $("#theme-btn");
  if (!btn) return;
  btn.textContent = THEME_ICON[currentTheme()] || "◐";
  btn.setAttribute("aria-haspopup", "true");
  btn.addEventListener("click", openThemeMenu);
}

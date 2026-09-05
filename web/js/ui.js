// Shared UI plumbing: toasts, the prompt/confirm/pick dialog, modal
// open/close + focus-trap/restore, icon buttons, and small render/timer
// helpers reused across the feature modules.
"use strict";
import { $, $$, timeAgo, onSessionChange } from "./api.js";

// ---- toasts (non-blocking notifications, replacing alert()) -----------------
export function toast(msg, type = "info", ms = 4200) {
  const box = $("#toasts");
  if (!box) { if (type === "error") console.error(msg); return; }
  const el = document.createElement("div");
  el.className = "toast toast-" + type;
  el.setAttribute("role", type === "error" ? "alert" : "status");
  const text = document.createElement("span");
  text.className = "toast-msg";
  text.textContent = msg;
  const close = document.createElement("button");
  close.className = "toast-x";
  close.setAttribute("aria-label", "Dismiss");
  close.textContent = "×";
  const dismiss = () => {
    el.classList.add("leaving");
    el.addEventListener("animationend", () => el.remove(), { once: true });
    setTimeout(() => el.remove(), 400);
  };
  close.onclick = dismiss;
  el.append(text, close);
  box.appendChild(el);
  if (ms) setTimeout(dismiss, ms);
  return el;
}
export const toastOk = (m) => toast(m, "ok");
export const toastErr = (m) => toast(m, "error", 7000);

// ---- render errors safely ----------------------------------------------------
// Device-supplied strings (interface names, driver error text) can reach these
// call sites; always render through textContent, never innerHTML, so a hostile
// LAN device can't get a stored-XSS path into an authenticated admin session.
export function renderError(el, msg, className = "auth-err") {
  el.innerHTML = "";
  const p = document.createElement("p");
  p.className = className;
  p.textContent = msg;
  el.appendChild(p);
}

// Persistent labels and field-local validation share one association contract.
let fieldSequence = 0;
export function field(control, caption, help = "") {
  const label = document.createElement("label"); label.className = "field";
  const text = document.createElement("span"); text.textContent = caption;
  text.id = `field-label-${++fieldSequence}`; control.setAttribute("aria-labelledby", text.id);
  label.append(text, control);
  if (help) {
    const hint = document.createElement("small"); hint.className = "muted";
    hint.id = `field-help-${++fieldSequence}`; hint.textContent = help;
    control.setAttribute("aria-describedby", hint.id); label.append(hint);
  }
  return label;
}
export function fieldError(control, message, { focus = true } = {}) {
  let error = control._fieldError;
  if (!error) {
    error = document.createElement("span"); error.className = "field-error";
    error.id = `field-error-${++fieldSequence}`; error.setAttribute("role", "alert");
    control.after(error); control._fieldError = error;
    const described = control.getAttribute("aria-describedby") || "";
    control.setAttribute("aria-describedby", `${described} ${error.id}`.trim());
    control.addEventListener("input", () => { error.textContent = ""; control.removeAttribute("aria-invalid"); });
  }
  error.textContent = message; control.setAttribute("aria-invalid", "true");
  if (focus) control.focus();
}
for (const label of $$("label.field")) {
  const caption = label.querySelector(":scope > span"), control = label.querySelector("input, select, textarea");
  if (!caption || !control || control.hasAttribute("aria-labelledby")) continue;
  caption.id ||= `field-label-${++fieldSequence}`;
  control.setAttribute("aria-labelledby", caption.id);
}
for (const form of $$("#pw-form, #add-user-form, #auth-form")) {
  form.addEventListener("invalid", event => fieldError(event.target, event.target.validationMessage, { focus: false }), true);
}

// ---- busy-button helper -------------------------------------------------------
// Wraps the disable/spin/restore sequence that every action button repeats.
// Restores the button's label and enabled state whether `fn` resolves,
// rejects, or times out.
export async function withBusy(btn, busyLabel, fn) {
  const orig = btn.textContent, wasDisabled = btn.disabled;
  const wasBusy = btn.getAttribute("aria-busy");
  btn.setAttribute("aria-busy", "true");
  btn.disabled = true;
  if (busyLabel) btn.textContent = busyLabel;
  btn.classList.add("spinning");
  try {
    return await fn();
  } finally {
    btn.disabled = wasDisabled;
    if (wasBusy === null) btn.removeAttribute("aria-busy");
    else btn.setAttribute("aria-busy", wasBusy);
    btn.textContent = orig;
    btn.classList.remove("spinning");
  }
}

// ---- focus trap + restore for modals -----------------------------------------
// A small stack so a modal opened from within another modal (the series/pie
// chart popups open on top of the device detail modal) traps Tab correctly and
// unwinds back to the parent's trap on close.
const _modalStack = [];
const _inertBefore = new Map();
let _overflowBefore = "";
function syncModalEnvironment() {
  for (const [el, inert] of _inertBefore) el.inert = inert;
  _inertBefore.clear();
  const top = _modalStack.at(-1)?.el;
  if (!top) { document.body.style.overflow = _overflowBefore; return; }
  document.body.style.overflow = "hidden";
  // Modal roots are body children. Preserve any pre-existing inert state.
  for (const el of document.body.children) {
    if (el === top || el.contains(top)) continue;
    _inertBefore.set(el, el.inert); el.inert = true;
  }
}

function focusableIn(el) {
  return $$('a[href], button:not([disabled]), textarea, input:not([disabled]), ' +
    'select:not([disabled]), [tabindex]:not([tabindex="-1"])', el)
    .filter((n) => n.offsetParent !== null || n === document.activeElement);
}

function trapTab(el, e) {
  const items = focusableIn(el);
  if (!items.length) { e.preventDefault(); el.focus(); return; }
  const first = items[0], last = items[items.length - 1];
  if (e.shiftKey && (document.activeElement === first || !el.contains(document.activeElement))) { e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && (document.activeElement === last || !el.contains(document.activeElement))) { e.preventDefault(); first.focus(); }
}

// Call when a modal becomes visible: remembers the previously-focused element
// (restored on popModal), moves focus inside, and traps Tab within `el`.
// Pass `onEscape` to have Escape close this modal — handled by one shared,
// stack-aware router below, so Escape always closes the topmost modal only
// (a series overlay over the device modal, a dialog over either) instead of
// every open modal wiring its own document-level listener and racing.
export function pushModal(el, { onEscape = null } = {}) {
  if (_modalStack.some(entry => entry.el === el)) return;
  if (!_modalStack.length) _overflowBefore = document.body.style.overflow;
  const prevFocus = document.activeElement;
  const keyHandler = (e) => { if (e.key === "Tab" && _modalStack.at(-1)?.el === el) trapTab(el, e); };
  document.addEventListener("keydown", keyHandler);
  _modalStack.push({ el, prevFocus, keyHandler, onEscape });
  syncModalEnvironment();
  const items = focusableIn(el);
  if (items.length) items[0].focus();
  else { el.setAttribute("tabindex", "-1"); el.focus(); }
}

// Bubble after local widgets: chart inspection may consume Escape first.
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape" || e.defaultPrevented) return;
  const top = _modalStack[_modalStack.length - 1];
  if (!top || !top.onEscape) return;
  e.preventDefault(); e.stopImmediatePropagation();
  top.onEscape();
});

// Call right after a modal is hidden/removed: releases the Tab trap and
// restores focus to whatever opened it.
export function popModal() {
  const top = _modalStack.pop();
  if (!top) return;
  document.removeEventListener("keydown", top.keyHandler);
  syncModalEnvironment();
  if (top.prevFocus && document.contains(top.prevFocus)) top.prevFocus.focus();
}

// A detail replacement/route departure also ends its nested presentations.
export function closeModalChildren(el) {
  const index = _modalStack.findIndex(entry => entry.el === el);
  if (index < 0) return;
  while (_modalStack.length > index + 1) {
    const top = _modalStack[_modalStack.length - 1];
    if (top.onEscape) top.onEscape();
    else { top.el.hidden = true; popModal(); }
  }
}

// A fully dynamic overlay (series-chart / pie-breakdown popups): builds the
// modal shell, wires backdrop-click + Escape + focus trap/restore, and hands
// back {overlay, body, close}. Replaces the two near-identical hand-rolled
// copies these popups used to carry.
export function openOverlay({ title, onClose = null }) {
  const overlay = document.createElement("div");
  overlay.className = "modal series-modal";
  overlay.innerHTML = `
    <div class="modal-backdrop"></div>
    <div class="modal-card series-card" role="dialog" aria-modal="true">
      <div class="modal-head">
        <h2><span></span></h2>
        <div class="modal-head-actions">
          <button type="button" class="btn btn-ghost btn-sm sc-close">Close</button>
        </div>
      </div>
      <div class="series-body"></div>
    </div>`;
  $(".modal-head h2 span", overlay).textContent = title || "Details";
  $('[role="dialog"]', overlay).setAttribute("aria-label", title || "Details");
  document.body.appendChild(overlay);

  function close() {
    if (!overlay.isConnected) return;
    onClose?.();
    popModal();
    overlay.remove();

  }
  $(".modal-backdrop", overlay).onclick = close;
  $(".sc-close", overlay).onclick = close;
  pushModal(overlay, { onEscape: close });
  return { overlay, body: $(".series-body", overlay), close };
}

// ---- promise-based prompt/confirm dialog (replaces native prompt/confirm) ---
let _dialogResolve = null;
onSessionChange(() => {
  $$(".field-error").forEach(error => { error.textContent = ""; });
  $$('[aria-invalid="true"]').forEach(control => control.removeAttribute("aria-invalid"));
  // Cancel confirmations rather than letting old account actions resume.
  if (_dialogResolve) _dialogClose(null);
  while (_modalStack.length) {
    const { el } = _modalStack[_modalStack.length - 1];
    el.hidden = true;
    if (el.classList.contains("series-modal")) el.remove();
    popModal();
  }
  for (const selector of ["#dialog-title", "#dialog-msg", "#dialog-list", "#toasts"]) {
    $(selector).replaceChildren();
  }
  document.body.style.overflow = "";
  delete document.body.dataset.overflowDepth;
});
function _dialogClose(result) {
  const dlg = $("#dialog");
  if (dlg) dlg.hidden = true;
  popModal();
  // Reset transient state so the shared dialog is clean for its next use.
  const listBox = $("#dialog-list");
  if (listBox) { listBox.hidden = true; listBox.innerHTML = ""; }
  const ok = $("#dialog-ok");
  if (ok) { ok.hidden = false; ok.classList.remove("btn-danger-solid"); }
  const input = $("#dialog-input");
  if (input) { input.value = ""; input.type = "text"; }
  const r = _dialogResolve; _dialogResolve = null;
  if (r) r(result);
}
export function promptDialog({ title, message, value = "", placeholder = "",
                               okLabel = "Save", inputType = "text" }) {
  return new Promise((resolve) => {
    _dialogResolve = resolve;
    $("#dialog-title").textContent = title || "";
    const msg = $("#dialog-msg");
    msg.textContent = message || ""; msg.hidden = !message;
    $("#dialog-field").hidden = false;
    $("#dialog-label").textContent = title || "Value";
    const input = $("#dialog-input");
    input.type = inputType;
    input.value = value; input.placeholder = placeholder;
    $("#dialog-ok").textContent = okLabel;
    $("#dialog-cancel").hidden = false;
    const dlg = $("#dialog"); dlg.hidden = false;
    pushModal(dlg, { onEscape: () => _dialogClose(null) });
    input.focus(); input.select();
  });
}
export function confirmDialog({ title, message, okLabel = "Confirm", danger = false }) {
  return new Promise((resolve) => {
    _dialogResolve = resolve;
    $("#dialog-title").textContent = title || "Are you sure?";
    const msg = $("#dialog-msg");
    msg.textContent = message || ""; msg.hidden = !message;
    $("#dialog-field").hidden = true;
    $("#dialog-input").type = "text";
    const ok = $("#dialog-ok");
    ok.textContent = okLabel;
    ok.classList.toggle("btn-danger-solid", danger);
    $("#dialog-cancel").hidden = false;
    const dlg = $("#dialog"); dlg.hidden = false;
    pushModal(dlg, { onEscape: () => _dialogClose(false) });
    ok.focus();
  });
}
// List picker: choose one item from a list of {value,label,sub}. Resolves the
// chosen value, or null on cancel.
export function pickDialog({ title, message, items, current }) {
  return new Promise((resolve) => {
    _dialogResolve = resolve;
    $("#dialog-title").textContent = title || "Choose";
    const msg = $("#dialog-msg");
    msg.textContent = message || ""; msg.hidden = !message;
    $("#dialog-field").hidden = true;
    const listBox = $("#dialog-list");
    listBox.hidden = false;
    listBox.innerHTML = "";
    for (const it of items) {
      const row = document.createElement("button");
      row.type = "button";
      row.className = "dialog-pick" + (it.value === current ? " current" : "");
      row.innerHTML = `<span class="dp-label"></span>` +
        (it.sub ? `<span class="dp-sub"></span>` : "");
      $(".dp-label", row).textContent = it.label +
        (it.value === current ? "  (current)" : "");
      if (it.sub) $(".dp-sub", row).textContent = it.sub;
      row.onclick = () => { listBox.hidden = true; _dialogClose(it.value); };
      listBox.appendChild(row);
    }
    $("#dialog-ok").hidden = true;
    $("#dialog-cancel").hidden = false;
    const dlg = $("#dialog"); dlg.hidden = false;
    pushModal(dlg, { onEscape: () => _dialogClose(null) });
  });
}

(function bindDialog() {
  const form = $("#dialog-form");
  if (!form) return;
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const withInput = !$("#dialog-field").hidden;
    _dialogClose(withInput ? $("#dialog-input").value.trim() : true);
    $("#dialog-ok").classList.remove("btn-danger-solid");
  });
  $$("[data-dialog-cancel]").forEach((el) =>
    el.addEventListener("click", () => {
      const withInput = !$("#dialog-field").hidden;
      _dialogClose(withInput ? null : false);
      $("#dialog-ok").classList.remove("btn-danger-solid");
    }));
  // Escape is handled by the shared modal-stack router (see pushModal) via the
  // onEscape each dialog open passes.
})();

// ---- icon buttons -------------------------------------------------------------
export const ICON_EDIT = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4 12.5-12.5z"/></svg>`;
export const ICON_TRASH = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><line x1="10" y1="11" x2="10" y2="17"/><line x1="14" y1="11" x2="14" y2="17"/></svg>`;
export const ICON_INFO = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>`;
export const ICON_SYNC = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>`;
export const ICON_SETTINGS = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .34 1.88l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06A1.7 1.7 0 0 0 15 19.4a1.7 1.7 0 0 0-1 .6 1.7 1.7 0 0 0-.4 1.1V21a2 2 0 1 1-4 0v-.09A1.7 1.7 0 0 0 8.5 19.4a1.7 1.7 0 0 0-1.88.34l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.7 1.7 0 0 0 4.6 15a1.7 1.7 0 0 0-.6-1 1.7 1.7 0 0 0-1.1-.4H3a2 2 0 1 1 0-4h.09A1.7 1.7 0 0 0 4.6 8.5a1.7 1.7 0 0 0-.34-1.88l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.7 1.7 0 0 0 9 4.6a1.7 1.7 0 0 0 1-.6 1.7 1.7 0 0 0 .4-1.1V3a2 2 0 1 1 4 0v.09A1.7 1.7 0 0 0 15.5 4.6a1.7 1.7 0 0 0 1.88-.34l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.7 1.7 0 0 0 19.4 9c.14.36.36.7.66.94.3.24.68.38 1.07.4H21a2 2 0 1 1 0 4h-.09A1.7 1.7 0 0 0 19.4 15z"/></svg>`;
export const ICON_HISTORY = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 1 0 .85-9.36L1 10"/><polyline points="12 7 12 12 15 14"/></svg>`;
export const ICON_CHECK = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>`;
export const ICON_REVOKE = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="4.93" y1="4.93" x2="19.07" y2="19.07"/></svg>`;
export const ICON_IGNORE = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>`;
export const ICON_UP = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="19" x2="12" y2="5"/><polyline points="5 12 12 5 19 12"/></svg>`;
export const ICON_DOWN = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="5" x2="12" y2="19"/><polyline points="19 12 12 19 5 12"/></svg>`;

// Compact icon-only action button used on both device and client cards.
export function iconBtn(svg, label, onclick, extra) {
  const b = document.createElement("button");
  b.type = "button";
  b.className = "icon-btn" + (extra ? " " + extra : "");
  b.innerHTML = svg;
  b.title = label;
  b.setAttribute("aria-label", label);
  if (onclick) b.onclick = onclick;
  return b;
}
export function fwIconBtn(svg, label, onclick, extra) {
  const b = document.createElement("button");
  b.type = "button";
  b.className = "fw-icon" + (extra ? " " + extra : "");
  b.innerHTML = svg;
  b.title = label;
  b.setAttribute("aria-label", label);
  b.onclick = onclick;
  return b;
}

// ---- keyed list reconciliation --------------------------------------------
// Patches a card grid in place instead of wiping + rebuilding it, so an
// in-progress tap/drag or an expanded card survives a background refresh.
// `cache` is a Map<key, entry> owned by the caller (persists across calls);
// `buildFn(item)` runs once per key and must return an object carrying `.el`;
// `patchFn(entry, item)` runs on every subsequent render for that key.
export function reconcileList(container, cache, items, keyFn, buildFn, patchFn) {
  const seen = new Set();
  let anchor = null;
  for (const item of items) {
    const key = keyFn(item);
    seen.add(key);
    let entry = cache.get(key);
    if (!entry) {
      entry = buildFn(item);
      cache.set(key, entry);
    }
    patchFn(entry, item);  // always patch, including right after build, so a
                            // fresh card starts from the same state a patched
                            // one would (e.g. first/last-aware button state)
    const wantedNext = anchor ? anchor.nextSibling : container.firstChild;
    if (wantedNext !== entry.el) container.insertBefore(entry.el, wantedNext);
    anchor = entry.el;
  }
  for (const [key, entry] of cache) {
    if (!seen.has(key)) { entry.el.remove(); cache.delete(key); }
  }
}

// ---- timer hygiene ----------------------------------------------------------
// Runs `fn` every `ms` while active — active means both "OS-visible tab" and
// whatever `isActive` says (a data-panel name, or a predicate for the odder
// cases like a modal that isn't a tab panel) — and stops cleanly otherwise,
// instead of each screen hand-rolling its own interval + visibility
// bookkeeping. The owner must create a new poll on reactivation. Returns an
// idempotent disposer; onStop invalidates the owner's in-flight read.
const activePolls = new Set();
onSessionChange(() => { for (const dispose of activePolls) dispose(); });
export function visiblePoll(isActive, fn, ms, { onStop = () => {}, afterCompletion = false, immediate = false } = {}) {
  const active = typeof isActive === "function" ? isActive
    : () => { const p = $(`[data-panel="${isActive}"]`); return !!p && !p.hidden; };
  let timer = null, running = false, disposed = false;
  async function tick() {
    if (disposed) return;
    if (!active()) return dispose();
    if (document.visibilityState === "hidden") return stop();
    if (running) return; // Skip missed ticks; never queue catch-up requests.
    running = true;
    if (afterCompletion && timer) { clearInterval(timer); timer = null; }
    try { await fn(); }
    catch (_) { /* Feature callbacks own refresh-error presentation. */ }
    finally {
      running = false;
      if (afterCompletion) start();
    }
  }
  function start() {
    if (!disposed && !timer && (!afterCompletion || !running) && active() && document.visibilityState !== "hidden") {
      timer = setInterval(tick, ms);
    }
  }
  function stop() {
    if (timer) { clearInterval(timer); timer = null; }
    onStop();
  }
  const onVisible = () => {
    if (document.visibilityState === "hidden") stop();
    else start();
  };
  function dispose() {
    if (disposed) return;
    disposed = true;
    stop(); document.removeEventListener("visibilitychange", onVisible);
    activePolls.delete(dispose);
  }
  document.addEventListener("visibilitychange", onVisible);
  activePolls.add(dispose);
  if (immediate) tick();
  else start();
  return dispose;
}

// ---- relative-time ticker ----------------------------------------------------
// "updated 42s ago" / "First seen …" labels otherwise only change when fresh
// data arrives. Tag any such element with data-ts="<unix seconds>" (and
// optionally data-ts-prefix="updated ") and this keeps it honest between
// refreshes without touching the rest of the card.
const RELTIME_TICK_MS = 30000;

export function startRelativeTimeTicker(ms = RELTIME_TICK_MS) {
  function tick() {
    if (document.visibilityState === "hidden") return;
    for (const el of $$("[data-ts]")) {
      const ts = Number(el.dataset.ts);
      if (!ts) continue;
      el.textContent = (el.dataset.tsPrefix || "") + timeAgo(ts);
    }
  }
  setInterval(tick, ms);
}

// ---- detail-modal section shell ------------------------------------------------
// A titled `.detail-section` block — shared by the device-detail submodules
// (metrics/tables/firewall/alerts/…) so none of them need to import each
// other just for this.
export function detailSection(title) {
  const s = document.createElement("div");
  s.className = "detail-section";
  s.innerHTML = `<h3></h3>`;
  $("h3", s).textContent = title;
  return s;
}

// ---- generic data table -------------------------------------------------------
// The header/body shell shared by every driver/client table (clientsTable,
// detailTable, ifTable, …), which otherwise each hand-roll the same
// thead/tbody/th-label loop with small variations.
// `cellFn(td, row, col)` fills in one cell (textContent, class, click
// handlers, an appended child — whatever that table needs); `extraHeadCols`
// are trailing `<th>`s (e.g. "Rate ↓↑", a blank actions column) with no
// matching `cols` entry, filled in per-row by `rowExtra(tr, row)`. Returns
// `{wrap, table, tbody}` so a caller can post-process rows (radiosTable's
// expandable chart row, ifTable's has-history/selected classes, …).
export function buildTable({ cols, rows, cellFn, extraHeadCols = [], rowExtra,
                              tableClass = "", wrapClass = "detail-table-wrap" }) {
  const wrap = document.createElement("div");
  wrap.className = wrapClass;
  const table = document.createElement("table");
  table.className = ("detail-table " + tableClass).trim();
  const thead = document.createElement("thead");
  const htr = document.createElement("tr");
  for (const c of cols) {
    const th = document.createElement("th");
    th.textContent = c.label + (c.unit ? ` (${c.unit})` : "");
    htr.appendChild(th);
  }
  for (const h of extraHeadCols) {
    const th = document.createElement("th");
    if (h && h.className) th.className = h.className;
    th.textContent = (h && h.label) || "";
    htr.appendChild(th);
  }
  thead.appendChild(htr);
  table.appendChild(thead);
  const tbody = document.createElement("tbody");
  for (const row of rows) {
    const tr = document.createElement("tr");
    for (const c of cols) {
      const td = document.createElement("td");
      cellFn(td, row, c);
      tr.appendChild(td);
    }
    if (rowExtra) rowExtra(tr, row);
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  wrap.appendChild(table);
  return { wrap, table, tbody };
}

// ---- skeleton loading placeholders --------------------------------------------
// Purely decorative — aria-hidden so a screen reader doesn't announce a wall
// of blank lines while waiting on a request.
export function skeletonRows(n = 4) {
  const wrap = document.createElement("div");
  wrap.className = "skeleton";
  wrap.setAttribute("aria-hidden", "true");
  for (let i = 0; i < n; i++) {
    const line = document.createElement("div");
    line.className = "skeleton-line";
    line.style.width = Math.round(55 + Math.random() * 40) + "%";
    wrap.appendChild(line);
  }
  return wrap;
}
export function skeletonCards(n = 3) {
  const wrap = document.createElement("div");
  wrap.className = "cards";
  wrap.setAttribute("aria-hidden", "true");
  for (let i = 0; i < n; i++) {
    const card = document.createElement("div");
    card.className = "skeleton-card";
    for (let j = 0; j < 3; j++) {
      const line = document.createElement("div");
      line.className = "skeleton-line";
      line.style.width = j === 0 ? "55%" : Math.round(70 + Math.random() * 20) + "%";
      card.appendChild(line);
    }
    wrap.appendChild(card);
  }
  return wrap;
}

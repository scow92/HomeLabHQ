// Shared UI plumbing: toasts, the prompt/confirm/pick dialog, modal
// open/close + focus-trap/restore, icon buttons, and small render/timer
// helpers reused across the feature modules.
"use strict";
import { $, $$, timeAgo } from "./api.js";

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

// ---- busy-button helper -------------------------------------------------------
// Wraps the disable/spin/restore sequence that every action button repeats.
// Restores the button's label and enabled state whether `fn` resolves,
// rejects, or times out.
export async function withBusy(btn, busyLabel, fn) {
  const orig = btn.textContent;
  btn.disabled = true;
  if (busyLabel) btn.textContent = busyLabel;
  btn.classList.add("spinning");
  try {
    return await fn();
  } finally {
    btn.disabled = false;
    btn.textContent = orig;
    btn.classList.remove("spinning");
  }
}

// ---- focus trap + restore for modals -----------------------------------------
// A small stack so a modal opened from within another modal (the series/pie
// chart popups open on top of the device detail modal) traps Tab correctly and
// unwinds back to the parent's trap on close.
const _modalStack = [];

function focusableIn(el) {
  return $$('a[href], button:not([disabled]), textarea, input:not([disabled]), ' +
    'select:not([disabled]), [tabindex]:not([tabindex="-1"])', el)
    .filter((n) => n.offsetParent !== null || n === document.activeElement);
}

function trapTab(el, e) {
  const items = focusableIn(el);
  if (!items.length) return;
  const first = items[0], last = items[items.length - 1];
  if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
}

// Call when a modal becomes visible: remembers the previously-focused element
// (restored on popModal), moves focus inside, and traps Tab within `el`.
export function pushModal(el) {
  const prevFocus = document.activeElement;
  const keyHandler = (e) => { if (e.key === "Tab") trapTab(el, e); };
  document.addEventListener("keydown", keyHandler);
  _modalStack.push({ el, prevFocus, keyHandler });
  const items = focusableIn(el);
  if (items.length) items[0].focus();
  else { el.setAttribute("tabindex", "-1"); el.focus(); }
}

// Call right after a modal is hidden/removed: releases the Tab trap and
// restores focus to whatever opened it.
export function popModal() {
  const top = _modalStack.pop();
  if (!top) return;
  document.removeEventListener("keydown", top.keyHandler);
  if (top.prevFocus && document.contains(top.prevFocus)) top.prevFocus.focus();
}

// A fully dynamic overlay (series-chart / pie-breakdown popups): builds the
// modal shell, wires backdrop-click + Escape + focus trap/restore, and hands
// back {overlay, body, close}. Replaces the two near-identical hand-rolled
// copies these popups used to carry.
export function openOverlay({ title }) {
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
  $(".modal-head h2 span", overlay).textContent = title || "";
  document.body.appendChild(overlay);
  document.body.style.overflow = "hidden";
  const prevBodyOverflow = document.body.dataset.overflowDepth
    ? Number(document.body.dataset.overflowDepth) : 0;
  document.body.dataset.overflowDepth = String(prevBodyOverflow + 1);

  const onEsc = (ev) => { if (ev.key === "Escape") close(); };
  function close() {
    document.removeEventListener("keydown", onEsc);
    popModal();
    overlay.remove();
    const depth = Math.max(0, (Number(document.body.dataset.overflowDepth) || 1) - 1);
    document.body.dataset.overflowDepth = String(depth);
    if (!depth) { document.body.style.overflow = ""; delete document.body.dataset.overflowDepth; }
  }
  $(".modal-backdrop", overlay).onclick = close;
  $(".sc-close", overlay).onclick = close;
  document.addEventListener("keydown", onEsc);
  pushModal(overlay);
  return { overlay, body: $(".series-body", overlay), close };
}

// ---- promise-based prompt/confirm dialog (replaces native prompt/confirm) ---
let _dialogResolve = null;
function _dialogClose(result) {
  const dlg = $("#dialog");
  if (dlg) dlg.hidden = true;
  document.body.style.removeProperty("overflow");
  popModal();
  // Reset transient state so the shared dialog is clean for its next use.
  const listBox = $("#dialog-list");
  if (listBox) { listBox.hidden = true; listBox.innerHTML = ""; }
  const ok = $("#dialog-ok");
  if (ok) { ok.hidden = false; ok.classList.remove("btn-danger-solid"); }
  const r = _dialogResolve; _dialogResolve = null;
  if (r) r(result);
}
export function promptDialog({ title, message, value = "", placeholder = "", okLabel = "Save" }) {
  return new Promise((resolve) => {
    _dialogResolve = resolve;
    $("#dialog-title").textContent = title || "";
    const msg = $("#dialog-msg");
    msg.textContent = message || ""; msg.hidden = !message;
    $("#dialog-field").hidden = false;
    const input = $("#dialog-input");
    input.value = value; input.placeholder = placeholder;
    $("#dialog-ok").textContent = okLabel;
    $("#dialog-cancel").hidden = false;
    const dlg = $("#dialog"); dlg.hidden = false;
    document.body.style.overflow = "hidden";
    pushModal(dlg);
    setTimeout(() => { input.focus(); input.select(); }, 30);
  });
}
export function confirmDialog({ title, message, okLabel = "Confirm", danger = false }) {
  return new Promise((resolve) => {
    _dialogResolve = resolve;
    $("#dialog-title").textContent = title || "Are you sure?";
    const msg = $("#dialog-msg");
    msg.textContent = message || ""; msg.hidden = !message;
    $("#dialog-field").hidden = true;
    const ok = $("#dialog-ok");
    ok.textContent = okLabel;
    ok.classList.toggle("btn-danger-solid", danger);
    $("#dialog-cancel").hidden = false;
    const dlg = $("#dialog"); dlg.hidden = false;
    document.body.style.overflow = "hidden";
    pushModal(dlg);
    setTimeout(() => ok.focus(), 30);
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
    document.body.style.overflow = "hidden";
    pushModal(dlg);
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
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("#dialog").hidden) {
      const withInput = !$("#dialog-field").hidden;
      _dialogClose(withInput ? null : false);
    }
  });
})();

// ---- icon buttons -------------------------------------------------------------
export const ICON_EDIT = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4 12.5-12.5z"/></svg>`;
export const ICON_TRASH = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><line x1="10" y1="11" x2="10" y2="17"/><line x1="14" y1="11" x2="14" y2="17"/></svg>`;
export const ICON_INFO = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>`;
export const ICON_SYNC = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>`;
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
// bookkeeping. Returns a stop() you can call early.
export function visiblePoll(isActive, fn, ms) {
  const active = typeof isActive === "function" ? isActive
    : () => { const p = $(`[data-panel="${isActive}"]`); return !!p && !p.hidden; };
  let timer = null;
  function tick() {
    if (!active() || document.visibilityState === "hidden") return stop();
    fn();
  }
  function start() {
    stop();
    timer = setInterval(tick, ms);
  }
  function stop() {
    if (timer) { clearInterval(timer); timer = null; }
  }
  const onVisible = () => {
    if (document.visibilityState === "hidden") stop();
    else if (!timer && active()) start();
  };
  document.addEventListener("visibilitychange", onVisible);
  start();
  return () => { stop(); document.removeEventListener("visibilitychange", onVisible); };
}

// ---- relative-time ticker ----------------------------------------------------
// "updated 42s ago" / "First seen …" labels otherwise only change when fresh
// data arrives. Tag any such element with data-ts="<unix seconds>" (and
// optionally data-ts-prefix="updated ") and this keeps it honest between
// refreshes without touching the rest of the card.
export function startRelativeTimeTicker(ms = 30000) {
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

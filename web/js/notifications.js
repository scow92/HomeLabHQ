// Persistent notification centre and backend-authoritative PWA badge.
"use strict";
import { refreshState, canceled } from "./refresh-state.js";
import { requestOwner } from "./request-owner.js";
import { $, api, timeAgo, SESSION } from "./api.js";

const requests = requestOwner();
let pendingRefresh = null;
const notificationState = refreshState("notifications-refresh-state", $("#notification-list"), "Notifications", refreshNotifications);
function showFailure(error) {
  if (canceled(error)) return;
  notificationState.fail(error);
  $("#notification-toggle").dataset.degraded = "true";
  $("#notification-toggle").setAttribute("aria-label", "Notifications, refresh unavailable");
}
let notifications = [];
let unreadCount = 0;
let refreshTimer = null;
let initialized = false;
let requestSequence = 0;

export function reconcileAppBadge(count) {
  const normalized = Math.max(0, Number(count) || 0);
  const operation = normalized
    ? navigator.setAppBadge?.(normalized)
    : navigator.clearAppBadge?.();
  if (operation?.catch) operation.catch(() => {});
}

function updateCount(count) {
  unreadCount = Math.max(0, Number(count) || 0);
  const badge = $("#notification-badge");
  badge.hidden = unreadCount === 0;
  badge.textContent = unreadCount > 99 ? "99+" : String(unreadCount);
  const toggle = $("#notification-toggle");
  toggle.setAttribute("aria-label", unreadCount
    ? `Notifications, ${unreadCount} unread` : "Notifications, none unread");
  $("#notification-summary").textContent = unreadCount
    ? `${unreadCount} unread` : "No unread notifications";
  $("#notification-read-all").hidden = unreadCount === 0;
  reconcileAppBadge(unreadCount);
}

function notificationUrl(item) {
  const value = item.data?.url;
  if (typeof value !== "string" || !value.startsWith("/")) return null;
  return value;
}

async function mutate(item, action) {
  const result = await api(`/api/notifications/${encodeURIComponent(item.id)}/${action}`, {
    method: "POST", body: "{}",
  });
  if (action === "dismiss") {
    notifications = notifications.filter((candidate) => candidate.id !== item.id);
  } else {
    item.readAt = result.notification?.readAt || Math.floor(Date.now() / 1000);
  }
  updateCount(result.unreadCount);
  renderNotifications();
  return result;
}

function renderNotifications() {
  const list = $("#notification-list");
  list.innerHTML = "";
  if (!notifications.length) {
    const empty = document.createElement("p");
    empty.className = "notification-empty muted";
    empty.textContent = "No notifications yet.";
    list.appendChild(empty);
    return;
  }
  for (const item of notifications) {
    const row = document.createElement("article");
    row.className = `notification-item${item.readAt ? "" : " unread"}`;
    row.setAttribute("role", "listitem");

    const url = notificationUrl(item);
    const content = document.createElement(url ? "a" : "button");
    content.className = "notification-content";
    if (url) content.href = url;
    else content.type = "button";
    const title = document.createElement("strong"); title.textContent = item.title || "HomelabHQ";
    const body = document.createElement("span"); body.textContent = item.body || "";
    content.append(title, body);
    content.onclick = async (event) => {
      if (url) event.preventDefault();
      try {
        if (!item.readAt) await mutate(item, "read");
      } catch (error) {
        showFailure(error);
      }
      if (url) location.assign(url);
    };

    const actions = document.createElement("div"); actions.className = "notification-actions";
    if (!item.readAt) {
      const read = document.createElement("button");
      read.type = "button"; read.className = "btn btn-ghost btn-sm"; read.textContent = "Read";
      read.setAttribute("aria-label", `Mark ${item.title || "notification"} read`);
      read.onclick = () => mutate(item, "read").catch((error) =>
        showFailure(error));
      actions.appendChild(read);
    }
    const dismiss = document.createElement("button");
    dismiss.type = "button"; dismiss.className = "btn btn-ghost btn-sm"; dismiss.textContent = "Dismiss";
    dismiss.setAttribute("aria-label", `Dismiss ${item.title || "notification"}`);
    dismiss.onclick = () => mutate(item, "dismiss").catch((error) =>
      showFailure(error));
    actions.appendChild(dismiss);

    const meta = document.createElement("div"); meta.className = "notification-meta";
    const category = document.createElement("span");
    category.textContent = String(item.category || "general").replaceAll("_", " ");
    const when = document.createElement("span");
    when.textContent = timeAgo(item.createdAt); when.dataset.ts = item.createdAt;
    meta.append(category, when);
    row.append(content, actions, meta); list.appendChild(row);
  }
}

export async function refreshNotifications() {
  if (!SESSION || pendingRefresh?.current()) return;
  const sequence = ++requestSequence;
  const request = requests.begin(() => sequence === requestSequence);
  pendingRefresh = request; notificationState.start();
  try {
    const result = await api("/api/notifications?limit=50", request);
    if (sequence !== requestSequence) return;
    notifications = result.notifications || [];
    updateCount(result.unreadCount);
    renderNotifications(); notificationState.success();
    delete $("#notification-toggle").dataset.degraded;
  } catch (error) {
    if (request.current()) showFailure(error);
  } finally { if (pendingRefresh === request) pendingRefresh = null; }
}

function setPanel(open) {
  const panel = $("#notification-panel");
  panel.hidden = !open;
  $("#notification-toggle").setAttribute("aria-expanded", String(open));
  if (open) refreshNotifications({ silent: true });
}

export function stopNotifications() {
  requests.invalidate(); pendingRefresh = null;
  notificationState.reset(); delete $("#notification-toggle").dataset.degraded;
  clearInterval(refreshTimer); refreshTimer = null;
  notifications = []; requestSequence += 1;
  updateCount(0); renderNotifications(); setPanel(false);
}

export function initNotifications() {
  if (!initialized) {
    initialized = true;
    $("#notification-toggle").addEventListener("click", () => {
      setPanel($("#notification-panel").hidden);
    });
    $("#notification-read-all").addEventListener("click", async () => {
      try {
        const result = await api("/api/notifications/read-all", { method: "POST", body: "{}" });
        notifications.forEach((item) => { item.readAt ||= Math.floor(Date.now() / 1000); });
        updateCount(result.unreadCount); renderNotifications();
      } catch (error) {
        showFailure(error);
      }
    });
    document.addEventListener("click", (event) => {
      if (!event.target.closest(".notification-centre")) setPanel(false);
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && !$("#notification-panel").hidden) {
        setPanel(false); $("#notification-toggle").focus();
      }
    });
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) refreshNotifications({ silent: true });
    });
    window.addEventListener("focus", () => refreshNotifications({ silent: true }));
    navigator.serviceWorker?.addEventListener("message", (event) => {
      if (event.data?.type === "homelabhq-notification") {
        refreshNotifications({ silent: true });
      }
    });
  }
  clearInterval(refreshTimer);
  refreshTimer = setInterval(() => refreshNotifications({ silent: true }), 60000);
  refreshNotifications();
}

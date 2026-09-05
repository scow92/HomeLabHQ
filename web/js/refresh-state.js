// Feedback belongs to a data region; request/session owners decide who may commit.
// Retrieval time is explicitly separate from a source's observation timestamp.
import { onSessionChange, timeAgo } from "./api.js";

const regions = new Set();
export const canceled = error => ["AbortError", "SessionChangedError", "RequestSupersededError"].includes(error?.name);
export function safeFailure(error) {
  if (navigator.onLine === false) return "Browser reports offline. Check connectivity and retry.";
  if (error?.name === "TimeoutError") return "Request timed out. Retry when the service responds.";
  if (error?.status >= 500) return "Service unavailable. Please retry.";
  if (error?.status === 403) return "This request is not permitted.";
  return "Request failed. Check connectivity and retry.";
}

export function refreshState(id, anchor, label, retry) {
  const el = document.createElement("div"); el.id = id; el.className = "refresh-state"; el.hidden = true;
  const message = document.createElement("span"); message.setAttribute("role", "status");
  const age = document.createElement("span"); age.className = "refresh-age muted";
  const time = document.createElement("time");
  const button = document.createElement("button"); button.type = "button"; button.className = "btn btn-ghost btn-sm"; button.textContent = "Retry";
  const diagnostics = document.createElement("details"), summary = document.createElement("summary"), diagnosticText = document.createElement("span");
  summary.textContent = "Diagnostics"; diagnostics.append(summary, diagnosticText);
  age.append("Last retrieved ", time); el.append(message, age, button, diagnostics);
  anchor.before(el);
  const priorDescription = anchor.getAttribute("aria-describedby");
  anchor.setAttribute("aria-describedby", [priorDescription, id].filter(Boolean).join(" "));
  let lastSuccess = null, failure = null, busy = false;
  function render() {
    const state = failure ? (lastSuccess === null ? "error" : "stale") : busy ? "refreshing" : "current";
    el.dataset.state = state;
    const text = `${label}: ${failure ? `${lastSuccess === null ? "Could not load. " : "Last-known data; refresh failed. "}${safeFailure(failure)}`
      : busy ? "Refreshing…" : "Retrieved successfully."}`;
    if (message.textContent !== text) message.textContent = text;
    age.hidden = lastSuccess === null;
    if (lastSuccess !== null) {
      time.dateTime = new Date(lastSuccess).toISOString();
      time.dataset.ts = String(lastSuccess / 1000);
      time.textContent = timeAgo(lastSuccess / 1000);
      time.title = time.dateTime;
    }
    button.hidden = !failure; button.disabled = busy;
    button.setAttribute("aria-label", `Retry`);
    const requestId = failure?.requestId;
    diagnostics.hidden = !failure || (!requestId && !failure.status);
    diagnosticText.textContent = failure ? [Number.isInteger(failure.status) ? `HTTP ${failure.status}` : "",
      /^[a-zA-Z0-9-]{1,64}$/.test(requestId || "") ? `Request ${requestId}` : ""].filter(Boolean).join(" · ") : "";
  }
  const state = {
    el,
    start() { busy = true; el.hidden = false; render(); },
    success() { lastSuccess = Date.now(); failure = null; busy = false; el.hidden = false; render(); },
    fail(error) { busy = false; if (!canceled(error)) failure = { name: error?.name, status: error?.status,
      requestId: /^[a-zA-Z0-9-]{1,64}$/.test(error?.requestId || "") ? error.requestId : null }; render(); },
    reset() { lastSuccess = null; failure = null; busy = false; el.hidden = true; message.textContent = ""; time.textContent = ""; time.removeAttribute("datetime"); time.removeAttribute("data-ts"); diagnosticText.textContent = ""; },
    get hasData() { return lastSuccess !== null; },
  };
  button.onclick = async () => { if (busy) return; try { await retry(); } catch (error) { state.fail(error); } };
  regions.add(state);
  return state;
}
onSessionChange(() => { for (const region of regions) region.reset(); });

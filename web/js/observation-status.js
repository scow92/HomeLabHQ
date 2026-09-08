// Classify only the current authorized device payload; never read global status.
export function observedAt(state) {
  for (const value of [state?.reachabilityCheckedAt, state?.sourceCheckedAt, state?.ts]) {
    const seconds = typeof value === "number" ? value : typeof value === "string" ? Date.parse(value) / 1000 : NaN;
    if (Number.isFinite(seconds) && seconds > 0) return seconds;
  }
  return null;
}
export function deviceObservation(device, now = Date.now() / 1000) {
  const state = device?.state, observed = observedAt(state);
  const online = state?.confirmedOnline ?? state?.online;
  if (!state || observed === null || typeof online !== "boolean") return "unknown";
  if (!online) return "offline";
  const threshold = device.monitoringStaleAfterSeconds;
  // Old servers without authoritative policy cannot establish freshness.
  if (!(typeof threshold === "number" && threshold > 0 && Number.isFinite(threshold))) return "unknown";
  if (now - observed > threshold) return "stale";
  return state.online === false ? "degraded" : "online";
}

export function workloadObservation(instance) {
  if (["stale", "unavailable"].includes(instance.discoveryState)) return "stale";
  return instance.discoveryState === "current" ? "current" : "unknown";
}

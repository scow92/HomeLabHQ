// Transport boundary for the Clients feature.
"use strict";
import { api } from "../api.js";

export const fetchClients = (request = {}) => api("/api/clients", request);
export const refreshClients = (request = {}) => api("/api/clients/refresh", {
  ...request,
  method: "POST", body: "{}", timeoutMs: 45000,
});
export const fetchClientHistory = (mac) =>
  api(`/api/clients/history?mac=${encodeURIComponent(mac)}`);
export const fetchClientEventSummary = (since, request = {}) => api(`/api/clients/events?since=${since}`, request);
export const setClientApproval = (deviceId, macs, approved) =>
  api(`/api/devices/${deviceId}/nac/approve`, {
    method: "POST", body: JSON.stringify({ ...(Array.isArray(macs) ? { macs } : { mac: macs }), approved }),
  });
export const forgetClients = (macs) => api("/api/clients/forget", {
  method: "POST", body: JSON.stringify(Array.isArray(macs) ? { macs } : { mac: macs }),
});
export const ignoreClient = (mac) => api("/api/nac/ignore", {
  method: "POST", body: JSON.stringify({ mac }),
});
export const setEnforcement = (deviceId, enabled) =>
  api(`/api/devices/${deviceId}/nac/enforcement`, {
    method: "POST", body: JSON.stringify({ enabled }),
  });

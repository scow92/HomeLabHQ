// The client feature has one state owner. Other client modules receive values
// and callbacks; they never import or mutate this module's state directly.
"use strict";

let roster = null;

export function getClients() { return roster; }
export function setClients(value) { roster = value; return roster; }
export function invalidateClients() { roster = null; }

export function removeClient(mac) {
  if (!roster) return;
  roster.clients = roster.clients.filter((client) => client.mac !== mac);
}

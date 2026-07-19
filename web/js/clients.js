// Compatibility entry point for integrations that still import ./clients.js.
// New code imports the focused feature entry point directly.
export { loadClients, renderClients, startAccessBadge, invalidateClients } from "./clients/index.js";

# Security boundaries

HomelabHQ is intended for a trusted private network. It stores infrastructure
credentials and lets authenticated users connect to network devices, so it is
not a hostile multi-tenant boundary. Give accounts only to people who are
allowed to manage devices on the networks the container can reach.

## Safe deployment

- Keep a new instance on a trusted LAN and create the first administrator
  before exposing its published port more broadly. The setup endpoint is
  intentionally available without authentication only while no user exists.
- Use built-in TLS or a trusted TLS-terminating reverse proxy. When the proxy
  forwards plain HTTP, configure `HLHQ_EXTERNAL_HTTPS=1`; enable
  `HLHQ_TRUST_PROXY=1` only if it removes client forwarding headers before
  setting its own.
- Back up the complete data directory. Device credentials are encrypted, but
  the decryption key, TLS private key, and VAPID private key are stored under
  `<data-dir>/secrets/` and are required for recovery.
- Keep the container's egress limited to the management networks and services
  it needs when accounts are not equally trusted. Host firewall or container
  network policy is the appropriate enforcement boundary.

## Device-connection boundary

Adding or polling a device causes HomelabHQ to connect to its configured host.
Cross-host HTTP redirects are rejected before credentials are sent, sensitive
headers are removed on origin changes, and SSH host keys use trust on first use
with mismatch rejection.

HomelabHQ does not apply a default destination CIDR allowlist. A safe universal
default would block legitimate private IPv4, IPv6, DNS, and mDNS device setups.
Deployments that include less-trusted accounts should enforce an egress
allowlist outside the application rather than treating application validation
as a network sandbox.

## Deliberate compatibility choices

- Initial setup does not require a separate bootstrap token. Requiring one by
  default would add an out-of-band recovery step to every first run. Network
  exposure before setup therefore remains an operator-controlled boundary.
- Passwords require at least 15 characters and use self-describing scrypt
  hashes. The work factor is not raised without measurements on supported
  low-power homelab hardware; an unmeasured increase could make login and setup
  appear hung. Existing hashes can support a future measured upgrade.
- The application bounds request bodies, idle HTTP connections, sessions,
  login-throttle keys, subscriptions, and SSH host keys. A public or otherwise
  hostile deployment should additionally use reverse-proxy connection and
  request-rate limits.

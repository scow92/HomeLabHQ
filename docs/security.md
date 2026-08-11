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

Proxmox package installation is an explicitly confirmed privileged action.
The read API token lists packages; fixed apt commands run through separately
configured root SSH credentials. Those credentials share the encrypted device
credential record, are never returned to the browser, and use the same pinned
SSH-host-key boundary. Accounts that can access a Proxmox device can trigger
its updates, so device ownership is an administrative trust boundary.

The optional Ansible controller is a separate privileged boundary for Compute
workloads. Only administrators can configure its encrypted credential,
contained project/inventory/playbook paths, approve discovered playbooks,
confirm mappings, choose Docker strategies, perform updates, or permit reboot.
Owners may view their Compute workloads and request approved read/check jobs.
Targets must come from `ansible-inventory --list`; mappings are never inferred
and persisted silently. Command construction uses fixed argument layouts,
validated targets, contained paths, metadata-approved variables, and quoted
argument lists. Configured Ansible executable paths must be absolute and free
of shell metacharacters; each is checked as a regular executable file before
use and invoked by its exact saved path. The API does not expose a generic
runner, shell, extra-vars, paths, or CLI arguments.

Controller passwords and private keys are never returned by settings APIs and
are redacted from connection errors and job output. Common secret-shaped output
is also masked. Playbooks remain responsible for marking their own
secret-bearing tasks `no_log: true`. See [Compute](compute.md).

HomelabHQ does not apply a default destination CIDR allowlist. A safe universal
default would block legitimate private IPv4, IPv6, DNS, and mDNS device setups.
Deployments that include less-trusted accounts should enforce an egress
allowlist outside the application rather than treating application validation
as a network sandbox.

The optional OPNsense VPN Endpoint Manager has additional fixed egress
destinations: NordVPN's public API for candidates, `rdap.org` for public address
ownership bootstrap, and the allowlisted official regional RDAP registries. It
accepts at most three HTTPS referrals between those registry hosts, with loop
detection; it does not accept an arbitrary destination. It accepts no
user-configured validation URL and performs no third-party login or service
probing. Ownership is metadata, not evidence that an endpoint works with an
application. OPNsense credentials, private WireGuard keys and rollback
snapshots stay behind the device-service boundary; only public endpoint
metadata and redacted results reach the browser or bounded history. See
[VPN Endpoints](vpn-endpoints.md) for the complete data flow and manual recovery
procedure.

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

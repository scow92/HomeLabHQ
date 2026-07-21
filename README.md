<p align="center">
  <img src="web/icon-mark.svg" alt="HomelabHQ logo" width="132" height="132">
</p>

<h1 align="center">HomelabHQ</h1>

<p align="center">
  A self-hosted, multi-user dashboard for the routers, switches, access points,
  firewalls, servers, and storage devices that run your homelab.
</p>

<p align="center">
  <a href="https://github.com/scow92/HomeLabHQ/actions/workflows/verify.yml"><img alt="Verification" src="https://github.com/scow92/HomeLabHQ/actions/workflows/verify.yml/badge.svg"></a>
  <a href="LICENSE"><img alt="MIT License" src="https://img.shields.io/badge/license-MIT-blue.svg"></a>
  <img alt="Python 3.11–3.13" src="https://img.shields.io/badge/python-3.11%E2%80%933.13-3776AB.svg">
</p>

HomelabHQ connects to devices over HTTP, REST APIs, SSH, or SNMP, identifies
them using a curated driver library, and presents their useful health data in
one installable web app. It provides live device cards, history charts,
driver-specific detail tables, network-client visibility, and optional device
controls.

No HomelabHQ cloud account is required and the application sends no analytics
or telemetry. Device integrations contact only the endpoints you configure;
optional web-push notifications also contact the browser's push provider.

![HomelabHQ device dashboard](docs/images/dashboard.png)

## Features

- **Guided device detection** ranks compatible drivers and helps select the
  sensors and controls to expose.
- **17 built-in drivers across four transports** cover common firewalls,
  routers, switches, access points, hypervisors, and NAS platforms.
- **Live dashboards** provide device status, sensor values, search, grouping,
  drag-and-drop ordering, and on-demand synchronization.
- **Rich device detail** includes history and throughput charts plus interfaces,
  switch ports, radios, clients, learned MAC addresses, and gateways where the
  driver supports them.
- **Network Access roster** discovers clients from an owner's devices and
  provides filtering, history, export, notifications, editing, AP bindings,
  and supported firewall/NAC controls.
- **Installable PWA with web push** can notify users about device availability
  transitions and newly discovered clients.
- **Multi-user isolation** keeps devices, dashboards, client rosters, and
  subscriptions scoped to their owner while giving administrators explicit
  management visibility.
- **Single-service operation** uses a versioned JSON document store with
  separate bounded history files; no database or message broker is required.

## Supported integrations

| Device or platform | Transport | Authentication |
|---|---|---|
| OPNsense | REST API | API key and secret |
| pfSense REST API v2 | REST API | `X-API-Key` header |
| UniFi Network 9+ | REST API | Integration API key |
| Proxmox VE | REST API | API token |
| Firewalla MSP | REST API | MSP token |
| TrueNAS | REST API | Bearer API key |
| MikroTik RouterOS | REST API | Username and password |
| OpenWrt | HTTP | ubus username and password |
| Synology DSM | HTTP | Username and password |
| QNAP QTS | HTTP | Username and password |
| Keeplink web-smart switches | HTTP | Username and password |
| Zyxel NWA/WAX access points | HTTP | Username and password |

Generic fallbacks support Linux/Unix hosts over SSH, SNMP devices, managed
switches and routers using SNMP IF-MIB, REST APIs, and HTTP web interfaces.

Vendor mappings are exercised against mock servers modelled on documented
endpoints. Firmware can differ, so real-hardware reports should include the
model and firmware version. Contributions that expand verified compatibility
are especially welcome.

## Quick start

You need Git, Docker Engine, and Docker Compose v2.

```bash
git clone https://github.com/scow92/HomeLabHQ.git
cd HomeLabHQ
docker compose up -d --build
```

Open <https://localhost:8770> and create the first administrator account. The
Compose deployment enables built-in TLS and stores application data in the
`hlhq-data` named volume. The first certificate is self-signed, so the browser
will warn until it is trusted.

To include the LAN names or addresses used by other devices in that
certificate, uncomment and set `HLHQ_TLS_HOSTS` in `docker-compose.yml` before
the first start. If the certificate already exists, back up the complete data
directory, stop HomelabHQ, and remove only
`<data-dir>/secrets/tls_cert.pem` and `tls_key.pem` before restarting so it can
create a replacement.

For a locally trusted certificate, install
[mkcert](https://github.com/FiloSottile/mkcert) and run:

```bash
./scripts/setup-mkcert.sh 192.168.1.10 homelabhq.lan
```

The helper writes `certs/nm.crt` and `certs/nm.key` and explains how to trust
its local CA on other devices. Uncomment the read-only `certs` mount in
`docker-compose.yml`, then recreate the service.

See [Configuration](docs/configuration.md) for every environment variable and
local development instructions. See [Operations](docs/operations.md) before
upgrading, changing storage, placing HomelabHQ behind a reverse proxy, or
restoring a backup.

## Security model

- Account passwords are scrypt-hashed and new passwords must contain at least
  15 characters.
- Device credentials are Fernet-encrypted with per-instance key material stored
  under `<data-dir>/secrets/`.
- The supplied container runs as unprivileged UID/GID `10001`, drops Linux
  capabilities, and uses a read-only root filesystem.
- Sessions use HttpOnly cookies and are marked `Secure` whenever HTTPS is used.
- Credentials, cookies, authorization headers, API keys, and common
  secret-shaped values are redacted from structured logs.
- Local development does not provide the container's process-isolation
  boundary. HomelabHQ therefore refuses to open a local data directory that
  already contains device credentials unless an explicit unsafe-development
  override is supplied.

Web push requires HTTPS or `localhost`. A trusted certificate gives the most
reliable PWA and push experience across desktop and mobile browsers.

## Documentation

- [Configuration](docs/configuration.md) — TLS, polling, retention, limits, and
  local development
- [Operations](docs/operations.md) — health checks, logs, upgrades, backups,
  reverse proxies, and capacity boundaries
- [Architecture](docs/architecture.md) — components, persistence, drivers, and
  deferred-change triggers
- [API reference](docs/api.md) — route catalogue and authentication policies
- [Verification](docs/verification.md) — complete local and CI-equivalent checks
- [Contributing](CONTRIBUTING.md) — development workflow and driver submissions

## Contributing

New integrations and real-firmware compatibility fixes are welcome. Read
[CONTRIBUTING.md](CONTRIBUTING.md), add tests for changed behaviour, and run
`./scripts/verify.sh` before opening a pull request. Never commit real hosts,
credentials, certificates, or populated data directories.

## License

[MIT](LICENSE) © scow92

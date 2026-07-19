<p align="center">
  <img src="web/icon-mark.svg" alt="HomelabHQ logo" width="132" height="132">
</p>

<h1 align="center">HomelabHQ</h1>

**A self-hosted, multi-user dashboard for the devices that run your homelab —
routers, switches, access points, firewalls, and NAS boxes — in one place.**

Point HomelabHQ at a device, tell it how to reach it (HTTP web UI, REST API,
SSH, or SNMP), and it fingerprints the device against a curated driver library.
From there you get a live card with an online/offline dot, the sensors worth
watching (CPU, memory, uptime, throughput, port/client counts…), a rich
per-device detail view with history charts and driver-specific tables
(interfaces, ports, radios, connected clients), and optional controls. It's a
PWA with web-push alerts, so you can install it to a phone home screen and get
notified when something goes offline.

Your device credentials are encrypted at rest; nothing phones home; everything
runs in a single container backed by one JSON document.

## Why it exists

Most homelab monitoring is either heavyweight (a full metrics stack per device)
or vendor-locked (one app per brand). HomelabHQ aims for the middle: a light,
single-container dashboard that speaks enough of each device's native protocol
to show the handful of things you actually care about — and that you can extend
with a small Python driver rather than a scrape config.

It began as a general-public sibling of a private network-access-control
dashboard, and reuses that project's proven shell — scrypt auth, an atomic
`flock`-guarded JSON store, PWA + web-push — but replaces all the hard-wired,
site-specific integrations with a plugin/driver architecture anyone can point at
their own gear.

## Features

- **Guided add-device wizard** — pick a connection method, enter host +
  credentials, and HomelabHQ *detects* the device: it probes every compatible
  driver, ranks them by confidence, and pre-selects the best match. You then
  choose which entities to **display** (sensors) and **control** (opt-in).
- **Curated driver library** — 17 drivers across 4 transports (see below), each
  ranking above its generic fallback on a real match and dropping out on bad
  credentials, so detection stays honest.
- **Live device cards** — online/offline status, latest sensor values, "updated
  Ns ago", auto-refresh, and on-demand "Sync now".
- **Rich per-device detail** — overview stat grid, history/throughput charts
  drawn from the poller's stored history (byte counters shown as a rate), and
  driver-provided tables: interfaces, switch ports, WiFi radios, connected
  clients, learned MACs, gateways.
- **Dashboards** — group devices into named dashboards (Network, Proxmox, …),
  drag to reorder, and drag-move between dashboards.
- **Background poller** — reads every device on an interval, persists the latest
  state and a short per-entity history, and tracks online/offline transitions.
- **Web-push alerts (VAPID)** — install as a PWA and get a push notification on
  an offline↔online transition. Built-in TLS means push works without an
  external reverse proxy.
- **Multi-user** — first-run admin setup, cookie sessions, scrypt-hashed
  passwords with a 15-character minimum and brute-force throttling, and admin
  user management. Devices are per-owner; admins see all.

## Supported devices

The wizard's **Device type** picker pre-fills the transport, auth style, default
port, and a credential hint for each. "Auto-detect / custom" keeps the manual
path.

| Device | Transport | Auth / credentials |
|--------|-----------|--------------------|
| OPNsense | `api` | Basic — API **key** + **secret** |
| pfSense (REST API v2) | `api` | header `X-API-Key` |
| UniFi (Network 9+) | `api` | header `X-API-KEY` (integration API) |
| Proxmox VE | `api` | header `Authorization` — whole `PVEAPIToken=user@realm!id=secret` |
| Firewalla (MSP) | `api` | header `Authorization: Token <token>`; host = MSP domain |
| TrueNAS | `api` | Bearer API key |
| MikroTik RouterOS | `api` | Basic — username → key, password → secret |
| OpenWrt router/AP | `http` | username + password (driver handles ubus login) |
| Synology DSM | `http` | username + password (auth.cgi), port 5000/5001 |
| QNAP (QTS) | `http` | username + password (authLogin.cgi), port 8080/443 |
| Keeplink web-smart switch | `http` | username + password (md5-cookie login) |
| Zyxel WiFi AP (NWA/WAX) | `http` | username + password, HTTPS, TLS verify off |

Plus **generic fallbacks** for any device on each transport: Linux/Unix host
(SSH), generic SNMP device, managed switch/router (SNMP IF-MIB), generic
HTTP/REST API, and generic HTTP web UI.

> Vendor API field mappings are validated against mock servers modelled on the
> documented endpoints; on real firmware some fields may need small tweaks.
> **Contributions welcome** — a driver is one small Python file (see below).

## Run it

### Docker (recommended)
```bash
docker compose up --build
# open https://localhost:8770  -> first load prompts you to create the admin
```
The compose file enables TLS (`HLHQ_TLS=auto`) and self-signs a cert on first
run, so web push + PWA install work. To reach it from another device by
IP/hostname, set `HLHQ_TLS_HOSTS` so those names land in the cert SAN. To avoid
the browser warning entirely, drop a trusted cert in as `./certs/nm.crt` +
`./certs/nm.key` (uncomment the `certs` mount).

### Trusted cert with mkcert (no browser warnings)
[mkcert](https://github.com/FiloSottile/mkcert) issues a locally-trusted cert —
the painless way to get web push working across your devices. A helper runs the
whole flow:

```bash
# from the repo root — pass the hostnames/IPs you'll use to reach HomelabHQ
./scripts/setup-mkcert.sh 192.168.1.10 homelabhq.lan
```

It installs the mkcert local CA, writes `./certs/nm.{crt,key}`, and prints how
to trust the CA on phones/other devices. Then uncomment
`- ./certs:/certs:ro` in `docker-compose.yml` and `docker compose up -d --build`.

### Local (dev)
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
HLHQ_DATA_DIR=./data HLHQ_TLS=auto python3 backend/app.py
# open https://localhost:8770   (omit HLHQ_TLS for plain http)
```
Only use this for empty/test stores. Docker runs the app as root, so the
volume is unreadable by any other process on the host; local mode runs as
your regular user, so it has no such isolation from *anything* else running
as that user — including AI coding agents. The app refuses to start this way
against a data dir that already has real device credentials in it (set
`HLHQ_ALLOW_UNSAFE_LOCAL_SECRETS=1` to override). See [Security
notes](#security-notes).

### Configuration
| var | default | meaning |
|-----|---------|---------|
| `HLHQ_PORT` | `8770` | listen port |
| `HLHQ_ICON_HTTP_PORT` | `8771` | plain-HTTP companion port for Home-Screen icons; active only with a self-signed cert so iOS can install the apple-touch-icon. `0` disables it. |
| `HLHQ_DATA_DIR` | `/data` | where the JSON store lives; raw key material (instance secret, TLS key, VAPID key) lives under `<HLHQ_DATA_DIR>/secrets/`, 0700 |
| `HLHQ_ALLOW_UNSAFE_LOCAL_SECRETS` | (off) | lets a non-root run boot against a data dir that already holds real device credentials; see [Local (dev)](#local-dev) |
| `HLHQ_WEB_DIR` | `../web` | static asset root |
| `HLHQ_TLS` | (off) | `auto`/`1` serves HTTPS (self-signed if no cert provided) |
| `HLHQ_TLS_HOSTS` | — | extra SAN hostnames/IPs for the self-signed cert (comma-separated) |
| `HLHQ_TLS_CERT` / `HLHQ_TLS_KEY` | — | paths to a trusted cert to use instead |
| `HLHQ_POLL_INTERVAL` | `60` | seconds between device polls |
| `HLHQ_TRUST_PROXY` | (off) | honor `X-Real-IP`; enable only behind a reverse proxy that strips client-supplied forwarding headers |
| `HLHQ_MAX_SESSIONS` | `10000` | maximum retained active sessions; oldest sessions are pruned after expired ones |
| `HLHQ_MAX_PUSH_SUBSCRIPTIONS_PER_USER` | `20` | maximum retained web-push subscriptions for each user |
| `HLHQ_MAX_SSH_HOST_KEYS` | `1024` | maximum remembered SSH TOFU host-key records |
| `HLHQ_CLIENT_RECORD_RETENTION_DAYS` | `180` | retain offline, unseen Access-roster records for this many days (`0` keeps them indefinitely) |
| `HLHQ_VAPID_SUB` | `mailto:admin@example.com` | VAPID `sub` claim for push. Use an address on a domain you control; Apple rejects reserved TLDs like `.local` with 403 and drops all iOS push. |

> **Web push needs a secure context** (HTTPS or `localhost`) — provided by the
> built-in TLS. With a self-signed cert the browser warns until you trust it;
> use a trusted cert for a clean experience.

### Operations and reverse proxies

`/healthz` is a process-liveness endpoint and returns `200` whenever the HTTP
server can answer. `/readyz` returns `200` only after the JSON store is readable
and the poller has completed a successful cycle; it returns `503` while either
dependency is unavailable. Use `/healthz` for the container health check and
`/readyz` for load-balancer readiness.

Container stdout is line-delimited JSON. Request records include a generated
request ID, route, status, and duration; poll records include device IDs and
durations. Credentials, cookies, authorization headers, API keys, and common
secret-shaped values are redacted before records enter either stdout or the
administrator diagnostic log. Store write observations are available through
`store.metrics()`, poll state through `poller.status()`, and push delivery
observations through `push.metrics()`.

When terminating TLS at a reverse proxy, publish only the proxy's listener and
keep HomelabHQ bound to a private network. Leave `HLHQ_TRUST_PROXY` unset unless
the proxy removes incoming `X-Real-IP` headers and sets its own; otherwise a
client can forge the address shown in diagnostics. The built-in TLS remains the
simplest deployment for a LAN-only instance.

The supplied Compose configuration runs the image as a dedicated unprivileged
user, drops all Linux capabilities, uses a read-only root filesystem, and gives
the process only `/data` plus an ephemeral `/tmp`. If you replace the named
volume with a bind mount, make it writable by UID/GID `10001` before startup.
Compose includes a one-shot `data-init` helper that repairs ownership for the
named volume when upgrading from an older root-owned image. For a bind mount,
run `sudo chown -R 10001:10001 ./your-data-directory` before `docker compose up`.
Dependabot opens weekly updates for Python dependencies and GitHub Actions;
review its changes through the normal verification workflow.

## Access roster isolation

The Access roster is **per owner**. It is built only from that owner’s devices,
and its history, exports, notifications, ignore state, edits, and deletions are
scoped to the same owner. Administrator status does not create an implicit
shared roster; admins continue to manage devices and users according to their
existing permissions. NAC settings and firewall lookup follow that same owner
context. AP client bindings are unique within an owner, dashboard assignments
must keep the device and dashboard under one owner, and a push subscription can
only be removed by its owner. Existing legacy global roster records are retained
in the store for recovery but are not exposed to any account.

## User deprovisioning

Removing a user immediately revokes all of their sessions and web-push
subscriptions. HomelabHQ will not remove the account while it still owns
devices or dashboards: an administrator must explicitly delete that
configuration first, which also removes each deleted device's encrypted
credential. HomelabHQ does not currently expose an ownership-transfer
operation; keep the account if its configuration must be retained. After those
resources are resolved, retrying removal deletes the user and their per-owner
Access roster. This prevents a deleted account's devices from continuing to be
polled and avoids a silent cascade of monitoring configuration.

## Password changes

New accounts and password changes require at least 15 characters; existing
shorter passwords continue to work until they are replaced. Self-service
changes require the account's current password. A successful change keeps the
browser session that submitted it and immediately revokes every other session
for that account.

## Development verification

Use Python 3.11–3.13 on Linux/Unix. The complete local verification command is
documented in [docs/verification.md](docs/verification.md); CI runs it for
every push and pull request. Production installs use `constraints.txt` for
reproducible dependency resolution.

The implemented architectural work and its remaining follow-ups are tracked in
[ARCHITECTURE_REFACTOR_PLAN.md](ARCHITECTURE_REFACTOR_PLAN.md).

## Architecture

A stdlib threading HTTP server, a single-page app, and a JSON document store —
no external database, no message broker.

```
backend/
  app.py            # application startup and server wiring
  http/             # request parsing, router, responses, handler, static delivery
  api/              # focused route modules, one per API domain
  context.py        # authenticated Actor and trusted-system context
  authorization.py  # central resource-visibility policy
  services.py       # actor-scoped application service boundary
  errors.py         # application errors mapped centrally to HTTP responses
  auth.py           # scrypt hashing, users, cookie sessions
  store.py          # atomic flock-guarded JSON document store
  history.py        # per-device chart history, one compact JSON file per device
  domain.py         # validated typed values at shared boundaries
  client_*.py       # discovery, pure merge, roster persistence, orchestration
  nac_service.py    # NAC/firewall coordination, separate from roster state
  crypto.py         # Fernet credential-at-rest (per-instance key)
  transports.py     # SSH / SNMP / HTTP-API connections + open_connection() factory
  snmp_backend.py   # isolated pysnmp 7 async->sync glue
  detect.py         # probe -> rank drivers; enumerate entities
  devices.py        # device persistence + live sensor reads
  dashboards.py     # per-owner dashboard grouping
  poller.py         # background poll loop: state, history, online tracking
  push.py           # VAPID web-push: keys, subscriptions, delivery
  tls.py            # HTTPS: self-signed generation + drop-in trusted cert
  drivers/
    base.py         # Driver + Entity contracts
    registry.py     # driver lookup by id / transport
    <vendor>.py     # one file per device (opnsense, proxmox, zyxel_ap, …)
web/                # index.html, native ES modules, styles.css, sw.js, PWA assets
_verify/            # end-to-end test scripts + mock device servers (dev only)
```

Most persistent state is one JSON document under the data dir:
`users`, `sessions`, `devices`, `credentials`, `dashboards`, `meta`. Per-device
chart history lives separately, one compact JSON file per device under
`<data dir>/history/<id>.json` — history dominates size and churn, so keeping
it out of the main doc means routine reads/writes (auth, session, rename)
never pay for chart data (see `history.py`).

The document has an explicit `schemaVersion`. HomelabHQ validates and applies
ordered migrations before accepting requests at startup; an unreadable,
invalid, or newer-version document prevents startup rather than being replaced.
Each successful main-document write records a validated `homelabhq.json.bak`
copy first. Store-write duration and document-byte observations are available
to backend diagnostics through `store.metrics()`.

### Backup and restore

Back up the complete data directory while HomelabHQ is stopped; it contains the
main document, chart-history files, and the `secrets/` keys needed to decrypt
credentials. For Docker deployments, stop the container, archive the named
volume or its host bind mount, then restart it. Test a restore in an isolated
data directory before relying on it.

To restore, stop HomelabHQ, preserve the current data directory as a separate
rollback copy, replace it with the selected backup, ensure the container can
read the restored files, and start HomelabHQ. For a failed latest write,
`homelabhq.json.bak` is the immediately preceding validated main document;
replace only `homelabhq.json` with that file while the service is stopped.
Never copy a backup while the service is writing it.

### JSON-store capacity boundary

The JSON store is intentionally for a single-container, single-writer
deployment. Each main-document mutation serializes the entire document, so
`store.metrics()` should be monitored for growing document bytes or write
latency. Per-device chart history is already kept in separate, bounded files;
client events, sessions, push subscriptions, stale roster records, and SSH
host-key records also have retention limits.

Reassess SQLite rather than increasing these limits indefinitely if the
deployment needs multiple processes, the roster or document becomes large,
filter/query requirements become material, migrations become frequent,
write latency is operationally significant, or per-owner authorization needs
increasingly complex document scans. If that point is reached, move users,
devices, dashboards, and roster metadata first; history can stay in its
specialized files until its query requirements justify a migration.

### Writing a driver

A driver is a small subclass of `Driver` (`backend/drivers/base.py`) with three
hooks:

- `probe(conn)` → a confidence score (higher wins detection),
- `entities()` → the sensors to display and controls to expose,
- `detail(conn)` → *(optional)* a structured `{info, tables}` read for the rich
  detail view.

Register it in `backend/drivers/registry.py`, add a mock server under `_verify/`
modelled on the vendor's documented endpoints, and you're done. Existing drivers
are the best templates.

## API

| method | path | purpose |
|--------|------|---------|
| GET  | `/api/drivers` | catalogue of drivers + transports (for the wizard) |
| POST | `/api/devices/detect` | probe `{host,transport,port,credentials}` → ranked candidates |
| POST | `/api/devices/entities` | list entities for a chosen `driverId` on that device |
| POST | `/api/devices` | save a device with selected entities (creds encrypted) |
| GET  | `/api/devices` | list your devices (admins: all) |
| GET  | `/api/devices/{id}/state` | live read of the device's sensor values |
| GET  | `/api/devices/{id}/detail` | rich drill-down: entity catalogue + `detail` tables + full history |
| GET  | `/api/devices/{id}/history?key=` | stored history for one numeric entity |
| PATCH | `/api/devices/{id}` | rename, move to a dashboard, or set enabled entities |
| POST | `/api/devices/reorder` | set device order from `{ids: [...]}` |
| DELETE | `/api/devices?id=` | remove a device + its stored credential |
| GET  | `/api/dashboards` | list your dashboards (admins: all) |
| POST | `/api/dashboards` | create a dashboard `{name}` |
| PATCH | `/api/dashboards/{id}` | rename / reorder a dashboard |
| DELETE | `/api/dashboards?id=` | delete a dashboard (its devices become unassigned) |
| GET  | `/api/push/vapid` | VAPID public key for the browser to subscribe |
| POST | `/api/push/subscribe` / `unsubscribe` / `test` | manage web-push subscriptions |

### `credentials` shapes per transport
The `credentials` object in detect/create requests (encrypted at rest):

| transport | fields |
|-----------|--------|
| `ssh`  | `username`, `password` **or** `privateKey` (PEM/OpenSSH) |
| `snmp` | `community` (default `public`), `version` (`2c` or `1`) |
| `api`  | `apiKey`, `apiSecret`, `authStyle` (`basic`\|`bearer`\|`header`, default `basic`), `scheme` (`https`\|`http`), `basePath`, `probePath`, `verifyTls` (default `true`), `keyHeader`/`secretHeader` (for `header` style) |
| `http` | `username`, `password`, `scheme` (`http`\|`https`, default `http`), `basePath`, `probePath`, `verifyTls` (default `true`) — device login is handled by the driver |

## Security notes

- Passwords are scrypt-hashed at rest; device credentials are Fernet-encrypted
  with a per-instance key kept `0600` in the data dir (never stored in the
  device record).
- Sessions are HttpOnly cookies, marked `Secure` when serving HTTPS.
- TLS is built in (`HLHQ_TLS`): a self-signed cert works but warns; a drop-in or
  `HLHQ_TLS_CERT`/`HLHQ_TLS_KEY` trusted cert avoids warnings. Web push requires
  HTTPS (or `localhost`) — satisfied by the built-in TLS.
- Nothing phones home; all device access is outbound from your instance to your
  own gear.
- **Secrets isolation from co-resident processes (including AI agents).** The
  Docker deployment runs in its own container under a dedicated unprivileged
  identity, so `<HLHQ_DATA_DIR>/secrets/` is behind an OS-enforced boundary
  from ordinary host processes. The dedicated container identity is intentional:
  it does not need root or Linux capabilities to bind its high ports and write
  its owned data volume.
  Local/dev mode has no such boundary: the app runs as your regular user, so
  anything else running as that user (an AI coding agent included) is exactly
  as able to read those files as you are. Real device credentials should only
  ever live behind the Docker deployment; local mode refuses to start against
  a data dir that already has them (see `HLHQ_ALLOW_UNSAFE_LOCAL_SECRETS`
  above).

## Contributing

Contributions are welcome — especially **new device drivers** and **fixes to
field mappings** where a driver was validated against a mock but needs a tweak
for real firmware.

**New driver?** See [Writing a driver](#writing-a-driver) above. In short:
subclass `Driver` in a new `backend/drivers/<vendor>.py`, implement
`probe` / `entities` / `detail`, register it in `backend/drivers/registry.py`,
and add a mock server + test under `_verify/` modelled on the vendor's
documented endpoints. Existing drivers are the best templates.

**Workflow:**
1. Fork the repo and create a branch for your change.
2. Keep it self-contained — the backend is stdlib-only plus the deps already in
   `requirements.txt`; please don't add new runtime dependencies without reason.
3. Run the verification scripts and make sure they pass:
   ```bash
   for t in _verify/*_test.py; do python3 "$t" || break; done
   ```
   Add or extend a `_verify/` test for anything you change.
4. Open a pull request describing what device/behaviour it covers and how you
   tested it. If you tested against real hardware, say which model and firmware.

Please **don't commit** real hosts, credentials, certs, or a populated `data/`
dir — `.gitignore` covers `/data/`, `/certs/`, and `.env`; use the fake
fixtures in `_verify/` as the pattern for test data.

By contributing you agree that your contributions are licensed under the
project's MIT license.

## License

[MIT](LICENSE) © scow92

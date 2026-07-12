# HomelabHQ

A self-hosted, multi-user tool for adding and managing your network devices.
Point it at a router, switch, AP or firewall, choose how to reach it
(HTTP / API / SSH / SNMP), and HomelabHQ fingerprints the device against a
curated driver library to offer a known set of entities to **display** (sensors)
and **control** (switches, buttons).

This is a fresh, general-public sibling of a private network-access-control
dashboard. It reuses that project's proven shell — scrypt auth, an atomic
`flock`-guarded JSON store, PWA + web-push — but drops all the hard-wired,
site-specific integrations in favour of a plugin/driver architecture.

## Status — Milestone 9 (per-interface history, SFP, interface editing)

Done (Milestone 9):
- **Per-interface upload/download history**: the poller now records each
  interface's rx/tx counters over time (`ifHistory`). In the detail view, the
  **Interfaces** table (OPNsense + OpenWrt) is clickable — pick an interface to
  see a dual-line **download/upload** rate chart of its history.
- **Edit / remove interfaces**: an **Edit** toggle on the Interfaces table lets
  you hide interfaces you don't care about (e.g. unassigned ones) and restore
  them later; the choice persists per device (`hiddenInterfaces`).
- **OpenWrt `/metrics` (SFP/optics)**: OpenWrt devices optionally scrape a
  Prometheus `/metrics` page (path set in the wizard, default `/metrics`). SFP /
  optical-module series are surfaced as an **SFP / optics** table — handy for
  OpenWrt-flashed switches. Non-SFP metrics are ignored; a missing page is a
  no-op.
- **Uptime** (and other identity fields) now render under **Device details**
  instead of as a metric graph, with human-readable formatting (`3d 4h`).
- **Keeplink** parsing hardened (tolerates `<td>` attribute variants) so the
  **Ports** table (interface · speed · PoE · packets · errors) and **Learned
  MACs** table match the Network Manager view.
- Verified in `_verify/richdetail_test.py`: `interfaces()`, per-interface
  history via the poller, `/metrics` SFP filtering, and `hiddenInterfaces`.

## Status — Milestone 8 (reorder, drag-move, richer detail)

Done (Milestone 8) — polish + deeper device detail:
- **Reorder & drag-move**: drag a device card to reorder it within a dashboard,
  or drop it onto a dashboard tab to move it there (touch users get a
  **Dashboard** selector in the detail modal). The per-card move dropdown is
  gone. Order persists via `POST /api/devices/reorder`.
- **Online at a glance**: the `Reachable` sensor is dropped from every driver —
  the status dot on each card already reflects online/offline from the poller.
- **Rich detail() for OPNsense / OpenWrt / Keeplink**, matching the Network
  Manager panels:
  - **OPNsense** — uptime/load/memory, aggregate in/out throughput counters
    (charted as rate), a **Gateways** table (status/delay/loss) and an
    **Interfaces** table (per-interface in/out).
  - **OpenWrt** — per-interface up/MAC/in/out from ubus `network.device`, plus
    aggregate throughput counters.
  - **Keeplink** — a **Ports** table (link/speed/PoE/packets/errors) and a
    **Learned MACs** table (mac/vlan/port), with PoE draw + firmware sensors.
- **UI**: "Check now" → **"Sync now"**; info/metric boxes size to their content
  so a value never breaks across rows.
- Verified in `_verify/richdetail_test.py` (drivers' probe/entities/detail
  against mocks) + reorder ownership checks.

Done (Milestone 7) — organize devices and tailor the detail view:
- **Dashboards**: create named dashboards ("Network", "Proxmox", …) and assign
  devices to them. The Devices tab gains a pill bar — **All / Unassigned /
  <your dashboards>** with per-tab counts — plus **+ New / Rename / Delete**
  (deleting a dashboard leaves its devices intact, moved to *Unassigned*).
  Every device card has a **"Move to…"** selector to add/remove/move it between
  dashboards, and the add-device wizard picks a target dashboard on save.
  Membership is single-homed (`device.dashboardId`); dashboards are per-owner.
- **Customizable device detail**: the detail modal now renders from the device's
  **entity catalogue** — string entities as *Device details*, numeric/boolean
  ones as *Metrics* (value + history chart) — and a **Customize** panel lets you
  check/uncheck which entities are displayed **and polled**. Defaults to the
  entities chosen when the device was added (device details, CPU, memory, …).
- **API**: `GET /api/dashboards`, `POST /api/dashboards`,
  `PATCH /api/dashboards/{id}`, `DELETE /api/dashboards?id=`, and
  `PATCH /api/devices/{id}` (rename / move / set enabled entities).
- Verified end-to-end (`_verify/zyxel_test.py` + dashboard/customization checks):
  dashboard CRUD, device move, delete-unassigns, and entity enable/disable
  round-tripping through `read_detail`.

Done (Milestone 1):
- Threading HTTP server (stdlib), SPA served from `web/`.
- **Multi-user auth**: first-run admin setup, login, cookie sessions,
  scrypt-hashed passwords, brute-force throttle.
- **User management** (admin): add/remove members and admins, with guards
  against deleting yourself or the last admin.
- Atomic JSON store and per-instance credential-encryption key.
- Tabbed SPA shell: Devices / Add device / Users / Settings.

Done (Milestone 2):
- **Transports**: SSH (paramiko), SNMP (pysnmp 7, async wrapped sync),
  **`api`** — HTTP/REST API with an **API key + secret** (Basic / Bearer /
  header auth), and **`http`** — a device **web UI reached with a username +
  password**, where the (device-specific) login lives in the driver.
- **Curated driver framework**: `Driver.probe()` returns a confidence score,
  `Driver.entities()` enumerates sensors (display) + controls.
- **Drivers**: Linux/Unix host (SSH), generic SNMP device, generic HTTP/REST
  API, generic HTTP web UI, and **Keeplink web-smart switch** (HTTP; md5-cookie
  login, reads the MAC table for learned-MAC / active-port counts).
- **Detection pipeline**: open connection → probe every compatible driver →
  rank by confidence → user picks (or takes the top match).
- **Devices**: create/list/delete, credentials **Fernet-encrypted at rest**
  (never in the device record), per-user ownership, admin-sees-all.
- **Live reads**: `GET /api/devices/{id}/state` decrypts creds, connects, and
  reads the opted-in sensor entities.
- Verified end-to-end against live `sshd` + `snmpd` targets (see `_verify/`).

Done (Milestone 3):
- **Guided add-device wizard** (the "Add device" tab): pick a connection
  method (SSH / HTTP web UI / HTTP API / SNMP) → enter host + transport-specific
  credentials → *Detecting…* → ranked driver candidates (best match
  pre-selected, override from the list) → pick which entities to **display**
  (sensors, on by default) and **control** (opt-in) → name + save.
- **Device cards**: live "Check now" (reads sensor values on demand) and
  "Remove". Empty-state onboarding.
- Wizard offers only transports the server has drivers for, shows detection
  confidence bars, and surfaces auth/reachability errors inline.

Done (Milestone 4):
- **Background poller** (daemon thread in the server): reads every device's
  sensors on an interval (`HLHQ_POLL_INTERVAL`, default 60s), persists the latest
  `state` (`online/values/errors/ts`) + a short per-entity history, and tracks
  online/offline.
- **Live device cards**: online/offline dot, latest values, "updated Ns ago",
  auto-refresh every 15s (plus on-demand "Check now").
- **Web push** (VAPID): per-instance keypair, subscribe/unsubscribe/test
  endpoints, a "Enable alerts" control in Settings, and a push notification to
  the device owner + admins on an offline↔online transition. Dead
  subscriptions (404/410) are pruned automatically.

- **Built-in TLS**: serves HTTPS (`HLHQ_TLS`), self-signing a cert on first run
  (with correct SANs) or using a drop-in / configured trusted cert — so web push
  works in the browser without an external reverse proxy. Session cookies get
  the `Secure` flag over HTTPS.

Done (Milestone 5) — more curated drivers, one per "smart" transport:
- **OpenWrt router/AP** (`http`): ubus JSON-RPC login → hostname, model, release,
  uptime, load, memory. Identified as OpenWrt with 0.9 confidence.
- **MikroTik RouterOS** (`api`): REST `/rest/system/resource` over Basic (enter
  the RouterOS **username as the API key, password as the API secret**) →
  version, board, uptime, CPU, memory. 0.9 when platform is MikroTik.
- **Managed switch/router (SNMP)**: IF-MIB high-capacity counters → interface
  count, total in/out bytes, in/out errors. Ranks 0.55, just above generic SNMP.

Plus eight platform/appliance drivers (all verified against mock APIs):
**OPNsense**, **pfSense**, **UniFi** (Network integration API), **Proxmox VE**,
**Synology DSM**, **TrueNAS**, **Firewalla** (MSP API), **QNAP** —
plus the **Zyxel WiFi AP** driver from Milestone 6, **17 drivers total** across
4 transports.

Each ranks above its generic fallback on a real match and drops out on bad
credentials, so detection stays honest.

> Vendor API field mappings are validated against mock servers modelled on the
> documented endpoints; on real firmware some fields may need small tweaks.
> Contributions welcome.

Done (Milestone 6) — per-device drill-down + a WiFi driver:
- **Device detail view**: clicking a device card (or its "Details →" button)
  opens a modal that fetches `GET /api/devices/{id}/detail` and renders an
  **overview** stat grid, **history/traffic charts** (inline `<canvas>`
  sparklines drawn from the poller's stored per-entity history — byte counters
  are shown as a per-second rate), and any driver-provided **tables**
  (interfaces / ports / radios / connected clients).
- **`Driver.detail(conn)` hook**: an optional structured read (`{info, tables}`)
  that powers the detail view; drivers opt in, and the UI falls back to the
  latest polled sensor values for those that don't.
- **Zyxel WiFi AP driver** (`http`, `zyxel.ap`): logs into the AP web UI and
  drives the `zysh-cgi` CLI (`show version` / `…station info` / …) for model,
  firmware, uptime, CPU, memory, per-radio channel + client counts (scalar
  sensors, so they get history charts), plus a **connected-clients table**
  (MAC, band, SSID, PHY, RSSI, Tx/Rx) and a per-radio table in the detail view.
  Verified end-to-end against an in-process AP mock (`_verify/zyxel_test.py`).
  **17 drivers total.**

### Device presets in the wizard
The Add-device wizard has a **Device type** picker: choose your platform
(OPNsense, Proxmox, Synology, …) and it pre-fills the transport, auth style,
default port, and shows a credential hint. "Auto-detect / custom" keeps the
manual path. Reference of what each preset configures:

| Device | Transport | Auth style | Credentials |
|--------|-----------|-----------|-------------|
| OPNsense | `api` | basic | API **key** → API key, **secret** → API secret |
| MikroTik RouterOS | `api` | basic | username → API key, password → API secret |
| TrueNAS | `api` | bearer | API key → API key |
| pfSense (REST API v2) | `api` | header, key header `X-API-Key` | key → API key |
| UniFi (Network 9+) | `api` | header, key header `X-API-KEY` | key → API key |
| Proxmox VE | `api` | header, key header `Authorization` | whole `PVEAPIToken=user@realm!id=secret` → API key |
| Firewalla (MSP) | `api` | header, key header `Authorization` | host = MSP domain; `Token <token>` → API key |
| OpenWrt | `http` | (driver handles ubus login) | username + password |
| Synology DSM | `http` | (driver handles auth.cgi login) | username + password, port 5000/5001 |
| QNAP (QTS) | `http` | (driver handles authLogin.cgi) | username + password, port 8080/443 |
| Keeplink switch | `http` | (driver handles md5-cookie) | username + password |
| Zyxel WiFi AP (NWA/WAX) | `http` | (driver handles zysh-cgi login) | username + password, HTTPS, TLS verify off |

> **Web push needs a secure context** (HTTPS or `localhost`) — now provided by
> the built-in TLS. With a self-signed cert the browser warns until you trust
> it; use a trusted cert (`HLHQ_TLS_CERT`/`HLHQ_TLS_KEY` or a `./certs` drop-in) for
> a clean experience.

## API (Milestone 2)
| method | path | purpose |
|--------|------|---------|
| GET  | `/api/drivers` | catalogue of drivers + transports (for the wizard) |
| POST | `/api/devices/detect` | probe `{host,transport,port,credentials}` → ranked candidates |
| POST | `/api/devices/entities` | list entities for a chosen `driverId` on that device |
| POST | `/api/devices` | save a device with selected entities (creds encrypted) |
| GET  | `/api/devices` | list your devices (admins: all) |
| GET  | `/api/devices/{id}/state` | live read of the device's sensor values |
| GET  | `/api/devices/{id}/detail` | rich drill-down: entity catalogue (meta + enabled + value) + `detail` tables + full history |
| GET  | `/api/devices/{id}/history?key=` | stored history for one numeric entity |
| PATCH | `/api/devices/{id}` | rename, move to a dashboard, or set enabled entities |
| POST | `/api/devices/reorder` | set device order from `{ids: [...]}` (a dashboard's new sequence) |
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
| `http` | `username`, `password`, `scheme` (`http`\|`https`, default `http`), `basePath`, `probePath`, `verifyTls` (default `true`) — device login is handled by the driver (e.g. Keeplink's md5 cookie) |

## Architecture

```
backend/
  app.py            # HTTP server + JSON API + static serving
  auth.py           # scrypt hashing, users, cookie sessions
  store.py          # atomic flock-guarded JSON document store
  crypto.py         # Fernet credential-at-rest (per-instance key)
  transports.py     # SSH / SNMP / HTTP-API connections + open_connection() factory
  snmp_backend.py   # isolated pysnmp 7 async->sync glue
  detect.py         # probe -> rank drivers; enumerate entities
  devices.py        # device persistence + live sensor reads
  poller.py         # background poll loop: state, history, online tracking
  push.py           # VAPID web-push: keys, subscriptions, delivery
  tls.py            # HTTPS: self-signed generation + drop-in trusted cert
  drivers/
    base.py         # Driver + Entity contracts
    registry.py     # driver lookup by id / transport
    generic_ssh.py  # Generic Linux/Unix host (SSH)
    generic_snmp.py # Generic SNMP device
    generic_api.py  # Generic HTTP/REST API device (key + secret)
    generic_http.py # Generic HTTP web UI (user + password)
    keeplink.py     # Keeplink web-smart switch (HTTP, md5-cookie login)
    openwrt.py      # OpenWrt router/AP (HTTP ubus JSON-RPC login)
    mikrotik.py     # MikroTik RouterOS (api transport, REST + Basic)
    snmp_switch.py  # Managed switch/router (SNMP IF-MIB HC counters)
    opnsense.py     # OPNsense firewall (api, Basic key:secret)
    pfsense.py      # pfSense firewall (api, REST API v2 pkg, X-API-Key)
    unifi.py        # UniFi Network controller (api, X-API-KEY integration API)
    proxmox.py      # Proxmox VE (api, PVEAPIToken header)
    synology.py     # Synology DSM NAS (http, auth.cgi login)
    truenas.py      # TrueNAS (api, Bearer API key)
    firewalla.py    # Firewalla (api, MSP API, Authorization: Token)
    qnap.py         # QNAP NAS (http, authLogin.cgi, XML)
web/                # index.html, app.js, styles.css, sw.js, manifest
_verify/            # end-to-end test scripts (dev only)
```

Data model (single JSON doc under `/data`):
`users`, `sessions`, `devices`, `credentials`, `meta`.

## Run it

### Docker (recommended)
```bash
docker compose up --build
# open https://localhost:8770  -> first load prompts you to create the admin
```
The compose file enables TLS (`HLHQ_TLS=auto`) and self-signs a cert on first run,
so web push + PWA install work. To reach it from another device by IP/hostname,
set `HLHQ_TLS_HOSTS` so those names land in the cert SAN (see below). To avoid the
browser warning entirely, drop a trusted cert in as `./certs/nm.crt` +
`./certs/nm.key` (uncomment the `certs` mount).

### Trusted cert with mkcert (recommended — no browser warnings)
[mkcert](https://github.com/FiloSottile/mkcert) issues a locally-trusted cert,
which is the painless way to get web push working across your devices. A helper
does the whole flow:

```bash
# from the repo root — pass the hostnames/IPs you'll use to reach HomelabHQ
./scripts/setup-mkcert.sh 192.168.1.10 homelabhq.lan
```

It installs the mkcert local CA, writes `./certs/nm.{crt,key}` (the TLS drop-in
path that the `certs` mount exposes at `/certs`), and prints how to trust the CA
on phones/other devices. Then:

```bash
# uncomment "- ./certs:/certs:ro" in docker-compose.yml, then:
docker compose up -d --build
```

Equivalent manual steps if you'd rather not use the script:
```bash
mkcert -install                                   # trust the local CA on this machine
mkdir -p certs
mkcert -cert-file certs/nm.crt -key-file certs/nm.key \
       localhost 127.0.0.1 192.168.1.10 homelabhq.lan
# to trust it on a phone, install $(mkcert -CAROOT)/rootCA.pem on the device
```

### Local (dev)
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
HLHQ_DATA_DIR=./data HLHQ_TLS=auto python3 backend/app.py
# open https://localhost:8770   (omit HLHQ_TLS for plain http)
```

### Environment
| var | default | meaning |
|-----|---------|---------|
| `HLHQ_PORT` | `8770` | listen port |
| `HLHQ_ICON_HTTP_PORT` | `8771` | plain-HTTP companion port for Home-Screen icons; active only with a self-signed cert so iOS can install the apple-touch-icon (which its icon fetcher won't load over a self-signed HTTPS origin). `0` disables it. |
| `HLHQ_DATA_DIR` | `/data` | where the JSON store + secrets live |
| `HLHQ_WEB_DIR` | `../web` | static asset root |
| `HLHQ_TLS` | (off) | `auto`/`1` serves HTTPS (self-signed if no cert provided) |
| `HLHQ_TLS_HOSTS` | — | extra SAN hostnames/IPs for the self-signed cert (comma-separated) |
| `HLHQ_TLS_CERT` / `HLHQ_TLS_KEY` | — | paths to a trusted cert to use instead |
| `HLHQ_POLL_INTERVAL` | `60` | seconds between device polls |
| `HLHQ_VAPID_SUB` | `mailto:admin@example.com` | VAPID `sub` claim for push. Use an address on a domain you control; Apple rejects reserved TLDs like `.local` with 403 and drops all iOS push. |

## Security notes
- Passwords are scrypt-hashed at rest; device credentials are Fernet-encrypted
  with a per-instance key kept `0600` in the data dir.
- Sessions are HttpOnly cookies, marked `Secure` when serving HTTPS.
- TLS is built in (`HLHQ_TLS`): a self-signed cert works but warns; a drop-in or
  `HLHQ_TLS_CERT`/`HLHQ_TLS_KEY` trusted cert avoids warnings. Web push requires
  HTTPS (or `localhost`) — now satisfied by the built-in TLS.

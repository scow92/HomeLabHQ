# HomelabHQ

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
  passwords with brute-force throttling, and admin user management. Devices are
  per-owner; admins see all.

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

### Configuration
| var | default | meaning |
|-----|---------|---------|
| `HLHQ_PORT` | `8770` | listen port |
| `HLHQ_ICON_HTTP_PORT` | `8771` | plain-HTTP companion port for Home-Screen icons; active only with a self-signed cert so iOS can install the apple-touch-icon. `0` disables it. |
| `HLHQ_DATA_DIR` | `/data` | where the JSON store + secrets live |
| `HLHQ_WEB_DIR` | `../web` | static asset root |
| `HLHQ_TLS` | (off) | `auto`/`1` serves HTTPS (self-signed if no cert provided) |
| `HLHQ_TLS_HOSTS` | — | extra SAN hostnames/IPs for the self-signed cert (comma-separated) |
| `HLHQ_TLS_CERT` / `HLHQ_TLS_KEY` | — | paths to a trusted cert to use instead |
| `HLHQ_POLL_INTERVAL` | `60` | seconds between device polls |
| `HLHQ_VAPID_SUB` | `mailto:admin@example.com` | VAPID `sub` claim for push. Use an address on a domain you control; Apple rejects reserved TLDs like `.local` with 403 and drops all iOS push. |

> **Web push needs a secure context** (HTTPS or `localhost`) — provided by the
> built-in TLS. With a self-signed cert the browser warns until you trust it;
> use a trusted cert for a clean experience.

## Architecture

A stdlib threading HTTP server, a single-page app, and a JSON document store —
no external database, no message broker.

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
  dashboards.py     # per-owner dashboard grouping
  poller.py         # background poll loop: state, history, online tracking
  push.py           # VAPID web-push: keys, subscriptions, delivery
  tls.py            # HTTPS: self-signed generation + drop-in trusted cert
  drivers/
    base.py         # Driver + Entity contracts
    registry.py     # driver lookup by id / transport
    <vendor>.py     # one file per device (opnsense, proxmox, zyxel_ap, …)
web/                # index.html, app.js, styles.css, sw.js, PWA manifest + icons
_verify/            # end-to-end test scripts + mock device servers (dev only)
```

The whole persistent state is one JSON document under the data dir:
`users`, `sessions`, `devices`, `credentials`, `dashboards`, `meta`.

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

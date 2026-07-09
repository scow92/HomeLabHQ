# NetManager

A self-hosted, multi-user tool for adding and managing your network devices.
Point it at a router, switch, AP or firewall, choose how to reach it
(HTTP / API / SSH / SNMP), and NetManager fingerprints the device against a
curated driver library to offer a known set of entities to **display** (sensors)
and **control** (switches, buttons).

This is a fresh, general-public sibling of a private network-access-control
dashboard. It reuses that project's proven shell — scrypt auth, an atomic
`flock`-guarded JSON store, PWA + web-push — but drops all the hard-wired,
site-specific integrations in favour of a plugin/driver architecture.

## Status — Milestone 4 (poller + live cards + web push)

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
  sensors on an interval (`NM_POLL_INTERVAL`, default 60s), persists the latest
  `state` (`online/values/errors/ts`) + a short per-entity history, and tracks
  online/offline.
- **Live device cards**: online/offline dot, latest values, "updated Ns ago",
  auto-refresh every 15s (plus on-demand "Check now").
- **Web push** (VAPID): per-instance keypair, subscribe/unsubscribe/test
  endpoints, a "Enable alerts" control in Settings, and a push notification to
  the device owner + admins on an offline↔online transition. Dead
  subscriptions (404/410) are pruned automatically.

- **Built-in TLS**: serves HTTPS (`NM_TLS`), self-signing a cert on first run
  (with correct SANs) or using a drop-in / configured trusted cert — so web push
  works in the browser without an external reverse proxy. Session cookies get
  the `Secure` flag over HTTPS.

Not yet:
- More curated drivers: OpenWRT (ubus), vendor-specific (Milestone 5).
- Entity history charts/sparklines on cards (history API already exists).

> **Web push needs a secure context** (HTTPS or `localhost`) — now provided by
> the built-in TLS. With a self-signed cert the browser warns until you trust
> it; use a trusted cert (`NM_TLS_CERT`/`NM_TLS_KEY` or a `./certs` drop-in) for
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
| GET  | `/api/devices/{id}/history?key=` | stored history for one numeric entity |
| DELETE | `/api/devices?id=` | remove a device + its stored credential |
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
The compose file enables TLS (`NM_TLS=auto`) and self-signs a cert on first run,
so web push + PWA install work. To reach it from another device by IP/hostname,
set `NM_TLS_HOSTS` so those names land in the cert SAN (see below). To avoid the
browser warning entirely, drop a trusted cert in as `./certs/nm.crt` +
`./certs/nm.key` (uncomment the `certs` mount).

### Local (dev)
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
NM_DATA_DIR=./data NM_TLS=auto python3 backend/app.py
# open https://localhost:8770   (omit NM_TLS for plain http)
```

### Environment
| var | default | meaning |
|-----|---------|---------|
| `NM_PORT` | `8770` | listen port |
| `NM_DATA_DIR` | `/data` | where the JSON store + secrets live |
| `NM_WEB_DIR` | `../web` | static asset root |
| `NM_TLS` | (off) | `auto`/`1` serves HTTPS (self-signed if no cert provided) |
| `NM_TLS_HOSTS` | — | extra SAN hostnames/IPs for the self-signed cert (comma-separated) |
| `NM_TLS_CERT` / `NM_TLS_KEY` | — | paths to a trusted cert to use instead |
| `NM_POLL_INTERVAL` | `60` | seconds between device polls |
| `NM_VAPID_SUB` | `mailto:admin@netmanager.local` | VAPID `sub` claim for push |

## Security notes
- Passwords are scrypt-hashed at rest; device credentials are Fernet-encrypted
  with a per-instance key kept `0600` in the data dir.
- Sessions are HttpOnly cookies, marked `Secure` when serving HTTPS.
- TLS is built in (`NM_TLS`): a self-signed cert works but warns; a drop-in or
  `NM_TLS_CERT`/`NM_TLS_KEY` trusted cert avoids warnings. Web push requires
  HTTPS (or `localhost`) — now satisfied by the built-in TLS.

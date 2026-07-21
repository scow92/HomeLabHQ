# Operations

The supported production shape is one HomelabHQ application process using one
writable data directory. The supplied Compose deployment runs that process as
an unprivileged user with a read-only root filesystem.

## Health and readiness

- `GET /healthz` returns `200` whenever the HTTP server can answer. Use it for
  process or container liveness.
- `GET /readyz` returns `200` only after the JSON store is readable and the
  poller has completed a successful cycle. It returns `503` while either
  dependency is unavailable. Use it for load-balancer readiness.

The Compose health check uses `/healthz` over the built-in TLS listener.

## Logs and diagnostics

Container stdout is line-delimited JSON. Request records include request ID,
route, status, and duration; poll records include device IDs and durations.
Credentials, cookies, authorization headers, API keys, and common
secret-shaped values are redacted before records reach stdout or the
administrator diagnostic log.

```bash
docker compose logs -f homelabhq
```

The application exposes store-write observations through `store.metrics()`,
poll state through `poller.status()`, and push-delivery observations through
`push.metrics()` for internal diagnostics.

## Container permissions

The image runs as UID/GID `10001`, and Compose drops all Linux capabilities.
The process receives only the writable `/data` volume and an ephemeral `/tmp`.

Compose includes a one-shot `data-init` service that repairs ownership on named
volumes created by older root-running images. When replacing the named volume
with a bind mount, prepare it before startup:

```bash
sudo chown -R 10001:10001 ./your-data-directory
```

## Upgrades

Back up the complete data directory first. Then update and recreate the image:

```bash
git pull --ff-only
docker compose build --pull
docker compose up -d
docker compose ps
```

Inspect startup logs and `/readyz` before considering the upgrade complete.
Do not run `docker compose down -v`; the `-v` option removes the named data
volume.

## Backup and restore

The data directory contains all state required for recovery:

- `homelabhq.json` and its immediately preceding validated `.bak` copy;
- per-device chart history under `history/`;
- encryption, TLS, and VAPID key material under `secrets/`; and
- supporting lock and host-key state.

Stop HomelabHQ before copying the directory or volume so the main document,
history, and keys represent one point in time. Archive the complete volume, not
only `homelabhq.json`, and store the backup with protections appropriate for
device credentials.

To restore:

1. Stop HomelabHQ.
2. Preserve the current data directory as a separate rollback copy.
3. Replace the complete directory with the selected backup.
4. Ensure UID/GID `10001` can read and write the restored content.
5. Start HomelabHQ and check logs, `/healthz`, and `/readyz`.

If only the latest main-document write is damaged,
`homelabhq.json.bak` contains the immediately preceding validated document.
With the service stopped, preserve both files and replace only
`homelabhq.json` with the `.bak` copy. Test restores in an isolated data
directory before relying on a backup process.

## Reverse proxies

When terminating TLS at a reverse proxy, publish only the proxy listener and
keep HomelabHQ on a private network. Leave `HLHQ_TRUST_PROXY` disabled unless
the proxy strips any incoming `X-Real-IP` value and supplies its own. Otherwise
clients can forge the address recorded in diagnostics.

The built-in TLS listener is the simplest arrangement for LAN-only deployment.
Web push and PWA installation require HTTPS or `localhost`.

## Account removal

Removing a user immediately revokes that user's sessions and web-push
subscriptions. Account deletion is refused while the account still owns
devices or dashboards. An administrator must explicitly remove those resources
first; HomelabHQ does not currently provide ownership transfer or silently
cascade-delete monitoring configuration.

After owned resources are removed, retrying account deletion removes the user
and their owner-scoped Access roster. Device deletion also removes its encrypted
credential.

## Capacity boundary

The main store is a versioned JSON document. Every mutation serializes that
document, while high-churn chart history is stored in separate bounded files.
Sessions, push subscriptions, stale roster records, client events, and SSH
host-key records have retention limits.

Monitor document size and write duration through `store.metrics()`. Reassess
the storage architecture when write latency becomes operationally significant,
multiple application processes are required, the roster becomes large, query
requirements become material, migrations become frequent, or authorization
requires increasingly complex document scans. The durable decision triggers
are recorded in [architecture.md](architecture.md).

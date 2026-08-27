# Operations

The supported production shape is one Uvicorn worker hosting the HomelabHQ
FastAPI application and using one writable data directory. The supplied Compose
deployment runs that process as an unprivileged user with a read-only root
filesystem. Do not add Uvicorn workers while scheduling, live job coordination,
and the poller remain process-local.

## Health and readiness

- `GET /health` and `GET /api/v1/health` return typed, timestamped liveness
  responses without scanning infrastructure. Compose uses `/health`.
- `GET /api/v1/readiness` checks the frontend configuration and primary JSON
  store under a strict local timeout. It returns `503` when either is
  unavailable.
- `GET /healthz` and `GET /readyz` remain compatibility endpoints. `/readyz`
  additionally waits for a successful poller cycle, as older deployments
  expect.
- `GET /api/v1/status/summary` reports Network, Proxmox, TrueNAS, and Docker
  state from cached or persisted observations. It never launches a scan. Each
  stack includes `source_checked_at`, exposing the age of the actual source
  observation rather than the time the summary request happened.

The application starts Network, Proxmox, TrueNAS, and Docker refreshes
asynchronously as soon as its FastAPI lifespan starts. Their default recurring
intervals are 60, 120, 300, and 300 seconds respectively. Per-job locks prevent
a slow integration from overlapping its next run, and every remote call is
timeout-bounded. One temporary failure records diagnostics but retains the last
successful payload; only its configured age threshold makes that payload
stale. Network reachability also retains its consecutive-failure debounce.
Keeplink background availability uses ICMP so a busy HTTP management plane does
not produce a false outage; its HTTP interface is contacted only for explicit
management and discovery reads.
FastAPI lifespan shutdown interrupts interval waits and joins scheduler workers.

OpenAPI is available at `/openapi.json`, Swagger UI at `/docs`, and ReDoc at
`/redoc`. The schema contains public field contracts and cookie-auth metadata,
not stored credentials or secret values.

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
volumes created by older root-running images. It publishes
`com.homelabhq.lifecycle=oneshot`, allowing status aggregation to ignore a
successful completion while still reporting a failed or currently running
initializer. When replacing the named volume with a bind mount, prepare it
before startup:

```bash
sudo chown -R 10001:10001 ./your-data-directory
```

## Upgrades

Back up the complete data directory and record the current image ID first:

```bash
current_image_id="$(docker inspect --format '{{.Image}}' homelabhq)"
rollback_tag="homelabhq:rollback-$(date +%Y%m%d%H%M%S)"
docker image tag "$current_image_id" "$rollback_tag"
printf '%s\n' "$rollback_tag" > homelabhq.rollback-image
docker compose config > homelabhq.rollback-compose.yml
docker inspect homelabhq > homelabhq.rollback-container.json
git pull --ff-only
docker compose build --pull
docker compose up -d
docker compose ps
```

Before recreating the service, use `docker compose config` to confirm the
`hlhq-data:/data` mapping and ports 8770/8771 remain unchanged. After startup,
inspect logs and verify `/health`, `/api/v1/readiness`, `/openapi.json`, the
frontend, login, and PWA delivery before considering the upgrade complete. Do
not run `docker compose down -v`; the `-v` option removes the named data volume.

If verification fails, preserve the new logs and recreate only the application
service from the recorded tag against the unchanged data volume:

```bash
rollback_tag="$(cat homelabhq.rollback-image)"
HLHQ_IMAGE="$rollback_tag" docker compose up -d --no-build homelabhq
curl -skf https://127.0.0.1:8770/healthz
curl -skf https://127.0.0.1:8770/readyz
```

The `HLHQ_IMAGE` override exists for this rollback path. Never remove or
recreate `hlhq-data` during rollback.

## Proxmox node updates

The Compute host view checks each online Proxmox node's cached apt catalogue.
Its API token needs `Sys.Modify` on the node or `/` path; a read-only
`PVEAuditor` token cannot call Proxmox's update-list endpoint.

Installing updates is a separate privileged operation:

- configure root SSH with a password in Compute, or provide a
  root password/private key when adding the Proxmox device;
- review and test backups before starting;
- HomelabHQ updates the explicitly selected node with `apt-get update` followed
  by a non-interactive `apt-get dist-upgrade`;
- services can restart during package installation, but package installation
  never reboots a node automatically; and
- Compute shows the real package-manager stage and a structured reboot status
  immediately after the update. The fixed SSH runner does not stream reliable
  package percentages or a current package, so preparing, metadata download,
  install, and reboot-check stages are explicitly indeterminate. Only a
  terminal result is reported as an exact 100 percent.

Reboot detection is conservative and ordered. It reads the running kernel,
honours Proxmox next-boot or permanent kernel pins, consumes `needrestart -b -k`
when available, and then falls back to installed boot images selected remotely
with Debian's version comparison semantics. `proxmox-boot-tool kernel list`
validates selected bootable kernels where its documented text format is
available. `/var/run/reboot-required` is an independent additional signal.
Failed or inconclusive probes produce **Reboot status unknown**, never **No
reboot required**. Compute refresh runs the same canonical check, so updates
installed outside HomelabHQ are detected. Package-update success and reboot
check success remain separate outcomes.

When a fresh check says **Reboot required**, an administrator may use **Reboot
node**. The action requires configured root SSH, an online exact node, and a
second server-side reboot-status check after confirmation. It sends the fixed
`systemctl reboot --no-wall` command and does not accept arbitrary command text.
The command acknowledgement is not a health check: wait for the node and its
workloads to return, then refresh Compute to verify the new running kernel.
Update installs and reboots share one runtime operation slot per Proxmox
Device, so they cannot overlap. The operation snapshot is persisted after each
stage and terminal transition. Compute keys it by exact node and task ID, keeps
sibling node results isolated, rejects stale task snapshots, and restores the
correct node after a browser/PWA reload or temporary API failure.

This button is for regular package updates, not a Proxmox/Debian major-version
upgrade. Follow Proxmox's release-specific upgrade procedure for major
versions. The SSH process cannot be reattached after a HomelabHQ backend
restart; the persisted running snapshot is converted to an explicit failed,
interrupted result on the next status read. Inspect apt/dpkg state directly on
the named affected node before retrying.

## Compute maintenance

Compute presents Proxmox host updates, but their direct API/SSH backend remains
separate from Ansible guest maintenance.
Before enabling them, verify controller host-key trust, inventory scope, each
approved playbook, and workload backups. The controller's configured execution
timeout bounds each Ansible process. Update and Docker jobs are persisted; one
may be active per workload.

Compute maintenance capabilities come from current Ansible inventory groups:
`debian_hosts` for OS work, `docker_hosts` for Docker work, and `appliances`
for the controller-local appliance API health check. Approval target/group
restrictions remain an additional narrowing boundary. After deployment,
refresh Ansible inventory once so incompatible legacy current-state caches are
removed; historical jobs are retained.

If HomelabHQ restarts while a job is queued or running, it marks the job failed
because it cannot safely reattach to an already-started remote process. Inspect
the target and the Ansible controller before retrying. Review the job's PLAY
RECAP and sanitized output in Compute detail. For a failed update that permitted
reboot, also confirm whether the target rebooted before submitting another job.

See [Compute and Ansible maintenance](compute.md) for setup, contracts, security
constraints, Docker update modes, and the `HLHQ_MAX_COMPUTE_JOBS` history bound.

## Morning update checks and PWA notifications

The persistent scheduler evaluates the configured IANA timezone and local time
every 30 seconds. It runs at most once for a scheduled local date, including
across process restarts and daylight-saving changes. A flock-protected store
transaction creates both the run record and a leased lock before any remote
work starts. Manual and scheduled runs return `409` while that lock is active;
existing per-workload Compute locks still prevent a scheduled check from
duplicating a manual Ansible job on the same target.

The run is read-only and ordered:

1. approved Compute OS and per-project Docker check playbooks run through the
   existing persisted maintenance-job service; the existing Proxmox catalogue
   and reboot probe supplies node package/kernel results; and
2. included Devices exposing the named `check_updates` action run through the
   same driver action as the Devices-page button.

Failures, unreachable targets, and unsupported providers remain distinct from
up-to-date results. Other targets continue. Results merge by persistent Device
or Compute ID; a unique exact inventory-host-to-Device-host association may
provide the mapping, but display names never do. Proxmox cluster nodes use the
stable parent-Device-and-node identity. Source rows remain separate in the run
record and notification counts use unique merged identities.

Web Push requires HTTPS (or browser-local `localhost`), an authenticated user
gesture to enable notifications, and a retained subscription. HomeLabHQ creates
and stores its VAPID keypair under `/data/secrets` on first use; back up both
VAPID files with the rest of `/data`, because rotating them can invalidate
subscriptions. Set `HLHQ_VAPID_SUB` to a `mailto:` or HTTPS URI on a real domain
you control before production delivery. The VAPID private key and subscription
endpoints are never included in application log fields or check-run delivery
records.

Each notification is written to the owner-scoped notification centre before
delivery is attempted. The API's unread count is the single source for the
header count and PWA app badge; startup, foregrounding, periodic refresh, read,
and dismiss actions all reconcile that count. A push-provider failure leaves
the in-app entry intact, and a zero API count explicitly clears a stale app
badge. Opening the PWA or notification panel does not mark entries read.

On iPhone and iPad, [Web Push is available to Home Screen web apps from iOS and
iPadOS 16.4](https://webkit.org/blog/13878/web-push-for-web-apps-on-ios-and-ipados/),
and permission must be requested from an explicit user interaction; install
HomelabHQ to the Home Screen before enabling it. Android behavior depends on
the installed browser and OS notification policy, and [its standard collapsed
notification text is truncated by default](https://developer.android.com/develop/ui/compose/notifications/create-notification).
Both platforms may collapse,
truncate, or omit expanded notification text, so the notification always links
to the complete run at
`/devices?filter=needs-attention&checkRun={run_id}`. HomeLabHQ does not depend on
notification action buttons because the web `actions` feature has
[limited browser availability](https://developer.mozilla.org/en-US/docs/Web/API/Notification/actions).

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
5. Start HomelabHQ and check logs, `/health`, and `/api/v1/readiness`.

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
Sessions, push subscriptions, stale roster records, client events, SSH host-key
records, and Compute maintenance jobs have retention limits.

Monitor document size and write duration through `store.metrics()`. Reassess
the storage architecture when write latency becomes operationally significant,
multiple application processes are required, the roster becomes large, query
requirements become material, migrations become frequent, or authorization
requires increasingly complex document scans. The durable decision triggers
are recorded in [architecture.md](architecture.md).

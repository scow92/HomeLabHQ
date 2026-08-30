# HTTP API reference

HomelabHQ's web application uses a JSON HTTP API under `/api`. This catalogue
documents the compatibility interface. New operational endpoints use the
versioned `/api/v1` namespace. FastAPI publishes the generated OpenAPI document
at `/openapi.json`, Swagger UI at `/docs`, and ReDoc at `/redoc`.

## Conventions

- Request and response bodies are JSON unless an export endpoint says
  otherwise.
- Authentication uses the HttpOnly session cookie returned by setup or login.
- Mutating requests from a browser must pass the server's same-origin checks.
- Routes are authenticated by default. The tables call out public and
  administrator-only exceptions.
- Resource access remains owner-scoped even when an administrator can see
  resources belonging to other users.
- Errors use an appropriate HTTP status and JSON fields `error` (safe message),
  `code` (stable machine-readable type), and `requestId` (log correlation).
- Unknown `/api/*` routes always return JSON 404 responses. They never enter
  frontend fallback routing.

## Operations

| Method | Path | Access | Purpose |
|---|---|---|---|
| `GET` | `/health` | Public | Lightweight process liveness with version and timezone-aware timestamp. |
| `GET` | `/api/v1/health` | Public | Versioned alias of the lightweight liveness response. |
| `GET` | `/api/v1/readiness` | Public | Strict-timeout checks of local configuration and the JSON datastore. |
| `GET` | `/api/v1/status/summary` | Public, read-only | Aggregate cached Network, Proxmox, TrueNAS, and Docker monitoring state without triggering a scan. |

The status summary uses `healthy`, `warning`, `critical`, `unknown`, and
`stale`. Missing polls are unknown; aged observations are stale; confirmed
outages are critical; and degraded-but-operational states are warnings. Docker
aggregation distinguishes running health checks, restarting containers,
unexpected stops, unknown lifecycle expectations, and successful one-shot
containers. Successful one-shot completions are excluded from Docker `total`
and `healthy`; failed initializers remain counted failures. Lifecycle intent is
derived from the `com.homelabhq.lifecycle=oneshot` label, published expectation,
Compose completion dependencies, restart policy, and exit code rather than a
container-name convention. Compose's `com.docker.compose.oneoff` label is not
used as the marker for normal init services because Compose publishes it as
`False` for those services. A labelled initializer that exits non-zero reports
the stable issue code `oneshot_failed`; while it is running, its current state
and health are counted normally.

`network.components` identifies every configured Device included in the Network
count as `name (host)` (or the best available one of those fields). The intended
inventory is the intersection of configured Device records and the explicit
network-driver allowlist: OPNsense firewall, OpenWrt, Keeplink switch, and Zyxel
AP. Client discoveries, generic devices, Proxmox, TrueNAS, and Docker workloads
are never counted. This makes the seven entries in the deployed response the
auditable inventory rather than an unexplained total. Issue component labels
use the same host qualification, including Docker containers, so repeated
service names on different hosts remain distinct.

Each stack object includes nullable `source_checked_at` in timezone-aware UTC.
It is the oldest successful source observation contributing to that stack, so
clients can display the real data age. `checked_at` remains the aggregate
response observation time. The endpoint is cache-only: requests never dispatch
any of the background monitoring jobs.

TrueNAS health requires current pool and non-dismissed-alert observations.
ONLINE pools with zero active alerts are healthy, DEGRADED pools and warning
alerts are warnings, and critical/error alerts are critical. Missing data is
unknown and aged data is stale. Issue records contain only a host-qualified
component label, safe code, message, and status.

## Session and account

| Method | Path | Access | Purpose |
|---|---|---|---|
| `GET` | `/api/session` | Public | Return authentication state, setup requirement, and current user. |
| `POST` | `/api/setup` | Public | Create the first administrator and start a session. |
| `POST` | `/api/login` | Public | Authenticate and start a session. |
| `POST` | `/api/logout` | Public | End the current session and clear its cookie. |
| `POST` | `/api/account/password` | Authenticated | Change the current user's password and revoke their other sessions. |

## Administration

| Method | Path | Access | Purpose |
|---|---|---|---|
| `GET` | `/api/users` | Administrator | List users. |
| `POST` | `/api/users` | Administrator | Create a member or administrator. |
| `DELETE` | `/api/users?id={user_id}` | Administrator | Remove a user after owned resources are resolved. |
| `GET` | `/api/logs` | Administrator | Return the redacted in-memory diagnostic log. |
| `DELETE` | `/api/logs` | Administrator | Clear the diagnostic log. |
| `GET` | `/api/diagnostics/metrics` | Administrator | Return safe process-local store-write, poll-cycle, per-device poll, and selected request-latency observations for a measured capacity baseline. |

## Drivers and devices

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/drivers` | List drivers and transports; optionally filter with `transport`. |
| `GET` | `/api/devices` | List visible devices. |
| `POST` | `/api/devices/detect` | Probe a host and return ranked driver candidates. |
| `POST` | `/api/devices/entities` | Enumerate sensors, controls, and driver capabilities. |
| `POST` | `/api/devices` | Create a device and encrypted credential. |
| `POST` | `/api/devices/reorder` | Persist an ordered list of device IDs. |
| `DELETE` | `/api/devices?id={device_id}` | Delete a device, credential, and history. |
| `PATCH` | `/api/devices/{device_id}` | Update name, dashboard, entities, hidden interfaces, driver, alerts, or `includeInScheduledUpdateChecks`. |
| `GET` | `/api/devices/{device_id}/history?key={key}&range={range}` | Read stored values for one numeric entity. |
| `GET` | `/api/devices/{device_id}/state` | Perform a live device read. |
| `GET` | `/api/devices/{device_id}/series?metric={metric}&id={id}` | Read a driver-specific time series. |
| `GET` | `/api/devices/{device_id}/detail` | Read entity metadata, detail tables, and history; client-identity tables are enriched from the device owner's roster. |
| `POST` | `/api/devices/{device_id}/action` | Invoke a named opt-in driver action. |
| `GET` | `/api/devices/{device_id}/updates` | Read live vendor update availability and the latest Proxmox maintenance operation. |
| `GET` | `/api/devices/{device_id}/updates/status` | Poll the latest Proxmox update or reboot operation without repeating package discovery; returns the persisted terminal/interrupted snapshot after a backend restart. |
| `POST` | `/api/devices/{device_id}/updates/install` | Start an asynchronous update installation for the optional exact `node` in the JSON body; returns `202`. |
| `POST` | `/api/devices/{device_id}/updates/reboot` | Administrator-only: recheck and reboot the exact Proxmox `node` when the body includes `confirmed: true` and fresh status is reboot-required; returns `202`. |
| `POST` | `/api/devices/{device_id}/updates/credentials` | Verify and encrypt privileged SSH credentials used for updates. |
| `GET` | `/api/devices/{device_id}/vpn-endpoints` | Read all owner-scoped VPN profiles, current WireGuard health, discovery candidates and bounded history. The first profile remains at the response root for compatibility. |
| `POST` | `/api/devices/{device_id}/vpn-endpoints` | Create an additional VPN endpoint profile. |
| `PATCH` | `/api/devices/{device_id}/vpn-endpoints` | Partially update the first VPN endpoint profile; retained for the single-profile compatibility period. |
| `GET` | `/api/devices/{device_id}/vpn-endpoints/choices` | List OPNsense WireGuard instances and peers plus provider-backed country and city choices available to the profile. |
| `POST` | `/api/devices/{device_id}/vpn-endpoints/compatibility` | Save one manual target validation for a discovered candidate. |
| `POST` | `/api/devices/{device_id}/vpn-endpoints/switch` | Apply and verify a confirmed preferred or eligible replacement, with rollback on failure. |
| `GET` | `/api/devices/{device_id}/vpn-endpoints/{profile_id}` | Refresh one profile, including the endpoint currently configured on its OPNsense peer. |
| `PATCH` | `/api/devices/{device_id}/vpn-endpoints/{profile_id}` | Partially update one profile without changing the others. |
| `DELETE` | `/api/devices/{device_id}/vpn-endpoints/{profile_id}` | Remove a confirmed HomeLabHQ manager profile without changing OPNsense. |
| `POST` | `/api/devices/{device_id}/vpn-endpoints/{profile_id}/compatibility` | Save one profile-scoped manual validation. |
| `POST` | `/api/devices/{device_id}/vpn-endpoints/{profile_id}/switch` | Apply and verify a replacement on the selected profile’s peer. |
| `GET` | `/api/devices/{device_id}/firewall/all` | List firewall rules exposed by the driver. |
| `POST` | `/api/devices/{device_id}/firewall/toggle` | Enable or disable one firewall rule. |
| `POST` | `/api/devices/{device_id}/firewall/rules` | Select rules managed from the Access view. |
| `GET` | `/api/devices/{device_id}/nac/interfaces` | List interfaces available for access-control setup. |
| `GET` | `/api/devices/{device_id}/nac/aliases` | List firewall aliases available for access control. |
| `POST` | `/api/devices/{device_id}/nac/setup` | Configure network-access control for the device. |
| `POST` | `/api/devices/{device_id}/nac/approve` | Approve or revoke one or more client MAC addresses. |
| `POST` | `/api/devices/{device_id}/nac/enforcement` | Enable or disable access-control enforcement. |
| `POST` | `/api/devices/{device_id}/binding` | Enable or disable AP client binding. |
| `POST` | `/api/devices/{device_id}/bind-client` | Bind or unbind a client MAC to the selected AP. |

Detection and creation accept a `credentials` object appropriate to the
transport:

| Transport | Credential fields |
|---|---|
| `ssh` | `username`; either `password` or `privateKey` |
| `snmp` | `community`; `version` (`2c` or `1`) |
| `api` | `apiKey`, `apiSecret`, `authStyle`, `scheme`, `basePath`, `probePath`, `verifyTls`, optional header names, and driver-specific encrypted secondary credentials |
| `http` | `username`, `password`, `scheme`, `basePath`, `probePath`, and `verifyTls` |

Credentials are encrypted at rest and are never returned by device-list
responses.

Proxmox update discovery uses its API token and requires `Sys.Modify` on the
nodes. Installation additionally requires root SSH credentials, stored as
`updateSsh` (`username`, `port`, and either `password` or `privateKey`) inside
the encrypted device credential. The update APIs expose only a configured
boolean, never that object.

Each public Proxmox catalogue node includes a `reboot` object with
`rebootStatus` (`required`, `not_required`, or `unknown`), nullable
`rebootRequired`, a reason, running and target kernels when known, individual
signals, and an ISO-8601 check time. Update jobs retain package-update outcome
and reboot-check outcome separately. `GET /api/compute` also returns safe host
records backed by the latest persisted node maintenance summary. Each node's
persisted `packages` retain `name`, installed/current and available/candidate
versions, description, section, repository `source` when Proxmox reports it,
and nullable `security` classification. `security: true` is emitted only for an
explicit security archive/site; unknown packages are not labelled non-security.

Update operations contain a stable device ID and task `id`; each node result
also carries that `taskId`, exact node name, state, stage, progress mode,
nullable percentage/current package, timestamps, message, outcome, and reboot
result. Active apt stages use `progressMode: indeterminate`; terminal results
use an exact 100 percent.

## Compute

Compute list/detail routes are owner-scoped; administrators can see every
workload. Configuration and mutating update routes are administrator-only as
shown. Check/discovery jobs may be requested by the workload owner.

| Method | Path | Access | Purpose |
|---|---|---|---|
| `GET` | `/api/compute` | Authenticated | List visible VM/LXC workloads with their parent Device summaries, approval-aware update eligibility, active-maintenance flags, and aggregate host, workload, Docker lifecycle, and healthcheck counts. Docker containers expose separate `state`, nullable `hasHealthcheck`, nullable `health`, lifecycle `exitCode`, nullable `oneShot`, and optional bounded `healthDetails`. |
| `POST` | `/api/compute/refresh` | Administrator | Refresh providers and Ansible inventory, then queue each workload's eligible Docker discovery, OS update check, and one Docker update check per discovered Compose project as an ordered maintenance sequence. Provider and workload entries include display names for per-task UI diagnostics; refresh and maintenance issues are also written to the structured application log. |
| `GET` | `/api/compute/{compute_id}` | Authenticated | Read available workload, parent, management, update, and Docker detail. |
| `POST` | `/api/compute/{compute_id}/ansible` | Administrator | Confirm or change a mapping with `{enabled: true, controllerId, inventoryHost}` and optional fixed `maintenance` operation references, or disable it with `{enabled: false}`. The response contains the persisted mapping and approval-aware action eligibility. |
| `POST` | `/api/compute/{compute_id}/health/check` | Authenticated owner or administrator | Queue the approved controller-local appliance API health check; the discovered target must belong to `appliances`. |
| `GET` | `/api/compute/{compute_id}/jobs` | Authenticated | List recent persisted maintenance jobs. |
| `GET` | `/api/compute/jobs/{job_id}` | Authenticated | Read one owner-visible job, recap, structured result/source/error, and sanitized logs. |
| `POST` | `/api/compute/{compute_id}/updates/check` | Authenticated | Queue the approved OS update-check playbook. |
| `POST` | `/api/compute/{compute_id}/updates` | Administrator | Queue the approved OS update playbook; `allowReboot` defaults false and true also requires `rebootConfirmed`. |
| `POST` | `/api/compute/{compute_id}/docker/check` | Authenticated | Queue the approved Docker update-check playbook for body `{projectName}`. The sole discovered project may be inferred; multiple projects always require an explicit name. |
| `POST` | `/api/compute/{compute_id}/docker/discover` | Authenticated | Queue approved structured Docker/Compose discovery. |
| `POST` | `/api/compute/{compute_id}/docker/projects/{project_name}/strategy` | Administrator | Set `{mode}` to the validated enum `pull`, `build`, or `read_only` for the exact discovered inventory project name. Documented legacy aliases are migrated for compatibility. |
| `POST` | `/api/compute/{compute_id}/docker/projects/{project_name}/update` | Administrator | Queue the approved generic mode-aware Docker update playbook, or an existing separate-mode fallback, for the exact discovered inventory project name. |

## Ansible settings

All Ansible settings routes are administrator-only. Credential values are
write-only; `credentialConfigured` is returned instead.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/settings/ansible` | Read the safe controller configuration, inventory summary, discovered files, and approvals. |
| `POST` | `/api/settings/ansible` | Save controller connection, encrypted credential, contained project paths, absolute Ansible executable paths, and timeouts. |
| `POST` | `/api/settings/ansible/test` | Discover or validate executable paths and test SSH, project, inventory parsing, version, host count, and group count. |
| `POST` | `/api/settings/ansible/inventory` | Refresh hosts/groups using `ansible-inventory --list`. |
| `POST` | `/api/settings/ansible/playbooks` | Discover `.yml`/`.yaml` files below the configured playbooks directory. |
| `POST` | `/api/settings/ansible/playbooks/approve` | Approve or revoke one discovered file for one fixed operation and its restricted metadata: label, check-mode support, allowed targets/groups/variable names, reboot variable, or Docker project/mode variables and supported modes. |

Compute instance responses include `ansible.capabilities` with
`osMaintenance`, `dockerMaintenance`, and `applianceHealth` booleans derived
from the current discovered inventory host record. Approval-aware eligibility
fields may narrow those capabilities further. Appliance health is returned in
`applianceHealthState`, separately from OS and Docker maintenance projections.

The OS update, Docker discovery, and Docker update-check contracts are documented
in [Compute and Ansible maintenance](compute.md).

## Dashboards

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/dashboards` | List visible dashboards. |
| `POST` | `/api/dashboards` | Create a named dashboard. |
| `PATCH` | `/api/dashboards/{dashboard_id}` | Update dashboard name or order. |
| `DELETE` | `/api/dashboards?id={dashboard_id}` | Delete a dashboard and leave its devices unassigned. |

## Access roster

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/clients` | Return the current owner-scoped client roster and source summary. |
| `POST` | `/api/clients/refresh` | Refresh the roster from configured devices. |
| `GET` | `/api/clients/history?mac={mac}` | Return history for one client. |
| `GET` | `/api/clients/events?since={timestamp}` | Count connection events and newly discovered clients after a timestamp. |
| `GET` | `/api/clients/export?format={json|csv}` | Download the current roster. |
| `POST` | `/api/clients/forget` | Forget one MAC or a supplied list of MAC addresses. |

The client-events response keeps connection-history events in `count` and
returns newly discovered, visible roster entries in `newCount`. The Access tab
uses `newCount` for its unread badge, so reconnecting a known device does not
create a notification.

## Network-access configuration

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/nac/config` | Return owner-scoped access-control configuration. |
| `POST` | `/api/nac/config` | Set managed aliases and DNS synchronization options. |
| `POST` | `/api/nac/ignore` | Toggle ignored state for a client MAC. |
| `POST` | `/api/nac/client/membership` | Read firewall alias membership for a client. |
| `POST` | `/api/nac/client` | Edit a client, notification settings, DNS sync, and aliases. |
| `POST` | `/api/nac/alias` | Create a managed firewall alias. |

## Web push

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/push/vapid` | Return the instance VAPID public key. |
| `POST` | `/api/push/subscribe` | Save a browser push subscription for the current user. |
| `POST` | `/api/push/unsubscribe` | Remove a subscription owned by the current user. |
| `POST` | `/api/push/test` | Send a test notification to the current user's subscriptions. |
| `GET` | `/api/notifications` | Return every unread notification, recent read notifications, and the authoritative unread count for the current user. |
| `POST` | `/api/notifications/read-all` | Mark every visible notification read and return the recalculated unread count. |
| `POST` | `/api/notifications/{notification_id}/read` | Mark one owner-scoped notification read and return the recalculated unread count. |
| `POST` | `/api/notifications/{notification_id}/dismiss` | Persistently dismiss one owner-scoped notification and return the recalculated unread count. |

Notification records are committed before Web Push delivery is attempted. A
provider rejection therefore affects only delivery, not the in-app record.

## Morning update checks

Schedule configuration is administrator-managed. Notification-class
preferences and push subscriptions remain per authenticated user. Run results
are owner-filtered for members; administrators see the complete merged run.

| Method | Path | Access | Purpose |
|---|---|---|---|
| `GET` | `/api/settings/morning-updates` | Authenticated | Read schedule configuration, the current user's notification preferences and subscription count, next occurrence, and last visible run summary. |
| `POST` | `/api/settings/morning-updates` | Authenticated | Save the current user's `notifications`; administrators may also save global `config`. |
| `POST` | `/api/morning-updates/run` | Administrator | Start one asynchronous manual run; returns `202`, or `409` if a run/target lock prevents duplication. |
| `GET` | `/api/morning-updates/runs/latest` | Authenticated | Read the latest owner-filtered run and its source-level device results. |
| `GET` | `/api/morning-updates/runs/{run_id}` | Authenticated | Read one owner-filtered persisted run for notification-click routing. |

The settings `config` contains `enabled`, `runTime`, `timezone`,
`runAnsibleChecks`, `runDeviceNativeChecks`, and `deviceTimeoutSeconds`.
Notification preferences contain `notifyUpdates`, `notifyFailures`, and
`notifySuccess`. A run retains phase status, unique counts, failed/unreachable
counts, unsupported devices, per-device source arrays, and non-secret delivery
outcomes.

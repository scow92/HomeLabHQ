# HTTP API reference

HomelabHQ's web application uses a JSON HTTP API under `/api`. This catalogue
documents the current application interface; it is not yet a separately
versioned compatibility contract.

## Conventions

- Request and response bodies are JSON unless an export endpoint says
  otherwise.
- Authentication uses the HttpOnly session cookie returned by setup or login.
- Mutating requests from a browser must pass the server's same-origin checks.
- Routes are authenticated by default. The tables call out public and
  administrator-only exceptions.
- Resource access remains owner-scoped even when an administrator can see
  resources belonging to other users.
- Errors use an appropriate HTTP status with a JSON `error` message.

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
| `PATCH` | `/api/devices/{device_id}` | Update name, dashboard, entities, hidden interfaces, driver, or alerts. |
| `GET` | `/api/devices/{device_id}/history?key={key}&range={range}` | Read stored values for one numeric entity. |
| `GET` | `/api/devices/{device_id}/state` | Perform a live device read. |
| `GET` | `/api/devices/{device_id}/series?metric={metric}&id={id}` | Read a driver-specific time series. |
| `GET` | `/api/devices/{device_id}/detail` | Read entity metadata, detail tables, and history. |
| `POST` | `/api/devices/{device_id}/action` | Invoke a named opt-in driver action. |
| `GET` | `/api/devices/{device_id}/updates` | Read live vendor update availability and the latest install operation. |
| `GET` | `/api/devices/{device_id}/updates/status` | Poll an update installation without repeating package discovery. |
| `POST` | `/api/devices/{device_id}/updates/install` | Start an asynchronous update installation; returns `202`. |
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

## Compute

Compute list/detail routes are owner-scoped; administrators can see every
workload. Configuration and mutating update routes are administrator-only as
shown. Check/discovery jobs may be requested by the workload owner.

| Method | Path | Access | Purpose |
|---|---|---|---|
| `GET` | `/api/compute` | Authenticated | List visible VM/LXC workloads and aggregate counts. |
| `POST` | `/api/compute/refresh` | Administrator | Refresh Proxmox providers, Ansible inventory, and queue approved Docker discovery. |
| `GET` | `/api/compute/{compute_id}` | Authenticated | Read available workload, parent, management, update, and Docker detail. |
| `POST` | `/api/compute/{compute_id}/ansible` | Administrator | Confirm or change a mapping with `{enabled: true, controllerId, inventoryHost}`, or disable it with `{enabled: false}`. The response contains the persisted workload mapping and action eligibility. |
| `GET` | `/api/compute/{compute_id}/jobs` | Authenticated | List recent persisted maintenance jobs. |
| `GET` | `/api/compute/jobs/{job_id}` | Authenticated | Read one owner-visible job, recap, structured result, and sanitized logs. |
| `POST` | `/api/compute/{compute_id}/updates/check` | Authenticated | Queue the approved OS update-check playbook. |
| `POST` | `/api/compute/{compute_id}/updates` | Administrator | Queue the approved OS update playbook; `allowReboot` defaults false and true also requires `rebootConfirmed`. |
| `POST` | `/api/compute/{compute_id}/docker/check` | Authenticated | Queue the approved Docker update-check playbook. |
| `POST` | `/api/compute/{compute_id}/docker/discover` | Authenticated | Queue approved structured Docker/Compose discovery. |
| `POST` | `/api/compute/{compute_id}/docker/projects/{project_id}/strategy` | Administrator | Select `pull`, `local_build`, or `unmanaged`. |
| `POST` | `/api/compute/{compute_id}/docker/projects/{project_id}/update` | Administrator | Queue the approved strategy-specific update playbook. |

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
| `POST` | `/api/settings/ansible/playbooks/approve` | Approve or revoke one discovered file for one fixed maintenance operation and its restricted metadata. |

The structured playbook result and Docker discovery contracts are documented
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

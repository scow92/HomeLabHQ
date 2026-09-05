# Compute and Ansible maintenance

Compute represents infrastructure hosts and their workloads. A configured
Proxmox host remains a Device for API monitoring and credentials, while its
node-level package updates and reboot status are presented under **Compute**.
Discovered virtual machines and LXC containers retain a link to that parent
Device. Proxmox host maintenance uses the canonical direct API/SSH service;
Ansible maintenance remains scoped to guests.

## Configure and discover

1. Add a Proxmox VE Device through the existing Add device wizard.
2. Open **Compute** as an administrator and choose **Refresh All**. HomeLabHQ reads
   `/cluster/resources` using the Device's existing API connection and upserts
   VMs and LXCs by parent Device and provider ID. The same refresh reads fresh
   package and reboot state for every Proxmox node with configured root SSH.
   Reboot-check failure is reported independently and does not fail workload
   discovery. When workloads are present
   but Ansible is not enabled, Compute links to **Settings** and labels update
   and Docker data as requiring Ansible rather than as unknown probe results.
   This button is an on-demand refresh; it is not required to keep the page
   current. Background monitoring starts immediately with HomeLabHQ and repeats
   Proxmox refreshes every two minutes and Docker discovery every five minutes.
3. Open **Settings → Ansible**, enter the controller connection and contained
   project paths, then save. Executable paths may initially be blank.
4. Use **Test Connection**. The test verifies SSH, the project directory,
   `ansible-playbook`, `ansible-inventory`, inventory JSON parsing, and reports
   each executable's absolute path, the Ansible version, and inventory counts.
   Review any discovered executable paths copied into the form, then save them.
5. Use **Discover / Refresh Inventory**. Ansible inventory remains authoritative;
   HomeLabHQ stores only its host addresses, groups, and relationships.
6. Use **Discover Playbooks**, then explicitly approve one discovered playbook
   for each operation it may perform. Filenames are never assumed. Give each
   approval a friendly label and, where needed, restrict it to discovered
   inventory hosts or groups.
7. Open a Compute workload, select its inventory host, and choose **Manage
   selected host**. Selecting **Not managed by Ansible** changes the action to
   **Stop managing with Ansible** so removing a mapping is explicit.
   Exact guest names and IP addresses may be suggested, but suggestions are
   never saved automatically; when there is a likely match, the detail view
   offers a one-click **Manage with Ansible as _host_** confirmation. The
   confirmed mapping is stored on the Compute workload and remains
   authoritative after reload; names are not compared again while rendering.

If an update-check or Docker-discovery playbook has not been approved, the
mapped workload explains which approval is missing and links back to
**Settings → Ansible**. An approved Compose project also explains when its
Docker update-check playbook is unavailable and links administrators to the
required `docker_project` approval. The detail view does not render a disabled
check button that looks actionable. Actions that are temporarily unavailable
while their project is being checked or updated remain visibly disabled.

A mapped workload whose checks have not run shows **Updates: Unknown**. When the
provider reports workload placement, Compute groups workloads by that node and
shows the parent Device as the discovery path; otherwise it groups them beneath
the parent Device. Clicking anywhere on a compact workload card opens its detail
view, where approved maintenance actions remain available. The detail view
keeps **Recent maintenance** collapsed until it is selected, including the
individual job output disclosures inside it. A representative production
baseline was reviewed privately and identified duplicated successful Docker
discovery output as a bounded persistence issue. HomeLabHQ retains the newest
successful discovery for each Compute instance in full. Older successful
discoveries remain visible with their audit metadata, summary, and recap, while
duplicated stdout, stderr, and structured results are compacted. Failed and
incomplete discoveries retain their full diagnostics. The JSON store remains
in use; SQLite, separate history storage, and multi-process support remain
deferred. Post-deployment measurements must be repeated privately before
Phase 2 begins.

Docker is shown on a card only when that workload has
discovered containers; the compact preview shows container names, aggregate
health, and actionable Docker update status. Host and project summaries say
**Running** or **Operational** when unchecked containers are running; **Healthy**
is reserved for sets where every container has an explicitly healthy check.
Changing the mapped target clears results collected from the previous target so
stale maintenance or Docker data cannot be attributed to the new one.

Each Proxmox node has its own native, keyboard-operable update disclosure. Its
collapsed label reports the node's count or **Up to date**; opening it shows
package name, current and candidate versions, repository source when supplied,
and only reliably identified security updates. Disclosure state, loading,
errors, packages, operation progress, and terminal results are node-scoped.
The last successful package list is persisted and remains visible with a
refresh-failed warning when a later API check fails.
After a successful update, a fresh zero-update check with an explicit
no-reboot-required result replaces the benign terminal progress card with the
normal **Up to date** state. Failed, reboot-required, unknown, and unreconciled
results remain visible.

The **Need Attention** filter includes its current workload count, including
zero, and uses the same health and update-status rules as the filtered cards.
**Update All** runs the approved OS update workflow for every workload that has
an available update, is running on a reachable parent, supports OS updates, and
has no maintenance job in progress. HomeLabHQ asks for one confirmation, starts
at most three updates concurrently, continues after individual failures, and
reports succeeded, failed, and skipped totals before refreshing Compute data.
Its **More details** disclosure identifies every workload, why an ineligible
workload was skipped, and each eligible workload's live queue/run/result state.
Bulk OS updates do not permit reboots.

For a Proxmox host with a fresh **Reboot required** result, configured root SSH,
and online node state, administrators can choose **Reboot node**. HomeLabHQ asks
for explicit confirmation, rechecks the exact node and reboot requirement, then
sends only its fixed `systemctl reboot --no-wall` command. The node and its
workloads can be unavailable during restart. Once the command is accepted, the
transient reboot progress clears and a success notification is shown. The cached
requirement changes to unknown until a later Compute refresh verifies the running
kernel; accepting the command is not treated as proof that the node returned
successfully.
Compute omits the reboot summary and kernel details when that refresh confirms
that no reboot is required, while retaining the node's maintenance actions.

Direct Proxmox update state is keyed by parent Device, exact node, and task ID.
The existing backend serializes direct maintenance per Proxmox Device, so while
`pve1` is active sibling nodes say **Waiting — update running on pve1** rather
than **Installing**. Poll responses for another task, older snapshots, and node
records whose `taskId` does not match are ignored. Refreshing or reopening the
PWA restores the persisted operation on the correct node.

Missing guests become stale after a successful provider refresh. A failed
provider refresh retains its workloads and marks them unavailable. **Refresh
All** also refreshes the configured Ansible inventory and queues each mapped
workload's eligible Docker discovery, OS update check, and one Docker update
check per discovered Compose project as an ordered sequence. Sequences may run
in parallel across workloads, but
only one maintenance playbook runs at a time for an individual workload.
The Refresh All **More details** disclosure retains the latest provider,
inventory, workload, project, and result summary after refresh completes so a
failure toast can be traced to the exact check. Provider, inventory, queueing,
and maintenance-job failures also emit named, redacted Compute events to the
administrator **Logs** tab and the HomeLabHQ structured process log.

### Executable discovery and validation

Test Connection honors an explicitly configured executable path. When a path
is blank, it first runs these commands in the non-interactive SSH session:

```sh
command -v ansible-playbook
command -v ansible-inventory
```

For a command not found there, HomeLabHQ resolves the authenticated SSH user's
home directory from the remote session; it does not construct
`/home/<username>`. It checks `~/.local/bin`, `/usr/local/bin`, and `/usr/bin`
for the corresponding executable. This covers common pipx and distribution
installs. Enter the full executable paths manually for a virtual environment or
another layout.

Paths must be absolute remote paths without shell metacharacters or whitespace.
Test Connection and every later Ansible invocation verify that the selected
path is a regular executable file. Inventory refreshes and maintenance jobs use
the exact persisted executable paths, independent of the remote session's
`PATH`.

## Approved operations

The six canonical operations exposed by HomeLabHQ are:

- appliance API health check;
- OS update check;
- OS update;
- Docker discovery;
- Docker update check;
- Docker update, with an approved `pull` and/or `build` mode.

There is no general command, arbitrary playbook, arbitrary argument, or remote
shell endpoint. A playbook must be below the configured playbooks directory,
present in the latest discovered allowlist, and approved for the requested
operation. Targets must be present in the latest discovered inventory and, when
the approval defines allowed hosts or groups, must also match that restriction.
A discovered playbook is never executable merely because it exists.

Inventory membership is an intrinsic capability boundary, independently of
the optional approval restrictions: OS checks and updates require
`debian_hosts`; Docker discovery, checks, and updates require `docker_hosts`;
and appliance health requires `appliances`. HomeLabHQ resolves these groups
from the latest controller inventory record. API callers and persisted Compute
mappings cannot supply or override group claims. An approval's allowed targets
or groups can narrow that capability further, but an empty approval restriction
does not broaden it. Membership in `appliances` alone never enables APT or
Docker operations.

Inventory refresh removes incompatible cached OS/Docker current-state
projections when group membership changes, while retaining the corresponding
jobs as maintenance history. Compatible state is preserved. Appliance API
health is stored separately as `applianceHealthState`; it does not change OS,
Docker, provider discovery, or parent Device availability.

For Home Assistant OS, approve the existing `homeassistant-health.yml` only as
the appliance health operation and limit it to `appliances` if an additional
approval restriction is desired. The playbook runs on the Ansible controller
and checks the authenticated REST API. HomeLabHQ does not substitute an
Ansible SSH ping, fact gathering, sudo, APT, or Docker operation for that
health check. A successful playbook execution records the appliance as
available; an API/playbook failure records a separate health failure. Ansible
output is redacted before persistence and browser exposure, including bearer,
token, password, private-key, and secret-shaped values.

Each approval stores a friendly label, operation type, whether the playbook
supports Ansible check mode, allowed hosts/groups, and approved variable names.
Those variable names do not create a browser extra-vars interface: HomeLabHQ
can supply values only from the corresponding fixed operation. There is no API
for a caller to submit an arbitrary playbook, target, variable, or shell command.

An OS update playbook may optionally advertise one approved Boolean variable
for reboot permission. HomeLabHQ passes it as `false` by default. Passing
`true` requires an administrator to turn on **Allow reboot if required** and
confirm the update.

A Docker check or update approval uses the fixed approved project-name variable
`docker_project`. An update approval also advertises supported modes and, when
it supports both modes, an approved mode variable.
A Compose project is configured in the inventory's `docker_compose_projects`
allowlist as **Pull and recreate** (`pull`), **Local build and recreate**
(`build`), or **Read-only** (`read-only`). HomeLabHQ retains only each entry's
validated name and mode from inventory discovery. It passes the selected
project's exact inventory `name` as `docker_project`; a filesystem `path`,
container, service, or image name is never used as the selector. Discovered
projects absent from the host allowlist remain visible but unmanaged, and an
empty allowlist makes every discovered container on that host read-only.

One generic Docker update playbook can support both modes. Existing
installations with separate pull and local-build approvals remain supported as
a compatibility fallback; new installations do not need duplicate approvals.
The Compute mapping persists the inventory host plus fixed operation references
and whether Docker discovery is enabled. It never resolves these from a guest
hostname or a playbook filename.

## Structured result contract

HomeLabHQ never guesses update counts by scraping prose. It prefers structured
callback/event data, including `ansible.builtin.set_stats`, when the runner
provides it. With the normal Ansible callback, a playbook can publish the same
object directly in `ansible.builtin.debug` `msg`; `msg` may already be an object
or a JSON-encoded string. The earlier one-line `HOMELABHQ_RESULT:` marker remains
supported for compatibility:

```yaml
- name: Publish update state to HomeLabHQ
  ansible.builtin.debug:
    msg: '{"homelabhq_update":{"available":true,"count":12,"reboot_required":false,"summary":"12 updates available"}}'

- name: Publish update state through Ansible stats
  ansible.builtin.set_stats:
    data:
      homelabhq_update: "{{ homelabhq_update }}"
```

The update object supports:

```json
{
  "homelabhq_update": {
    "available": true,
    "count": 12,
    "reboot_required": false,
    "summary": "12 updates available"
  }
}
```

Fields may be omitted when unknown. If no supported result key can be extracted,
PLAY RECAP remains available, but a successful Ansible process is recorded as
an incomplete check and update availability/count stays unknown.
OS checks and updates both consume `homelabhq_update`: `available: true` records
`updates_available`, while `available: false` records `up_to_date` immediately,
including after an update. A fresh check is recommended only when the update
playbook returns no usable OS result. Reboot permission remains off unless an
administrator explicitly enables and confirms it for that job.

OS update state, Docker discovery state, and Docker update state are independent.
Each maintenance job changes only the state and structured payload owned by its
operation; foreign result keys in a playbook's output are retained in job history
but do not overwrite another workflow's current state. Existing check and update
timestamps are retained when the other operation in the same state machine runs.

Docker discovery uses the same publication forms with this object:

```json
{
  "homelabhq_docker": {
    "available": true,
    "version": "example-version",
    "compose_available": true,
    "compose_version": "example-version",
    "projects": [
      {
        "name": "synthetic-project",
        "config_files": ["/configured/project/compose.yml"],
        "update_mode": "pull",
        "containers": [
          {
            "name": "web",
            "state": "running",
            "has_healthcheck": true,
            "health": "healthy",
            "exit_code": 0,
            "one_shot": false,
            "expected_to_run": true,
            "restart_policy": "unless-stopped",
            "image": "example/image:tag"
          }
        ],
        "images": [
          {"name": "example/image:tag", "id": "sha256:example", "tags": ["latest"]}
        ]
      }
    ],
    "containers": [],
    "images": []
  }
}
```

`available: false` represents Docker not being installed. Docker version and
Compose availability may be omitted and render as unknown. HomeLabHQ accepts
the normalized lowercase fields above, camel-case `hasHealthcheck`, and legacy
Docker CLI JSON fields such as
`Name`, `Status`, `ConfigFiles`, `Names`, `Image`, `State`, `Health`,
`HealthStatus`, `ExitCode`, `Project`, `Service`, `RestartPolicy`, `Labels`,
`Networks`, and `Ports`.

Structured `docker inspect` container objects are preferred. HomeLabHQ reads
the lifecycle from `State.Status`, checks whether `State.Health` exists, and
uses `Config.Healthcheck` to distinguish a container with no configured check
from missing monitoring data. It also reads lifecycle exit codes from
`State.ExitCode`, restart policy from `HostConfig.RestartPolicy`, and Compose
metadata from `Config.Labels`; explicit `expected_to_run`/`expectedToRun`
expectations are retained. Healthcheck log exit codes remain separate inside
`healthDetails`.

Some Compose `ps --format json` releases omit structured `Health` and
`ExitCode` fields while retaining them in Docker's canonical `Status` forms,
such as `Up 2 minutes (healthy)` and `Exited (0) 2 minutes ago`. When structured
fields and healthcheck configuration are absent, HomeLabHQ accepts only those
bounded canonical forms as a compatibility fallback. Explicit structured
fields always take precedence. Discovery playbooks should still prefer
`docker inspect` rather than deriving these values themselves.

Compose membership is derived from `com.docker.compose.project` and
`com.docker.compose.service` labels when present. Config paths and working
directories can also be supplied by `com.docker.compose.project.config_files`
and `com.docker.compose.project.working_dir`. Containers without a Compose
project label remain valid direct host containers and render separately.
The browser-safe container contract keeps lifecycle `state` separate from
`hasHealthcheck` (`true`, `false`, or `null` when the configuration could not be
determined) and `health` (`healthy`, `unhealthy`, `starting`, `unknown`, or
`null` when no check is configured). For compatibility, legacy `health` values
`no_healthcheck`, `none`, and an explicitly supplied empty value normalize to
`hasHealthcheck: false`; a completely missing health field remains unknown.
No-healthcheck containers are operational when running and do not make their
Compose project or host unhealthy. Retained inventory from a stale, failed, or
unreachable discovery is shown as unknown until a successful refresh replaces
it; an old healthy result is not treated as current health.

Exited services are not hidden. A service labelled
`com.homelabhq.lifecycle=oneshot`, explicitly marked with `one_shot` or
`oneShot`, marked by Compose as one-off, or referenced by a
`service_completed_successfully` dependency is treated as expected to finish.
The HomeLabHQ label is the explicit contract for normal Compose init services;
their `com.docker.compose.oneoff` label is normally `False` and is not sufficient
to identify them.
An expected one-shot with exit code `0` renders as **Completed** and does not
make its project need attention. Any non-zero exit code renders as **Failed**.
For a one-shot carrying the HomeLabHQ lifecycle label, the status summary uses
the issue code `oneshot_failed`. A labelled one-shot that is still running is
counted and classified from its current state and health.
A stopped service explicitly identified as long-running remains **Exited
unexpectedly**. When discovery omits whether a stopped container is one-shot or
long-running, HomeLabHQ reports **Expected state unknown** instead of treating
the incomplete result as a definite failure. Project health is derived from
those actionable states: a healthy running service plus a completed initializer
is **Operational**, while unhealthy, failed, restarting, paused, dead, or
unexpectedly stopped services remain visible warnings or errors.

Docker update availability is deliberately separate from discovery. A Docker
update-check playbook receives `docker_project` for one selected project and can
emit the contract below. When the playbook fails, its stored error summary is
shown in the Docker update status; full command output remains available under
**Recent maintenance**.

```json
{
  "homelabhq_docker_update": {
    "available": true,
    "summary": "One project has an update",
    "projects": [
      {
        "name": "synthetic-project",
        "updates_available": true,
        "update_mode": "pull",
        "summary": "New image available"
      },
      {
        "name": "locally-built-project",
        "update_mode": "build",
        "summary": "Source revision was not checked"
      }
    ]
  }
}
```

Because each invocation targets one project, a compact single-project result is
also accepted. `project`, `project_name`, or `docker_project` identifies the
inventory project; `updates_available` (or `update_available`) supplies its
availability. When the project name is omitted, a top-level Boolean availability
is assigned only to the project explicitly requested by that invocation.

For a locally built project, registry update availability is not applicable,
even if a generic availability field is present. A read-only project's result is
shown as read-only and never enables its update action. If a check or discovery contract
is missing, a successful Ansible process is recorded as an incomplete
maintenance job while the corresponding maintenance state becomes unknown.
The detail view identifies the missing contract instead of reporting the
operation as successful.

Existing Docker check playbooks that emit the earlier `homelabhq_update`
object remain supported for an overall available/count result. Per-project
status requires the `homelabhq_docker_update` contract above.

Discovery alone labels an approved pull-based project **Not checked**. It does
not claim that a playbook omitted a result. Malformed or missing output is shown
as **Check incomplete** only after an update check actually runs. Update status
is presented on each Compose project rather than repeated as a host-wide banner.

Each approved discovered Compose project has its own check control; only approved
`pull` and `build` projects have an update control. Hosts with more than one
project never infer a selection: the API and UI send the exact inventory project
name and inventory host. **Refresh All** and scheduled checks queue each unique
approved host/project pair once. One pair's result, loading state, or failure is
applied only to that project's containers. While a check or update job is queued
or running, that project shows an indeterminate progress bar because Ansible does
not provide a trustworthy percentage.

The per-workload **Refresh inventory & containers** action refreshes the
controller inventory before running Docker discovery. This makes newly approved
inventory projects checkable without requiring a separate global refresh and
clears obsolete global failures when the discovered projects are all unmanaged.
An unmanaged project is explained once, with its mapped inventory host, the
configured external inventory path, and the required `docker_compose_projects`
name, path, and `update_mode`. The notice makes clear that HomeLabHQ Settings
configure the inventory location but do not edit inventory contents. It does not
repeat an update failure, link to an unrelated settings control, or offer an
unsafe check action.

## Jobs and troubleshooting

Maintenance requests return immediately with persisted queued jobs. One manual
job or refresh sequence may be active per Compute instance, and at most one job
in that sequence runs at a time. The detail view shows recent jobs,
operation, Compute instance, target, approved playbook, validated Docker mode,
requesting user, timestamps, duration, exit status, per-host PLAY RECAP counts,
structured results, and sanitized stdout/stderr. Structured results remain
separate from raw output. Jobs also record the structured-result source and a
specific extraction/schema diagnostic when parsing is incomplete. A restart
marks in-flight jobs failed because the
remote process cannot be safely reattached. Finished history is bounded by
`HLHQ_MAX_COMPUTE_JOBS` (default 500).

Controller credentials are encrypted with the existing instance credential
key, never returned after save, and redacted from errors and captured output.
Ansible playbooks should also use `no_log: true` for tasks that handle their own
application secrets.

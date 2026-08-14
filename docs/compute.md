# Compute and Ansible maintenance

Compute represents workloads running on infrastructure Devices. A configured
Proxmox host remains a Device, including its existing API monitoring and root
SSH update controls. Its discovered virtual machines and LXC containers appear
under **Compute** and retain a link to that parent Device.

## Configure and discover

1. Add a Proxmox VE Device through the existing Add device wizard.
2. Open **Compute** as an administrator and choose **Refresh all**. HomeLabHQ reads
   `/cluster/resources` using the Device's existing API connection and upserts
   VMs and LXCs by parent Device and provider ID. When workloads are present
   but Ansible is not enabled, Compute links to **Settings** and labels update
   and Docker data as requiring Ansible rather than as unknown probe results.
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
**Settings → Ansible**. Actions whose required playbook is not approved remain
disabled in the Compute detail view instead of failing only after they are
clicked.

A mapped workload whose checks have not run shows **Updates: Unknown**. Compute
groups workloads beneath their parent host and uses compact cards as summaries:
clicking anywhere on a card opens its detail view, where approved maintenance
actions remain available. Docker is shown on a card only when that workload has
discovered containers; the compact preview shows container names, aggregate
health, and actionable Docker update status. Host and project summaries say
**Running** or **Operational** when unchecked containers are running; **Healthy**
is reserved for sets where every container has an explicitly healthy check.
Changing the mapped target clears results collected from the previous target so
stale maintenance or Docker data cannot be attributed to the new one.

Missing guests become stale after a successful provider refresh. A failed
provider refresh retains its workloads and marks them unavailable. **Refresh
all** also refreshes the configured Ansible inventory and queues each mapped
workload's eligible Docker discovery, OS update check, and one Docker update
check per discovered Compose project as an ordered sequence. Sequences may run
in parallel across workloads, but
only one maintenance playbook runs at a time for an individual workload.

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

The five canonical operations exposed by HomeLabHQ are:

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
A Compose project is configured as **Pull and recreate** (`pull`), **Local build
and recreate** (`build`), or **Read-only** (`read_only`). Projects are read-only
until an administrator confirms their mode. HomeLabHQ passes only the selected
project's exact inventory `name` as `docker_project` and the validated
`pull`/`build` enum through those approved variables. A filesystem `path` or
Compose config file is never used as the project selector; the playbook resolves
that implementation detail from its approved inventory data.

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
`Name`, `Status`, `ConfigFiles`, `Names`, `Image`, `State`, `HealthStatus`,
`Labels`, `Networks`, and `Ports`.

Structured `docker inspect` container objects are preferred. HomeLabHQ reads
the lifecycle from `State.Status`, checks whether `State.Health` exists, and
uses `Config.Healthcheck` to distinguish a container with no configured check
from missing monitoring data. When Docker supplies health logs, only the most
recent exit code and a bounded output excerpt are retained as `healthDetails`.
Playbooks should not derive health by parsing the human-readable `Status`
string.

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

Docker update availability is deliberately separate from discovery. A Docker
update-check playbook receives `docker_project` for one selected project and can
emit:

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

For a locally built project, `updates_available` remains unknown unless the
playbook supplies a meaningful signal. HomeLabHQ never presents registry update
availability for a local build by inference. If a check or discovery contract
is missing, a successful Ansible process is recorded as an incomplete
maintenance job while the corresponding maintenance state becomes unknown.
The detail view identifies the missing contract instead of reporting the
operation as successful.

Existing Docker check playbooks that emit the earlier `homelabhq_update`
object remain supported for an overall available/count result. Per-project
status requires the `homelabhq_docker_update` contract above.

Each discovered Compose project has its own check and update controls. Hosts
with more than one project never infer a selection: the API and UI send the
exact inventory project name. **Refresh all** explicitly queues one check for
each currently discovered project. While a check or update job is queued or
running, Compute shows an indeterminate progress bar because Ansible does not
provide a trustworthy percentage.

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

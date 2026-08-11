# Compute and Ansible maintenance

Compute represents workloads running on infrastructure Devices. A configured
Proxmox host remains a Device, including its existing API monitoring and root
SSH update controls. Its discovered virtual machines and LXC containers appear
under **Compute** and retain a link to that parent Device.

## Configure and discover

1. Add a Proxmox VE Device through the existing Add device wizard.
2. Open **Compute** as an administrator and choose **Refresh**. HomeLabHQ reads
   `/cluster/resources` using the Device's existing API connection and upserts
   VMs and LXCs by parent Device and provider ID.
3. Open **Settings → Ansible**, enter the controller connection and contained
   project paths, then save. Executable paths may initially be blank.
4. Use **Test Connection**. The test verifies SSH, the project directory,
   `ansible-playbook`, `ansible-inventory`, inventory JSON parsing, and reports
   each executable's absolute path, the Ansible version, and inventory counts.
   Review any discovered executable paths copied into the form, then save them.
5. Use **Discover / Refresh Inventory**. Ansible inventory remains authoritative;
   HomeLabHQ stores only its host addresses, groups, and relationships.
6. Use **Discover Playbooks**, then explicitly approve one discovered playbook
   for each operation it may perform. Filenames are never assumed.
7. Open a Compute workload and confirm an inventory-host mapping. Exact guest
   names and IP addresses may be suggested, but suggestions are never saved
   automatically.

Missing guests become stale after a successful provider refresh. A failed
provider refresh retains its workloads and marks them unavailable. Compute
refresh also refreshes the configured Ansible inventory and queues Docker
discovery for mapped workloads when an approved discovery playbook exists.

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

The only operations exposed by HomeLabHQ are:

- OS update check;
- OS update;
- Docker update check;
- Docker discovery;
- Docker pull/recreate update;
- Docker local-build/recreate update.

There is no general command, arbitrary playbook, arbitrary argument, or remote
shell endpoint. A playbook must be below the configured playbooks directory,
present in the latest discovered allowlist, and approved for the requested
operation. Targets must be present in the latest discovered inventory.

An OS update playbook may optionally advertise one approved Boolean variable
for reboot permission. HomeLabHQ passes it as `false` by default. Passing
`true` requires an administrator to turn on **Allow reboot if required** and
confirm the update.

Docker update playbooks advertise their approved project variable. A Compose
project is configured as **Pull and recreate**, **Local build and recreate**,
or **Read-only**. HomeLabHQ passes only the selected project's discovered path
(or name when no path was reported) through that approved variable.

## Structured result contract

HomeLabHQ never guesses update counts by scraping prose. A playbook can emit a
single compact JSON object after the exact marker `HOMELABHQ_RESULT:`. This can
be a direct callback line or a one-line `ansible.builtin.debug` `msg`:

```yaml
- name: Publish update state to HomeLabHQ
  ansible.builtin.debug:
    msg: 'HOMELABHQ_RESULT: {"homelabhq_update":{"available":true,"count":12,"reboot_required":false,"summary":"12 updates available"}}'
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

Fields may be omitted when unknown. Without the marker, job success/failure and
PLAY RECAP remain available while update availability/count stays unknown.

Docker discovery uses the same marker with this optional object:

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
        "path": "/configured/project/path",
        "update_strategy": "pull",
        "containers": [
          {
            "name": "web",
            "state": "running",
            "health": "healthy",
            "image": "example/image:tag"
          }
        ]
      }
    ]
  }
}
```

Allowed strategies are `pull`, `local_build`, and `unmanaged`. An absent or
invalid strategy becomes read-only until an administrator configures it.

## Jobs and troubleshooting

Maintenance requests return immediately with a persisted queued job. One job
may be queued/running per Compute instance. The detail view shows recent jobs,
duration, exit status, per-host PLAY RECAP counts, structured results, and
sanitized stdout/stderr. A restart marks in-flight jobs failed because the
remote process cannot be safely reattached. Finished history is bounded by
`HLHQ_MAX_COMPUTE_JOBS` (default 500).

Controller credentials are encrypted with the existing instance credential
key, never returned after save, and redacted from errors and captured output.
Ansible playbooks should also use `no_log: true` for tasks that handle their own
application secrets.

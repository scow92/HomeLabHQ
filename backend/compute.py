"""Persisted Compute workloads discovered from existing infrastructure Devices."""
from __future__ import annotations

import copy
import secrets
import time
from typing import Any

import ansible_integration as ansible
import devices
import store
from domain import safe_error


_TYPES = frozenset({"vm", "lxc"})


def _parent_public(parent: dict | None) -> dict | None:
    if not parent:
        return None
    result = {
        "id": parent["id"],
        "name": parent.get("name") or parent.get("host"),
        "host": parent.get("host"),
        "driverId": parent.get("driverId"),
    }
    if parent.get("state") is not None:
        result["state"] = copy.deepcopy(parent["state"])
    return result


def list_hosts(owner_id: str, *, is_admin=False) -> list[dict]:
    """Return provider-node records so Compute can manage hosts without guests.

    Proxmox maintenance state is persisted on the parent Device by the one
    canonical direct update service.  Workload discovery contributes node
    names before the first maintenance refresh; persisted catalogue nodes keep
    empty hosts visible afterwards.
    """
    document = store.load()
    instances = [item for item in document["computeInstances"].values()
                 if is_admin or item.get("ownerId") == owner_id]
    by_parent: dict[str, set[str]] = {}
    for item in instances:
        parent_id = item.get("parentDeviceId")
        if parent_id and item.get("node"):
            by_parent.setdefault(parent_id, set()).add(str(item["node"]))
    result = []
    for parent in document["devices"].values():
        if not (is_admin or parent.get("ownerId") == owner_id):
            continue
        try:
            driver = devices._drv_for(parent)
        except Exception:
            continue
        if not (getattr(driver, "supports_compute", False) and
                getattr(driver, "supports_updates", False)):
            continue
        maintenance = copy.deepcopy(parent.get("proxmoxMaintenance") or {})
        node_states = maintenance.get("nodes") or {}
        names = set(by_parent.get(parent["id"], set())) | set(node_states)
        for name in sorted(names or {""}):
            result.append({
                "id": f'{parent["id"]}:{name or "parent"}',
                "node": name or None,
                "parentDevice": _parent_public(parent),
                "maintenance": copy.deepcopy(node_states.get(name)) if name else None,
                "maintenanceCheckedAt": maintenance.get("checkedAt"),
                "maintenanceRefreshError": maintenance.get("refreshError"),
                "maintenanceRefreshFailedAt": maintenance.get("refreshFailedAt"),
                "sshConfigured": bool(maintenance.get("sshConfigured")),
            })
    return result


def managed_by_ansible(record: dict) -> bool:
    """Return whether the workload has a complete, persisted Ansible mapping."""
    mapping = record.get("ansible") or {}
    return (mapping.get("enabled") is True and
            bool(mapping.get("controllerId")) and
            bool(mapping.get("inventoryHost")))


def _inventory_project_state(mode: str) -> dict | None:
    if mode == "read_only":
        return {
            "state": "read_only", "updatesAvailable": None,
            "summary": "Update checks are read-only for this inventory project",
        }
    if mode == "build":
        return {
            "state": "not_applicable", "updatesAvailable": None,
            "summary": "Registry update availability is not applicable to locally built projects",
        }
    return None


def reconcile_docker_projects(docker: dict, controller: dict, target: str,
                              previous: dict | None = None) -> dict:
    """Associate discovered Compose projects with their exact inventory allowlist."""
    host = ansible.inventory_host(controller, ansible.validate_inventory_host(target)) or {}
    approved = {project["name"]: project for project in host.get("dockerProjects") or []}
    old_projects = {project.get("name"): project for project in
                    (previous or {}).get("projects") or []}
    for project in docker.get("projects") or []:
        name = project.get("name")
        inventory_project = approved.get(name)
        for container in project.get("containers") or []:
            container["inventoryHost"] = target
            container["composeProject"] = name
        project["inventoryHost"] = target
        project["approved"] = inventory_project is not None
        if inventory_project is None:
            project.update(managed=False, updateMode="read_only", updateStrategy="unmanaged")
            project["updateState"] = {
                "state": "unmanaged", "updatesAvailable": None,
                "summary": "Not listed in docker_compose_projects for this inventory host",
            }
            continue
        mode = inventory_project["updateMode"]
        project.update(
            managed=mode in ansible.DOCKER_UPDATE_MODES,
            updateMode=mode,
            updateStrategy={"build": "local_build", "read_only": "unmanaged"}.get(mode, mode),
        )
        prior = (old_projects.get(name) or {}).get("updateState")
        if (prior or {}).get("state") == "unknown" and not any(
                (prior or {}).get(key) for key in ("lastCheckedAt", "lastJobAt")):
            prior = None
        project["updateState"] = (_inventory_project_state(mode) or
                                  copy.deepcopy(prior) or
                                  {"state": "not_checked", "updatesAvailable": None})
    for container in docker.get("containers") or []:
        container["inventoryHost"] = target
    return docker


def public_instance(record: dict, document: dict | None = None) -> dict:
    """Return a browser-safe workload with its dynamic parent Device summary."""
    document = document or store.load()
    result = copy.deepcopy(record)
    result["parentDevice"] = _parent_public(
        document["devices"].get(record.get("parentDeviceId")))
    mapping = result.get("ansible") or {}
    managed = managed_by_ansible(result)
    maintenance = ansible.compute_maintenance_mapping(mapping)
    controller = document["ansibleControllers"].get(mapping.get("controllerId"))
    inventory_host = mapping.get("inventoryHost")
    target = inventory_host if isinstance(inventory_host, str) else ""
    capabilities = {
        "osMaintenance": bool(
            managed and ansible.operation_supported_by_host(
                controller, "os_check", target)),
        "dockerMaintenance": bool(
            managed and ansible.operation_supported_by_host(
                controller, "docker_discovery", target)),
        "applianceHealth": bool(
            managed and ansible.operation_supported_by_host(
                controller, "appliance_health", target)),
    }
    if managed and not capabilities["osMaintenance"]:
        result.pop("updateState", None)
    if managed and not capabilities["dockerMaintenance"]:
        result.pop("docker", None)
        result.pop("dockerDiscoveryState", None)
        result.pop("dockerUpdateState", None)
    if managed and not capabilities["applianceHealth"]:
        result.pop("applianceHealthState", None)
    if (managed and controller and isinstance(inventory_host, str) and
            capabilities["dockerMaintenance"] and isinstance(result.get("docker"), dict)):
        try:
            result["docker"] = reconcile_docker_projects(
                result["docker"], controller, inventory_host, result["docker"])
        except ValueError:
            # An invalid or stale mapping remains visible but cannot expose an action.
            pass
    active_jobs = [
        job for job in document["computeJobs"].values()
        if (job.get("computeInstanceId") == record.get("id") and
            job.get("state") in {"queued", "running"})
    ]

    def eligible(field):
        operation = maintenance.get(field)
        return bool(managed and operation and
                    ansible.operation_is_allowed_for_target(
                        controller, operation, target))

    docker_modes: list[str] = []
    if (managed and controller and capabilities["dockerMaintenance"] and
            maintenance.get("dockerUpdateOperation") == "docker_update"):
        generic = (controller.get("playbooks") or {}).get("docker_update") or {}
        if (ansible.operation_is_allowed_for_target(
                controller, "docker_update", target) and
                generic.get("projectVariable") == "docker_project"):
            docker_modes.extend(mode for mode in generic.get("supportedModes") or []
                                if mode in ansible.DOCKER_UPDATE_MODES)
        for mode, operation in (("pull", "docker_update_pull"),
                                ("build", "docker_update_local_build")):
            approval = (controller.get("playbooks") or {}).get(operation) or {}
            if (mode not in docker_modes and
                    ansible.operation_is_allowed_for_target(
                        controller, operation, target) and
                    approval.get("projectVariable") == "docker_project"):
                docker_modes.append(mode)
    result["ansible"] = {
        "enabled": managed,
        "controllerId": mapping.get("controllerId"),
        "inventoryHost": mapping.get("inventoryHost"),
        "maintenance": maintenance,
        "capabilities": capabilities,
        "updateCheckEligible": eligible("osCheckOperation"),
        "updateEligible": eligible("osUpdateOperation"),
        "dockerDiscoveryEligible": bool(
            maintenance.get("dockerDiscoveryEnabled") and
            eligible("dockerDiscoveryOperation")),
        "dockerUpdateCheckEligible": bool(
            eligible("dockerCheckOperation") and controller and
            ((controller.get("playbooks") or {}).get(
                maintenance.get("dockerCheckOperation")) or {}).get(
                    "projectVariable") == "docker_project"),
        "dockerUpdateModes": docker_modes,
        "applianceHealthEligible": eligible("applianceHealthOperation"),
        "maintenanceActive": bool(active_jobs),
    }
    return result


def list_instances(owner_id: str, *, is_admin=False) -> list[dict]:
    document = store.load()
    records = [public_instance(item, document)
               for item in document["computeInstances"].values()
               if is_admin or item.get("ownerId") == owner_id]
    records.sort(key=lambda item: (
        item.get("type") or "", (item.get("name") or "").lower(), item["id"]))
    return records


def get_instance(instance_id: str) -> dict | None:
    return store.load()["computeInstances"].get(instance_id)


def _normalize_workload(value: dict[str, Any]) -> dict:
    provider_id = str(value.get("providerInstanceId") or "").strip()
    workload_type = str(value.get("type") or "").lower()
    if not provider_id or workload_type not in _TYPES:
        raise ValueError("provider returned an invalid compute workload")
    result = {
        "providerInstanceId": provider_id,
        "type": workload_type,
        "name": str(value.get("name") or provider_id)[:200],
        "status": str(value.get("status") or "unknown")[:50],
        "node": value.get("node"),
        "cpuCores": value.get("cpuCores"),
        "memoryBytes": value.get("memoryBytes"),
        "diskBytes": value.get("diskBytes"),
        "uptimeSeconds": value.get("uptimeSeconds"),
        "ipAddresses": [str(address)[:255] for address in value.get("ipAddresses") or []
                        if address],
    }
    if value.get("os"):
        result["os"] = str(value["os"])[:200]
    return result


def _mark_discovery_failure(device_id: str, error: Exception) -> None:
    now = int(time.time())
    message = safe_error(error)[:300]

    def mutate(document):
        for item in document["computeInstances"].values():
            if item.get("parentDeviceId") == device_id:
                item["discoveryState"] = "unavailable"
                item["lastDiscoveryAttemptAt"] = now
                item["lastDiscoveryError"] = message

    store.update(mutate)


def discover_device(device_id: str, *, timeout=20) -> dict:
    """Refresh one provider Device; retain stale records on absence or failure."""
    parent = devices.get_device(device_id)
    if not parent:
        raise ValueError("device not found")
    driver = devices._drv_for(parent)
    if not getattr(driver, "supports_compute", False):
        raise ValueError("device does not provide compute workloads")
    try:
        with devices.device_conn(device_id, timeout=timeout) as (_, active_driver, conn):
            discovered = [_normalize_workload(item)
                          for item in active_driver.compute_instances(conn)]
    except Exception as error:
        _mark_discovery_failure(device_id, error)
        raise

    now = int(time.time())
    provider = str(getattr(driver, "compute_provider", None) or driver.id)

    def mutate(document):
        existing = {
            (item.get("provider"), item.get("providerInstanceId")): item
            for item in document["computeInstances"].values()
            if item.get("parentDeviceId") == device_id
        }
        seen = set()
        created = 0
        for workload in discovered:
            key = (provider, workload["providerInstanceId"])
            seen.add(key)
            record = existing.get(key)
            if record is None:
                instance_id = secrets.token_hex(8)
                record = {
                    "id": instance_id,
                    "ownerId": parent.get("ownerId"),
                    "parentDeviceId": device_id,
                    "provider": provider,
                    "createdAt": now,
                    "updateState": {"state": "unknown"},
                }
                document["computeInstances"][instance_id] = record
                created += 1
            record.update(workload)
            record.update({
                "ownerId": parent.get("ownerId"),
                "discoveryState": "current",
                "lastDiscoveredAt": now,
                "lastDiscoveryAttemptAt": now,
            })
            record.pop("lastDiscoveryError", None)
        stale = 0
        for key, record in existing.items():
            if key not in seen:
                record["discoveryState"] = "stale"
                record["lastDiscoveryAttemptAt"] = now
                stale += 1
        return {"discovered": len(discovered), "created": created, "stale": stale}

    return store.batch_update(mutate)


def discover_all(owner_id: str | None = None, *, is_admin=False, timeout=20) -> dict:
    """Refresh every visible virtualization Device independently."""
    document = store.load()
    candidates = []
    for item in document["devices"].values():
        if not (is_admin or item.get("ownerId") == owner_id):
            continue
        try:
            capable = getattr(devices._drv_for(item), "supports_compute", False)
        except Exception:
            capable = False
        if capable:
            candidates.append(item)
    results = []
    for parent in candidates:
        try:
            result = discover_device(parent["id"], timeout=timeout)
            results.append({"deviceId": parent["id"], "ok": True, **result})
        except Exception as error:
            results.append({"deviceId": parent["id"], "ok": False,
                            "error": safe_error(error)[:300]})
    return {"providers": results}


def summary(owner_id: str, *, is_admin=False) -> dict:
    records = list_instances(owner_id, is_admin=is_admin)
    hosts = {}
    for item in records:
        parent_key = item.get("parentDeviceId")
        if not parent_key:
            continue
        node = str(item.get("node") or "").strip()
        key = (parent_key, "node", node) if node else (parent_key, "parent")
        hosts[key] = item.get("parentDevice")
    containers: list[dict] = []
    for item in records:
        docker = item.get("docker") or {}
        containers.extend(docker.get("containers") or [])
        containers.extend(container for project in docker.get("projects") or []
                          for container in project.get("containers") or [])
    def host_online(parent):
        state = (parent or {}).get("state")
        if not isinstance(state, dict):
            return None
        return state.get("confirmedOnline", state.get("online"))

    def has_healthcheck(container):
        configured = container.get("hasHealthcheck")
        if isinstance(configured, bool):
            return configured
        health = container.get("health")
        if health in {"healthy", "unhealthy", "starting"}:
            return True
        if health in {"no_healthcheck", "none"}:
            return False
        return None

    def docker_data_current(record):
        discovery = (record.get("dockerDiscoveryState") or {}).get("state")
        return (record.get("discoveryState") not in {"stale", "unavailable"} and
                discovery not in {"failed", "unreachable", "unknown", "incomplete"})

    current_container_ids = {
        id(container)
        for record in records if docker_data_current(record)
        for container in ((record.get("docker") or {}).get("containers") or [])
    }
    current_container_ids.update(
        id(container)
        for record in records if docker_data_current(record)
        for project in ((record.get("docker") or {}).get("projects") or [])
        for container in project.get("containers") or [])

    def current(container):
        return id(container) in current_container_ids

    def lifecycle_unknown(container):
        if container.get("state") not in {"stopped", "exited"}:
            return False
        exit_code = container.get("exitCode")
        known_exit = isinstance(exit_code, int) and not isinstance(exit_code, bool)
        if known_exit and exit_code != 0:
            return False
        one_shot = container.get("oneShot")
        if one_shot is True and known_exit and exit_code == 0:
            return False
        return one_shot is not False

    return {
        "hosts": len(hosts),
        "onlineHosts": sum(host_online(parent) is True for parent in hosts.values()),
        "offlineHosts": sum(host_online(parent) is False for parent in hosts.values()),
        "unknownHosts": sum(host_online(parent) is None for parent in hosts.values()),
        "workloads": len(records),
        "running": sum(item.get("status") == "running" for item in records),
        "stopped": sum(item.get("status") in {"stopped", "exited"} for item in records),
        "containers": len(containers),
        "healthyContainers": sum(
            current(container) and container.get("state") == "running" and
            has_healthcheck(container) is True and
            container.get("health") == "healthy" for container in containers),
        "unhealthyContainers": sum(
            current(container) and container.get("state") == "running" and
            has_healthcheck(container) is True and
            container.get("health") == "unhealthy" for container in containers),
        "startingContainers": sum(
            current(container) and container.get("state") == "running" and
            has_healthcheck(container) is True and
            container.get("health") == "starting" for container in containers),
        "withoutHealthcheckContainers": sum(
            has_healthcheck(container) is False for container in containers),
        "unknownContainers": sum(
            not current(container) or container.get("state") == "unknown" or
            (container.get("state") == "running" and has_healthcheck(container) is None) or
            lifecycle_unknown(container)
            for container in containers),
        "needsUpdates": sum((item.get("updateState") or {}).get("state") in
                            ("updates_available", "reboot_required") for item in records),
    }

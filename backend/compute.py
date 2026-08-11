"""Persisted Compute workloads discovered from existing infrastructure Devices."""
from __future__ import annotations

import copy
import secrets
import time
from typing import Any

import devices
import store
from domain import safe_error


_TYPES = frozenset({"vm", "lxc"})


def _parent_public(parent: dict | None) -> dict | None:
    if not parent:
        return None
    return {
        "id": parent["id"],
        "name": parent.get("name") or parent.get("host"),
        "host": parent.get("host"),
        "driverId": parent.get("driverId"),
    }


def managed_by_ansible(record: dict) -> bool:
    """Return whether the workload has a complete, persisted Ansible mapping."""
    mapping = record.get("ansible") or {}
    return (mapping.get("enabled") is True and
            bool(mapping.get("controllerId")) and
            bool(mapping.get("inventoryHost")))


def public_instance(record: dict, document: dict | None = None) -> dict:
    """Return a browser-safe workload with its dynamic parent Device summary."""
    document = document or store.load()
    result = copy.deepcopy(record)
    result["parentDevice"] = _parent_public(
        document["devices"].get(record.get("parentDeviceId")))
    mapping = result.get("ansible") or {}
    managed = managed_by_ansible(result)
    result["ansible"] = {
        "enabled": managed,
        "controllerId": mapping.get("controllerId"),
        "inventoryHost": mapping.get("inventoryHost"),
        "updateCheckEligible": managed,
        "dockerDiscoveryEligible": managed,
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


def discover_all(owner_id: str | None = None, *, is_admin=False) -> dict:
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
            result = discover_device(parent["id"])
            results.append({"deviceId": parent["id"], "ok": True, **result})
        except Exception as error:
            results.append({"deviceId": parent["id"], "ok": False,
                            "error": safe_error(error)[:300]})
    return {"providers": results}


def summary(owner_id: str, *, is_admin=False) -> dict:
    records = list_instances(owner_id, is_admin=is_admin)
    containers = [container for item in records
                  for project in (item.get("docker") or {}).get("projects") or []
                  for container in project.get("containers") or []]
    return {
        "workloads": len(records),
        "running": sum(item.get("status") == "running" for item in records),
        "stopped": sum(item.get("status") == "stopped" for item in records),
        "containers": len(containers),
        "healthyContainers": sum(container.get("health") == "healthy"
                                  for container in containers),
        "needsUpdates": sum((item.get("updateState") or {}).get("state") in
                            ("updates_available", "reboot_required") for item in records),
    }

"""Persistent daily update-check orchestration and result aggregation.

The orchestrator deliberately calls the same read-only services used by the
manual Compute and Devices controls.  It never invokes an update or reboot
operation.  A lease stored in the flock-protected application document is the
cross-thread/process lock; completed runs retain source-level evidence.
"""
from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, time as datetime_time, timezone
import os
import re
import secrets
import threading
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import compute
import compute_maintenance
import device_updates
import devices
import logbuf
import push
import store
from domain import safe_error
from errors import Conflict


DEFAULT_CONFIG = {
    "enabled": True,
    "runTime": "07:00",
    "timezone": "Europe/London",
    "runAnsibleChecks": True,
    "runDeviceNativeChecks": True,
    "deviceTimeoutSeconds": 30,
}
DEFAULT_NOTIFICATIONS = {
    "notifyUpdates": True,
    "notifyFailures": True,
    "notifySuccess": True,
}
TERMINAL_RUN_STATES = frozenset({"successful", "partial", "failed"})
TERMINAL_JOB_STATES = compute_maintenance.TERMINAL_STATES
MAX_RUNS = max(10, int(os.environ.get("HLHQ_MAX_MORNING_UPDATE_RUNS", "90")))
MAX_WORKERS = max(1, int(os.environ.get("HLHQ_UPDATE_CHECK_CONCURRENCY", "4")))
LOCK_LEASE_SECONDS = max(300, int(os.environ.get("HLHQ_UPDATE_CHECK_LOCK_LEASE", "14400")))
PUSH_BODY_MAX_CHARS = max(80, int(os.environ.get("HLHQ_PUSH_BODY_MAX_CHARS", "240")))
_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_scheduler = None
_scheduler_guard = threading.Lock()


def _iso(value: datetime | None = None) -> str:
    value = value or datetime.now(timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _configured(document=None) -> dict:
    document = document or store.load()
    raw = (document.get("meta") or {}).get("morningUpdateCheck") or {}
    return {**DEFAULT_CONFIG, **{key: raw[key] for key in DEFAULT_CONFIG if key in raw}}


def _preferences(user: dict | None) -> dict:
    raw = (user or {}).get("morningUpdateNotifications") or {}
    return {
        **DEFAULT_NOTIFICATIONS,
        **{key: raw[key] for key in DEFAULT_NOTIFICATIONS if isinstance(raw.get(key), bool)},
    }


def _validate_config(value: dict) -> dict:
    if not isinstance(value, dict):
        raise ValueError("morning update settings must be an object")
    unknown = set(value) - set(DEFAULT_CONFIG)
    if unknown:
        raise ValueError("morning update settings contain unsupported fields")
    result = {**DEFAULT_CONFIG, **value}
    for key in ("enabled", "runAnsibleChecks", "runDeviceNativeChecks"):
        if not isinstance(result[key], bool):
            raise ValueError(f"{key} must be a boolean")
    if not isinstance(result["runTime"], str) or not _TIME_RE.fullmatch(result["runTime"]):
        raise ValueError("run time must use HH:MM")
    if not isinstance(result["timezone"], str):
        raise ValueError("timezone must be an IANA timezone")
    try:
        ZoneInfo(result["timezone"])
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ValueError("timezone must be a valid IANA timezone") from error
    timeout = result["deviceTimeoutSeconds"]
    if type(timeout) is not int or not 5 <= timeout <= 300:
        raise ValueError("device timeout must be between 5 and 300 seconds")
    return result


def _validate_preferences(value: dict) -> dict:
    if not isinstance(value, dict):
        raise ValueError("notification preferences must be an object")
    unknown = set(value) - set(DEFAULT_NOTIFICATIONS)
    if unknown:
        raise ValueError("notification preferences contain unsupported fields")
    result = {**DEFAULT_NOTIFICATIONS, **value}
    if any(not isinstance(result[key], bool) for key in DEFAULT_NOTIFICATIONS):
        raise ValueError("notification preferences must be booleans")
    return result


def save_settings(user_id: str, *, is_admin: bool, config=None, notifications=None) -> dict:
    normalized_config = _validate_config(config) if config is not None else None
    normalized_notifications = (
        _validate_preferences(notifications) if notifications is not None else None)
    if normalized_config is not None and not is_admin:
        raise PermissionError("administrator access required")

    def mutate(document):
        if normalized_config is not None:
            document["meta"]["morningUpdateCheck"] = normalized_config
        if normalized_notifications is not None:
            user = document["users"].get(user_id)
            if not user:
                raise ValueError("user not found")
            user["morningUpdateNotifications"] = normalized_notifications

    store.update(mutate)
    return settings(user_id, is_admin=is_admin)


def _next_occurrence(config: dict, now: datetime | None = None) -> datetime:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    zone = ZoneInfo(config["timezone"])
    local = now.astimezone(zone)
    hour, minute = map(int, config["runTime"].split(":"))
    scheduled = datetime.combine(local.date(), datetime_time(hour, minute), tzinfo=zone)
    if scheduled <= local:
        from datetime import timedelta
        scheduled += timedelta(days=1)
    return scheduled.astimezone(timezone.utc)


def settings(user_id: str, *, is_admin: bool) -> dict:
    document = store.load()
    config = _configured(document)
    latest = _latest_from_document(document)
    return {
        "config": config,
        "notifications": _preferences(document["users"].get(user_id)),
        "subscription": push.subscription_status(user_id),
        "lastRun": _run_summary(_visible_run(latest, user_id, is_admin)) if latest else None,
        "nextRunAt": _iso(_next_occurrence(config)) if config["enabled"] else None,
        "canManageSchedule": bool(is_admin),
    }


def _new_source(device_id, name, owner_id, resource_type, source, **values):
    return {
        "deviceId": str(device_id),
        "name": str(name or device_id)[:200],
        "ownerId": owner_id,
        "resourceType": resource_type,
        "source": source,
        **values,
    }


def _identity_for_compute(instance: dict, document: dict) -> tuple[str, str]:
    """Resolve only explicit or exact inventory-to-device mappings.

    Display names are intentionally ignored.  Unmapped workloads retain their
    own persistent Compute ID as their canonical logical-device identity.
    """
    mapping = instance.get("ansible") or {}
    explicit = mapping.get("deviceId")
    if explicit in document["devices"]:
        device = document["devices"][explicit]
        if device.get("ownerId") == instance.get("ownerId"):
            return explicit, "device"
    inventory_host = str(mapping.get("inventoryHost") or "").strip().casefold()
    matches = [device for device in document["devices"].values()
               if device.get("ownerId") == instance.get("ownerId") and
               str(device.get("host") or "").strip().casefold() == inventory_host]
    if inventory_host and len(matches) == 1:
        return matches[0]["id"], "device"
    return instance["id"], "compute"


def _wait_for_jobs(job_ids: list[str], timeout: int, heartbeat=None) -> list[dict]:
    deadline = time.monotonic() + max(1, timeout)
    while True:
        jobs = [compute_maintenance.get_job(job_id) for job_id in job_ids]
        if all(job and job.get("state") in TERMINAL_JOB_STATES for job in jobs):
            return jobs
        if time.monotonic() >= deadline:
            timed_out = []
            for job_id, job in zip(job_ids, jobs, strict=True):
                if job and job.get("state") in TERMINAL_JOB_STATES:
                    timed_out.append(job)
                    continue
                result = copy.deepcopy(job) if job else {"id": job_id}
                result.update({
                    "state": "incomplete",
                    "summary": "Timed out waiting for maintenance result",
                })
                timed_out.append(result)
            return timed_out
        if heartbeat:
            heartbeat()
        time.sleep(0.1)


def _compute_check(instance: dict, document: dict, requested_by: str, heartbeat=None,
                   docker_project_names=None) -> list[dict]:
    public = compute.public_instance(instance, document)
    mapping = public.get("ansible") or {}
    operations = []
    if mapping.get("updateCheckEligible"):
        operations.append("os_check")
    if mapping.get("dockerUpdateCheckEligible"):
        project_names = (compute_maintenance.approved_docker_project_names(instance, document)
                         if docker_project_names is None else docker_project_names)
        operations.extend({"operation": "docker_check", "projectName": project_name}
                          for project_name in project_names)
    if not operations:
        return []
    canonical_id, resource_type = _identity_for_compute(instance, document)
    name = instance.get("name") or mapping.get("inventoryHost") or instance["id"]
    try:
        jobs = compute_maintenance.start_job_sequence(instance["id"], operations, requested_by)
    except Exception as error:
        return [_new_source(
            canonical_id, name, instance.get("ownerId"), resource_type, "ansibleOs",
            status="incomplete" if isinstance(error, Conflict) else "failed",
            error=safe_error(error)[:500], summary="Ansible checks could not be started")]
    controller = document["ansibleControllers"].get(
        (instance.get("ansible") or {}).get("controllerId")) or {}
    per_job_timeout = int(controller.get("executionTimeout") or 1800) + 30
    completed = _wait_for_jobs(
        [job["id"] for job in jobs], per_job_timeout * len(jobs), heartbeat)
    results = []
    for job in completed:
        normalized = compute_maintenance.update_check_result(job)
        source = "ansibleDocker" if job.get("operation") == "docker_check" else "ansibleOs"
        state = normalized["state"]
        status = ("updates_available" if normalized.get("updatesAvailable") else
                  "up_to_date" if state == "up_to_date" else
                  "unsupported" if state in {"read_only", "not_applicable"} else
                  "incomplete" if state == "unknown" else state)
        normalized_error = normalized.get("error")
        if status == "incomplete" and not normalized_error:
            normalized_error = "Update availability could not be determined"
        results.append(_new_source(
            canonical_id, name, instance.get("ownerId"), resource_type, source,
            status=status, updateCount=normalized.get("updateCount"),
            rebootRequired=normalized.get("rebootRequired"),
            summary=normalized.get("summary"), error=normalized_error,
            jobId=normalized.get("jobId"), projectName=normalized.get("projectName"),
            resultSource=normalized.get("structuredResultSource")))
    return results


def _kernel_update(packages: list[dict], reboot: dict) -> bool:
    if ((reboot or {}).get("signals") or {}).get("kernelMismatch") is True:
        return True
    return any(re.search(r"(?:^|[-_])(?:kernel|linux-image)(?:[-_]|$)",
                         str(package.get("name") or ""), re.I)
               for package in packages)


def _proxmox_check(device: dict) -> list[dict]:
    try:
        catalogue = device_updates.check(device["id"])
    except Exception as error:
        return [_new_source(
            device["id"], device.get("name") or device.get("host"), device.get("ownerId"),
            "device", "ansibleProxmox", status="failed", error=safe_error(error)[:500],
            summary="Proxmox package check failed")]
    results = []
    for node in catalogue.get("nodes") or []:
        name = node.get("node") or device.get("name") or device["host"]
        canonical_id = f'{device["id"]}:node:{name}'
        packages = node.get("packages") or []
        reboot = node.get("reboot") or {}
        if node.get("status") != "online":
            status = "unreachable"
            error = "Proxmox node was not online"
        else:
            status = "updates_available" if packages else "up_to_date"
            error = None
        results.append(_new_source(
            canonical_id, name, device.get("ownerId"), "proxmoxNode",
            "ansibleProxmox", status=status, updateCount=len(packages),
            rebootRequired=reboot.get("rebootRequired"),
            kernelUpdateAvailable=_kernel_update(packages, reboot), error=error,
            summary=(f"{len(packages)} Proxmox package updates" if packages else
                     "Proxmox packages are up to date"),
            detail={"parentDeviceId": device["id"], "nodeStatus": node.get("status"),
                    "packages": [{key: package.get(key) for key in
                                  ("name", "installed", "available")}
                                 for package in packages], "reboot": reboot}))
    if not results:
        results.append(_new_source(
            device["id"], device.get("name") or device.get("host"), device.get("ownerId"),
            "device", "ansibleProxmox", status="incomplete",
            error="Proxmox returned no node results", summary="Proxmox check incomplete"))
    return results


def run_ansible_phase(document: dict, requested_by: str, heartbeat=None) -> list[dict]:
    tasks = []
    queued_docker_pairs = set()
    for instance in document["computeInstances"].values():
        if compute.managed_by_ansible(instance):
            canonical_id, resource_type = _identity_for_compute(instance, document)
            fallback = _new_source(
                canonical_id, instance.get("name") or instance["id"], instance.get("ownerId"),
                resource_type, "ansibleOs", status="failed",
                error="Ansible target check failed unexpectedly",
                summary="Ansible target check failed")
            mapping = instance.get("ansible") or {}
            project_names = []
            for project_name in compute_maintenance.approved_docker_project_names(
                    instance, document):
                pair = (mapping.get("controllerId"), mapping.get("inventoryHost"), project_name)
                if pair in queued_docker_pairs:
                    continue
                queued_docker_pairs.add(pair)
                project_names.append(project_name)
            tasks.append((_compute_check, (
                instance, document, requested_by, heartbeat, project_names), fallback))
    for device in document["devices"].values():
        try:
            is_proxmox = (device.get("driverId") == "proxmox.ve" and
                           getattr(devices._drv_for(device), "supports_updates", False))
        except Exception:
            is_proxmox = False
        if is_proxmox:
            fallback = _new_source(
                device["id"], device.get("name") or device.get("host"),
                device.get("ownerId"), "device", "ansibleProxmox", status="failed",
                error="Proxmox target check failed unexpectedly",
                summary="Proxmox target check failed")
            tasks.append((_proxmox_check, (device,), fallback))
    results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="morning-ansible") as pool:
        futures = {pool.submit(function, *args): fallback
                   for function, args, fallback in tasks}
        for future in as_completed(futures):
            try:
                results.extend(future.result())
            except Exception as error:
                fallback = copy.deepcopy(futures[future])
                fallback["error"] = safe_error(error)[:500]
                results.append(fallback)
                logbuf.log_event("error", "morning_update_target", source="morning-updates",
                                 error=safe_error(error)[:500])
    return results


def _native_capable(device: dict) -> bool:
    try:
        return any(action.get("name") == "check_updates"
                   for action in devices._drv_for(device).actions() or [])
    except Exception:
        return False


def _integer(value):
    try:
        return max(0, int(value)) if value is not None else None
    except (TypeError, ValueError):
        return None


def _native_check(device: dict, timeout: int) -> dict:
    base = (device["id"], device.get("name") or device.get("host"),
            device.get("ownerId"), "device", "deviceNative")
    if not _native_capable(device):
        return _new_source(*base, status="unsupported",
                           summary="Device-native update checks are not supported")
    checked_at = _iso()
    try:
        payload = devices.run_action(device["id"], "check_updates", {}, timeout=timeout)
        if not isinstance(payload, dict):
            raise ValueError("update provider returned an invalid result")
        provider_unavailable = (
            "latestStable" in payload and "latestBuild" in payload and
            payload.get("latestStable") is None and payload.get("latestBuild") is None)
        if payload.get("ok") is False or provider_unavailable:
            status = "failed"
        elif isinstance(payload.get("updateAvailable"), bool):
            status = "updates_available" if payload["updateAvailable"] else "up_to_date"
        else:
            status = "incomplete"
        error = (payload.get("error") or payload.get("message")) if status == "failed" else None
        result = _new_source(
            *base, status=status,
            currentVersion=payload.get("current") or payload.get("currentVersion") or
            payload.get("version"),
            availableVersion=payload.get("latest") or payload.get("latestStable") or
            payload.get("availableVersion"),
            updateCount=_integer(payload.get("updateCount", payload.get("count"))),
            rebootRequired=(payload.get("rebootRequired")
                            if isinstance(payload.get("rebootRequired"), bool) else None),
            summary=str(payload.get("message") or "Device update check completed")[:500],
            error=str(error)[:500] if error else None, checkedAt=checked_at)
    except Exception as error:
        result = _new_source(*base, status="failed", error=safe_error(error)[:500],
                             summary="Device-native update check failed", checkedAt=checked_at)

    def persist(document):
        current = document["devices"].get(device["id"])
        if current is not None:
            current["scheduledUpdateState"] = {
                key: copy.deepcopy(result.get(key)) for key in
                ("status", "currentVersion", "availableVersion", "updateCount",
                 "rebootRequired", "summary", "error", "checkedAt")
            }

    store.update(persist)
    return result


def run_device_phase(document: dict, timeout: int) -> list[dict]:
    candidates = [device for device in document["devices"].values()
                  if device.get("includeInScheduledUpdateChecks", True)]
    results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="morning-device") as pool:
        futures = {pool.submit(_native_check, device, timeout): device for device in candidates}
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as error:
                device = futures[future]
                results.append(_new_source(
                    device["id"], device.get("name") or device.get("host"),
                    device.get("ownerId"), "device", "deviceNative", status="failed",
                    error=safe_error(error)[:500],
                    summary="Device-native update check failed unexpectedly"))
    return results


def aggregate(source_results: list[dict]) -> dict:
    merged = {}
    for source in source_results:
        device_id = source["deviceId"]
        device = merged.setdefault(device_id, {
            "deviceId": device_id, "name": source.get("name") or device_id,
            "ownerId": source.get("ownerId"), "resourceType": source.get("resourceType"),
            "sources": {},
        })
        device["sources"].setdefault(source["source"], []).append(copy.deepcopy(source))
    for device in merged.values():
        checks = [item for values in device["sources"].values() for item in values]
        statuses = {item.get("status") for item in checks}
        device["requiresUpdates"] = "updates_available" in statuses
        device["rebootRequired"] = any(item.get("rebootRequired") is True for item in checks)
        device["unreachable"] = "unreachable" in statuses
        device["checkIncomplete"] = bool(statuses & {"failed", "incomplete", "unreachable"})
        device["unsupported"] = bool(checks) and statuses == {"unsupported"}
        device["upToDate"] = (
            "up_to_date" in statuses and not device["requiresUpdates"] and
            not device["checkIncomplete"])
        reboot_sources = [{"source": item["source"], "required": item.get("rebootRequired"),
                           "summary": item.get("summary")}
                          for item in checks if item.get("rebootRequired") is not None]
        if reboot_sources:
            device["sources"]["rebootRequired"] = reboot_sources
    devices_list = sorted(merged.values(), key=lambda item: (item["name"].casefold(),
                                                              item["deviceId"]))
    checked = [item for item in devices_list if not item["unsupported"]]
    updates = [item for item in devices_list if item["requiresUpdates"]]
    reboots = [item for item in devices_list if item["rebootRequired"]]
    incomplete = [item for item in devices_list if item["checkIncomplete"]]
    unreachable = [item for item in devices_list if item["unreachable"]]
    unsupported = [item for item in devices_list if item["unsupported"]]
    failed_checks = sum(
        source.get("status") in {"failed", "incomplete", "unreachable"}
        for item in devices_list for values in item["sources"].values()
        for source in values if isinstance(source, dict) and "status" in source)
    successful_checks = sum(
        source.get("status") in {"up_to_date", "updates_available"}
        for item in devices_list for values in item["sources"].values()
        for source in values if isinstance(source, dict) and "status" in source)
    status = ("partial" if failed_checks and (successful_checks or updates) else
              "failed" if failed_checks else "successful")
    return {
        "status": status,
        "devices": devices_list,
        "uniqueDevicesChecked": len(checked),
        "devicesRequiringUpdates": len(updates),
        "devicesRequiringReboot": len(reboots),
        "failedChecks": failed_checks,
        "unreachableChecks": sum(item["unreachable"] for item in devices_list),
        "unsupportedDevices": len(unsupported),
        "deviceIds": {
            "checked": [item["deviceId"] for item in checked],
            "updates": [item["deviceId"] for item in updates],
            "reboot": [item["deviceId"] for item in reboots],
            "incomplete": [item["deviceId"] for item in incomplete],
            "unreachable": [item["deviceId"] for item in unreachable],
            "unsupported": [item["deviceId"] for item in unsupported],
        },
    }


def _source_summary(device: dict) -> str:
    parts = []
    labels = {
        "ansibleOs": "OS", "ansibleDocker": "container",
        "ansibleProxmox": "Proxmox", "deviceNative": "device",
    }
    for source_name, values in device.get("sources", {}).items():
        if source_name == "rebootRequired":
            continue
        for result in values:
            if result.get("status") != "updates_available":
                continue
            count = result.get("updateCount")
            label = labels.get(source_name, "update")
            if count is not None:
                noun = "update" if count == 1 else "updates"
                parts.append(f"{count} {label} {noun}")
            else:
                parts.append(f"{label} update")
            if result.get("kernelUpdateAvailable"):
                parts.append("kernel update")
    if device.get("rebootRequired"):
        parts.append("reboot required")
    return "; ".join(dict.fromkeys(parts)) or "updates available"


def _bounded_body(prefix: str, entries: list[str], max_chars: int) -> str:
    if not entries:
        return prefix[:max_chars]
    for count in range(len(entries), -1, -1):
        omitted = len(entries) - count
        suffix = f"\n…and {omitted} more devices" if omitted else ""
        body = "\n".join(([prefix] if prefix else []) + entries[:count]) + suffix
        if len(body) <= max_chars:
            return body
    return prefix[:max_chars]


def format_notification(run: dict, max_chars: int = PUSH_BODY_MAX_CHARS) -> dict:
    updates = [item for item in run.get("devices", []) if item.get("requiresUpdates")]
    failed = [item for item in run.get("devices", []) if item.get("checkIncomplete")]
    reboots = sum(item.get("rebootRequired") is True for item in run.get("devices", []))
    if updates:
        title = f"HomeLabHQ: {len(updates)} devices require updates"
        prefix = ""
        if failed:
            prefix = (f"{len(updates)} need updates · {reboots} reboot required · "
                      f"{run.get('failedChecks', 0)} checks failed")
        entries = [f"{item['name']} — {_source_summary(item)}" for item in updates]
        failed_only = [item for item in failed if not item.get("requiresUpdates")]
        entries.extend(f"{item['name']} — check failed" for item in failed_only)
        body = _bounded_body(prefix, entries, max_chars)
        kind = "updates"
    elif failed:
        title = "HomeLabHQ: Morning check incomplete"
        prefix = f"{run.get('failedChecks', 0)} checks failed"
        body = _bounded_body(prefix, [f"{item['name']} — check failed" for item in failed],
                             max_chars)
        kind = "failures"
    else:
        count = run.get("uniqueDevicesChecked", 0)
        unsupported = run.get("unsupportedDevices", 0)
        if unsupported:
            title = "HomeLabHQ: Morning check completed"
            body = (f"{count} devices checked · {unsupported} unsupported."
                    if count else f"{unsupported} devices do not support update checks.")
        elif count:
            title = "HomeLabHQ: Everything is up to date"
            body = f"Morning check completed. {count} devices checked."
        else:
            title = "HomeLabHQ: Morning check completed"
            body = "No update checks were enabled or applicable."
        kind = "success"
    return {
        "title": title, "body": body, "kind": kind,
        "data": {
            "url": (f"/devices?filter=needs-attention&checkRun={run.get('id')}"),
            "checkRun": run.get("id"), "tag": f"morning-update-{run.get('id')}",
            "type": "available_updates" if kind == "updates" else
                    "backup_failure" if kind == "failures" and
                    any("backup" in str(item).lower() for item in failed) else
                    f"morning_check_{kind}",
        },
    }


def _run_summary(run: dict | None) -> dict | None:
    if not run:
        return None
    return {key: copy.deepcopy(run.get(key)) for key in (
        "id", "trigger", "scheduledFor", "startedAt", "completedAt", "status",
        "uniqueDevicesChecked", "devicesRequiringUpdates", "devicesRequiringReboot",
        "failedChecks", "unreachableChecks", "unsupportedDevices")}


def _view_aggregate(run: dict, devices_view: list[dict]) -> dict:
    source_results = [source for device in devices_view
                      for source_name, values in device.get("sources", {}).items()
                      if source_name != "rebootRequired" for source in values]
    result = aggregate(source_results)
    result.update({key: copy.deepcopy(run.get(key)) for key in
                   ("id", "trigger", "scheduledFor", "startedAt", "completedAt",
                    "phaseStatus", "notificationDelivery", "config")})
    result["status"] = result["status"] if devices_view else run.get("status")
    return result


def _visible_run(run: dict | None, user_id: str, is_admin: bool) -> dict | None:
    if not run:
        return None
    if is_admin:
        return copy.deepcopy(run)
    visible = [device for device in run.get("devices", [])
               if device.get("ownerId") == user_id]
    return _view_aggregate(run, visible)


def _latest_from_document(document: dict) -> dict | None:
    runs = list(document["morningUpdateRuns"].values())
    return max(runs, key=lambda run: (run.get("startedAt") or "", run["id"])) if runs else None


def get_run(run_id: str | None, user_id: str, *, is_admin: bool) -> dict | None:
    document = store.load()
    run = (document["morningUpdateRuns"].get(run_id) if run_id else
           _latest_from_document(document))
    return _visible_run(run, user_id, is_admin)


def _renew_lock(run_id: str, token: str):
    def mutate(document):
        lock = document["meta"].get("morningUpdateLock") or {}
        if lock.get("runId") == run_id and lock.get("token") == token:
            lock["expiresAt"] = int(time.time()) + LOCK_LEASE_SECONDS
    store.update(mutate)


def _acquire(trigger: str, requested_by: str, scheduled_for=None) -> tuple[dict, str]:
    now = int(time.time())
    run_id = secrets.token_hex(12)
    token = secrets.token_hex(16)

    def mutate(document):
        lock = document["meta"].get("morningUpdateLock") or {}
        if lock.get("expiresAt", 0) > now:
            raise Conflict("an update check run is already active")
        if lock.get("runId"):
            abandoned = document["morningUpdateRuns"].get(lock["runId"])
            if abandoned and abandoned.get("status") == "running":
                abandoned.update({
                    "status": "failed", "completedAt": _iso(),
                    "fatalError": "The previous process stopped before the run completed",
                })
            document["meta"].pop("morningUpdateLock", None)
        if scheduled_for and any(run.get("trigger") == "scheduled" and
                                 run.get("scheduledFor") == scheduled_for
                                 for run in document["morningUpdateRuns"].values()):
            raise Conflict("this scheduled update check has already run")
        config = _configured(document)
        run = {
            "id": run_id, "trigger": trigger, "requestedBy": requested_by,
            "scheduledFor": scheduled_for, "startedAt": _iso(), "completedAt": None,
            "status": "running", "config": copy.deepcopy(config), "phaseStatus": {
                "ansible": "pending" if config["runAnsibleChecks"] else "disabled",
                "deviceNative": "pending" if config["runDeviceNativeChecks"] else "disabled",
            }, "devices": [], "uniqueDevicesChecked": 0,
            "devicesRequiringUpdates": 0, "devicesRequiringReboot": 0,
            "failedChecks": 0, "unreachableChecks": 0, "unsupportedDevices": 0,
            "notificationDelivery": [],
        }
        document["morningUpdateRuns"][run_id] = run
        document["meta"]["morningUpdateLock"] = {
            "runId": run_id, "token": token, "expiresAt": now + LOCK_LEASE_SECONDS}
        terminal = sorted(
            (item for item in document["morningUpdateRuns"].values()
             if item.get("status") in TERMINAL_RUN_STATES),
            key=lambda item: (item.get("completedAt") or item.get("startedAt") or "", item["id"]))
        for old in terminal[:max(0, len(document["morningUpdateRuns"]) - MAX_RUNS)]:
            document["morningUpdateRuns"].pop(old["id"], None)
        return copy.deepcopy(run)

    return store.update(mutate), token


def _release(run_id: str, token: str):
    def mutate(document):
        lock = document["meta"].get("morningUpdateLock") or {}
        if lock.get("runId") == run_id and lock.get("token") == token:
            document["meta"].pop("morningUpdateLock", None)
    store.update(mutate)


def _persist_phase(run_id: str, phase: str, source_results: list[dict]):
    def mutate(document):
        run = document["morningUpdateRuns"].get(run_id)
        if not run:
            return
        existing = [source for device in run.get("devices", [])
                    for source_name, values in device.get("sources", {}).items()
                    if source_name != "rebootRequired" for source in values]
        aggregated = aggregate(existing + source_results)
        run.update(aggregated)
        run["phaseStatus"][phase] = "completed"
    store.update(mutate)


def _notify(run: dict) -> list[dict]:
    document = store.load()
    delivery = []
    for user_id, user in document["users"].items():
        visible = _visible_run(run, user_id, user.get("role") == "admin")
        notification = format_notification(visible)
        preferences = _preferences(user)
        should_send = (
            notification["kind"] == "updates" and preferences["notifyUpdates"] or
            notification["kind"] == "failures" and preferences["notifyFailures"] or
            notification["kind"] == "success" and preferences["notifySuccess"])
        if notification["kind"] == "updates" and visible.get("failedChecks"):
            should_send = should_send or preferences["notifyFailures"]
        if not should_send:
            delivery.append({"userId": user_id, "kind": notification["kind"],
                             "skipped": "disabled by user preference"})
            continue
        try:
            result = push.notify({user_id}, notification["title"], notification["body"],
                                 notification["data"])
            delivery.append({"userId": user_id, "kind": notification["kind"], **result})
        except Exception as error:
            delivery.append({"userId": user_id, "kind": notification["kind"], "sent": 0,
                             "failed": 1, "removed": 0, "error": safe_error(error)[:500]})
    return delivery


def _execute(run_id: str, token: str):
    try:
        initial = store.load()["morningUpdateRuns"].get(run_id)
        if not initial:
            return
        config = initial["config"]
        heartbeat = lambda: _renew_lock(run_id, token)
        if config["runAnsibleChecks"]:
            phase_results = run_ansible_phase(store.load(), initial["requestedBy"], heartbeat)
            _persist_phase(run_id, "ansible", phase_results)
        heartbeat()
        if config["runDeviceNativeChecks"]:
            phase_results = run_device_phase(store.load(), config["deviceTimeoutSeconds"])
            _persist_phase(run_id, "deviceNative", phase_results)
        run = store.load()["morningUpdateRuns"][run_id]
        delivery = _notify(run)

        def complete(document):
            current = document["morningUpdateRuns"].get(run_id)
            if current:
                current["completedAt"] = _iso()
                current["status"] = aggregate([
                    source for device in current.get("devices", [])
                    for name, values in device.get("sources", {}).items()
                    if name != "rebootRequired" for source in values])["status"]
                current["notificationDelivery"] = delivery
        store.update(complete)
    except Exception as error:
        message = safe_error(error)[:500]
        logbuf.log_event("error", "morning_update_run", source="morning-updates",
                         run_id=run_id, error=message)

        def fail(document):
            run = document["morningUpdateRuns"].get(run_id)
            if run:
                run.update({"completedAt": _iso(), "status": "failed",
                            "fatalError": message})
        store.update(fail)
    finally:
        _release(run_id, token)


def start_run(trigger: str, requested_by: str, scheduled_for=None) -> dict:
    if trigger not in {"scheduled", "manual"}:
        raise ValueError("invalid update check trigger")
    run, token = _acquire(trigger, requested_by, scheduled_for)
    thread = threading.Thread(target=_execute, args=(run["id"], token),
                              name=f'morning-update-{run["id"]}', daemon=True)
    thread.start()
    return run


def _due_occurrence(now: datetime | None = None) -> str | None:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    config = _configured()
    if not config["enabled"]:
        return None
    zone = ZoneInfo(config["timezone"])
    local = now.astimezone(zone)
    hour, minute = map(int, config["runTime"].split(":"))
    scheduled = datetime.combine(local.date(), datetime_time(hour, minute), tzinfo=zone)
    if local < scheduled:
        return None
    return _iso(scheduled)


def scheduler_tick(now: datetime | None = None) -> dict | None:
    scheduled_for = _due_occurrence(now)
    if not scheduled_for:
        return None
    try:
        return start_run("scheduled", "morning-scheduler", scheduled_for)
    except Conflict:
        return None


class _Scheduler:
    def __init__(self):
        self.stop_event = threading.Event()
        self.thread = None

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._loop, name="morning-update-scheduler",
                                       daemon=True)
        self.thread.start()

    def _loop(self):
        while not self.stop_event.is_set():
            try:
                scheduler_tick()
            except Exception as error:
                logbuf.log_event("error", "morning_update_scheduler", source="morning-updates",
                                 error=safe_error(error)[:500])
            self.stop_event.wait(30)

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=2)


def start_scheduler():
    global _scheduler
    with _scheduler_guard:
        if _scheduler is None:
            _scheduler = _Scheduler()
        _scheduler.start()


def stop_scheduler():
    global _scheduler
    with _scheduler_guard:
        if _scheduler is not None:
            _scheduler.stop()
            _scheduler = None

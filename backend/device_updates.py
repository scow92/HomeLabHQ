"""Software-update discovery and asynchronous maintenance for devices.

The Proxmox API lists apt updates, but its upgrade console is interactive and
restricted to ``root@pam`` (API tokens cannot launch it as root). HomelabHQ
therefore keeps the monitoring API token and optional root SSH credentials in
the same encrypted credential record. Discovery uses the driver API;
maintenance jobs use fixed apt/reboot commands over SSH and expose
process-local progress.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import re
import threading
import time

import crypto
import devices
import logbuf
import store
import transports
from domain import safe_error
from errors import Conflict


_JOBS: dict[str, dict] = {}
_LOCK = threading.RLock()
_APT_UPDATE = "apt-get -q update"
_APT_UPGRADE = (
    "DEBIAN_FRONTEND=noninteractive apt-get -y -q "
    "-o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold "
    "dist-upgrade"
)
_RUNNING_KERNEL = "uname -r"
_KERNEL_PINS = (
    "if test -s /etc/kernel/next-boot-pin; then "
    "printf 'next\\t'; sed -n '1p' /etc/kernel/next-boot-pin; "
    "elif test -s /etc/kernel/proxmox-boot-pin; then "
    "printf 'permanent\\t'; sed -n '1p' /etc/kernel/proxmox-boot-pin; fi"
)
_BOOT_TOOL_KERNEL_LIST = "proxmox-boot-tool kernel list"
_NEEDRESTART_KERNEL = "needrestart -b -k"
_INSTALLED_KERNEL_TARGET = (
    "best=''; for image in /boot/vmlinuz-*-pve; do "
    "test -e \"$image\" || continue; kernel=${image##*/vmlinuz-}; "
    "if test -z \"$best\" || dpkg --compare-versions \"$kernel\" gt \"$best\"; "
    "then best=$kernel; fi; done; test -n \"$best\" && printf '%s\\n' \"$best\""
)
_REBOOT_REQUIRED = "test -e /var/run/reboot-required"
_REBOOT_NODE = "systemctl reboot --no-wall"
_PROBE_TIMEOUT = 30


def _driver(dev):
    driver = devices._drv_for(dev)
    return _require_driver(driver)


def _require_driver(driver):
    if not getattr(driver, "supports_updates", False):
        raise ValueError("device does not support software updates")
    return driver


def _ssh_credentials(dev):
    configured = devices._credentials_for(dev).get("updateSsh") or {}
    if configured.get("username") == "root" and (
            configured.get("password") or configured.get("privateKey")):
        return configured
    return None


def _public_catalogue(catalogue, device_id, configured):
    result = copy.deepcopy(catalogue)
    for node in result.get("nodes") or []:
        node.pop("_targetHost", None)
    result["supported"] = True
    result["sshConfigured"] = bool(configured)
    result["operation"] = status(device_id)
    return result


def _checked_at():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _unknown_reboot(reason, *, checked_at=None, running_kernel=None, signals=None):
    return {
        "rebootStatus": "unknown",
        "rebootRequired": None,
        "reason": reason,
        "runningKernel": running_kernel,
        "targetKernel": None,
        "signals": signals or {
            "kernelMismatch": None,
            "needrestart": None,
            "rebootRequiredFile": None,
        },
        "checkedAt": checked_at or _checked_at(),
    }


def _run_probe(conn, command):
    """Run a fixed read-only probe without allowing one failure to abort the check."""
    try:
        code, output, _ = conn.run(command, timeout=_PROBE_TIMEOUT)
        return code, output
    except Exception:
        return None, ""


def _kernel_value(value):
    candidate = str(value or "").strip().splitlines()[0] if value else ""
    return candidate if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+:~_-]{1,199}", candidate) else None


def _parse_pin(output):
    fields = str(output or "").strip().split("\t", 1)
    if len(fields) == 2 and fields[0] in {"next", "permanent"}:
        kernel = _kernel_value(fields[1])
        if kernel:
            return kernel, fields[0]
    return None, None


def _parse_boot_tool(output):
    """Parse documented ``kernel list`` text; reject unfamiliar layouts."""
    selected = []
    pinned = None
    recognized = False
    section = None
    lines = str(output or "").splitlines()
    for raw in lines:
        line = raw.strip()
        lower = line.lower()
        if lower.startswith("manually selected kernels:"):
            recognized = True
            section = "manual"
            continue
        if lower.startswith("automatically selected kernels:"):
            recognized = True
            section = "automatic"
            continue
        if lower.startswith("pinned kernel:"):
            recognized = True
            section = "pinned"
            same_line = line.split(":", 1)[1].strip()
            if same_line:
                pinned = _kernel_value(same_line)
            continue
        if not line or lower == "none":
            continue
        kernel = _kernel_value(line)
        if section == "pinned" and kernel and pinned is None:
            pinned = kernel
        elif section in {"manual", "automatic"} and kernel:
            selected.append(kernel)
    return {"recognized": recognized, "pinned": pinned, "selected": selected}


def _parse_needrestart(output):
    values = {}
    for line in str(output or "").splitlines():
        match = re.fullmatch(r"NEEDRESTART-(KCUR|KEXP|KSTA):\s*(.*?)\s*", line)
        if match:
            values[match.group(1)] = match.group(2)
    try:
        status = int(values.get("KSTA", ""))
    except ValueError:
        status = None
    if status not in {0, 1, 2, 3}:
        status = None
    return {
        "status": status,
        "running": _kernel_value(values.get("KCUR")),
        "expected": _kernel_value(values.get("KEXP")),
    }


def reboot_status(conn):
    """Return a structured, conservative Proxmox reboot decision.

    Pin files and ``proxmox-boot-tool`` are authoritative for an explicitly
    selected next kernel.  ``needrestart`` supplies its machine-readable
    expected-kernel decision when there is no pin.  Only then do we fall back
    to the newest installed boot image, selected remotely with Debian's
    ``dpkg --compare-versions`` semantics rather than lexical comparison.
    Debian's reboot marker remains an independent additional signal.
    """
    checked_at = _checked_at()
    errors = []

    running_code, running_output = _run_probe(conn, _RUNNING_KERNEL)
    running = _kernel_value(running_output) if running_code == 0 else None
    if running is None:
        errors.append("running kernel could not be read")

    pin_code, pin_output = _run_probe(conn, _KERNEL_PINS)
    pinned, pin_kind = _parse_pin(pin_output) if pin_code == 0 else (None, None)
    if pin_code is None:
        errors.append("kernel pin files could not be read")

    boot_code, boot_output = _run_probe(conn, _BOOT_TOOL_KERNEL_LIST)
    boot = _parse_boot_tool(boot_output) if boot_code == 0 else {
        "recognized": False, "pinned": None, "selected": []}
    if pinned is None and boot["pinned"]:
        pinned, pin_kind = boot["pinned"], "boot-tool"
    if boot_code != 0:
        errors.append("proxmox-boot-tool is unavailable")
    elif not boot["recognized"]:
        errors.append("proxmox-boot-tool returned an unrecognized format")

    need_code, need_output = _run_probe(conn, _NEEDRESTART_KERNEL)
    need = _parse_needrestart(need_output) if need_code == 0 else {
        "status": None, "running": None, "expected": None}
    if running is None and need["running"]:
        running = need["running"]
    need_signal = ({1: False, 2: True, 3: True}).get(need["status"])
    if need_code != 0:
        errors.append("needrestart is unavailable")
    elif need["status"] in {None, 0}:
        errors.append("needrestart could not determine kernel status")

    marker_code, _ = _run_probe(conn, _REBOOT_REQUIRED)
    marker = True if marker_code == 0 else False if marker_code == 1 else None
    if marker is None:
        errors.append("Debian reboot marker could not be checked")

    target = pinned
    source = "pinned" if pinned else None
    kernel_mismatch = None
    if running and pinned:
        kernel_mismatch = running != pinned
    elif running and need_signal is not None:
        target = need["expected"] or (running if need_signal is False else None)
        source = "needrestart"
        kernel_mismatch = need_signal
    elif running:
        installed_code, installed_output = _run_probe(conn, _INSTALLED_KERNEL_TARGET)
        installed = _kernel_value(installed_output) if installed_code == 0 else None
        if installed and (not boot["selected"] or installed in boot["selected"]):
            target = installed
            source = "installed-kernel"
            kernel_mismatch = running != installed
        else:
            errors.append("expected boot kernel could not be determined")

    signals = {
        "kernelMismatch": kernel_mismatch,
        "needrestart": need_signal,
        "rebootRequiredFile": marker,
        "bootTool": True if boot_code == 0 and boot["recognized"] else None,
        "pinnedKernel": bool(pinned) if pin_code == 0 or boot["recognized"] else None,
    }
    if kernel_mismatch is True:
        status = "required"
        required = True
        if source == "pinned":
            reason = f"The {pin_kind or 'selected'} Proxmox kernel differs from the running kernel"
        else:
            reason = "A newer Proxmox kernel is installed and selected for the next boot"
    elif marker is True:
        status = "required"
        required = True
        reason = "Debian reports that a reboot is required after package updates"
    elif kernel_mismatch is False:
        status = "not_required"
        required = False
        reason = "The running kernel matches the kernel selected for the next boot"
    else:
        status = "unknown"
        required = None
        reason = "Could not determine the kernel selected for the next boot"

    result = {
        "rebootStatus": status,
        "rebootRequired": required,
        "reason": reason,
        "runningKernel": running or need["running"],
        "targetKernel": target,
        "signals": signals,
        "checkedAt": checked_at,
    }
    if errors:
        result["diagnostics"] = errors
    return result


def _persist_maintenance(device_id, catalogue, configured):
    checked_at = _checked_at()
    nodes = {}
    for node in catalogue.get("nodes") or []:
        name = node.get("node")
        if not name:
            continue
        nodes[name] = {
            "status": node.get("status") or "unknown",
            "updateCount": len(node.get("packages") or []),
            "packages": copy.deepcopy(node.get("packages") or []),
            "reboot": copy.deepcopy(node.get("reboot")),
        }
    maintenance = {
        "checkedAt": checked_at,
        "totalUpdates": int(catalogue.get("total") or 0),
        "sshConfigured": bool(configured),
        "nodes": nodes,
        "refreshError": None,
        "refreshFailedAt": None,
    }

    def mutate(document):
        device = document["devices"].get(device_id)
        if device is not None:
            device["proxmoxMaintenance"] = maintenance

    store.update(mutate)
    return maintenance


def _persist_node_reboot(device_id, name, reboot):
    def mutate(document):
        device = document["devices"].get(device_id)
        if device is None:
            return
        maintenance = device.setdefault("proxmoxMaintenance", {
            "checkedAt": reboot["checkedAt"], "totalUpdates": None,
            "sshConfigured": True, "nodes": {}})
        maintenance["checkedAt"] = reboot["checkedAt"]
        node = maintenance.setdefault("nodes", {}).setdefault(name, {
            "status": "online", "updateCount": None})
        node["reboot"] = copy.deepcopy(reboot)

    store.update(mutate)


def record_refresh_failure(device_id, reason):
    """Retain the last successful maintenance snapshot after a failed attempt."""
    checked_at = _checked_at()

    def mutate(document):
        device = document["devices"].get(device_id)
        if device is None:
            return
        maintenance = device.setdefault("proxmoxMaintenance", {
            "totalUpdates": None, "sshConfigured": False, "nodes": {}})
        maintenance["refreshError"] = safe_error(reason)[:300]
        maintenance["refreshFailedAt"] = checked_at

    store.update(mutate)


def _inspect_reboots(catalogue, ssh_credentials):
    for node in catalogue.get("nodes") or []:
        if node.get("status") != "online":
            node["reboot"] = _unknown_reboot("Node is offline; reboot status was not checked")
            continue
        if not ssh_credentials:
            node["reboot"] = _unknown_reboot(
                "Configure Proxmox root SSH credentials to check reboot status")
            continue
        try:
            with transports.open_connection(
                    "ssh", node["_targetHost"], ssh_credentials.get("port") or 22,
                    ssh_credentials, timeout=20) as conn:
                node["reboot"] = reboot_status(conn)
        except Exception as error:
            node["reboot"] = _unknown_reboot(
                f"Reboot check failed: {safe_error(error)[:220]}")


def check(device_id, timeout=20):
    """Return live update availability plus the most recent install state."""
    with devices.update_check_lock(device_id):
        with devices.device_conn(device_id, timeout=timeout) as (dev, driver, conn):
            _require_driver(driver)
            catalogue = driver.available_updates(conn)
        ssh_credentials = _ssh_credentials(dev)
        _inspect_reboots(catalogue, ssh_credentials)
        _persist_maintenance(device_id, catalogue, ssh_credentials)
        return _public_catalogue(catalogue, device_id, ssh_credentials)


def configure_ssh(device_id, username, password=None, private_key=None, port=22,
                  timeout=12):
    """Validate and encrypt privileged SSH credentials used only for updates."""
    dev = devices.get_device(device_id)
    if not dev:
        raise ValueError("device not found")
    _driver(dev)
    username = (username or "root").strip()
    if username != "root":
        raise ValueError("Proxmox updates require the root SSH account")
    if not password and not private_key:
        raise ValueError("an SSH password or private key is required")
    try:
        port = int(port or 22)
    except (TypeError, ValueError) as error:
        raise ValueError("invalid SSH port") from error
    if port < 1 or port > 65535:
        raise ValueError("invalid SSH port")
    candidate = {"username": username, "port": port}
    if password:
        candidate["password"] = str(password)
    if private_key:
        candidate["privateKey"] = str(private_key)

    # Verify root before persisting a new secret. This also pins the SSH host
    # key through the transport's existing TOFU policy.
    with transports.open_connection(
            "ssh", dev["host"], port, candidate, timeout=timeout) as conn:
        code, output, _ = conn.run("id -u", timeout=timeout)
    if code != 0 or output.strip() != "0":
        raise ValueError("SSH login did not produce a root shell")

    credentials = devices._credentials_for(dev)
    credentials["updateSsh"] = candidate
    encrypted = crypto.encrypt(credentials)

    def _mut(document):
        current = document["devices"].get(device_id)
        if not current or not current.get("credRef"):
            raise ValueError("device not found")
        document["credentials"][current["credRef"]] = encrypted

    store.update(_mut)
    return {"sshConfigured": True}


def status(device_id):
    """Return the latest operation, including a restart-safe terminal result."""
    with _LOCK:
        job = _JOBS.get(device_id)
        if job:
            return copy.deepcopy(job)
    persisted = (store.load()["devices"].get(device_id) or {}).get(
        "proxmoxUpdateOperation")
    if not persisted:
        return None
    if persisted.get("state") != "running":
        return copy.deepcopy(persisted)

    # A process-local SSH thread cannot survive a backend restart. Convert the
    # durable running snapshot into an explicit interrupted result rather than
    # leaving the UI stuck or pretending work is still progressing.
    now = int(time.time())

    def mutate(document):
        device = document["devices"].get(device_id)
        operation = (device or {}).get("proxmoxUpdateOperation")
        if not operation or operation.get("id") != persisted.get("id") or \
                operation.get("state") != "running":
            return copy.deepcopy(operation)
        operation.update({
            "state": "failed", "stage": "interrupted", "progressMode": "exact",
            "percent": 100, "finishedAt": now, "updatedAt": now,
            "message": "Update interrupted because the HomelabHQ backend restarted.",
        })
        for node in operation.get("nodes") or []:
            if node.get("state") in {"pending", "running"}:
                node.update({
                    "state": "failed", "stage": "interrupted",
                    "progressMode": "exact", "percent": 100,
                    "updateOutcome": "failed",
                    "message": "Update interrupted by a backend restart",
                })
        return copy.deepcopy(operation)

    interrupted = store.update(mutate)
    logbuf.log_event("warn", "proxmox_update_interrupted", source="device-updates",
                     device_id=device_id, task_id=persisted.get("id"))
    return interrupted


def _persist_operation(device_id, job):
    snapshot = copy.deepcopy(job)

    def mutate(document):
        device = document["devices"].get(device_id)
        if device is not None:
            device["proxmoxUpdateOperation"] = snapshot

    try:
        store.update(mutate)
    except Exception as error:
        logbuf.log_event("error", "proxmox_update_persistence", source="device-updates",
                         device_id=device_id, task_id=job.get("id"),
                         error=safe_error(error)[:220])


def _set_job(device_id, job_id, **changes):
    with _LOCK:
        job = _JOBS.get(device_id)
        if not job or job["id"] != job_id:
            return None
        job.update(changes)
        snapshot = copy.deepcopy(job)
    _persist_operation(device_id, snapshot)
    return snapshot


def _set_node(device_id, job_id, index, **changes):
    with _LOCK:
        job = _JOBS.get(device_id)
        if not job or job["id"] != job_id:
            return None
        job["nodes"][index].update(changes)
        job["updatedAt"] = int(time.time())
        snapshot = copy.deepcopy(job)
    _persist_operation(device_id, snapshot)
    return snapshot


def _set_node_progress(device_id, job_id, index, *, stage, message):
    """Publish one atomic indeterminate stage for the targeted node and task."""
    with _LOCK:
        job = _JOBS.get(device_id)
        if not job or job["id"] != job_id:
            return None
        node = job["nodes"][index]
        node.update({
            "state": "running", "stage": stage, "progressMode": "indeterminate",
            "percent": None, "currentPackage": None, "message": message,
        })
        now = int(time.time())
        job.update({
            "currentNode": node["node"], "stage": stage,
            "progressMode": "indeterminate", "percent": None,
            "message": f"{message} on {node['node']}", "updatedAt": now,
        })
        snapshot = copy.deepcopy(job)
    _persist_operation(device_id, snapshot)
    logbuf.log_event("info", "proxmox_update_progress", source="device-updates",
                     device_id=device_id, node=node["node"], task_id=job_id, stage=stage)
    return snapshot


def _run_command(conn, command, label, timeout=3600):
    code, _, _ = conn.run(command, timeout=timeout)
    if code != 0:
        raise RuntimeError(f"{label} exited with status {code}")


def _run_install(device_id, job_id, targets, ssh_credentials):
    failed = 0
    reboot_nodes = []
    reboot_unknown = 0
    total = max(1, len(targets))
    for index, target in enumerate(targets):
        name = target["node"]
        _set_node_progress(device_id, job_id, index, stage="preparing",
                           message="Preparing update")
        try:
            with transports.open_connection(
                    "ssh", target["_targetHost"], ssh_credentials.get("port") or 22,
                    ssh_credentials, timeout=20) as conn:
                _set_node_progress(device_id, job_id, index, stage="downloading",
                                   message="Refreshing package lists")
                _run_command(conn, _APT_UPDATE, "apt-get update")
                _set_node_progress(device_id, job_id, index, stage="installing",
                                   message="Installing updates")
                _run_command(conn, _APT_UPGRADE, "apt-get dist-upgrade")
                _set_node_progress(device_id, job_id, index,
                                   stage="checking_reboot_status",
                                   message="Checking reboot status")
                try:
                    reboot = reboot_status(conn)
                except Exception as error:
                    reboot = _unknown_reboot(
                        f"Reboot check failed: {safe_error(error)[:220]}")
            try:
                _persist_node_reboot(device_id, name, reboot)
            except Exception as error:
                reboot.setdefault("diagnostics", []).append(
                    f"Reboot status could not be persisted: {safe_error(error)[:180]}")
            reboot_required = reboot["rebootRequired"]
            if reboot["rebootStatus"] == "required":
                reboot_nodes.append(name)
                node_message = "Updates installed; reboot required"
            elif reboot["rebootStatus"] == "not_required":
                node_message = "Updates installed; no reboot required"
            else:
                reboot_unknown += 1
                node_message = "Updates installed; reboot requirement unknown"
            _set_node(device_id, job_id, index, state="completed", stage="completed",
                      progressMode="exact", percent=100,
                      updateOutcome="succeeded", rebootStatus=reboot["rebootStatus"],
                      rebootRequired=reboot_required, reboot=reboot, message=node_message)
        except Exception as error:
            failed += 1
            _set_node(device_id, job_id, index, state="failed", stage="failed",
                      progressMode="exact", percent=100,
                      updateOutcome="failed", message=safe_error(error)[:300])
        _set_job(device_id, job_id, completedNodes=index + 1,
                 progressMode="exact", percent=round((index + 1) / total * 100),
                 updatedAt=int(time.time()))

    try:
        refreshed = check(device_id)
        _set_job(device_id, job_id, packageRefresh={
            "status": "succeeded", "totalUpdates": refreshed.get("total", 0)})
    except Exception as error:
        _set_job(device_id, job_id, packageRefresh={
            "status": "failed", "reason": safe_error(error)[:300]})

    finished = int(time.time())
    if failed:
        plural = "s" if failed != 1 else ""
        _set_job(device_id, job_id, state="failed", stage="failed", currentNode=None,
                 progressMode="exact", percent=100,
                 failedNodes=failed, finishedAt=finished,
                 message=f"Updates failed on {failed} node{plural}.")
    else:
        if reboot_nodes:
            message = f"Reboot required on {', '.join(reboot_nodes)}."
        elif reboot_unknown:
            plural = "s" if reboot_unknown != 1 else ""
            message = f"All updates installed; reboot requirement unknown on {reboot_unknown} node{plural}."
        else:
            message = "All updates installed; no reboot required."
        _set_job(device_id, job_id, state="completed", stage="completed", currentNode=None,
                 progressMode="exact", percent=100,
                 finishedAt=finished, message=message)


def start(device_id, timeout=20, node=None):
    """Validate current availability, start one background install, and return it."""
    with _LOCK:
        current = _JOBS.get(device_id)
        if current and current.get("state") == "running":
            raise Conflict("an update installation is already running")

    dev = devices.get_device(device_id)
    if not dev:
        raise ValueError("device not found")
    driver = _driver(dev)
    ssh_credentials = _ssh_credentials(dev)
    if not ssh_credentials:
        raise ValueError("configure root SSH credentials before installing updates")

    with devices.device_conn(device_id, timeout=timeout) as (_, _, conn):
        catalogue = driver.available_updates(conn)
    requested_node = str(node or "").strip() or None
    known_nodes = {item.get("node") for item in catalogue.get("nodes") or []}
    if requested_node and requested_node not in known_nodes:
        raise ValueError("unknown Proxmox node")
    targets = [item for item in catalogue.get("nodes") or []
               if item.get("status") == "online" and item.get("packages") and
               (requested_node is None or item.get("node") == requested_node)]
    if not targets:
        raise ValueError("no updates are available on online nodes")

    job_id = f"{device_id}-{time.time_ns()}"
    now = int(time.time())
    job = {
        "id": job_id,
        "deviceId": device_id,
        "operationType": "update",
        "state": "running",
        "stage": "preparing",
        "progressMode": "indeterminate",
        "startedAt": now,
        "updatedAt": now,
        "finishedAt": None,
        "totalNodes": len(targets),
        "completedNodes": 0,
        "failedNodes": 0,
        "currentNode": None,
        "percent": None,
        "message": "Starting update installation",
        "requestedNode": requested_node,
        "packageRefresh": {"status": "pending"},
        "nodes": [{
            "taskId": job_id,
            "node": target["node"],
            "packages": len(target.get("packages") or []),
            "state": "pending",
            "stage": "preparing",
            "progressMode": "indeterminate",
            "percent": None,
            "currentPackage": None,
            "message": "Waiting",
            "updateOutcome": "pending",
            "rebootStatus": "unknown",
            "rebootRequired": None,
        } for target in targets],
    }
    with _LOCK:
        current = _JOBS.get(device_id)
        if current and current.get("state") == "running":
            raise Conflict("an update installation is already running")
        _JOBS[device_id] = job
    _persist_operation(device_id, job)
    logbuf.log_event("info", "proxmox_update_started", source="device-updates",
                     device_id=device_id, node=requested_node, task_id=job_id,
                     packages=sum(item.get("packages", 0) for item in job["nodes"]))
    thread = threading.Thread(
        target=_run_install,
        args=(device_id, job_id, targets, ssh_credentials),
        name=f"updates-{device_id}",
        daemon=True,
    )
    thread.start()
    return copy.deepcopy(job)


def _run_reboot(device_id, job_id, target, ssh_credentials):
    name = target["node"]
    _set_node(device_id, job_id, 0, state="running", stage="rebooting",
              progressMode="indeterminate", percent=None,
              message="Sending reboot command")
    _set_job(device_id, job_id, currentNode=name, stage="rebooting",
             progressMode="indeterminate", percent=None,
             message=f"Sending reboot command to {name}", updatedAt=int(time.time()))
    try:
        with transports.open_connection(
                "ssh", target["_targetHost"], ssh_credentials.get("port") or 22,
                ssh_credentials, timeout=20) as conn:
            _run_command(conn, _REBOOT_NODE, "systemctl reboot", timeout=30)
        reboot = _unknown_reboot(
            "Reboot requested; refresh after the node returns to verify its kernel")
        try:
            _persist_node_reboot(device_id, name, reboot)
        except Exception as error:
            reboot.setdefault("diagnostics", []).append(
                f"Reboot state could not be persisted: {safe_error(error)[:180]}")
        finished = int(time.time())
        _set_node(device_id, job_id, 0, state="completed", stage="completed",
                  progressMode="exact", percent=100,
                  rebootStatus="unknown", rebootRequired=None, reboot=reboot,
                  message="Reboot command sent")
        _set_job(device_id, job_id, state="completed", stage="completed", currentNode=None,
                 progressMode="exact",
                 completedNodes=1, percent=100, finishedAt=finished,
                 updatedAt=finished, message=f"Reboot command sent to {name}.")
    except Exception as error:
        finished = int(time.time())
        message = safe_error(error)[:300]
        _set_node(device_id, job_id, 0, state="failed", stage="failed",
                  progressMode="exact", percent=100,
                  message=message)
        _set_job(device_id, job_id, state="failed", stage="failed", currentNode=None,
                 progressMode="exact",
                 completedNodes=1, failedNodes=1, percent=100,
                 finishedAt=finished, updatedAt=finished,
                 message=f"Reboot failed on {name}: {message}")


def start_reboot(device_id, node, confirmed=False, timeout=20):
    """Validate fresh node state and asynchronously send one fixed reboot command."""
    if confirmed is not True:
        raise ValueError("node reboot requires explicit confirmation")
    requested_node = str(node or "").strip()
    if not requested_node:
        raise ValueError("Proxmox node is required")
    with _LOCK:
        current = _JOBS.get(device_id)
        if current and current.get("state") == "running":
            raise Conflict("a Proxmox maintenance operation is already running")

    dev = devices.get_device(device_id)
    if not dev:
        raise ValueError("device not found")
    driver = _driver(dev)
    ssh_credentials = _ssh_credentials(dev)
    if not ssh_credentials:
        raise ValueError("configure root SSH credentials before rebooting a node")

    with devices.update_check_lock(device_id):
        with devices.device_conn(device_id, timeout=timeout) as (_, _, conn):
            catalogue = driver.available_updates(conn)
        _inspect_reboots(catalogue, ssh_credentials)
        _persist_maintenance(device_id, catalogue, ssh_credentials)
    target = next((item for item in catalogue.get("nodes") or []
                   if item.get("node") == requested_node), None)
    if target is None:
        raise ValueError("unknown Proxmox node")
    if target.get("status") != "online":
        raise ValueError("Proxmox node is not online")
    if (target.get("reboot") or {}).get("rebootStatus") != "required":
        raise ValueError("Proxmox node does not currently require a reboot")

    job_id = f"{device_id}-{time.time_ns()}"
    now = int(time.time())
    job = {
        "id": job_id,
        "deviceId": device_id,
        "operationType": "reboot",
        "state": "running",
        "stage": "preparing",
        "progressMode": "indeterminate",
        "startedAt": now,
        "updatedAt": now,
        "finishedAt": None,
        "totalNodes": 1,
        "completedNodes": 0,
        "failedNodes": 0,
        "currentNode": None,
        "percent": None,
        "message": "Starting node reboot",
        "requestedNode": requested_node,
        "nodes": [{
            "taskId": job_id,
            "node": requested_node,
            "state": "pending",
            "stage": "preparing",
            "progressMode": "indeterminate",
            "percent": None,
            "message": "Waiting",
            "rebootStatus": "required",
            "rebootRequired": True,
        }],
    }
    with _LOCK:
        current = _JOBS.get(device_id)
        if current and current.get("state") == "running":
            raise Conflict("a Proxmox maintenance operation is already running")
        _JOBS[device_id] = job
    _persist_operation(device_id, job)
    thread = threading.Thread(
        target=_run_reboot,
        args=(device_id, job_id, target, ssh_credentials),
        name=f"reboot-{device_id}-{requested_node}",
        daemon=True,
    )
    thread.start()
    return copy.deepcopy(job)

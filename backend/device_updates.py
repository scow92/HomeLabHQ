"""Software-update discovery and asynchronous installation for devices.

The Proxmox API lists apt updates, but its upgrade console is interactive and
restricted to ``root@pam`` (API tokens cannot launch it as root). HomelabHQ
therefore keeps the monitoring API token and optional root SSH credentials in
the same encrypted credential record. Discovery uses the driver API; install
jobs use fixed apt commands over SSH and expose process-local progress.
"""
from __future__ import annotations

import copy
import threading
import time

import crypto
import devices
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


def check(device_id, timeout=20):
    """Return live update availability plus the most recent install state."""
    with devices.device_conn(device_id, timeout=timeout) as (dev, driver, conn):
        _require_driver(driver)
        catalogue = driver.available_updates(conn)
    return _public_catalogue(catalogue, device_id, _ssh_credentials(dev))


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
    """Return a safe copy of a device's latest install operation."""
    with _LOCK:
        job = _JOBS.get(device_id)
        return copy.deepcopy(job) if job else None


def _set_job(device_id, job_id, **changes):
    with _LOCK:
        job = _JOBS.get(device_id)
        if not job or job["id"] != job_id:
            return None
        job.update(changes)
        return copy.deepcopy(job)


def _set_node(device_id, job_id, index, **changes):
    with _LOCK:
        job = _JOBS.get(device_id)
        if not job or job["id"] != job_id:
            return None
        job["nodes"][index].update(changes)
        job["updatedAt"] = int(time.time())
        return copy.deepcopy(job)


def _run_command(conn, command, label):
    code, _, _ = conn.run(command, timeout=3600)
    if code != 0:
        raise RuntimeError(f"{label} exited with status {code}")


def _run_install(device_id, job_id, targets, ssh_credentials):
    failed = 0
    total = max(1, len(targets))
    for index, target in enumerate(targets):
        name = target["node"]
        base = index / total * 100
        _set_node(device_id, job_id, index, state="running", percent=5,
                  message="Refreshing package lists")
        _set_job(device_id, job_id, currentNode=name, percent=round(base),
                 message=f"Refreshing package lists on {name}",
                 updatedAt=int(time.time()))
        try:
            with transports.open_connection(
                    "ssh", target["_targetHost"], ssh_credentials.get("port") or 22,
                    ssh_credentials, timeout=20) as conn:
                _run_command(conn, _APT_UPDATE, "apt-get update")
                _set_node(device_id, job_id, index, percent=35,
                          message="Installing updates")
                _set_job(device_id, job_id,
                         percent=round(base + (35 / total)),
                         message=f"Installing updates on {name}",
                         updatedAt=int(time.time()))
                _run_command(conn, _APT_UPGRADE, "apt-get dist-upgrade")
            _set_node(device_id, job_id, index, state="completed", percent=100,
                      message="Updates installed")
        except Exception as error:
            failed += 1
            _set_node(device_id, job_id, index, state="failed", percent=100,
                      message=safe_error(error)[:300])
        _set_job(device_id, job_id, completedNodes=index + 1,
                 percent=round((index + 1) / total * 100),
                 updatedAt=int(time.time()))

    finished = int(time.time())
    if failed:
        plural = "s" if failed != 1 else ""
        _set_job(device_id, job_id, state="failed", currentNode=None,
                 failedNodes=failed, finishedAt=finished,
                 message=f"Updates failed on {failed} node{plural}.")
    else:
        _set_job(device_id, job_id, state="completed", currentNode=None,
                 finishedAt=finished, message="All updates installed.")


def start(device_id, timeout=20):
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
    targets = [node for node in catalogue.get("nodes") or []
               if node.get("status") == "online" and node.get("packages")]
    if not targets:
        raise ValueError("no updates are available on online nodes")

    job_id = f"{device_id}-{time.time_ns()}"
    now = int(time.time())
    job = {
        "id": job_id,
        "state": "running",
        "startedAt": now,
        "updatedAt": now,
        "finishedAt": None,
        "totalNodes": len(targets),
        "completedNodes": 0,
        "failedNodes": 0,
        "currentNode": None,
        "percent": 0,
        "message": "Starting update installation",
        "nodes": [{
            "node": target["node"],
            "packages": len(target.get("packages") or []),
            "state": "pending",
            "percent": 0,
            "message": "Waiting",
        } for target in targets],
    }
    with _LOCK:
        current = _JOBS.get(device_id)
        if current and current.get("state") == "running":
            raise Conflict("an update installation is already running")
        _JOBS[device_id] = job
    thread = threading.Thread(
        target=_run_install,
        args=(device_id, job_id, targets, ssh_credentials),
        name=f"updates-{device_id}",
        daemon=True,
    )
    thread.start()
    return copy.deepcopy(job)

"""Aggregate operational status from persisted monitoring snapshots only."""
from datetime import datetime, timezone
import time
from typing import Iterable

import poller
import store

from backend.asgi.models import (
    ComponentStatus,
    StatusIssue,
    StatusSummaryResponse,
    StatusValue,
    TrueNASStatus,
)


_NETWORK_DRIVERS = poller.NETWORK_DRIVERS
_SEVERITY = {
    StatusValue.HEALTHY: 0,
    StatusValue.UNKNOWN: 1,
    StatusValue.STALE: 2,
    StatusValue.WARNING: 3,
    StatusValue.CRITICAL: 4,
}


def _worst(values: Iterable[StatusValue], *, empty=StatusValue.UNKNOWN) -> StatusValue:
    return max(values, key=lambda value: _SEVERITY[value], default=empty)


def _timestamp(value) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _as_datetime(value: float | None) -> datetime | None:
    return datetime.fromtimestamp(value, timezone.utc) if value is not None else None


def _source_timestamp(state: dict) -> float | None:
    return (_timestamp(state.get("reachabilityCheckedAt")) or
            _timestamp(state.get("sourceCheckedAt")) or
            _timestamp(state.get("ts")))


def _oldest_timestamp(values: Iterable[float | None]) -> float | None:
    return min((value for value in values if value is not None), default=None)


def _device_status(device: dict, now: float, stack: str) -> tuple[StatusValue, str | None]:
    state = device.get("state")
    if not isinstance(state, dict):
        return StatusValue.UNKNOWN, "no monitoring result is available"
    observed = _source_timestamp(state)
    if observed is None:
        return StatusValue.UNKNOWN, "the monitoring result has no timestamp"
    confirmed = state.get("confirmedOnline", state.get("online"))
    if confirmed is False:
        return StatusValue.CRITICAL, "the device is unreachable"
    if confirmed is not True:
        return StatusValue.UNKNOWN, "device reachability is unknown"
    if now - observed > poller.stale_after(stack):
        return StatusValue.STALE, "the last successful monitoring result is stale"
    if state.get("online") is False:
        return StatusValue.WARNING, "the latest poll failed but the offline threshold is not met"
    return StatusValue.HEALTHY, None


def _issue(component: str, code: str, message: str, status: StatusValue) -> StatusIssue:
    return StatusIssue(component=component, code=code, message=message, status=status)


def _component_identifier(item: dict, fallback: str) -> str:
    name = str(item.get("name") or item.get("id") or "").strip()
    host = str(item.get("host") or "").strip()
    if name and host and name.casefold() != host.casefold():
        return f"{name} ({host})"
    return name or host or fallback


def _network_status(devices: list[dict], now: float) -> ComponentStatus:
    issues = []
    states = []
    components = []
    for device in devices:
        identifier = _component_identifier(device, "network")
        components.append(identifier)
        status, message = _device_status(device, now, "network")
        states.append(status)
        if message:
            issues.append(_issue(
                identifier,
                "device_unreachable" if status is StatusValue.CRITICAL else
                "monitoring_stale" if status is StatusValue.STALE else "monitoring_unknown",
                message,
                status,
            ))
    return ComponentStatus(
        status=_worst(states),
        source_checked_at=_as_datetime(_oldest_timestamp(
            _source_timestamp(device.get("state") or {}) for device in devices)),
        healthy=sum(value is StatusValue.HEALTHY for value in states),
        total=len(states),
        components=components,
        issues=issues,
    )


def _proxmox_status(document: dict, devices: list[dict], now: float) -> ComponentStatus:
    issues: list[StatusIssue] = []
    states: list[StatusValue] = []
    node_states: list[StatusValue] = []
    instances = list(document.get("computeInstances", {}).values())
    for device in devices:
        maintenance = device.get("proxmoxMaintenance") or {}
        node_data = maintenance.get("nodes") or {}
        node_names = set(node_data)
        node_names.update(
            str(item["node"]) for item in instances
            if item.get("parentDeviceId") == device.get("id") and item.get("node")
        )
        if not node_names:
            node_names.add(str(device.get("name") or device.get("id") or "proxmox"))
        parent_status, parent_message = _device_status(device, now, "proxmox")
        checked = _timestamp(maintenance.get("checkedAt"))
        maintenance_stale = (
            checked is not None and now - checked > poller.stale_after("proxmox"))
        for node in sorted(node_names):
            current = parent_status
            message = parent_message
            code = "node_unreachable" if current is StatusValue.CRITICAL else "monitoring_unknown"
            node_state = node_data.get(node) or {}
            reported = str(node_state.get("status") or "").lower()
            if current is StatusValue.HEALTHY and maintenance_stale:
                current, message, code = (
                    StatusValue.STALE, "the cached Proxmox result is stale", "monitoring_stale")
            elif current is StatusValue.HEALTHY and reported in {"offline", "unreachable", "failed"}:
                current, message, code = (
                    StatusValue.CRITICAL, "the Proxmox node is unreachable", "node_unreachable")
            elif current is StatusValue.HEALTHY and reported in {"unknown", "unavailable"}:
                current, message, code = (
                    StatusValue.UNKNOWN, "the Proxmox node state is unknown", "monitoring_unknown")
            states.append(current)
            node_states.append(current)
            if message:
                issues.append(_issue(node, code, message, current))

        for item in instances:
            if item.get("parentDeviceId") != device.get("id"):
                continue
            if item.get("expectedToRun") is True and item.get("status") in {"stopped", "exited"}:
                states.append(StatusValue.CRITICAL)
                issues.append(_issue(
                    str(item.get("name") or item.get("id") or "guest"),
                    "guest_unexpectedly_stopped",
                    "a guest configured to remain running is stopped",
                    StatusValue.CRITICAL,
                ))
    return ComponentStatus(
        status=_worst(states),
        source_checked_at=_as_datetime(_oldest_timestamp(
            _timestamp((device.get("proxmoxMaintenance") or {}).get("checkedAt"))
            or _source_timestamp(device.get("state") or {}) for device in devices)),
        healthy=sum(value is StatusValue.HEALTHY for value in node_states),
        total=len(node_states),
        issues=issues,
    )


def _truenas_status(devices: list[dict], now: float) -> TrueNASStatus:
    if not devices:
        return TrueNASStatus(status=StatusValue.UNKNOWN)
    issues: list[StatusIssue] = []
    states: list[StatusValue] = []
    pools: list[str] = []
    alerts: list[int] = []
    for device in devices:
        current, message = _device_status(device, now, "truenas")
        values = ((device.get("state") or {}).get("values") or {})
        pool_value = values.get("pool_health", values.get("pool_status"))
        pool = str(pool_value).strip().upper() if pool_value is not None else None
        pool = pool or None
        pool_status = StatusValue.UNKNOWN
        if pool:
            pools.append(pool)
            pool_states = {item.strip() for item in pool.split(",") if item.strip()}
            if pool_states <= {"ONLINE", "HEALTHY"}:
                pool_status = StatusValue.HEALTHY
            elif pool_states & {"UNKNOWN", "UNAVAILABLE"}:
                pool_status = StatusValue.UNKNOWN
            else:
                pool_status = StatusValue.CRITICAL if pool_states & {
                    "FAULTED", "UNAVAIL", "OFFLINE", "SUSPENDED"
                } else StatusValue.WARNING
        active = values.get("alerts")
        alert_level = str(values.get("alert_level") or "").upper()
        active_count = active if isinstance(active, int) and not isinstance(active, bool) else None
        alert_status = StatusValue.UNKNOWN
        if active_count is not None:
            alerts.append(max(0, active_count))
            if active_count <= 0 or alert_level in {"INFO", "NOTICE"}:
                alert_status = StatusValue.HEALTHY
            elif alert_level in {
                    "ERROR", "CRITICAL", "ALERT", "EMERGENCY"
            }:
                alert_status = StatusValue.CRITICAL
            else:
                alert_status = StatusValue.WARNING
        if current is StatusValue.HEALTHY:
            current = _worst((pool_status, alert_status))
            if alert_status is StatusValue.CRITICAL:
                message = f"{active_count} active alert(s)"
            elif pool_status in {StatusValue.WARNING, StatusValue.CRITICAL}:
                message = f"pool state is {pool}"
            elif alert_status is StatusValue.WARNING:
                message = f"{active_count} active alert(s)"
            elif current is StatusValue.UNKNOWN:
                message = "current pool and alert monitoring data are unavailable"
        states.append(current)
        if message:
            issues.append(_issue(
                _component_identifier(device, "TrueNAS"),
                "active_alerts" if alert_status is StatusValue.CRITICAL
                else "pool_degraded" if pool_status in {
                    StatusValue.WARNING, StatusValue.CRITICAL
                } else "active_alerts" if alert_status is StatusValue.WARNING
                else "monitoring_stale" if current is StatusValue.STALE
                else "monitoring_unavailable",
                message,
                current,
            ))
    return TrueNASStatus(
        status=_worst(states),
        source_checked_at=_as_datetime(_oldest_timestamp(
            _source_timestamp(device.get("state") or {}) for device in devices)),
        pool=", ".join(pools) if pools else None,
        active_alerts=sum(alerts) if alerts else None,
        issues=issues,
    )


def _docker_status(document: dict, now: float) -> ComponentStatus:
    states: list[StatusValue] = []
    issues: list[StatusIssue] = []
    seen: set[tuple] = set()
    for workload in document.get("computeInstances", {}).values():
        docker = workload.get("docker") or {}
        containers = list(docker.get("containers") or [])
        for project in docker.get("projects") or []:
            containers.extend(project.get("containers") or [])
        discovery = workload.get("dockerDiscoveryState") or {}
        observed = _timestamp(discovery.get("sourceCheckedAt"))
        if observed is None and discovery.get("state") == "successful":
            # Compatibility for snapshots written before sourceCheckedAt was
            # introduced. Failed attempts never advance this fallback.
            observed = _timestamp(discovery.get("lastJobAt"))
        if observed is None:
            discovery_status = StatusValue.UNKNOWN
        elif now - observed > poller.stale_after("docker"):
            discovery_status = StatusValue.STALE
        else:
            discovery_status = StatusValue.HEALTHY
        for container in containers:
            key = (
                workload.get("id"), container.get("composeProject"),
                container.get("composeService"), container.get("name"),
            )
            if key in seen:
                continue
            seen.add(key)
            name = str(container.get("name") or container.get("composeService") or "container")
            host = str(workload.get("host") or workload.get("name") or
                       workload.get("id") or "Docker host")
            identifier = f"{name} ({host})"
            state = str(container.get("state") or "unknown").lower()
            health = str(container.get("health") or "unknown").lower()
            one_shot = container.get("oneShot")
            expected_to_run = container.get("expectedToRun")
            labels = container.get("labels") or {}
            restart_policy = str(container.get("restartPolicy") or "").lower()
            lifecycle = str(
                labels.get("com.homelabhq.lifecycle") or ""
            ).strip().lower()
            labelled_one_shot = lifecycle == "oneshot"
            if labelled_one_shot:
                one_shot = True
            elif not isinstance(one_shot, bool) and isinstance(expected_to_run, bool):
                one_shot = not expected_to_run
            if not isinstance(one_shot, bool) and str(
                    labels.get("com.docker.compose.oneoff") or "").lower() in {
                        "1", "true", "yes"}:
                one_shot = True
            if (not isinstance(one_shot, bool) and
                    restart_policy in {"always", "unless-stopped"}):
                one_shot = False
            exit_code = container.get("exitCode")
            message = None
            code = "container_unknown"
            if (state in {"exited", "stopped"} and one_shot is True and exit_code == 0):
                # A successful completion is expected history, not active inventory.
                continue
            if (state in {"exited", "stopped"} and labelled_one_shot and
                    exit_code not in {0, None}):
                status = StatusValue.CRITICAL
                message, code = (
                    f"one-shot container exited with code {exit_code}", "oneshot_failed")
            elif discovery_status is not StatusValue.HEALTHY:
                status = discovery_status
                message = (
                    "Docker discovery data is stale"
                    if status is StatusValue.STALE
                    else "Docker discovery data is unavailable"
                )
                code = "monitoring_stale" if status is StatusValue.STALE else "monitoring_unknown"
            elif state == "restarting":
                status = StatusValue.CRITICAL
                message, code = "container is restarting", "container_restarting"
            elif state == "running" and health == "unhealthy":
                status = StatusValue.CRITICAL
                message, code = "container health check is failing", "container_unhealthy"
            elif state == "running" and health == "starting":
                status = StatusValue.WARNING
                message, code = "container health check is starting", "container_starting"
            elif state == "running":
                status = StatusValue.HEALTHY
            elif state in {"exited", "stopped"} and (one_shot is False or exit_code not in {0, None}):
                status = StatusValue.CRITICAL
                message, code = "container stopped unexpectedly", "container_stopped"
            elif state in {"exited", "stopped"}:
                status = StatusValue.UNKNOWN
                message = "container lifecycle expectation is unknown"
            else:
                status = StatusValue.UNKNOWN
                message = "container state is unknown"
            states.append(status)
            if message:
                issues.append(_issue(identifier, code, message, status))
    return ComponentStatus(
        status=_worst(states),
        source_checked_at=_as_datetime(_oldest_timestamp(
            (_timestamp((workload.get("dockerDiscoveryState") or {}).get(
                "sourceCheckedAt")) or
             (_timestamp((workload.get("dockerDiscoveryState") or {}).get("lastJobAt"))
              if (workload.get("dockerDiscoveryState") or {}).get("state") == "successful"
              else None))
            for workload in document.get("computeInstances", {}).values()
            if (workload.get("docker") or {}).get("containers") or
            (workload.get("docker") or {}).get("projects"))),
        healthy=sum(value is StatusValue.HEALTHY for value in states),
        total=len(states),
        issues=issues,
    )


def status_summary(now: float | None = None) -> StatusSummaryResponse:
    """Return cached state without invoking any integration or scan."""
    now = time.time() if now is None else now
    document = store.load()
    devices = list(document.get("devices", {}).values())
    network = _network_status(
        [item for item in devices if item.get("driverId") in _NETWORK_DRIVERS], now)
    proxmox = _proxmox_status(
        document, [item for item in devices if item.get("driverId") == "proxmox.ve"], now)
    truenas = _truenas_status(
        [item for item in devices if item.get("driverId") == "truenas.system"], now)
    docker = _docker_status(document, now)
    statuses = [network.status, proxmox.status, truenas.status, docker.status]
    observations = [
        timestamp for timestamp in (
            _source_timestamp(item.get("state") or {}) for item in devices
        ) if timestamp is not None
    ]
    observations.extend(
        timestamp for timestamp in (
            _timestamp((item.get("dockerDiscoveryState") or {}).get("sourceCheckedAt"))
            or (_timestamp((item.get("dockerDiscoveryState") or {}).get("lastJobAt"))
                if (item.get("dockerDiscoveryState") or {}).get("state") == "successful"
                else None)
            for item in document.get("computeInstances", {}).values()
        ) if timestamp is not None
    )
    checked = max(observations, default=now)
    stale = any(value is StatusValue.STALE for value in statuses)
    return StatusSummaryResponse(
        overall=_worst(statuses),
        network=network,
        proxmox=proxmox,
        truenas=truenas,
        docker=docker,
        checked_at=datetime.fromtimestamp(checked, timezone.utc),
        stale=stale,
    )

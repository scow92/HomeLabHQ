"""Persisted, restricted background maintenance jobs for Compute instances."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import secrets
import threading
import time
from datetime import datetime, timezone

import ansible_integration as ansible
import compute
import logbuf
import store
from domain import safe_error
from errors import Conflict


ACTIVE_STATES = frozenset({"queued", "running"})
TERMINAL_STATES = frozenset({
    "successful", "incomplete", "failed", "unreachable", "cancelled",
})
OPERATIONS = frozenset({
    "os_check", "os_update", "docker_check", "docker_discovery",
    "docker_project_update", "appliance_health",
})
MAX_JOBS = max(10, int(os.environ.get("HLHQ_MAX_COMPUTE_JOBS", "500")))
_RECAP_RE = re.compile(
    r"^(.+?)\s+:\s+ok=(\d+)\s+changed=(\d+)\s+unreachable=(\d+)\s+"
    r"failed=(\d+)\s+skipped=(\d+)\s+rescued=(\d+)\s+ignored=(\d+)\s*$")
_MARKER = "HOMELABHQ_RESULT:"
_DOCKER_STATUS_EXIT_RE = re.compile(r"^Exited \((\d+)\)(?:\s|$)", re.IGNORECASE)
_DOCKER_STATUS_HEALTH_RE = re.compile(
    r"\((?:(healthy|unhealthy)|health:\s*(starting))\)(?:\s|$)", re.IGNORECASE)
_RESULT_KEYS = (
    "homelabhq_update", "homelabhq_docker", "homelabhq_docker_update",
)
_LOCK = threading.RLock()


def parse_recap(output: str) -> dict[str, dict]:
    """Parse only Ansible's standard PLAY RECAP counters."""
    result = {}
    for raw in (output or "").splitlines():
        match = _RECAP_RE.match(raw.strip())
        if not match:
            continue
        host, *values = match.groups()
        result[host] = dict(zip(
            ("ok", "changed", "unreachable", "failed", "skipped", "rescued", "ignored"),
            map(int, values), strict=True))
    return result


def _extract_json_after_marker(line: str):
    candidate = line.split(_MARKER, 1)[1].strip().rstrip(",")
    if candidate.endswith('"') and candidate.startswith('"'):
        try:
            candidate = json.loads(candidate)
        except json.JSONDecodeError:
            pass
    # The default Ansible callback renders debug.msg as an escaped JSON string
    # on one line. Decode that representation without treating any other
    # human-readable output as structured data.
    if '\\"' in candidate:
        if candidate.endswith('"'):
            candidate = candidate[:-1]
        candidate = candidate.replace('\\"', '"').replace('\\\\', '\\')
    decoder = json.JSONDecoder()
    for index, char in enumerate(candidate):
        if char != "{":
            continue
        try:
            return decoder.raw_decode(candidate[index:])[0]
        except json.JSONDecodeError:
            continue
    return None


def _result_mapping(value) -> dict | None:
    if not isinstance(value, dict):
        return None
    result = {key: value[key] for key in _RESULT_KEYS if key in value}
    return result or None


def _walk_mappings(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_mappings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_mappings(child)


def _set_stats_result(value) -> dict | None:
    """Prefer data published through set_stats in callback/event structures."""
    for mapping in _walk_mappings(value):
        stats = mapping.get("ansible_stats")
        if not isinstance(stats, dict):
            continue
        data = stats.get("data", stats)
        if result := _result_mapping(data):
            return result
    return None


def _direct_result(value) -> dict | None:
    for mapping in _walk_mappings(value):
        if result := _result_mapping(mapping):
            return result
    return None


def _debug_msg_result(value) -> tuple[dict | None, str | None]:
    error = None
    for mapping in _walk_mappings(value):
        if "msg" not in mapping:
            continue
        message = mapping["msg"]
        if isinstance(message, dict):
            if result := _direct_result(message):
                return result, None
            error = "HomeLabHQ result key not found in msg object"
            continue
        if not isinstance(message, str):
            continue
        candidate = message.strip()
        if _MARKER in candidate:
            decoded = _extract_json_after_marker(candidate)
        elif candidate.startswith(("{", "[")):
            try:
                decoded = json.loads(candidate)
            except json.JSONDecodeError:
                error = "JSON in msg could not be decoded"
                continue
        else:
            continue
        if result := _direct_result(decoded):
            return result, None
        error = "HomeLabHQ result key not found in msg JSON"
    return None, error


def _json_values(output: str):
    """Yield complete JSON values embedded in plain, non-ANSI Ansible output."""
    decoder = json.JSONDecoder()
    index = 0
    while index < len(output):
        positions = [position for char in ("{", "[")
                     if (position := output.find(char, index)) >= 0]
        if not positions:
            return
        start = min(positions)
        try:
            value, end = decoder.raw_decode(output[start:])
        except json.JSONDecodeError:
            index = start + 1
            continue
        yield value
        index = start + end


def parse_structured_result_details(output: str, callback_data=None) -> dict:
    """Extract a HomeLabHQ result with source and safe parse diagnostics.

    Explicit callback/event data is authoritative. Sanitized stdout is a
    compatibility fallback for the default Ansible callback's JSON blocks.
    """
    errors = []
    def extract(roots):
        for prefix, root in roots:
            if result := _set_stats_result(root):
                return {"result": result, "source": f"{prefix}.set_stats", "error": None}
        for prefix, root in roots:
            if result := _direct_result(root):
                return {"result": result, "source": f"{prefix}.result", "error": None}
        for prefix, root in roots:
            result, error = _debug_msg_result(root)
            if result:
                return {"result": result, "source": f"{prefix}.debug_msg", "error": None}
            if error:
                errors.append(error)
        return None

    if callback_data is not None:
        if parsed := extract([("callback", callback_data)]):
            return parsed
    text = str(output or "")
    stripped = text.strip()
    if stripped.startswith(("{", "[")):
        try:
            if parsed := extract([("stdout_callback", json.loads(stripped))]):
                return parsed
        except json.JSONDecodeError:
            pass
    roots = [("stdout_json", value) for value in _json_values(text)]
    if parsed := extract(roots):
        return parsed

    found = None
    for line in text.splitlines():
        if _MARKER not in line:
            continue
        candidate = _extract_json_after_marker(line)
        if result := _direct_result(candidate):
            found = result
        else:
            errors.append("HomeLabHQ result marker payload could not be decoded")
    if found:
        return {"result": found, "source": "stdout_marker", "error": None}
    return {"result": None, "source": None,
            "error": errors[0] if errors else "HomeLabHQ result key not found"}


def parse_structured_result(output: str, callback_data=None) -> dict | None:
    """Return the parsed HomeLabHQ contract from callback data or stdout."""
    return parse_structured_result_details(output, callback_data)["result"]


def _update_contract(value) -> dict | None:
    if not isinstance(value, dict):
        return None
    count = value.get("count")
    if count is not None:
        try:
            count = max(0, int(count))
        except (TypeError, ValueError):
            count = None
    available = value.get("available")
    reboot = value.get("reboot_required")
    return {
        "available": available if isinstance(available, bool) else None,
        "count": count,
        "rebootRequired": reboot if isinstance(reboot, bool) else None,
        "summary": str(value.get("summary") or "")[:500] or None,
    }


def _project_id(name: str, paths) -> str:
    if isinstance(paths, str):
        paths = [paths]
    identity = name + "\0" + "\0".join(paths or [])
    return hashlib.sha256(identity.encode()).hexdigest()[:16]


def _docker_project(instance: dict, selector, *, allow_single=False) -> dict:
    """Resolve an approved discovered project by inventory name.

    Persisted opaque IDs remain accepted for backwards-compatible API calls,
    but every Ansible variable is built from the matched project's name.
    """
    projects = (instance.get("docker") or {}).get("projects") or []
    selected = str(selector or "").strip()
    if selected:
        project = next((item for item in projects if item.get("name") == selected), None)
        if project is None:
            project = next((item for item in projects if item.get("id") == selected), None)
        if project is None:
            raise ValueError("Docker Compose project was not discovered")
        return project
    if allow_single and len(projects) == 1:
        return projects[0]
    if len(projects) > 1:
        raise ValueError("a Docker Compose project must be selected")
    raise ValueError("Docker Compose project was not discovered")


def _text_list(value, *, limit=1000) -> list[str]:
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, (list, tuple)):
        return []
    result = []
    for raw in value:
        item = str(raw or "").strip()[:limit]
        if item and item not in result:
            result.append(item)
    return result


def _labels_contract(value) -> tuple[dict[str, str], str | None]:
    if isinstance(value, dict):
        labels = {str(key).strip()[:300]: str(raw or "").strip()[:1000]
                  for key, raw in list(value.items())[:200] if str(key).strip()}
        return labels, None
    raw = str(value or "").strip()[:20_000]
    labels = {}
    previous_key = None
    for item in raw.split(","):
        key, separator, label_value = item.partition("=")
        key = key.strip()[:300]
        if separator and key:
            labels[key] = label_value.strip()[:1000]
            previous_key = key
        elif key and previous_key:
            labels[previous_key] = f"{labels[previous_key]},{key}"[:1000]
    return labels, raw or None


def _integer_contract(*values) -> int | None:
    for value in values:
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            try:
                return int(value.strip())
            except ValueError:
                continue
    return None


def _truthy_label(value) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes"}


def _docker_status_health(status: str) -> str | None:
    match = _DOCKER_STATUS_HEALTH_RE.search(status)
    if not match:
        return None
    return str(match.group(1) or match.group(2)).lower()


def _docker_status_exit_code(status: str) -> int | None:
    match = _DOCKER_STATUS_EXIT_RE.match(status)
    return int(match.group(1)) if match else None


def _restart_policy_contract(raw_container, inspect_host_config) -> str | None:
    value = raw_container.get(
        "restart_policy", raw_container.get("restartPolicy", raw_container.get("RestartPolicy")))
    if value is None and isinstance(inspect_host_config, dict):
        value = inspect_host_config.get("RestartPolicy")
    if isinstance(value, dict):
        value = value.get("Name") or value.get("name")
    policy = str(value or "").strip().lower().replace("_", "-")[:50]
    return policy if policy in {"no", "none", "always", "unless-stopped", "on-failure"} else None


def _container_contract(raw_container) -> dict | None:
    if not isinstance(raw_container, dict):
        return None
    name = str(raw_container.get("name") or raw_container.get("Names") or
               raw_container.get("Name") or "").strip().lstrip("/")
    if not name:
        return None
    inspect_state = raw_container.get("State")
    state_value = raw_container.get("state")
    if state_value is None:
        state_value = (inspect_state.get("Status") if isinstance(inspect_state, dict)
                       else inspect_state)
    if isinstance(inspect_state, dict):
        if inspect_state.get("Dead") is True:
            state_value = "dead"
        elif inspect_state.get("Paused") is True:
            state_value = "paused"
        elif inspect_state.get("Restarting") is True:
            state_value = "restarting"
        elif inspect_state.get("Running") is True:
            state_value = "running"
    state = str(state_value or "unknown").lower()[:50]
    if state not in {
            "created", "running", "stopped", "restarting", "removing", "paused",
            "exited", "dead", "unknown"}:
        state = "unknown"
    status = str(raw_container.get("status", raw_container.get("Status")) or "").strip()
    health_present = any(key in raw_container for key in ("health", "Health", "HealthStatus"))
    health_value = raw_container.get(
        "health", raw_container.get("Health", raw_container.get("HealthStatus")))
    inspect_health = inspect_state.get("Health") if isinstance(inspect_state, dict) else None
    if isinstance(inspect_health, dict):
        health_present = True
        health_value = inspect_health.get("Status")

    configured_value = raw_container.get(
        "has_healthcheck", raw_container.get("hasHealthcheck"))
    has_healthcheck = configured_value if isinstance(configured_value, bool) else None
    if has_healthcheck is None and isinstance(inspect_state, dict) and "Health" in inspect_state:
        has_healthcheck = isinstance(inspect_health, dict)
    config = raw_container.get("Config")
    inspect_host_config = raw_container.get("HostConfig")
    if has_healthcheck is None and isinstance(inspect_state, dict) and isinstance(config, dict):
        healthcheck = config.get("Healthcheck")
        test = healthcheck.get("Test") if isinstance(healthcheck, dict) else None
        has_healthcheck = bool(healthcheck) and test != ["NONE"]
    if not health_present and has_healthcheck is None:
        status_health = _docker_status_health(status)
        if status_health is not None:
            health_present = True
            health_value = status_health

    health_text = str(health_value or "").strip().lower()[:50]
    if health_present and health_text in {
            "", "none", "null", "no healthcheck", "no_healthcheck"}:
        has_healthcheck = False
        health = None
    elif health_text in {"healthy", "unhealthy", "starting"}:
        has_healthcheck = True
        health = health_text
    elif health_text == "unknown":
        health = "unknown"
    elif health_present or has_healthcheck is True:
        health = "unknown"
    else:
        health = None if has_healthcheck is False else "unknown"

    raw_health_details = raw_container.get(
        "health_details", raw_container.get("healthDetails"))
    health_details: dict[str, object] = {}
    if isinstance(raw_health_details, dict):
        failing_streak = raw_health_details.get(
            "failing_streak", raw_health_details.get("failingStreak"))
        if isinstance(failing_streak, int) and failing_streak >= 0:
            health_details["failingStreak"] = min(failing_streak, 1_000_000)
        output = str(raw_health_details.get("output") or "").strip()
        if output:
            health_details["output"] = output[:2000]
        exit_code = raw_health_details.get(
            "exit_code", raw_health_details.get("exitCode"))
        if isinstance(exit_code, int):
            health_details["exitCode"] = exit_code
    if isinstance(inspect_health, dict):
        failing_streak = inspect_health.get("FailingStreak")
        if isinstance(failing_streak, int) and failing_streak >= 0:
            health_details["failingStreak"] = min(failing_streak, 1_000_000)
        logs = inspect_health.get("Log")
        if isinstance(logs, list):
            latest = next((entry for entry in reversed(logs)
                           if isinstance(entry, dict)), None)
            if latest:
                output = str(latest.get("Output") or "").strip()
                if output:
                    health_details["output"] = output[:2000]
                exit_code = latest.get("ExitCode")
                if isinstance(exit_code, int):
                    health_details["exitCode"] = exit_code
    config_labels, _ = _labels_contract(
        config.get("Labels") if isinstance(config, dict) else None)
    labels, labels_raw = _labels_contract(
        raw_container.get("labels", raw_container.get("Labels")))
    labels = {**config_labels, **labels}
    exit_code = _integer_contract(
        raw_container.get("exit_code"), raw_container.get("exitCode"),
        raw_container.get("ExitCode"),
        inspect_state.get("ExitCode") if isinstance(inspect_state, dict) else None)
    if exit_code is None and state == "exited":
        exit_code = _docker_status_exit_code(status)
    one_shot_value = raw_container.get("one_shot", raw_container.get("oneShot"))
    one_shot = one_shot_value if isinstance(one_shot_value, bool) else None
    expected_to_run = raw_container.get(
        "expected_to_run", raw_container.get("expectedToRun"))
    expected_to_run = expected_to_run if isinstance(expected_to_run, bool) else None
    lifecycle = str(labels.get("com.homelabhq.lifecycle") or "").strip().lower()
    if lifecycle == "oneshot":
        one_shot = True
    elif one_shot is None and isinstance(expected_to_run, bool):
        one_shot = not expected_to_run
    if one_shot is None and _truthy_label(labels.get("com.docker.compose.oneoff")):
        one_shot = True
    networks = _text_list(
        raw_container.get("networks", raw_container.get("Networks")), limit=300)
    ports = str(raw_container.get("ports", raw_container.get("Ports")) or "").strip()
    compose_project = str(
        raw_container.get("compose_project") or raw_container.get("composeProject") or
        raw_container.get("Project") or labels.get("com.docker.compose.project") or ""
    ).strip()[:200] or None
    compose_service = str(
        raw_container.get("compose_service") or raw_container.get("composeService") or
        raw_container.get("Service") or labels.get("com.docker.compose.service") or ""
    ).strip()[:200] or None
    return {
        "name": name[:200],
        "state": state,
        "health": health,
        "hasHealthcheck": has_healthcheck,
        "healthDetails": health_details or None,
        "exitCode": exit_code,
        "oneShot": one_shot,
        "expectedToRun": expected_to_run,
        "restartPolicy": _restart_policy_contract(raw_container, inspect_host_config),
        "image": str(raw_container.get("image", raw_container.get("Image")) or "")[:500] or None,
        "status": status[:500] or None,
        "labels": labels,
        "labelsRaw": labels_raw,
        "networks": networks,
        "ports": ports[:2000] or None,
        "composeProject": compose_project,
        "composeService": compose_service,
    }


def _mark_completed_dependencies(containers: list[dict]) -> None:
    completed_services = set()
    for container in containers:
        dependency_spec = container.get("labels", {}).get("com.docker.compose.depends_on")
        for dependency in str(dependency_spec or "").split(","):
            service, separator, remainder = dependency.strip().partition(":")
            if separator and remainder.split(":", 1)[0] == "service_completed_successfully":
                completed_services.add(service)
    for container in containers:
        if container.get("composeService") in completed_services:
            container["oneShot"] = True


def _image_contract(raw_image) -> dict | None:
    if isinstance(raw_image, str):
        return {"name": raw_image[:500], "id": None, "tags": []}
    if not isinstance(raw_image, dict):
        return None
    name = str(raw_image.get("name") or raw_image.get("repository") or "").strip()[:500]
    image_id = str(raw_image.get("id") or "").strip()[:200] or None
    tags = _text_list(raw_image.get("tags"), limit=500)
    if not name and not image_id and not tags:
        return None
    return {"name": name or None, "id": image_id, "tags": tags}


def _docker_contract(value) -> dict | None:
    if not isinstance(value, dict):
        return None
    exists = value.get("available", value.get("exists"))
    if not isinstance(exists, bool):
        return None
    if any(key in value and not isinstance(value[key], list)
           for key in ("projects", "containers", "images")):
        return None
    projects_by_name = {}
    for raw_project in value.get("projects") or []:
        if not isinstance(raw_project, dict):
            continue
        name = str(raw_project.get("name") or raw_project.get("Name") or "").strip()[:200]
        config_files = _text_list(
            raw_project.get("config_files", raw_project.get(
                "configFiles", raw_project.get("ConfigFiles"))))
        legacy_path = str(raw_project.get("path") or "").strip()[:1000]
        if legacy_path and legacy_path not in config_files:
            config_files.insert(0, legacy_path)
        if not name:
            continue
        containers = [item for raw in raw_project.get("containers") or []
                      if (item := _container_contract(raw)) is not None]
        for container in containers:
            container["composeProject"] = name
        images = [item for raw in raw_project.get("images") or []
                  if (item := _image_contract(raw)) is not None]
        raw_mode = raw_project.get(
            "update_mode", raw_project.get("updateMode",
            raw_project.get("update_strategy", raw_project.get("updateStrategy"))))
        try:
            mode = ansible.normalize_project_mode(raw_mode)
        except ValueError:
            mode = "read_only"
        legacy_strategy = {"build": "local_build", "read_only": "unmanaged"}.get(mode, mode)
        projects_by_name[name] = {
            "name": name,
            "path": legacy_path or (config_files[0] if config_files else None),
            "configFiles": config_files,
            "workingDir": str(raw_project.get(
                "working_dir", raw_project.get("workingDir", "")))[:1000] or None,
            "status": str(raw_project.get("status", raw_project.get("Status")) or "")[:500] or None,
            "managed": False,
            "updateMode": mode,
            "updateStrategy": legacy_strategy,
            "containers": containers,
            "images": images,
        }

    direct_containers = []
    for raw in value.get("containers") or []:
        container = _container_contract(raw)
        if container is None:
            continue
        project_name = container.get("composeProject")
        if not project_name:
            direct_containers.append(container)
            continue
        labels = container["labels"]
        project = projects_by_name.setdefault(project_name, {
            "name": project_name, "path": None, "configFiles": [],
            "workingDir": None, "status": None, "managed": False,
            "updateMode": "read_only", "updateStrategy": "unmanaged",
            "containers": [], "images": [],
        })
        for config_file in _text_list(
                labels.get("com.docker.compose.project.config_files")):
            if config_file not in project["configFiles"]:
                project["configFiles"].append(config_file)
        if project["path"] is None and project["configFiles"]:
            project["path"] = project["configFiles"][0]
        project["workingDir"] = (project.get("workingDir") or
                                 labels.get("com.docker.compose.project.working_dir") or None)
        if not any(item["name"] == container["name"] for item in project["containers"]):
            project["containers"].append(container)

    projects = []
    for project in projects_by_name.values():
        _mark_completed_dependencies(project["containers"])
        project["id"] = _project_id(project["name"], project["configFiles"])
        projects.append(project)
    images = [item for raw in value.get("images") or []
              if (item := _image_contract(raw)) is not None]
    return {
        "available": exists,
        "version": str(value.get("version") or "")[:200] or None,
        "composeAvailable": (value.get("compose_available", value.get("composeAvailable"))
                             if isinstance(value.get(
                                 "compose_available", value.get("composeAvailable")), bool)
                             else None),
        "composeVersion": str(value.get(
            "compose_version", value.get("composeVersion")) or "")[:200] or None,
        "summary": str(value.get("summary") or "")[:500] or None,
        "projects": projects,
        "containers": direct_containers,
        "images": images,
        "lastDiscoveredAt": int(time.time()),
    }


def _docker_update_contract(value) -> dict | None:
    if not isinstance(value, dict):
        return None
    available = value.get(
        "updates_available", value.get("update_available", value.get("available")))
    if not isinstance(available, bool):
        available = None
    raw_projects = value.get("projects")
    if isinstance(raw_projects, dict):
        raw_projects = [raw_projects]
    elif raw_projects is None:
        raw_projects = []
    elif not isinstance(raw_projects, list):
        return None
    single = value.get("project")
    if not raw_projects and isinstance(single, dict):
        raw_projects = [single]
    elif not raw_projects:
        name = (single if isinstance(single, str) else
                value.get("project_name", value.get("docker_project", value.get("name"))))
        if isinstance(name, str) and name.strip():
            raw_projects = [{**value, "name": name}]
    projects = []
    for raw in raw_projects:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()[:200]
        if not name:
            continue
        mode = raw.get("update_mode", raw.get("updateMode"))
        try:
            mode = ansible.normalize_project_mode(mode)
        except ValueError:
            mode = None
        updates = raw.get("updates_available", raw.get(
            "update_available", raw.get("updatesAvailable")))
        projects.append({
            "name": name,
            "updatesAvailable": updates if isinstance(updates, bool) else None,
            "updateMode": mode,
            "summary": str(raw.get("summary") or "")[:500] or None,
        })
    summary = str(value.get("summary") or "")[:500] or None
    supported = value.get("supported")
    if available is None and not projects and summary is None and not isinstance(supported, bool):
        return None
    return {
        "available": available,
        "projects": projects,
        "summary": summary,
        "supported": supported if isinstance(supported, bool) else None,
        "lastCheckedAt": int(time.time()),
    }


def _approved_project(controller: dict, target: str, project_name: str) -> dict:
    """Resolve the authoritative inventory project for one Ansible target."""
    project = ansible.inventory_docker_project(controller, target, project_name)
    if project is None:
        raise ValueError(
            "Docker Compose project is not approved for this inventory host")
    return project


def approved_docker_project_names(instance: dict, document: dict | None = None) -> list[str]:
    """Return discovered projects approved for this instance's exact host."""
    document = document or store.load()
    mapping = instance.get("ansible") or {}
    controller = document["ansibleControllers"].get(mapping.get("controllerId")) or {}
    target = mapping.get("inventoryHost")
    try:
        host = ansible.inventory_host(controller, ansible.validate_inventory_host(target))
    except ValueError:
        return []
    approved = {project.get("name") for project in (host or {}).get("dockerProjects") or []}
    result = []
    for project in (instance.get("docker") or {}).get("projects") or []:
        name = project.get("name")
        if name in approved and name not in result:
            result.append(name)
    return result


def _public_job(job: dict) -> dict:
    return copy.deepcopy(job)


def get_job(job_id: str) -> dict | None:
    job = store.load()["computeJobs"].get(job_id)
    return _public_job(job) if job else None


def list_jobs(instance_id: str, limit=20) -> list[dict]:
    jobs = [job for job in store.load()["computeJobs"].values()
            if job.get("computeInstanceId") == instance_id]
    jobs.sort(key=lambda job: job.get("createdAt", 0), reverse=True)
    return [_public_job(job) for job in jobs[:max(1, min(int(limit), 100))]]


def update_check_result(job: dict) -> dict:
    """Expose the normalized result already used by Compute state persistence.

    Scheduled orchestration consumes this adapter so it cannot drift from the
    manual Compute checks' HOMELABHQ_RESULT parsing or update-detection rules.
    """
    operation = job.get("operation")
    if operation not in {"os_check", "docker_check"}:
        raise ValueError("job is not an update check")
    job_state = job.get("state") or "failed"
    normalized = _state_for_result(
        operation, job.get("structuredResult"), job.get("finishedAt") or int(time.time()),
        job.get("projectName"), job.get("mode"))
    state = normalized.get("state")
    if job_state not in {"successful", "incomplete"}:
        state = job_state
    elif job_state == "incomplete":
        state = "incomplete"
    return {
        "jobId": job.get("id"),
        "state": state,
        "updatesAvailable": state == "updates_available",
        "updateCount": normalized.get("updateCount"),
        "rebootRequired": normalized.get("rebootRequired"),
        "summary": normalized.get("summary") or job.get("summary"),
        "error": job.get("summary") if job_state != "successful" else None,
        "projectName": job.get("projectName"),
        "structuredResultSource": job.get("structuredResultSource"),
    }


def _set_job(job_id: str, **changes) -> dict | None:
    def mutate(document):
        job = document["computeJobs"].get(job_id)
        if not job:
            return None
        job.update(changes)
        return copy.deepcopy(job)
    return store.update(mutate)


def _set_instance(instance_id: str, **changes) -> None:
    def mutate(document):
        instance = document["computeInstances"].get(instance_id)
        if instance:
            instance.update(changes)
    store.update(mutate)


def _set_maintenance_state(instance_id: str, state_key: str, changes: dict) -> None:
    """Replace one state machine while retaining its independent timestamps."""
    def mutate(document):
        instance = document["computeInstances"].get(instance_id)
        if not instance:
            return
        previous = instance.get(state_key) or {}
        state = {key: previous[key] for key in (
            "lastCheckedAt", "lastUpdatedAt", "sourceCheckedAt")
                 if key in previous}
        state.update(changes)
        instance[state_key] = state
    store.update(mutate)


def _set_docker_project_state(job: dict, changes: dict) -> None:
    """Update only the requested host/project pair and recompute host summaries."""
    project_name = job.get("projectName")
    if not project_name:
        return

    def mutate(document):
        for instance in document["computeInstances"].values():
            mapping = instance.get("ansible") or {}
            if (mapping.get("controllerId") != job.get("controllerId") or
                    mapping.get("inventoryHost") != job.get("ansibleTarget")):
                continue
            projects = (instance.get("docker") or {}).get("projects") or []
            project = next((item for item in projects
                            if item.get("name") == project_name), None)
            if not project:
                continue
            previous = project.get("updateState") or {}
            state = {key: previous[key] for key in ("lastCheckedAt", "lastUpdatedAt")
                     if key in previous}
            state.update(changes)
            project["updateState"] = state
            previous_aggregate = instance.get("dockerUpdateState") or {}
            aggregate = _aggregate_docker_project_state(projects)
            for key in ("lastCheckedAt", "lastUpdatedAt"):
                timestamps = [(item.get("updateState") or {}).get(key)
                              for item in projects]
                timestamps.append(previous_aggregate.get(key))
                if values := [value for value in timestamps if isinstance(value, int)]:
                    aggregate[key] = max(values)
            aggregate.update(lastJobId=job["id"], lastJobAt=changes.get("lastJobAt"))
            instance["dockerUpdateState"] = aggregate
    store.update(mutate)


def _aggregate_docker_project_state(projects: list[dict]) -> dict:
    approved_projects = [project for project in projects
                         if project.get("approved") is not False]
    if projects and not approved_projects:
        return {
            "state": "not_applicable", "updateCount": None,
            "summary": "No discovered Docker projects are approved in inventory",
        }
    states = [(project.get("updateState") or {}).get("state")
              for project in approved_projects]
    values = [(project.get("updateState") or {}).get("updatesAvailable")
              for project in approved_projects]
    if "updating" in states:
        return {"state": "updating", "updateCount": values.count(True)}
    if "checking" in states:
        return {"state": "checking", "updateCount": values.count(True)}
    if True in values:
        return {"state": "updates_available", "updateCount": values.count(True)}
    for failed_state in ("unreachable", "failed", "incomplete"):
        if failed_state in states:
            count = states.count(failed_state)
            return {
                "state": failed_state, "updateCount": None,
                "summary": f"{count} Docker project check{'' if count == 1 else 's'} "
                           f"{failed_state.replace('_', ' ')}",
            }
    supported_values = [value for state, value in zip(states, values, strict=True)
                        if state not in {"read_only", "not_applicable", "unmanaged"}]
    if supported_values and all(value is False for value in supported_values):
        return {"state": "up_to_date", "updateCount": 0}
    if "check_recommended" in states:
        return {"state": "check_recommended", "updateCount": None,
                "summary": "A Docker project was updated; run a new check"}
    if states and all(state in {"read_only", "not_applicable", "unmanaged"}
                      for state in states):
        return {"state": "not_applicable", "updateCount": None,
                "summary": "Registry update checks are not applicable"}
    if "unknown" in states:
        return {"state": "unknown", "updateCount": None,
                "summary": "A Docker project check could not determine update availability"}
    if "not_checked" in states:
        return {"state": "not_checked", "updateCount": None}
    return {"state": "unknown", "updateCount": None}


def _docker_project_result_state(contract: dict | None, project_name: str,
                                 mode: str, now: int) -> dict:
    update = _docker_update_contract(
        (contract or {}).get("homelabhq_docker_update"))
    selected = next((item for item in (update or {}).get("projects") or []
                     if item.get("name") == project_name), None)
    legacy = (_update_contract((contract or {}).get("homelabhq_update"))
              if update is None else None)
    result = {
        "state": "unknown", "updatesAvailable": None,
        "lastCheckedAt": now,
    }
    if selected is not None:
        result["summary"] = selected.get("summary") or (update or {}).get("summary")
        available = selected.get("updatesAvailable")
    elif update is not None and not update.get("projects"):
        result["summary"] = update.get("summary")
        available = update.get("available")
    elif legacy is not None:
        result["summary"] = legacy.get("summary")
        available = legacy.get("available")
    else:
        result["summary"] = "Playbook returned no structured Docker update result"
        available = None
    if mode == "read_only":
        result.update(
            state="read_only", updatesAvailable=None,
            summary=result.get("summary") or
            "Update checks are read-only for this inventory project")
    elif mode == "build":
        result.update(
            state="not_applicable", updatesAvailable=None,
            summary=result.get("summary") or
            "Registry update availability is not applicable to locally built projects")
    else:
        result["updatesAvailable"] = available if isinstance(available, bool) else None
        result["state"] = ("updates_available" if available is True else
                           "up_to_date" if available is False else "unknown")
    return result


def _os_state_for_result(contract: dict | None, now: int, timestamp: str) -> dict:
    update = _update_contract((contract or {}).get("homelabhq_update"))
    state = {"state": "unknown", "lastJobAt": now, timestamp: now}
    if update is None:
        state["summary"] = "Playbook returned no structured OS update result"
        return state
    available = update.get("available")
    state["state"] = ("updates_available" if available is True else
                      "up_to_date" if available is False else "unknown")
    state.update({"updateCount": update.get("count"),
                  "rebootRequired": update.get("rebootRequired"),
                  "summary": update.get("summary")})
    return state


def _state_for_result(operation: str, contract: dict | None, now: int,
                      project_name=None, project_mode=None) -> dict:
    state = {"state": "successful", "lastJobAt": now}
    if operation == "os_check":
        state = _os_state_for_result(contract, now, "lastCheckedAt")
    elif operation == "os_update":
        state = _os_state_for_result(contract, now, "lastUpdatedAt")
    elif operation == "docker_check":
        if project_name and project_mode:
            project_state = _docker_project_result_state(
                contract, project_name, project_mode, now)
            state.update({
                "state": project_state["state"],
                "lastCheckedAt": now,
                "updateCount": 1 if project_state["updatesAvailable"] is True else 0
                if project_state["updatesAvailable"] is False else None,
                "summary": project_state.get("summary"),
            })
            return state
        update = _docker_update_contract(
            (contract or {}).get("homelabhq_docker_update"))
        legacy_update = (_update_contract((contract or {}).get("homelabhq_update"))
                         if update is None else None)
        state["lastCheckedAt"] = now
        if update is None and legacy_update is None:
            state["state"] = "unknown"
            state["summary"] = "Playbook returned no structured Docker update result"
        elif legacy_update is not None:
            available = legacy_update.get("available")
            state["state"] = ("updates_available" if available is True else
                              "up_to_date" if available is False else "unknown")
            state["updateCount"] = legacy_update.get("count")
            state["summary"] = legacy_update.get("summary")
        else:
            assert update is not None
            if project_name:
                update["projects"] = [item for item in update["projects"]
                                      if item["name"] == project_name]
            values = [item.get("updatesAvailable") for item in update["projects"]]
            available = update.get("available")
            if available is None:
                available = True if True in values else (
                    False if values and all(value is False for value in values) else None)
            state["state"] = ("updates_available" if available is True else
                              "up_to_date" if available is False else "unknown")
            state["updateCount"] = sum(value is True for value in values) or None
            state["summary"] = update.get("summary")
            state["projects"] = update["projects"]
    elif operation == "docker_discovery":
        docker = _docker_contract((contract or {}).get("homelabhq_docker"))
        state["lastCheckedAt"] = now
        state["state"] = "successful" if docker is not None else "unknown"
        if docker is not None:
            state["sourceCheckedAt"] = datetime.fromtimestamp(
                now, timezone.utc).isoformat().replace("+00:00", "Z")
        if docker is None:
            state["summary"] = "Playbook returned no structured Docker discovery result"
    elif operation == "docker_project_update":
        state["lastUpdatedAt"] = now
        state["checkRecommended"] = True
    elif operation == "appliance_health":
        state.update(
            state="available", healthy=True, lastCheckedAt=now,
            summary="Authenticated appliance API health check succeeded")
    return state


def _missing_result_summary(operation: str, contract: dict | None,
                            parse_error: str | None = None,
                            project_name=None) -> str | None:
    """Explain when a successful playbook did not complete its read operation."""
    result = contract or {}
    if (operation in {"os_check", "os_update"} and
            _update_contract(result.get("homelabhq_update")) is None):
        if "homelabhq_update" in result:
            return "Playbook completed but OS update payload schema is invalid"
        if parse_error == "JSON in msg could not be decoded":
            return "Playbook completed but JSON in msg could not be decoded"
        return "Playbook completed but homelabhq_update key not found"
    if (operation == "docker_discovery" and
            _docker_contract(result.get("homelabhq_docker")) is None):
        if "homelabhq_docker" in result:
            return "Playbook completed but Docker discovery payload schema is invalid"
        if parse_error == "JSON in msg could not be decoded":
            return "Playbook completed but JSON in msg could not be decoded"
        return "Playbook completed but homelabhq_docker key not found"
    if operation == "docker_check":
        current = _docker_update_contract(result.get("homelabhq_docker_update"))
        legacy = _update_contract(result.get("homelabhq_update"))
        if current is None and legacy is None:
            if "homelabhq_docker_update" in result or "homelabhq_update" in result:
                return "Playbook completed but Docker update payload schema is invalid"
            if parse_error == "JSON in msg could not be decoded":
                return "Playbook completed but JSON in msg could not be decoded"
            return "Playbook completed but Docker update result key not found"
        if (current is not None and project_name and not any(
                item["name"] == project_name for item in current["projects"]) and
                current["projects"]):
            return "Playbook completed but the selected Docker project was not returned"
    return None


def _log_job_issue(job: dict, state: str, summary: str) -> None:
    instance = compute.get_instance(job["computeInstanceId"]) or {}
    name = instance.get("name") or job.get("ansibleTarget") or job["computeInstanceId"]
    operation = job["operation"].replace("_", " ")
    project = f' for project "{job["projectName"]}"' if job.get("projectName") else ""
    message = f'Compute maintenance {operation}{project} on "{name}" {state}: {summary}'
    logbuf.log_event(
        "warn" if state == "incomplete" else "error",
        "compute_maintenance_issue", source="compute", message=message,
        compute_instance_id=job["computeInstanceId"], job_id=job["id"],
        operation=job["operation"], project_name=job.get("projectName"),
        job_state=state, error=summary)


def _ansible_failure_summary(stdout: str, stderr: str, callback_data=None) -> str:
    """Return the most useful sanitized Ansible failure message available."""
    messages = []
    roots = [callback_data] if callback_data is not None else []
    roots.extend(_json_values(f"{stdout or ''}\n{stderr or ''}"))
    for root in roots:
        for mapping in _walk_mappings(root):
            message = mapping.get("msg")
            if (isinstance(message, str) and message.strip() and
                    _MARKER not in message):
                messages.append(message.strip())
    if messages:
        return f"Ansible playbook failed: {messages[-1][:450]}"
    for raw in reversed((stderr or "").splitlines() + (stdout or "").splitlines()):
        line = raw.strip()
        if line and ("FAILED!" in line or line.lower().startswith("error")):
            return f"Ansible playbook failed: {line[:450]}"
    return "Ansible playbook failed"


def _run_job(job_id: str) -> None:
    job = get_job(job_id)
    if not job:
        return
    instance_id = job["computeInstanceId"]
    started = int(time.time())
    _set_job(job_id, state="running", startedAt=started)
    state_key = {
        "os_check": "updateState",
        "os_update": "updateState",
        "docker_discovery": "dockerDiscoveryState",
        "docker_check": "dockerUpdateState",
        "docker_project_update": "dockerUpdateState",
        "appliance_health": "applianceHealthState",
    }[job["operation"]]
    project_operation = (job["operation"] in {"docker_check", "docker_project_update"}
                         and job.get("projectName"))
    running_changes = {
        "state": "checking" if job["operation"].endswith("check") or
        job["operation"] in {"docker_discovery", "appliance_health"} else "updating",
        "lastJobId": job_id, "lastJobAt": started,
    }
    if project_operation:
        _set_docker_project_state(job, running_changes)
    else:
        _set_maintenance_state(instance_id, state_key, running_changes)
    controller = ansible.get_controller(job["controllerId"])
    try:
        if not controller or not controller.get("enabled"):
            raise ValueError("Ansible controller is disabled or unavailable")
        argv, _ = ansible.playbook_command(
            controller, job["playbookOperation"], job["ansibleTarget"],
            variables=job.get("variables"))
        with ansible.controller_connection(controller) as connection:
            runner_result = ansible._run(
                connection, controller, argv,
                timeout=job.get("integrationTimeout") or
                controller.get("executionTimeout", 1800))
        code, stdout, stderr = runner_result[:3]
        callback_data = runner_result[3] if len(runner_result) > 3 else None
        if callback_data is not None:
            try:
                callback_data = json.loads(ansible._redact(
                    json.dumps(callback_data), controller))
            except (TypeError, json.JSONDecodeError):
                callback_data = None
        recap = parse_recap(stdout)
        parsed = parse_structured_result_details(stdout, callback_data)
        structured = parsed["result"]
        unreachable = sum(value["unreachable"] for value in recap.values())
        failed = sum(value["failed"] for value in recap.values())
        finished = int(time.time())
        state = ("unreachable" if unreachable else "failed"
                 if code or failed else "successful")
        summary = ("Ansible target was unreachable" if state == "unreachable" else
                   _ansible_failure_summary(stdout, stderr, callback_data)
                   if state == "failed" else
                   "Maintenance completed successfully")
        if state == "successful":
            missing_result = _missing_result_summary(
                job["operation"], structured, parsed["error"], job.get("projectName"))
            if missing_result:
                state = "incomplete"
                summary = missing_result
        _set_job(
            job_id, state=state, finishedAt=finished,
            durationSeconds=max(0, finished - started), exitStatus=code,
            recap=recap, structuredResult=structured, stdout=stdout, stderr=stderr,
            structuredResultSource=parsed["source"],
            structuredResultError=(
                summary.removeprefix("Playbook completed but ")
                if state == "incomplete" else parsed["error"]),
            summary=summary)
        if state in {"successful", "incomplete"}:
            updates = _state_for_result(
                job["operation"], structured, finished, job.get("projectName"),
                job.get("mode"))
            if state == "incomplete":
                updates["summary"] = summary
            updates.update(lastJobId=job_id, lastJobAt=finished)
            if job["operation"] == "docker_discovery":
                docker = _docker_contract((structured or {}).get("homelabhq_docker"))
                if docker is not None:
                    existing = store.load()["computeInstances"].get(instance_id) or {}
                    docker = compute.reconcile_docker_projects(
                        docker, controller, job["ansibleTarget"], existing.get("docker"))
                    aggregate = _aggregate_docker_project_state(docker.get("projects") or [])
                    previous_aggregate = existing.get("dockerUpdateState") or {}
                    for key in ("lastCheckedAt", "lastUpdatedAt"):
                        if key in previous_aggregate:
                            aggregate[key] = previous_aggregate[key]
                    _set_instance(
                        instance_id, docker=docker, dockerUpdateState=aggregate)
                _set_maintenance_state(instance_id, state_key, updates)
            elif job["operation"] == "docker_check" and job.get("projectName"):
                project_state = _docker_project_result_state(
                    structured, job["projectName"], job["mode"], finished)
                if state == "incomplete":
                    project_state.update(state="incomplete", summary=summary)
                project_state.update(lastJobId=job_id, lastJobAt=finished)
                _set_docker_project_state(job, project_state)
            elif job["operation"] == "docker_project_update" and job.get("projectName"):
                _set_docker_project_state(job, {
                    "state": "check_recommended", "updatesAvailable": None,
                    "summary": "Update completed; run a new project check",
                    "lastUpdatedAt": finished, "lastJobId": job_id,
                    "lastJobAt": finished,
                })
            else:
                _set_maintenance_state(instance_id, state_key, updates)
            if job["operation"] == "os_update" and state == "incomplete":
                _set_job(job_id, followUpRecommended="os_check")
        else:
            failed_changes = {
                "state": state, "lastJobId": job_id, "lastErrorSummary": summary,
                "summary": summary, "lastJobAt": finished,
            }
            if job["operation"] == "appliance_health":
                failed_changes["healthy"] = False
            if project_operation:
                _set_docker_project_state(job, failed_changes)
            else:
                _set_maintenance_state(instance_id, state_key, failed_changes)
        if state != "successful":
            _log_job_issue(job, state, summary)
    except Exception as error:
        finished = int(time.time())
        message = ansible.sanitized_error(error, controller)[:500]
        _set_job(job_id, state="failed", finishedAt=finished,
                 durationSeconds=max(0, finished - started), exitStatus=None,
                 recap={}, structuredResult=None, structuredResultSource=None,
                 structuredResultError=message, stdout="", stderr="", summary=message)
        failed_changes = {
            "state": "failed", "lastJobId": job_id, "lastErrorSummary": message,
            "summary": message, "lastJobAt": finished,
        }
        if job["operation"] == "appliance_health":
            failed_changes["healthy"] = False
        if project_operation:
            _set_docker_project_state(job, failed_changes)
        else:
            _set_maintenance_state(instance_id, state_key, failed_changes)
        _log_job_issue(job, "failed", message)


def _job_context(instance_id: str, operation: str, allow_reboot=False,
                 project_name=None) -> tuple[
                     dict, dict, str, dict | None, str, str | None, dict | None]:
    document = store.load()
    instance = document["computeInstances"].get(instance_id)
    if not instance:
        raise ValueError("compute instance not found")
    mapping = instance.get("ansible") or {}
    if not compute.managed_by_ansible(instance):
        raise ValueError("compute instance is not managed by Ansible")
    controller = document["ansibleControllers"].get(mapping.get("controllerId"))
    if not controller or not controller.get("enabled"):
        raise ValueError("Ansible controller is disabled or unavailable")
    target = str(mapping.get("inventoryHost") or "")
    if not target:
        raise ValueError("compute instance has no Ansible inventory host")
    ansible.require_operation_capability(controller, operation, target)
    maintenance = ansible.compute_maintenance_mapping(mapping)
    variables: dict | None = None
    mode = None
    project = None
    operation_fields = {
        "os_check": "osCheckOperation",
        "os_update": "osUpdateOperation",
        "docker_discovery": "dockerDiscoveryOperation",
        "docker_check": "dockerCheckOperation",
        "appliance_health": "applianceHealthOperation",
    }
    playbook_operation = maintenance.get(operation_fields.get(operation))
    if operation == "docker_discovery" and not maintenance.get("dockerDiscoveryEnabled"):
        raise ValueError("Docker discovery is disabled for this compute mapping")
    if operation != "docker_project_update" and not isinstance(playbook_operation, str):
        raise ValueError("this maintenance operation is disabled for the compute mapping")
    if operation == "os_update":
        metadata, _ = ansible.approved_playbook(controller, playbook_operation)
        if allow_reboot:
            if not metadata.get("supportsReboot") or not metadata.get("rebootVariable"):
                raise ValueError("the approved update playbook does not support optional reboot")
            variables = {metadata["rebootVariable"]: True}
        elif metadata.get("rebootVariable"):
            variables = {metadata["rebootVariable"]: False}
    elif operation == "docker_check":
        project = _docker_project(instance, project_name, allow_single=True)
        approved = _approved_project(controller, target, project["name"])
        mode = approved["updateMode"]
        metadata, _ = ansible.approved_playbook(controller, playbook_operation)
        variable = metadata.get("projectVariable")
        if variable != "docker_project":
            raise ValueError(
                "approved Docker check playbook must use the docker_project variable")
        variables = {variable: project["name"]}
    elif operation == "docker_project_update":
        project = _docker_project(instance, project_name)
        approved = _approved_project(controller, target, project["name"])
        mode = approved["updateMode"]
        if mode == "read_only":
            raise ValueError("Docker Compose project is configured as read-only")
        if maintenance.get("dockerUpdateOperation") != "docker_update":
            raise ValueError("Docker updates are disabled for the compute mapping")
        playbook_operation = ansible.docker_update_operation(controller, mode)
        metadata, _ = ansible.approved_playbook(controller, playbook_operation)
        variable = metadata.get("projectVariable")
        if variable != "docker_project":
            raise ValueError("approved Docker playbook must use the docker_project variable")
        variables = {variable: project["name"]}
        if playbook_operation == "docker_update" and metadata.get("modeVariable"):
            variables[metadata["modeVariable"]] = mode
    if not isinstance(playbook_operation, str):
        raise ValueError("this maintenance operation is disabled for the compute mapping")
    ansible.playbook_command(controller, playbook_operation, target, variables)
    return instance, controller, target, variables, playbook_operation, mode, project


def _new_job(instance_id: str, operation: str, requested_by: str, *,
             allow_reboot=False, project_name=None, project_id=None,
             integration_timeout=None) -> dict:
    if operation not in OPERATIONS:
        raise ValueError("unsupported maintenance operation")
    selector = project_name if project_name is not None else project_id
    instance, controller, target, variables, playbook_operation, mode, project = _job_context(
        instance_id, operation, allow_reboot=allow_reboot, project_name=selector)
    now = int(time.time())
    job_id = secrets.token_hex(12)
    job = {
        "id": job_id, "computeInstanceId": instance_id,
        "controllerId": controller["id"], "ansibleTarget": target,
        "operation": operation, "playbookOperation": playbook_operation,
        "playbook": controller["playbooks"][playbook_operation]["playbook"],
        "mode": mode,
        "requestedBy": requested_by, "allowReboot": bool(allow_reboot),
        "projectId": project.get("id") if project else None,
        "projectName": project.get("name") if project else None,
        "variables": variables,
        "state": "queued", "createdAt": now, "startedAt": None,
        "finishedAt": None, "durationSeconds": None, "exitStatus": None,
        "recap": {}, "structuredResult": None, "structuredResultSource": None,
        "structuredResultError": None, "stdout": "", "stderr": "",
        "summary": "Queued",
    }
    if integration_timeout is not None:
        job["integrationTimeout"] = max(1, min(300, int(integration_timeout)))
    return job


def _run_job_sequence(job_ids: list[str]) -> None:
    for job_id in job_ids:
        _run_job(job_id)


def _start_jobs(jobs: list[dict]) -> list[dict]:
    if not jobs:
        return []
    instance_id = jobs[0]["computeInstanceId"]
    if any(job["computeInstanceId"] != instance_id for job in jobs):
        raise ValueError("maintenance sequences must target one compute instance")

    with _LOCK:
        def mutate(document):
            if any(current.get("computeInstanceId") == instance_id and
                   current.get("state") in ACTIVE_STATES
                   for current in document["computeJobs"].values()):
                raise Conflict("a maintenance job is already active for this compute instance")
            for job in jobs:
                document["computeJobs"][job["id"]] = job
            excess = len(document["computeJobs"]) - MAX_JOBS
            if excess > 0:
                terminal = sorted(
                    (item for item in document["computeJobs"].values()
                     if item.get("state") in TERMINAL_STATES),
                    key=lambda item: (item.get("finishedAt") or item.get("createdAt", 0),
                                      item["id"]))
                for old in terminal[:excess]:
                    document["computeJobs"].pop(old["id"], None)
        store.update(mutate)
        job_ids = [job["id"] for job in jobs]
        thread = threading.Thread(
            target=_run_job_sequence, args=(job_ids,),
            name=f"compute-job-{job_ids[0]}", daemon=True)
        thread.start()
    return [_public_job(job) for job in jobs]


def start_job(instance_id: str, operation: str, requested_by: str, *,
              allow_reboot=False, project_name=None, project_id=None,
              integration_timeout=None) -> dict:
    job = _new_job(
        instance_id, operation, requested_by,
        allow_reboot=allow_reboot, project_name=project_name, project_id=project_id,
        integration_timeout=integration_timeout)
    return _start_jobs([job])[0]


def start_job_sequence(instance_id: str, operations, requested_by: str) -> list[dict]:
    """Queue approved read operations in order, with only one running at a time."""
    requested: list[tuple[str, str | None]] = []
    for item in operations:
        if isinstance(item, str):
            requested.append((item, None))
        elif isinstance(item, dict):
            operation = item.get("operation")
            project_name = item.get("projectName")
            if (not isinstance(operation, str) or
                    (project_name is not None and not isinstance(project_name, str))):
                raise ValueError("maintenance check sequence is invalid")
            requested.append((operation, project_name))
        else:
            raise ValueError("maintenance check sequence is invalid")
    if not requested or any(operation not in {
            "docker_discovery", "os_check", "docker_check", "appliance_health"}
            for operation, _project_name in requested):
        raise ValueError("maintenance check sequence is invalid")
    if len(requested) != len(set(requested)):
        raise ValueError("maintenance check sequence contains duplicate operations")
    jobs = [_new_job(instance_id, operation, requested_by, project_name=project_name)
            for operation, project_name in requested]
    return _start_jobs(jobs)


def set_project_strategy(instance_id: str, project_name: str, strategy: str) -> dict:
    mode = ansible.normalize_project_mode(strategy)

    def mutate(document):
        instance = document["computeInstances"].get(instance_id)
        if not instance:
            raise ValueError("compute instance not found")
        project = _docker_project(instance, project_name)
        mapping = instance.get("ansible") or {}
        controller = document["ansibleControllers"].get(mapping.get("controllerId")) or {}
        approved = _approved_project(
            controller, mapping.get("inventoryHost"), project["name"])
        if mode != approved["updateMode"]:
            raise ValueError("Docker update mode is managed by Ansible inventory")
        project["managed"] = mode != "read_only"
        project["updateMode"] = mode
        project["updateStrategy"] = {
            "build": "local_build", "read_only": "unmanaged",
        }.get(mode, mode)
        project["approved"] = True
        return copy.deepcopy(project)

    return store.update(mutate)


def recover_interrupted_jobs() -> int:
    """Fail persisted active jobs after restart; no remote process is resumed."""
    now = int(time.time())

    def mutate(document):
        recovered = 0
        for job in document["computeJobs"].values():
            if job.get("state") not in ACTIVE_STATES:
                continue
            job.update(state="failed", finishedAt=now,
                       durationSeconds=max(0, now - (job.get("startedAt") or now)),
                       summary="HomelabHQ restarted before the job result was collected")
            recovered += 1
        return recovered
    return store.update(mutate)

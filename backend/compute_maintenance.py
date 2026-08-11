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

import ansible_integration as ansible
import compute
import store
from domain import safe_error
from errors import Conflict


ACTIVE_STATES = frozenset({"queued", "running"})
TERMINAL_STATES = frozenset({
    "successful", "incomplete", "failed", "unreachable", "cancelled",
})
OPERATIONS = frozenset({
    "os_check", "os_update", "docker_check", "docker_discovery",
    "docker_project_update",
})
MAX_JOBS = max(10, int(os.environ.get("HLHQ_MAX_COMPUTE_JOBS", "500")))
_RECAP_RE = re.compile(
    r"^(.+?)\s+:\s+ok=(\d+)\s+changed=(\d+)\s+unreachable=(\d+)\s+"
    r"failed=(\d+)\s+skipped=(\d+)\s+rescued=(\d+)\s+ignored=(\d+)\s*$")
_MARKER = "HOMELABHQ_RESULT:"
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


def _container_contract(raw_container) -> dict | None:
    if not isinstance(raw_container, dict):
        return None
    name = str(raw_container.get("name") or raw_container.get("Names") or "").strip()
    if not name:
        return None
    state = str(raw_container.get("state", raw_container.get("State")) or "unknown").lower()[:50]
    if state not in {
            "created", "running", "stopped", "restarting", "removing", "paused",
            "exited", "dead", "unknown"}:
        state = "unknown"
    health = raw_container.get("health", raw_container.get("HealthStatus"))
    health = str(health or "").strip().lower()[:50]
    if health in {"", "none", "null", "no healthcheck", "no_healthcheck"}:
        health = "no_healthcheck"
    elif health not in {"healthy", "unhealthy", "starting", "unknown"}:
        health = "unknown"
    labels, labels_raw = _labels_contract(
        raw_container.get("labels", raw_container.get("Labels")))
    networks = _text_list(
        raw_container.get("networks", raw_container.get("Networks")), limit=300)
    status = str(raw_container.get("status", raw_container.get("Status")) or "").strip()
    ports = str(raw_container.get("ports", raw_container.get("Ports")) or "").strip()
    return {
        "name": name[:200],
        "state": state,
        "health": health,
        "image": str(raw_container.get("image", raw_container.get("Image")) or "")[:500] or None,
        "status": status[:500] or None,
        "labels": labels,
        "labelsRaw": labels_raw,
        "networks": networks,
        "ports": ports[:2000] or None,
        "composeProject": labels.get("com.docker.compose.project") or None,
        "composeService": labels.get("com.docker.compose.service") or None,
    }


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
    available = value.get("available")
    if not isinstance(available, bool):
        available = None
    projects = []
    for raw in value.get("projects") or []:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()[:200]
        if not name:
            continue
        mode = raw.get("update_mode", raw.get("updateMode"))
        try:
            mode = ansible.normalize_project_mode(mode)
            mode = None if mode == "read_only" else mode
        except ValueError:
            mode = None
        updates = raw.get("updates_available", raw.get("updatesAvailable"))
        projects.append({
            "name": name,
            "updatesAvailable": updates if isinstance(updates, bool) else None,
            "updateMode": mode,
            "summary": str(raw.get("summary") or "")[:500] or None,
        })
    return {
        "available": available,
        "projects": projects,
        "summary": str(value.get("summary") or "")[:500] or None,
        "lastCheckedAt": int(time.time()),
    }


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
        state = {key: previous[key] for key in ("lastCheckedAt", "lastUpdatedAt")
                 if key in previous}
        state.update(changes)
        instance[state_key] = state
    store.update(mutate)


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


def _state_for_result(operation: str, contract: dict | None, now: int) -> dict:
    state = {"state": "successful", "lastJobAt": now}
    if operation == "os_check":
        state = _os_state_for_result(contract, now, "lastCheckedAt")
    elif operation == "os_update":
        state = _os_state_for_result(contract, now, "lastUpdatedAt")
    elif operation == "docker_check":
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
        if docker is None:
            state["summary"] = "Playbook returned no structured Docker discovery result"
    elif operation == "docker_project_update":
        state["lastUpdatedAt"] = now
        state["checkRecommended"] = True
    return state


def _missing_result_summary(operation: str, contract: dict | None,
                            parse_error: str | None = None) -> str | None:
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
    return None


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
    }[job["operation"]]
    _set_maintenance_state(instance_id, state_key, {
        "state": "checking" if job["operation"].endswith("check") or
        job["operation"] == "docker_discovery" else "updating",
        "lastJobId": job_id,
    })
    controller = ansible.get_controller(job["controllerId"])
    try:
        if not controller or not controller.get("enabled"):
            raise ValueError("Ansible controller is disabled or unavailable")
        argv, _ = ansible.playbook_command(
            controller, job["playbookOperation"], job["ansibleTarget"],
            variables=job.get("variables"))
        with ansible.controller_connection(controller) as connection:
            runner_result = ansible._run(connection, controller, argv)
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
                   "Ansible playbook failed" if state == "failed" else
                   "Maintenance completed successfully")
        if state == "successful":
            missing_result = _missing_result_summary(
                job["operation"], structured, parsed["error"])
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
            updates = _state_for_result(job["operation"], structured, finished)
            if state == "incomplete":
                updates["summary"] = summary
            updates["lastJobId"] = job_id
            instance_changes = {state_key: updates}
            docker = (_docker_contract((structured or {}).get("homelabhq_docker"))
                      if job["operation"] == "docker_discovery" else None)
            if docker is not None:
                # Preserve administrator-selected strategies when a later
                # discovery omits them or calls a project read-only.
                existing = store.load()["computeInstances"].get(instance_id) or {}
                old = {project["id"]: project for project in
                       (existing.get("docker") or {}).get("projects") or []}
                for project in docker["projects"]:
                    previous = old.get(project["id"])
                    if previous and (previous.get("strategyConfigured") or
                                     previous.get("managementConfigured")):
                        mode = previous.get("updateMode", previous.get("updateStrategy"))
                        try:
                            mode = ansible.normalize_project_mode(mode)
                        except ValueError:
                            mode = "read_only"
                        project["managed"] = bool(previous.get(
                            "managed", mode != "read_only"))
                        project["updateMode"] = mode
                        project["updateStrategy"] = {
                            "build": "local_build", "read_only": "unmanaged",
                        }.get(mode, mode)
                        project["strategyConfigured"] = True
                        project["managementConfigured"] = True
                instance_changes["docker"] = docker
            docker_update = (_docker_update_contract(
                (structured or {}).get("homelabhq_docker_update"))
                if job["operation"] in {"docker_check", "docker_project_update"}
                else None)
            if docker_update is not None:
                existing = store.load()["computeInstances"].get(instance_id) or {}
                discovered = copy.deepcopy(existing.get("docker") or {})
                if job["operation"] == "docker_check":
                    for project in discovered.get("projects") or []:
                        project.pop("updateState", None)
                states_by_name = {item["name"]: item for item in docker_update["projects"]}
                for project in discovered.get("projects") or []:
                    if project.get("name") in states_by_name:
                        project["updateState"] = states_by_name[project["name"]]
                if discovered:
                    instance_changes["docker"] = discovered
            elif job["operation"] == "docker_check":
                existing = store.load()["computeInstances"].get(instance_id) or {}
                discovered = copy.deepcopy(existing.get("docker") or {})
                for project in discovered.get("projects") or []:
                    project.pop("updateState", None)
                if discovered:
                    instance_changes["docker"] = discovered
            state_changes = instance_changes.pop(state_key)
            if instance_changes:
                _set_instance(instance_id, **instance_changes)
            _set_maintenance_state(instance_id, state_key, state_changes)
            if job["operation"] == "os_update" and state == "incomplete":
                _set_job(job_id, followUpRecommended="os_check")
        else:
            _set_maintenance_state(instance_id, state_key, {
                "state": state, "lastJobId": job_id, "lastErrorSummary": summary,
                "lastJobAt": finished,
            })
    except Exception as error:
        finished = int(time.time())
        message = ansible.sanitized_error(error, controller)[:500]
        _set_job(job_id, state="failed", finishedAt=finished,
                 durationSeconds=max(0, finished - started), exitStatus=None,
                 recap={}, structuredResult=None, structuredResultSource=None,
                 structuredResultError=message, stdout="", stderr="", summary=message)
        _set_maintenance_state(instance_id, state_key, {
            "state": "failed", "lastJobId": job_id, "lastErrorSummary": message,
            "lastJobAt": finished,
        })


def _job_context(instance_id: str, operation: str, allow_reboot=False,
                 project_id=None) -> tuple[dict, dict, str, dict | None, str, str | None]:
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
    maintenance = ansible.compute_maintenance_mapping(mapping)
    variables: dict | None = None
    mode = None
    operation_fields = {
        "os_check": "osCheckOperation",
        "os_update": "osUpdateOperation",
        "docker_discovery": "dockerDiscoveryOperation",
        "docker_check": "dockerCheckOperation",
    }
    playbook_operation = maintenance.get(operation_fields.get(operation))
    if operation == "docker_discovery" and not maintenance.get("dockerDiscoveryEnabled"):
        raise ValueError("Docker discovery is disabled for this compute mapping")
    if operation != "docker_project_update" and not playbook_operation:
        raise ValueError("this maintenance operation is disabled for the compute mapping")
    if operation == "os_update":
        metadata, _ = ansible.approved_playbook(controller, playbook_operation)
        if allow_reboot:
            if not metadata.get("supportsReboot") or not metadata.get("rebootVariable"):
                raise ValueError("the approved update playbook does not support optional reboot")
            variables = {metadata["rebootVariable"]: True}
        elif metadata.get("rebootVariable"):
            variables = {metadata["rebootVariable"]: False}
    elif operation == "docker_project_update":
        project = next((item for item in (instance.get("docker") or {}).get("projects") or []
                        if item.get("id") == project_id), None)
        if not project:
            raise ValueError("Docker Compose project was not discovered")
        mode = ansible.normalize_project_mode(
            project.get("updateMode", project.get("updateStrategy", "read_only")))
        managed = bool(project.get("managed", project.get("strategyConfigured") and
                                   mode != "read_only"))
        if not managed or mode == "read_only":
            raise ValueError("Docker Compose project is configured as read-only")
        if maintenance.get("dockerUpdateOperation") != "docker_update":
            raise ValueError("Docker updates are disabled for the compute mapping")
        playbook_operation = ansible.docker_update_operation(controller, mode)
        metadata, _ = ansible.approved_playbook(controller, playbook_operation)
        variable = metadata.get("projectVariable")
        if not variable:
            raise ValueError("approved Docker playbook has no project variable metadata")
        project_target = ((project.get("configFiles") or [None])[0] or
                          project.get("path") or project.get("name"))
        variables = {variable: project_target}
        if playbook_operation == "docker_update" and metadata.get("modeVariable"):
            variables[metadata["modeVariable"]] = mode
    if not isinstance(playbook_operation, str):
        raise ValueError("this maintenance operation is disabled for the compute mapping")
    ansible.playbook_command(controller, playbook_operation, target, variables)
    return instance, controller, target, variables, playbook_operation, mode


def _new_job(instance_id: str, operation: str, requested_by: str, *,
             allow_reboot=False, project_id=None) -> dict:
    if operation not in OPERATIONS:
        raise ValueError("unsupported maintenance operation")
    instance, controller, target, variables, playbook_operation, mode = _job_context(
        instance_id, operation, allow_reboot=allow_reboot, project_id=project_id)
    now = int(time.time())
    job_id = secrets.token_hex(12)
    job = {
        "id": job_id, "computeInstanceId": instance_id,
        "controllerId": controller["id"], "ansibleTarget": target,
        "operation": operation, "playbookOperation": playbook_operation,
        "playbook": controller["playbooks"][playbook_operation]["playbook"],
        "mode": mode,
        "requestedBy": requested_by, "allowReboot": bool(allow_reboot),
        "projectId": project_id, "variables": variables,
        "state": "queued", "createdAt": now, "startedAt": None,
        "finishedAt": None, "durationSeconds": None, "exitStatus": None,
        "recap": {}, "structuredResult": None, "structuredResultSource": None,
        "structuredResultError": None, "stdout": "", "stderr": "",
        "summary": "Queued",
    }
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
              allow_reboot=False, project_id=None) -> dict:
    job = _new_job(
        instance_id, operation, requested_by,
        allow_reboot=allow_reboot, project_id=project_id)
    return _start_jobs([job])[0]


def start_job_sequence(instance_id: str, operations, requested_by: str) -> list[dict]:
    """Queue approved read operations in order, with only one running at a time."""
    requested = list(operations)
    if not requested or any(operation not in {
            "docker_discovery", "os_check", "docker_check"}
            for operation in requested):
        raise ValueError("maintenance check sequence is invalid")
    if len(requested) != len(set(requested)):
        raise ValueError("maintenance check sequence contains duplicate operations")
    jobs = [_new_job(instance_id, operation, requested_by)
            for operation in requested]
    return _start_jobs(jobs)


def set_project_strategy(instance_id: str, project_id: str, strategy: str) -> dict:
    mode = ansible.normalize_project_mode(strategy)

    def mutate(document):
        instance = document["computeInstances"].get(instance_id)
        if not instance:
            raise ValueError("compute instance not found")
        project = next((item for item in (instance.get("docker") or {}).get("projects") or []
                        if item.get("id") == project_id), None)
        if not project:
            raise ValueError("Docker Compose project was not discovered")
        project["managed"] = mode != "read_only"
        project["updateMode"] = mode
        project["updateStrategy"] = {
            "build": "local_build", "read_only": "unmanaged",
        }.get(mode, mode)
        project["strategyConfigured"] = True
        project["managementConfigured"] = True
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

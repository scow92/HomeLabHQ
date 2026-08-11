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
TERMINAL_STATES = frozenset({"successful", "failed", "unreachable", "cancelled"})
MAX_JOBS = max(10, int(os.environ.get("HLHQ_MAX_COMPUTE_JOBS", "500")))
_RECAP_RE = re.compile(
    r"^(.+?)\s+:\s+ok=(\d+)\s+changed=(\d+)\s+unreachable=(\d+)\s+"
    r"failed=(\d+)\s+skipped=(\d+)\s+rescued=(\d+)\s+ignored=(\d+)\s*$")
_MARKER = "HOMELABHQ_RESULT:"
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


def parse_structured_result(output: str) -> dict | None:
    """Read the explicit one-line HomeLabHQ contract; ignore human output."""
    found = None
    for line in (output or "").splitlines():
        if _MARKER not in line:
            continue
        candidate = _extract_json_after_marker(line)
        if isinstance(candidate, dict) and (
                "homelabhq_update" in candidate or "homelabhq_docker" in candidate):
            found = candidate
    return found


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


def _project_id(name: str, path: str) -> str:
    return hashlib.sha256(f"{name}\0{path}".encode()).hexdigest()[:16]


def _docker_contract(value) -> dict | None:
    if not isinstance(value, dict):
        return None
    exists = value.get("available", value.get("exists"))
    if not isinstance(exists, bool):
        exists = None
    projects = []
    for raw_project in value.get("projects") or []:
        if not isinstance(raw_project, dict):
            continue
        name = str(raw_project.get("name") or "").strip()[:200]
        path = str(raw_project.get("path") or "").strip()[:1000]
        if not name:
            continue
        containers = []
        for raw_container in raw_project.get("containers") or []:
            if not isinstance(raw_container, dict) or not raw_container.get("name"):
                continue
            containers.append({
                "name": str(raw_container["name"])[:200],
                "state": str(raw_container.get("state") or "unknown")[:50],
                "health": (str(raw_container.get("health"))[:50]
                           if raw_container.get("health") is not None else None),
                "image": (str(raw_container.get("image"))[:500]
                          if raw_container.get("image") is not None else None),
            })
        strategy = raw_project.get("update_strategy", raw_project.get("updateStrategy"))
        if strategy not in ansible.UPDATE_STRATEGIES:
            strategy = "unmanaged"
        projects.append({
            "id": _project_id(name, path), "name": name, "path": path or None,
            "updateStrategy": strategy, "containers": containers,
        })
    return {
        "available": exists,
        "version": str(value.get("version") or "")[:200] or None,
        "composeAvailable": (value.get("compose_available")
                             if isinstance(value.get("compose_available"), bool) else None),
        "composeVersion": str(value.get("compose_version") or "")[:200] or None,
        "projects": projects,
        "lastDiscoveredAt": int(time.time()),
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


def _state_for_result(operation: str, contract: dict | None, now: int) -> dict:
    update = _update_contract((contract or {}).get("homelabhq_update"))
    state = {"state": "successful", "lastJobAt": now}
    if operation in ("os_check", "docker_check"):
        state["lastCheckedAt"] = now
        if update is None:
            state["state"] = "unknown"
        elif update.get("rebootRequired"):
            state["state"] = "reboot_required"
        elif update.get("available"):
            state["state"] = "updates_available"
        elif update.get("available") is False:
            state["state"] = "up_to_date"
    elif operation in ("os_update", "docker_project_update"):
        state["lastUpdatedAt"] = now
        if update and update.get("rebootRequired"):
            state["state"] = "reboot_required"
    if update:
        state.update({"updateCount": update.get("count"),
                      "rebootRequired": update.get("rebootRequired"),
                      "summary": update.get("summary")})
    return state


def _run_job(job_id: str) -> None:
    job = get_job(job_id)
    if not job:
        return
    instance_id = job["computeInstanceId"]
    started = int(time.time())
    _set_job(job_id, state="running", startedAt=started)
    state_key = ("updateState" if job["operation"].startswith("os_")
                 else "dockerUpdateState")
    _set_instance(instance_id, **{state_key: {
        "state": "checking" if job["operation"].endswith("check") or
        job["operation"] == "docker_discovery" else "updating",
        "lastJobId": job_id,
    }})
    controller = ansible.get_controller(job["controllerId"])
    try:
        if not controller or not controller.get("enabled"):
            raise ValueError("Ansible controller is disabled or unavailable")
        argv, _ = ansible.playbook_command(
            controller, job["playbookOperation"], job["ansibleTarget"],
            variables=job.get("variables"))
        with ansible.controller_connection(controller) as connection:
            code, stdout, stderr = ansible._run(connection, controller, argv)
        recap = parse_recap(stdout)
        structured = parse_structured_result(stdout)
        unreachable = sum(value["unreachable"] for value in recap.values())
        failed = sum(value["failed"] for value in recap.values())
        finished = int(time.time())
        state = ("unreachable" if unreachable else "failed"
                 if code or failed else "successful")
        summary = ("Ansible target was unreachable" if state == "unreachable" else
                   "Ansible playbook failed" if state == "failed" else
                   "Maintenance completed successfully")
        _set_job(
            job_id, state=state, finishedAt=finished,
            durationSeconds=max(0, finished - started), exitStatus=code,
            recap=recap, structuredResult=structured, stdout=stdout, stderr=stderr,
            summary=summary)
        if state == "successful":
            updates = _state_for_result(job["operation"], structured, finished)
            updates["lastJobId"] = job_id
            instance_changes = {state_key: updates}
            docker = _docker_contract((structured or {}).get("homelabhq_docker"))
            if docker is not None:
                # Preserve administrator-selected strategies when a later
                # discovery omits them or calls a project read-only.
                existing = store.load()["computeInstances"].get(instance_id) or {}
                old = {project["id"]: project for project in
                       (existing.get("docker") or {}).get("projects") or []}
                for project in docker["projects"]:
                    previous = old.get(project["id"])
                    if previous and previous.get("strategyConfigured"):
                        project["updateStrategy"] = previous.get("updateStrategy", "unmanaged")
                        project["strategyConfigured"] = True
                instance_changes["docker"] = docker
            _set_instance(instance_id, **instance_changes)
        else:
            _set_instance(instance_id, **{state_key: {
                "state": state, "lastJobId": job_id, "lastErrorSummary": summary,
                "lastJobAt": finished,
            }})
    except Exception as error:
        finished = int(time.time())
        message = ansible.sanitized_error(error, controller)[:500]
        _set_job(job_id, state="failed", finishedAt=finished,
                 durationSeconds=max(0, finished - started), exitStatus=None,
                 recap={}, structuredResult=None, stdout="", stderr="", summary=message)
        _set_instance(instance_id, **{state_key: {
            "state": "failed", "lastJobId": job_id, "lastErrorSummary": message,
            "lastJobAt": finished,
        }})


def _job_context(instance_id: str, operation: str, allow_reboot=False,
                 project_id=None) -> tuple[dict, dict, str, dict | None, str]:
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
    target = mapping.get("inventoryHost")
    variables = None
    playbook_operation = operation
    if operation == "os_update":
        metadata, _ = ansible.approved_playbook(controller, operation)
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
        strategy = project.get("updateStrategy") or "unmanaged"
        if strategy == "unmanaged":
            raise ValueError("Docker Compose project is configured as read-only")
        playbook_operation = ("docker_update_pull" if strategy == "pull"
                              else "docker_update_local_build")
        metadata, _ = ansible.approved_playbook(controller, playbook_operation)
        variable = metadata.get("projectVariable")
        if not variable:
            raise ValueError("approved Docker playbook has no project variable metadata")
        variables = {variable: project.get("path") or project.get("name")}
    ansible.playbook_command(controller, playbook_operation, target, variables)
    return instance, controller, target, variables, playbook_operation


def start_job(instance_id: str, operation: str, requested_by: str, *,
              allow_reboot=False, project_id=None) -> dict:
    if operation not in {"os_check", "os_update", "docker_check", "docker_discovery",
                         "docker_project_update"}:
        raise ValueError("unsupported maintenance operation")
    instance, controller, target, variables, playbook_operation = _job_context(
        instance_id, operation, allow_reboot=allow_reboot, project_id=project_id)
    now = int(time.time())
    job_id = secrets.token_hex(12)
    job = {
        "id": job_id, "computeInstanceId": instance_id,
        "controllerId": controller["id"], "ansibleTarget": target,
        "operation": operation, "playbookOperation": playbook_operation,
        "playbook": controller["playbooks"][playbook_operation]["playbook"],
        "requestedBy": requested_by, "allowReboot": bool(allow_reboot),
        "projectId": project_id, "variables": variables,
        "state": "queued", "createdAt": now, "startedAt": None,
        "finishedAt": None, "durationSeconds": None, "exitStatus": None,
        "recap": {}, "structuredResult": None, "stdout": "", "stderr": "",
        "summary": "Queued",
    }

    with _LOCK:
        def mutate(document):
            if any(current.get("computeInstanceId") == instance_id and
                   current.get("state") in ACTIVE_STATES
                   for current in document["computeJobs"].values()):
                raise Conflict("a maintenance job is already active for this compute instance")
            document["computeJobs"][job_id] = job
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
        thread = threading.Thread(target=_run_job, args=(job_id,),
                                  name=f"compute-job-{job_id}", daemon=True)
        thread.start()
    return _public_job(job)


def set_project_strategy(instance_id: str, project_id: str, strategy: str) -> dict:
    if strategy not in ansible.UPDATE_STRATEGIES:
        raise ValueError("Docker update strategy is invalid")

    def mutate(document):
        instance = document["computeInstances"].get(instance_id)
        if not instance:
            raise ValueError("compute instance not found")
        project = next((item for item in (instance.get("docker") or {}).get("projects") or []
                        if item.get("id") == project_id), None)
        if not project:
            raise ValueError("Docker Compose project was not discovered")
        project["updateStrategy"] = strategy
        project["strategyConfigured"] = True
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

import copy
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

import ansible_integration as ansible
import compute
import compute_maintenance as maintenance
import services
import store
from backend.api import compute_routes
from context import Actor, Role
from errors import Conflict, Forbidden, NotFound


@pytest.fixture(autouse=True)
def isolated_store(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(store, "DB_FILE", str(tmp_path / "homelabhq.json"))
    monkeypatch.setattr(store, "LOCK_FILE", str(tmp_path / "homelabhq.lock"))
    store._cache.update(doc=None, mtime=None)


def controller_record(*, playbooks=None):
    discovered = [
        "linux-health.yml", "linux-update.yml", "docker-health.yml",
        "docker-discover.yml", "docker-maintain.yml", "docker-pull.yml",
        "docker-build.yml",
    ]
    return {
        "id": "primary", "enabled": True,
        "inventoryPath": "/srv/automation/inventory.yml",
        "playbooksDirectory": "/srv/automation/playbooks",
        "ansiblePlaybookExecutable": "/usr/bin/ansible-playbook",
        "inventory": {
            "hosts": {
                "workload-a": {"name": "workload-a", "address": "192.0.2.20",
                               "groups": ["compute"]},
                "other": {"name": "other", "address": "192.0.2.21", "groups": []},
            },
            "groups": {"compute": {"name": "compute", "hosts": ["workload-a"]}},
        },
        "discoveredPlaybooks": discovered,
        "playbooks": copy.deepcopy(playbooks or {}),
    }


def seed(*, playbooks=None, mapped=True, project=None):
    instance = {
        "id": "compute-a", "ownerId": "owner-a", "provider": "proxmox",
        "providerInstanceId": "101", "type": "lxc", "name": "not-the-hostname",
        "status": "running", "updateState": {"state": "unknown"},
    }
    if mapped:
        instance["ansible"] = {
            "enabled": True, "controllerId": "primary", "inventoryHost": "workload-a",
            "maintenance": copy.deepcopy(ansible.DEFAULT_COMPUTE_MAINTENANCE),
            "confirmedAt": 1,
        }
    if project:
        instance["docker"] = {"available": True, "projects": [project]}

    def mutate(document):
        document["ansibleControllers"]["primary"] = controller_record(playbooks=playbooks)
        document["computeInstances"]["compute-a"] = instance

    store.update(mutate)


def approval(operation, playbook, **metadata):
    payload = {"operation": operation, "playbook": playbook, "approved": True,
               "label": metadata.pop("label", operation.replace("_", " ").title()),
               **metadata}
    return ansible.approve_playbook("primary", payload)[operation]


class NoStartThread:
    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        pass


class ImmediateThread(NoStartThread):
    def __init__(self, target, args, **kwargs):
        self.target = target
        self.args = args

    def start(self):
        self.target(*self.args)


class NullConnection:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def run_contract_job(monkeypatch, operation, contract, *, code=0, project_id=None,
                     project_name=None):
    output = f"HOMELABHQ_RESULT: {json.dumps(contract)}\n" \
        "workload-a : ok=2 changed=1 unreachable=0 failed=0 skipped=0 rescued=0 ignored=0"
    monkeypatch.setattr(maintenance.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(maintenance.ansible, "controller_connection", lambda *_: NullConnection())
    monkeypatch.setattr(maintenance.ansible, "_run", lambda *_args, **_kwargs: (
        code, output, ""))
    job = maintenance.start_job(
        "compute-a", operation, "admin-a", project_id=project_id,
        project_name=project_name)
    return maintenance.get_job(job["id"]), store.load()["computeInstances"]["compute-a"]


def test_all_canonical_approvals_store_generic_restrictions():
    seed()
    configured = {
        "os_check": approval("os_check", "linux-health.yml", checkModeSupported=True,
                             allowedGroups=["compute"]),
        "os_update": approval("os_update", "linux-update.yml", supportsReboot=True,
                              rebootVariable="allow_reboot", allowedTargets=["workload-a"]),
        "docker_discovery": approval("docker_discovery", "docker-discover.yml"),
        "docker_check": approval(
            "docker_check", "docker-health.yml", projectVariable="docker_project"),
        "docker_update": approval(
            "docker_update", "docker-maintain.yml", projectVariable="docker_project",
            modeVariable="update_mode", supportedModes=["pull", "build"],
            allowedExtraVariables=["maintenance_window"]),
    }

    assert set(configured) == ansible.CANONICAL_OPERATIONS
    assert configured["os_check"]["operationType"] == "os_check"
    assert configured["os_check"]["checkModeSupported"] is True
    assert configured["os_update"]["rebootVariable"] == "allow_reboot"
    assert configured["docker_update"]["supportedModes"] == ["pull", "build"]


def test_generic_docker_update_playbook_executes_both_validated_modes(monkeypatch):
    approvals = {
        "docker_update": {
            "playbook": "docker-maintain.yml", "approved": True,
            "projectVariable": "docker_project", "modeVariable": "update_mode",
            "supportedModes": ["pull", "build"],
        },
    }
    project = {
        "id": "project-a", "name": "example", "path": "/srv/stacks/example/compose.yml",
        "configFiles": ["/srv/stacks/example/compose.yml"], "managed": False,
        "updateMode": "read_only", "updateStrategy": "unmanaged", "containers": [],
    }
    seed(playbooks=approvals, project=project)
    monkeypatch.setattr(maintenance.threading, "Thread", NoStartThread)

    jobs = []
    for mode in ("pull", "build"):
        maintenance.set_project_strategy("compute-a", "project-a", mode)
        jobs.append(maintenance.start_job(
            "compute-a", "docker_project_update", "admin-a", project_id="project-a"))
        store.update(lambda document: document["computeJobs"][jobs[-1]["id"]].update(
            state="successful"))

    assert [job["playbookOperation"] for job in jobs] == ["docker_update", "docker_update"]
    assert [job["mode"] for job in jobs] == ["pull", "build"]
    assert jobs[0]["variables"] == {
        "docker_project": "example", "update_mode": "pull"}
    assert jobs[1]["variables"] == {"docker_project": "example", "update_mode": "build"}


def test_docker_check_and_update_supply_selected_inventory_project_name(monkeypatch):
    project = {
        "id": "project-a", "name": "frigate", "path": "/opt/frigate",
        "configFiles": ["/opt/frigate/compose.yml"], "managed": True,
        "updateMode": "pull", "containers": [],
    }
    seed(playbooks={
        "docker_check": {
            "playbook": "docker-health.yml", "approved": True,
            "projectVariable": "docker_project",
        },
        "docker_update": {
            "playbook": "docker-maintain.yml", "approved": True,
            "projectVariable": "docker_project", "supportedModes": ["pull"],
        },
    }, project=project)
    monkeypatch.setattr(maintenance.threading, "Thread", NoStartThread)

    check = maintenance.start_job(
        "compute-a", "docker_check", "owner-a", project_name="frigate")
    store.update(lambda document: document["computeJobs"][check["id"]].update(
        state="successful"))
    update = maintenance.start_job(
        "compute-a", "docker_project_update", "admin-a", project_name="frigate")

    assert check["projectName"] == "frigate"
    assert check["variables"] == {"docker_project": "frigate"}
    assert update["projectName"] == "frigate"
    assert update["variables"] == {"docker_project": "frigate"}


def test_multiple_docker_projects_require_selection_and_remain_independent(monkeypatch):
    projects = [
        {"id": "project-a", "name": "frigate", "managed": True,
         "updateMode": "pull", "configFiles": ["/opt/frigate/compose.yml"]},
        {"id": "project-b", "name": "immich", "managed": True,
         "updateMode": "pull", "configFiles": ["/opt/immich/compose.yml"]},
    ]
    seed(playbooks={
        "docker_check": {
            "playbook": "docker-health.yml", "approved": True,
            "projectVariable": "docker_project",
        },
    }, project=projects[0])
    store.update(lambda document: document["computeInstances"]["compute-a"]["docker"].update(
        projects=projects))
    monkeypatch.setattr(maintenance.threading, "Thread", NoStartThread)

    with pytest.raises(ValueError, match="must be selected"):
        maintenance.start_job("compute-a", "docker_check", "owner-a")
    with pytest.raises(ValueError, match="was not discovered"):
        maintenance.start_job(
            "compute-a", "docker_check", "owner-a", project_name="/opt/frigate")

    jobs = []
    for name in ("frigate", "immich"):
        job = maintenance.start_job(
            "compute-a", "docker_check", "owner-a", project_name=name)
        jobs.append(job)
        store.update(lambda document, job_id=job["id"]:
                     document["computeJobs"][job_id].update(state="successful"))

    assert [job["projectName"] for job in jobs] == ["frigate", "immich"]
    assert [job["variables"] for job in jobs] == [
        {"docker_project": "frigate"}, {"docker_project": "immich"}]


def test_project_check_updates_only_selected_project_state(monkeypatch):
    projects = [
        {"id": "project-a", "name": "frigate", "managed": True,
         "updateMode": "pull", "configFiles": ["/opt/frigate/compose.yml"]},
        {"id": "project-b", "name": "immich", "managed": True,
         "updateMode": "pull", "configFiles": ["/opt/immich/compose.yml"],
         "updateState": {"updatesAvailable": True, "summary": "Immich update"}},
    ]
    seed(playbooks={
        "docker_check": {
            "playbook": "docker-health.yml", "approved": True,
            "projectVariable": "docker_project",
        },
    }, project=projects[0])
    store.update(lambda document: document["computeInstances"]["compute-a"]["docker"].update(
        projects=projects))

    _job, instance = run_contract_job(monkeypatch, "docker_check", {
        "homelabhq_docker_update": {
            "available": False,
            "projects": [{"name": "frigate", "updates_available": False,
                          "summary": "Frigate is current"}],
        },
    }, project_name="frigate")

    saved = {project["name"]: project for project in instance["docker"]["projects"]}
    assert saved["frigate"]["updateState"]["updatesAvailable"] is False
    assert saved["immich"]["updateState"] == {
        "updatesAvailable": True, "summary": "Immich update"}
    assert instance["dockerUpdateState"]["state"] == "updates_available"
    assert instance["dockerUpdateState"]["updateCount"] == 1


@pytest.mark.parametrize("project_variable", [None, "compose_config"])
def test_docker_approvals_require_canonical_project_name_variable(project_variable):
    seed()
    payload = {}
    if project_variable:
        payload["projectVariable"] = project_variable

    with pytest.raises(ValueError, match="requires docker_project"):
        approval("docker_check", "docker-health.yml", **payload)


def test_docker_check_route_passes_selected_inventory_project_name(monkeypatch):
    calls = []
    monkeypatch.setattr(
        services, "compute_docker_check",
        lambda actor, instance_id, project_name: calls.append(
            (actor.user_id, instance_id, project_name)) or {"id": "job-a"})
    request = SimpleNamespace(
        params={"compute_id": "compute-a"}, body={"projectName": "frigate"},
        require_actor=lambda: Actor("owner-a", Role.MEMBER))

    response = compute_routes.docker_check(request)

    assert response.status == 202
    assert response.value == {"job": {"id": "job-a"}}
    assert calls == [("owner-a", "compute-a", "frigate")]


def test_separate_legacy_docker_playbooks_remain_supported(monkeypatch):
    approvals = {
        "docker_update_pull": {"playbook": "docker-pull.yml", "approved": True,
                               "projectVariable": "docker_project"},
        "docker_update_local_build": {"playbook": "docker-build.yml", "approved": True,
                                      "projectVariable": "docker_project"},
    }
    project = {"id": "project-a", "name": "example", "path": "/srv/example.yml",
               "managed": True, "updateMode": "pull", "strategyConfigured": True}
    seed(playbooks=approvals, project=project)
    monkeypatch.setattr(maintenance.threading, "Thread", NoStartThread)

    pull = maintenance.start_job(
        "compute-a", "docker_project_update", "admin-a", project_id="project-a")
    store.update(lambda document: document["computeJobs"][pull["id"]].update(state="successful"))
    maintenance.set_project_strategy("compute-a", "project-a", "build")
    build = maintenance.start_job(
        "compute-a", "docker_project_update", "admin-a", project_id="project-a")

    assert pull["playbookOperation"] == "docker_update_pull"
    assert build["playbookOperation"] == "docker_update_local_build"
    assert pull["mode"] == "pull" and build["mode"] == "build"


@pytest.mark.parametrize("mode", ["shell", "pull; rm -rf", "registry", ""])
def test_invalid_docker_modes_are_rejected(mode):
    project = {"id": "project-a", "name": "example", "managed": False,
               "updateMode": "read_only"}
    seed(project=project)
    with pytest.raises(ValueError, match="must be pull, build, or read_only"):
        maintenance.set_project_strategy("compute-a", "project-a", mode)


def test_structured_os_contract_is_used_without_stdout_inference():
    structured = maintenance.parse_structured_result(
        'noise: 99 updates available\nHOMELABHQ_RESULT: '
        '{"homelabhq_update":{"available":true,"count":12,'
        '"reboot_required":false,"summary":"12 updates available"}}')

    state = maintenance._state_for_result("os_check", structured, 100)
    assert state == {
        "state": "updates_available", "lastJobAt": 100, "lastCheckedAt": 100,
        "updateCount": 12, "rebootRequired": False, "summary": "12 updates available",
    }
    assert maintenance.parse_structured_result("99 updates available") is None


@pytest.mark.parametrize(("operation", "available", "count", "expected"), [
    ("os_check", True, 4, "updates_available"),
    ("os_check", False, 0, "up_to_date"),
    ("os_update", False, 0, "up_to_date"),
    ("os_update", True, 2, "updates_available"),
])
def test_os_jobs_consume_their_contract_without_mutating_docker_state(
        monkeypatch, operation, available, count, expected):
    seed(playbooks={
        "os_check": {"playbook": "linux-health.yml", "approved": True},
        "os_update": {"playbook": "linux-update.yml", "approved": True},
    })
    original_docker = {
        "available": True,
        "version": "stable-version",
        "projects": [{"id": "stable", "name": "stable-project", "containers": []}],
    }
    original_discovery = {"state": "successful", "lastCheckedAt": 30,
                          "summary": "stable discovery"}
    original_updates = {"state": "updates_available", "lastCheckedAt": 40,
                        "lastUpdatedAt": 50, "updateCount": 1}

    def configure(document):
        instance = document["computeInstances"]["compute-a"]
        instance["updateState"] = {
            "state": "unknown", "lastCheckedAt": 10, "lastUpdatedAt": 20,
        }
        instance["docker"] = copy.deepcopy(original_docker)
        instance["dockerDiscoveryState"] = copy.deepcopy(original_discovery)
        instance["dockerUpdateState"] = copy.deepcopy(original_updates)
    store.update(configure)
    monkeypatch.setattr(maintenance.time, "time", lambda: 100)
    contract = {
        "homelabhq_update": {
            "available": available, "count": count, "reboot_required": False,
            "summary": f"{count} updates remain",
        },
        # Foreign payloads must never let an OS workflow overwrite Docker data.
        "homelabhq_docker": {"available": False, "projects": []},
        "homelabhq_docker_update": {"available": False, "projects": []},
    }

    job, instance = run_contract_job(monkeypatch, operation, contract)

    assert job["state"] == "successful"
    assert instance["updateState"]["state"] == expected
    assert instance["updateState"]["updateCount"] == count
    assert instance["updateState"]["rebootRequired"] is False
    if operation == "os_check":
        assert instance["updateState"]["lastCheckedAt"] == 100
        assert instance["updateState"]["lastUpdatedAt"] == 20
    else:
        assert instance["updateState"]["lastCheckedAt"] == 10
        assert instance["updateState"]["lastUpdatedAt"] == 100
        assert "followUpRecommended" not in job
    assert instance["docker"] == original_docker
    assert instance["dockerDiscoveryState"] == original_discovery
    assert instance["dockerUpdateState"] == original_updates


@pytest.mark.parametrize(("operation", "expected"), [
    ("docker_check", "up_to_date"),
    ("docker_project_update", "successful"),
])
def test_docker_jobs_do_not_mutate_os_or_discovery_state(
        monkeypatch, operation, expected):
    project = {
        "id": "project-a", "name": "example", "managed": True,
        "updateMode": "pull", "configFiles": ["/srv/example.yml"],
    }
    seed(playbooks={
        "docker_check": {"playbook": "docker-health.yml", "approved": True,
                         "projectVariable": "docker_project"},
        "docker_update": {
            "playbook": "docker-maintain.yml", "approved": True,
            "projectVariable": "docker_project", "supportedModes": ["pull"],
        },
    }, project=project)
    original_os = {"state": "updates_available", "lastCheckedAt": 10,
                   "lastUpdatedAt": 20, "updateCount": 3}
    original_discovery = {"state": "successful", "lastCheckedAt": 30,
                          "summary": "stable discovery"}

    def configure(document):
        instance = document["computeInstances"]["compute-a"]
        instance["updateState"] = copy.deepcopy(original_os)
        instance["dockerDiscoveryState"] = copy.deepcopy(original_discovery)
        instance["dockerUpdateState"] = {
            "state": "updates_available", "lastCheckedAt": 40,
            "lastUpdatedAt": 50, "updateCount": 1,
        }
    store.update(configure)
    monkeypatch.setattr(maintenance.time, "time", lambda: 200)
    contract = {
        # A Docker workflow must ignore an OS result even if output contains one.
        "homelabhq_update": {"available": False, "count": 0},
        "homelabhq_docker": {"available": False, "projects": []},
        "homelabhq_docker_update": {
            "available": False,
            "projects": [{"name": "example", "updates_available": False}],
            "summary": "No Docker updates",
        },
    }

    job, instance = run_contract_job(
        monkeypatch, operation, contract,
        project_id="project-a" if operation == "docker_project_update" else None)

    assert job["state"] == "successful"
    assert instance["updateState"] == original_os
    assert instance["dockerDiscoveryState"] == original_discovery
    assert instance["dockerUpdateState"]["state"] == expected
    if operation == "docker_check":
        assert instance["dockerUpdateState"]["lastCheckedAt"] == 200
        assert instance["dockerUpdateState"]["lastUpdatedAt"] == 50
    else:
        assert instance["dockerUpdateState"]["lastCheckedAt"] == 40
        assert instance["dockerUpdateState"]["lastUpdatedAt"] == 200


@pytest.mark.parametrize("operation", ["os_check", "os_update"])
def test_failed_os_jobs_leave_all_docker_state_unchanged(monkeypatch, operation):
    seed(playbooks={
        "os_check": {"playbook": "linux-health.yml", "approved": True},
        "os_update": {"playbook": "linux-update.yml", "approved": True},
    })
    docker = {"available": True, "projects": []}
    discovery = {"state": "successful", "lastCheckedAt": 30}
    updates = {"state": "up_to_date", "lastCheckedAt": 40}

    def configure(document):
        instance = document["computeInstances"]["compute-a"]
        instance["docker"] = copy.deepcopy(docker)
        instance["dockerDiscoveryState"] = copy.deepcopy(discovery)
        instance["dockerUpdateState"] = copy.deepcopy(updates)
    store.update(configure)

    job, instance = run_contract_job(monkeypatch, operation, {}, code=2)

    assert job["state"] == "failed"
    assert instance["updateState"]["state"] == "failed"
    assert instance["docker"] == docker
    assert instance["dockerDiscoveryState"] == discovery
    assert instance["dockerUpdateState"] == updates


@pytest.mark.parametrize("operation", ["docker_check", "docker_project_update"])
def test_failed_docker_jobs_leave_os_state_unchanged(monkeypatch, operation):
    project = {
        "id": "project-a", "name": "example", "managed": True,
        "updateMode": "pull", "configFiles": ["/srv/example.yml"],
    }
    seed(playbooks={
        "docker_check": {"playbook": "docker-health.yml", "approved": True,
                         "projectVariable": "docker_project"},
        "docker_update": {
            "playbook": "docker-maintain.yml", "approved": True,
            "projectVariable": "docker_project", "supportedModes": ["pull"],
        },
    }, project=project)
    original_os = {"state": "up_to_date", "lastCheckedAt": 10,
                   "lastUpdatedAt": 20, "updateCount": 0}
    store.update(lambda document: document["computeInstances"]["compute-a"].update(
        {"updateState": copy.deepcopy(original_os)}))

    job, instance = run_contract_job(
        monkeypatch, operation, {}, code=2,
        project_id="project-a" if operation == "docker_project_update" else None)

    assert job["state"] == "failed"
    assert instance["updateState"] == original_os
    assert instance["dockerUpdateState"]["state"] == "failed"


def test_debug_msg_json_string_is_extracted_from_real_ansible_shape():
    output = (ROOT / "tests" / "fixtures" /
              "ansible_docker_discovery_debug.txt").read_text()

    parsed = maintenance.parse_structured_result_details(output)

    assert parsed["source"] == "stdout_json.debug_msg"
    assert parsed["error"] is None
    assert parsed["result"]["homelabhq_docker"]["available"] is True


def test_debug_msg_object_and_already_decoded_result_are_supported():
    payload = {"available": False, "projects": [], "containers": []}
    debug = {"changed": False, "msg": {"homelabhq_docker": payload}}

    assert maintenance.parse_structured_result(json.dumps(debug)) == {
        "homelabhq_docker": payload}
    assert maintenance.parse_structured_result(
        "", {"homelabhq_docker": payload}) == {"homelabhq_docker": payload}


def test_set_stats_callback_is_preferred_over_stdout_debug():
    callback_payload = {"available": True, "version": "callback-version"}
    stdout_payload = {"available": False, "version": "stdout-version"}
    callback = {"event": "runner_on_ok", "event_data": {"res": {
        "ansible_stats": {"data": {"homelabhq_docker": callback_payload},
                          "aggregate": True, "per_host": False}}}}
    stdout = json.dumps({"msg": json.dumps({"homelabhq_docker": stdout_payload})})

    parsed = maintenance.parse_structured_result_details(stdout, callback)

    assert parsed["source"] == "callback.set_stats"
    assert parsed["result"]["homelabhq_docker"] == callback_payload


def test_direct_callback_result_is_preferred_over_stdout_set_stats():
    callback_payload = {"available": True, "version": "callback-version"}
    stdout_payload = {"available": False, "version": "stdout-version"}
    callback = {"event_data": {"res": {"homelabhq_docker": callback_payload}}}
    stdout = json.dumps({"ansible_stats": {
        "data": {"homelabhq_docker": stdout_payload}}})

    parsed = maintenance.parse_structured_result_details(stdout, callback)

    assert parsed["source"] == "callback.result"
    assert parsed["result"]["homelabhq_docker"] == callback_payload


def test_set_stats_in_default_callback_output_is_supported():
    payload = {"available": False, "summary": "Docker unavailable"}
    stdout = "ok: [synthetic-host] => " + json.dumps({
        "changed": False,
        "ansible_stats": {"data": {"homelabhq_docker": payload},
                          "aggregate": True, "per_host": False},
    })

    parsed = maintenance.parse_structured_result_details(stdout)

    assert parsed["source"] == "stdout_json.set_stats"
    assert parsed["result"] == {"homelabhq_docker": payload}


@pytest.mark.parametrize(("message", "expected"), [
    ('{"homelabhq_docker":', "JSON in msg could not be decoded"),
    (json.dumps({"unrelated": True}), "HomeLabHQ result key not found in msg JSON"),
])
def test_debug_msg_parse_failures_have_specific_diagnostics(message, expected):
    parsed = maintenance.parse_structured_result_details(json.dumps({"msg": message}))

    assert parsed["result"] is None
    assert parsed["error"] == expected


def test_structured_docker_discovery_contract_covers_inventory_and_unknowns():
    docker = maintenance._docker_contract({
        "available": True, "version": "29.x", "compose_available": True,
        "projects": [{
            "name": "example", "config_files": ["/srv/example/compose.yml"],
            "update_mode": "build",
            "containers": [
                {"name": "web", "image": "example/web:latest", "state": "running",
                 "health": "healthy"},
                {"name": "worker", "state": "restarting"},
            ],
            "images": [{"name": "example/web:latest", "id": "sha256:example"}],
        }],
        "containers": [{"name": "standalone", "state": "stopped", "health": "none"}],
        "images": ["standalone:latest"],
    })

    assert docker["available"] is True and docker["version"] == "29.x"
    assert docker["composeAvailable"] is True
    assert docker["projects"][0]["configFiles"] == ["/srv/example/compose.yml"]
    assert docker["projects"][0]["updateMode"] == "build"
    assert docker["projects"][0]["managed"] is False
    assert docker["projects"][0]["containers"][1]["health"] == "unknown"
    assert docker["projects"][0]["containers"][1]["hasHealthcheck"] is None
    assert docker["containers"][0]["state"] == "stopped"
    assert docker["containers"][0]["health"] is None
    assert docker["containers"][0]["hasHealthcheck"] is False
    assert docker["images"][0]["name"] == "standalone:latest"


def test_docker_cli_fields_normalize_projects_membership_and_health():
    output = (ROOT / "tests" / "fixtures" /
              "ansible_docker_discovery_debug.txt").read_text()
    contract = maintenance.parse_structured_result(output)

    docker = maintenance._docker_contract(contract["homelabhq_docker"])

    assert docker["available"] is True
    assert docker["version"] == "99.1.0"
    assert docker["composeAvailable"] is True
    assert docker["composeVersion"] == "9.8.0"
    assert docker["summary"] == "5 container(s), 2 Compose project(s)"
    assert [project["name"] for project in docker["projects"]] == [
        "alpha-stack", "beta-stack"]
    alpha, beta = docker["projects"]
    assert alpha["status"] == "running(2)"
    assert alpha["configFiles"] == ["/srv/alpha/compose.yml"]
    assert alpha["workingDir"] == "/srv/alpha"
    assert [container["name"] for container in alpha["containers"]] == [
        "alpha-web", "alpha-worker"]
    assert alpha["containers"][0]["composeService"] == "web"
    assert alpha["containers"][0]["image"] == "example.invalid/alpha-web:stable"
    assert alpha["containers"][0]["status"] == "Up 2 hours (healthy)"
    assert alpha["containers"][0]["labels"]["com.docker.compose.project"] == \
        "alpha-stack"
    assert alpha["containers"][0]["labelsRaw"]
    assert alpha["containers"][0]["networks"] == ["alpha_default"]
    assert alpha["containers"][0]["ports"] == "8080/tcp"
    assert alpha["containers"][1]["health"] is None
    assert alpha["containers"][1]["hasHealthcheck"] is False
    assert [container["health"] for container in beta["containers"]] == [
        "unhealthy", "starting"]
    assert [container["name"] for container in docker["containers"]] == ["direct-agent"]
    assert docker["containers"][0]["composeProject"] is None
    assert docker["containers"][0]["health"] is None
    assert docker["containers"][0]["hasHealthcheck"] is False


def test_docker_unavailable_contract_is_valid():
    docker = maintenance._docker_contract({
        "available": False, "summary": "Docker is not installed",
        "projects": [], "containers": []})

    assert docker["available"] is False
    assert docker["summary"] == "Docker is not installed"


@pytest.mark.parametrize(("raw", "state", "configured", "health"), [
    ({"name": "healthy", "state": "running", "has_healthcheck": True,
      "health": "healthy"}, "running", True, "healthy"),
    ({"name": "unhealthy", "state": "running", "hasHealthcheck": True,
      "health": "unhealthy"}, "running", True, "unhealthy"),
    ({"name": "starting", "state": "running", "health": "starting"},
     "running", True, "starting"),
    ({"name": "unchecked", "state": "running", "hasHealthcheck": False},
     "running", False, None),
    ({"name": "restarting", "state": "restarting", "health": "none"},
     "restarting", False, None),
    ({"name": "exited", "state": "exited", "health": "none"},
     "exited", False, None),
    ({"name": "missing", "state": "running"}, "running", None, "unknown"),
    ({"name": "malformed", "state": "unexpected", "health": "banana"},
     "unknown", None, "unknown"),
])
def test_container_contract_separates_lifecycle_and_healthcheck(
        raw, state, configured, health):
    container = maintenance._container_contract(raw)

    assert container["state"] == state
    assert container["hasHealthcheck"] is configured
    assert container["health"] == health


def test_docker_inspect_state_detects_healthcheck_and_bounds_failure_details():
    container = maintenance._container_contract({
        "Name": "/api", "State": {
            "Status": "running",
            "Health": {"Status": "unhealthy", "FailingStreak": 3, "Log": [
                {"ExitCode": 1, "Output": "connection refused\n"},
            ]},
        },
        "Config": {"Healthcheck": {"Test": ["CMD", "check-api"]}},
    })
    unchecked = maintenance._container_contract({
        "Name": "/worker", "State": {"Status": "running"},
        "Config": {"Healthcheck": None},
    })

    assert container["name"] == "api"
    assert container["state"] == "running"
    assert container["hasHealthcheck"] is True
    assert container["health"] == "unhealthy"
    assert container["healthDetails"] == {
        "failingStreak": 3, "output": "connection refused", "exitCode": 1}
    assert unchecked["hasHealthcheck"] is False
    assert unchecked["health"] is None


def test_compose_labels_can_create_project_and_preserve_multiple_config_files():
    docker = maintenance._docker_contract({
        "available": True,
        "projects": [],
        "containers": [{
            "Names": "synthetic-service", "State": "running", "HealthStatus": "none",
            "Labels": "com.docker.compose.project=synthetic-stack,"
                      "com.docker.compose.service=api,"
                      "com.docker.compose.project.config_files=/srv/one.yml,/srv/two.yml,"
                      "com.docker.compose.project.working_dir=/srv",
        }],
    })

    assert docker["containers"] == []
    assert docker["projects"][0]["name"] == "synthetic-stack"
    assert docker["projects"][0]["configFiles"] == ["/srv/one.yml", "/srv/two.yml"]
    assert docker["projects"][0]["workingDir"] == "/srv"


def test_docker_payload_without_available_is_schema_invalid():
    assert maintenance._docker_contract({"projects": [], "containers": []}) is None
    assert maintenance._missing_result_summary(
        "docker_discovery", {"homelabhq_docker": {}}) == \
        "Playbook completed but Docker discovery payload schema is invalid"


def test_structured_docker_update_check_keeps_local_build_unknown_without_signal():
    contract = maintenance.parse_structured_result(
        'HOMELABHQ_RESULT: {"homelabhq_docker_update":{"projects":['
        '{"name":"registry","updates_available":true,"update_mode":"pull"},'
        '{"name":"local","update_mode":"build","summary":"source not checked"}]}}')
    state = maintenance._state_for_result("docker_check", contract, 200)

    assert state["state"] == "updates_available"
    assert state["projects"][0]["updatesAvailable"] is True
    assert state["projects"][1]["updateMode"] == "build"
    assert state["projects"][1]["updatesAvailable"] is None


def test_legacy_docker_update_check_contract_remains_supported():
    contract = maintenance.parse_structured_result(
        'HOMELABHQ_RESULT: {"homelabhq_update":{"available":true,"count":2,'
        '"summary":"Two image updates"}}')

    state = maintenance._state_for_result("docker_check", contract, 250)

    assert state["state"] == "updates_available"
    assert state["updateCount"] == 2
    assert state["summary"] == "Two image updates"


def test_docker_update_check_explains_missing_structured_result():
    state = maintenance._state_for_result("docker_check", None, 300)

    assert state["state"] == "unknown"
    assert state["summary"] == "Playbook returned no structured Docker update result"


def test_legacy_docker_check_maps_overall_result_to_selected_project(monkeypatch):
    project = {
        "id": "project-a", "name": "example", "managed": True, "updateMode": "pull",
        "configFiles": ["/srv/example.yml"],
        "updateState": {"updatesAvailable": False, "summary": "stale"},
    }
    seed(playbooks={
        "docker_check": {"playbook": "docker-health.yml", "approved": True,
                         "projectVariable": "docker_project"},
    }, project=project)
    monkeypatch.setattr(maintenance.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(maintenance.ansible, "controller_connection", lambda *_: NullConnection())
    monkeypatch.setattr(maintenance.ansible, "_run", lambda *_args, **_kwargs: (
        0, 'HOMELABHQ_RESULT: {"homelabhq_update":{"available":true,"count":1}}\n'
        "workload-a : ok=1 changed=0 unreachable=0 failed=0 skipped=0 rescued=0 ignored=0",
        ""))

    maintenance.start_job("compute-a", "docker_check", "owner-a")
    instance = store.load()["computeInstances"]["compute-a"]

    assert instance["dockerUpdateState"]["state"] == "updates_available"
    assert instance["dockerUpdateState"]["updateCount"] == 1
    assert instance["docker"]["projects"][0]["updateState"] == {
        "updatesAvailable": True, "summary": None}


def test_mapped_compute_serialization_reflects_each_approved_operation():
    seed(playbooks={
        "os_check": {"playbook": "linux-health.yml", "approved": True},
        "os_update": {"playbook": "linux-update.yml", "approved": True},
        "docker_discovery": {"playbook": "docker-discover.yml", "approved": True},
        "docker_check": {"playbook": "docker-health.yml", "approved": True,
                         "projectVariable": "docker_project"},
        "docker_update": {"playbook": "docker-maintain.yml", "approved": True,
                          "projectVariable": "docker_project",
                          "supportedModes": ["pull", "build"]},
    })
    public = compute.public_instance(store.load()["computeInstances"]["compute-a"])

    assert public["name"] != public["ansible"]["inventoryHost"]
    assert public["ansible"]["updateCheckEligible"] is True
    assert public["ansible"]["updateEligible"] is True
    assert public["ansible"]["dockerDiscoveryEligible"] is True
    assert public["ansible"]["dockerUpdateCheckEligible"] is True
    assert public["ansible"]["dockerUpdateModes"] == ["pull", "build"]


def test_compute_mapping_persists_independent_operation_configuration():
    seed(playbooks={
        "os_check": {"playbook": "linux-health.yml", "approved": True},
        "docker_discovery": {"playbook": "docker-discover.yml", "approved": True},
    }, mapped=False)
    mapping = copy.deepcopy(ansible.DEFAULT_COMPUTE_MAINTENANCE)
    mapping["dockerDiscoveryEnabled"] = False

    ansible.set_mapping("compute-a", True, "primary", "workload-a", mapping)
    persisted = store.load()["computeInstances"]["compute-a"]["ansible"]
    public = compute.public_instance(store.load()["computeInstances"]["compute-a"])

    assert persisted["maintenance"]["osCheckOperation"] == "os_check"
    assert persisted["maintenance"]["dockerDiscoveryEnabled"] is False
    assert public["ansible"]["updateCheckEligible"] is True
    assert public["ansible"]["dockerDiscoveryEligible"] is False
    with pytest.raises(ValueError, match="approved operation"):
        ansible.set_mapping("compute-a", True, "primary", "workload-a", {
            **mapping, "osCheckOperation": "arbitrary.yml"})


def test_mapping_operation_eligibility_is_independent(monkeypatch):
    seed(playbooks={
        "os_update": {"playbook": "linux-update.yml", "approved": True},
        "docker_update": {
            "playbook": "docker-maintain.yml", "approved": True,
            "projectVariable": "docker_project", "supportedModes": ["pull", "build"],
        },
    })
    mapping = copy.deepcopy(ansible.DEFAULT_COMPUTE_MAINTENANCE)
    mapping["osCheckOperation"] = None
    mapping["dockerUpdateOperation"] = None
    store.update(lambda document: document["computeInstances"]["compute-a"]["ansible"]
                 .__setitem__("maintenance", mapping))
    monkeypatch.setattr(maintenance.threading, "Thread", NoStartThread)

    public = compute.public_instance(store.load()["computeInstances"]["compute-a"])
    job = maintenance.start_job("compute-a", "os_update", "admin-a")

    assert public["ansible"]["updateCheckEligible"] is False
    assert public["ansible"]["updateEligible"] is True
    assert public["ansible"]["dockerUpdateModes"] == []
    assert job["playbookOperation"] == "os_update"


def test_refresh_skips_mapping_with_docker_discovery_disabled(monkeypatch):
    seed(playbooks={
        "docker_discovery": {"playbook": "docker-discover.yml", "approved": True},
    })
    store.update(lambda document: document["computeInstances"]["compute-a"]["ansible"]
                 ["maintenance"].__setitem__("dockerDiscoveryEnabled", False))
    monkeypatch.setattr(services.compute, "discover_all", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        services.ansible_integration, "refresh_inventory",
        lambda *_args, **_kwargs: {"hosts": [], "groups": []})
    starts = []
    monkeypatch.setattr(
        services.compute_maintenance, "start_job_sequence",
        lambda *_args, **_kwargs: starts.append((_args, _kwargs)))

    result = services.refresh_compute(Actor("admin-a", Role.ADMIN))

    assert result["dockerJobs"] == []
    assert result["maintenanceJobs"] == []
    assert starts == []


def test_refresh_queues_all_eligible_checks_in_per_workload_order(monkeypatch):
    project = {"id": "project-a", "name": "frigate", "managed": True,
               "updateMode": "pull", "configFiles": ["/opt/frigate"]}
    other_project = {"id": "project-b", "name": "immich", "managed": True,
                     "updateMode": "pull", "configFiles": ["/opt/immich"]}
    seed(playbooks={
        "docker_discovery": {"playbook": "docker-discover.yml", "approved": True},
        "os_check": {"playbook": "linux-health.yml", "approved": True},
        "docker_check": {"playbook": "docker-health.yml", "approved": True,
                         "projectVariable": "docker_project"},
    }, project=project)
    store.update(lambda document: document["computeInstances"]["compute-a"]["docker"].update(
        projects=[project, other_project]))
    monkeypatch.setattr(services.compute, "discover_all", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        services.ansible_integration, "refresh_inventory",
        lambda *_args, **_kwargs: {"hosts": [], "groups": []})
    starts = []

    def start_sequence(instance_id, operations, requested_by):
        starts.append((instance_id, operations, requested_by))
        return [{"id": f"job-{index}",
                 "operation": item if isinstance(item, str) else item["operation"],
                 "projectName": None if isinstance(item, str) else item["projectName"]}
                for index, item in enumerate(operations)]

    monkeypatch.setattr(
        services.compute_maintenance, "start_job_sequence", start_sequence)

    result = services.refresh_compute(Actor("admin-a", Role.ADMIN))

    expected = ["docker_discovery", "os_check", "docker_check", "docker_check"]
    requested = ["docker_discovery", "os_check",
                 {"operation": "docker_check", "projectName": "frigate"},
                 {"operation": "docker_check", "projectName": "immich"}]
    assert starts == [("compute-a", requested, "admin-a")]
    assert result["maintenanceJobs"] == [{
        "computeInstanceId": "compute-a", "computeInstanceName": "not-the-hostname",
        "operations": expected, "queued": True,
        "jobs": [
            {"jobId": "job-0", "operation": "docker_discovery"},
            {"jobId": "job-1", "operation": "os_check"},
            {"jobId": "job-2", "operation": "docker_check",
             "projectName": "frigate"},
            {"jobId": "job-3", "operation": "docker_check",
             "projectName": "immich"},
        ],
    }]
    assert result["dockerJobs"] == [{
        "computeInstanceId": "compute-a",
        "jobId": "job-0", "queued": True,
    }]


def test_refresh_logs_provider_inventory_and_queue_issues(monkeypatch):
    seed(playbooks={
        "os_check": {"playbook": "linux-health.yml", "approved": True},
    })
    events = []
    monkeypatch.setattr(
        services.logbuf, "log_event",
        lambda level, event, **fields: events.append((level, event, fields)))
    monkeypatch.setattr(
        services.compute, "discover_all",
        lambda *_args, **_kwargs: {"providers": [{
            "deviceId": "missing-provider", "ok": False,
            "error": "provider unavailable",
        }]})
    monkeypatch.setattr(
        services.ansible_integration, "refresh_inventory",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("inventory unavailable")))
    monkeypatch.setattr(
        services.compute_maintenance, "start_job_sequence",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            Conflict("job active")))

    result = services.refresh_compute(Actor("admin-a", Role.ADMIN))

    assert result["providers"][0]["deviceName"] == "missing-provider"
    assert result["ansibleInventory"]["ok"] is False
    assert result["maintenanceJobs"][0]["reason"] == "job active"
    messages = [fields["message"] for _level, event, fields in events
                if event == "compute_refresh_issue"]
    assert any("Compute discovery failed" in message for message in messages)
    assert any("Ansible inventory refresh failed" in message for message in messages)
    assert any("Maintenance checks skipped" in message for message in messages)


def test_refresh_sequence_is_persisted_as_one_ordered_active_batch(monkeypatch):
    project = {"id": "project-a", "name": "frigate", "managed": True,
               "updateMode": "pull", "configFiles": ["/opt/frigate"]}
    other_project = {"id": "project-b", "name": "immich", "managed": True,
                     "updateMode": "pull", "configFiles": ["/opt/immich"]}
    seed(playbooks={
        "docker_discovery": {"playbook": "docker-discover.yml", "approved": True},
        "os_check": {"playbook": "linux-health.yml", "approved": True},
        "docker_check": {"playbook": "docker-health.yml", "approved": True,
                         "projectVariable": "docker_project"},
    }, project=project)
    store.update(lambda document: document["computeInstances"]["compute-a"]["docker"].update(
        projects=[project, other_project]))
    thread_calls = []

    class CaptureThread:
        def __init__(self, target, args, **kwargs):
            thread_calls.append((target, args, kwargs))

        def start(self):
            pass

    monkeypatch.setattr(maintenance.threading, "Thread", CaptureThread)

    jobs = maintenance.start_job_sequence(
        "compute-a", ["docker_discovery", "os_check",
                      {"operation": "docker_check", "projectName": "frigate"},
                      {"operation": "docker_check", "projectName": "immich"}],
        "admin-a")

    assert [job["operation"] for job in jobs] == [
        "docker_discovery", "os_check", "docker_check", "docker_check"]
    assert [job["variables"] for job in jobs[2:]] == [
        {"docker_project": "frigate"}, {"docker_project": "immich"}]
    assert all(job["state"] == "queued" for job in jobs)
    assert len(thread_calls) == 1
    assert thread_calls[0][0] is maintenance._run_job_sequence
    assert thread_calls[0][1] == ([job["id"] for job in jobs],)
    with pytest.raises(Conflict, match="already active"):
        maintenance.start_job("compute-a", "os_check", "admin-a")


def test_read_only_project_cannot_execute_an_update(monkeypatch):
    project = {"id": "project-a", "name": "example", "managed": False,
               "updateMode": "read_only", "configFiles": ["/srv/example.yml"]}
    seed(playbooks={"docker_update": {
        "playbook": "docker-maintain.yml", "approved": True,
        "projectVariable": "docker_project", "supportedModes": ["pull"]}}, project=project)
    monkeypatch.setattr(maintenance.threading, "Thread", NoStartThread)

    with pytest.raises(ValueError, match="read-only"):
        maintenance.start_job(
            "compute-a", "docker_project_update", "admin-a", project_id="project-a")


def test_check_without_structured_result_is_incomplete_and_persisted_as_unknown(monkeypatch):
    seed(playbooks={"os_check": {"playbook": "linux-health.yml", "approved": True}})
    monkeypatch.setattr(maintenance.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(maintenance.ansible, "controller_connection", lambda *_: NullConnection())
    monkeypatch.setattr(maintenance.ansible, "_run", lambda *_args, **_kwargs: (
        0, "workload-a : ok=1 changed=0 unreachable=0 failed=0 skipped=0 rescued=0 ignored=0\n"
        "12 updates are available", ""))

    job = maintenance.start_job("compute-a", "os_check", "owner-a")
    persisted = maintenance.get_job(job["id"])
    instance = store.load()["computeInstances"]["compute-a"]

    assert persisted["state"] == "incomplete" and persisted["structuredResult"] is None
    assert persisted["summary"] == \
        "Playbook completed but homelabhq_update key not found"
    assert instance["updateState"]["state"] == "unknown"
    assert instance["updateState"]["summary"] == \
        "Playbook completed but homelabhq_update key not found"


def test_docker_discovery_without_structured_result_preserves_last_inventory(monkeypatch):
    project = {
        "id": "project-a", "name": "example", "managed": True,
        "updateMode": "pull", "configFiles": ["/srv/example.yml"],
    }
    seed(playbooks={
        "docker_discovery": {"playbook": "docker-discover.yml", "approved": True},
    }, project=project)
    monkeypatch.setattr(maintenance.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(maintenance.ansible, "controller_connection", lambda *_: NullConnection())
    monkeypatch.setattr(maintenance.ansible, "_run", lambda *_args, **_kwargs: (
        0, "workload-a : ok=1 changed=0 unreachable=0 failed=0 skipped=0 rescued=0 ignored=0",
        ""))

    job = maintenance.start_job("compute-a", "docker_discovery", "owner-a")
    instance = store.load()["computeInstances"]["compute-a"]

    persisted = maintenance.get_job(job["id"])
    assert persisted["state"] == "incomplete"
    assert persisted["summary"] == \
        "Playbook completed but homelabhq_docker key not found"
    assert persisted["structuredResultError"] == "homelabhq_docker key not found"
    assert instance["docker"]["projects"] == [project]
    assert instance["dockerDiscoveryState"]["state"] == "unknown"
    assert instance["dockerDiscoveryState"]["summary"] == \
        "Playbook completed but homelabhq_docker key not found"


def test_docker_check_without_structured_result_is_incomplete(monkeypatch):
    project = {"id": "project-a", "name": "frigate", "managed": True,
               "updateMode": "pull", "configFiles": ["/opt/frigate"]}
    seed(playbooks={
        "docker_check": {"playbook": "docker-health.yml", "approved": True,
                         "projectVariable": "docker_project"},
    }, project=project)
    monkeypatch.setattr(maintenance.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(maintenance.ansible, "controller_connection", lambda *_: NullConnection())
    monkeypatch.setattr(maintenance.ansible, "_run", lambda *_args, **_kwargs: (
        0, "workload-a : ok=1 changed=0 unreachable=0 failed=0 skipped=0 rescued=0 ignored=0",
        ""))

    job = maintenance.start_job("compute-a", "docker_check", "owner-a")
    persisted = maintenance.get_job(job["id"])
    state = store.load()["computeInstances"]["compute-a"]["dockerUpdateState"]

    assert persisted["state"] == "incomplete"
    assert persisted["summary"] == \
        "Playbook completed but Docker update result key not found"
    assert state["state"] == "unknown"
    assert state["summary"] == "Playbook completed but Docker update result key not found"


def test_real_debug_result_persists_and_serializes_through_compute_api(monkeypatch):
    seed(playbooks={
        "docker_discovery": {"playbook": "docker-discover.yml", "approved": True},
    })
    output = (ROOT / "tests" / "fixtures" /
              "ansible_docker_discovery_debug.txt").read_text()
    monkeypatch.setattr(maintenance.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(maintenance.ansible, "controller_connection", lambda *_: NullConnection())
    monkeypatch.setattr(maintenance.ansible, "_run", lambda *_args, **_kwargs: (
        0, output, ""))

    job = maintenance.start_job("compute-a", "docker_discovery", "owner-a")
    persisted = maintenance.get_job(job["id"])
    response = compute_routes.detail(SimpleNamespace(
        params={"compute_id": "compute-a"},
        require_actor=lambda: Actor("owner-a", Role.MEMBER)))
    serialized = response.value["instance"]

    assert persisted["state"] == "successful"
    assert persisted["structuredResultSource"] == "stdout_json.debug_msg"
    assert persisted["structuredResultError"] is None
    assert "ok: [synthetic-host]" in persisted["stdout"]
    assert serialized["dockerDiscoveryState"]["state"] == "successful"
    assert serialized["docker"]["version"] == "99.1.0"
    assert len(serialized["docker"]["projects"]) == 2
    assert serialized["docker"]["containers"][0]["name"] == "direct-agent"
    summary = compute.summary("owner-a")
    assert summary["hosts"] == 0
    assert summary["unknownHosts"] == 0
    assert summary["containers"] == 5
    assert summary["healthyContainers"] == 1
    assert summary["unhealthyContainers"] == 1
    assert summary["startingContainers"] == 1
    assert summary["withoutHealthcheckContainers"] == 2
    assert summary["unknownContainers"] == 0


def test_malformed_debug_msg_diagnostic_is_persisted(monkeypatch):
    seed(playbooks={
        "docker_discovery": {"playbook": "docker-discover.yml", "approved": True},
    })
    output = json.dumps({"msg": '{"homelabhq_docker":'}) + "\n" + \
        "workload-a : ok=1 changed=0 unreachable=0 failed=0 skipped=0 rescued=0 ignored=0"
    monkeypatch.setattr(maintenance.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(maintenance.ansible, "controller_connection", lambda *_: NullConnection())
    monkeypatch.setattr(maintenance.ansible, "_run", lambda *_args, **_kwargs: (
        0, output, ""))

    job = maintenance.start_job("compute-a", "docker_discovery", "owner-a")
    persisted = maintenance.get_job(job["id"])

    assert persisted["state"] == "incomplete"
    assert persisted["structuredResultError"] == \
        "JSON in msg could not be decoded"
    assert store.load()["computeInstances"]["compute-a"] \
        ["dockerDiscoveryState"]["summary"] == \
        "Playbook completed but JSON in msg could not be decoded"


def test_os_update_without_a_result_recommends_a_fresh_check_and_keeps_reboot_off(monkeypatch):
    seed(playbooks={"os_update": {
        "playbook": "linux-update.yml", "approved": True,
        "supportsReboot": True, "rebootVariable": "allow_reboot"}})
    monkeypatch.setattr(maintenance.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(maintenance.ansible, "controller_connection", lambda *_: NullConnection())
    monkeypatch.setattr(maintenance.ansible, "_run", lambda *_args, **_kwargs: (
        0, "workload-a : ok=2 changed=1 unreachable=0 failed=0 skipped=0 rescued=0 ignored=0",
        ""))

    job = maintenance.start_job("compute-a", "os_update", "admin-a")
    persisted = maintenance.get_job(job["id"])
    state = store.load()["computeInstances"]["compute-a"]["updateState"]

    assert job["allowReboot"] is False and job["variables"] == {"allow_reboot": False}
    assert persisted["state"] == "incomplete"
    assert persisted["followUpRecommended"] == "os_check"
    assert state["state"] == "unknown"


def test_approval_restrictions_block_arbitrary_targets_variables_and_playbooks():
    seed()
    approval("os_check", "linux-health.yml", allowedGroups=["compute"])
    controller = ansible.get_controller()

    argv, _ = ansible.playbook_command(controller, "os_check", "workload-a")
    assert argv[-2:] == ["--limit", "workload-a"]
    with pytest.raises(ValueError, match="outside"):
        ansible.playbook_command(controller, "os_check", "other")
    with pytest.raises(ValueError, match="variables are not approved"):
        ansible.playbook_command(controller, "os_check", "workload-a", {"shell": "id"})
    with pytest.raises(ValueError, match="discovered"):
        approval("os_update", "not-discovered.yml")


def test_maintenance_permissions_preserve_owner_reads_and_admin_mutations(monkeypatch):
    seed(playbooks={"os_check": {"playbook": "linux-health.yml", "approved": True},
                    "os_update": {"playbook": "linux-update.yml", "approved": True}})
    monkeypatch.setattr(maintenance.threading, "Thread", NoStartThread)
    owner = Actor("owner-a", Role.MEMBER)
    outsider = Actor("owner-b", Role.MEMBER)

    assert services.compute_check_updates(owner, "compute-a")["requestedBy"] == "owner-a"
    with pytest.raises(Forbidden):
        services.compute_update(owner, "compute-a")
    with pytest.raises(NotFound):
        services.compute_check_updates(outsider, "compute-a")


def test_v3_migration_preserves_legacy_approvals_mappings_and_projects():
    document = {
        "schemaVersion": 3,
        "ansibleControllers": {"primary": controller_record(playbooks={
            "docker_update_local_build": {
                "playbook": "docker-build.yml", "approved": True,
                "projectVariable": "compose_config"},
        })},
        "computeInstances": {"compute-a": {
            "ansible": {"enabled": True, "controllerId": "primary",
                        "inventoryHost": "workload-a"},
            "docker": {"projects": [{
                "name": "example", "path": "/srv/example.yml",
                "updateStrategy": "local_build", "strategyConfigured": True,
            }]},
        }},
        "computeJobs": {"job-a": {"id": "job-a"}},
    }

    migrated, changed = store._migrate_doc(document)
    project = migrated["computeInstances"]["compute-a"]["docker"]["projects"][0]

    assert changed is True and migrated["schemaVersion"] == 4
    assert migrated["computeInstances"]["compute-a"]["ansible"]["maintenance"] == \
        ansible.DEFAULT_COMPUTE_MAINTENANCE
    assert project["updateMode"] == "build" and project["managed"] is True
    assert migrated["ansibleControllers"]["primary"]["playbooks"] \
        ["docker_update_local_build"]["supportedModes"] == ["build"]
    assert migrated["ansibleControllers"]["primary"]["playbooks"] \
        ["docker_update_local_build"]["projectVariable"] == "compose_config"
    assert migrated["computeJobs"]["job-a"]["mode"] is None

import contextlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import ansible_integration as ansible
import compute
import compute_maintenance as maintenance
import crypto
import services
import store
from context import Actor, Role
from errors import Conflict, Forbidden, NotFound, ValidationError


@pytest.fixture(autouse=True)
def isolated_store(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(store, "DB_FILE", str(tmp_path / "homelabhq.json"))
    monkeypatch.setattr(store, "LOCK_FILE", str(tmp_path / "homelabhq.lock"))
    secrets_dir = tmp_path / "secrets"
    monkeypatch.setattr(store, "SECRETS_DIR", str(secrets_dir))
    monkeypatch.setattr(crypto, "SECRETS_DIR", str(secrets_dir))
    monkeypatch.setattr(crypto, "SECRET_FILE", str(secrets_dir / "instance_secret"))
    store._cache.update(doc=None, mtime=None)


def seed_parent(owner="owner-1"):
    parent = {
        "id": "device-1", "ownerId": owner, "name": "Synthetic Hypervisor",
        "host": "hypervisor.example.test", "driverId": "proxmox.ve",
        "transport": "api", "created": 1,
    }
    store.update(lambda document: document["devices"].update({parent["id"]: parent}))
    return parent


class DiscoveryDriver:
    supports_compute = True
    id = "synthetic.provider"
    compute_provider = "proxmox"

    def __init__(self, workloads):
        self.workloads = workloads

    def compute_instances(self, _connection):
        return self.workloads


def discovery_context(driver):
    @contextlib.contextmanager
    def connected(*_args, **_kwargs):
        yield seed_parent() if False else {}, driver, object()
    return connected


def test_compute_discovery_persists_vms_lxcs_and_parent_relationship(monkeypatch):
    parent = seed_parent()
    driver = DiscoveryDriver([
        {"providerInstanceId": "101", "type": "vm", "name": "Example VM",
         "status": "running", "node": "node-a", "cpuCores": 4,
         "memoryBytes": 8 * 1024**3, "diskBytes": 64 * 1024**3,
         "ipAddresses": ["192.0.2.10"], "uptimeSeconds": 90},
        {"providerInstanceId": "202", "type": "lxc", "name": "Example CT",
         "status": "stopped", "node": "node-b"},
    ])
    monkeypatch.setattr(compute.devices, "_drv_for", lambda _: driver)
    monkeypatch.setattr(compute.devices, "device_conn", discovery_context(driver))

    result = compute.discover_device(parent["id"])
    instances = compute.list_instances(parent["ownerId"])

    assert result == {"discovered": 2, "created": 2, "stale": 0}
    assert {item["type"] for item in instances} == {"vm", "lxc"}
    vm = next(item for item in instances if item["type"] == "vm")
    assert vm["parentDevice"] == {
        "id": "device-1", "name": "Synthetic Hypervisor",
        "host": "hypervisor.example.test", "driverId": "proxmox.ve",
    }
    assert vm["providerInstanceId"] == "101" and vm["cpuCores"] == 4


def test_compute_summary_reports_host_reachability(monkeypatch):
    parent = seed_parent()
    parent["state"] = {"online": True, "confirmedOnline": False}
    store.update(lambda document: document["devices"].update({parent["id"]: parent}))
    driver = DiscoveryDriver([
        {"providerInstanceId": "101", "type": "vm", "name": "Example",
         "status": "running"},
    ])
    monkeypatch.setattr(compute.devices, "_drv_for", lambda _: driver)
    monkeypatch.setattr(compute.devices, "device_conn", discovery_context(driver))
    compute.discover_device(parent["id"])

    summary = compute.summary(parent["ownerId"])

    assert summary["hosts"] == 1
    assert summary["onlineHosts"] == 0
    assert summary["offlineHosts"] == 1
    assert summary["unknownHosts"] == 0
    assert compute.list_instances(parent["ownerId"])[0]["parentDevice"]["state"] == {
        "online": True, "confirmedOnline": False}


def test_compute_summary_does_not_treat_missing_health_as_no_healthcheck():
    parent = seed_parent()
    store.update(lambda document: document["computeInstances"].update({
        "compute-unknown": {
            "id": "compute-unknown", "ownerId": parent["ownerId"],
            "parentDeviceId": parent["id"], "type": "lxc", "name": "Unknown",
            "status": "running", "docker": {"available": True, "containers": [
                {"name": "missing", "state": "running", "health": "unknown"},
                {"name": "legacy", "state": "running", "health": "no_healthcheck"},
            ]},
        },
        "compute-stale": {
            "id": "compute-stale", "ownerId": parent["ownerId"],
            "parentDeviceId": parent["id"], "type": "vm", "name": "Stale",
            "status": "running", "discoveryState": "unavailable",
            "dockerDiscoveryState": {"state": "failed"},
            "docker": {"available": True, "containers": [
                {"name": "formerly-healthy", "state": "running",
                 "hasHealthcheck": True, "health": "healthy"},
            ]},
        },
    }))

    summary = compute.summary(parent["ownerId"])

    assert summary["withoutHealthcheckContainers"] == 1
    assert summary["healthyContainers"] == 0
    assert summary["unknownContainers"] == 2


def test_compute_discovery_marks_missing_stale_and_failure_unavailable(monkeypatch):
    seed_parent()
    driver = DiscoveryDriver([
        {"providerInstanceId": "101", "type": "vm", "name": "Example", "status": "running"},
    ])
    monkeypatch.setattr(compute.devices, "_drv_for", lambda _: driver)
    monkeypatch.setattr(compute.devices, "device_conn", discovery_context(driver))
    compute.discover_device("device-1")
    driver.workloads = []
    assert compute.discover_device("device-1")["stale"] == 1
    instance = next(iter(store.load()["computeInstances"].values()))
    assert instance["discoveryState"] == "stale"

    @contextlib.contextmanager
    def failed(*_args, **_kwargs):
        raise RuntimeError("provider unavailable")
        yield

    monkeypatch.setattr(compute.devices, "device_conn", failed)
    with pytest.raises(RuntimeError, match="provider unavailable"):
        compute.discover_device("device-1")
    retained = next(iter(store.load()["computeInstances"].values()))
    assert retained["discoveryState"] == "unavailable"
    assert retained["lastDiscoveryError"] == "provider unavailable"


def controller_payload(**overrides):
    value = {
        "enabled": True, "displayName": "Synthetic Controller",
        "host": "controller.example.test", "sshPort": 2222,
        "sshUsername": "automation", "authMethod": "private_key",
        "privateKey": "SYNTHETIC-PRIVATE-KEY", "projectDirectory": "/srv/automation/project",
        "inventoryPath": "inventory/hosts.yml", "playbooksDirectory": "playbooks",
        "ansiblePlaybookExecutable": "/opt/ansible/bin/ansible-playbook",
        "ansibleInventoryExecutable": "/opt/ansible/bin/ansible-inventory",
        "connectionTimeout": 9, "executionTimeout": 600,
    }
    value.update(overrides)
    return value


def configured_controller():
    controller = ansible.save_controller(controller_payload())
    assert "privateKey" not in controller and "credentialRef" not in controller
    return ansible.get_controller()


def test_ansible_configuration_encrypts_and_never_returns_credentials():
    public = ansible.save_controller(controller_payload())
    document = store.load()

    assert public["credentialConfigured"] is True
    assert "SYNTHETIC-PRIVATE-KEY" not in json.dumps(public)
    assert "SYNTHETIC-PRIVATE-KEY" not in json.dumps(document)
    assert crypto.decrypt(next(iter(document["credentials"].values())))["privateKey"] == \
        "SYNTHETIC-PRIVATE-KEY"
    assert document["ansibleControllers"]["primary"]["ansiblePlaybookExecutable"] == \
        "/opt/ansible/bin/ansible-playbook"


@pytest.mark.parametrize("field,value", [
    ("projectDirectory", "/"),
    ("inventoryPath", "../../etc/passwd"),
    ("playbooksDirectory", "/var/tmp/elsewhere"),
])
def test_ansible_configuration_blocks_path_traversal(field, value):
    with pytest.raises(ValueError, match="path|inside|contained"):
        ansible.save_controller(controller_payload(**{field: value}))


@pytest.mark.parametrize(("field", "value"), [
    ("ansiblePlaybookExecutable", "ansible-playbook"),
    ("ansibleInventoryExecutable", "/opt/ansible/bin/ansible-inventory;id"),
    ("ansiblePlaybookExecutable", "/opt/ansible env/bin/ansible-playbook"),
    ("ansibleInventoryExecutable", "/opt/ansible/../bin/ansible-inventory"),
])
def test_ansible_configuration_rejects_unsafe_executable_paths(field, value):
    with pytest.raises(ValueError, match="absolute|metacharacters"):
        ansible.save_controller(controller_payload(**{field: value}))


class ControllerConnection:
    def __init__(self, inventory=None, failure=None):
        self.inventory = inventory or {
            "_meta": {"hostvars": {
                "example-vm": {"ansible_host": "192.0.2.10"},
                "other-host": {"ansible_host": "192.0.2.11"},
            }},
            "all": {"children": ["compute_group"]},
            "compute_group": {"hosts": ["example-vm", "other-host"]},
        }
        self.failure = failure
        self.commands = []

    def __enter__(self):
        if self.failure:
            raise RuntimeError(self.failure)
        return self

    def __exit__(self, *_args):
        return None

    def run(self, command, timeout):
        self.commands.append((command, timeout))
        if "test -d" in command:
            return 0, "", ""
        if command.startswith("test -f ") or command.startswith("test -x "):
            return 0, "", ""
        if "ansible-playbook --version" in command:
            return 0, "ansible-playbook [core 2.19.1]\n", ""
        if "ansible-inventory --version" in command:
            return 0, "ansible-inventory [core 2.19.1]\n", ""
        if "ansible-inventory" in command and "--list" in command:
            return 0, json.dumps(self.inventory), ""
        if command.startswith("cd") and "find" in command:
            return 0, "/srv/automation/project/playbooks/check.yml\n" \
                "/srv/automation/project/playbooks/update.yaml\n/etc/outside.yml\n", ""
        raise AssertionError(command)


def test_connection_inventory_and_playbook_discovery_are_structured(monkeypatch):
    configured_controller()
    connection = ControllerConnection()
    monkeypatch.setattr(ansible, "controller_connection", lambda *_: connection)

    status = ansible.test_connection()
    inventory = ansible.refresh_inventory()
    playbooks = ansible.discover_playbooks()

    assert status["controller"]["ok"] and status["ansiblePlaybook"]["version"] == "2.19.1"
    assert status["ansiblePlaybook"]["path"] == "/opt/ansible/bin/ansible-playbook"
    assert status["inventory"] == {"ok": True, "hosts": 2, "groups": 2}
    assert {host["name"] for host in inventory["hosts"]} == {"example-vm", "other-host"}
    assert next(host for host in inventory["hosts"] if host["name"] == "example-vm")["groups"] \
        == ["all", "compute_group"]
    assert playbooks == ["check.yml", "update.yaml"]
    assert all("SYNTHETIC-PRIVATE-KEY" not in command for command, _ in connection.commands)
    assert any("/opt/ansible/bin/ansible-inventory -i" in command
               for command, _ in connection.commands)


class DiscoveryConnection(ControllerConnection):
    def run(self, command, timeout):
        self.commands.append((command, timeout))
        if command == "command -v ansible-playbook":
            return 1, "", ""
        if command == "command -v ansible-inventory":
            return 0, "/usr/bin/ansible-inventory\n", ""
        if command == "cd && pwd -P":
            return 0, "/srv/controller-users/automation\n", ""
        if command.startswith("test -f ") or command.startswith("test -x "):
            if ("/srv/controller-users/automation/.local/bin/ansible-playbook" in command or
                    "/usr/bin/ansible-inventory" in command):
                return 0, "", ""
            return 1, "", ""
        if "ansible-playbook --version" in command:
            return 0, "ansible-playbook [core 2.19.1]\n", ""
        if "ansible-inventory --version" in command:
            return 0, "ansible-inventory [core 2.19.1]\n", ""
        if "ansible-inventory" in command and "--list" in command:
            return 0, json.dumps(self.inventory), ""
        if "test -d" in command:
            return 0, "", ""
        raise AssertionError(command)


def test_connection_discovers_non_path_executables_from_resolved_remote_home(monkeypatch):
    configured_controller()
    store.update(lambda document: document["ansibleControllers"]["primary"].update(
        ansiblePlaybookExecutable="", ansibleInventoryExecutable=""))
    connection = DiscoveryConnection()
    monkeypatch.setattr(ansible, "controller_connection", lambda *_: connection)

    status = ansible.test_connection()

    assert status["ansiblePlaybook"] == {
        "ok": True, "path": "/srv/controller-users/automation/.local/bin/ansible-playbook",
        "version": "2.19.1", "discovered": True,
    }
    assert status["ansibleInventory"]["path"] == "/usr/bin/ansible-inventory"
    assert status["ansibleInventory"]["discovered"] is True
    assert connection.commands[1][0] == "command -v ansible-playbook"
    assert connection.commands[2][0] == "command -v ansible-inventory"
    assert any(command == "cd && pwd -P" for command, _ in connection.commands)


@pytest.mark.parametrize(("failed_check", "message"), [
    ("test -f", "not a regular file"),
    ("test -x", "not executable"),
])
def test_connection_rejects_invalid_configured_executable(monkeypatch, failed_check, message):
    configured_controller()
    connection = ControllerConnection()
    original_run = connection.run

    def missing_playbook(command, timeout):
        if command == f"{failed_check} /opt/ansible/bin/ansible-playbook":
            connection.commands.append((command, timeout))
            return 1, "", ""
        return original_run(command, timeout)

    connection.run = missing_playbook
    monkeypatch.setattr(ansible, "controller_connection", lambda *_: connection)

    status = ansible.test_connection()

    assert status["controller"]["ok"] is True
    assert status["ansiblePlaybook"]["ok"] is False
    assert message in status["ansiblePlaybook"]["error"]


def test_connection_failure_redacts_controller_secret(monkeypatch):
    configured_controller()
    monkeypatch.setattr(ansible, "controller_connection",
                        lambda *_: ControllerConnection(failure="bad SYNTHETIC-PRIVATE-KEY"))
    status = ansible.test_connection()
    assert "SYNTHETIC-PRIVATE-KEY" not in json.dumps(status)
    assert "[REDACTED]" in status["controller"]["error"]


def seed_compute_and_inventory(mapped=False):
    seed_parent()
    controller = configured_controller()
    controller["inventory"] = {
        "hosts": {"example-vm": {"name": "example-vm", "address": "192.0.2.10",
                                  "groups": ["compute_group"]}},
        "groups": {"compute_group": {"name": "compute_group", "hosts": ["example-vm"]}},
        "discoveredAt": 1,
    }
    controller["discoveredPlaybooks"] = [
        "os-check.yml", "os-update.yml", "docker.yml", "docker-pull.yml", "docker-build.yml"]
    controller["playbooks"] = {
        "os_check": {"playbook": "os-check.yml", "approved": True},
        "os_update": {"playbook": "os-update.yml", "approved": True,
                      "supportsReboot": True, "rebootVariable": "maintenance_reboot"},
        "docker_check": {"playbook": "docker.yml", "approved": True,
                         "projectVariable": "docker_project"},
        "docker_discovery": {"playbook": "docker.yml", "approved": True},
        "docker_update_pull": {"playbook": "docker-pull.yml", "approved": True,
                               "projectVariable": "docker_project", "updateStrategy": "pull"},
        "docker_update_local_build": {
            "playbook": "docker-build.yml", "approved": True,
            "projectVariable": "docker_project", "updateStrategy": "local_build"},
    }
    instance = {
        "id": "compute-1", "ownerId": "owner-1", "parentDeviceId": "device-1",
        "provider": "proxmox", "providerInstanceId": "101", "type": "vm",
        "name": "example-vm", "status": "running", "ipAddresses": ["192.0.2.10"],
        "discoveryState": "current", "updateState": {"state": "unknown"},
    }
    if mapped:
        instance["ansible"] = {"enabled": True, "controllerId": "primary",
                               "inventoryHost": "example-vm"}

    def mutate(document):
        document["ansibleControllers"]["primary"] = controller
        document["computeInstances"]["compute-1"] = instance
    store.update(mutate)
    return instance


def test_mapping_is_suggested_but_not_persisted_until_confirmation():
    instance = seed_compute_and_inventory(mapped=False)
    suggestions = ansible.mapping_suggestions(instance)
    assert suggestions == [{"controllerId": "primary", "inventoryHost": "example-vm",
                             "signals": ["exact_hostname", "ip_address"]}]
    assert not (store.load()["computeInstances"]["compute-1"].get("ansible") or {}).get("enabled")
    mapped = ansible.set_mapping("compute-1", True, "primary", "example-vm")
    assert mapped["ansible"]["enabled"] is True
    assert mapped["ansible"]["confirmedAt"]


def test_mapping_rejects_undiscovered_or_injected_inventory_target():
    seed_compute_and_inventory()
    with pytest.raises(ValueError, match="not discovered"):
        ansible.set_mapping("compute-1", True, "primary", "missing-host")
    with pytest.raises(ValueError, match="invalid"):
        ansible.set_mapping("compute-1", True, "primary", "example-vm; id")


class ImmediateThread:
    def __init__(self, target, args, **_kwargs):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


@contextlib.contextmanager
def unused_connection(*_args, **_kwargs):
    yield object()


def run_job(monkeypatch, operation, stdout, code=0, **kwargs):
    monkeypatch.setattr(maintenance.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(maintenance.ansible, "controller_connection", unused_connection)
    monkeypatch.setattr(maintenance.ansible, "_run", lambda *_args, **_kwargs: (code, stdout, ""))
    maintenance.start_job("compute-1", operation, "admin-1", **kwargs)
    return max(store.load()["computeJobs"].values(), key=lambda job: job["createdAt"])


def test_update_check_parses_contract_recap_and_persists_history(monkeypatch):
    seed_compute_and_inventory(mapped=True)
    output = 'HOMELABHQ_RESULT: {"homelabhq_update":{"available":true,"count":7,' \
             '"reboot_required":false,"summary":"7 updates available"}}\n' \
             'example-vm : ok=4 changed=0 unreachable=0 failed=0 skipped=1 rescued=0 ignored=0\n'
    job = run_job(monkeypatch, "os_check", output)
    instance = store.load()["computeInstances"]["compute-1"]

    assert job["state"] == "successful" and job["recap"]["example-vm"]["ok"] == 4
    assert instance["updateState"]["state"] == "updates_available"
    assert instance["updateState"]["updateCount"] == 7
    assert maintenance.list_jobs("compute-1")[0]["structuredResult"]["homelabhq_update"]


def test_reboot_defaults_false_and_requires_service_confirmation(monkeypatch):
    seed_compute_and_inventory(mapped=True)
    monkeypatch.setattr(maintenance.threading, "Thread", type("NoStart", (), {
        "__init__": lambda self, *args, **kwargs: None, "start": lambda self: None}))
    job = maintenance.start_job("compute-1", "os_update", "admin-1")
    assert job["allowReboot"] is False
    assert job["variables"] == {"maintenance_reboot": False}
    with pytest.raises(ValidationError, match="explicit confirmation"):
        services.compute_update(Actor("admin-1", Role.ADMIN), "compute-1",
                                allow_reboot=True, reboot_confirmed=False)


def test_duplicate_jobs_are_prevented():
    seed_compute_and_inventory(mapped=True)
    store.update(lambda document: document["computeJobs"].update({
        "active": {"id": "active", "computeInstanceId": "compute-1", "state": "running"}}))
    with pytest.raises(Conflict, match="already active"):
        maintenance.start_job("compute-1", "os_check", "owner-1")


@pytest.mark.parametrize(("code", "recap", "expected"), [
    (4, "example-vm : ok=0 changed=0 unreachable=1 failed=0 skipped=0 rescued=0 ignored=0", "unreachable"),
    (2, "example-vm : ok=1 changed=0 unreachable=0 failed=1 skipped=0 rescued=0 ignored=0", "failed"),
])
def test_unreachable_and_failed_playbooks_persist_safe_state(monkeypatch, code, recap, expected):
    seed_compute_and_inventory(mapped=True)
    job = run_job(monkeypatch, "os_check", recap, code=code)
    assert job["state"] == expected
    assert store.load()["computeInstances"]["compute-1"]["updateState"]["state"] == expected


def test_docker_discovery_models_projects_health_and_update_modes(monkeypatch):
    seed_compute_and_inventory(mapped=True)
    contract = {
        "homelabhq_docker": {"available": True, "version": "28.0", "compose_available": True,
            "projects": [
                {"name": "synthetic-pull", "path": "/srv/stacks/pull", "update_strategy": "pull",
                 "containers": [{"name": "web", "state": "running", "health": "healthy",
                                 "image": "example/web:1"}]},
                {"name": "synthetic-build", "path": "/srv/stacks/build",
                 "update_strategy": "local_build",
                 "containers": [{"name": "worker", "state": "restarting", "health": None,
                                 "image": "example/worker:1"}]},
                {"name": "synthetic-readonly", "path": "/srv/stacks/read",
                 "containers": []},
            ]}}
    output = f"HOMELABHQ_RESULT: {json.dumps(contract)}\n" \
             "example-vm : ok=3 changed=0 unreachable=0 failed=0 skipped=0 rescued=0 ignored=0\n"
    run_job(monkeypatch, "docker_discovery", output)
    projects = store.load()["computeInstances"]["compute-1"]["docker"]["projects"]

    assert [project["updateStrategy"] for project in projects] == [
        "pull", "local_build", "unmanaged"]
    assert projects[0]["containers"][0]["health"] == "healthy"
    assert projects[1]["containers"][0]["state"] == "restarting"

    maintenance.set_project_strategy("compute-1", projects[2]["id"], "pull")
    monkeypatch.setattr(maintenance.threading, "Thread", type("NoStart", (), {
        "__init__": lambda self, *args, **kwargs: None, "start": lambda self: None}))
    update_job = maintenance.start_job(
        "compute-1", "docker_project_update", "admin-1", project_id=projects[2]["id"])
    assert update_job["playbookOperation"] == "docker_update_pull"
    assert update_job["variables"] == {"docker_project": "synthetic-readonly"}


def test_recap_and_structured_parsers_ignore_unstructured_output():
    assert maintenance.parse_recap("noise\nhost : ok=1 changed=2 unreachable=0 failed=0 skipped=3 rescued=0 ignored=0") \
        == {"host": {"ok": 1, "changed": 2, "unreachable": 0, "failed": 0,
                     "skipped": 3, "rescued": 0, "ignored": 0}}
    assert maintenance.parse_structured_result("12 updates available") is None
    assert maintenance.parse_structured_result(
        'HOMELABHQ_RESULT: {"homelabhq_update":{"available":false}}') \
        == {"homelabhq_update": {"available": False}}
    assert maintenance.parse_structured_result(
        '    "msg": "HOMELABHQ_RESULT: {\\"homelabhq_update\\":'
        '{\\"available\\":true,\\"count\\":2}}"') == {
            "homelabhq_update": {"available": True, "count": 2}}


def test_permissions_follow_existing_roles_and_owner_visibility():
    seed_compute_and_inventory(mapped=True)
    member = Actor("owner-1", Role.MEMBER)
    outsider = Actor("owner-2", Role.MEMBER)
    with pytest.raises(Forbidden):
        services.save_ansible_controller(member, controller_payload())
    with pytest.raises(NotFound):
        services.compute_detail(outsider, "compute-1")
    with pytest.raises(Forbidden):
        services.compute_update(member, "compute-1")
    assert services.compute_detail(member, "compute-1")["name"] == "example-vm"


def test_compute_list_reports_whether_ansible_maintenance_is_enabled():
    actor = Actor("admin-1", Role.ADMIN)
    assert services.list_compute(actor)["ansibleEnabled"] is False

    store.update(lambda document: document["ansibleControllers"].update({
        "primary": {"id": "primary", "enabled": True},
    }))

    assert services.list_compute(actor)["ansibleEnabled"] is True


def test_approved_playbooks_block_path_and_argument_injection():
    controller = configured_controller()
    controller["inventory"] = {"hosts": {"safe-host": {
        "name": "safe-host", "address": "192.0.2.50", "groups": []}}, "groups": {}}
    controller["discoveredPlaybooks"] = ["safe.yml"]
    controller["playbooks"] = {"os_check": {"playbook": "../../unsafe.yml", "approved": True}}
    with pytest.raises(ValueError, match="allowlist"):
        ansible.playbook_command(controller, "os_check", "safe-host")
    controller["playbooks"]["os_check"]["playbook"] = "safe.yml"
    with pytest.raises(ValueError, match="invalid"):
        ansible.playbook_command(controller, "os_check", "safe-host;touch /tmp/x")


def test_playbook_commands_use_the_exact_configured_executable_path():
    controller = configured_controller()
    controller["inventory"] = {"hosts": {"safe-host": {
        "name": "safe-host", "address": "192.0.2.50", "groups": []}}, "groups": {}}
    controller["discoveredPlaybooks"] = ["safe.yml"]
    controller["playbooks"] = {"os_check": {"playbook": "safe.yml", "approved": True}}

    argv, _ = ansible.playbook_command(controller, "os_check", "safe-host")

    assert argv[0] == "/opt/ansible/bin/ansible-playbook"


def test_compute_page_and_card_markup_are_wired():
    html = (ROOT / "web" / "index.html").read_text()
    script = (ROOT / "web" / "js" / "compute.js").read_text()
    settings_script = (ROOT / "web" / "js" / "settings.js").read_text()
    assert 'data-tab="compute"' in html and 'data-panel="compute"' in html
    assert 'id="compute-refresh"' in html and "Refresh All" in html
    assert "Refresh all" not in html
    assert 'id="compute-update-all"' in html and "Update All" in html
    assert 'id="compute-refresh-progress"' in html
    assert 'id="compute-update-all-progress"' in html
    assert "Need Attention 0" in html and "Check Updates" in script
    assert "Hosted on" in script and "Allow reboot if required" in script
    assert 'id="ans-playbook-executable"' in html
    assert 'id="ans-inventory-executable"' in html
    assert "Ansible executable paths discovered. Review and Save them." in settings_script

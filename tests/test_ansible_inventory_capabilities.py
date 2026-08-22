import copy
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

import ansible_integration as ansible
import compute
import compute_maintenance as maintenance
import logbuf
import services
import store
from context import Actor, Role


@pytest.fixture(autouse=True)
def isolated_store(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(store, "DB_FILE", str(tmp_path / "homelabhq.json"))
    monkeypatch.setattr(store, "LOCK_FILE", str(tmp_path / "homelabhq.lock"))
    store._cache.update(doc=None, mtime=None)
    logbuf.REQUEST_LOG.clear()


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

    def __exit__(self, *_args):
        return False


def seed_capabilities():
    hosts = {
        "homeassistant": {
            "name": "homeassistant", "address": "appliance.invalid",
            "groups": ["appliances"], "dockerProjects": [],
        },
        "debian-only": {
            "name": "debian-only", "address": "debian.invalid",
            "groups": ["debian_hosts"], "dockerProjects": [],
        },
        "docker-only": {
            "name": "docker-only", "address": "docker.invalid",
            "groups": ["docker_hosts"],
            "dockerProjects": [{"name": "stack", "updateMode": "pull"}],
        },
        "union-host": {
            "name": "union-host", "address": "union.invalid",
            "groups": ["debian_hosts", "docker_hosts"],
            "dockerProjects": [{"name": "stack", "updateMode": "pull"}],
        },
    }
    controller = {
        "id": "primary", "enabled": True,
        "inventoryPath": "/srv/ansible/inventory.yml",
        "playbooksDirectory": "/srv/ansible/playbooks",
        "ansiblePlaybookExecutable": "/usr/bin/ansible-playbook",
        "ansibleInventoryExecutable": "/usr/bin/ansible-inventory",
        "executionTimeout": 60,
        "inventory": {
            "hosts": hosts,
            "groups": {
                group: {"name": group, "hosts": [
                    name for name, host in hosts.items() if group in host["groups"]]}
                for group in ("appliances", "debian_hosts", "docker_hosts")
            },
        },
        "discoveredPlaybooks": [
            "os-check.yml", "os-update.yml", "docker-discover.yml",
            "docker-check.yml", "docker-update.yml", "homeassistant-health.yml",
        ],
        "playbooks": {
            "os_check": {"playbook": "os-check.yml", "approved": True},
            "os_update": {"playbook": "os-update.yml", "approved": True},
            "docker_discovery": {
                "playbook": "docker-discover.yml", "approved": True},
            "docker_check": {
                "playbook": "docker-check.yml", "approved": True,
                "projectVariable": "docker_project"},
            "docker_update": {
                "playbook": "docker-update.yml", "approved": True,
                "projectVariable": "docker_project", "supportedModes": ["pull"]},
            "appliance_health": {
                "playbook": "homeassistant-health.yml", "approved": True,
                "allowedGroups": ["appliances"]},
        },
    }
    instances = {}
    for index, name in enumerate(hosts, 1):
        instance = {
            "id": name, "ownerId": "owner", "provider": "proxmox",
            "providerInstanceId": str(index), "type": "vm", "name": name,
            "status": "running", "discoveryState": "current",
            "ansible": {
                "enabled": True, "controllerId": "primary", "inventoryHost": name,
                "maintenance": copy.deepcopy(ansible.DEFAULT_COMPUTE_MAINTENANCE),
            },
        }
        if "docker_hosts" in hosts[name]["groups"]:
            instance["docker"] = {"available": True, "projects": [{
                "id": f"{name}-stack", "name": "stack", "approved": True,
                "managed": True, "updateMode": "pull", "containers": [],
            }]}
        instances[name] = instance

    def mutate(document):
        document["ansibleControllers"]["primary"] = controller
        document["computeInstances"].update(instances)

    store.update(mutate)


@pytest.mark.parametrize("operation", ["os_check", "os_update"])
def test_appliances_only_host_rejects_os_operations(monkeypatch, operation):
    seed_capabilities()
    monkeypatch.setattr(maintenance.threading, "Thread", NoStartThread)

    with pytest.raises(ValueError, match="requires Ansible inventory group debian_hosts"):
        maintenance.start_job("homeassistant", operation, "owner")


@pytest.mark.parametrize(
    "operation", ["docker_discovery", "docker_check", "docker_project_update"])
def test_appliances_only_host_rejects_every_docker_operation(monkeypatch, operation):
    seed_capabilities()
    monkeypatch.setattr(maintenance.threading, "Thread", NoStartThread)

    with pytest.raises(ValueError, match="requires Ansible inventory group docker_hosts"):
        maintenance.start_job(
            "homeassistant", operation, "owner", project_name="stack")


def test_group_capabilities_retain_valid_operations_and_union(monkeypatch):
    seed_capabilities()
    monkeypatch.setattr(maintenance.threading, "Thread", NoStartThread)

    os_job = maintenance.start_job("debian-only", "os_check", "owner")
    docker_job = maintenance.start_job("docker-only", "docker_discovery", "owner")
    store.update(lambda document: document["computeJobs"][os_job["id"]].update(
        state="successful"))
    union = compute.public_instance(store.load()["computeInstances"]["union-host"])

    assert os_job["operation"] == "os_check"
    assert docker_job["operation"] == "docker_discovery"
    assert union["ansible"]["capabilities"] == {
        "osMaintenance": True,
        "dockerMaintenance": True,
        "applianceHealth": False,
    }
    assert union["ansible"]["updateCheckEligible"] is True
    assert union["ansible"]["dockerDiscoveryEligible"] is True


def test_approval_restrictions_still_narrow_mandatory_capability(monkeypatch):
    seed_capabilities()
    monkeypatch.setattr(maintenance.threading, "Thread", NoStartThread)
    store.update(lambda document: document["ansibleControllers"]["primary"]["playbooks"]
                 ["os_check"].update(allowedTargets=["debian-only"]))

    union = compute.public_instance(store.load()["computeInstances"]["union-host"])

    assert union["ansible"]["capabilities"]["osMaintenance"] is True
    assert union["ansible"]["updateCheckEligible"] is False
    with pytest.raises(ValueError, match="outside this playbook's approved targets"):
        maintenance.start_job("union-host", "os_check", "owner")


def test_forged_service_request_cannot_bypass_discovered_groups(monkeypatch):
    seed_capabilities()
    monkeypatch.setattr(maintenance.threading, "Thread", NoStartThread)
    monkeypatch.setattr(ansible, "refresh_inventory", lambda *_args, **_kwargs: {})
    actor = Actor("owner", Role.ADMIN)

    with pytest.raises(ValueError, match="docker_hosts"):
        services.compute_docker_discover(actor, "homeassistant")
    with pytest.raises(ValueError, match="debian_hosts"):
        services.compute_update(actor, "homeassistant")


def test_inventory_refresh_clears_only_incompatible_cached_state(monkeypatch):
    seed_capabilities()

    def add_stale(document):
        appliance = document["computeInstances"]["homeassistant"]
        appliance.update(
            updateState={"state": "failed"},
            dockerDiscoveryState={"state": "unreachable"},
            dockerUpdateState={"state": "failed"},
            docker={"available": False},
            applianceHealthState={"state": "available", "healthy": True},
        )
        debian = document["computeInstances"]["debian-only"]
        debian["updateState"] = {"state": "up_to_date", "lastCheckedAt": 10}
        document["computeJobs"]["old-invalid-job"] = {
            "id": "old-invalid-job", "computeInstanceId": "homeassistant",
            "operation": "docker_discovery", "state": "unreachable",
        }

    store.update(add_stale)
    controller = ansible.get_controller()
    payload = {
        "_meta": {"hostvars": {name: {} for name in controller["inventory"]["hosts"]}},
        "appliances": {"hosts": ["homeassistant"]},
        "debian_hosts": {"hosts": ["debian-only", "union-host"]},
        "docker_hosts": {"hosts": ["docker-only", "union-host"]},
    }
    monkeypatch.setattr(ansible, "controller_connection", lambda *_args: NullConnection())
    monkeypatch.setattr(ansible, "_run", lambda *_args, **_kwargs: (
        0, json.dumps(payload), ""))

    inventory = ansible.refresh_inventory()
    document = store.load()
    appliance = document["computeInstances"]["homeassistant"]

    assert next(host for host in inventory["hosts"]
                if host["name"] == "homeassistant")["groups"] == ["appliances"]
    assert "updateState" not in appliance
    assert "dockerDiscoveryState" not in appliance
    assert "dockerUpdateState" not in appliance
    assert "docker" not in appliance
    assert appliance["applianceHealthState"]["state"] == "available"
    assert document["computeInstances"]["debian-only"]["updateState"]["state"] == "up_to_date"
    assert document["computeJobs"]["old-invalid-job"]["state"] == "unreachable"


def test_homeassistant_health_success_is_available_and_secret_safe(monkeypatch):
    seed_capabilities()
    secret = "synthetic-home-assistant-token"
    vault_secret = "synthetic-vault-password"
    raw = (
        f'Authorization: Bearer {secret}\n'
        f'vault_password={vault_secret}\n'
        'homeassistant : ok=1 changed=0 unreachable=0 failed=0 skipped=0 '
        'rescued=0 ignored=0'
    )
    controller = ansible.get_controller()
    monkeypatch.setattr(maintenance.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(maintenance.ansible, "controller_connection",
                        lambda *_args: NullConnection())
    monkeypatch.setattr(maintenance.ansible, "_run", lambda *_args, **_kwargs: (
        0, ansible._redact(raw, controller), ""))

    job = maintenance.start_job("homeassistant", "appliance_health", "owner")
    document = store.load()
    public = compute.public_instance(document["computeInstances"]["homeassistant"], document)
    exposed = json.dumps({"job": maintenance.get_job(job["id"]), "instance": public,
                          "logs": list(logbuf.REQUEST_LOG)})

    assert public["applianceHealthState"]["state"] == "available"
    assert public["applianceHealthState"]["healthy"] is True
    assert "updateState" not in public and "dockerDiscoveryState" not in public
    assert secret not in exposed and vault_secret not in exposed
    assert "[REDACTED]" in exposed


def test_homeassistant_health_failure_is_separate_from_maintenance(monkeypatch):
    seed_capabilities()
    monkeypatch.setattr(maintenance.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(maintenance.ansible, "controller_connection",
                        lambda *_args: NullConnection())
    monkeypatch.setattr(maintenance.ansible, "_run", lambda *_args, **_kwargs: (
        2,
        "homeassistant : ok=0 changed=0 unreachable=0 failed=1 skipped=0 "
        "rescued=0 ignored=0",
        "ERROR: authenticated Home Assistant API returned HTTP 503",
    ))

    maintenance.start_job("homeassistant", "appliance_health", "owner")
    public = compute.public_instance(store.load()["computeInstances"]["homeassistant"])

    assert public["applianceHealthState"]["state"] == "failed"
    assert public["applianceHealthState"]["healthy"] is False
    assert "HTTP 503" in public["applianceHealthState"]["summary"]
    assert "updateState" not in public
    assert "dockerDiscoveryState" not in public

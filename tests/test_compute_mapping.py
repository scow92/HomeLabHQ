import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

import compute
import compute_maintenance as maintenance
import services
import store
from backend.api import ansible_routes
from context import Actor, Role
from errors import Forbidden


@pytest.fixture(autouse=True)
def isolated_store(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(store, "DB_FILE", str(tmp_path / "homelabhq.json"))
    monkeypatch.setattr(store, "LOCK_FILE", str(tmp_path / "homelabhq.lock"))
    store._cache.update(doc=None, mtime=None)


def seed_mapping_state(*, mapped=False):
    controller = {
        "id": "primary",
        "enabled": True,
        "inventoryPath": "/srv/ansible/inventory.yml",
        "playbooksDirectory": "/srv/ansible/playbooks",
        "ansiblePlaybookExecutable": "/usr/bin/ansible-playbook",
        "inventory": {
            "hosts": {
                "immich": {
                    "name": "immich",
                    "address": "192.0.2.60",
                    "groups": ["containers"],
                },
            },
            "groups": {},
        },
        "discoveredPlaybooks": ["os-check.yml", "docker-discover.yml"],
        "playbooks": {
            "os_check": {"playbook": "os-check.yml", "approved": True},
            "docker_discovery": {
                "playbook": "docker-discover.yml",
                "approved": True,
            },
        },
    }
    instance = {
        "id": "compute-1",
        "ownerId": "owner-1",
        "provider": "proxmox",
        "providerInstanceId": "101",
        "type": "lxc",
        # Deliberately different: rendering must not infer management by name.
        "name": "proxmox-workload-name",
        "status": "running",
        "updateState": {"state": "up_to_date", "lastCheckedAt": 10},
        "docker": {"available": True, "projects": [], "lastDiscoveredAt": 10},
    }
    if mapped:
        instance["ansible"] = {
            "enabled": True,
            "controllerId": "primary",
            "inventoryHost": "immich",
            "confirmedAt": 5,
        }

    def mutate(document):
        document["ansibleControllers"]["primary"] = controller
        document["computeInstances"]["compute-1"] = instance

    store.update(mutate)


def mapping_request(body, actor=None):
    return SimpleNamespace(
        body=body,
        params={"compute_id": "compute-1"},
        require_actor=lambda: actor or Actor("admin-1", Role.ADMIN),
    )


def test_save_mapping_persists_reloads_and_returns_managed_unknown_state():
    seed_mapping_state()

    response = ansible_routes.mapping(mapping_request({
        "enabled": True,
        "controllerId": "primary",
        "inventoryHost": "immich",
    }))

    serialized = response.value["instance"]
    assert serialized["ansible"]["enabled"] is True
    assert serialized["ansible"]["controllerId"] == "primary"
    assert serialized["ansible"]["inventoryHost"] == "immich"
    assert serialized["ansible"]["updateCheckEligible"] is True
    assert serialized["ansible"]["dockerDiscoveryEligible"] is True
    assert serialized["ansible"]["maintenance"] == {
        "osCheckOperation": "os_check",
        "osUpdateOperation": "os_update",
        "dockerDiscoveryEnabled": True,
        "dockerDiscoveryOperation": "docker_discovery",
        "dockerCheckOperation": "docker_check",
        "dockerUpdateOperation": "docker_update",
    }
    assert serialized["updateState"] == {"state": "unknown"}
    assert "docker" not in serialized

    # Force a disk read to prove the association survived the write/cache cycle.
    store._cache.update(doc=None, mtime=None)
    reloaded = services.compute_detail(Actor("owner-1", Role.MEMBER), "compute-1")
    assert reloaded["ansible"] == serialized["ansible"]
    persisted = store.load()["computeInstances"]["compute-1"]["ansible"]
    assert persisted["inventoryHost"] == "immich"
    assert persisted["confirmedAt"]


def test_unmapped_and_incomplete_mappings_serialize_as_ineligible():
    seed_mapping_state()
    record = store.load()["computeInstances"]["compute-1"]

    serialized = compute.public_instance(record)["ansible"]
    assert serialized["enabled"] is False
    assert serialized["controllerId"] is None
    assert serialized["inventoryHost"] is None
    assert serialized["updateCheckEligible"] is False
    assert serialized["updateEligible"] is False
    assert serialized["dockerDiscoveryEligible"] is False
    assert serialized["dockerUpdateCheckEligible"] is False
    assert serialized["dockerUpdateModes"] == []
    assert serialized["maintenanceActive"] is False

    record["ansible"] = {"enabled": True, "controllerId": "primary"}
    assert compute.public_instance(record)["ansible"]["enabled"] is False


def test_compute_serialization_reports_active_update_jobs():
    seed_mapping_state(mapped=True)

    def queue_update(document):
        document["computeJobs"]["job-1"] = {
            "id": "job-1", "computeInstanceId": "compute-1",
            "operation": "os_update", "state": "queued",
        }

    store.update(queue_update)
    record = store.load()["computeInstances"]["compute-1"]
    serialized = compute.public_instance(record)["ansible"]

    assert serialized["maintenanceActive"] is True


@pytest.mark.parametrize("body", [
    {},
    {"enabled": "true", "controllerId": "primary", "inventoryHost": "immich"},
    {"enabled": False, "controllerId": "primary", "inventoryHost": "immich"},
])
def test_mapping_request_rejects_missing_or_contradictory_state(body):
    seed_mapping_state()
    with pytest.raises(ValueError, match="boolean|must not include"):
        ansible_routes.mapping(mapping_request(body))
    assert "ansible" not in store.load()["computeInstances"]["compute-1"]


def test_mapping_save_remains_administrator_only():
    seed_mapping_state()
    with pytest.raises(Forbidden, match="admin only"):
        ansible_routes.mapping(mapping_request({
            "enabled": True,
            "controllerId": "primary",
            "inventoryHost": "immich",
        }, Actor("owner-1", Role.MEMBER)))


class NoStartThread:
    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        pass


@pytest.mark.parametrize("operation", ["os_check", "docker_discovery"])
def test_persisted_mapping_enables_update_and_docker_discovery_jobs(monkeypatch, operation):
    seed_mapping_state(mapped=True)
    monkeypatch.setattr(maintenance.threading, "Thread", NoStartThread)

    job = maintenance.start_job("compute-1", operation, "owner-1")

    assert job["operation"] == operation
    assert job["ansibleTarget"] == "immich"


@pytest.mark.parametrize("operation", ["os_check", "docker_discovery"])
def test_unmapped_workload_is_ineligible_for_maintenance_jobs(operation):
    seed_mapping_state(mapped=False)
    with pytest.raises(ValueError, match="not managed by Ansible"):
        maintenance.start_job("compute-1", operation, "owner-1")

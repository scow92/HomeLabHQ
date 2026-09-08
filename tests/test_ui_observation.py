"""M05's additive device metadata is a read-only, authorized projection."""
import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import devices
import poller
import services
import store
import pytest
from context import Actor, Role


@pytest.mark.parametrize("driver,expected", [("generic.http", 321), ("proxmox.ve", 654), ("truenas.system", 987)])
def test_device_observation_policy_preserves_owner_scope_without_io(monkeypatch, driver, expected):
    records = {
        owner: {"id": owner, "ownerId": owner, "host": "192.0.2.10",
                "transport": "http", "driverId": driver,
                "credRef": "fictional-private-reference", "state": {"online": True, "ts": 123}}
        for owner in ["alice", "bob"]
    }
    original = copy.deepcopy(records)
    monkeypatch.setattr(store, "load", lambda: {"devices": records})
    monkeypatch.setattr(poller, "STALE_THRESHOLDS", {"network": 321, "proxmox": 654, "truenas": 987})
    monkeypatch.setattr(services.compute, "list_instances", lambda *_a, **_kw: [])
    def forbidden(*_a, **_kw):
        raise AssertionError("projection must not connect or mutate")
    monkeypatch.setattr(store, "update", forbidden)
    monkeypatch.setattr(devices.transports, "open_connection", forbidden)
    member = services.list_devices(Actor("alice", Role.MEMBER))
    assert [item["id"] for item in member] == ["alice"]
    admin = services.list_devices(Actor("admin", Role.ADMIN))
    assert {item["id"] for item in admin} == {"alice", "bob"}
    for item in member + admin:
        assert item["monitoringStaleAfterSeconds"] == expected
        assert item["state"] == {"online": True, "ts": 123}
        assert "credRef" not in item
    assert records == original

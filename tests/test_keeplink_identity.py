import sys
from copy import deepcopy
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import services
import store
from context import Actor, Role
from drivers import keeplink


MAC = "AA:BB:CC:DD:EE:01"
UNKNOWN_MAC = "AA:BB:CC:DD:EE:02"


def configure_store(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(store, "DB_FILE", str(tmp_path / "homelabhq.json"))
    monkeypatch.setattr(store, "LOCK_FILE", str(tmp_path / "homelabhq.lock"))
    store._cache.update(doc=None, mtime=None)


def test_keeplink_learned_mac_table_requests_client_identity(monkeypatch):
    monkeypatch.setattr(keeplink, "_snapshot", lambda connection: {
        "port": "", "poe_port": "", "poe_sys": "", "stats": "",
        "mac": (
            "<table><tr><td>10</td><td>AA:BB:CC:DD:EE:01</td>"
            "<td>1</td><td>dynamic</td><td>Port 1</td></tr></table>"
        ),
        "info": "",
    })

    detail = keeplink.KeeplinkSwitch().detail(object())
    table = detail["tables"][0]

    assert table["clientIdentity"] is True
    assert [column["key"] for column in table["columns"]] == [
        "mac", "hostname", "ip", "vlan", "port"]
    assert table["rows"][0]["mac"] == MAC


def test_device_detail_enriches_only_marked_tables_from_device_owner_roster(
        monkeypatch, tmp_path):
    configure_store(monkeypatch, tmp_path)
    store.update(lambda document: document["devices"].update({
        "alice-switch": {"id": "alice-switch", "ownerId": "alice"},
    }))
    raw_detail = {
        "detail": {"tables": [
            {
                "title": "Learned MACs (2)",
                "clientIdentity": True,
                "columns": [],
                "rows": [{"mac": MAC}, {"mac": UNKNOWN_MAC}],
            },
            {
                "title": "Unrelated table",
                "columns": [],
                "rows": [{"mac": MAC}],
            },
        ]},
    }
    monkeypatch.setattr(services.devices, "read_detail",
                        lambda device_id: deepcopy(raw_detail))
    roster_reads = []

    def read_snapshot(owner_id):
        roster_reads.append(owner_id)
        return {"clients": [{
            "mac": MAC.lower(), "hostname": "media-server", "ip": "192.0.2.20",
        }]}

    monkeypatch.setattr(services.client_roster, "read_snapshot", read_snapshot)

    result = services.device_detail(Actor("admin", Role.ADMIN), "alice-switch")
    learned, unrelated = result["detail"]["tables"]

    assert roster_reads == ["alice"]
    assert "clientIdentity" not in learned
    assert learned["rows"] == [
        {"mac": MAC, "hostname": "media-server", "ip": "192.0.2.20"},
        {"mac": UNKNOWN_MAC, "hostname": "", "ip": ""},
    ]
    assert unrelated["rows"] == [{"mac": MAC}]

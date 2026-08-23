import sys
from copy import deepcopy
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import services
import store
from context import Actor, Role
from drivers import zyxel_ap


MAC = "AA:BB:CC:DD:EE:01"
UNKNOWN_MAC = "AA:BB:CC:DD:EE:02"


def configure_store(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(store, "DB_FILE", str(tmp_path / "homelabhq.json"))
    monkeypatch.setattr(store, "LOCK_FILE", str(tmp_path / "homelabhq.lock"))
    store._cache.update(doc=None, mtime=None)


def test_zyxel_client_table_requests_roster_identity_enrichment(monkeypatch):
    monkeypatch.setattr(zyxel_ap, "_snapshot", lambda connection: {
        "clients": {"_Slot1": "1", "_Slot2": "0"},
        "channel": {"_Slot1": "6", "_Slot2": "36"},
        "stations": [{
            "_MAC": MAC,
            "_IPv4": "192.0.2.10",
            "_Band": "2.4 GHz",
            "_SSID": "Homelab",
        }],
    })
    monkeypatch.setattr(zyxel_ap, "_hostnames", lambda ips: {})

    detail = zyxel_ap.ZyxelAP().detail(object())
    table = detail["tables"][1]

    assert table["clientIdentity"] is True
    assert table["rows"][0] == {
        "client": "192.0.2.10",
        "ip": "192.0.2.10",
        "mac": MAC,
        "band": "2.4 GHz",
        "ssid": "Homelab",
        "phy": "",
        "rssi": None,
        "tx": "",
        "rx": "",
    }


def test_device_detail_uses_roster_hostname_for_zyxel_client_label(monkeypatch, tmp_path):
    configure_store(monkeypatch, tmp_path)
    store.update(lambda document: document["devices"].update({
        "alice-ap": {"id": "alice-ap", "ownerId": "alice"},
    }))
    raw_detail = {
        "detail": {"tables": [{
            "title": "Connected clients (2)",
            "clientIdentity": True,
            "columns": [],
            "rows": [
                {"mac": MAC, "client": "192.0.2.10", "ip": "192.0.2.10"},
                {"mac": UNKNOWN_MAC, "client": "192.0.2.30", "ip": "192.0.2.30"},
            ],
        }]},
    }
    monkeypatch.setattr(services.devices, "read_detail",
                        lambda device_id: deepcopy(raw_detail))
    monkeypatch.setattr(services.client_roster, "read_snapshot", lambda owner_id: {
        "clients": [{
            "mac": MAC.lower(), "hostname": "media-server", "ip": "192.0.2.20",
        }],
    })

    result = services.device_detail(Actor("admin", Role.ADMIN), "alice-ap")
    table = result["detail"]["tables"][0]

    assert "clientIdentity" not in table
    assert table["rows"] == [
        {"mac": MAC, "client": "media-server", "hostname": "media-server",
         "ip": "192.0.2.20"},
        {"mac": UNKNOWN_MAC, "client": "192.0.2.30", "hostname": "",
         "ip": "192.0.2.30"},
    ]

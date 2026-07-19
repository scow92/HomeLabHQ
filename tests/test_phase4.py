import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

import client_merge
import client_roster
import client_service
import store
from context import Actor, Role
from drivers.opnsense import OPNsense


def configure_store(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(store, "DB_FILE", str(tmp_path / "homelabhq.json"))
    monkeypatch.setattr(store, "LOCK_FILE", str(tmp_path / "homelabhq.lock"))
    store._cache.update(doc=None, mtime=None)


def test_merge_is_pure_and_prefers_authoritative_hostname():
    observations = [
        client_merge.ClientObservation("aa:bb", "switch", "Switch", hostname="first"),
        client_merge.ClientObservation("AA:BB", "dhcp", "Router", hostname="lease",
                                       hostname_authoritative=True, kind="wifi", signal=-55),
    ]
    merged = client_merge.merge_observations(observations)
    assert merged == [{"mac": "AA:BB", "ip": "", "hostname": "lease", "vendor": "",
                       "kind": "wifi", "signal": -55,
                       "seen": [{"via": "Switch", "where": "", "kind": "wired", "signal": None},
                                {"via": "Router", "where": "", "kind": "wifi", "signal": -55}]}]


def test_list_clients_reads_snapshot_without_discovery(monkeypatch, tmp_path):
    configure_store(monkeypatch, tmp_path)
    client_roster.record_observations("alice", [{"mac": "AA:BB:CC:DD:EE:01", "seen": []}])
    monkeypatch.setattr(client_service.client_discovery, "discover",
                        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected discovery")))
    snapshot = client_service.list_clients(Actor("alice", Role.MEMBER))
    assert [client["mac"] for client in snapshot["clients"]] == ["AA:BB:CC:DD:EE:01"]


def test_roster_snapshot_is_owner_scoped(monkeypatch, tmp_path):
    configure_store(monkeypatch, tmp_path)
    client_roster.record_observations("alice", [{"mac": "AA:BB:CC:DD:EE:01", "seen": []}])
    assert client_roster.read_snapshot("bob")["clients"] == []


def test_alias_membership_reconciles_previously_tracked_client(monkeypatch, tmp_path):
    configure_store(monkeypatch, tmp_path)
    mac = "AA:BB:CC:DD:EE:01"
    client_roster.record_observations("alice", [{"mac": mac, "seen": []}], approved=set())
    # The client is absent from this discovery, but it is still in OPNsense's
    # allow-list. That alias membership is authoritative.
    client_roster.record_observations("alice", [], approved={mac})
    assert client_roster.read_snapshot("alice")["clients"][0]["nac"] == "approved"


def test_opnsense_reads_string_selected_alias_entries():
    assert OPNsense._parse_members({"AA:BB:CC:DD:EE:01": {"selected": "1"}}) == [
        "AA:BB:CC:DD:EE:01"]

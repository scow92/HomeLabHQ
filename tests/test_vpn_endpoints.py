import base64
import contextlib
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import devices
import nordvpn_client
import rdap_client
import services
import store
import vpn_endpoint_service as service
from context import Actor, Role
from drivers.opnsense import OPNsense
from errors import NotFound


KEY_A = base64.b64encode(b"a" * 32).decode()
KEY_B = base64.b64encode(b"b" * 32).decode()


def configure_store(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(store, "DB_FILE", str(tmp_path / "homelabhq.json"))
    monkeypatch.setattr(store, "LOCK_FILE", str(tmp_path / "homelabhq.lock"))
    store._cache.update(doc=None, mtime=None)


def nord_row(ip="192.0.2.10", key=KEY_A):
    return {"hostname": "uk-test.nordvpn.com", "station": ip, "load": 17,
            "locations": [{"country": {"name": "United Kingdom", "city": {"name": "London"}}}],
            "technologies": [{"identifier": "wireguard_udp", "metadata": [{"name": "public_key", "value": key}]}]}


def test_nordvpn_parser_rejects_malformed_and_duplicate_candidates():
    rows = [nord_row(), nord_row("192.0.2.10", KEY_B), nord_row("192.0.2.11", KEY_A),
            {"hostname": "bad", "station": "not-an-ip", "technologies": []},
            nord_row("2001:db8::5", KEY_B)]
    result = nordvpn_client.parse_candidates(rows, 100)
    assert [(x.endpoint_ip, x.endpoint_port, x.city) for x in result] == [
        ("192.0.2.10", 51820, "London"), ("2001:db8::5", 51820, "London")]


def test_nordvpn_parser_requires_wireguard_metadata():
    row = nord_row()
    row["technologies"] = [{"identifier": "openvpn_udp", "metadata": [{"value": KEY_A}]}]
    assert nordvpn_client.parse_candidates([row]) == []


def test_rdap_parser_and_safe_unknown_response():
    payload = {"startAddress": "192.0.2.0", "endAddress": "192.0.2.255",
               "entities": [{"roles": ["registrant"], "vcardArray": ["vcard", [["fn", {}, "text", "Example Hosting Ltd"]]]}],
               "arin_originas0_originautnums": ["AS12345"]}
    ownership = rdap_client.parse_ownership("192.0.2.10", payload, 50)
    assert ownership.organisation == "Example Hosting Ltd"
    assert ownership.asn == "12345"
    assert ownership.cidr == "192.0.2.0/24"
    assert rdap_client.Ownership("192.0.2.1", None, "", "", "", "rdap.org", 1, "unknown").owner == ""


def test_fixed_clients_use_local_mock_http_services(monkeypatch):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            if self.path.startswith("/v1/servers/countries"):
                body = [{"id": 1, "name": "United Kingdom"}]
            elif self.path.startswith("/v1/servers/recommendations"):
                body = [nord_row()]
            elif self.path.startswith("/ip/"):
                body = {"entities": [{"roles": ["registrant"], "vcardArray": ["vcard", [["fn", {}, "text", "Example Hosting"]]]}]}
            else:
                self.send_response(404); self.end_headers(); return
            raw = json.dumps(body).encode(); self.send_response(200)
            self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(raw)))
            self.end_headers(); self.wfile.write(raw)
    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever); thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setattr(nordvpn_client, "API_ORIGIN", origin)
    monkeypatch.setattr(rdap_client, "RDAP_ORIGIN", origin)
    try:
        candidates = nordvpn_client.NordVPNClient().discover("United Kingdom", 2)
        owner = rdap_client.RDAPClient().lookup(candidates[0].endpoint_ip)
    finally:
        server.shutdown(); server.server_close(); thread.join()
    assert candidates[0].endpoint_ip == "192.0.2.10"
    assert owner.organisation == "Example Hosting"


def test_owner_patterns_are_whole_word_case_insensitive():
    profile = service._profile({"preferredOwners": ["Example Hosting"],
                                "excludedOwners": ["Other Network"]})
    assert service._classification("EXAMPLE HOSTING Europe", profile) == "Preferred"
    assert service._classification("Other Network Ltd", profile) == "Excluded"
    assert service._classification("NotOther Network", profile) == "Eligible"
    assert service._classification("", profile) == "Unknown"


def test_new_profile_defaults_are_neutral():
    profile = service._profile({})
    assert profile["preferredOwners"] == []
    assert profile["excludedOwners"] == []
    assert profile["compatibilityTargets"] == []


def test_profile_history_is_owner_scoped_and_bounded(monkeypatch, tmp_path):
    configure_store(monkeypatch, tmp_path)
    store.update(lambda doc: doc["devices"].update({"a": {"id": "a", "ownerId": "alice", "driverId": "opnsense.firewall"},
                                                       "b": {"id": "b", "ownerId": "bob", "driverId": "opnsense.firewall"}}))
    profile = service.configure("alice", "a", {
        "enabled": False, "notes": "Manually managed endpoint",
        "preferredOwners": ["Example Hosting"], "excludedOwners": ["Other Network"],
    })
    assert profile["country"] == "United Kingdom"
    updated = service.configure("alice", "a", {"notes": "Updated note"})
    assert updated["preferredOwners"] == ["Example Hosting"]
    assert updated["excludedOwners"] == ["Other Network"]
    with pytest.raises(ValueError):
        service.configure("bob", "a", {})
    for n in range(service.MAX_HISTORY + 10):
        candidate = {"candidateId": str(n), "endpointIp": f"192.0.2.{n % 250 + 1}", "hostname": str(n),
                     "publicKey": KEY_A + str(n), "classification": "Preferred"}
        service._save_discovery("alice", "a", profile, [candidate])
    doc = store.load()
    assert len(doc["vpnEndpointHistory"]["alice"]["a"]["candidates"]) == service.MAX_HISTORY
    assert "bob" not in doc["vpnEndpointHistory"]


def test_legacy_validation_and_classification_are_normalised_without_data_loss(monkeypatch, tmp_path):
    configure_store(monkeypatch, tmp_path)
    def seed(doc):
        doc["devices"]["a"] = {
            "id": "a", "ownerId": "alice", "driverId": "opnsense.firewall",
            "vpnEndpointProfile": {"enabled": False, "rejectedOwners": ["Saved Network"]},
        }
        doc["vpnEndpointHistory"]["alice"] = {"a": {
            "candidates": [{
                "candidateId": "legacy", "endpointIp": "192.0.2.10",
                "classification": "Rejected", "compatibility": "Verified",
                "compatibilityAt": 1_700_000_000, "compatibilityNote": "Manual check",
            }],
            "lastCandidates": [{
                "candidateId": "legacy", "endpointIp": "192.0.2.10",
                "endpointPort": 51820, "hostname": "uk-test.nordvpn.com",
                "classification": "Rejected", "publicKey": KEY_A,
            }],
            "events": [], "state": {}, "lastDiscovery": 1_700_000_000,
        }}
    store.update(seed)

    snapshot = service.status("alice", "a")
    assert snapshot["profile"]["excludedOwners"] == ["Saved Network"]
    assert "rejectedOwners" not in snapshot["profile"]
    assert snapshot["profile"]["compatibilityTargets"][0]["name"] == "Imported compatibility check"
    candidate = snapshot["discovery"]["candidates"][0]
    assert candidate["classification"] == "Excluded"
    assert candidate["compatibilityTargets"][0] == {
        "id": service.IMPORTED_TARGET_ID,
        "name": "Imported compatibility check",
        "description": "Imported from compatibility data saved by an earlier version.",
        "state": "Verified", "lastValidatedAt": 1_700_000_000, "note": "Manual check",
    }

    service.configure("alice", "a", {"notes": "Preserve migrated values"})
    saved = store.load()
    saved_profile = saved["devices"]["a"]["vpnEndpointProfile"]
    saved_candidate = saved["vpnEndpointHistory"]["alice"]["a"]["candidates"][0]
    assert saved_profile["excludedOwners"] == ["Saved Network"]
    assert "rejectedOwners" not in saved_profile
    assert saved_candidate["validations"][service.IMPORTED_TARGET_ID]["state"] == "Verified"
    assert all(key not in saved_candidate for key in
               ("compatibility", "compatibilityAt", "compatibilityNote"))


def test_unknown_legacy_placeholder_does_not_create_imported_target(monkeypatch, tmp_path):
    configure_store(monkeypatch, tmp_path)
    store.update(lambda doc: (
        doc["devices"].update({"a": {"id": "a", "ownerId": "alice",
                                             "driverId": "opnsense.firewall"}}),
        doc["vpnEndpointHistory"].update({"alice": {"a": {
            "candidates": [{"candidateId": "old", "compatibility": "Unknown"}],
            "lastCandidates": [], "events": [], "state": {}, "lastDiscovery": None,
        }}}),
    ))
    snapshot = service.status("alice", "a")
    assert snapshot["profile"]["compatibilityTargets"] == []


def test_numeric_profile_values_are_strictly_bounded(monkeypatch, tmp_path):
    configure_store(monkeypatch, tmp_path)
    store.update(lambda doc: doc["devices"].update({
        "a": {"id": "a", "ownerId": "alice", "driverId": "opnsense.firewall"}}))
    for patch in ({"maxCandidates": "20"}, {"maxCandidates": 0},
                  {"discoveryIntervalSeconds": 299}, {"handshakeWarningSeconds": 86401}):
        with pytest.raises(ValueError):
            service.configure("alice", "a", patch)


def test_owner_cannot_configure_another_owners_vpn_profile(monkeypatch, tmp_path):
    configure_store(monkeypatch, tmp_path)
    store.update(lambda doc: doc["devices"].update({"a": {"id": "a", "ownerId": "alice", "driverId": "opnsense.firewall"}}))
    with pytest.raises(NotFound):
        services.vpn_endpoint_configure(Actor("bob", Role.MEMBER), "a", {"enabled": False})


def test_candidate_validations_remain_owner_scoped_even_with_identical_ids(monkeypatch, tmp_path):
    configure_store(monkeypatch, tmp_path)
    store.update(lambda doc: doc["devices"].update({
        "a": {"id": "a", "ownerId": "alice", "driverId": "opnsense.firewall"},
        "b": {"id": "b", "ownerId": "bob", "driverId": "opnsense.firewall"},
    }))
    target = {"id": "shared-looking-id", "name": "Corporate portal"}
    for owner, device_id in (("alice", "a"), ("bob", "b")):
        profile = service.configure(owner, device_id, {"compatibilityTargets": [target]})
        service._save_discovery(owner, device_id, profile, [{
            "candidateId": "same-candidate", "endpointIp": "192.0.2.10",
            "endpointPort": 51820, "hostname": "uk-test.nordvpn.com",
            "publicKey": KEY_A, "classification": "Eligible",
        }])
    service.set_validation(
        "alice", "a", "same-candidate", "shared-looking-id", "Verified", "Works for Alice")
    saved = store.load()["vpnEndpointHistory"]
    assert saved["alice"]["a"]["candidates"][0]["validations"]["shared-looking-id"]["state"] == "Verified"
    assert saved["bob"]["b"]["candidates"][0]["validations"] == {}


def test_new_status_response_contains_no_installation_specific_terms(monkeypatch, tmp_path):
    configure_store(monkeypatch, tmp_path)
    store.update(lambda doc: doc["devices"].update({
        "a": {"id": "a", "ownerId": "alice", "driverId": "opnsense.firewall"}}))
    response = json.dumps(service.status("alice", "a")).casefold()
    for term in ("ring", "packethub", "hydra communications", '"rejected"'):
        assert term not in response


def test_store_migrates_existing_v1_documents_for_vpn_history(monkeypatch, tmp_path):
    configure_store(monkeypatch, tmp_path)
    Path(store.DB_FILE).parent.mkdir(parents=True, exist_ok=True)
    Path(store.DB_FILE).write_text(json.dumps({"schemaVersion": 1, "users": {}, "sessions": {}, "devices": {},
        "dashboards": {}, "credentials": {}, "push_subs": {}, "meta": {}, "sshHostKeys": {}, "clientRosters": {}}))
    doc = store.load()
    assert doc["schemaVersion"] == 2 and doc["vpnEndpointHistory"] == {}


def test_opnsense_wireguard_driver_uses_documented_controller_routes():
    class Response:
        status = 200
        def __init__(self, body): self.body = body
        def json(self): return self.body
    class Connection:
        def __init__(self): self.calls = []
        def request(self, method, path, **kwargs):
            self.calls.append((method, path, kwargs))
            if path.endswith("setClient/peer"): return Response({"result": "saved"})
            if path.endswith("reconfigure"): return Response({"result": "ok"})
            return Response({"rows": []})
        def get(self, path):
            self.calls.append(("GET", path, {}))
            if path.endswith("getClient/peer"): return Response({"client": {"pubkey": KEY_A}})
            if path.endswith("service/show"): return Response({"rows": []})
            return Response({})
    conn = Connection(); driver = OPNsense()
    assert driver.wireguard_peer(conn, "peer")["pubkey"] == KEY_A
    driver.wireguard_update_peer(conn, "peer", {"pubkey": KEY_B})
    driver.wireguard_reconfigure(conn)
    paths = [call[1] for call in conn.calls]
    assert "/api/wireguard/client/getClient/peer" in paths
    assert "/api/wireguard/client/setClient/peer" in paths
    assert "/api/wireguard/service/reconfigure" in paths


class FakeDriver:
    def __init__(self, handshake=True, apply_fails=False):
        self.peer = {"name": "peer", "servers": "instance", "serveraddress": "192.0.2.10", "serverport": "51820", "pubkey": KEY_A, "psk": "sensitive"}
        self.handshake, self.apply_fails, self.saved = handshake, apply_fails, []
    def wireguard_peer(self, conn, uuid): return dict(self.peer)
    def gateway(self, conn, uuid): return None
    def wireguard_update_peer(self, conn, uuid, peer): self.peer = dict(peer); self.saved.append(dict(peer))
    def wireguard_reconfigure(self, conn):
        if self.apply_fails: raise ValueError("apply failed")
    def wireguard_status(self, conn, peer):
        return {"latestHandshake": 9_999_999_999 if self.handshake else None, "handshakeAge": 0 if self.handshake else None}


def test_switch_updates_key_and_rolls_back_on_failed_verification(monkeypatch, tmp_path):
    configure_store(monkeypatch, tmp_path)
    store.update(lambda doc: doc["devices"].update({"a": {"id": "a", "ownerId": "alice", "driverId": "opnsense.firewall",
        "vpnEndpointProfile": {"enabled": True, "peerUuid": "peer", "instanceUuid": "instance"}}}))
    profile = service._profile({"enabled": True, "peerUuid": "peer", "instanceUuid": "instance"})
    candidate = {"candidateId": "new", "endpointIp": "192.0.2.11", "endpointPort": 51820, "publicKey": KEY_B,
                 "classification": "Preferred", "hostname": "new"}
    service._save_discovery("alice", "a", profile, [candidate])
    driver = FakeDriver(handshake=True)
    @contextlib.contextmanager
    def connected(*args, **kwargs): yield {}, driver, object()
    monkeypatch.setattr(devices, "device_conn", connected)
    result = service.switch("alice", "a", "new", True)
    assert result["ok"] is True and driver.peer["pubkey"] == KEY_B
    driver = FakeDriver(handshake=False)
    monkeypatch.setattr(devices, "device_conn", connected)
    monkeypatch.setattr(service.time, "sleep", lambda _: None)
    result = service.switch("alice", "a", "new", True)
    assert result["ok"] is False and driver.peer["serveraddress"] == "192.0.2.10"
    assert all("sensitive" not in str(event) for event in store.load()["vpnEndpointHistory"]["alice"]["a"]["events"])

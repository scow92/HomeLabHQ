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
import logbuf
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


def test_rdap_parser_prefers_organisation_over_registry_maintainer():
    payload = {
        "name": "EXAMPLE-NETWORK",
        "entities": [
            {"handle": "EXAMPLE-MNT", "roles": ["registrant"],
             "vcardArray": ["vcard", [["fn", {}, "text", "EXAMPLE-MNT"],
                                        ["kind", {}, "text", "individual"]]]},
            {"handle": "ORG-EH1-RIPE", "roles": ["registrant"],
             "vcardArray": ["vcard", [["fn", {}, "text", "Example Hosting S.A."],
                                        ["kind", {}, "text", "org"]]]},
        ],
    }
    ownership = rdap_client.parse_ownership("192.0.2.10", payload, 50)
    assert ownership.organisation == "Example Hosting S.A."


def test_rdap_client_follows_only_bounded_approved_registry_redirects(monkeypatch):
    class Response:
        def __init__(self, status, *, location="", payload=None):
            self.status_code = status
            self.headers = {"Location": location} if location else {}
            self.payload = payload
            self.closed = False

        def iter_content(self, _size):
            yield json.dumps(self.payload).encode()

        def close(self):
            self.closed = True

    class Session:
        def __init__(self, *redirects):
            self.redirects = redirects
            self.urls = []
            self.responses = []

        def get(self, url, **_kwargs):
            self.urls.append(url)
            if len(self.urls) <= len(self.redirects):
                response = Response(302, location=self.redirects[len(self.urls) - 1])
            else:
                response = Response(200, payload={"entities": [{
                    "roles": ["registrant"],
                    "vcardArray": ["vcard", [["fn", {}, "text", "Example Hosting"]]],
                }]})
            self.responses.append(response)
            return response

    monkeypatch.setattr(rdap_client, "RDAP_REGISTRY_HOSTS",
                        frozenset({"registry-a.example", "registry-b.example"}))
    approved = Session("https://registry-a.example/ip/192.0.2.10",
                       "https://registry-b.example/ip/192.0.2.10")
    ownership = rdap_client.RDAPClient(session=approved).lookup("192.0.2.10")
    assert ownership.organisation == "Example Hosting"
    assert ownership.source == "registry-b.example"
    assert approved.urls == ["https://rdap.org/ip/192.0.2.10",
                             "https://registry-a.example/ip/192.0.2.10",
                             "https://registry-b.example/ip/192.0.2.10"]
    assert all(response.closed for response in approved.responses)

    blocked = Session("https://untrusted.example/ip/192.0.2.11")
    unknown = rdap_client.RDAPClient(session=blocked).lookup("192.0.2.11")
    assert unknown.status == "unknown"
    assert blocked.urls == ["https://rdap.org/ip/192.0.2.11"]
    assert blocked.responses[0].closed

    looped = Session("https://registry-a.example/ip/192.0.2.12",
                     "https://registry-a.example/ip/192.0.2.12")
    unknown = rdap_client.RDAPClient(session=looped).lookup("192.0.2.12")
    assert unknown.status == "unknown"
    assert looped.urls == ["https://rdap.org/ip/192.0.2.12",
                           "https://registry-a.example/ip/192.0.2.12"]
    assert all(response.closed for response in looped.responses)

    too_long = Session("https://registry-a.example/referral/1",
                       "https://registry-b.example/referral/2",
                       "https://registry-a.example/referral/3",
                       "https://registry-b.example/referral/4")
    unknown = rdap_client.RDAPClient(session=too_long).lookup("192.0.2.13")
    assert unknown.status == "unknown"
    assert too_long.urls == ["https://rdap.org/ip/192.0.2.13",
                             "https://registry-a.example/referral/1",
                             "https://registry-b.example/referral/2",
                             "https://registry-a.example/referral/3"]
    assert all(response.closed for response in too_long.responses)


def test_fixed_clients_use_local_mock_http_services(monkeypatch):
    rdap_requests = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            if self.path.startswith("/v1/servers/countries"):
                body = [{"id": 1, "name": "United Kingdom"}]
            elif self.path.startswith("/v1/servers/recommendations"):
                body = [nord_row()]
            elif self.path.startswith("/ip/"):
                rdap_requests.append(self.path)
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
        rdap = rdap_client.RDAPClient()
        owner = rdap.lookup(candidates[0].endpoint_ip)
        assert rdap.lookup(candidates[0].endpoint_ip) == owner
    finally:
        server.shutdown(); server.server_close(); thread.join()
    assert candidates[0].endpoint_ip == "192.0.2.10"
    assert owner.organisation == "Example Hosting"
    assert len(rdap_requests) == 1


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
    saved_profile = saved["devices"]["a"]["vpnEndpointProfiles"][0]
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


def test_multiple_profiles_are_independent_and_legacy_profile_is_preserved(monkeypatch, tmp_path):
    configure_store(monkeypatch, tmp_path)
    store.update(lambda doc: doc["devices"].update({"a": {
        "id": "a", "ownerId": "alice", "driverId": "opnsense.firewall",
        "vpnEndpointProfile": {"enabled": False, "country": "United Kingdom",
                               "peerUuid": "uk-peer", "instanceUuid": "uk-instance",
                               "preferredOwners": ["UK Network"]},
    }}))

    legacy = service.statuses("alice", "a")["profiles"][0]["profile"]
    assert legacy["id"] == service.LEGACY_PROFILE_ID
    assert legacy["peerUuid"] == "uk-peer"
    assert legacy["preferredOwners"] == ["UK Network"]

    netherlands = service.configure("alice", "a", {
        "name": "Netherlands", "enabled": False, "country": "Netherlands",
        "city": "Amsterdam", "peerUuid": "nl-peer", "instanceUuid": "nl-instance",
        "excludedOwners": ["NL Excluded"],
        "compatibilityTargets": [{"id": "portal", "name": "Portal"}],
    }, create=True)
    service.configure("alice", "a", {"name": "United Kingdom", "notes": "UK notes"},
                      profile_id=legacy["id"])
    service._save_discovery("alice", "a", service._profile(legacy), [{
        "candidateId": "same", "endpointIp": "192.0.2.10", "endpointPort": 51820,
        "hostname": "uk-test.nordvpn.com", "publicKey": KEY_A, "classification": "Eligible",
    }])
    service._save_discovery("alice", "a", netherlands, [{
        "candidateId": "same", "endpointIp": "192.0.2.11", "endpointPort": 51820,
        "hostname": "nl-test.nordvpn.com", "publicKey": KEY_B, "classification": "Eligible",
    }])
    service.set_validation("alice", "a", "same", "portal", "Verified",
                           profile_id=netherlands["id"])

    snapshots = service.statuses("alice", "a")["profiles"]
    assert [item["profile"]["name"] for item in snapshots] == ["United Kingdom", "Netherlands"]
    assert snapshots[0]["history"][0]["endpointIp"] == "192.0.2.10"
    assert snapshots[1]["history"][0]["endpointIp"] == "192.0.2.11"
    assert snapshots[1]["history"][0]["compatibilityTargets"][0]["state"] == "Verified"
    saved = store.load()
    assert "vpnEndpointProfile" not in saved["devices"]["a"]
    assert len(saved["devices"]["a"]["vpnEndpointProfiles"]) == 2
    assert len(saved["vpnEndpointHistory"]["alice"]["a"]["candidates"]) == 2

    with pytest.raises(ValueError, match="already managed"):
        service.configure("alice", "a", {
            "name": "Duplicate peer", "enabled": False, "peerUuid": "uk-peer",
        }, create=True)
    with pytest.raises(ValueError, match="explicit confirmation"):
        service.remove_profile("alice", "a", netherlands["id"], False)
    service.remove_profile("alice", "a", netherlands["id"], True)
    remaining = store.load()
    assert [profile["name"] for profile in remaining["devices"]["a"]["vpnEndpointProfiles"]] == [
        "United Kingdom"]
    assert all(candidate["profileId"] == service.LEGACY_PROFILE_ID
               for candidate in remaining["vpnEndpointHistory"]["alice"]["a"]["candidates"])


def test_numeric_profile_values_are_strictly_bounded(monkeypatch, tmp_path):
    configure_store(monkeypatch, tmp_path)
    store.update(lambda doc: doc["devices"].update({
        "a": {"id": "a", "ownerId": "alice", "driverId": "opnsense.firewall"}}))
    for patch in ({"maxCandidates": "20"}, {"maxCandidates": 0},
                  {"discoveryIntervalSeconds": 299}, {"handshakeWarningSeconds": 86401}):
        with pytest.raises(ValueError):
            service.configure("alice", "a", patch)


def test_profile_count_is_bounded(monkeypatch, tmp_path):
    configure_store(monkeypatch, tmp_path)
    profiles = [service._profile({"name": f"Tunnel {index}"}, f"profile-{index}")
                for index in range(service.MAX_PROFILES)]
    store.update(lambda doc: doc["devices"].update({"a": {
        "id": "a", "ownerId": "alice", "driverId": "opnsense.firewall",
        "vpnEndpointProfiles": profiles,
    }}))

    with pytest.raises(ValueError, match=f"at most {service.MAX_PROFILES}"):
        service.configure("alice", "a", {"name": "One too many"}, create=True)


def test_target_removal_requires_confirmation_when_validation_history_exists(monkeypatch, tmp_path):
    configure_store(monkeypatch, tmp_path)
    store.update(lambda doc: doc["devices"].update({
        "a": {"id": "a", "ownerId": "alice", "driverId": "opnsense.firewall"}}))
    profile = service.configure("alice", "a", {
        "compatibilityTargets": [{"id": "portal", "name": "Corporate portal"}]})
    service._save_discovery("alice", "a", profile, [{
        "candidateId": "candidate", "endpointIp": "192.0.2.10", "endpointPort": 51820,
        "hostname": "uk-test.nordvpn.com", "publicKey": KEY_A, "classification": "Eligible",
    }])
    service.set_validation("alice", "a", "candidate", "portal", "Verified")
    with pytest.raises(ValueError, match="confirm removal"):
        service.configure("alice", "a", {"compatibilityTargets": []})
    updated = service.configure("alice", "a", {
        "compatibilityTargets": [], "confirmTargetRemoval": True})
    assert updated["compatibilityTargets"] == []
    assert store.load()["vpnEndpointHistory"]["alice"]["a"]["candidates"][0]["validations"] == {}


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
            if path.endswith("getClient/peer"):
                return Response({"client": {
                    "pubkey": KEY_A,
                    "servers": {
                        "instance-a": {"value": "WireGuard A", "selected": "1"},
                        "instance-b": {"value": "WireGuard B", "selected": 0},
                    },
                    "endpoint": "192.0.2.10:51820",
                }})
            if path.endswith("service/show"): return Response({"rows": []})
            return Response({})
    conn = Connection(); driver = OPNsense()
    peer = driver.wireguard_peer(conn, "peer")
    assert peer == {"pubkey": KEY_A, "servers": "instance-a"}
    driver.wireguard_update_peer(conn, "peer", peer)
    driver.wireguard_reconfigure(conn)
    paths = [call[1] for call in conn.calls]
    assert "/api/wireguard/client/getClient/peer" in paths
    assert "/api/wireguard/client/setClient/peer" in paths
    assert "/api/wireguard/service/reconfigure" in paths


class FakeDriver:
    def __init__(self, handshake=True, apply_fails=False, gateway=None, rollback_fails=False,
                 rollback_handshake=True):
        self.peer = {"name": "peer", "servers": "instance", "serveraddress": "192.0.2.10", "serverport": "51820", "pubkey": KEY_A, "psk": "sensitive"}
        self.handshake, self.apply_fails, self.saved = handshake, apply_fails, []
        self.gateway_value = dict(gateway) if gateway else None
        self.gateway_saved = []
        self.rollback_fails = rollback_fails
        self.rollback_handshake = rollback_handshake
        self.requested_peers = []
    def wireguard_peer(self, conn, uuid):
        self.requested_peers.append(uuid)
        return dict(self.peer)
    def gateway(self, conn, uuid): return dict(self.gateway_value) if self.gateway_value else None
    def wireguard_update_peer(self, conn, uuid, peer):
        if self.rollback_fails and self.saved: raise ValueError("rollback failed")
        self.peer = dict(peer); self.saved.append(dict(peer))
    def gateway_update(self, conn, uuid, gateway):
        self.gateway_value = dict(gateway); self.gateway_saved.append(dict(gateway))
    def wireguard_reconfigure(self, conn):
        if self.apply_fails: raise ValueError("apply failed")
    def wireguard_status(self, conn, peer):
        restored = (self.rollback_handshake and len(self.saved) >= 2
                    and peer.get("serveraddress") == "192.0.2.10")
        healthy = self.handshake or restored
        return {"latestHandshake": 9_999_999_999 if healthy else None,
                "handshakeAge": 0 if healthy else None}


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
    driver = FakeDriver(handshake=True, apply_fails=True)
    monkeypatch.setattr(devices, "device_conn", connected)
    result = service.switch("alice", "a", "new", True)
    assert result["ok"] is False and result["rollback"] is False
    assert driver.peer["serveraddress"] == "192.0.2.10"


def test_switch_is_scoped_to_the_selected_profile_peer(monkeypatch, tmp_path):
    configure_store(monkeypatch, tmp_path)
    store.update(lambda doc: doc["devices"].update({"a": {
        "id": "a", "ownerId": "alice", "driverId": "opnsense.firewall",
        "vpnEndpointProfiles": [
            service._profile({"name": "United Kingdom", "enabled": False}, "uk"),
            service._profile({"name": "Netherlands", "enabled": True,
                              "peerUuid": "nl-peer", "instanceUuid": "instance"}, "nl"),
        ],
    }}))
    profile = service._profile({"name": "Netherlands", "enabled": True,
                                "peerUuid": "nl-peer", "instanceUuid": "instance"}, "nl")
    service._save_discovery("alice", "a", profile, [{
        "candidateId": "nl-new", "endpointIp": "192.0.2.21", "endpointPort": 51820,
        "publicKey": KEY_B, "classification": "Eligible", "hostname": "nl-new",
    }])
    driver = FakeDriver(handshake=True)
    @contextlib.contextmanager
    def connected(*args, **kwargs): yield {}, driver, object()
    monkeypatch.setattr(devices, "device_conn", connected)

    result = service.switch("alice", "a", "nl-new", True, profile_id="nl")
    assert result["ok"] is True
    assert driver.requested_peers == ["nl-peer"]
    assert driver.peer["serveraddress"] == "192.0.2.21"


def test_switch_leaves_configuration_unchanged_when_peer_instance_does_not_match(monkeypatch, tmp_path):
    configure_store(monkeypatch, tmp_path)
    logbuf.REQUEST_LOG.clear()
    store.update(lambda doc: doc["devices"].update({"a": {
        "id": "a", "ownerId": "alice", "driverId": "opnsense.firewall",
        "vpnEndpointProfile": {"enabled": True, "peerUuid": "peer",
                               "instanceUuid": "different-instance"},
    }}))
    profile = service._profile({"enabled": True, "peerUuid": "peer",
                                "instanceUuid": "different-instance"})
    service._save_discovery("alice", "a", profile, [{
        "candidateId": "new", "endpointIp": "192.0.2.11", "endpointPort": 51820,
        "publicKey": KEY_B, "classification": "Eligible", "hostname": "new",
    }])
    driver = FakeDriver(handshake=True)
    @contextlib.contextmanager
    def connected(*args, **kwargs): yield {}, driver, object()
    monkeypatch.setattr(devices, "device_conn", connected)

    result = service.switch("alice", "a", "new", True)
    assert result["ok"] is False
    assert result["rollback"] is None
    assert "left unchanged" in result["message"]
    assert driver.saved == []
    assert driver.peer["serveraddress"] == "192.0.2.10"
    entry = logbuf.REQUEST_LOG[-1]
    assert entry["switch_stage"] == "validate the WireGuard peer association"
    assert entry["rollback_stage"] == "not required"
    assert entry["rollback"] is None


def test_switch_restores_complete_peer_and_gateway_and_reports_rollback_failure(monkeypatch, tmp_path):
    configure_store(monkeypatch, tmp_path)
    logbuf.REQUEST_LOG.clear()
    store.update(lambda doc: doc["devices"].update({"a": {
        "id": "a", "ownerId": "alice", "driverId": "opnsense.firewall",
        "vpnEndpointProfile": {"enabled": True, "peerUuid": "peer",
                               "instanceUuid": "instance", "gatewayUuid": "gateway"},
    }}))
    profile = service._profile({"enabled": True, "peerUuid": "peer",
                                "instanceUuid": "instance", "gatewayUuid": "gateway"})
    service._save_discovery("alice", "a", profile, [{
        "candidateId": "new", "endpointIp": "192.0.2.11", "endpointPort": 51820,
        "publicKey": KEY_B, "classification": "Eligible", "hostname": "new",
    }])
    gateway = {"name": "VPN gateway", "gateway": "192.0.2.10",
               "monitor": "192.0.2.10", "status": "online", "other": "preserved"}
    driver = FakeDriver(handshake=False, gateway=gateway)
    @contextlib.contextmanager
    def connected(*args, **kwargs): yield {}, driver, object()
    monkeypatch.setattr(devices, "device_conn", connected)
    monkeypatch.setattr(service.time, "sleep", lambda _: None)
    result = service.switch("alice", "a", "new", True)
    assert result == {"ok": False, "rollback": True, "changedGateway": True,
                      "message": "Endpoint verification failed; the previous configuration was restored."}
    assert driver.peer == {"name": "peer", "servers": "instance", "serveraddress": "192.0.2.10",
                           "serverport": "51820", "pubkey": KEY_A, "psk": "sensitive"}
    assert driver.gateway_value == gateway
    assert driver.gateway_saved[-1] == gateway
    entry = logbuf.REQUEST_LOG[-1]
    assert entry["event"] == "vpn_endpoint_switch_failed"
    assert entry["level"] == "warn"
    assert entry["switch_stage"] == "verify a new WireGuard handshake"
    assert entry["rollback"] is True
    assert entry["rollback_handshake_observed"] is True
    assert "sensitive" not in str(entry)

    driver = FakeDriver(handshake=False, gateway=gateway, rollback_handshake=False)
    monkeypatch.setattr(devices, "device_conn", connected)
    result = service.switch("alice", "a", "new", True)
    assert result["rollback"] is True
    assert result["message"].endswith(
        "A restored-tunnel handshake has not yet been observed.")
    assert logbuf.REQUEST_LOG[-1]["rollback_handshake_observed"] is False

    driver = FakeDriver(handshake=False, gateway=gateway, rollback_fails=True)
    monkeypatch.setattr(devices, "device_conn", connected)
    result = service.switch("alice", "a", "new", True)
    assert result["ok"] is False and result["rollback"] is False
    entry = logbuf.REQUEST_LOG[-1]
    assert entry["level"] == "error"
    assert entry["rollback_stage"] == "restore the WireGuard peer"
    assert "rollback failed" in entry["message"]


def test_gateway_state_is_not_used_as_wireguard_health():
    profile = service._profile({"handshakeWarningSeconds": 300})
    assert service._health({"configured": True, "gateway": {"status": "online"},
                            "status": {"latestHandshake": None, "status": "offline"}}, profile) == "Offline"
    assert service._health({"configured": True,
                            "status": {"latestHandshake": 100, "handshakeAge": 301,
                                       "status": "online"}}, profile) == "Warning"

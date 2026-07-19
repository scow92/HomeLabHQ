import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import app
import auth
import dashboards
import devices
import poller
import push
import services
import store
from context import Actor, Role
from errors import Conflict, Forbidden, NotFound, ValidationError


def configure_store(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(store, "DB_FILE", str(tmp_path / "homelabhq.json"))
    monkeypatch.setattr(store, "LOCK_FILE", str(tmp_path / "homelabhq.lock"))
    store._cache.update(doc=None, mtime=None)


def seed_resources(monkeypatch, tmp_path):
    configure_store(monkeypatch, tmp_path)
    def mutate(doc):
        doc["users"].update({
            "alice": {"id": "alice", "username": "alice", "role": "member"},
            "bob": {"id": "bob", "username": "bob", "role": "member"},
        })
        doc["devices"]["alice-device"] = {"id": "alice-device", "ownerId": "alice"}
        doc["dashboards"]["alice-dashboard"] = {
            "id": "alice-dashboard", "ownerId": "alice", "name": "Alice"}
    store.update(mutate)


def test_actor_scoped_services_hide_other_owners(monkeypatch, tmp_path):
    seed_resources(monkeypatch, tmp_path)
    alice = Actor("alice", Role.MEMBER)
    bob = Actor("bob", Role.MEMBER)
    assert services.authorized_device(alice, "alice-device")["ownerId"] == "alice"
    with pytest.raises(NotFound):
        services.authorized_device(bob, "alice-device")
    with pytest.raises(NotFound):
        services.update_dashboard(bob, "alice-dashboard", name="nope")


def test_client_bindings_remain_within_selected_device_owner(monkeypatch, tmp_path):
    configure_store(monkeypatch, tmp_path)
    mac = "AA:BB:CC:DD:EE:01"

    def mutate(document):
        document["devices"].update({
            "alice-preferred": {
                "id": "alice-preferred", "ownerId": "alice", "name": "Alice AP",
                "host": "alice-ap", "transport": "ssh", "boundClients": [],
            },
            "alice-other": {
                "id": "alice-other", "ownerId": "alice", "name": "Alice other AP",
                "host": "alice-other", "transport": "ssh", "boundClients": [mac],
            },
            "bob-preferred": {
                "id": "bob-preferred", "ownerId": "bob", "name": "Bob AP",
                "host": "bob-ap", "transport": "ssh", "boundClients": [mac],
            },
        })

    store.update(mutate)
    services.set_client_binding(Actor("admin", Role.ADMIN), "alice-preferred", mac, True)
    stored = store.load()["devices"]

    assert stored["alice-preferred"]["boundClients"] == [mac]
    assert "boundClients" not in stored["alice-other"]
    assert stored["bob-preferred"]["boundClients"] == [mac]
    assert devices.binding_map("alice") == {mac: "alice-preferred"}
    assert devices.binding_map("bob") == {mac: "bob-preferred"}


def test_binding_enforcement_uses_each_owners_preferred_ap(monkeypatch, tmp_path):
    configure_store(monkeypatch, tmp_path)
    mac = "AA:BB:CC:DD:EE:01"

    def device(device_id, owner_id, *, preferred=False, online=True, binding=False):
        record = {
            "id": device_id, "ownerId": owner_id, "name": device_id,
            "host": device_id, "transport": "ssh", "driverId": "test-ap",
            "apBinding": binding, "state": {"online": online},
        }
        if preferred:
            record["boundClients"] = [mac]
        return record

    store.update(lambda document: document["devices"].update({
        "alice-preferred": device("alice-preferred", "alice", preferred=True, online=False),
        "alice-current": device("alice-current", "alice", binding=True),
        "bob-preferred": device("bob-preferred", "bob", preferred=True),
        "bob-current": device("bob-current", "bob", binding=True),
    }))
    enforced = []

    class Connection:
        def __init__(self, device_id):
            self.device_id = device_id

        def close(self):
            pass

    class Driver:
        supports_binding = True

        def enforce_bindings(self, connection, roam_off):
            enforced.append((connection.device_id, roam_off))
            return {}

    monkeypatch.setattr(poller.registry, "get", lambda driver_id: Driver())
    monkeypatch.setattr(devices, "open_conn",
                        lambda record, timeout: Connection(record["id"]))

    poller.enforce_bindings()

    assert enforced == [("bob-current", {mac})]


def test_admin_cannot_assign_device_to_another_owners_dashboard(monkeypatch, tmp_path):
    configure_store(monkeypatch, tmp_path)
    store.update(lambda document: (
        document["devices"].update({
            "alice-device": {"id": "alice-device", "ownerId": "alice"},
        }),
        document["dashboards"].update({
            "bob-dashboard": {"id": "bob-dashboard", "ownerId": "bob", "name": "Bob"},
        }),
    ))

    with pytest.raises(ValidationError, match="same owner"):
        services.update_device(Actor("admin", Role.ADMIN), "alice-device",
                               dashboard_id="bob-dashboard")

    assert store.load()["devices"]["alice-device"].get("dashboardId") is None


def test_admin_nac_lookup_stays_in_their_roster_owner_context(monkeypatch, tmp_path):
    configure_store(monkeypatch, tmp_path)
    store.update(lambda document: document["devices"].update({
        "alice-firewall": {
            "id": "alice-firewall", "ownerId": "alice",
            "nac": {"alias": "alice-allow", "managedAliases": []},
        },
        "admin-firewall": {
            "id": "admin-firewall", "ownerId": "admin",
            "nac": {"alias": "admin-allow", "managedAliases": []},
        },
    }))
    admin = Actor("admin", Role.ADMIN)

    assert services.get_nac_config(admin)["deviceId"] == "admin-firewall"
    services.set_nac_config(admin, [{"uuid": "admin-alias"}], {"enabled": False})
    stored = store.load()["devices"]
    assert stored["alice-firewall"]["nac"]["managedAliases"] == []
    assert stored["admin-firewall"]["nac"]["managedAliases"][0]["uuid"] == "admin-alias"


def test_push_unsubscribe_requires_subscription_owner(monkeypatch, tmp_path):
    configure_store(monkeypatch, tmp_path)
    endpoint = "https://push.example/bob"
    store.update(lambda document: document["push_subs"].update({
        endpoint: {"userId": "bob", "subscription": {"endpoint": endpoint}},
    }))

    assert push.unsubscribe("alice", endpoint) is False
    assert endpoint in store.load()["push_subs"]
    assert push.unsubscribe("bob", endpoint) is True
    assert endpoint not in store.load()["push_subs"]


def test_admin_policy_is_centralized():
    with pytest.raises(Forbidden, match="admin only"):
        services.require_admin(Actor("member", Role.MEMBER))
    assert services.require_admin(Actor("admin", Role.ADMIN)).is_admin


def test_last_admin_invariant_is_enforced_in_auth_service(monkeypatch, tmp_path):
    configure_store(monkeypatch, tmp_path)
    store.update(lambda doc: doc["users"].update({
        "admin": {"id": "admin", "username": "admin", "role": "admin"},
        "member": {"id": "member", "username": "member", "role": "member"},
    }))
    with pytest.raises(Conflict, match="last admin"):
        auth.delete_user("admin")
    assert "admin" in store.load()["users"]


def test_password_minimum_applies_to_setup_and_new_users(monkeypatch, tmp_path):
    configure_store(monkeypatch, tmp_path)

    with pytest.raises(ValidationError, match="at least 15 characters"):
        auth.create_initial_admin("admin", "too-short")
    assert store.load()["users"] == {}

    auth.create_initial_admin("admin", "a-secure-admin-password")
    with pytest.raises(ValidationError, match="at least 15 characters"):
        auth.create_user("alice", "too-short")


def test_password_change_verifies_current_password_and_revokes_other_sessions(
        monkeypatch, tmp_path):
    configure_store(monkeypatch, tmp_path)
    old_password = "original-secure-password"
    new_password = "replacement-secure-password"
    user = auth.create_user("alice", old_password)
    current_token, _ = auth.login("alice", old_password)
    other_token, _ = auth.login("alice", old_password)
    original_hash = store.load()["users"][user["id"]]["passHash"]

    with pytest.raises(ValidationError, match="at least 15 characters"):
        auth.set_password(user["id"], old_password, "too-short", current_token)
    with pytest.raises(ValidationError, match="current password is incorrect"):
        auth.set_password(user["id"], "incorrect-current-password",
                          new_password, current_token)

    assert store.load()["users"][user["id"]]["passHash"] == original_hash
    assert auth.user_for_token(current_token) == user
    assert auth.user_for_token(other_token) == user

    assert auth.set_password(user["id"], old_password, new_password, current_token) == 1

    assert auth.user_for_token(current_token) == user
    assert auth.user_for_token(other_token) is None
    assert auth.login("alice", old_password) == (None, None)
    new_token, logged_in_user = auth.login("alice", new_password)
    assert new_token
    assert logged_in_user == user


def test_user_deprovisioning_revokes_access_and_preserves_owned_resources(
        monkeypatch, tmp_path):
    configure_store(monkeypatch, tmp_path)

    def seed(document):
        document["users"]["alice"] = {
            "id": "alice", "username": "alice", "role": "member"}
        document["sessions"]["alice-session"] = {"userId": "alice"}
        document["push_subs"]["https://push.example/alice"] = {
            "userId": "alice", "subscription": {}}
        document["credentials"]["alice-credential"] = "encrypted"
        document["devices"]["alice-device"] = {
            "id": "alice-device", "ownerId": "alice",
            "credRef": "alice-credential"}
        document["dashboards"]["alice-dashboard"] = {
            "id": "alice-dashboard", "ownerId": "alice", "name": "Alice"}
        document["clientRosters"]["alice"] = {
            "AA:BB:CC:DD:EE:01": {"name": "Alice phone"}}

    store.update(seed)

    with pytest.raises(Conflict, match="1 device, 1 dashboard"):
        auth.delete_user("alice")

    document = store.load()
    assert "alice-session" not in document["sessions"]
    assert "https://push.example/alice" not in document["push_subs"]
    assert "alice" in document["users"]
    assert "alice-device" in document["devices"]
    assert document["credentials"]["alice-credential"] == "encrypted"
    assert "alice-dashboard" in document["dashboards"]
    assert "alice" in document["clientRosters"]

    devices.delete_device("alice-device")
    dashboards.delete("alice-dashboard")
    auth.delete_user("alice")

    document = store.load()
    assert "alice" not in document["users"]
    assert "alice-credential" not in document["credentials"]
    assert "alice" not in document["clientRosters"]


@pytest.mark.parametrize(("error", "status"), [
    (Forbidden("admin only"), 403),
    (NotFound(), 404),
    (Conflict("already set up"), 409),
])
def test_http_error_mapping_is_central(error, status):
    handler = app.Handler.__new__(app.Handler)
    sent = []
    handler._send_json = lambda code, body: sent.append((code, body))
    handler._send_application_error(error)
    assert sent == [(status, {"error": str(error)})]

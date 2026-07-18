import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import app
import auth
import services
import store
from context import Actor, Role
from errors import Conflict, Forbidden, NotFound


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

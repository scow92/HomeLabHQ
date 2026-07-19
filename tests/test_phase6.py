import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import push
import store
import auth
import client_roster


def configure_store(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(store, "DB_FILE", str(tmp_path / "homelabhq.json"))
    monkeypatch.setattr(store, "LOCK_FILE", str(tmp_path / "homelabhq.lock"))
    store._cache.update(doc=None, mtime=None)


def test_startup_integrity_check_migrates_legacy_document_once(monkeypatch, tmp_path):
    configure_store(monkeypatch, tmp_path)
    Path(store.DB_FILE).parent.mkdir(parents=True, exist_ok=True)
    Path(store.DB_FILE).write_text(json.dumps({"users": {}}))

    first = store.startup_integrity_check()
    migrated = json.loads(Path(store.DB_FILE).read_text())
    second = store.startup_integrity_check()

    assert first["schemaVersion"] == store.SCHEMA_VERSION
    assert first["migrated"] is True
    assert store.metrics()["last_document_bytes"] == first["documentBytes"]
    assert migrated["schemaVersion"] == store.SCHEMA_VERSION
    assert set(store._DEFAULT_DOC).issubset(migrated)
    assert second["migrated"] is False


def test_startup_integrity_check_preserves_invalid_or_newer_documents(monkeypatch, tmp_path):
    configure_store(monkeypatch, tmp_path)
    Path(store.DB_FILE).parent.mkdir(parents=True, exist_ok=True)
    for document in (b"{not JSON", json.dumps({"schemaVersion": store.SCHEMA_VERSION + 1}).encode()):
        Path(store.DB_FILE).write_bytes(document)
        store._cache.update(doc=None, mtime=None)
        with pytest.raises(store.StoreError):
            store.startup_integrity_check()
        assert Path(store.DB_FILE).read_bytes() == document


def test_noop_mutator_does_not_rewrite_the_document(monkeypatch, tmp_path):
    configure_store(monkeypatch, tmp_path)
    store.update(lambda document: document["meta"].update(value=1))
    before = Path(store.DB_FILE).read_bytes()
    writes = store.metrics()["writes"]

    assert store.update(lambda document: "unchanged") == "unchanged"

    assert Path(store.DB_FILE).read_bytes() == before
    assert store.metrics()["writes"] == writes
    assert store.metrics()["no_op_updates"] >= 1


def test_batch_update_commits_related_records_together(monkeypatch, tmp_path):
    configure_store(monkeypatch, tmp_path)

    def add_device_and_credential(document):
        document["credentials"]["credential-1"] = "encrypted"
        document["devices"]["device-1"] = {"id": "device-1", "credRef": "credential-1"}

    store.batch_update(add_device_and_credential)
    document = store.load()
    assert document["devices"]["device-1"]["credRef"] == "credential-1"
    assert document["credentials"]["credential-1"] == "encrypted"


def test_ssh_host_key_records_are_bounded(monkeypatch, tmp_path):
    configure_store(monkeypatch, tmp_path)
    monkeypatch.setattr(store, "MAX_SSH_HOST_KEYS", 2)
    for host in ("one", "two", "three"):
        store.pin_ssh_host_key(host, 22, "ssh-ed25519", host)
    keys = store.load()["sshHostKeys"]
    assert len(keys) == 2
    assert "one:22" not in keys


def test_push_subscriptions_are_bounded_per_user(monkeypatch, tmp_path):
    configure_store(monkeypatch, tmp_path)
    monkeypatch.setattr(push, "MAX_PUSH_SUBSCRIPTIONS_PER_USER", 2)
    for endpoint in ("https://push/one", "https://push/two", "https://push/three"):
        push.subscribe("alice", {"endpoint": endpoint, "keys": {}})
    subscriptions = store.load()["push_subs"]
    assert len(subscriptions) == 2
    assert "https://push/one" not in subscriptions


def test_sessions_are_bounded_after_expired_sessions_are_swept(monkeypatch, tmp_path):
    configure_store(monkeypatch, tmp_path)
    monkeypatch.setattr(auth, "MAX_SESSIONS", 2)
    auth.create_user("alice", "a-secure-test-password")
    for _ in range(3):
        token, user = auth.login("alice", "a-secure-test-password")
        assert token and user["username"] == "alice"
    assert len(store.load()["sessions"]) == 2


def test_offline_roster_records_follow_the_retention_policy(monkeypatch, tmp_path):
    configure_store(monkeypatch, tmp_path)
    monkeypatch.setattr(client_roster, "CLIENT_RECORD_RETENTION_DAYS", 1)
    store.update(lambda document: document["clientRosters"].update({
        "alice": {"AA:BB:CC:DD:EE:01": {
            "firstSeen": 1, "lastSeen": 1, "online": False,
        }},
    }))

    client_roster.record_observations("alice", [])

    assert store.load()["clientRosters"]["alice"] == {}

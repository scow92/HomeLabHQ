import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import push
import store


@pytest.fixture(autouse=True)
def isolated_store(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(store, "DB_FILE", str(tmp_path / "homelabhq.json"))
    monkeypatch.setattr(store, "LOCK_FILE", str(tmp_path / "homelabhq.lock"))
    store._cache.update(doc=None, mtime=None)
    store.update(lambda document: document["users"].update({
        "alice": {"id": "alice", "role": "member"},
        "bob": {"id": "bob", "role": "member"},
    }))
    yield
    store._cache.update(doc=None, mtime=None)


def test_pushed_notification_is_persisted_with_backend_unread_count(monkeypatch):
    payloads = []
    store.update(lambda document: document["push_subs"].update({
        "https://push.example/alice": {
            "userId": "alice", "subscription": {"endpoint": "alice"}},
    }))
    monkeypatch.setattr(push, "_ensure_vapid", lambda: None)
    monkeypatch.setattr("pywebpush.webpush", lambda **kwargs: payloads.append(
        json.loads(kwargs["data"])))

    delivery = push.notify(
        {"alice"}, "Device offline", "pve1 became unreachable.",
        {"type": "offline", "deviceId": "proxmox-1"},
    )

    centre = push.notification_center("alice")
    assert delivery["sent"] == 1
    assert delivery["persisted"] == 1
    assert delivery["unreadCount"] == centre["unreadCount"] == 1
    assert centre["notifications"][0]["title"] == "Device offline"
    assert centre["notifications"][0]["category"] == "host_offline"
    assert payloads[0]["data"]["unreadCount"] == 1
    assert payloads[0]["data"]["notificationId"] == centre["notifications"][0]["id"]
    assert push.notification_center("bob") == {"notifications": [], "unreadCount": 0}


def test_push_failure_keeps_notification_in_centre(monkeypatch):
    store.update(lambda document: document["push_subs"].update({
        "https://push.example/alice": {
            "userId": "alice", "subscription": {"endpoint": "alice"}},
    }))
    monkeypatch.setattr(push, "_ensure_vapid", lambda: None)
    monkeypatch.setattr("pywebpush.webpush", lambda **_kwargs: (_ for _ in ()).throw(
        RuntimeError("provider unavailable")))

    result = push.notify({"alice"}, "Backup failed", "Nightly backup failed.", {
        "type": "backup_failure",
    })

    assert result["failed"] == 1
    assert result["persisted"] == 1
    assert push.notification_center("alice")["notifications"][0]["category"] == \
        "backup_failure"


def test_read_and_dismiss_recalculate_authoritative_unread_count(monkeypatch):
    monkeypatch.setattr(push, "_ensure_vapid", lambda: None)
    push.notify({"alice"}, "High CPU", "CPU is above 90%.", {
        "type": "alert", "key": "cpu_pct",
    })
    push.notify({"alice"}, "Storage warning", "Pool capacity is high.", {
        "type": "alert", "key": "storage_pct",
    })
    centre = push.notification_center("alice")
    first, second = centre["notifications"]
    assert centre["unreadCount"] == 2

    read = push.mark_notification_read("alice", first["id"])
    dismissed = push.dismiss_notification("alice", second["id"])

    assert read["unreadCount"] == 1
    assert dismissed["unreadCount"] == 0
    refreshed = push.notification_center("alice")
    assert refreshed["unreadCount"] == 0
    assert [item["id"] for item in refreshed["notifications"]] == [first["id"]]
    assert push.mark_notification_read("bob", first["id"]) is None


def test_mark_all_read_clears_unread_without_deleting_recent_entries(monkeypatch):
    monkeypatch.setattr(push, "_ensure_vapid", lambda: None)
    push.notify({"alice"}, "Updates available", "12 updates available.", {
        "type": "available_updates",
    })

    result = push.mark_all_notifications_read("alice")
    centre = push.notification_center("alice")

    assert result == {"unreadCount": 0}
    assert centre["unreadCount"] == 0
    assert len(centre["notifications"]) == 1
    assert centre["notifications"][0]["readAt"] is not None


def test_v5_store_migration_adds_notification_collection():
    document = store.load()
    document.pop("notifications")
    document["schemaVersion"] = 5

    migrated, changed = store._migrate_doc(document)

    assert changed is True
    assert migrated["schemaVersion"] == store.SCHEMA_VERSION
    assert migrated["notifications"] == {}

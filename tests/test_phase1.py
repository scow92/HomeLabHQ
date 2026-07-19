import io
import json
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import app
import auth
import client_roster
import store


def configure_store(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(store, "DB_FILE", str(tmp_path / "homelabhq.json"))
    monkeypatch.setattr(store, "LOCK_FILE", str(tmp_path / "homelabhq.lock"))
    store._cache.update(doc=None, mtime=None)


def test_corrupt_existing_store_is_not_replaced(monkeypatch, tmp_path):
    configure_store(monkeypatch, tmp_path)
    raw = b"{not json"
    Path(store.DB_FILE).parent.mkdir(parents=True, exist_ok=True)
    Path(store.DB_FILE).write_bytes(raw)
    with pytest.raises(store.StoreError):
        store.load()
    assert Path(store.DB_FILE).read_bytes() == raw


def test_store_update_keeps_a_validated_backup(monkeypatch, tmp_path):
    configure_store(monkeypatch, tmp_path)
    store.update(lambda doc: doc["meta"].update(version=1))
    previous = Path(store.DB_FILE).read_text()
    store.update(lambda doc: doc["meta"].update(version=2))
    assert json.loads(Path(store.DB_FILE + ".bak").read_text()) == json.loads(previous)


def test_initial_setup_is_atomic(monkeypatch, tmp_path):
    configure_store(monkeypatch, tmp_path)
    results = []
    def create():
        try:
            results.append(auth.create_initial_admin("admin", "a-secure-test-password"))
        except ValueError as exc:
            results.append(str(exc))
    threads = [threading.Thread(target=create) for _ in range(2)]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    assert len(store.load()["users"]) == 1
    assert sum(isinstance(result, dict) for result in results) == 1
    assert "already set up" in results


def test_roster_records_are_owner_scoped(monkeypatch, tmp_path):
    configure_store(monkeypatch, tmp_path)
    mac = "AA:BB:CC:DD:EE:01"
    client_roster.record_observations("alice", [{"mac": mac, "ip": "192.0.2.9", "seen": []}])
    client_roster.set_metadata("alice", mac, "Alice phone", "private")
    assert client_roster.client_history("bob", mac)["events"] == []
    client_roster.forget("bob", [mac])
    assert client_roster.client_history("alice", mac)["events"]
    assert store.load()["clientRosters"]["alice"][mac]["name"] == "Alice phone"


def test_static_paths_use_resolved_containment(monkeypatch, tmp_path):
    web = tmp_path / "web"
    web.mkdir(); (web / "index.html").write_text("ok")
    sibling = tmp_path / "web-private"
    sibling.mkdir(); (sibling / "secret.txt").write_text("secret")
    monkeypatch.setattr(app, "WEB_DIR", str(web))
    handler = app.Handler.__new__(app.Handler)
    responses = []
    handler._send_json = lambda code, obj, **_: responses.append((code, obj))
    handler._serve_static("/../web-private/secret.txt")
    handler._serve_static("/%2e%2e/web-private/secret.txt")
    (web / "outside-link").symlink_to(sibling, target_is_directory=True)
    handler._serve_static("/outside-link/secret.txt")
    assert responses == [(403, {"error": "forbidden"})] * 3


@pytest.mark.parametrize(("headers", "body", "message"), [
    ({"Content-Length": "bad", "Content-Type": "application/json"}, b"", "invalid Content-Length"),
    ({"Content-Length": "0"}, b"", "Content-Type"),
    ({"Content-Length": "3", "Content-Type": "text/plain"}, b"{} ", "Content-Type"),
    ({"Content-Length": "2", "Content-Type": "application/json"}, b"[]", "object"),
])
def test_json_request_validation(headers, body, message):
    handler = app.Handler.__new__(app.Handler)
    handler.headers = headers
    handler.rfile = io.BytesIO(body)
    with pytest.raises(ValueError, match=message):
        handler._read_json()


def test_json_request_size_limit(monkeypatch):
    monkeypatch.setattr(app, "MAX_JSON_BODY_BYTES", 1)
    handler = app.Handler.__new__(app.Handler)
    handler.headers = {"Content-Length": "2", "Content-Type": "application/json"}
    handler.rfile = io.BytesIO(b"{}")
    with pytest.raises(ValueError, match="too large"):
        handler._read_json()

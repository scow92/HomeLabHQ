import base64
import io
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import app
import auth
import client_roster
import crypto
import store
import transports
from drivers import zyxel_ap


def configure_store(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(store, "DB_FILE", str(tmp_path / "homelabhq.json"))
    monkeypatch.setattr(store, "LOCK_FILE", str(tmp_path / "homelabhq.lock"))
    store._cache.update(doc=None, mtime=None)


def configure_secret_dir(monkeypatch, tmp_path):
    secrets_dir = tmp_path / "secrets"
    monkeypatch.setattr(store, "SECRETS_DIR", str(secrets_dir))
    monkeypatch.setattr(crypto, "SECRETS_DIR", str(secrets_dir))
    monkeypatch.setattr(crypto, "SECRET_FILE", str(secrets_dir / "instance_secret"))
    return secrets_dir


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
    state_files = [Path(store.DB_FILE), Path(store.DB_FILE + ".bak"), Path(store.LOCK_FILE)]
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in state_files)

    for path in state_files:
        path.chmod(0o666)
    store._cache.update(doc=None, mtime=None)
    store.load()
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in state_files)


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


def test_instance_secret_first_use_is_atomic(monkeypatch, tmp_path):
    secrets_dir = configure_secret_dir(monkeypatch, tmp_path)
    barrier = threading.Barrier(2)
    candidates = iter((b"A" * 32, b"B" * 32))
    candidate_lock = threading.Lock()

    def generate_candidate():
        barrier.wait(timeout=2)
        with candidate_lock:
            return next(candidates)

    monkeypatch.setattr(crypto, "_new_instance_secret", generate_candidate)
    results = []
    errors = []

    def load_secret():
        try:
            results.append(crypto._instance_secret())
        except Exception as error:  # pragma: no cover - assertion reports the error
            errors.append(error)

    threads = [threading.Thread(target=load_secret) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)

    assert not errors
    assert all(not thread.is_alive() for thread in threads)
    assert len(results) == 2
    assert results[0] == results[1]
    assert base64.urlsafe_b64decode((secrets_dir / "instance_secret").read_bytes()) == results[0]


@pytest.mark.parametrize("contents", [b"", b"not-base64", base64.urlsafe_b64encode(b"short")])
def test_invalid_instance_secret_is_not_silently_replaced(monkeypatch, tmp_path, contents):
    secrets_dir = configure_secret_dir(monkeypatch, tmp_path)
    secrets_dir.mkdir()
    secret_file = secrets_dir / "instance_secret"
    secret_file.write_bytes(contents)

    with pytest.raises(RuntimeError, match="restore it from backup"):
        crypto._instance_secret()

    assert secret_file.read_bytes() == contents


def test_roster_records_are_owner_scoped(monkeypatch, tmp_path):
    configure_store(monkeypatch, tmp_path)
    mac = "AA:BB:CC:DD:EE:01"
    client_roster.record_observations("alice", [{"mac": mac, "ip": "192.0.2.9", "seen": []}])
    client_roster.set_metadata("alice", mac, "Alice phone", "private")
    assert client_roster.client_history("bob", mac)["events"] == []
    client_roster.forget("bob", [mac])
    assert client_roster.client_history("alice", mac)["events"]
    assert store.load()["clientRosters"]["alice"][mac]["name"] == "Alice phone"


def test_roster_event_summary_distinguishes_new_devices_from_reconnections(monkeypatch, tmp_path):
    configure_store(monkeypatch, tmp_path)
    monkeypatch.setattr(client_roster, "CLIENT_OFFLINE_AFTER", 60)
    now = 100
    monkeypatch.setattr(client_roster.time, "time", lambda: now)
    known_mac = "AA:BB:CC:DD:EE:01"
    new_mac = "AA:BB:CC:DD:EE:02"

    client_roster.record_observations("alice", [{"mac": known_mac, "seen": []}])
    now = 200
    client_roster.record_observations("alice", [])
    now = 300
    client_roster.record_observations("alice", [
        {"mac": known_mac, "seen": []},
        {"mac": new_mac, "seen": []},
    ])

    summary = client_roster.events_since("alice", 150)
    assert summary == {"since": 150, "count": 3, "newCount": 1}
    assert client_roster.events_since("bob", 150)["newCount"] == 0


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


def test_http_transport_blocks_cross_host_redirect_before_forwarding_credentials():
    received = []

    class Target(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append(dict(self.headers))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *_args):
            pass

    class Redirect(BaseHTTPRequestHandler):
        destination = ""

        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", self.destination)
            self.end_headers()

        def log_message(self, *_args):
            pass

    target = HTTPServer(("127.0.0.1", 0), Target)
    redirect = HTTPServer(("127.0.0.1", 0), Redirect)
    Redirect.destination = f"http://localhost:{target.server_port}/target"
    threads = [threading.Thread(target=server.serve_forever) for server in (target, redirect)]
    for thread in threads:
        thread.start()
    try:
        connection = transports.HTTPConnection(
            "127.0.0.1", port=redirect.server_port, scheme="http",
            api_key="device-key", auth_style="header", verify_tls=False,
        )
        with pytest.raises(transports.ConnectionError, match="cross-host redirect blocked"):
            connection.connect()
        assert received == []
    finally:
        for server in (target, redirect):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(1)


def test_http_transport_keeps_same_host_redirects_but_drops_auth_on_origin_change():
    received = []

    class Target(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append(dict(self.headers))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *_args):
            pass

    class Redirect(BaseHTTPRequestHandler):
        destination = ""

        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", self.destination)
            self.end_headers()

        def log_message(self, *_args):
            pass

    target = HTTPServer(("127.0.0.1", 0), Target)
    redirect = HTTPServer(("127.0.0.1", 0), Redirect)
    Redirect.destination = f"http://127.0.0.1:{target.server_port}/target"
    threads = [threading.Thread(target=server.serve_forever) for server in (target, redirect)]
    for thread in threads:
        thread.start()
    try:
        connection = transports.HTTPConnection(
            "127.0.0.1", port=redirect.server_port, scheme="http",
            api_key="device-key", auth_style="header", verify_tls=False,
        ).connect()
        connection.close()
        assert len(received) == 1
        assert "X-API-Key" not in received[0]
    finally:
        for server in (target, redirect):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(1)


def test_zyxel_interactive_ssh_uses_persistent_tofu_policy(monkeypatch):
    class Channel:
        def recv_ready(self):
            return True

        def recv(self, _size):
            return b"ap# "

    class Client:
        def __init__(self):
            self.policy = None
            self.connected = None
            self.closed = False

        def set_missing_host_key_policy(self, policy):
            self.policy = policy

        def connect(self, host, **kwargs):
            self.connected = (host, kwargs)

        def invoke_shell(self, **_kwargs):
            return Channel()

        def close(self):
            self.closed = True

    client = Client()
    monkeypatch.setitem(sys.modules, "paramiko", SimpleNamespace(SSHClient=lambda: client))

    assert zyxel_ap._ap_ssh("ap.lan", "admin", "test-password", [], timeout=1) == ""
    assert isinstance(client.policy, transports._TOFUHostKeyPolicy)
    assert (client.policy.host, client.policy.port) == ("ap.lan", 22)
    assert client.connected[0] == "ap.lan"
    assert client.closed is True

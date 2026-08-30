"""Deployment and observability regressions for Phase 8."""
import re
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

import logbuf
import poller
import push
import services
import store
from backend.asgi.main import create_app
from context import Actor, Role
from errors import Forbidden


def configure_vapid(monkeypatch, tmp_path):
    secrets_dir = tmp_path / "secrets"
    monkeypatch.setattr(store, "SECRETS_DIR", str(secrets_dir))
    monkeypatch.setattr(push, "VAPID_PRIV", str(secrets_dir / "vapid_private.pem"))
    monkeypatch.setattr(push, "VAPID_PUB", str(secrets_dir / "vapid_public.txt"))
    return secrets_dir


def test_structured_logs_redact_secrets_before_the_ring_buffer_and_stdout():
    logbuf.REQUEST_LOG.clear()
    entry = logbuf.log_event(
        "info", "request", source="http", authorization="Bearer top-secret",
        credentials={"password": "not-for-logs"},
        message=("token=also-not-for-logs private key:private-value "
                 "psk=preshared-value https://user:password@example.test"),
    )

    assert entry["authorization"] == "[redacted]"
    assert entry["credentials"] == "[redacted]"
    assert "top-secret" not in str(entry)
    assert "not-for-logs" not in str(entry)
    assert "also-not-for-logs" not in str(entry)
    assert "private-value" not in str(entry)
    assert "preshared-value" not in str(entry)
    assert "password@example.test" not in str(entry)
    assert logbuf.REQUEST_LOG[-1] == entry


def test_legacy_readiness_requires_store_and_completed_poller_cycle(monkeypatch):
    monkeypatch.setattr(store, "load", lambda: {})
    client = TestClient(create_app(use_lifespan=False))
    monkeypatch.setattr(poller, "status", lambda: {"ready": False, "running": True})
    response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["store"] == "ready"

    monkeypatch.setattr(poller, "status", lambda: {"ready": True, "running": True})
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "store": "ready", "poller": "ready"}


def test_poller_stop_joins_its_thread(monkeypatch):
    entered = threading.Event()

    def controlled_loop():
        entered.set()
        poller._stop.wait()

    monkeypatch.setattr(poller, "_loop", controlled_loop)
    poller._thread = None
    poller._stop.clear()
    thread = poller.start()
    assert entered.wait(1)
    assert poller.stop(timeout=1) is True
    assert not thread.is_alive()


def test_push_delivery_failures_are_counted_and_redacted(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(store, "DB_FILE", str(tmp_path / "homelabhq.json"))
    monkeypatch.setattr(store, "LOCK_FILE", str(tmp_path / "homelabhq.lock"))
    store._cache.update(doc=None, mtime=None)
    store.update(lambda doc: doc["push_subs"].update({
        "https://push.example/subscription": {"userId": "alice", "subscription": {}},
    }))
    monkeypatch.setattr(push, "_ensure_vapid", lambda: None)

    def failed_delivery(**kwargs):
        raise RuntimeError("authorization=delivery-secret")

    monkeypatch.setattr("pywebpush.webpush", failed_delivery)
    before = push.metrics()["failures"]
    result = push.notify({"alice"}, "title", "body")

    assert result["failed"] == 1
    assert "delivery-secret" not in result["error"]
    assert push.metrics()["failures"] == before + 1


def test_admin_diagnostic_metrics_report_safe_in_process_baselines(monkeypatch):
    logbuf.REQUEST_LOG.clear()
    for path, duration in (
        ("/api/session", 1), ("/api/session", 2),
        ("/api/session", 3), ("/api/session", 4),
        ("/api/devices", 9.25), ("/api/unrelated", 999),
    ):
        logbuf.REQUEST_LOG.append({"event": "request", "path": path, "ms": duration})
    monkeypatch.setattr(services.store, "metrics", lambda: {
        "writes": 8, "last_document_bytes": 4096,
    })
    monkeypatch.setattr(services.poller, "status", lambda: {
        "lastCycleDurationMs": 125, "ready": True,
    })

    result = services.diagnostic_metrics(Actor("admin", Role.ADMIN))

    assert result == {
        "store": {"writes": 8, "last_document_bytes": 4096},
        "poller": {"lastCycleDurationMs": 125, "ready": True},
        "requestLatencies": {
            "/api/session": {"samples": 4, "p50Ms": 2, "p95Ms": 4},
            "/api/devices": {"samples": 1, "p50Ms": 9.25, "p95Ms": 9.25},
            "/api/clients": {"samples": 0, "p50Ms": None, "p95Ms": None},
        },
    }
    with pytest.raises(Forbidden):
        services.diagnostic_metrics(Actor("member", Role.MEMBER))
    logbuf.REQUEST_LOG.clear()


def test_vapid_first_use_is_atomic_across_threads(monkeypatch, tmp_path):
    secrets_dir = configure_vapid(monkeypatch, tmp_path)
    real_generate = push._new_vapid_pair
    generated = []
    generation_lock = threading.Lock()

    def counted_generate():
        with generation_lock:
            generated.append(True)
        time.sleep(0.02)
        return real_generate()

    monkeypatch.setattr(push, "_new_vapid_pair", counted_generate)
    start = threading.Barrier(4)
    results = []
    errors = []

    def load_public_key():
        try:
            start.wait(timeout=2)
            results.append(push.public_key())
        except Exception as error:  # pragma: no cover - assertion reports the error
            errors.append(error)

    threads = [threading.Thread(target=load_public_key) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)

    assert not errors
    assert all(not thread.is_alive() for thread in threads)
    assert len(generated) == 1
    assert len(set(results)) == 1
    assert (secrets_dir / "vapid_private.pem").stat().st_mode & 0o777 == 0o600
    assert (secrets_dir / "vapid_public.txt").stat().st_mode & 0o777 == 0o600


def test_partial_vapid_keypair_fails_closed(monkeypatch, tmp_path):
    secrets_dir = configure_vapid(monkeypatch, tmp_path)
    secrets_dir.mkdir()
    private_file = secrets_dir / "vapid_private.pem"
    private_file.write_bytes(b"do-not-replace")

    with pytest.raises(RuntimeError, match="incomplete"):
        push.public_key()

    assert private_file.read_bytes() == b"do-not-replace"
    assert not (secrets_dir / "vapid_public.txt").exists()


def test_malformed_vapid_keypair_fails_closed(monkeypatch, tmp_path):
    secrets_dir = configure_vapid(monkeypatch, tmp_path)
    secrets_dir.mkdir()
    private_file = secrets_dir / "vapid_private.pem"
    public_file = secrets_dir / "vapid_public.txt"
    private_file.write_bytes(b"not-a-private-key")
    public_file.write_text("not-a-public-key")

    with pytest.raises(RuntimeError, match="invalid"):
        push.public_key()

    assert private_file.read_bytes() == b"not-a-private-key"
    assert public_file.read_text() == "not-a-public-key"


def test_mismatched_vapid_keypair_fails_closed(monkeypatch, tmp_path):
    secrets_dir = configure_vapid(monkeypatch, tmp_path)
    secrets_dir.mkdir()
    private_pem, _ = push._new_vapid_pair()
    _, unrelated_public = push._new_vapid_pair()
    private_file = secrets_dir / "vapid_private.pem"
    public_file = secrets_dir / "vapid_public.txt"
    private_file.write_bytes(private_pem)
    public_file.write_text(unrelated_public)

    with pytest.raises(RuntimeError, match="does not match"):
        push.public_key()

    assert private_file.read_bytes() == private_pem
    assert public_file.read_text() == unrelated_public


def test_production_launcher_declares_one_uvicorn_worker():
    launcher = (ROOT / "backend" / "run.py").read_text()
    assert "uvicorn.run(" in launcher
    assert "workers=1" in launcher
    assert "ThreadingHTTPServer" not in launcher


def test_hardened_deployment_and_update_automation_are_declared():
    dockerfile = (ROOT / "Dockerfile").read_text()
    compose = (ROOT / "docker-compose.yml").read_text()
    dependabot = (ROOT / ".github" / "dependabot.yml").read_text()
    workflow = (ROOT / ".github" / "workflows" / "verify.yml").read_text()

    assert "USER homelabhq" in dockerfile
    assert re.search(r"^FROM python:3\.13-slim@sha256:[0-9a-f]{64}$", dockerfile,
                     re.MULTILINE)
    assert "COPY --chown=homelabhq:homelabhq backend/ ./backend/" in dockerfile
    assert "read_only: true" in compose
    assert "- ALL" in compose
    assert "no-new-privileges:true" in compose
    assert "data-init:" in compose
    assert "chown -R 10001:10001 /data" in compose
    assert "package-ecosystem: docker" in dependabot
    assert "package-ecosystem: pip" in dependabot
    assert "package-ecosystem: github-actions" in dependabot
    assert "development-tools:" in dependabot
    assert "dependency-type: development" in dependabot
    assert "github-actions:" in dependabot
    assert dependabot.count('- "*"') == 2
    assert re.search(
        r'python:\s*\["3\.11",\s*"3\.12",\s*"3\.13",\s*"3\.14"\]', workflow)
    action_revisions = re.findall(r"^\s*- uses: [^@\s]+@([^\s]+)", workflow, re.MULTILINE)
    assert action_revisions
    assert all(re.fullmatch(r"[0-9a-f]{40}", revision) for revision in action_revisions)

"""Deployment and observability regressions for Phase 8."""
import sys
import threading
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

import logbuf
import poller
import push
import store
from backend.http.handler import Handler
from backend.http.hq_server import ThreadingHTTPServer


def test_structured_logs_redact_secrets_before_the_ring_buffer_and_stdout():
    logbuf.REQUEST_LOG.clear()
    entry = logbuf.log_event(
        "info", "request", source="http", authorization="Bearer top-secret",
        credentials={"password": "not-for-logs"},
        message="token=also-not-for-logs https://user:password@example.test",
    )

    assert entry["authorization"] == "[redacted]"
    assert entry["credentials"] == "[redacted]"
    assert "top-secret" not in str(entry)
    assert "not-for-logs" not in str(entry)
    assert "also-not-for-logs" not in str(entry)
    assert "password@example.test" not in str(entry)
    assert logbuf.REQUEST_LOG[-1] == entry


def test_readiness_requires_both_store_and_completed_poller_cycle(monkeypatch):
    sent = []

    class FakeHandler:
        def _send_json(self, status, value, head=False):
            sent.append((status, value, head))

    monkeypatch.setattr("backend.http.handler.store.load", lambda: {})
    monkeypatch.setattr("backend.http.handler.poller.status",
                        lambda: {"ready": False, "running": True})
    Handler._ready_response(FakeHandler())
    assert sent[-1][0] == 503
    assert sent[-1][1]["store"] == "ready"

    monkeypatch.setattr("backend.http.handler.poller.status",
                        lambda: {"ready": True, "running": True})
    Handler._ready_response(FakeHandler(), head=True)
    assert sent[-1] == (200, {"ok": True, "store": "ready", "poller": "ready"}, True)


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


def test_http_server_shutdown_and_close_release_serve_forever_thread():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, name="test-http-server")
    thread.start()
    try:
        time.sleep(0.02)
        server.shutdown()
    finally:
        server.server_close()
    thread.join(1)
    assert not thread.is_alive()
    assert server.daemon_threads is False


def test_hardened_deployment_and_update_automation_are_declared():
    dockerfile = (ROOT / "Dockerfile").read_text()
    compose = (ROOT / "docker-compose.yml").read_text()
    dependabot = (ROOT / ".github" / "dependabot.yml").read_text()

    assert "USER homelabhq" in dockerfile
    assert "read_only: true" in compose
    assert "- ALL" in compose
    assert "no-new-privileges:true" in compose
    assert "package-ecosystem: docker" in dependabot
    assert "package-ecosystem: pip" in dependabot
    assert "package-ecosystem: github-actions" in dependabot

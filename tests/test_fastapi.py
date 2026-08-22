"""FastAPI transport, operational endpoint, and status aggregation coverage."""
from datetime import datetime, timezone
from pathlib import Path
import re
import sys
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

import auth
import store
from backend.api import all_routes
from backend.api.contracts import AuthPolicy, Route, json_response
from backend.asgi.compat import compatibility_router
from backend.asgi.main import create_app
from backend.asgi.status_service import status_summary


def configure_store(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(store, "DB_FILE", str(tmp_path / "homelabhq.json"))
    monkeypatch.setattr(store, "LOCK_FILE", str(tmp_path / "homelabhq.lock"))
    store._cache.update(doc=None, mtime=None)


@pytest.fixture
def client(monkeypatch, tmp_path):
    configure_store(monkeypatch, tmp_path)
    auth._auth_fails.clear()
    with TestClient(create_app(use_lifespan=False)) as test_client:
        yield test_client


def test_application_lifespan_starts_and_stops_process_services(monkeypatch, tmp_path):
    configure_store(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(store, "secrets_isolated_from_agents", lambda: True)
    monkeypatch.setattr("backend.asgi.main.history.migrate_from_store", lambda: calls.append("history"))
    monkeypatch.setattr(
        "backend.asgi.main.compute_maintenance.recover_interrupted_jobs", lambda: []
    )
    monkeypatch.setattr("backend.asgi.main.poller.start", lambda: calls.append("poller-start"))
    monkeypatch.setattr("backend.asgi.main.poller.stop", lambda: calls.append("poller-stop"))
    monkeypatch.setattr(
        "backend.asgi.main.morning_updates.start_scheduler",
        lambda: calls.append("scheduler-start"),
    )
    monkeypatch.setattr(
        "backend.asgi.main.morning_updates.stop_scheduler",
        lambda: calls.append("scheduler-stop"),
    )

    with TestClient(create_app()) as test_client:
        assert test_client.get("/health").status_code == 200

    assert calls == [
        "history", "poller-start", "scheduler-start", "poller-stop", "scheduler-stop"
    ]


def test_operational_and_documentation_endpoints(client):
    for path in ("/health", "/api/v1/health"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.json()["status"] == "healthy"
        assert response.json()["version"] == "0.1.0"
        assert datetime.fromisoformat(response.json()["timestamp"]).tzinfo is not None

    readiness = client.get("/api/v1/readiness")
    assert readiness.status_code == 200
    assert readiness.json()["dependencies"] == {
        "datastore": {"status": "ready", "message": None},
        "configuration": {"status": "ready", "message": None},
    }
    schema = client.get("/openapi.json")
    assert schema.status_code == 200
    assert "/api/devices" in schema.json()["paths"]
    assert "/api/v1/status/summary" in schema.json()["paths"]
    assert "HomelabHQSession" in schema.json()["components"]["securitySchemes"]
    assert client.get("/docs").status_code == 200
    assert client.get("/redoc").status_code == 200


def test_api_errors_frontend_fallback_static_and_pwa(client):
    missing_api = client.get("/api/does-not-exist")
    assert missing_api.status_code == 404
    assert missing_api.headers["content-type"].startswith("application/json")
    assert missing_api.json() == {
        "error": "not found",
        "code": "not_found",
        "requestId": missing_api.headers["X-Request-ID"],
    }
    assert client.get("/devices").text.startswith("<!doctype html>")
    assert client.get("/another-browser-route").status_code == 200
    missing_asset = client.get("/missing.css")
    assert missing_asset.status_code == 404
    assert missing_asset.headers["content-type"].startswith("application/json")
    manifest = client.get("/manifest.webmanifest")
    worker = client.get("/sw.js")
    assert manifest.status_code == 200
    assert manifest.headers["content-type"].startswith("application/manifest+json")
    assert worker.status_code == 200 and "hlhq-shell-v" in worker.text


def test_request_and_security_headers_cover_success_and_errors(client):
    for response in (client.get("/health"), client.get("/api/nope"), client.get("/")):
        assert len(response.headers["X-Request-ID"]) == 32
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["Referrer-Policy"] == "no-referrer"
        assert response.headers["Permissions-Policy"] == (
            "camera=(), geolocation=(), microphone=()"
        )
        assert response.headers["X-Frame-Options"] == "SAMEORIGIN"
        assert "default-src 'self'" in response.headers["Content-Security-Policy"]


def test_every_legacy_route_dispatches_through_fastapi(monkeypatch):
    """One transport-level success contract per migrated method/path."""
    for original in all_routes():
        expected = {"route": original.name}
        route = Route(
            original.method,
            original.path,
            lambda _request, value=expected: json_response(value),
            AuthPolicy.PUBLIC,
            original.name,
        )
        monkeypatch.setattr("backend.asgi.compat.all_routes", lambda selected=route: [selected])
        app = FastAPI()
        app.include_router(compatibility_router())
        path = re.sub(r"\{[^}]+\}", "sample", original.path)
        response = TestClient(app).request(
            original.method,
            path,
            content=b"{}" if original.method in {"POST", "PATCH", "PUT"} else None,
            headers={"Content-Type": "application/json", "Origin": "http://testserver"},
        )
        assert response.status_code == 200, f"{original.method} {original.path}"
        assert response.json() == expected


def _device(device_id, driver_id, now, *, online=True, values=None):
    return {
        "id": device_id,
        "ownerId": "owner",
        "name": device_id,
        "host": f"{device_id}.example.test",
        "transport": "api",
        "driverId": driver_id,
        "state": {
            "online": online,
            "confirmedOnline": online,
            "ts": now,
            "values": values or {},
        },
    }


def test_status_summary_healthy_and_successful_one_shot(monkeypatch, tmp_path):
    configure_store(monkeypatch, tmp_path)
    now = int(time.time())
    proxmox = _device("pve", "proxmox.ve", now)
    proxmox["proxmoxMaintenance"] = {
        "checkedAt": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        "nodes": {name: {"status": "online"} for name in ("pve-a", "pve-b", "pve-c")},
    }
    store.update(lambda document: (
        document["devices"].update({
            "opnsense": _device("opnsense", "opnsense.firewall", now),
            "openwrt": _device("openwrt", "openwrt.ubus", now),
            "switch-a": _device("switch-a", "keeplink.switch", now),
            "switch-b": _device("switch-b", "keeplink.switch", now),
            "ap-a": _device("ap-a", "zyxel.ap", now),
            "ap-b": _device("ap-b", "zyxel.ap", now),
            "ap-c": _device("ap-c", "zyxel.ap", now),
            "pve": proxmox,
            "nas": _device(
                "nas", "truenas.system", now,
                values={"pool_health": "ONLINE", "alerts": 0},
            ),
        }),
        document["computeInstances"].update({
            "docker-host": {
                "dockerDiscoveryState": {"state": "successful", "lastJobAt": now},
                "id": "docker-host", "docker": {"containers": [
                    {"name": "api", "state": "running", "health": "healthy"},
                    {"name": "schema-migrate", "state": "exited", "exitCode": 0,
                     "expectedToRun": False, "restartPolicy": "no"},
                ]},
            },
        }),
    ))
    result = status_summary(now)
    assert result.overall == "healthy"
    assert result.network.total == 7 and result.network.healthy == 7
    assert result.network.components == [
        f"{name} ({name}.example.test)" for name in (
            "opnsense", "openwrt", "switch-a", "switch-b", "ap-a", "ap-b", "ap-c")
    ]
    assert result.proxmox.total == 3 and result.proxmox.healthy == 3
    assert result.docker.total == 1 and result.docker.healthy == 1
    assert result.truenas.pool == "ONLINE" and result.truenas.active_alerts == 0
    assert all(stack.source_checked_at is not None and
               stack.source_checked_at.tzinfo is not None for stack in (
                   result.network, result.proxmox, result.truenas, result.docker))
    assert result.stale is False


@pytest.mark.parametrize(("container", "code"), [
    ({"name": "web", "state": "exited", "exitCode": 0,
      "restartPolicy": "unless-stopped"},
     "container_stopped"),
    ({"name": "db", "state": "running", "health": "unhealthy"},
     "container_unhealthy"),
    ({"name": "worker", "state": "restarting"}, "container_restarting"),
])
def test_status_summary_reports_docker_failures(monkeypatch, tmp_path, container, code):
    configure_store(monkeypatch, tmp_path)
    now = int(time.time())
    store.update(lambda document: document["computeInstances"].update({
        "host": {
            "id": "host",
            "dockerDiscoveryState": {"state": "successful", "lastJobAt": now},
            "docker": {"containers": [container]},
        },
    }))
    result = status_summary(now)
    assert result.docker.status == "critical"
    assert result.docker.healthy == 0 and result.docker.total == 1
    assert result.docker.issues[0].code == code


def test_status_summary_reports_failed_one_shot_and_qualifies_duplicate_names(
    monkeypatch, tmp_path
):
    configure_store(monkeypatch, tmp_path)
    now = int(time.time())
    store.update(lambda document: document["computeInstances"].update({
        "one": {
            "id": "one", "name": "Docker", "host": "node-a.example.test",
            "dockerDiscoveryState": {"state": "successful", "lastJobAt": now},
            "docker": {"containers": [{
                "name": "init", "state": "exited", "exitCode": 3, "oneShot": True,
            }]},
        },
        "two": {
            "id": "two", "name": "Docker", "host": "node-b.example.test",
            "dockerDiscoveryState": {"state": "successful", "lastJobAt": now},
            "docker": {"containers": [{
                "name": "init", "state": "exited", "exitCode": 4,
                "expectedToRun": False,
            }]},
        },
    }))

    result = status_summary(now)

    assert result.docker.status == "critical"
    assert result.docker.total == 2 and result.docker.healthy == 0
    assert {issue.component for issue in result.docker.issues} == {
        "init (node-a.example.test)",
        "init (node-b.example.test)",
    }
    assert {issue.code for issue in result.docker.issues} == {"container_stopped"}


@pytest.mark.parametrize(("container", "status", "total", "healthy", "issue_code"), [
    ({"name": "init", "state": "exited", "exitCode": 0},
     "unknown", 0, 0, None),
    ({"name": "init", "state": "exited", "exitCode": 23},
     "critical", 1, 0, "oneshot_failed"),
    ({"name": "init", "state": "running", "health": "healthy"},
     "healthy", 1, 1, None),
])
def test_status_summary_honours_labelled_oneshot_lifecycle(
    monkeypatch, tmp_path, container, status, total, healthy, issue_code
):
    configure_store(monkeypatch, tmp_path)
    now = int(time.time())
    container["labels"] = {
        "com.homelabhq.lifecycle": "oneshot",
        "com.docker.compose.oneoff": "False",
    }
    store.update(lambda document: document["computeInstances"].update({
        "host": {
            "id": "host", "host": "docker.example.test",
            "dockerDiscoveryState": {"state": "successful", "lastJobAt": now},
            "docker": {"containers": [container]},
        },
    }))

    result = status_summary(now)

    assert result.docker.status == status
    assert result.docker.total == total
    assert result.docker.healthy == healthy
    assert [issue.code for issue in result.docker.issues] == (
        [issue_code] if issue_code else [])


def test_status_summary_distinguishes_proxmox_unreachable(monkeypatch, tmp_path):
    configure_store(monkeypatch, tmp_path)
    now = int(time.time())
    device = _device("pve", "proxmox.ve", now)
    device["proxmoxMaintenance"] = {
        "checkedAt": now,
        "nodes": {
            "pve-a": {"status": "online"},
            "pve-b": {"status": "unreachable"},
            "pve-c": {"status": "online"},
        },
    }
    store.update(lambda document: document["devices"].update(pve=device))
    result = status_summary(now)
    assert result.proxmox.status == "critical"
    assert result.proxmox.total == 3 and result.proxmox.healthy == 2
    assert result.proxmox.issues[0].component == "pve-b"
    assert result.proxmox.issues[0].code == "node_unreachable"


def test_status_summary_reports_truenas_degradation_and_alerts(monkeypatch, tmp_path):
    configure_store(monkeypatch, tmp_path)
    now = int(time.time())
    store.update(lambda document: document["devices"].update(nas=_device(
        "nas", "truenas.system", now,
        values={"pool_health": "DEGRADED", "alerts": 2},
    )))
    result = status_summary(now)
    assert result.truenas.status == "warning"
    assert result.truenas.pool == "DEGRADED" and result.truenas.active_alerts == 2
    assert result.truenas.issues[0].code == "pool_degraded"


@pytest.mark.parametrize(("level", "expected"), [
    ("WARNING", "warning"),
    ("CRITICAL", "critical"),
])
def test_status_summary_uses_active_truenas_alert_severity(
    monkeypatch, tmp_path, level, expected
):
    configure_store(monkeypatch, tmp_path)
    now = int(time.time())
    store.update(lambda document: document["devices"].update(nas=_device(
        "nas", "truenas.system", now,
        values={"pool_health": "ONLINE", "alerts": 1, "alert_level": level},
    )))

    result = status_summary(now)

    assert result.truenas.status == expected
    assert result.truenas.pool == "ONLINE" and result.truenas.active_alerts == 1
    assert result.truenas.issues[0].code == "active_alerts"


def test_status_summary_does_not_infer_truenas_health_without_monitoring_data(
    monkeypatch, tmp_path
):
    configure_store(monkeypatch, tmp_path)
    now = int(time.time())
    store.update(lambda document: document["devices"].update(
        nas=_device("nas", "truenas.system", now)
    ))

    result = status_summary(now)

    assert result.truenas.status == "unknown"
    assert result.truenas.pool is None and result.truenas.active_alerts is None
    assert result.truenas.issues[0].code == "monitoring_unavailable"


def test_status_summary_marks_truenas_monitoring_data_stale(monkeypatch, tmp_path):
    configure_store(monkeypatch, tmp_path)
    now = int(time.time())
    store.update(lambda document: document["devices"].update(nas=_device(
        "nas", "truenas.system", now - 1000,
        values={"pool_health": "ONLINE", "alerts": 0},
    )))

    result = status_summary(now)

    assert result.truenas.status == "stale"
    assert result.truenas.pool == "ONLINE" and result.truenas.active_alerts == 0
    assert result.truenas.issues[0].code == "monitoring_stale"


def test_status_summary_marks_old_network_data_stale(monkeypatch, tmp_path):
    configure_store(monkeypatch, tmp_path)
    now = int(time.time())
    store.update(lambda document: document["devices"].update(
        switch=_device("switch", "keeplink.switch", now - 1000)
    ))
    result = status_summary(now)
    assert result.network.status == "stale"
    assert result.network.healthy == 0 and result.stale is True
    assert result.network.issues[0].code == "monitoring_stale"


def test_status_summary_marks_old_docker_discovery_stale(monkeypatch, tmp_path):
    configure_store(monkeypatch, tmp_path)
    now = int(time.time())
    store.update(lambda document: document["computeInstances"].update({
        "host": {
            "id": "host",
            "dockerDiscoveryState": {"state": "successful", "lastJobAt": now - 1000},
            "docker": {"containers": [
                {"name": "web", "state": "running", "health": "healthy"},
                {"name": "setup", "state": "exited", "exitCode": 0,
                 "oneShot": True},
            ]},
        },
    }))
    result = status_summary(now)
    assert result.docker.status == "stale" and result.stale is True
    assert result.docker.total == 1 and result.docker.healthy == 0
    assert result.docker.issues[0].code == "monitoring_stale"


def test_status_summary_uses_unknown_for_missing_poll_and_warning_for_transient_failure(
    monkeypatch, tmp_path
):
    configure_store(monkeypatch, tmp_path)
    now = int(time.time())
    unknown = _device("ap-unknown", "zyxel.ap", now)
    unknown.pop("state")
    warning = _device("ap-warning", "zyxel.ap", now)
    warning["state"].update(online=False, confirmedOnline=True)
    store.update(lambda document: document["devices"].update({
        "unknown": unknown, "warning": warning,
    }))
    result = status_summary(now)
    assert result.network.status == "warning"
    assert {issue.status for issue in result.network.issues} == {"unknown", "warning"}

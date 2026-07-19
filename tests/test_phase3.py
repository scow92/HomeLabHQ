import http.client
import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

from backend.api import all_routes
from backend.api import auth_routes
from backend.api import device_routes
from backend.http.handler import Handler
from backend.http.hq_server import ThreadingHTTPServer
from backend.http.responses import JsonResponse
from backend.http.router import AuthPolicy, Route, Router
import auth
from context import Actor, Role
import store


def configure_store(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(store, "DB_FILE", str(tmp_path / "homelabhq.json"))
    monkeypatch.setattr(store, "LOCK_FILE", str(tmp_path / "homelabhq.lock"))
    store._cache.update(doc=None, mtime=None)


@pytest.fixture
def http_server(monkeypatch, tmp_path):
    """Run the production handler on an ephemeral local port."""
    configure_store(monkeypatch, tmp_path)
    auth._auth_fails.clear()
    monkeypatch.setattr(Handler, "router", Router(all_routes()))
    monkeypatch.setattr(Handler, "tls_enabled", False)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, name="test-phase3-http")
    thread.start()
    host, port = server.server_address

    def request(method, path, *, body=None, headers=None):
        supplied_headers = dict(headers or {})
        payload = None
        if body is not None:
            payload = json.dumps(body).encode()
            supplied_headers.setdefault("Content-Type", "application/json")
        connection = http.client.HTTPConnection(host, port, timeout=2)
        try:
            connection.request(method, path, body=payload, headers=supplied_headers)
            response = connection.getresponse()
            raw = response.read()
            value = json.loads(raw) if raw else None
            return response.status, value, response.headers
        finally:
            connection.close()

    request.origin = f"http://{host}:{port}"
    try:
        yield request
    finally:
        server.shutdown()
        server.server_close()
        thread.join(1)
        assert not thread.is_alive()


def test_router_extracts_named_path_parameters_without_a_socket():
    route = Route("GET", "/api/devices/{device_id}/state", lambda request: None,
                  AuthPolicy.AUTHENTICATED, "device-state")
    resolved, params = Router([route]).resolve("GET", "/api/devices/nas-1/state")
    assert resolved is route
    assert params == {"device_id": "nas-1"}


def test_all_routes_declare_an_explicit_authentication_policy():
    routes = all_routes()
    assert len(routes) > 20
    assert all(route.name and isinstance(route.auth, AuthPolicy) for route in routes)


def test_route_function_can_be_tested_without_http_server(monkeypatch):
    actor = object()
    request = SimpleNamespace(
        params={"device_id": "nas-1"},
        query_value=lambda name, default=None: {"key": "cpu", "range": "24h"}.get(name, default),
        require_actor=lambda: actor,
    )
    monkeypatch.setattr(device_routes.services, "device_history",
                        lambda supplied_actor, device_id, key, range_name: {
                            "actor": supplied_actor, "device": device_id,
                            "key": key, "range": range_name,
                        })
    response = device_routes.history(request)
    assert isinstance(response, JsonResponse)
    assert response.value["series"] == {
        "actor": actor, "device": "nas-1", "key": "cpu", "range": "24h",
    }


def test_password_route_passes_current_password_and_session_token(monkeypatch):
    actor = Actor("alice", Role.MEMBER)
    supplied = []
    monkeypatch.setattr(
        auth_routes.auth,
        "set_password",
        lambda user_id, current, new, token: supplied.append(
            (user_id, current, new, token)
        ) or 2,
    )
    request = SimpleNamespace(
        body={"currentPassword": "old-password", "password": "new-password"},
        require_actor=lambda: actor,
        handler=SimpleNamespace(token=lambda: "raw-session-token"),
    )

    response = auth_routes.set_password(request)

    assert supplied == [("alice", "old-password", "new-password", "raw-session-token")]
    assert response.value == {"ok": True, "sessionsRevoked": 2}


def test_real_handler_enforces_public_authenticated_and_admin_policies(http_server):
    auth.create_initial_admin("admin", "admin-password-for-http-tests")
    auth.create_user("member", "member-password-for-http-tests")
    admin_token, _ = auth.login("admin", "admin-password-for-http-tests")
    member_token, _ = auth.login("member", "member-password-for-http-tests")

    status, body, _ = http_server("GET", "/api/session")
    assert status == 200
    assert body == {"authenticated": False, "needsSetup": False, "user": None}

    status, body, _ = http_server("GET", "/api/devices")
    assert status == 401
    assert body == {"error": "unauthenticated"}

    status, body, _ = http_server(
        "GET", "/api/users", headers={"Cookie": f"{auth.COOKIE_NAME}={member_token}"}
    )
    assert status == 403
    assert body == {"error": "admin only"}

    status, body, _ = http_server(
        "GET", "/api/users", headers={"Cookie": f"{auth.COOKIE_NAME}={admin_token}"}
    )
    assert status == 200
    assert {user["username"] for user in body["users"]} == {"admin", "member"}
    assert all("passHash" not in user for user in body["users"])


def test_real_handler_enforces_same_origin_and_session_cookie_lifecycle(http_server):
    password = "admin-password-for-http-tests"
    auth.create_initial_admin("admin", password)
    credentials = {"username": "admin", "password": password}

    for headers in (
        {"Origin": "https://attacker.example"},
        {"Sec-Fetch-Site": "cross-site"},
    ):
        status, body, response_headers = http_server(
            "POST", "/api/login", body=credentials, headers=headers
        )
        assert status == 403
        assert body == {"error": "cross-origin request blocked"}
        assert response_headers.get("Set-Cookie") is None

    status, body, response_headers = http_server(
        "POST", "/api/login", body=credentials, headers={"Origin": http_server.origin}
    )
    assert status == 200
    assert body["user"]["username"] == "admin"
    set_cookie = response_headers.get("Set-Cookie")
    assert set_cookie is not None
    assert "HttpOnly" in set_cookie
    assert "Path=/" in set_cookie
    assert "SameSite=Lax" in set_cookie
    assert f"Max-Age={auth.SESSION_TTL}" in set_cookie
    assert "Secure" not in set_cookie
    cookie = set_cookie.split(";", 1)[0]

    status, body, _ = http_server("GET", "/api/session", headers={"Cookie": cookie})
    assert status == 200
    assert body["authenticated"] is True
    assert body["user"]["username"] == "admin"

    status, body, response_headers = http_server(
        "POST", "/api/logout", body={},
        headers={"Cookie": cookie, "Origin": http_server.origin},
    )
    assert status == 200
    assert body == {"ok": True}
    assert "Max-Age=0" in response_headers.get("Set-Cookie", "")

    status, body, _ = http_server("GET", "/api/session", headers={"Cookie": cookie})
    assert status == 200
    assert body["authenticated"] is False


def test_tls_session_cookies_are_secure():
    handler = Handler.__new__(Handler)
    handler.tls_enabled = True

    assert "; Secure;" in handler.set_session_cookie("token")[1]
    assert "; Secure;" in handler.clear_session_cookie()[1]


def test_admin_device_assignment_keeps_the_selected_devices_owner(http_server):
    auth.create_initial_admin("admin", "admin-password-for-http-tests")
    admin_token, _ = auth.login("admin", "admin-password-for-http-tests")

    def seed(document):
        document["users"].update({
            "alice": {"id": "alice", "username": "alice", "role": "member"},
            "bob": {"id": "bob", "username": "bob", "role": "member"},
        })
        document["devices"]["alice-device"] = {
            "id": "alice-device", "ownerId": "alice", "name": "Alice device",
            "host": "alice-device.example", "transport": "https",
        }
        document["dashboards"].update({
            "alice-dashboard": {
                "id": "alice-dashboard", "ownerId": "alice", "name": "Alice",
            },
            "bob-dashboard": {
                "id": "bob-dashboard", "ownerId": "bob", "name": "Bob",
            },
        })

    store.update(seed)
    headers = {
        "Cookie": f"{auth.COOKIE_NAME}={admin_token}",
        "Origin": http_server.origin,
    }

    status, body, _ = http_server(
        "PATCH", "/api/devices/alice-device",
        body={"dashboardId": "bob-dashboard"}, headers=headers,
    )
    assert status == 400
    assert body == {"error": "dashboard must have the same owner as the device"}
    assert store.load()["devices"]["alice-device"].get("dashboardId") is None

    status, body, _ = http_server(
        "PATCH", "/api/devices/alice-device",
        body={"dashboardId": "alice-dashboard"}, headers=headers,
    )
    assert status == 200
    assert body["device"]["dashboardId"] == "alice-dashboard"

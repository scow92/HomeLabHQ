import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

from backend.api import all_routes
from backend.api import auth_routes
from backend.api import device_routes
from backend.api import vpn_endpoints
from backend.api.contracts import AuthPolicy, JsonResponse, Route
from backend.asgi.compat import HandlerFacade
from backend.asgi.main import create_app
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
    """Exercise the production FastAPI routing stack without infrastructure I/O."""
    configure_store(monkeypatch, tmp_path)
    auth._auth_fails.clear()
    client = TestClient(create_app(use_lifespan=False), base_url="http://testserver")

    def request(method, path, *, body=None, headers=None):
        supplied_headers = dict(headers or {})
        if body is not None:
            supplied_headers.setdefault("Content-Type", "application/json")
        response = client.request(
            method, path,
            content=json.dumps(body).encode() if body is not None else None,
            headers=supplied_headers,
        )
        value = response.json() if response.content else None
        return response.status_code, value, response.headers

    request.origin = "http://testserver"
    with client:
        yield request


def test_fastapi_registers_named_path_parameters():
    route = Route("GET", "/api/devices/{device_id}/state", lambda request: None,
                  AuthPolicy.AUTHENTICATED, "device-state")
    assert route.path == "/api/devices/{device_id}/state"
    schema = create_app(use_lifespan=False).openapi()
    registered = {
        (method.upper(), path)
        for path, operations in schema["paths"].items()
        for method in operations
    }
    assert ("GET", route.path) in registered


def test_all_routes_declare_an_explicit_authentication_policy():
    routes = all_routes()
    assert len(routes) > 20
    assert all(route.name and isinstance(route.auth, AuthPolicy) for route in routes)


def test_every_compatibility_route_is_registered_with_fastapi():
    schema = create_app(use_lifespan=False).openapi()
    registered = {
        (method.upper(), path)
        for path, operations in schema["paths"].items()
        for method in operations
    }
    assert {(route.method, route.path) for route in all_routes()} <= registered


def test_vpn_profile_patch_reaches_real_authenticated_handler(http_server, monkeypatch):
    password = "admin-password-for-vpn-http-tests"
    auth.create_initial_admin("admin", password)
    token, user = auth.login("admin", password)
    store.update(lambda document: document["devices"].update({
        "vpn-device": {
            "id": "vpn-device", "ownerId": user["id"], "name": "Test firewall",
            "host": "192.0.2.1", "transport": "api", "driverId": "opnsense.firewall",
        },
    }))
    cookie = f"{auth.COOKIE_NAME}={token}"
    path = "/api/devices/vpn-device/vpn-endpoints"
    body = {"enabled": False, "maxCandidates": 7, "preferredOwners": [],
            "excludedOwners": [], "compatibilityTargets": []}

    status, response, _ = http_server(
        "PATCH", path, body=body, headers={"Origin": http_server.origin})
    assert status == 401 and response["error"] == "unauthenticated"
    assert response["code"] == "authentication_required" and response["requestId"]

    status, response, _ = http_server(
        "PATCH", path, body=body,
        headers={"Cookie": cookie, "Origin": "https://attacker.example"})
    assert status == 403 and response["error"] == "cross-origin request blocked"

    status, response, _ = http_server(
        "PATCH", path, body=body,
        headers={"Cookie": cookie, "Origin": http_server.origin})
    assert status == 200
    assert response["profile"]["maxCandidates"] == 7
    saved_profiles = store.load()["devices"]["vpn-device"]["vpnEndpointProfiles"]
    assert saved_profiles[0]["maxCandidates"] == 7

    status, response, _ = http_server(
        "POST", path, body={"name": "Netherlands", "enabled": False,
                            "country": "Netherlands", "city": "Amsterdam"},
        headers={"Cookie": cookie, "Origin": http_server.origin})
    assert status == 201
    netherlands_id = response["profile"]["id"]
    status, response, _ = http_server(
        "PATCH", path + "/" + netherlands_id, body={"notes": "Second tunnel"},
        headers={"Cookie": cookie, "Origin": http_server.origin})
    assert status == 200 and response["profile"]["notes"] == "Second tunnel"
    status, response, _ = http_server(
        "GET", path, headers={"Cookie": cookie})
    assert status == 200
    assert [item["profile"]["name"] for item in response["profiles"]] == [
        "VPN endpoint", "Netherlands"]

    routed = []
    monkeypatch.setattr(
        vpn_endpoints.services, "vpn_endpoint_compatibility",
        lambda actor, device_id, candidate_id, target_id, state, note:
        routed.append(("validation", device_id, candidate_id, target_id, state)),
    )
    monkeypatch.setattr(
        vpn_endpoints.services, "vpn_endpoint_switch",
        lambda actor, device_id, candidate_id, confirmed:
        routed.append(("switch", device_id, candidate_id, confirmed))
        or {"ok": True, "rollback": None},
    )
    status, response, _ = http_server(
        "POST", path + "/compatibility",
        body={"candidateId": "candidate", "targetId": "target", "state": "Verified"},
        headers={"Cookie": cookie, "Origin": http_server.origin})
    assert status == 200 and response == {"ok": True}
    status, response, _ = http_server(
        "POST", path + "/switch", body={"candidateId": "candidate", "confirmed": True},
        headers={"Cookie": cookie, "Origin": http_server.origin})
    assert status == 200 and response["ok"] is True
    assert routed == [
        ("validation", "vpn-device", "candidate", "target", "Verified"),
        ("switch", "vpn-device", "candidate", True),
    ]


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
    assert body["error"] == "unauthenticated"

    status, body, _ = http_server(
        "GET", "/api/users", headers={"Cookie": f"{auth.COOKIE_NAME}={member_token}"}
    )
    assert status == 403
    assert body["error"] == "admin only"

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
        assert body["error"] == "cross-origin request blocked"
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
    assert response_headers.get("X-Content-Type-Options") == "nosniff"
    assert response_headers.get("Referrer-Policy") == "no-referrer"
    assert response_headers.get("Permissions-Policy") == (
        "camera=(), geolocation=(), microphone=()"
    )
    assert response_headers.get("X-Frame-Options") == "SAMEORIGIN"
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


def _starlette_request(*, scheme="http", headers=()):
    return Request({
        "type": "http", "http_version": "1.1", "method": "GET", "scheme": scheme,
        "path": "/", "raw_path": b"/", "query_string": b"", "headers": headers,
        "client": ("127.0.0.1", 1234), "server": ("testserver", 80),
    })


def test_tls_session_cookies_are_secure():
    handler = HandlerFacade(_starlette_request(scheme="https"))
    assert "; Secure;" in handler.set_session_cookie("token")[1]
    assert "; Secure;" in handler.clear_session_cookie()[1]


def test_reverse_proxy_session_cookies_are_secure_only_for_external_https(monkeypatch):
    config = SimpleNamespace(external_https=True, trust_proxy=False)
    monkeypatch.setattr("backend.asgi.compat.settings", config)
    handler = HandlerFacade(_starlette_request())
    assert "; Secure;" in handler.set_session_cookie("token")[1]

    config.external_https = False
    config.trust_proxy = True
    handler = HandlerFacade(_starlette_request(headers=[(b"x-forwarded-proto", b"https")]))
    assert "; Secure;" in handler.set_session_cookie("token")[1]

    handler = HandlerFacade(_starlette_request(headers=[(b"x-forwarded-proto", b"http")]))
    assert "; Secure;" not in handler.set_session_cookie("token")[1]


def test_login_failure_tracking_is_bounded_and_success_clears_failures(monkeypatch):
    monkeypatch.setattr(auth, "_AUTH_FAIL_KEYS_MAX", 2)
    auth._auth_fails.clear()
    auth.record_login_fail("192.0.2.1")
    auth.record_login_fail("192.0.2.2")
    auth.record_login_fail("192.0.2.3")

    assert list(auth._auth_fails) == ["192.0.2.2", "192.0.2.3"]

    cleared = []
    monkeypatch.setattr(auth_routes.auth, "login_locked", lambda ip: False)
    monkeypatch.setattr(auth_routes.auth, "login", lambda username, password: ("token", {}))
    monkeypatch.setattr(auth_routes.auth, "clear_login_fails", cleared.append)
    request = SimpleNamespace(
        body={"username": "alice", "password": "a-valid-password"},
        handler=SimpleNamespace(
            client_ip=lambda: "192.0.2.3",
            set_session_cookie=lambda token: ("Set-Cookie", token),
        ),
    )

    auth_routes.login(request)

    assert cleared == ["192.0.2.3"]


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
    assert body["error"] == "dashboard must have the same owner as the device"
    assert store.load()["devices"]["alice-device"].get("dashboardId") is None

    status, body, _ = http_server(
        "PATCH", "/api/devices/alice-device",
        body={"dashboardId": "alice-dashboard"}, headers=headers,
    )
    assert status == 200
    assert body["device"]["dashboardId"] == "alice-dashboard"

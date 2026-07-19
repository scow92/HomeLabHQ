import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

from backend.api import all_routes
from backend.api import auth_routes
from backend.api import device_routes
from backend.http.responses import JsonResponse
from backend.http.router import AuthPolicy, Route, Router
from context import Actor, Role


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

"""Administrator-only user and diagnostic routes."""
import logbuf
import services

from errors import ValidationError
from backend.http.router import AuthPolicy, Route
from backend.http.responses import json_response


def list_users(request):
    return json_response({"users": services.list_users(request.require_actor())})


def create_user(request):
    body = request.body
    actor = request.require_actor()
    return json_response({"user": services.create_user(
        actor, body.get("username"), body.get("password"), body.get("role", "member"))})


def delete_user(request):
    user_id = request.query_value("id")
    if not user_id:
        raise ValidationError("id required")
    services.delete_user(request.require_actor(), user_id)
    return json_response({"ok": True})


def logs(request):
    return json_response({"logs": list(logbuf.REQUEST_LOG)[::-1]})


def clear_logs(request):
    logbuf.REQUEST_LOG.clear()
    return json_response({"ok": True})


def routes():
    return (
        Route("GET", "/api/users", list_users, AuthPolicy.ADMIN, "users-list"),
        Route("POST", "/api/users", create_user, AuthPolicy.ADMIN, "users-create"),
        Route("DELETE", "/api/users", delete_user, AuthPolicy.ADMIN, "users-delete"),
        Route("GET", "/api/logs", logs, AuthPolicy.ADMIN, "logs-list"),
        Route("DELETE", "/api/logs", clear_logs, AuthPolicy.ADMIN, "logs-clear"),
    )

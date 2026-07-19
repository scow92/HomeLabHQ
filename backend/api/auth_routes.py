"""Session, initial setup, and account routes."""
import auth

from backend.http.router import AuthPolicy, Route
from backend.http.responses import json_response


def session(request):
    user = request.current_user
    return json_response({
        "authenticated": bool(user),
        "needsSetup": not auth.has_any_user(),
        "user": user,
    })


def setup(request):
    body = request.body
    auth.create_initial_admin(body.get("username"), body.get("password"))
    token, user = auth.login(body.get("username"), body.get("password"))
    return json_response({"user": user}, headers=(request.handler.set_session_cookie(token),))


def login(request):
    body = request.body
    ip = request.handler.client_ip()
    if auth.login_locked(ip):
        return json_response({"error": "too many attempts"}, 429)
    token, user = auth.login(body.get("username"), body.get("password"))
    if not token:
        auth.record_login_fail(ip)
        return json_response({"error": "invalid credentials"}, 401)
    return json_response({"user": user}, headers=(request.handler.set_session_cookie(token),))


def logout(request):
    auth.logout(request.handler.token())
    return json_response({"ok": True}, headers=(request.handler.clear_session_cookie(),))


def set_password(request):
    body = request.body
    actor = request.require_actor()
    revoked = auth.set_password(
        actor.user_id,
        body.get("currentPassword"),
        body.get("password"),
        request.handler.token(),
    )
    return json_response({"ok": True, "sessionsRevoked": revoked})


def routes():
    return (
        Route("GET", "/api/session", session, AuthPolicy.PUBLIC, "session"),
        Route("POST", "/api/setup", setup, AuthPolicy.PUBLIC, "setup"),
        Route("POST", "/api/login", login, AuthPolicy.PUBLIC, "login"),
        Route("POST", "/api/logout", logout, AuthPolicy.PUBLIC, "logout"),
        Route("POST", "/api/account/password", set_password, AuthPolicy.AUTHENTICATED,
              "account-password"),
    )

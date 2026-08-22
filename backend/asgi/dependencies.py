"""FastAPI dependencies for sessions, actor authorization, and CSRF checks."""
from http.cookies import SimpleCookie

from fastapi import Request, Security
from fastapi.security import APIKeyCookie

import auth
import services
from context import Actor
from errors import AuthenticationRequired, Forbidden


session_cookie = APIKeyCookie(
    name=auth.COOKIE_NAME,
    scheme_name="HomelabHQSession",
    description="HttpOnly session cookie returned by setup or login.",
    auto_error=False,
)


def token_from_request(request: Request) -> str | None:
    raw = request.headers.get("cookie")
    if not raw:
        return None
    try:
        cookies = SimpleCookie()
        cookies.load(raw)
        morsel = cookies.get(auth.COOKIE_NAME)
        return morsel.value if morsel else None
    except Exception:
        return None


def current_user(request: Request, _cookie: str | None = Security(session_cookie)) -> dict | None:
    user = auth.user_for_token(token_from_request(request))
    request.state.current_user = user
    return user


def authenticated_actor(request: Request, user: dict | None = Security(current_user)) -> Actor:
    if not user:
        raise AuthenticationRequired()
    actor = Actor.from_user(user)
    request.state.actor = actor
    return actor


def administrator_actor(
    request: Request, actor: Actor = Security(authenticated_actor)
) -> Actor:
    services.require_admin(actor)
    request.state.actor = actor
    return actor


def same_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin is not None:
        host = request.headers.get("host", "")
        if origin not in {f"http://{host}", f"https://{host}"}:
            raise Forbidden("cross-origin request blocked")
        return
    site = request.headers.get("sec-fetch-site")
    if site is not None and site not in {"same-origin", "none"}:
        raise Forbidden("cross-origin request blocked")

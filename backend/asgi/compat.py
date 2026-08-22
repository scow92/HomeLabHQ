"""Compatibility adapter from existing route functions to FastAPI."""
from dataclasses import dataclass, field
import json
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response as FastAPIResponse
from starlette.concurrency import run_in_threadpool

import auth
from context import Actor
from errors import ValidationError

from backend.api import all_routes
from backend.api.contracts import AuthPolicy, FileResponse, JsonResponse, Route
from backend.asgi.config import settings
from backend.asgi.dependencies import (
    administrator_actor,
    authenticated_actor,
    current_user,
    same_origin,
    token_from_request,
)
from backend.asgi.models import ErrorResponse, request_schema
from backend.asgi.middleware import client_ip


@dataclass
class RequestFacade:
    """The narrow request helper consumed by the existing domain route modules."""

    request: Request
    raw_body: bytes
    params: dict[str, str]
    actor: Actor | None = None
    _body: dict | None = field(default=None, init=False, repr=False)

    @property
    def handler(self):
        return HandlerFacade(self.request)

    @property
    def query(self):
        return parse_qs(self.request.url.query)

    def query_value(self, name, default=None):
        return (self.query.get(name) or [default])[0]

    @property
    def body(self):
        if self._body is None:
            content_type = self.request.headers.get("content-type", "")
            if content_type.split(";", 1)[0].strip().lower() != "application/json":
                raise ValidationError("Content-Type must be application/json")
            if not self.raw_body:
                self._body = {}
            else:
                try:
                    value = json.loads(self.raw_body)
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise ValidationError("invalid JSON body") from error
                if not isinstance(value, dict):
                    raise ValidationError("JSON body must be an object")
                self._body = value
        return self._body

    @property
    def current_user(self):
        return auth.user_for_token(token_from_request(self.request))

    def require_actor(self):
        if self.actor is None:
            from errors import AuthenticationRequired
            raise AuthenticationRequired()
        return self.actor


class HandlerFacade:
    """Cookie and connection helpers retained by session route functions."""

    max_json_body_bytes = settings.max_json_body_bytes

    def __init__(self, request: Request):
        self.request = request

    def client_ip(self):
        return client_ip(self.request)

    def token(self):
        return token_from_request(self.request)

    def current_user(self):
        return auth.user_for_token(self.token())

    def _secure_cookie(self):
        if self.request.url.scheme == "https" or settings.external_https:
            return True
        if not settings.trust_proxy:
            return False
        forwarded = self.request.headers.get("x-forwarded-proto", "")
        return forwarded.split(",", 1)[0].strip().lower() == "https"

    def set_session_cookie(self, token):
        secure = "; Secure" if self._secure_cookie() else ""
        value = (
            f"{auth.COOKIE_NAME}={token}; HttpOnly; Path=/; SameSite=Lax"
            f"{secure}; Max-Age={auth.SESSION_TTL}"
        )
        return "Set-Cookie", value

    def clear_session_cookie(self):
        secure = "; Secure" if self._secure_cookie() else ""
        return (
            "Set-Cookie",
            f"{auth.COOKIE_NAME}=; HttpOnly; Path=/; SameSite=Lax{secure}; Max-Age=0",
        )


async def _limited_body(request: Request) -> bytes:
    raw_length = request.headers.get("content-length")
    if raw_length is not None:
        if not raw_length.isascii() or not raw_length.isdecimal():
            raise ValidationError("invalid Content-Length")
        if int(raw_length) > settings.max_json_body_bytes:
            raise ValidationError("JSON body too large")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > settings.max_json_body_bytes:
            raise ValidationError("JSON body too large")
    return bytes(body)


def _actor(request: Request) -> Actor | None:
    return getattr(request.state, "actor", None)


def _to_response(response) -> FastAPIResponse:
    headers = dict(response.headers)
    if isinstance(response, JsonResponse):
        return JSONResponse(response.value, status_code=response.status, headers=headers)
    if isinstance(response, FileResponse):
        if response.cache_control:
            headers["Cache-Control"] = response.cache_control
        if response.filename:
            headers["Content-Disposition"] = f"attachment; filename={response.filename}"
        return FastAPIResponse(
            response.data,
            status_code=response.status,
            media_type=response.content_type,
            headers=headers,
        )
    raise TypeError("route must return a response contract")


def _tag(route: Route) -> str:
    path = route.path
    if path.startswith("/api/settings/ansible"):
        return "ansible"
    if path.startswith("/api/devices") or path == "/api/drivers":
        return "devices"
    if path.startswith("/api/compute"):
        return "compute"
    if path.startswith("/api/clients") or path.startswith("/api/nac"):
        return "network access"
    if path.startswith("/api/push") or path.startswith("/api/notifications"):
        return "notifications"
    if path.startswith("/api/morning-updates") or path.startswith("/api/settings/morning"):
        return "updates"
    if path.startswith("/api/dashboards"):
        return "dashboards"
    if path.startswith("/api/users") or path.startswith("/api/logs"):
        return "administration"
    return "sessions"


def compatibility_router() -> APIRouter:
    router = APIRouter()

    def build_endpoint(selected: Route):
        async def endpoint(request: Request):
            raw_body = await _limited_body(request)
            facade = RequestFacade(
                request=request,
                raw_body=raw_body,
                params={key: str(value) for key, value in request.path_params.items()},
                actor=_actor(request),
            )
            result = await run_in_threadpool(selected.endpoint, facade)
            return _to_response(result)

        endpoint.__name__ = selected.name.replace("-", "_")
        return endpoint

    for route in all_routes():
        dependencies = []
        if route.auth is AuthPolicy.AUTHENTICATED:
            dependencies.append(Depends(authenticated_actor))
        elif route.auth is AuthPolicy.ADMIN:
            dependencies.append(Depends(administrator_actor))
        elif route.name == "session":
            dependencies.append(Depends(current_user))
        if route.method in {"POST", "PATCH", "PUT", "DELETE"}:
            dependencies.append(Depends(same_origin))

        openapi_extra = None
        if route.method in {"POST", "PATCH", "PUT"}:
            openapi_extra = {
                "requestBody": {
                    "required": False,
                    "content": {"application/json": {"schema": request_schema(route.name)}},
                }
            }
        router.add_api_route(
            route.path,
            build_endpoint(route),
            methods=[route.method],
            name=route.name,
            tags=[_tag(route)],
            dependencies=dependencies,
            response_model=None,
            responses={
                400: {"model": ErrorResponse},
                401: {"model": ErrorResponse},
                403: {"model": ErrorResponse},
                404: {"model": ErrorResponse},
                500: {"model": ErrorResponse},
            },
            openapi_extra=openapi_extra,
        )
    return router

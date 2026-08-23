"""Transport-neutral API route and response contracts.

The domain route modules return these small values while FastAPI owns request
parsing, authentication dependencies, serialization, and OpenAPI generation.
Keeping the values transport-neutral lets route behaviour remain focused on
the existing service layer during the ASGI migration.
"""
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Callable


class AuthPolicy(StrEnum):
    PUBLIC = "public"
    AUTHENTICATED = "authenticated"
    ADMIN = "admin"


@dataclass(frozen=True)
class Route:
    method: str
    path: str
    endpoint: Callable
    auth: AuthPolicy = AuthPolicy.AUTHENTICATED
    name: str = ""

    def __post_init__(self):
        if not self.method or self.method.upper() != self.method:
            raise ValueError("route methods must be uppercase")
        if not self.path.startswith("/"):
            raise ValueError("route paths must start with '/'")


@dataclass(frozen=True)
class Response:
    status: int = 200
    headers: tuple[tuple[str, str], ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class JsonResponse(Response):
    value: object = None


@dataclass(frozen=True)
class FileResponse(Response):
    data: bytes = b""
    content_type: str = "application/octet-stream"
    filename: str | None = None
    cache_control: str = "no-store"


def json_response(value, status=200, headers=()):
    return JsonResponse(status=status, headers=tuple(headers), value=value)

"""HTTP response values and their only serialization point."""
from dataclasses import dataclass, field
import json

from errors import (ApplicationError, AuthenticationRequired, Conflict, Forbidden,
                    NotFound, UpstreamUnavailable, ValidationError)


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


def error_response(error: ApplicationError):
    status = {
        ValidationError: 400,
        AuthenticationRequired: 401,
        Forbidden: 403,
        NotFound: 404,
        Conflict: 409,
        UpstreamUnavailable: 502,
    }.get(type(error), 500)
    return json_response({"error": str(error)}, status)


def write_response(handler, response: Response, *, head=False):
    """Serialize a route result without exposing socket details to routes."""
    if isinstance(response, JsonResponse):
        data = json.dumps(response.value).encode()
        content_type = "application/json"
        cache_control = "no-store"
        filename = None
    elif isinstance(response, FileResponse):
        data = response.data
        content_type = response.content_type
        cache_control = response.cache_control
        filename = response.filename
    else:
        raise TypeError("route must return a Response")
    handler._record_response(response.status)
    handler.send_response(response.status)
    request_id = getattr(handler, "_request_id", None)
    if request_id:
        handler.send_header("X-Request-ID", request_id)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(data)))
    if cache_control:
        handler.send_header("Cache-Control", cache_control)
    if filename:
        handler.send_header("Content-Disposition", f"attachment; filename={filename}")
    for key, value in response.headers:
        handler.send_header(key, value)
    handler.end_headers()
    if not head:
        handler.wfile.write(data)

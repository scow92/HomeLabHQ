"""Request correlation, browser security headers, and safe request logging."""
import time
import uuid

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

import logbuf


CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; connect-src 'self'; base-uri 'self'; "
    "form-action 'self'; frame-ancestors 'self'; object-src 'none'"
)
DOCS_CSP = (
    "default-src 'self'; script-src 'self' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "img-src 'self' data: https://fastapi.tiangolo.com; connect-src 'self'; "
    "base-uri 'self'; form-action 'self'; frame-ancestors 'self'; object-src 'none'"
)


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        started = time.monotonic()
        request_id = uuid.uuid4().hex
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), geolocation=(), microphone=()"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Content-Security-Policy"] = (
            DOCS_CSP if request.url.path in {"/docs", "/redoc"} else CSP
        )
        if request.url.path.startswith("/api/") or request.url.path in {
            "/health", "/healthz", "/readyz", "/openapi.json"
        }:
            response.headers.setdefault("Cache-Control", "no-store")

        if request.url.path not in logbuf.LOG_SKIP_PATHS:
            route = request.scope.get("route")
            route_name = getattr(route, "name", None) or "not-found"
            status = response.status_code
            fields = {
                "ip": client_ip(request),
                "method": request.method,
                "path": request.url.path,
                "status": status,
                "ms": round((time.monotonic() - started) * 1000),
                "request_id": request_id,
                "route": route_name,
            }
            if status >= 400:
                fields["error"] = "request failed"
            logbuf.log_event(
                "error" if status >= 500 else "warn" if status >= 400 else "info",
                "request", source="http", **fields,
            )
        return response


def client_ip(request: Request) -> str:
    from backend.asgi.config import settings

    if settings.trust_proxy:
        forwarded = request.headers.get("x-real-ip")
        if forwarded:
            return forwarded[:128]
    return request.client.host if request.client else ""

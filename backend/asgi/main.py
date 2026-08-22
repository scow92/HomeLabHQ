"""HomelabHQ FastAPI application factory and lifespan."""
from contextlib import asynccontextmanager
from pathlib import Path
import os
import sys
import threading
import time

# Legacy business modules use top-level imports. Keep one canonical module
# identity while the application is launched as ``backend.asgi.main``.
BACKEND_DIR = str(Path(__file__).resolve().parents[1])
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

import compute_maintenance
import history
import logbuf
import morning_updates
import poller
import store
import tls
import transports
import uvicorn
from domain import safe_error
from errors import (
    ApplicationError,
    AuthenticationRequired,
    Conflict,
    Forbidden,
    NotFound,
    UpstreamUnavailable,
    ValidationError,
)

import drivers  # noqa: F401  # bundled drivers self-register

from backend.asgi.compat import compatibility_router
from backend.asgi.config import settings
from backend.asgi.middleware import RequestContextMiddleware
from backend.asgi.models import ErrorResponse
from backend.asgi.routers import icon_application, operational_router, static_router


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "unknown")


def _error(request: Request, status: int, code: str, message: str):
    payload = ErrorResponse(
        error=message,
        code=code,
        requestId=_request_id(request),
    )
    return JSONResponse(payload.model_dump(), status_code=status)


def _application_error(error: ApplicationError) -> tuple[int, str]:
    if isinstance(error, Conflict):
        return 409, "conflict"
    if isinstance(error, AuthenticationRequired):
        return 401, "authentication_required"
    if isinstance(error, Forbidden):
        return 403, "forbidden"
    if isinstance(error, NotFound):
        return 404, "not_found"
    if isinstance(error, UpstreamUnavailable):
        return 502, "upstream_unavailable"
    if isinstance(error, ValidationError):
        return 400, "validation_error"
    return 500, "application_error"


class IconServer:
    def __init__(self):
        config = uvicorn.Config(
            icon_application(),
            host=settings.host,
            port=settings.icon_http_port,
            access_log=False,
            server_header=False,
            timeout_keep_alive=settings.http_request_timeout,
            log_config=None,
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(
            target=self.server.run, name="icon-uvicorn", daemon=True
        )

    def start(self):
        self.thread.start()
        deadline = time.monotonic() + 2
        while not self.server.started and self.thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        if not self.server.started:
            raise RuntimeError("icon listener failed to start")

    def stop(self):
        self.server.should_exit = True
        self.thread.join(timeout=5)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    logbuf.configure_logging()
    integrity = store.startup_integrity_check()
    if not store.secrets_isolated_from_agents():
        credential_count = len(store.load().get("credentials", {}))
        if credential_count and not os.environ.get("HLHQ_ALLOW_UNSAFE_LOCAL_SECRETS"):
            raise RuntimeError(
                f"refusing local startup: {credential_count} credential(s) require "
                "container isolation or HLHQ_ALLOW_UNSAFE_LOCAL_SECRETS=1"
            )
    if integrity["migrated"]:
        logbuf.log_event(
            "info", "store_migration", source="startup",
            schema_version=integrity["schemaVersion"],
        )
    history.migrate_from_store()
    recovered_jobs = compute_maintenance.recover_interrupted_jobs()
    if recovered_jobs:
        logbuf.log_event(
            "warn", "compute_jobs_recovered", source="startup", jobs=recovered_jobs
        )
    poller.start()
    morning_updates.start_scheduler()
    icon_server = None
    if settings.tls_requested and tls.is_self_signed() and settings.icon_http_port:
        try:
            icon_server = IconServer()
            icon_server.start()
        except Exception as error:
            icon_server = None
            logbuf.log_event(
                "warn", "icon_listener", source="startup",
                port=settings.icon_http_port, error=safe_error(error),
            )
    logbuf.log_event(
        "info", "backend_started", source="startup",
        server="uvicorn", scheme="https" if settings.tls_requested else "http",
        host=settings.host, port=settings.port, data_dir=store.DATA_DIR,
    )
    try:
        yield
    finally:
        poller.stop()
        morning_updates.stop_scheduler()
        if icon_server is not None:
            icon_server.stop()
        logbuf.log_event("info", "backend_stopped", source="startup")


def create_app(*, use_lifespan: bool = True) -> FastAPI:
    app = FastAPI(
        title="HomelabHQ API",
        summary="Homelab infrastructure monitoring and management API",
        version=settings.version,
        lifespan=lifespan if use_lifespan else None,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )
    app.add_middleware(RequestContextMiddleware)

    @app.exception_handler(ApplicationError)
    async def application_error_handler(request: Request, error: ApplicationError):
        status, code = _application_error(error)
        return _error(request, status, code, str(error))

    @app.exception_handler(transports.ConnectionError)
    async def transport_error_handler(request: Request, _error_value):
        return _error(request, 502, "upstream_unavailable", "upstream unavailable")

    @app.exception_handler(RequestValidationError)
    async def request_validation_handler(request: Request, _error_value):
        return _error(request, 422, "request_validation_error", "request validation failed")

    @app.exception_handler(HTTPException)
    async def http_error_handler(request: Request, error: HTTPException):
        message = str(error.detail) if error.status_code < 500 else "internal server error"
        code = "not_found" if error.status_code == 404 else \
            "method_not_allowed" if error.status_code == 405 else "http_error"
        return _error(request, error.status_code, code, message)

    @app.exception_handler(ValueError)
    async def value_error_handler(request: Request, error: ValueError):
        return _error(request, 400, "validation_error", str(error))

    @app.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, error: Exception):
        logbuf.log_event(
            "error", "unhandled_exception", source="http",
            request_id=_request_id(request), path=request.url.path,
            error=safe_error(error),
        )
        return _error(request, 500, "internal_error", "internal server error")

    app.include_router(operational_router())
    app.include_router(compatibility_router())

    @app.api_route(
        "/api/{path:path}",
        methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        include_in_schema=False,
        name="api-not-found",
    )
    async def api_not_found():
        raise NotFound()

    app.include_router(static_router())
    return app


app = create_app()

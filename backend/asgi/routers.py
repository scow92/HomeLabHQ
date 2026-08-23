"""Operational, certificate, static-file, and SPA routes."""
import asyncio
from datetime import datetime
import mimetypes
from pathlib import Path
from urllib.parse import unquote

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

import poller
import store
import tls
from errors import NotFound, ValidationError

from backend.asgi.config import settings
from backend.asgi.models import HealthResponse, ReadinessResponse, StatusSummaryResponse
from backend.asgi.status_service import status_summary


STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript",
    ".css": "text/css",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".webmanifest": "application/manifest+json",
}
ICON_ASSETS = frozenset({
    "apple-touch-icon.png", "apple-touch-icon-precomposed.png", "icon-192.png",
    "icon-512.png", "icon-maskable-512.png", "icon-mark.svg", "favicon-32.png",
})


def _now() -> datetime:
    return datetime.now().astimezone()


def operational_router() -> APIRouter:
    router = APIRouter(tags=["operations"])

    @router.head("/health", include_in_schema=False)
    @router.get("/health", response_model=HealthResponse, name="health")
    @router.head("/api/v1/health", include_in_schema=False)
    @router.get(
        "/api/v1/health", response_model=HealthResponse, name="versioned-health"
    )
    async def health() -> HealthResponse:
        return HealthResponse(version=settings.version, timestamp=_now())

    @router.head("/healthz", include_in_schema=False)
    @router.get("/healthz", name="legacy-health", response_model=None)
    async def legacy_health():
        return {"ok": True}

    @router.head("/api/v1/readiness", include_in_schema=False)
    @router.get(
        "/api/v1/readiness", response_model=ReadinessResponse, name="readiness"
    )
    async def readiness():
        dependencies = {}
        try:
            await asyncio.wait_for(
                run_in_threadpool(store.load), timeout=settings.readiness_timeout_seconds
            )
            dependencies["datastore"] = {"status": "ready"}
        except TimeoutError:
            dependencies["datastore"] = {
                "status": "unavailable", "message": "datastore check timed out"
            }
        except Exception:
            dependencies["datastore"] = {
                "status": "unavailable", "message": "datastore is unavailable"
            }
        config_ready = settings.web_dir.is_dir() and (settings.web_dir / "index.html").is_file()
        dependencies["configuration"] = {
            "status": "ready" if config_ready else "unavailable",
            **({} if config_ready else {"message": "frontend directory is unavailable"}),
        }
        ready = all(item["status"] == "ready" for item in dependencies.values())
        payload = ReadinessResponse(
            status="healthy" if ready else "unhealthy",
            version=settings.version,
            timestamp=_now(),
            dependencies=dependencies,
        )
        return JSONResponse(
            payload.model_dump(mode="json"), status_code=200 if ready else 503
        )

    @router.head("/readyz", include_in_schema=False)
    @router.get("/readyz", name="legacy-readiness", response_model=None)
    async def legacy_readiness():
        try:
            await asyncio.wait_for(
                run_in_threadpool(store.load), timeout=settings.readiness_timeout_seconds
            )
            store_ready = True
        except Exception:
            store_ready = False
        state = poller.status()
        ready = store_ready and state["ready"]
        return JSONResponse({
            "ok": ready,
            "store": "ready" if store_ready else "unavailable",
            "poller": "ready" if state["ready"] else "starting",
        }, status_code=200 if ready else 503)

    @router.get(
        "/api/v1/status/summary",
        response_model=StatusSummaryResponse,
        name="status-summary",
    )
    def summary() -> StatusSummaryResponse:
        return status_summary()

    return router


def _safe_static_path(path: str) -> Path:
    root = settings.web_dir
    try:
        full = (root / unquote(path).lstrip("/")).resolve()
        full.relative_to(root)
        return full
    except (ValueError, OSError) as error:
        raise ValidationError("forbidden path") from error


def _rewrite_index(data: bytes, request: Request) -> bytes:
    if not (
        request.url.scheme == "https" and tls.is_self_signed() and settings.icon_http_port
    ):
        return data
    host = request.headers.get("host", "").split(":", 1)[0]
    if not host:
        return data
    icon = settings.web_dir / "apple-touch-icon.png"
    try:
        version = str(int(icon.stat().st_mtime))
    except OSError:
        version = "1"
    base = f"http://{host}:{settings.icon_http_port}".encode()
    return data.replace(
        b'rel="apple-touch-icon" href="/apple-touch-icon.png"',
        b'rel="apple-touch-icon" href="' + base
        + b'/apple-touch-icon.png?v=' + version.encode() + b'"',
    )


def static_router() -> APIRouter:
    router = APIRouter(include_in_schema=False)

    @router.api_route("/homelabhq.crt", methods=["GET", "HEAD"], name="certificate")
    @router.api_route("/nac.crt", methods=["GET", "HEAD"], name="legacy-certificate")
    async def certificate(request: Request):
        try:
            certfile, _ = await run_in_threadpool(tls.ensure_cert)
            data = await run_in_threadpool(Path(certfile).read_bytes)
        except Exception as error:
            raise NotFound("certificate unavailable") from error
        return Response(
            b"" if request.method == "HEAD" else data,
            media_type="application/x-x509-ca-cert",
            headers={
                "Content-Disposition": "attachment; filename=homelabhq.crt",
                "Content-Length": str(len(data)),
            },
        )

    @router.api_route("/{path:path}", methods=["GET", "HEAD"], name="frontend")
    async def frontend(path: str, request: Request):
        requested = _safe_static_path(path or "index.html")
        is_index = not path or path == "/"
        if requested.is_file():
            selected = requested
        elif Path(path).suffix:
            raise NotFound("static asset not found")
        else:
            selected = settings.web_dir / "index.html"
            is_index = True
            if not selected.is_file():
                raise NotFound("frontend not found")
        try:
            data = await run_in_threadpool(selected.read_bytes)
        except OSError as error:
            raise NotFound("static asset not found") from error
        if is_index or selected.name == "index.html":
            data = _rewrite_index(data, request)
        media_type = STATIC_TYPES.get(
            selected.suffix.lower(), mimetypes.guess_type(selected.name)[0]
            or "application/octet-stream"
        )
        return Response(
            b"" if request.method == "HEAD" else data,
            media_type=media_type,
            headers={"Content-Length": str(len(data))},
        )

    return router


def icon_application():
    from fastapi import FastAPI
    from fastapi.responses import RedirectResponse

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.api_route("/{path:path}", methods=["GET", "HEAD"])
    async def icon_or_redirect(path: str, request: Request):
        name = Path(path).name
        full = _safe_static_path(name)
        if name in ICON_ASSETS and full.is_file():
            data = await run_in_threadpool(full.read_bytes)
            return Response(
                b"" if request.method == "HEAD" else data,
                media_type=STATIC_TYPES.get(full.suffix.lower(), "application/octet-stream"),
                headers={"Cache-Control": "public, max-age=86400", "Content-Length": str(len(data))},
            )
        host = request.headers.get("host", "").split(":", 1)[0] or "localhost"
        return RedirectResponse(
            f"https://{host}:{settings.port}/{path}", status_code=301
        )

    return app

"""Static assets and public certificate delivery, separate from API routing."""
import os
from pathlib import Path
from urllib.parse import unquote

from .responses import FileResponse, json_response, write_response


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


def serve_static(handler, path, web_dir, csp, rewrite_index, *, head=False):
    if path == "/" or not path:
        path = "/index.html"
    root = Path(web_dir).resolve()
    try:
        full = (root / unquote(path).lstrip("/")).resolve()
        full.relative_to(root)
    except (ValueError, OSError):
        return write_response(handler, json_response({"error": "forbidden"}, 403), head=head)
    if not full.is_file():
        full = root / "index.html"
        if not full.is_file():
            return write_response(handler, json_response({"error": "not found"}, 404), head=head)
    try:
        data = full.read_bytes()
    except OSError:
        return write_response(handler, json_response({"error": "read failed"}, 500), head=head)
    if full.name == "index.html":
        data = rewrite_index(data)
    response = FileResponse(
        data=data,
        content_type=STATIC_TYPES.get(full.suffix.lower(), "application/octet-stream"),
        cache_control="",
        headers=(("Content-Security-Policy", csp),),
    )
    return write_response(handler, response, head=head)


def serve_certificate(handler, *, head=False):
    try:
        import tls
        certfile, _ = tls.ensure_cert()
        data = Path(certfile).read_bytes()
    except Exception as error:
        return write_response(handler, json_response({"error": f"no certificate: {error}"}, 500), head=head)
    return write_response(handler, FileResponse(
        data=data,
        content_type="application/x-x509-ca-cert",
        filename="homelabhq.crt",
        cache_control="",
    ), head=head)

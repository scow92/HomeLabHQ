#!/usr/bin/env python3
"""Compatibility entry point; HomelabHQ is served by Uvicorn/FastAPI."""
from pathlib import Path
import sys

BACKEND_DIR = str(Path(__file__).resolve().parent)
ROOT_DIR = str(Path(__file__).resolve().parent.parent)
for entry in (ROOT_DIR, BACKEND_DIR):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from backend.asgi.config import settings
from backend.asgi.main import app  # noqa: F401
from backend.run import main

WEB_DIR = str(settings.web_dir)
PORT = settings.port
MAX_JSON_BODY_BYTES = settings.max_json_body_bytes
ICON_HTTP_PORT = settings.icon_http_port
TRUST_PROXY = settings.trust_proxy
EXTERNAL_HTTPS = settings.external_https


if __name__ == "__main__":
    main()

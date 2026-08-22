"""Production Uvicorn launcher with HomelabHQ's built-in TLS support."""
from pathlib import Path
import sys

BACKEND_DIR = str(Path(__file__).resolve().parent)
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import logbuf
import tls
import uvicorn

from backend.asgi.config import settings


def main() -> None:
    ssl_certfile = None
    ssl_keyfile = None
    if settings.tls_requested:
        ssl_certfile, ssl_keyfile = tls.ensure_cert()
        logbuf.log_event(
            "info", "tls", source="startup", certificate=ssl_certfile,
            self_signed=tls.is_self_signed(),
        )
    uvicorn.run(
        "backend.asgi.main:app",
        host=settings.host,
        port=settings.port,
        workers=1,
        access_log=False,
        server_header=False,
        timeout_keep_alive=settings.http_request_timeout,
        proxy_headers=settings.trust_proxy,
        forwarded_allow_ips="127.0.0.1" if settings.trust_proxy else "",
        ssl_certfile=ssl_certfile,
        ssl_keyfile=ssl_keyfile,
    )


if __name__ == "__main__":
    main()

"""Generic HTTP web UI (username + password).

Fallback for any device with a web interface we don't have a specific driver
for. It can't parse a vendor's pages, so it surfaces transport-level facts
(reachability, status, latency, page title). Low confidence so a specific web
driver (Keeplink, …) always outranks it.
"""
import re

from .base import Driver, Entity, SENSOR
from .registry import register

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


class GenericHTTPDevice(Driver):
    id = "generic.http"
    display_name = "Generic HTTP web UI"
    transports = ["http"]

    def probe(self, conn) -> float:
        resp = conn.last  # connect() already fetched probe_path
        if resp is None:
            return 0.0
        return 0.3 if resp.status < 500 else 0.15

    def entities(self, conn):
        def reachable():
            return conn.get(conn.probe_path).status < 400

        def http_status():
            return conn.get(conn.probe_path).status

        def response_ms():
            return conn.get(conn.probe_path).elapsed_ms

        def page_title():
            m = _TITLE_RE.search(conn.get(conn.probe_path).text or "")
            return m.group(1).strip() if m else None

        return [
            Entity("reachable", "Reachable", SENSOR, read=reachable),
            Entity("http_status", "HTTP status", SENSOR, read=http_status),
            Entity("response_ms", "Response time", SENSOR, unit="ms",
                   read=response_ms),
            Entity("page_title", "Page title", SENSOR, read=page_title),
        ]


register(GenericHTTPDevice())

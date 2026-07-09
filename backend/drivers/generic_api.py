"""Generic HTTP/REST API device (API key + secret).

The broad fallback for anything exposing an authenticated HTTP API. It can't
know a vendor's schema, so it surfaces transport-level entities that are true
for any API — reachability, HTTP status, latency, content type — plus, when the
probe endpoint returns JSON, a count of its top-level keys. Vendor drivers
(OPNsense, UniFi, …) added later declare real entities and outrank this by
inspecting the response body.
"""
from .base import Driver, Entity, SENSOR
from .registry import register


class GenericAPIDevice(Driver):
    id = "generic.api"
    display_name = "Generic HTTP/REST API"
    transports = ["api"]

    def probe(self, conn) -> float:
        # connect() already fetched probe_path; inspect that response.
        resp = conn.last
        if resp is None:
            return 0.0
        if resp.status >= 500:
            return 0.2                      # reachable but erroring
        if resp.json() is not None:
            return 0.5                      # authenticated JSON API — good sign
        return 0.35                         # some HTTP response

    def entities(self, conn):
        def http_status():
            return conn.get(conn.probe_path).status

        def response_ms():
            return conn.get(conn.probe_path).elapsed_ms

        def content_type():
            return conn.get(conn.probe_path).headers.get("Content-Type")

        def json_keys():
            data = conn.get(conn.probe_path).json()
            if isinstance(data, dict):
                return len(data)
            if isinstance(data, list):
                return len(data)
            return None

        return [
            Entity("http_status", "HTTP status", SENSOR, read=http_status),
            Entity("response_ms", "Response time", SENSOR, unit="ms",
                   read=response_ms),
            Entity("content_type", "Content type", SENSOR, read=content_type),
            Entity("json_keys", "JSON top-level keys", SENSOR, read=json_keys),
        ]


register(GenericAPIDevice())

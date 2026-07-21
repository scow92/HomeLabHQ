"""The thread-per-request server configuration."""
import os

from http.server import HTTPServer
from socketserver import ThreadingMixIn


REQUEST_TIMEOUT_SECONDS = max(
    1.0, float(os.environ.get("HLHQ_HTTP_REQUEST_TIMEOUT", "30")))


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    # Let in-flight requests finish during ``server_close()`` instead of
    # abandoning them when the container receives SIGTERM.
    daemon_threads = False
    block_on_close = True
    request_timeout = REQUEST_TIMEOUT_SECONDS

    def get_request(self):
        request, client_address = super().get_request()
        request.settimeout(self.request_timeout)
        return request, client_address

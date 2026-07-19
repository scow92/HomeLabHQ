"""The thread-per-request server configuration."""
from http.server import HTTPServer
from socketserver import ThreadingMixIn


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    # Let in-flight requests finish during ``server_close()`` instead of
    # abandoning them when the container receives SIGTERM.
    daemon_threads = False
    block_on_close = True

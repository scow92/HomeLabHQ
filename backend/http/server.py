"""The thread-per-request server configuration."""
from http.server import HTTPServer
from socketserver import ThreadingMixIn


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = False
    block_on_close = True

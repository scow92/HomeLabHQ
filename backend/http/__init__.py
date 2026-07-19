"""Small standard-library HTTP adapter used by HomelabHQ.

Some legacy tools add ``backend`` directly to ``sys.path``.  In that mode this
directory is discovered as top-level ``http`` before Python's standard library.
Execute the standard library package here and search it first so third-party
code (for example aiohttp) continues to see ``http.HTTPStatus`` and
``http.server``.  HomelabHQ-specific modules remain available as
``backend.http.router`` and friends.
"""
import sysconfig
from pathlib import Path

_LOCAL = Path(__file__).parent
_STDLIB = Path(sysconfig.get_path("stdlib")) / "http"
if _STDLIB.is_dir():
    # Top-level ``http`` must prefer the standard library.  The namespaced
    # ``backend.http`` package prefers the local adapter so its documented
    # ``server.py`` remains importable.
    __path__[:] = ([str(_STDLIB), str(_LOCAL)] if __name__ == "http"
                    else [str(_LOCAL), str(_STDLIB)])
    _stdlib_init = _STDLIB / "__init__.py"
    exec(compile(_stdlib_init.read_bytes(), str(_stdlib_init), "exec"), globals())

"""Shared in-memory diagnostic log ring.

A tiny standalone module so both the web app (request/error logging) and the
background poller (reachability + poll-failure events) append to the SAME
buffer. The app runs as __main__ (`python3 backend/app.py`), so anything that
did `import app` would get a *second* module object with its own ring — hence
this neutral home that everyone imports by the same name. Bounded ring, lost on
restart, which is fine for a live diagnostic view.
"""
import collections
import time

REQUEST_LOG = collections.deque(maxlen=1000)

# API paths that would spam the ring (its own poll, health checks) — skipped.
LOG_SKIP_PATHS = frozenset({"/api/logs", "/healthz"})


def log_note(level, message, source="app"):
    """Record a free-form log line (startup, background tasks). Best-effort;
    never raises."""
    try:
        REQUEST_LOG.append({
            "ts": time.time(), "level": level, "source": source,
            "message": str(message)[:500],
        })
    except Exception:
        pass

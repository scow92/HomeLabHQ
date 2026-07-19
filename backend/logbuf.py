"""Shared in-memory diagnostic log ring.

A tiny standalone module so both the web app (request/error logging) and the
background poller (reachability + poll-failure events) append to the SAME
buffer. The app runs as __main__ (`python3 backend/app.py`), so anything that
did `import app` would get a *second* module object with its own ring — hence
this neutral home that everyone imports by the same name. Bounded ring, lost on
restart, which is fine for a live diagnostic view.
"""
import collections
import json
import logging
import re
import time


_LOGGER = logging.getLogger("homelabhq")
_SENSITIVE_FIELD = re.compile(
    r"(?:authorization|cookie|pass(?:word|wd)?|secret|token|credential|"
    r"private[_-]?key|api[_-]?key|vapid)", re.IGNORECASE)
_SENSITIVE_TEXT = re.compile(
    r"(?i)(authorization|cookie|pass(?:word|wd)?|secret|token|api[_-]?key|"
    r"private[_-]?key)\s*([=:])\s*[^\s,;]+")
_URL_CREDENTIALS = re.compile(r"(https?://[^:/\s]+:)[^@/\s]+@")

REQUEST_LOG = collections.deque(maxlen=1000)

# API paths that would spam the ring (its own poll, health checks) — skipped.
LOG_SKIP_PATHS = frozenset({"/api/logs", "/healthz", "/readyz"})


def redact(value, *, field_name=""):
    """Return a safe, JSON-friendly diagnostic value.

    Diagnostics cross several trust boundaries (HTTP headers, driver errors,
    and push-provider responses), so redaction belongs at the final logging
    boundary instead of relying on every caller to remember it.
    """
    if _SENSITIVE_FIELD.search(field_name):
        return "[redacted]"
    if isinstance(value, dict):
        return {str(key): redact(item, field_name=str(key)) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [redact(item) for item in value]
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    if isinstance(value, str):
        value = _SENSITIVE_TEXT.sub(r"\1\2[redacted]", value)
        return _URL_CREDENTIALS.sub(r"\1[redacted]@", value)[:500]
    return value


def configure_logging():
    """Configure a single line-delimited JSON stream for container logs."""
    if _LOGGER.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    _LOGGER.addHandler(handler)
    _LOGGER.setLevel(logging.INFO)
    _LOGGER.propagate = False


def log_event(level, event, source="app", **fields):
    """Record and emit one structured, automatically redacted event."""
    entry = {
        "ts": time.time(),
        "level": str(level).lower(),
        "event": str(event),
        "source": str(source),
    }
    entry.update(fields)
    entry = redact(entry)
    try:
        REQUEST_LOG.append(entry)
        configure_logging()
        level_no = {"debug": logging.DEBUG, "info": logging.INFO,
                    "warn": logging.WARNING, "warning": logging.WARNING,
                    "error": logging.ERROR}.get(entry["level"], logging.INFO)
        _LOGGER.log(level_no, json.dumps(entry, separators=(",", ":"), default=str))
    except Exception:
        pass
    return entry


def log_note(level, message, source="app"):
    """Record a free-form log line (startup, background tasks). Best-effort;
    never raises."""
    return log_event(level, "note", source=source, message=message)

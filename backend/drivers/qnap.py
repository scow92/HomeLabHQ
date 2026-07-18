"""QNAP QTS/QuTS NAS via the CGI WebAPI (http transport, username + password).

QNAP logs in at /cgi-bin/authLogin.cgi (password base64-encoded in the query),
returning an XML session id, then exposes system info CGIs. Responses are XML,
parsed defensively. DSM-style: the web UI is usually on port 8080 (http) /
443 (https) — set the port in the wizard.
"""
import base64
import xml.etree.ElementTree as ET

from .base import Driver, Entity, SENSOR
from .registry import register


def _xml(text):
    try:
        return ET.fromstring(text or "")
    except Exception:
        return None


def _find(root, *tags):
    """First matching descendant text for any of `tags` (case-insensitive)."""
    if root is None:
        return None
    wanted = {t.lower() for t in tags}
    for el in root.iter():
        if el.tag.lower() in wanted and (el.text or "").strip():
            return el.text.strip()
    return None


def _login(conn):
    pwd = base64.b64encode((conn.password or "").encode()).decode()
    try:
        r = conn.get("/cgi-bin/authLogin.cgi",
                     params={"user": conn.username or "", "pwd": pwd})
    except Exception:
        return None
    root = _xml(r.text)
    if _find(root, "authPassed") == "1":
        return _find(root, "authSid")
    return None


def _sysinfo(conn, sid):
    try:
        r = conn.get("/cgi-bin/management/manaRequest.cgi",
                     params={"subfunc": "sysinfo", "sid": sid})
    except Exception:
        return None
    return _xml(r.text)


class QNAP(Driver):
    id = "qnap.qts"
    display_name = "QNAP"
    transports = ["http"]

    def probe(self, conn) -> float:
        return 0.85 if _login(conn) else 0.0

    def entities(self, conn):
        cache = {}

        def info():
            if "i" not in cache:
                sid = _login(conn)
                cache["i"] = _sysinfo(conn, sid) if sid else None
            return cache["i"]

        def _temp(*tags):
            v = _find(info(), *tags)
            try:
                return int("".join(ch for ch in (v or "") if ch.isdigit()))
            except Exception:
                return None

        return [
            Entity("model", "Model", SENSOR,
                   read=lambda: _find(info(), "modelName", "model", "internalModelName")),
            Entity("firmware", "Firmware", SENSOR,
                   read=lambda: _find(info(), "firmwareVersion", "version", "fwVersion")),
            Entity("hostname", "Hostname", SENSOR,
                   read=lambda: _find(info(), "hostName", "hostname", "serverName")),
            Entity("cpu_temp", "CPU temperature", SENSOR, unit="°C",
                   read=lambda: _temp("cpuTemp", "cpu_temperature", "cpuTemperature")),
            Entity("sys_temp", "System temperature", SENSOR, unit="°C",
                   read=lambda: _temp("sysTemp", "systemTemperature", "temperature")),
        ]


register(QNAP())

"""Synology DSM NAS via WebAPI (http transport, username + password).

DSM logs in at /webapi/auth.cgi to get a session id (sid), then queries
SYNO.Core.System for hardware/firmware info. That login is device-specific, so
it lives here over the generic `http` transport. DSM's web UI is typically on
port 5000 (http) / 5001 (https) — set the port accordingly in the wizard.
"""
from .base import Driver, Entity, SENSOR
from .registry import register


def _api(conn, path, params):
    try:
        r = conn.get(path, params=params)
        return r.json() if r.status == 200 else None
    except Exception:
        return None


def _login(conn):
    j = _api(conn, "/webapi/auth.cgi", {
        "api": "SYNO.API.Auth", "version": "3", "method": "login",
        "account": conn.username or "", "passwd": conn.password or "",
        "session": "Core", "format": "sid"})
    if isinstance(j, dict) and j.get("success"):
        return (j.get("data") or {}).get("sid")
    return None


def _system(conn, sid):
    j = _api(conn, "/webapi/entry.cgi", {
        "api": "SYNO.Core.System", "version": "1", "method": "info",
        "_sid": sid})
    if isinstance(j, dict) and j.get("success"):
        return j.get("data") or {}
    return {}


class SynologyDSM(Driver):
    id = "synology.dsm"
    display_name = "Synology DSM"
    transports = ["http"]

    def probe(self, conn) -> float:
        sid = _login(conn)
        if not sid:
            return 0.0
        info = _system(conn, sid)
        return 0.9 if info.get("model") or info.get("firmware_ver") else 0.4

    def entities(self, conn):
        cache = {}

        def info():
            if "i" not in cache:
                sid = _login(conn)
                cache["i"] = _system(conn, sid) if sid else {}
            return cache["i"]

        def temp():
            t = info().get("sys_temp")
            try:
                return int(t)
            except Exception:
                return None

        return [
            Entity("model", "Model", SENSOR, read=lambda: info().get("model")),
            Entity("dsm_version", "DSM version", SENSOR,
                   read=lambda: info().get("firmware_ver")),
            Entity("uptime", "Uptime", SENSOR, unit="s",
                   read=lambda: info().get("up_time")),
            Entity("temperature", "System temperature", SENSOR, unit="°C",
                   read=temp),
        ]


register(SynologyDSM())

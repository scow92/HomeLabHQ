"""Generic Linux/Unix host over SSH.

The broad fallback for anything you can SSH into — servers, NAS boxes, most
Linux-based routers/APs. Confidence stays moderate on purpose so a more
specific SSH driver (OpenWRT, EdgeOS, …) added later will outrank it.
"""
import re

from .base import Driver, Entity, SENSOR, BUTTON
from .registry import register


def _first_line(s):
    return (s or "").strip().splitlines()[0].strip() if (s or "").strip() else ""


class GenericLinuxHost(Driver):
    id = "generic.linux-ssh"
    display_name = "Generic Linux/Unix host (SSH)"
    transports = ["ssh"]

    def probe(self, conn) -> float:
        rc, out, err = conn.run("uname -sr")
        kernel = _first_line(out).lower()
        if rc == 0 and "linux" in kernel:
            return 0.65
        if rc == 0 and any(k in kernel for k in ("bsd", "darwin", "sunos")):
            return 0.55
        if rc == 0 and out.strip():
            return 0.4          # some shell answered, unknown OS
        return 0.3              # SSH connected but uname failed (limited shell)

    def entities(self, conn):
        def hostname():
            return _first_line(conn.run("uname -n")[1])

        def kernel():
            return _first_line(conn.run("uname -sr")[1])

        def uptime_seconds():
            # /proc/uptime is the most portable machine-readable source.
            out = conn.run("cat /proc/uptime")[1]
            m = re.match(r"\s*([\d.]+)", out or "")
            return round(float(m.group(1))) if m else None

        def load_average():
            out = conn.run("cat /proc/loadavg")[1]
            return _first_line(out).split()[0] if out.strip() else None

        def memory_used_pct():
            # `free` output: parse the Mem: line -> used/total.
            out = conn.run("free -b")[1]
            for line in out.splitlines():
                if line.lower().startswith("mem:"):
                    parts = line.split()
                    try:
                        total, used = float(parts[1]), float(parts[2])
                        return round(used / total * 100, 1) if total else None
                    except Exception:
                        return None
            return None

        def reachable():
            return conn.run("true")[0] == 0

        return [
            Entity("reachable", "Reachable", SENSOR, read=reachable),
            Entity("hostname", "Hostname", SENSOR, read=hostname),
            Entity("kernel", "Kernel", SENSOR, read=kernel),
            Entity("uptime", "Uptime", SENSOR, unit="s", read=uptime_seconds),
            Entity("loadavg", "Load average (1m)", SENSOR, read=load_average),
            Entity("mem_used", "Memory used", SENSOR, unit="%",
                   read=memory_used_pct),
            # A control the user must explicitly opt into during setup.
            Entity("reboot", "Reboot", BUTTON,
                   write=lambda _=None: conn.run("reboot")),
        ]


register(GenericLinuxHost())

"""Generic SNMP device.

Works against anything that answers SNMP — switches, routers, printers, PDUs —
using only the standard MIB-II OIDs every agent implements. Confidence is
moderate; vendor-specific SNMP drivers (added later) can outrank it by matching
sysObjectID.
"""
from .base import Driver, Entity, SENSOR
from .registry import register
from transports import (
    OID_SYS_DESCR, OID_SYS_NAME, OID_SYS_UPTIME, OID_IF_NUMBER,
    OID_IF_OPER_STATUS,
)

_IF_STATUS = {1: "up", 2: "down", 3: "testing", 4: "unknown",
              5: "dormant", 6: "notPresent", 7: "lowerLayerDown"}


class GenericSNMPDevice(Driver):
    id = "generic.snmp"
    display_name = "Generic SNMP device"
    transports = ["snmp"]

    def probe(self, conn) -> float:
        descr = conn.get(OID_SYS_DESCR)
        if descr:
            return 0.5
        return 0.0

    def entities(self, conn):
        def sys_name():
            return conn.get(OID_SYS_NAME)

        def sys_descr():
            return conn.get(OID_SYS_DESCR)

        def uptime_seconds():
            # sysUpTime is in hundredths of a second (TimeTicks).
            ticks = conn.get(OID_SYS_UPTIME)
            try:
                return round(int(ticks) / 100)
            except Exception:
                return None

        def interface_count():
            n = conn.get(OID_IF_NUMBER)
            try:
                return int(n)
            except Exception:
                return None

        def interfaces_up():
            up = 0
            total = 0
            for _oid, val in conn.walk(OID_IF_OPER_STATUS):
                total += 1
                try:
                    if int(val) == 1:
                        up += 1
                except Exception:
                    pass
            return f"{up}/{total}" if total else None

        return [
            Entity("sys_name", "System name", SENSOR, read=sys_name),
            Entity("sys_descr", "Description", SENSOR, read=sys_descr),
            Entity("uptime", "Uptime", SENSOR, unit="s", read=uptime_seconds),
            Entity("if_count", "Interfaces", SENSOR, read=interface_count),
            Entity("if_up", "Interfaces up", SENSOR, read=interfaces_up),
        ]


register(GenericSNMPDevice())

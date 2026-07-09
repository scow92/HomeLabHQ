"""Managed switch / router over SNMP (IF-MIB detail).

Extends the generic SNMP device with per-interface throughput and error totals
from the standard IF-MIB high-capacity counters — the numbers you actually want
off a switch or router. Ranks just above the generic SNMP driver for any device
that exposes interfaces, but stays standard-MIB only (no vendor OIDs).
"""
from .base import Driver, Entity, SENSOR
from .registry import register
from transports import OID_IF_NUMBER

# IF-MIB counters (walked across all interfaces, then summed).
OID_IF_HC_IN_OCTETS = "1.3.6.1.2.1.31.1.1.1.6"
OID_IF_HC_OUT_OCTETS = "1.3.6.1.2.1.31.1.1.1.10"
OID_IF_IN_ERRORS = "1.3.6.1.2.1.2.2.1.14"
OID_IF_OUT_ERRORS = "1.3.6.1.2.1.2.2.1.20"


def _sum(conn, oid):
    total = 0
    seen = False
    for _o, v in conn.walk(oid):
        try:
            total += int(v)
            seen = True
        except Exception:
            pass
    return total if seen else None


class SNMPSwitch(Driver):
    id = "snmp.switch"
    display_name = "Managed switch/router (SNMP)"
    transports = ["snmp"]

    def probe(self, conn) -> float:
        n = conn.get(OID_IF_NUMBER)
        try:
            # Interfaces present -> looks like network gear; edge out generic.
            return 0.55 if int(n) > 0 else 0.0
        except Exception:
            return 0.0

    def entities(self, conn):
        def if_count():
            try:
                return int(conn.get(OID_IF_NUMBER))
            except Exception:
                return None

        return [
            Entity("if_count", "Interfaces", SENSOR, read=if_count),
            Entity("in_octets", "Total in", SENSOR, unit="bytes",
                   read=lambda: _sum(conn, OID_IF_HC_IN_OCTETS)),
            Entity("out_octets", "Total out", SENSOR, unit="bytes",
                   read=lambda: _sum(conn, OID_IF_HC_OUT_OCTETS)),
            Entity("in_errors", "In errors", SENSOR,
                   read=lambda: _sum(conn, OID_IF_IN_ERRORS)),
            Entity("out_errors", "Out errors", SENSOR,
                   read=lambda: _sum(conn, OID_IF_OUT_ERRORS)),
        ]


register(SNMPSwitch())

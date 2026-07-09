"""pysnmp (7.x) glue, isolated so the async API is handled in one place.

pysnmp 7 is asyncio-only: get_cmd is a coroutine, walk_cmd an async generator,
and UdpTransportTarget.create an async classmethod. We wrap each operation in a
fresh event loop so the rest of the app can stay synchronous — SNMP polls are
infrequent and short, so a per-call loop is fine.
"""
import asyncio

from pysnmp.hlapi.asyncio import (
    SnmpEngine, CommunityData, ContextData, ObjectType, ObjectIdentity,
    UdpTransportTarget, get_cmd, walk_cmd,
)


def _mp_model(version):
    # SNMP v1 -> mpModel 0, v2c -> mpModel 1. v3 (USM) is a later addition.
    return 0 if str(version) in ("1", "v1") else 1


def _run(coro):
    return asyncio.run(coro)


async def _get(conn, oid):
    engine = SnmpEngine()
    target = await UdpTransportTarget.create(
        (conn.host, conn.port), timeout=conn.timeout, retries=conn.retries)
    err_ind, err_stat, err_idx, var_binds = await get_cmd(
        engine, CommunityData(conn.community, mpModel=_mp_model(conn.version)),
        target, ContextData(), ObjectType(ObjectIdentity(oid)))
    if err_ind or err_stat:
        return None
    for name, val in var_binds:
        return _coerce(val)
    return None


async def _walk(conn, base_oid):
    engine = SnmpEngine()
    target = await UdpTransportTarget.create(
        (conn.host, conn.port), timeout=conn.timeout, retries=conn.retries)
    out = []
    async for err_ind, err_stat, err_idx, var_binds in walk_cmd(
            engine, CommunityData(conn.community, mpModel=_mp_model(conn.version)),
            target, ContextData(), ObjectType(ObjectIdentity(base_oid)),
            lexicographicMode=False):
        if err_ind or err_stat:
            break
        for name, val in var_binds:
            out.append((str(name), _coerce(val)))
    return out


def _coerce(val):
    """Turn a pysnmp value into a plain Python str/int, or None for no-such."""
    try:
        # SNMP 'noSuchInstance'/'noSuchObject'/'endOfMibView' stringify to those.
        s = val.prettyPrint()
    except Exception:
        s = str(val)
    if s in ("", "No Such Object currently exists at this OID",
             "No Such Instance currently exists at this OID"):
        return None
    # Prefer int when the value is integral.
    try:
        return int(val)
    except Exception:
        return s


class _Snmp:
    def get(self, conn, oid):
        try:
            return _run(_get(conn, oid))
        except Exception:
            return None

    def walk(self, conn, oid):
        try:
            return _run(_walk(conn, oid))
        except Exception:
            return []


snmp = _Snmp()

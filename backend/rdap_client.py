"""Fixed-destination, cached RDAP address ownership lookups."""
from __future__ import annotations

import ipaddress
import json
import time
from dataclasses import dataclass

import requests


RDAP_ORIGIN = "https://rdap.org"
MAX_RESPONSE_BYTES = 256_000
POSITIVE_TTL = 24 * 60 * 60
NEGATIVE_TTL = 60 * 60


@dataclass(frozen=True)
class Ownership:
    ip: str
    asn: str | None
    asn_name: str
    organisation: str
    cidr: str
    source: str
    looked_up_at: int
    status: str

    @property
    def owner(self) -> str:
        return " ".join(x for x in (self.organisation, self.asn_name) if x).strip()


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [x.strip() for x in value if isinstance(x, str) and x.strip()]
    return []


def parse_ownership(ip: str, payload: object, now: int | None = None) -> Ownership:
    if not isinstance(payload, dict):
        raise ValueError("RDAP response is not an object")
    timestamp = int(time.time()) if now is None else now
    org, asn_name = "", ""
    for entity in payload.get("entities") or []:
        if not isinstance(entity, dict):
            continue
        roles = {str(x).casefold() for x in entity.get("roles") or []}
        cards = entity.get("vcardArray")
        fields = cards[1] if isinstance(cards, list) and len(cards) > 1 and isinstance(cards[1], list) else []
        names = [x[3] for x in fields if isinstance(x, list) and len(x) > 3 and x[0] in ("fn", "org") and isinstance(x[3], str)]
        if names and ("registrant" in roles or "technical" not in roles):
            org = names[0].strip()
            break
    start, end = payload.get("startAddress"), payload.get("endAddress")
    cidr = ""
    try:
        if start and end:
            cidr = str(next(ipaddress.summarize_address_range(ipaddress.ip_address(start), ipaddress.ip_address(end))))
    except (ValueError, StopIteration):
        pass
    asn_values = _strings(payload.get("arin_originas0_originautnums")) + _strings(payload.get("originAutnum"))
    asn = asn_values[0].removeprefix("AS") if asn_values else None
    remarks = payload.get("remarks") or []
    for remark in remarks:
        if isinstance(remark, dict):
            title = str(remark.get("title") or "").casefold()
            values = _strings(remark.get("description"))
            if "asn" in title and values:
                asn_name = values[0]
    return Ownership(ip, asn, asn_name, org, cidr, "rdap.org", timestamp,
                     "known" if org or asn else "unknown")


class RDAPClient:
    def __init__(self, *, session: requests.Session | None = None, connect_timeout=3, read_timeout=8):
        self.session = session or requests.Session()
        self.connect_timeout, self.read_timeout = connect_timeout, read_timeout
        self._cache: dict[str, tuple[int, Ownership]] = {}

    def lookup(self, endpoint_ip: str) -> Ownership:
        ip = str(ipaddress.ip_address(endpoint_ip))
        now = int(time.time())
        cached = self._cache.get(ip)
        if cached and cached[0] > now:
            return cached[1]
        unknown = Ownership(ip, None, "", "", "", "rdap.org", now, "unknown")
        try:
            response = self.session.get(f"{RDAP_ORIGIN}/ip/{ip}",
                                        headers={"Accept": "application/rdap+json"},
                                        timeout=(self.connect_timeout, self.read_timeout),
                                        allow_redirects=False, stream=True)
            if response.status_code != 200:
                self._cache[ip] = (now + NEGATIVE_TTL, unknown)
                return unknown
            chunks, size = [], 0
            for chunk in response.iter_content(64 * 1024):
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise ValueError("too large")
                chunks.append(chunk)
            value = parse_ownership(ip, json.loads(b"".join(chunks)), now)
            self._cache[ip] = (now + (POSITIVE_TTL if value.status == "known" else NEGATIVE_TTL), value)
            return value
        except (requests.RequestException, ValueError):
            self._cache[ip] = (now + NEGATIVE_TTL, unknown)
            return unknown

"""Fixed-destination, cached RDAP address ownership lookups."""
from __future__ import annotations

import ipaddress
import json
import time
from dataclasses import dataclass
from urllib.parse import urlparse

import requests


RDAP_ORIGIN = "https://rdap.org"
RDAP_REGISTRY_HOSTS = frozenset({
    "rdap.afrinic.net",
    "rdap.apnic.net",
    "rdap.arin.net",
    "rdap.db.ripe.net",
    "rdap.lacnic.net",
})
MAX_RDAP_REDIRECTS = 3
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


def _entity_identity(entity: dict) -> tuple[str, str]:
    cards = entity.get("vcardArray")
    fields = cards[1] if isinstance(cards, list) and len(cards) > 1 and isinstance(cards[1], list) else []
    names: list[tuple[str, str]] = []
    kind = ""
    for field in fields:
        if not isinstance(field, list) or len(field) < 4 or not isinstance(field[3], str):
            continue
        field_name, value = str(field[0]).casefold(), field[3].strip()
        if field_name == "kind":
            kind = value.casefold()
        elif field_name in ("org", "fn") and value:
            names.append((field_name, value))
    organisation = next((value for field_name, value in names if field_name == "org"), "")
    return organisation or next((value for _, value in names), ""), kind


def _safe_registry_redirect(location: str) -> tuple[str, str] | None:
    try:
        target = urlparse(location)
        if (target.scheme != "https" or target.username or target.password
                or target.port not in (None, 443) or target.hostname not in RDAP_REGISTRY_HOSTS):
            return None
    except ValueError:
        return None
    return location, target.hostname


def parse_ownership(ip: str, payload: object, now: int | None = None,
                    source: str = "rdap.org") -> Ownership:
    if not isinstance(payload, dict):
        raise ValueError("RDAP response is not an object")
    timestamp = int(time.time()) if now is None else now
    org, asn_name = "", ""
    organisations: list[tuple[int, str]] = []
    for entity in payload.get("entities") or []:
        if not isinstance(entity, dict):
            continue
        roles = {str(x).casefold() for x in entity.get("roles") or []}
        name, kind = _entity_identity(entity)
        if not name:
            continue
        score = (100 if kind == "org" else 0) + (40 if "registrant" in roles else 0)
        if kind == "individual":
            score -= 80
        if roles.intersection({"abuse", "technical", "administrative"}):
            score -= 30
        if name.casefold() == str(entity.get("handle") or "").casefold():
            score -= 20
        organisations.append((score, name))
    if organisations:
        org = max(organisations, key=lambda candidate: candidate[0])[1]
    elif isinstance(payload.get("name"), str):
        org = payload["name"].strip()
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
    return Ownership(ip, asn, asn_name, org, cidr, source, timestamp,
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
        response = None
        try:
            response = self.session.get(f"{RDAP_ORIGIN}/ip/{ip}",
                                        headers={"Accept": "application/rdap+json"},
                                        timeout=(self.connect_timeout, self.read_timeout),
                                        allow_redirects=False, stream=True)
            source = "rdap.org"
            visited = {f"{RDAP_ORIGIN}/ip/{ip}"}
            redirects = 0
            while response.status_code in (301, 302, 303, 307, 308):
                approved = _safe_registry_redirect(response.headers.get("Location", ""))
                response.close()
                response = None
                if not approved or redirects >= MAX_RDAP_REDIRECTS or approved[0] in visited:
                    self._cache[ip] = (now + NEGATIVE_TTL, unknown)
                    return unknown
                target, source = approved
                visited.add(target)
                redirects += 1
                response = self.session.get(target,
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
            value = parse_ownership(ip, json.loads(b"".join(chunks)), now, source)
            self._cache[ip] = (now + (POSITIVE_TTL if value.status == "known" else NEGATIVE_TTL), value)
            return value
        except (requests.RequestException, ValueError):
            self._cache[ip] = (now + NEGATIVE_TTL, unknown)
            return unknown
        finally:
            if response is not None:
                response.close()

"""Bounded client for NordVPN's public server recommendation API.

This intentionally has no user supplied URL or credentials.  It is an
application integration, not a general-purpose HTTP proxy.
"""
from __future__ import annotations

import base64
import ipaddress
import json
import time
from dataclasses import dataclass
from typing import Any

import requests


API_ORIGIN = "https://api.nordvpn.com"
COUNTRIES_PATH = "/v1/servers/countries"
RECOMMENDATIONS_PATH = "/v1/servers/recommendations"
MAX_RESPONSE_BYTES = 512_000
DEFAULT_LIMIT = 20
MAX_LIMIT = 50
USER_AGENT = "HomeLabHQ-NordVPN-Endpoint-Manager/0.1"


class NordVPNError(RuntimeError):
    """Safe failure suitable for an operator-facing discovery status."""


@dataclass(frozen=True)
class NordVPNCandidate:
    server_id: int | None
    hostname: str
    endpoint_ip: str
    endpoint_port: int
    country: str
    city: str
    load: float | None
    public_key: str
    discovered_at: int


def _valid_key(value: object) -> str | None:
    if not isinstance(value, str) or len(value) != 44:
        return None
    try:
        return value if len(base64.b64decode(value, validate=True)) == 32 else None
    except (ValueError, TypeError):
        return None


def _metadata_key(technologies: object) -> str | None:
    if not isinstance(technologies, list):
        return None
    for technology in technologies:
        if not isinstance(technology, dict) or technology.get("identifier") != "wireguard_udp":
            continue
        metadata = technology.get("metadata")
        if not isinstance(metadata, list):
            continue
        # NordVPN currently calls this public_key, while older replies carried
        # only a value.  Never accept a value that is not a WireGuard key.
        for item in metadata:
            if isinstance(item, dict) and str(item.get("name", "")).lower() in {
                "public_key", "public key", "pubkey"
            }:
                key = _valid_key(item.get("value"))
                if key:
                    return key
        for item in metadata:
            if isinstance(item, dict):
                key = _valid_key(item.get("value"))
                if key:
                    return key
    return None


def parse_candidates(payload: object, discovered_at: int | None = None) -> list[NordVPNCandidate]:
    """Parse untrusted recommendations, keeping only usable unique peers."""
    if not isinstance(payload, list):
        raise NordVPNError("NordVPN returned an unexpected response")
    now = int(time.time()) if discovered_at is None else discovered_at
    candidates: list[NordVPNCandidate] = []
    ips, keys = set(), set()
    for row in payload:
        if not isinstance(row, dict):
            continue
        hostname = row.get("hostname")
        station = row.get("station")
        key = _metadata_key(row.get("technologies"))
        if not isinstance(hostname, str) or not hostname.strip() or not key:
            continue
        try:
            endpoint_ip = str(ipaddress.ip_address(str(station).strip()))
        except ValueError:
            continue
        # The public response commonly omits a port; WireGuard UDP's standard
        # port is the only default we apply, and only for valid server records.
        port = row.get("port") or row.get("station_port") or 51820
        try:
            port = int(port)
        except (TypeError, ValueError):
            continue
        if not 1 <= port <= 65535 or endpoint_ip in ips or key in keys:
            continue
        country = city = ""
        for location in row.get("locations") or []:
            if not isinstance(location, dict) or not isinstance(location.get("country"), dict):
                continue
            country_obj = location["country"]
            country = str(country_obj.get("name") or "").strip()
            city_obj = country_obj.get("city")
            city = str(city_obj.get("name") or "").strip() if isinstance(city_obj, dict) else ""
            if country or city:
                break
        try:
            load = float(row["load"]) if row.get("load") is not None else None
        except (TypeError, ValueError):
            load = None
        raw_server_id = row.get("id")
        server_id = raw_server_id if type(raw_server_id) is int and raw_server_id > 0 else None
        candidates.append(NordVPNCandidate(server_id, hostname.strip(), endpoint_ip, port,
                                           country, city, load, key, now))
        ips.add(endpoint_ip)
        keys.add(key)
    return candidates


class NordVPNClient:
    def __init__(self, *, session: requests.Session | None = None,
                 connect_timeout: float = 3, read_timeout: float = 8):
        self.session = session or requests.Session()
        self.connect_timeout, self.read_timeout = connect_timeout, read_timeout

    def _get_json(self, path: str, *, params: dict[str, Any] | None = None) -> object:
        try:
            response = self.session.get(API_ORIGIN + path, params=params,
                                        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                                        timeout=(self.connect_timeout, self.read_timeout),
                                        allow_redirects=False, stream=True)
            if response.status_code == 429:
                raise NordVPNError("NordVPN rate limited discovery; it will retry later")
            if response.status_code != 200:
                raise NordVPNError(f"NordVPN discovery failed (HTTP {response.status_code})")
            chunks, size = [], 0
            for chunk in response.iter_content(64 * 1024):
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise NordVPNError("NordVPN response exceeded the safety limit")
                chunks.append(chunk)
            data = b"".join(chunks)
            if len(data) > MAX_RESPONSE_BYTES:
                raise NordVPNError("NordVPN response exceeded the safety limit")
            return json.loads(data)
        except NordVPNError:
            raise
        except (requests.RequestException, ValueError) as error:
            raise NordVPNError("NordVPN discovery is temporarily unavailable") from error

    def _country_id(self, country: str) -> int:
        countries = self._get_json(COUNTRIES_PATH)
        wanted = country.casefold().strip()
        if not isinstance(countries, list):
            raise NordVPNError("NordVPN country catalogue was malformed")
        for item in countries:
            if not isinstance(item, dict) or str(item.get("name", "")).casefold() != wanted:
                continue
            try:
                return int(item["id"])
            except (KeyError, TypeError, ValueError):
                break
        raise NordVPNError(f"NordVPN does not recognise country {country!r}")

    def discover(self, country: str, limit: int = DEFAULT_LIMIT) -> list[NordVPNCandidate]:
        limit = max(1, min(MAX_LIMIT, int(limit)))
        payload = self._get_json(RECOMMENDATIONS_PATH, params={
            "filters[servers_technologies][identifier]": "wireguard_udp",
            "filters[country_id]": self._country_id(country), "limit": limit,
        })
        return parse_candidates(payload)

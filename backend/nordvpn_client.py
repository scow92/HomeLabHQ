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
SERVERS_PATH = "/v1/servers"
MAX_RESPONSE_BYTES = 512_000
DEFAULT_LIMIT = 20
MAX_LIMIT = 50
CATALOG_PAGE_LIMIT = 100
MAX_CATALOG_PAGES = 50
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


def parse_locations(payload: object) -> list[dict[str, Any]]:
    """Parse the provider catalogue into bounded country and city choices."""
    if not isinstance(payload, list):
        raise NordVPNError("NordVPN country catalogue was malformed")
    countries: list[dict[str, Any]] = []
    seen_countries: set[str] = set()
    for item in payload[:300]:
        if not isinstance(item, dict):
            continue
        raw_name, raw_id = item.get("name"), item.get("id")
        if not isinstance(raw_name, str) or type(raw_id) is not int:
            continue
        name, country_id = raw_name.strip()[:80], raw_id
        folded_name = name.casefold()
        if not name or country_id <= 0 or folded_name in seen_countries:
            continue
        cities: list[dict[str, Any]] = []
        seen_cities: set[str] = set()
        raw_cities = item.get("cities")
        for city in raw_cities[:500] if isinstance(raw_cities, list) else []:
            if not isinstance(city, dict):
                continue
            raw_city_name, raw_city_id = city.get("name"), city.get("id")
            if not isinstance(raw_city_name, str) or type(raw_city_id) is not int:
                continue
            city_name, city_id = raw_city_name.strip()[:80], raw_city_id
            folded_city = city_name.casefold()
            if not city_name or city_id <= 0 or folded_city in seen_cities:
                continue
            seen_cities.add(folded_city)
            cities.append({"id": city_id, "name": city_name})
        cities.sort(key=lambda value: value["name"].casefold())
        seen_countries.add(folded_name)
        countries.append({"id": country_id, "name": name, "cities": cities})
    countries.sort(key=lambda value: value["name"].casefold())
    return countries


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

    def locations(self) -> list[dict[str, Any]]:
        return parse_locations(self._get_json(COUNTRIES_PATH))

    def _location_ids(self, country: str, city: str = "") -> tuple[int, int | None]:
        countries = self.locations()
        wanted = country.casefold().strip()
        for item in countries:
            if item["name"].casefold() != wanted:
                continue
            wanted_city = city.casefold().strip()
            if not wanted_city:
                return item["id"], None
            selected_city = next(
                (value for value in item["cities"] if value["name"].casefold() == wanted_city), None)
            if selected_city:
                return item["id"], selected_city["id"]
            raise NordVPNError(f"NordVPN does not recognise city {city!r} in {country!r}")
        raise NordVPNError(f"NordVPN does not recognise country {country!r}")

    def _country_id(self, country: str) -> int:
        return self._location_ids(country)[0]

    def discover(self, country: str, limit: int = DEFAULT_LIMIT,
                 city: str = "") -> list[NordVPNCandidate]:
        limit = max(1, min(MAX_LIMIT, int(limit)))
        country_id, city_id = self._location_ids(country, city)
        filters = {
            "filters[servers_technologies][identifier]": "wireguard_udp",
            "filters[country_id]": country_id, "limit": limit,
        }
        if city_id is not None:
            filters["filters[city_id]"] = city_id
        payload = self._get_json(RECOMMENDATIONS_PATH, params=filters)
        return parse_candidates(payload)

    def connected_server(self, country: str, *, server_id: int | None = None,
                         hostname: str = "", endpoint: str = "",
                         cache: dict[str, dict[str, Any]] | None = None
                         ) -> NordVPNCandidate:
        """Find one connected server in the bounded country catalogue.

        NordVPN's current catalogue accepts country and offset filters but
        ignores exact server filters. Pages may be shared by callers in one
        background poll pass so profiles in the same country do not refetch
        catalogue data.
        """
        wanted_endpoint = str(endpoint or "").strip().casefold()
        wanted_hostname = str(hostname or "").strip().casefold()

        def matches(candidate: NordVPNCandidate) -> bool:
            if wanted_endpoint:
                return wanted_endpoint in {
                    candidate.endpoint_ip.casefold(), candidate.hostname.casefold()}
            if type(server_id) is int and server_id > 0:
                return candidate.server_id == server_id
            return bool(wanted_hostname and candidate.hostname.casefold() == wanted_hostname)

        shared = cache if cache is not None else {}
        cache_key = country.casefold().strip()
        state = shared.get(cache_key)
        if state is None:
            state = {
                "countryId": self._country_id(country),
                "candidates": [],
                "offset": 0,
                "complete": False,
            }
            shared[cache_key] = state

        found = next((item for item in state["candidates"] if matches(item)), None)
        if found:
            return found
        pages = int(state["offset"]) // CATALOG_PAGE_LIMIT
        while not state["complete"] and pages < MAX_CATALOG_PAGES:
            payload = self._get_json(SERVERS_PATH, params={
                "filters[country_id]": state["countryId"],
                "limit": CATALOG_PAGE_LIMIT,
                "offset": state["offset"],
            })
            if not isinstance(payload, list):
                raise NordVPNError("NordVPN server catalogue was malformed")
            page = parse_candidates(payload)
            state["candidates"].extend(page)
            state["offset"] += len(payload)
            state["complete"] = len(payload) < CATALOG_PAGE_LIMIT
            pages += 1
            found = next((item for item in page if matches(item)), None)
            if found:
                return found
        if not state["complete"]:
            raise NordVPNError("NordVPN server catalogue exceeded the safety limit")
        raise NordVPNError("Connected NordVPN server was not found in the selected country")

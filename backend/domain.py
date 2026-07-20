"""Typed values at the driver, polling, roster, and API boundaries.

The store remains JSON-backed.  These dataclasses deliberately convert to and
from dictionaries only at that boundary, so application code does not need to
depend on undocumented dictionary shapes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Mapping, NotRequired, TypedDict


Scalar = str | int | float | bool | None
JsonValue = Scalar | list["JsonValue"] | dict[str, "JsonValue"]
ENTITY_KINDS = frozenset({"sensor", "switch", "button", "number"})


class EntityDescriptionShape(TypedDict):
    key: str
    name: str
    kind: str
    unit: NotRequired[str | None]
    controllable: bool


class DeviceStateShape(TypedDict):
    online: bool
    confirmedOnline: bool
    miss: int
    values: dict[str, JsonValue]
    errors: dict[str, str]
    ts: int
    since: int


class ClientRosterShape(TypedDict, total=False):
    mac: str
    ip: str
    hostname: str
    vendor: str
    kind: str
    signal: int | float | None
    seen: list[dict[str, Any]]
    via: str
    online: bool
    firstSeen: int | None
    lastSeen: int | None
    name: str
    notes: str
    notify: bool
    new: bool
    nac: str
    aliases: list[dict[str, Any]]


def safe_error(error: object) -> str:
    """Keep diagnostics useful without reflecting common credential formats."""
    text = str(error)
    text = re.sub(r"(?i)(password|passwd|token|secret|api[_-]?key)\s*([=:])\s*[^\s,;]+",
                  r"\1\2[redacted]", text)
    text = re.sub(r"(https?://[^:/\s]+:)[^@/\s]+@", r"\1[redacted]@", text)
    return text[:500]


@dataclass(frozen=True)
class EntityDescription:
    key: str
    name: str
    kind: str = "sensor"
    unit: str | None = None
    controllable: bool = False

    def __post_init__(self):
        if not self.key.strip() or not self.name.strip():
            raise ValueError("entity key and name must be nonempty")
        if self.kind not in ENTITY_KINDS:
            raise ValueError(f"unsupported entity kind: {self.kind}")

    def to_dict(self) -> EntityDescriptionShape:
        return {"key": self.key, "name": self.name, "kind": self.kind,
                "unit": self.unit, "controllable": self.controllable}


@dataclass(frozen=True)
class ClientObservation:
    """One normalized client sighting emitted by a device driver."""
    mac: str
    source_id: str
    source_name: str
    ip: str = ""
    hostname: str = ""
    hostname_authoritative: bool = False
    vendor: str = ""
    kind: str = "wired"
    signal: int | float | None = None
    where: str = ""

    def __post_init__(self):
        object.__setattr__(self, "mac", self.mac.upper())
        if not self.mac or not self.source_id or not self.source_name:
            raise ValueError("client observation requires a MAC and source")


@dataclass(frozen=True)
class ClientRosterRecord:
    ip: str = ""
    hostname: str = ""
    vendor: str = ""
    kind: str = "wired"
    signal: int | float | None = None
    seen: list[dict[str, Any]] = field(default_factory=list)
    via: str = ""
    online: bool = False
    first_seen: int | None = None
    last_seen: int | None = None
    name: str = ""
    notes: str = ""
    notify: bool = False
    is_new: bool = False
    nac_approved: bool | None = None
    aliases: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "ClientRosterRecord":
        signal = record.get("signal")
        # RSSI is Wi-Fi-only in the driver contract. Older partial NAC scans
        # could persist ``kind=wired`` over an AP observation, so infer the
        # correct API type from the retained dBm value while those records heal.
        kind = "wifi" if signal is not None else str(record.get("kind") or "wired")
        return cls(ip=str(record.get("ip") or ""), hostname=str(record.get("hostname") or ""),
                   vendor=str(record.get("vendor") or ""), kind=kind,
                   signal=signal, seen=list(record.get("seen") or []),
                   via=str(record.get("via") or ""), online=bool(record.get("online")),
                   first_seen=record.get("firstSeen"), last_seen=record.get("lastSeen"),
                   name=str(record.get("name") or ""), notes=str(record.get("notes") or ""),
                   notify=bool(record.get("notify")), is_new=bool(record.get("new")),
                   nac_approved=record.get("nacApproved"), aliases=list(record.get("aliases") or []))

    def to_api(self, mac: str) -> ClientRosterShape:
        value: ClientRosterShape = {
            "mac": mac, "ip": self.ip, "hostname": self.hostname, "vendor": self.vendor,
            "kind": self.kind, "signal": self.signal, "seen": self.seen, "via": self.via,
            "online": self.online, "firstSeen": self.first_seen, "lastSeen": self.last_seen,
            "name": self.name, "notes": self.notes, "notify": self.notify, "new": self.is_new,
        }
        if self.nac_approved is not None:
            value["nac"] = "approved" if self.nac_approved else "blocked"
        if self.aliases:
            value["aliases"] = self.aliases
        return value


@dataclass(frozen=True)
class NacConfiguration:
    configured: bool = False
    enforced: bool = False
    device_id: str | None = None
    device_name: str | None = None
    alias: str | None = None
    mode: str | None = None
    managed_externally: bool = False

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "NacConfiguration":
        value = value or {}
        return cls(bool(value.get("configured")), bool(value.get("enforced")),
                   value.get("deviceId"), value.get("deviceName"), value.get("alias"),
                   value.get("mode"), bool(value.get("managedExternally")))

    def to_dict(self) -> dict[str, Any]:
        return {"configured": self.configured, "enforced": self.enforced,
                "deviceId": self.device_id, "deviceName": self.device_name,
                "alias": self.alias, "mode": self.mode,
                "managedExternally": self.managed_externally}


@dataclass(frozen=True)
class AlertRule:
    key: str
    op: str
    value: float
    label: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "AlertRule":
        key, op = str(value.get("key") or ""), value.get("op")
        if not key or op not in {"above", "below"}:
            raise ValueError("invalid alert rule")
        threshold = value.get("value")
        if threshold is None:
            raise ValueError("invalid alert rule value")
        return cls(key, op, float(threshold), str(value.get("label") or key))

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "op": self.op, "value": self.value, "label": self.label}


@dataclass(frozen=True)
class HistoryPoint:
    timestamp: int
    value: int | float

    def to_wire(self) -> list[int | float]:
        return [self.timestamp, self.value]


@dataclass(frozen=True)
class DevicePollResult:
    values: dict[str, JsonValue] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    interfaces: list[dict[str, Any]] = field(default_factory=list)
    elapsed: float | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "DevicePollResult":
        elapsed = value.get("_elapsed")
        return cls(dict(value.get("values") or {}),
                   {str(k): safe_error(v) for k, v in dict(value.get("errors") or {}).items()},
                   list(value.get("interfaces") or []),
                   float(elapsed) if elapsed is not None else None)

    def to_dict(self) -> dict[str, Any]:
        value = {"values": self.values, "errors": self.errors, "interfaces": self.interfaces}
        if self.elapsed is not None:
            value["_elapsed"] = self.elapsed
        return value


@dataclass(frozen=True)
class DeviceState:
    online: bool
    confirmed_online: bool
    misses: int
    values: dict[str, JsonValue]
    errors: dict[str, str]
    timestamp: int
    since: int

    def to_dict(self) -> DeviceStateShape:
        return {"online": self.online, "confirmedOnline": self.confirmed_online,
                "miss": self.misses, "values": self.values, "errors": self.errors,
                "ts": self.timestamp, "since": self.since}


@dataclass(frozen=True)
class DriverDetail:
    """Validate shared table structure while retaining vendor-specific keys."""
    value: dict[str, Any]

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "DriverDetail":
        data = dict(value or {})
        if not isinstance(data.get("info", {}), Mapping):
            raise ValueError("driver detail info must be an object")
        tables = data.get("tables", [])
        if not isinstance(tables, list):
            raise ValueError("driver detail tables must be a list")
        for table in tables:
            if (not isinstance(table, Mapping)
                    or not isinstance(table.get("title"), str)
                    or not table["title"].strip()):
                raise ValueError("driver detail table must have a title")
            if not isinstance(table.get("columns", []), list) or not isinstance(table.get("rows", []), list):
                raise ValueError("driver detail table columns and rows must be lists")
            for column in table.get("columns", []):
                if not isinstance(column, Mapping) or not str(column.get("key") or ""):
                    raise ValueError("driver detail column must have a key")
            if any(not isinstance(row, Mapping) for row in table.get("rows", [])):
                raise ValueError("driver detail rows must be objects")
        return cls(data)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.value)

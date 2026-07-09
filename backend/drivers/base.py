"""Driver + entity base classes (skeleton).

The curated-driver model: each driver declares which transports it speaks,
probes a connection to say how confident it is that a device is its kind, and
enumerates entities (sensors to display, controls to actuate). Milestone 1 only
lays down the contract; real drivers and the probing/detection pipeline arrive
in Milestone 2.
"""
from dataclasses import dataclass, field
from typing import Callable, List, Optional

# Entity kinds the UI knows how to render.
SENSOR = "sensor"   # read-only value (uptime, temp, link state)
SWITCH = "switch"   # boolean control (port up/down, radio on/off)
BUTTON = "button"   # momentary action (reboot)
NUMBER = "number"   # settable numeric (channel, tx-power)


@dataclass
class Entity:
    key: str
    name: str
    kind: str = SENSOR
    unit: Optional[str] = None
    read: Optional[Callable[[], object]] = None
    write: Optional[Callable[[object], None]] = None

    @property
    def controllable(self) -> bool:
        return self.kind in (SWITCH, BUTTON, NUMBER) and self.write is not None

    def describe(self) -> dict:
        return {"key": self.key, "name": self.name, "kind": self.kind,
                "unit": self.unit, "controllable": self.controllable}


class Driver:
    """Subclass and set `id`, `display_name`, `transports`."""
    id: str = ""
    display_name: str = ""
    transports: List[str] = field(default_factory=list)

    def probe(self, conn) -> float:
        """Return confidence 0..1 that `conn` is this driver's device kind."""
        raise NotImplementedError

    def entities(self, conn) -> List[Entity]:
        """Enumerate the entities this device exposes over `conn`."""
        raise NotImplementedError

    def detail(self, conn) -> dict:
        """Optional rich, structured read for the device detail view.

        Where entities() is a flat list of pollable scalar sensors, detail() is
        the per-device drill-down: an overview map plus zero or more tables
        (interfaces, ports, connected clients, radios...). Drivers opt in; the
        default is empty and the UI falls back to the latest sensor values.

        Shape:
            {
              "info": {"Model": "NWA50AX", "Firmware": "...", ...},  # ordered kv
              "tables": [
                {"title": "Connected clients",
                 "columns": [{"key": "mac", "label": "MAC"},
                             {"key": "rssi", "label": "Signal", "unit": "dBm"}],
                 "rows": [{"mac": "...", "rssi": -58}, ...]},
              ],
            }

        All values must be JSON-serializable. Raise nothing the caller can't
        handle — connection errors propagate, everything else should be caught
        by the driver and surfaced as a partial result.
        """
        return {}

    def interfaces(self, conn) -> list:
        """Optional per-interface counters for network gear.

        Return a list of dicts, one per interface/port:
            {"device": "igc0",      # stable id used as the history key
             "name": "WAN",         # human label (falls back to device)
             "status": "up"|"down"|bool,
             "rx": <int bytes>|None, "tx": <int bytes>|None}

        Used two ways: detail() renders it as a table, and the poller records
        each interface's rx/tx over time so the UI can chart per-interface
        upload/download history. Include unassigned/unused interfaces too — the
        user can hide the ones they don't care about. Default: none.
        """
        return []

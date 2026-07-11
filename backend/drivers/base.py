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

    # Set True by drivers that can pin a wireless client to a preferred AP (see
    # enforce_bindings). Lets the UI show the per-client lock control and the
    # poller run enforcement only for capable devices.
    supports_binding: bool = False

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

    def actions(self) -> list:
        """Named actions this driver can perform on a device (beyond entity
        writes). Each: {"name","label","argKey"?,"argLabel"?}. `argKey` names a
        single string argument the caller must supply (e.g. a client MAC to act
        on). Surfaced so the UI can offer buttons; default: none."""
        return []

    def run_action(self, conn, name: str, args: dict) -> dict:
        """Execute a named action from actions(). Return a small JSON-able dict
        describing the outcome. Raise ValueError for an unknown/invalid action.
        Default: no actions supported."""
        raise ValueError(f"unsupported action: {name}")

    def clients(self, conn) -> list:
        """Optional: network clients this device can see, for the aggregated
        network-wide Clients view. Return a list of dicts:

            {"mac": "AA:BB:..",      # required, uppercased
             "ip": "192.168.1.5"|"",  # if known
             "hostname": "nas"|"",    # if resolvable
             "kind": "wifi"|"wired",
             "signal": -58|None,      # dBm, wifi only
             "where": "5 GHz · SSID"|"Port 3 · VLAN 10"}  # human location

        APs report associated wireless stations; switches report learned MACs.
        Default: none (most devices aren't a client source)."""
        return []

    def enforce_bindings(self, conn, roam_off: set) -> dict:
        """Pin bound wireless clients to their preferred AP (opt-in; requires
        supports_binding). `roam_off` is the set of uppercased client MACs that
        are locked to a *different* AP than this one — the driver should kick any
        of them currently associated here so they re-associate to their AP. Runs
        every poll interval, so it must be a cheap no-op when nothing matches.
        Default: not supported."""
        return {}

    def binding_ready(self, conn) -> bool:
        """Confirm this device can actually enforce a binding right now (e.g. the
        SSH/credentials the roam action needs are usable). Called when a user
        opts into roam-binding so the UI only offers it when it will work. Raises
        with a human message when not ready. Default: not supported."""
        return False

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

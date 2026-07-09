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

"""Driver registry.

Real drivers register themselves here in Milestone 2. For now this exposes the
lookup surface the rest of the app will call so nothing else needs to change
when drivers arrive.
"""
from typing import Dict, List

from .base import Driver

_DRIVERS: Dict[str, Driver] = {}


def register(driver: Driver):
    _DRIVERS[driver.id] = driver


def all_drivers() -> List[Driver]:
    return list(_DRIVERS.values())


def get(driver_id: str):
    return _DRIVERS.get(driver_id)


def for_transport(transport: str) -> List[Driver]:
    return [d for d in _DRIVERS.values() if transport in d.transports]

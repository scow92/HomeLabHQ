"""Driver registry.

Real drivers register themselves here in Milestone 2. For now this exposes the
lookup surface the rest of the app will call so nothing else needs to change
when drivers arrive.
"""
import json
from typing import Dict, Iterable, List

from .base import Driver, Entity, validate_detail

_DRIVERS: Dict[str, Driver] = {}


def register(driver: Driver):
    validate_driver(driver)
    if driver.id in _DRIVERS:
        raise ValueError(f"duplicate driver id: {driver.id}")
    _DRIVERS[driver.id] = driver


def validate_driver(driver: Driver) -> None:
    """Offline contract checks shared by startup and the driver test suite."""
    if not isinstance(driver.id, str) or not driver.id.strip():
        raise ValueError("driver id must be a nonempty string")
    if not isinstance(driver.display_name, str) or not driver.display_name.strip():
        raise ValueError(f"driver {driver.id} has no display name")
    if not isinstance(driver.transports, list) or not driver.transports:
        raise ValueError(f"driver {driver.id} must declare transports")
    if len(set(driver.transports)) != len(driver.transports):
        raise ValueError(f"driver {driver.id} repeats a transport")
    if any(not isinstance(transport, str) or not transport.strip()
           for transport in driver.transports):
        raise ValueError(f"driver {driver.id} has an invalid transport declaration")
    for capability in ("supports_binding", "nac_supported"):
        value = getattr(driver, capability, False)
        if not isinstance(value, bool):
            raise ValueError(f"driver {driver.id} has an invalid {capability} capability")
    rule_states = getattr(driver, "firewall_rule_states", None)
    if rule_states is not None and not callable(rule_states):
        raise ValueError(f"driver {driver.id} has an invalid firewall capability")
    actions = driver.actions() or []
    if not isinstance(actions, list):
        raise ValueError(f"driver {driver.id} actions must be a list")
    names = []
    for action in actions:
        if not isinstance(action, dict) or not str(action.get("name") or ""):
            raise ValueError(f"driver {driver.id} has an invalid action")
        names.append(action["name"])
    if len(set(names)) != len(names):
        raise ValueError(f"driver {driver.id} repeats an action name")


def validate_driver_output(driver: Driver, entities: Iterable[Entity], detail: dict | None = None) -> None:
    """Validate connection-derived driver output in deterministic contract tests.

    Drivers may retain vendor-specific fields, but shared entity descriptions
    and detail tables must remain stable JSON API values.
    """
    keys = []
    for entity in entities:
        if not isinstance(entity, Entity):
            raise ValueError(f"driver {driver.id} returned a non-Entity")
        description = entity.describe()
        json.dumps(description)
        keys.append(entity.key)
    if len(set(keys)) != len(keys):
        raise ValueError(f"driver {driver.id} returned duplicate entity keys")
    if detail is not None:
        json.dumps(validate_detail(detail))


def all_drivers() -> List[Driver]:
    return list(_DRIVERS.values())


def get(driver_id: str):
    return _DRIVERS.get(driver_id)


def for_transport(transport: str) -> List[Driver]:
    return [d for d in _DRIVERS.values() if transport in d.transports]

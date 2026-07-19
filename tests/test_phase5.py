import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

from domain import (AlertRule, ClientRosterRecord, DevicePollResult, DeviceState,
                    DriverDetail, EntityDescription, HistoryPoint, NacConfiguration)
from drivers import registry
from drivers.base import Driver


def test_registered_drivers_have_unique_valid_static_contracts():
    drivers = registry.all_drivers()
    assert drivers
    assert len({driver.id for driver in drivers}) == len(drivers)
    for driver in drivers:
        registry.validate_driver(driver)
        # Entity catalogues are connection-lazy; constructing them must not
        # perform I/O, so this verifies every bundled driver's stable keys.
        registry.validate_driver_output(driver, driver.entities(None))


def test_driver_contract_rejects_invalid_declarations_and_detail_tables():
    class InvalidDriver(Driver):
        id = ""
        display_name = "Invalid"
        transports = ["bogus"]

    with pytest.raises(ValueError, match="id"):
        registry.validate_driver(InvalidDriver())
    with pytest.raises(ValueError, match="column"):
        DriverDetail.from_mapping({"tables": [{"title": "Ports", "columns": [{}], "rows": []}]})


def test_domain_values_serialize_to_existing_wire_shapes_without_secrets():
    assert EntityDescription("cpu", "CPU").to_dict()["key"] == "cpu"
    assert HistoryPoint(10, 2.5).to_wire() == [10, 2.5]
    assert AlertRule.from_mapping({"key": "cpu", "op": "above", "value": "90"}).to_dict() == {
        "key": "cpu", "op": "above", "value": 90.0, "label": "cpu"}
    result = DevicePollResult.from_mapping({"errors": {"cpu": "password=not-for-output"}})
    assert result.errors["cpu"] == "password=[redacted]"
    assert DeviceState(True, True, 0, {}, {}, 10, 10).to_dict()["confirmedOnline"] is True
    assert NacConfiguration().to_dict()["configured"] is False
    roster = ClientRosterRecord.from_record({"hostname": "nas", "nacApproved": True})
    assert roster.to_api("AA:BB")["nac"] == "approved"

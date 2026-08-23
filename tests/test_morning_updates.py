import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import morning_updates
import devices
import store
from errors import Conflict


@pytest.fixture(autouse=True)
def isolated_store(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(store, "DB_FILE", str(tmp_path / "homelabhq.json"))
    monkeypatch.setattr(store, "LOCK_FILE", str(tmp_path / "homelabhq.lock"))
    store._cache.update(doc=None, mtime=None)
    store.update(lambda document: document["users"].update({
        "admin": {"id": "admin", "username": "admin", "role": "admin"},
        "member": {"id": "member", "username": "member", "role": "member"},
    }))


def source(device_id, source_name, status, *, owner="member", **values):
    return morning_updates._new_source(
        device_id, values.pop("name", device_id), owner, values.pop("resourceType", "device"),
        source_name, status=status, **values)


def run_synchronously(monkeypatch, config=None):
    config = {**morning_updates.DEFAULT_CONFIG, **(config or {})}
    store.update(lambda document: document["meta"].update(morningUpdateCheck=config))
    run, token = morning_updates._acquire("manual", "admin")
    morning_updates._execute(run["id"], token)
    return store.load()["morningUpdateRuns"][run["id"]]


def test_both_phases_execute_and_merge_without_overwriting(monkeypatch):
    calls = []
    monkeypatch.setattr(morning_updates, "run_ansible_phase", lambda *_args, **_kwargs:
                        calls.append("ansible") or [source(
                            "router", "ansibleOs", "updates_available", updateCount=3)])
    monkeypatch.setattr(morning_updates, "run_device_phase", lambda *_args, **_kwargs:
                        calls.append("native") or [source(
                            "router", "deviceNative", "up_to_date", currentVersion="1.0")])
    monkeypatch.setattr(morning_updates, "_notify", lambda _run: [])

    run = run_synchronously(monkeypatch)

    assert calls == ["ansible", "native"]
    assert run["devicesRequiringUpdates"] == 1
    assert run["uniqueDevicesChecked"] == 1
    assert set(run["devices"][0]["sources"]) >= {"ansibleOs", "deviceNative"}


@pytest.mark.parametrize("disabled,expected", [
    ("runAnsibleChecks", ["native"]),
    ("runDeviceNativeChecks", ["ansible"]),
])
def test_either_phase_can_be_disabled(monkeypatch, disabled, expected):
    calls = []
    monkeypatch.setattr(morning_updates, "run_ansible_phase", lambda *_args, **_kwargs:
                        calls.append("ansible") or [])
    monkeypatch.setattr(morning_updates, "run_device_phase", lambda *_args, **_kwargs:
                        calls.append("native") or [])
    monkeypatch.setattr(morning_updates, "_notify", lambda _run: [])

    run = run_synchronously(monkeypatch, {disabled: False})

    assert calls == expected
    phase = "ansible" if disabled == "runAnsibleChecks" else "deviceNative"
    assert run["phaseStatus"][phase] == "disabled"


def test_aggregation_handles_ansible_only_native_only_and_unique_device_merging():
    result = morning_updates.aggregate([
        source("vm-1", "ansibleOs", "up_to_date", resourceType="compute"),
        source("router", "deviceNative", "updates_available", updateCount=1),
        source("shared", "ansibleOs", "updates_available", updateCount=2),
        source("shared", "ansibleDocker", "up_to_date"),
        source("shared", "deviceNative", "up_to_date"),
    ])

    assert result["uniqueDevicesChecked"] == 3
    assert result["devicesRequiringUpdates"] == 2
    shared = next(item for item in result["devices"] if item["deviceId"] == "shared")
    assert shared["requiresUpdates"] is True
    assert set(shared["sources"]) == {"ansibleOs", "ansibleDocker", "deviceNative"}


def test_conflicting_success_failure_unreachable_kernel_and_reboot_are_preserved():
    result = morning_updates.aggregate([
        source("pve2", "ansibleProxmox", "updates_available", updateCount=8,
               kernelUpdateAvailable=True, rebootRequired=True),
        source("pve2", "deviceNative", "up_to_date"),
        source("pve2", "ansibleDocker", "failed", error="playbook failed"),
        source("immich", "ansibleOs", "unreachable", error="host unreachable"),
    ])

    pve = next(item for item in result["devices"] if item["deviceId"] == "pve2")
    assert pve["requiresUpdates"] is True
    assert pve["checkIncomplete"] is True
    assert pve["rebootRequired"] is True
    assert pve["sources"]["ansibleProxmox"][0]["kernelUpdateAvailable"] is True
    assert result["devicesRequiringUpdates"] == 1
    assert result["devicesRequiringReboot"] == 1
    assert result["failedChecks"] == 2
    assert result["unreachableChecks"] == 1
    assert result["status"] == "partial"


def test_unsupported_is_not_reported_as_up_to_date():
    result = morning_updates.aggregate([
        source("unsupported", "deviceNative", "unsupported"),
    ])
    device = result["devices"][0]
    assert device["unsupported"] is True
    assert device["upToDate"] is False
    assert result["uniqueDevicesChecked"] == 0
    result["id"] = "unsupported-run"
    notification = morning_updates.format_notification(result)
    assert notification["title"] == "HomeLabHQ: Morning check completed"
    assert "do not support" in notification["body"]


def test_native_phase_uses_existing_driver_action_timeout_and_continues(monkeypatch):
    records = {
        "good": {"id": "good", "ownerId": "member", "name": "Good", "host": "a",
                 "driverId": "test", "includeInScheduledUpdateChecks": True},
        "bad": {"id": "bad", "ownerId": "member", "name": "Bad", "host": "b",
                "driverId": "test", "includeInScheduledUpdateChecks": True},
    }
    store.update(lambda document: document["devices"].update(records))

    class Driver:
        def actions(self):
            return [{"name": "check_updates", "label": "Check for updates"}]

    calls = []
    monkeypatch.setattr(morning_updates.devices, "_drv_for", lambda _device: Driver())

    def action(device_id, action, args, timeout):
        calls.append((device_id, action, args, timeout))
        if device_id == "bad":
            raise RuntimeError("provider unavailable")
        return {"ok": True, "updateAvailable": True, "current": "1", "latest": "2",
                "count": 1, "rebootRequired": False, "message": "Update available"}

    monkeypatch.setattr(morning_updates.devices, "run_action", action)
    results = morning_updates.run_device_phase(store.load(), 17)

    assert {item["status"] for item in results} == {"updates_available", "failed"}
    assert all(call[1:] == ("check_updates", {}, 17) for call in calls)
    persisted = store.load()["devices"]
    assert persisted["good"]["scheduledUpdateState"]["availableVersion"] == "2"
    assert persisted["bad"]["scheduledUpdateState"]["error"] == "provider unavailable"


def test_device_update_check_lock_is_persistent_and_released():
    with devices.update_check_lock("router"):
        with pytest.raises(Conflict, match="already active"):
            with devices.update_check_lock("router"):
                pass
    with devices.update_check_lock("router"):
        pass
    assert "deviceUpdateCheckLocks" not in store.load()["meta"]


def test_native_provider_unreachable_payload_is_failed_not_up_to_date(monkeypatch):
    device = {"id": "openwrt", "ownerId": "member", "name": "Router", "host": "a",
              "driverId": "openwrt"}
    store.update(lambda document: document["devices"].update(openwrt=device))

    class Driver:
        def actions(self):
            return [{"name": "check_updates"}]

    monkeypatch.setattr(morning_updates.devices, "_drv_for", lambda _device: Driver())
    monkeypatch.setattr(morning_updates.devices, "run_action", lambda *_args, **_kwargs: {
        "ok": True, "updateAvailable": False, "current": "24.10",
        "latestStable": None, "latestBuild": None,
        "message": "Couldn't reach the OpenWrt update servers",
    })

    result = morning_updates._native_check(device, 20)

    assert result["status"] == "failed"
    assert result["error"] == "Couldn't reach the OpenWrt update servers"


def test_timed_out_ansible_job_is_incomplete(monkeypatch):
    monkeypatch.setattr(morning_updates.compute_maintenance, "get_job", lambda _job_id: {
        "id": "job-1", "state": "running", "operation": "os_check"})
    ticks = iter((0.0, 2.0))
    monkeypatch.setattr(morning_updates.time, "monotonic", lambda: next(ticks))

    result = morning_updates._wait_for_jobs(["job-1"], timeout=1)

    assert result[0]["state"] == "incomplete"
    assert result[0]["summary"] == "Timed out waiting for maintenance result"


def test_failed_ansible_job_cannot_be_reported_as_updates_available():
    normalized = morning_updates.compute_maintenance.update_check_result({
        "id": "job-1", "operation": "os_check", "state": "failed",
        "summary": "playbook failed", "structuredResult": {
            "homelabhq_update": {"available": True, "count": 4,
                                 "reboot_required": False}},
    })

    assert normalized["state"] == "failed"
    assert normalized["updatesAvailable"] is False


def test_notification_formatting_partial_failures_and_truncation():
    aggregated = morning_updates.aggregate([
        source(f"device-{index}", "ansibleOs", "updates_available", updateCount=index + 1,
               name=f"long-device-name-{index}")
        for index in range(8)
    ] + [source("failed", "deviceNative", "failed", name="pool-monitor")])
    aggregated["id"] = "run-123"

    notification = morning_updates.format_notification(aggregated, max_chars=120)

    assert notification["title"] == "HomeLabHQ: 8 devices require updates"
    assert "8 need updates" in notification["body"]
    assert "…and " in notification["body"]
    assert len(notification["body"]) <= 120
    assert notification["data"]["url"] == (
        "/devices?filter=needs-attention&checkRun=run-123")


def test_success_notification_can_be_disabled_per_user(monkeypatch):
    run = {"id": "run", **morning_updates.aggregate([
        source("router", "deviceNative", "up_to_date")])}
    store.update(lambda document: document["users"]["member"].update(
        morningUpdateNotifications={
            "notifyUpdates": True, "notifyFailures": True, "notifySuccess": False}))
    sent = []
    monkeypatch.setattr(morning_updates.push, "notify", lambda *args, **kwargs:
                        sent.append(args) or {"sent": 1, "failed": 0, "removed": 0})

    delivery = morning_updates._notify(run)

    member = next(item for item in delivery if item["userId"] == "member")
    assert member["skipped"] == "disabled by user preference"
    assert all(args[0] != {"member"} for args in sent)


def test_scheduler_timezone_tracks_london_daylight_saving():
    store.update(lambda document: document["meta"].update(morningUpdateCheck={
        **morning_updates.DEFAULT_CONFIG, "timezone": "Europe/London", "runTime": "07:00"}))

    spring = morning_updates._due_occurrence(
        datetime(2026, 3, 29, 6, 0, tzinfo=timezone.utc))
    autumn = morning_updates._due_occurrence(
        datetime(2026, 10, 25, 7, 0, tzinfo=timezone.utc))

    assert spring == "2026-03-29T06:00:00Z"
    assert autumn == "2026-10-25T07:00:00Z"


def test_persistent_lock_and_scheduled_occurrence_prevent_duplicates():
    first, token = morning_updates._acquire(
        "scheduled", "scheduler", "2026-08-16T06:00:00Z")
    with pytest.raises(Conflict, match="already active"):
        morning_updates._acquire("manual", "admin")
    morning_updates._release(first["id"], token)
    with pytest.raises(Conflict, match="already run"):
        morning_updates._acquire(
            "scheduled", "scheduler", "2026-08-16T06:00:00Z")


def test_inventory_mapping_deduplicates_by_device_id_not_display_name():
    document = store.load()
    document["devices"] = {
        "canonical": {"id": "canonical", "ownerId": "member", "host": "192.0.2.20",
                      "name": "different display name"},
        "same-name": {"id": "same-name", "ownerId": "member", "host": "192.0.2.30",
                      "name": "workload"},
    }
    instance = {"id": "compute-id", "ownerId": "member", "name": "workload",
                "ansible": {"inventoryHost": "192.0.2.20"}}
    assert morning_updates._identity_for_compute(instance, document) == ("canonical", "device")
    instance["ansible"]["inventoryHost"] = "inventory-alias"
    assert morning_updates._identity_for_compute(instance, document) == ("compute-id", "compute")


def test_service_worker_click_routes_to_notification_result_url():
    worker = (ROOT / "web" / "sw.js").read_text()
    assert "e.notification.data.url" in worker
    assert "c.navigate(target)" in worker
    assert "self.clients.openWindow(target)" in worker

"""Controlled-time coverage for recurring background monitoring."""
from datetime import datetime, timezone
from pathlib import Path
import sys
import threading


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

import poller
import store
from backend.asgi.status_service import status_summary
from domain import DevicePollResult
from monitoring_scheduler import IntervalScheduler, ScheduledJob


def _configure_store(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(store, "DB_FILE", str(tmp_path / "homelabhq.json"))
    monkeypatch.setattr(store, "LOCK_FILE", str(tmp_path / "homelabhq.lock"))
    store._cache.update(doc=None, mtime=None)


def test_start_dispatches_every_monitoring_job_asynchronously():
    started = [threading.Event() for _ in range(4)]
    scheduler = IntervalScheduler([
        ScheduledJob(str(index), 60, event.set)
        for index, event in enumerate(started)
    ])

    scheduler.start()
    try:
        assert all(event.wait(1) for event in started)
    finally:
        assert scheduler.stop(1)


def test_controlled_clock_dispatches_jobs_on_their_recurring_intervals():
    calls = []
    scheduler = IntervalScheduler([
        ScheduledJob("network", 60, lambda: calls.append("network")),
        ScheduledJob("proxmox", 120, lambda: calls.append("proxmox")),
    ], clock=lambda: 0)

    assert scheduler.dispatch_due(0) == ["network", "proxmox"]
    assert scheduler.wait_for_idle(1)
    assert scheduler.dispatch_due(59) == []
    assert scheduler.dispatch_due(60) == ["network"]
    assert scheduler.wait_for_idle(1)
    assert scheduler.dispatch_due(120) == ["network", "proxmox"]
    assert scheduler.wait_for_idle(1)
    assert calls.count("network") == 3
    assert calls.count("proxmox") == 2


def test_per_job_lock_prevents_an_overlapping_run():
    entered = threading.Event()
    release = threading.Event()
    scheduler = IntervalScheduler([
        ScheduledJob("docker", 60, lambda: (entered.set(), release.wait(1))),
    ])

    assert scheduler.dispatch_due(0) == ["docker"]
    assert entered.wait(1)
    assert scheduler.dispatch_due(60) == []
    release.set()
    assert scheduler.wait_for_idle(1)
    assert scheduler.dispatch_due(120) == ["docker"]
    release.set()
    assert scheduler.wait_for_idle(1)


def test_shutdown_interrupts_waits_and_joins_cooperative_jobs():
    entered = threading.Event()
    scheduler = None

    def run_until_shutdown():
        entered.set()
        assert scheduler is not None
        scheduler.stop_event.wait()

    scheduler = IntervalScheduler([ScheduledJob("network", 60, run_until_shutdown)])
    thread = scheduler.start()
    assert entered.wait(1)
    assert scheduler.stop(1)
    assert not thread.is_alive()


def test_default_job_intervals_match_monitoring_contract():
    scheduler = poller._default_scheduler()
    assert {name: job.interval for name, job in scheduler.jobs.items()} == {
        "network": 60,
        "proxmox": 120,
        "truenas": 300,
        "docker": 300,
    }


def test_failed_poll_preserves_last_success_and_utc_source_time(monkeypatch):
    monkeypatch.setattr(poller, "OFFLINE_AFTER", 3)
    source = "2026-08-21T19:00:00Z"
    device = {"id": "switch", "state": {
        "online": True,
        "confirmedOnline": True,
        "miss": 0,
        "ts": 100,
        "sourceCheckedAt": source,
        "values": {"clients": 12},
    }}

    poller._apply_record(
        device, False, DevicePollResult(errors={"_connection": "timeout"}), 200)

    state = device["state"]
    assert state["values"] == {"clients": 12}
    assert state["sourceCheckedAt"] == source
    assert datetime.fromisoformat(state["checkedAt"].replace("Z", "+00:00")).tzinfo
    assert state["online"] is False
    assert state["confirmedOnline"] is True
    assert state["miss"] == 1


def test_docker_uses_last_success_until_its_stale_threshold(monkeypatch, tmp_path):
    _configure_store(monkeypatch, tmp_path)
    now = 2_000_000_000
    source = now - 30
    store.update(lambda document: document["computeInstances"].update(host={
        "id": "host",
        "dockerDiscoveryState": {
            "state": "failed",
            "lastJobAt": now,
            "sourceCheckedAt": datetime.fromtimestamp(source, timezone.utc).isoformat(),
        },
        "docker": {"containers": [
            {"name": "web", "state": "running", "health": "healthy"},
        ]},
    }))

    current = status_summary(now)
    stale = status_summary(source + poller.stale_after("docker") + 1)

    assert current.docker.status == "healthy"
    assert current.docker.healthy == current.docker.total == 1
    assert current.docker.source_checked_at.tzinfo is not None
    assert stale.docker.status == "stale"

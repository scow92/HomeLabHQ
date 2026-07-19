"""Background poller.

Runs in a daemon thread inside the server process (single-container deploy, no
cron needed). Every interval it reads each device's sensor entities, persists
the latest values + a short per-entity history, tracks online/offline, and — on
a reachability transition — fires a web-push notification to the device's owner
and admins.
"""
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import store
import history
import devices
import nac_service
import client_service
import logbuf
import transports
from drivers import registry
from context import POLLER_CONTEXT
from domain import DevicePollResult, DeviceState, HistoryPoint, safe_error

try:
    import push
except Exception:  # push deps optional; poller still runs without them
    push = None

POLL_INTERVAL = int(os.environ.get("HLHQ_POLL_INTERVAL", "60"))
HISTORY_MAX = 120  # points kept per numeric entity (~2h at 60s)
# Per-device poll timeout. KeepLink (and similar cheap-management-plane)
# switches briefly refuse TCP connections when their management CPU is busy;
# a little more headroom lets some of those polls land instead of timing out.
POLL_TIMEOUT = max(1, int(os.environ.get("HLHQ_POLL_TIMEOUT", "10")))
# Consecutive missed polls before a device is treated as offline for
# notifications. Debounces slow/transient management responses (e.g. KeepLink
# switches, which tend to time out for a poll or two then recover) so they don't
# flap offline/online alerts. ~5 min at the default interval. Recovery is
# immediate on the first successful poll.
OFFLINE_AFTER = max(1, int(os.environ.get("HLHQ_OFFLINE_AFTER", "5")))

_stop = threading.Event()
_thread = None
_metrics_lock = threading.Lock()
_metrics: dict[str, Any] = {
    "lastCycleStartedAt": None,
    "lastCycleCompletedAt": None,
    "lastSuccessfulCycleAt": None,
    "lastCycleDurationMs": None,
    "lastCycleError": None,
    "devices": {},
}


def _record_device_metric(dev_id: str, online: bool, result: DevicePollResult):
    """Keep bounded, process-local polling diagnostics for readiness/ops."""
    now = int(time.time())
    duration = result.elapsed
    with _metrics_lock:
        previous = _metrics["devices"].get(dev_id) or {}
        failures = 0 if online else previous.get("consecutiveFailures", 0) + 1
        value: dict[str, object] = {
            "lastPollAt": now,
            "lastDurationMs": round((duration or 0) * 1000),
            "consecutiveFailures": failures,
        }
        if online:
            value["lastSuccessAt"] = now
        else:
            value["lastFailureAt"] = now
            value["lastError"] = _short_err(result.errors)
        _metrics["devices"][dev_id] = value


def status():
    """Return safe poller observations used by readiness and diagnostics."""
    with _metrics_lock:
        data = {key: value for key, value in _metrics.items() if key != "devices"}
        data["devices"] = {key: dict(value) for key, value in _metrics["devices"].items()}
    data["running"] = bool(_thread and _thread.is_alive())
    # A completed successful cycle means the loop is functioning. Individual
    # device failures are intentionally reported separately and do not make a
    # healthy scheduler unreadable.
    data["ready"] = data["running"] and data["lastSuccessfulCycleAt"] is not None
    return data


def poll_once():
    """Poll every device once, concurrently — a slow/unreachable device no
    longer delays the ones behind it — then persist every result in a single
    store write instead of one per device. Returns the number polled."""
    dev_ids = list(store.load()["devices"].keys())
    if not dev_ids:
        return 0
    reads = {}
    with ThreadPoolExecutor(max_workers=min(8, len(dev_ids))) as ex:
        futs = {ex.submit(_read, dev_id): dev_id for dev_id in dev_ids}
        for fut in futs:
            dev_id = futs[fut]
            reads[dev_id] = fut.result()
            _record_device_metric(dev_id, *reads[dev_id])
    if not _stop.is_set():
        _record_all(reads)
    return len(dev_ids)


def enforce_bindings():
    """Keep pinned wireless clients on their preferred AP. For every AP that can
    enforce a binding, kick any client associated to it that's actually locked to
    a *different* AP. A cheap no-op when no bindings exist."""
    doc = store.load()
    devs = doc["devices"]
    pref = devices.binding_map(doc)   # mac -> preferred AP device id
    if not pref:
        return

    # Friendly labels for the Logs screen: a bound client's saved name, else its
    # hostname, else its bare MAC — plus an AP's device name.
    rosters = doc.get("clientRosters") or {}

    # Hostnames come from list_clients() (DHCP-authoritative + reverse-DNS), which
    # re-polls every client source — too heavy to run every cycle, so resolve it
    # lazily and once, only when a roam actually happens.
    _hosts = {"loaded": False, "map": {}}

    def _hostname_for(mac):
        if not _hosts["loaded"]:
            _hosts["loaded"] = True
            try:
                data = client_service.refresh(POLLER_CONTEXT, dev.get("ownerId"), timeout=6)
                for c in data.get("clients") or []:
                    h = (c.get("hostname") or "").strip()
                    if h:
                        _hosts["map"][(c.get("mac") or "").upper()] = h
            except Exception:
                pass
        return _hosts["map"].get((mac or "").upper(), "")

    def _client_label(mac):
        rec = (rosters.get(dev.get("ownerId"), {})
               .get((mac or "").upper()) or {})
        name = (rec.get("name") or "").strip() or _hostname_for(mac)
        return f"{name} ({mac})" if name else mac

    def _ap_name(dev_id):
        return (devs.get(dev_id) or {}).get("name") or dev_id

    # Never roam a client off its current AP toward a preferred AP that's
    # offline/unreachable — it would have nowhere to land and just bounce. Uses
    # the latest poll state (poll_once ran first this cycle). Mirrors Network
    # Manager's safety check.
    def _ap_online(dev_id):
        st = (devs.get(dev_id) or {}).get("state") or {}
        return bool(st.get("online"))

    for dev in list(devs.values()):
        # Only APs the user opted into roam-binding on (SSH-verified) do the
        # kicking — so we never SSH-roam on a device that isn't set up for it.
        if not dev.get("apBinding"):
            continue
        roam_off = {m for m, pid in pref.items()
                    if pid != dev["id"] and _ap_online(pid)}
        if not roam_off:
            continue
        drv = registry.get(dev.get("driverId"))
        if not drv or not getattr(drv, "supports_binding", False):
            continue
        try:
            conn = devices.open_conn(dev, timeout=20)
        except Exception:
            continue  # AP unreachable this round; try again next interval
        try:
            res = drv.enforce_bindings(conn, roam_off) or {}
            for mac in res.get("roamed") or []:
                pref_ap = _ap_name(pref.get((mac or "").upper()))
                _plog("info",
                      f"Force-roamed {_client_label(mac)} off "
                      f"{_ap_name(dev['id'])} -> preferred AP {pref_ap}")
        except Exception as error:
            _plog("error", "AP binding enforcement failed", device_id=dev["id"],
                  error=safe_error(error))
        finally:
            try:
                conn.close()
            except Exception:
                pass


def _plog(level, message, **fields):
    """Record a poller event on the admin Logs screen (shared ring buffer)."""
    logbuf.log_event(level, "poll", source="poller", message=message, **fields)


def _short_err(errs):
    """Condense a poll's error dict into one short, readable phrase — the raw
    urllib3 ConnectionError string is a screenful and overflows on mobile."""
    if not errs:
        return ""
    out = []
    for v in errs.values():
        s = str(v); low = s.lower()
        if "connect timeout" in low or "timed out" in low:
            out.append("connection timed out")
        elif "refused" in low:
            out.append("connection refused")
        elif "no route to host" in low:
            out.append("no route to host")
        elif "name or service not known" in low or "nodename nor servname" in low:
            out.append("DNS lookup failed")
        else:
            out.append(s.split(" (Caused by")[0].strip()[:120])
    return "; ".join(out)


def _read(dev_id):
    t0 = time.time()
    try:
        result = devices.poll_read(dev_id, timeout=POLL_TIMEOUT)
        return True, DevicePollResult(values=result.values, errors=result.errors,
                                      interfaces=result.interfaces,
                                      elapsed=round(time.time() - t0, 1))
    except transports.ConnectionError as e:
        return False, DevicePollResult(errors={"_connection": safe_error(e)},
                                       elapsed=round(time.time() - t0, 1))
    except Exception as e:
        return False, DevicePollResult(errors={"_error": safe_error(e)},
                                       elapsed=round(time.time() - t0, 1))


def _apply_record(dev, online, result, ts):
    """Mutate one device record in place with a poll result: latest
    (debounced) reachability state and alert-rule evaluation. Returns a dict
    the caller uses after the write commits: {dev, online, miss, transition,
    alert_events, samples, if_samples}. transition is 'online', 'offline', or
    None. samples/if_samples are the numeric points to append to this
    device's history file — history itself no longer lives on the device
    record (see history.py), so this stays a pure read of `result`."""
    prev = dev.get("state") or {}
    prev_confirmed = prev.get("confirmedOnline")
    # Debounced reachability: count consecutive misses and only flip to
    # offline once we've missed OFFLINE_AFTER polls in a row. A single slow
    # poll (e.g. KeepLink management lag) keeps the confirmed state. Recovery
    # is immediate on the first successful poll.
    miss = 0 if online else prev.get("miss", 0) + 1
    if online:
        confirmed = True
    elif miss >= OFFLINE_AFTER:
        confirmed = False
    else:
        confirmed = True if prev_confirmed is None else prev_confirmed
    # `since`: when the *confirmed* state last changed — lets the UI show
    # "offline for 3h" instead of just a dot. Reset on every transition;
    # carried forward otherwise (falling back to `ts` for older records or
    # the very first poll, so it's never missing).
    since = prev.get("since") or ts
    if prev_confirmed is not None and prev_confirmed != confirmed:
        since = ts
    dev["state"] = DeviceState(online=online, confirmed_online=confirmed, misses=miss,
                                 values=result.values, errors=result.errors,
                                 timestamp=ts, since=since).to_dict()
    samples = {k: v for k, v in result.values.items()
              if isinstance(v, (int, float)) and not isinstance(v, bool)}
    # Per-interface rx/tx counters -> per-interface upload/download history.
    if_samples = {}
    for f in result.interfaces:
        dvc = f.get("device")
        if not dvc:
            continue
        entry = {"name": f.get("name") or dvc}
        for key in ("rx", "tx"):
            val = f.get(key)
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                entry[key] = val
        if_samples[dvc] = entry
    # Threshold alerts: evaluate the device's rules against the fresh values
    # and edge-trigger (notify only when a rule crosses into or out of
    # breach), tracked in dev['alertState'] keyed by rule identity.
    alert_events = _eval_alerts(dev, result.values)
    # Notify on the debounced (confirmed) state, not the raw poll, and only
    # once we have a known previous state (skip the first poll).
    if prev_confirmed is None or prev_confirmed == confirmed:
        transition = None
    else:
        transition = "online" if confirmed else "offline"
    return {"dev": dict(dev), "online": online, "miss": miss,
            "transition": transition, "alert_events": alert_events,
            "samples": samples, "if_samples": if_samples}


def _append_history(dev_id, ts, samples, if_samples, online):
    """Append this cycle's samples + the per-poll online flag to dev_id's
    history file, trimming each series to its cap. Runs outside the store
    lock (one small file write per polled device, same shape as the old
    inline arrays). Always writes: an offline poll has no samples but its
    online=0 point is exactly what the availability strip needs."""

    def mut(doc):
        # Reachability series behind the detail view's 24h availability strip.
        onl = doc.setdefault("online", [])
        onl.append(HistoryPoint(ts, 1 if online else 0).to_wire())
        if len(onl) > history.ONLINE_MAX:
            del onl[:-history.ONLINE_MAX]
        hist = doc.setdefault("history", {})
        long_hist = doc.setdefault("historyLong", {})
        for k, v in samples.items():
            arr = hist.setdefault(k, [])
            arr.append(HistoryPoint(ts, v).to_wire())
            if len(arr) > HISTORY_MAX:
                del arr[:-HISTORY_MAX]
            # Long-range series: one sample per LONG_INTERVAL, kept for ~7d,
            # backing the chart 24h/7d ranges (see history.LONG_INTERVAL).
            larr = long_hist.setdefault(k, [])
            if not larr or ts - larr[-1][0] >= history.LONG_INTERVAL:
                larr.append(HistoryPoint(ts, v).to_wire())
                if len(larr) > history.LONG_MAX:
                    del larr[:-history.LONG_MAX]
        ifh = doc.setdefault("ifHistory", {})
        for dvc, entry in if_samples.items():
            rec = ifh.setdefault(dvc, {"name": entry["name"], "rx": [], "tx": []})
            rec["name"] = entry["name"]
            for key in ("rx", "tx"):
                if key not in entry:
                    continue
                arr = rec.setdefault(key, [])
                arr.append(HistoryPoint(ts, entry[key]).to_wire())
                if len(arr) > HISTORY_MAX:
                    del arr[:-HISTORY_MAX]

    history.update(dev_id, mut)


def _record_all(reads):
    """Persist every device's poll result (dev_id -> (online, result)) in one
    store write, then append history + log/notify each outside the lock."""
    ts = int(time.time())
    captured = {}

    def mut(doc):
        for dev_id, (online, result) in reads.items():
            dev = doc["devices"].get(dev_id)
            if dev is not None:
                captured[dev_id] = _apply_record(dev, online, result, ts)

    store.update(mut)
    for dev_id, cap in captured.items():
        _append_history(dev_id, ts, cap.get("samples"), cap.get("if_samples"),
                        cap.get("online"))
        _finish_one(dev_id, reads[dev_id][1], cap)


def _finish_one(dev_id, result, captured):
    """Diagnostics + notifications for one device's poll, run after the
    batched write commits. Records every missed poll (with latency + error)
    and each confirmed reachability transition on the Logs screen, so
    intermittent flapping on slow management planes is visible after the
    fact; fires push notifications for transitions and alert edges."""
    dev = captured.get("dev")
    transition = captured.get("transition")
    if dev is not None:
        name = dev.get("name") or dev.get("host") or dev_id
        if not captured.get("online"):
            errs = _short_err(result.errors)
            elapsed = result.elapsed
            took = f", {elapsed}s" if elapsed is not None else ""
            _plog("warn", f"{name}: poll failed (miss {captured.get('miss')}/"
                          f"{OFFLINE_AFTER}{took}) — {errs}".rstrip(" —").rstrip(),
                  device_id=dev_id, duration_ms=round((elapsed or 0) * 1000))
        if transition == "offline":
            _plog("error", f"{name}: OFFLINE — {OFFLINE_AFTER} missed polls in a row",
                  device_id=dev_id)
        elif transition == "online":
            _plog("info", f"{name}: back online", device_id=dev_id)
    if dev and push is not None:
        if transition:
            _notify(dev, transition)
        for rule, cur, breached in captured.get("alert_events") or []:
            _notify_alert(dev, rule, cur, breached)


def _rule_id(rule):
    return f"{rule.get('key')}:{rule.get('op')}:{rule.get('value')}"


def _eval_alerts(dev, values):
    """Evaluate dev's threshold rules against fresh values. Mutates
    dev['alertState'] and returns the list of (rule, current, breached) that
    just changed state, so the caller can notify. Only numeric values are
    evaluated; an offline device (no value) never flips a rule."""
    rules = dev.get("alerts") or []
    if not rules:
        dev.pop("alertState", None)
        return []
    state = dev.setdefault("alertState", {})
    events, live_ids = [], set()
    for rule in rules:
        rid = _rule_id(rule)
        live_ids.add(rid)
        cur = values.get(rule.get("key"))
        if isinstance(cur, bool) or not isinstance(cur, (int, float)):
            continue
        try:
            thr = float(rule.get("value"))
        except (TypeError, ValueError):
            continue
        breached = cur > thr if rule.get("op") == "above" else cur < thr
        if state.get(rid) != breached:
            state[rid] = breached
            events.append((rule, cur, breached))
    # Drop state for rules that no longer exist.
    for rid in list(state):
        if rid not in live_ids:
            del state[rid]
    return events


def _notify_alert(dev, rule, cur, breached):
    name = dev.get("name") or dev.get("host")
    label = rule.get("label") or rule.get("key")
    sign = ">" if rule.get("op") == "above" else "<"
    if breached:
        title = f"Alert: {name}"
        body = f"{label} is {cur} ({sign} {rule.get('value')})."
    else:
        title = f"Recovered: {name}"
        body = f"{label} back to {cur}."
    try:
        push.notify(push.recipients_for_device(dev), title, body,
                    data={"deviceId": dev["id"], "type": "alert",
                          "key": rule.get("key"), "breached": breached})
    except Exception as error:
        _plog("error", "alert push delivery failed", device_id=dev["id"],
              error=safe_error(error))


def _notify(dev, transition):
    name = dev.get("name") or dev.get("host")
    if transition == "offline":
        title, body = "Device offline", f"{name} became unreachable."
    else:
        title, body = "Device back online", f"{name} is reachable again."
    recipients = push.recipients_for_device(dev)
    try:
        push.notify(recipients, title, body,
                    data={"deviceId": dev["id"], "type": transition})
    except Exception as error:
        _plog("error", "transition push delivery failed", device_id=dev["id"],
              error=safe_error(error))


def notify_new_devices():
    """Scan the NAC firewall for newly-appeared, unapproved clients and push a
    "new device" notification to the owner + admins — once per device. A cheap
    no-op when NAC isn't set up. Mirrors Network Manager's new-device alerts."""
    if push is None:
        return
    try:
        dev, events = nac_service.scan_new_clients()
    except Exception as error:
        logbuf.log_event("error", "client_scan", source="poller", error=safe_error(error))
        return
    if not dev or not events:
        return
    # New-client events belong to the NAC device owner's per-owner roster.
    recipients = {dev["ownerId"]}
    for e in events:
        vendor = f" — {e['vendor']}" if e.get("vendor") else ""
        where = f" on {e['where']}" if e.get("where") else ""
        try:
            push.notify(recipients, "New device on your network",
                        f"{e['name']} ({e['mac']}){vendor}{where}",
                        data={"type": "new_device", "mac": e["mac"],
                              "deviceId": dev["id"]})
        except Exception as error:
            logbuf.log_event("error", "push_delivery", source="poller",
                             device_id=dev["id"], error=safe_error(error))


def _loop():
    _plog("info", f"started, interval {POLL_INTERVAL}s")
    while not _stop.is_set():
        started = time.monotonic()
        with _metrics_lock:
            _metrics["lastCycleStartedAt"] = int(time.time())
            _metrics["lastCycleError"] = None
        try:
            poll_once()
        except Exception as error:
            with _metrics_lock:
                _metrics["lastCycleError"] = safe_error(error)
            logbuf.log_event("error", "poll_cycle", source="poller", error=safe_error(error))
        else:
            with _metrics_lock:
                _metrics["lastSuccessfulCycleAt"] = int(time.time())
        try:
            enforce_bindings()
        except Exception as error:
            logbuf.log_event("error", "binding_cycle", source="poller", error=safe_error(error))
        try:
            notify_new_devices()
        except Exception as error:
            logbuf.log_event("error", "client_scan", source="poller", error=safe_error(error))
        try:
            # Persistent Access roster: rate-limits itself (default 5 min), so
            # connection history accrues without a browser open.
            client_service.refresh_rosters(POLLER_CONTEXT)
        except Exception as error:
            logbuf.log_event("error", "roster_tracking", source="poller", error=safe_error(error))
        finally:
            with _metrics_lock:
                _metrics["lastCycleCompletedAt"] = int(time.time())
                _metrics["lastCycleDurationMs"] = round((time.monotonic() - started) * 1000)
        _stop.wait(POLL_INTERVAL)
    _plog("info", "stopped")


def start():
    global _thread
    if _thread and _thread.is_alive():
        return _thread
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="poller", daemon=True)
    _thread.start()
    return _thread


def stop(timeout=10):
    """Signal the poller and wait for its thread to leave the loop.

    ``Event.wait`` makes the normal interval sleep interruptible, so shutdown
    does not spend up to one poll interval keeping the process alive.
    """
    _stop.set()
    thread = _thread
    if thread and thread is not threading.current_thread():
        thread.join(timeout)
    return not (thread and thread.is_alive())

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
import traceback

import store
import devices
import logbuf
import transports
from drivers import registry

try:
    import push
except Exception:  # push deps optional; poller still runs without them
    push = None

POLL_INTERVAL = int(os.environ.get("HLHQ_POLL_INTERVAL", "60"))
HISTORY_MAX = 120  # points kept per numeric entity (~2h at 60s)
# Consecutive missed polls before a device is treated as offline for
# notifications. Debounces slow/transient management responses (e.g. KeepLink
# switches) so they don't flap offline/online alerts. Recovery is immediate.
OFFLINE_AFTER = max(1, int(os.environ.get("HLHQ_OFFLINE_AFTER", "3")))

_stop = threading.Event()
_thread = None


def poll_once():
    """Poll every device once. Returns the number polled."""
    dev_ids = list(store.load()["devices"].keys())
    for dev_id in dev_ids:
        if _stop.is_set():
            break
        online, result = _read(dev_id)
        _record(dev_id, online, result)
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
            drv.enforce_bindings(conn, roam_off)
        except Exception:
            traceback.print_exc()
        finally:
            try:
                conn.close()
            except Exception:
                pass


def _plog(level, message):
    """Record a poller event on the admin Logs screen (shared ring buffer)."""
    logbuf.log_note(level, message, source="poller")


def _read(dev_id):
    t0 = time.time()
    try:
        result = devices.poll_read(dev_id, timeout=8)
        result["_elapsed"] = round(time.time() - t0, 1)
        return True, result
    except transports.ConnectionError as e:
        return False, {"values": {}, "errors": {"_connection": str(e)},
                       "interfaces": [], "_elapsed": round(time.time() - t0, 1)}
    except Exception as e:
        return False, {"values": {}, "errors": {"_error": str(e)},
                       "interfaces": [], "_elapsed": round(time.time() - t0, 1)}


def _record(dev_id, online, result):
    """Persist latest state + history; return (device, transition) so callers
    can notify. transition is 'online', 'offline', or None."""
    ts = int(time.time())
    captured = {}

    def mut(doc):
        dev = doc["devices"].get(dev_id)
        if not dev:
            return
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
        dev["state"] = {"online": online, "confirmedOnline": confirmed,
                        "miss": miss, "values": result["values"],
                        "errors": result["errors"], "ts": ts}
        hist = dev.setdefault("history", {})
        for k, v in result["values"].items():
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                continue
            arr = hist.setdefault(k, [])
            arr.append([ts, v])
            if len(arr) > HISTORY_MAX:
                del arr[:-HISTORY_MAX]
        # Per-interface rx/tx counters -> per-interface upload/download history.
        ifh = dev.setdefault("ifHistory", {})
        for f in result.get("interfaces") or []:
            dvc = f.get("device")
            if not dvc:
                continue
            rec = ifh.setdefault(dvc, {"name": dvc, "rx": [], "tx": []})
            rec["name"] = f.get("name") or dvc
            for key in ("rx", "tx"):
                val = f.get(key)
                if isinstance(val, (int, float)) and not isinstance(val, bool):
                    arr = rec.setdefault(key, [])
                    arr.append([ts, val])
                    if len(arr) > HISTORY_MAX:
                        del arr[:-HISTORY_MAX]
        # Threshold alerts: evaluate the device's rules against the fresh values
        # and edge-trigger (notify only when a rule crosses into or out of
        # breach), tracked in dev['alertState'] keyed by rule identity.
        captured["alert_events"] = _eval_alerts(dev, result["values"])
        captured["dev"] = dict(dev)
        captured["online"] = online
        captured["miss"] = miss
        # Notify on the debounced (confirmed) state, not the raw poll, and only
        # once we have a known previous state (skip the first poll).
        if prev_confirmed is None or prev_confirmed == confirmed:
            captured["transition"] = None
        else:
            captured["transition"] = "online" if confirmed else "offline"

    store.update(mut)
    dev = captured.get("dev")
    transition = captured.get("transition")
    # Diagnostics: record every missed poll (with latency + error) and each
    # confirmed reachability transition on the Logs screen, so intermittent
    # flapping on slow management planes is visible after the fact.
    if dev is not None:
        name = dev.get("name") or dev.get("host") or dev_id
        if not captured.get("online"):
            errs = "; ".join(f"{k}={v}" for k, v in (result.get("errors") or {}).items())
            elapsed = result.get("_elapsed")
            took = f", {elapsed}s" if elapsed is not None else ""
            _plog("warn", f"{name}: poll failed (miss {captured.get('miss')}/"
                          f"{OFFLINE_AFTER}{took}) {errs}".rstrip())
        if transition == "offline":
            _plog("error", f"{name}: OFFLINE — {OFFLINE_AFTER} missed polls in a row")
        elif transition == "online":
            _plog("info", f"{name}: back online")
    if dev and push is not None:
        if transition:
            _notify(dev, transition)
        for rule, cur, breached in captured.get("alert_events") or []:
            _notify_alert(dev, rule, cur, breached)
    return dev, transition


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
    except Exception:
        traceback.print_exc()


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
    except Exception:
        traceback.print_exc()


def notify_new_devices():
    """Scan the NAC firewall for newly-appeared, unapproved clients and push a
    "new device" notification to the owner + admins — once per device. A cheap
    no-op when NAC isn't set up. Mirrors Network Manager's new-device alerts."""
    if push is None:
        return
    try:
        dev, events = devices.scan_new_clients()
    except Exception:
        traceback.print_exc()
        return
    if not dev or not events:
        return
    recipients = push.recipients_for_device(dev)
    for e in events:
        vendor = f" — {e['vendor']}" if e.get("vendor") else ""
        where = f" on {e['where']}" if e.get("where") else ""
        try:
            push.notify(recipients, "New device on your network",
                        f"{e['name']} ({e['mac']}){vendor}{where}",
                        data={"type": "new_device", "mac": e["mac"],
                              "deviceId": dev["id"]})
        except Exception:
            traceback.print_exc()


def _loop():
    print(f"poller: started, interval {POLL_INTERVAL}s", flush=True)
    while not _stop.is_set():
        try:
            poll_once()
        except Exception:
            traceback.print_exc()
        try:
            enforce_bindings()
        except Exception:
            traceback.print_exc()
        try:
            notify_new_devices()
        except Exception:
            traceback.print_exc()
        _stop.wait(POLL_INTERVAL)


def start():
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="poller", daemon=True)
    _thread.start()


def stop():
    _stop.set()

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
from concurrent.futures import ThreadPoolExecutor

import store
import devices
import clients
import nac
import logbuf
import transports
from drivers import registry

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
            reads[futs[fut]] = fut.result()
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
    nac = (doc.get("meta") or {}).get("nacClients") or {}

    # Hostnames come from list_clients() (DHCP-authoritative + reverse-DNS), which
    # re-polls every client source — too heavy to run every cycle, so resolve it
    # lazily and once, only when a roam actually happens.
    _hosts = {"loaded": False, "map": {}}

    def _hostname_for(mac):
        if not _hosts["loaded"]:
            _hosts["loaded"] = True
            try:
                data = clients.list_clients(owner_id=None, is_admin=True, timeout=6)
                for c in data.get("clients") or []:
                    h = (c.get("hostname") or "").strip()
                    if h:
                        _hosts["map"][(c.get("mac") or "").upper()] = h
            except Exception:
                pass
        return _hosts["map"].get((mac or "").upper(), "")

    def _client_label(mac):
        rec = nac.get((mac or "").upper()) or {}
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
        result["_elapsed"] = round(time.time() - t0, 1)
        return True, result
    except transports.ConnectionError as e:
        return False, {"values": {}, "errors": {"_connection": str(e)},
                       "interfaces": [], "_elapsed": round(time.time() - t0, 1)}
    except Exception as e:
        return False, {"values": {}, "errors": {"_error": str(e)},
                       "interfaces": [], "_elapsed": round(time.time() - t0, 1)}


def _apply_record(dev, online, result, ts):
    """Mutate one device record in place with a poll result: latest
    (debounced) reachability state, per-entity history, per-interface
    rx/tx history, and alert-rule evaluation. Returns a dict the caller uses
    after the write commits to log/notify: {dev, online, miss, transition,
    alert_events}. transition is 'online', 'offline', or None."""
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
    alert_events = _eval_alerts(dev, result["values"])
    # Notify on the debounced (confirmed) state, not the raw poll, and only
    # once we have a known previous state (skip the first poll).
    if prev_confirmed is None or prev_confirmed == confirmed:
        transition = None
    else:
        transition = "online" if confirmed else "offline"
    return {"dev": dict(dev), "online": online, "miss": miss,
            "transition": transition, "alert_events": alert_events}


def _record_all(reads):
    """Persist every device's poll result (dev_id -> (online, result)) in one
    store write, then log/notify each outside the lock."""
    ts = int(time.time())
    captured = {}

    def mut(doc):
        for dev_id, (online, result) in reads.items():
            dev = doc["devices"].get(dev_id)
            if dev is not None:
                captured[dev_id] = _apply_record(dev, online, result, ts)

    store.update(mut)
    for dev_id, cap in captured.items():
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
            errs = _short_err(result.get("errors"))
            elapsed = result.get("_elapsed")
            took = f", {elapsed}s" if elapsed is not None else ""
            _plog("warn", f"{name}: poll failed (miss {captured.get('miss')}/"
                          f"{OFFLINE_AFTER}{took}) — {errs}".rstrip(" —").rstrip())
        if transition == "offline":
            _plog("error", f"{name}: OFFLINE — {OFFLINE_AFTER} missed polls in a row")
        elif transition == "online":
            _plog("info", f"{name}: back online")
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
        dev, events = nac.scan_new_clients()
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
        try:
            # Persistent Access roster: rate-limits itself (default 5 min), so
            # connection history accrues without a browser open.
            clients.track_roster()
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

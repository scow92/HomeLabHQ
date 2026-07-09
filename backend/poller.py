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
import transports

try:
    import push
except Exception:  # push deps optional; poller still runs without them
    push = None

POLL_INTERVAL = int(os.environ.get("HLHQ_POLL_INTERVAL", "60"))
HISTORY_MAX = 120  # points kept per numeric entity (~2h at 60s)

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


def _read(dev_id):
    try:
        return True, devices.poll_read(dev_id, timeout=8)
    except transports.ConnectionError as e:
        return False, {"values": {}, "errors": {"_connection": str(e)}, "interfaces": []}
    except Exception as e:
        return False, {"values": {}, "errors": {"_error": str(e)}, "interfaces": []}


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
        prev_online = prev.get("online")
        dev["state"] = {"online": online, "values": result["values"],
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
        captured["dev"] = dict(dev)
        # transition only when we have a known previous state (skip first poll)
        if prev_online is None or prev_online == online:
            captured["transition"] = None
        else:
            captured["transition"] = "online" if online else "offline"

    store.update(mut)
    dev = captured.get("dev")
    transition = captured.get("transition")
    if dev and transition and push is not None:
        _notify(dev, transition)
    return dev, transition


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


def _loop():
    print(f"poller: started, interval {POLL_INTERVAL}s", flush=True)
    while not _stop.is_set():
        try:
            poll_once()
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

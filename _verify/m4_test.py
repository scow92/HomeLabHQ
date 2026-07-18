"""Verifies Milestone 4 logic without real gear or a real push service:
  - poller persists latest state + per-entity history, tracks online/offline,
    and fires a notification on a reachability transition (not on first poll);
  - push selects owner+admins as recipients, delivers to their subscriptions,
    and prunes ones the push service reports gone (410).
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("HLHQ_DATA_DIR", tempfile.mkdtemp(prefix="hlhq-verify-"))
sys.path.insert(0, os.path.join(HERE, "..", "backend"))

import transports, devices, store, history, poller, push, auth  # noqa: E402
from drivers.base import Driver, Entity, SENSOR  # noqa: E402
from drivers.registry import register  # noqa: E402

fails = []
def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond: fails.append(name)

# ---- fake togglable device ----
ONLINE = {"v": True}
class FakeConn(transports.Connection):
    transport = "fake"; host = "10.0.0.1"
    def info(self): return ""
    def close(self): pass
class FakeDriver(Driver):
    id = "fake.dev"; display_name = "Fake"; transports = ["fake"]
    def probe(self, conn): return 0.9
    def entities(self, conn):
        return [Entity("reachable", "Reachable", SENSOR, read=lambda: True),
                Entity("count", "Count", SENSOR, read=lambda: 5)]
register(FakeDriver())

def fake_open(transport, host, port=None, credentials=None, timeout=8):
    if not ONLINE["v"]:
        raise transports.ConnectionError("simulated down")
    return FakeConn()
transports.open_connection = fake_open

# capture poller -> push transitions
notes = []
push.notify = lambda uids, title, body, data=None: notes.append((title, data))

# ---- poll lifecycle ----
dev = devices.create_device("owner1", "10.0.0.1", "fake", None, {}, "fake.dev",
                            "Box", entities=[{"key": "reachable"}, {"key": "count"}])
did = dev["id"]

poller.poll_once()  # first poll — online, no transition (no prior state)
s = devices.get_device(did)["state"]
check("first poll records online=True", s["online"] is True)
check("first poll captures values", s["values"].get("count") == 5)
check("no notification on first poll", len(notes) == 0)

poller.poll_once()  # second poll — history should grow, still no transition
hist = history.load(did)["history"]
check("history accumulates for numeric entity", len(hist.get("count", [])) == 2)
check("history is not embedded on the device record",
      "history" not in devices.get_device(did))
check("still no notification (no change)", len(notes) == 0)

ONLINE["v"] = False
for _ in range(poller.OFFLINE_AFTER):
    poller.poll_once()  # debounce before the confirmed offline transition
s = devices.get_device(did)["state"]
check("poll records online=False when unreachable", s["online"] is False)
check("confirmed offline after the debounce window", s["confirmedOnline"] is False)
check("offline transition fired a notification", notes and notes[-1][1]["type"] == "offline")

ONLINE["v"] = True
poller.poll_once()  # back up — online transition -> notify
check("online transition fired a notification", notes[-1][1]["type"] == "online")

# ---- push recipient + delivery + prune ----
push.notify = push.__dict__.get("notify")  # restore real notify? re-import
import importlib; importlib.reload(push)

# users: an admin + the owner (member)
admin = auth.create_user("admin_u", "pw", role="admin")
owner = store.load()["users"]  # ensure exists
# make owner1 a real member user id so recipients match
store.update(lambda d: d["users"].__setitem__("owner1", {"id": "owner1", "username": "owner", "passHash": "x", "role": "member"}))

recips = push.recipients_for_device(devices.get_device(did))
check("recipients include device owner", "owner1" in recips)
check("recipients include admin", admin["id"] in recips)

# two subscriptions: owner + admin
push.subscribe("owner1", {"endpoint": "https://push.example/aaa", "keys": {"p256dh": "x", "auth": "y"}})
push.subscribe(admin["id"], {"endpoint": "https://push.example/bbb", "keys": {"p256dh": "x", "auth": "y"}})

# mock the actual webpush send: bbb is 'gone' (410)
import pywebpush
sent = []
class _Resp:  # minimal response with a status_code
    def __init__(self, c): self.status_code = c
def fake_webpush(subscription_info, data, vapid_private_key, vapid_claims):
    ep = subscription_info["endpoint"]
    if ep.endswith("bbb"):
        raise pywebpush.WebPushException("gone", response=_Resp(410))
    sent.append(ep)
pywebpush.webpush = fake_webpush

res = push.notify(recips, "T", "B", data={"x": 1})
check("delivered to the live subscription", sent == ["https://push.example/aaa"])
check("reported one send", res["sent"] == 1)
check("pruned the 410 subscription", res["removed"] == 1)
remaining = store.load()["push_subs"]
check("410 subscription removed from store", "https://push.example/bbb" not in remaining)
check("live subscription retained", "https://push.example/aaa" in remaining)

print("\n" + ("ALL PASSED" if not fails else f"{len(fails)} FAILED: {fails}"))
sys.exit(1 if fails else 0)

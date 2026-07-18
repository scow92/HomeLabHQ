"""Verifies history storage lives in per-device files (history.py), not the
main JSON doc:
  - poller samples land in history/<id>.json and never touch the main doc;
  - series trim at HISTORY_MAX;
  - read_detail() and the /history series lookup keep their old shape;
  - delete_device() unlinks the device's history file;
  - migrate_from_store() moves legacy embedded history/ifHistory out of the
    main doc and is idempotent (a second run is a no-op).
"""
import json
import os
import sys
import tempfile

os.environ.setdefault("HLHQ_DATA_DIR", tempfile.mkdtemp(prefix="hlhq-history-"))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend"))

import transports, devices, store, history, poller  # noqa: E402
from drivers.base import Driver, Entity, SENSOR  # noqa: E402
from drivers.registry import register  # noqa: E402

fails = []
def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


# ---- fake device: one numeric sensor + one interface with rx/tx counters ---
class FakeConn(transports.Connection):
    transport = "fake"; host = "10.0.0.9"
    def info(self): return ""
    def close(self): pass

class FakeDriver(Driver):
    id = "fake.history"; display_name = "Fake"; transports = ["fake"]
    def probe(self, conn): return 0.9
    def entities(self, conn):
        return [Entity("count", "Count", SENSOR, read=lambda: 5)]
    def interfaces(self, conn):
        return [{"device": "eth0", "name": "WAN", "rx": 1000, "tx": 500}]

register(FakeDriver())
transports.open_connection = (
    lambda transport, host, port=None, credentials=None, timeout=8: FakeConn())

dev = devices.create_device("owner1", "10.0.0.9", "fake", None, {},
                            "fake.history", "Box", entities=[{"key": "count"}])
dev_id = dev["id"]

# ---- poll: samples land in the history file, not the main doc -------------
poller.poll_once()
poller.poll_once()

hist_path = os.path.join(store.DATA_DIR, "history", f"{dev_id}.json")
check("history file created", os.path.exists(hist_path))
with open(hist_path) as f:
    on_disk = json.load(f)
check("history file compact (no indent)", "\n" not in open(hist_path).read())
check("entity samples recorded", len(on_disk["history"].get("count", [])) == 2)
check("interface samples recorded",
      len(on_disk["ifHistory"].get("eth0", {}).get("rx", [])) == 2)

doc = store.load()
check("main doc device record has no history key",
      "history" not in doc["devices"][dev_id])
check("main doc device record has no ifHistory key",
      "ifHistory" not in doc["devices"][dev_id])

# ---- trim at HISTORY_MAX ---------------------------------------------------
def _mut(d):
    d["history"]["count"] = [[i, i] for i in range(poller.HISTORY_MAX - 1)]
history.update(dev_id, _mut)
poller.poll_once()
trimmed = history.load(dev_id)["history"]["count"]
check("series trimmed to HISTORY_MAX", len(trimmed) == poller.HISTORY_MAX)

# ---- read_detail / series lookup keep their old shape ----------------------
det = devices.read_detail(dev_id)
check("read_detail exposes history", det["history"].get("count") == trimmed)
check("read_detail exposes ifHistory",
      det["ifHistory"].get("eth0", {}).get("name") == "WAN")
check("history.series matches the /history endpoint's data source",
      history.series(dev_id, "count") == trimmed)

# ---- delete_device unlinks the history file --------------------------------
devices.delete_device(dev_id)
check("history file removed on delete", not os.path.exists(hist_path))
check("deleting twice (no file) doesn't raise",
      history.delete(dev_id) is None)

# ---- migration moves legacy embedded history, then is a no-op -------------
dev2 = devices.create_device("owner1", "10.0.0.10", "fake", None, {},
                             "fake.history", "Legacy", entities=[{"key": "count"}])
dev2_id = dev2["id"]

def _embed_legacy(d):
    d["devices"][dev2_id]["history"] = {"count": [[1, 1], [2, 2]]}
    d["devices"][dev2_id]["ifHistory"] = {"eth0": {"name": "WAN", "rx": [[1, 10]], "tx": []}}
store.update(_embed_legacy)

moved = history.migrate_from_store()
check("migration reports the legacy device", moved == [dev2_id])
doc = store.load()
check("migration strips history from the main doc",
      "history" not in doc["devices"][dev2_id]
      and "ifHistory" not in doc["devices"][dev2_id])
migrated = history.load(dev2_id)
check("migration preserves the entity series",
      migrated["history"]["count"] == [[1, 1], [2, 2]])
check("migration preserves the interface series",
      migrated["ifHistory"]["eth0"]["rx"] == [[1, 10]])

moved_again = history.migrate_from_store()
check("re-running migration is a no-op", moved_again == [])

print("\n" + ("ALL PASSED" if not fails else f"{len(fails)} FAILED: {fails}"))
sys.exit(1 if fails else 0)

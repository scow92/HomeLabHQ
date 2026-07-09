"""Exercises detect -> create -> read through the REAL store/crypto/devices
stack, with a fake transport so no network gear is needed. Verifies:
  - detection ranks a registered driver,
  - credentials are Fernet-encrypted at rest (no plaintext in the JSON),
  - a saved device reads live sensor values back.
"""
import json
import os
import sys

sys.path.insert(0, "/app/backend")

import transports  # noqa: E402
import detect       # noqa: E402
import devices      # noqa: E402
import store        # noqa: E402
from drivers.base import Driver, Entity, SENSOR  # noqa: E402
from drivers.registry import register            # noqa: E402


# ---- a fake driver + connection over a fake transport ----------------------
class FakeConn:
    transport = "fake"
    host = "203.0.113.9"

    def __init__(self, creds):
        self.creds = creds

    def info(self):
        return "FakeOS 1.0"

    def close(self):
        pass


class FakeDriver(Driver):
    id = "fake.device"
    display_name = "Fake test device"
    transports = ["fake"]

    def probe(self, conn):
        return 0.9

    def entities(self, conn):
        return [
            Entity("model", "Model", SENSOR, read=lambda: "FZ-1000"),
            Entity("uptime", "Uptime", SENSOR, unit="s", read=lambda: 4242),
            # secret echoes the decrypted credential to prove it round-trips
            Entity("echo_secret", "Echo", SENSOR,
                   read=lambda: conn_holder["c"].creds.get("password")),
        ]


conn_holder = {"c": None}


def fake_open(transport, host, port=None, credentials=None, timeout=8):
    c = FakeConn(credentials or {})
    conn_holder["c"] = c
    return c


register(FakeDriver())
transports.open_connection = fake_open  # monkeypatch the factory

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


# ---- 1. detection ranks the fake driver ----
res = detect.detect("fake", "203.0.113.9", None, {"password": "topsecret"})
check("detect returns fake.device as top candidate",
      res["candidates"] and res["candidates"][0]["driverId"] == "fake.device")
check("detect reports confidence 0.9",
      res["candidates"][0]["confidence"] == 0.9)

# ---- 2. create a device with credentials ----
dev = devices.create_device(
    owner_id="user1", host="203.0.113.9", transport="fake", port=None,
    credentials={"username": "root", "password": "topsecret"},
    driver_id="fake.device", name="Lab box",
    entities=[{"key": "model"}, {"key": "uptime"}, {"key": "echo_secret"}])
check("create_device returns public record without credentials",
      "credRef" not in dev and "credentials" not in dev)
dev_id = dev["id"]

# ---- 3. credentials are encrypted at rest (no plaintext in the file) ----
with open(os.path.join(store.DATA_DIR, "netmanager.json")) as f:
    raw = f.read()
check("plaintext password absent from stored JSON", "topsecret" not in raw)
doc = json.loads(raw)
check("a credential blob was stored", len(doc["credentials"]) == 1)

# ---- 4. list scoping ----
mine = devices.list_devices("user1")
others = devices.list_devices("user2")
check("owner sees their device", len(mine) == 1)
check("other user does not see it", len(others) == 0)
check("admin sees all", len(devices.list_devices("x", is_admin=True)) == 1)

# ---- 5. live read decrypts creds + returns sensor values ----
state = devices.read_state(dev_id)
check("read_state returns model", state["values"].get("model") == "FZ-1000")
check("read_state returns uptime", state["values"].get("uptime") == 4242)
check("credential decrypted correctly on read",
      state["values"].get("echo_secret") == "topsecret")

# ---- 6. delete removes device and its credential ----
devices.delete_device(dev_id)
doc2 = json.loads(open(os.path.join(store.DATA_DIR, "netmanager.json")).read())
check("device removed", dev_id not in doc2["devices"])
check("orphan credential removed", len(doc2["credentials"]) == 0)

print("\n" + ("ALL PASSED" if not fails else f"{len(fails)} FAILED: {fails}"))
sys.exit(1 if fails else 0)

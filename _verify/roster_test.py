"""Verifies the persistent Access roster (clients kept after they disconnect):
  - a scanned client is tracked online with a connect event;
  - a client absent from a full scan flips offline (after the debounce
    window), gets a disconnect event, and stays listed as an offline entry;
  - a member's partial view never flips clients offline;
  - reconnecting flips it back online with a fresh connect event;
  - client_roster.client_history returns the event log; forgetting erases the record.
"""
import os
import sys
import tempfile

os.environ.setdefault("HLHQ_DATA_DIR", tempfile.mkdtemp(prefix="hlhq-roster-"))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend"))

import transports, devices, client_roster, client_service, store  # noqa: E402
from context import POLLER_CONTEXT  # noqa: E402
from drivers.base import Driver, Entity, SENSOR  # noqa: E402
from drivers.registry import register  # noqa: E402

fails = []
def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)

MAC = "AA:BB:CC:DD:EE:01"

# ---- fake AP that reports a controllable client list ----
SCAN = {"clients": [{"mac": MAC, "ip": "10.0.0.50", "hostname": "phone",
                     "vendor": "Acme", "kind": "wifi", "signal": -55,
                     "where": "ch 36"}]}

class FakeConn(transports.Connection):
    transport = "fake"; host = "10.0.0.2"
    def info(self): return ""
    def close(self): pass

class FakeAP(Driver):
    id = "fake.ap"; display_name = "Fake AP"; transports = ["fake"]
    def probe(self, conn): return 0.9
    def entities(self, conn):
        return [Entity("clients", "Clients", SENSOR, read=lambda: 1)]
    def clients(self, conn):
        return list(SCAN["clients"])

register(FakeAP())
transports.open_connection = (
    lambda transport, host, port=None, credentials=None, timeout=8: FakeConn())

dev = devices.create_device("owner1", "10.0.0.2", "fake", None, {}, "fake.ap", "AP")

# ---- online: first scan tracks the client with a connect event ----
res = client_service.refresh(POLLER_CONTEXT, "owner1")
c = next((x for x in res["clients"] if x["mac"] == MAC), None)
check("scanned client is listed", c is not None)
check("client is online", c and c.get("online") is True)
rec = store.load()["clientRosters"]["owner1"][MAC]
check("roster remembers identity", rec.get("ip") == "10.0.0.50"
      and rec.get("hostname") == "phone" and rec.get("kind") == "wifi")
check("Access snapshot exposes live Wi-Fi RSSI", c and c.get("signal") == -55)
check("connect event recorded", [e["ev"] for e in rec["events"]] == ["up"])
check("event carries the AP name", "AP" in rec["events"][0]["via"])

# ---- absent, but within the debounce window: still online ----
SCAN["clients"] = []
res = client_service.refresh(POLLER_CONTEXT, "owner1")
c = next((x for x in res["clients"] if x["mac"] == MAC), None)
check("absent client still listed", c is not None)
check("still online inside the grace window", c and c.get("online") is True)

# ---- absent past the window, member view: must NOT flip offline ----
def _age(doc):
    doc["clientRosters"]["owner1"][MAC]["lastSeen"] -= client_roster.CLIENT_OFFLINE_AFTER + 1
store.update(_age)
client_roster.record_observations("someone-else", [], full_scan=True)
check("member scan never flips offline",
      store.load()["clientRosters"]["owner1"][MAC].get("online") is True)

# ---- absent past the window, full scan: offline + disconnect event ----
res = client_service.refresh(POLLER_CONTEXT, "owner1")
c = next((x for x in res["clients"] if x["mac"] == MAC), None)
check("offline client stays listed", c is not None)
check("client flipped offline", c and c.get("online") is False)
check("offline entry keeps identity", c and c["ip"] == "10.0.0.50"
      and c["hostname"] == "phone")
check("offline Access entry retains its last Wi-Fi RSSI", c and c.get("signal") == -55)
rec = store.load()["clientRosters"]["owner1"][MAC]
check("disconnect event recorded",
      [e["ev"] for e in rec["events"]] == ["up", "down"])

# ---- reconnect: back online with a fresh connect event ----
SCAN["clients"] = [{"mac": MAC, "ip": "10.0.0.51", "hostname": "phone",
                    "kind": "wifi", "signal": -60}]
res = client_service.refresh(POLLER_CONTEXT, "owner1")
c = next((x for x in res["clients"] if x["mac"] == MAC), None)
check("reconnected client online again", c and c.get("online") is True)
check("roster updated to the new IP",
      store.load()["clientRosters"]["owner1"][MAC]["ip"] == "10.0.0.51")
check("Access snapshot refreshes Wi-Fi RSSI", c and c.get("signal") == -60)
hist = client_roster.client_history("owner1", MAC)
check("history returns the full event log",
      [e["ev"] for e in hist["events"]] == ["up", "down", "up"])
check("history reports online", hist["online"] is True)

# ---- event log is bounded ----
def _flood(doc):
    rec = doc["clientRosters"]["owner1"][MAC]
    for i in range(client_roster.CLIENT_EVENTS_MAX * 2):
        client_roster._event(rec, 1000 + i, "up")
store.update(_flood)
check("event log bounded",
      len(client_roster.client_history("owner1", MAC)["events"]) == client_roster.CLIENT_EVENTS_MAX)

# ---- forget erases the record ----
client_roster.forget("owner1", [MAC])
check("forget removes the roster record",
      MAC not in store.load()["clientRosters"]["owner1"])
SCAN["clients"] = []
res = client_service.refresh(POLLER_CONTEXT, "owner1")
check("forgotten client no longer listed",
      all(x["mac"] != MAC for x in res["clients"]))

print(("\nALL PASS" if not fails else f"\n{len(fails)} FAILURES") +
      f" — roster_test")
sys.exit(1 if fails else 0)

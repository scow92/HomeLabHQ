"""Zyxel WiFi access point (NWA / WAX / NAP series) over its web UI.

These APs have no public REST API — management is the `zysh-cgi` endpoint behind
the web login. You POST a form to log in (a CSRF cookie is handed out on the
first GET), then POST CLI commands to `/cgi-bin/zysh-cgi`; each reply embeds a
`zyshdata0 = [ {...} ];` JavaScript array literal we parse out. This is a
read-only view (writes/deauth need SSH on current firmware), which is all the
detail view needs.

Ported from the private NAC's proven AP integration: the login flow and the
`show ...` command set are the same, wrapped here in the curated-driver shape so
it slots into detection, polling (scalar sensors) and the rich detail() view
(radios + a connected-clients table).
"""
import ast
import re
import time

from netutil import resolve_hostnames as _hostnames  # noqa: F401

from .base import Driver, Entity, SENSOR
from .registry import register

_ZYSHDATA_RE = re.compile(r"zyshdata0\s*=\s*(\[.*?\]);", re.DOTALL)
_SNAP_TTL = 5  # seconds; one detail/poll fans out to ~7 commands, share them


def _parse(text):
    """Extract and eval the zyshdata0 array literal from a zysh-cgi reply."""
    m = _ZYSHDATA_RE.search(text or "")
    if not m:
        return None
    try:
        return ast.literal_eval(m.group(1))
    except Exception:
        return None


def _login(conn):
    """Log into the AP web UI on conn's session. Best-effort: real proof of a
    working session is a subsequent zysh command that parses."""
    conn.get("/")  # hands out the csrftok cookie
    csrf = ""
    try:
        csrf = conn.session.cookies.get("csrftok", "") or ""
    except Exception:
        pass
    try:
        conn.request("POST", "/", allow_redirects=False,
                     headers={"Content-Type": "application/x-www-form-urlencoded"},
                     data={"username": conn.username or "", "pwd": conn.password or "",
                           "password": conn.password or "", "CSRFToken": csrf})
    except Exception:
        pass


def _zysh(conn, cmd):
    """POST a single CLI command and return its parsed zyshdata list (or None)."""
    r = conn.request("POST", "/cgi-bin/zysh-cgi",
                     headers={"Content-Type": "application/x-www-form-urlencoded"},
                     data={"filter": "js2", "cmd": cmd})
    return _parse(r.text)


def _snapshot(conn):
    """Log in (once) and read every field the driver needs in one shot, cached
    briefly on the connection so entities() + detail() don't re-run commands."""
    cached = getattr(conn, "_zyxel_snap", None)
    if cached and (time.time() - cached[0]) < _SNAP_TTL:
        return cached[1]

    _login(conn)

    def first(cmd):
        try:
            d = _zysh(conn, cmd)
            return d[0] if d else {}
        except Exception:
            return {}

    def index(cmd, key):
        try:
            d = _zysh(conn, cmd)
            return (d[0].get(key) if d else None) or []
        except Exception:
            return []

    snap = {
        "version": first("show version"),
        "uptime": first("show system uptime"),
        "cpu": first("show cpu status"),
        "mem": first("show mem status"),
        "clients": first("show wireless-hal station number"),
        "channel": first("show wireless-hal current channel"),
        "stations": index("show wireless-hal station info", "_index"),
    }
    conn._zyxel_snap = (time.time(), snap)
    return snap


_MAC_FULL_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")
_MACFILTER = "nac-block"          # reserved deny-filter profile we manage
_MASK = "FF:FF:FF:FF:FF:FF"       # Zyxel stores each entry as "<mac> <mask>"


def _ssid_profiles(conn):
    """Configured SSID-profile records via 'show wlan-ssid-profile all' (the
    same read NAC uses): [{name, ssid, macfilter}], skipping the reserved
    deny-filter name and the factory 'default'/unconfigured slots."""
    _login(conn)
    data = _zysh(conn, "show wlan-ssid-profile all")
    recs = data[0].get("_ssid_profile", []) if data else []
    out = []
    for p in recs:
        name = (p.get("__name") or "").strip()
        ssid = (p.get("_SSID") or "").strip()
        if not name or name.lower() in (_MACFILTER, "default"):
            continue
        if not ssid or ssid.lower() == "unconfigured":
            continue
        out.append({"name": name, "ssid": ssid,
                    "macfilter": (p.get("_MACFilter_profile") or "").strip()})
    return out


def _deny_macs(conn):
    """Uppercased MACs currently in the nac-block deny filter (empty on error).
    Re-adding an already-present MAC is a silent no-op that never re-kicks the
    client, so the caller removes-then-adds when a MAC is already denied."""
    data = _zysh(conn, "show wlan-macfilter-profile " + _MACFILTER)
    prof = (data[0].get("_macfilter_profile") or [{}])[0] if data else {}
    return {(e.get("_MAC") or "").upper() for e in prof.get("_entry", [])
            if e.get("_MAC")}


def _ap_ssh(host, user, pw, cmds, timeout=20):
    """Run Zyxel CLI lines over one interactive SSH shell and return the output.

    The AP web (zysh-cgi) API is read-only on current firmware — config-mode
    edits only take effect over the SSH CLI — so every state change (macfilter
    edits) goes here. Ported from NAC's proven ap_ssh_run: one shell, each line
    sent after the previous prompt returns (config mode must persist across
    lines, so exec-per-command won't do)."""
    import paramiko
    prompt = r"[>#]\s*$"
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    out = []
    try:
        cli.connect(host, port=22, username=user, password=pw, timeout=timeout,
                    look_for_keys=False, allow_agent=False)
        ch = cli.invoke_shell(width=200, height=4000)

        def _expect(pat, t):
            buf, end = "", time.time() + t
            while time.time() < end:
                if ch.recv_ready():
                    buf += ch.recv(65535).decode("utf-8", "replace")
                    if re.search(pat, buf):
                        return buf
                else:
                    time.sleep(0.15)
            return buf

        _expect(prompt, min(timeout, 8))   # login banner + first prompt
        for c in cmds:
            ch.send(c + "\n")
            out.append(_expect(prompt, timeout))
        return "".join(out)
    finally:
        cli.close()


def _pct(raw):
    """'12 %' / '45%' / 12 -> 12 (int), or None."""
    if raw is None:
        return None
    m = re.search(r"-?\d+", str(raw))
    return int(m.group(0)) if m else None


def _model(snap):
    return (snap["version"].get("_model") or "").strip()


def _fw(snap):
    return (snap["version"].get("_firmware_version") or "").strip()


class ZyxelAP(Driver):
    id = "zyxel.ap"
    display_name = "Zyxel WiFi access point (web UI)"
    transports = ["http"]

    def probe(self, conn) -> float:
        try:
            snap = _snapshot(conn)
        except Exception:
            return 0.0
        # Only Zyxel's zysh-cgi emits the zyshdata0 literal we just parsed, so a
        # parse of any command is already a strong fingerprint.
        parsed_anything = any(snap[k] for k in ("version", "cpu", "channel")) \
            or bool(snap["stations"])
        if not parsed_anything:
            return 0.0
        return 0.9 if _model(snap) else 0.7

    def entities(self, conn):
        def snap():
            return _snapshot(conn)

        def model():
            return _model(snap()) or None

        def firmware():
            return _fw(snap()) or None

        def uptime():
            return (snap()["uptime"].get("_system_uptime") or "").strip() or None

        def cpu():
            return _pct(snap()["cpu"].get("_CPU_utilization"))

        def mem():
            return _pct(snap()["mem"].get("_memory_usage"))

        def clients_24():
            return _pct(snap()["clients"].get("_Slot1"))

        def clients_5():
            return _pct(snap()["clients"].get("_Slot2"))

        def clients_total():
            a, b = clients_24(), clients_5()
            if a is None and b is None:
                return None
            return (a or 0) + (b or 0)

        def channel_24():
            return snap()["channel"].get("_Slot1") or None

        def channel_5():
            return snap()["channel"].get("_Slot2") or None

        return [
            Entity("model", "Model", SENSOR, read=model),
            Entity("firmware", "Firmware", SENSOR, read=firmware),
            Entity("uptime", "Uptime", SENSOR, read=uptime),
            Entity("cpu", "CPU", SENSOR, unit="%", read=cpu),
            Entity("mem", "Memory", SENSOR, unit="%", read=mem),
            Entity("clients", "Clients", SENSOR, read=clients_total),
            Entity("clients_24", "Clients 2.4 GHz", SENSOR, read=clients_24),
            Entity("clients_5", "Clients 5 GHz", SENSOR, read=clients_5),
            Entity("channel_24", "Channel 2.4 GHz", SENSOR, read=channel_24),
            Entity("channel_5", "Channel 5 GHz", SENSOR, read=channel_5),
        ]

    def clients(self, conn):
        snap = _snapshot(conn)
        stations = snap["stations"]
        hosts = _hostnames([st.get("_IPv4") for st in stations])
        out = []
        for st in stations:
            ip = (st.get("_IPv4") or "").strip()
            band = st.get("_Band") or ""
            ssid = st.get("_SSID") or ""
            where = " · ".join(x for x in (band, ssid) if x)
            rssi = st.get("_RSSI_dBm")
            try:
                rssi = int(rssi) if rssi not in (None, "") else None
            except Exception:
                rssi = None
            out.append({
                "mac": (st.get("_MAC") or "").upper(),
                "ip": ip, "hostname": hosts.get(ip, ""),
                "kind": "wifi", "signal": rssi, "where": where,
            })
        return out

    def actions(self):
        # Device-level actions (buttons). force_roam is a per-client row action
        # surfaced on the clients table, so it's not listed here.
        return [{"name": "reboot", "label": "Reboot AP", "danger": True,
                 "confirm": True}]

    def run_action(self, conn, name, args):
        if name == "force_roam":
            return self._force_roam(conn, (args or {}).get("mac", ""))
        if name == "reboot":
            return self._reboot(conn)
        raise ValueError(f"unsupported action: {name}")

    def _reboot(self, conn):
        """Reboot the AP over the SSH CLI (the web API is read-only)."""
        if not conn.password:
            raise ValueError("AP password required to reboot (SSH)")
        _ap_ssh(conn.host, conn.username or "admin", conn.password,
                ["reboot"], timeout=15)
        return {"ok": True, "message": "Reboot command sent to the AP."}

    def _force_roam(self, conn, mac):
        """Kick a client off this AP so it re-associates elsewhere — the same
        deny-filter trick NAC uses. Adding the MAC to an active deny macfilter
        (bound to every SSID) kicks it immediately and blocks re-association
        here, so it roams to another AP; a background timer lifts the block
        after 60s so the device can return later."""
        import threading
        mac = (mac or "").strip().lower()
        if not _MAC_FULL_RE.match(mac):
            raise ValueError("invalid MAC address")
        if not conn.password:
            raise ValueError("AP password required for force-roam (SSH)")
        host, user, pw = conn.host, conn.username or "admin", conn.password
        entry = mac + " " + _MASK

        profiles = _ssid_profiles(conn)
        ssids = [p["name"] for p in profiles] or \
            ["Wiz_SSID_1", "Wiz_SSID_2", "Wiz_SSID_3", "Wiz_SSID_4"]
        bound = sum(1 for p in profiles if p.get("macfilter") == _MACFILTER)
        present = mac.upper() in _deny_macs(conn)
        steps = []

        # 1) First-time setup: create the deny filter and bind it to every SSID
        #    profile (persisted with 'write'). Skipped once already bound.
        if bound < len(ssids):
            setup = ["configure terminal",
                     "wlan-macfilter-profile " + _MACFILTER,
                     "filter-action deny", "exit"]
            for s in ssids:
                setup += ["wlan-ssid-profile " + s, "macfilter " + _MACFILTER, "exit"]
            setup += ["exit", "write"]
            _ap_ssh(host, user, pw, setup)
            steps.append(f"bound deny filter to {len(ssids)} SSID profile(s)")

        # 2) If already denied, remove first so the re-add is a fresh re-kick.
        if present:
            _ap_ssh(host, user, pw, ["configure terminal",
                    "wlan-macfilter-profile " + _MACFILTER, "no " + entry,
                    "exit", "exit"])
            steps.append("cleared stale entry")
            time.sleep(3)

        # 3) Add the MAC to the deny filter -> AP kicks it now and denies
        #    re-association here -> it roams to another AP.
        _ap_ssh(host, user, pw, ["configure terminal",
                "wlan-macfilter-profile " + _MACFILTER, "filter-action deny",
                entry, "exit", "exit"])
        steps.append("added deny entry — client kicked")

        # 4) Lift the block after a while so the device can return. Per-MAC
        #    edits aren't 'write'-persisted, so this self-heals on reboot too.
        def _unblock():
            time.sleep(60)
            try:
                _ap_ssh(host, user, pw, ["configure terminal",
                        "wlan-macfilter-profile " + _MACFILTER, "no " + entry,
                        "exit", "exit"])
            except Exception:
                pass
        threading.Thread(target=_unblock, daemon=True).start()

        return {"ok": True, "mac": mac,
                "message": "Forced roam for " + mac + ": " + "; ".join(steps),
                "steps": steps, "unblockAfter": 60}

    def detail(self, conn) -> dict:
        # Overview fields (model/firmware/uptime/cpu/mem/clients/channels) are
        # exposed as entities, so the detail view renders them from there and
        # lets the user customize which show. detail() only adds the structured
        # tables entities can't express: the per-radio and per-client breakdowns.
        snap = _snapshot(conn)
        c24 = _pct(snap["clients"].get("_Slot1"))
        c5 = _pct(snap["clients"].get("_Slot2"))
        ch24 = snap["channel"].get("_Slot1") or "–"
        ch5 = snap["channel"].get("_Slot2") or "–"

        radios = {
            "title": "Radios",
            "columns": [
                {"key": "band", "label": "Band"},
                {"key": "channel", "label": "Channel"},
                {"key": "clients", "label": "Clients"},
            ],
            "rows": [
                {"band": "2.4 GHz", "channel": ch24, "clients": c24 or 0},
                {"band": "5 GHz", "channel": ch5, "clients": c5 or 0},
            ],
        }

        rows = []
        hosts = _hostnames([st.get("_IPv4") for st in snap["stations"]])
        for st in snap["stations"]:
            mac = (st.get("_MAC") or "").upper()
            ip = (st.get("_IPv4") or "").strip()
            host = hosts.get(ip, "")
            rows.append({
                # Identify a client by the friendliest available handle:
                # hostname first, then IP, then MAC (per user preference).
                "client": host or ip or mac or "–",
                "ip": ip or "–",
                "mac": mac or "–",
                "band": st.get("_Band") or "",
                "ssid": st.get("_SSID") or "",
                "phy": st.get("_Capability") or "",
                "rssi": st.get("_RSSI_dBm"),
                "tx": st.get("_TxRate") or "",
                "rx": st.get("_RxRate") or "",
            })
        clients = {
            "title": f"Connected clients ({len(rows)})",
            "columns": [
                {"key": "client", "label": "Client"},
                {"key": "ip", "label": "IP"},
                {"key": "mac", "label": "MAC"},
                {"key": "band", "label": "Band"},
                {"key": "ssid", "label": "SSID"},
                {"key": "phy", "label": "PHY"},
                {"key": "rssi", "label": "Signal", "unit": "dBm"},
                {"key": "tx", "label": "Tx"},
                {"key": "rx", "label": "Rx"},
            ],
            "rows": rows,
            # Per-row action: kick a client so it re-associates to another AP.
            "rowActions": [{"action": "force_roam", "label": "Force roam",
                            "argKey": "mac", "confirm": True}],
        }
        return {"tables": [radios, clients]}


register(ZyxelAP())

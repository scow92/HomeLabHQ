"""Owner-scoped NordVPN endpoint discovery, health and safe switching."""
from __future__ import annotations

import hashlib
import ipaddress
import re
import time
from typing import Any

import devices
import logbuf
import store
from nordvpn_client import NordVPNClient, NordVPNError
from rdap_client import RDAPClient


MAX_HISTORY = 100
MIN_DISCOVERY_SECONDS = 300
MAX_DISCOVERY_SECONDS = 7 * 24 * 60 * 60
DEFAULT_PROFILE = {
    "enabled": False, "country": "United Kingdom", "city": "London", "maxCandidates": 20,
    "preferredOwners": ["PacketHub", "Packethub"],
    "rejectedOwners": ["Hydra Communications", "Hydra"], "handshakeWarningSeconds": 300,
    "discoveryIntervalSeconds": 3600, "includeUnknownOwners": False,
    "peerUuid": "", "instanceUuid": "", "gatewayUuid": "", "notes": "",
}

_nord = NordVPNClient()
_rdap = RDAPClient()


def _fingerprint(key: str) -> str:
    return "SHA256:" + hashlib.sha256(key.encode("ascii", "ignore")).hexdigest()[:20]


def _bounded_text(value: object, length=160) -> str:
    return str(value or "").strip()[:length]


def _patterns(value: object, defaults: list[str]) -> list[str]:
    if not isinstance(value, list):
        return list(defaults)
    return [x for x in (_bounded_text(item, 80) for item in value) if x][:20]


def _profile(value: object) -> dict[str, Any]:
    result = dict(DEFAULT_PROFILE)
    if isinstance(value, dict):
        result.update({key: value[key] for key in result if key in value})
    result["enabled"] = bool(result["enabled"])
    result["country"] = _bounded_text(result["country"], 80) or DEFAULT_PROFILE["country"]
    result["city"] = _bounded_text(result["city"], 80)
    try:
        result["maxCandidates"] = min(50, max(1, int(result["maxCandidates"])))
        result["handshakeWarningSeconds"] = min(86400, max(60, int(result["handshakeWarningSeconds"])))
        result["discoveryIntervalSeconds"] = min(MAX_DISCOVERY_SECONDS, max(
            MIN_DISCOVERY_SECONDS, int(result["discoveryIntervalSeconds"])))
    except (TypeError, ValueError):
        result["maxCandidates"] = DEFAULT_PROFILE["maxCandidates"]
        result["handshakeWarningSeconds"] = DEFAULT_PROFILE["handshakeWarningSeconds"]
        result["discoveryIntervalSeconds"] = DEFAULT_PROFILE["discoveryIntervalSeconds"]
    result["preferredOwners"] = _patterns(result["preferredOwners"], DEFAULT_PROFILE["preferredOwners"])
    result["rejectedOwners"] = _patterns(result["rejectedOwners"], DEFAULT_PROFILE["rejectedOwners"])
    result["includeUnknownOwners"] = bool(result["includeUnknownOwners"])
    for key in ("peerUuid", "instanceUuid", "gatewayUuid"):
        result[key] = _bounded_text(result[key], 80)
    result["notes"] = _bounded_text(result["notes"], 500)
    return result


def _match(owner: str, patterns: list[str]) -> bool:
    """Match whole normalised words, never arbitrary substrings."""
    words = " ".join(re.findall(r"[a-z0-9]+", owner.casefold()))
    return any(re.search(r"(?<![a-z0-9])" + re.escape(" ".join(re.findall(r"[a-z0-9]+", p.casefold()))) +
                         r"(?![a-z0-9])", words) is not None
               for p in patterns if re.findall(r"[a-z0-9]+", p.casefold()))


def _classification(owner: str, profile: dict[str, Any]) -> str:
    if not owner:
        return "Unknown"
    if _match(owner, profile["rejectedOwners"]):
        return "Rejected"
    if _match(owner, profile["preferredOwners"]):
        return "Preferred"
    return "Unknown"


def _candidate_id(candidate: dict[str, Any]) -> str:
    return hashlib.sha256((candidate["endpointIp"] + "\0" + candidate["publicKey"]).encode()).hexdigest()[:24]


def _history(doc: dict, owner_id: str, device_id: str) -> dict:
    return doc.setdefault("vpnEndpointHistory", {}).setdefault(owner_id, {}).setdefault(device_id, {
        "candidates": [], "events": [], "lastCandidates": [], "state": {}, "lastDiscovery": None,
    })


def _public_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in candidate.items() if key != "publicKey"}


def configure(owner_id: str, device_id: str, data: object) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("profile must be an object")
    profile = _profile(data)
    dev = devices.get_device(device_id)
    if not dev or dev.get("ownerId") != owner_id:
        raise ValueError("device not found")
    if dev.get("driverId") != "opnsense.firewall":
        raise ValueError("VPN endpoint management requires an OPNsense device")
    if profile["enabled"] and (not profile["peerUuid"] or not profile["instanceUuid"]):
        raise ValueError("select a WireGuard instance and peer before enabling endpoint management")

    def mutate(doc):
        device = doc["devices"].get(device_id)
        if not device or device.get("ownerId") != owner_id:
            raise ValueError("device not found")
        device["vpnEndpointProfile"] = profile
        _history(doc, owner_id, device_id)
    store.update(mutate)
    return profile


def choices(device_id: str) -> dict[str, Any]:
    with devices.device_conn(device_id, timeout=12) as (_, driver, conn):
        if not hasattr(driver, "wireguard_peers"):
            raise ValueError("device does not support OPNsense WireGuard")
        return {"peers": driver.wireguard_peers(conn), "instances": driver.wireguard_instances(conn)}


def _score(candidate: dict[str, Any], profile: dict[str, Any]) -> tuple:
    return (0 if candidate["classification"] == "Preferred" else 1,
            0 if profile["city"] and candidate.get("city", "").casefold() == profile["city"].casefold() else 1,
            0 if candidate.get("publicKey") else 1,
            candidate.get("load") if isinstance(candidate.get("load"), (int, float)) else 10001,
            0 if candidate.get("lastVerification") == "success" else 1,
            candidate["hostname"].casefold(), candidate["endpointIp"])


def _save_discovery(owner_id: str, device_id: str, profile: dict, discovered: list[dict]) -> None:
    now = int(time.time())
    def mutate(doc):
        record = _history(doc, owner_id, device_id)
        existing = {x.get("candidateId"): x for x in record["candidates"]}
        seen = {candidate["candidateId"] for candidate in discovered}
        for old in record["candidates"]:
            if old.get("candidateId") not in seen:
                old["classification"] = "Stale"
        for candidate in discovered:
            ident = candidate["candidateId"]
            previous = existing.get(ident)
            if previous and previous.get("lastVerification"):
                candidate["lastVerification"] = previous["lastVerification"]
            entry = {"candidateId": ident, "endpointIp": candidate["endpointIp"],
                     "hostname": candidate["hostname"], "publicKeyFingerprint": _fingerprint(candidate["publicKey"]),
                     "owner": candidate.get("owner", ""), "asn": candidate.get("asn"),
                     "load": candidate.get("load"), "firstSeen": previous.get("firstSeen", now) if previous else now,
                     "lastSeen": now, "classification": candidate["classification"],
                     "compatibility": previous.get("compatibility", "Unknown") if previous else "Unknown"}
            if previous:
                previous.update(entry)
            else:
                record["candidates"].append(entry)
        record["candidates"].sort(key=lambda x: x.get("lastSeen", 0), reverse=True)
        del record["candidates"][MAX_HISTORY:]
        # The current bounded discovery set keeps public keys only long enough
        # to perform a confirmed switch; durable history stores fingerprints.
        record["lastCandidates"] = discovered[:50]
        record["lastDiscovery"] = now
    store.update(mutate)


def discover(device_id: str, *, force=False) -> dict[str, Any]:
    dev = devices.get_device(device_id)
    if not dev:
        raise ValueError("device not found")
    profile = _profile(dev.get("vpnEndpointProfile"))
    record = _history(store.load(), dev["ownerId"], device_id)
    now = int(time.time())
    if not force and record.get("lastDiscovery") and now - record["lastDiscovery"] < profile["discoveryIntervalSeconds"]:
        return {"status": "cached", "candidates": [_public_candidate(x) for x in record.get("lastCandidates", [])]}
    try:
        raw = _nord.discover(profile["country"], profile["maxCandidates"])
    except NordVPNError as error:
        return {"status": "error", "error": str(error), "candidates": [_public_candidate(x) for x in record.get("lastCandidates", [])]}
    candidates = []
    for candidate in raw:  # deliberately serial: at most MAX_LIMIT bounded RDAP HTTP requests through cache
        ownership = _rdap.lookup(candidate.endpoint_ip)
        owner = ownership.owner
        item = {"hostname": candidate.hostname, "endpointIp": candidate.endpoint_ip,
                "endpointPort": candidate.endpoint_port, "country": candidate.country,
                "city": candidate.city, "load": candidate.load, "publicKey": candidate.public_key,
                "publicKeyFingerprint": _fingerprint(candidate.public_key), "discoveredAt": candidate.discovered_at,
                "owner": owner, "asn": ownership.asn, "asnName": ownership.asn_name,
                "organisation": ownership.organisation, "cidr": ownership.cidr,
                "lookupSource": ownership.source, "lookupAt": ownership.looked_up_at,
                "lookupStatus": ownership.status, "classification": _classification(owner, profile)}
        item["candidateId"] = _candidate_id(item)
        candidates.append(item)
    candidates.sort(key=lambda item: _score(item, profile))
    _save_discovery(dev["ownerId"], device_id, profile, candidates)
    return {"status": "ok", "candidates": [_public_candidate(x) for x in candidates]}


def _runtime(device_id: str, profile: dict) -> dict[str, Any]:
    if not profile["peerUuid"]:
        return {"configured": False}
    with devices.device_conn(device_id, timeout=12) as (_, driver, conn):
        peer = driver.wireguard_peer(conn, profile["peerUuid"])
        status = driver.wireguard_status(conn, peer)
        gateway = driver.gateway(conn, profile["gatewayUuid"]) if profile["gatewayUuid"] else None
    endpoint = _bounded_text(peer.get("serveraddress"), 200)
    try:
        endpoint = str(ipaddress.ip_address(endpoint))
    except ValueError:
        pass
    return {"configured": True, "endpointIp": endpoint, "endpointPort": peer.get("serverport") or 51820,
            "publicKeyFingerprint": _fingerprint(str(peer.get("pubkey") or "")), "peerUuid": profile["peerUuid"],
            "instanceUuid": profile["instanceUuid"], "gatewayUuid": profile["gatewayUuid"], "status": status,
            "gateway": {"name": gateway.get("name"), "status": gateway.get("status")} if gateway else None}


def status(owner_id: str, device_id: str, *, refresh=False) -> dict[str, Any]:
    dev = devices.get_device(device_id)
    if not dev or dev.get("ownerId") != owner_id:
        raise ValueError("device not found")
    profile = _profile(dev.get("vpnEndpointProfile"))
    if refresh:
        discovery = discover(device_id, force=True)
    else:
        record = _history(store.load(), owner_id, device_id)
        discovery = {"status": "cached", "candidates": [_public_candidate(x) for x in record.get("lastCandidates", [])]}
    try:
        current = _runtime(device_id, profile)
    except Exception:
        current = {"configured": bool(profile["peerUuid"]), "error": "Could not read WireGuard runtime status"}
    record = _history(store.load(), owner_id, device_id)
    compatible = {x.get("candidateId"): x.get("compatibility", "Unknown") for x in record.get("candidates", [])}
    candidates = []
    for item in discovery["candidates"]:
        item = dict(item)
        item["compatibility"] = compatible.get(item.get("candidateId"), "Unknown")
        item["active"] = current.get("endpointIp") == item.get("endpointIp")
        if item["active"]:
            current.update({"hostname": item.get("hostname", ""), "owner": item.get("owner", ""),
                            "classification": "Active", "appearsInDiscovery": True})
        candidates.append(item)
    if current.get("configured") and "appearsInDiscovery" not in current:
        current.update({"appearsInDiscovery": False, "classification": "Stale"})
    return {"profile": profile, "discovery": {**discovery, "candidates": candidates}, "current": current,
            "history": [{k: v for k, v in x.items() if k != "publicKey"} for x in record.get("candidates", [])]}


def set_compatibility(owner_id: str, device_id: str, candidate_id: str, state: str, note: str = "") -> None:
    if state not in {"Verified", "Failed", "Assumed from provider", "Unknown"}:
        raise ValueError("invalid compatibility state")
    def mutate(doc):
        record = _history(doc, owner_id, device_id)
        for candidate in record["candidates"]:
            if candidate.get("candidateId") == candidate_id:
                candidate.update({"compatibility": state, "compatibilityAt": int(time.time()),
                                  "compatibilityNote": _bounded_text(note, 500)})
                return
        raise ValueError("candidate not found")
    store.update(mutate)


def switch(owner_id: str, device_id: str, candidate_id: str, confirmed: bool) -> dict[str, Any]:
    if not confirmed:
        raise ValueError("explicit confirmation is required before switching")
    dev = devices.get_device(device_id)
    if not dev or dev.get("ownerId") != owner_id:
        raise ValueError("device not found")
    profile = _profile(dev.get("vpnEndpointProfile"))
    if not profile["enabled"] or not profile["peerUuid"]:
        raise ValueError("endpoint management is not configured and enabled")
    record = _history(store.load(), owner_id, device_id)
    candidate = next((x for x in record.get("lastCandidates", []) if x.get("candidateId") == candidate_id), None)
    if not candidate or candidate.get("classification") != "Preferred":
        raise ValueError("only a current preferred candidate can be switched to")
    try:
        ipaddress.ip_address(candidate["endpointIp"])
        if not 1 <= int(candidate["endpointPort"]) <= 65535 or not candidate.get("publicKey"):
            raise ValueError
    except (ValueError, TypeError, KeyError):
        raise ValueError("replacement candidate is malformed") from None
    start = int(time.time())
    changed_gateway = False
    rollback_ok = None
    before = None
    gateway_before = None
    try:
        with devices.device_conn(device_id, timeout=20) as (_, driver, conn):
            before = driver.wireguard_peer(conn, profile["peerUuid"])
            peer_instances = {value.strip() for value in str(before.get("servers") or "").split(",") if value.strip()}
            if peer_instances and profile["instanceUuid"] not in peer_instances:
                raise ValueError("selected WireGuard instance is not associated with the selected peer")
            gateway_before = driver.gateway(conn, profile["gatewayUuid"]) if profile["gatewayUuid"] else None
            replacement = dict(before)
            replacement.update({"serveraddress": candidate["endpointIp"], "serverport": str(candidate["endpointPort"]),
                                "pubkey": candidate["publicKey"]})
            gateway_after = dict(gateway_before) if gateway_before else None
            # A gateway is only altered where its configured address/monitor is
            # exactly the old endpoint.  Tunnel-address gateways are untouched.
            if gateway_after:
                for field in ("gateway", "monitor"):
                    if gateway_after.get(field) == before.get("serveraddress"):
                        gateway_after[field] = candidate["endpointIp"]
                        changed_gateway = True
            driver.wireguard_update_peer(conn, profile["peerUuid"], replacement)
            if changed_gateway:
                driver.gateway_update(conn, profile["gatewayUuid"], gateway_after)
            driver.wireguard_reconfigure(conn)
            verified = False
            for _ in range(6):
                live = driver.wireguard_status(conn, replacement)
                handshake = live.get("latestHandshake")
                if handshake and int(handshake) >= start:
                    verified = True
                    break
                time.sleep(2)
            if not verified:
                raise RuntimeError("no recent authenticated WireGuard handshake after switch")
        result = {"ok": True, "rollback": None, "changedGateway": changed_gateway,
                  "message": "Endpoint switched and authenticated handshake verified."}
    except Exception:
        try:
            with devices.device_conn(device_id, timeout=20) as (_, driver, conn):
                if before is None:
                    raise RuntimeError("no peer snapshot available for rollback")
                driver.wireguard_update_peer(conn, profile["peerUuid"], before)
                if changed_gateway and gateway_before is not None:
                    driver.gateway_update(conn, profile["gatewayUuid"], gateway_before)
                driver.wireguard_reconfigure(conn)
                restored = driver.wireguard_status(conn, before)
                rollback_ok = bool(restored.get("latestHandshake"))
        except Exception:
            rollback_ok = False
        result = {"ok": False, "rollback": rollback_ok, "changedGateway": changed_gateway,
                  "message": "Switch verification failed; rollback was attempted."}
    def audit(doc):
        rec = _history(doc, owner_id, device_id)
        for saved in rec["candidates"]:
            if saved.get("candidateId") == candidate_id:
                saved["lastVerification"] = "success" if result["ok"] else "failed"
                saved["lastVerifiedAt"] = int(time.time())
                break
        rec["events"].append({"at": int(time.time()), "type": "switch", "candidateId": candidate_id,
                              "result": "success" if result["ok"] else "failed", "rollback": result["rollback"]})
        del rec["events"][:-MAX_HISTORY]
    store.update(audit)
    return result


def poll_enabled() -> None:
    """Run one bounded background health pass; failures never stop polling."""
    doc = store.load()
    for device_id, dev in doc.get("devices", {}).items():
        profile = _profile(dev.get("vpnEndpointProfile"))
        if not profile["enabled"] or dev.get("driverId") != "opnsense.firewall":
            continue
        try:
            discovery = discover(device_id, force=False)
            outcome = status(dev.get("ownerId"), device_id)
            current = outcome.get("current", {})
            candidates = outcome.get("discovery", {}).get("candidates", [])
            active = next((x for x in candidates if x.get("active")), None)
            conditions = []
            age = (current.get("status") or {}).get("handshakeAge")
            if isinstance(age, (int, float)) and age > profile["handshakeWarningSeconds"]:
                conditions.append("stale_handshake")
            if discovery.get("status") == "ok" and current.get("configured") and not active:
                conditions.append("endpoint_missing")
            if discovery.get("status") == "ok" and active and active.get("classification") in {"Rejected", "Unknown"}:
                conditions.append("owner_not_preferred")
            if discovery.get("status") == "ok" and not any(x.get("classification") == "Preferred" for x in candidates):
                conditions.append("no_preferred_candidate")
            _update_alert_state(dev, conditions)
        except Exception:
            # A management-plane error is already covered by the ordinary
            # device poller; endpoint alerts must not manufacture a false
            # tunnel conclusion from an unavailable firewall API.
            continue


def _update_alert_state(device: dict, conditions: list[str]) -> None:
    now = int(time.time())
    emitted: list[str] = []
    def mutate(doc):
        rec = _history(doc, device["ownerId"], device["id"])
        state = rec.setdefault("state", {}).setdefault("alerts", {})
        for name in set(state) | set(conditions):
            item = state.setdefault(name, {"count": 0, "sent": False})
            if name in conditions:
                item["count"] = int(item.get("count", 0)) + 1
                if item["count"] >= 2 and not item.get("sent"):
                    item["sent"] = True
                    emitted.append(name)
            else:
                state.pop(name, None)
    store.update(mutate)
    for condition in emitted:
        logbuf.log_event("warn", "vpn_endpoint_alert", source="vpn-endpoints", device_id=device["id"],
                         condition=condition)
        try:
            import push
            push.notify(push.recipients_for_device(device), "VPN endpoint needs attention",
                        f"{device.get('name') or device.get('host')}: {condition.replace('_', ' ')}.",
                        data={"deviceId": device["id"], "type": "vpn_endpoint", "condition": condition})
        except Exception:
            pass

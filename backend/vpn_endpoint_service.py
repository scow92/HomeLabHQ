"""Owner-scoped NordVPN endpoint discovery, health and safe switching."""
from __future__ import annotations

import hashlib
import ipaddress
import re
import time
import uuid
from typing import Any

import devices
import logbuf
import store
from nordvpn_client import NordVPNClient, NordVPNError
from rdap_client import RDAPClient


MAX_HISTORY = 100
MIN_DISCOVERY_SECONDS = 300
MAX_DISCOVERY_SECONDS = 7 * 24 * 60 * 60
VALIDATION_STATES = {"Verified", "Failed", "Assumed", "Unknown"}
IMPORTED_TARGET_ID = "imported-compatibility-check"
DEFAULT_PROFILE = {
    "enabled": False, "country": "United Kingdom", "city": "London", "maxCandidates": 20,
    "preferredOwners": [], "excludedOwners": [], "compatibilityTargets": [],
    "handshakeWarningSeconds": 300,
    "discoveryIntervalSeconds": 3600, "includeUnknownOwners": False,
    "peerUuid": "", "instanceUuid": "", "gatewayUuid": "", "notes": "",
}

_nord = NordVPNClient()
_rdap = RDAPClient()


def _fingerprint(key: str) -> str:
    return "SHA256:" + hashlib.sha256(key.encode("ascii", "ignore")).hexdigest()[:20]


def _bounded_text(value: object, length=160) -> str:
    return str(value or "").strip()[:length]


def _patterns(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [x for x in (_bounded_text(item, 80) for item in value) if x][:20]


def _validation_state(value: object) -> str:
    if value == "Assumed from provider":
        return "Assumed"
    return str(value) if value in VALIDATION_STATES else "Unknown"


def _target(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    target_id = _bounded_text(value.get("id"), 80)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", target_id):
        target_id = uuid.uuid4().hex
    name = _bounded_text(value.get("name"), 120)
    if not name:
        return None
    return {
        "id": target_id,
        "name": name,
        "description": _bounded_text(value.get("description"), 500),
        "state": "Unknown",
        "lastValidatedAt": None,
        "note": "",
    }


def _targets(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result, seen = [], set()
    for raw in value[:20]:
        target = _target(raw)
        if target and target["id"] not in seen:
            seen.add(target["id"])
            result.append(target)
    return result


def _profile(value: object) -> dict[str, Any]:
    result = dict(DEFAULT_PROFILE)
    if isinstance(value, dict):
        result.update({key: value[key] for key in result if key in value})
        if "excludedOwners" not in value and "rejectedOwners" in value:
            result["excludedOwners"] = value["rejectedOwners"]
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
    result["preferredOwners"] = _patterns(result["preferredOwners"])
    result["excludedOwners"] = _patterns(result["excludedOwners"])
    result["compatibilityTargets"] = _targets(result["compatibilityTargets"])
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
    if _match(owner, profile["excludedOwners"]):
        return "Excluded"
    if _match(owner, profile["preferredOwners"]):
        return "Preferred"
    return "Eligible"


def _normalise_classification(value: object) -> str:
    return "Excluded" if value == "Rejected" else str(value or "Unknown")


def _candidate_id(candidate: dict[str, Any]) -> str:
    return hashlib.sha256((candidate["endpointIp"] + "\0" + candidate["publicKey"]).encode()).hexdigest()[:24]


def _history(doc: dict, owner_id: str, device_id: str) -> dict:
    return doc.setdefault("vpnEndpointHistory", {}).setdefault(owner_id, {}).setdefault(device_id, {
        "candidates": [], "events": [], "lastCandidates": [], "state": {}, "lastDiscovery": None,
    })


def _has_legacy_validation(candidate: dict[str, Any]) -> bool:
    state = _validation_state(candidate.get("compatibility"))
    return (state != "Unknown" or bool(_bounded_text(candidate.get("compatibilityNote"), 500))
            or isinstance(candidate.get("compatibilityAt"), (int, float)))


def _normalise_record(record: dict[str, Any], profile: dict[str, Any]) -> None:
    """Read legacy branch data without manufacturing a validation target.

    The compatibility shim remains in place for schema-v2 documents through
    the next document-schema migration. Any subsequent profile or discovery
    write persists only the neutral target/validation shape.
    """
    imported = False
    for collection in (record.get("candidates", []), record.get("lastCandidates", [])):
        for candidate in collection if isinstance(collection, list) else []:
            if not isinstance(candidate, dict):
                continue
            candidate["classification"] = _normalise_classification(
                candidate.get("classification"))
            validations = candidate.get("validations")
            if not isinstance(validations, dict):
                validations = {}
                candidate["validations"] = validations
            if _has_legacy_validation(candidate):
                validations.setdefault(IMPORTED_TARGET_ID, {
                    "state": _validation_state(candidate.get("compatibility")),
                    "lastValidatedAt": candidate.get("compatibilityAt")
                    if isinstance(candidate.get("compatibilityAt"), (int, float)) else None,
                    "note": _bounded_text(candidate.get("compatibilityNote"), 500),
                })
                imported = True
            candidate.pop("compatibility", None)
            candidate.pop("compatibilityAt", None)
            candidate.pop("compatibilityNote", None)
    target_ids = {target["id"] for target in profile["compatibilityTargets"]}
    if imported and IMPORTED_TARGET_ID not in target_ids:
        profile["compatibilityTargets"].append({
            "id": IMPORTED_TARGET_ID,
            "name": "Imported compatibility check",
            "description": "Imported from compatibility data saved by an earlier version.",
            "state": "Unknown",
            "lastValidatedAt": None,
            "note": "",
        })


def _candidate_targets(candidate: dict[str, Any], profile: dict[str, Any]) -> list[dict[str, Any]]:
    validations = candidate.get("validations")
    validations = validations if isinstance(validations, dict) else {}
    result = []
    for definition in profile["compatibilityTargets"]:
        saved = validations.get(definition["id"])
        saved = saved if isinstance(saved, dict) else {}
        result.append({
            **definition,
            "state": _validation_state(saved.get("state")),
            "lastValidatedAt": saved.get("lastValidatedAt")
            if isinstance(saved.get("lastValidatedAt"), (int, float)) else None,
            "note": _bounded_text(saved.get("note"), 500),
        })
    return result


def _public_candidate(candidate: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    result = {key: value for key, value in candidate.items()
              if key not in {"publicKey", "validations"}}
    result["classification"] = _normalise_classification(result.get("classification"))
    result["compatibilityTargets"] = _candidate_targets(candidate, profile)
    if result.get("lastVerification") == "failed":
        result["runtimeClassification"] = "Unhealthy"
    elif result.get("active"):
        result["runtimeClassification"] = "Active"
    return result


def _validate_profile_patch(data: dict[str, Any]) -> None:
    numeric = {
        "maxCandidates": (1, 50),
        "handshakeWarningSeconds": (60, 86400),
        "discoveryIntervalSeconds": (MIN_DISCOVERY_SECONDS, MAX_DISCOVERY_SECONDS),
    }
    for key, (minimum, maximum) in numeric.items():
        if key in data and (type(data[key]) is not int or not minimum <= data[key] <= maximum):
            raise ValueError(f"{key} must be an integer from {minimum} to {maximum}")
    for key in ("preferredOwners", "excludedOwners", "rejectedOwners"):
        if key in data and (not isinstance(data[key], list)
                            or any(not isinstance(item, str) for item in data[key])):
            raise ValueError(f"{key} must be a list of strings")
    if "compatibilityTargets" in data:
        raw = data["compatibilityTargets"]
        if not isinstance(raw, list) or len(raw) > 20:
            raise ValueError("compatibilityTargets must be a list with at most 20 items")
        normalised = [_target(item) for item in raw]
        if any(item is None for item in normalised):
            raise ValueError("each compatibility target requires a name")
        ids = [item["id"] for item in normalised if item]
        if len(ids) != len(set(ids)):
            raise ValueError("compatibility target IDs must be unique")


def configure(owner_id: str, device_id: str, data: object) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("profile must be an object")
    _validate_profile_patch(data)
    dev = devices.get_device(device_id)
    if not dev or dev.get("ownerId") != owner_id:
        raise ValueError("device not found")
    if dev.get("driverId") != "opnsense.firewall":
        raise ValueError("VPN endpoint management requires an OPNsense device")
    profile = _profile(dev.get("vpnEndpointProfile"))
    record = _history(store.load(), owner_id, device_id)
    _normalise_record(record, profile)
    previous_target_ids = {item["id"] for item in profile["compatibilityTargets"]}
    patch = dict(data)
    if "excludedOwners" not in patch and "rejectedOwners" in patch:
        patch["excludedOwners"] = patch["rejectedOwners"]
    patch.pop("rejectedOwners", None)
    merged = dict(profile)
    merged.update({key: value for key, value in patch.items() if key in DEFAULT_PROFILE})
    profile = _profile(merged)
    new_target_ids = {item["id"] for item in profile["compatibilityTargets"]}
    removed_target_ids = previous_target_ids - new_target_ids
    has_removed_history = any(
        isinstance(candidate, dict)
        and isinstance(candidate.get("validations"), dict)
        and any(target_id in candidate["validations"] for target_id in removed_target_ids)
        for candidate in record.get("candidates", [])
    )
    if has_removed_history and data.get("confirmTargetRemoval") is not True:
        raise ValueError("confirm removal of compatibility targets with saved validation history")
    if profile["enabled"] and (not profile["peerUuid"] or not profile["instanceUuid"]):
        raise ValueError("select a WireGuard instance and peer before enabling endpoint management")

    def mutate(doc):
        device = doc["devices"].get(device_id)
        if not device or device.get("ownerId") != owner_id:
            raise ValueError("device not found")
        saved_record = _history(doc, owner_id, device_id)
        _normalise_record(saved_record, profile)
        for candidate in saved_record.get("candidates", []):
            validations = candidate.get("validations")
            if isinstance(validations, dict):
                for target_id in removed_target_ids:
                    validations.pop(target_id, None)
        device["vpnEndpointProfile"] = profile
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
        _normalise_record(record, profile)
        existing = {x.get("candidateId"): x for x in record["candidates"]}
        seen = {candidate["candidateId"] for candidate in discovered}
        for old in record["candidates"]:
            if old.get("candidateId") not in seen:
                old["classification"] = "Stale"
        for candidate in discovered:
            ident = candidate["candidateId"]
            previous = existing.get(ident)
            entry = {"candidateId": ident, "endpointIp": candidate["endpointIp"],
                     "hostname": candidate["hostname"], "publicKeyFingerprint": _fingerprint(candidate["publicKey"]),
                     "owner": candidate.get("owner", ""), "asn": candidate.get("asn"),
                     "load": candidate.get("load"), "firstSeen": previous.get("firstSeen", now) if previous else now,
                     "lastSeen": now, "classification": candidate["classification"],
                     "validations": dict(previous.get("validations", {})) if previous else {}}
            if previous and previous.get("lastVerification"):
                entry["lastVerification"] = previous["lastVerification"]
                entry["lastVerifiedAt"] = previous.get("lastVerifiedAt")
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
        _normalise_record(record, profile)
        return {"status": "cached", "candidates": [
            _public_candidate(x, profile) for x in record.get("lastCandidates", [])]}
    try:
        raw = _nord.discover(profile["country"], profile["maxCandidates"])
    except NordVPNError as error:
        _normalise_record(record, profile)
        return {"status": "error", "error": str(error), "candidates": [
            _public_candidate(x, profile) for x in record.get("lastCandidates", [])]}
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
    return {"status": "ok", "candidates": [_public_candidate(x, profile) for x in candidates]}


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
    record = _history(store.load(), owner_id, device_id)
    _normalise_record(record, profile)
    if refresh:
        discovery = discover(device_id, force=True)
    else:
        discovery = {"status": "cached", "candidates": [
            _public_candidate(x, profile) for x in record.get("lastCandidates", [])]}
    try:
        current = _runtime(device_id, profile)
    except Exception:
        current = {"configured": bool(profile["peerUuid"]), "error": "Could not read WireGuard runtime status"}
    record = _history(store.load(), owner_id, device_id)
    _normalise_record(record, profile)
    saved = {x.get("candidateId"): x for x in record.get("candidates", [])}
    candidates = []
    for item in discovery.get("candidates", []):
        item = dict(item)
        saved_item = saved.get(item.get("candidateId"), {})
        item["validations"] = saved_item.get("validations", {})
        item["active"] = current.get("endpointIp") == item.get("endpointIp")
        if item["active"]:
            current.update({"hostname": item.get("hostname", ""), "owner": item.get("owner", ""),
                            "asn": item.get("asn"),
                            "classification": _normalise_classification(item.get("classification")),
                            "appearsInDiscovery": True})
        candidates.append(_public_candidate(item, profile))
    if current.get("configured") and "appearsInDiscovery" not in current:
        current.update({"appearsInDiscovery": False, "runtimeClassification": "Stale"})
    current["health"] = _health(current, profile)
    return {
        "profileConfigured": isinstance(dev.get("vpnEndpointProfile"), dict),
        "profile": profile,
        "discovery": {**discovery, "candidates": candidates},
        "current": current,
        "history": [_public_candidate(x, profile) for x in record.get("candidates", [])],
    }


def _health(current: dict[str, Any], profile: dict[str, Any]) -> str:
    if not current.get("configured"):
        return "Unknown"
    if current.get("error"):
        return "Unknown"
    runtime = current.get("status") if isinstance(current.get("status"), dict) else {}
    if str(runtime.get("status") or "").casefold() in {"offline", "down"}:
        return "Offline"
    age = runtime.get("handshakeAge")
    if isinstance(age, (int, float)) and age > profile["handshakeWarningSeconds"]:
        return "Warning"
    if runtime.get("latestHandshake"):
        return "Healthy"
    return "Offline"


def set_validation(owner_id: str, device_id: str, candidate_id: str,
                   target_id: str, state: str, note: str = "") -> None:
    if state not in VALIDATION_STATES:
        raise ValueError("invalid validation state")
    dev = devices.get_device(device_id)
    if not dev or dev.get("ownerId") != owner_id:
        raise ValueError("device not found")
    profile = _profile(dev.get("vpnEndpointProfile"))
    record = _history(store.load(), owner_id, device_id)
    _normalise_record(record, profile)
    if target_id not in {target["id"] for target in profile["compatibilityTargets"]}:
        raise ValueError("compatibility target not found")
    def mutate(doc):
        record = _history(doc, owner_id, device_id)
        _normalise_record(record, profile)
        for candidate in record["candidates"]:
            if candidate.get("candidateId") == candidate_id:
                candidate.setdefault("validations", {})[target_id] = {
                    "state": state,
                    "lastValidatedAt": int(time.time()),
                    "note": _bounded_text(note, 500),
                }
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
    _normalise_record(record, profile)
    candidate = next((x for x in record.get("lastCandidates", []) if x.get("candidateId") == candidate_id), None)
    if not candidate or _normalise_classification(candidate.get("classification")) not in {
            "Preferred", "Eligible"}:
        raise ValueError("only a current preferred or eligible candidate can be switched to")
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
            if discovery.get("status") == "ok" and active and active.get("classification") in {"Excluded", "Unknown"}:
                conditions.append("owner_not_preferred")
            if (discovery.get("status") == "ok" and profile["preferredOwners"]
                    and not any(x.get("classification") == "Preferred" for x in candidates)):
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

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
MAX_PROFILES = 10
MIN_DISCOVERY_SECONDS = 300
MAX_DISCOVERY_SECONDS = 7 * 24 * 60 * 60
VALIDATION_STATES = {"Verified", "Failed", "Assumed", "Unknown"}
IMPORTED_TARGET_ID = "imported-compatibility-check"
LEGACY_PROFILE_ID = "default"
DEFAULT_PROFILE = {
    "name": "VPN endpoint", "enabled": False, "country": "United Kingdom", "city": "London",
    "maxCandidates": 20,
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


def _profile(value: object, profile_id: str = LEGACY_PROFILE_ID) -> dict[str, Any]:
    result: dict[str, Any] = dict(DEFAULT_PROFILE)
    if isinstance(value, dict):
        result.update({key: value[key] for key in result if key in value})
        if "excludedOwners" not in value and "rejectedOwners" in value:
            result["excludedOwners"] = value["rejectedOwners"]
        saved_id = _bounded_text(value.get("id"), 80)
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", saved_id):
            profile_id = saved_id
    result["id"] = profile_id
    result["name"] = _bounded_text(result["name"], 120) or DEFAULT_PROFILE["name"]
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


def _profiles(device: dict[str, Any]) -> list[dict[str, Any]]:
    raw_profiles = device.get("vpnEndpointProfiles")
    if isinstance(raw_profiles, list):
        values = raw_profiles[:MAX_PROFILES]
    elif isinstance(device.get("vpnEndpointProfile"), dict):
        legacy = dict(device["vpnEndpointProfile"])
        legacy.setdefault("id", LEGACY_PROFILE_ID)
        legacy.setdefault("name", _bounded_text(legacy.get("country"), 120) or DEFAULT_PROFILE["name"])
        values = [legacy]
    else:
        values = []
    result, seen = [], set()
    for index, value in enumerate(values):
        if not isinstance(value, dict):
            continue
        fallback = LEGACY_PROFILE_ID if index == 0 else f"profile-{index + 1}"
        profile = _profile(value, fallback)
        if profile["id"] in seen:
            continue
        seen.add(profile["id"])
        result.append(profile)
    return result


def _selected_profile(device: dict[str, Any], profile_id: str | None = None) -> dict[str, Any]:
    profiles = _profiles(device)
    if profile_id is None and profiles:
        return profiles[0]
    selected = next((profile for profile in profiles if profile["id"] == profile_id), None)
    if not selected:
        raise ValueError("VPN endpoint profile not found")
    return selected


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
        "candidates": [], "events": [], "discoveries": {}, "lastCandidates": [],
        "state": {}, "lastDiscovery": None,
    })


def _profile_discovery(record: dict[str, Any], profile_id: str) -> dict[str, Any]:
    discoveries = record.setdefault("discoveries", {})
    if not isinstance(discoveries, dict):
        discoveries = {}
        record["discoveries"] = discoveries
    saved = discoveries.get(profile_id)
    if not isinstance(saved, dict):
        saved = {}
        if profile_id == LEGACY_PROFILE_ID:
            saved = {
                "lastCandidates": record.get("lastCandidates", []),
                "lastDiscovery": record.get("lastDiscovery"),
            }
        discoveries[profile_id] = saved
    if not isinstance(saved.get("lastCandidates"), list):
        saved["lastCandidates"] = []
    if not isinstance(saved.get("lastDiscovery"), (int, float)):
        saved["lastDiscovery"] = None
    return saved


def _for_profile(candidate: object, profile_id: str) -> bool:
    return isinstance(candidate, dict) and candidate.get("profileId", LEGACY_PROFILE_ID) == profile_id


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
    profile_id = profile["id"]
    discovery = _profile_discovery(record, profile_id)
    for collection in (record.get("candidates", []), discovery.get("lastCandidates", [])):
        for candidate in collection if isinstance(collection, list) else []:
            if not _for_profile(candidate, profile_id):
                continue
            candidate["profileId"] = profile_id
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
    if "name" in data and (not isinstance(data["name"], str) or not data["name"].strip()):
        raise ValueError("name must be a non-empty string")
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


def configure(owner_id: str, device_id: str, data: object, *,
              profile_id: str | None = None, create: bool = False) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("profile must be an object")
    _validate_profile_patch(data)
    dev = devices.get_device(device_id)
    if not dev or dev.get("ownerId") != owner_id:
        raise ValueError("device not found")
    if dev.get("driverId") != "opnsense.firewall":
        raise ValueError("VPN endpoint management requires an OPNsense device")
    profiles = _profiles(dev)
    if create:
        if len(profiles) >= MAX_PROFILES:
            raise ValueError(f"at most {MAX_PROFILES} VPN endpoint profiles are supported")
        profile = _profile({}, uuid.uuid4().hex)
    elif profiles:
        profile = _selected_profile(dev, profile_id)
    elif profile_id is not None:
        raise ValueError("VPN endpoint profile not found")
    else:
        profile = _profile({}, LEGACY_PROFILE_ID)
    record = _history(store.load(), owner_id, device_id)
    _normalise_record(record, profile)
    previous_target_ids = ({item["id"] for item in profile["compatibilityTargets"]}
                           if not create else set())
    patch = dict(data)
    if "excludedOwners" not in patch and "rejectedOwners" in patch:
        patch["excludedOwners"] = patch["rejectedOwners"]
    patch.pop("rejectedOwners", None)
    merged = dict(profile if not create else _profile({}, profile["id"]))
    merged.update({key: value for key, value in patch.items() if key in DEFAULT_PROFILE})
    profile = _profile(merged)
    new_target_ids = {item["id"] for item in profile["compatibilityTargets"]}
    removed_target_ids = previous_target_ids - new_target_ids
    has_removed_history = any(
        isinstance(candidate, dict)
        and _for_profile(candidate, profile["id"])
        and isinstance(candidate.get("validations"), dict)
        and any(target_id in candidate["validations"] for target_id in removed_target_ids)
        for candidate in record.get("candidates", [])
    )
    if has_removed_history and data.get("confirmTargetRemoval") is not True:
        raise ValueError("confirm removal of compatibility targets with saved validation history")
    if profile["enabled"] and (not profile["peerUuid"] or not profile["instanceUuid"]):
        raise ValueError("select a WireGuard instance and peer before enabling endpoint management")
    if (profile["peerUuid"] and any(saved["id"] != profile["id"]
                                    and saved["peerUuid"] == profile["peerUuid"]
                                    for saved in profiles)):
        raise ValueError("the selected WireGuard peer is already managed by another profile")

    def mutate(doc):
        device = doc["devices"].get(device_id)
        if not device or device.get("ownerId") != owner_id:
            raise ValueError("device not found")
        saved_profiles = _profiles(device)
        if create:
            if len(saved_profiles) >= MAX_PROFILES:
                raise ValueError(f"at most {MAX_PROFILES} VPN endpoint profiles are supported")
            saved_profiles.append(profile)
        else:
            replaced = False
            for index, saved_profile in enumerate(saved_profiles):
                if saved_profile["id"] == profile["id"]:
                    saved_profiles[index] = profile
                    replaced = True
                    break
            if not replaced:
                if profile["id"] != LEGACY_PROFILE_ID or saved_profiles:
                    raise ValueError("VPN endpoint profile not found")
                saved_profiles.append(profile)
        saved_record = _history(doc, owner_id, device_id)
        _normalise_record(saved_record, profile)
        for candidate in saved_record.get("candidates", []):
            if not _for_profile(candidate, profile["id"]):
                continue
            validations = candidate.get("validations")
            if isinstance(validations, dict):
                for target_id in removed_target_ids:
                    validations.pop(target_id, None)
        device["vpnEndpointProfiles"] = saved_profiles
        device.pop("vpnEndpointProfile", None)
    store.update(mutate)
    return profile


def remove_profile(owner_id: str, device_id: str, profile_id: str, confirmed: bool) -> None:
    if not confirmed:
        raise ValueError("explicit confirmation is required before removing a profile")
    dev = devices.get_device(device_id)
    if not dev or dev.get("ownerId") != owner_id:
        raise ValueError("device not found")
    _selected_profile(dev, profile_id)

    def mutate(doc):
        device = doc["devices"].get(device_id)
        if not device or device.get("ownerId") != owner_id:
            raise ValueError("device not found")
        profiles = [profile for profile in _profiles(device) if profile["id"] != profile_id]
        if len(profiles) == len(_profiles(device)):
            raise ValueError("VPN endpoint profile not found")
        device["vpnEndpointProfiles"] = profiles
        device.pop("vpnEndpointProfile", None)
        record = _history(doc, owner_id, device_id)
        record["candidates"] = [candidate for candidate in record.get("candidates", [])
                                if not _for_profile(candidate, profile_id)]
        record["events"] = [event for event in record.get("events", [])
                            if not _for_profile(event, profile_id)]
        discoveries = record.get("discoveries")
        if isinstance(discoveries, dict):
            discoveries.pop(profile_id, None)
        if profile_id == LEGACY_PROFILE_ID:
            record["lastCandidates"] = []
            record["lastDiscovery"] = None
    store.update(mutate)


def choices(device_id: str) -> dict[str, Any]:
    with devices.device_conn(device_id, timeout=12) as (_, driver, conn):
        if not hasattr(driver, "wireguard_peers"):
            raise ValueError("device does not support OPNsense WireGuard")
        result = {"peers": driver.wireguard_peers(conn),
                  "instances": driver.wireguard_instances(conn)}
    try:
        result["locations"] = _nord.locations()
    except NordVPNError as error:
        result["locations"] = []
        result["locationsError"] = str(error)
    return result


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
        profile_id = profile["id"]
        existing = {x.get("candidateId"): x for x in record["candidates"]
                    if _for_profile(x, profile_id)}
        seen = {candidate["candidateId"] for candidate in discovered}
        for old in record["candidates"]:
            if _for_profile(old, profile_id) and old.get("candidateId") not in seen:
                old["classification"] = "Stale"
        for candidate in discovered:
            ident = candidate["candidateId"]
            previous = existing.get(ident)
            entry = {"profileId": profile_id, "candidateId": ident,
                     "serverId": candidate.get("serverId"),
                     "endpointIp": candidate["endpointIp"],
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
        discovery = _profile_discovery(record, profile_id)
        discovery["lastCandidates"] = [{**candidate, "profileId": profile_id}
                                       for candidate in discovered[:50]]
        discovery["lastDiscovery"] = now
    store.update(mutate)


def discover(device_id: str, profile_id: str | None = None, *, force=False) -> dict[str, Any]:
    dev = devices.get_device(device_id)
    if not dev:
        raise ValueError("device not found")
    profile = _selected_profile(dev, profile_id)
    record = _history(store.load(), dev["ownerId"], device_id)
    saved_discovery = _profile_discovery(record, profile["id"])
    now = int(time.time())
    if (not force and saved_discovery.get("lastDiscovery")
            and now - saved_discovery["lastDiscovery"] < profile["discoveryIntervalSeconds"]):
        _normalise_record(record, profile)
        return {"status": "cached", "candidates": [
            _public_candidate(x, profile) for x in saved_discovery.get("lastCandidates", [])]}
    try:
        raw = _nord.discover(profile["country"], profile["maxCandidates"], profile["city"])
    except NordVPNError as error:
        _normalise_record(record, profile)
        return {"status": "error", "error": str(error), "candidates": [
            _public_candidate(x, profile) for x in saved_discovery.get("lastCandidates", [])]}
    candidates = []
    for candidate in raw:  # deliberately serial: at most MAX_LIMIT bounded RDAP HTTP requests through cache
        ownership = _rdap.lookup(candidate.endpoint_ip)
        owner = ownership.owner or (f"AS{ownership.asn}" if ownership.asn else "")
        item = {"serverId": candidate.server_id, "hostname": candidate.hostname,
                "endpointIp": candidate.endpoint_ip,
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


def status(owner_id: str, device_id: str, profile_id: str | None = None, *,
           refresh=False) -> dict[str, Any]:
    dev = devices.get_device(device_id)
    if not dev or dev.get("ownerId") != owner_id:
        raise ValueError("device not found")
    profiles = _profiles(dev)
    if not profiles and profile_id is None:
        profile = _profile({}, LEGACY_PROFILE_ID)
    else:
        profile = _selected_profile(dev, profile_id)
    record = _history(store.load(), owner_id, device_id)
    _normalise_record(record, profile)
    saved_discovery = _profile_discovery(record, profile["id"])
    if refresh:
        discovery = discover(device_id, profile["id"], force=True)
    else:
        discovery = {"status": "cached", "candidates": [
            _public_candidate(x, profile) for x in saved_discovery.get("lastCandidates", [])]}
    try:
        current = _runtime(device_id, profile)
    except Exception:
        current = {"configured": bool(profile["peerUuid"]), "error": "Could not read WireGuard runtime status"}
    record = _history(store.load(), owner_id, device_id)
    _normalise_record(record, profile)
    saved_discovery = _profile_discovery(record, profile["id"])
    saved = {x.get("candidateId"): x for x in record.get("candidates", [])
             if _for_profile(x, profile["id"])}
    candidates = []
    for item in discovery.get("candidates", []):
        item = dict(item)
        saved_item = saved.get(item.get("candidateId"), {})
        item["validations"] = saved_item.get("validations", {})
        item["active"] = current.get("endpointIp") == item.get("endpointIp")
        if item["active"]:
            _enrich_current(current, item, profile)
            current["appearsInDiscovery"] = True
        candidates.append(_public_candidate(item, profile))
    if current.get("configured") and "appearsInDiscovery" not in current:
        historical = next((item for item in saved.values()
                           if item.get("endpointIp") == current.get("endpointIp")), None)
        if historical:
            _enrich_current(current, historical, profile)
        current["appearsInDiscovery"] = False
        if saved_discovery.get("lastDiscovery"):
            current["runtimeClassification"] = "Stale"
    current["health"] = _health(current, profile)
    return {
        "profileConfigured": bool(profiles),
        "profile": profile,
        "discovery": {**discovery, "at": saved_discovery.get("lastDiscovery"),
                      "candidates": candidates},
        "current": current,
        "history": [_public_candidate(x, profile) for x in record.get("candidates", [])
                    if _for_profile(x, profile["id"])],
    }


def _enrich_current(current: dict[str, Any], candidate: dict[str, Any],
                    profile: dict[str, Any]) -> None:
    current.update({
        "serverId": candidate.get("serverId"),
        "hostname": candidate.get("hostname", ""),
        "owner": candidate.get("owner", ""),
        "asn": candidate.get("asn"),
        "asnName": candidate.get("asnName", ""),
        "organisation": candidate.get("organisation", ""),
        "candidateId": candidate.get("candidateId"),
        "compatibilityTargets": _candidate_targets(candidate, profile),
        "classification": _normalise_classification(candidate.get("classification")),
    })


def statuses(owner_id: str, device_id: str, *, refresh=False) -> dict[str, Any]:
    dev = devices.get_device(device_id)
    if not dev or dev.get("ownerId") != owner_id:
        raise ValueError("device not found")
    profiles = _profiles(dev)
    if not profiles:
        return {
            "profileConfigured": False,
            "profile": _profile({}, LEGACY_PROFILE_ID),
            "discovery": {"status": "idle", "candidates": []},
            "current": {"configured": False, "health": "Unknown"},
            "history": [],
            "profiles": [],
        }
    results = [status(owner_id, device_id, profile["id"], refresh=refresh)
               for profile in profiles]
    return {**results[0], "profiles": results}


def _health(current: dict[str, Any], profile: dict[str, Any]) -> str:
    if not current.get("configured"):
        return "Unknown"
    if current.get("error"):
        return "Unknown"
    runtime_value = current.get("status")
    runtime: dict[str, Any] = runtime_value if isinstance(runtime_value, dict) else {}
    if str(runtime.get("status") or "").casefold() in {"offline", "down"}:
        return "Offline"
    age = runtime.get("handshakeAge")
    if isinstance(age, (int, float)) and age > profile["handshakeWarningSeconds"]:
        return "Warning"
    if runtime.get("latestHandshake"):
        return "Healthy"
    return "Offline"


def set_validation(owner_id: str, device_id: str, candidate_id: str,
                   target_id: str, state: str, note: str = "", *,
                   profile_id: str | None = None) -> None:
    if state not in VALIDATION_STATES:
        raise ValueError("invalid validation state")
    dev = devices.get_device(device_id)
    if not dev or dev.get("ownerId") != owner_id:
        raise ValueError("device not found")
    profile = _selected_profile(dev, profile_id)
    record = _history(store.load(), owner_id, device_id)
    _normalise_record(record, profile)
    if target_id not in {target["id"] for target in profile["compatibilityTargets"]}:
        raise ValueError("compatibility target not found")
    def mutate(doc):
        record = _history(doc, owner_id, device_id)
        _normalise_record(record, profile)
        for candidate in record["candidates"]:
            if (_for_profile(candidate, profile["id"])
                    and candidate.get("candidateId") == candidate_id):
                candidate.setdefault("validations", {})[target_id] = {
                    "state": state,
                    "lastValidatedAt": int(time.time()),
                    "note": _bounded_text(note, 500),
                }
                return
        raise ValueError("candidate not found")
    store.update(mutate)


def switch(owner_id: str, device_id: str, candidate_id: str, confirmed: bool, *,
           profile_id: str | None = None) -> dict[str, Any]:
    if not confirmed:
        raise ValueError("explicit confirmation is required before switching")
    dev = devices.get_device(device_id)
    if not dev or dev.get("ownerId") != owner_id:
        raise ValueError("device not found")
    profile = _selected_profile(dev, profile_id)
    if not profile["enabled"] or not profile["peerUuid"]:
        raise ValueError("endpoint management is not configured and enabled")
    record = _history(store.load(), owner_id, device_id)
    _normalise_record(record, profile)
    discovery = _profile_discovery(record, profile["id"])
    candidate = next((x for x in discovery.get("lastCandidates", [])
                      if x.get("candidateId") == candidate_id), None)
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
    configuration_changed = False
    rollback_ok = None
    rollback_handshake = None
    before = None
    gateway_before = None
    switch_stage = "connect to OPNsense"
    rollback_stage = "not started"
    switch_error: Exception | None = None
    rollback_error: Exception | None = None
    try:
        with devices.device_conn(device_id, timeout=20) as (_, driver, conn):
            switch_stage = "read the WireGuard peer"
            before = driver.wireguard_peer(conn, profile["peerUuid"])
            switch_stage = "validate the WireGuard peer association"
            peer_instances = {value.strip() for value in str(before.get("servers") or "").split(",") if value.strip()}
            if peer_instances and profile["instanceUuid"] not in peer_instances:
                raise ValueError("selected WireGuard instance is not associated with the selected peer")
            switch_stage = "read the associated gateway"
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
            switch_stage = "save the replacement WireGuard peer"
            driver.wireguard_update_peer(conn, profile["peerUuid"], replacement)
            configuration_changed = True
            if changed_gateway:
                switch_stage = "save the associated gateway"
                driver.gateway_update(conn, profile["gatewayUuid"], gateway_after)
            switch_stage = "reload the WireGuard interface"
            driver.wireguard_reload(conn)
            verified = False
            switch_stage = "verify a new WireGuard handshake"
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
    except Exception as error:
        switch_error = error
        if configuration_changed:
            try:
                rollback_stage = "connect to OPNsense"
                with devices.device_conn(device_id, timeout=20) as (_, driver, conn):
                    if before is None:
                        raise RuntimeError("no peer snapshot available for rollback")
                    rollback_stage = "restore the WireGuard peer"
                    driver.wireguard_update_peer(conn, profile["peerUuid"], before)
                    if changed_gateway and gateway_before is not None:
                        rollback_stage = "restore the associated gateway"
                        driver.gateway_update(conn, profile["gatewayUuid"], gateway_before)
                    rollback_stage = "reload the restored WireGuard interface"
                    driver.wireguard_reload(conn)
                    # OPNsense accepting the complete saved configuration and
                    # service reconfigure is the rollback result. A handshake
                    # is separate and may require routed traffic.
                    rollback_ok = True
                    rollback_stage = "observe the restored WireGuard handshake"
                    try:
                        restored = driver.wireguard_status(conn, before)
                        rollback_handshake = bool(restored.get("latestHandshake"))
                    except Exception as status_error:
                        rollback_error = status_error
            except Exception as error:
                rollback_error = error
                rollback_ok = False
        else:
            rollback_stage = "not required"
        switch_detail = f"{type(switch_error).__name__}: {switch_error}"
        if rollback_ok is None:
            rollback_detail = "not required because no configuration was changed"
        elif rollback_ok:
            if rollback_error:
                rollback_detail = ("configuration restored; handshake observation failed: "
                                   f"{type(rollback_error).__name__}: {rollback_error}")
            else:
                rollback_detail = ("configuration restored; authenticated handshake observed"
                                   if rollback_handshake else
                                   "configuration restored; authenticated handshake not yet observed")
        else:
            rollback_detail = f"failed during {rollback_stage}: {type(rollback_error).__name__}: {rollback_error}"
        logbuf.log_event(
            "error" if rollback_ok is False else "warn", "vpn_endpoint_switch_failed",
            source="vpn-endpoints", device_id=device_id, profile_id=profile["id"],
            candidate_id=candidate_id, switch_stage=switch_stage,
            rollback_stage=rollback_stage, rollback=rollback_ok,
            rollback_handshake_observed=rollback_handshake,
            error=switch_detail,
            message=(f"VPN endpoint switch failed during {switch_stage}: {switch_detail}; "
                     f"rollback {rollback_detail}."),
        )
        if rollback_ok is None:
            message = ("Endpoint change was not applied, so the existing OPNsense configuration "
                       "was left unchanged. Check the selected WireGuard instance and peer in Settings.")
        elif rollback_ok:
            message = "Endpoint verification failed; the previous configuration was restored."
            if not rollback_handshake:
                message += " A restored-tunnel handshake has not yet been observed."
        else:
            message = "Endpoint verification and rollback failed. Diagnostic details were recorded in Logs."
        result = {"ok": False, "rollback": rollback_ok, "changedGateway": changed_gateway,
                  "message": message}
    def audit(doc):
        rec = _history(doc, owner_id, device_id)
        for saved in rec["candidates"]:
            if (_for_profile(saved, profile["id"])
                    and saved.get("candidateId") == candidate_id):
                saved["lastVerification"] = "success" if result["ok"] else "failed"
                saved["lastVerifiedAt"] = int(time.time())
                break
        rec["events"].append({"profileId": profile["id"], "at": int(time.time()),
                              "type": "switch", "candidateId": candidate_id,
                              "result": "success" if result["ok"] else "failed", "rollback": result["rollback"]})
        del rec["events"][:-MAX_HISTORY]
    store.update(audit)
    return result


def poll_enabled() -> None:
    """Run one bounded background health pass; failures never stop polling."""
    doc = store.load()
    for device_id, dev in doc.get("devices", {}).items():
        if dev.get("driverId") != "opnsense.firewall":
            continue
        for profile in _profiles(dev):
            if not profile["enabled"]:
                continue
            try:
                discovery = discover(device_id, profile["id"], force=False)
                outcome = status(dev.get("ownerId"), device_id, profile["id"])
                current = outcome.get("current", {})
                candidates = outcome.get("discovery", {}).get("candidates", [])
                active = next((x for x in candidates if x.get("active")), None)
                conditions = []
                age = (current.get("status") or {}).get("handshakeAge")
                if isinstance(age, (int, float)) and age > profile["handshakeWarningSeconds"]:
                    conditions.append("stale_handshake")
                if discovery.get("status") == "ok" and current.get("configured") and not active:
                    conditions.append("endpoint_missing")
                if (discovery.get("status") == "ok" and active
                        and active.get("classification") in {"Excluded", "Unknown"}):
                    conditions.append("owner_not_preferred")
                if (discovery.get("status") == "ok" and profile["preferredOwners"]
                        and not any(x.get("classification") == "Preferred" for x in candidates)):
                    conditions.append("no_preferred_candidate")
                _update_alert_state(dev, profile, conditions)
            except Exception:
                # A management-plane error is already covered by the ordinary
                # device poller; endpoint alerts must not manufacture a false
                # tunnel conclusion from an unavailable firewall API.
                continue


def _update_alert_state(device: dict, profile: dict, conditions: list[str]) -> None:
    now = int(time.time())
    emitted: list[str] = []
    def mutate(doc):
        rec = _history(doc, device["ownerId"], device["id"])
        state = rec.setdefault("state", {}).setdefault("profileAlerts", {}).setdefault(profile["id"], {})
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
                        f"{device.get('name') or device.get('host')} · {profile['name']}: "
                        f"{condition.replace('_', ' ')}.",
                        data={"deviceId": device["id"], "profileId": profile["id"],
                              "type": "vpn_endpoint", "condition": condition})
        except Exception:
            pass

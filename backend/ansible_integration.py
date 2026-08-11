"""Restricted Ansible-controller integration for Compute maintenance.

This module deliberately exposes named discovery and maintenance primitives,
not a generic SSH, shell, or playbook runner. Every remote path comes from the
administrator's contained controller configuration and every playbook must be
discovered and explicitly approved before another service may execute it.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import PurePosixPath
import re
import shlex
import time

import crypto
import store
import transports
from domain import safe_error


CONTROLLER_ID = "primary"
OPERATIONS = frozenset({
    "os_check", "os_update", "docker_check", "docker_discovery",
    "docker_update_pull", "docker_update_local_build",
})
UPDATE_STRATEGIES = frozenset({"pull", "local_build", "unmanaged"})
_INVENTORY_HOST_RE = re.compile(r"^[A-Za-z0-9_.:@+-]{1,255}$")
_VARIABLE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_SHELL_METACHAR_RE = re.compile(r"[\s;&|<>`$(){}\[\]*?!'\"\\]")
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_SECRET_VALUE_RE = re.compile(
    r"(?i)((?:password|passwd|private[_-]?key|api[_-]?token|token|secret)"
    r"[\"']?\s*[:=]\s*[\"']?)([^\s,}\"']+)")
MAX_OUTPUT_BYTES = max(10_000, int(os.environ.get("HLHQ_MAX_ANSIBLE_OUTPUT_BYTES", "200000")))
MAX_INVENTORY_BYTES = max(
    MAX_OUTPUT_BYTES, int(os.environ.get("HLHQ_MAX_ANSIBLE_INVENTORY_BYTES", "5000000")))


def _safe_path(value, label, *, base=None, directory=False) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError(f"{label} is required")
    candidate = PurePosixPath(raw)
    if not candidate.is_absolute():
        if base is None:
            raise ValueError(f"{label} must be an absolute path")
        candidate = PurePosixPath(base) / candidate
    if ".." in candidate.parts or str(candidate) == "/":
        raise ValueError(f"{label} is not a safe contained path")
    normalized = str(candidate)
    if base is not None:
        try:
            candidate.relative_to(PurePosixPath(base))
        except ValueError as error:
            raise ValueError(f"{label} must be inside the project directory") from error
    if directory and normalized.endswith("/"):
        normalized = normalized.rstrip("/")
    return normalized


def _bounded_int(value, label, low, high) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} must be a number") from error
    if result < low or result > high:
        raise ValueError(f"{label} must be between {low} and {high}")
    return result


def _executable_path(value, label, *, required=False) -> str:
    """Validate a remote executable path before it reaches command construction."""
    raw = str(value or "").strip()
    if not raw and not required:
        return ""
    if not raw:
        raise ValueError(f"{label} is required; run Test Connection to discover it")
    candidate = PurePosixPath(raw)
    if (len(raw) > 4096 or not candidate.is_absolute() or ".." in candidate.parts or
            str(candidate) == "/"):
        raise ValueError(f"{label} must be an absolute remote path")
    if _SHELL_METACHAR_RE.search(raw):
        raise ValueError(f"{label} cannot contain shell metacharacters or whitespace")
    return str(candidate)


def _public_controller(record: dict | None) -> dict:
    if not record:
        return {
            "id": CONTROLLER_ID, "enabled": False, "displayName": "Ansible",
            "credentialConfigured": False, "inventory": {"hosts": [], "groups": []},
            "discoveredPlaybooks": [], "playbooks": {},
        }
    result = {key: copy.deepcopy(value) for key, value in record.items()
              if key not in {"credentialRef"}}
    result["credentialConfigured"] = bool(record.get("credentialRef"))
    inventory = result.get("inventory") or {}
    hosts = inventory.get("hosts") or {}
    groups = inventory.get("groups") or {}
    inventory["hosts"] = sorted(hosts.values(), key=lambda item: item["name"].lower())
    inventory["groups"] = sorted(groups.values(), key=lambda item: item["name"].lower())
    result["inventory"] = inventory
    return result


def get_controller(controller_id=CONTROLLER_ID, *, public=False) -> dict | None:
    record = store.load()["ansibleControllers"].get(controller_id)
    return _public_controller(record) if public else record


def save_controller(config: dict) -> dict:
    if not isinstance(config, dict):
        raise ValueError("Ansible configuration must be an object")
    project = _safe_path(config.get("projectDirectory"), "project directory", directory=True)
    inventory = _safe_path(config.get("inventoryPath"), "inventory path", base=project)
    playbooks = _safe_path(config.get("playbooksDirectory"), "playbooks directory",
                           base=project, directory=True)
    host = str(config.get("host") or "").strip()
    username = str(config.get("sshUsername") or "").strip()
    if not host or len(host) > 255:
        raise ValueError("controller hostname/IP is required")
    if not username or len(username) > 128:
        raise ValueError("SSH username is required")
    auth_method = str(config.get("authMethod") or "private_key")
    if auth_method not in ("password", "private_key"):
        raise ValueError("authentication method must be password or private_key")

    secret_value = config.get("password") if auth_method == "password" else config.get("privateKey")
    current = get_controller() or {}
    playbook_executable = _executable_path(
        config.get("ansiblePlaybookExecutable", current.get("ansiblePlaybookExecutable")),
        "Ansible Playbook executable")
    inventory_executable = _executable_path(
        config.get("ansibleInventoryExecutable", current.get("ansibleInventoryExecutable")),
        "Ansible Inventory executable")
    credential_ref = current.get("credentialRef")
    if secret_value:
        credential_ref = credential_ref or __import__("secrets").token_hex(8)
        credential = ({"username": username, "password": str(secret_value)}
                      if auth_method == "password" else
                      {"username": username, "privateKey": str(secret_value)})
        encrypted = crypto.encrypt(credential)
    elif not credential_ref:
        raise ValueError("a controller password or private key is required")
    elif (current.get("authMethod") != auth_method or
          current.get("sshUsername") != username):
        raise ValueError("enter a new credential when changing authentication or SSH username")
    else:
        encrypted = None

    record = {
        "id": CONTROLLER_ID,
        "enabled": bool(config.get("enabled")),
        "displayName": str(config.get("displayName") or "Ansible").strip()[:100],
        "host": host,
        "sshPort": _bounded_int(config.get("sshPort", 22), "SSH port", 1, 65535),
        "sshUsername": username,
        "authMethod": auth_method,
        "credentialRef": credential_ref,
        "projectDirectory": project,
        "inventoryPath": inventory,
        "playbooksDirectory": playbooks,
        "ansiblePlaybookExecutable": playbook_executable,
        "ansibleInventoryExecutable": inventory_executable,
        "connectionTimeout": _bounded_int(
            config.get("connectionTimeout", 12), "connection timeout", 1, 120),
        "executionTimeout": _bounded_int(
            config.get("executionTimeout", 1800), "execution timeout", 30, 86400),
        "inventory": copy.deepcopy(current.get("inventory") or
                                   {"hosts": {}, "groups": {}}),
        "discoveredPlaybooks": copy.deepcopy(current.get("discoveredPlaybooks") or []),
        "playbooks": copy.deepcopy(current.get("playbooks") or {}),
        "updatedAt": int(time.time()),
    }

    def mutate(document):
        if encrypted is not None:
            document["credentials"][credential_ref] = encrypted
        document["ansibleControllers"][CONTROLLER_ID] = record

    store.batch_update(mutate)
    return _public_controller(record)


def _credentials(record: dict) -> dict:
    token = store.load()["credentials"].get(record.get("credentialRef"))
    if not token:
        raise ValueError("Ansible controller credentials are not configured")
    return crypto.decrypt(token)


def _redact(text: object, record: dict | None = None, *,
            max_bytes=MAX_OUTPUT_BYTES) -> str:
    value = _ANSI_RE.sub("", str(text or ""))
    if record:
        try:
            secret = _credentials(record)
        except Exception:
            secret = {}
        for key, candidate in secret.items():
            if key not in {"password", "privateKey", "private_key", "token", "secret"}:
                continue
            if candidate:
                value = value.replace(str(candidate), "[REDACTED]")
    value = _SECRET_VALUE_RE.sub(r"\1[REDACTED]", value)
    if len(value) <= max_bytes:
        return value
    half = max(1, (max_bytes - 40) // 2)
    return value[:half] + "\n[... output truncated ...]\n" + value[-half:]


def sanitized_error(error: Exception, record: dict | None = None) -> str:
    return _redact(safe_error(error), record)[:500]


def controller_connection(record: dict | None = None):
    record = record or get_controller()
    if not record:
        raise ValueError("Ansible controller is not configured")
    credential = _credentials(record)
    return transports.open_connection(
        "ssh", record["host"], record["sshPort"], credential,
        timeout=record["connectionTimeout"])


def _command(record: dict, argv: list[str], *, cwd=True) -> str:
    """Build one quoted command from server-owned, previously validated values."""
    configured_executables = {
        _executable_path(record.get("ansiblePlaybookExecutable"),
                         "Ansible Playbook executable"),
        _executable_path(record.get("ansibleInventoryExecutable"),
                         "Ansible Inventory executable"),
    }
    configured_executables.discard("")
    if not argv or argv[0] not in configured_executables | {"test", "find", "command"}:
        raise ValueError("unsupported controller command")
    command = shlex.join([str(part) for part in argv])
    if cwd:
        return f"cd -- {shlex.quote(record['projectDirectory'])} && {command}"
    return command


def _run(conn, record, argv, *, timeout=None, cwd=True,
         output_limit=MAX_OUTPUT_BYTES):
    executable_label = next((label for field, label in (
        ("ansiblePlaybookExecutable", "Ansible Playbook executable"),
        ("ansibleInventoryExecutable", "Ansible Inventory executable"),
    ) if argv and argv[0] == record.get(field)), None)
    if executable_label:
        _validate_remote_executable(
            conn, record, argv[0], executable_label,
            timeout=timeout or record["executionTimeout"])
    code, stdout, stderr = conn.run(
        _command(record, argv, cwd=cwd),
        timeout=timeout or record["executionTimeout"])
    return (code, _redact(stdout, record, max_bytes=output_limit),
            _redact(stderr, record, max_bytes=output_limit))


def _remote_home(conn, record: dict) -> str:
    """Resolve the authenticated SSH account's home without assuming its layout."""
    code, stdout, stderr = conn.run(
        "cd && pwd -P", timeout=record["connectionTimeout"])
    if code:
        raise ValueError(_redact(stderr, record).strip() or
                         "could not resolve the remote SSH user's home directory")
    lines = str(stdout or "").splitlines()
    home = lines[0].strip() if len(lines) == 1 else ""
    if (not home or not PurePosixPath(home).is_absolute() or ".." in PurePosixPath(home).parts or
            _SHELL_METACHAR_RE.search(home)):
        raise ValueError("remote SSH user home directory is invalid")
    return str(PurePosixPath(home))


def _validate_remote_executable(conn, record: dict, path: str, label: str, *, timeout) -> str:
    candidate = _executable_path(path, label, required=True)
    code, _, _ = conn.run(
        _command(record, ["test", "-f", candidate], cwd=False), timeout=timeout)
    if code:
        raise ValueError(f"{label} is not a regular file")
    code, _, stderr = conn.run(
        _command(record, ["test", "-x", candidate], cwd=False), timeout=timeout)
    if code:
        detail = _redact(stderr, record).strip()
        raise ValueError(detail or f"{label} is not executable")
    return candidate


def _discover_executables(conn, record: dict) -> dict[str, str]:
    """Discover both Ansible tools, preferring the non-interactive shell PATH."""
    found: dict[str, str] = {}
    missing: list[tuple[str, str, str]] = []
    specs = [
        ("ansiblePlaybookExecutable", "ansible-playbook", "Ansible Playbook executable"),
        ("ansibleInventoryExecutable", "ansible-inventory", "Ansible Inventory executable"),
    ]
    for field, name, label in specs:
        configured = record.get(field)
        if configured:
            found[field] = _executable_path(configured, label, required=True)
            continue
        code, output, _ = _run(
            conn, record, ["command", "-v", name],
            timeout=record["connectionTimeout"], cwd=False)
        candidate = output.splitlines()[0].strip() if code == 0 and output.splitlines() else ""
        if candidate:
            try:
                found[field] = _validate_remote_executable(
                    conn, record, candidate, label,
                    timeout=record["connectionTimeout"])
                continue
            except ValueError:
                pass
        missing.append((field, name, label))

    if missing:
        try:
            home = _remote_home(conn, record)
        except ValueError:
            return found
        for field, name, label in missing:
            candidates = [
                str(PurePosixPath(home) / ".local" / "bin" / name),
                f"/usr/local/bin/{name}",
                f"/usr/bin/{name}",
            ]
            for candidate in candidates:
                try:
                    _validate_remote_executable(
                        conn, record, candidate, label,
                        timeout=record["connectionTimeout"])
                    found[field] = candidate
                    break
                except ValueError:
                    continue
    return found


def _parse_version(output: str) -> str | None:
    match = re.search(r"ansible(?:-playbook)?\s+\[core\s+([^\]]+)\]", output, re.I)
    if not match:
        match = re.search(r"ansible(?:-playbook)?\s+([0-9][^\s]*)", output, re.I)
    return match.group(1) if match else None


def _inventory_records(payload: dict) -> tuple[dict, dict]:
    if not isinstance(payload, dict):
        raise ValueError("Ansible inventory did not return a JSON object")
    hostvars = ((payload.get("_meta") or {}).get("hostvars") or {})
    hosts: dict[str, dict] = {}
    groups: dict[str, dict] = {}
    visiting = set()

    def visit(group_name: str) -> set[str]:
        if group_name in visiting:
            return set()
        visiting.add(group_name)
        value = payload.get(group_name) or {}
        direct = {str(name) for name in value.get("hosts") or []}
        for child in value.get("children") or []:
            direct.update(visit(str(child)))
        visiting.discard(group_name)
        if group_name not in {"_meta"}:
            groups[group_name] = {"name": group_name, "hosts": sorted(direct)}
        return direct

    for name in payload:
        if name != "_meta":
            visit(str(name))
    all_names = set(hostvars)
    for group in groups.values():
        all_names.update(group["hosts"])
    for name in all_names:
        variables = hostvars.get(name) if isinstance(hostvars.get(name), dict) else {}
        address = str(variables.get("ansible_host") or name)
        hosts[str(name)] = {
            "name": str(name),
            "address": address[:255],
            "groups": sorted(group["name"] for group in groups.values()
                             if str(name) in group["hosts"]),
        }
    return hosts, groups


def test_connection(controller_id=CONTROLLER_ID) -> dict:
    record = get_controller(controller_id)
    if not record:
        raise ValueError("Ansible controller is not configured")
    result = {
        "controller": {"ok": False}, "project": {"ok": False},
        "ansiblePlaybook": {"ok": False}, "ansibleInventory": {"ok": False},
        "inventory": {"ok": False, "hosts": 0, "groups": 0},
    }
    try:
        with controller_connection(record) as conn:
            result["controller"] = {"ok": True, "message": "Connected"}
            code, _, error = _run(conn, record, ["test", "-d", record["projectDirectory"]],
                                  timeout=record["connectionTimeout"], cwd=False)
            result["project"] = {"ok": code == 0}
            if code != 0:
                result["project"]["error"] = error or "Project directory is unavailable"

            discovered = _discover_executables(conn, record)
            test_record = {**record, **discovered}
            specs = [
                ("ansiblePlaybook", "ansiblePlaybookExecutable", "ansible-playbook"),
                ("ansibleInventory", "ansibleInventoryExecutable", "ansible-inventory"),
            ]
            for result_key, field, command_name in specs:
                path = discovered.get(field)
                if not path:
                    result[result_key] = {
                        "ok": False,
                        "error": f"{command_name} was not found; enter an absolute executable path",
                    }
                    continue
                try:
                    code, output, error = _run(
                        conn, test_record, [path, "--version"],
                        timeout=record["connectionTimeout"])
                    result[result_key] = {
                        "ok": code == 0, "path": path, "version": _parse_version(output),
                        "discovered": not bool(record.get(field)),
                        **({"error": error or f"{command_name} is unavailable"} if code else {}),
                    }
                except Exception as error:
                    result[result_key] = {
                        "ok": False, "path": path,
                        "discovered": not bool(record.get(field)),
                        "error": sanitized_error(error, record),
                    }

            inventory_executable = discovered.get("ansibleInventoryExecutable")
            if result["ansibleInventory"].get("ok") and inventory_executable:
                try:
                    code, output, error = _run(
                        conn, test_record,
                        [inventory_executable, "-i", record["inventoryPath"], "--list"],
                        timeout=record["executionTimeout"], output_limit=MAX_INVENTORY_BYTES)
                    if code:
                        result["inventory"] = {
                            "ok": False, "hosts": 0, "groups": 0,
                            "error": error or "Inventory validation failed",
                        }
                    else:
                        hosts, groups = _inventory_records(json.loads(output))
                        result["inventory"] = {
                            "ok": True, "hosts": len(hosts), "groups": len(groups),
                        }
                except Exception as error:
                    result["inventory"]["error"] = sanitized_error(error, record)
            else:
                result["inventory"]["error"] = "Ansible Inventory executable is unavailable"
    except Exception as error:
        result["controller"] = {"ok": False, "error": sanitized_error(error, record)}
    return result


def refresh_inventory(controller_id=CONTROLLER_ID) -> dict:
    record = get_controller(controller_id)
    if not record:
        raise ValueError("Ansible controller is not configured")
    try:
        with controller_connection(record) as conn:
            code, output, error = _run(
                conn, record,
                [_executable_path(record.get("ansibleInventoryExecutable"),
                                  "Ansible Inventory executable", required=True),
                 "-i", record["inventoryPath"], "--list"],
                output_limit=MAX_INVENTORY_BYTES)
        if code:
            raise ValueError(error or f"ansible-inventory exited with status {code}")
        hosts, groups = _inventory_records(json.loads(output))
    except Exception as error:
        raise ValueError(sanitized_error(error, record)) from error
    inventory = {"hosts": hosts, "groups": groups, "discoveredAt": int(time.time())}
    store.update(lambda document: document["ansibleControllers"][controller_id].update(
        inventory=inventory))
    return _public_controller({**record, "inventory": inventory})["inventory"]


def discover_playbooks(controller_id=CONTROLLER_ID) -> list[str]:
    record = get_controller(controller_id)
    if not record:
        raise ValueError("Ansible controller is not configured")
    args = ["find", record["playbooksDirectory"], "-type", "f", "(", "-name", "*.yml",
            "-o", "-name", "*.yaml", ")", "-print"]
    try:
        with controller_connection(record) as conn:
            code, output, error = _run(conn, record, args)
        if code:
            raise ValueError(error or f"playbook discovery exited with status {code}")
        root = PurePosixPath(record["playbooksDirectory"])
        discovered = []
        for line in output.splitlines():
            candidate = PurePosixPath(line.strip())
            try:
                relative = candidate.relative_to(root)
            except ValueError:
                continue
            if relative.suffix.lower() in (".yml", ".yaml") and ".." not in relative.parts:
                discovered.append(str(relative))
        discovered = sorted(set(discovered))
    except Exception as error:
        raise ValueError(sanitized_error(error, record)) from error
    store.update(lambda document: document["ansibleControllers"][controller_id].update(
        discoveredPlaybooks=discovered, playbooksDiscoveredAt=int(time.time())))
    return discovered


def approve_playbook(controller_id: str, config: dict) -> dict:
    record = get_controller(controller_id)
    if not record:
        raise ValueError("Ansible controller is not configured")
    operation = str(config.get("operation") or "")
    if operation not in OPERATIONS:
        raise ValueError("unsupported maintenance operation")
    if not bool(config.get("approved", True)):
        store.update(lambda document: document["ansibleControllers"][controller_id]
                     .setdefault("playbooks", {}).pop(operation, None))
        return get_controller(controller_id, public=True)["playbooks"]
    relative = str(config.get("playbook") or "")
    if relative not in (record.get("discoveredPlaybooks") or []):
        raise ValueError("playbook must be discovered before it can be approved")
    _safe_path(str(PurePosixPath(record["playbooksDirectory"]) / relative),
               "playbook", base=record["playbooksDirectory"])
    metadata = {"playbook": relative, "approved": True}
    reboot_variable = str(config.get("rebootVariable") or "").strip()
    project_variable = str(config.get("projectVariable") or "").strip()
    if reboot_variable:
        if not _VARIABLE_RE.fullmatch(reboot_variable):
            raise ValueError("reboot variable name is invalid")
        metadata.update(supportsReboot=bool(config.get("supportsReboot")),
                        rebootVariable=reboot_variable)
    if project_variable:
        if not _VARIABLE_RE.fullmatch(project_variable):
            raise ValueError("project variable name is invalid")
        metadata["projectVariable"] = project_variable
    strategy = config.get("updateStrategy")
    if strategy is not None:
        if strategy not in UPDATE_STRATEGIES:
            raise ValueError("Docker update strategy is invalid")
        metadata["updateStrategy"] = strategy
    store.update(lambda document: document["ansibleControllers"][controller_id]
                 .setdefault("playbooks", {}).__setitem__(operation, metadata))
    return get_controller(controller_id, public=True)["playbooks"]


def inventory_host(record: dict, name: str) -> dict | None:
    return ((record.get("inventory") or {}).get("hosts") or {}).get(name)


def validate_inventory_host(name: str) -> str:
    value = str(name or "")
    if not _INVENTORY_HOST_RE.fullmatch(value):
        raise ValueError("invalid Ansible inventory host")
    return value


def mapping_suggestions(instance: dict) -> list[dict]:
    record = get_controller()
    if not record:
        return []
    name = str(instance.get("name") or "").strip().lower()
    addresses = set(instance.get("ipAddresses") or [])
    suggestions = []
    for host in ((record.get("inventory") or {}).get("hosts") or {}).values():
        signals = []
        if name and host["name"].lower() == name:
            signals.append("exact_hostname")
        if host.get("address") in addresses:
            signals.append("ip_address")
        if signals:
            suggestions.append({"controllerId": record["id"], "inventoryHost": host["name"],
                                "signals": signals})
    return suggestions


def set_mapping(instance_id: str, enabled: bool, controller_id=None,
                inventory_host_name=None) -> dict:
    if not enabled:
        mapping = {"enabled": False}
    else:
        controller_id = str(controller_id or CONTROLLER_ID)
        controller = get_controller(controller_id)
        if not controller:
            raise ValueError("Ansible controller is not configured")
        inventory_host_name = validate_inventory_host(inventory_host_name)
        if not inventory_host(controller, inventory_host_name):
            raise ValueError("inventory host was not discovered by Ansible")
        mapping = {"enabled": True, "controllerId": controller_id,
                   "inventoryHost": inventory_host_name, "confirmedAt": int(time.time())}

    def mutate(document):
        instance = document["computeInstances"].get(instance_id)
        if not instance:
            raise ValueError("compute instance not found")
        instance["ansible"] = mapping
        return copy.deepcopy(instance)

    return store.update(mutate)


def approved_playbook(record: dict, operation: str) -> tuple[dict, str]:
    metadata = (record.get("playbooks") or {}).get(operation)
    if not metadata or not metadata.get("approved"):
        raise ValueError(f"no approved playbook is configured for {operation}")
    relative = metadata.get("playbook")
    if relative not in (record.get("discoveredPlaybooks") or []):
        raise ValueError("approved playbook is no longer in the discovered allowlist")
    full = _safe_path(str(PurePosixPath(record["playbooksDirectory"]) / relative),
                      "playbook", base=record["playbooksDirectory"])
    return metadata, full


def playbook_command(record: dict, operation: str, target: str,
                     variables: dict | None = None) -> tuple[list[str], dict]:
    metadata, playbook = approved_playbook(record, operation)
    target = validate_inventory_host(target)
    if not inventory_host(record, target):
        raise ValueError("Ansible target is not present in the discovered inventory")
    executable = _executable_path(
        record.get("ansiblePlaybookExecutable"),
        "Ansible Playbook executable", required=True)
    argv = [executable, "-i", record["inventoryPath"], playbook, "--limit", target]
    if variables:
        allowed = {metadata.get("rebootVariable"), metadata.get("projectVariable")}
        if any(key not in allowed or not key for key in variables):
            raise ValueError("playbook variables are not approved")
        argv.extend(["--extra-vars", json.dumps(variables, separators=(",", ":"))])
    return argv, metadata

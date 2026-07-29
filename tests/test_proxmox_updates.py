import contextlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

import device_updates
from drivers.proxmox import ProxmoxVE


class Response:
    def __init__(self, data=None, status=200):
        self.status = status
        self._data = data

    def json(self):
        return {"data": self._data}


class ProxmoxConnection:
    host = "pve-api.example"

    def __init__(self, forbidden=False):
        self.forbidden = forbidden
        self.paths = []

    def get(self, path):
        self.paths.append(path)
        if path == "/api2/json/cluster/resources":
            return Response([
                {"type": "node", "node": "pve/one", "status": "online"},
                {"type": "node", "node": "pve-two", "status": "offline"},
            ])
        if path == "/api2/json/cluster/status":
            return Response([
                {"type": "node", "name": "pve/one", "ip": "192.0.2.10"},
                {"type": "node", "name": "pve-two", "ip": "192.0.2.11"},
            ])
        if path == "/api2/json/nodes/pve%2Fone/apt/update":
            if self.forbidden:
                return Response(status=403)
            return Response([{
                "Package": "pve-manager",
                "OldVersion": "8.2.1",
                "Version": "8.2.2",
                "Title": "Proxmox VE management tools",
                "Section": "admin",
            }])
        raise AssertionError(path)


def test_proxmox_lists_updates_for_online_nodes_and_keeps_install_target_private():
    connection = ProxmoxConnection()

    result = ProxmoxVE().available_updates(connection)

    assert result["total"] == 1
    by_name = {node["node"]: node for node in result["nodes"]}
    assert by_name["pve/one"] == {
        "node": "pve/one",
        "status": "online",
        "_targetHost": "192.0.2.10",
        "packages": [{
            "name": "pve-manager",
            "installed": "8.2.1",
            "available": "8.2.2",
            "description": "Proxmox VE management tools",
            "section": "admin",
        }],
    }
    assert by_name["pve-two"]["packages"] == []
    assert all("pve-two/apt" not in path for path in connection.paths)


def test_proxmox_update_permission_failure_is_not_reported_as_no_updates():
    with pytest.raises(ValueError, match=r"Sys\.Modify"):
        ProxmoxVE().available_updates(ProxmoxConnection(forbidden=True))


class UpdateDriver:
    supports_updates = True

    def available_updates(self, _):
        return {
            "total": 2,
            "nodes": [{
                "node": "pve-one",
                "status": "online",
                "_targetHost": "192.0.2.10",
                "packages": [{"name": "one"}, {"name": "two"}],
            }],
        }


class SSHConnection:
    def __init__(self, commands):
        self.commands = commands

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def run(self, command, timeout):
        self.commands.append((command, timeout))
        return 0, "", ""


class ImmediateThread:
    def __init__(self, target, args, **_):
        self.target = target
        self.args = args

    def start(self):
        self.target(*self.args)


def test_install_job_runs_fixed_apt_commands_and_reaches_completed(monkeypatch):
    driver = UpdateDriver()
    device = {"id": "device-1", "driverId": "proxmox.ve"}
    commands = []

    @contextlib.contextmanager
    def device_conn(*_args, **_kwargs):
        yield device, driver, object()

    monkeypatch.setattr(device_updates.devices, "get_device", lambda _: device)
    monkeypatch.setattr(device_updates.devices, "_drv_for", lambda _: driver)
    monkeypatch.setattr(
        device_updates.devices, "_credentials_for",
        lambda _: {"updateSsh": {"username": "root", "password": "secret", "port": 22}},
    )
    monkeypatch.setattr(device_updates.devices, "device_conn", device_conn)
    monkeypatch.setattr(
        device_updates.transports, "open_connection",
        lambda *_args, **_kwargs: SSHConnection(commands),
    )
    monkeypatch.setattr(device_updates.threading, "Thread", ImmediateThread)
    device_updates._JOBS.clear()

    operation = device_updates.start("device-1")

    assert operation["state"] == "completed"
    assert operation["percent"] == 100
    assert operation["nodes"][0]["state"] == "completed"
    assert [command for command, _ in commands] == [
        "apt-get -q update",
        "DEBIAN_FRONTEND=noninteractive apt-get -y -q "
        "-o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold "
        "dist-upgrade",
    ]


def test_configure_ssh_verifies_root_and_reencrypts_combined_credentials(monkeypatch):
    driver = UpdateDriver()
    device = {
        "id": "device-1",
        "driverId": "proxmox.ve",
        "host": "pve.example",
        "credRef": "credential-1",
    }
    encrypted = []
    document = {
        "devices": {"device-1": device},
        "credentials": {"credential-1": "old"},
    }

    class RootConnection(SSHConnection):
        def run(self, command, timeout):
            assert command == "id -u"
            return 0, "0\n", ""

    monkeypatch.setattr(device_updates.devices, "get_device", lambda _: device)
    monkeypatch.setattr(device_updates.devices, "_drv_for", lambda _: driver)
    monkeypatch.setattr(
        device_updates.devices, "_credentials_for",
        lambda _: {"apiKey": "monitoring-token"},
    )
    monkeypatch.setattr(
        device_updates.transports, "open_connection",
        lambda *args, **kwargs: RootConnection([]),
    )
    monkeypatch.setattr(
        device_updates.crypto, "encrypt",
        lambda value: encrypted.append(value) or "new-encrypted-value",
    )
    monkeypatch.setattr(
        device_updates.store, "update",
        lambda mutator: mutator(document),
    )

    result = device_updates.configure_ssh(
        "device-1", "root", password="root-password", port="2222")

    assert result == {"sshConfigured": True}
    assert encrypted == [{
        "apiKey": "monitoring-token",
        "updateSsh": {
            "username": "root",
            "password": "root-password",
            "port": 2222,
        },
    }]
    assert document["credentials"]["credential-1"] == "new-encrypted-value"


def test_update_catalogue_never_exposes_resolved_ssh_targets(monkeypatch):
    device = {"id": "device-1"}
    driver = UpdateDriver()

    @contextlib.contextmanager
    def device_conn(*_args, **_kwargs):
        yield device, driver, object()

    monkeypatch.setattr(device_updates.devices, "device_conn", device_conn)
    monkeypatch.setattr(device_updates, "_ssh_credentials", lambda _: None)
    device_updates._JOBS.clear()

    result = device_updates.check("device-1")

    assert "_targetHost" not in result["nodes"][0]
    assert result["sshConfigured"] is False
    assert result["operation"] is None

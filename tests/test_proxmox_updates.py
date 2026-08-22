import contextlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

import device_updates
import store
from drivers.proxmox import ProxmoxVE


RUNNING = "6.8.12-8-pve"
TARGET = "6.8.12-9-pve"
BOOT_LIST = f"""Manually selected kernels:
None

Automatically selected kernels:
{RUNNING}
{TARGET}
"""


@pytest.fixture(autouse=True)
def isolated_store(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(store, "DB_FILE", str(tmp_path / "homelabhq.json"))
    monkeypatch.setattr(store, "LOCK_FILE", str(tmp_path / "homelabhq.lock"))
    store._cache.update(doc=None, mtime=None)
    yield
    device_updates._JOBS.clear()
    store._cache.update(doc=None, mtime=None)


def needrestart(status=1, running=RUNNING, expected=RUNNING):
    return ("NEEDRESTART-VER: 3.6\n"
            f"NEEDRESTART-KCUR: {running}\n"
            f"NEEDRESTART-KEXP: {expected}\n"
            f"NEEDRESTART-KSTA: {status}\n")


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
                "Origin": "Debian",
                "Archive": "bookworm-security",
                "Site": "security.debian.org",
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
            "source": "Debian · bookworm-security · security.debian.org",
            "security": True,
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
    def __init__(self, commands, reboot_required=False, responses=None, fail_all=False):
        self.commands = commands
        self.reboot_required = reboot_required
        self.responses = {
            device_updates._RUNNING_KERNEL: (0, f"{RUNNING}\n", ""),
            device_updates._KERNEL_PINS: (0, "", ""),
            device_updates._BOOT_TOOL_KERNEL_LIST: (0, BOOT_LIST, ""),
            device_updates._NEEDRESTART_KERNEL: (0, needrestart(), ""),
            device_updates._INSTALLED_KERNEL_TARGET: (0, f"{RUNNING}\n", ""),
        }
        self.responses.update(responses or {})
        self.fail_all = fail_all

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def run(self, command, timeout):
        self.commands.append((command, timeout))
        if self.fail_all and command not in {device_updates._APT_UPDATE,
                                             device_updates._APT_UPGRADE}:
            raise RuntimeError("synthetic SSH probe failure")
        if command == device_updates._REBOOT_REQUIRED:
            return (0 if self.reboot_required else 1), "", ""
        if command in self.responses:
            return self.responses[command]
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
    monkeypatch.setattr(device_updates, "_persist_maintenance", lambda *_: None)
    monkeypatch.setattr(device_updates, "_persist_node_reboot", lambda *_: None)
    device_updates._JOBS.clear()

    operation = device_updates.start("device-1")

    assert operation["state"] == "completed"
    assert operation["percent"] == 100
    assert operation["nodes"][0]["state"] == "completed"
    assert operation["nodes"][0]["rebootRequired"] is False
    assert operation["nodes"][0]["rebootStatus"] == "not_required"
    assert operation["nodes"][0]["message"] == "Updates installed; no reboot required"
    assert operation["message"] == "All updates installed; no reboot required."
    command_names = [command for command, _ in commands]
    assert command_names[:2] == [device_updates._APT_UPDATE, device_updates._APT_UPGRADE]
    assert device_updates._RUNNING_KERNEL in command_names
    assert device_updates._BOOT_TOOL_KERNEL_LIST in command_names
    assert device_updates._NEEDRESTART_KERNEL in command_names
    assert all(timeout == 30 for command, timeout in commands
               if command not in {device_updates._APT_UPDATE, device_updates._APT_UPGRADE})


def test_install_job_reports_each_node_that_requires_a_reboot(monkeypatch):
    commands = []
    device_updates._JOBS.clear()
    device_updates._JOBS["device-1"] = {
        "id": "job-1", "nodes": [{"node": "pve-one"}],
    }
    monkeypatch.setattr(
        device_updates.transports, "open_connection",
        lambda *_args, **_kwargs: SSHConnection(commands, responses={
            device_updates._NEEDRESTART_KERNEL: (0, needrestart(3, expected=TARGET), ""),
        }),
    )
    monkeypatch.setattr(device_updates, "check", lambda *_: {"total": 0})
    monkeypatch.setattr(
        device_updates, "_persist_node_reboot",
        lambda *_: (_ for _ in ()).throw(RuntimeError("synthetic persistence failure")),
    )

    device_updates._run_install("device-1", "job-1", [{
        "node": "pve-one", "_targetHost": "192.0.2.10",
    }], {"username": "root", "password": "secret"})

    operation = device_updates.status("device-1")
    assert operation["state"] == "completed"
    assert operation["message"] == "Reboot required on pve-one."
    assert operation["nodes"][0]["rebootRequired"] is True
    assert operation["nodes"][0]["reboot"]["runningKernel"] == RUNNING
    assert operation["nodes"][0]["reboot"]["targetKernel"] == TARGET
    assert operation["nodes"][0]["message"] == "Updates installed; reboot required"


def test_reboot_job_rechecks_required_state_and_runs_only_fixed_command(monkeypatch):
    driver = UpdateDriver()
    device = {"id": "device-1", "driverId": "proxmox.ve"}
    commands = []
    persisted = []

    @contextlib.contextmanager
    def device_conn(*_args, **_kwargs):
        yield device, driver, object()

    monkeypatch.setattr(device_updates.devices, "get_device", lambda _: device)
    monkeypatch.setattr(device_updates.devices, "_drv_for", lambda _: driver)
    monkeypatch.setattr(
        device_updates.devices, "update_check_lock",
        lambda _: contextlib.nullcontext(),
    )
    monkeypatch.setattr(
        device_updates.devices, "_credentials_for",
        lambda _: {"updateSsh": {"username": "root", "password": "secret", "port": 22}},
    )
    monkeypatch.setattr(device_updates.devices, "device_conn", device_conn)
    monkeypatch.setattr(
        device_updates.transports, "open_connection",
        lambda *_args, **_kwargs: SSHConnection(commands, responses={
            device_updates._NEEDRESTART_KERNEL: (0, needrestart(3, expected=TARGET), ""),
        }),
    )
    monkeypatch.setattr(device_updates.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(device_updates, "_persist_maintenance", lambda *_: None)
    monkeypatch.setattr(
        device_updates, "_persist_node_reboot",
        lambda device_id, node, reboot: persisted.append((device_id, node, reboot)),
    )
    device_updates._JOBS.clear()

    operation = device_updates.start_reboot(
        "device-1", node="pve-one", confirmed=True)

    assert operation["operationType"] == "reboot"
    assert operation["state"] == "completed"
    assert operation["message"] == "Reboot command sent to pve-one."
    assert commands[-1] == (device_updates._REBOOT_NODE, 30)
    assert sum(command == device_updates._REBOOT_NODE for command, _ in commands) == 1
    assert persisted[0][:2] == ("device-1", "pve-one")
    assert persisted[0][2]["rebootStatus"] == "unknown"
    assert "refresh after the node returns" in persisted[0][2]["reason"]


def test_reboot_requires_confirmation_and_fresh_required_state(monkeypatch):
    with pytest.raises(ValueError, match="explicit confirmation"):
        device_updates.start_reboot("device-1", node="pve-one", confirmed=False)

    driver = UpdateDriver()
    device = {"id": "device-1", "driverId": "proxmox.ve"}

    @contextlib.contextmanager
    def device_conn(*_args, **_kwargs):
        yield device, driver, object()

    monkeypatch.setattr(device_updates.devices, "get_device", lambda _: device)
    monkeypatch.setattr(device_updates.devices, "_drv_for", lambda _: driver)
    monkeypatch.setattr(
        device_updates.devices, "update_check_lock",
        lambda _: contextlib.nullcontext(),
    )
    monkeypatch.setattr(
        device_updates.devices, "_credentials_for",
        lambda _: {"updateSsh": {"username": "root", "password": "secret", "port": 22}},
    )
    monkeypatch.setattr(device_updates.devices, "device_conn", device_conn)
    monkeypatch.setattr(
        device_updates.transports, "open_connection",
        lambda *_args, **_kwargs: SSHConnection([]),
    )
    monkeypatch.setattr(device_updates, "_persist_maintenance", lambda *_: None)
    device_updates._JOBS.clear()

    with pytest.raises(ValueError, match="does not currently require"):
        device_updates.start_reboot("device-1", node="pve-one", confirmed=True)


def test_reboot_status_matches_selected_kernel():
    result = device_updates.reboot_status(SSHConnection([]))

    assert result["rebootStatus"] == "not_required"
    assert result["rebootRequired"] is False
    assert result["runningKernel"] == RUNNING
    assert result["targetKernel"] == RUNNING
    assert result["signals"]["kernelMismatch"] is False


def test_reboot_status_detects_selected_newer_kernel_without_debian_marker():
    connection = SSHConnection([], responses={
        device_updates._NEEDRESTART_KERNEL: (0, needrestart(3, expected=TARGET), ""),
    })

    result = device_updates.reboot_status(connection)

    assert result["rebootStatus"] == "required"
    assert result["rebootRequired"] is True
    assert result["targetKernel"] == TARGET
    assert result["signals"] == {
        "kernelMismatch": True,
        "needrestart": True,
        "rebootRequiredFile": False,
        "bootTool": True,
        "pinnedKernel": False,
    }


def test_reboot_marker_can_require_reboot_for_non_kernel_reason():
    result = device_updates.reboot_status(SSHConnection([], reboot_required=True))

    assert result["rebootStatus"] == "required"
    assert result["signals"]["kernelMismatch"] is False
    assert result["signals"]["rebootRequiredFile"] is True
    assert "Debian reports" in result["reason"]


def test_needrestart_unavailable_uses_dpkg_ordered_installed_kernel_fallback():
    result = device_updates.reboot_status(SSHConnection([], responses={
        device_updates._NEEDRESTART_KERNEL: (127, "", "not found"),
        device_updates._INSTALLED_KERNEL_TARGET: (0, f"{TARGET}\n", ""),
    }))

    assert result["rebootStatus"] == "required"
    assert result["targetKernel"] == TARGET
    assert result["signals"]["needrestart"] is None
    assert "needrestart is unavailable" in result["diagnostics"]


@pytest.mark.parametrize("boot_response", [
    (127, "", "not found"),
    (0, "A future structured format with no known headings\n", ""),
])
def test_boot_tool_unavailable_or_changed_format_falls_back_to_needrestart(boot_response):
    result = device_updates.reboot_status(SSHConnection([], responses={
        device_updates._BOOT_TOOL_KERNEL_LIST: boot_response,
        device_updates._NEEDRESTART_KERNEL: (0, needrestart(3, expected=TARGET), ""),
    }))

    assert result["rebootStatus"] == "required"
    assert result["targetKernel"] == TARGET
    assert result["signals"]["bootTool"] is None


def test_pinned_kernel_wins_over_numerically_newest_installed_kernel():
    pinned_output = f"permanent\t{RUNNING}\n"
    result = device_updates.reboot_status(SSHConnection([], responses={
        device_updates._KERNEL_PINS: (0, pinned_output, ""),
        device_updates._NEEDRESTART_KERNEL: (0, needrestart(3, expected=TARGET), ""),
        device_updates._INSTALLED_KERNEL_TARGET: (0, f"{TARGET}\n", ""),
    }))

    assert result["rebootStatus"] == "not_required"
    assert result["targetKernel"] == RUNNING
    assert result["signals"]["pinnedKernel"] is True


def test_failed_remote_reboot_check_is_unknown_not_no_reboot():
    result = device_updates.reboot_status(SSHConnection([], fail_all=True))

    assert result["rebootStatus"] == "unknown"
    assert result["rebootRequired"] is None
    assert result["signals"]["kernelMismatch"] is None
    assert "Could not determine" in result["reason"]


def test_update_success_is_independent_from_failed_reboot_check(monkeypatch):
    commands = []
    device_updates._JOBS.clear()
    device_updates._JOBS["device-1"] = {
        "id": "job-1", "nodes": [{"node": "pve-one"}],
    }
    monkeypatch.setattr(
        device_updates.transports, "open_connection",
        lambda *_args, **_kwargs: SSHConnection(commands, fail_all=True),
    )
    monkeypatch.setattr(device_updates, "check", lambda *_: {"total": 0})
    monkeypatch.setattr(
        device_updates, "_persist_node_reboot",
        lambda *_: (_ for _ in ()).throw(RuntimeError("synthetic persistence failure")),
    )

    device_updates._run_install("device-1", "job-1", [{
        "node": "pve-one", "_targetHost": "192.0.2.10",
    }], {"username": "root", "password": "secret"})

    operation = device_updates.status("device-1")
    assert operation["state"] == "completed"
    assert operation["nodes"][0]["updateOutcome"] == "succeeded"
    assert operation["nodes"][0]["rebootStatus"] == "unknown"
    assert operation["nodes"][0]["rebootRequired"] is None
    assert "reboot requirement unknown" in operation["nodes"][0]["message"]
    assert "could not be persisted" in operation["nodes"][0]["reboot"]["diagnostics"][-1]


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


def test_update_catalogue_never_exposes_resolved_ssh_targets(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(store, "DB_FILE", str(tmp_path / "homelabhq.json"))
    monkeypatch.setattr(store, "LOCK_FILE", str(tmp_path / "homelabhq.lock"))
    store._cache.update(doc=None, mtime=None)
    device = {"id": "device-1"}
    driver = UpdateDriver()

    @contextlib.contextmanager
    def device_conn(*_args, **_kwargs):
        yield device, driver, object()

    monkeypatch.setattr(device_updates.devices, "device_conn", device_conn)
    monkeypatch.setattr(device_updates, "_ssh_credentials", lambda _: None)
    monkeypatch.setattr(device_updates, "_persist_maintenance", lambda *_: None)
    device_updates._JOBS.clear()

    result = device_updates.check("device-1")

    assert "_targetHost" not in result["nodes"][0]
    assert result["sshConfigured"] is False
    assert result["operation"] is None


def test_successful_package_list_survives_a_later_refresh_failure():
    store.update(lambda document: document["devices"].update({
        "device-1": {"id": "device-1"},
    }))
    catalogue = {"total": 1, "nodes": [{
        "node": "pve-one", "status": "online", "_targetHost": "192.0.2.10",
        "packages": [{"name": "pve-manager", "installed": "8.2.1",
                      "available": "8.2.2", "source": "Proxmox"}],
        "reboot": {"rebootStatus": "not_required", "rebootRequired": False},
    }]}

    device_updates._persist_maintenance("device-1", catalogue, {"username": "root"})
    device_updates.record_refresh_failure("device-1", "Proxmox API unavailable")

    maintenance = store.load()["devices"]["device-1"]["proxmoxMaintenance"]
    assert maintenance["nodes"]["pve-one"]["packages"] == catalogue["nodes"][0]["packages"]
    assert maintenance["nodes"]["pve-one"]["updateCount"] == 1
    assert maintenance["refreshError"] == "Proxmox API unavailable"
    assert maintenance["refreshFailedAt"] is not None
    assert maintenance["nodes"]["pve-one"]["reboot"] == catalogue["nodes"][0]["reboot"]


def test_persisted_running_job_becomes_interrupted_after_backend_restart():
    operation = {
        "id": "job-before-restart", "deviceId": "device-1", "operationType": "update",
        "state": "running", "stage": "installing", "progressMode": "indeterminate",
        "percent": None, "startedAt": 100, "updatedAt": 101, "finishedAt": None,
        "message": "Installing updates on pve-one", "nodes": [{
            "taskId": "job-before-restart", "node": "pve-one", "state": "running",
            "stage": "installing", "progressMode": "indeterminate", "percent": None,
            "message": "Installing updates", "updateOutcome": "pending",
        }],
    }
    store.update(lambda document: document["devices"].update({
        "device-1": {"id": "device-1", "proxmoxUpdateOperation": operation},
    }))
    device_updates._JOBS.clear()

    restored = device_updates.status("device-1")

    assert restored["id"] == "job-before-restart"
    assert restored["state"] == "failed"
    assert restored["stage"] == "interrupted"
    assert restored["nodes"][0]["state"] == "failed"
    assert "backend restart" in restored["nodes"][0]["message"]
    assert store.load()["devices"]["device-1"]["proxmoxUpdateOperation"] == restored


def test_progress_updates_require_matching_task_id(monkeypatch):
    persisted = []
    monkeypatch.setattr(device_updates, "_persist_operation",
                        lambda _device_id, operation: persisted.append(operation))
    device_updates._JOBS["device-1"] = {
        "id": "active-task", "state": "running", "updatedAt": 100,
        "nodes": [{"taskId": "active-task", "node": "pve-one", "state": "pending"}],
    }

    stale = device_updates._set_node(
        "device-1", "stale-task", 0, state="running", stage="installing")
    current = device_updates._set_node_progress(
        "device-1", "active-task", 0, stage="installing", message="Installing updates")

    assert stale is None
    assert current["nodes"][0]["stage"] == "installing"
    assert current["nodes"][0]["percent"] is None
    assert current["nodes"][0]["progressMode"] == "indeterminate"
    assert len(persisted) == 1


def test_install_exposes_only_real_indeterminate_stages(monkeypatch):
    snapshots = []
    device_updates._JOBS["device-1"] = {
        "id": "job-1", "state": "running", "stage": "preparing",
        "startedAt": 100, "updatedAt": 100,
        "nodes": [{"taskId": "job-1", "node": "pve-one", "state": "pending"}],
    }
    monkeypatch.setattr(device_updates, "_persist_operation",
                        lambda _device_id, operation: snapshots.append(operation))
    monkeypatch.setattr(
        device_updates.transports, "open_connection",
        lambda *_args, **_kwargs: SSHConnection([]),
    )
    monkeypatch.setattr(device_updates, "check", lambda *_: {"total": 0})
    monkeypatch.setattr(device_updates, "_persist_node_reboot", lambda *_: None)

    device_updates._run_install("device-1", "job-1", [{
        "node": "pve-one", "_targetHost": "192.0.2.10",
    }], {"username": "root", "password": "secret"})

    stages = [snapshot["nodes"][0].get("stage") for snapshot in snapshots]
    assert [stage for stage in ("preparing", "downloading", "installing",
                                "checking_reboot_status") if stage in stages] == [
        "preparing", "downloading", "installing", "checking_reboot_status"]
    active = [snapshot["nodes"][0] for snapshot in snapshots
              if snapshot["nodes"][0].get("stage") in {
                  "preparing", "downloading", "installing", "checking_reboot_status"}]
    assert all(node["progressMode"] == "indeterminate" and node["percent"] is None
               for node in active)
    assert snapshots[-1]["state"] == "completed"
    assert snapshots[-1]["percent"] == 100

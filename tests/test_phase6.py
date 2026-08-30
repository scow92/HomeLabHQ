import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import push
import store
import auth
import client_roster


def discovery_job(job_id, instance_id, *, state="successful", created_at=1,
                  finished_at=2):
    return {
        "id": job_id,
        "computeInstanceId": instance_id,
        "controllerId": "synthetic-controller",
        "ansibleTarget": "synthetic-target",
        "operation": "docker_discovery",
        "state": state,
        "createdAt": created_at,
        "startedAt": created_at,
        "finishedAt": finished_at,
        "durationSeconds": 1,
        "requestedBy": "synthetic-user",
        "summary": f"Summary for {job_id}",
        "recap": {"synthetic-target": {"ok": 1, "changed": 0}},
        "stdout": f"stdout for {job_id}",
        "stderr": f"stderr for {job_id}",
        "structuredResult": {"homelabhq_docker": {"available": True}},
        "detailsRetained": True,
    }


def configure_store(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(store, "DB_FILE", str(tmp_path / "homelabhq.json"))
    monkeypatch.setattr(store, "LOCK_FILE", str(tmp_path / "homelabhq.lock"))
    store._cache.update(doc=None, mtime=None)


def test_startup_integrity_check_migrates_legacy_document_once(monkeypatch, tmp_path):
    configure_store(monkeypatch, tmp_path)
    Path(store.DB_FILE).parent.mkdir(parents=True, exist_ok=True)
    Path(store.DB_FILE).write_text(json.dumps({"users": {}}))

    first = store.startup_integrity_check()
    migrated = json.loads(Path(store.DB_FILE).read_text())
    second = store.startup_integrity_check()

    assert first["schemaVersion"] == store.SCHEMA_VERSION
    assert first["migrated"] is True
    assert store.metrics()["last_document_bytes"] == first["documentBytes"]
    assert migrated["schemaVersion"] == store.SCHEMA_VERSION
    assert set(store._DEFAULT_DOC).issubset(migrated)
    assert second["migrated"] is False


def test_startup_integrity_check_preserves_invalid_or_newer_documents(monkeypatch, tmp_path):
    configure_store(monkeypatch, tmp_path)
    Path(store.DB_FILE).parent.mkdir(parents=True, exist_ok=True)
    for document in (b"{not JSON", json.dumps({"schemaVersion": store.SCHEMA_VERSION + 1}).encode()):
        Path(store.DB_FILE).write_bytes(document)
        store._cache.update(doc=None, mtime=None)
        with pytest.raises(store.StoreError):
            store.startup_integrity_check()
        assert Path(store.DB_FILE).read_bytes() == document


def test_compute_discovery_compaction_clears_only_superseded_success_details():
    older = discovery_job("job-old", "instance-a", created_at=1, finished_at=2)
    newer = discovery_job("job-new", "instance-a", created_at=3, finished_at=4)
    failed = discovery_job("job-failed", "instance-a", state="failed")
    incomplete = discovery_job("job-incomplete", "instance-a", state="incomplete")
    non_discovery = discovery_job("job-other", "instance-a")
    non_discovery["operation"] = "os_check"
    document = {"computeJobs": {
        job["id"]: job for job in (older, newer, failed, incomplete, non_discovery)
    }}
    audit_fields = {
        key: copy.deepcopy(older[key]) for key in (
            "id", "computeInstanceId", "controllerId", "ansibleTarget", "operation",
            "state", "createdAt", "startedAt", "finishedAt", "durationSeconds",
            "requestedBy", "summary", "recap",
        )
    }

    assert store.compact_compute_discovery_history(document) == 1

    assert older["stdout"] == ""
    assert older["stderr"] == ""
    assert older["structuredResult"] is None
    assert older["detailsRetained"] is False
    assert {key: older[key] for key in audit_fields} == audit_fields
    for retained in (newer, failed, incomplete, non_discovery):
        assert retained["stdout"]
        assert retained["stderr"]
        assert retained["structuredResult"] is not None
        assert retained["detailsRetained"] is True


def test_compute_discovery_compaction_retains_newest_success_per_instance():
    jobs = {
        "a-old": discovery_job("a-old", "instance-a", created_at=1, finished_at=2),
        "a-new": discovery_job("a-new", "instance-a", created_at=2, finished_at=3),
        "b-old": discovery_job("b-old", "instance-b", created_at=1, finished_at=2),
        "b-new": discovery_job("b-new", "instance-b", created_at=2, finished_at=3),
    }

    assert store.compact_compute_discovery_history({"computeJobs": jobs}) == 2

    assert jobs["a-old"]["detailsRetained"] is False
    assert jobs["b-old"]["detailsRetained"] is False
    for job_id in ("a-new", "b-new"):
        assert jobs[job_id]["detailsRetained"] is True
        assert jobs[job_id]["stdout"]
        assert jobs[job_id]["structuredResult"] is not None


def test_compute_discovery_compaction_order_is_deterministic_and_idempotent():
    jobs = {
        "latest-finish": discovery_job(
            "latest-finish", "instance-a", created_at=1, finished_at=4),
        "earlier-created": discovery_job(
            "earlier-created", "instance-a", created_at=2, finished_at=4),
        "job-a": discovery_job("job-a", "instance-a", created_at=3, finished_at=4),
        "job-z": discovery_job("job-z", "instance-a", created_at=3, finished_at=4),
    }
    document = {"computeJobs": jobs}

    assert store.compact_compute_discovery_history(document) == 3
    after_first_pass = copy.deepcopy(document)
    assert jobs["job-z"]["detailsRetained"] is True
    assert all(jobs[job_id]["detailsRetained"] is False for job_id in (
        "latest-finish", "earlier-created", "job-a"))

    assert store.compact_compute_discovery_history(document) == 0
    assert document == after_first_pass


def test_compute_discovery_compaction_handles_missing_legacy_optional_fields():
    jobs = {
        "legacy-a": {
            "id": "legacy-a", "computeInstanceId": "instance-a",
            "operation": "docker_discovery", "state": "successful",
            "stdout": "older details",
        },
        "legacy-z": {
            "id": "legacy-z", "computeInstanceId": "instance-a",
            "operation": "docker_discovery", "state": "successful",
            "stdout": "newer details",
        },
        "legacy-unscoped": {
            "id": "legacy-unscoped", "operation": "docker_discovery",
            "state": "successful", "stdout": "unscoped details",
        },
        "legacy-invalid": None,
    }

    assert store.compact_compute_discovery_history({"computeJobs": jobs}) == 1

    assert jobs["legacy-a"]["detailsRetained"] is False
    assert jobs["legacy-z"]["stdout"] == "newer details"
    assert jobs["legacy-unscoped"]["stdout"] == "unscoped details"


def test_v7_migration_compacts_a_synthetic_duplicated_payload():
    document = copy.deepcopy(store._DEFAULT_DOC)
    document["schemaVersion"] = 7
    document["computeJobs"] = {
        f"job-{index}": discovery_job(
            f"job-{index}", "instance-a", created_at=index, finished_at=index)
        for index in range(4)
    }
    for job in document["computeJobs"].values():
        job["stdout"] = "synthetic diagnostic output\n" * 400
        job["stderr"] = "synthetic diagnostic error\n" * 400
        job["structuredResult"] = {
            "homelabhq_docker": {"synthetic": "projection-data" * 400}}
    before_bytes = len(json.dumps(document).encode())

    migrated, changed = store._migrate_doc(document)
    after_bytes = len(json.dumps(migrated).encode())

    assert changed is True
    assert migrated["schemaVersion"] == 8
    assert sum(job.get("detailsRetained") is False
               for job in migrated["computeJobs"].values()) == 3
    assert migrated["computeJobs"]["job-3"]["structuredResult"] is not None
    assert after_bytes < before_bytes // 2


def test_noop_mutator_does_not_rewrite_the_document(monkeypatch, tmp_path):
    configure_store(monkeypatch, tmp_path)
    store.update(lambda document: document["meta"].update(value=1))
    before = Path(store.DB_FILE).read_bytes()
    writes = store.metrics()["writes"]

    assert store.update(lambda document: "unchanged") == "unchanged"

    assert Path(store.DB_FILE).read_bytes() == before
    assert store.metrics()["writes"] == writes
    assert store.metrics()["no_op_updates"] >= 1


def test_batch_update_commits_related_records_together(monkeypatch, tmp_path):
    configure_store(monkeypatch, tmp_path)

    def add_device_and_credential(document):
        document["credentials"]["credential-1"] = "encrypted"
        document["devices"]["device-1"] = {"id": "device-1", "credRef": "credential-1"}

    store.batch_update(add_device_and_credential)
    document = store.load()
    assert document["devices"]["device-1"]["credRef"] == "credential-1"
    assert document["credentials"]["credential-1"] == "encrypted"


def test_ssh_host_key_records_are_bounded(monkeypatch, tmp_path):
    configure_store(monkeypatch, tmp_path)
    monkeypatch.setattr(store, "MAX_SSH_HOST_KEYS", 2)
    for host in ("one", "two", "three"):
        store.pin_ssh_host_key(host, 22, "ssh-ed25519", host)
    keys = store.load()["sshHostKeys"]
    assert len(keys) == 2
    assert "one:22" not in keys


def test_push_subscriptions_are_bounded_per_user(monkeypatch, tmp_path):
    configure_store(monkeypatch, tmp_path)
    monkeypatch.setattr(push, "MAX_PUSH_SUBSCRIPTIONS_PER_USER", 2)
    for endpoint in ("https://push/one", "https://push/two", "https://push/three"):
        push.subscribe("alice", {"endpoint": endpoint, "keys": {}})
    subscriptions = store.load()["push_subs"]
    assert len(subscriptions) == 2
    assert "https://push/one" not in subscriptions


def test_sessions_are_bounded_after_expired_sessions_are_swept(monkeypatch, tmp_path):
    configure_store(monkeypatch, tmp_path)
    monkeypatch.setattr(auth, "MAX_SESSIONS", 2)
    auth.create_user("alice", "a-secure-test-password")
    for _ in range(3):
        token, user = auth.login("alice", "a-secure-test-password")
        assert token and user["username"] == "alice"
    assert len(store.load()["sessions"]) == 2


def test_offline_roster_records_follow_the_retention_policy(monkeypatch, tmp_path):
    configure_store(monkeypatch, tmp_path)
    monkeypatch.setattr(client_roster, "CLIENT_RECORD_RETENTION_DAYS", 1)
    store.update(lambda document: document["clientRosters"].update({
        "alice": {"AA:BB:CC:DD:EE:01": {
            "firstSeen": 1, "lastSeen": 1, "online": False,
        }},
    }))

    client_roster.record_observations("alice", [])

    assert store.load()["clientRosters"]["alice"] == {}

"""Host metadata/control/retention tests; no production connections."""

from __future__ import annotations

import copy
import hashlib
import json
import os
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest

from scripts.deployment import backup_control_host as host

NOW = datetime(2026, 10, 10, 12, tzinfo=UTC)


class Session:
    def __init__(self, value):
        self.value = value
        self.swaps = 0
        self.identity = "14:14"

    def read(self):
        return copy.deepcopy(self.value)

    def compare_and_swap(self, old, new):
        assert old == self.value
        assert new["version"] == old["version"] + 1
        self.value = copy.deepcopy(new)
        self.swaps += 1

    def sql(self, sql):
        return [self.identity]


@pytest.fixture
def environment(tmp_path, monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, timezone=None):
            return NOW

    monkeypatch.setattr(host, "datetime", Clock)
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    monkeypatch.setattr(host.backup, "DIRECTORY", directory)
    monkeypatch.setattr(host.backup, "secure_directory", lambda path: None)
    monkeypatch.setattr(host.backup, "read_artifact", lambda path: path.read_bytes())
    monkeypatch.setattr(
        host.backup.monitor, "protected_read", lambda path: json.loads(path.read_text())
    )
    monkeypatch.setattr(host, "fsync_directory", lambda path: None)
    monkeypatch.setattr(host, "publish", lambda value: None)
    session = Session(dict(host.DEFAULT))

    @contextmanager
    def transaction():
        yield session

    monkeypatch.setattr(host, "transaction", transaction)
    return directory, session


def create_copy(directory, identifier, age=0):
    snapshot = NOW - timedelta(days=age)
    path = directory / (
        "backup-" + snapshot.strftime("%Y%m%dT%H%M%SZ") + "-" + identifier + ".tlbk"
    )
    raw = b"TLBK1" + identifier.encode() + b"ciphertext"
    path.write_bytes(raw)
    path.chmod(0o600)
    receipt = {
        "status": "PASS",
        "restore": "PASS",
        "artifact": path.name,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
        "schema": host.backup.SCHEMA,
        "postgres_image_id": host.backup.PG_IMAGE,
        "tables_checked": 28,
        "constraint_validation": "PASS",
        "started_at": snapshot.isoformat(),
        "verified_at": snapshot.isoformat(),
    }
    receipt_path = path.with_name(path.name + ".receipt.json")
    receipt_path.write_text(json.dumps(receipt))
    receipt_path.chmod(0o600)
    return path


def request(value, known, kind="verify", artifact_id="a" * 32):
    result = copy.deepcopy(value)
    result["request"] = {
        "request_id": "b" * 32,
        "kind": kind,
        "artifact_id": artifact_id,
        "catalogue_version": host.catalogue(value, known, NOW)["version"],
        "expected_active_revision": 14,
        "expected_image": host.IMAGE,
        "requested_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(minutes=15)).isoformat(),
        "status": "PENDING",
        "result": {},
        "claimed_at": None,
        "finished_at": None,
    }
    return result


def test_default_control_no_db_mutation():
    assert host.control(None) == {"version": 0, "retention_days": 7, "request": None}


@pytest.mark.parametrize("days", [0, 91, True, "7"])
def test_policy_bounds(days):
    with pytest.raises(ValueError):
        host.control({"version": 0, "retention_days": days, "request": None})


def test_catalogue_stable_fresh_time_only_and_private_content_absent(environment):
    directory, session = environment
    create_copy(directory, "a" * 32)
    known = host.records(NOW)
    first = host.catalogue(session.value, known, NOW)
    second = host.catalogue(session.value, known, NOW + timedelta(minutes=1))
    assert first["version"] == second["version"]
    assert first["generated_at"] != second["generated_at"]
    raw = json.dumps(first)
    assert str(directory) not in raw and "ciphertext" not in raw
    assert set(first["entries"][0]) == {
        "id",
        "snapshot_at",
        "verified_at",
        "size_bytes",
        "sha256",
        "schema",
        "restore",
    }
    session.value["retention_days"] = 8
    assert host.catalogue(session.value, known, NOW)["version"] != first["version"]


def test_corrupt_unknown_unpaired_receipts_do_not_enter_catalogue(environment):
    directory, _ = environment
    valid = create_copy(directory, "a" * 32)
    corrupt = create_copy(directory, "c" * 32)
    corrupt.write_bytes(b"corrupted")
    orphan = create_copy(directory, "d" * 32)
    orphan.with_name(orphan.name + ".receipt.json").unlink()
    (directory / "unrelated.tlbk").write_text("unchanged")
    assert set(host.records(NOW)) == {"a" * 32}
    assert valid.exists() and corrupt.exists() and orphan.exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("restore", "FAILED"),
        ("status", "REJECTED"),
        ("tables_checked", 27),
        ("constraint_validation", "FAILED"),
    ],
)
def test_failed_or_partial_receipts_never_deleted_or_catalogued(environment, field, value):
    directory, _ = environment
    failed = create_copy(directory, "a" * 32, 30)
    receipt_path = failed.with_name(failed.name + ".receipt.json")
    receipt = json.loads(receipt_path.read_text())
    receipt[field] = value
    receipt_path.write_text(json.dumps(receipt))
    create_copy(directory, "b" * 32)
    assert set(host.records(NOW)) == {"b" * 32}
    assert host.retention(NOW)["deleted"] == 0
    assert failed.exists() and receipt_path.exists()


def test_retention_deletes_only_expired_success_preserves_newest_selected(environment):
    directory, session = environment
    expired = create_copy(directory, "a" * 32, 9)
    selected = create_copy(directory, "b" * 32, 8)
    recent = create_copy(directory, "c" * 32, 1)
    newest = create_copy(directory, "d" * 32, 0)
    invalid = create_copy(directory, "e" * 32, 10)
    invalid.write_bytes(b"corrupt")
    session.value = request(session.value, host.records(NOW), "restore", "b" * 32)
    session.value["request"]["status"] = "REQUIRES_OPERATOR"
    session.value["request"]["claimed_at"] = NOW.isoformat()
    session.value["request"]["finished_at"] = NOW.isoformat()
    result = host.retention(NOW)
    assert result["deleted"] == 1 and not expired.exists()
    assert not expired.with_name(expired.name + ".receipt.json").exists()
    assert all(path.exists() for path in (selected, recent, newest, invalid))


def test_retention_never_deletes_last_valid_copy(environment):
    directory, _ = environment
    last = create_copy(directory, "a" * 32, 30)
    assert host.retention(NOW)["deleted"] == 0
    assert last.exists()


@pytest.mark.parametrize("status", ["PENDING", "CLAIMED", "REQUIRES_OPERATOR"])
def test_selected_inflight_retention_protection(environment, status):
    directory, session = environment
    selected = create_copy(directory, "a" * 32, 30)
    create_copy(directory, "b" * 32)
    session.value = request(session.value, host.records(NOW), artifact_id="a" * 32)
    session.value["request"]["status"] = status
    if status != "PENDING":
        session.value["request"]["claimed_at"] = NOW.isoformat()
    if status == "REQUIRES_OPERATOR":
        session.value["request"]["kind"] = "restore"
        session.value["request"]["finished_at"] = NOW.isoformat()
    assert host.retention(NOW)["deleted"] == 0
    assert selected.exists()


def test_verify_is_single_claim_and_sanitized_result(environment, monkeypatch):
    directory, session = environment
    create_copy(directory, "a" * 32)
    known = host.records(NOW)
    session.value = request(session.value, known)
    monkeypatch.setattr(host.backup, "identity", lambda: host.backup.PG_IMAGE)
    monkeypatch.setattr(host.backup, "crypt", lambda raw, operation: b"PGDMPfixture")
    restore = Mock(return_value={"tables_checked": 28, "sensitive": "must not propagate"})
    monkeypatch.setattr(host.backup, "restore", restore)
    assert host.process_request(known, NOW) == "PASS"
    assert session.swaps == 2 and session.value["version"] == 2
    assert host.process_request(known, NOW) == "NO_PENDING_REQUEST"
    restore.assert_called_once()
    result = session.value["request"]["result"]
    assert result["table_count"] == 28 and "sensitive" not in result


def test_restore_request_requires_operator_never_restores_production(environment, monkeypatch):
    directory, session = environment
    create_copy(directory, "a" * 32)
    known = host.records(NOW)
    session.value = request(session.value, known, "restore")
    monkeypatch.setattr(host.backup, "identity", lambda: host.backup.PG_IMAGE)
    forbidden = Mock(side_effect=AssertionError("Never actual restore"))
    monkeypatch.setattr(host.backup, "restore", forbidden)
    assert host.process_request(known, NOW) == "REQUIRES_OPERATOR"
    forbidden.assert_not_called()


@pytest.mark.parametrize(
    "fault,reason",
    [
        ("expired", "EXPIRED"),
        ("revision", "IDENTITY_CHANGED"),
        ("catalogue", "CATALOGUE_CHANGED"),
        ("unknown", "ARTIFACT_INVALID"),
    ],
)
def test_pending_requests_fail_closed(environment, monkeypatch, fault, reason):
    directory, session = environment
    create_copy(directory, "a" * 32)
    known = host.records(NOW)
    session.value = request(session.value, known)
    if fault == "expired":
        session.value["request"]["requested_at"] = (NOW - timedelta(minutes=16)).isoformat()
        session.value["request"]["expires_at"] = (NOW - timedelta(minutes=1)).isoformat()
    elif fault == "revision":
        session.identity = "15:15"
    elif fault == "catalogue":
        session.value["request"]["catalogue_version"] = "c" * 64
    else:
        session.value["request"]["artifact_id"] = "f" * 32
    restore = Mock()
    monkeypatch.setattr(host.backup, "restore", restore)
    assert host.process_request(known, NOW) == "REJECTED"
    assert session.value["request"]["result"]["reason_code"] == reason
    restore.assert_not_called()


def test_post_claim_corruption_rejects(environment, monkeypatch):
    directory, session = environment
    path = create_copy(directory, "a" * 32)
    known = host.records(NOW)
    session.value = request(session.value, known)
    path.write_bytes(b"corruption after catalogue")
    assert host.process_request(known, NOW) == "REJECTED"
    assert session.value["request"]["result"] == {"reason_code": "VERIFY_REJECTED"}


@pytest.mark.skipif(os.name != "posix", reason="Linux public catalogue protections")
def test_public_catalogue_readonly_and_symlink_rejected(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    path = tmp_path / "backup-catalogue.json"
    monkeypatch.setattr(host, "PUBLIC_DIRECTORY", tmp_path)
    monkeypatch.setattr(host, "CATALOGUE", path)
    value = host.catalogue(dict(host.DEFAULT), {}, NOW)
    host.publish(value)
    assert path.stat().st_mode & 0o777 == 0o444
    assert json.loads(path.read_text()) == value
    path.unlink()
    target = tmp_path / "unrelated"
    target.write_text("unchanged")
    path.symlink_to(target)
    with pytest.raises(ValueError):
        host.publish(value)
    assert target.read_text() == "unchanged"


def test_no_plaintext_or_secret_on_poll_failure(monkeypatch, capsys):
    monkeypatch.setattr(host, "poll", Mock(side_effect=ValueError("secret database payload")))
    assert host.main() == 2
    assert "secret database payload" not in capsys.readouterr().out


def test_shared_repository_validator_accepts_claim_finish_contract(environment):
    from time_tl.infrastructure.db.admin import _backup_control

    directory, session = environment
    create_copy(directory, "a" * 32)
    initial = request(session.value, host.records(NOW))
    claimed = host.changed_request(initial, "CLAIMED", {}, NOW)
    finished = host.changed_request(
        claimed,
        "PASS",
        {
            "reason_code": "VERIFY_PASSED",
            "artifact_sha256": "a" * 64,
            "size_bytes": 100,
            "schema": host.backup.SCHEMA,
            "table_count": 28,
            "verified_at": NOW.isoformat(),
        },
        NOW,
    )
    assert _backup_control(claimed) == claimed
    assert _backup_control(finished) == finished


def test_shared_ui_catalogue_reader_accepts_producer(tmp_path, environment):
    from time_tl.admin.backups import BackupCatalogue

    directory, session = environment
    create_copy(directory, "a" * 32)
    value = host.catalogue(session.value, host.records(NOW), NOW)
    public = tmp_path / "catalogue.json"
    public.write_bytes(host.canonical(value))
    assert BackupCatalogue(public).read(NOW) == value


def test_compare_and_swap_conflict_does_not_write(monkeypatch):
    session = object.__new__(host.DatabaseSession)
    expected = dict(host.DEFAULT)
    monkeypatch.setattr(
        session, "read", lambda: {"version": 1, "retention_days": 8, "request": None}
    )
    write = Mock()
    monkeypatch.setattr(session, "sql", write)
    with pytest.raises(ValueError, match="conflict"):
        session.compare_and_swap(expected, {"version": 1, "retention_days": 7, "request": None})
    write.assert_not_called()


def test_unknown_result_and_invalid_lifecycle_rejected(environment):
    directory, session = environment
    create_copy(directory, "a" * 32)
    value = request(session.value, host.records(NOW))
    value["request"]["result"] = {"raw_database_text": "secret"}
    with pytest.raises(ValueError):
        host.control(value)
    value["request"]["result"] = {}
    value["request"]["status"] = "CLAIMED"
    with pytest.raises(ValueError):
        host.control(value)


@pytest.mark.skipif(os.name != "posix", reason="Linux private artifact protections")
def test_retention_does_not_delete_symlinks_bad_permissions_or_unknown(tmp_path, monkeypatch):
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    monkeypatch.setattr(host.backup, "DIRECTORY", directory)
    session = Session(dict(host.DEFAULT))

    @contextmanager
    def transaction():
        yield session

    monkeypatch.setattr(host, "transaction", transaction)
    monkeypatch.setattr(host, "publish", lambda value: None)
    newest = create_copy(directory, "a" * 32)
    bad = create_copy(directory, "b" * 32, 10)
    bad.chmod(0o640)
    target = directory / "unrelated"
    target.write_text("unchanged")
    link = directory / ("backup-20260901T120000Z-" + "c" * 32 + ".tlbk")
    link.symlink_to(target)
    assert host.retention(NOW)["deleted"] == 0
    assert newest.exists() and bad.exists() and link.is_symlink()
    assert target.read_text() == "unchanged"

"""No server contacts: backup rejection and publication invariants."""

import json
import os
from unittest.mock import Mock

import pytest

from scripts.deployment import backup_control_host
from scripts.deployment import verified_backup as backup


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    monkeypatch.setattr(backup, "DIRECTORY", tmp_path)
    monkeypatch.setattr(backup, "secure_directory", lambda path: None)
    monkeypatch.setattr(backup, "identity", lambda: "sha256:" + "a" * 64)
    monkeypatch.setattr(
        backup,
        "command",
        lambda args, **kw: backup.SCHEMA.encode() if "-Atc" in args else b"PGDMPfixture",
    )
    monkeypatch.setattr(
        backup,
        "crypt",
        lambda raw, operation: b"TLBK1" + b"x" * 40 if operation == "encrypt" else b"PGDMPfixture",
    )
    monkeypatch.setattr(backup, "read_artifact", lambda path: path.read_bytes())
    if os.name != "posix":
        monkeypatch.setattr(backup, "write_artifact", lambda path, raw: path.write_bytes(raw))
    monkeypatch.setattr(backup, "durable_json", backup.monitor.atomic_write)
    monkeypatch.setattr(backup_control_host, "retention", lambda now: {"deleted": 0})
    monkeypatch.setattr(
        backup, "restore", lambda *args: {"restore": "PASS", "schema": backup.SCHEMA}
    )
    publish = Mock()
    monkeypatch.setattr(backup, "publish_timestamp", publish)
    return tmp_path, publish


def test_success_rechecks_persisted_artifact_and_receipt_before_timestamp(sandbox, monkeypatch):
    directory, publish = sandbox
    reads = []

    def reread(path):
        reads.append(path)
        return path.read_bytes()

    monkeypatch.setattr(backup, "read_artifact", reread)
    result = backup.backup()
    assert reads == [directory / result["artifact"]]
    receipt = json.loads((directory / (result["artifact"] + ".receipt.json")).read_text())
    assert receipt["restore"] == "PASS"
    publish.assert_called_once_with(result["started_at"])
    assert list(directory.glob("*.tlbk"))


@pytest.mark.parametrize(
    "stage", ["dump", "encrypt", "write", "read", "decrypt", "restore", "receipt"]
)
def test_every_failed_stage_leaves_timestamp_unchanged(stage, sandbox, monkeypatch):
    _, publish = sandbox

    def fail(*args, **kwargs):
        raise RuntimeError("injected secret must not print")

    if stage == "dump":
        monkeypatch.setattr(backup, "command", fail)
    elif stage in ("encrypt", "decrypt"):
        original = backup.crypt
        monkeypatch.setattr(
            backup, "crypt", lambda raw, op: fail() if op == stage else original(raw, op)
        )
    elif stage == "receipt":
        monkeypatch.setattr(backup, "durable_json", fail)
    else:
        monkeypatch.setattr(
            backup,
            {"write": "write_artifact", "read": "read_artifact", "restore": "restore"}[stage],
            fail,
        )
    with pytest.raises(RuntimeError):
        backup.backup()
    publish.assert_not_called()


def test_readback_corruption_does_not_publish(sandbox, monkeypatch):
    _, publish = sandbox
    monkeypatch.setattr(backup, "read_artifact", lambda path: b"TLBK1bad")
    with pytest.raises(ValueError, match="Persisted"):
        backup.backup()
    publish.assert_not_called()


def test_backup_schema_drift_never_advances_guard_or_runs_retention(sandbox, monkeypatch):
    _, publish = sandbox
    monkeypatch.setattr(
        backup,
        "command",
        lambda args, **kw: b"0009_calendar_foundation" if "-Atc" in args else b"PGDMPfixture",
    )
    retention = Mock(side_effect=AssertionError("must not delete retained copies"))
    monkeypatch.setattr(backup_control_host, "retention", retention)
    with pytest.raises(ValueError, match="Schema changed"):
        backup.backup()
    publish.assert_not_called()
    retention.assert_not_called()


def test_restore_rejects_unsupported_expected_schema_before_container(monkeypatch):
    command = Mock(side_effect=AssertionError("no container"))
    monkeypatch.setattr(backup, "command", command)
    with pytest.raises(ValueError, match="Unsupported"):
        backup.restore(b"PGDMPfixture", backup.PG_IMAGE, expected_schema="future_schema")
    command.assert_not_called()


def test_named_table_contract_has_exact_old_and_new_tables():
    assert len(backup.LEGACY_TABLES) == 28
    assert len(backup.SCHEMA_TABLES["0009_calendar_foundation"]) == 31
    assert backup.SCHEMA_TABLES["0009_calendar_foundation"] - backup.LEGACY_TABLES == {
        "calendars",
        "calendar_memberships",
        "calendar_invitations",
    }


@pytest.mark.parametrize("content", [b"", b"TLBK1", b"wrong" + b"x" * 40])
@pytest.mark.skipif(os.name != "posix", reason="Linux O_NOFOLLOW/ownership")
def test_truncated_artifacts_fail(tmp_path, monkeypatch, content):
    tmp_path.chmod(0o700)
    monkeypatch.setattr(backup, "DIRECTORY", tmp_path)
    path = tmp_path / ("backup-20261002T120000Z-" + "a" * 32 + ".tlbk")
    path.write_bytes(content)
    path.chmod(0o600)
    with pytest.raises(ValueError):
        backup.read_artifact(path)


@pytest.mark.skipif(os.name != "posix", reason="Linux permissions")
def test_symlink_and_bad_modes_rejected(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    monkeypatch.setattr(backup, "DIRECTORY", tmp_path)
    path = tmp_path / ("backup-20261002T120000Z-" + "a" * 32 + ".tlbk")
    path.write_bytes(b"TLBK1" + b"x" * 40)
    path.chmod(0o640)
    with pytest.raises(ValueError):
        backup.read_artifact(path)
    path.unlink()
    target = tmp_path / "other"
    target.write_bytes(b"TLBK1" + b"x" * 40)
    target.chmod(0o600)
    path.symlink_to(target)
    with pytest.raises(OSError):
        backup.read_artifact(path)


def test_restore_cleanup_on_run_timeout(monkeypatch):
    calls = []

    def run(args, *rest, **kwargs):
        calls.append(args)
        if args[1] == "run":
            raise TimeoutError()
        return b""

    monkeypatch.setattr(backup, "command", run)
    with pytest.raises(TimeoutError):
        backup.restore(b"PGDMPfixture", "sha256:" + "a" * 64)
    assert calls[-1][0:3] == ["docker", "rm", "-f"]
    assert calls[-1][3].startswith("time-tl-rc24-restore-")
    assert calls[-1][3] != backup.PG


def test_cli_error_never_prints_exception_or_secret(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["verified_backup", "backup"])
    monkeypatch.setattr(backup, "backup", Mock(side_effect=ValueError("SECRET_PASSWORD")))
    assert backup.main() == 2
    output = capsys.readouterr().out
    assert "SECRET_PASSWORD" not in output
    assert json.loads(output)["status"] == "REJECTED"


def test_encrypt_decrypt_and_authentication(monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("OWNER_ALERTS_FERNET_KEY", Fernet.generate_key().decode())
    import subprocess
    import sys

    def crypto(data, operation):
        return subprocess.run(
            [sys.executable, "-c", backup.CRYPTO, operation], input=data, capture_output=True
        )

    plaintext = b"PGDMPfixture"
    first = crypto(plaintext, "encrypt")
    second = crypto(plaintext, "encrypt")
    assert first.returncode == 0 and first.stdout != second.stdout
    assert crypto(first.stdout, "decrypt").stdout == plaintext
    corrupted = first.stdout[:-1] + bytes([first.stdout[-1] ^ 1])
    rejected = crypto(corrupted, "decrypt")
    assert rejected.returncode == 2 and rejected.stdout == b"" and rejected.stderr == b""


@pytest.mark.parametrize("schema", tuple(backup.SCHEMA_TABLES))
def test_restore_actual_schema_toc_and_counts(monkeypatch, schema):
    tables = sorted(backup.SCHEMA_TABLES[schema])
    calls = []

    def command(args, *rest, **kwargs):
        calls.append(args)
        if "--list" in args:
            return "\n".join(
                f"{i}; 1259 {i} TABLE public {table} time_tl" for i, table in enumerate(tables)
            ).encode()
        if "-Atc" in args:
            sql = args[-1]
            if "pg_tables" in sql:
                return "\n".join(tables).encode()
            if "version_num" in sql:
                return schema.encode()
            if "pg_constraint" in sql:
                return b"0"
            return b"2"
        return b""

    monkeypatch.setattr(backup, "command", command)
    inspector = Mock()
    monkeypatch.setattr(backup, "validate_personal_restore", inspector)
    result = backup.restore(b"PGDMPfixture", "sha256:" + "a" * 64, expected_schema=schema)
    assert inspector.call_count == int(schema in backup.RESTORE_INSPECTORS)
    if schema in backup.RESTORE_INSPECTORS:
        assert inspector.call_args.args[2] == schema
    assert result["schema"] == schema
    assert result["tables_checked"] == len(tables)
    assert result["restored_total_rows"] == 2 * len(tables)
    assert "--exit-on-error" in next(call for call in calls if "--no-owner" in call)
    run = calls[0]
    assert "--read-only" in run and run[run.index("--network") + 1] == "none"
    assert run[run.index("--log-driver") + 1] == "none"
    assert "512m" in run and run.count("--tmpfs") == 3
    assert calls[-1][1:3] == ["rm", "-f"]


def test_postgres_image_drift_rejected(monkeypatch):
    monkeypatch.setattr(backup.monitor, "protected_read", lambda path: {"image": "approved"})
    monkeypatch.setattr(backup.monitor, "validate_config", lambda value: None)
    metadata = [
        {"State": {"Running": True}, "Image": "sha256:" + "a" * 64},
        {"State": {"Running": True}, "Config": {"Image": "approved"}},
    ]
    monkeypatch.setattr(backup, "command", lambda *args, **kw: json.dumps(metadata))
    with pytest.raises(ValueError, match="PostgreSQL identity"):
        backup.identity()


@pytest.mark.parametrize("fault", ["healthy", "old_admin", "other_admin", "unapproved_pin"])
def test_backup_admin_only_image_pin(monkeypatch, fault):
    pin = "localhost:5000/time-tl-bot@sha256:" + "a" * 64
    config = {"image": "old-approved", "admin_image": pin}
    monkeypatch.setattr(backup.monitor, "RC25_ADMIN_IMAGE", pin)
    monkeypatch.setattr(backup.monitor, "protected_read", lambda path: config)
    monkeypatch.setattr(backup.monitor, "validate_config", lambda value: None)
    metadata = [
        {"State": {"Running": True}, "Image": backup.PG_IMAGE},
        {"State": {"Running": True}, "Config": {"Image": pin}},
    ]
    if fault == "old_admin":
        metadata[1]["Config"]["Image"] = config["image"]
    elif fault == "other_admin":
        metadata[1]["Config"]["Image"] = "other"
    elif fault == "unapproved_pin":
        config["admin_image"] = "other"
    monkeypatch.setattr(backup, "command", lambda *args, **kw: json.dumps(metadata))
    if fault == "healthy":
        assert backup.identity() == backup.PG_IMAGE
    else:
        with pytest.raises(ValueError, match="identity"):
            backup.identity()


def test_command_output_limit():
    import sys

    with pytest.raises(ValueError, match="Output limit"):
        backup.command([sys.executable, "-c", "print('x'*10000)"], limit=10)


def test_command_timeout():
    import sys

    with pytest.raises(RuntimeError, match="Command failed"):
        backup.command([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.05)


def test_malformed_key_no_output(monkeypatch):
    import subprocess
    import sys

    monkeypatch.setenv("OWNER_ALERTS_FERNET_KEY", "malformed_secret")
    result = subprocess.run(
        [sys.executable, "-c", backup.CRYPTO, "encrypt"],
        input=b"PGDMPfixture",
        capture_output=True,
    )
    assert result.returncode == 2 and result.stdout == b"" and result.stderr == b""


@pytest.mark.skipif(os.name != "posix", reason="Linux flock")
def test_collector_lock_timeout_never_changes_config(tmp_path, monkeypatch):
    import fcntl

    tmp_path.chmod(0o700)
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    config = tmp_path / "config.json"
    config.write_text("original")
    config.chmod(0o600)
    monkeypatch.setattr(backup, "CONFIG", config)
    ticks = iter([0, 31])
    monkeypatch.setattr(backup.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(fcntl, "flock", Mock(side_effect=BlockingIOError()))
    with pytest.raises(TimeoutError):
        backup.publish_timestamp("2026-10-02T00:00:00+00:00")
    assert config.read_text() == "original"


@pytest.mark.skipif(os.name != "posix", reason="Linux protected event log")
@pytest.mark.parametrize("bad", ["symlink", "mode", "directory"])
def test_bad_event_log_prevents_backup(tmp_path, monkeypatch, bad):
    tmp_path.chmod(0o700)
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    monkeypatch.setattr(backup, "CONFIG", tmp_path / "config.json")
    log = state / "backup-events.jsonl"
    if bad == "symlink":
        target = state / "unrelated"
        target.write_text("unchanged")
        log.symlink_to(target)
    elif bad == "mode":
        log.write_text("unchanged")
        log.chmod(0o644)
    else:
        log.mkdir()
    operation = Mock()
    monkeypatch.setattr(backup, "backup", operation)
    monkeypatch.setattr("sys.argv", ["backup", "backup", "--event-log"])
    assert backup.main() == 2
    operation.assert_not_called()
    if bad == "mode":
        assert log.read_text() == "unchanged"
    elif bad == "symlink":
        assert target.read_text() == "unchanged"


@pytest.mark.skipif(os.name != "posix", reason="Linux protected event log")
def test_event_log_only_structured_output(tmp_path, monkeypatch, capsys):
    tmp_path.chmod(0o700)
    (tmp_path / "state").mkdir(mode=0o700)
    monkeypatch.setattr(backup, "CONFIG", tmp_path / "config.json")
    monkeypatch.setattr(backup, "backup", lambda: {"status": "PASS"})
    monkeypatch.setattr("sys.argv", ["backup", "backup", "--event-log"])
    assert backup.main() == 0
    log = tmp_path / "state" / "backup-events.jsonl"
    assert json.loads(log.read_text()) == {"status": "PASS"}
    assert log.stat().st_mode & 0o777 == 0o600
    assert capsys.readouterr().out == ""


@pytest.mark.skipif(os.name != "posix", reason="Linux guard rollback")
def test_guard_post_replace_failure_restores_old_timestamp(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    (tmp_path / "state").mkdir(mode=0o700)
    path = tmp_path / "config.json"
    old = {"backup_verified_at": "old", "baseline": {"unchanged": True}}
    backup.monitor.atomic_write(path, old)
    monkeypatch.setattr(backup, "CONFIG", path)
    monkeypatch.setattr(backup.monitor, "validate_config", lambda value: None)

    def failed_write(path, value):
        backup.monitor.atomic_write(path, value)
        raise OSError("Injected postreplace fsync failure")

    monkeypatch.setattr(backup, "durable_json", failed_write)
    with pytest.raises(OSError):
        backup.publish_timestamp("new")
    assert backup.monitor.protected_read(path) == old

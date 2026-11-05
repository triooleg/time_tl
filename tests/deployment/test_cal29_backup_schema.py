"""LOCAL fake Docker and native frozen-inspector tests, not actual PG restore."""

from __future__ import annotations

import ast
import asyncio
import importlib
import json
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import sqlalchemy
from sqlalchemy.dialects import postgresql

from scripts.deployment import verified_backup as backup
from tests.persistence.test_calendar_schema import Inspector

PG_ID = "a" * 64
APP_ID = "sha256:" + "b" * 64
CONTAINER_ID = "c" * 64
PIN = "localhost:5000/time-tl-bot@sha256:" + "d" * 64
NAME = "time-tl-rc24-restore-" + "e" * 32


def test_exact_additive_catalog_and_unchanged_crypto_format():
    assert len(backup.SCHEMA_TABLES[backup.SCHEMA]) == 28
    assert len(backup.SCHEMA_TABLES["0009_calendar_foundation"]) == 31
    assert len(backup.SCHEMA_TABLES[backup.PERSONAL_SCHEMA]) == 32
    assert backup.SCHEMA_TABLES[backup.PERSONAL_SCHEMA] - backup.SCHEMA_TABLES[
        "0009_calendar_foundation"
    ] == {"personal_calendar_overrides"}
    assert "aad=b'TimeTL/RC24/revision14/0008_homework_foundation'" in backup.CRYPTO


def restore_fixture(monkeypatch, *, head=backup.PERSONAL_SCHEMA, tables=None, validated=b"0"):
    rows = sorted(tables if tables is not None else backup.SCHEMA_TABLES[backup.PERSONAL_SCHEMA])
    calls = []

    def command(args, *unused, **kwargs):
        calls.append(args)
        if "--list" in args:
            return "\n".join(f"1; 1259 1 TABLE public {name} time_tl" for name in rows).encode()
        if "-Atc" in args:
            if "pg_tables" in args[-1]:
                return "\n".join(rows).encode()
            if "version_num" in args[-1]:
                return head.encode()
            if "pg_constraint" in args[-1]:
                return validated
            return b"0"
        return b""

    monkeypatch.setattr(backup, "command", command)
    inspector = Mock()
    monkeypatch.setattr(backup, "validate_personal_restore", inspector)
    return calls, inspector


@pytest.mark.parametrize("fault", ["unknown_head", "partial", "foreign", "duplicate", "head9"])
def test_forged_or_partial_schema_never_calls_inspector(monkeypatch, fault):
    tables = sorted(backup.SCHEMA_TABLES[backup.PERSONAL_SCHEMA])
    head = backup.PERSONAL_SCHEMA
    if fault == "unknown_head":
        head = "0011_forged"
    elif fault == "partial":
        tables.remove("personal_calendar_overrides")
    elif fault == "foreign":
        tables[-1] = "forged_table"
    elif fault == "duplicate":
        tables.append(tables[0])
    elif fault == "head9":
        head = "0009_calendar_foundation"
    calls, inspector = restore_fixture(monkeypatch, head=head, tables=tables)
    with pytest.raises(ValueError):
        backup.restore(b"PGDMPfixture", backup.PG_IMAGE, expected_schema=backup.PERSONAL_SCHEMA)
    inspector.assert_not_called()
    assert calls[-1][1:3] == ["rm", "-f"]


def test_unknown_expected_schema_rejected_before_docker(monkeypatch):
    command = Mock()
    monkeypatch.setattr(backup, "command", command)
    with pytest.raises(ValueError):
        backup.restore(b"PGDMPfixture", backup.PG_IMAGE, expected_schema="0011_forged")
    command.assert_not_called()


def test_native_inspector_failure_cannot_publish_restore_pass(monkeypatch):
    calls, inspector = restore_fixture(monkeypatch)
    inspector.side_effect = RuntimeError("fixed schema mismatch")
    with pytest.raises(RuntimeError):
        backup.restore(b"PGDMPfixture", backup.PG_IMAGE)
    inspector.assert_called_once()
    assert not any("count(*) FROM \"" in args[-1] for args in calls)
    assert calls[-1][1:3] == ["rm", "-f"]


def test_unvalidated_constraints_reject_before_native_inspector(monkeypatch):
    _, inspector = restore_fixture(monkeypatch, validated=b"1")
    with pytest.raises(ValueError, match="Unvalidated"):
        backup.restore(b"PGDMPfixture", backup.PG_IMAGE)
    inspector.assert_not_called()


def inspector_fixture(monkeypatch, *, ack=b"CAL29_SCHEMA_VALIDATED\n", fault=None):
    calls = []
    postgres = {
        "Id": PG_ID, "Name": "/" + NAME, "Image": backup.PG_IMAGE,
        "HostConfig": {"NetworkMode": "none"}, "State": {"Running": True},
    }
    admin = {
        "Name": "/" + backup.ADMIN, "Image": APP_ID,
        "Config": {"Image": PIN}, "State": {"Running": True},
    }
    if fault == "pg_network":
        postgres["HostConfig"]["NetworkMode"] = "production"
    elif fault == "pg_id":
        postgres["Id"] = "short"
    elif fault == "pg_image":
        postgres["Image"] = APP_ID
    elif fault == "admin_image":
        admin["Config"]["Image"] = "foreign"
    elif fault == "admin_stopped":
        admin["State"]["Running"] = False
    actual = {}
    removed = False

    def command(args, *unused, **kwargs):
        nonlocal removed
        calls.append((args, kwargs))
        if args[1] == "create":
            inspector_name = args[args.index("--name") + 1]
            actual.update({
                "Id": CONTAINER_ID, "Name": "/" + inspector_name, "Image": APP_ID,
                "Config": {"Labels": {"time-tl.backup-inspector": inspector_name}},
                "State": {"Running": False, "ExitCode": 0},
                "HostConfig": {"NetworkMode": "container:" + PG_ID},
            })
            if fault == "foreign_label":
                actual["Config"]["Labels"]["time-tl.backup-inspector"] = "foreign"
            if fault == "create_timeout":
                raise TimeoutError()
            return CONTAINER_ID.encode()
        if args[1] == "inspect":
            return json.dumps([postgres, admin] if len(args) == 4 else [actual]).encode()
        if args[1] == "start":
            if fault == "start_timeout":
                raise TimeoutError()
            return ack
        if args[1] == "ps":
            if removed and fault != "rm_no_effect":
                return b""
            if not actual:
                return b""
            return (
                json.dumps({"ID": CONTAINER_ID}).encode()
                if "{{json .}}" in args else CONTAINER_ID.encode()
            )
        if args[1] == "rm":
            removed = True
        return b""

    monkeypatch.setattr(backup.monitor, "protected_read", lambda path: {"image": PIN})
    monkeypatch.setattr(backup.monitor, "validate_config", lambda config: None)
    monkeypatch.setattr(backup.monitor, "expected_admin_image", lambda config: PIN)
    monkeypatch.setattr(backup, "command", command)
    monkeypatch.setattr(backup, "inspector_creators_quiescent", lambda name: None)
    return calls


def test_native_validator_exact_physical_image_no_production_env_or_mounts(monkeypatch):
    calls = inspector_fixture(monkeypatch)
    backup.validate_personal_restore(NAME, backup.PG_IMAGE)
    create = next(args for args, _ in calls if args[1] == "create")
    assert create[create.index("--network") + 1] == "container:" + PG_ID
    assert create[create.index("--entrypoint") + 2] == APP_ID
    assert create[create.index("--user") + 1] == "10001:10001"
    assert {"--read-only", "--cap-drop", "--memory", "--pids-limit"} <= set(create)
    assert not {"-e", "--env", "--env-file", "-v", "--volume", "--mount", "-p"} & set(create)
    assert next(args for args, _ in calls if args[1] == "start") == [
        "docker", "start", "-a", CONTAINER_ID,
    ]
    assert any(args == ["docker", "rm", "-f", CONTAINER_ID] for args, _ in calls)
    assert [args[-1] for args, _ in calls[-3:]] == [
        "id=" + CONTAINER_ID,
        "name=^/" + create[create.index("--name") + 1] + "$",
        "label=time-tl.backup-inspector=" + create[create.index("--name") + 1],
    ]


@pytest.mark.parametrize("fault", ["pg_network", "pg_id", "pg_image", "admin_image",
                                   "admin_stopped"])
def test_inspector_target_drift_refuses_create(monkeypatch, fault):
    calls = inspector_fixture(monkeypatch, fault=fault)
    with pytest.raises(ValueError):
        backup.validate_personal_restore(NAME, backup.PG_IMAGE)
    assert not any(args[1] == "create" for args, _ in calls)


@pytest.mark.parametrize("ack", [b"", b"PASS", b"CAL29_SCHEMA_VALIDATED\nprivate-data"])
def test_old_missing_migration_or_invalid_output_never_passes(monkeypatch, ack):
    calls = inspector_fixture(monkeypatch, ack=ack)
    with pytest.raises(ValueError, match="validation"):
        backup.validate_personal_restore(NAME, backup.PG_IMAGE)
    assert any(args == ["docker", "rm", "-f", CONTAINER_ID] for args, _ in calls)


@pytest.mark.parametrize("fault", ["create_timeout", "start_timeout"])
def test_timeout_cleanup_only_verified_owned_full_id(monkeypatch, fault):
    calls = inspector_fixture(monkeypatch, fault=fault)
    with pytest.raises(TimeoutError):
        backup.validate_personal_restore(NAME, backup.PG_IMAGE)
    assert any(args == ["docker", "rm", "-f", CONTAINER_ID] for args, _ in calls)
    if fault == "create_timeout":
        assert not any(args[1] == "start" for args, _ in calls)


def test_foreign_label_is_not_started_or_removed(monkeypatch):
    calls = inspector_fixture(monkeypatch, fault="foreign_label")
    with pytest.raises(ValueError):
        backup.validate_personal_restore(NAME, backup.PG_IMAGE)
    assert not any(args[1] in {"start", "rm"} for args, _ in calls)


@pytest.mark.parametrize("running,exit_code", [(True, 0), (False, 1), (False, False)])
def test_ack_requires_terminal_success(monkeypatch, running, exit_code):
    calls = inspector_fixture(monkeypatch)
    original = backup.command
    started = False

    def command(args, **kwargs):
        nonlocal started
        raw = original(args, **kwargs)
        if args[1] == "start":
            started = True
        if started and args[1] == "inspect":
            records = json.loads(raw)
            records[0]["State"] = {"Running": running, "ExitCode": exit_code}
            return json.dumps(records).encode()
        return raw

    monkeypatch.setattr(backup, "command", command)
    with pytest.raises(ValueError, match="terminal"):
        backup.validate_personal_restore(NAME, backup.PG_IMAGE)
    assert any(args[1] == "rm" for args, _ in calls)


def test_rm_ack_without_fresh_absence_is_rejected(monkeypatch):
    calls = inspector_fixture(monkeypatch, fault="rm_no_effect")
    with pytest.raises(ValueError, match="cleanup unconfirmed"):
        backup.validate_personal_restore(NAME, backup.PG_IMAGE)
    assert calls[-1][0][1] == "ps"


def test_uncertain_create_live_creator_prevents_adoption(monkeypatch):
    calls = inspector_fixture(monkeypatch, fault="create_timeout")

    def reject(name):
        raise ValueError("creator still live")

    monkeypatch.setattr(backup, "inspector_creators_quiescent", reject)
    with pytest.raises(ValueError, match="creator"):
        backup.validate_personal_restore(NAME, backup.PG_IMAGE)
    assert not any(args[1] in {"start", "rm"} for args, _ in calls)


@pytest.mark.parametrize("case", ["python_large", "docker_large", "docker_create",
                                  "compose_create", "foreign_name", "read_failure", "race"])
def test_creator_scan_filters_argv0_before_cap_and_fails_closed(monkeypatch, case):
    name = "time-tl-cal29-inspector-" + "f" * 32
    raw = b"python\0-c\0" + b"x" * 93000 + b"\0"
    if case == "docker_large":
        raw = b"docker\0create\0" + b"x" * 65536 + b"\0"
    elif case in {"docker_create", "compose_create", "foreign_name"}:
        executable = b"docker-compose" if case == "compose_create" else b"docker"
        target = "foreign" if case == "foreign_name" else name
        raw = executable + b"\0create\0--name\0" + target.encode() + b"\0"

    class Process:
        name = "123"

        def stat(self):
            return SimpleNamespace(st_uid=1000)

        def __truediv__(self, key):
            assert key == "cmdline"
            return self

        def open(self, mode):
            if case == "read_failure":
                raise PermissionError()
            if case == "race":
                raise FileNotFoundError()
            return BytesIO(raw)

    monkeypatch.setattr(backup.Path, "iterdir", lambda path: iter((Process(),)))
    monkeypatch.setattr(backup.os, "geteuid", lambda: 1000, raising=False)
    if case in {"docker_large", "docker_create", "compose_create", "read_failure"}:
        with pytest.raises((ValueError, PermissionError)):
            backup.inspector_creators_quiescent(name)
    else:
        backup.inspector_creators_quiescent(name)


def native_main():
    tree = ast.parse(backup.PERSONAL_SCHEMA_INSPECTOR)
    definitions = [
        item for item in tree.body if isinstance(item, (ast.Import, ast.AsyncFunctionDef))
    ]
    namespace = {}
    exec(compile(ast.Module(definitions, type_ignores=[]), "native-schema-test", "exec"), namespace)
    return namespace["main"]


@pytest.mark.parametrize("fault", [None, "check", "cascade", "pk", "index", "nullable",
                                   "prior", "head"])
def test_actual_frozen_native_inspectors_not_only_convalidated(monkeypatch, fault):
    prior = importlib.import_module("migrations.versions.0009_calendar_foundation")
    personal = importlib.import_module("migrations.versions.0010_personal_calendar_overrides")
    frozen = prior._schema
    monkeypatch.setattr(prior, "_schema", lambda: (*frozen(), personal._schema()))
    inspector = Inspector()
    monkeypatch.setattr(prior, "_schema", frozen)
    name = "personal_calendar_overrides"
    if fault == "check":
        inspector.checks[name][0]["sqltext"] = "version > 0"
    elif fault == "cascade":
        inspector.foreign[name][0]["options"]["ondelete"] = "RESTRICT"
    elif fault == "pk":
        inspector.pk[name]["constrained_columns"].reverse()
    elif fault == "index":
        inspector.indexes[name].clear()
    elif fault == "nullable":
        inspector.columns[name][0]["nullable"] = True
    elif fault == "prior":
        inspector.columns["calendars"].pop()
    monkeypatch.setattr(sqlalchemy, "inspect", lambda bind: inspector)
    queries = []

    class Connection:
        dialect = postgresql.dialect()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def begin(self):
            return self

        async def execute(self, statement):
            queries.append(str(statement))
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [
                "0009_calendar_foundation" if fault == "head" else backup.PERSONAL_SCHEMA,
            ]))

        async def run_sync(self, function):
            function(self)

    class Engine:
        def connect(self):
            return Connection()

        async def dispose(self):
            pass

    def create(url, **kwargs):
        assert url == "postgresql+asyncpg://backupcheck@127.0.0.1/backupcheck"
        assert kwargs == {"echo": False}
        return Engine()

    monkeypatch.setattr(sqlalchemy.ext.asyncio, "create_async_engine", create)
    original_schema = json.dumps(inspector.columns, default=str, sort_keys=True)
    if fault:
        with pytest.raises((RuntimeError, ValueError)):
            asyncio.run(native_main()())
    else:
        asyncio.run(native_main()())
    assert queries == ["SET TRANSACTION READ ONLY", "SELECT version_num FROM alembic_version"]
    assert json.dumps(inspector.columns, default=str, sort_keys=True) == original_schema


def test_embedded_native_program_compiles_without_upgrade_or_mutating_sql():
    compile(backup.PERSONAL_SCHEMA_INSPECTOR, "native-schema-check", "exec")
    assert "._validate(" in backup.PERSONAL_SCHEMA_INSPECTOR
    assert ".upgrade(" not in backup.PERSONAL_SCHEMA_INSPECTOR
    assert "os.environ" not in backup.PERSONAL_SCHEMA_INSPECTOR

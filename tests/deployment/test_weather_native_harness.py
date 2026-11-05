"""Admission and failure-output checks; actual PostgreSQL proof requires Linux lab."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FOLDER = ROOT / ".release" / "weather-geo-20261105"


@pytest.fixture
def modules(monkeypatch):
    def load(name, path):
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        return module

    helper = load("lab_helpers", FOLDER / "lab_helpers.py")
    native = load("weather_native_test_owned", FOLDER / "native_test.py")
    return helper, native


def manifest(helper):
    return {"version": 1, "files": {name: "a" * 64 for name in helper.REQUIRED}}


def test_import_has_no_connection_or_execution_side_effect(modules):
    helper, native = modules
    assert native.PHASE == "admission" and native.PHASES == []
    assert helper.URL == "postgresql+asyncpg://weathercheck@127.0.0.1:5432/weathercheck"
    assert not hasattr(native.run, "cleanup")


@pytest.mark.parametrize(
    "kind",
    [
        "duplicate",
        "deep",
        "extra",
        "boolean_version",
        "traversal",
        "absolute",
        "backslash",
        "missing_core",
        "wrong_hash",
        "unknown_prefix",
        "constant",
    ],
)
def test_manifest_invalidity_fails_closed(modules, kind):
    helper, _ = modules
    data = manifest(helper)
    if kind == "duplicate":
        raw = b'{"version":1,"version":1,"files":{}}'
    elif kind == "deep":
        raw = (b"[" * 2000) + b"0" + (b"]" * 2000)
    elif kind == "constant":
        raw = b'{"version":NaN,"files":{}}'
    else:
        if kind == "extra":
            data["unexpected"] = True
        if kind == "boolean_version":
            data["version"] = True
        if kind == "missing_core":
            data["files"].pop(next(iter(data["files"])))
        if kind == "wrong_hash":
            data["files"][next(iter(data["files"]))] = "not-a-hash"
        if kind == "traversal":
            data["files"]["migrations/../secret.py"] = "b" * 64
        if kind == "absolute":
            data["files"]["/migrations/secret.py"] = "b" * 64
        if kind == "backslash":
            data["files"]["migrations\\secret.py"] = "b" * 64
        if kind == "unknown_prefix":
            data["files"]["production/config.py"] = "b" * 64
        raw = json.dumps(data).encode()
    with pytest.raises(helper.LabRejected):
        helper.parse_manifest(raw)


def test_manifest_valid_but_incomplete_inventory_cannot_omit_other_installed_code(
    modules, tmp_path
):
    helper, _ = modules
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest(helper)))
    with pytest.raises(helper.LabRejected):
        helper.verified_inventory(path, ROOT)


def test_complete_installed_inventory_accepts_exact_bytes_and_rejects_change(
    modules, tmp_path, monkeypatch
):
    import hashlib
    from types import SimpleNamespace

    helper, _ = modules
    package = tmp_path / "installed" / "time_tl"
    project = tmp_path / "app"
    files = {}
    for name in helper.REQUIRED | {"src/time_tl/__init__.py"}:
        target = (
            package / name.removeprefix("src/time_tl/")
            if name.startswith("src/time_tl/")
            else project / name
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"# synthetic hash-only inventory\n")
        files[name] = hashlib.sha256(target.read_bytes()).hexdigest()
    monkeypatch.setattr(
        helper.importlib.util,
        "find_spec",
        lambda _: SimpleNamespace(origin=str(package / "__init__.py")),
    )
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"version": 1, "files": files}))
    assert helper.verified_inventory(path, project) == hashlib.sha256(path.read_bytes()).hexdigest()
    (package / "infrastructure/db/weather.py").write_bytes(b"# changed\n")
    with pytest.raises(helper.LabRejected):
        helper.verified_inventory(path, project)


@pytest.mark.parametrize(
    "environment",
    [
        {"DATABASE_URL": "PRIVATE"},
        {"PGPASSFILE": "PRIVATE"},
        {"TELEGRAM_TOKEN": "PRIVATE"},
        {"FERNET_KEY": "PRIVATE"},
        {"PGPASSWORD": "PRIVATE"},
        {"PYTHONPATH": "PRIVATE"},
        {"SSH_PRIVATE_KEY": "PRIVATE"},
    ],
)
def test_ambient_credentials_or_source_override_reject(modules, environment):
    helper, _ = modules
    with pytest.raises(helper.LabRejected):
        helper.validate_lab_environment(
            platform="linux", interfaces={"lo"}, environment=environment
        )


@pytest.mark.parametrize(
    "platform,interfaces", [("win32", {"lo"}), ("linux", {"lo", "eth0"}), ("linux", set())]
)
def test_nonlinux_or_external_interface_reject(modules, platform, interfaces):
    helper, _ = modules
    with pytest.raises(helper.LabRejected):
        helper.validate_lab_environment(platform=platform, interfaces=interfaces, environment={})


def test_only_exact_uuid_owned_schema_can_be_used_for_cleanup(modules):
    helper, _ = modules
    valid = "weather_native_" + "a" * 32
    assert helper.owned_schema(valid) == valid
    for value in ("public", "weather_native_public", valid + ";DROP SCHEMA public", valid.upper()):
        with pytest.raises(helper.LabRejected):
            helper.owned_schema(value)


def test_synthetic_head9_seed_does_not_precreate_post9_feature_tables(modules):
    helper, native = modules
    original, legacy, excluded = native.baseline_metadata()
    assert len(excluded) == 13
    assert {"weather_locations", "personal_calendar_overrides", "power_providers"} <= excluded
    assert not set(legacy.tables) & excluded
    assert {"users", "calendars", "homework_assignments", "schedule_profiles"} <= set(legacy.tables)
    assert len(original.tables) == len(legacy.tables) + len(excluded)


def test_driver_error_is_closed_failure_receipt_without_exception_or_payload(
    modules, monkeypatch, capsys
):
    helper, native = modules
    import logging

    import httpx

    original_client = httpx.AsyncClient
    original_logging = logging.root.manager.disable
    original_path = list(sys.path)
    monkeypatch.setattr(
        sys,
        "argv",
        ["native_test.py", "--project-root", str(ROOT), "--manifest", "public-manifest.json"],
    )
    monkeypatch.setattr(native, "validate_lab_environment", lambda: None)
    monkeypatch.setattr(native, "verified_inventory", lambda *_: "a" * 64)

    async def failed(_root):
        native.phase("real_sql_phase")
        failed.cleanup = True
        print("PRIVATE_PAYLOAD")
        raise RuntimeError("PRIVATE_DRIVER_MESSAGE")

    monkeypatch.setattr(native, "run", failed)
    assert native.main() == 1
    assert httpx.AsyncClient is original_client
    assert logging.root.manager.disable == original_logging
    assert sys.path == original_path
    captured = capsys.readouterr()
    assert not captured.err and "PRIVATE" not in captured.out
    value = json.loads(captured.out)
    assert value["status"] == "FAIL" and value["phase"] == "real_sql_phase"
    assert value["owned_schema_cleanup"] is True
    assert value["production_mutations"] is False
    assert value["real_http_calls"] == value["real_telegram_calls"] == 0
    assert set(value) == {
        "status",
        "phase",
        "passed_phases",
        "source_manifest_sha256",
        "migration_head",
        "baseline",
        "owned_schema_cleanup",
        "production_mutations",
        "real_http_calls",
        "real_telegram_calls",
        "native_helper_sha256",
        "lab_helpers_sha256",
    }


def test_failed_admission_does_not_open_postgres(modules, monkeypatch, capsys):
    helper, native = modules
    monkeypatch.setattr(
        sys,
        "argv",
        ["native_test.py", "--project-root", str(ROOT), "--manifest", "public-manifest.json"],
    )

    def reject():
        raise helper.LabRejected()

    monkeypatch.setattr(native, "validate_lab_environment", reject)

    async def forbidden(_root):
        raise AssertionError("connection must not be attempted")

    monkeypatch.setattr(native, "run", forbidden)
    assert native.main() == 1
    assert json.loads(capsys.readouterr().out)["phase"] == "admission"


def test_debug_failure_contains_only_known_types_and_code_locations(modules, monkeypatch, capsys):
    helper, native = modules
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "native_test.py",
            "--project-root",
            str(ROOT),
            "--manifest",
            "public-manifest.json",
            "--debug-failure",
        ],
    )
    monkeypatch.setattr(native, "validate_lab_environment", lambda: None)
    monkeypatch.setattr(native, "verified_inventory", lambda *_: "a" * 64)

    async def failed(_root):
        failed.cleanup = True
        raise RuntimeError("PRIVATE_SQL_AND_PARAMETERS")

    monkeypatch.setattr(native, "run", failed)
    assert native.main() == 1
    output = capsys.readouterr().out
    assert "PRIVATE" not in output
    debug = json.loads(output)["debug_failure"]
    assert debug["exception_types"] == ["RuntimeError"] and debug["sqlstates"] == []
    assert debug["frames"] and all(set(frame) == {"file", "line"} for frame in debug["frames"])
    assert all(frame["file"].startswith("lab/") for frame in debug["frames"])


def test_debug_unknown_exception_type_never_emits_class_name_or_message(modules):
    helper, _ = modules

    class PRIVATEException(Exception):
        pass

    error = PRIVATEException("PRIVATE_SQL")
    assert helper.failure_diagnostic(error) == {
        "exception_types": ["OTHER"],
        "frames": [],
        "sqlstates": [],
    }


def test_schema_debug_reads_only_known_weather_static_checks(modules, monkeypatch):
    import importlib

    import sqlalchemy as sa

    helper, _ = modules
    weather = importlib.import_module("migrations.versions.0012_weather_coverage")
    called = []
    tables = {table.name: table for table in weather._validation_schema()}

    class Inspector:
        def get_table_names(self):
            return [*tables, "users"]

        def get_check_constraints(self, name):
            called.append(name)
            result = [
                {"name": item.name, "sqltext": str(item.sqltext)}
                for item in tables[name].constraints
                if isinstance(item, sa.CheckConstraint)
            ]
            if name == "weather_locations":
                for item in result:
                    if item["name"] == "ck_weather_locations_approval_required":
                        item["sqltext"] = "enabled"
                result.append({"name": "UNKNOWN_PRIVATE_CONSTRAINT", "sqltext": "PRIVATE"})
            return result

    monkeypatch.setattr(sa, "inspect", lambda _connection: Inspector())
    result = helper.weather_schema_diagnostic(object())
    assert set(called) == set(helper.WEATHER_CHECKS) and "users" not in called
    assert len(result) == 1
    assert result[0]["constraint"] == "ck_weather_locations_approval_required"
    assert result[0]["actual"] == "enabled" and "PRIVATE" not in json.dumps(result)
    assert all(set(item) == {"table", "constraint", "actual", "expected"} for item in result)

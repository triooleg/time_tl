"""Closed-input helpers for the credential-free network-none weather native lab."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import sys
from pathlib import Path
from typing import Any
from uuid import UUID

URL = "postgresql+asyncpg://weathercheck@127.0.0.1:5432/weathercheck"
HEADS = (
    "0009_calendar_foundation",
    "0010_personal_calendar_overrides",
    "0011_power_outages",
    "0012_weather_coverage",
)
WEATHER_CHECKS = {
    "weather_locations": frozenset(
        {
            "ck_weather_locations_approval_required",
            "ck_weather_locations_coverage_allowed",
            "ck_weather_locations_katottg_shape",
            "ck_weather_locations_provenance_digest",
            "ck_weather_locations_ukraine_latitude",
            "ck_weather_locations_ukraine_longitude",
        }
    ),
    "weather_forecast_runs": frozenset(
        {"ck_weather_forecast_runs_counts_bounded", "ck_weather_forecast_runs_counts_nonnegative"}
    ),
    "weather_forecast_snapshots": frozenset({"ck_weather_forecast_snapshots_status_allowed"}),
    "weather_forecast_hours": frozenset(
        {
            "ck_weather_forecast_hours_cloud_range",
            "ck_weather_forecast_hours_precipitation_nonnegative",
            "ck_weather_forecast_hours_probability_range",
            "ck_weather_forecast_hours_temperature_range",
            "ck_weather_forecast_hours_wind_nonnegative",
        }
    ),
    "weather_change_events": frozenset(
        {
            "ck_weather_change_events_kind_allowed",
            "ck_weather_change_events_status_allowed",
            "ck_weather_change_events_valid_interval",
        }
    ),
    "user_weather_preferences": frozenset(
        {"ck_user_weather_preferences_lead_range", "ck_user_weather_preferences_revision_positive"}
    ),
    "weather_notification_deliveries": frozenset(),
}
REQUIRED = frozenset(
    {
        "migrations/env.py",
        "migrations/versions/0001_initial.py",
        "migrations/versions/0009_calendar_foundation.py",
        "migrations/versions/0010_personal_calendar_overrides.py",
        "migrations/versions/0011_power_outages.py",
        "migrations/versions/0012_weather_coverage.py",
        "src/time_tl/infrastructure/db/models.py",
        "src/time_tl/infrastructure/db/weather.py",
        "src/time_tl/infrastructure/db/weather_notifications.py",
        "src/time_tl/infrastructure/db/geographic_coverage.py",
        "src/time_tl/infrastructure/db/weather_configuration.py",
        "src/time_tl/infrastructure/db/workers.py",
        "src/time_tl/infrastructure/db/outbox.py",
        "src/time_tl/application/weather_collection.py",
        "src/time_tl/application/weather_notifications.py",
        "src/time_tl/application/sender_worker.py",
        "src/time_tl/runtime/weather_worker.py",
        "src/time_tl/infrastructure/weather/met_norway.py",
    }
)


class LabRejected(Exception):
    """Never expose underlying exception text or paths to the receipt."""


def require(condition: object) -> None:
    if not condition:
        raise LabRejected()


def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise LabRejected()
        result[key] = value
    return result


def parse_manifest(raw: bytes) -> dict[str, str]:
    require(0 < len(raw) <= 2_000_000)
    try:
        value = json.loads(
            raw,
            object_pairs_hook=reject_duplicates,
            parse_constant=lambda _: (_ for _ in ()).throw(LabRejected()),
        )
    except (ValueError, RecursionError) as error:
        raise LabRejected() from error
    require(type(value) is dict and set(value) == {"version", "files"})
    require(type(value["version"]) is int and value["version"] == 1)
    rows = value["files"]
    require(type(rows) is dict and 1 <= len(rows) <= 3000 and REQUIRED <= set(rows))
    for name, digest in rows.items():
        require(type(name) is str and len(name) <= 220 and "\\" not in name)
        require(re.fullmatch(r"(?:src/time_tl|migrations)/[A-Za-z0-9_./-]+\.py", name))
        require(not any(part in {".", "..", ""} for part in name.split("/")))
        require(type(digest) is str and re.fullmatch("[0-9a-f]{64}", digest))
    return rows


def verified_inventory(manifest: Path, project_root: Path) -> str:
    raw = manifest.read_bytes()
    files = parse_manifest(raw)
    spec = importlib.util.find_spec("time_tl")
    require(spec is not None and spec.origin is not None)
    installed = Path(spec.origin).resolve().parent
    actual_sources = {
        "src/time_tl/" + str(path.relative_to(installed)).replace("\\", "/")
        for path in installed.rglob("*.py")
    }
    actual_migrations = {
        str(path.relative_to(project_root)).replace("\\", "/")
        for path in (project_root / "migrations").rglob("*.py")
    }
    require(set(files) == actual_sources | actual_migrations)
    for name, digest in files.items():
        root = installed if name.startswith("src/time_tl/") else project_root
        suffix = name.removeprefix("src/time_tl/") if root == installed else name
        target = root / suffix
        require(not target.is_symlink() and target.resolve().is_relative_to(root.resolve()))
        require(hashlib.sha256(target.read_bytes()).hexdigest() == digest)
    return hashlib.sha256(raw).hexdigest()


def validate_lab_environment(
    *,
    platform: str | None = None,
    interfaces: set[str] | None = None,
    environment: dict[str, str] | None = None,
) -> None:
    require((platform or sys.platform).startswith("linux"))
    names = (
        interfaces if interfaces is not None else {p.name for p in Path("/sys/class/net").iterdir()}
    )
    require(names == {"lo"})
    values = dict(os.environ) if environment is None else environment
    for name in values:
        upper = name.upper()
        require(
            upper
            not in {
                "DATABASE_URL",
                "FERNET_KEY",
                "API_KEY",
                "PRIVATE_KEY",
                "TOKEN",
                "PGPASSWORD",
                "PGPASSFILE",
                "PGSERVICE",
                "PGSERVICEFILE",
                "PYTHONPATH",
            }
        )
        require(
            not upper.endswith(("_TOKEN", "_API_KEY", "_FERNET_KEY", "_PRIVATE_KEY", "_PASSWORD"))
        )


def owned_schema(value: str) -> str:
    require(type(value) is str and re.fullmatch(r"weather_native_[a-f0-9]{32}", value))
    UUID(value.removeprefix("weather_native_"))
    return value


def failure_diagnostic(error: BaseException) -> dict[str, object]:
    """Return code locations only; never inspect exception strings, SQL or locals."""
    allowed_types = {
        "LabRejected",
        "AssertionError",
        "RuntimeError",
        "ValueError",
        "TypeError",
        "AttributeError",
        "KeyError",
        "ImportError",
        "ModuleNotFoundError",
        "ProgrammingError",
        "IntegrityError",
        "DBAPIError",
        "OperationalError",
        "UndefinedTableError",
        "DuplicateTableError",
        "DuplicateObjectError",
        "UndefinedColumnError",
        "InvalidSchemaNameError",
        "NotNullViolationError",
        "CheckViolationError",
        "ForeignKeyViolationError",
        "UniqueViolationError",
        "DataError",
        "InvalidTextRepresentationError",
        "TimeoutError",
        "CancelledError",
    }
    types: list[str] = []
    frames: list[dict[str, object]] = []
    states: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and len(types) < 4 and id(current) not in seen:
        seen.add(id(current))
        name = type(current).__name__
        types.append(name if name in allowed_types else "OTHER")
        state = getattr(current, "sqlstate", None)
        if type(state) is str and re.fullmatch(r"[A-Z0-9]{5}", state):
            states.append(state)
        trace = current.__traceback__
        while trace is not None:
            path = trace.tb_frame.f_code.co_filename.replace("\\", "/")
            selected = None
            for marker, prefix in (
                ("/time_tl/", "src/time_tl/"),
                ("/migrations/", "migrations/"),
                ("/alembic/", "dependency/alembic/"),
                ("/sqlalchemy/", "dependency/sqlalchemy/"),
                ("/asyncpg/", "dependency/asyncpg/"),
            ):
                if marker in path:
                    selected = prefix + path.rsplit(marker, 1)[1]
                    break
            if path.rsplit("/", 1)[-1] in {"native_test.py", "lab_helpers.py"}:
                selected = "lab/" + path.rsplit("/", 1)[-1]
            if (
                selected
                and len(selected) <= 220
                and re.fullmatch(r"[A-Za-z0-9_./-]+\.py", selected)
            ):
                frames.append({"file": selected, "line": trace.tb_lineno})
            trace = trace.tb_next
        current = current.__cause__ or current.__context__
    return {"exception_types": types, "frames": frames[-16:], "sqlstates": sorted(set(states))}


def weather_schema_diagnostic(connection) -> list[dict[str, object]]:
    """Read only known static CHECK DDL in this synthetic schema, never rows."""
    from importlib import import_module

    import sqlalchemy as sa

    weather = import_module("migrations.versions.0012_weather_coverage")
    inspector = sa.inspect(connection)
    present = set(inspector.get_table_names())
    mismatches = []
    for table in weather._validation_schema():
        require(table.name in WEATHER_CHECKS)
        if table.name not in present:
            continue
        expected = {
            item.name: weather._weather_expression(item.sqltext)
            for item in table.constraints
            if isinstance(item, sa.CheckConstraint)
        }
        require(set(expected) == WEATHER_CHECKS[table.name])
        actual = {
            item["name"]: weather._weather_expression(item["sqltext"])
            for item in inspector.get_check_constraints(table.name)
            if item["name"] in WEATHER_CHECKS[table.name]
        }
        for name, wanted in sorted(expected.items()):
            observed = actual.get(name)
            if observed == wanted:
                continue
            require(
                all(
                    value is None or (len(value) <= 2048 and re.fullmatch(r"[\x20-\x7e]+", value))
                    for value in (wanted, observed)
                )
            )
            require(len(mismatches) < 20)
            mismatches.append(
                {"table": table.name, "constraint": name, "expected": wanted, "actual": observed}
            )
    return mismatches


def receipt(
    *, phase: str, phases: list[str], source_sha: str | None, cleanup: bool, passed: bool
) -> dict[str, object]:
    require(re.fullmatch(r"[a-z0-9_]{1,64}", phase))
    require(all(re.fullmatch(r"[a-z0-9_]{1,64}", item) for item in phases))
    return {
        "status": "PASS" if passed and cleanup else "FAIL",
        "phase": phase,
        "passed_phases": phases,
        "source_manifest_sha256": source_sha,
        "migration_head": HEADS[-1] if "migration_chain" in phases else None,
        "baseline": "synthetic_head9_post9_tables_excluded",
        "owned_schema_cleanup": cleanup,
        "production_mutations": False,
        "real_http_calls": 0,
        "real_telegram_calls": 0,
        "native_helper_sha256": hashlib.sha256(
            Path(__file__).with_name("native_test.py").read_bytes()
        ).hexdigest(),
        "lab_helpers_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }

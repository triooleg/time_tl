"""Offline frozen-schema parity. Does not pretend to execute PostgreSQL migrations."""

from importlib import import_module

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from time_tl.infrastructure.db.models import Base


def test_weather_migration_is_frozen_and_matches_all_orm_constraints():
    migration = import_module("migrations.versions.0012_weather_coverage")
    assert migration.down_revision == "0011_power_outages"
    tables = migration._schema()
    assert len(tables) == 7
    dialect = postgresql.dialect()
    for table in tables:
        orm = Base.metadata.tables[table.name]
        # DDL order of constraints need not be deterministic; compare individual pieces.
        assert list(table.columns.keys()) == list(orm.columns.keys())
        for column in table.columns:
            wanted = orm.c[column.name]
            assert str(column.type.compile(dialect)) == str(wanted.type.compile(dialect))
            assert column.nullable == wanted.nullable
            assert column.primary_key == wanted.primary_key
            assert str(column.server_default.arg if column.server_default else None) == str(
                wanted.server_default.arg if wanted.server_default else None
            )
        assert {str(c.name) for c in table.constraints} == {str(c.name) for c in orm.constraints}
        assert {str(CreateIndex(i).compile(dialect=dialect)) for i in table.indexes} == {
            str(CreateIndex(i).compile(dialect=dialect)) for i in orm.indexes
        }
        ddl = str(CreateTable(table).compile(dialect=dialect))
        assert "CREATE TABLE" in ddl
        orm_ddl = str(CreateTable(orm).compile(dialect=dialect))
        assert {line.strip().rstrip(",") for line in ddl.splitlines()} == {
            line.strip().rstrip(",") for line in orm_ddl.splitlines()
        }


def test_schema_has_no_enabled_source_or_delivery_seed():
    migration = import_module("migrations.versions.0012_weather_coverage")
    schema = {table.name: table for table in migration._schema()}
    assert str(schema["weather_locations"].c.enabled.server_default.arg) == "false"
    prefs = schema["user_weather_preferences"]
    assert str(prefs.c.weather_enabled.server_default.arg) == "false"
    assert str(prefs.c.forecast_requested.server_default.arg) == "false"


def test_weather_backup_preserves_existing_encryption_and_frozen_restore_validation():
    from scripts.deployment import verified_backup as backup

    assert (
        len(backup.SCHEMA_TABLES[backup.WEATHER_SCHEMA] - backup.SCHEMA_TABLES[backup.POWER_SCHEMA])
        == 7
    )
    script = backup.WEATHER_SCHEMA_INSPECTOR
    compile(script, "weather-restore-inspector", "exec")
    assert "if heads != ['0012_weather_coverage']" in script
    assert "weather.validate_schema(bind)" in script
    assert "power.validate_schema(bind)" in script
    assert "SET TRANSACTION READ ONLY" in script
    assert "command.upgrade" not in script
    assert "aad=b'TimeTL/RC24/revision14/0008_homework_foundation'" in backup.CRYPTO
    current = backup.REMINDER_SCHEMA_INSPECTOR
    compile(current, "reminder-restore-inspector", "exec")
    assert backup.SCHEMA_TABLES[backup.REMINDER_SCHEMA] == (
        backup.SCHEMA_TABLES[backup.WEATHER_SCHEMA]
    )
    assert "if heads != ['0014_calendar_reminders']" in current
    assert "alert.validate_schema(bind)" in current
    assert "reminders.validate_schema(bind)" in current
    assert "SET TRANSACTION READ ONLY" in current


def test_native_float8_and_time_rendering_are_canonical_but_changed_constraints_reject():
    migration = import_module("migrations.versions.0012_weather_coverage")
    expression = migration._weather_expression
    assert expression("temperature_c BETWEEN -100 AND 70") == expression(
        "((temperature_c >= '-100'::double precision) AND "
        "(temperature_c <= (70)::double precision))"
    )
    assert expression("'07:00'") == expression("'07:00:00'::time without time zone")
    assert expression("temperature_c BETWEEN -100 AND 70") == expression(
        "((temperature_c >= ('-100'::integer)::double precision) "
        "AND (temperature_c <= (70)::double precision))"
    )
    assert expression("value = '-100'") != expression("value = -100")
    assert expression("temperature_c BETWEEN -100 AND 70") != expression(
        "temperature_c BETWEEN -99 AND 70"
    )
    assert expression("latitude >= 44") != expression("latitude::real >= 44")
    table = next(t for t in migration._validation_schema() if t.name == "weather_locations")
    assert str(table.c.latitude.type.compile(postgresql.dialect())) == "DOUBLE PRECISION"


def test_weather_boolean_or_preserves_postgres_grouping_and_rejects_changed_logic():
    migration = import_module("migrations.versions.0012_weather_coverage")
    prior = import_module("migrations.versions.0009_calendar_foundation")
    frozen = prior._expression("NOT enabled OR approved_at IS NOT NULL")
    expression = migration._weather_expression
    expected = expression("NOT enabled OR approved_at IS NOT NULL")
    assert expected == expression("((NOT enabled) OR (approved_at IS NOT NULL))")
    assert expected != expression("NOT (enabled OR approved_at IS NOT NULL)")
    assert expected != expression("NOT enabled AND approved_at IS NOT NULL")
    assert expression("a OR b AND c") == expression("a OR (b AND c)")
    assert expression("a OR b AND c") != expression("(a OR b) AND c")
    assert expression("NOT (a OR b)") != expression("NOT a OR b")
    assert expression("a BETWEEN 1 AND 2 OR b") == expression("(a BETWEEN 1 AND 2) OR b")
    assert prior._expression("NOT enabled OR approved_at IS NOT NULL") == frozen


def test_weather_count_comparison_preserves_addition_precedence():
    expression = import_module("migrations.versions.0012_weather_coverage")._weather_expression
    expected = expression("successful_locations+failed_locations<=requested_locations")
    assert expected == expression(
        "((successful_locations + failed_locations) <= requested_locations)"
    )
    assert expected != expression("successful_locations+failed_locations>=requested_locations")
    assert expected != expression("successful_locations-failed_locations<=requested_locations")
    assert expression("a+b BETWEEN 1 AND 2") == expression("(a+b) BETWEEN 1 AND 2")


@pytest.mark.asyncio
async def test_opt_in_real_weather_migration_and_exact_inspection():
    """Only a disposable passwordless loopback DB, never any production DSN."""
    import os
    from unittest.mock import patch
    from uuid import uuid4

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect, text
    from sqlalchemy.engine import make_url
    from sqlalchemy.ext.asyncio import create_async_engine

    raw = os.environ.get("WEATHER_STORAGE_TEST_DATABASE_URL")
    if not raw:
        pytest.skip("WEATHER_STORAGE_TEST_DATABASE_URL unset; no migration DB connection attempted")
    url = make_url(raw)
    assert (
        url.drivername == "postgresql+asyncpg"
        and url.host == "127.0.0.1"
        and url.port is not None
        and 1024 < url.port < 65536
        and url.username == url.database == "weathercheck"
        and url.password is None
        and not url.query
    ), "Disposable local weathercheck DB only"
    schema = "weather_migration_" + uuid4().hex
    owner = create_async_engine(raw)
    scoped = None
    created = False
    migration = import_module("migrations.versions.0012_weather_coverage")
    try:
        async with owner.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            created = True
        scoped = create_async_engine(raw, connect_args={"server_settings": {"search_path": schema}})
        async with scoped.begin() as connection:

            def exercise(bind):
                frozen = migration._schema()
                for name in ("users", "outbox_messages"):
                    frozen[0].metadata.tables[name].create(bind)
                operations = Operations(MigrationContext.configure(bind))
                with patch.object(migration, "op", operations):
                    migration.upgrade()
                    migration.upgrade()  # Exact existing feature accepted, not repaired.
                    migration.validate_schema(bind)
                    savepoint = bind.begin_nested()
                    try:
                        bind.execute(
                            text(
                                "ALTER TABLE weather_locations DROP CONSTRAINT "
                                "ck_weather_locations_ukraine_latitude"
                            )
                        )
                        with pytest.raises(RuntimeError):
                            migration.validate_schema(bind)
                    finally:
                        savepoint.rollback()
                    migration.downgrade()
                    # Historical 0001 creates current ORM metadata first.
                    for table in frozen:
                        Base.metadata.tables[table.name].create(bind)
                    migration.upgrade()
                    migration.downgrade()
                    assert set(inspect(bind).get_table_names()) == {"users", "outbox_messages"}
                    frozen[0].create(bind)
                    with pytest.raises(RuntimeError, match="partial_weather_schema"):
                        migration.upgrade()

            await connection.run_sync(exercise)
    finally:
        if scoped is not None:
            await scoped.dispose()
        if created:
            # schema is generated above and this test owns it exclusively.
            async with owner.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await owner.dispose()

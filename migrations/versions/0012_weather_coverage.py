"""Frozen additive weather schema; no source or delivery is enabled."""

from __future__ import annotations

import re
from importlib import import_module
from types import FunctionType, SimpleNamespace

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0012_weather_coverage"
down_revision = "0011_power_outages"
branch_labels = None
depends_on = None


def _schema() -> tuple[sa.Table, ...]:
    m = sa.MetaData()
    for name in ("users", "outbox_messages"):
        sa.Table(name, m, sa.Column("id", UUID(as_uuid=True), primary_key=True))
    sa.Table(
        "weather_locations",
        m,
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("katottg_code", sa.String(19), nullable=False),
        sa.Column("region_id", sa.String(64), nullable=False),
        sa.Column("locality_name", sa.String(160), nullable=False),
        sa.Column("latitude", sa.Float(), nullable=False),
        sa.Column("longitude", sa.Float(), nullable=False),
        sa.Column("coverage_type", sa.String(16), nullable=False, server_default="BASE"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("provenance_url", sa.Text(), nullable=False),
        sa.Column("provenance_sha256", sa.String(64), nullable=False),
        sa.Column("provenance_version", sa.String(80), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "NOT enabled OR approved_at IS NOT NULL",
            name=sa.schema.conv("ck_weather_locations_approval_required"),
        ),
        sa.CheckConstraint(
            "coverage_type IN ('BASE','EXTENDED')",
            name=sa.schema.conv("ck_weather_locations_coverage_allowed"),
        ),
        sa.CheckConstraint(
            "katottg_code ~ '^UA[0-9]{17}$'",
            name=sa.schema.conv("ck_weather_locations_katottg_shape"),
        ),
        sa.CheckConstraint(
            "length(provenance_sha256)=64",
            name=sa.schema.conv("ck_weather_locations_provenance_digest"),
        ),
        sa.CheckConstraint(
            "latitude BETWEEN 44 AND 53.5",
            name=sa.schema.conv("ck_weather_locations_ukraine_latitude"),
        ),
        sa.CheckConstraint(
            "longitude BETWEEN 22 AND 41",
            name=sa.schema.conv("ck_weather_locations_ukraine_longitude"),
        ),
    )
    m.tables["weather_locations"].primary_key.name = sa.schema.conv("pk_weather_locations")
    sa.Index(
        sa.schema.conv("ix_weather_locations_katottg_code"),
        m.tables["weather_locations"].c["katottg_code"],
        unique=False,
    )
    sa.Index(
        sa.schema.conv("ix_weather_locations_region_id"),
        m.tables["weather_locations"].c["region_id"],
        unique=False,
    )
    sa.Index(
        sa.schema.conv("uq_weather_base_location"),
        m.tables["weather_locations"].c["katottg_code"],
        unique=True,
        postgresql_where=sa.text("coverage_type='BASE'"),
    )
    sa.Table(
        "weather_forecast_runs",
        m,
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("requested_locations", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("successful_locations", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_locations", sa.Integer(), nullable=False, server_default="0"),
        sa.CheckConstraint(
            "successful_locations+failed_locations<=requested_locations",
            name=sa.schema.conv("ck_weather_forecast_runs_counts_bounded"),
        ),
        sa.CheckConstraint(
            "requested_locations>=0 AND successful_locations>=0 AND failed_locations>=0",
            name=sa.schema.conv("ck_weather_forecast_runs_counts_nonnegative"),
        ),
    )
    m.tables["weather_forecast_runs"].primary_key.name = sa.schema.conv("pk_weather_forecast_runs")
    sa.Index(
        sa.schema.conv("ix_weather_runs_provider_started"),
        m.tables["weather_forecast_runs"].c["provider"],
        m.tables["weather_forecast_runs"].c["started_at"],
        unique=False,
    )
    sa.Table(
        "weather_forecast_snapshots",
        m,
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("weather_location_id", UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("forecast_run_id", UUID(as_uuid=True), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provider_updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cache_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("forecast_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("last_modified", sa.String(160), nullable=True),
        sa.CheckConstraint(
            "status IN ('FRESH','STALE','PARTIAL','UNKNOWN')",
            name=sa.schema.conv("ck_weather_forecast_snapshots_status_allowed"),
        ),
        sa.ForeignKeyConstraint(
            ["forecast_run_id"],
            ["weather_forecast_runs.id"],
            name=sa.schema.conv(
                "fk_weather_forecast_snapshots_forecast_run_id_weather_forecast_runs"
            ),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["weather_location_id"],
            ["weather_locations.id"],
            name=sa.schema.conv(
                "fk_weather_forecast_snapshots_weather_location_id_weather_locations"
            ),
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "weather_location_id",
            "provider",
            "forecast_hash",
            name=sa.schema.conv("uq_weather_forecast_snapshots_weather_location_id"),
        ),
    )
    m.tables["weather_forecast_snapshots"].primary_key.name = sa.schema.conv(
        "pk_weather_forecast_snapshots"
    )
    sa.Index(
        sa.schema.conv("ix_weather_snapshot_location_fetched"),
        m.tables["weather_forecast_snapshots"].c["weather_location_id"],
        m.tables["weather_forecast_snapshots"].c["provider"],
        m.tables["weather_forecast_snapshots"].c["fetched_at"],
        unique=False,
    )
    sa.Table(
        "weather_forecast_hours",
        m,
        sa.Column("snapshot_id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("forecast_at", sa.DateTime(timezone=True), primary_key=True, nullable=False),
        sa.Column("temperature_c", sa.Float(), nullable=True),
        sa.Column("feels_like_c", sa.Float(), nullable=True),
        sa.Column("precipitation_mm", sa.Float(), nullable=True),
        sa.Column("precipitation_probability", sa.Float(), nullable=True),
        sa.Column("snowfall_cm", sa.Float(), nullable=True),
        sa.Column("wind_speed_ms", sa.Float(), nullable=True),
        sa.Column("wind_gust_ms", sa.Float(), nullable=True),
        sa.Column("cloud_cover_percent", sa.Float(), nullable=True),
        sa.Column("weather_code", sa.String(64), nullable=True),
        sa.CheckConstraint(
            "cloud_cover_percent BETWEEN 0 AND 100",
            name=sa.schema.conv("ck_weather_forecast_hours_cloud_range"),
        ),
        sa.CheckConstraint(
            "precipitation_mm>=0 AND snowfall_cm>=0",
            name=sa.schema.conv("ck_weather_forecast_hours_precipitation_nonnegative"),
        ),
        sa.CheckConstraint(
            "precipitation_probability BETWEEN 0 AND 100",
            name=sa.schema.conv("ck_weather_forecast_hours_probability_range"),
        ),
        sa.CheckConstraint(
            "temperature_c BETWEEN -100 AND 70",
            name=sa.schema.conv("ck_weather_forecast_hours_temperature_range"),
        ),
        sa.CheckConstraint(
            "wind_speed_ms>=0 AND wind_gust_ms>=0",
            name=sa.schema.conv("ck_weather_forecast_hours_wind_nonnegative"),
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["weather_forecast_snapshots.id"],
            name=sa.schema.conv("fk_weather_forecast_hours_snapshot_id_weather_forecast_snapshots"),
            ondelete="CASCADE",
        ),
    )
    m.tables["weather_forecast_hours"].primary_key.name = sa.schema.conv(
        "pk_weather_forecast_hours"
    )
    sa.Index(
        sa.schema.conv("ix_weather_hours_valid_time"),
        m.tables["weather_forecast_hours"].c["forecast_at"],
        unique=False,
    )
    sa.Table(
        "weather_change_events",
        m,
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("location_id", UUID(as_uuid=True), nullable=False),
        sa.Column("snapshot_id", UUID(as_uuid=True), nullable=True),
        sa.Column("event_key", sa.String(64), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("source_revision", sa.String(40), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("expected_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expected_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("previous_value", sa.Float(), nullable=True),
        sa.Column("current_value", sa.Float(), nullable=True),
        sa.Column("severity", sa.String(16), nullable=False, server_default="ADVISORY"),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="ACTIVE"),
        sa.CheckConstraint(
            "kind IN ('UPCOMING','CORRECTION','CANCELLATION')",
            name=sa.schema.conv("ck_weather_change_events_kind_allowed"),
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE','CANCELLED','EXPIRED')",
            name=sa.schema.conv("ck_weather_change_events_status_allowed"),
        ),
        sa.CheckConstraint(
            "expected_end>expected_start",
            name=sa.schema.conv("ck_weather_change_events_valid_interval"),
        ),
        sa.ForeignKeyConstraint(
            ["location_id"],
            ["weather_locations.id"],
            name=sa.schema.conv("fk_weather_change_events_location_id_weather_locations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["weather_forecast_snapshots.id"],
            name=sa.schema.conv("fk_weather_change_events_snapshot_id_weather_forecast_snapshots"),
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint("event_key", name=sa.schema.conv("uq_weather_change_events_event_key")),
    )
    m.tables["weather_change_events"].primary_key.name = sa.schema.conv("pk_weather_change_events")
    sa.Index(
        sa.schema.conv("ix_weather_events_location_time"),
        m.tables["weather_change_events"].c["location_id"],
        m.tables["weather_change_events"].c["expected_start"],
        m.tables["weather_change_events"].c["status"],
        unique=False,
    )
    sa.Table(
        "user_weather_preferences",
        m,
        sa.Column("user_id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "forecast_requested", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("weather_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "daily_forecast_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("daily_forecast_time", sa.Time(), nullable=False, server_default="07:00"),
        sa.Column(
            "weather_changes_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column(
            "rain_alert_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column(
            "snow_alert_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column(
            "wind_alert_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column(
            "temperature_alert_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "frost_alert_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column("notification_lead_minutes", sa.Integer(), nullable=False, server_default="120"),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "notification_lead_minutes BETWEEN 0 AND 1440",
            name=sa.schema.conv("ck_user_weather_preferences_lead_range"),
        ),
        sa.CheckConstraint(
            "revision>=1", name=sa.schema.conv("ck_user_weather_preferences_revision_positive")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=sa.schema.conv("fk_user_weather_preferences_user_id_users"),
            ondelete="CASCADE",
        ),
    )
    m.tables["user_weather_preferences"].primary_key.name = sa.schema.conv(
        "pk_user_weather_preferences"
    )
    sa.Index(
        sa.schema.conv("ix_weather_preferences_enabled"),
        m.tables["user_weather_preferences"].c["weather_enabled"],
        m.tables["user_weather_preferences"].c["forecast_requested"],
        unique=False,
    )
    sa.Table(
        "weather_notification_deliveries",
        m,
        sa.Column("user_id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("notification_key", sa.String(64), primary_key=True, nullable=False),
        sa.Column("outbox_id", UUID(as_uuid=True), nullable=True),
        sa.Column("event_keys", JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["outbox_id"],
            ["outbox_messages.id"],
            name=sa.schema.conv("fk_weather_notification_deliveries_outbox_id_outbox_messages"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=sa.schema.conv("fk_weather_notification_deliveries_user_id_users"),
            ondelete="CASCADE",
        ),
    )
    m.tables["weather_notification_deliveries"].primary_key.name = sa.schema.conv(
        "pk_weather_notification_deliveries"
    )
    return tuple(
        m.tables[name]
        for name in (
            "weather_locations",
            "weather_forecast_runs",
            "weather_forecast_snapshots",
            "weather_forecast_hours",
            "weather_change_events",
            "user_weather_preferences",
            "weather_notification_deliveries",
        )
    )


def _weather_canonical(tokens: list[str]) -> str:
    """Preserve SQL OR/AND/NOT precedence in this new migration only."""
    prior = import_module("migrations.versions.0009_calendar_foundation")
    tokens = prior._outer(tokens)
    depth = 0
    disjunctions = []
    top = []
    for position, token in enumerate(tokens):
        if token in ("(", "["):
            depth += 1
        elif token in (")", "]"):
            depth -= 1
        elif depth == 0:
            top.append(position)
            if token == "or":
                disjunctions.append(position)
    if disjunctions:
        position = disjunctions[-1]
        return (
            f"or({_weather_canonical(tokens[:position])},"
            f"{_weather_canonical(tokens[position + 1 :])})"
        )
    # Comparison binds less tightly than addition. The frozen parser walks
    # operators left to right, so PG's parentheses around a sum otherwise
    # produce a different result for the exact same CHECK constraint.
    between = False
    conjunction = False
    for position in top:
        if tokens[position] == "between":
            between = True
        elif tokens[position] == "and":
            if between:
                between = False
            else:
                conjunction = True
    if not conjunction and tokens[:1] != ["not"]:
        comparisons = {"=", ">=", "<=", ">", "~", "in", "is", "between"}
        relation = next((p for p in top if tokens[p] in comparisons), None)
        if relation is not None:
            tokens = ["("] + tokens[:relation] + [")", tokens[relation]] + tokens[relation + 1 :]
    # Existing AND/BETWEEN/function/NOT handling recurses through the same
    # OR-aware parser. Neither frozen migration module is mutated.
    canonical = FunctionType(
        prior._canonical.__code__,
        {**prior._canonical.__globals__, "_canonical": _weather_canonical},
    )
    return canonical(tokens)


def _weather_expression(value: object) -> str:
    """Normalize only PostgreSQL's equivalent float8/time literal rendering."""
    rendered = str(value)
    rendered = re.sub(
        r"'(-?[0-9]+(?:\.[0-9]+)?)'::(?:double precision|integer)\b", r"\1", rendered
    )
    # Work outside strings: source names/regular expressions remain byte-exact.
    pieces = re.split(r"('(?:''|[^'])*')", rendered)
    for position in range(0, len(pieces), 2):
        pieces[position] = re.sub(
            r"::(?:double precision|time without time zone)\b", "", pieces[position]
        )
    rendered = "".join(pieces)
    rendered = re.sub(r"'([0-9]{2}:[0-9]{2})'", r"'\1:00'", rendered)
    prior = import_module("migrations.versions.0009_calendar_foundation")
    expression = FunctionType(
        prior._expression.__code__,
        {**prior._expression.__globals__, "_canonical": _weather_canonical},
    )
    return expression(rendered)


def _validation_schema() -> tuple[sa.Table, ...]:
    tables = _schema()
    for table in tables:
        for column in table.columns:
            # Generic Float emits FLOAT; PostgreSQL stores/refelects it as float8.
            # Require exactly DOUBLE PRECISION, not REAL or arbitrary numeric.
            if isinstance(column.type, sa.Float) and column.type.precision is None:
                column.type = sa.Double()
    return tables


def validate_schema(connection: sa.Connection) -> None:
    # Reuse the frozen exact type/constraint/index validator, with this
    # migration's frozen schema and without mutating the prior module.
    prior = import_module("migrations.versions.0011_power_outages")
    calendar = import_module("migrations.versions.0009_calendar_foundation")
    local_calendar = SimpleNamespace(**vars(calendar))
    local_calendar._expression = _weather_expression
    local_calendar._validate = FunctionType(
        calendar._validate.__code__,
        {**calendar._validate.__globals__, "_expression": _weather_expression},
    )

    def local_import(name: str):
        return local_calendar if name == calendar.__name__ else import_module(name)

    validate = FunctionType(
        prior.validate_schema.__code__,
        {
            **prior.validate_schema.__globals__,
            "_schema": _validation_schema,
            "import_module": local_import,
        },
    )
    validate(connection)


def upgrade() -> None:
    tables = _schema()
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    present = existing.intersection(table.name for table in tables)
    if present and present != {table.name for table in tables}:
        raise RuntimeError("partial_weather_schema")
    if not present:
        for table in tables:
            table.create(op.get_bind())
    validate_schema(op.get_bind())


def downgrade() -> None:
    for table in reversed(_schema()):
        table.drop(op.get_bind())

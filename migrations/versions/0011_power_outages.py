"""Independent power schedules, subscriptions and durable notification identity.

Additive migration only. No provider is enabled by applying this migration.
The schema is frozen here rather than imported from mutable application models.
Downgrade deletes only this feature's tables; operational rollback disables it.
"""

from __future__ import annotations

from importlib import import_module
from types import FunctionType, SimpleNamespace

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0011_power_outages"
down_revision = "0010_personal_calendar_overrides"
branch_labels = None
depends_on = None

NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def _schema() -> tuple[sa.Table, ...]:
    m = sa.MetaData(naming_convention=NAMING)
    for name in ("users", "outbox_messages"):
        sa.Table(name, m, sa.Column("id", UUID(as_uuid=True), primary_key=True))

    def timestamps() -> list[sa.Column]:
        return [
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
        ]

    providers = sa.Table(
        "power_providers",
        m,
        sa.Column("provider_id", sa.String(24), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("official_url", sa.String(240), nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("source_status", sa.String(16), nullable=False),
        sa.Column("last_success_at", sa.DateTime(timezone=True)),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("last_error_code", sa.String(64)),
        sa.Column("consecutive_failures", sa.Integer, nullable=False, server_default="0"),
        sa.Column("configuration_revision", sa.Integer, nullable=False, server_default="1"),
        sa.Column("refresh_requested", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.CheckConstraint(
            "provider_id IN ('dtek_dnipro','dtek_kyiv','dtek_kyiv_region',"
            "'dtek_odesa','dtek_donetsk','cek_dnipro')",
            name="provider_allowed",
        ),
        sa.CheckConstraint(
            "source_status IN ('FRESH','STALE','UNAVAILABLE','INVALID')", name="status_allowed"
        ),
        sa.CheckConstraint("consecutive_failures >= 0", name="failure_count"),
        sa.CheckConstraint("configuration_revision >= 1", name="configuration_revision"),
        *timestamps(),
    )
    group_check = "group_id ~ '^[1-6]\\.[12]$'"
    snapshots = sa.Table(
        "power_schedule_snapshots",
        m,
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "provider_id",
            sa.ForeignKey("power_providers.provider_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("group_id", sa.String(3), nullable=False),
        sa.Column("valid_for_date", sa.Date, nullable=False),
        sa.Column("schedule_type", sa.String(20), nullable=False),
        sa.Column("mapping_version", sa.String(16), nullable=False),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("source_kind", sa.String(16), nullable=False, server_default="official"),
        sa.Column("publication_status", sa.String(16), nullable=False, server_default="PLANNED"),
        sa.Column("source_updated_at", sa.DateTime(timezone=True)),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("schema_version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("is_current", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.UniqueConstraint(
            "provider_id", "group_id", "valid_for_date", "schedule_type", "revision"
        ),
        sa.CheckConstraint(group_check, name="group_allowed"),
        sa.CheckConstraint(
            "schedule_type IN ('CONFIRMED_DAY','FORECAST_WEEK')", name="schedule_type_allowed"
        ),
        sa.CheckConstraint("revision >= 1 AND schema_version = 1", name="version_allowed"),
        sa.CheckConstraint("source_kind = 'official'", name="official_only"),
        sa.CheckConstraint(
            "publication_status IN ('PLANNED','NO_SCHEDULE','EMERGENCY','UNKNOWN')",
            name="publication_allowed",
        ),
        *timestamps(),
    )
    sa.Index(
        "uq_power_current_schedule",
        snapshots.c.provider_id,
        snapshots.c.group_id,
        snapshots.c.valid_for_date,
        snapshots.c.schedule_type,
        unique=True,
        postgresql_where=sa.text("is_current"),
    )
    intervals = sa.Table(
        "power_schedule_intervals",
        m,
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "snapshot_id",
            sa.ForeignKey("power_schedule_snapshots.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("group_id", sa.String(3), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.CheckConstraint("ends_at > starts_at", name="positive_duration"),
        sa.CheckConstraint(
            "state IN ('OUTAGE_CONFIRMED','POWER_EXPECTED','POSSIBLE_OUTAGE',"
            "'UNKNOWN','NO_SCHEDULE')",
            name="state_allowed",
        ),
        sa.CheckConstraint(group_check, name="group_allowed"),
    )
    subscriptions = sa.Table(
        "user_power_subscriptions",
        m,
        sa.Column("user_id", sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("provider_id", sa.ForeignKey("power_providers.provider_id"), nullable=False),
        sa.Column("group_id", sa.String(3), nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column(
            "outage_notifications_enabled",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "restoration_notifications_enabled",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column("outage_lead_minutes", sa.Integer, nullable=False, server_default="30"),
        sa.Column("restoration_lead_minutes", sa.Integer, nullable=False, server_default="15"),
        sa.Column("revision", sa.Integer, nullable=False, server_default="1"),
        sa.CheckConstraint(group_check, name="group_allowed"),
        sa.CheckConstraint(
            "outage_lead_minutes >= 0 AND outage_lead_minutes <= 180 AND "
            "restoration_lead_minutes >= 0 AND restoration_lead_minutes <= 180",
            name="lead_allowed",
        ),
        sa.CheckConstraint("revision >= 1", name="revision_allowed"),
        *timestamps(),
    )
    sa.Index(
        "ix_power_subscriptions_provider_group",
        subscriptions.c.provider_id,
        subscriptions.c.group_id,
    )
    events = sa.Table(
        "power_notification_events",
        m,
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            sa.ForeignKey("user_power_subscriptions.user_id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "snapshot_id",
            sa.ForeignKey("power_schedule_snapshots.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "outbox_id", sa.ForeignKey("outbox_messages.id", ondelete="SET NULL"), unique=True
        ),
        sa.Column("event_key", sa.String(255), nullable=False, unique=True),
        sa.Column("subscription_revision", sa.Integer, nullable=False),
        sa.Column("schedule_revision", sa.Integer, nullable=False),
        sa.Column("transition_type", sa.String(40), nullable=False),
        sa.Column("transition_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("planned_send_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="PENDING"),
        sa.CheckConstraint("status IN ('PENDING','SENT','CANCELED')", name="status_allowed"),
        sa.CheckConstraint(
            "transition_type IN ('POWER_OUTAGE_UPCOMING',"
            "'POWER_RESTORATION_UPCOMING','POWER_SCHEDULE_CHANGED')",
            name="transition_allowed",
        ),
        sa.CheckConstraint(
            "subscription_revision >= 1 AND schedule_revision >= 1", name="revisions_allowed"
        ),
        *timestamps(),
    )
    sa.Index("ix_power_events_snapshot_status", events.c.snapshot_id, events.c.status)
    return providers, snapshots, intervals, subscriptions, events


def upgrade() -> None:
    tables = _schema()
    # Historical 0001 creates current ORM metadata on a brand-new database.
    # Accept that path only after exact read-only validation; never repair a
    # partial or incompatible pre-existing feature schema silently.
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    present = existing.intersection(table.name for table in tables)
    if present:
        if present != {table.name for table in tables}:
            raise RuntimeError("partial_power_schema")
        validate_schema(op.get_bind())
    else:
        for table in tables:
            table.create(op.get_bind())
    op.bulk_insert(
        tables[0],
        [
            {
                "provider_id": "dtek_dnipro",
                "name": "ДТЕК Дніпровські електромережі",
                "official_url": "https://www.dtek-dnem.com.ua/ua/shutdowns",
                "enabled": False,
                "source_status": "UNAVAILABLE",
                "last_error_code": "SOURCE_GATE_BLOCKED",
            },
            {
                "provider_id": "cek_dnipro",
                "name": "Центральна енергетична компанія (ЦЕК)",
                "official_url": "https://cek.dp.ua/index.php/cpojivaham/vidkliuchennia.html",
                "enabled": False,
                "source_status": "UNAVAILABLE",
                "last_error_code": "SOURCE_GATE_BLOCKED",
            },
            {
                "provider_id": "dtek_kyiv",
                "name": "ДТЕК Київські електромережі",
                "official_url": "https://www.dtek-kem.com.ua/ua/shutdowns",
                "enabled": False,
                "source_status": "UNAVAILABLE",
                "last_error_code": "SOURCE_GATE_BLOCKED",
            },
            {
                "provider_id": "dtek_kyiv_region",
                "name": "ДТЕК Київські регіональні електромережі",
                "official_url": "https://www.dtek-krem.com.ua/ua/shutdowns",
                "enabled": False,
                "source_status": "UNAVAILABLE",
                "last_error_code": "SOURCE_GATE_BLOCKED",
            },
            {
                "provider_id": "dtek_odesa",
                "name": "ДТЕК Одеські електромережі",
                "official_url": "https://www.dtek-oem.com.ua/ua/shutdowns",
                "enabled": False,
                "source_status": "UNAVAILABLE",
                "last_error_code": "SOURCE_GATE_BLOCKED",
            },
            {
                "provider_id": "dtek_donetsk",
                "name": "ДТЕК Донецькі електромережі",
                "official_url": "https://www.dtek-dem.com.ua/ua/shutdowns",
                "enabled": False,
                "source_status": "UNAVAILABLE",
                "last_error_code": "SOURCE_GATE_BLOCKED",
            },
        ],
    )
    validate_schema(op.get_bind())


def validate_schema(connection: sa.Connection) -> None:
    """Read-only exact frozen schema validation for backup inspectors."""
    prior = import_module("migrations.versions.0009_calendar_foundation")
    inspector = sa.inspect(connection)
    tables = _schema()
    for table in tables:
        actual_unique = {
            (item["name"], tuple(item["column_names"]))
            for item in inspector.get_unique_constraints(table.name)
        }
        wanted_unique = {
            (item.name, tuple(column.name for column in item.columns))
            for item in table.constraints
            if isinstance(item, sa.UniqueConstraint)
        }
        if actual_unique != wanted_unique:
            raise RuntimeError("power_schema_unique_mismatch")
        actual_indexes = {
            item["name"]: item
            for item in inspector.get_indexes(table.name)
            if not item.get("duplicates_constraint")
        }
        if set(actual_indexes) != {item.name for item in table.indexes}:
            raise RuntimeError("power_schema_index_mismatch")
        for index in table.indexes:
            actual = actual_indexes[index.name]
            options = actual.get("dialect_options", {})
            wanted_where = index.dialect_options["postgresql"].get("where")
            actual_where = options.get("postgresql_where")
            if (
                tuple(actual["column_names"]) != tuple(column.name for column in index.columns)
                or bool(actual["unique"]) != bool(index.unique)
                or prior._expression(str(actual_where)) != prior._expression(str(wanted_where))
                or actual.get("include_columns")
                or actual.get("column_sorting")
                or actual.get("expressions") not in (None, [], actual["column_names"])
                or any(
                    value not in (None, False, "", [], (), {})
                    for key, value in options.items()
                    if key != "postgresql_where"
                )
            ):
                raise RuntimeError("power_schema_index_options_mismatch")

    class ValidatedInspector:
        def __getattr__(self, name):
            return getattr(inspector, name)

        def get_unique_constraints(self, name):
            return []  # exact constraints already validated above

        def get_indexes(self, name):
            return [
                {
                    "name": item["name"],
                    "column_names": item["column_names"],
                    "unique": item["unique"],
                }
                for item in inspector.get_indexes(name)
                if not item.get("duplicates_constraint")
            ]  # predicates/options already validated above

    # 0009's frozen type compiler obtains dialect through Alembic's proxy.
    # Bind only this invocation to our connection without replacing/removing an
    # outer migration proxy, and without mutating the frozen module's globals.
    validate = FunctionType(
        prior._validate.__code__,
        {**prior._validate.__globals__, "op": SimpleNamespace(get_bind=lambda: connection)},
    )
    validate(ValidatedInspector(), tables)


def downgrade() -> None:
    for table in reversed(_schema()):
        table.drop(op.get_bind())

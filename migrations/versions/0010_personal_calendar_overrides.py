"""Add actor-only occurrence changes without touching shared or delivery data.

Forward: one additive table, FK cascade and bounded scalar checks. Online schema
inspection is required. Operational rollback disables the personal-change UI and
retains this table. Explicit downgrade irreversibly deletes personal changes and
CAS tombstones; it never restores application data or modifies shared calendars.
"""

from __future__ import annotations

import re
from importlib import import_module

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects.postgresql import UUID

revision = "0010_personal_calendar_overrides"
down_revision = "0009_calendar_foundation"
branch_labels = None
depends_on = None


def _schema() -> sa.Table:
    """Frozen scalar schema; never import evolving application ORM metadata."""
    metadata = sa.MetaData()
    for name in ("users", "calendars"):
        sa.Table(name, metadata, sa.Column("id", UUID(as_uuid=True), primary_key=True))
    table = sa.Table(
        "personal_calendar_overrides",
        metadata,
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey(
                "users.id",
                ondelete="CASCADE",
                name="fk_personal_calendar_overrides_user_id_users",
            ),
            primary_key=True,
        ),
        sa.Column(
            "calendar_id",
            UUID(as_uuid=True),
            sa.ForeignKey(
                "calendars.id",
                ondelete="CASCADE",
                name="fk_personal_calendar_overrides_calendar_id_calendars",
            ),
            primary_key=True,
        ),
        sa.Column("snapshot_version", sa.Integer, primary_key=True),
        sa.Column("source_day", sa.Date, primary_key=True),
        sa.Column("event_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("action", sa.String(8), nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("target_day", sa.Date),
        sa.Column("start_time", sa.Time),
        sa.Column("end_time", sa.Time),
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
            "snapshot_version BETWEEN 1 AND 2147483646",
            name="ck_personal_calendar_overrides_snapshot_range",
        ),
        sa.CheckConstraint(
            "version BETWEEN 1 AND 2147483646", name="ck_personal_calendar_overrides_version_range"
        ),
        sa.CheckConstraint(
            "action IN ('CANCEL','MOVE','RESET')",
            name="ck_personal_calendar_overrides_action_allowed",
        ),
        sa.CheckConstraint(
            "(action = 'MOVE' AND target_day IS NOT NULL AND start_time IS NOT NULL "
            "AND end_time IS NOT NULL AND end_time > start_time) OR "
            "(action IN ('CANCEL','RESET') AND target_day IS NULL "
            "AND start_time IS NULL AND end_time IS NULL)",
            name="ck_personal_calendar_overrides_action_shape",
        ),
        sa.CheckConstraint(
            "(start_time IS NULL OR EXTRACT(SECOND FROM start_time) = 0) AND "
            "(end_time IS NULL OR EXTRACT(SECOND FROM end_time) = 0)",
            name="ck_personal_calendar_overrides_minute_precision",
        ),
    )
    sa.Index("ix_personal_calendar_overrides_calendar_id", table.c.calendar_id)
    return table


def _check_expression(value: object) -> str:
    """Keep logical grouping; normalize only PostgreSQL's known scalar casts."""
    prior = import_module("migrations.versions.0009_calendar_foundation")
    pieces = re.split(r"('(?:''|[^'])*')", str(value))
    for index in range(0, len(pieces), 2):
        pieces[index] = re.sub(
            r"::(?:text|character varying|integer|numeric)\b(?:\[\])?",
            "",
            pieces[index].lower(),
        )
    tokens = re.findall(
        r"'(?:''|[^'])*'|[a-z_][a-z_0-9]*|[0-9]+|>=|<=|<>|!=|[^\s]", "".join(pieces)
    )

    def canonical(items: list[str]) -> str:
        items = prior._outer(items)
        depth, between = 0, False
        operators: dict[str, list[int]] = {"or": [], "and": []}
        for index, token in enumerate(items):
            if token in ("(", "["):
                depth += 1
            elif token in (")", "]"):
                depth -= 1
            elif depth == 0:
                if token == "between":
                    between = True
                elif token == "and" and between:
                    between = False
                elif token in operators:
                    operators[token].append(index)
        for operator in ("or", "and"):
            if operators[operator]:
                index = operators[operator][-1]
                return f"{operator}({canonical(items[:index])},{canonical(items[index + 1 :])})"
        return str(prior._canonical(items))

    return canonical(tokens)


def _validate(table: sa.Table) -> None:
    # Reuse the preceding immutable revision's conservative full column/default/
    # PK/FK/check/index inspector, not the application's current metadata.
    validator = import_module("migrations.versions.0009_calendar_foundation")
    inspector = sa.inspect(op.get_bind())
    expected = {
        item.name: str(item.sqltext)
        for item in table.constraints
        if isinstance(item, sa.CheckConstraint)
    }
    observed = {
        item["name"]: item["sqltext"] for item in inspector.get_check_constraints(table.name)
    }
    if set(observed) != set(expected) or any(
        _check_expression(observed[name]) != _check_expression(wanted)
        for name, wanted in expected.items()
    ):
        raise RuntimeError("personal calendar check mismatch requires operator review")

    class ValidatedChecks:
        # The prior inspector does not parse OR. After conservative grouped
        # semantic equality above, delegate every other strict schema check and
        # provide its exact expected check spelling, without altering old globals.
        def __getattr__(self, name: str):
            return getattr(inspector, name)

        def get_check_constraints(self, name: str):
            if name != table.name:
                raise RuntimeError("unexpected personal calendar table")
            return [{"name": key, "sqltext": value} for key, value in expected.items()]

    validator._validate(ValidatedChecks(), (table,))


def upgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("personal calendar migration requires online inspection")
    table = _schema()
    if table.name not in sa.inspect(op.get_bind()).get_table_names():
        table.create(op.get_bind(), checkfirst=False)
    _validate(table)


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("personal calendar migration requires online inspection")
    table = _schema()
    if table.name in sa.inspect(op.get_bind()).get_table_names():
        _validate(table)
        table.drop(op.get_bind(), checkfirst=False)

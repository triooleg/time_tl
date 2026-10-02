"""Add isolated explicit calendars; online schema validation is mandatory.

Downgrade destroys calendar/membership/invitation data. Operational rollback is
disabling CALENDARS_ENABLED and retaining this additive schema, not downgrading.
No automatic legacy schedule conversion or notification changes.
"""

from __future__ import annotations

import re

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0009_calendar_foundation"
down_revision = "0008_homework_foundation"
branch_labels = None
depends_on = None


def _schema() -> tuple[sa.Table, ...]:
    """Frozen revision definitions, independent of future ORM metadata."""
    metadata = sa.MetaData()
    sa.Table("users", metadata, sa.Column("id", UUID(as_uuid=True), primary_key=True))

    def timestamp(name: str, *, nullable: bool = False, default: bool = False) -> sa.Column:
        return sa.Column(
            name,
            sa.DateTime(timezone=True),
            nullable=nullable,
            server_default=sa.text("now()") if default else None,
        )

    calendars = sa.Table(
        "calendars",
        metadata,
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "owner_user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE", name="fk_calendars_owner_user_id_users"),
            nullable=False,
        ),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("snapshot_payload", JSONB, nullable=False),
        sa.Column("owner_visible", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("snapshot_version", sa.Integer, nullable=False, server_default="1"),
        timestamp("created_at", default=True),
        timestamp("updated_at", default=True),
        sa.CheckConstraint(
            "kind IN ('SCHOOL','PERSONAL','ACTIVITY')", name="ck_calendars_kind_allowed"
        ),
        sa.CheckConstraint(
            "char_length(btrim(name)) BETWEEN 1 AND 120", name="ck_calendars_name_length"
        ),
        sa.CheckConstraint("version >= 1", name="ck_calendars_version_positive"),
        sa.CheckConstraint("snapshot_version >= 1", name="ck_calendars_snapshot_version_positive"),
        sa.CheckConstraint(
            "jsonb_typeof(snapshot_payload) = 'object'", name="ck_calendars_snapshot_object"
        ),
    )
    sa.Index("ix_calendars_owner_user_id", calendars.c.owner_user_id)
    memberships = sa.Table(
        "calendar_memberships",
        metadata,
        sa.Column(
            "calendar_id",
            UUID(as_uuid=True),
            sa.ForeignKey(
                "calendars.id",
                ondelete="CASCADE",
                name="fk_calendar_memberships_calendar_id_calendars",
            ),
            primary_key=True,
        ),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey(
                "users.id", ondelete="CASCADE", name="fk_calendar_memberships_user_id_users"
            ),
            primary_key=True,
        ),
        sa.Column("role", sa.String(16), nullable=False, server_default="MEMBER"),
        sa.Column("visible", sa.Boolean, nullable=False, server_default=sa.text("true")),
        timestamp("created_at", default=True),
        timestamp("updated_at", default=True),
        sa.CheckConstraint(
            "role IN ('MEMBER','EDITOR','ADMIN')", name="ck_calendar_memberships_role_allowed"
        ),
    )
    sa.Index("ix_calendar_memberships_user_id", memberships.c.user_id)
    invitations = sa.Table(
        "calendar_invitations",
        metadata,
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column(
            "calendar_id",
            UUID(as_uuid=True),
            sa.ForeignKey(
                "calendars.id",
                ondelete="CASCADE",
                name="fk_calendar_invitations_calendar_id_calendars",
            ),
            nullable=False,
        ),
        sa.Column(
            "target_user_id",
            UUID(as_uuid=True),
            sa.ForeignKey(
                "users.id", ondelete="CASCADE", name="fk_calendar_invitations_target_user_id_users"
            ),
            nullable=False,
        ),
        sa.Column(
            "issuer_user_id",
            UUID(as_uuid=True),
            sa.ForeignKey(
                "users.id", ondelete="CASCADE", name="fk_calendar_invitations_issuer_user_id_users"
            ),
            nullable=False,
        ),
        sa.Column("calendar_version", sa.Integer, nullable=False),
        timestamp("created_at", default=True),
        timestamp("expires_at"),
        timestamp("consumed_at", nullable=True),
        timestamp("revoked_at", nullable=True),
        sa.CheckConstraint(
            "token_hash ~ '^[0-9a-f]{64}$'", name="ck_calendar_invitations_token_hash_shape"
        ),
        sa.CheckConstraint(
            "calendar_version >= 1", name="ck_calendar_invitations_version_positive"
        ),
        sa.CheckConstraint(
            "expires_at > created_at AND expires_at <= created_at + interval '24 hours'",
            name="ck_calendar_invitations_expiry_bounded",
        ),
        sa.CheckConstraint(
            "NOT (consumed_at IS NOT NULL AND revoked_at IS NOT NULL)",
            name="ck_calendar_invitations_terminal",
        ),
    )
    for column in ("calendar_id", "target_user_id", "issuer_user_id"):
        sa.Index(f"ix_calendar_invitations_{column}", invitations.c[column])
    sa.Index(
        "ix_calendar_invitations_expiry_token", invitations.c.expires_at, invitations.c.token_hash
    )
    return calendars, memberships, invitations


def _expression(value: object) -> str:
    # A small conservative canonicalizer for this frozen schema's expressions.
    # Logical grouping is preserved: NOT(A AND B) != (NOT A) AND B.
    pieces = re.split(r"('(?:''|[^'])*')", str(value))
    for position in range(0, len(pieces), 2):
        pieces[position] = re.sub(
            r"::(?:text|character varying|integer|interval)\b(?:\[\])?",
            "",
            pieces[position].lower(),
        )
    text = "".join(pieces).replace("'24:00:00'", "'24 hours'")
    text = re.sub(r"interval\s*('24 hours')", r"\1", text)
    tokens = re.findall(r"'(?:''|[^'])*'|[a-z_][a-z_0-9]*|[0-9]+|>=|<=|<>|!=|[^\s]", text)
    return _canonical(tokens)


def _outer(tokens: list[str]) -> list[str]:
    while tokens and tokens[0] == "(" and tokens[-1] == ")":
        depth = 0
        for _position, token in enumerate(tokens):
            depth += (token == "(") - (token == ")")
            if depth == 0:
                break
        if _position != len(tokens) - 1:
            break
        tokens = tokens[1:-1]
    return tokens


def _canonical(tokens: list[str]) -> str:
    tokens = _outer(tokens)
    depth = 0
    top = []
    for position, token in enumerate(tokens):
        if token in ("(", "["):
            depth += 1
        elif token in (")", "]"):
            depth -= 1
        elif depth == 0:
            top.append(position)
    # BETWEEN consumes its own AND; other ANDs remain logical conjunctions.
    between = False
    logical = []
    for position in top:
        if tokens[position] == "between":
            between = True
        elif tokens[position] == "and":
            if between:
                between = False
            else:
                logical.append(position)
    if logical:
        position = logical[-1]
        return f"and({_canonical(tokens[:position])},{_canonical(tokens[position + 1 :])})"
    if tokens and tokens[0] == "not":
        return f"not({_canonical(tokens[1:])})"
    for position in top:
        if tokens[position] == "between":
            delimiter = next(index for index in top if index > position and tokens[index] == "and")
            left = _canonical(tokens[:position])
            return (
                f"and(>=({left},{_canonical(tokens[position + 1 : delimiter])}),"
                f"<=({left},{_canonical(tokens[delimiter + 1 :])}))"
            )
        if tokens[position] in ("=", ">=", "<=", ">", "~", "+", "in", "is"):
            operator = tokens[position]
            right = _outer(tokens[position + 1 :])
            if operator == "=" and right and right[0] == "any":
                array = _outer(right[1:])
                if array[:2] == ["array", "["] and array[-1:] == ["]"]:
                    operator, right = "in", array[2:-1]
            return f"{operator}({_canonical(tokens[:position])},{_canonical(right)})"
    if len(tokens) >= 3 and tokens[1] == "(" and tokens[-1] == ")":
        return f"{tokens[0]}({_canonical(tokens[2:-1])})"
    return "".join(tokens)


def _validate(inspector: sa.Inspector, tables: tuple[sa.Table, ...]) -> None:
    dialect = op.get_bind().dialect
    for table in tables:
        columns = {column["name"]: column for column in inspector.get_columns(table.name)}
        if set(columns) != set(table.c.keys()):
            raise RuntimeError("calendar schema column mismatch requires operator review")
        for expected in table.c:
            actual = columns[expected.name]
            if (
                str(actual["type"].compile(dialect=dialect)).upper()
                != str(expected.type.compile(dialect=dialect)).upper()
                or actual["nullable"] != expected.nullable
            ):
                raise RuntimeError("calendar schema column type mismatch requires operator review")
            wanted = str(expected.server_default.arg) if expected.server_default else None
            if expected.server_default and isinstance(expected.server_default.arg, str):
                wanted = "'" + expected.server_default.arg.replace("'", "''") + "'"
            default = actual.get("default")
            wanted_default = _expression(wanted) if wanted is not None else None
            actual_default = _expression(default) if default is not None else None
            if not isinstance(expected.type, sa.String):
                wanted_default = wanted_default.strip("'") if wanted_default is not None else None
                actual_default = actual_default.strip("'") if actual_default is not None else None
            if wanted_default != actual_default:
                raise RuntimeError("calendar schema default mismatch requires operator review")
        if inspector.get_pk_constraint(table.name)["constrained_columns"] != [
            column.name for column in table.primary_key.columns
        ]:
            raise RuntimeError("calendar schema primary key mismatch requires operator review")
        if inspector.get_unique_constraints(table.name):
            raise RuntimeError("calendar schema unique mismatch requires operator review")
        foreign = {
            (
                tuple(item["constrained_columns"]),
                item.get("referred_schema") or inspector.default_schema_name,
                item["referred_table"],
                tuple(item["referred_columns"]),
                item.get("options", {}).get("ondelete"),
            )
            for item in inspector.get_foreign_keys(table.name)
        }
        expected_foreign = {
            (
                tuple(element.parent.name for element in constraint.elements),
                constraint.referred_table.schema or inspector.default_schema_name,
                constraint.referred_table.name,
                tuple(element.column.name for element in constraint.elements),
                constraint.ondelete,
            )
            for constraint in table.foreign_key_constraints
        }
        if foreign != expected_foreign:
            raise RuntimeError("calendar schema cascade mismatch requires operator review")
        for item in inspector.get_foreign_keys(table.name):
            if any(
                value not in (None, False, "", [], ())
                for key, value in item.get("options", {}).items()
                if key != "ondelete"
            ):
                raise RuntimeError("calendar schema FK options mismatch requires operator review")
        checks = {
            item["name"]: _expression(item["sqltext"])
            for item in inspector.get_check_constraints(table.name)
        }
        expected_checks = {
            item.name: _expression(item.sqltext)
            for item in table.constraints
            if isinstance(item, sa.CheckConstraint)
        }
        if checks != expected_checks:
            raise RuntimeError("calendar schema check mismatch requires operator review")
        indexes = {
            item["name"]: (tuple(item["column_names"]), bool(item["unique"]))
            for item in inspector.get_indexes(table.name)
        }
        wanted_indexes = {
            item.name: (tuple(column.name for column in item.columns), item.unique)
            for item in table.indexes
        }
        if indexes != wanted_indexes:
            raise RuntimeError("calendar schema index mismatch requires operator review")
        for item in inspector.get_indexes(table.name):
            if (
                item.get("expressions") not in (None, [], item["column_names"])
                or item.get("include_columns")
                or item.get("column_sorting")
                or any(
                    value not in (None, False, "", [], (), {})
                    for value in item.get("dialect_options", {}).values()
                )
            ):
                raise RuntimeError(
                    "calendar schema index options mismatch requires operator review"
                )


def upgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("0009 calendar migration requires online schema inspection")
    tables = _schema()
    inspector = sa.inspect(op.get_bind())
    names = {table.name for table in tables}
    present = names.intersection(inspector.get_table_names())
    if present and present != names:
        raise RuntimeError("partial calendar schema requires operator review")
    if present:
        _validate(inspector, tables)
        return
    for table in tables:
        table.create(op.get_bind(), checkfirst=False)
    _validate(sa.inspect(op.get_bind()), tables)


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("0009 calendar migration requires online schema inspection")
    tables = _schema()
    inspector = sa.inspect(op.get_bind())
    names = {table.name for table in tables}
    present = names.intersection(inspector.get_table_names())
    if not present:
        return
    if present != names:
        raise RuntimeError("partial calendar schema requires operator review")
    _validate(inspector, tables)
    for table in reversed(tables):
        table.drop(op.get_bind(), checkfirst=False)

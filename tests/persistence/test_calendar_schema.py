"""Frozen migration mock branches and compiled PG DDL; no live migration."""

from __future__ import annotations

import copy
import importlib
from unittest.mock import MagicMock

import pytest
from sqlalchemy import CheckConstraint, Integer, String, Table
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from time_tl.infrastructure.db.models import Calendar, CalendarInvitation, CalendarMembership

migration = importlib.import_module("migrations.versions.0009_calendar_foundation")


class Inspector:
    default_schema_name = "public"

    def __init__(self):
        self.tables = {table.name: table for table in migration._schema()}
        self.names = list(self.tables)
        self.columns = {
            name: [
                {
                    "name": column.name,
                    "type": column.type,
                    "nullable": column.nullable,
                    "default": (
                        "'" + column.server_default.arg.replace("'", "''") + "'"
                        if column.server_default and isinstance(column.server_default.arg, str)
                        else str(column.server_default.arg)
                        if column.server_default
                        else None
                    ),
                }
                for column in table.c
            ]
            for name, table in self.tables.items()
        }
        self.pk = {
            name: {"constrained_columns": [column.name for column in table.primary_key.columns]}
            for name, table in self.tables.items()
        }
        self.unique = {name: [] for name in self.tables}
        self.foreign = {
            name: [
                {
                    "constrained_columns": [element.parent.name for element in constraint.elements],
                    "referred_schema": None,
                    "referred_table": constraint.referred_table.name,
                    "referred_columns": [element.column.name for element in constraint.elements],
                    "options": {"ondelete": constraint.ondelete},
                }
                for constraint in table.foreign_key_constraints
            ]
            for name, table in self.tables.items()
        }
        self.checks = {
            name: [
                {"name": constraint.name, "sqltext": str(constraint.sqltext)}
                for constraint in table.constraints
                if isinstance(constraint, CheckConstraint)
            ]
            for name, table in self.tables.items()
        }
        self.indexes = {
            name: [
                {
                    "name": index.name,
                    "column_names": [column.name for column in index.columns],
                    "unique": index.unique,
                }
                for index in table.indexes
            ]
            for name, table in self.tables.items()
        }

    def get_table_names(self):
        return self.names

    def get_columns(self, name):
        return self.columns[name]

    def get_pk_constraint(self, name):
        return self.pk[name]

    def get_foreign_keys(self, name):
        return self.foreign[name]

    def get_unique_constraints(self, name):
        return self.unique[name]

    def get_check_constraints(self, name):
        return self.checks[name]

    def get_indexes(self, name):
        return self.indexes[name]


@pytest.fixture
def setup(monkeypatch):
    inspector = Inspector()
    operations = MagicMock()
    operations.get_bind.return_value.dialect = postgresql.dialect()
    monkeypatch.setattr(migration.context, "is_offline_mode", lambda: False)
    monkeypatch.setattr(migration, "op", operations)
    monkeypatch.setattr(migration.sa, "inspect", lambda bind: inspector)
    return inspector, operations


def test_frozen_migration_matches_current_models_columns_and_constraints():
    by_name = {table.name: table for table in migration._schema()}
    for model in (Calendar, CalendarMembership, CalendarInvitation):
        frozen, current = by_name[model.__tablename__], model.__table__
        assert set(frozen.c.keys()) == set(current.c.keys())
        for name in frozen.c.keys():
            assert str(frozen.c[name].type) == str(current.c[name].type)
            assert frozen.c[name].nullable == current.c[name].nullable
        assert {
            str(item.sqltext) for item in frozen.constraints if isinstance(item, CheckConstraint)
        } == {
            str(item.sqltext) for item in current.constraints if isinstance(item, CheckConstraint)
        }
        ddl = str(CreateTable(current).compile(dialect=postgresql.dialect()))
        assert "ON DELETE CASCADE" in ddl
        assert "join_url" not in ddl
    ddl = str(CreateTable(CalendarMembership.__table__).compile(dialect=postgresql.dialect()))
    assert "PRIMARY KEY (calendar_id, user_id)" in ddl
    assert "'OWNER'" not in ddl
    ddl = str(CreateTable(CalendarInvitation.__table__).compile(dialect=postgresql.dialect()))
    assert "token_hash VARCHAR(64)" in ddl and "24 hours" in ddl
    assert "snapshot_version" in str(CreateTable(Calendar.__table__))


def test_compiled_lookup_indexes_bound_user_reads_and_invitation_lookups():
    for model, expected in (
        (Calendar, "owner_user_id"),
        (CalendarMembership, "user_id"),
        (CalendarInvitation, "target_user_id"),
    ):
        sql = "\n".join(
            str(CreateIndex(index).compile(dialect=postgresql.dialect()))
            for index in model.__table__.indexes
        )
        assert f"({expected})" in sql and "WHERE" not in sql


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_offline_fails_without_inspecting_or_mutating(direction, monkeypatch):
    operations = MagicMock()
    monkeypatch.setattr(migration.context, "is_offline_mode", lambda: True)
    monkeypatch.setattr(migration, "op", operations)
    with pytest.raises(RuntimeError, match="online"):
        getattr(migration, direction)()
    assert operations.mock_calls == []


def test_complete_valid_schema_is_validated_without_create(setup, monkeypatch):
    created = []
    monkeypatch.setattr(Table, "create", lambda table, *args, **kwargs: created.append(table.name))
    migration.upgrade()
    assert not created


def test_absent_creates_all_then_validates(setup, monkeypatch):
    inspector, operations = setup
    inspector.names = []
    created = []
    monkeypatch.setattr(Table, "create", lambda table, *args, **kwargs: created.append(table.name))
    migration.upgrade()
    assert created == ["calendars", "calendar_memberships", "calendar_invitations"]
    assert operations.get_bind.called


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_partial_tables_fail_closed(direction, setup, monkeypatch):
    inspector, _ = setup
    inspector.names = ["calendars"]
    created, dropped = [], []
    monkeypatch.setattr(Table, "create", lambda table, *args, **kwargs: created.append(table.name))
    monkeypatch.setattr(Table, "drop", lambda table, *args, **kwargs: dropped.append(table.name))
    with pytest.raises(RuntimeError, match="partial"):
        getattr(migration, direction)()
    assert not created and not dropped


def test_downgrade_reverse_dependency_order_only_after_full_validation(setup, monkeypatch):
    inspector, _ = setup
    dropped = []
    monkeypatch.setattr(Table, "drop", lambda table, *args, **kwargs: dropped.append(table.name))
    migration.downgrade()
    assert dropped == ["calendar_invitations", "calendar_memberships", "calendars"]
    dropped.clear()
    inspector.names = []
    migration.downgrade()
    assert not dropped


@pytest.mark.parametrize(
    "mismatch",
    [
        "column_missing",
        "column_extra",
        "column_type",
        "column_nullable",
        "column_default",
        "default_literal",
        "primary_key",
        "unique_constraint",
        "foreign_schema",
        "foreign_cascade",
        "foreign_deferred",
        "foreign_missing",
        "check_missing",
        "check_semantics",
        "check_literal",
        "index_columns",
        "index_unique",
        "index_predicate",
        "index_include",
        "index_expression",
        "index_sort",
        "index_missing",
    ],
)
def test_existing_mismatched_schema_rejected_without_writes(mismatch, setup, monkeypatch):
    inspector, _ = setup
    if mismatch == "column_missing":
        inspector.columns["calendars"].pop()
    elif mismatch == "column_extra":
        inspector.columns["calendars"].append({"name": "extra", "type": Integer()})
    elif mismatch == "column_type":
        inspector.columns["calendars"][0]["type"] = String(36)
    elif mismatch == "column_nullable":
        inspector.columns["calendars"][0]["nullable"] = True
    elif mismatch == "column_default":
        inspector.columns["calendars"][0]["default"] = "gen_random_uuid()"
    elif mismatch == "primary_key":
        inspector.pk["calendars"]["constrained_columns"] = ["owner_user_id"]
    elif mismatch == "default_literal":
        for column in inspector.columns["calendar_memberships"]:
            if column["name"] == "role":
                column["default"] = "'member'::character varying"
    elif mismatch == "unique_constraint":
        inspector.unique["calendars"] = [{"name": "foreign_unique", "column_names": ["name"]}]
    elif mismatch == "foreign_schema":
        inspector.foreign["calendars"][0]["referred_schema"] = "another_schema"
    elif mismatch == "foreign_cascade":
        inspector.foreign["calendars"][0]["options"]["ondelete"] = "RESTRICT"
    elif mismatch == "foreign_deferred":
        inspector.foreign["calendars"][0]["options"]["deferrable"] = True
    elif mismatch == "foreign_missing":
        inspector.foreign["calendars"] = []
    elif mismatch == "check_missing":
        inspector.checks["calendars"].pop()
    elif mismatch == "check_semantics":
        for check in inspector.checks["calendar_invitations"]:
            if check["name"].endswith("terminal"):
                check["sqltext"] = "(NOT (consumed_at IS NOT NULL)) AND (revoked_at IS NOT NULL)"
    elif mismatch == "check_literal":
        for check in inspector.checks["calendars"]:
            if check["name"].endswith("kind_allowed"):
                check["sqltext"] = "kind IN ('school','PERSONAL','ACTIVITY')"
    elif mismatch == "index_columns":
        inspector.indexes["calendars"][0]["column_names"] = ["id"]
    elif mismatch == "index_unique":
        inspector.indexes["calendars"][0]["unique"] = True
    elif mismatch == "index_predicate":
        inspector.indexes["calendars"][0]["dialect_options"] = {"postgresql_where": "owner_visible"}
    elif mismatch == "index_include":
        inspector.indexes["calendars"][0]["include_columns"] = ["snapshot_payload"]
    elif mismatch == "index_expression":
        inspector.indexes["calendars"][0]["expressions"] = ["lower(name)"]
    elif mismatch == "index_sort":
        inspector.indexes["calendars"][0]["column_sorting"] = {"owner_user_id": ("desc",)}
    elif mismatch == "index_missing":
        inspector.indexes["calendars"] = []
    before = copy.deepcopy(inspector.columns)
    created = []
    monkeypatch.setattr(Table, "create", lambda table, *args, **kwargs: created.append(table.name))
    with pytest.raises(RuntimeError, match="mismatch"):
        migration.upgrade()
    assert not created and set(inspector.columns) == set(before)


@pytest.mark.parametrize(
    "expression,reflected",
    [
        (
            "kind IN ('SCHOOL','PERSONAL','ACTIVITY')",
            "((kind)::text = ANY ((ARRAY['SCHOOL'::character varying, "
            "'PERSONAL'::character varying, "
            "'ACTIVITY'::character varying])::text[]))",
        ),
        (
            "char_length(btrim(name)) BETWEEN 1 AND 120",
            "((char_length(btrim((name)::text)) >= 1) AND "
            "(char_length(btrim((name)::text)) <= 120))",
        ),
        (
            "NOT (consumed_at IS NOT NULL AND revoked_at IS NOT NULL)",
            "(NOT ((consumed_at IS NOT NULL) AND (revoked_at IS NOT NULL)))",
        ),
        (
            "expires_at > created_at AND expires_at <= created_at + interval '24 hours'",
            "((expires_at > created_at) AND (expires_at <= (created_at + '24:00:00'::interval)))",
        ),
    ],
)
def test_conservative_check_canonicalization_handles_known_pg_deparse(expression, reflected):
    assert migration._expression(expression) == migration._expression(reflected)


def test_logical_grouping_and_literal_contents_are_never_erased():
    assert migration._expression("NOT (a IS NOT NULL AND b IS NOT NULL)") != migration._expression(
        "(NOT (a IS NOT NULL)) AND (b IS NOT NULL)"
    )
    assert migration._expression("kind IN ('SCHOOL')") != migration._expression(
        "kind IN ('school')"
    )
    assert migration._expression("kind IN ('SCHOOL')") != migration._expression(
        "kind IN ('SCHOOL::text')"
    )

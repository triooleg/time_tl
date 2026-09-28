from __future__ import annotations

import importlib
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from time_tl.infrastructure.db import models
from time_tl.infrastructure.db.base import Base


def test_homework_tables_and_personal_cascades_are_present() -> None:
    assert {
        "schedule_subjects",
        "homework_assignments",
        "homework_user_states",
        "pending_homework_drafts",
    }.issubset(Base.metadata.tables)

    assignment = models.HomeworkAssignment.__table__
    owner_fk = next(
        fk for fk in assignment.foreign_key_constraints if "owner_user_id" in fk.column_keys
    )
    assert owner_fk.ondelete == "CASCADE"
    subject_fk = next(
        fk for fk in assignment.foreign_key_constraints if "subject_id" in fk.column_keys
    )
    assert subject_fk.column_keys == ["subject_id", "profile_id"]
    assert subject_fk.ondelete == "CASCADE"


def test_completion_state_is_per_user_and_consistent() -> None:
    state = models.HomeworkUserState.__table__
    assert {column.name for column in state.primary_key.columns} == {"homework_id", "user_id"}
    ddl = str(CreateTable(state).compile(dialect=postgresql.dialect()))
    assert "completed_at IS NOT NULL" in ddl
    assert "NOT completed AND completed_at IS NULL" in ddl


def test_draft_persists_only_token_hash_and_has_bounded_active_index() -> None:
    table = models.PendingHomeworkDraft.__table__
    assert "token" not in table.c
    assert "token_hash" in table.c
    indexes = {item.name: item for item in table.indexes}
    sql = str(
        CreateIndex(indexes["ix_pending_homework_active_user_created"]).compile(
            dialect=postgresql.dialect()
        )
    )
    assert "(user_id, created_at, token_hash)" in sql
    assert "revoked_at IS NULL AND consumed_at IS NULL" in sql
    confirmed_fk = next(
        fk for fk in table.foreign_key_constraints if "confirmed_homework_id" in fk.column_keys
    )
    assert confirmed_fk.ondelete == "CASCADE"
    assert confirmed_fk.name == "fk_pending_homework_confirmed_assignment"
    # PostgreSQL rejects identifiers longer than 63 bytes.  Compile the full
    # table here so generated constraint names cannot hide behind SQLite tests.
    ddl = str(CreateTable(table).compile(dialect=postgresql.dialect()))
    assert "fk_pending_homework_confirmed_assignment" in ddl
    assert all(
        constraint.name is None or len(constraint.name.encode("utf-8")) <= 63
        for constraint in table.constraints
    )


def test_manual_only_source_constraint_has_no_excluded_integrations() -> None:
    ddl = str(
        CreateTable(models.HomeworkAssignment.__table__).compile(dialect=postgresql.dialect())
    )
    assert "source_type = 'MANUAL'" in ddl
    assert "OCR" not in ddl
    assert "FORWARDED" not in ddl


def test_0008_fails_closed_offline_instead_of_emitting_a_false_head() -> None:
    migration = importlib.import_module("migrations.versions.0008_homework_foundation")
    with (
        patch.object(migration.context, "is_offline_mode", return_value=True),
        patch.object(migration, "op") as operations,
        patch.object(migration.sa, "inspect") as inspect,
        pytest.raises(RuntimeError, match="requires online schema inspection"),
    ):
        migration.upgrade()
    assert operations.mock_calls == []
    inspect.assert_not_called()

    with (
        patch.object(migration.context, "is_offline_mode", return_value=True),
        patch.object(migration, "op") as operations,
        pytest.raises(RuntimeError, match="requires online schema inspection"),
    ):
        migration.downgrade()
    assert operations.mock_calls == []


def test_0008_fails_closed_on_partial_schema() -> None:
    migration = importlib.import_module("migrations.versions.0008_homework_foundation")
    inspector = MagicMock()
    inspector.get_table_names.return_value = ["schedule_subjects"]
    with (
        patch.object(migration.context, "is_offline_mode", return_value=False),
        patch.object(migration, "op") as operations,
        patch.object(migration.sa, "inspect", return_value=inspector),
        pytest.raises(RuntimeError, match="partial homework schema"),
    ):
        migration.upgrade()
    operations.create_table.assert_not_called()


def test_0008_is_online_noop_when_current_metadata_tables_already_exist() -> None:
    migration = importlib.import_module("migrations.versions.0008_homework_foundation")
    inspector = MagicMock()
    inspector.get_table_names.return_value = list(migration._TABLES)
    with (
        patch.object(migration.context, "is_offline_mode", return_value=False),
        patch.object(migration, "op") as operations,
        patch.object(migration.sa, "inspect", return_value=inspector),
    ):
        migration.upgrade()
    operations.create_table.assert_not_called()

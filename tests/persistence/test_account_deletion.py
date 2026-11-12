from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from time_tl.application.schedule_worker import PlannedOutboxMessage
from time_tl.infrastructure.db.accounts import AsyncPostgresAccountRepository
from time_tl.infrastructure.db.models import (
    AlertEpisode,
    AlertScope,
    AlertSnapshot,
    DateOverride,
    OutboxMessage,
    OverrideLesson,
    PendingImportSession,
    RuleLesson,
    ScheduleProfile,
    ScheduleRule,
    ShareSession,
    User,
)
from time_tl.infrastructure.db.workers import PostgresScheduleWorkerRepository


class Result:
    def __init__(self, *, scalar=None, scalars=()) -> None:
        self.scalar = scalar
        self.values = tuple(scalars)

    def scalar_one_or_none(self):
        return self.scalar

    def scalars(self):
        return iter(self.values)


class Transaction:
    def __init__(self, session: Session) -> None:
        self.session = session

    async def __aenter__(self):
        return None

    async def __aexit__(self, exc_type, exc, traceback):
        self.session.committed = exc_type is None
        self.session.rolled_back = exc_type is not None
        return False


class Session:
    def __init__(self, results, *, fail_at: int | None = None) -> None:
        self.results = list(results)
        self.fail_at = fail_at
        self.statements = []
        self.committed = False
        self.rolled_back = False

    def begin(self):
        return Transaction(self)

    async def execute(self, statement):
        self.statements.append(statement)
        if self.fail_at == len(self.statements):
            raise RuntimeError("forced database failure")
        return self.results.pop(0) if self.results else Result()


class SessionContext:
    def __init__(self, session: Session) -> None:
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, traceback):
        return False


def factory_for(*sessions: Session):
    queue = list(sessions)

    def factory():
        return SessionContext(queue.pop(0))

    return factory


def delete_statements(session: Session):
    return [statement for statement in session.statements if statement.is_delete]


def compiled(statement):
    return statement.compile(dialect=postgresql.dialect())


@pytest.mark.asyncio
async def test_delete_account_is_isolated_type_aware_and_removes_all_outbox_states() -> None:
    target = User(id=uuid4(), telegram_user_id=101)
    other = User(id=uuid4(), telegram_user_id=202)
    scope = AlertScope(
        id=uuid4(),
        user_id=target.id,
        selected_region_id="31",
        applicable_region_ids=["31"],
    )
    session = Session(
        (
            Result(scalar=target.id),
            Result(scalar=scope.id),
            Result(),
            Result(scalar=target),
            Result(scalar=scope.id),
            Result(),
            Result(),
        )
    )
    repository = AsyncPostgresAccountRepository(factory_for(session))  # type: ignore[arg-type]

    assert await repository.delete_account(101) is True
    assert session.committed is True
    assert session.rolled_back is False

    outbox_lock_sql = str(compiled(session.statements[2]))
    user_lock_sql = str(compiled(session.statements[3]))
    scope_lock_sql = str(compiled(session.statements[4]))
    assert "FOR UPDATE" in outbox_lock_sql
    assert "FOR UPDATE NOWAIT" in user_lock_sql
    assert "FOR UPDATE NOWAIT" in scope_lock_sql

    deletes = delete_statements(session)
    assert [statement.table.name for statement in deletes] == [
        "outbox_messages",
        "runtime_settings",
        "users",
    ]
    outbox_sql = compiled(deletes[0])
    values = set(outbox_sql.params.values())
    assert {
        "schedule",
        "power",
        "power_map",
        "alert_scope",
        str(target.id),
        str(scope.id),
    }.issubset(values)
    assert str(other.id) not in values
    normalized_sql = str(outbox_sql).casefold()
    assert "aggregate_type" in normalized_sql
    assert "aggregate_id" in normalized_sql
    assert "delivered_at" not in normalized_sql
    assert "dead_at" not in normalized_sql

    preference_sql = compiled(deletes[1])
    private_keys = set(next(iter(preference_sql.params.values())))
    assert private_keys == {
        "power_api_user_pref:" + str(target.id),
        "power_api_reminder_state:" + str(target.id),
        "power_api_emergency_state:" + str(target.id),
    }
    assert "power_api_user_pref:" + str(other.id) not in private_keys
    assert "power_api_reminder_state:" + str(other.id) not in private_keys
    assert "power_api_emergency_state:" + str(other.id) not in private_keys
    user_sql = compiled(deletes[2])
    assert target.id in user_sql.params.values()
    assert other.id not in user_sql.params.values()


@pytest.mark.asyncio
async def test_delete_account_is_idempotent_and_second_call_is_noop() -> None:
    target = User(id=uuid4(), telegram_user_id=101)
    first = Session(
        (
            Result(scalar=target.id),
            Result(scalar=None),
            Result(),
            Result(scalar=target),
            Result(scalar=None),
            Result(),
            Result(),
        )
    )
    second = Session((Result(scalar=None),))
    repository = AsyncPostgresAccountRepository(  # type: ignore[arg-type]
        factory_for(first, second)
    )

    assert await repository.delete_account(101) is True
    assert await repository.delete_account(101) is False
    assert first.committed is True
    assert second.committed is True
    assert delete_statements(second) == []
    assert len(second.statements) == 1


@pytest.mark.asyncio
async def test_delete_account_retries_if_scope_changes_before_owner_lock() -> None:
    target = User(id=uuid4(), telegram_user_id=101)
    stale_scope_id = uuid4()
    current_scope_id = uuid4()
    raced = Session(
        (
            Result(scalar=target.id),
            Result(scalar=stale_scope_id),
            Result(),
            Result(scalar=target),
            Result(scalar=current_scope_id),
        )
    )
    retried = Session(
        (
            Result(scalar=target.id),
            Result(scalar=current_scope_id),
            Result(),
            Result(scalar=target),
            Result(scalar=current_scope_id),
            Result(),
            Result(),
        )
    )
    repository = AsyncPostgresAccountRepository(  # type: ignore[arg-type]
        factory_for(raced, retried)
    )

    assert await repository.delete_account(101) is True
    assert raced.rolled_back is True
    assert retried.committed is True


@pytest.mark.asyncio
async def test_delete_account_rolls_back_and_does_not_swallow_failure() -> None:
    target = User(id=uuid4(), telegram_user_id=101)
    scope_id = uuid4()
    session = Session(
        (
            Result(scalar=target.id),
            Result(scalar=scope_id),
            Result(),
            Result(scalar=target),
            Result(scalar=scope_id),
        ),
        fail_at=6,
    )
    repository = AsyncPostgresAccountRepository(factory_for(session))  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="forced database failure"):
        await repository.delete_account(101)

    assert session.rolled_back is True
    assert session.committed is False
    assert all(
        not (statement.is_delete and statement.table.name == "users")
        for statement in session.statements
    )


def _foreign_key(table, referred_table: str):
    return next(key for key in table.foreign_keys if key.column.table.name == referred_table)


def test_owned_rows_and_encrypted_links_have_cascade_delete_chain() -> None:
    expected = (
        (ScheduleProfile.__table__, "users"),
        (ScheduleRule.__table__, "schedule_profiles"),
        (DateOverride.__table__, "schedule_profiles"),
        (RuleLesson.__table__, "schedule_rules"),
        (OverrideLesson.__table__, "date_overrides"),
        (PendingImportSession.__table__, "users"),
        (ShareSession.__table__, "users"),
        (AlertScope.__table__, "users"),
        (AlertSnapshot.__table__, "alert_scopes"),
        (AlertEpisode.__table__, "alert_snapshots"),
    )
    for table, parent in expected:
        assert _foreign_key(table, parent).ondelete == "CASCADE"

    assert "join_url_ciphertext" in RuleLesson.__table__.columns
    assert "join_url_ciphertext" in OverrideLesson.__table__.columns
    assert not OutboxMessage.__table__.foreign_keys


@pytest.mark.asyncio
async def test_stale_schedule_scan_cannot_recreate_outbox_after_owner_delete() -> None:
    session = Session((Result(scalar=None),))
    repository = PostgresScheduleWorkerRepository(  # type: ignore[arg-type]
        factory_for(session),
        stale_after_seconds=120,
    )
    message = PlannedOutboxMessage(
        event_key="deleted-user-reminder",
        aggregate_id=str(uuid4()),
        event_type="LESSON_REMINDER",
        payload={"lesson_title": "Математика"},
        available_at=datetime(2026, 9, 25, tzinfo=UTC),
    )

    assert await repository.enqueue_deduplicated((message,)) == 0
    assert session.committed is True
    assert len(session.statements) == 1
    assert not any(statement.is_insert for statement in session.statements)

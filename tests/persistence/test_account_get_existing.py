from __future__ import annotations

from datetime import date, time
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from time_tl.bot.models import NotificationMode
from time_tl.infrastructure.db.accounts import AsyncPostgresAccountRepository
from time_tl.infrastructure.db.models import (
    DateOverride,
    OverrideLesson,
    RuleLesson,
    ScheduleProfile,
    ScheduleRule,
    User,
)


class ReverseCodec:
    def encrypt(self, value: str) -> bytes:
        return value[::-1].encode()

    def decrypt(self, value: bytes) -> str:
        return value.decode()[::-1]


class Result:
    def __init__(self, *, scalar=None, scalars=()) -> None:
        self.scalar = scalar
        self.values = tuple(scalars)

    def scalar_one_or_none(self):
        return self.scalar

    def scalars(self):
        return self

    def all(self):
        return list(self.values)


class ReadOnlySession:
    def __init__(self, results) -> None:
        self.results = list(results)
        self.statements = []
        self.write_attempted = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback):
        return False

    async def execute(self, statement):
        self.statements.append(statement)
        return self.results.pop(0)

    def add(self, _row) -> None:
        self.write_attempted = True
        raise AssertionError("get_existing must not add rows")

    async def flush(self) -> None:
        self.write_attempted = True
        raise AssertionError("get_existing must not flush")


class Factory:
    def __init__(self, *sessions: ReadOnlySession) -> None:
        self.sessions = list(sessions)

    def __call__(self):
        return self.sessions.pop(0)


@pytest.mark.asyncio
async def test_absent_account_returns_none_without_writes_or_row_lock() -> None:
    session = ReadOnlySession((Result(scalar=None),))
    repository = AsyncPostgresAccountRepository(Factory(session))  # type: ignore[arg-type]

    assert await repository.get_existing(101) is None

    assert session.write_attempted is False
    assert len(session.statements) == 1
    sql = str(session.statements[0].compile(dialect=postgresql.dialect()))
    assert "INSERT" not in sql
    assert "FOR UPDATE" not in sql
    assert 101 in session.statements[0].compile(dialect=postgresql.dialect()).params.values()


@pytest.mark.asyncio
async def test_existing_account_uses_full_profile_lesson_and_override_hydration() -> None:
    codec = ReverseCodec()
    user = User(
        id=uuid4(),
        telegram_user_id=101,
        mode="LESSONS_ONLY",
        confirmed_region_id="140101",
        region_name="Київська громада",
        reminder_lead_minutes=7,
        alerts_enabled=False,
        lessons_enabled=True,
    )
    profile = ScheduleProfile(
        id=uuid4(),
        user_id=user.id,
        public_id="school",
        name="Школа",
        timezone="Europe/Kyiv",
        is_active=True,
        version=1,
    )
    rule = ScheduleRule(
        id=uuid4(),
        profile_id=profile.id,
        weekday=0,
        week_parity="ANY",
        date_parity="ANY",
        position=0,
    )
    rule_lesson = RuleLesson(
        id=uuid4(),
        rule_id=rule.id,
        position=0,
        start_time=time(9),
        end_time=time(9, 45),
        title="Математика",
        enabled=True,
        join_url_ciphertext=codec.encrypt("https://example.invalid/join"),
    )
    override = DateOverride(
        id=uuid4(),
        profile_id=profile.id,
        day=date(2026, 9, 28),
        day_off=False,
    )
    override_lesson = OverrideLesson(
        id=uuid4(),
        override_id=override.id,
        position=0,
        start_time=time(10),
        end_time=time(10, 45),
        title="Фізика",
        enabled=True,
        join_url_ciphertext=None,
    )
    session = ReadOnlySession(
        (
            Result(scalar=user),
            Result(scalars=(profile,)),
            Result(scalars=(rule,)),
            Result(scalars=(rule_lesson,)),
            Result(scalars=(override,)),
            Result(scalars=(override_lesson,)),
        )
    )
    repository = AsyncPostgresAccountRepository(  # type: ignore[arg-type]
        Factory(session), codec
    )

    account = await repository.get_existing(101)

    assert account is not None
    assert account.user_id == 101
    assert account.mode is NotificationMode.LESSONS
    assert account.region_id == "140101"
    assert account.reminder_lead_minutes == 7
    restored = account.profiles["school"]
    assert restored.active is True
    assert restored.rules[0].lessons[0].title == "Математика"
    assert restored.rules[0].lessons[0].join_url == "https://example.invalid/join"
    assert restored.overrides[0].lessons[0].title == "Фізика"
    assert session.write_attempted is False


@pytest.mark.asyncio
async def test_deleted_account_remains_absent_across_repeated_reads() -> None:
    first = ReadOnlySession((Result(scalar=None),))
    second = ReadOnlySession((Result(scalar=None),))
    repository = AsyncPostgresAccountRepository(  # type: ignore[arg-type]
        Factory(first, second)
    )

    assert await repository.get_existing(101) is None
    assert await repository.get_existing(101) is None
    assert first.write_attempted is False
    assert second.write_attempted is False


@pytest.mark.asyncio
async def test_two_users_are_hydrated_in_isolation() -> None:
    first_user = User(id=uuid4(), telegram_user_id=101, reminder_lead_minutes=5)
    second_user = User(id=uuid4(), telegram_user_id=202, reminder_lead_minutes=10)
    first = ReadOnlySession((Result(scalar=first_user), Result(scalars=())))
    second = ReadOnlySession((Result(scalar=second_user), Result(scalars=())))
    repository = AsyncPostgresAccountRepository(  # type: ignore[arg-type]
        Factory(first, second)
    )

    first_account = await repository.get_existing(101)
    second_account = await repository.get_existing(202)

    assert first_account is not None and first_account.user_id == 101
    assert second_account is not None and second_account.user_id == 202
    first_params = first.statements[0].compile(dialect=postgresql.dialect()).params.values()
    second_params = second.statements[0].compile(dialect=postgresql.dialect()).params.values()
    assert 101 in first_params and 202 not in first_params
    assert 202 in second_params and 101 not in second_params
    assert first.write_attempted is False
    assert second.write_attempted is False

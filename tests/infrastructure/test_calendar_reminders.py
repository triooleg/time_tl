"""Offline SQL and projection tests for the real calendar reminder policy."""

from __future__ import annotations

import os
from datetime import UTC, date, datetime, time, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql.dml import Insert
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema

from time_tl.application.sender_worker import DeliveryRecipient, SenderWorker
from time_tl.domain.calendar_memberships import serialize_profile
from time_tl.domain.calendar_overrides import event_choices
from time_tl.domain.schedule import DateOverride, Lesson, ScheduleProfile
from time_tl.infrastructure.db.base import Base
from time_tl.infrastructure.db.calendar_reminders import (
    CALENDAR_REMINDER,
    PostgresCalendarReminderRepository,
    calendar_reminder_deliverable,
)
from time_tl.infrastructure.db.models import (
    Calendar,
    OutboxMessage,
    PersonalCalendarOverride,
    RuntimeSetting,
    User,
)
from time_tl.infrastructure.db.workers import PostgresSenderRepository

NOW = datetime(2026, 11, 5, 21, 55, tzinfo=UTC)
START = datetime(2026, 11, 5, 22, 3, tzinfo=UTC)
DAY = date(2026, 11, 6)


class Rows:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows

    def scalars(self):
        return self


class Session:
    def __init__(self, user, calendar):
        self.user = user
        self.calendars = {calendar.id: calendar}
        self.overrides = []
        self.outbox = {}
        self.locked = []
        self.inserts = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def begin(self):
        return self

    async def get(self, entity, identity, *, with_for_update, populate_existing):
        assert populate_existing == with_for_update
        if with_for_update:
            self.locked.append(entity)
        if entity is User:
            return self.user if self.user.id == identity else None
        assert entity is Calendar
        return self.calendars.get(identity)

    async def scalar(self, query):
        column = query.column_descriptions[0]
        if column["expr"] is User:
            assert query._for_update_arg is not None
            return self.user
        assert column["name"] == "calendar_reminder_generation"
        return self.user.calendar_reminder_generation

    async def scalars(self, query):
        compiled = query.compile(dialect=postgresql.dialect())
        if isinstance(query, Insert):
            assert "ON CONFLICT (event_key) DO NOTHING" in str(compiled)
            self.inserts += 1
            rows = []
            for index in range(len(compiled.params)):
                suffix = f"_m{index}"
                key = compiled.params.get("event_key" + suffix)
                if key is None:
                    break
                if key not in self.outbox:
                    payload = {
                        name.removesuffix(suffix): value
                        for name, value in compiled.params.items()
                        if name.endswith(suffix)
                    }
                    self.outbox[key] = OutboxMessage(id=uuid4(), attempts=0, **payload)
                    rows.append(self.outbox[key].id)
            return Rows(rows)
        column = query.column_descriptions[0]
        if column["entity"] is User:
            assert "users.calendar_reminder_generation IS NOT NULL" in str(compiled)
            return Rows([self.user.id] if self.user.calendar_reminder_generation else [])
        if column["entity"] is Calendar:
            assert "calendars.owner_user_id =" in str(compiled)
            assert "calendars.owner_visible IS true" in str(compiled)
            assert "FOR UPDATE" in str(compiled)
            assert compiled.params["kind_1"] == "PERSONAL"
            return Rows(list(self.calendars.values()))
        assert column["entity"] is OutboxMessage
        sql = str(compiled)
        assert "split_part(outbox_messages.event_key," in sql
        assert "outbox_messages.delivered_at IS NOT NULL" in sql
        assert "outbox_messages.dispatch_started_at IS NOT NULL" in sql
        assert "outbox_messages.indeterminate_at IS NOT NULL" in sql
        assert "outbox_messages.attempts IS DISTINCT FROM" in sql
        assert "FOR UPDATE" not in sql
        current_keys = compiled.params["event_key_1"]
        semantic_keys = compiled.params["split_part_3"]
        return Rows(
            [
                key for key, row in self.outbox.items()
                if key.partition("|")[0] in semantic_keys
                and (
                    key in current_keys
                    or row.delivered_at is not None
                    or row.dispatch_started_at is not None
                    or row.indeterminate_at is not None
                    or row.attempts != 0
                )
            ]
        )

    async def execute(self, query):
        assert query.column_descriptions[0]["entity"] is PersonalCalendarOverride
        return Rows(self.overrides)


@pytest.fixture
def calendar_data():
    user = User(id=uuid4(), telegram_user_id=17)
    profile = ScheduleProfile(
        "personal",
        "Особисте",
        overrides=(
            DateOverride(
                DAY,
                lessons=(
                    Lesson(time(0, 3), time(0, 4), "Подія"),
                    Lesson(time(0, 4), time(0, 5), "Вимкнена", enabled=False),
                    Lesson(time(0, 30), time(1), "Пізніше"),
                ),
            ),
        ),
    )
    calendar = Calendar(
        id=uuid4(),
        owner_user_id=user.id,
        name=profile.name,
        snapshot_payload=serialize_profile(profile),
        kind="PERSONAL",
        owner_visible=True,
        version=1,
        snapshot_version=1,
    )
    return user, calendar, profile, Session(user, calendar)


async def test_opt_in_plans_only_owned_personal_timed_events_once_across_midnight(calendar_data):
    user, calendar, _, session = calendar_data
    for owner, kind, visible in (
        (uuid4(), "PERSONAL", True),
        (user.id, "SCHOOL", True),
        (user.id, "ACTIVITY", True),
        (user.id, "PERSONAL", False),
    ):
        item = Calendar(
            id=uuid4(),
            owner_user_id=owner,
            kind=kind,
            owner_visible=visible,
            name=calendar.name,
            snapshot_payload=calendar.snapshot_payload,
            snapshot_version=1,
            version=1,
        )
        session.calendars[item.id] = item
    repo = PostgresCalendarReminderRepository(lambda: session)
    assert not await repo.preferences(user.telegram_user_id)
    assert await repo.plan(NOW) == 0
    assert await repo.set_enabled(user.telegram_user_id, True)
    generation = user.calendar_reminder_generation
    assert await repo.set_enabled(user.telegram_user_id, True)
    assert generation == user.calendar_reminder_generation
    assert await repo.preferences(user.telegram_user_id)
    assert await repo.plan(NOW) == 1
    assert await repo.plan(NOW + timedelta(minutes=1)) == 0
    assert session.inserts == 1
    row = next(iter(session.outbox.values()))
    assert row.event_type == CALENDAR_REMINDER and row.aggregate_type == "schedule"
    assert row.payload["calendar_id"] == str(calendar.id)
    assert row.payload["starts_at"] == START.isoformat()
    assert row.payload["event_title"] == "Подія"
    assert "telegram_chat_id" not in row.payload
    assert "join_link_ref" not in row.payload


@pytest.mark.parametrize(
    "change",
    ["opt_out", "re_opt_in", "hide", "delete", "edit", "other_owner", "school", "expired",
     "too_early", "recipient", "payload", "terminal", "cancel"],
)
async def test_final_policy_rechecks_current_opt_in_event_and_recipient(calendar_data, change):
    user, calendar, profile, session = calendar_data
    repo = PostgresCalendarReminderRepository(lambda: session)
    await repo.set_enabled(user.telegram_user_id, True)
    assert await repo.plan(NOW) == 1
    row = next(iter(session.outbox.values()))
    recipient = DeliveryRecipient(user.telegram_user_id, None, None)
    session.locked.clear()
    assert await calendar_reminder_deliverable(
        session, row, NOW, lock=True, expected_recipient=recipient
    )
    assert session.locked == [User, Calendar]
    at = NOW
    if change in {"opt_out", "re_opt_in"}:
        await repo.set_enabled(user.telegram_user_id, False)
        if change == "re_opt_in":
            await repo.set_enabled(user.telegram_user_id, True)
    elif change == "hide":
        calendar.owner_visible = False
    elif change == "delete":
        session.calendars.pop(calendar.id)
    elif change == "edit":
        calendar.version += 1
    elif change == "other_owner":
        calendar.owner_user_id = uuid4()
    elif change == "school":
        calendar.kind = "SCHOOL"
    elif change == "expired":
        at = START
    elif change == "too_early":
        at = START - timedelta(minutes=11)
    elif change == "recipient":
        recipient = DeliveryRecipient(user.telegram_user_id + 1, None, None)
    elif change == "payload":
        row.payload["event_title"] = "Інший текст"
    elif change == "terminal":
        row.dead_at = NOW
    elif change == "cancel":
        event = event_choices(calendar.id, 1, 1, profile, DAY, user_id=user.telegram_user_id)[0]
        session.overrides.append(
            PersonalCalendarOverride(
                user_id=user.id,
                calendar_id=calendar.id,
                snapshot_version=1,
                source_day=DAY,
                event_id=event.event_id,
                action="CANCEL",
                version=1,
            )
        )
    assert not await calendar_reminder_deliverable(
        session, row, at, lock=True, expected_recipient=recipient
    )


async def test_moved_personal_occurrence_drops_old_time_and_plans_new_due_time(calendar_data):
    user, calendar, profile, session = calendar_data
    repo = PostgresCalendarReminderRepository(lambda: session)
    await repo.set_enabled(user.telegram_user_id, True)
    assert await repo.plan(NOW) == 1
    old = next(iter(session.outbox.values()))
    event = event_choices(calendar.id, 1, 1, profile, DAY, user_id=user.telegram_user_id)[0]
    session.overrides.append(
        PersonalCalendarOverride(
            user_id=user.id,
            calendar_id=calendar.id,
            snapshot_version=1,
            source_day=DAY,
            event_id=event.event_id,
            action="MOVE",
            target_day=DAY,
            start_time=time(0, 9),
            end_time=time(0, 10),
            version=1,
        )
    )
    assert not await calendar_reminder_deliverable(session, old, NOW)
    assert await repo.plan(NOW) == 0
    at = NOW + timedelta(minutes=5)
    assert await repo.plan(at) == 1
    latest = tuple(session.outbox.values())[-1]
    assert latest.payload["occurrence_id"] == old.payload["occurrence_id"]
    assert await calendar_reminder_deliverable(session, latest, at)


@pytest.mark.parametrize("change", ["edit_version", "re_opt_in"])
@pytest.mark.parametrize(
    ("prior_state", "can_replace"),
    [
        ("pending", True),
        ("claimed", True),
        ("dead_without_attempt", True),
        ("delivered", False),
        ("dispatch_started", False),
        ("indeterminate", False),
        ("attempted", False),
    ],
)
async def test_reminder_replan_requires_no_prior_delivery_attempt(
    calendar_data, change, prior_state, can_replace
):
    user, calendar, _, session = calendar_data
    planner = PostgresCalendarReminderRepository(lambda: session)
    await planner.set_enabled(user.telegram_user_id, True)
    assert await planner.plan(NOW) == 1
    original = next(iter(session.outbox.values()))
    if prior_state == "claimed":
        original.claimed_at = NOW
    elif prior_state == "dead_without_attempt":
        original.dead_at = NOW
    elif prior_state == "delivered":
        original.delivered_at = NOW
    elif prior_state == "dispatch_started":
        original.dispatch_started_at = NOW
    elif prior_state == "indeterminate":
        original.dispatch_started_at = original.dead_at = original.indeterminate_at = NOW
    elif prior_state == "attempted":
        original.attempts = 1

    if change == "edit_version":
        calendar.version += 1
    else:
        await planner.set_enabled(user.telegram_user_id, False)
        await planner.set_enabled(user.telegram_user_id, True)
    assert not await calendar_reminder_deliverable(session, original, NOW)
    assert await planner.plan(NOW) == int(can_replace)
    assert await planner.plan(NOW) == 0
    if can_replace:
        current = tuple(session.outbox.values())[-1]
        assert current.event_key != original.event_key
        assert current.event_key.partition("|")[0] == original.event_key.partition("|")[0]
        assert await calendar_reminder_deliverable(session, current, NOW)


async def test_reminder_replan_checks_all_older_revisions(calendar_data):
    user, calendar, _, session = calendar_data
    planner = PostgresCalendarReminderRepository(lambda: session)
    await planner.set_enabled(user.telegram_user_id, True)
    assert await planner.plan(NOW) == 1
    original = next(iter(session.outbox.values()))
    calendar.version += 1
    assert await planner.plan(NOW) == 1
    original.attempts = 1
    calendar.version += 1
    assert await planner.plan(NOW) == 0


async def test_disposable_postgres_calendar_planner_to_sender_with_opt_out(calendar_data):
    raw = os.environ.get("TIME_TL_CALENDAR_REMINDER_TEST_DSN")
    if raw is None:
        pytest.skip("requires explicit isolated loopback calendar reminder PostgreSQL")
    url = make_url(raw)
    if (
        url.drivername != "postgresql+asyncpg"
        or url.host != "127.0.0.1"
        or url.port is None
        or not 1024 < url.port < 65536
        or url.database != "time_tl_audit"
        or url.username != "postgres"
        or url.query
    ):
        pytest.fail("isolated loopback time_tl_audit fixture required")
    schema = f"calendar_reminder_test_{uuid4().hex}"
    admin = create_async_engine(url)
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    first, calendar, _, _ = calendar_data
    second = User(id=uuid4(), telegram_user_id=18)
    created = False
    try:
        async with admin.begin() as connection:
            await connection.execute(CreateSchema(schema))
        created = True
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with factory() as session, session.begin():
            session.add_all([first, second])
            await session.flush()
            session.add_all(
                [
                    calendar,
                    Calendar(
                        id=uuid4(),
                        owner_user_id=second.id,
                        name=calendar.name,
                        snapshot_payload=calendar.snapshot_payload,
                        kind="PERSONAL",
                        owner_visible=True,
                        version=1,
                        snapshot_version=1,
                    ),
                    RuntimeSetting(
                        key="owner_configuration_state",
                        value={"active_revision": 15},
                        updated_by="fixture",
                    ),
                    RuntimeSetting(
                        key="owner_configuration_active",
                        value={"revision": 15, "staging_allowlist": [17, 18]},
                        updated_by="fixture",
                    ),
                ]
            )
        planner = PostgresCalendarReminderRepository(factory)
        assert await planner.set_enabled(17, True)
        assert await planner.set_enabled(18, True)
        assert await planner.plan(NOW) == 2
        async with factory() as session, session.begin():
            current = await session.get(Calendar, calendar.id)
            current.version += 1
        assert await planner.plan(NOW) == 1
        assert await planner.plan(NOW) == 0

        class OptOutAfterClaim(PostgresSenderRepository):
            async def claim_batch_skip_locked(self, now, *, limit):
                rows = await super().claim_batch_skip_locked(now, limit=limit)
                if rows:
                    assert {row.recipient.chat_id for row in rows} == {17, 18}
                    assert await planner.set_enabled(18, False) is False
                return rows

        class Transport:
            def __init__(self):
                self.sent = []

            async def send(self, message):
                self.sent.append(message)

        async def sleep(_seconds):
            pass

        transport = Transport()
        sender = SenderWorker(
            repository=OptOutAfterClaim(
                factory,
                stale_after_seconds=120,
                expected_owner_config_revision=15,
                expected_staging_allowlist=(17, 18),
                calendar_reminders_enabled=True,
                clock=lambda: NOW,
            ),
            transport=transport,
            clock=lambda: NOW,
        )
        assert await sender.run_once(NOW, sleep=sleep) == 1
        assert await sender.run_once(NOW, sleep=sleep) == 0
        assert len(transport.sent) == 1
        message = transport.sent[0]
        assert message.chat_id == 17
        assert "Подія" in message.text and "00:03" in message.text
        assert message.join_url is None
        async with factory() as session, session.begin():
            stored = (await session.scalars(select(OutboxMessage))).all()
            assert len(stored) == 3
            accepted, = [row for row in stored if row.delivered_at is not None]
            assert accepted.aggregate_id == str(first.id) and accepted.delivered_at == NOW
            superseded, = [
                row for row in stored
                if row.aggregate_id == str(first.id) and row.delivered_at is None
            ]
            assert superseded.last_error_code == "delivery_policy_changed"
            revoked, = [row for row in stored if row.aggregate_id == str(second.id)]
            assert revoked.delivered_at is None and revoked.dead_at == NOW
            assert revoked.last_error_code == "final_delivery_policy_changed"
            assert revoked.dispatch_started_at is None and revoked.indeterminate_at is None
            # Isolate the persisted uncertainty case without calling a transport.
            revoked.dispatch_started_at = revoked.indeterminate_at = NOW
        assert await planner.set_enabled(17, False) is False
        assert await planner.set_enabled(17, True)
        assert await planner.set_enabled(18, True)
        assert await planner.plan(NOW) == 0
    finally:
        await engine.dispose()
        try:
            if created:
                async with admin.begin() as connection:
                    await connection.execute(DropSchema(schema, cascade=True))
        finally:
            await admin.dispose()

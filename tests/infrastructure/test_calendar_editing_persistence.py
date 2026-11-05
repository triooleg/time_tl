"""CAL28 transaction simulations; PostgreSQL evidence is explicitly opt-in."""

from __future__ import annotations

import asyncio
import copy
import json
import os
from datetime import date, time
from uuid import uuid4

import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.infrastructure.test_calendar_repository import Database
from time_tl.domain.calendar_memberships import (
    CalendarAccessDenied,
    CalendarConflict,
    CalendarRole,
)
from time_tl.domain.calendars import CalendarKind
from time_tl.domain.schedule import DateOverride, Lesson, ScheduleProfile, ScheduleRule
from time_tl.infrastructure.db.calendars import AsyncPostgresCalendarRepository
from time_tl.infrastructure.db.models import (
    Calendar,
    CalendarInvitation,
    CalendarMembership,
    OutboxMessage,
    RuleLesson,
    RuntimeSetting,
    User,
)
from time_tl.infrastructure.db.models import ScheduleProfile as ProfileRow
from time_tl.infrastructure.db.models import ScheduleRule as RuleRow


def profile(name="Independent", *, empty=False):
    return ScheduleProfile(
        profile_id="local-editor-proposal",
        name=name,
        rules=() if empty else (ScheduleRule(0, (Lesson(time(9), time(10), "Fixture"),)),),
        overrides=(DateOverride(date(2026, 10, 8), day_off=True),) if not empty else (),
    )


def snapshot(db):
    return copy.deepcopy(
        {
            kind: {
                identity: {
                    column.name: getattr(row, column.name) for column in kind.__table__.columns
                }
                for identity, row in rows.items()
            }
            for kind, rows in db.rows.items()
        }
    )


def database():
    db = Database()
    user = db.user(1)
    user.mode = "LESSONS_AND_ALERTS"
    user.confirmed_region_id = "synthetic-region"
    user.alerts_enabled, user.lessons_enabled, user.reminder_lead_minutes = True, True, 5
    active = ProfileRow(
        id=uuid4(),
        user_id=user.id,
        public_id="active",
        name="Original",
        timezone="Europe/Kyiv",
        is_active=True,
        version=7,
    )
    rule = RuleRow(
        id=uuid4(),
        profile_id=active.id,
        weekday=0,
        week_parity="ANY",
        date_parity="ANY",
        position=0,
    )
    lesson = RuleLesson(
        id=uuid4(),
        rule_id=rule.id,
        position=0,
        start_time=time(8),
        end_time=time(9),
        title="Original",
        enabled=True,
        join_url_ciphertext=b"synthetic-encrypted-link",
    )
    outbox = OutboxMessage(
        id=uuid4(),
        event_key="synthetic-existing",
        aggregate_type="test",
        aggregate_id="fixture",
        event_type="ALERT",
        payload={"fixture": True},
    )
    setting = RuntimeSetting(key="owner_configuration_active", value={"revision": 15})
    for row in (active, rule, lesson, outbox, setting):
        identity = row.key if isinstance(row, RuntimeSetting) else row.id
        db.rows[type(row)] = {identity: row}
    return db


async def test_empty_calendar_create_then_edit_is_independent_and_override_roundtrip():
    db = database()
    before = snapshot(db)
    repo = db.repository()
    created = await repo.create_calendar(1, profile(empty=True), CalendarKind.PERSONAL)
    summary, stored = await repo.get_snapshot(1, created.calendar_id)
    assert summary == created and summary.role is CalendarRole.OWNER
    assert not stored.active and stored.rules == stored.overrides == ()
    assert stored.profile_id == str(created.calendar_id)
    assert db.rows[Calendar][created.calendar_id].snapshot_version == 1
    desired = profile("Edited")
    updated = await repo.update_calendar(1, created.calendar_id, 1, desired)
    assert updated.version == 2 and updated.name == "Edited"
    assert db.rows[Calendar][created.calendar_id].snapshot_version == 2
    _, stored = await repo.get_snapshot(1, created.calendar_id)
    assert stored.overrides == desired.overrides
    assert stored.rules[0].lessons[0].title == "Fixture" and not stored.active
    after = snapshot(db)
    for kind in before:
        if kind is not Calendar:
            # Includes active schedule/links/outbox/owner/settings and shared membership.
            assert after[kind] == before[kind]
    assert all(
        "schedule_profiles" not in sql and "outbox_messages" not in sql for sql in db.queries
    )
    assert any("calendars" in sql and "FOR UPDATE" in sql for sql in db.queries)


@pytest.mark.parametrize("role", [CalendarRole.OWNER, CalendarRole.ADMIN, CalendarRole.EDITOR])
async def test_authorized_roles_edit_hidden_calendar_without_changing_membership(role):
    db = database()
    repo = db.repository()
    created = await repo.create_calendar(1, profile(), CalendarKind.SCHOOL)
    actor = 1 if role is CalendarRole.OWNER else 2
    if actor == 2:
        member = CalendarMembership(
            calendar_id=created.calendar_id, user_id=db.user(2).id, role=role.value, visible=False
        )
        db.rows[CalendarMembership][created.calendar_id, member.user_id] = member
    before = snapshot(db)
    result = await repo.update_calendar(actor, created.calendar_id, 1, profile("Changed"))
    assert result.role is role and result.version == 2
    assert result.visible is (actor == 1)
    after = snapshot(db)
    assert after[CalendarMembership] == before[CalendarMembership]
    assert db.rows[Calendar][created.calendar_id].owner_visible is True
    assert db.rows[Calendar][created.calendar_id].owner_user_id == db.user(1).id


@pytest.mark.parametrize("member", [False, True])
async def test_member_and_outsider_denial_do_not_mutate_any_row(member):
    db = database()
    repo = db.repository()
    created = await repo.create_calendar(1, profile(), CalendarKind.SCHOOL)
    if member:
        entry = CalendarMembership(
            calendar_id=created.calendar_id,
            user_id=db.user(2).id,
            role=CalendarRole.MEMBER.value,
            visible=True,
        )
        db.rows[CalendarMembership][created.calendar_id, entry.user_id] = entry
    before = snapshot(db)
    with pytest.raises(CalendarAccessDenied):
        await repo.update_calendar(2, created.calendar_id, 1, profile("Denied"))
    assert snapshot(db) == before


async def test_stale_cas_and_parallel_content_updates_do_not_lose_winner():
    db = database()
    repo = db.repository()
    created = await repo.create_calendar(1, profile(), CalendarKind.PERSONAL)
    results = await asyncio.gather(
        *(repo.update_calendar(1, created.calendar_id, 1, profile(f"Edit {i}")) for i in range(8)),
        return_exceptions=True,
    )
    assert sum(isinstance(result, CalendarConflict) for result in results) == 7
    winner = next(result for result in results if not isinstance(result, Exception))
    assert winner.version == 2
    before = snapshot(db)
    with pytest.raises(CalendarConflict):
        await repo.update_calendar(1, created.calendar_id, 1, profile("Stale retry"))
    assert snapshot(db) == before
    assert db.rows[Calendar][created.calendar_id].snapshot_version == 2


@pytest.mark.parametrize(
    "change",
    ["overlap_rule", "overlap_override", "enabled_int", "weekday_bool", "blank_name", "quota"],
)
async def test_invalid_typed_profile_revalidates_before_session_and_preserves_rows(change):
    db = database()
    repo = db.repository()
    created = await repo.create_calendar(1, profile(), CalendarKind.PERSONAL)
    desired = profile()
    overlap = (Lesson(time(9), time(10), "A"), Lesson(time(9, 30), time(10, 30), "B"))
    # Simulate an untrusted in-process caller bypassing frozen model construction.
    if change == "overlap_rule":
        object.__setattr__(desired.rules[0], "lessons", overlap)
    elif change == "overlap_override":
        entry = DateOverride(date(2026, 10, 9))
        object.__setattr__(entry, "lessons", overlap)
        object.__setattr__(desired, "overrides", (entry,))
    elif change == "enabled_int":
        object.__setattr__(desired.rules[0].lessons[0], "enabled", 1)
    elif change == "weekday_bool":
        object.__setattr__(desired.rules[0], "weekday", True)
    elif change == "blank_name":
        object.__setattr__(desired, "name", " ")
    else:
        object.__setattr__(desired, "rules", desired.rules * 33)
    before, opened = snapshot(db), db.sessions
    for action in (
        lambda: repo.create_calendar(1, desired, CalendarKind.PERSONAL),
        lambda: repo.update_calendar(1, created.calendar_id, 1, desired),
    ):
        with pytest.raises(ValueError):
            await action()
        assert db.sessions == opened and snapshot(db) == before


@pytest.mark.parametrize("action", ["create", "edit"])
async def test_flush_failure_rolls_back_payload_versions_and_all_unrelated_state(action):
    db = database()
    repo = db.repository()
    created = await repo.create_calendar(1, profile(), CalendarKind.PERSONAL)
    before = snapshot(db)
    db.fail_flush = True
    with pytest.raises(RuntimeError, match="injected transaction failure"):
        if action == "create":
            await repo.create_calendar(1, profile("Not saved"), CalendarKind.SCHOOL)
        else:
            await repo.update_calendar(1, created.calendar_id, 1, profile("Not saved"))
    assert snapshot(db) == before
    db.fail_flush = False
    assert (await repo.update_calendar(1, created.calendar_id, 1, profile("Retry"))).version == 2


async def test_edit_link_identity_and_active_state_do_not_enter_calendar_payload():
    db = database()
    repo = db.repository()
    created = await repo.create_calendar(1, profile(empty=True), CalendarKind.ACTIVITY)
    lesson = Lesson(
        time(9),
        time(10),
        "Fixture",
        join_url="https://example.invalid/private",
        storage_ref="private-legacy-reference",
    )
    desired = ScheduleProfile(
        "private-legacy-profile", "Independent", active=True, rules=(ScheduleRule(0, (lesson,)),)
    )
    await repo.update_calendar(1, created.calendar_id, 1, desired)
    rendered = json.dumps(db.rows[Calendar][created.calendar_id].snapshot_payload)
    for private in ("private-legacy", "https://", "join_url", "storage_ref", "active", "user_id"):
        assert private not in rendered
    _, stored = await repo.get_snapshot(1, created.calendar_id)
    assert stored.active is False and stored.rules[0].lessons[0].join_url is None
    assert desired.active is True and desired.rules[0].lessons[0].join_url is not None
    assert db.rows[ProfileRow][next(iter(db.rows[ProfileRow]))].is_active is True


@pytest.mark.parametrize(
    "change",
    [
        {"actor": True},
        {"actor": 0},
        {"actor": 2**63},
        {"calendar_id": "not-a-uuid"},
        {"expected_version": True},
        {"expected_version": 0},
        {"expected_version": 2**31 - 1},
        {"profile": None},
    ],
)
async def test_edit_input_identity_and_version_are_strict_before_transaction(change):
    db = database()
    repo = db.repository()
    created = await repo.create_calendar(1, profile(), CalendarKind.SCHOOL)
    args = {
        "actor": 1,
        "calendar_id": created.calendar_id,
        "expected_version": 1,
        "profile": profile("Rejected"),
    }
    args.update(change)
    before, opened = snapshot(db), db.sessions
    with pytest.raises(ValueError):
        await repo.update_calendar(**args)
    assert db.sessions == opened and snapshot(db) == before


async def test_create_kind_does_not_accept_coercible_string():
    db = database()
    before = snapshot(db)
    with pytest.raises(ValueError, match="invalid calendar kind"):
        await db.repository().create_calendar(1, profile(empty=True), "PERSONAL")
    assert db.sessions == 0 and snapshot(db) == before


async def test_actual_postgres_empty_calendar_content_cas_is_opt_in_disposable_only():
    """Never connect unless a separate loopback trust-only lab is explicitly supplied."""
    database_url = os.environ.get("CALENDAR_EDITING_TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("requires isolated disposable loopback calendar_editcheck PostgreSQL")
    url = make_url(database_url)
    if (
        url.drivername != "postgresql+asyncpg"
        or url.host != "127.0.0.1"
        or url.database != "calendar_editcheck"
        or url.username != "calendar_editcheck"
        or url.password is not None
        or url.query
    ):
        pytest.fail("fixture must be loopback trust-only calendar_editcheck database")
    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    repo = AsyncPostgresCalendarRepository(factory)
    try:
        # This explicitly disposable fixture must have no pre-existing tables.
        async with engine.begin() as connection:
            for table in (
                User.__table__,
                Calendar.__table__,
                CalendarMembership.__table__,
                CalendarInvitation.__table__,
            ):
                await connection.run_sync(table.create)
        async with factory() as session, session.begin():
            session.add_all(
                [User(id=uuid4(), telegram_user_id=1), User(id=uuid4(), telegram_user_id=2)]
            )
        created = await repo.create_calendar(1, profile(empty=True), CalendarKind.PERSONAL)
        summary, stored = await repo.get_snapshot(1, created.calendar_id)
        assert summary.version == 1 and stored.rules == stored.overrides == ()
        results = await asyncio.gather(
            *(
                repo.update_calendar(1, created.calendar_id, 1, profile(f"Edit {i}"))
                for i in range(8)
            ),
            return_exceptions=True,
        )
        assert sum(isinstance(result, CalendarConflict) for result in results) == 7
        summary, stored = await repo.get_snapshot(1, created.calendar_id)
        assert summary.version == 2 and stored.overrides == profile().overrides
        with pytest.raises(CalendarAccessDenied):
            await repo.update_calendar(2, created.calendar_id, 2, profile("Denied"))
        assert (await repo.get_snapshot(1, created.calendar_id))[0] == summary
    finally:
        await engine.dispose()

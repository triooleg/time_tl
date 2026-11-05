"""LOCAL SQL/transaction simulation; disposable PostgreSQL is explicit opt-in."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from sqlalchemy import CheckConstraint
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateTable

from tests.infrastructure.test_calendar_editing_persistence import snapshot
from tests.infrastructure.test_calendar_repository import Database, Result, Session
from tests.persistence.test_calendar_schema import Inspector
from time_tl.domain.calendar_memberships import (
    CalendarAccessDenied,
    CalendarConflict,
    CalendarDataUnavailable,
    CalendarLimitExceeded,
)
from time_tl.domain.calendar_overrides import OverrideAction, PersonalEventChange
from time_tl.domain.calendars import Calendar, CalendarKind, CalendarMembership, CalendarSource
from time_tl.domain.schedule import DateOverride, Lesson, ScheduleProfile
from time_tl.infrastructure.db.calendar_overrides import AsyncPostgresPersonalCalendarRepository
from time_tl.infrastructure.db.calendars import AsyncPostgresCalendarRepository
from time_tl.infrastructure.db.models import Calendar as CalendarRow
from time_tl.infrastructure.db.models import CalendarMembership as MembershipRow
from time_tl.infrastructure.db.models import PersonalCalendarOverride as OverrideRow
from time_tl.infrastructure.db.models import RuntimeSetting, User

NOW = datetime(2026, 10, 4, 6, tzinfo=UTC)
DAY = date(2026, 10, 4)


class PersonalSession(Session):
    async def execute(self, query):
        if self.db.read_error:
            raise RuntimeError("injected unavailable storage")
        entities = [description["entity"] for description in query.column_descriptions]
        if entities == [OverrideRow]:
            compiled = query.compile(dialect=postgresql.dialect())
            self.db.queries.append(str(compiled))
            params = compiled.params
            rows = [
                row
                for row in self.db.rows[OverrideRow].values()
                if row.user_id == params["user_id_1"]
            ]
            rows.sort(key=lambda r: (r.calendar_id, r.snapshot_version, r.source_day, r.event_id))
            return Result(rows[: params["param_1"]])
        return await super().execute(query)

    def add(self, row):
        if isinstance(row, OverrideRow):
            key = row.user_id, row.calendar_id, row.snapshot_version, row.source_day, row.event_id
            self.db.rows[OverrideRow][key] = row
        else:
            super().add(row)


class PersonalDatabase(Database):
    def __init__(self):
        super().__init__()
        self.rows[OverrideRow] = {}
        self.rows[RuntimeSetting] = {
            "owner_configuration_active": RuntimeSetting(
                key="owner_configuration_active",
                value={"revision": 15},
            ),
        }
        self.read_error = False

    def factory(self):
        return PersonalSession(self)

    def repository(self):
        return AsyncPostgresCalendarRepository(self.factory)

    def personal(self):
        return AsyncPostgresPersonalCalendarRepository(self.factory)


def profile():
    return ScheduleProfile(
        "independent",
        "Shared",
        overrides=(
            DateOverride(
                DAY,
                lessons=(Lesson(time(9), time(10), "A"), Lesson(time(10), time(11), "B")),
            ),
        ),
    )


async def setup_calendar(*, member=True):
    db = PersonalDatabase()
    created = await db.repository().create_calendar(1, profile(), CalendarKind.SCHOOL)
    if member:
        row = MembershipRow(
            calendar_id=created.calendar_id, user_id=db.user(2).id, role="MEMBER", visible=True
        )
        db.rows[MembershipRow][created.calendar_id, row.user_id] = row
    return db, created.calendar_id


async def choice(db, identity, actor=2):
    return (await db.personal().list_event_choices(actor, identity, DAY, NOW))[0]


async def test_cancel_move_reset_actor_only_tombstone_no_shared_or_owner_writes():
    db, identity = await setup_calendar()
    before = snapshot(db)
    db.queries.clear()
    selected = await choice(db, identity)
    assert (
        await db.personal().save_override(
            2, PersonalEventChange(selected, OverrideAction.CANCEL), NOW
        )
        == 1
    )
    mine = await db.personal().read_personal_day(2, NOW)
    other = await db.personal().read_personal_day(1, NOW)
    assert [e.title for e in mine.events] == ["B"]
    assert [e.title for e in other.events] == ["A", "B"]
    selected = await choice(db, identity)
    assert selected.override_version == 1
    tomorrow = DAY + timedelta(days=1)
    move = PersonalEventChange(selected, OverrideAction.MOVE, tomorrow, time(12), time(13))
    assert await db.personal().save_override(2, move, NOW) == 2
    moved = await db.personal().read_personal_day(2, NOW + timedelta(days=1))
    assert len(moved.events) == 1 and moved.events[0].title == "A"
    assert moved.events[0].start.hour == 12
    selected = await choice(db, identity)
    assert (
        await db.personal().save_override(
            2, PersonalEventChange(selected, OverrideAction.RESET), NOW
        )
        == 3
    )
    assert [e.title for e in (await db.personal().read_personal_day(2, NOW)).events] == ["A", "B"]
    rows = list(db.rows[OverrideRow].values())
    assert len(rows) == 1 and rows[0].action == "RESET" and rows[0].version == 3
    after = snapshot(db)
    for kind in before:
        if kind is not OverrideRow:
            assert before[kind] == after[kind]
    queries = "\n".join(db.queries)
    assert "FOR UPDATE OF calendars" in queries
    assert all(
        name not in queries for name in ("outbox_messages", "runtime_settings", "schedule_profiles")
    )
    locks = [q for q in db.queries if "FOR UPDATE" in q]
    assert "users" in locks[0] and "calendars" in locks[1]


async def test_parallel_override_cas_and_reset_aba_refusal():
    db, identity = await setup_calendar()
    selected = await choice(db, identity)
    results = await asyncio.gather(
        *(
            db.personal().save_override(
                2,
                PersonalEventChange(selected, OverrideAction.CANCEL),
                NOW,
            )
            for _ in range(6)
        ),
        return_exceptions=True,
    )
    assert results.count(1) == 1 and sum(isinstance(r, CalendarConflict) for r in results) == 5
    await db.personal().save_override(
        2, PersonalEventChange(await choice(db, identity), OverrideAction.RESET), NOW
    )
    before = snapshot(db)
    with pytest.raises(CalendarConflict):
        await db.personal().save_override(
            2, PersonalEventChange(selected, OverrideAction.CANCEL), NOW
        )
    assert snapshot(db) == before


@pytest.mark.parametrize("role", ["OWNER", "MEMBER", "EDITOR", "ADMIN"])
async def test_every_visible_joined_role_may_edit_only_own_view(role):
    db, identity = await setup_calendar()
    actor = 1 if role == "OWNER" else 2
    if actor == 2:
        db.rows[MembershipRow][identity, db.user(2).id].role = role
    await db.personal().save_override(
        actor,
        PersonalEventChange(await choice(db, identity, actor), OverrideAction.CANCEL),
        NOW,
    )
    assert len((await db.personal().read_personal_day(actor, NOW)).events) == 1
    other = 2 if actor == 1 else 1
    assert len((await db.personal().read_personal_day(other, NOW)).events) == 2
    assert next(iter(db.rows[OverrideRow].values())).user_id == db.user(actor).id


async def test_personal_move_overlap_stays_visible_not_auto_resolved():
    db, identity = await setup_calendar()
    change = PersonalEventChange(
        await choice(db, identity),
        OverrideAction.MOVE,
        DAY,
        time(10, 15),
        time(10, 45),
    )
    await db.personal().save_override(2, change, NOW)
    day = await db.personal().read_personal_day(2, NOW)
    assert len(day.events) == 2 and len(day.conflicts) == 1


@pytest.mark.parametrize("denial", ["hidden", "left", "outsider", "owner-hidden", "deleted"])
async def test_current_visibility_and_membership_rechecked_no_leak_or_mutation(denial):
    db, identity = await setup_calendar()
    selected = await choice(db, identity)
    actor = 2
    if denial == "hidden":
        db.rows[MembershipRow][identity, db.user(2).id].visible = False
    elif denial == "left":
        del db.rows[MembershipRow][identity, db.user(2).id]
    elif denial == "outsider":
        actor = 3
    elif denial == "owner-hidden":
        actor = 1
        db.rows[CalendarRow][identity].owner_visible = False
    else:
        del db.rows[CalendarRow][identity]
        del db.rows[MembershipRow][identity, db.user(2).id]
    before = snapshot(db)
    with pytest.raises((CalendarAccessDenied, CalendarDataUnavailable)):
        await db.personal().save_override(
            actor, PersonalEventChange(selected, OverrideAction.CANCEL), NOW
        )
    with pytest.raises((CalendarAccessDenied, CalendarDataUnavailable)):
        await db.personal().list_event_choices(actor, identity, DAY, NOW)
    assert not (await db.personal().read_personal_day(actor, NOW)).events
    assert snapshot(db) == before


@pytest.mark.parametrize("drift", ["calendar", "snapshot", "source"])
async def test_version_and_source_semantics_exact_cas(drift):
    db, identity = await setup_calendar()
    selected = await choice(db, identity)
    row = db.rows[CalendarRow][identity]
    if drift == "calendar":
        row.version += 1
    elif drift == "snapshot":
        row.snapshot_version += 1
    else:
        row.snapshot_payload["overrides"][0]["lessons"][0]["title"] = "Changed"
    before = snapshot(db)
    with pytest.raises(CalendarConflict):
        await db.personal().save_override(
            2, PersonalEventChange(selected, OverrideAction.CANCEL), NOW
        )
    assert snapshot(db) == before


async def test_new_shared_snapshot_does_not_rebind_old_personal_change():
    db, identity = await setup_calendar()
    await db.personal().save_override(
        2, PersonalEventChange(await choice(db, identity), OverrideAction.CANCEL), NOW
    )
    row = db.rows[CalendarRow][identity]
    row.version += 1
    row.snapshot_version += 1
    assert len((await db.personal().read_personal_day(2, NOW)).events) == 2
    assert (await choice(db, identity)).override_version == 0
    assert len(db.rows[OverrideRow]) == 1


@pytest.mark.parametrize("offset", [-1, 31])
async def test_source_date_bounded_before_transaction(offset):
    db, identity = await setup_calendar()
    opened = db.sessions
    with pytest.raises(ValueError):
        await db.personal().list_event_choices(2, identity, DAY + timedelta(days=offset), NOW)
    assert db.sessions == opened


@pytest.mark.parametrize("offset", [-1, 31])
async def test_target_date_bounded_and_no_write(offset):
    db, identity = await setup_calendar()
    selected = await choice(db, identity)
    before = snapshot(db)
    move = PersonalEventChange(
        selected, OverrideAction.MOVE, DAY + timedelta(days=offset), time(12), time(13)
    )
    with pytest.raises(ValueError):
        await db.personal().save_override(2, move, NOW)
    assert snapshot(db) == before


async def test_kyiv_today_not_utc_date_and_upper_bound():
    db, identity = await setup_calendar()
    late_utc = NOW.replace(day=3, hour=22)
    assert await db.personal().list_event_choices(2, identity, DAY, late_utc)
    selected = await choice(db, identity)
    move = PersonalEventChange(
        selected, OverrideAction.MOVE, DAY + timedelta(days=30), time(12), time(13)
    )
    assert await db.personal().save_override(2, move, NOW) == 1


async def test_tombstones_and_stale_rows_count_towards_quota():
    db, identity = await setup_calendar()
    selected = await choice(db, identity)
    for index in range(500):
        row = OverrideRow(
            user_id=db.user(2).id,
            calendar_id=identity,
            snapshot_version=index + 100,
            source_day=DAY,
            event_id=uuid4(),
            action="RESET",
            version=1,
        )
        PersonalSession(db).add(row)
    before = snapshot(db)
    with pytest.raises(CalendarLimitExceeded):
        await db.personal().save_override(
            2, PersonalEventChange(selected, OverrideAction.CANCEL), NOW
        )
    assert snapshot(db) == before
    assert len((await db.personal().read_personal_day(2, NOW)).events) == 2


async def test_existing_row_can_reset_at_quota_but_overquota_reads_fail_closed():
    db, identity = await setup_calendar()
    await db.personal().save_override(
        2,
        PersonalEventChange(await choice(db, identity), OverrideAction.CANCEL),
        NOW,
    )
    for index in range(499):
        PersonalSession(db).add(
            OverrideRow(
                user_id=db.user(2).id,
                calendar_id=identity,
                snapshot_version=index + 100,
                source_day=DAY,
                event_id=uuid4(),
                action="RESET",
                version=1,
            )
        )
    assert (
        await db.personal().save_override(
            2,
            PersonalEventChange(await choice(db, identity), OverrideAction.RESET),
            NOW,
        )
        == 2
    )
    PersonalSession(db).add(
        OverrideRow(
            user_id=db.user(2).id,
            calendar_id=identity,
            snapshot_version=1000,
            source_day=DAY,
            event_id=uuid4(),
            action="RESET",
            version=1,
        )
    )
    with pytest.raises(CalendarLimitExceeded):
        await db.personal().read_personal_day(2, NOW)


@pytest.mark.parametrize("tamper", ["actor", "action", "override_version", "target_time"])
async def test_strict_tampered_boundary_rejected_before_session(tamper):
    db, identity = await setup_calendar()
    selected = await choice(db, identity)
    change = PersonalEventChange(selected, OverrideAction.CANCEL)
    actor = 2
    if tamper == "actor":
        actor = True
    elif tamper == "action":
        object.__setattr__(change, "action", "CANCEL")
    elif tamper == "override_version":
        object.__setattr__(selected, "override_version", True)
    else:
        object.__setattr__(change, "start", time(12, 0, 1))
    before, sessions = snapshot(db), db.sessions
    with pytest.raises(ValueError):
        await db.personal().save_override(actor, change, NOW)
    assert db.sessions == sessions and snapshot(db) == before


async def test_corrupt_current_override_never_silently_returns_shared_day():
    db, identity = await setup_calendar()
    await db.personal().save_override(
        2,
        PersonalEventChange(await choice(db, identity), OverrideAction.CANCEL),
        NOW,
    )
    next(iter(db.rows[OverrideRow].values())).action = "UNKNOWN"
    with pytest.raises(ValueError):
        await db.personal().read_personal_day(2, NOW)


async def test_read_calendar_quota_is_bounded_even_for_corrupt_oversized_state():
    db, identity = await setup_calendar()
    original = db.rows[CalendarRow][identity]
    for _ in range(11):
        row = CalendarRow(
            id=uuid4(),
            owner_user_id=db.user(2).id,
            name="Synthetic",
            kind="PERSONAL",
            snapshot_payload=original.snapshot_payload,
            owner_visible=True,
            version=1,
            snapshot_version=1,
        )
        db.rows[CalendarRow][row.id] = row
    with pytest.raises(CalendarLimitExceeded):
        await db.personal().read_personal_day(2, NOW)


async def test_flush_failure_rolls_back_and_storage_read_failure_never_falls_back():
    db, identity = await setup_calendar()
    selected = await choice(db, identity)
    before = snapshot(db)
    db.fail_flush = True
    with pytest.raises(RuntimeError):
        await db.personal().save_override(
            2, PersonalEventChange(selected, OverrideAction.CANCEL), NOW
        )
    assert snapshot(db) == before
    db.fail_flush = False
    db.read_error = True
    with pytest.raises(RuntimeError):
        await db.personal().read_personal_day(2, NOW)


async def test_optional_single_actor_legacy_source_preserved_without_changes():
    db, _ = await setup_calendar()
    legacy = CalendarSource(
        Calendar("legacy:" + "a" * 64, "Active legacy", CalendarKind.PERSONAL, profile()),
        CalendarMembership(2, "legacy:" + "a" * 64),
    )
    day = await db.personal().read_personal_day(2, NOW, extra_sources=(legacy,))
    assert len(day.events) == 4 and len(day.conflicts) == 2
    for extra in (
        (legacy, legacy),
        (replace(legacy, membership=CalendarMembership(3, "legacy:" + "a" * 64)),),
    ):
        with pytest.raises(ValueError):
            await db.personal().read_personal_day(2, NOW, extra_sources=extra)


async def test_personal_export_self_only_all_hidden_stale_reset_rows_closed_schema():
    db, identity = await setup_calendar()
    selected = await choice(db, identity)
    await db.personal().save_override(
        2,
        PersonalEventChange(selected, OverrideAction.CANCEL),
        NOW,
    )
    stale = OverrideRow(
        user_id=db.user(2).id,
        calendar_id=identity,
        snapshot_version=99,
        source_day=DAY - timedelta(days=20),
        event_id=uuid4(),
        action="RESET",
        version=5,
    )
    moved = OverrideRow(
        user_id=db.user(2).id,
        calendar_id=identity,
        snapshot_version=99,
        source_day=DAY - timedelta(days=19),
        event_id=uuid4(),
        action="MOVE",
        version=6,
        target_day=DAY - timedelta(days=18),
        start_time=time(12),
        end_time=time(13),
    )
    foreign = OverrideRow(
        user_id=db.user(1).id,
        calendar_id=identity,
        snapshot_version=100,
        source_day=DAY,
        event_id=uuid4(),
        action="CANCEL",
        version=1,
    )
    for row in (stale, moved, foreign):
        PersonalSession(db).add(row)
    db.rows[MembershipRow][identity, db.user(2).id].visible = False
    db.rows[CalendarRow][identity].owner_visible = False
    before = snapshot(db)
    raw = await db.personal().export_personal_overrides(2)
    value = json.loads(raw)
    assert set(value) == {"schema_version", "overrides"} and value["schema_version"] == "1.0"
    assert len(value["overrides"]) == 3
    fields = {
        "calendar_id",
        "snapshot_version",
        "source_day",
        "event_id",
        "action",
        "version",
        "target_day",
        "start",
        "end",
    }
    assert all(set(row) == fields for row in value["overrides"])
    assert {row["event_id"] for row in value["overrides"]} == {
        str(selected.event_id),
        str(stale.event_id),
        str(moved.event_id),
    }
    move = next(row for row in value["overrides"] if row["action"] == "MOVE")
    assert move["start"] == "12:00" and move["end"] == "13:00"
    assert any(row["action"] == "RESET" and row["version"] == 5 for row in value["overrides"])
    for private in (
        str(foreign.event_id),
        str(db.user(1).id),
        str(db.user(2).id),
        '"title"',
        '"user_id"',
        "join_url",
        "token",
        "https://",
    ):
        assert private not in raw
    assert snapshot(db) == before
    own = json.loads(await db.personal().export_personal_overrides(1))
    assert len(own["overrides"]) == 1 and own["overrides"][0]["event_id"] == str(foreign.event_id)


async def test_personal_export_empty_and_foreign_quota_does_not_affect_actor():
    db, identity = await setup_calendar()
    for index in range(501):
        PersonalSession(db).add(
            OverrideRow(
                user_id=db.user(1).id,
                calendar_id=identity,
                snapshot_version=index + 1,
                source_day=DAY,
                event_id=uuid4(),
                action="RESET",
                version=1,
            )
        )
    assert json.loads(await db.personal().export_personal_overrides(2)) == {
        "schema_version": "1.0",
        "overrides": [],
    }
    with pytest.raises(CalendarLimitExceeded):
        await db.personal().export_personal_overrides(1)


@pytest.mark.parametrize("actor", [True, 0, -1, 2**63])
async def test_personal_export_strict_actor_before_transaction(actor):
    db = PersonalDatabase()
    with pytest.raises(ValueError):
        await db.personal().export_personal_overrides(actor)
    assert db.sessions == 0


async def test_personal_export_read_errors_and_corruption_not_silently_empty():
    db, identity = await setup_calendar()
    PersonalSession(db).add(
        OverrideRow(
            user_id=db.user(2).id,
            calendar_id=identity,
            snapshot_version=1,
            source_day=DAY,
            event_id=uuid4(),
            action="UNKNOWN",
            version=1,
        )
    )
    with pytest.raises(ValueError):
        await db.personal().export_personal_overrides(2)
    db.read_error = True
    with pytest.raises(RuntimeError):
        await db.personal().export_personal_overrides(2)


def test_frozen_migration_schema_matches_row_and_cascades_only_new_table():
    migration = importlib.import_module("migrations.versions.0010_personal_calendar_overrides")
    frozen, current = migration._schema(), OverrideRow.__table__
    assert set(frozen.c.keys()) == set(current.c.keys())
    for name in frozen.c.keys():
        assert str(frozen.c[name].type) == str(current.c[name].type)
        assert frozen.c[name].nullable == current.c[name].nullable
    assert {str(c.sqltext) for c in frozen.constraints if isinstance(c, CheckConstraint)} == {
        str(c.sqltext) for c in current.constraints if isinstance(c, CheckConstraint)
    }
    ddl = str(CreateTable(frozen).compile(dialect=postgresql.dialect()))
    assert ddl.count("ON DELETE CASCADE") == 2
    assert "outbox" not in ddl and "snapshot_payload" not in ddl and "title" not in ddl


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_migration_offline_rejected_before_schema_mutation(monkeypatch, direction):
    migration = importlib.import_module("migrations.versions.0010_personal_calendar_overrides")
    monkeypatch.setattr(migration.context, "is_offline_mode", lambda: True)
    operations = MagicMock()
    monkeypatch.setattr(migration, "op", operations)
    with pytest.raises(RuntimeError):
        getattr(migration, direction)()
    operations.get_bind.assert_not_called()


def test_migration_validate_and_refuse_existing_schema_drift(monkeypatch):
    migration = importlib.import_module("migrations.versions.0010_personal_calendar_overrides")
    prior = importlib.import_module("migrations.versions.0009_calendar_foundation")
    table = migration._schema()
    monkeypatch.setattr(prior, "_schema", lambda: (table,))
    inspector = Inspector()
    operations = MagicMock()
    operations.get_bind.return_value.dialect = postgresql.dialect()
    monkeypatch.setattr(migration, "op", operations)
    monkeypatch.setattr(prior, "op", operations)
    monkeypatch.setattr(migration.context, "is_offline_mode", lambda: False)
    monkeypatch.setattr(migration.sa, "inspect", lambda _: inspector)
    migration.upgrade()
    inspector.foreign[table.name][0]["options"]["ondelete"] = "RESTRICT"
    with pytest.raises(RuntimeError):
        migration.upgrade()


def test_migration_grouped_or_and_reflected_casts_not_weakened():
    migration = importlib.import_module("migrations.versions.0010_personal_calendar_overrides")
    canonical = migration._check_expression
    assert canonical(
        "(action = 'MOVE' AND target_day IS NOT NULL) OR action IN ('CANCEL','RESET')"
    ) == canonical(
        "(((action)::text = 'MOVE'::text) AND (target_day IS NOT NULL)) OR "
        "((action)::text = ANY (ARRAY['CANCEL'::text, 'RESET'::text]))",
    )
    assert canonical("(start_time IS NULL OR EXTRACT(SECOND FROM start_time) = 0)") == canonical(
        "((start_time IS NULL) OR (EXTRACT(second FROM start_time) = (0)::numeric))",
    )
    assert canonical("(a OR b) AND c") != canonical("a OR (b AND c)")
    assert canonical("action IN ('CANCEL','RESET')") != canonical("action IN ('MOVE','RESET')")
    assert canonical("version BETWEEN 1 AND 2147483646") != canonical(
        "version BETWEEN 0 AND 2147483646"
    )


async def test_disposable_postgres_cas_and_cascade_opt_in():
    url_text = os.environ.get("PERSONAL_CALENDAR_TEST_DATABASE_URL")
    if url_text is None:
        pytest.skip("requires explicit disposable loopback personal_overridecheck PostgreSQL")
    url = make_url(url_text)
    if (
        url.drivername != "postgresql+asyncpg"
        or url.host != "127.0.0.1"
        or url.database != "personal_overridecheck"
        or url.username != "personal_overridecheck"
        or url.password is not None
        or url.query
    ):
        pytest.fail("isolated loopback trust-only personal_overridecheck fixture required")
    engine = create_async_engine(url_text)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    tables = (User.__table__, CalendarRow.__table__, MembershipRow.__table__, OverrideRow.__table__)
    try:
        async with engine.begin() as connection:
            for table in tables:
                await connection.run_sync(table.create)
        async with factory() as session, session.begin():
            session.add(User(id=uuid4(), telegram_user_id=1))
        shared = AsyncPostgresCalendarRepository(factory)
        personal = AsyncPostgresPersonalCalendarRepository(factory)
        created = await shared.create_calendar(1, profile(), CalendarKind.SCHOOL)
        selected = (await personal.list_event_choices(1, created.calendar_id, DAY, NOW))[0]
        results = await asyncio.gather(
            *(
                personal.save_override(
                    1,
                    PersonalEventChange(selected, OverrideAction.CANCEL),
                    NOW,
                )
                for _ in range(4)
            ),
            return_exceptions=True,
        )
        assert results.count(1) == 1 and sum(isinstance(r, CalendarConflict) for r in results) == 3
        async with factory() as session, session.begin():
            user = await AsyncPostgresCalendarRepository._user(session, 1, lock=True)
            await session.delete(user)
        async with factory() as session:
            assert not (await session.execute(OverrideRow.__table__.select())).all()
    finally:
        await engine.dispose()

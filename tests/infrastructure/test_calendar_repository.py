"""SQL-query/transaction simulation; these tests are NOT PostgreSQL evidence."""

from __future__ import annotations

import asyncio
import copy
import json
import os
from datetime import UTC, datetime, time, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.sql.dml import Delete

from time_tl.domain.calendar_memberships import (
    CalendarAccessDenied,
    CalendarConflict,
    CalendarDataUnavailable,
    CalendarLimitExceeded,
    CalendarRole,
)
from time_tl.domain.calendars import CalendarKind, build_my_day
from time_tl.domain.schedule import Lesson, ScheduleProfile, ScheduleRule
from time_tl.infrastructure.db.calendars import (
    AsyncPostgresCalendarRepository,
    digest_invitation_token,
)
from time_tl.infrastructure.db.models import Calendar, CalendarInvitation, CalendarMembership, User

NOW = datetime(2026, 9, 28, 6, 35, tzinfo=UTC)


def profile(name="Math"):
    return ScheduleProfile(
        profile_id="private",
        name=name,
        rules=(ScheduleRule(weekday=0, lessons=(Lesson(time(9), time(9, 45), name),)),),
    )


class Result:
    def __init__(self, values):
        self.values = values

    def scalar_one_or_none(self):
        assert len(self.values) <= 1
        return self.values[0] if self.values else None

    def one_or_none(self):
        return self.scalar_one_or_none()

    def scalars(self):
        return self

    def all(self):
        return list(self.values)


class Transaction:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        await self.session.db.lock.acquire()
        self.before = copy.deepcopy(self.session.db.rows)

    async def __aexit__(self, kind, value, traceback):
        if kind is not None:
            self.session.db.rows = self.before
        self.session.db.lock.release()


class Session:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        self.db.sessions += 1
        return self

    async def __aexit__(self, *args):
        pass

    def begin(self):
        return Transaction(self)

    async def execute(self, query):
        sql = str(query.compile(dialect=postgresql.dialect()))
        self.db.queries.append(sql)
        params = query.compile().params
        if isinstance(query, Delete):
            identities = params["token_hash_1"]
            deleted = []
            for identity in identities:
                if identity in self.db.rows[CalendarInvitation]:
                    del self.db.rows[CalendarInvitation][identity]
                    deleted.append(identity)
            return Result(deleted)
        entities = [description["entity"] for description in query.column_descriptions]
        if entities == [User]:
            identity = params.get("telegram_user_id_1")
            values = (
                [row for row in self.db.rows[User].values() if row.telegram_user_id in identity]
                if isinstance(identity, list)
                else [
                    row for row in self.db.rows[User].values() if row.telegram_user_id == identity
                ]
            )
            return Result(sorted(values, key=lambda row: row.telegram_user_id))
        if entities == [Calendar]:
            row = self.db.rows[Calendar].get(params["id_1"])
            return Result([row] if row else [])
        if entities == [CalendarMembership]:
            row = self.db.rows[CalendarMembership].get(
                (params["calendar_id_1"], params["user_id_1"])
            )
            return Result([row] if row else [])
        if entities == [CalendarInvitation]:
            if "expires_at_1" in params:
                rows = sorted(
                    (
                        row
                        for row in self.db.rows[CalendarInvitation].values()
                        if row.expires_at <= params["expires_at_1"]
                    ),
                    key=lambda row: (row.expires_at, row.token_hash),
                )
                return Result([row.token_hash for row in rows[: params["param_1"]]])
            row = self.db.rows[CalendarInvitation].get(params["token_hash_1"])
            return Result([row] if row else [])
        if entities == [Calendar, CalendarInvitation]:
            invite = self.db.rows[CalendarInvitation].get(params["token_hash_1"])
            row = self.db.rows[Calendar].get(invite.calendar_id) if invite else None
            return Result([(row, invite)] if row else [])
        if entities == [Calendar, CalendarMembership]:
            actor = params["owner_user_id_1"]
            assert params["user_id_1"] == actor and params["user_id_2"] == actor
            result = []
            for row in sorted(self.db.rows[Calendar].values(), key=lambda item: item.id):
                member = self.db.rows[CalendarMembership].get((row.id, actor))
                if row.owner_user_id == actor or member:
                    if "id_1" not in params or row.id == params["id_1"]:
                        result.append((row, member))
            return Result(result[: params.get("param_1", len(result))])
        raise AssertionError(f"unexpected query entities {entities}")

    async def scalar(self, query):
        params = query.compile().params
        return sum(
            invite.calendar_id == params["calendar_id_1"]
            and invite.revoked_at is None
            and invite.consumed_at is None
            and invite.expires_at > params["expires_at_1"]
            for invite in self.db.rows[CalendarInvitation].values()
        )

    async def get(self, kind, identity):
        return self.db.rows[kind].get(identity)

    def add(self, row):
        if isinstance(row, CalendarMembership):
            identity = row.calendar_id, row.user_id
        elif isinstance(row, CalendarInvitation):
            identity = row.token_hash
        else:
            identity = row.id
        self.db.rows[type(row)][identity] = row

    async def delete(self, row):
        if isinstance(row, CalendarMembership):
            del self.db.rows[CalendarMembership][row.calendar_id, row.user_id]
        else:
            del self.db.rows[Calendar][row.id]
            # Simulation of declared FK cascade; real cascade needs the opt-in PG test.
            for kind in (CalendarMembership, CalendarInvitation):
                self.db.rows[kind] = {
                    key: value
                    for key, value in self.db.rows[kind].items()
                    if value.calendar_id != row.id
                }

    async def flush(self):
        if self.db.fail_flush:
            raise RuntimeError("injected transaction failure")


class Database:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.sessions = 0
        self.queries = []
        self.fail_flush = False
        self.rows = {User: {}, Calendar: {}, CalendarMembership: {}, CalendarInvitation: {}}
        for identity in range(1, 26):
            user = User(id=uuid4(), telegram_user_id=identity)
            self.rows[User][user.id] = user

    def repository(self):
        return AsyncPostgresCalendarRepository(lambda: Session(self))

    def user(self, identity):
        return next(row for row in self.rows[User].values() if row.telegram_user_id == identity)


async def created(db, owner=1):
    return await db.repository().create_calendar(owner, profile(), CalendarKind.SCHOOL)


async def invited(db, calendar, target=2):
    repo = db.repository()
    version = db.rows[Calendar][calendar.calendar_id].version
    return await repo.create_invitation(1, calendar.calendar_id, target, version, NOW)


async def joined(db, calendar, target=2):
    repo = db.repository()
    token = await invited(db, calendar, target)
    preview = await repo.preview_invitation(target, token, NOW)
    return await repo.accept_invitation(target, token, preview.summary.version, NOW)


async def test_create_is_independent_no_backfill_or_foreign_identity_exposure():
    db = Database()
    calendar = await created(db)
    assert calendar.role is CalendarRole.OWNER and calendar.version == 1
    assert await db.repository().list_calendars(2) == ()
    sources = await db.repository().list_sources(1)
    assert len(sources) == 1
    assert sources[0].calendar.calendar_id == f"calendar:{calendar.calendar_id}"
    assert sources[0].calendar.profile.active is False
    assert "private" not in str(db.rows[Calendar][calendar.calendar_id].snapshot_payload)
    assert "owner_user_id" not in repr(calendar)
    assert len(build_my_day(1, sources, NOW).events) == 1
    assert any("FOR UPDATE" in query and "users" in query for query in db.queries)


async def test_snapshot_is_one_authorized_consistent_read_hidden_allowed():
    db = Database()
    calendar = await created(db)
    member = await joined(db, calendar)
    hidden = await db.repository().set_visible(2, calendar.calendar_id, member.version, False)
    summary, snapshot = await db.repository().get_snapshot(2, calendar.calendar_id)
    assert summary == hidden and not summary.visible
    assert snapshot.active is False and snapshot.name == "Math"
    with pytest.raises(CalendarDataUnavailable):
        await db.repository().get_snapshot(3, calendar.calendar_id)
    with pytest.raises(CalendarDataUnavailable):
        await db.repository().get_snapshot(2, uuid4())


async def test_export_owned_and_joined_hidden_without_other_identities_or_invitation_data():
    db = Database()
    shared = await created(db)
    member = await joined(db, shared)
    await db.repository().set_visible(2, shared.calendar_id, member.version, False)
    personal = await created(db, owner=2)
    token = await db.repository().create_invitation(2, personal.calendar_id, 3, 1, NOW)
    exported = await db.repository().export_calendars(2)
    assert len(exported) == 2
    assert {item["role"] for item in exported} == {"OWNER", "MEMBER"}
    assert set(exported[0]) == {
        "calendar_id",
        "name",
        "kind",
        "role",
        "visible",
        "version",
        "snapshot",
    }
    rendered = json.dumps(exported, ensure_ascii=False)
    for forbidden in (
        str(db.user(1).id),
        str(db.user(3).id),
        "telegram_user_id",
        "owner_user_id",
        "user_id",
        "target_user_id",
        "issuer_user_id",
        "token",
        token,
        digest_invitation_token(token),
        "join_url",
    ):
        assert forbidden not in rendered
    assert await db.repository().export_calendars(4) == ()


async def test_export_corrupt_payload_fails_whole_result_without_sensitive_exception():
    db = Database()
    calendar = await created(db)
    db.rows[Calendar][calendar.calendar_id].snapshot_payload["unexpected"] = "sensitive"
    with pytest.raises(ValueError) as error:
        await db.repository().export_calendars(1)
    assert "sensitive" not in str(error.value)


async def test_cleanup_bounded_expiry_tombstones_and_no_calendar_writes():
    db = Database()
    calendar = await created(db)
    token1 = await invited(db, calendar, 2)
    token2 = await invited(db, calendar, 3)
    await db.repository().revoke_invitation(1, token2, 3)
    preview = await db.repository().preview_invitation(2, token1, NOW)
    await db.repository().accept_invitation(2, token1, preview.summary.version, NOW)
    await invited(db, calendar, 4)
    version = db.rows[Calendar][calendar.calendar_id].version
    assert await db.repository().cleanup_invitations(NOW + timedelta(hours=23)) == 0
    assert await db.repository().cleanup_invitations(NOW + timedelta(hours=24), limit=2) == 2
    assert len(db.rows[CalendarInvitation]) == 1
    assert await db.repository().cleanup_invitations(NOW + timedelta(hours=24), limit=2) == 1
    assert db.rows[Calendar][calendar.calendar_id].version == version
    assert len(db.rows[CalendarMembership]) == 1
    assert any("SKIP LOCKED" in query for query in db.queries)


@pytest.mark.parametrize(
    "change", [{"now": NOW.replace(tzinfo=None)}, {"limit": True}, {"limit": 0}, {"limit": 1001}]
)
async def test_bad_cleanup_guard_rejects_before_session(change):
    db = Database()
    arguments = {"now": NOW, "limit": 200}
    arguments.update(change)
    with pytest.raises(ValueError):
        await db.repository().cleanup_invitations(**arguments)
    assert db.sessions == 0


async def test_owner_and_member_visibility_are_personal_and_hidden_counts_for_quota():
    db = Database()
    calendar = await created(db)
    member = await joined(db, calendar)
    repo = db.repository()
    hidden = await repo.set_visible(2, calendar.calendar_id, member.version, False)
    assert hidden.role is CalendarRole.MEMBER and not hidden.visible
    assert await repo.list_sources(2) == ()
    assert len(await repo.list_sources(1)) == 1
    owner_hidden = await repo.set_visible(1, calendar.calendar_id, hidden.version, False)
    assert not owner_hidden.visible
    assert len(await repo.list_calendars(1)) == 1
    assert len(await repo.list_calendars(2)) == 1


async def test_roles_shared_update_and_version_compare_and_swap():
    db = Database()
    calendar = await created(db)
    member = await joined(db, calendar)
    repo = db.repository()
    with pytest.raises(CalendarAccessDenied):
        await repo.update_calendar(2, calendar.calendar_id, member.version, profile("Science"))
    owner = await repo.set_role(1, calendar.calendar_id, 2, member.version, CalendarRole.EDITOR)
    assert owner.role is CalendarRole.OWNER
    updated = await repo.update_calendar(2, calendar.calendar_id, owner.version, profile("Science"))
    assert updated.role is CalendarRole.EDITOR
    assert (await repo.list_sources(1))[0].calendar.name == "Science"
    assert (await repo.list_sources(2))[0].calendar.name == "Science"
    with pytest.raises(CalendarConflict):
        await repo.update_calendar(1, calendar.calendar_id, owner.version, profile())
    with pytest.raises(CalendarAccessDenied):
        await repo.set_role(2, calendar.calendar_id, 3, updated.version, CalendarRole.ADMIN)
    with pytest.raises(ValueError):
        await repo.set_role(1, calendar.calendar_id, 2, updated.version, CalendarRole.OWNER)


async def test_access_denied_for_foreign_user_no_writes_and_owner_cannot_leave():
    db = Database()
    calendar = await created(db)
    before = db.rows[Calendar][calendar.calendar_id].version
    for action in (
        lambda: db.repository().set_visible(2, calendar.calendar_id, before, False),
        lambda: db.repository().delete_calendar(2, calendar.calendar_id, before),
        lambda: db.repository().leave_calendar(1, calendar.calendar_id, before),
    ):
        with pytest.raises(CalendarAccessDenied):
            await action()
    assert db.rows[Calendar][calendar.calendar_id].version == before


async def test_membership_leave_then_owner_delete_cascades_simulation():
    db = Database()
    calendar = await created(db)
    member = await joined(db, calendar)
    repo = db.repository()
    await repo.leave_calendar(2, calendar.calendar_id, member.version)
    assert await repo.list_calendars(2) == ()
    assert len(await repo.list_calendars(1)) == 1
    token = await invited(db, calendar, 3)
    await repo.delete_calendar(1, calendar.calendar_id, member.version + 2)
    assert not db.rows[CalendarInvitation] and not db.rows[CalendarMembership]
    with pytest.raises(CalendarDataUnavailable):
        await repo.preview_invitation(3, token, NOW)


async def test_targeted_invitation_hash_expiry_and_single_use_confirmation():
    db = Database()
    calendar = await created(db)
    repo = db.repository()
    token = await invited(db, calendar)
    assert len(token) >= 32 and token not in str(db.rows[CalendarInvitation])
    invite = db.rows[CalendarInvitation][digest_invitation_token(token)]
    assert invite.expires_at - invite.created_at == timedelta(hours=24)
    assert await repo.list_calendars(2) == ()  # Preview never joins or saves.
    preview = await repo.preview_invitation(2, token, NOW)
    assert preview.summary.role is CalendarRole.MEMBER
    with pytest.raises(CalendarConflict):
        await repo.preview_invitation(3, token, NOW)
    for timestamp in (NOW - timedelta(seconds=1), NOW + timedelta(hours=24)):
        with pytest.raises(CalendarConflict):
            await repo.accept_invitation(2, token, preview.summary.version, timestamp)
    with pytest.raises(CalendarConflict):
        await repo.accept_invitation(2, token, preview.summary.version - 1, NOW)
    member = await repo.accept_invitation(2, token, preview.summary.version, NOW)
    with pytest.raises(CalendarConflict):
        await repo.accept_invitation(2, token, member.version, NOW)
    assert len(db.rows[CalendarMembership]) == 1


async def test_multiple_invitations_survive_accept_hide_and_role_but_not_content_change():
    db = Database()
    calendar = await created(db)
    repo = db.repository()
    first, second, third = (
        await invited(db, calendar, 2),
        await invited(db, calendar, 3),
        await invited(db, calendar, 4),
    )
    old = await repo.preview_invitation(2, first, NOW)
    accepted = await repo.accept_invitation(2, first, old.summary.version, NOW)
    hidden = await repo.set_visible(2, calendar.calendar_id, accepted.version, False)
    role = await repo.set_role(1, calendar.calendar_id, 2, hidden.version, CalendarRole.ADMIN)
    second_preview = await repo.preview_invitation(3, second, NOW)
    assert second_preview.summary.version == role.version
    accepted2 = await repo.accept_invitation(3, second, role.version, NOW)
    await repo.update_calendar(1, calendar.calendar_id, accepted2.version, profile("Changed"))
    with pytest.raises(CalendarConflict):
        await repo.preview_invitation(4, third, NOW)


async def test_revoke_owner_only_and_cas():
    db = Database()
    calendar = await created(db)
    token = await invited(db, calendar)
    repo = db.repository()
    with pytest.raises(CalendarAccessDenied):
        await repo.revoke_invitation(2, token, 2)
    with pytest.raises(CalendarConflict):
        await repo.revoke_invitation(1, token, 1)
    await repo.revoke_invitation(1, token, 2)
    with pytest.raises(CalendarConflict):
        await repo.preview_invitation(2, token, NOW)


async def test_quota_counts_hidden_owned_and_memberships_and_fail_closed_on_corrupt_oversize():
    db = Database()
    repo = db.repository()
    for _ in range(11):
        item = await repo.create_calendar(2, profile(), CalendarKind.PERSONAL)
        await repo.set_visible(2, item.calendar_id, item.version, False)
    with pytest.raises(CalendarLimitExceeded):
        await repo.create_calendar(2, profile(), CalendarKind.SCHOOL)
    calendar = await created(db)
    with pytest.raises(CalendarLimitExceeded):
        await invited(db, calendar)
    bad = copy.deepcopy(db.rows[Calendar][item.calendar_id])
    bad.id = uuid4()
    db.rows[Calendar][bad.id] = bad
    with pytest.raises(CalendarLimitExceeded):
        await repo.list_sources(2)


async def test_pending_invitation_capacity_bounded_and_expired_slots_reusable():
    db = Database()
    calendar = await created(db)
    for _ in range(20):
        await invited(db, calendar)
    version = db.rows[Calendar][calendar.calendar_id].version
    with pytest.raises(CalendarLimitExceeded):
        await invited(db, calendar)
    assert db.rows[Calendar][calendar.calendar_id].version == version
    await db.repository().create_invitation(
        1, calendar.calendar_id, 2, version, NOW + timedelta(hours=24)
    )


async def test_transaction_failure_restores_payload_and_acceptance_in_simulation():
    db = Database()
    calendar = await created(db)
    token = await invited(db, calendar)
    repo = db.repository()
    db.fail_flush = True
    with pytest.raises(RuntimeError):
        await repo.update_calendar(1, calendar.calendar_id, 2, profile("Not saved"))
    assert db.rows[Calendar][calendar.calendar_id].name == "Math"
    assert db.rows[Calendar][calendar.calendar_id].snapshot_version == 1
    with pytest.raises(RuntimeError):
        await repo.accept_invitation(2, token, 2, NOW)
    assert not db.rows[CalendarMembership]
    assert db.rows[CalendarInvitation][digest_invitation_token(token)].consumed_at is None
    db.fail_flush = False
    assert (await repo.accept_invitation(2, token, 2, NOW)).version == 3


async def test_existing_user_required_no_account_creation_or_implicit_subscription():
    db = Database()
    for action in (
        lambda: db.repository().create_calendar(500, profile(), CalendarKind.PERSONAL),
        lambda: db.repository().list_sources(500),
    ):
        with pytest.raises(CalendarDataUnavailable):
            await action()
    assert len(db.rows[User]) == 25 and not db.rows[Calendar]


@pytest.mark.parametrize("token", [None, "", "short", "x" * 97, "a" * 31 + " ", "а" * 32])
async def test_bad_tokens_rejected_before_sql(token):
    db = Database()
    with pytest.raises(ValueError):
        await db.repository().preview_invitation(2, token, NOW)
    assert db.sessions == 0


@pytest.mark.parametrize(
    "change",
    [
        {"actor": True},
        {"actor": 0},
        {"actor": 2**63},
        {"calendar_id": "fake"},
        {"expected_version": True},
        {"expected_version": 0},
        {"visible": 1},
    ],
)
async def test_bad_mutation_inputs_rejected_before_sql(change):
    db = Database()
    arguments = dict(actor=1, calendar_id=UUID(int=1), expected_version=1, visible=False)
    arguments.update(change)
    with pytest.raises(ValueError):
        await db.repository().set_visible(**arguments)
    assert db.sessions == 0


async def test_query_simulation_serializes_concurrent_cas_without_claiming_postgres():
    db = Database()
    calendar = await created(db)
    results = await asyncio.gather(
        *(db.repository().set_visible(1, calendar.calendar_id, 1, False) for _ in range(10)),
        return_exceptions=True,
    )
    assert sum(isinstance(result, CalendarConflict) for result in results) == 9
    assert db.rows[Calendar][calendar.calendar_id].version == 2


async def test_actual_postgres_calendar_concurrency_and_user_deletion_cascades():
    """Opt-in disposable trust-only fixture. Never reuse an application DB."""
    database_url = os.environ.get("CALENDAR_TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("requires isolated disposable loopback calendarcheck PostgreSQL")
    url = make_url(database_url)
    if (
        url.drivername != "postgresql+asyncpg"
        or url.host != "127.0.0.1"
        or url.database != "calendarcheck"
        or url.username != "calendarcheck"
        or url.password is not None
    ):
        pytest.fail("fixture must be loopback trust-only calendarcheck database")
    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    repo = AsyncPostgresCalendarRepository(factory)
    try:
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
        calendar = await repo.create_calendar(1, profile(), CalendarKind.SCHOOL)
        results = await asyncio.gather(
            *(repo.set_visible(1, calendar.calendar_id, 1, True) for _ in range(10)),
            return_exceptions=True,
        )
        assert sum(isinstance(result, CalendarConflict) for result in results) == 9
        token = await repo.create_invitation(1, calendar.calendar_id, 2, 2, NOW)
        results = await asyncio.gather(
            *(repo.accept_invitation(2, token, 3, NOW) for _ in range(10)), return_exceptions=True
        )
        assert sum(isinstance(result, CalendarConflict) for result in results) == 9
        async with factory() as session, session.begin():
            user = (
                await session.execute(User.__table__.select().where(User.telegram_user_id == 2))
            ).first()
            await session.execute(User.__table__.delete().where(User.id == user.id))
        assert len(await repo.list_sources(1)) == 1  # Member deletion preserves shared content.
        async with factory() as session, session.begin():
            await session.execute(User.__table__.delete().where(User.telegram_user_id == 1))
        async with factory() as session:
            assert (await session.execute(Calendar.__table__.select())).all() == []
            assert (await session.execute(CalendarInvitation.__table__.select())).all() == []
    finally:
        await engine.dispose()

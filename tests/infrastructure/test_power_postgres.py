"""Opt-in actual PostgreSQL checks, exclusively an empty disposable local powercheck DB.

No connection is attempted without POWER_TEST_DATABASE_URL. The migration fixture
requires an empty public schema; it never downgrades or resets an existing DB.
"""

from __future__ import annotations

import asyncio
import importlib
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, func, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from time_tl.domain.power_outages.models import (
    PowerDaySchedule,
    PowerInterval,
    PowerProvider,
    PowerPublicationStatus,
    PowerScheduleSnapshot,
    PowerState,
    PowerSubscription,
    ScheduleKind,
    SourceStatus,
)
from time_tl.domain.power_outages.normalization import day_bounds
from time_tl.infrastructure.db.models import (
    OutboxMessage,
    PowerNotificationEvent,
    PowerProviderState,
    PowerScheduleSnapshotRow,
    User,
    UserPowerSubscription,
)
from time_tl.infrastructure.db.power import PostgresPowerRepository, power_delivery_policy

NOW = datetime(2026, 11, 4, 10, tzinfo=UTC)


def checked_url():
    raw = os.environ.get("POWER_TEST_DATABASE_URL")
    if not raw:
        pytest.skip("POWER_TEST_DATABASE_URL unset; no DB connection attempted")
    url = make_url(raw)
    if not (
        url.drivername == "postgresql+asyncpg"
        and url.host == "127.0.0.1"
        and url.port is not None
        and 1024 < url.port < 65536
        and url.database == "powercheck"
        and url.username == "powercheck"
        and url.password is None
        and not url.query
    ):
        pytest.fail("Only passwordless disposable local powercheck database permitted")
    return raw


@pytest.fixture(scope="module")
def migrated_url():
    url = checked_url()

    async def require_empty():
        engine = create_async_engine(url)
        try:
            async with engine.connect() as connection:
                tables = await connection.run_sync(lambda conn: inspect(conn).get_table_names())
                assert not tables, "Refusing migration: disposable public schema must be empty"
        finally:
            await engine.dispose()

    asyncio.run(require_empty())
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "0010_personal_calendar_overrides")
    command.upgrade(config, "0011_power_outages")
    # Exercise both historical fresh-install metadata and the genuinely
    # additive 0010->0011 path, exclusively inside this initially empty DB.
    command.downgrade(config, "0010_personal_calendar_overrides")
    command.upgrade(config, "0011_power_outages")
    return url


@pytest_asyncio.fixture
async def db(migrated_url):
    engine = create_async_engine(migrated_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    # This is an explicitly guarded, newly migrated disposable DB. Leave all
    # unrelated tables intact; each test gets only its own user and power rows.
    async with factory() as session, session.begin():
        await session.execute(delete(PowerNotificationEvent))
        await session.execute(delete(OutboxMessage).where(OutboxMessage.aggregate_type == "power"))
        await session.execute(delete(UserPowerSubscription))
        await session.execute(delete(PowerScheduleSnapshotRow))
        for state in (await session.execute(select(PowerProviderState))).scalars():
            state.enabled = False
            state.source_status = "UNAVAILABLE"
            state.configuration_revision = 1
            state.next_attempt_at = None
            state.last_success_at = None
            state.last_attempt_at = None
            state.consecutive_failures = 0
        user = User(id=uuid4(), telegram_user_id=100000 + uuid4().int % 100000000)
        session.add(user)
    repo = PostgresPowerRepository(factory)
    await repo.set_provider_enabled(PowerProvider.DTEK, True, 1, "test", NOW)
    yield factory, repo, user.id
    async with factory() as session, session.begin():
        await session.execute(delete(User).where(User.id == user.id))
    await engine.dispose()


def schedule(offset=30, group="1.1"):
    lower, upper = day_bounds(NOW.date())
    start, end = NOW + timedelta(minutes=offset), NOW + timedelta(minutes=90)
    return PowerDaySchedule(
        PowerProvider.DTEK,
        group,
        NOW.date(),
        ScheduleKind.CONFIRMED_DAY,
        (
            PowerInterval(group, lower, start, PowerState.POWER_EXPECTED),
            PowerInterval(group, start, end, PowerState.OUTAGE_CONFIRMED),
            PowerInterval(group, end, upper, PowerState.POWER_EXPECTED),
        ),
    )


async def publish(repo, *days):
    await repo.store_snapshot(
        PowerScheduleSnapshot(PowerProvider.DTEK, NOW, None, days, SourceStatus.FRESH), NOW
    )


async def subscribe(repo, user):
    return await repo.save_subscription(
        PowerSubscription(str(user), PowerProvider.DTEK, "1.1", True)
    )


async def test_real_schema_constraints_and_six_disabled_seeds(migrated_url):
    engine = create_async_engine(migrated_url)
    try:
        async with engine.connect() as connection:
            migration = importlib.import_module("migrations.versions.0011_power_outages")
            await connection.run_sync(migration.validate_schema)
            assert (
                await connection.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar() == "0011_power_outages"
            rows = (
                await connection.execute(text("SELECT enabled, source_status FROM power_providers"))
            ).all()
            assert len(rows) == 6 and all(
                not enabled and status == "UNAVAILABLE" for enabled, status in rows
            )
    finally:
        await engine.dispose()


async def test_revision_restart_dedupe_and_omission_fence(db):
    factory, repo, user = db
    await subscribe(repo, user)
    await publish(repo, schedule(), schedule(group="1.2"))
    assert await repo.plan_notifications(NOW) == 1
    restarted = PostgresPowerRepository(factory)
    assert await restarted.plan_notifications(NOW) == 0
    await publish(restarted, schedule())
    view = await repo.read_schedule(PowerProvider.DTEK, "1.2", NOW.date(), now=NOW)
    assert view.source_status is SourceStatus.STALE
    await publish(repo, schedule(35))
    await publish(repo, schedule())
    view = await repo.read_schedule(PowerProvider.DTEK, "1.1", NOW.date(), now=NOW)
    assert view.revision == 3
    async with factory() as session:
        assert (
            await session.execute(select(func.count()).select_from(PowerNotificationEvent))
        ).scalar() == 1


async def test_subscription_cas_concurrency_and_user_cascade(db):
    factory, repo, user = db
    sub = await subscribe(repo, user)
    results = await asyncio.gather(
        repo.save_subscription(replace(sub, group_id="1.2"), expected_revision=1),
        repo.save_subscription(replace(sub, group_id="2.1"), expected_revision=1),
        return_exceptions=True,
    )
    assert sum(isinstance(value, PowerSubscription) for value in results) == 1
    assert sum(isinstance(value, ValueError) for value in results) == 1
    async with factory() as session, session.begin():
        await session.execute(delete(User).where(User.id == user))
    assert await repo.get_subscription(str(user)) is None


async def test_final_policy_observes_disable_revision_and_expiry(db):
    factory, repo, user = db
    sub = await subscribe(repo, user)
    await publish(repo, schedule())
    assert await repo.plan_notifications(NOW) == 1

    async def policy(at):
        async with factory() as session, session.begin():
            row = (
                await session.execute(
                    select(OutboxMessage).where(OutboxMessage.aggregate_type == "power")
                )
            ).scalar_one()
            return await power_delivery_policy(
                session, row, at, stale_after_seconds=1800, lock=True
            )

    assert await policy(NOW) == "AUTHORIZED"
    assert await policy(NOW + timedelta(minutes=30, microseconds=1)) == "REJECTED"
    await repo.save_subscription(replace(sub, enabled=False), expected_revision=1)
    assert await policy(NOW) == "REJECTED"


async def test_poll_slot_restart_retry_after_and_emergency_no_notification(db):
    factory, repo, user = db
    slots = await asyncio.gather(
        repo.acquire_poll_slot(PowerProvider.DTEK, NOW),
        PostgresPowerRepository(factory).acquire_poll_slot(PowerProvider.DTEK, NOW),
    )
    assert sum(value is not None for value in slots) == 1
    await repo.store_snapshot(
        PowerScheduleSnapshot(
            PowerProvider.DTEK, NOW, None, (), SourceStatus.UNAVAILABLE, "RATE_LIMITED"
        ),
        NOW,
        retry_after_seconds=3600,
    )
    assert await repo.acquire_poll_slot(PowerProvider.DTEK, NOW + timedelta(minutes=10)) is None
    await subscribe(repo, user)
    unknown = replace(schedule(), intervals=(), publication_status=PowerPublicationStatus.EMERGENCY)
    await publish(repo, unknown)
    view = await repo.read_schedule(PowerProvider.DTEK, "1.1", NOW.date(), now=NOW)
    assert view.schedule.publication_status is PowerPublicationStatus.EMERGENCY
    assert await repo.plan_notifications(NOW) == 0


async def test_concurrent_planners_and_semantic_correction_a_b_a(db):
    factory, repo, user = db
    await subscribe(repo, user)
    await publish(repo, schedule())
    counts = await asyncio.gather(
        repo.plan_notifications(NOW), PostgresPowerRepository(factory).plan_notifications(NOW)
    )
    assert sum(counts) == 1

    async def mark_latest_sent(at):
        async with factory() as session, session.begin():
            event = (
                await session.execute(
                    select(PowerNotificationEvent).where(PowerNotificationEvent.status == "PENDING")
                )
            ).scalar_one()
            event.status = "SENT"
            outbox = await session.get(OutboxMessage, event.outbox_id)
            outbox.delivered_at = at

    await mark_latest_sent(NOW)
    await publish(repo, schedule(35))
    assert await repo.plan_notifications(NOW + timedelta(seconds=1)) == 1
    assert await repo.plan_notifications(NOW + timedelta(seconds=1)) == 0
    await mark_latest_sent(NOW + timedelta(seconds=1))
    await publish(repo, schedule())
    assert await repo.plan_notifications(NOW + timedelta(seconds=2)) == 1
    async with factory() as session:
        events = (await session.execute(select(PowerNotificationEvent))).scalars().all()
        assert len({event.event_key for event in events}) == 3
        assert sum(event.transition_type == "POWER_SCHEDULE_CHANGED" for event in events) == 2


async def test_actual_policy_lock_serializes_subscription_change(db):
    factory, repo, user = db
    sub = await subscribe(repo, user)
    await publish(repo, schedule())
    assert await repo.plan_notifications(NOW) == 1
    change = None
    try:
        async with factory() as session, session.begin():
            row = (
                await session.execute(
                    select(OutboxMessage).where(OutboxMessage.aggregate_type == "power")
                )
            ).scalar_one()
            fences = []
            assert (
                await power_delivery_policy(
                    session, row, NOW, stale_after_seconds=1800, lock=True, fences=fences
                )
                == "AUTHORIZED"
            )
            assert len(fences) == 1 and fences[0].valid_at(NOW)
            change = asyncio.create_task(
                repo.save_subscription(replace(sub, group_id="2.1"), expected_revision=1)
            )
            done, _ = await asyncio.wait({change}, timeout=0.1)
            assert not done, "User/subscription policy locks must remain held until send ends"
        await asyncio.wait_for(change, timeout=5)
        async with factory() as session, session.begin():
            row = await session.get(OutboxMessage, row.id)
            assert (
                await power_delivery_policy(session, row, NOW, stale_after_seconds=1800, lock=True)
                == "REJECTED"
            )
    finally:
        if change is not None and not change.done():
            change.cancel()
            await asyncio.gather(change, return_exceptions=True)


@pytest.mark.parametrize("loss", ["UNKNOWN", "GAP", "OMITTED", "STALE"])
async def test_uncertain_coverage_never_creates_false_empty_correction(db, loss):
    factory, repo, user = db
    await subscribe(repo, user)
    await publish(repo, schedule())
    assert await repo.plan_notifications(NOW) == 1
    async with factory() as session, session.begin():
        event = (await session.execute(select(PowerNotificationEvent))).scalar_one()
        event.status = "SENT"
        outbox = await session.get(OutboxMessage, event.outbox_id)
        outbox.delivered_at = NOW
    old = schedule()
    neighbor = replace(
        old,
        local_date=old.local_date + timedelta(days=1),
        intervals=tuple(
            replace(
                item,
                starts_at=item.starts_at + timedelta(days=1),
                ends_at=item.ends_at + timedelta(days=1),
            )
            for item in old.intervals
        ),
    )
    if loss == "UNKNOWN":
        changed = replace(
            old, intervals=tuple(replace(item, state=PowerState.UNKNOWN) for item in old.intervals)
        )
        await publish(repo, changed, neighbor)
    elif loss == "GAP":
        await publish(repo, replace(old, intervals=(old.intervals[0], old.intervals[2])), neighbor)
    elif loss == "OMITTED":
        await publish(repo, neighbor)
    else:
        await publish(repo, old, neighbor)
        async with factory() as session, session.begin():
            row = (
                await session.execute(
                    select(PowerScheduleSnapshotRow).where(
                        PowerScheduleSnapshotRow.valid_for_date == NOW.date(),
                        PowerScheduleSnapshotRow.is_current.is_(True),
                    )
                )
            ).scalar_one()
            row.fetched_at = NOW - timedelta(hours=1)
    assert await repo.plan_notifications(NOW + timedelta(seconds=1)) == 0
    async with factory() as session:
        assert (
            await session.execute(
                select(func.count())
                .select_from(PowerNotificationEvent)
                .where(PowerNotificationEvent.transition_type == "POWER_SCHEDULE_CHANGED")
            )
        ).scalar() == 0

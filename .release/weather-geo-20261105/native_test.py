"""Real, isolated Linux/PostgreSQL weather proof. Never contacts real providers.

Run only in an independently verified network-none lab sharing its disposable
weathercheck PostgreSQL network namespace. The script owns one UUID schema;
it never touches public schema, server credentials, Docker, or production.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib
import io
import json
import logging
import sys
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from lab_helpers import (
    HEADS,
    URL,
    LabRejected,
    failure_diagnostic,
    owned_schema,
    receipt,
    require,
    validate_lab_environment,
    verified_inventory,
    weather_schema_diagnostic,
)

PHASE = "admission"
PHASES: list[str] = []
SOURCE_SHA: str | None = None
DEBUG_FAILURE = False
SCHEMA_DIAGNOSTIC = None
BASE = datetime(2026, 11, 5, 5, tzinfo=UTC)
ACTOR = 990001
USER = UUID(int=1001)
POINT = UUID(int=2001)
VERSION = "native-synthetic-catalogue"


def phase(name: str) -> None:
    global PHASE
    PHASE = name


def passed() -> None:
    PHASES.append(PHASE)


def apply_migration(connection, root: Path, target: str) -> None:
    """Run the actual Alembic revision bodies on the caller's scoped connection."""
    from alembic.config import Config
    from alembic.runtime.environment import EnvironmentContext
    from alembic.script import ScriptDirectory

    global SCHEMA_DIAGNOSTIC

    config = Config()
    config.set_main_option("script_location", str(root / "migrations"))
    script = ScriptDirectory.from_config(config)
    with EnvironmentContext(
        config, script, fn=lambda revision, _: script._upgrade_revs(target, revision)
    ) as env:
        env.configure(connection=connection)
        with env.begin_transaction():
            try:
                env.run_migrations()
            except BaseException:
                if DEBUG_FAILURE and target == HEADS[-1]:
                    try:
                        SCHEMA_DIAGNOSTIC = weather_schema_diagnostic(connection)
                    except BaseException:
                        SCHEMA_DIAGNOSTIC = None
                raise


def baseline_metadata():
    """Avoid historical0001's live-ORM shortcut accidentally precreating head12.

    This is an explicit synthetic baseline, not a restored production backup.
    No migration body is edited; only its initial metadata input is bounded to
    the feature tables present at head9 before the genuine additive upgrades.
    """
    from sqlalchemy import MetaData

    from time_tl.infrastructure.db import models  # noqa: F401
    from time_tl.infrastructure.db.base import Base

    power = importlib.import_module("migrations.versions.0011_power_outages")
    weather = importlib.import_module("migrations.versions.0012_weather_coverage")
    override = importlib.import_module("migrations.versions.0010_personal_calendar_overrides")
    excluded = {table.name for table in (*power._schema(), *weather._schema())} | {
        override._schema().name
    }
    original = Base.metadata
    legacy = MetaData(naming_convention=original.naming_convention)
    for name, table in original.tables.items():
        if name not in excluded:
            table.to_metadata(legacy)
    return original, legacy, excluded


async def legacy_seed(factory) -> dict[str, int]:
    from sqlalchemy import func, select

    from time_tl.infrastructure.db.models import (
        Calendar,
        HomeworkAssignment,
        ScheduleProfile,
        ScheduleSubject,
        User,
    )

    profile, subject = uuid4(), uuid4()
    async with factory() as session, session.begin():
        session.add(
            User(
                id=USER,
                telegram_user_id=ACTOR,
                alerts_enabled=False,
                confirmed_region_id="native-h1",
                region_name="Synthetic community",
            )
        )
        await session.flush()
        session.add(
            ScheduleProfile(
                id=profile,
                user_id=USER,
                public_id="native-profile",
                name="Synthetic profile",
                is_active=True,
            )
        )
        await session.flush()
        session.add(
            ScheduleSubject(
                id=subject, profile_id=profile, normalized_name="synthetic", name="Synthetic"
            )
        )
        session.add(
            Calendar(
                id=uuid4(),
                owner_user_id=USER,
                name="Synthetic calendar",
                kind="PERSONAL",
                snapshot_payload={},
            )
        )
        await session.flush()
        session.add(
            HomeworkAssignment(
                owner_user_id=USER,
                profile_id=profile,
                subject_id=subject,
                text="Synthetic homework",
                due_at=BASE + timedelta(days=1),
            )
        )
    async with factory() as session:
        return {
            model.__tablename__: await session.scalar(select(func.count()).select_from(model))
            for model in (User, ScheduleProfile, ScheduleSubject, HomeworkAssignment, Calendar)
        }


async def migrate(factory, engine, root: Path) -> None:
    from unittest.mock import patch

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import func, inspect, select, text

    from time_tl.infrastructure.db.base import Base
    from time_tl.infrastructure.db.models import (
        Calendar,
        HomeworkAssignment,
        ScheduleProfile,
        ScheduleSubject,
        User,
    )

    phase("migration_chain")
    original, legacy, excluded = baseline_metadata()
    with patch.object(Base, "metadata", legacy):
        async with engine.begin() as connection:
            await connection.run_sync(lambda bind: apply_migration(bind, root, HEADS[0]))
            names = await connection.run_sync(lambda bind: set(inspect(bind).get_table_names()))
            require(not (names & excluded))
    require(Base.metadata is original)
    before = await legacy_seed(factory)
    for target in HEADS[1:]:
        async with engine.begin() as connection:
            await connection.run_sync(
                lambda bind, selected=target: apply_migration(bind, root, selected)
            )
            require(
                (
                    await connection.execute(text("SELECT version_num FROM alembic_version"))
                ).scalar_one()
                == target
            )
    async with factory() as session:
        after = {
            model.__tablename__: await session.scalar(select(func.count()).select_from(model))
            for model in (User, ScheduleProfile, ScheduleSubject, HomeworkAssignment, Calendar)
        }
    require(before == after and all(value == 1 for value in after.values()))
    passed()
    phase("frozen_schema_and_drift")
    power = importlib.import_module("migrations.versions.0011_power_outages")
    weather = importlib.import_module("migrations.versions.0012_weather_coverage")
    override = importlib.import_module("migrations.versions.0010_personal_calendar_overrides")
    async with engine.begin() as connection:

        def validate(bind):
            with Operations.context(MigrationContext.configure(bind)):
                override._validate(override._schema())
                power.validate_schema(bind)
                weather.validate_schema(bind)
                weather.upgrade()  # Exact repeat adopts, never repairs.
                nested = bind.begin_nested()
                try:
                    bind.execute(
                        text(
                            "ALTER TABLE weather_locations DROP CONSTRAINT "
                            "ck_weather_locations_ukraine_latitude"
                        )
                    )
                    rejected = False
                    try:
                        weather.validate_schema(bind)
                    except RuntimeError:
                        rejected = True
                    require(rejected)
                finally:
                    nested.rollback()
                weather.validate_schema(bind)

        await connection.run_sync(validate)
    passed()


async def geography(factory) -> None:
    from time_tl.infrastructure.db.models import (
        ActiveTerritoryCatalog,
        Territory,
        TerritoryCatalogVersion,
        WeatherLocation,
    )

    async with factory() as session, session.begin():
        session.add(
            TerritoryCatalogVersion(
                provider="alerts.in.ua",
                catalog_version=VERSION,
                status="ACTIVE",
                source_revision=VERSION,
                source_sha256="a" * 64,
                record_count=4,
                selectable_count=4,
                hidden_nonselectable_count=0,
                unresolved_total=0,
            )
        )
        await session.flush()
        for index, (identifier, parent, kind) in enumerate(
            (
                ("native-o", None, "OBLAST"),
                ("native-r", "native-o", "RAION"),
                ("native-h1", "native-r", "HROMADA"),
                ("native-h2", "native-r", "HROMADA"),
            ),
            1,
        ):
            session.add(
                Territory(
                    provider="alerts.in.ua",
                    catalog_version=VERSION,
                    region_id=identifier,
                    parent_region_id=parent,
                    kind=kind,
                    name="Synthetic community",
                    external_code="UA" + str(index).zfill(17),
                    sort_key=str(index),
                    is_selectable=True,
                )
            )
        await session.flush()
        session.add(ActiveTerritoryCatalog(provider="alerts.in.ua", catalog_version=VERSION))
        session.add(
            WeatherLocation(
                id=POINT,
                katottg_code="UA00000000000000003",
                region_id="native-h1",
                locality_name="Synthetic community",
                latitude=48.5,
                longitude=35.0,
                coverage_type="BASE",
                enabled=True,
                provenance_url="https://example.invalid/native-approved-points",
                provenance_sha256="a" * 64,
                provenance_version="synthetic-lab",
                approved_at=BASE - timedelta(days=1),
            )
        )


class Clock:
    now = BASE

    def __call__(self):
        return self.now


async def preferences_coverage(factory, clock):
    from sqlalchemy import delete, update

    from time_tl.infrastructure.db.geographic_coverage import PostgresActiveGeographicCoverage
    from time_tl.infrastructure.db.models import User, UserWeatherPreference
    from time_tl.infrastructure.db.weather import PostgresWeatherPreferences
    from time_tl.infrastructure.db.weather_configuration import (
        PostgresWeatherConfiguration,
        WeatherConfiguration,
    )

    phase("preferences_and_coverage")
    config = PostgresWeatherConfiguration(factory)
    candidates = await asyncio.gather(
        *(
            config.save(
                WeatherConfiguration(enabled=True, terms_approved=True),
                expected_revision=0,
                actor="native-lab",
                now=clock(),
            )
            for _ in range(2)
        ),
        return_exceptions=True,
    )
    require(sum(not isinstance(value, BaseException) for value in candidates) == 1)
    require(sum(isinstance(value, ValueError) for value in candidates) == 1)
    prefs = PostgresWeatherPreferences(factory)
    require((await prefs.get(ACTOR)).revision == 0)
    require(not (await prefs.request_forecast(ACTOR)).weather_enabled)
    candidates = await asyncio.gather(
        *(
            prefs.update(ACTOR, expected_revision=1, notification_lead_minutes=lead)
            for lead in (30, 90)
        ),
        return_exceptions=True,
    )
    require(sum(not isinstance(value, BaseException) for value in candidates) == 1)
    require(sum(isinstance(value, ValueError) for value in candidates) == 1)
    current = await prefs.get(ACTOR)
    await prefs.update(
        ACTOR,
        expected_revision=current.revision,
        weather_enabled=True,
        notification_lead_minutes=120,
    )
    ids = [uuid4() for _ in range(99)]
    async with factory() as session, session.begin():
        for index, identifier in enumerate(ids, 1):
            session.add(
                User(
                    id=identifier,
                    telegram_user_id=ACTOR + index,
                    confirmed_region_id="native-h1",
                    region_name="Synthetic community",
                    alerts_enabled=False,
                )
            )
        await session.flush()
        for identifier in ids:
            session.add(UserWeatherPreference(user_id=identifier, forecast_requested=True))
    coverage = PostgresActiveGeographicCoverage(factory)
    snapshot = await coverage.snapshot(clock())
    require(len(snapshot.weather_points) == 1 and snapshot.weather_points[0].user_count == 100)
    async with factory() as session, session.begin():
        await session.execute(
            update(UserWeatherPreference)
            .where(UserWeatherPreference.user_id.in_(ids))
            .values(forecast_requested=False)
        )
        await session.execute(
            update(User).where(User.id == USER).values(confirmed_region_id="native-h2")
        )
    require(not (await PostgresActiveGeographicCoverage(factory).snapshot(clock())).weather_points)
    async with factory() as session, session.begin():
        await session.execute(
            update(User).where(User.id == USER).values(confirmed_region_id="native-h1")
        )
        await session.execute(delete(User).where(User.id.in_(ids)))
    require(len((await coverage.snapshot(clock())).weather_points) == 1)
    current = await prefs.get(ACTOR)
    await prefs.disable_weather(ACTOR, expected_revision=current.revision)
    require(not (await coverage.snapshot(clock())).weather_points)
    await prefs.request_forecast(ACTOR)
    current = await prefs.get(ACTOR)
    await prefs.toggle_notifications(ACTOR, True, expected_revision=current.revision)
    passed()
    return prefs, config


def forecast(clock, *, wind=20.0, today=10.0, tomorrow=20.0):
    from time_tl.domain.weather import ForecastHour, ForecastSnapshot, ForecastStatus

    return ForecastSnapshot(
        "MET Norway",
        str(POINT),
        clock(),
        clock(),
        clock() + timedelta(hours=8),
        (
            ForecastHour(
                BASE + timedelta(hours=2),
                temperature_c=today,
                precipitation_mm=2.0,
                precipitation_probability=80.0,
                wind_gust_ms=wind,
            ),
            ForecastHour(BASE + timedelta(hours=26), temperature_c=tomorrow),
        ),
        ForecastStatus.FRESH,
        "https://api.met.no/weatherapi/locationforecast/2.0/compact",
    )


async def store_forecast(factory, clock, config, **kwargs):
    from time_tl.infrastructure.db.weather import PostgresWeatherStorage

    repo = PostgresWeatherStorage(
        factory, clock=clock, expected_configuration_revision=(await config.load()).revision
    )
    await repo.begin_run(str(uuid4()), clock(), 1)
    await repo.previous(str(POINT))
    await repo.save(forecast(clock, **kwargs))
    return repo


async def source_permissions(factory, clock, prefs, config):
    from time_tl.domain.weather import WeatherPoint
    from time_tl.infrastructure.db.weather import PostgresWeatherStorage
    from time_tl.infrastructure.weather.met_norway import WeatherProviderError

    phase("source_permissions_and_restart")
    point = WeatherPoint(str(POINT), 48.5, 35.0)
    repos = [
        PostgresWeatherStorage(factory, clock=clock, expected_configuration_revision=1)
        for _ in range(2)
    ]
    results = await asyncio.gather(
        *(repo.claim_source_request(point, clock()) for repo in repos), return_exceptions=True
    )
    require(sum(value is None for value in results) == 1)
    require(
        sum(
            isinstance(value, WeatherProviderError) and value.code == "REQUEST_DEFERRED"
            for value in results
        )
        == 1
    )
    winner = repos[next(index for index, value in enumerate(results) if value is None)]
    current = await prefs.get(ACTOR)
    await prefs.disable_weather(ACTOR, expected_revision=current.revision)
    try:
        await winner.authorize_source_request(point, clock())
    except WeatherProviderError as error:
        require(error.code == "DEMAND_REMOVED")
    else:
        raise LabRejected()
    await prefs.request_forecast(ACTOR)
    current = await prefs.get(ACTOR)
    await prefs.toggle_notifications(ACTOR, True, expected_revision=current.revision)
    clock.now += timedelta(seconds=2)
    await winner.claim_source_request(point, clock())
    old = await config.load()
    await config.save(
        old.model_copy(update={"enabled": False}),
        expected_revision=old.revision,
        actor="native-lab",
        now=clock(),
    )
    try:
        await winner.authorize_source_request(point, clock())
    except WeatherProviderError as error:
        require(error.code in {"WEATHER_DISABLED", "WEATHER_CONFIGURATION_CHANGED"})
    else:
        raise LabRejected()
    old = await config.load()
    current = await config.save(
        old.model_copy(update={"enabled": True}),
        expected_revision=old.revision,
        actor="native-lab",
        now=clock(),
    )
    clock.now += timedelta(seconds=2)
    repo = PostgresWeatherStorage(
        factory, clock=clock, expected_configuration_revision=current.revision
    )
    await repo.begin_run(str(uuid4()), clock(), 1)
    await repo.claim_source_request(point, clock())
    await repo.authorize_source_request(point, clock())  # Fake HTTP would begin only here.
    await repo.record_source_response(point, clock() + timedelta(minutes=5), clock())
    await repo.save(forecast(clock))
    restarted = PostgresWeatherStorage(factory, clock=clock)
    require(await restarted.previous(str(POINT)) == forecast(clock))
    try:
        await restarted.claim_source_request(point, clock() + timedelta(seconds=2))
    except WeatherProviderError as error:
        require(
            error.code == "REQUEST_DEFERRED" and error.retry_at == clock() + timedelta(minutes=5)
        )
    else:
        raise LabRejected()
    retry = clock() + timedelta(hours=1)
    await repo.record_source_retry("MET Norway", retry)
    require(await restarted.source_retry_at("MET Norway") == retry)
    await restarted.record_source_retry("MET Norway", clock() + timedelta(minutes=1))
    require(await PostgresWeatherStorage(factory).source_retry_at("MET Norway") == retry)
    passed()


async def lease_check(engine):
    from time_tl.runtime.weather_worker import (
        WeatherLeaseLost,
        WeatherPostgresLease,
        _collect_guarded,
    )

    phase("singleton_lease_loss")
    first, second = WeatherPostgresLease(engine), WeatherPostgresLease(engine)
    require(await first.acquire() and not await second.acquire() and await first.renew())
    await first.release()
    require(await second.acquire())
    started, cancelled = asyncio.Event(), asyncio.Event()

    class Collector:
        async def collect(self):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    task = asyncio.create_task(_collect_guarded(Collector(), second, asyncio.Event(), 0.01))
    await asyncio.wait_for(started.wait(), 5)
    require(second.connection is not None)
    await second.connection.invalidate()
    try:
        await asyncio.wait_for(task, 5)
    except WeatherLeaseLost:
        pass
    else:
        raise LabRejected()
    require(cancelled.is_set() and not await second.renew())
    with contextlib.suppress(Exception):
        await second.release()
    third = WeatherPostgresLease(engine)
    require(await third.acquire())
    await third.release()
    passed()


class FakeTransport:
    def __init__(self, callback=None):
        self.messages = []
        self.callback = callback

    async def send(self, message):
        self.messages.append(message)
        if self.callback is not None:
            await self.callback()


async def no_sleep(_seconds):
    return None


async def new_user(factory, *, daily=False):
    from time_tl.infrastructure.db.models import User, UserWeatherPreference

    identifier = uuid4()
    actor = 1_000_000 + identifier.int % 1_000_000_000
    async with factory() as session, session.begin():
        session.add(
            User(
                id=identifier,
                telegram_user_id=actor,
                confirmed_region_id="native-h1",
                region_name="Synthetic community",
                alerts_enabled=False,
            )
        )
        await session.flush()
        session.add(
            UserWeatherPreference(
                user_id=identifier,
                forecast_requested=True,
                weather_enabled=True,
                daily_forecast_enabled=daily,
                daily_forecast_time=time(7),
                rain_alert_enabled=not daily,
                wind_alert_enabled=not daily,
                snow_alert_enabled=not daily,
                temperature_alert_enabled=not daily,
                frost_alert_enabled=not daily,
            )
        )
    return identifier, actor


async def planner_sender(factory, clock, config):
    from sqlalchemy import select, update

    from time_tl.application.sender_worker import SenderWorker
    from time_tl.infrastructure.db.models import OutboxMessage, WeatherChangeEvent
    from time_tl.infrastructure.db.weather_notifications import (
        PostgresWeatherNotificationPlanner,
        due_weather_targets,
    )
    from time_tl.infrastructure.db.workers import PostgresSenderRepository

    phase("due_sql_planner_dedupe_and_fake_sender")
    planner = PostgresWeatherNotificationPlanner(factory, enabled=True)
    async with factory() as session:
        require(list((await session.scalars(due_weather_targets(clock()))).all()) == [USER])
        require(len((await session.scalars(select(WeatherChangeEvent))).all()) == 2)
    results = await asyncio.gather(
        planner.plan_user(USER, clock()),
        PostgresWeatherNotificationPlanner(factory, enabled=True).plan_user(USER, clock()),
    )
    require(sorted(results) == [0, 1])
    async with factory() as session:
        rows = list(
            (
                await session.scalars(
                    select(OutboxMessage).where(OutboxMessage.aggregate_type == "weather")
                )
            ).all()
        )
        require(len(rows) == 1 and len(rows[0].payload["event_keys"]) == 2)
    transport = FakeTransport()
    repository = PostgresSenderRepository(
        factory, stale_after_seconds=1800, clock=clock, weather_notifications_enabled=True
    )
    worker = SenderWorker(repository=repository, transport=transport, clock=clock)
    require(await worker.run_once(clock(), sleep=no_sleep) == 1 and len(transport.messages) == 1)
    require(await planner.plan_user(USER, clock()) == 0)
    require(await worker.run_once(clock(), sleep=no_sleep) == 0)
    passed()
    phase("immutable_recovery_and_ambiguous_no_replay")
    user, _ = await new_user(factory)
    require(await planner.plan_user(user, clock()) == 1)
    async with factory() as session, session.begin():
        old = await session.scalar(
            select(OutboxMessage).where(OutboxMessage.aggregate_id == str(user))
        )
        original = json.dumps(old.payload, sort_keys=True)
        original_key = old.event_key
        old.dead_at = clock()
        old.last_error_code = "delivery_policy_changed"
    require(await planner.plan_user(user, clock()) == 1)
    async with factory() as session, session.begin():
        rows = list(
            (
                await session.scalars(
                    select(OutboxMessage).where(OutboxMessage.aggregate_id == str(user))
                )
            ).all()
        )
        require(len(rows) == 2)
        old = next(row for row in rows if row.event_key == original_key)
        current = next(row for row in rows if row.event_key != original_key)
        require(json.dumps(old.payload, sort_keys=True) == original)
        current.dead_at = clock()
        current.claimed_at = None
        current.dispatch_started_at = clock()
        current.indeterminate_at = clock()
    require(await planner.plan_user(user, clock()) == 0)
    # Even a new preference epoch cannot replay an indeterminate event identity.
    from time_tl.infrastructure.db.weather import PostgresWeatherPreferences

    async with factory() as session:
        from time_tl.infrastructure.db.models import User

        actor = await session.scalar(select(User.telegram_user_id).where(User.id == user))
    prefs = PostgresWeatherPreferences(factory)
    value = await prefs.get(actor)
    await prefs.update(actor, expected_revision=value.revision, frost_alert_enabled=False)
    require(
        await PostgresWeatherNotificationPlanner(factory, enabled=True).plan_user(user, clock())
        == 0
    )
    passed()
    # No pending weather from this phase may contaminate subsequent claim tests.
    async with factory() as session, session.begin():
        await session.execute(
            update(OutboxMessage)
            .where(OutboxMessage.delivered_at.is_(None), OutboxMessage.dead_at.is_(None))
            .values(dead_at=clock(), claimed_at=None)
        )


async def final_revalidation(factory, clock, config):
    from sqlalchemy import update

    from time_tl.infrastructure.db.models import OutboxMessage, User
    from time_tl.infrastructure.db.weather import PostgresWeatherPreferences
    from time_tl.infrastructure.db.weather_notifications import PostgresWeatherNotificationPlanner
    from time_tl.infrastructure.db.workers import PostgresSenderRepository

    phase("final_claim_revalidation_zero_sends")
    calls = 0

    async def forbidden_delivery():
        nonlocal calls
        calls += 1
        raise LabRejected()

    prefs = PostgresWeatherPreferences(factory)
    for kind in ("preferences", "territory", "configuration", "event"):
        user, actor = await new_user(factory)
        require(
            await PostgresWeatherNotificationPlanner(factory, enabled=True).plan_user(user, clock())
            == 1
        )
        repo = PostgresSenderRepository(
            factory, stale_after_seconds=1800, clock=clock, weather_notifications_enabled=True
        )
        claims = await repo.claim_batch_skip_locked(clock(), limit=20)
        require(len(claims) == 1 and claims[0].recipient is not None)
        if kind == "preferences":
            value = await prefs.get(actor)
            await prefs.toggle_notifications(actor, False, expected_revision=value.revision)
        elif kind == "territory":
            async with factory() as session, session.begin():
                await session.execute(
                    update(User).where(User.id == user).values(confirmed_region_id="native-h2")
                )
        elif kind == "configuration":
            value = await config.load()
            await config.save(
                value.model_copy(update={"enabled": False}),
                expected_revision=value.revision,
                actor="native-lab",
                now=clock(),
            )
        else:
            clock.now += timedelta(seconds=1)
            await store_forecast(factory, clock, config, wind=25.0)
        require(
            not await repo.deliver_if_authorized(
                claims[0].message_id,
                clock(),
                forbidden_delivery,
                expected_recipient=claims[0].recipient,
            )
        )
        if kind == "configuration":
            value = await config.load()
            await config.save(
                value.model_copy(update={"enabled": True}),
                expected_revision=value.revision,
                actor="native-lab",
                now=clock(),
            )
        async with factory() as session:
            row = await session.get(OutboxMessage, UUID(claims[0].message_id))
            require(row.dead_at is not None and row.delivered_at is None)
    require(calls == 0)
    passed()


async def daily_semantics(factory, clock, config):
    from time_tl.infrastructure.db.models import OutboxMessage
    from time_tl.infrastructure.db.weather_notifications import PostgresWeatherNotificationPlanner
    from time_tl.infrastructure.db.workers import PostgresSenderRepository

    phase("daily_selected_date_semantic_fence")
    user, _ = await new_user(factory, daily=True)
    planner = PostgresWeatherNotificationPlanner(factory, enabled=True)
    require(await planner.plan_user(user, clock()) == 1)
    repo = PostgresSenderRepository(
        factory, stale_after_seconds=1800, clock=clock, weather_notifications_enabled=True
    )
    claims = await repo.claim_batch_skip_locked(clock(), limit=20)
    require(len(claims) == 1 and claims[0].event_type == "WEATHER_DAILY")
    clock.now += timedelta(seconds=1)
    await store_forecast(factory, clock, config, wind=25.0, today=11.0)
    calls = 0

    async def delivery():
        nonlocal calls
        calls += 1

    require(
        await repo.deliver_if_authorized(
            claims[0].message_id, clock(), delivery, expected_recipient=claims[0].recipient
        )
    )
    require(calls == 1)
    user, _ = await new_user(factory, daily=True)
    require(await planner.plan_user(user, clock()) == 1)
    claims = await repo.claim_batch_skip_locked(clock(), limit=20)
    require(len(claims) == 1)
    clock.now += timedelta(seconds=1)
    await store_forecast(factory, clock, config, wind=25.0, today=11.0, tomorrow=30.0)
    require(
        not await repo.deliver_if_authorized(
            claims[0].message_id, clock(), delivery, expected_recipient=claims[0].recipient
        )
    )
    require(calls == 1)
    async with factory() as session:
        row = await session.get(OutboxMessage, UUID(claims[0].message_id))
        require(row.dead_at is not None and row.delivered_at is None)
    passed()


async def priority_and_skip_locked(factory, clock):
    from sqlalchemy import delete, select

    from time_tl.application.sender_worker import SenderWorker
    from time_tl.infrastructure.db.models import OutboxMessage
    from time_tl.infrastructure.db.outbox import pending_outbox_query
    from time_tl.infrastructure.db.weather_notifications import PostgresWeatherNotificationPlanner
    from time_tl.infrastructure.db.workers import PostgresSenderRepository

    phase("real_priority_sql_skip_locked")
    ids = []
    kinds = ("WEATHER_DAILY", "POWER_OUTAGE_UPCOMING", "LESSON_REMINDER", "ALERT_STARTED")
    async with factory() as session, session.begin():
        for kind in kinds:
            identifier = uuid4()
            ids.append(identifier)
            session.add(
                OutboxMessage(
                    id=identifier,
                    event_key="native:" + str(identifier),
                    aggregate_type="native_priority",
                    aggregate_id="native",
                    event_type=kind,
                    payload={},
                    available_at=clock(),
                )
            )
    async with factory() as first, first.begin(), factory() as second, second.begin():
        rows = (await first.scalars(pending_outbox_query(clock(), limit=20))).all()
        selected = [row for row in rows if row.id in ids]
        require([row.event_type for row in selected] == list(reversed(kinds)))
        require(
            not any(
                row.id in ids
                for row in (await second.scalars(pending_outbox_query(clock(), limit=20))).all()
            )
        )
    async with factory() as session, session.begin():
        await session.execute(delete(OutboxMessage).where(OutboxMessage.id.in_(ids)))
    passed()
    phase("new_alert_preempts_weather_tail")
    planner = PostgresWeatherNotificationPlanner(factory, enabled=True)
    for _ in range(2):
        user, _ = await new_user(factory)
        require(await planner.plan_user(user, clock()) == 1)
    alert_id = uuid4()

    async def alert_arrives():
        async with factory() as session, session.begin():
            session.add(
                OutboxMessage(
                    id=alert_id,
                    event_key="native:" + str(alert_id),
                    aggregate_type="native_priority",
                    aggregate_id="native",
                    event_type="ALERT_STARTED",
                    payload={},
                    available_at=clock(),
                )
            )

    transport = FakeTransport(alert_arrives)
    repo = PostgresSenderRepository(
        factory, stale_after_seconds=1800, clock=clock, weather_notifications_enabled=True
    )
    worker = SenderWorker(repository=repo, transport=transport, clock=clock)
    require(await worker.run_once(clock(), sleep=no_sleep) == 1 and len(transport.messages) == 1)
    async with factory() as session, session.begin():
        require(await session.get(OutboxMessage, alert_id) is not None)
        pending = (
            await session.scalars(
                select(OutboxMessage).where(
                    OutboxMessage.aggregate_type == "weather",
                    OutboxMessage.dead_at.is_(None),
                    OutboxMessage.delivered_at.is_(None),
                )
            )
        ).all()
        require(len(pending) == 1 and pending[0].claimed_at is None and pending[0].attempts == 0)
        await session.delete(await session.get(OutboxMessage, alert_id))
    passed()


async def expire_claim(factory, clock):
    from sqlalchemy import update

    from time_tl.infrastructure.db.models import OutboxMessage
    from time_tl.infrastructure.db.weather_notifications import PostgresWeatherNotificationPlanner
    from time_tl.infrastructure.db.workers import PostgresSenderRepository

    phase("final_expiry_zero_send")
    # Retire the intentionally released weather tail before testing a new recipient.
    async with factory() as session, session.begin():
        await session.execute(
            update(OutboxMessage)
            .where(OutboxMessage.delivered_at.is_(None), OutboxMessage.dead_at.is_(None))
            .values(dead_at=clock(), claimed_at=None, last_error_code="native_fixture_retired")
        )
    user, _ = await new_user(factory)
    require(
        await PostgresWeatherNotificationPlanner(factory, enabled=True).plan_user(user, clock())
        == 1
    )
    repo = PostgresSenderRepository(
        factory, stale_after_seconds=1800, clock=clock, weather_notifications_enabled=True
    )
    claims = await repo.claim_batch_skip_locked(clock(), limit=20)
    require(len(claims) == 1)
    clock.now = datetime.fromisoformat(claims[0].payload["expires_at"])
    calls = 0

    async def delivery():
        nonlocal calls
        calls += 1

    require(
        not await repo.deliver_if_authorized(
            claims[0].message_id, clock(), delivery, expected_recipient=claims[0].recipient
        )
    )
    require(calls == 0)
    passed()


async def run(root: Path) -> bool:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    schema = owned_schema("weather_native_" + uuid4().hex)
    token = uuid4().hex
    owner = create_async_engine(URL)
    scoped = None
    created = False
    cleanup = False
    success = False
    clock = Clock()
    try:
        phase("owned_schema")
        async with owner.begin() as connection:
            require(
                (await connection.execute(text("SELECT current_database()"))).scalar_one()
                == "weathercheck"
            )
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            created = True
            await connection.execute(
                text(f'CREATE TABLE "{schema}".native_lab_ownership(token text primary key)')
            )
            await connection.execute(
                text(f'INSERT INTO "{schema}".native_lab_ownership VALUES (:token)'),
                {"token": token},
            )
        passed()
        scoped = create_async_engine(
            URL,
            connect_args={
                "server_settings": {
                    "search_path": schema,
                    "statement_timeout": "15000",
                    "lock_timeout": "5000",
                }
            },
        )
        factory = async_sessionmaker(scoped, expire_on_commit=False)
        await migrate(factory, scoped, root)
        await geography(factory)
        prefs, config = await preferences_coverage(factory, clock)
        await source_permissions(factory, clock, prefs, config)
        await lease_check(scoped)
        await planner_sender(factory, clock, config)
        await final_revalidation(factory, clock, config)
        await daily_semantics(factory, clock, config)
        await priority_and_skip_locked(factory, clock)
        await expire_claim(factory, clock)
        success = True
    finally:
        if scoped is not None:
            await scoped.dispose()
        if created:
            try:
                async with owner.begin() as connection:
                    require(
                        (
                            await connection.execute(
                                text(f'SELECT token FROM "{schema}".native_lab_ownership')
                            )
                        ).scalar_one()
                        == token
                    )
                    await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
                cleanup = True
            except Exception:
                cleanup = False
        else:
            cleanup = True
        await owner.dispose()
        run.cleanup = cleanup
    return success


def main() -> int:
    global SOURCE_SHA, DEBUG_FAILURE, SCHEMA_DIAGNOSTIC
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--debug-failure", action="store_true")
    args = parser.parse_args()
    DEBUG_FAILURE = args.debug_failure
    SCHEMA_DIAGNOSTIC = None
    ok = False
    debug = None
    run.cleanup = False
    previous_logging = logging.root.manager.disable
    previous_path = list(sys.path)
    logging.disable(logging.CRITICAL)
    # Driver/ORM/assertion text is never returned, even for failed native phases.
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        try:
            validate_lab_environment()
            SOURCE_SHA = verified_inventory(args.manifest, args.project_root)
            sys.path.append(str(args.project_root.resolve()))  # migrations only; never project/src
            for name, folder in (
                ("migrations", "migrations"),
                ("migrations.versions", "migrations/versions"),
            ):
                spec = importlib.util.find_spec(name)
                require(spec is not None and spec.origin is None)
                require(
                    {Path(value).resolve() for value in spec.submodule_search_locations}
                    == {(args.project_root / folder).resolve()}
                )
            passed()
            import httpx

            def forbidden(*_args, **_kwargs):
                raise LabRejected()

            from unittest.mock import patch

            with patch.object(httpx, "AsyncClient", forbidden):
                ok = asyncio.run(asyncio.wait_for(run(args.project_root.resolve()), timeout=240))
        except BaseException as error:
            ok = False
            if args.debug_failure:
                debug = failure_diagnostic(error)
                if SCHEMA_DIAGNOSTIC is not None:
                    debug["schema_checks"] = SCHEMA_DIAGNOSTIC
        finally:
            logging.disable(previous_logging)
            sys.path[:] = previous_path
    result = receipt(
        phase=PHASE, phases=PHASES, source_sha=SOURCE_SHA, cleanup=run.cleanup, passed=ok
    )
    if args.debug_failure:
        result["debug_failure"] = debug
    print(json.dumps(result, sort_keys=True))
    return 0 if ok and run.cleanup else 1


if __name__ == "__main__":
    raise SystemExit(main())

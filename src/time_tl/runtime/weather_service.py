"""Central weather composition. Production is blocked pending a new signed head12 release.

This module never reads a project .env, Telegram token, owner seed or paid API key.
Only MET is constructed, after persisted explicit configuration and contact approval.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from uuid import uuid4

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from time_tl.application.weather_collection import WeatherCollector
from time_tl.domain.weather import ForecastRun, WeatherThresholds
from time_tl.infrastructure.db.engine import create_engine, create_session_factory
from time_tl.infrastructure.db.weather import PostgresWeatherCoverage, PostgresWeatherStorage
from time_tl.infrastructure.db.weather_configuration import (
    PostgresWeatherConfiguration,
    WeatherConfiguration,
)
from time_tl.infrastructure.db.weather_retention import PostgresWeatherRetention
from time_tl.infrastructure.db.workers import PostgresWorkerHeartbeatRepository
from time_tl.infrastructure.weather.met_norway import MetNorwayProvider, WeatherProviderError

from .contracts import PRODUCTION_SCHEMA_HEAD
from .weather_release import WeatherFeatureAuthority, verify_weather_scope, weather_authority_guard
from .weather_worker import (
    WeatherPostgresLease,
    WeatherRuntimeConfiguration,
    WeatherWorkerDependencies,
    WeatherWorkerSettings,
    run_weather_worker,
    validate_weather_environment,
)

MIGRATION_HEAD = PRODUCTION_SCHEMA_HEAD


class WeatherServiceError(Exception):
    pass


def validate_weather_release_environment(
    settings: WeatherWorkerSettings,
) -> WeatherFeatureAuthority | None:
    if settings.environment == "production":
        try:
            authority = verify_weather_scope("weather-worker", settings)
            if not authority.enabled("weather_collect"):
                raise WeatherServiceError("WEATHER_PRODUCTION_RELEASE_NOT_AUTHORIZED")
            return authority
        except Exception:
            raise WeatherServiceError("WEATHER_PRODUCTION_RELEASE_NOT_AUTHORIZED") from None
    return None


async def validate_weather_schema(session: AsyncSession, now: datetime) -> None:
    if now.tzinfo is None:
        raise WeatherServiceError("WEATHER_CLOCK_INVALID")
    versions = tuple(
        (await session.execute(text("SELECT version_num FROM alembic_version"))).scalars()
    )
    if versions != (MIGRATION_HEAD,):
        raise WeatherServiceError("WEATHER_SCHEMA_NOT_AUTHORIZED")


class WeatherServiceComposition:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        client: httpx.AsyncClient,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        authority_guard: Callable[[AsyncSession, datetime], Awaitable[None]] | None = None,
        health_marker_enabled: bool = False,
    ) -> None:
        self.factory, self.client, self.clock = factory, client, clock
        self.authority_guard = authority_guard or validate_weather_schema
        self.health_marker_enabled = health_marker_enabled
        self.configuration_store = PostgresWeatherConfiguration(
            factory, authority_guard=authority_guard
        )
        self.coverage = PostgresWeatherCoverage(factory, clock=clock)
        self.heartbeat_store = PostgresWorkerHeartbeatRepository(factory)
        self.instance_id = str(uuid4())
        self.current: WeatherConfiguration | None = None
        self.generation: int | None = None
        self.storage: PostgresWeatherStorage | None = None
        self.provider: MetNorwayProvider | None = None
        self.manual_sync_id: str | None = None

    async def configuration(self) -> WeatherRuntimeConfiguration:
        self.current = await self.configuration_store.load()
        return WeatherRuntimeConfiguration(
            self.current.enabled, self.current.cycles, self.current.met_settings()
        )

    def collector(self, configuration: WeatherRuntimeConfiguration) -> WeatherCollector:
        current = self.current
        if current is None or not configuration.admitted:
            raise WeatherProviderError("WEATHER_DISABLED")
        if self.generation != current.revision:
            self.storage = PostgresWeatherStorage(
                self.factory,
                authority_guard=self.authority_guard,
                thresholds=WeatherThresholds(
                    rain_mm=current.rain_mm,
                    rain_probability=current.rain_probability,
                    gust_ms=current.gust_ms,
                    temperature_change_c=current.temperature_change_c,
                ),
                expected_configuration_revision=current.revision,
                clock=self.clock,
            )
            self.provider = MetNorwayProvider(
                self.client,
                configuration.met,
                before_request=self.storage.authorize_source_request,
                after_response=self.storage.record_source_response,
                clock=self.clock,
            )
            self.generation = current.revision
        assert self.storage is not None and self.provider is not None
        return WeatherCollector(self.coverage, self.storage, self.provider, self.clock)

    async def initial_fetch_pending(self) -> bool:
        self.manual_sync_id = await self.configuration_store.pending_sync()
        if self.manual_sync_id is not None:
            return True
        # Reads only active points; there is no all-country scan and no HTTP here.
        for point in await self.coverage.active_weather_points():
            reader = self.storage or PostgresWeatherStorage(self.factory, clock=self.clock)
            previous = await reader.previous(point.point_id)
            if previous is None or not previous.usable(self.clock()):
                return True
        return False

    async def record_run(self, run: ForecastRun) -> None:
        # Collector's begin/finish lifecycle is the durable owner. This callback
        # verifies the result, it must not create a second unrelated run FK.
        if run.status not in {"PASS", "PARTIAL", "INTERRUPTED"}:
            raise WeatherServiceError("WEATHER_RUN_INVALID")
        if run.status == "PASS" and self.manual_sync_id is not None:
            await self.configuration_store.complete_sync(self.manual_sync_id)
            self.manual_sync_id = None

    async def heartbeat(self, now: datetime, status: str) -> None:
        await self.heartbeat_store.upsert(
            worker_name="weather-worker",
            instance_id=self.instance_id,
            status=status,
            heartbeat_at=now,
            details={
                "provider": "MET Norway",
                "configuration_revision": self.generation,
                "schema": MIGRATION_HEAD,
            },
        )
        if self.health_marker_enabled:
            from .weather_health import write_weather_marker

            write_weather_marker("weather-worker", self.instance_id, now)


async def _retention_loop(
    retention: PostgresWeatherRetention, stop: asyncio.Event, clock: Callable[[], datetime]
) -> None:
    while not stop.is_set():
        await retention.cleanup(clock(), limit=200)
        try:
            await asyncio.wait_for(stop.wait(), timeout=3600)
        except TimeoutError:
            pass


async def run_weather_components(
    settings: WeatherWorkerSettings,
    dependencies: WeatherWorkerDependencies,
    retention: PostgresWeatherRetention,
    *,
    stop: asyncio.Event | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
    shared_stop = stop or asyncio.Event()
    async with asyncio.TaskGroup() as tasks:
        tasks.create_task(_retention_loop(retention, shared_stop, clock))
        try:
            await run_weather_worker(settings, dependencies, stop=shared_stop, clock=clock)
        finally:
            shared_stop.set()


async def run_weather_service(
    settings: WeatherWorkerSettings, *, check_only: bool = False, stop: asyncio.Event | None = None
) -> None:
    authority = validate_weather_release_environment(settings)
    engine = create_engine(settings.database_url.get_secret_value(), pool_size=3, max_overflow=2)
    try:
        factory = create_session_factory(engine)
        async with factory() as session:
            await validate_weather_schema(session, datetime.now(UTC))
        if check_only or not settings.weather_enabled:
            return
        async with httpx.AsyncClient(trust_env=False) as client:
            guard = weather_authority_guard(authority, settings) if authority else None
            composition = WeatherServiceComposition(
                factory, client, authority_guard=guard, health_marker_enabled=True
            )
            dependencies = WeatherWorkerDependencies(
                WeatherPostgresLease(engine),
                composition.configuration,
                composition.collector,
                composition.record_run,
                composition.heartbeat,
                composition.initial_fetch_pending,
            )
            await run_weather_components(
                settings,
                dependencies,
                PostgresWeatherRetention(factory, authority_guard=guard),
                stop=stop,
            )
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        validate_weather_environment(os.environ)
        settings = WeatherWorkerSettings()  # type: ignore[call-arg]
        asyncio.run(run_weather_service(settings, check_only=args.check))
    except Exception:
        print(
            json.dumps(
                {
                    "status": "REJECTED",
                    "service": "weather-worker",
                    "code": "WEATHER_RUNTIME_NOT_AUTHORIZED",
                }
            )
        )
        raise SystemExit(1) from None
    if args.check:
        print(json.dumps({"status": "PASS", "scope": "weather_nonproduction_startup_only"}))


if __name__ == "__main__":
    main()

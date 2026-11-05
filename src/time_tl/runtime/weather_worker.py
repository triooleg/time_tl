"""Central active-point weather loop; composition is injected by runtime services.

No .env is read. The deployment flag and persisted configuration must both opt in.
The dedicated PostgreSQL connection is retained for the lifetime of its advisory lease.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from time_tl.application.weather_collection import WeatherCollector
from time_tl.domain.weather import ForecastRun, next_weather_collection
from time_tl.infrastructure.weather.met_norway import MetNorwaySettings

WEATHER_LOCK_KEY = 1728849652


class WeatherWorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=None, extra="ignore", case_sensitive=False, hide_input_in_errors=True
    )
    environment: Literal["production", "staging", "test", "local"] = Field(
        default="production", alias="TIME_TL_ENV"
    )
    service_role: Literal["weather-worker"] = Field(default="weather-worker", alias="SERVICE_ROLE")
    database_url: SecretStr = Field(alias="DATABASE_URL")
    weather_enabled: bool = Field(default=False, alias="WEATHER_ENABLED")
    poll_seconds: float = Field(default=15, ge=1, le=60, alias="WEATHER_POLL_SECONDS")
    initial_fetch_cooldown_seconds: int = Field(
        default=300, ge=60, le=3600, alias="WEATHER_INITIAL_FETCH_COOLDOWN_SECONDS"
    )

    @field_validator("database_url")
    @classmethod
    def postgres_only(cls, value: SecretStr) -> SecretStr:
        parsed = urlsplit(value.get_secret_value())
        if parsed.scheme != "postgresql+asyncpg" or not parsed.hostname or not parsed.path:
            raise ValueError("weather worker requires PostgreSQL")
        return value


@dataclass(frozen=True)
class WeatherRuntimeConfiguration:
    enabled: bool = False
    cycles_per_day: int = 6
    met: MetNorwaySettings = MetNorwaySettings()

    def __post_init__(self) -> None:
        if self.cycles_per_day not in (4, 6):
            raise ValueError("unsupported_frequency")

    @property
    def admitted(self) -> bool:
        return self.enabled and self.met.enabled and self.met.terms_approved


class WeatherLease(Protocol):
    async def acquire(self) -> bool: ...
    async def renew(self) -> bool: ...
    async def release(self) -> None: ...


class WeatherLeaseLost(Exception):
    pass


class WeatherPostgresLease:
    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine
        self.connection: AsyncConnection | None = None

    async def acquire(self) -> bool:
        if self.connection is not None:
            return False
        connection = await self.engine.connect()
        try:
            held = await connection.scalar(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": WEATHER_LOCK_KEY}
            )
            if held is not True:
                await connection.close()
                return False
            self.connection = connection
            return True
        except BaseException:
            await connection.close()
            raise

    async def renew(self) -> bool:
        if self.connection is None:
            return False
        try:
            held = await self.connection.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype='advisory' "
                    "AND pid=pg_backend_pid() AND classid=0 AND objid=:key "
                    "AND objsubid=1 AND granted)"
                ),
                {"key": WEATHER_LOCK_KEY},
            )
            return held is True
        except Exception:
            return False

    async def release(self) -> None:
        connection, self.connection = self.connection, None
        if connection is not None:
            try:
                await connection.execute(
                    text("SELECT pg_advisory_unlock(:key)"), {"key": WEATHER_LOCK_KEY}
                )
            finally:
                await connection.close()


@dataclass(frozen=True)
class WeatherWorkerDependencies:
    lease: WeatherLease
    configuration: Callable[[], Awaitable[WeatherRuntimeConfiguration]]
    collector_factory: Callable[[WeatherRuntimeConfiguration], WeatherCollector]
    record_run: Callable[[ForecastRun], Awaitable[None]]
    heartbeat: Callable[[datetime, str], Awaitable[None]]
    initial_fetch_pending: Callable[[], Awaitable[bool]]


def validate_weather_environment(environment: Mapping[str, str]) -> None:
    for key in environment:
        if key.upper().endswith(("_TOKEN", "_API_KEY", "_FERNET_KEY", "_PRIVATE_KEY")):
            raise ValueError("weather worker must not inherit transport or signing secrets")


async def _collect_guarded(
    collector: WeatherCollector, lease: WeatherLease, stop: asyncio.Event, interval: float
) -> ForecastRun | None:
    task = asyncio.create_task(collector.collect())
    try:
        while True:
            if stop.is_set():
                return None
            if not await lease.renew():
                raise WeatherLeaseLost("weather_singleton_lost")
            done, _ = await asyncio.wait({task}, timeout=interval)
            if task in done:
                if not await lease.renew():
                    raise WeatherLeaseLost("weather_singleton_lost")
                return task.result()
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def run_weather_worker(
    settings: WeatherWorkerSettings,
    dependencies: WeatherWorkerDependencies,
    *,
    stop: asyncio.Event | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Sequential cycles, active points only, no replay of previous scheduled slots.

    On startup only a persisted pending initial fetch can run immediately. A restart
    without pending points waits for the next future Kyiv slot. Persistent singleton
    admission and previous snapshots are always supplied from PostgreSQL.
    """
    if not settings.weather_enabled:
        return
    stop = stop or asyncio.Event()
    if not await dependencies.lease.acquire():
        return
    next_slot: datetime | None = None
    last_frequency: int | None = None
    initial_not_before = clock()
    try:
        while not stop.is_set():
            if not await dependencies.lease.renew():
                raise WeatherLeaseLost("weather_singleton_lost")
            config = await dependencies.configuration()
            now = clock()
            if next_slot is None or last_frequency != config.cycles_per_day:
                next_slot = next_weather_collection(now, config.cycles_per_day)
                last_frequency = config.cycles_per_day
            if not config.admitted:
                await dependencies.heartbeat(now, "DISABLED")
                await sleep(settings.poll_seconds)
                continue
            config.met.validate()
            pending = now >= initial_not_before and await dependencies.initial_fetch_pending()
            if now >= next_slot or pending:
                collector = dependencies.collector_factory(config)
                run = await _collect_guarded(
                    collector, dependencies.lease, stop, settings.poll_seconds
                )
                if run is None:
                    return
                await dependencies.record_run(run)
                await dependencies.heartbeat(clock(), "PARTIAL" if run.failed_locations else "PASS")
                initial_not_before = clock() + timedelta(
                    seconds=settings.initial_fetch_cooldown_seconds
                )
                next_slot = next_weather_collection(clock(), config.cycles_per_day)
            else:
                await dependencies.heartbeat(now, "WAITING")
            await sleep(settings.poll_seconds)
    finally:
        await dependencies.lease.release()

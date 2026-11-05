"""Cache-only weather planner. Shares the existing sender, never a Telegram token."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from time_tl.infrastructure.db.engine import create_engine, create_session_factory
from time_tl.infrastructure.db.lease import dedicated_advisory_lease
from time_tl.infrastructure.db.weather_notifications import PostgresWeatherNotificationPlanner
from time_tl.infrastructure.db.workers import PostgresWorkerHeartbeatRepository

from .weather_release import verify_weather_scope, weather_authority_guard
from .weather_service import MIGRATION_HEAD, validate_weather_schema
from .weather_worker import WeatherLease, validate_weather_environment

PLANNER_LOCK_KEY = 0x54494D45574E


class WeatherPlannerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore", hide_input_in_errors=True)
    environment: Literal["production", "staging", "local", "test"] = Field(
        default="production", alias="TIME_TL_ENV"
    )
    enabled: bool = Field(default=False, alias="WEATHER_NOTIFICATIONS_ENABLED")
    service_role: Literal["weather-planner"] = Field(
        default="weather-planner", alias="SERVICE_ROLE"
    )
    database_url: SecretStr = Field(alias="DATABASE_URL")
    interval: int = Field(default=30, ge=10, le=60, alias="WEATHER_PLANNER_INTERVAL_SECONDS")

    @field_validator("database_url")
    @classmethod
    def postgres_only(cls, value: SecretStr) -> SecretStr:
        parsed = urlsplit(value.get_secret_value())
        if parsed.scheme != "postgresql+asyncpg" or not parsed.hostname or not parsed.path:
            raise ValueError("weather planner requires PostgreSQL")
        return value


async def planner_loop(
    planner: PostgresWeatherNotificationPlanner,
    lease: WeatherLease,
    heartbeat: Callable[[datetime], Awaitable[None]],
    *,
    stop: asyncio.Event,
    interval: int = 30,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    while not stop.is_set():
        renew = lease.renew
        if not await renew():
            raise RuntimeError("weather_planner_lease_lost")
        now = clock()
        await planner.run_once(now)
        if not await renew():
            raise RuntimeError("weather_planner_lease_lost")
        await heartbeat(clock())
        await sleep(interval)


async def run_weather_notification_service(
    settings: WeatherPlannerSettings, *, stop: asyncio.Event | None = None
) -> None:
    authority = None
    if settings.environment == "production":
        try:
            authority = verify_weather_scope("weather-planner", settings)
            if not authority.enabled("weather_plan"):
                raise RuntimeError("weather_production_release_not_authorized")
        except Exception:
            raise RuntimeError("weather_production_release_not_authorized") from None
    if not settings.enabled:
        return
    engine = create_engine(settings.database_url.get_secret_value(), pool_size=3, max_overflow=2)
    try:
        factory = create_session_factory(engine)
        async with factory() as session:
            await validate_weather_schema(session, datetime.now(UTC))
        heartbeat_store = PostgresWorkerHeartbeatRepository(factory)
        instance = str(uuid4())

        async def heartbeat(now: datetime) -> None:
            await heartbeat_store.upsert(
                worker_name="weather-planner",
                instance_id=instance,
                status="PASS",
                heartbeat_at=now,
                details={"schema": MIGRATION_HEAD},
            )
            from .weather_health import write_weather_marker

            write_weather_marker("weather-planner", instance, now)

        async with dedicated_advisory_lease(
            settings.database_url.get_secret_value(), PLANNER_LOCK_KEY
        ) as lease:
            if not await lease.acquire():
                raise RuntimeError("weather_planner_already_running")
            await planner_loop(
                PostgresWeatherNotificationPlanner(
                    factory,
                    enabled=True,
                    authority_guard=weather_authority_guard(authority, settings)
                    if authority
                    else None,
                ),
                lease,
                heartbeat,
                stop=stop or asyncio.Event(),
                interval=settings.interval,
            )
    finally:
        await engine.dispose()


def main() -> None:
    logging.disable(logging.CRITICAL)
    try:
        validate_weather_environment(os.environ)
        settings = WeatherPlannerSettings()  # type: ignore[call-arg]
        asyncio.run(run_weather_notification_service(settings))
    except Exception:
        raise SystemExit("weather_planner_failed; inspect redacted local metadata") from None


if __name__ == "__main__":
    main()

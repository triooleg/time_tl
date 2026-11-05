import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from time_tl.runtime.weather_notification_service import (
    WeatherPlannerSettings,
    planner_loop,
    run_weather_notification_service,
)


@pytest.mark.asyncio
async def test_disabled_service_does_not_connect_or_plan(monkeypatch):
    monkeypatch.setattr(
        "time_tl.runtime.weather_notification_service.create_engine",
        lambda *a, **k: pytest.fail("disabled weather planner must not connect"),
    )
    await run_weather_notification_service(
        WeatherPlannerSettings(
            TIME_TL_ENV="local", DATABASE_URL="postgresql+asyncpg://synthetic@127.0.0.1/test"
        )
    )


@pytest.mark.asyncio
async def test_production_is_blocked_before_any_connection(monkeypatch):
    monkeypatch.setattr(
        "time_tl.runtime.weather_notification_service.create_engine",
        lambda *a, **k: pytest.fail("production must not be touched"),
    )
    with pytest.raises(RuntimeError, match="not_authorized"):
        await run_weather_notification_service(
            WeatherPlannerSettings(
                TIME_TL_ENV="production",
                WEATHER_NOTIFICATIONS_ENABLED=True,
                DATABASE_URL="postgresql+asyncpg://synthetic@127.0.0.1/test",
            )
        )


@pytest.mark.asyncio
async def test_loop_actually_calls_shared_db_planner_without_provider_or_transport():
    stop = asyncio.Event()
    now = datetime(2026, 11, 5, tzinfo=UTC)
    planner = SimpleNamespace(run_once=AsyncMock(return_value=1))
    lease = SimpleNamespace(renew=AsyncMock(return_value=True))
    heartbeat = AsyncMock()

    async def sleep(interval):
        assert interval == 30
        stop.set()

    await planner_loop(planner, lease, heartbeat, stop=stop, clock=lambda: now, sleep=sleep)
    planner.run_once.assert_awaited_once_with(now)
    heartbeat.assert_awaited_once_with(now)

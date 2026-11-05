from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from time_tl.infrastructure.db.weather_configuration import WeatherConfiguration
from time_tl.runtime.weather_service import (
    MIGRATION_HEAD,
    WeatherServiceComposition,
    WeatherServiceError,
    run_weather_service,
    validate_weather_schema,
)
from time_tl.runtime.weather_worker import WeatherRuntimeConfiguration, WeatherWorkerSettings

NOW = datetime(2026, 11, 5, 10, tzinfo=UTC)


async def test_production_blocked_before_engine_or_http(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("No production DB/API access")

    monkeypatch.setattr("time_tl.runtime.weather_service.create_engine", forbidden)
    settings = WeatherWorkerSettings(
        DATABASE_URL="postgresql+asyncpg://postgres/test",
        TIME_TL_ENV="production",
        WEATHER_ENABLED=True,
    )
    with pytest.raises(WeatherServiceError, match="WEATHER_PRODUCTION_RELEASE_NOT_AUTHORIZED"):
        await run_weather_service(settings)


@pytest.mark.parametrize(
    "heads",
    [("0009_calendar_foundation",), ("0010_personal_calendars",), (MIGRATION_HEAD, "other"), ()],
)
async def test_schema_exact_no_old_attestation_admission(heads: tuple[str, ...]) -> None:
    session = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(scalars=lambda: heads))
    )
    with pytest.raises(WeatherServiceError, match="WEATHER_SCHEMA_NOT_AUTHORIZED"):
        await validate_weather_schema(session, NOW)


async def test_schema_head12_only_local_validation() -> None:
    session = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(scalars=lambda: (MIGRATION_HEAD,)))
    )
    await validate_weather_schema(session, NOW)


async def test_provider_reused_by_config_revision_and_final_permissions_wired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Storage:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.authorize_source_request = AsyncMock()
            self.record_source_response = AsyncMock()
            self.previous = AsyncMock(return_value=None)

    monkeypatch.setattr("time_tl.runtime.weather_service.PostgresWeatherStorage", Storage)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: (_ for _ in ()).throw(AssertionError("no actual request"))
        )
    ) as client:
        service = WeatherServiceComposition(None, client, clock=lambda: NOW)
        config = WeatherConfiguration(revision=1, enabled=True, terms_approved=True)
        service.current = config
        runtime = WeatherRuntimeConfiguration(True, config.cycles, config.met_settings())
        first = service.collector(runtime)
        assert first.provider is service.collector(runtime).provider
        assert service.provider.before_request == service.storage.authorize_source_request
        assert service.provider.after_response == service.storage.record_source_response
        service.current = config.model_copy(update={"revision": 2})
        assert service.collector(runtime).provider is not first.provider


async def test_initial_pending_reads_active_points_only_no_transport() -> None:
    async with httpx.AsyncClient() as client:
        service = WeatherServiceComposition(None, client, clock=lambda: NOW)
        service.coverage = SimpleNamespace(active_weather_points=AsyncMock(return_value=()))
        service.configuration_store = SimpleNamespace(pending_sync=AsyncMock(return_value=None))
        assert not await service.initial_fetch_pending()


async def test_manual_sync_is_persisted_interest_and_ack_only_full_pass() -> None:
    from time_tl.domain.weather import ForecastRun

    async with httpx.AsyncClient() as client:
        service = WeatherServiceComposition(None, client, clock=lambda: NOW)
        config = SimpleNamespace(
            pending_sync=AsyncMock(return_value="job-1"), complete_sync=AsyncMock()
        )
        service.configuration_store = config
        assert await service.initial_fetch_pending()
        await service.record_run(ForecastRun("run", NOW, NOW, 2, 1, 1, "PARTIAL"))
        config.complete_sync.assert_not_called()
        await service.record_run(ForecastRun("run", NOW, NOW, 2, 2, 0, "PASS"))
        config.complete_sync.assert_awaited_once_with("job-1")
        assert service.manual_sync_id is None


async def test_retention_is_bounded_and_stops_with_source_worker(monkeypatch):
    import asyncio

    from time_tl.runtime.weather_service import run_weather_components

    stop = asyncio.Event()
    retention = SimpleNamespace(cleanup=AsyncMock())

    async def worker(*args, **kwargs):
        for _ in range(5):
            await asyncio.sleep(0)
            if retention.cleanup.await_count:
                break

    monkeypatch.setattr("time_tl.runtime.weather_service.run_weather_worker", worker)
    settings = WeatherWorkerSettings(
        DATABASE_URL="postgresql+asyncpg://postgres/test", TIME_TL_ENV="test", WEATHER_ENABLED=True
    )
    await asyncio.wait_for(
        run_weather_components(settings, None, retention, stop=stop, clock=lambda: NOW), 1
    )
    retention.cleanup.assert_awaited_once_with(NOW, limit=200)
    assert stop.is_set()

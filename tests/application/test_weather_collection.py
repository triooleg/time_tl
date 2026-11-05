from datetime import UTC, datetime, timedelta

from time_tl.application.weather_collection import WeatherCollector
from time_tl.domain.weather import ForecastHour, ForecastSnapshot, ForecastStatus, WeatherPoint
from time_tl.infrastructure.weather.met_norway import WeatherProviderError

NOW = datetime(2026, 11, 5, 10, tzinfo=UTC)


class Coverage:
    async def active_weather_points(self) -> tuple[WeatherPoint, ...]:
        return (WeatherPoint("a", 48, 34), WeatherPoint("a", 48, 34), WeatherPoint("b", 49, 35))


class Storage:
    def __init__(self) -> None:
        self.saved: dict[str, ForecastSnapshot] = {}
        self.errors: list[tuple[str, str]] = []

    async def previous(self, point_id: str) -> ForecastSnapshot | None:
        return self.saved.get(point_id)

    async def save(self, snapshot: ForecastSnapshot) -> None:
        self.saved[snapshot.point_id] = snapshot

    async def record_failure(self, point_id: str, code: str, now: datetime) -> None:
        self.errors.append((point_id, code))


class Provider:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def fetch(
        self, point: WeatherPoint, now: datetime, previous: ForecastSnapshot | None = None
    ) -> ForecastSnapshot:
        self.calls.append(point.point_id)
        if point.point_id == "b":
            raise WeatherProviderError("UNAVAILABLE")
        return ForecastSnapshot(
            "MET Norway",
            point.point_id,
            now,
            now,
            now + timedelta(hours=1),
            (ForecastHour(now, temperature_c=4),),
            ForecastStatus.FRESH,
            "url",
        )


async def test_unique_active_points_failure_and_restart() -> None:
    store, provider = Storage(), Provider()
    run = await WeatherCollector(Coverage(), store, provider, lambda: NOW).collect()
    assert (run.requested_locations, run.successful_locations, run.failed_locations) == (2, 1, 1)
    assert provider.calls == ["a", "b"]
    assert set(store.saved) == {"a"}
    again = await WeatherCollector(Coverage(), store, provider, lambda: NOW).collect()
    assert again.successful_locations == 1 and store.errors == [("b", "UNAVAILABLE")] * 2


async def test_no_active_points_no_http() -> None:
    class Empty:
        async def active_weather_points(self) -> tuple[WeatherPoint, ...]:
            return ()

    provider = Provider()
    run = await WeatherCollector(Empty(), Storage(), provider, lambda: NOW).collect()
    assert run.requested_locations == 0 and provider.calls == []


async def test_durable_run_exists_before_snapshot_and_finalizes_same_id() -> None:
    class Durable(Storage):
        def __init__(self) -> None:
            super().__init__()
            self.run_id: str | None = None
            self.finished_id: str | None = None

        async def begin_run(
            self, run_id: str, started_at: datetime, requested_locations: int
        ) -> None:
            self.run_id = run_id
            assert requested_locations == 2

        async def save(self, snapshot: ForecastSnapshot) -> None:
            assert self.run_id is not None and self.finished_id is None
            await super().save(snapshot)

        async def finish_run(self, run: object) -> None:
            from time_tl.domain.weather import ForecastRun

            assert isinstance(run, ForecastRun)
            self.finished_id = run.run_id
            assert run.status == "PARTIAL"

    store = Durable()
    run = await WeatherCollector(Coverage(), store, Provider(), lambda: NOW).collect()
    assert store.run_id == store.finished_id == run.run_id


async def test_cancellation_finalizes_interrupted_run_without_empty_forecast() -> None:
    import asyncio

    from time_tl.domain.weather import ForecastRun

    class Durable(Storage):
        finished: ForecastRun | None = None

        async def begin_run(
            self, run_id: str, started_at: datetime, requested_locations: int
        ) -> None:
            pass

        async def finish_run(self, run: ForecastRun) -> None:
            self.finished = run

    class Cancel(Provider):
        async def fetch(
            self, point: WeatherPoint, now: datetime, previous: ForecastSnapshot | None = None
        ) -> ForecastSnapshot:
            raise asyncio.CancelledError

    import pytest

    store = Durable()
    with pytest.raises(asyncio.CancelledError):
        await WeatherCollector(Coverage(), store, Cancel(), lambda: NOW).collect()
    assert store.finished is not None and store.finished.status == "INTERRUPTED"
    assert store.saved == {}


async def test_source_retry_fence_blocks_other_points_and_survives_restart() -> None:
    class Durable(Storage):
        retry_at: datetime | None = None

        async def source_retry_at(self, provider: str) -> datetime | None:
            assert provider == "MET Norway"
            return self.retry_at

        async def record_source_retry(self, provider: str, retry_at: datetime) -> None:
            self.retry_at = max(self.retry_at or retry_at, retry_at)

    class RateLimited(Provider):
        async def fetch(
            self, point: WeatherPoint, now: datetime, previous: ForecastSnapshot | None = None
        ) -> ForecastSnapshot:
            self.calls.append(point.point_id)
            raise WeatherProviderError("RATE_LIMITED", now + timedelta(hours=1))

    store, provider = Durable(), RateLimited()
    await WeatherCollector(Coverage(), store, provider, lambda: NOW).collect()
    assert provider.calls == ["a"]
    await WeatherCollector(Coverage(), store, provider, lambda: NOW).collect()
    assert provider.calls == ["a"]  # A fresh provider process cannot bypass the DB fence.


async def test_durable_request_spacing_waits_once_and_rechecks_claim() -> None:
    now = NOW

    class Durable(Storage):
        claims: int = 0

        async def claim_source_request(self, point: WeatherPoint, at: datetime) -> None:
            self.claims += 1
            if self.claims == 1:
                raise WeatherProviderError("REQUEST_DEFERRED", at + timedelta(seconds=1))

    async def sleep(seconds: float) -> None:
        nonlocal now
        now += timedelta(seconds=seconds)

    store, provider = Durable(), Provider()
    run = await WeatherCollector(Coverage(), store, provider, lambda: now, sleep=sleep).collect()
    assert store.claims == 3 and provider.calls == ["a", "b"]
    assert run.successful_locations == 1


async def test_long_durable_request_fence_never_sleeps_or_calls_source() -> None:
    class Durable(Storage):
        async def claim_source_request(self, point: WeatherPoint, at: datetime) -> None:
            raise WeatherProviderError("REQUEST_DEFERRED", at + timedelta(hours=1))

    async def sleep(seconds: float) -> None:
        raise AssertionError("long retry should not sleep")

    provider = Provider()
    run = await WeatherCollector(
        Coverage(), Durable(), provider, lambda: NOW, sleep=sleep
    ).collect()
    assert provider.calls == [] and run.failed_locations == 2

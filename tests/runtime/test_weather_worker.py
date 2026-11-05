import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from time_tl.application.weather_collection import WeatherCollector
from time_tl.domain.weather import ForecastRun
from time_tl.infrastructure.weather.met_norway import MetNorwaySettings
from time_tl.runtime.weather_worker import (
    WeatherLeaseLost,
    WeatherRuntimeConfiguration,
    WeatherWorkerDependencies,
    WeatherWorkerSettings,
    run_weather_worker,
    validate_weather_environment,
)

NOW = datetime(2026, 11, 5, 10, 30, tzinfo=UTC)
CONFIG = WeatherRuntimeConfiguration(
    True, 6, MetNorwaySettings(True, True, "TimeTL/1.0", "https://time.yoleotard.com")
)


class Lease:
    def __init__(self, available: bool = True) -> None:
        self.available = available
        self.held = False
        self.released = False

    async def acquire(self) -> bool:
        self.held = self.available
        return self.held

    async def renew(self) -> bool:
        return self.held

    async def release(self) -> None:
        self.released = True
        self.held = False


class Collector(WeatherCollector):
    def __init__(self, slow: bool = False) -> None:
        self.calls = 0
        self.cancelled = False
        self.slow = slow

    async def collect(self) -> ForecastRun:
        self.calls += 1
        if self.slow:
            try:
                await asyncio.Event().wait()
            finally:
                self.cancelled = True
        return ForecastRun("run", NOW, NOW, 1, 1, 0)


class Harness:
    def __init__(self) -> None:
        self.now = NOW
        self.stop = asyncio.Event()
        self.lease = Lease()
        self.collector = Collector()
        self.run_count = 0
        self.pending = True
        self.config = CONFIG
        self.pulses: list[str] = []
        self.sleeps = 0
        self.stop_after = 4

    async def configuration(self) -> WeatherRuntimeConfiguration:
        return self.config

    async def record(self, run: ForecastRun) -> None:
        self.run_count += 1

    async def heartbeat(self, now: datetime, status: str) -> None:
        self.pulses.append(status)

    async def initial(self) -> bool:
        return self.pending

    async def sleep(self, seconds: float) -> None:
        self.sleeps += 1
        self.now += timedelta(seconds=seconds)
        if self.sleeps >= self.stop_after:
            self.stop.set()

    def deps(self) -> WeatherWorkerDependencies:
        return WeatherWorkerDependencies(
            self.lease,
            self.configuration,
            lambda _: self.collector,
            self.record,
            self.heartbeat,
            self.initial,
        )

    async def run(self, enabled: bool = True) -> None:
        settings = WeatherWorkerSettings(
            DATABASE_URL="postgresql+asyncpg://postgres/test", WEATHER_ENABLED=enabled
        )
        await run_weather_worker(
            settings, self.deps(), stop=self.stop, clock=lambda: self.now, sleep=self.sleep
        )


async def test_flag_default_off_never_acquires_or_collects() -> None:
    h = Harness()
    await h.run(False)
    assert not h.lease.held and not h.lease.released and h.collector.calls == 0


async def test_singleton_denial_no_requests() -> None:
    h = Harness()
    h.lease.available = False
    await h.run()
    assert h.collector.calls == 0


async def test_one_initial_run_then_cooldown_not_every_tick() -> None:
    h = Harness()
    await h.run()
    assert h.collector.calls == h.run_count == 1
    assert h.lease.released and h.pulses == ["PASS", "WAITING", "WAITING", "WAITING"]


async def test_restart_no_pending_waits_future_no_catchup() -> None:
    h = Harness()
    h.pending = False
    await h.run()
    assert h.collector.calls == 0 and set(h.pulses) == {"WAITING"}


async def test_config_disabled_no_requests() -> None:
    h = Harness()
    h.config = WeatherRuntimeConfiguration()
    await h.run()
    assert h.collector.calls == 0 and set(h.pulses) == {"DISABLED"}


async def test_lease_loss_cancels_inflight_collection() -> None:
    h = Harness()
    h.collector = Collector(slow=True)
    settings = WeatherWorkerSettings(
        DATABASE_URL="postgresql+asyncpg://postgres/test",
        WEATHER_ENABLED=True,
        WEATHER_POLL_SECONDS=1,
    )
    task = asyncio.create_task(run_weather_worker(settings, h.deps(), stop=h.stop))
    for _ in range(20):
        await asyncio.sleep(0)
        if h.collector.calls:
            break
    h.lease.held = False
    with pytest.raises(WeatherLeaseLost):
        await asyncio.wait_for(task, 2)
    assert h.collector.cancelled and h.lease.released and h.run_count == 0


@pytest.mark.parametrize(
    "name",
    [
        "TELEGRAM_BOT_TOKEN",
        "OWNER_ALERTS_FERNET_KEY",
        "EXAMPLE_PRIVATE_KEY",
        "ALERTS_ENERGY_API_KEY",
    ],
)
def test_no_inherited_transport_secrets(name: str) -> None:
    with pytest.raises(ValueError):
        validate_weather_environment({name: "not printed"})


def test_configuration_frequency_and_database_validation() -> None:
    with pytest.raises(ValueError):
        WeatherRuntimeConfiguration(cycles_per_day=12)
    with pytest.raises(ValueError):
        WeatherWorkerSettings(DATABASE_URL="https://example.test")

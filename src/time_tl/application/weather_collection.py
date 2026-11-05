"""One forecast per active point; durable storage and singleton supplied by runtime."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Protocol, runtime_checkable
from uuid import uuid4

from time_tl.domain.weather import ForecastRun, ForecastSnapshot, WeatherPoint
from time_tl.infrastructure.weather.met_norway import WeatherProviderError


class WeatherCoverage(Protocol):
    async def active_weather_points(self) -> tuple[WeatherPoint, ...]: ...


class WeatherStorage(Protocol):
    async def previous(self, point_id: str) -> ForecastSnapshot | None: ...
    async def save(self, snapshot: ForecastSnapshot) -> None: ...
    async def record_failure(self, point_id: str, code: str, now: datetime) -> None: ...


@runtime_checkable
class WeatherRunStorage(Protocol):
    """Optional durable lifecycle: establish the run FK before any snapshot save."""

    async def begin_run(
        self, run_id: str, started_at: datetime, requested_locations: int
    ) -> None: ...
    async def finish_run(self, run: ForecastRun) -> None: ...


@runtime_checkable
class WeatherRetryStorage(Protocol):
    """Source-global durable Retry-After fence shared across points/restarts."""

    async def source_retry_at(self, provider: str) -> datetime | None: ...
    async def record_source_retry(self, provider: str, retry_at: datetime) -> None: ...


@runtime_checkable
class WeatherRequestStorage(Protocol):
    """Atomic persisted request fence, including approved active-point admission."""

    async def claim_source_request(self, point: WeatherPoint, now: datetime) -> None: ...


class WeatherProvider(Protocol):
    async def fetch(
        self, point: WeatherPoint, now: datetime, previous: ForecastSnapshot | None = None
    ) -> ForecastSnapshot: ...


class WeatherCollector:
    def __init__(
        self,
        coverage: WeatherCoverage,
        storage: WeatherStorage,
        provider: WeatherProvider,
        clock: Callable[[], datetime],
        *,
        provider_name: str = "MET Norway",
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.coverage = coverage
        self.storage = storage
        self.provider = provider
        self.clock = clock
        self.provider_name = provider_name
        self.sleep = sleep

    async def collect(self) -> ForecastRun:
        started = self.clock()
        points = {p.point_id: p for p in await self.coverage.active_weather_points()}
        run_id = str(uuid4())
        lifecycle = self.storage if isinstance(self.storage, WeatherRunStorage) else None
        if lifecycle is not None:
            await lifecycle.begin_run(run_id, started, len(points))
        retry_store = self.storage if isinstance(self.storage, WeatherRetryStorage) else None
        request_store = self.storage if isinstance(self.storage, WeatherRequestStorage) else None
        succeeded = failed = 0
        status = "INTERRUPTED"
        try:
            for point_id in sorted(points):
                # Recheck coverage before HTTP; removals stop future collection.
                active = {p.point_id for p in await self.coverage.active_weather_points()}
                if point_id not in active:
                    continue
                try:
                    if retry_store is not None:
                        fence = await retry_store.source_retry_at(self.provider_name)
                        if fence is not None and fence > self.clock():
                            raise WeatherProviderError("RATE_LIMITED", fence)
                    previous = await self.storage.previous(point_id)
                    if request_store is not None and (
                        previous is None
                        or previous.cache_expires_at is None
                        or previous.cache_expires_at <= self.clock()
                    ):
                        try:
                            await request_store.claim_source_request(points[point_id], self.clock())
                        except WeatherProviderError as error:
                            delay = (
                                None
                                if error.retry_at is None
                                else (error.retry_at - self.clock()).total_seconds()
                            )
                            if (
                                error.code != "REQUEST_DEFERRED"
                                or delay is None
                                or not 0 < delay <= 60
                            ):
                                raise
                            await self.sleep(delay)
                            await request_store.claim_source_request(points[point_id], self.clock())
                    snapshot = await self.provider.fetch(points[point_id], self.clock(), previous)
                    if snapshot.point_id != point_id:
                        raise WeatherProviderError("CACHE_IDENTITY_MISMATCH")
                    await self.storage.save(snapshot)
                    succeeded += 1
                except WeatherProviderError as error:
                    if (
                        retry_store is not None
                        and error.retry_at is not None
                        and error.code == "RATE_LIMITED"
                    ):
                        await retry_store.record_source_retry(self.provider_name, error.retry_at)
                    await self.storage.record_failure(point_id, error.code, self.clock())
                    failed += 1  # Failure never replaces previous facts with empty/clear.
            status = "PARTIAL" if failed else "PASS"
        finally:
            run = ForecastRun(run_id, started, self.clock(), len(points), succeeded, failed, status)
            if lifecycle is not None:
                await lifecycle.finish_run(run)
        return run

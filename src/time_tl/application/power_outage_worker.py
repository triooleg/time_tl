"""One central poller per operator; provider errors cannot stop alert workers."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from time_tl.domain.power_outages.models import (
    PowerProvider,
    PowerScheduleSnapshot,
    SourceStatus,
    aware,
)
from time_tl.infrastructure.power_outages.base import (
    PowerFetchError,
    PowerScheduleProvider,
)


class PowerLease(Protocol):
    async def acquire(self) -> bool: ...
    async def renew(self) -> bool: ...


class PowerPollRepository(Protocol):
    async def acquire_poll_slot(
        self, provider: PowerProvider, now: datetime, interval_seconds: int = 300
    ) -> int | None: ...

    async def store_snapshot(
        self,
        snapshot: PowerScheduleSnapshot,
        now: datetime,
        interval_seconds: int = 300,
        *,
        expected_configuration_revision: int | None = None,
        retry_after_seconds: float | None = None,
        permanent: bool = False,
    ) -> None: ...


class PowerOutageWorker:
    def __init__(
        self,
        provider: PowerScheduleProvider,
        repository: PowerPollRepository,
        lease: PowerLease,
        *,
        poll_interval_seconds: int = 300,
        request_timeout_seconds: float = 15,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if type(poll_interval_seconds) is not int or poll_interval_seconds < 1:
            raise ValueError("invalid_power_poll_interval")
        if not 0 < request_timeout_seconds <= 30:
            raise ValueError("invalid_power_poll_timeout")
        self.provider, self.repository, self.lease = provider, repository, lease
        self.interval = poll_interval_seconds
        self.timeout = request_timeout_seconds
        self.clock = clock or (lambda: datetime.now(UTC))
        self._acquired = False
        self._cycle = asyncio.Lock()

    async def run_once(self, now: datetime) -> bool:
        aware(now)
        async with self._cycle:
            if not self._acquired:
                self._acquired = await self.lease.acquire()
            elif not await self.lease.renew():
                raise RuntimeError("power_lease_lost")
            if not self._acquired:
                return False
            revision = await self.repository.acquire_poll_slot(
                self.provider.provider_id, now, self.interval
            )
            if revision is None:
                return False
            retry_after, permanent = None, False
            try:
                snapshot = await asyncio.wait_for(
                    self.provider.fetch_schedules(), timeout=self.timeout
                )
                if snapshot.provider is not self.provider.provider_id:
                    raise ValueError("power_provider_identity_mismatch")
            except PowerFetchError as error:
                retry_after, permanent = error.retry_after_seconds, error.permanent
                snapshot = PowerScheduleSnapshot(
                    self.provider.provider_id,
                    self.clock(),
                    None,
                    (),
                    SourceStatus.UNAVAILABLE,
                    error.code.value,
                )
            except Exception:
                # Never retain a response body, URL, address or arbitrary exception text.
                snapshot = PowerScheduleSnapshot(
                    self.provider.provider_id,
                    self.clock(),
                    None,
                    (),
                    SourceStatus.UNAVAILABLE,
                    "FETCH_UNAVAILABLE",
                )
            if not await self.lease.renew():
                raise RuntimeError("power_lease_lost")
            await self.repository.store_snapshot(
                snapshot,
                self.clock(),
                self.interval,
                expected_configuration_revision=revision,
                retry_after_seconds=retry_after,
                permanent=permanent,
            )
            return True

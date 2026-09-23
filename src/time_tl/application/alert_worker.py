"""Single global poll worker; no per-user provider calls are permitted."""

from __future__ import annotations

import random
from collections.abc import Awaitable, Callable
from dataclasses import replace

from time_tl.infrastructure.alerts.lease import SingletonLease
from time_tl.infrastructure.alerts.provider import AlertsInUaAdapter, OperatorCircuitOpen
from time_tl.infrastructure.alerts.rate_limit import NoCatchUpRateLimiter
from time_tl.infrastructure.alerts.repositories import AlertRepository


class AlertWorker:
    def __init__(
        self,
        *,
        provider: AlertsInUaAdapter,
        repository: AlertRepository,
        lease: SingletonLease,
        poll_interval_seconds: float = 15.0,
        requests_per_minute: float = 5.0,
        monotonic: Callable[[], float],
        jitter: Callable[[float, float], float] = random.uniform,
    ) -> None:
        if poll_interval_seconds < 15:
            raise ValueError("poll cadence must be at least 15 seconds")
        self.provider, self.repository, self.lease = provider, repository, lease
        self.poll_interval_seconds = poll_interval_seconds
        self.rate_limiter = NoCatchUpRateLimiter(requests_per_minute)
        self.monotonic = monotonic
        self.startup_jitter_seconds = jitter(0.0, 15.0)

    async def serve(self, sleep: Callable[[float], Awaitable[None]]) -> None:
        """Production loop: hold the singleton lease until cancellation/exit."""
        if not await self.lease.acquire():
            return
        try:
            await self.repository.apply_process_restart_atomically()
            await sleep(self.startup_jitter_seconds)
            while await self.lease.renew():
                delay = await self._poll_once_while_held()
                await sleep(delay)
        finally:
            await self.lease.release()

    async def run_once(self) -> float:
        """Bounded integration/test hook that still enforces singleton ownership."""
        if not await self.lease.acquire():
            return self.poll_interval_seconds
        try:
            return await self._poll_once_while_held()
        finally:
            await self.lease.release()

    async def _poll_once_while_held(self) -> float:
        state = await self.repository.poll_state()
        now = self.monotonic()
        if state.circuit_open:
            raise OperatorCircuitOpen("401/403 circuit requires operator resume")
        if now < state.retry_not_before_monotonic:
            return max(self.poll_interval_seconds, state.retry_not_before_monotonic - now)
        limiter_delay = self.rate_limiter.delay(now)
        if limiter_delay:
            return max(self.poll_interval_seconds, limiter_delay)
        if not self.rate_limiter.consume(now):
            return self.poll_interval_seconds
        revision = state.fetch_revision + 1
        result = await self.provider.fetch(
            fetch_revision=revision, last_modified=state.last_modified
        )
        if result.failure is not None:
            await self.repository.apply_failure_atomically(result.failure)
            failures = state.consecutive_failures + 1
            exponential = min(300.0, self.poll_interval_seconds * (2 ** (failures - 1)))
            retry: float = max(float(exponential), float(result.retry_after_seconds or 0.0))
            await self.repository.set_poll_state(
                replace(
                    state,
                    fetch_revision=revision,
                    circuit_open=result.open_operator_circuit,
                    retry_not_before_monotonic=now + retry,
                    consecutive_failures=failures,
                )
            )
            return retry
        assert result.snapshot is not None
        await self.repository.apply_snapshot_and_poll_state_atomically(
            result.snapshot,
            replace(
                state,
                fetch_revision=revision,
                last_modified=result.last_modified,
                retry_not_before_monotonic=0.0,
                consecutive_failures=0,
            ),
        )
        return self.poll_interval_seconds

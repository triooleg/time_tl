"""Every Alerts Energy consumer shares durable account admission before HTTP."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from time_tl.infrastructure.db.energy_api_budget import QuotaDeferred

from .alerts_energy_api import AlertsEnergyApiClient
from .base import PowerFetchError, PowerFetchErrorCode


class BudgetedEnergyApiClient(AlertsEnergyApiClient):
    def __init__(
        self,
        *,
        budget: Any,
        before_request: Callable[[], Awaitable[None]] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        **kwargs: Any,
    ) -> None:
        super().__init__(clock=clock, **kwargs)
        self._budget, self._before, self._clock, self._sleep = budget, before_request, clock, sleep

    async def _get(self, path: str, *, params: Mapping[str, str] | None = None) -> object:
        # A spacing retry does not spend quota. All admitted attempts do, even failed HTTP.
        while True:
            if self._before is not None:
                await self._before()
            try:
                await self._budget.reserve(self._clock())
                break
            except QuotaDeferred as exc:
                if exc.reason == "quota_spacing":
                    await self._sleep(exc.retry_after_seconds)
                    continue
                raise PowerFetchError(
                    PowerFetchErrorCode.RATE_LIMITED,
                    status_code=429,
                    retry_after_seconds=min(86400, exc.retry_after_seconds),
                ) from None
        try:
            if self._before is not None:
                await self._before()
            return await super()._get(path, params=params)
        except PowerFetchError as exc:
            if exc.code is PowerFetchErrorCode.RATE_LIMITED:
                await self._budget.defer(self._clock(), exc.retry_after_seconds or 86400)
            raise

"""Lazy address directory for explicitly enabled local testing, never automatic collection."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from time import monotonic
from typing import Any, Protocol

from pydantic import SecretStr

from time_tl.domain.power_outages.address import (
    AssignmentChoice,
    HouseChoice,
    SettlementChoice,
    StreetChoice,
)
from time_tl.domain.power_outages.location import PowerLocationSeed
from time_tl.infrastructure.db.energy_api_credentials import (
    EnergyApiCheckStatus,
    PostgresEnergyApiCredentials,
)
from time_tl.infrastructure.power_outages.address_directory import AlertsEnergyAddressDirectory
from time_tl.infrastructure.power_outages.alerts_energy_api import AlertsEnergyApiClient
from time_tl.infrastructure.power_outages.base import PowerFetchError, PowerFetchErrorCode
from time_tl.infrastructure.power_outages.budget_client import BudgetedEnergyApiClient


class LocationSeedPort(Protocol):
    async def location_seed(self, telegram_user_id: int) -> PowerLocationSeed | None: ...


class AddressRequestPacer:
    """One local bot process; production requires separately reviewed shared quota admission."""

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.next_request = 0.0
        self.blocked_until = 0.0


class _AddressApiClient(AlertsEnergyApiClient):
    def __init__(self, *, api_key: SecretStr, pacer: AddressRequestPacer) -> None:
        super().__init__(api_key=api_key)
        self._pacer = pacer

    async def _get(self, path: str, *, params: Mapping[str, str] | None = None) -> object:
        async with self._pacer.lock:
            now = monotonic()
            if now < self._pacer.blocked_until:
                raise PowerFetchError(
                    PowerFetchErrorCode.RATE_LIMITED,
                    status_code=429,
                    retry_after_seconds=min(86400, self._pacer.blocked_until - now),
                )
            await asyncio.sleep(max(0, self._pacer.next_request - now))
            self._pacer.next_request = monotonic() + 0.25
            try:
                return await super()._get(path, params=params)
            except PowerFetchError as exc:
                if exc.code is PowerFetchErrorCode.RATE_LIMITED:
                    self._pacer.blocked_until = monotonic() + (
                        exc.retry_after_seconds if exc.retry_after_seconds is not None else 86400
                    )
                raise


class PostgresPowerAddressPort:
    def __init__(
        self,
        subscriptions: LocationSeedPort,
        credentials: PostgresEnergyApiCredentials,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        budget: Any | None = None,
    ) -> None:
        self._subscriptions, self._credentials, self._clock = subscriptions, credentials, clock
        self._revision: int | None = None
        self._directory: AlertsEnergyAddressDirectory | None = None
        self._lock = asyncio.Lock()
        self._pacer = AddressRequestPacer()
        self._budget = budget

    async def seed(self, telegram_user_id: int) -> PowerLocationSeed | None:
        return await self._subscriptions.location_seed(telegram_user_id)

    async def _current(self) -> AlertsEnergyAddressDirectory:
        async with self._lock:
            status = await self._credentials.status(self._clock())
            if not status.configured or status.check_status is not EnergyApiCheckStatus.VALID:
                self._directory, self._revision = None, None
                raise ValueError("power_address_api_access_unverified")
            if self._directory is None or self._revision != status.revision:
                self._directory, self._revision = None, None
                key = await self._credentials.load()
                current = await self._credentials.status(self._clock())
                if key is None or current != status:
                    raise ValueError("power_address_api_access_changed")
                expected_revision = status.revision

                async def fence() -> None:
                    latest = await self._credentials.status(self._clock())
                    if (
                        not latest.configured
                        or latest.check_status is not EnergyApiCheckStatus.VALID
                        or latest.revision != expected_revision
                        or self._revision != expected_revision
                    ):
                        raise ValueError("power_address_api_access_changed")

                self._directory = AlertsEnergyAddressDirectory(
                    BudgetedEnergyApiClient(api_key=key, budget=self._budget, before_request=fence)
                    if self._budget is not None
                    else _AddressApiClient(api_key=key, pacer=self._pacer)
                )
                self._revision = status.revision
            return self._directory

    async def settlements(
        self, seed: PowerLocationSeed, q: str = ""
    ) -> tuple[SettlementChoice, ...]:
        return await (await self._current()).settlements(seed, q)

    async def streets(
        self,
        seed: PowerLocationSeed,
        settlement: SettlementChoice,
        q: str,
    ) -> tuple[StreetChoice, ...]:
        return await (await self._current()).streets(seed, settlement, q)

    async def houses(self, street: StreetChoice) -> tuple[HouseChoice, ...]:
        return await (await self._current()).houses(street)

    async def assignments(
        self,
        seed: PowerLocationSeed,
        settlement: SettlementChoice,
        street: StreetChoice,
        house: HouseChoice,
    ) -> tuple[AssignmentChoice, ...]:
        return await (await self._current()).assignments(seed, settlement, street, house)

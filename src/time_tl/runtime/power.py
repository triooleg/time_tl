"""Cache-only Telegram/admin ports; user operations never invoke a provider."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any, cast

from pydantic import SecretStr
from sqlalchemy import select

from time_tl.admin.power import PowerAdminConflict
from time_tl.domain.power_outages.location import PowerLocationSeed
from time_tl.domain.power_outages.models import (
    PowerProvider,
    PowerScheduleView,
    PowerSourceKind,
    PowerSubscription,
    ScheduleKind,
)
from time_tl.infrastructure.db.energy_api_credentials import (
    EnergyApiCheckResult,
    EnergyApiCheckStatus,
)
from time_tl.infrastructure.db.models import User


class PostgresPowerBotPort:
    def __init__(self, factory: Any, repository: Any, *, map_reader: Any | None = None) -> None:
        self._factory = factory
        self._repository = repository
        self._map_reader = map_reader
        self.address_port: Any | None = None

    async def _existing_user(self, telegram_user_id: int) -> str:
        async with self._factory() as session:
            user_id = (
                await session.execute(
                    select(User.id).where(User.telegram_user_id == telegram_user_id)
                )
            ).scalar_one_or_none()
        if user_id is None:
            raise ValueError("power_account_missing")
        return str(user_id)

    async def subscription(self, telegram_user_id: int) -> PowerSubscription | None:
        return await self._repository.get_subscription(await self._existing_user(telegram_user_id))  # type: ignore[no-any-return]

    async def location_seed(self, telegram_user_id: int) -> PowerLocationSeed | None:
        """Read actor-owned confirmed geography without changing alert preferences."""
        from time_tl.infrastructure.db.territories import PostgresTerritoryCatalogRepository

        async with self._factory() as session:
            region_id = await session.scalar(
                select(User.confirmed_region_id).where(
                    User.telegram_user_id == telegram_user_id,
                )
            )
        if not region_id:
            return None
        catalog = PostgresTerritoryCatalogRepository(self._factory)
        try:
            path = await catalog.get_path(provider="alerts_in_ua", region_id=str(region_id))
        except LookupError:
            return None
        if not path or path[-1].region_id != region_id:
            return None
        node = path[-1]
        if node.external_code is None:
            return None
        try:
            return PowerLocationSeed(
                node.external_code,
                node.kind,
                node.name,
                node.catalog_version,
            )
        except ValueError:
            return None

    async def save(
        self,
        telegram_user_id: int,
        provider: PowerProvider,
        group_id: str,
        *,
        enabled: bool,
        outage_enabled: bool,
        restoration_enabled: bool,
        outage_lead: int,
        restoration_lead: int,
        expected_revision: int | None,
    ) -> PowerSubscription:
        user_id = await self._existing_user(telegram_user_id)
        value = PowerSubscription(
            user_id,
            provider,
            group_id,
            enabled,
            outage_enabled,
            restoration_enabled,
            outage_lead,
            restoration_lead,
            expected_revision or 1,
        )
        return await self._repository.save_subscription(value, expected_revision=expected_revision)  # type: ignore[no-any-return]

    async def day(self, telegram_user_id: int, day: date, kind: ScheduleKind) -> PowerScheduleView:
        value = await self.subscription(telegram_user_id)
        if value is None:
            raise ValueError("power_subscription_missing")
        return cast(
            PowerScheduleView,
            await (self._map_reader or self._repository).read_schedule(
                value.provider, value.group_id, day, schedule_type=kind, now=datetime.now(UTC)
            ),
        )


class AlertsEnergyAccessChecker:
    """One owner-requested read-only API check; never starts collection."""

    def __init__(self, budget: Any | None = None) -> None:
        self._budget = budget

    async def check(self, api_key: SecretStr) -> EnergyApiCheckResult:
        from time_tl.infrastructure.power_outages.alerts_energy_api import AlertsEnergyApiClient
        from time_tl.infrastructure.power_outages.base import PowerFetchError, PowerFetchErrorCode

        try:
            if self._budget is None:
                client = AlertsEnergyApiClient(api_key=api_key)
            else:
                from time_tl.infrastructure.power_outages.budget_client import (
                    BudgetedEnergyApiClient,
                )

                client = BudgetedEnergyApiClient(api_key=api_key, budget=self._budget)
            accepted = await client.check_access()
        except PowerFetchError as exc:
            if exc.code is PowerFetchErrorCode.RATE_LIMITED:
                return EnergyApiCheckResult(
                    EnergyApiCheckStatus.RATE_LIMITED,
                    retry_after_seconds=exc.retry_after_seconds
                    if exc.retry_after_seconds is not None
                    else 86400,
                )
            if exc.status_code == 401:
                return EnergyApiCheckResult(EnergyApiCheckStatus.INVALID)
            return EnergyApiCheckResult(EnergyApiCheckStatus.UNAVAILABLE)
        return EnergyApiCheckResult(
            EnergyApiCheckStatus.VALID if accepted else EnergyApiCheckStatus.INVALID
        )


class PostgresPowerAdminPort:
    def __init__(self, repository: Any) -> None:
        self._repository = repository

    async def stats(self, now: datetime) -> tuple[dict[str, object], ...]:
        return await self._repository.provider_states(now)  # type: ignore[no-any-return]

    async def set_enabled(
        self,
        provider: PowerProvider,
        enabled: bool,
        *,
        expected_revision: int,
        actor_id: str,
        now: datetime,
    ) -> int:
        try:
            result = await self._repository.set_provider_enabled(
                provider, enabled, expected_revision, actor_id, now
            )
        except ValueError as exc:
            raise PowerAdminConflict("power_configuration_changed") from exc
        return int(result["revision"])

    async def request_refresh(
        self, provider: PowerProvider, *, actor_id: str, now: datetime
    ) -> bool:
        return bool(await self._repository.request_refresh(provider, now, actor_id))


class PostgresPowerSourceAdminPort:
    """Source controls record intent and never override collection permission."""

    def __init__(self, repository: Any) -> None:
        self._repository = repository

    async def stats(self, now: datetime) -> tuple[dict[str, object], ...]:
        return cast(tuple[dict[str, object], ...], await self._repository.source_states(now))

    async def set_source_enabled(
        self,
        provider: PowerProvider,
        source_kind: PowerSourceKind,
        enabled: bool,
        *,
        expected_revision: int,
        actor_id: str,
        now: datetime,
    ) -> int:
        try:
            return int(
                await self._repository.set_source_enabled(
                    provider,
                    source_kind,
                    enabled,
                    expected_revision=expected_revision,
                    actor_id=actor_id,
                    now=now,
                )
            )
        except ValueError as exc:
            raise PowerAdminConflict("power_source_configuration_changed") from exc

    async def request_source_refresh(
        self,
        provider: PowerProvider,
        source_kind: PowerSourceKind,
        *,
        actor_id: str,
        now: datetime,
    ) -> bool:
        return bool(
            await self._repository.request_source_refresh(
                provider,
                source_kind,
                actor_id=actor_id,
                now=now,
            )
        )

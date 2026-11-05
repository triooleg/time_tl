"""Telegram map port with private actor selection and strictly cache-only views."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import select

from time_tl.domain.power_outages.location import PowerLocationSeed
from time_tl.domain.power_outages.models import (
    PowerProvider,
    PowerScheduleView,
    PowerSubscription,
    ScheduleKind,
)
from time_tl.infrastructure.db.models import User
from time_tl.infrastructure.db.power_map import PostgresPowerMapRepository
from time_tl.infrastructure.db.power_map_preferences import PostgresPowerMapPreferences

from .power_map import PowerMapScheduleReader


class PostgresMapPowerBotPort:
    def __init__(
        self, factory: Any, *, stale_after_seconds: int = 1800, notifications_enabled: bool = False
    ) -> None:
        if type(notifications_enabled) is not bool:
            raise ValueError("invalid_power_map_notification_capability")
        self.notifications_enabled = notifications_enabled
        self._factory = factory
        self._preferences = PostgresPowerMapPreferences(factory)
        self._reader = PowerMapScheduleReader(
            PostgresPowerMapRepository(factory, stale_after_seconds=stale_after_seconds)
        )
        self.address_port: Any | None = None

    async def subscription(self, actor: int) -> PowerSubscription | None:
        return await self._preferences.get(
            actor, initialize_legacy_reminders=self.notifications_enabled
        )

    async def save(
        self,
        actor: int,
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
        # API map selections never enter the official notification store/outbox.
        if any(type(flag) is not bool for flag in (enabled, outage_enabled, restoration_enabled)):
            raise ValueError("invalid_power_map_preference")
        if not self.notifications_enabled and (enabled or outage_enabled or restoration_enabled):
            raise ValueError("power_api_notifications_unavailable")
        return await self._preferences.save(
            actor,
            provider,
            group_id,
            expected_revision=expected_revision,
            enabled=enabled,
            outage_enabled=outage_enabled,
            restoration_enabled=restoration_enabled,
            outage_lead=outage_lead,
            restoration_lead=restoration_lead,
        )

    async def day(self, actor: int, day: date, kind: ScheduleKind) -> PowerScheduleView:
        value = await self.subscription(actor)
        if value is None:
            raise ValueError("power_subscription_missing")
        return await self._reader.read_schedule(
            value.provider, value.group_id, day, schedule_type=kind, now=datetime.now(UTC),
            display_only=True,
        )

    async def location_seed(self, actor: int) -> PowerLocationSeed | None:
        from time_tl.infrastructure.db.territories import PostgresTerritoryCatalogRepository

        async with self._factory() as session:
            region_id = await session.scalar(
                select(User.confirmed_region_id).where(User.telegram_user_id == actor)
            )
        if not region_id:
            return None
        try:
            path = await PostgresTerritoryCatalogRepository(self._factory).get_path(
                provider="alerts_in_ua", region_id=str(region_id)
            )
        except LookupError:
            return None
        if not path or path[-1].region_id != region_id or path[-1].external_code is None:
            return None
        node = path[-1]
        if node.external_code is None:
            return None
        return PowerLocationSeed(node.external_code, node.kind, node.name, node.catalog_version)


def build_power_map_bot_port(
    factory: Any,
    owner_seed: Any,
    *,
    address_lookup_enabled: bool = True,
    notifications_enabled: bool = False,
) -> PostgresMapPowerBotPort:
    from time_tl.infrastructure.db.energy_api_budget import PostgresEnergyApiBudget
    from time_tl.infrastructure.db.energy_api_credentials import PostgresEnergyApiCredentials
    from time_tl.runtime.power_address import PostgresPowerAddressPort

    port = PostgresMapPowerBotPort(factory, notifications_enabled=notifications_enabled)
    if address_lookup_enabled:
        port.address_port = PostgresPowerAddressPort(
            port,
            PostgresEnergyApiCredentials(factory, owner_seed),
            budget=PostgresEnergyApiBudget(factory),
        )
    return port

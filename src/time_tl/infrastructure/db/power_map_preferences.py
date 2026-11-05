"""Actor-owned map selection, separate from notification authority; schema9-safe."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from time_tl.domain.power_outages.models import PowerProvider, PowerSubscription, validate_lead

from .models import RuntimeSetting, User

PREFIX = "power_api_user_pref:"
SUPPORTED = frozenset({PowerProvider.DTEK, PowerProvider.CEK})


def preference_key(user_id: str) -> str:
    return PREFIX + str(UUID(user_id))


def decode(value: object, user_id: str) -> PowerSubscription:
    common = {"version", "provider", "group", "revision"}
    notification_fields = {
        "enabled",
        "outage_enabled",
        "restoration_enabled",
        "outage_lead",
        "restoration_lead",
    }
    if (
        type(value) is not dict
        or set(value) != common | (notification_fields if value.get("version") == 2 else set())
        or type(value["version"]) is not int
        or value["version"] not in {1, 2}
        or type(value["revision"]) is not int
        or not 1 <= value["revision"] < 2**31
        or type(value["provider"]) is not str
        or type(value["group"]) is not str
    ):
        raise ValueError("invalid_power_map_preference")
    provider = PowerProvider(value["provider"])
    if provider not in SUPPORTED:
        raise ValueError("unsupported_power_map_provider")
    if value["version"] == 2:
        if any(
            type(value[key]) is not bool
            for key in ("enabled", "outage_enabled", "restoration_enabled")
        ):
            raise ValueError("invalid_power_map_preference")
        return PowerSubscription(
            user_id,
            provider,
            value["group"],
            value["enabled"],
            value["outage_enabled"],
            value["restoration_enabled"],
            validate_lead(value["outage_lead"]),
            validate_lead(value["restoration_lead"]),
            revision=value["revision"],
        )
    return PowerSubscription(
        user_id, provider, value["group"], False, False, False, 10, 10, revision=value["revision"]
    )


class PostgresPowerMapPreferences:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    async def get(
        self, actor: int, *, initialize_legacy_reminders: bool = False
    ) -> PowerSubscription | None:
        if type(initialize_legacy_reminders) is not bool:
            raise ValueError("invalid_power_map_preference")
        if type(actor) is not int or actor <= 0:
            raise ValueError("invalid_power_actor")
        async with self._factory() as session, session.begin():
            identifier = await session.scalar(
                select(User.id).where(User.telegram_user_id == actor).with_for_update(read=True)
            )
            if identifier is None:
                raise ValueError("power_account_missing")
            row = await session.get(RuntimeSetting, preference_key(str(identifier)))
            if row is None:
                return None
            result = decode(row.value, str(identifier))
            if initialize_legacy_reminders and row.value["version"] == 1:
                # Dormant defaults only: no persistence and master remains OFF.
                # Explicit confirmation is still required before any delivery.
                result = replace(
                    result,
                    outage_notifications_enabled=True,
                    restoration_notifications_enabled=True,
                )
            return result

    async def save(
        self,
        actor: int,
        provider: PowerProvider,
        group: str,
        *,
        expected_revision: int | None,
        enabled: bool = False,
        outage_enabled: bool = False,
        restoration_enabled: bool = False,
        outage_lead: int = 10,
        restoration_lead: int = 10,
    ) -> PowerSubscription:
        if (
            type(actor) is not int
            or actor <= 0
            or not isinstance(provider, PowerProvider)
            or provider not in SUPPORTED
            or expected_revision is not None
            and (type(expected_revision) is not int or not 1 <= expected_revision < 2**31 - 1)
        ):
            raise ValueError("invalid_power_map_selection")
        if any(type(flag) is not bool for flag in (enabled, outage_enabled, restoration_enabled)):
            raise ValueError("invalid_power_map_preference")
        validate_lead(outage_lead)
        validate_lead(restoration_lead)
        async with self._factory() as session, session.begin():
            identifier = await session.scalar(
                select(User.id).where(User.telegram_user_id == actor).with_for_update()
            )
            if identifier is None:
                raise ValueError("power_account_missing")
            key, user_id = preference_key(str(identifier)), str(identifier)
            row = await session.get(RuntimeSetting, key, with_for_update=True)
            current = decode(row.value, user_id) if row is not None else None
            if (current.revision if current else None) != expected_revision:
                raise ValueError("power_configuration_changed")
            revision = current.revision + 1 if current else 1
            result = PowerSubscription(
                user_id,
                provider,
                group,
                enabled,
                outage_enabled,
                restoration_enabled,
                outage_lead,
                restoration_lead,
                revision=revision,
            )
            value = {
                "version": 2,
                "provider": result.provider.value,
                "group": result.group_id,
                "revision": result.revision,
                "enabled": result.enabled,
                "outage_enabled": result.outage_notifications_enabled,
                "restoration_enabled": result.restoration_notifications_enabled,
                "outage_lead": result.outage_lead_minutes,
                "restoration_lead": result.restoration_lead_minutes,
            }
            if row is None:
                session.add(RuntimeSetting(key=key, value=value, updated_by="power-map-user"))
            else:
                row.value, row.updated_at = value, datetime.now(UTC)
            return result

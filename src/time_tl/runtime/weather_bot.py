"""Weather bot composition: exact approved territory binding, local PostgreSQL only."""

from __future__ import annotations

from dataclasses import fields
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, select

from time_tl.bot.weather_router import WeatherContext, WeatherUiPreferences
from time_tl.domain.weather import ForecastSnapshot
from time_tl.infrastructure.db.geographic_coverage import _weather_point
from time_tl.infrastructure.db.models import (
    ActiveTerritoryCatalog,
    Territory,
    User,
    WeatherLocation,
)
from time_tl.infrastructure.db.weather_configuration import PostgresWeatherConfiguration


class PostgresWeatherBotPort:
    def __init__(
        self,
        factory: Any,
        preferences: Any,
        storage: Any,
        *,
        notifications_enabled: bool = False,
        authority_guard: Any = None,
    ) -> None:
        if type(notifications_enabled) is not bool:
            raise ValueError("invalid_weather_capability")
        self.factory = factory
        self.preference_store = preferences
        self.storage = storage
        self.notifications_enabled = notifications_enabled
        self.authority_guard = authority_guard

    async def context(self, actor: int) -> WeatherContext:
        async with self.factory() as session:
            if self.authority_guard is not None:
                await self.authority_guard(session, datetime.now(UTC))
            exists = await session.scalar(select(User.id).where(User.telegram_user_id == actor))
            if exists is None:
                raise ValueError("weather_account_missing")
            row = (
                await session.execute(
                    select(WeatherLocation)
                    .select_from(User)
                    .join(
                        Territory,
                        and_(
                            Territory.region_id == User.confirmed_region_id,
                            Territory.provider == "alerts.in.ua",
                            Territory.kind.in_(("HROMADA", "SPECIAL_STATUS_CITY")),
                            Territory.is_selectable.is_(True),
                        ),
                    )
                    .join(
                        ActiveTerritoryCatalog,
                        and_(
                            ActiveTerritoryCatalog.provider == Territory.provider,
                            ActiveTerritoryCatalog.catalog_version == Territory.catalog_version,
                        ),
                    )
                    .join(
                        WeatherLocation,
                        and_(
                            WeatherLocation.katottg_code == Territory.external_code,
                            WeatherLocation.region_id == Territory.region_id,
                            WeatherLocation.enabled.is_(True),
                            WeatherLocation.approved_at.is_not(None),
                            WeatherLocation.coverage_type == "BASE",
                        ),
                    )
                    .where(User.telegram_user_id == actor)
                )
            ).scalar_one_or_none()
        configuration = await PostgresWeatherConfiguration(self.factory).load()
        collection_enabled = configuration.enabled and configuration.terms_approved
        if row is None or not _weather_point(row).approved():
            return WeatherContext("", False, collection_enabled=collection_enabled)
        return WeatherContext(row.locality_name, True, str(row.id), collection_enabled)

    @staticmethod
    def _view(value: Any) -> WeatherUiPreferences:
        values: dict[str, Any] = {
            field.name: getattr(value, field.name, field.default)
            for field in fields(WeatherUiPreferences)
        }
        return WeatherUiPreferences(**values)

    async def preferences(self, actor: int) -> WeatherUiPreferences:
        return self._view(await self.preference_store.get(actor))

    async def snapshot(self, actor: int) -> ForecastSnapshot | None:
        context = await self.context(actor)
        preferences = await self.preferences(actor)
        if not context.has_precise_point or not preferences.forecast_requested:
            return None
        result = await self.storage.previous(context.point_id)
        if result is not None and not isinstance(result, ForecastSnapshot):
            raise ValueError("invalid_weather_cache")
        return result

    async def request_forecast(self, actor: int) -> WeatherUiPreferences:
        context = await self.context(actor)
        if not context.has_precise_point:
            raise ValueError("weather_point_not_approved")
        saved = self._view(await self.preference_store.request_forecast(actor))
        if not saved.forecast_requested:
            raise ValueError("weather_forecast_request_not_saved")
        return saved

    async def save_notifications(
        self, actor: int, enabled: bool, expected_revision: int
    ) -> WeatherUiPreferences:
        if enabled and not self.notifications_enabled:
            raise ValueError("weather_delivery_unavailable")
        return self._view(
            await self.preference_store.toggle_notifications(
                actor, enabled, expected_revision=expected_revision
            )
        )

    async def disable_weather(self, actor: int, expected_revision: int) -> WeatherUiPreferences:
        return self._view(
            await self.preference_store.disable_weather(actor, expected_revision=expected_revision)
        )

    async def save_settings(
        self, actor: int, expected_revision: int, **changes: object
    ) -> WeatherUiPreferences:
        allowed = {field.name for field in fields(WeatherUiPreferences)} - {
            "forecast_requested",
            "weather_enabled",
            "revision",
        }
        if not changes or set(changes) - allowed:
            raise ValueError("invalid_weather_preferences")
        return self._view(
            await self.preference_store.update(
                actor, expected_revision=expected_revision, **changes
            )
        )


def build_weather_bot_port(
    factory: Any, *, notifications_enabled: bool = False, authority_guard: Any = None
) -> PostgresWeatherBotPort:
    from time_tl.infrastructure.db.weather import (
        PostgresWeatherPreferences,
        PostgresWeatherStorage,
    )

    return PostgresWeatherBotPort(
        factory,
        PostgresWeatherPreferences(factory, authority_guard=authority_guard),
        PostgresWeatherStorage(factory, authority_guard=authority_guard),
        notifications_enabled=notifications_enabled,
        authority_guard=authority_guard,
    )

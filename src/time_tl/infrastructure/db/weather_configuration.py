"""Owner-controlled weather configuration; disabled until explicitly configured.

No API key is required for MET. Existing alert and energy credential stores
remain the only owners of those secrets.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from time_tl.infrastructure.weather.met_norway import MetNorwaySettings, WeatherProviderError

from .models import AuditLog, RuntimeSetting

SETTING = "weather_runtime_configuration"
CHECK_SETTING = "weather_met_connection_check"
SYNC_SETTING = "weather_manual_sync_request"


class WeatherConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    revision: int = Field(default=0, ge=0, lt=2**31 - 1)
    enabled: bool = False
    cycles: Literal[4, 6] = 4
    terms_approved: bool = False
    user_agent: str = Field(
        default="TimeTLWeather/1 (+https://github.com/triooleg/time_tl)",
        min_length=1,
        max_length=200,
    )
    contact: str = Field(default="https://github.com/triooleg/time_tl", max_length=200)
    forecast_validity_hours: int = Field(default=8, ge=1, le=12)
    unused_cache_days: int = Field(default=7, ge=1, le=90)
    snapshot_history_days: int = Field(default=14, ge=1, le=90)
    aggregate_history_days: int = Field(default=90, ge=14, le=730)
    rain_mm: float = Field(default=1.0, ge=0.1, le=100)
    rain_probability: float = Field(default=70.0, ge=1, le=100)
    gust_ms: float = Field(default=15.0, ge=1, le=100)
    temperature_change_c: float = Field(default=8.0, ge=1, le=50)

    def met_settings(self) -> MetNorwaySettings:
        return MetNorwaySettings(
            enabled=self.enabled,
            terms_approved=self.terms_approved,
            user_agent=self.user_agent,
            contact=self.contact,
            forecast_validity_hours=self.forecast_validity_hours,
        )

    @model_validator(mode="after")
    def validate_contact(self) -> WeatherConfiguration:
        if any(ord(c) < 32 or ord(c) == 127 for c in self.contact + self.user_agent):
            raise ValueError("invalid_weather_contact")
        if self.enabled:
            try:
                self.met_settings().validate()
            except WeatherProviderError:
                raise ValueError("invalid_weather_configuration") from None
        return self


class PostgresWeatherConfiguration:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        *,
        authority_guard: Callable[[AsyncSession, datetime], Awaitable[None]] | None = None,
    ) -> None:
        self.factory = factory
        self.authority_guard = authority_guard

    async def _authorize(self, session: AsyncSession, now: datetime) -> None:
        if self.authority_guard is not None:
            await self.authority_guard(session, now)

    async def load(self) -> WeatherConfiguration:
        async with self.factory() as session:
            await self._authorize(session, datetime.now(UTC))
            row = await session.get(RuntimeSetting, SETTING)
            return WeatherConfiguration.model_validate(row.value if row else {})

    async def save(
        self, value: WeatherConfiguration, *, expected_revision: int, actor: str, now: datetime
    ) -> WeatherConfiguration:
        if now.tzinfo is None or not actor or len(actor) > 80:
            raise ValueError("invalid_weather_change")
        async with self.factory() as session, session.begin():
            await self._authorize(session, now)
            await session.execute(
                insert(RuntimeSetting)
                .values(key=SETTING, value=WeatherConfiguration().model_dump(), updated_by=actor)
                .on_conflict_do_nothing(index_elements=[RuntimeSetting.key])
            )
            row = (
                await session.execute(
                    select(RuntimeSetting).where(RuntimeSetting.key == SETTING).with_for_update()
                )
            ).scalar_one()
            previous = WeatherConfiguration.model_validate(row.value)
            if previous.revision != expected_revision:
                raise ValueError("weather_configuration_changed")
            updated = value.model_copy(update={"revision": previous.revision + 1})
            row.value, row.updated_by, row.updated_at = updated.model_dump(), actor, now
            session.add(
                AuditLog(
                    actor=actor,
                    action="weather_configuration_saved",
                    result="RECORDED",
                    target_type="weather_configuration",
                    target_id=SETTING,
                    redacted_details={"revision": updated.revision},
                )
            )
            await self._authorize(session, now)
            return updated

    async def claim_diagnostic(self, *, revision: int, now: datetime) -> bool:
        """Persist cooldown BEFORE HTTP so concurrent clicks/restarts cannot multiply calls."""
        async with self.factory() as session, session.begin():
            await self._authorize(session, now)
            await session.execute(
                insert(RuntimeSetting)
                .values(key=CHECK_SETTING, value={}, updated_by="system")
                .on_conflict_do_nothing(index_elements=[RuntimeSetting.key])
            )
            row = (
                await session.execute(
                    select(RuntimeSetting)
                    .where(RuntimeSetting.key == CHECK_SETTING)
                    .with_for_update()
                )
            ).scalar_one()
            config = await session.get(RuntimeSetting, SETTING)
            if (
                WeatherConfiguration.model_validate(config.value if config else {}).revision
                != revision
            ):
                return False
            if row.value.get("next_check_at"):
                if datetime.fromisoformat(str(row.value["next_check_at"])) > now:
                    return False
            row.value = {
                "revision": revision,
                "status": "UNAVAILABLE",
                "checked_at": now.isoformat(),
                "next_check_at": (now + timedelta(seconds=60)).isoformat(),
            }
            await self._authorize(session, now)
            return True

    async def record_diagnostic(
        self, *, revision: int, status: str, now: datetime, point_id: str | None = None
    ) -> None:
        if status not in {
            "CONNECTED",
            "AUTH_FAILED",
            "RATE_LIMITED",
            "UNAVAILABLE",
            "INVALID_RESPONSE",
            "NOT_CONFIGURED",
        }:
            raise ValueError("invalid_diagnostic_status")
        async with self.factory() as session, session.begin():
            await self._authorize(session, now)
            config = await session.get(RuntimeSetting, SETTING, with_for_update=True)
            if (
                WeatherConfiguration.model_validate(config.value if config else {}).revision
                != revision
            ):
                raise ValueError("weather_configuration_changed")
            if point_id is not None:
                from uuid import UUID

                from .weather import _lock_demand

                await _lock_demand(session, UUID(point_id), now)
            row = await session.get(RuntimeSetting, CHECK_SETTING, with_for_update=True)
            if row is not None and row.value.get("revision") == revision:
                row.value = {**row.value, "status": status, "checked_at": now.isoformat()}
            await self._authorize(session, now)

    async def diagnostic_status(self) -> dict[str, object]:
        async with self.factory() as session:
            row = await session.get(RuntimeSetting, CHECK_SETTING)
            # Explicit projection never returns arbitrary RuntimeSetting values.
            value = row.value if row else {}
            return {key: value.get(key) for key in ("status", "checked_at", "revision")}

    async def request_sync(self, *, expected_revision: int, actor: str, now: datetime) -> str:
        async with self.factory() as session, session.begin():
            await self._authorize(session, now)
            config_row = await session.get(RuntimeSetting, SETTING, with_for_update={"read": True})
            config = WeatherConfiguration.model_validate(config_row.value if config_row else {})
            if (
                config.revision != expected_revision
                or not config.enabled
                or not config.terms_approved
            ):
                raise ValueError("weather_configuration_changed")
            await session.execute(
                insert(RuntimeSetting)
                .values(key=SYNC_SETTING, value={}, updated_by=actor)
                .on_conflict_do_nothing(index_elements=[RuntimeSetting.key])
            )
            row = await session.get(RuntimeSetting, SYNC_SETTING, with_for_update=True)
            assert row is not None
            if (
                row.value.get("next_request_at")
                and datetime.fromisoformat(str(row.value["next_request_at"])) > now
            ):
                raise ValueError("weather_sync_deferred")
            request_id = str(uuid4())
            row.value = {
                "version": 1,
                "request_id": request_id,
                "requested_at": now.isoformat(),
                "completed_at": None,
                "configuration_revision": config.revision,
                "next_request_at": (now + timedelta(minutes=1)).isoformat(),
            }
            row.updated_by, row.updated_at = actor, now
            session.add(
                AuditLog(
                    actor=actor,
                    action="weather_sync_requested",
                    result="QUEUED",
                    target_type="weather_configuration",
                    target_id=SETTING,
                    redacted_details={"revision": config.revision},
                )
            )
            await self._authorize(session, now)
            return request_id

    async def pending_sync(self) -> str | None:
        async with self.factory() as session:
            row = await session.get(RuntimeSetting, SYNC_SETTING)
            if row is None or row.value.get("completed_at") is not None:
                return None
            config = await session.get(RuntimeSetting, SETTING)
            if (
                row.value.get("configuration_revision")
                != WeatherConfiguration.model_validate(config.value if config else {}).revision
            ):
                return None
            value = row.value.get("request_id")
            if type(value) is not str or str(UUID(value)) != value:
                raise ValueError("invalid_weather_sync_request")
            return value

    async def complete_sync(self, request_id: str) -> None:
        async with self.factory() as session, session.begin():
            await self._authorize(session, datetime.now(UTC))
            row = await session.get(RuntimeSetting, SYNC_SETTING, with_for_update=True)
            if row is not None and row.value.get("request_id") == request_id:
                row.value = {**row.value, "completed_at": datetime.now(UTC).isoformat()}
            await self._authorize(session, datetime.now(UTC))

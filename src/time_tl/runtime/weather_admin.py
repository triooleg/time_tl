"""Aggregated owner weather/coverage views and a single bounded MET diagnostic."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import func, select

from time_tl.domain.weather import ForecastStatus, WeatherPoint, next_weather_collection
from time_tl.infrastructure.db.geographic_coverage import PostgresActiveGeographicCoverage
from time_tl.infrastructure.db.models import (
    OutboxMessage,
    WeatherChangeEvent,
    WeatherForecastHour,
    WeatherForecastRun,
    WeatherForecastSnapshot,
)
from time_tl.infrastructure.db.weather import PostgresWeatherStorage
from time_tl.infrastructure.db.weather_configuration import (
    PostgresWeatherConfiguration,
    WeatherConfiguration,
)
from time_tl.infrastructure.weather.met_norway import (
    ENDPOINT,
    WeatherProviderError,
    _coordinate,
    _header_date,
    normalize_met,
)


class WeatherDiagnosticDeferred(ValueError):
    pass


class WeatherAdminPort:
    def __init__(
        self,
        factory: Any,
        *,
        client_factory: Any = httpx.AsyncClient,
        storage_factory: Any = PostgresWeatherStorage,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        authority_guard: Any = None,
    ) -> None:
        self.factory, self.client_factory = factory, client_factory
        self.configuration = PostgresWeatherConfiguration(factory, authority_guard=authority_guard)
        self.coverage = PostgresActiveGeographicCoverage(factory)
        self.storage_factory, self.clock = storage_factory, clock
        self.authority_guard = authority_guard

    async def status(self, now: datetime) -> dict[str, Any]:
        config = await self.configuration.load()
        coverage = await self.coverage.snapshot(now)
        async with self.factory() as session:
            latest = (
                await session.execute(
                    select(WeatherForecastRun)
                    .order_by(WeatherForecastRun.started_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            latest_points = (
                select(WeatherForecastSnapshot)
                .distinct(WeatherForecastSnapshot.weather_location_id)
                .where(
                    WeatherForecastSnapshot.provider == "MET Norway",
                    WeatherForecastSnapshot.weather_location_id.in_(
                        [UUID(item.location.location_id) for item in coverage.weather_points]
                    ),
                )
            )
            latest_point_rows = latest_points.order_by(
                WeatherForecastSnapshot.weather_location_id,
                WeatherForecastSnapshot.fetched_at.desc(),
            ).subquery()
            future_hours = (
                select(WeatherForecastHour.snapshot_id)
                .where(
                    WeatherForecastHour.snapshot_id == latest_point_rows.c.id,
                    WeatherForecastHour.forecast_at >= now,
                )
                .exists()
            )
            fresh = (
                await session.execute(
                    select(func.count())
                    .select_from(latest_point_rows)
                    .where(
                        latest_point_rows.c.expires_at > now,
                        latest_point_rows.c.provider_updated_at >= now - timedelta(hours=24),
                        latest_point_rows.c.provider_updated_at <= now + timedelta(minutes=5),
                        latest_point_rows.c.fetched_at <= now,
                        latest_point_rows.c.status == ForecastStatus.FRESH.value,
                        future_hours,
                    )
                )
            ).scalar_one()
            changes = (
                await session.execute(
                    select(func.count())
                    .select_from(WeatherChangeEvent)
                    .where(WeatherChangeEvent.expected_end > now)
                )
            ).scalar_one()
            pending_notifications = (
                await session.execute(
                    select(func.count())
                    .select_from(OutboxMessage)
                    .where(
                        OutboxMessage.aggregate_type == "weather",
                        OutboxMessage.delivered_at.is_(None),
                        OutboxMessage.dead_at.is_(None),
                    )
                )
            ).scalar_one()
        oblasts = {item.oblast.region_id for item in coverage.territories}
        return {
            "active_source": "MET Norway" if config.enabled else None,
            "next_scheduled_sync": next_weather_collection(now, config.cycles).isoformat()
            if config.enabled
            else None,
            "manual_sync_pending": bool(await self.configuration.pending_sync()),
            "detected_events": int(changes),
            "pending_notifications": int(pending_notifications),
            "configuration": config.model_dump(),
            "diagnostic": await self.configuration.diagnostic_status(),
            "coverage": {
                "observed_at": coverage.observed_at.isoformat(),
                "oblast_count": len(oblasts),
                "hromada_count": sum(item.hromada is not None for item in coverage.territories),
                "weather_point_count": len(coverage.weather_points),
                "power_group_count": len(coverage.power_groups),
                "subscriber_count": coverage.active_user_count,
                "requested_user_count": coverage.requested_user_count,
                "pending_weather_points": max(0, len(coverage.weather_points) - int(fresh)),
                "exclusions": [
                    {"reason": item.reason, "count": item.user_count} for item in coverage.excluded
                ],
                "territories": [
                    {
                        "katottg": item.territory.katottg,
                        "name": item.territory.name,
                        "oblast": item.oblast.name,
                        "raion": item.raion.name if item.raion else None,
                        "hromada": item.hromada.name if item.hromada else None,
                        "users": item.user_count,
                        "alerts": item.alert_user_count,
                        "power": item.power_user_count,
                        "weather": item.weather_user_count,
                        "changed_at": item.last_subscription_change.isoformat(),
                    }
                    for item in coverage.territories
                ],
                "power_groups": [
                    {
                        "provider": item.provider.value,
                        "group": item.group_id,
                        "users": item.user_count,
                    }
                    for item in coverage.power_groups
                ],
            },
            "last_run": None
            if latest is None
            else {
                "started_at": latest.started_at.isoformat(),
                "completed_at": latest.completed_at.isoformat() if latest.completed_at else None,
                "status": latest.status,
                "successful": latest.successful_locations,
                "failed": latest.failed_locations,
                "duration_seconds": (latest.completed_at - latest.started_at).total_seconds()
                if latest.completed_at
                else None,
            },
        }

    async def save(
        self, payload: dict[str, Any], *, expected_revision: int, actor: str, now: datetime
    ) -> None:
        await self.configuration.save(
            WeatherConfiguration.model_validate(payload),
            expected_revision=expected_revision,
            actor=actor,
            now=now,
        )

    async def request_sync(self, *, revision: int, actor: str, now: datetime) -> str:
        if not (await self.coverage.snapshot(now)).weather_points:
            raise ValueError("weather_points_not_configured")
        return await self.configuration.request_sync(
            expected_revision=revision, actor=actor, now=now
        )

    async def check(self, *, revision: int, now: datetime) -> str:
        config = await self.configuration.load()
        if config.revision != revision:
            raise ValueError("weather_configuration_changed")
        config.met_settings().validate()
        coverage = await self.coverage.snapshot(now)
        if not coverage.weather_points:
            return "NOT_CONFIGURED"
        if not await self.configuration.claim_diagnostic(revision=revision, now=now):
            raise WeatherDiagnosticDeferred("weather_diagnostic_deferred")
        location = coverage.weather_points[0].location
        point = WeatherPoint(location.location_id, location.latitude, location.longitude)
        store = self.storage_factory(
            self.factory,
            expected_configuration_revision=revision,
            clock=self.clock,
            authority_guard=self.authority_guard,
        )
        try:
            await store.claim_source_request(point, self.clock())
        except WeatherProviderError as error:
            if error.code == "REQUEST_DEFERRED":
                raise WeatherDiagnosticDeferred("weather_diagnostic_deferred") from None
            raise
        result = "UNAVAILABLE"
        try:
            # One request, no retries, redirects, credentials, callback or background sync.
            async with self.client_factory(
                timeout=10, follow_redirects=False, trust_env=False
            ) as client:
                await store.authorize_source_request(point, self.clock())
                async with client.stream(
                    "GET",
                    ENDPOINT,
                    params={
                        "lat": _coordinate(point.latitude),
                        "lon": _coordinate(point.longitude),
                    },
                    headers={"User-Agent": config.user_agent + " " + config.contact},
                ) as response:
                    received_at = self.clock()
                    expires = _header_date(
                        response.headers.get("Expires"), received_at + timedelta(hours=1)
                    )
                    await store.record_source_response(
                        point, expires if response.status_code in {200, 304} else None, received_at
                    )
                    result = {401: "AUTH_FAILED", 403: "AUTH_FAILED", 429: "RATE_LIMITED"}.get(
                        response.status_code, "UNAVAILABLE"
                    )
                    if response.status_code == 429:
                        try:
                            delay = float(response.headers.get("Retry-After", "60"))
                        except ValueError:
                            delay = 60.0
                        delay = min(86400.0, max(1.0, delay))
                        await store.record_source_retry(
                            "MET Norway", received_at + timedelta(seconds=delay)
                        )
                    if response.status_code == 200:
                        data = bytearray()
                        async for chunk in response.aiter_bytes():
                            data.extend(chunk)
                            if len(data) > 2_000_000:
                                raise ValueError("weather_response_too_large")
                        result = "INVALID_RESPONSE"
                        normalized = normalize_met(
                            bytes(data),
                            point,
                            received_at,
                            expires,
                            forecast_validity_hours=config.forecast_validity_hours,
                        )
                        if normalized.usable(received_at):
                            result = "CONNECTED"
        except (httpx.HTTPError, ValueError, WeatherProviderError):
            pass
        await self.configuration.record_diagnostic(
            revision=revision, status=result, now=self.clock(), point_id=point.point_id
        )
        return result

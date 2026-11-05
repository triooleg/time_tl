"""Restart-safe read model of actual subscriptions; no registry, cache or catalog writes."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import String, and_, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from time_tl.domain.coverage import (
    ALERT_PROVIDER,
    ActiveGeographicCoverage,
    CoverageAlertScope,
    CoveragePowerSelection,
    CoverageSubscriber,
    CoverageTerritoryNode,
    CoverageWeatherLocation,
    WeatherSourceReference,
    derive_active_coverage,
)
from time_tl.domain.power_outages.models import aware

from .models import (
    ActiveTerritoryCatalog,
    AlertScope,
    RuntimeSetting,
    Territory,
    TerritoryCatalogVersion,
    User,
    UserWeatherPreference,
    WeatherLocation,
)
from .power_map_preferences import PREFIX, decode


def _subscriber_query() -> Any:
    """Batch projections deliberately omit Telegram IDs, addresses and credentials."""
    return (
        select(
            User.id.label("user_key"),
            User.confirmed_region_id.label("confirmed_region_id"),
            User.mode.label("mode"),
            User.alerts_enabled.label("alerts_enabled"),
            User.updated_at.label("changed_at"),
            AlertScope.territory_provider.label("scope_provider"),
            AlertScope.territory_catalog_version.label("scope_version"),
            AlertScope.selected_region_id.label("scope_selected"),
            AlertScope.applicable_region_ids.label("scope_regions"),
            RuntimeSetting.value.label("power_preference"),
            RuntimeSetting.key.label("power_preference_key"),
            RuntimeSetting.updated_at.label("power_changed_at"),
            UserWeatherPreference.weather_enabled.label("weather_enabled"),
            UserWeatherPreference.forecast_requested.label("forecast_requested"),
            UserWeatherPreference.updated_at.label("weather_changed_at"),
        )
        .select_from(User)
        .outerjoin(AlertScope, AlertScope.user_id == User.id)
        .outerjoin(
            RuntimeSetting,
            RuntimeSetting.key == PREFIX + User.id.cast(String),
        )
        .outerjoin(UserWeatherPreference, UserWeatherPreference.user_id == User.id)
        .where(
            or_(
                and_(
                    User.alerts_enabled.is_(True),
                    User.mode.in_(("LESSONS_AND_ALERTS", "ALERTS_ONLY")),
                ),
                RuntimeSetting.key.is_not(None),
                UserWeatherPreference.weather_enabled.is_(True),
                UserWeatherPreference.forecast_requested.is_(True),
            )
        )
        .order_by(User.id)
    )


def _subscribers(rows: Sequence[Mapping[str, Any]]) -> tuple[CoverageSubscriber, ...]:
    result = []
    for row in rows:
        key = str(row["user_key"])
        issues: list[str] = []
        power = None
        if row["power_preference_key"] is not None:
            try:
                preference = decode(row["power_preference"], key)
                power = CoveragePowerSelection(
                    preference.provider, preference.group_id, preference.enabled
                )
            except (ValueError, KeyError, TypeError, RecursionError):
                issues.append("INVALID_POWER_PREFERENCE")
        scope = None
        raw_regions = row["scope_regions"]
        if (
            type(raw_regions) is list
            and all(type(item) is str for item in raw_regions)
            and type(row["scope_selected"]) is str
        ):
            scope = CoverageAlertScope(
                row["scope_provider"],
                row["scope_version"],
                row["scope_selected"],
                tuple(raw_regions),
            )
        weather = row["weather_enabled"]
        requested = row["forecast_requested"]
        if (
            weather is not None
            and type(weather) is not bool
            or requested is not None
            and type(requested) is not bool
        ):
            issues.append("INVALID_WEATHER_PREFERENCE")
        result.append(
            CoverageSubscriber(
                key,
                row["confirmed_region_id"],
                row["mode"],
                row["alerts_enabled"] is True,
                aware(row["changed_at"]),
                scope,
                power,
                row["power_changed_at"],
                weather is True,
                requested is True,
                row["weather_changed_at"],
                tuple(issues),
            )
        )
    return tuple(result)


def _weather_point(row: WeatherLocation) -> CoverageWeatherLocation:
    source = (
        WeatherSourceReference(
            row.provenance_url, row.provenance_sha256, row.provenance_version, row.approved_at
        )
        if row.approved_at is not None
        else None
    )
    return CoverageWeatherLocation(
        str(row.id),
        row.katottg_code,
        row.region_id,
        row.locality_name,
        row.latitude,
        row.longitude,
        row.coverage_type,
        row.enabled,
        source,
    )


class PostgresActiveGeographicCoverage:
    """One consistent read snapshot, reconstructed after every process restart.

    The desired work set, rather than notifications or full alert polling, is
    selected here. Callers must recheck current demand at cache commit and final
    dispatch; a previous snapshot does not authorize a later send.
    """

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    async def snapshot(self, now: datetime) -> ActiveGeographicCoverage:
        clock = aware(now)
        async with self._factory() as session, session.begin():
            # SET affects this transaction only, before any application query.
            # Concurrent account/scope/preference changes are seen all-or-none.
            await session.execute(
                text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            )
            return await read_coverage_in_session(session, clock)


async def read_coverage_in_session(
    session: AsyncSession,
    now: datetime,
) -> ActiveGeographicCoverage:
    """Read within a caller-owned transaction, without changing its isolation/mode.

    A writer must establish its demand/source locks before calling this helper;
    this projection alone is not a cache-commit or notification authorization.
    """
    clock = aware(now)
    version = (
        await session.execute(
            select(ActiveTerritoryCatalog.catalog_version)
            .join(
                TerritoryCatalogVersion,
                and_(
                    TerritoryCatalogVersion.provider == ActiveTerritoryCatalog.provider,
                    TerritoryCatalogVersion.catalog_version
                    == ActiveTerritoryCatalog.catalog_version,
                ),
            )
            .where(
                ActiveTerritoryCatalog.provider == ALERT_PROVIDER,
                TerritoryCatalogVersion.status == "ACTIVE",
            )
        )
    ).scalar_one_or_none()
    catalog: list[CoverageTerritoryNode] = []
    if version is not None:
        catalog_rows = (
            (
                await session.execute(
                    select(Territory)
                    .where(
                        Territory.provider == ALERT_PROVIDER,
                        Territory.catalog_version == version,
                    )
                    .order_by(Territory.region_id)
                )
            )
            .scalars()
            .all()
        )
        catalog = [
            CoverageTerritoryNode(
                row.region_id,
                row.external_code,
                row.name,
                row.kind,
                row.parent_region_id,
                row.is_selectable,
            )
            for row in catalog_rows
        ]
    subscribers = _subscribers(
        [dict(row) for row in (await session.execute(_subscriber_query())).mappings().all()]
    )
    initial = derive_active_coverage(
        clock, catalog_version=version, territories=tuple(catalog), subscribers=subscribers
    )
    codes = {
        item.territory.katottg
        for item in initial.territories
        if item.weather_user_count and item.territory.katottg is not None
    }
    weather: tuple[CoverageWeatherLocation, ...] = ()
    if codes:
        weather_rows = (
            (
                await session.execute(
                    select(WeatherLocation)
                    .where(
                        WeatherLocation.enabled.is_(True),
                        WeatherLocation.katottg_code.in_(sorted(codes)),
                    )
                    .order_by(WeatherLocation.id)
                )
            )
            .scalars()
            .all()
        )
        weather = tuple(_weather_point(row) for row in weather_rows)
    return derive_active_coverage(
        clock,
        catalog_version=version,
        territories=tuple(catalog),
        subscribers=subscribers,
        weather_locations=weather,
    )

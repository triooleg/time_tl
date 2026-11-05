"""Documented whole-region fetches: one overview, one request per region."""

from __future__ import annotations

from time_tl.domain.power_outages.map import MapArea, RegionMap

from .alerts_energy_api import AlertsEnergyApiClient
from .base import Clock, PowerFetchError, PowerFetchErrorCode, utc_now


class AlertsEnergyMapApi:
    def __init__(self, client: AlertsEnergyApiClient, *, clock: Clock = utc_now,
                 stale_after_seconds: int = 1800) -> None:
        if not isinstance(client, AlertsEnergyApiClient) or type(stale_after_seconds) is not int \
                or not 1 <= stale_after_seconds <= 86400:
            raise ValueError("invalid_power_map_client")
        self._client, self._clock, self._ttl = client, clock, stale_after_seconds

    async def overview(self) -> tuple[MapArea, ...]:
        value = await self._client._get("source-registry/areas/overview")
        try:
            if type(value) is not list or not 1 <= len(value) <= 64:
                raise ValueError("invalid_power_map_overview")
            areas = []
            for row in value:
                if type(row) is not dict or not {"slug", "katottg", "name"} <= set(row):
                    raise ValueError("invalid_power_map_area")
                areas.append(MapArea(row["slug"], row["katottg"], row["name"]))
            if len({area.slug for area in areas}) != len(areas) \
                    or len({area.katottg for area in areas}) != len(areas):
                raise ValueError("duplicate_power_map_area")
            return tuple(sorted(areas, key=lambda area: area.slug))
        except (ValueError, TypeError, KeyError, RecursionError):
            raise PowerFetchError(PowerFetchErrorCode.INVALID_RESPONSE) from None

    async def region(self, area: MapArea) -> RegionMap:
        if type(area) is not MapArea:
            raise ValueError("invalid_power_map_area")
        value = await self._client._get(f"source-registry/areas/{area.slug}/shutdowns")
        try:
            if type(value) is not list:
                raise ValueError("invalid_power_map_rows")
            return RegionMap(area, tuple(value), self._clock(), self._ttl)
        except (ValueError, TypeError, KeyError, RecursionError):
            raise PowerFetchError(PowerFetchErrorCode.INVALID_RESPONSE) from None

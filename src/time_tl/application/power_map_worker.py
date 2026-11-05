"""One country map cycle every 300 seconds; no Telegram or per-user requests."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from time_tl.application.power_coverage import demanded_areas
from time_tl.domain.power_outages.map import RegionMap
from time_tl.infrastructure.db.energy_api_credentials import EnergyApiCheckStatus
from time_tl.infrastructure.power_outages.base import PowerFetchError, PowerFetchErrorCode
from time_tl.infrastructure.power_outages.budget_client import BudgetedEnergyApiClient
from time_tl.infrastructure.power_outages.map_api import AlertsEnergyMapApi


class PowerMapWorker:
    def __init__(
        self,
        repository: Any,
        credentials: Any,
        budget: Any,
        lease: Any,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        api_factory: Any = AlertsEnergyMapApi,
        client_factory: Any = BudgetedEnergyApiClient,
        coverage: Any | None = None,
    ) -> None:
        self._repo, self._credentials, self._budget, self._lease = (
            repository,
            credentials,
            budget,
            lease,
        )
        self._clock, self._api_factory, self._client_factory = clock, api_factory, client_factory
        self._coverage = coverage
        self._recent_maps: dict[str, RegionMap] = {}
        self._recent_revision: int | None = None

    async def run_once(self) -> bool:
        groups = None
        status = None
        if self._coverage is not None:
            stamp = self._clock()
            groups = (await self._coverage.snapshot(stamp)).power_group_keys
            if groups:
                status = await self._credentials.status(stamp)
            if (
                status is None
                or not status.configured
                or status.check_status is not EnergyApiCheckStatus.VALID
                or status.revision != self._recent_revision
            ):
                self._recent_maps, self._recent_revision = {}, None
            else:
                areas_in_use = demanded_areas(groups)
                self._recent_maps = {
                    slug: snapshot for slug, snapshot in self._recent_maps.items()
                    if slug in areas_in_use and 0 <= (stamp - snapshot.fetched_at).total_seconds()
                    <= snapshot.stale_after_seconds
                }
            if self._recent_maps and not await self._lease.renew():
                raise RuntimeError("power_map_lease_lost")
            await self._repo.prune_unused_groups(
                stamp,
                recent_maps=self._recent_maps,
                credential_revision=self._recent_revision,
            )
            if not groups:
                return False  # No API, credential load or cycle for an empty demand set.
        if status is None:
            status = await self._credentials.status(self._clock())
        if not status.configured or status.check_status is not EnergyApiCheckStatus.VALID:
            return False
        if not await self._lease.renew():
            raise RuntimeError("power_map_lease_lost")
        cycle = await self._repo.begin_cycle(self._clock(), status.revision)
        if cycle is None:
            return False
        self._recent_maps, self._recent_revision = {}, None
        requested_area: str | None = None

        async def fence() -> None:
            if self._coverage is not None:
                current_groups = (await self._coverage.snapshot(self._clock())).power_group_keys
                if not current_groups or (
                    requested_area is not None
                    and requested_area not in demanded_areas(current_groups)
                ):
                    raise RuntimeError("power_map_demand_removed")
            if not await self._lease.renew() or not await self._repo.cycle_current(
                cycle,
                status.revision,
                self._clock(),
            ):
                raise RuntimeError("power_map_cycle_lost")
            current = await self._credentials.status(self._clock())
            if (
                current.revision != status.revision
                or not current.configured
                or current.check_status is not EnergyApiCheckStatus.VALID
            ):
                raise RuntimeError("power_map_credential_changed")

        error = "CYCLE_FAILED"
        try:
            async with asyncio.timeout(240):
                key = await self._credentials.load()
                if key is None:
                    raise RuntimeError("power_map_credential_absent")
                api = self._api_factory(
                    self._client_factory(
                        api_key=key,
                        budget=self._budget,
                        before_request=fence,
                        clock=self._clock,
                    ),
                    clock=self._clock,
                )
                areas = await api.overview()
                if groups is not None:
                    needed = demanded_areas(groups)
                    areas = tuple(area for area in areas if area.slug in needed)
                # Reserve daily headroom for owner checks and address resolution. No silent subset.
                if (
                    not areas
                    or any(len(area.slug) > 83 for area in areas)
                    or (len(areas) + 1) * 300 > 9000
                ):
                    error = "QUOTA_DEFERRED"
                    raise ValueError("power_map_capacity_unavailable")
                successful, failures = {}, {}
                fatal = False
                for area in areas:
                    if self._coverage is not None:
                        groups = (await self._coverage.snapshot(self._clock())).power_group_keys
                        if area.slug not in demanded_areas(groups):
                            failures[area.slug] = "NOT_FETCHED"
                            continue
                    if fatal:
                        failures[area.slug] = "NOT_FETCHED"
                        continue
                    try:
                        requested_area = area.slug
                        successful[area.slug] = await api.region(area)
                    except PowerFetchError as exc:
                        failures[area.slug] = exc.code.value
                        fatal = exc.code in {
                            PowerFetchErrorCode.RATE_LIMITED,
                            PowerFetchErrorCode.ACCESS_DENIED,
                        }
                await fence()
                committed = bool(
                    await self._repo.finish_cycle(
                        cycle, areas, successful, failures, self._clock(), status.revision
                    )
                )
                if committed and self._coverage is not None:
                    self._recent_maps, self._recent_revision = successful, status.revision
                return committed
        except PowerFetchError as exc:
            error = exc.code.value
        except (TimeoutError, ValueError, RuntimeError):
            pass
        await self._repo.fail_cycle(cycle, error, self._clock(), status.revision)
        return False

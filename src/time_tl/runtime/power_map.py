"""Known-provider cache projection only; user reads never invoke HTTP."""

from __future__ import annotations

from datetime import date, datetime
from typing import Protocol

from time_tl.domain.power_outages.models import (
    PowerDaySchedule,
    PowerProvider,
    PowerScheduleView,
    PowerSourceKind,
    ScheduleKind,
    SourceStatus,
    aware,
    validate_group,
)
from time_tl.infrastructure.db.power_map import PowerMapView
from time_tl.infrastructure.power_outages.alerts_energy_api_normalizer import (
    KNOWN_BINDINGS,
    normalize_api_schedules,
    normalize_cached_api_day,
)


class PowerMapCache(Protocol):
    async def read_region(self, slug: str, now: datetime) -> PowerMapView: ...


class PowerMapScheduleReader:
    def __init__(self, repository: PowerMapCache, *,
                 stale_after_seconds: int | None = None) -> None:
        ttl = getattr(repository, "stale_after_seconds", 1800) \
            if stale_after_seconds is None else stale_after_seconds
        if type(ttl) is not int or not 1 <= ttl <= 86400:
            raise ValueError("invalid_power_map_reader_ttl")
        self._repository, self._ttl = repository, ttl

    async def read_schedule(self, provider: PowerProvider, group_id: str, day: date, *,
                            schedule_type: ScheduleKind, now: datetime,
                            display_only: bool = False) -> PowerScheduleView:
        validate_group(group_id)
        aware(now)
        if not isinstance(provider, PowerProvider) or type(day) is not date \
                or not isinstance(schedule_type, ScheduleKind):
            raise ValueError("invalid_power_map_schedule_request")
        binding = KNOWN_BINDINGS.get(provider)
        if binding is None or schedule_type is not ScheduleKind.CONFIRMED_DAY:
            return PowerScheduleView(
                provider, group_id, SourceStatus.UNAVAILABLE, None, None, None,
                error_code="MAP_PROJECTION_UNSUPPORTED",
                source_kind=PowerSourceKind.ALERTS_ENERGY_API,
            )
        slug, supplier = binding
        source_url = f"https://alerts.energy/api/v1/source-registry/areas/{slug}/shutdowns"
        view = await self._repository.read_region(slug, now)
        data = view.data

        def result(status: SourceStatus, code: str | None,
                   schedule: PowerDaySchedule | None = None) -> PowerScheduleView:
            return PowerScheduleView(
                provider, group_id, status,
                data.fetched_at if status is SourceStatus.FRESH and data is not None
                else view.last_success_at, view.last_attempt_at, schedule,
                view.revision, code, PowerSourceKind.ALERTS_ENERGY_API, source_url,
            )

        if view.area_slug != slug or data is not None and data.area.slug != slug:
            return result(SourceStatus.INVALID, "MAP_REGION_IDENTITY_INVALID")
        if data is None:
            return result(SourceStatus.UNAVAILABLE, view.error_code or "MAP_NOT_FETCHED")
        if (
            display_only
            and view.last_fetch_succeeded
            and view.error_code in {None, "NO_SCHEDULE", "MAP_UNVERIFIED_OR_STALE"}
            and 0 <= (aware(now) - aware(data.fetched_at)).total_seconds() <= self._ttl
            and not any(
                row["initiator"] == supplier and row["queue"] == group_id for row in data.rows
            )
        ):
            return result(SourceStatus.UNAVAILABLE, "MAP_GROUP_PENDING")
        if view.source_status in {SourceStatus.INVALID, SourceStatus.UNAVAILABLE}:
            return result(view.source_status, view.error_code or "MAP_FETCH_UNAVAILABLE")
        if not view.last_fetch_succeeded or view.error_code in {
            "REGION_MAPPING_CHANGED", "REGION_REMOVED", "CYCLE_IN_PROGRESS",
        }:
            return result(SourceStatus.STALE, view.error_code or "MAP_LAST_FETCH_FAILED")
        if not 0 <= (aware(now) - aware(data.fetched_at)).total_seconds() <= self._ttl:
            return result(SourceStatus.STALE, "STALE_MAP_FETCH")
        record_status = data.record_status(supplier, group_id, now, day=day)
        if record_status is not SourceStatus.FRESH:
            return result(record_status, "MAP_RECORD_UNAVAILABLE_OR_STALE")
        checked = data.record_checked_at(supplier, group_id, day=day)
        if checked is None or not 0 <= (aware(now) - checked).total_seconds() <= self._ttl:
            return result(SourceStatus.STALE, "STALE_MAP_RECORD_CHECK")
        rows = [row for row in data.to_wire()["rows"] if row["initiator"] == supplier]
        try:
            if display_only:
                # Dynamic coverage caches only the subscribed groups. The selected
                # record has passed its own date, source and freshness checks above.
                row = next((row for row in rows if row["queue"] == group_id), None)
                if row is None:
                    return result(SourceStatus.UNAVAILABLE, "MAP_DAY_UNAVAILABLE")
                display_schedule = normalize_cached_api_day(
                    row, provider=provider, area_slug=slug, day=day,
                    fetched_at=data.fetched_at, stale_after_seconds=self._ttl,
                )
                return result(SourceStatus.FRESH, None, display_schedule)
            normalized = normalize_api_schedules(
                rows, provider=provider, area_slug=slug, supplier_key=supplier,
                fetched_at=data.fetched_at, stale_after_seconds=self._ttl,
            )
        except (ValueError, TypeError, RecursionError):
            return result(SourceStatus.INVALID, "MAP_OPERATOR_PROJECTION_INVALID")
        if normalized.status is not SourceStatus.FRESH or normalized.source_checked_at is None:
            return result(SourceStatus.STALE, normalized.error_code or "STALE_MAP_PROJECTION")
        if not 0 <= (aware(now) - aware(normalized.source_checked_at)).total_seconds() <= self._ttl:
            return result(SourceStatus.STALE, "STALE_MAP_SOURCE_CHECK")
        schedule = next((item for item in normalized.days if item.group_id == group_id
                         and item.local_date == day and item.schedule_type is schedule_type), None)
        if schedule is None:
            return result(SourceStatus.UNAVAILABLE, "MAP_DAY_UNAVAILABLE")
        return result(SourceStatus.FRESH, None, schedule)

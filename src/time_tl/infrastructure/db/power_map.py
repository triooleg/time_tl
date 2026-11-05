"""Latest public region maps with durable cadence and stale-cycle fencing.

This cache is never notification authority and never performs HTTP. All writes
use existing runtime_settings; no migration or provider/outbox mutation occurs.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from time_tl.domain.power_outages.map import MAX_MAP_BYTES, MapArea, RegionMap
from time_tl.domain.power_outages.models import SourceStatus, aware

from .models import RuntimeSetting

STATE_KEY = "power_map:state"
REGION_PREFIX = "power_map:region:"
CADENCE_SECONDS = 300
CYCLE_LEASE_SECONDS = 600
MAX_REGIONS = 64
ERROR_CODES = frozenset(
    {
        "HTTP_ERROR",
        "TIMEOUT",
        "CONNECTION_ERROR",
        "RATE_LIMITED",
        "ACCESS_DENIED",
        "INVALID_RESPONSE",
        "SOURCE_UNAVAILABLE",
        "QUOTA_DEFERRED",
        "CYCLE_FAILED",
        "NOT_FETCHED",
        "REGION_REMOVED",
        "REGION_MAPPING_CHANGED",
        "CYCLE_IN_PROGRESS",
        "MAP_UNVERIFIED_OR_STALE",
        "NO_SCHEDULE",
        "PARTIAL_CYCLE",
    }
)


def _slug(value: str) -> str:
    if type(value) is not str or re.fullmatch(r"[a-z0-9][a-z0-9-]{0,82}", value) is None:
        raise ValueError("invalid_power_map_slug")
    return value


def _stamp(value: object) -> datetime | None:
    if value is None:
        return None
    if type(value) is not str or len(value) > 40:
        raise ValueError("invalid_power_map_state")
    try:
        return aware(datetime.fromisoformat(value))
    except ValueError:
        raise ValueError("invalid_power_map_state") from None


def _revision(value: object, *, zero: bool = False) -> int:
    if type(value) is not int or not (0 if zero else 1) <= value < 2**63:
        raise ValueError("invalid_power_map_revision")
    return value


def _state_empty() -> dict[str, Any]:
    return {
        "version": 1,
        "active_cycle": None,
        "cycle_started_at": None,
        "credential_revision": None,
        "next_due_at": None,
        "last_finished_at": None,
        "expected_regions": [],
        "complete": False,
        "collected_complete": False,
        "last_error_code": None,
        "revision": 1,
    }


def _state(value: object) -> dict[str, Any]:
    if type(value) is not dict or set(value) != set(_state_empty()):
        raise ValueError("invalid_power_map_state")
    if (
        type(value["version"]) is not int
        or value["version"] != 1
        or type(value["complete"]) is not bool
        or type(value["collected_complete"]) is not bool
    ):
        raise ValueError("invalid_power_map_state")
    _revision(value["revision"])
    for key in ("cycle_started_at", "next_due_at", "last_finished_at"):
        _stamp(value[key])
    if value["credential_revision"] is not None:
        _revision(value["credential_revision"])
    if value["active_cycle"] is not None:
        try:
            if (
                type(value["active_cycle"]) is not str
                or UUID(value["active_cycle"]).hex != value["active_cycle"]
            ):
                raise ValueError
        except ValueError:
            raise ValueError("invalid_power_map_state") from None
        if value["cycle_started_at"] is None or value["credential_revision"] is None:
            raise ValueError("invalid_power_map_state")
    regions = value["expected_regions"]
    if type(regions) is not list or len(regions) > MAX_REGIONS or len(set(regions)) != len(regions):
        raise ValueError("invalid_power_map_state")
    for region in regions:
        _slug(region)
    if value["last_error_code"] is not None and value["last_error_code"] not in ERROR_CODES:
        raise ValueError("invalid_power_map_state")
    return dict(value)


def _area_wire(area: MapArea) -> dict[str, str]:
    _slug(area.slug)
    return {"slug": area.slug, "katottg": area.katottg, "name": area.name}


def _empty_region(area: MapArea) -> dict[str, Any]:
    return {
        "version": 1,
        "area": _area_wire(area),
        "revision": 0,
        "payload": None,
        "content_hash": None,
        "source_status": "UNAVAILABLE",
        "error_code": "NOT_FETCHED",
        "last_attempt_at": None,
        "last_success_at": None,
        "last_fetch_succeeded": False,
    }


def _region(value: object, slug: str) -> dict[str, Any]:
    if (
        type(value) is not dict
        or type(value.get("area")) is not dict
        or set(value["area"]) != {"slug", "katottg", "name"}
    ):
        raise ValueError("invalid_power_map_region_state")
    area = MapArea(**value["area"])
    if (
        area.slug != slug
        or set(value) != set(_empty_region(area))
        or type(value["version"]) is not int
        or value["version"] != 1
    ):
        raise ValueError("invalid_power_map_region_state")
    if len(json.dumps(value).encode("utf-8")) > MAX_MAP_BYTES + 8192:
        raise ValueError("invalid_power_map_region_state")
    _revision(value["revision"], zero=True)
    if type(value["last_fetch_succeeded"]) is not bool:
        raise ValueError("invalid_power_map_region_state")
    SourceStatus(value["source_status"])
    if value["error_code"] is not None and value["error_code"] not in ERROR_CODES:
        raise ValueError("invalid_power_map_region_state")
    _stamp(value["last_attempt_at"])
    _stamp(value["last_success_at"])
    if value["payload"] is None:
        if (
            value["content_hash"] is not None
            or value["revision"] != 0
            or value["source_status"] == "FRESH"
        ):
            raise ValueError("invalid_power_map_region_state")
    else:
        payload = RegionMap.from_wire(value["payload"])
        if (
            payload.area.slug != slug
            or payload.semantic_hash != value["content_hash"]
            or value["revision"] < 1
        ):
            raise ValueError("invalid_power_map_region_state")
        if payload.area.katottg != area.katottg and (
            value["source_status"] != "UNAVAILABLE"
            or value["error_code"] not in {"REGION_MAPPING_CHANGED", "REGION_REMOVED"}
        ):
            raise ValueError("invalid_power_map_region_identity")
    return dict(value)


@dataclass(frozen=True, slots=True, repr=False)
class PowerMapView:
    area_slug: str
    source_status: SourceStatus
    age_seconds: float | None
    data: RegionMap | None
    revision: int
    last_attempt_at: datetime | None
    last_success_at: datetime | None
    error_code: str | None
    scope: str = "REGION_CACHE_ONLY"
    last_fetch_succeeded: bool = False


@dataclass(frozen=True, slots=True, repr=False)
class PowerMapRecordView:
    area_slug: str
    supplier_key: str
    group_id: str
    local_date: date
    source_status: SourceStatus
    record: Mapping[str, object] | None
    revision: int
    last_success_at: datetime | None
    error_code: str | None
    scope: str = "REGION_CACHE_ONLY"


class PostgresPowerMapRepository:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        *,
        stale_after_seconds: int = 1800,
        dynamic_coverage: bool = False,
        authority_guard: Any = None,
    ) -> None:
        if type(stale_after_seconds) is not int or not 1 <= stale_after_seconds <= 86400:
            raise ValueError("invalid_power_map_stale_limit")
        self._factory, self.stale_after_seconds = factory, stale_after_seconds
        self.dynamic_coverage = dynamic_coverage
        self.authority_guard = authority_guard

    async def prune_unused_groups(
        self,
        now: datetime,
        *,
        recent_maps: dict[str, RegionMap] | None = None,
        credential_revision: int | None = None,
    ) -> None:
        if not self.dynamic_coverage:
            raise ValueError("dynamic_power_coverage_not_configured")
        from time_tl.application.power_coverage import retain_demanded_groups

        from .power_coverage import locked_power_groups

        stamp = aware(now)
        async with self._factory() as session, session.begin():
            if self.authority_guard is not None:
                await self.authority_guard(session, stamp)
            groups = await locked_power_groups(session)
            reusable = {}
            if recent_maps and credential_revision is not None and groups:
                state_row = await session.get(RuntimeSetting, STATE_KEY, with_for_update=True)
                if state_row is not None:
                    state = _state(state_row.value)
                    if (
                        state["active_cycle"] is None
                        and state["credential_revision"] == credential_revision
                        and await self._credential_valid(session, credential_revision)
                    ):
                        reusable = recent_maps
            rows = (
                (
                    await session.execute(
                        select(RuntimeSetting)
                        .where(RuntimeSetting.key.startswith(REGION_PREFIX))
                        .order_by(RuntimeSetting.key)
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            for row in rows:
                slug = row.key.removeprefix(REGION_PREFIX)
                value = _region(row.value, slug)
                if value["payload"] is None:
                    continue
                previous = RegionMap.from_wire(value["payload"])
                snapshot = reusable.get(slug)
                mapped_success = (
                    value["last_fetch_succeeded"]
                    and value["error_code"] != "REGION_REMOVED"
                    and previous.area.katottg == value["area"]["katottg"]
                )
                # Reproject only the same successful fetch. Never advance its
                # timestamps or revive data from before a failed/moved region.
                if (
                    snapshot is None
                    or not mapped_success
                    or snapshot.area != previous.area
                    or snapshot.fetched_at != previous.fetched_at
                    or not 0 <= (stamp - aware(snapshot.fetched_at)).total_seconds()
                    <= self.stale_after_seconds
                ):
                    snapshot = previous
                filtered = retain_demanded_groups(snapshot, groups)
                if filtered.semantic_hash != previous.semantic_hash:
                    value.update(
                        payload=filtered.to_wire(),
                        content_hash=filtered.semantic_hash,
                        revision=value["revision"] + 1,
                    )
                    if mapped_success:
                        value.update(
                            source_status=filtered.status_at(stamp).value,
                            error_code=filtered.error_code,
                        )
                    row.value = value

            if self.authority_guard is not None:
                await self.authority_guard(session, stamp)

    @staticmethod
    async def _locked(session: AsyncSession, key: str, initial: dict[str, Any]) -> RuntimeSetting:
        await session.execute(
            insert(RuntimeSetting)
            .values(
                key=key,
                value=initial,
                updated_by="power-map-worker",
            )
            .on_conflict_do_nothing(index_elements=[RuntimeSetting.key])
        )
        return (
            await session.execute(
                select(RuntimeSetting).where(RuntimeSetting.key == key).with_for_update()
            )
        ).scalar_one()

    async def initialize(self) -> None:
        async with self._factory() as session, session.begin():
            _state((await self._locked(session, STATE_KEY, _state_empty())).value)

    @staticmethod
    async def _credential_valid(session: AsyncSession, revision: int) -> bool:
        # Project only public metadata, never load the encrypted credential row.
        result = (
            await session.execute(
                select(
                    RuntimeSetting.value["revision"],
                    RuntimeSetting.value["check_status"],
                )
                .where(RuntimeSetting.key == "power_alerts_energy_api_credentials")
                .with_for_update()
            )
        ).one_or_none()
        return (
            result is not None
            and type(result[0]) is int
            and result[0] == revision
            and result[1] == "VALID"
        )

    @staticmethod
    def _current(
        state: dict[str, Any], cycle_id: str, credential_revision: int, now: datetime
    ) -> bool:
        start = _stamp(state["cycle_started_at"])
        return (
            state["active_cycle"] == cycle_id
            and state["credential_revision"] == credential_revision
            and start is not None
            and timedelta(0) <= aware(now) - start < timedelta(seconds=CYCLE_LEASE_SECONDS)
        )

    async def begin_cycle(self, now: datetime, credential_revision: int) -> str | None:
        stamp = aware(now)
        _revision(credential_revision)
        async with self._factory() as session, session.begin():
            if self.authority_guard is not None:
                await self.authority_guard(session, stamp)
            row = await self._locked(session, STATE_KEY, _state_empty())
            value = _state(row.value)
            if not await self._credential_valid(session, credential_revision):
                raise ValueError("power_map_credential_unavailable")
            due, started = _stamp(value["next_due_at"]), _stamp(value["cycle_started_at"])
            if due is not None and stamp < due:
                return None
            if (
                value["active_cycle"] is not None
                and started is not None
                and stamp - started < timedelta(seconds=CYCLE_LEASE_SECONDS)
            ):
                return None
            cycle = uuid4().hex
            value.update(
                active_cycle=cycle,
                cycle_started_at=stamp.isoformat(),
                credential_revision=credential_revision,
                next_due_at=(stamp + timedelta(seconds=CADENCE_SECONDS)).isoformat(),
                complete=False,
                collected_complete=False,
                last_error_code="CYCLE_IN_PROGRESS",
                revision=value["revision"] + 1,
            )
            row.value = value
            return cycle

    async def cycle_current(self, cycle_id: str, credential_revision: int, now: datetime) -> bool:
        async with self._factory() as session, session.begin():
            if self.authority_guard is not None:
                await self.authority_guard(session, aware(now))
            row = await session.get(RuntimeSetting, STATE_KEY)
            return (
                row is not None
                and self._current(_state(row.value), cycle_id, credential_revision, now)
                and await self._credential_valid(session, credential_revision)
            )

    async def finish_cycle(
        self,
        cycle_id: str,
        regions: Sequence[MapArea],
        successful_maps: Mapping[str, RegionMap],
        failures: Mapping[str, str],
        now: datetime,
        credential_revision: int,
    ) -> bool:
        stamp = aware(now)
        _revision(credential_revision)
        if not 1 <= len(regions) <= MAX_REGIONS or any(
            type(area) is not MapArea for area in regions
        ):
            raise ValueError("invalid_power_map_overview")
        expected = {area.slug: area for area in regions}
        if len(expected) != len(regions) or len({area.katottg for area in regions}) != len(regions):
            raise ValueError("ambiguous_power_map_overview")
        for area in regions:
            _area_wire(area)
        if (
            set(successful_maps) & set(failures)
            or set(successful_maps) | set(failures) != set(expected)
            or any(code not in ERROR_CODES for code in failures.values())
        ):
            raise ValueError("incomplete_power_map_cycle")
        for slug, candidate in successful_maps.items():
            if (
                type(candidate) is not RegionMap
                or candidate.area != expected[slug]
                or aware(candidate.fetched_at) > stamp
            ):
                raise ValueError("mismatched_power_map_result")
        async with self._factory() as session, session.begin():
            if self.authority_guard is not None:
                await self.authority_guard(session, stamp)
            if self.dynamic_coverage:
                from time_tl.application.power_coverage import retain_demanded_groups

                from .power_coverage import locked_power_groups

                # Re-read and lock actual selections after HTTP. An in-flight fetch
                # cannot resurrect a group removed while the request was running.
                groups = await locked_power_groups(session)
                successful_maps = {
                    slug: retain_demanded_groups(value, groups)
                    for slug, value in successful_maps.items()
                }
            state_row = await self._locked(session, STATE_KEY, _state_empty())
            state = _state(state_row.value)
            if not self._current(
                state, cycle_id, credential_revision, stamp
            ) or not await self._credential_valid(session, credential_revision):
                return False
            complete = True
            for slug in sorted(set(state["expected_regions"]) | set(expected)):
                if slug not in expected:
                    row = await session.get(
                        RuntimeSetting, REGION_PREFIX + slug, with_for_update=True
                    )
                    if row is not None:
                        value = _region(row.value, slug)
                        value.update(
                            source_status="UNAVAILABLE",
                            error_code="REGION_REMOVED",
                            last_attempt_at=stamp.isoformat(),
                            last_fetch_succeeded=False,
                        )
                        row.value = value
                    continue
                row = await self._locked(
                    session, REGION_PREFIX + slug, _empty_region(expected[slug])
                )
                value = _region(row.value, slug)
                value.update(area=_area_wire(expected[slug]), last_attempt_at=stamp.isoformat())
                result = successful_maps.get(slug)
                if result is None:
                    mapping_changed = (
                        value["payload"] is not None
                        and RegionMap.from_wire(value["payload"]).area.katottg
                        != expected[slug].katottg
                    )
                    value.update(
                        source_status="UNAVAILABLE"
                        if mapping_changed or value["payload"] is None
                        else "STALE",
                        error_code="REGION_MAPPING_CHANGED" if mapping_changed else failures[slug],
                        last_fetch_succeeded=False,
                    )
                else:
                    effective = result.status_at(stamp)
                    if result.semantic_hash != value["content_hash"]:
                        value["revision"] += 1
                    value.update(
                        payload=result.to_wire(),
                        content_hash=result.semantic_hash,
                        source_status=effective.value,
                        error_code=result.error_code,
                        last_fetch_succeeded=True,
                    )
                    if effective is SourceStatus.FRESH:
                        value["last_success_at"] = stamp.isoformat()
                if value["source_status"] != "FRESH":
                    complete = False
                row.value = value
            state.update(
                active_cycle=None,
                expected_regions=sorted(expected),
                complete=complete,
                collected_complete=set(successful_maps) == set(expected),
                last_finished_at=stamp.isoformat(),
                last_error_code=None if complete else "PARTIAL_CYCLE",
                revision=state["revision"] + 1,
            )
            state_row.value = state
            if self.authority_guard is not None:
                await self.authority_guard(session, stamp)
            return True

    async def fail_cycle(
        self, cycle_id: str, error_code: str, now: datetime, credential_revision: int
    ) -> bool:
        stamp = aware(now)
        if error_code not in ERROR_CODES:
            raise ValueError("invalid_power_map_error")
        _revision(credential_revision)
        async with self._factory() as session, session.begin():
            state_row = await self._locked(session, STATE_KEY, _state_empty())
            state = _state(state_row.value)
            if not self._current(
                state, cycle_id, credential_revision, stamp
            ) or not await self._credential_valid(session, credential_revision):
                return False
            for slug in state["expected_regions"]:
                row = await session.get(RuntimeSetting, REGION_PREFIX + slug, with_for_update=True)
                if row is not None:
                    value = _region(row.value, slug)
                    mapping_changed = (
                        value["payload"] is not None
                        and RegionMap.from_wire(value["payload"]).area.katottg
                        != value["area"]["katottg"]
                    )
                    value.update(
                        source_status="UNAVAILABLE"
                        if mapping_changed or value["payload"] is None
                        else "STALE",
                        error_code="REGION_MAPPING_CHANGED" if mapping_changed else error_code,
                        last_attempt_at=stamp.isoformat(),
                        last_fetch_succeeded=False,
                    )
                    row.value = value
            state.update(
                active_cycle=None,
                complete=False,
                collected_complete=False,
                last_finished_at=stamp.isoformat(),
                last_error_code=error_code,
                revision=state["revision"] + 1,
            )
            state_row.value = state
            return True

    async def read_region(self, slug: str, now: datetime) -> PowerMapView:
        _slug(slug)
        stamp = aware(now)
        async with self._factory() as session:
            row = await session.get(RuntimeSetting, REGION_PREFIX + slug)
            return self._view(slug, row, stamp)

    def _view(self, slug: str, row: RuntimeSetting | None, stamp: datetime) -> PowerMapView:
        if row is None:
            return PowerMapView(
                slug, SourceStatus.UNAVAILABLE, None, None, 0, None, None, "NOT_FETCHED"
            )
        value = _region(row.value, slug)
        data = RegionMap.from_wire(value["payload"]) if value["payload"] is not None else None
        if data is not None and data.area.katottg != value["area"]["katottg"]:
            data = None
        status = SourceStatus(value["source_status"])
        age = (stamp - aware(data.fetched_at)).total_seconds() if data else None
        if status is SourceStatus.FRESH and (
            data is None
            or data.status_at(stamp) is not SourceStatus.FRESH
            or age is None
            or not 0 <= age <= self.stale_after_seconds
        ):
            status = SourceStatus.STALE
        return PowerMapView(
            slug,
            status,
            age,
            data,
            value["revision"],
            _stamp(value["last_attempt_at"]),
            _stamp(value["last_success_at"]),
            value["error_code"],
            last_fetch_succeeded=value["last_fetch_succeeded"],
        )

    async def read_record(
        self, slug: str, supplier: str, group: str, day: date, now: datetime
    ) -> PowerMapRecordView:
        if (
            any(type(value) is not str or not 1 <= len(value) <= 128 for value in (supplier, group))
            or type(day) is not date
        ):
            raise ValueError("invalid_power_map_record_request")
        view = await self.read_region(slug, now)
        record = (
            next(
                (
                    row
                    for row in view.data.rows
                    if row["initiator"] == supplier
                    and row["queue"] == group
                    and day.isoformat() in (row["today_date"], row["tomorrow_date"])
                ),
                None,
            )
            if view.data
            else None
        )
        status = view.source_status if record is not None else SourceStatus.UNAVAILABLE
        if (
            record is not None
            and view.data is not None
            and view.last_fetch_succeeded
            and view.age_seconds is not None
            and 0 <= view.age_seconds <= self.stale_after_seconds
        ):
            status = view.data.record_status(supplier, group, now, day=day)
        return PowerMapRecordView(
            slug,
            supplier,
            group,
            day,
            status,
            record,
            view.revision,
            view.last_success_at,
            view.error_code if record else "NO_SCHEDULE",
        )

    async def statistics(self, now: datetime) -> dict[str, object]:
        stamp = aware(now)
        async with self._factory() as session, session.begin():
            # Serialize against cycle publication so scope and all region views
            # describe one coherent country cache, without nested connections.
            row = await session.get(RuntimeSetting, STATE_KEY, with_for_update=True)
            state = _state(row.value) if row is not None else _state_empty()
            views = [
                self._view(slug, await session.get(RuntimeSetting, REGION_PREFIX + slug), stamp)
                for slug in state["expected_regions"]
            ]
        fresh = sum(view.source_status is SourceStatus.FRESH for view in views)
        region_metadata = [
            {
                "area_slug": view.area_slug,
                "source_status": view.source_status.value,
                "revision": view.revision,
                "age_seconds": view.age_seconds,
                "last_fetch_at": aware(view.data.fetched_at).isoformat() if view.data else None,
                "last_attempt_at": view.last_attempt_at.isoformat()
                if view.last_attempt_at
                else None,
                "last_source_checked_at": view.data.checked_at.isoformat()
                if view.data and view.data.checked_at
                else None,
                "error_code": view.error_code,
                "operator_count": len({row["initiator"] for row in view.data.rows})
                if view.data
                else 0,
                "group_count": len(view.data.rows) if view.data else 0,
                "content_hash": view.data.semantic_hash if view.data else None,
                "collected": view.last_fetch_succeeded,
            }
            for view in views
        ]
        return {
            "expected_regions": tuple(state["expected_regions"]),
            "region_count": len(views),
            "fresh_regions": fresh,
            "complete": bool(views) and state["complete"] and fresh == len(views),
            "fresh_complete": bool(views) and state["complete"] and fresh == len(views),
            "collected_complete": bool(views) and state["collected_complete"],
            "regions": region_metadata,
            "next_due_at": state["next_due_at"],
            "last_finished_at": state["last_finished_at"],
            "active_cycle": state["active_cycle"],
            "credential_revision": state["credential_revision"],
            "last_error_code": state["last_error_code"],
            "revision": state["revision"],
            "scope": "COUNTRY_CACHE_ONLY",
        }

"""Bounded weather-cache retention; official geographic reference rows survive.

The unused clock starts at the first observed loss of demand, never at the last
forecast fetch. Point locks coordinate with planning/publication; contested work
is skipped instead of reversing sender/account lock order.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import String, delete, exists, or_, select, true
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from time_tl.domain.weather import aware

from .geographic_coverage import read_coverage_in_session
from .models import (
    ActiveTerritoryCatalog,
    OutboxMessage,
    RuntimeSetting,
    User,
    UserWeatherPreference,
    WeatherChangeEvent,
    WeatherForecastRun,
    WeatherForecastSnapshot,
    WeatherLocation,
)
from .weather import FAILURE_PREFIX, POINT_IDENTITY_PREFIX, POINT_REQUEST_PREFIX, PROVIDER
from .weather_configuration import SETTING, WeatherConfiguration

UNUSED_PREFIX = "weather_unused_since:"
CURSOR_KEY = "weather_retention_cursor"
POINT_PREFIXES = (FAILURE_PREFIX, POINT_IDENTITY_PREFIX, POINT_REQUEST_PREFIX)


@dataclass(frozen=True)
class WeatherRetentionResult:
    points_examined: int = 0
    snapshots_deleted: int = 0
    events_deleted: int = 0
    runs_deleted: int = 0
    metadata_deleted: int = 0
    markers_started: int = 0
    markers_cleared: int = 0
    contended: int = 0
    pending_protected: int = 0

    @property
    def deleted(self) -> int:
        return (
            self.snapshots_deleted + self.events_deleted + self.runs_deleted + self.metadata_deleted
        )


def unused_since(value: Any) -> datetime:
    if (
        type(value) is not dict
        or set(value) != {"version", "since"}
        or (
            type(value["version"]) is not int
            or value["version"] != 1
            or type(value["since"]) is not str
        )
    ):
        raise ValueError("invalid_weather_unused_marker")
    result = datetime.fromisoformat(value["since"])
    aware(result)
    return result.astimezone(UTC)


@dataclass(frozen=True)
class PendingWeatherReferences:
    points: frozenset[UUID] = frozenset()
    event_keys: frozenset[str] = frozenset()
    uncertain: bool = False


def pending_references(payloads: list[Any]) -> PendingWeatherReferences:
    points: set[UUID] = set()
    keys: set[str] = set()
    for payload in payloads:
        if (
            type(payload) is not dict
            or type(payload.get("point_id")) is not str
            or (type(payload.get("event_keys")) is not list)
        ):
            return PendingWeatherReferences(uncertain=True)
        try:
            point = UUID(payload["point_id"])
        except ValueError:
            return PendingWeatherReferences(uncertain=True)
        if str(point) != payload["point_id"]:
            return PendingWeatherReferences(uncertain=True)
        points.add(point)
        for key in payload["event_keys"]:
            if (
                type(key) is not str
                or len(key) != 64
                or any(c not in "0123456789abcdef" for c in key)
            ):
                return PendingWeatherReferences(uncertain=True)
            keys.add(key)
    return PendingWeatherReferences(frozenset(points), frozenset(keys))


async def _pending(session: AsyncSession) -> PendingWeatherReferences:
    payloads = (
        (
            await session.execute(
                select(OutboxMessage.payload)
                .where(
                    OutboxMessage.aggregate_type == "weather",
                    OutboxMessage.delivered_at.is_(None),
                    OutboxMessage.dead_at.is_(None),
                )
                .limit(1001)
            )
        )
        .scalars()
        .all()
    )
    if len(payloads) > 1000:
        return PendingWeatherReferences(uncertain=True)
    return pending_references(list(payloads))


def snapshot_expired_for_retention(
    *,
    now: datetime,
    fetched_at: datetime,
    expires_at: datetime,
    latest: bool,
    unused_at: datetime | None,
    config: WeatherConfiguration,
) -> bool:
    for clock in (now, fetched_at, expires_at):
        aware(clock)
    if unused_at is not None:
        aware(unused_at)
        if unused_at <= now - timedelta(days=config.unused_cache_days):
            return expires_at <= now
    return (
        not latest
        and expires_at <= now
        and fetched_at <= now - timedelta(days=config.snapshot_history_days)
    )


class PostgresWeatherRetention:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        *,
        authority_guard: Callable[[AsyncSession, datetime], Awaitable[None]] | None = None,
    ) -> None:
        self._factory = factory
        self._authority_guard = authority_guard

    async def cleanup(self, now: datetime, limit: int = 200) -> WeatherRetentionResult:
        aware(now)
        clock = now.astimezone(UTC)
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("invalid_weather_retention_limit")
        totals = {key: 0 for key in WeatherRetentionResult.__dataclass_fields__}
        async with self._factory() as session, session.begin():
            if self._authority_guard is not None:
                await self._authority_guard(session, now)
            config_row = await session.get(RuntimeSetting, SETTING)
            config = WeatherConfiguration.model_validate(config_row.value if config_row else {})
            cursor = await session.get(RuntimeSetting, CURSOR_KEY)
            last: UUID | None = None
            if cursor is not None:
                if type(cursor.value) is not dict or set(cursor.value) != {"after"}:
                    raise ValueError("invalid_weather_retention_cursor")
                last = UUID(cursor.value["after"]) if cursor.value["after"] else None
            metadata = exists(
                select(RuntimeSetting.key).where(
                    RuntimeSetting.key.in_(
                        [
                            prefix + WeatherLocation.id.cast(String)
                            for prefix in (*POINT_PREFIXES, UNUSED_PREFIX)
                        ]
                    )
                )
            )
            candidates = (
                select(WeatherLocation.id)
                .where(
                    or_(
                        WeatherLocation.id.in_(select(WeatherForecastSnapshot.weather_location_id)),
                        WeatherLocation.id.in_(select(WeatherChangeEvent.location_id)),
                        metadata,
                    )
                )
                .order_by(WeatherLocation.id)
                .limit(limit)
            )
            if last is not None:
                candidates = candidates.where(WeatherLocation.id > last)
            point_ids = list((await session.execute(candidates)).scalars().all())
            # Page boundary wraps on the following call; reference catalogues are never rewritten.
            after = str(point_ids[-1]) if point_ids else None
            if cursor is None:
                session.add(
                    RuntimeSetting(
                        key=CURSOR_KEY, value={"after": after}, updated_by="weather-retention"
                    )
                )
            else:
                cursor.value = {"after": after}
            if self._authority_guard is not None:
                await self._authority_guard(session, now)
        for point_id in point_ids:
            remaining = limit - sum(
                totals[key]
                for key in (
                    "snapshots_deleted",
                    "events_deleted",
                    "runs_deleted",
                    "metadata_deleted",
                )
            )
            if remaining <= 0:
                break
            try:
                result = await self._point(point_id, clock, config, remaining)
                for key, value in result.items():
                    totals[key] += value
            except DBAPIError as error:
                if getattr(error.orig, "sqlstate", None) != "55P03":
                    raise
                totals["contended"] += 1
        remaining = limit - sum(
            totals[key]
            for key in ("snapshots_deleted", "events_deleted", "runs_deleted", "metadata_deleted")
        )
        if remaining > 0:
            totals["runs_deleted"] += await self._runs(clock, config, remaining)
        return WeatherRetentionResult(**totals)

    async def _point(
        self, point_id: UUID, now: datetime, config: WeatherConfiguration, budget: int
    ) -> dict[str, int]:
        counts = {key: 0 for key in WeatherRetentionResult.__dataclass_fields__}
        counts["points_examined"] = 1
        async with self._factory() as session, session.begin():
            if self._authority_guard is not None:
                await self._authority_guard(session, now)
            point = await session.get(WeatherLocation, point_id)
            if point is None:
                return counts
            # Matching accounts, including notification-off/interest-off accounts,
            # are locked before point, in the same order as preference updates.
            user_ids = (
                (
                    await session.execute(
                        select(User.id)
                        .where(User.confirmed_region_id == point.region_id)
                        .order_by(User.id)
                        .with_for_update(read=True, nowait=True)
                    )
                )
                .scalars()
                .all()
            )
            if user_ids:
                await session.execute(
                    select(UserWeatherPreference.user_id)
                    .where(UserWeatherPreference.user_id.in_(user_ids))
                    .order_by(UserWeatherPreference.user_id)
                    .with_for_update(read=True, nowait=True)
                )
            await session.execute(
                select(ActiveTerritoryCatalog)
                .where(ActiveTerritoryCatalog.provider == "alerts.in.ua")
                .with_for_update(read=True, nowait=True)
            )
            await session.execute(
                select(WeatherLocation.id)
                .where(WeatherLocation.id == point_id)
                .with_for_update(nowait=True)
            )
            active = (await read_coverage_in_session(session, now)).weather_location_ids
            marker = await session.get(
                RuntimeSetting, UNUSED_PREFIX + str(point_id), with_for_update=True
            )
            lost_at: datetime | None = None
            if str(point_id) in active:
                if marker is not None:
                    await session.delete(marker)
                    counts["markers_cleared"] += 1
                    counts["metadata_deleted"] += 1
                    budget -= 1
            elif marker is None:
                lost_at = now
                session.add(
                    RuntimeSetting(
                        key=UNUSED_PREFIX + str(point_id),
                        value={"version": 1, "since": now.isoformat()},
                        updated_by="weather-retention",
                    )
                )
                counts["markers_started"] += 1
            else:
                lost_at = unused_since(marker.value)
            # Re-read AFTER point lock: planner holds that lock through INSERT.
            # Do not lock outbox here (sender lock order is outbox -> point).
            pending = await _pending(session)
            if pending.uncertain or point_id in pending.points:
                counts["pending_protected"] += 1
                return counts
            protected = select(WeatherChangeEvent.snapshot_id).where(
                WeatherChangeEvent.event_key.in_(pending.event_keys),
                WeatherChangeEvent.snapshot_id.is_not(None),
            )
            latest_id = await session.scalar(
                select(WeatherForecastSnapshot.id)
                .where(
                    WeatherForecastSnapshot.weather_location_id == point_id,
                    WeatherForecastSnapshot.provider == PROVIDER,
                )
                .order_by(
                    WeatherForecastSnapshot.fetched_at.desc(), WeatherForecastSnapshot.id.desc()
                )
                .limit(1)
            )
            unused_mature = lost_at is not None and lost_at <= now - timedelta(
                days=config.unused_cache_days
            )
            rows = list(
                (
                    await session.execute(
                        select(WeatherForecastSnapshot)
                        .where(
                            WeatherForecastSnapshot.weather_location_id == point_id,
                            WeatherForecastSnapshot.provider == PROVIDER,
                            WeatherForecastSnapshot.id.not_in(protected),
                            WeatherForecastSnapshot.expires_at <= now,
                            true()
                            if unused_mature
                            else (
                                (
                                    WeatherForecastSnapshot.fetched_at
                                    <= now - timedelta(days=config.snapshot_history_days)
                                )
                                & (WeatherForecastSnapshot.id != latest_id)
                            ),
                        )
                        .order_by(
                            WeatherForecastSnapshot.fetched_at.desc(),
                            WeatherForecastSnapshot.id.desc(),
                        )
                        .limit(budget)
                        .with_for_update(skip_locked=True)
                    )
                )
                .scalars()
                .all()
            )
            for snapshot in rows:
                if budget <= 0:
                    break
                if snapshot_expired_for_retention(
                    now=now,
                    fetched_at=snapshot.fetched_at,
                    expires_at=snapshot.expires_at,
                    latest=snapshot.id == latest_id,
                    unused_at=lost_at,
                    config=config,
                ):
                    await session.delete(snapshot)
                    counts["snapshots_deleted"] += 1
                    budget -= 1
            events = (
                list(
                    (
                        await session.execute(
                            select(WeatherChangeEvent)
                            .where(
                                WeatherChangeEvent.location_id == point_id,
                                WeatherChangeEvent.expected_end <= now,
                                WeatherChangeEvent.detected_at
                                <= now - timedelta(days=config.aggregate_history_days),
                                WeatherChangeEvent.event_key.not_in(pending.event_keys),
                            )
                            .order_by(WeatherChangeEvent.detected_at, WeatherChangeEvent.id)
                            .limit(budget)
                            .with_for_update(skip_locked=True)
                        )
                    )
                    .scalars()
                    .all()
                )
                if budget
                else []
            )
            for event in events:
                await session.delete(event)
                counts["events_deleted"] += 1
                budget -= 1
            if lost_at is not None and lost_at <= now - timedelta(days=config.unused_cache_days):
                for prefix in POINT_PREFIXES:
                    if budget <= 0:
                        break
                    setting = await session.get(RuntimeSetting, prefix + str(point_id))
                    if setting is not None:
                        await session.delete(setting)
                        counts["metadata_deleted"] += 1
                        budget -= 1
                if marker is not None and budget > 0:
                    await session.flush()
                    has_snapshot = await session.scalar(
                        select(
                            exists(
                                select(WeatherForecastSnapshot.id).where(
                                    WeatherForecastSnapshot.weather_location_id == point_id
                                )
                            )
                        )
                    )
                    has_events = await session.scalar(
                        select(
                            exists(
                                select(WeatherChangeEvent.id).where(
                                    WeatherChangeEvent.location_id == point_id
                                )
                            )
                        )
                    )
                    remaining_metadata = any(
                        [
                            await session.get(RuntimeSetting, prefix + str(point_id))
                            for prefix in POINT_PREFIXES
                        ]
                    )
                    if not has_snapshot and not has_events and not remaining_metadata:
                        await session.delete(marker)
                        counts["metadata_deleted"] += 1
                        counts["markers_cleared"] += 1
            if self._authority_guard is not None:
                await self._authority_guard(session, now)
        return counts

    async def _runs(self, now: datetime, config: WeatherConfiguration, limit: int) -> int:
        async with self._factory() as session, session.begin():
            if self._authority_guard is not None:
                await self._authority_guard(session, now)
            ids = list(
                (
                    await session.execute(
                        select(WeatherForecastRun.id)
                        .where(
                            WeatherForecastRun.provider == PROVIDER,
                            WeatherForecastRun.completed_at
                            <= now - timedelta(days=config.aggregate_history_days),
                            ~exists(
                                select(WeatherForecastSnapshot.id).where(
                                    WeatherForecastSnapshot.forecast_run_id == WeatherForecastRun.id
                                )
                            ),
                        )
                        .order_by(WeatherForecastRun.completed_at, WeatherForecastRun.id)
                        .limit(limit)
                        .with_for_update(skip_locked=True)
                    )
                )
                .scalars()
                .all()
            )
            if ids:
                await session.execute(
                    delete(WeatherForecastRun).where(WeatherForecastRun.id.in_(ids))
                )
            if self._authority_guard is not None:
                await self._authority_guard(session, now)
            return len(ids)

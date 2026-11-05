"""Bounded public source observations; no source approval or notification outbox."""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from time_tl.domain.power_outages.models import (
    PowerDaySchedule,
    PowerInterval,
    PowerProvider,
    PowerPublicationStatus,
    PowerScheduleSnapshot,
    PowerSourceKind,
    PowerState,
    ScheduleKind,
    SourceStatus,
    aware,
)
from time_tl.domain.power_outages.sources import SOURCE_ORDER, ComparisonStatus, compare_snapshots
from time_tl.domain.power_outages.sources import _fresh as _snapshot_is_fresh

from .models import AuditLog, RuntimeSetting

MAX_OBSERVATION_BYTES = 1_048_576
REFRESH_SECONDS = 300


class PowerSourceConflict(ValueError):
    """Administrator edited an outdated source policy revision."""


def _key(provider: PowerProvider, source: PowerSourceKind) -> str:
    if not isinstance(provider, PowerProvider) or not isinstance(source, PowerSourceKind):
        raise ValueError("invalid_power_source_identity")
    return f"power_source:{provider.value}:{source.value}"


def _stamp(value: datetime | None) -> str | None:
    return None if value is None else aware(value).isoformat()


def _date(value: object) -> datetime:
    if type(value) is not str:
        raise ValueError("invalid_power_source_timestamp")
    return aware(datetime.fromisoformat(value))


def encode_snapshot(snapshot: PowerScheduleSnapshot) -> dict[str, Any]:
    if snapshot.source_url is not None and urlsplit(snapshot.source_url).query:
        raise ValueError("power_source_query_not_persistable")
    value: dict[str, Any] = {
        "provider": snapshot.provider.value,
        "source_kind": snapshot.source_kind.value,
        "fetched_at": _stamp(snapshot.fetched_at),
        "source_updated_at": _stamp(snapshot.source_updated_at),
        "source_checked_at": _stamp(snapshot.source_checked_at),
        "status": snapshot.status.value,
        "error_code": snapshot.error_code,
        "schema_version": snapshot.schema_version,
        "source_url": snapshot.source_url,
        "locality": snapshot.locality,
        "days": [
            {
                "group": day.group_id,
                "date": day.local_date.isoformat(),
                "kind": day.schedule_type.value,
                "mapping": day.mapping_version,
                "publication": day.publication_status.value,
                "intervals": [
                    [_stamp(item.starts_at), _stamp(item.ends_at), item.state.value]
                    for item in day.intervals
                ],
            }
            for day in snapshot.days
        ],
    }
    if len(json.dumps(value).encode("utf-8")) > MAX_OBSERVATION_BYTES:
        raise ValueError("power_source_observation_too_large")
    return value


def decode_snapshot(value: object) -> PowerScheduleSnapshot:
    fields = {
        "provider",
        "source_kind",
        "fetched_at",
        "source_updated_at",
        "status",
        "error_code",
        "schema_version",
        "source_url",
        "locality",
        "days",
    }
    if type(value) is not dict or set(value) not in (fields, fields | {"source_checked_at"}):
        raise ValueError("invalid_power_source_snapshot_shape")
    if len(json.dumps(value).encode("utf-8")) > MAX_OBSERVATION_BYTES:
        raise ValueError("power_source_observation_too_large")
    provider = PowerProvider(value["provider"])
    raw_days = value["days"]
    if type(raw_days) is not list or len(raw_days) > 168:
        raise ValueError("invalid_power_source_days")
    days = []
    for day in raw_days:
        if type(day) is not dict or set(day) != {
            "group",
            "date",
            "kind",
            "mapping",
            "publication",
            "intervals",
        }:
            raise ValueError("invalid_power_source_day_shape")
        intervals = day["intervals"]
        if (
            type(intervals) is not list
            or len(intervals) > 192
            or any(type(item) is not list or len(item) != 3 for item in intervals)
        ):
            raise ValueError("invalid_power_source_intervals")
        days.append(
            PowerDaySchedule(
                provider,
                day["group"],
                date.fromisoformat(day["date"]),
                ScheduleKind(day["kind"]),
                tuple(
                    PowerInterval(day["group"], _date(item[0]), _date(item[1]), PowerState(item[2]))
                    for item in intervals
                ),
                day["mapping"],
                PowerPublicationStatus(day["publication"]),
            )
        )
    snapshot = PowerScheduleSnapshot(
        provider,
        _date(value["fetched_at"]),
        None if value["source_updated_at"] is None else _date(value["source_updated_at"]),
        tuple(days),
        SourceStatus(value["status"]),
        value["error_code"],
        value["schema_version"],
        PowerSourceKind(value["source_kind"]),
        value["source_url"],
        value["locality"],
        None if value.get("source_checked_at") is None else _date(value["source_checked_at"]),
    )
    encode_snapshot(snapshot)
    return snapshot


def initial_state(provider: PowerProvider, source: PowerSourceKind) -> dict[str, Any]:
    _key(provider, source)
    return {
        "schema_version": 1,
        "provider_id": provider.value,
        "source_kind": source.value,
        "enabled": False,
        "priority": SOURCE_ORDER.index(source),
        "revision": 1,
        "source_status": "UNAVAILABLE",
        "verification_status": "UNVERIFIED",
        "structural_status": "UNVERIFIED",
        "matched_count": 0,
        "compared_count": 0,
        "last_pair_key": None,
        "last_success_at": None,
        "last_attempt_at": None,
        "last_error_code": "SOURCE_GATE_BLOCKED",
        "next_attempt_at": None,
        "refresh_requested": False,
        "snapshot": None,
        "collection_allowed": False,
        "notifications_allowed": False,
    }


def validate_state(
    value: object,
    provider: PowerProvider,
    source: PowerSourceKind,
) -> dict[str, Any]:
    default = initial_state(provider, source)
    if type(value) is not dict or set(value) != set(default):
        raise ValueError("invalid_power_source_state_shape")
    if len(json.dumps(value).encode("utf-8")) > MAX_OBSERVATION_BYTES + 4096:
        raise ValueError("power_source_state_too_large")
    if any(
        type(value[key]) is not type(default[key]) or value[key] != default[key]
        for key in (
            "schema_version",
            "provider_id",
            "source_kind",
            "collection_allowed",
            "notifications_allowed",
            "matched_count",
            "compared_count",
            "verification_status",
        )
    ):
        raise ValueError("power_source_approval_not_admitted")
    if (
        type(value["revision"]) is not int
        or value["revision"] < 1
        or type(value["priority"]) is not int
        or not 0 <= value["priority"] <= 100
        or type(value["enabled"]) is not bool
        or type(value["refresh_requested"]) is not bool
    ):
        raise ValueError("invalid_power_source_policy")
    SourceStatus(value["source_status"])
    ComparisonStatus(value["structural_status"])
    if value["last_pair_key"] is not None and (
        type(value["last_pair_key"]) is not str
        or re.fullmatch(r"[a-f0-9]{64}", value["last_pair_key"]) is None
    ):
        raise ValueError("invalid_power_source_pair_key")
    if value["last_error_code"] is not None and (
        type(value["last_error_code"]) is not str
        or re.fullmatch(r"[A-Z0-9_]{1,64}", value["last_error_code"]) is None
    ):
        raise ValueError("invalid_power_source_error_code")
    for key in ("last_success_at", "last_attempt_at", "next_attempt_at"):
        if value[key] is not None:
            _date(value[key])
    if value["snapshot"] is not None:
        snapshot = decode_snapshot(value["snapshot"])
        if snapshot.provider is not provider or snapshot.source_kind is not source:
            raise ValueError("mismatched_power_source_observation")
    return dict(value)


def request_refresh_state(state: dict[str, Any], now: datetime) -> bool:
    """Persist one request per cadence even while G0 denies collection."""
    stamp = aware(now)
    if state["refresh_requested"] or (
        state["next_attempt_at"] is not None and stamp < _date(state["next_attempt_at"])
    ):
        return False
    state["refresh_requested"] = True
    state["next_attempt_at"] = _stamp(stamp + timedelta(seconds=REFRESH_SECONDS))
    state["revision"] += 1
    return True


class PostgresPowerSourceRepository:
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], *, stale_after_seconds: int = 1800
    ) -> None:
        if type(stale_after_seconds) is not int or stale_after_seconds < 1:
            raise ValueError("invalid_power_source_stale_limit")
        self._factory, self._stale_after = session_factory, stale_after_seconds

    async def _locked(
        self, session: AsyncSession, provider: PowerProvider, source: PowerSourceKind
    ) -> RuntimeSetting:
        key = _key(provider, source)
        await session.execute(
            insert(RuntimeSetting)
            .values(
                key=key,
                value=initial_state(provider, source),
                updated_by="power_sources",
            )
            .on_conflict_do_nothing(index_elements=[RuntimeSetting.key])
        )
        row = await session.scalar(
            select(RuntimeSetting).where(RuntimeSetting.key == key).with_for_update()
        )
        assert row is not None
        validate_state(row.value, provider, source)
        return row

    async def initialize(self) -> None:
        async with self._factory() as session, session.begin():
            for provider in PowerProvider:
                for source in SOURCE_ORDER:
                    await self._locked(session, provider, source)

    async def source_states(self, now: datetime) -> tuple[dict[str, object], ...]:
        stamp = aware(now)
        values: list[dict[str, object]] = []
        async with self._factory() as session:
            for provider in PowerProvider:
                for source in SOURCE_ORDER:
                    row = await session.get(RuntimeSetting, _key(provider, source))
                    state = (
                        initial_state(provider, source)
                        if row is None
                        else validate_state(row.value, provider, source)
                    )
                    snapshot = state.pop("snapshot")
                    if snapshot is not None:
                        observed = decode_snapshot(snapshot)
                        if observed.status is SourceStatus.FRESH and not _snapshot_is_fresh(
                            observed, stamp, self._stale_after
                        ):
                            state["source_status"] = "STALE"
                    state["match_percent"] = None
                    values.append(state)
        return tuple(values)

    async def cached_snapshots(self, provider: PowerProvider) -> tuple[PowerScheduleSnapshot, ...]:
        result = []
        async with self._factory() as session:
            for source in SOURCE_ORDER:
                row = await session.get(RuntimeSetting, _key(provider, source))
                if row is not None:
                    value = validate_state(row.value, provider, source)["snapshot"]
                    if value is not None:
                        result.append(decode_snapshot(value))
        return tuple(result)

    @staticmethod
    def _audit(
        session: AsyncSession, actor_id: str, key: str, action: str, details: dict[str, Any]
    ) -> None:
        if (
            type(actor_id) is not str
            or not 1 <= len(actor_id) <= 80
            or any(ord(char) < 32 for char in actor_id)
        ):
            raise ValueError("invalid_power_source_actor")
        session.add(
            AuditLog(
                actor=actor_id,
                action=action,
                result="OK",
                target_type="power_source",
                target_id=key,
                redacted_details=details,
            )
        )

    async def set_source_enabled(
        self,
        provider: PowerProvider,
        source_kind: PowerSourceKind,
        enabled: bool,
        *,
        expected_revision: int,
        actor_id: str,
        now: datetime,
    ) -> int:
        aware(now)
        if type(enabled) is not bool or type(expected_revision) is not int:
            raise ValueError("invalid_power_source_change")
        async with self._factory() as session, session.begin():
            row = await self._locked(session, provider, source_kind)
            state = dict(row.value)
            if state["revision"] != expected_revision:
                raise PowerSourceConflict("power_source_revision_conflict")
            state.update(enabled=enabled, revision=expected_revision + 1)
            self._audit(
                session,
                actor_id,
                row.key,
                "power_source_enabled",
                {
                    "enabled": enabled,
                    "revision": state["revision"],
                    "collection_allowed": False,
                },
            )
            row.value, row.updated_by = state, actor_id
            return expected_revision + 1

    async def request_source_refresh(
        self, provider: PowerProvider, source_kind: PowerSourceKind, *, actor_id: str, now: datetime
    ) -> bool:
        async with self._factory() as session, session.begin():
            row = await self._locked(session, provider, source_kind)
            state = dict(row.value)
            accepted = request_refresh_state(state, now)
            self._audit(
                session,
                actor_id,
                row.key,
                "power_source_refresh",
                {
                    "coalesced": not accepted,
                    "collection_allowed": False,
                },
            )
            row.value, row.updated_by = state, actor_id
            return accepted

    async def acquire_source_slot(
        self,
        provider: PowerProvider,
        source_kind: PowerSourceKind,
        now: datetime,
        interval_seconds: int,
    ) -> int | None:
        _key(provider, source_kind)
        aware(now)
        if type(interval_seconds) is not int or not 300 <= interval_seconds <= 86400:
            raise ValueError("invalid_power_source_interval")
        # No admitted source policy exists. Admin enable/refresh never grants network access.
        return None

    async def record_observation(
        self,
        snapshot: PowerScheduleSnapshot,
        official_snapshot: PowerScheduleSnapshot | None,
        now: datetime,
    ) -> None:
        stamp = aware(now)
        if aware(snapshot.fetched_at) > stamp:
            raise ValueError("future_power_source_observation")
        encoded = encode_snapshot(snapshot)
        comparison = compare_snapshots(
            snapshot, official_snapshot, stamp, stale_after_seconds=self._stale_after
        )
        async with self._factory() as session, session.begin():
            row = await self._locked(session, snapshot.provider, snapshot.source_kind)
            state = dict(row.value)
            if state["last_attempt_at"] is not None and (
                aware(snapshot.fetched_at) < _date(state["last_attempt_at"])
            ):
                return
            state.update(
                snapshot=encoded,
                source_status=snapshot.status.value,
                last_attempt_at=_stamp(snapshot.fetched_at),
                last_error_code=snapshot.error_code,
                refresh_requested=False,
                structural_status=comparison.structural_status.value,
                last_pair_key=comparison.pair_key,
                revision=state["revision"] + 1,
            )
            if snapshot.status is SourceStatus.FRESH:
                state["last_success_at"] = _stamp(snapshot.fetched_at)
            row.value, row.updated_by = state, "power_sources"

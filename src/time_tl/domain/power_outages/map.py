"""Canonical public map wire cache, never notification authority or telemetry."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from types import MappingProxyType
from typing import Any

from .models import SourceStatus, aware
from .normalization import KYIV

MAX_MAP_BYTES = 2_097_152
MAP_STATUSES = frozenset({
    "ScheduleApplies", "NoShutdowns", "WaitingForSchedule", "EmergencyShutdowns",
})


def _text(value: object, maximum: int = 128) -> str:
    if (type(value) is not str or not 1 <= len(value) <= maximum
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError("invalid_power_map_text")
    return value


def _timestamp(value: object, fetched: datetime, *, future: bool = False) -> datetime | None:
    if value is None:
        return None
    stamp = aware(datetime.fromisoformat(_text(value, 40).replace("Z", "+00:00")))
    if not future and stamp > fetched:
        raise ValueError("future_power_map_timestamp")
    return stamp


def _plain(value: object, depth: int = 0) -> Any:
    if depth > 16:
        raise ValueError("power_map_depth_limit")
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise ValueError("invalid_power_map_object")
        return {key: _plain(item, depth + 1) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item, depth + 1) for item in value]
    if value is None or type(value) in {str, int, bool}:
        return value
    raise ValueError("invalid_power_map_value")


def _freeze(value: object) -> Any:
    if type(value) is dict:
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if type(value) is list:
        return tuple(_freeze(item) for item in value)
    return value


def _slots(value: object) -> list[int]:
    if type(value) is not list or len(value) not in {0, 24} or any(
        type(item) is not int or item not in {0, 1, 2, 3} for item in value
    ):
        raise ValueError("invalid_power_map_slots")
    return value


def _announcements(value: object, fetched: datetime) -> list[dict[str, object]]:
    if type(value) is not list or len(value) > 64:
        raise ValueError("invalid_power_map_announcements")
    normalized = []
    for announcement in value:
        if type(announcement) is not dict or not {"kind", "state", "scope"} <= set(
            announcement
        ) or set(announcement) - {
            "kind", "state", "scope", "published_at", "effective_at", "expires_at",
        }:
            raise ValueError("invalid_power_map_announcement")
        _text(announcement["kind"], 64)
        if announcement["state"] not in {"active", "cancelled", "possible"} \
                or announcement["scope"] not in {"supplier", "local", "regional"}:
            raise ValueError("unknown_power_map_announcement")
        for key in ("published_at", "effective_at", "expires_at"):
            if key in announcement:
                _timestamp(announcement[key], fetched, future=key != "published_at")
        normalized.append(dict(announcement))
    return sorted(normalized, key=lambda item: json.dumps(_semantic(item), sort_keys=True))


def _published(value: object) -> dict[str, object]:
    if type(value) is not dict or not {"hours", "status", "date_slug"} <= set(value) \
            or set(value) - {"hours", "status", "date_slug", "possible"}:
        raise ValueError("invalid_power_map_published_plan")
    _slots(value["hours"])
    if value["status"] not in MAP_STATUSES:
        raise ValueError("unknown_power_map_published_status")
    stamp = date.fromisoformat(_text(value["date_slug"], 10))
    if stamp.isoformat() != value["date_slug"]:
        raise ValueError("invalid_power_map_published_date")
    if "possible" in value:
        _slots(value["possible"])
    return dict(value)


def _row(value: object, fetched: datetime) -> dict[str, object]:
    required = {"initiator", "queue", "updated", "today", "tomorrow", "today_date",
                "tomorrow_date", "today_status", "tomorrow_status"}
    allowed = required | {prefix + suffix for prefix in ("today", "tomorrow") for suffix in (
        "_possible", "_checked_at", "_announcements", "_published_schedule",
    )}
    if type(value) is not dict or not required <= set(value) or set(value) - allowed:
        raise ValueError("invalid_power_map_row_schema")
    _text(value["initiator"])
    _text(value["queue"], 64)
    _timestamp(value["updated"], fetched)
    local_today = fetched.astimezone(KYIV).date()
    result = dict(value)
    for prefix, offset in (("today", 0), ("tomorrow", 1)):
        if value[prefix + "_date"] != (local_today + timedelta(days=offset)).isoformat():
            raise ValueError("outdated_power_map_date")
        status = value[prefix + "_status"]
        if status not in MAP_STATUSES:
            raise ValueError("unknown_power_map_status")
        slots = _slots(value[prefix])
        if status == "WaitingForSchedule" and slots:
            raise ValueError("inconsistent_power_map_waiting")
        if status == "ScheduleApplies" and not slots:
            raise ValueError("missing_power_map_planned_hours")
        if status == "NoShutdowns" and any(slots):
            raise ValueError("inconsistent_power_map_no_shutdowns")
        if prefix + "_possible" in value:
            _slots(value[prefix + "_possible"])
        if prefix + "_checked_at" in value:
            _timestamp(value[prefix + "_checked_at"], fetched)
        if prefix + "_announcements" in value:
            result[prefix + "_announcements"] = _announcements(
                value[prefix + "_announcements"], fetched,
            )
        if prefix + "_published_schedule" in value:
            result[prefix + "_published_schedule"] = _published(
                value[prefix + "_published_schedule"],
            )
    return result


def _checked(row: Mapping[str, object], fetched: datetime, day: date | None = None) \
        -> datetime | None:
    if day is not None:
        if type(day) is not date:
            raise ValueError("invalid_power_map_read_date")
        prefixes = [prefix for prefix in ("today", "tomorrow")
                    if row[prefix + "_date"] == day.isoformat()]
    else:
        prefixes = [prefix for prefix in ("today", "tomorrow") if
                    row[prefix + "_status"] in {"ScheduleApplies", "NoShutdowns"} and row[prefix]]
        if not prefixes:
            prefixes = ["today"]
    checks = [_timestamp(row.get(prefix + "_checked_at"), fetched) for prefix in prefixes]
    return min(item for item in checks if item is not None) if checks and all(
        item is not None for item in checks
    ) else None


def _record_status(row: Mapping[str, object], fetched: datetime, now: datetime,
                   ttl: int, day: date | None = None, *,
                   require_publication_timestamp: bool = True) -> SourceStatus:
    if day is not None and type(day) is not date:
        raise ValueError("invalid_power_map_read_date")
    if day is not None and day.isoformat() not in {row["today_date"], row["tomorrow_date"]}:
        return SourceStatus.UNAVAILABLE
    checked = _checked(row, fetched, day)
    if (require_publication_timestamp and row["updated"] is None) or checked is None or not 0 <= (
        aware(now) - aware(fetched)
    ).total_seconds() <= ttl or not 0 <= (aware(now) - checked).total_seconds() <= ttl:
        return SourceStatus.STALE
    return SourceStatus.FRESH


def _semantic(value: object, depth: int = 0) -> Any:
    if depth > 16:
        raise ValueError("power_map_depth_limit")
    if isinstance(value, Mapping):
        return {key: _semantic(item, depth + 1) for key, item in value.items() if key not in {
            "updated", "today_checked_at", "tomorrow_checked_at", "published_at",
        }}
    if isinstance(value, (tuple, list)):
        return [_semantic(item, depth + 1) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class MapArea:
    slug: str
    katottg: str
    name: str

    def __post_init__(self) -> None:
        if re.fullmatch(r"[a-z0-9][a-z0-9-]{0,82}", _text(self.slug, 83)) is None \
                or re.fullmatch(r"UA[0-9]{17}", _text(self.katottg)) is None:
            raise ValueError("invalid_power_map_area")
        _text(self.name, 256)


@dataclass(frozen=True, slots=True)
class RegionMap:
    area: MapArea
    rows: tuple[Mapping[str, object], ...]
    fetched_at: datetime
    stale_after_seconds: int = 1800
    semantic_hash: str = field(init=False)
    source_updated_at: datetime | None = field(init=False)
    checked_at: datetime | None = field(init=False)
    status: SourceStatus = field(init=False)
    error_code: str | None = field(init=False)

    def __post_init__(self) -> None:
        fetched = aware(self.fetched_at)
        if type(self.area) is not MapArea or type(self.rows) is not tuple \
                or len(self.rows) > 1536 or type(self.stale_after_seconds) is not int \
                or not 1 <= self.stale_after_seconds <= 86400:
            raise ValueError("invalid_power_region_map")
        raw = _plain(self.rows)
        if len(json.dumps(raw).encode("utf-8")) > MAX_MAP_BYTES:
            raise ValueError("power_map_size_limit")
        rows = sorted((_row(row, fetched) for row in raw),
                      key=lambda row: (str(row["initiator"]), str(row["queue"])))
        identities = {(row["initiator"], row["queue"]) for row in rows}
        if len(identities) != len(rows):
            raise ValueError("duplicate_power_map_queue")
        publications = [_timestamp(row["updated"], fetched) for row in rows]
        checks = [_checked(row, fetched) for row in rows]
        # Collection can be fresh while publication is emergency/no usable plan.
        checked = min(item for item in checks if item is not None) if checks and all(
            item is not None for item in checks
        ) else None
        updated = min(item for item in publications if item is not None) if publications and all(
            item is not None for item in publications
        ) else None
        status = SourceStatus.UNAVAILABLE if not rows else SourceStatus.FRESH if all(
            _record_status(row, fetched, fetched, self.stale_after_seconds) is SourceStatus.FRESH
            for row in rows
        ) else SourceStatus.STALE
        semantic_rows = _semantic(rows)
        digest = hashlib.sha256(json.dumps({
            "area": [self.area.slug, self.area.katottg], "rows": semantic_rows,
        }, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()
        object.__setattr__(self, "rows", tuple(_freeze(row) for row in rows))
        object.__setattr__(self, "semantic_hash", digest)
        object.__setattr__(self, "source_updated_at", updated)
        object.__setattr__(self, "checked_at", checked)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "error_code", "NO_SCHEDULE" if not rows else
                           "MAP_UNVERIFIED_OR_STALE" if status is SourceStatus.STALE else None)

    @property
    def content_hash(self) -> str:
        return self.semantic_hash

    def status_at(self, now: datetime) -> SourceStatus:
        stamp = aware(now)
        if self.status is not SourceStatus.FRESH:
            return self.status
        if self.checked_at is None or not 0 <= (stamp - aware(self.fetched_at)).total_seconds() \
                <= self.stale_after_seconds or not 0 <= (stamp - self.checked_at).total_seconds() \
                <= self.stale_after_seconds:
            return SourceStatus.STALE
        return self.status

    def record_checked_at(self, initiator: str, queue: str, *, day: date | None = None) \
            -> datetime | None:
        for row in self.rows:
            if row["initiator"] == initiator and row["queue"] == queue:
                return _checked(row, aware(self.fetched_at), day)
        return None

    def record_status(self, initiator: str, queue: str, now: datetime, *,
                      day: date | None = None,
                      require_publication_timestamp: bool = True) -> SourceStatus:
        aware(now)
        for row in self.rows:
            if row["initiator"] == initiator and row["queue"] == queue:
                return _record_status(row, aware(self.fetched_at), now,
                                      self.stale_after_seconds, day,
                                      require_publication_timestamp=require_publication_timestamp)
        return SourceStatus.UNAVAILABLE

    def to_wire(self) -> dict[str, Any]:
        return {"area": {"slug": self.area.slug, "katottg": self.area.katottg,
                         "name": self.area.name}, "rows": _plain(self.rows),
                "fetched_at": aware(self.fetched_at).isoformat(),
                "stale_after_seconds": self.stale_after_seconds}

    @classmethod
    def from_wire(cls, value: object) -> RegionMap:
        if type(value) is not dict or set(value) != {
            "area", "rows", "fetched_at", "stale_after_seconds",
        } or type(value["area"]) is not dict or set(value["area"]) != {
            "slug", "katottg", "name",
        } or type(value["rows"]) is not list:
            raise ValueError("invalid_power_map_cache_shape")
        area = value["area"]
        return cls(MapArea(area["slug"], area["katottg"], area["name"]), tuple(value["rows"]),
                   aware(datetime.fromisoformat(_text(value["fetched_at"], 40))),
                   value["stale_after_seconds"])

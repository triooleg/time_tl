"""Observed aggregate alert statistics; missing observation is never a clear."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import cast
from zoneinfo import ZoneInfo

KYIV = ZoneInfo("Europe/Kyiv")


def aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("alert history timestamp must be timezone-aware")
    return value.astimezone(UTC)


def report_window(now: datetime) -> tuple[datetime, datetime]:
    """The immutable local 22:00 boundary, independent of retry wall-clock."""
    local = aware(now).astimezone(KYIV)
    end = datetime.combine(local.date(), time(22), KYIV)
    if local < end:
        end = datetime.combine(local.date() - timedelta(days=1), time(22), KYIV)
    start = datetime.combine(end.date() - timedelta(days=1), time(22), KYIV)
    return start.astimezone(UTC), end.astimezone(UTC)


@dataclass(frozen=True)
class ObservedSegment:
    started_at: datetime
    ended_at: datetime
    active: bool
    period_id: str | None = None


@dataclass(frozen=True)
class ObservedPeriod:
    period_id: str
    started_at: datetime
    start_confirmed: bool
    ended_at: datetime | None = None
    has_gaps: bool = False
    tracking_ended_at: datetime | None = None


@dataclass(frozen=True)
class AlertStatistics:
    alert_count: int
    total_duration_seconds: int
    carried_over_count: int
    ongoing_count: int
    unknown_start_count: int
    history_complete: bool


def _union_seconds(ranges: list[tuple[datetime, datetime]]) -> float:
    total = 0.0
    last_start = last_end = None
    for start, end in sorted(ranges):
        if last_end is None or start > last_end:
            if last_end is not None and last_start is not None:
                total += (last_end - last_start).total_seconds()
            last_start, last_end = start, end
        else:
            last_end = max(last_end, end)
    if last_end is not None and last_start is not None:
        total += (last_end - last_start).total_seconds()
    return total


def summarize(
    start: datetime,
    end: datetime,
    segments: tuple[ObservedSegment, ...],
    periods: tuple[ObservedPeriod, ...],
) -> AlertStatistics:
    start, end = aware(start), aware(end)
    if end <= start:
        raise ValueError("invalid alert report window")
    all_ranges, active_ranges, active_ids = [], [], set()
    for item in segments:
        lower, upper = aware(item.started_at), aware(item.ended_at)
        if upper < lower or item.active != (item.period_id is not None):
            raise ValueError("invalid alert history segment")
        lower, upper = max(lower, start), min(upper, end)
        if upper <= lower:
            continue
        all_ranges.append((lower, upper))
        if item.active:
            active_ranges.append((lower, upper))
            active_ids.add(item.period_id)
    by_id = {item.period_id: item for item in periods}
    if len(by_id) != len(periods):
        raise ValueError("duplicate alert history period")
    if not active_ids <= by_id.keys():
        raise ValueError("missing alert history period")
    count = carry = ongoing = unknown = 0
    relevant_gaps = False
    for period in periods:
        began = aware(period.started_at)
        finished = aware(period.ended_at) if period.ended_at is not None else None
        tracking_end = (
            aware(period.tracking_ended_at) if period.tracking_ended_at is not None else None
        )
        if tracking_end is not None:
            finished = min(finished, tracking_end) if finished is not None else tracking_end
        if finished is not None and finished < began:
            raise ValueError("invalid alert history period")
        if began >= end or (finished is not None and finished <= start):
            continue
        relevant_gaps = relevant_gaps or period.has_gaps
        if not period.start_confirmed:
            unknown += 1
        elif start <= began < end:
            count += 1
        elif began < start:
            carry += 1
        if finished is None or finished > end:
            ongoing += 1
    complete = (
        _union_seconds(all_ranges) >= (end - start).total_seconds()
        and not unknown
        and not relevant_gaps
    )
    return AlertStatistics(
        count, int(_union_seconds(active_ranges)), carry, ongoing, unknown, complete
    )


def validate_report_payload(payload: Mapping[str, object]) -> Mapping[str, object]:
    raw_start, raw_end = payload["period_start"], payload["period_end"]
    if not isinstance(raw_start, str) or not isinstance(raw_end, str):
        raise ValueError("invalid alert report timestamps")
    start = aware(datetime.fromisoformat(raw_start))
    end = aware(datetime.fromisoformat(raw_end))
    if report_window(end) != (start, end):
        raise ValueError("invalid anchored alert report window")
    for field in ("alert_count", "duration_seconds", "carried_over_count", "ongoing_count"):
        value = payload[field]
        if type(value) is not int or not 0 <= value <= 2**31 - 1:
            raise ValueError("invalid alert report count")
    if "unknown_start_count" in payload:
        value = payload["unknown_start_count"]
        if type(value) is not int or not 0 <= value <= 2**31 - 1:
            raise ValueError("invalid unknown alert start count")
    if cast(int, payload["duration_seconds"]) > (end - start).total_seconds():
        raise ValueError("alert report duration exceeds window")
    if type(payload["is_partial"]) is not bool:
        raise ValueError("invalid alert report coverage")
    return payload

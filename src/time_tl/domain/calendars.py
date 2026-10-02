"""Pure, bounded projections of explicitly visible calendars into a Kyiv day."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from enum import StrEnum
from zoneinfo import ZoneInfo

from .schedule.models import ScheduleProfile
from .schedule.quotas import (
    MAX_LESSON_TITLE_LENGTH,
    MAX_PROFILE_ID_LENGTH,
    MAX_PROFILE_NAME_LENGTH,
    require_length,
    require_quota,
)
from .schedule.resolver import resolve_day

MAX_CALENDAR_SOURCES = 12
KYIV = ZoneInfo("Europe/Kyiv")


class CalendarKind(StrEnum):
    SCHOOL = "SCHOOL"
    PERSONAL = "PERSONAL"
    ACTIVITY = "ACTIVITY"


class ProjectionTimeError(ValueError):
    """A schedule wall time has no unique instant in Europe/Kyiv."""


@dataclass(frozen=True)
class Calendar:
    calendar_id: str
    name: str
    kind: CalendarKind
    profile: ScheduleProfile = field(repr=False)

    def __post_init__(self) -> None:
        _validate_text(self.calendar_id, "calendar_id", MAX_PROFILE_ID_LENGTH)
        _validate_text(self.name, "calendar_name", MAX_PROFILE_NAME_LENGTH)
        if not isinstance(self.kind, CalendarKind):
            raise ValueError("calendar kind must be CalendarKind")
        if not isinstance(self.profile, ScheduleProfile):
            raise ValueError("calendar profile must be ScheduleProfile")


@dataclass(frozen=True)
class CalendarMembership:
    user_id: int
    calendar_id: str
    visible: bool = True

    def __post_init__(self) -> None:
        _validate_user_id(self.user_id)
        _validate_text(self.calendar_id, "calendar_id", MAX_PROFILE_ID_LENGTH)
        if not isinstance(self.visible, bool):
            raise ValueError("membership visibility must be boolean")


@dataclass(frozen=True)
class CalendarSource:
    calendar: Calendar
    membership: CalendarMembership

    def __post_init__(self) -> None:
        if self.calendar.calendar_id != self.membership.calendar_id:
            raise ValueError("membership calendar_id must match its calendar")


@dataclass(frozen=True)
class TimelineEvent:
    event_id: str
    calendar_id: str
    calendar_name: str
    calendar_kind: CalendarKind
    title: str
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        _validate_text(self.event_id, "event_id", MAX_PROFILE_ID_LENGTH)
        _validate_text(self.calendar_id, "calendar_id", MAX_PROFILE_ID_LENGTH)
        _validate_text(self.calendar_name, "calendar_name", MAX_PROFILE_NAME_LENGTH)
        _validate_text(self.title, "event_title", MAX_LESSON_TITLE_LENGTH)
        if not isinstance(self.calendar_kind, CalendarKind):
            raise ValueError("event calendar kind must be CalendarKind")
        _require_aware(self.start)
        _require_aware(self.end)
        if self.start.astimezone(UTC) >= self.end.astimezone(UTC):
            raise ValueError("event end must be after start")


@dataclass(frozen=True)
class MyDay:
    day: date
    events: tuple[TimelineEvent, ...]
    current_events: tuple[TimelineEvent, ...]
    next_events: tuple[TimelineEvent, ...]
    conflicts: tuple[tuple[TimelineEvent, TimelineEvent], ...]


def build_my_day(user_id: int, sources: Iterable[CalendarSource], at: datetime) -> MyDay:
    """Project a local date without activating profiles or consulting alert state.

    Explicit sources may contain inactive profiles. Memberships are supplied by the
    caller; foreign/hidden memberships are omitted. Invalid or duplicate sources
    reject the complete projection before any schedule is resolved.
    """
    _validate_user_id(user_id)
    _require_aware(at)
    local_at = at.astimezone(KYIV)
    day = local_at.date()
    selected: list[Calendar] = []
    calendar_ids: set[str] = set()
    for count, source in enumerate(sources, start=1):
        require_quota(code="max_calendar_sources", actual=count, limit=MAX_CALENDAR_SOURCES)
        calendar_id = source.calendar.calendar_id
        if calendar_id != source.membership.calendar_id:
            raise ValueError("membership calendar_id must match its calendar")
        if calendar_id in calendar_ids:
            raise ValueError("duplicate source calendar_id")
        calendar_ids.add(calendar_id)
        if source.membership.user_id == user_id and source.membership.visible:
            selected.append(source.calendar)

    events: list[TimelineEvent] = []
    for calendar in selected:
        resolved = resolve_day(calendar.profile, day)
        occurrence_keys: set[str] = set()
        for position, lesson in enumerate(resolved.lessons):
            if not lesson.enabled:
                continue
            occurrence_key = lesson.storage_ref or (
                f"{resolved.source}:{position}:{lesson.start.isoformat()}:{lesson.end.isoformat()}"
            )
            if occurrence_key in occurrence_keys:
                raise ValueError("duplicate lesson occurrence identity")
            occurrence_keys.add(occurrence_key)
            identity = json.dumps([calendar.calendar_id, day.isoformat(), occurrence_key])
            event_id = "event:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
            events.append(
                TimelineEvent(
                    event_id=event_id,
                    calendar_id=calendar.calendar_id,
                    calendar_name=calendar.name,
                    calendar_kind=calendar.kind,
                    title=lesson.title,
                    start=_wall_instant(day, lesson.start),
                    end=_wall_instant(day, lesson.end),
                )
            )
    ordered = tuple(
        sorted(
            events, key=lambda event: (event.start, event.end, event.calendar_id, event.event_id)
        )
    )
    current = tuple(event for event in ordered if event.start <= local_at < event.end)
    future = tuple(event for event in ordered if event.start > local_at)
    upcoming = tuple(event for event in future if event.start == future[0].start) if future else ()
    conflicts = tuple(
        (first, second)
        for index, first in enumerate(ordered)
        for second in ordered[index + 1 :]
        if second.start < first.end
    )
    return MyDay(day, ordered, current, upcoming, conflicts)


def _wall_instant(day: date, wall_time: time) -> datetime:
    wall = datetime.combine(day, wall_time)
    candidates: set[datetime] = set()
    for fold in (0, 1):
        candidate = wall.replace(tzinfo=KYIV, fold=fold)
        instant = candidate.astimezone(UTC)
        if instant.astimezone(KYIV).replace(tzinfo=None) == wall:
            candidates.add(instant)
    if len(candidates) != 1:
        reason = "nonexistent" if not candidates else "ambiguous"
        raise ProjectionTimeError(f"{reason} Europe/Kyiv wall time: {wall.isoformat()}")
    return next(iter(candidates)).astimezone(KYIV)


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("projection datetime must be timezone-aware")


def _validate_user_id(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("user_id must be a positive integer")


def _validate_text(value: str, label: str, limit: int) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must not be empty")
    require_length(code=f"{label}_too_long", value=value, limit=limit)

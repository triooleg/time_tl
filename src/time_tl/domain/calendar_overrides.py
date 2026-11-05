"""Actor-only, snapshot-bound occurrence changes; no shared schedule writes."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time
from enum import StrEnum
from uuid import UUID, uuid5

from .calendar_memberships import MAX_PERSISTENT_CALENDARS, CalendarConflict, CalendarLimitExceeded
from .calendars import (
    KYIV,
    MAX_CALENDAR_SOURCES,
    Calendar,
    CalendarKind,
    CalendarMembership,
    CalendarSource,
    MyDay,
    TimelineEvent,
    _wall_instant,
    build_my_day,
)
from .schedule.models import ScheduleProfile
from .schedule.quotas import MAX_LESSON_TITLE_LENGTH

MAX_PERSONAL_OVERRIDES = 500


class OverrideAction(StrEnum):
    CANCEL = "CANCEL"
    MOVE = "MOVE"
    RESET = "RESET"


def _require(ok: bool) -> None:
    if not ok:
        raise ValueError("invalid personal calendar change")


def _uuid(value: UUID) -> None:
    # asyncpg returns a UUID subclass for real PostgreSQL rows.
    _require(isinstance(value, UUID))


def _integer(value: int, *, minimum: int = 1) -> None:
    _require(type(value) is int and minimum <= value < 2**63)


def _day(value: date) -> None:
    _require(type(value) is date)


def _wall(value: time) -> None:
    _require(type(value) is time)
    _require(value.tzinfo is None and value.utcoffset() is None)
    _require(not value.second and not value.microsecond and value.fold == 0)


def _event_times(start: datetime, end: datetime, day: date) -> None:
    _day(day)
    for instant in (start, end):
        _require(type(instant) is datetime)
        _require(instant.tzinfo is not None and instant.utcoffset() is not None)
        local = instant.astimezone(KYIV)
        _require(local.date() == day and not local.second and not local.microsecond)
        unique = _wall_instant(day, local.time().replace(fold=0))
        _require(unique.astimezone(UTC) == instant.astimezone(UTC))
    _require(start.astimezone(UTC) < end.astimezone(UTC))


def _target(action: OverrideAction, day: date | None, start: time | None, end: time | None) -> None:
    _require(type(action) is OverrideAction)
    if action is OverrideAction.MOVE:
        _require(day is not None and start is not None and end is not None)
        assert day is not None and start is not None and end is not None
        _day(day)
        _wall(start)
        _wall(end)
        _require(start < end)
        _event_times(_wall_instant(day, start), _wall_instant(day, end), day)
    else:
        _require(day is None and start is None and end is None)


@dataclass(frozen=True)
class PersonalEventChoice:
    calendar_id: UUID
    calendar_version: int
    snapshot_version: int
    source_day: date
    event_id: UUID
    title: str
    start: datetime
    end: datetime
    override_version: int = 0

    def __post_init__(self) -> None:
        _uuid(self.calendar_id)
        _uuid(self.event_id)
        _integer(self.calendar_version)
        _integer(self.snapshot_version)
        _integer(self.override_version, minimum=0)
        _require(type(self.title) is str and bool(self.title.strip()))
        _require(len(self.title) <= MAX_LESSON_TITLE_LENGTH)
        _event_times(self.start, self.end, self.source_day)
        _require(
            self.event_id
            == _semantic_id(self.calendar_id, self.source_day, self.title, self.start, self.end)
        )


@dataclass(frozen=True)
class PersonalEventChange:
    choice: PersonalEventChoice
    action: OverrideAction
    target_day: date | None = None
    start: time | None = None
    end: time | None = None

    def __post_init__(self) -> None:
        _require(type(self.choice) is PersonalEventChoice)
        _target(self.action, self.target_day, self.start, self.end)


@dataclass(frozen=True)
class PersonalOverride:
    user_id: int = field(repr=False)
    calendar_id: UUID
    snapshot_version: int
    source_day: date
    event_id: UUID
    action: OverrideAction
    version: int
    target_day: date | None = None
    start: time | None = None
    end: time | None = None

    def __post_init__(self) -> None:
        _integer(self.user_id)
        _uuid(self.calendar_id)
        _integer(self.snapshot_version)
        _day(self.source_day)
        _uuid(self.event_id)
        _integer(self.version)
        _target(self.action, self.target_day, self.start, self.end)


def _semantic_id(calendar_id: UUID, day: date, title: str, start: datetime, end: datetime) -> UUID:
    semantic = [
        day.isoformat(),
        title,
        start.astimezone(KYIV).strftime("%H:%M"),
        end.astimezone(KYIV).strftime("%H:%M"),
    ]
    return uuid5(calendar_id, json.dumps(semantic, ensure_ascii=False, separators=(",", ":")))


def occurrence_id(calendar_id: UUID, event: TimelineEvent) -> UUID:
    _uuid(calendar_id)
    _require(type(event) is TimelineEvent)
    _require(event.calendar_id == str(calendar_id))
    day = event.start.astimezone(KYIV).date()
    _event_times(event.start, event.end, day)
    return _semantic_id(calendar_id, day, event.title, event.start, event.end)


def event_choices(
    calendar_id: UUID,
    calendar_version: int,
    snapshot_version: int,
    profile: ScheduleProfile,
    day: date,
    *,
    user_id: int,
) -> tuple[PersonalEventChoice, ...]:
    _uuid(calendar_id)
    _integer(calendar_version)
    _integer(snapshot_version)
    _integer(user_id)
    _day(day)
    _require(type(profile) is ScheduleProfile)
    identity = str(calendar_id)
    source = CalendarSource(
        Calendar(identity, profile.name, CalendarKind.PERSONAL, profile),
        CalendarMembership(user_id, identity),
    )
    events = build_my_day(user_id, (source,), _wall_instant(day, time(12))).events
    identities = [occurrence_id(calendar_id, event) for event in events]
    if len(set(identities)) != len(identities):
        # Do not manufacture distinct handles for identical semantic events.
        raise CalendarConflict()
    return tuple(
        PersonalEventChoice(
            calendar_id,
            calendar_version,
            snapshot_version,
            day,
            event_id,
            event.title,
            event.start,
            event.end,
        )
        for event_id, event in zip(identities, events, strict=True)
    )


def apply_personal_overrides(
    user_id: int,
    sources: Iterable[CalendarSource],
    snapshot_versions: dict[UUID, int],
    overrides: Iterable[PersonalOverride],
    at: datetime,
) -> MyDay:
    _integer(user_id)
    _require(type(at) is datetime and at.tzinfo is not None and at.utcoffset() is not None)
    _require(type(snapshot_versions) is dict)
    if len(snapshot_versions) > MAX_PERSISTENT_CALENDARS:
        raise CalendarLimitExceeded()
    for identifier, version in snapshot_versions.items():
        _uuid(identifier)
        _integer(version)
    bounded: list[CalendarSource] = []
    selected: dict[UUID, CalendarSource] = {}
    persistent = 0
    legacy = 0
    for source in sources:
        if len(bounded) >= MAX_CALENDAR_SOURCES:
            raise CalendarLimitExceeded()
        _require(type(source) is CalendarSource)
        bounded.append(source)
        try:
            identifier = UUID(source.calendar.calendar_id)
        except (ValueError, AttributeError):
            legacy += 1
            if legacy > 1:
                raise CalendarLimitExceeded() from None
            continue  # Uneditable legacy source; base projection stays untouched.
        persistent += 1
        if persistent > MAX_PERSISTENT_CALENDARS:
            raise CalendarLimitExceeded()
        if source.membership.user_id == user_id and source.membership.visible:
            _require(identifier in snapshot_versions)
            _require(identifier not in selected)
            selected[identifier] = source
    base = build_my_day(user_id, bounded, at)
    events = list(base.events)
    resolved: dict[tuple[UUID, date], tuple[TimelineEvent, ...]] = {}
    seen: set[tuple[UUID, int, date, UUID]] = set()
    for count, item in enumerate(overrides, start=1):
        if count > MAX_PERSONAL_OVERRIDES:
            raise CalendarLimitExceeded()
        _require(type(item) is PersonalOverride)
        if item.user_id != user_id or item.calendar_id not in selected:
            continue
        if snapshot_versions[item.calendar_id] != item.snapshot_version:
            continue  # Never rebind a stale personal change to a newer snapshot.
        key = (item.calendar_id, item.snapshot_version, item.source_day, item.event_id)
        if key in seen:
            raise CalendarConflict()
        seen.add(key)
        source = selected[item.calendar_id]
        cache_key = (item.calendar_id, item.source_day)
        if cache_key not in resolved:
            resolved[cache_key] = build_my_day(
                user_id, (source,), _wall_instant(item.source_day, time(12))
            ).events
        matches = [
            event
            for event in resolved[cache_key]
            if occurrence_id(item.calendar_id, event) == item.event_id
        ]
        if len(matches) != 1:
            raise CalendarConflict()
        original = matches[0]
        if item.action is OverrideAction.RESET:
            continue
        if item.source_day == base.day:
            events = [event for event in events if event.event_id != original.event_id]
        if item.action is OverrideAction.MOVE and item.target_day == base.day:
            assert item.start is not None and item.end is not None
            events.append(
                replace(
                    original,
                    event_id="personal:" + str(item.event_id),
                    start=_wall_instant(base.day, item.start),
                    end=_wall_instant(base.day, item.end),
                )
            )
    local_at = at.astimezone(KYIV)
    ordered = tuple(sorted(events, key=lambda e: (e.start, e.end, e.calendar_id, e.event_id)))
    current = tuple(event for event in ordered if event.start <= local_at < event.end)
    future = tuple(event for event in ordered if event.start > local_at)
    upcoming = tuple(event for event in future if event.start == future[0].start) if future else ()
    conflicts = tuple(
        (first, second)
        for index, first in enumerate(ordered)
        for second in ordered[index + 1 :]
        if second.start < first.end
    )
    return MyDay(base.day, ordered, current, upcoming, conflicts)

"""Link-free persistent calendar contracts; no delivery or account mutation."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

from .calendars import CalendarKind, CalendarSource
from .schedule.models import DateOverride, Lesson, Parity, ScheduleProfile, ScheduleRule
from .schedule.quotas import (
    MAX_LESSONS_PER_OVERRIDE,
    MAX_LESSONS_PER_RULE,
    MAX_OVERRIDES_PER_PROFILE,
    MAX_PROFILE_NAME_LENGTH,
    MAX_RULES_PER_PROFILE,
    require_length,
    require_quota,
)

MAX_PERSISTENT_CALENDARS = 11
MAX_INVITATION_HOURS = 24
MAX_CALENDAR_PAYLOAD_BYTES = 256_000
CALENDAR_SCHEMA_VERSION = "1.0"
_TIME = re.compile(r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")
_DATE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")


class CalendarRole(StrEnum):
    OWNER = "OWNER"
    ADMIN = "ADMIN"
    EDITOR = "EDITOR"
    MEMBER = "MEMBER"


class CalendarAccessDenied(ValueError):
    def __init__(self) -> None:
        super().__init__("calendar access denied")


class CalendarConflict(ValueError):
    def __init__(self) -> None:
        super().__init__("calendar changed; refresh and confirm again")


class CalendarLimitExceeded(ValueError):
    def __init__(self) -> None:
        super().__init__("calendar limit reached")


class CalendarDataUnavailable(ValueError):
    def __init__(self) -> None:
        super().__init__("calendar data unavailable")


class CalendarInvitationInvalid(ValueError):
    def __init__(self) -> None:
        super().__init__("calendar invitation unavailable")


@dataclass(frozen=True)
class CalendarSummary:
    calendar_id: UUID
    name: str
    kind: CalendarKind
    role: CalendarRole
    visible: bool
    version: int

    def __post_init__(self) -> None:
        _uuid(self.calendar_id)
        _text(self.name, "calendar name")
        require_length(
            code="calendar_name_too_long", value=self.name, limit=MAX_PROFILE_NAME_LENGTH
        )
        if not isinstance(self.kind, CalendarKind) or not isinstance(self.role, CalendarRole):
            raise ValueError("invalid calendar kind or role")
        _boolean(self.visible)
        if type(self.version) is not int or self.version < 1:
            raise ValueError("calendar version must be a positive integer")


@dataclass(frozen=True)
class InvitationPreview:
    summary: CalendarSummary
    expires_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.summary, CalendarSummary):
            raise ValueError("invitation requires a calendar summary")
        if (
            not isinstance(self.expires_at, datetime)
            or self.expires_at.tzinfo is None
            or self.expires_at.utcoffset() is None
        ):
            raise ValueError("invitation expiry must be timezone-aware")


class AsyncCalendarPort(Protocol):
    async def list_sources(self, user_id: int) -> tuple[CalendarSource, ...]: ...

    async def list_calendars(self, user_id: int) -> tuple[CalendarSummary, ...]: ...


def may_edit(role: CalendarRole) -> bool:
    _role(role)
    return role in (CalendarRole.OWNER, CalendarRole.ADMIN, CalendarRole.EDITOR)


def may_manage(role: CalendarRole) -> bool:
    _role(role)
    return role is CalendarRole.OWNER


def serialize_profile(profile: ScheduleProfile) -> dict[str, Any]:
    """Produce an independent, versioned payload, never copying links or identity."""
    if not isinstance(profile, ScheduleProfile):
        raise ValueError("calendar profile must be ScheduleProfile")
    payload: dict[str, Any] = {
        "schema_version": CALENDAR_SCHEMA_VERSION,
        "timezone": profile.timezone,
        "rules": [
            {
                "weekday": rule.weekday,
                "week_parity": rule.week_parity.value,
                "date_parity": rule.date_parity.value,
                "valid_from": rule.valid_from.isoformat() if rule.valid_from else None,
                "valid_to": rule.valid_to.isoformat() if rule.valid_to else None,
                "lessons": [_lesson_data(lesson) for lesson in rule.lessons],
            }
            for rule in profile.rules
        ],
        "overrides": [
            {
                "day": override.day.isoformat(),
                "day_off": override.day_off,
                "lessons": [_lesson_data(lesson) for lesson in override.lessons],
            }
            for override in profile.overrides
        ],
    }
    # Domain models historically permit some coercible primitives. Revalidate the
    # exact outgoing representation through this strict codec before persistence.
    deserialize_profile(payload, UUID(int=1), profile.name)
    return payload


def deserialize_profile(payload: object, calendar_id: UUID, name: str) -> ScheduleProfile:
    """Read bounded JSON data into an inactive, link-free Kyiv schedule.

    UUID/name are trusted row attributes, not accepted from a shared payload.
    Occurrence references depend only on row position/date, not random values or
    private legacy storage references, and therefore remain stable on every read.
    """
    _uuid(calendar_id)
    _text(name, "calendar name")
    _bounded_json(payload)
    root = _object(payload, {"schema_version", "timezone", "rules", "overrides"})
    version = _text(root["schema_version"], "calendar schema version")
    timezone = _text(root["timezone"], "calendar timezone")
    if version != CALENDAR_SCHEMA_VERSION or timezone != "Europe/Kyiv":
        raise ValueError("unsupported calendar schema or timezone")
    rules = _list(root["rules"], MAX_RULES_PER_PROFILE, "max_rules_per_profile")
    overrides = _list(root["overrides"], MAX_OVERRIDES_PER_PROFILE, "max_overrides_per_profile")
    return ScheduleProfile(
        profile_id=str(calendar_id),
        name=name,
        active=False,
        timezone="Europe/Kyiv",
        rules=tuple(_rule_data(value, index) for index, value in enumerate(rules)),
        overrides=tuple(_override_data(value) for value in overrides),
    )


def _lesson_data(lesson: Lesson) -> dict[str, Any]:
    return {
        "start": lesson.start.isoformat(timespec="minutes"),
        "end": lesson.end.isoformat(timespec="minutes"),
        "title": lesson.title,
        "enabled": lesson.enabled,
    }


def _rule_data(value: object, index: int) -> ScheduleRule:
    item = _object(
        value, {"weekday", "week_parity", "date_parity", "valid_from", "valid_to", "lessons"}
    )
    weekday = item["weekday"]
    if type(weekday) is not int or not 0 <= weekday <= 6:
        raise ValueError("calendar weekday must be an integer in 0..6")
    lessons = _list(item["lessons"], MAX_LESSONS_PER_RULE, "max_lessons_per_rule")
    return ScheduleRule(
        weekday=weekday,
        week_parity=_parity(item["week_parity"]),
        date_parity=_parity(item["date_parity"]),
        valid_from=_date(item["valid_from"], optional=True),
        valid_to=_date(item["valid_to"], optional=True),
        lessons=tuple(
            _lesson(value, f"calendar:rule:{index}:{position}")
            for position, value in enumerate(lessons)
        ),
    )


def _override_data(value: object) -> DateOverride:
    item = _object(value, {"day", "day_off", "lessons"})
    day = _date(item["day"])
    assert day is not None
    lessons = _list(item["lessons"], MAX_LESSONS_PER_OVERRIDE, "max_lessons_per_override")
    return DateOverride(
        day=day,
        day_off=_boolean(item["day_off"]),
        lessons=tuple(
            _lesson(value, f"calendar:override:{day.isoformat()}:{position}")
            for position, value in enumerate(lessons)
        ),
    )


def _lesson(value: object, storage_ref: str) -> Lesson:
    item = _object(value, {"start", "end", "title", "enabled"})
    return Lesson(
        start=_time(item["start"]),
        end=_time(item["end"]),
        title=_text(item["title"], "lesson title"),
        enabled=_boolean(item["enabled"]),
        join_url=None,
        storage_ref=storage_ref,
    )


def _bounded_json(value: object) -> None:
    try:
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        if len(raw.encode("utf-8")) > MAX_CALENDAR_PAYLOAD_BYTES:
            raise ValueError("calendar payload is oversized")
    except (TypeError, RecursionError, UnicodeError) as exc:
        raise ValueError("calendar payload must be bounded JSON data") from exc


def _object(value: object, keys: set[str]) -> dict[str, Any]:
    if type(value) is not dict or set(value) != keys:
        raise ValueError("calendar payload contains unknown or missing fields")
    return value


def _list(value: object, limit: int, code: str) -> list[Any]:
    if type(value) is not list:
        raise ValueError("calendar collection must be a JSON array")
    require_quota(code=code, actual=len(value), limit=limit)
    return value


def _text(value: object, label: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{label} must be a nonempty string")
    return value


def _boolean(value: object) -> bool:
    if type(value) is not bool:
        raise ValueError("calendar flag must be a boolean")
    return value


def _date(value: object, *, optional: bool = False) -> date | None:
    if value is None and optional:
        return None
    if type(value) is not str or _DATE.fullmatch(value) is None:
        raise ValueError("calendar date must use YYYY-MM-DD")
    return date.fromisoformat(value)


def _time(value: object) -> time:
    if type(value) is not str or _TIME.fullmatch(value) is None:
        raise ValueError("calendar time must use naive HH:MM")
    return time.fromisoformat(value)


def _parity(value: object) -> Parity:
    if type(value) is not str:
        raise ValueError("calendar parity must be a string")
    try:
        return Parity(value)
    except ValueError:
        raise ValueError("invalid calendar parity") from None


def _uuid(value: object) -> None:
    if not isinstance(value, UUID):
        raise ValueError("calendar identity must be UUID")


def _role(value: object) -> None:
    if not isinstance(value, CalendarRole):
        raise ValueError("calendar role must be CalendarRole")

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, time
from enum import StrEnum
from urllib.parse import urlparse

from .quotas import (
    MAX_LESSON_JOIN_URL_LENGTH,
    MAX_LESSON_TITLE_LENGTH,
    MAX_LESSONS_PER_OVERRIDE,
    MAX_LESSONS_PER_RULE,
    MAX_OVERRIDE_LESSONS_PER_PROFILE,
    MAX_OVERRIDES_PER_PROFILE,
    MAX_PROFILE_ID_LENGTH,
    MAX_PROFILE_NAME_LENGTH,
    MAX_RECURRING_LESSONS_PER_PROFILE,
    MAX_RULES_PER_PROFILE,
    require_length,
    require_quota,
)


class Parity(StrEnum):
    ANY = "ANY"
    ODD = "ODD"
    EVEN = "EVEN"

    def matches(self, value: int) -> bool:
        return self is Parity.ANY or (value % 2 == 0) is (self is Parity.EVEN)


@dataclass(frozen=True)
class Lesson:
    start: time
    end: time
    title: str
    enabled: bool = True
    join_url: str | None = field(default=None, repr=False)
    storage_ref: str | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        for label, value in (("start", self.start), ("end", self.end)):
            if value.tzinfo is not None or value.utcoffset() is not None:
                raise ValueError(f"lesson {label} must be a naive local wall time")
            if value.second or value.microsecond:
                raise ValueError(f"lesson {label} must have HH:MM precision")
        if self.start >= self.end:
            raise ValueError("lesson end must be after start")
        if not self.title.strip():
            raise ValueError("lesson title must not be empty")
        require_length(
            code="lesson_title_too_long",
            value=self.title,
            limit=MAX_LESSON_TITLE_LENGTH,
        )
        if self.join_url is not None:
            require_length(
                code="lesson_join_url_too_long",
                value=self.join_url,
                limit=MAX_LESSON_JOIN_URL_LENGTH,
            )
            parsed = urlparse(self.join_url)
            if parsed.scheme.lower() != "https" or not parsed.netloc:
                raise ValueError("lesson join_url must use https")
        if self.storage_ref is not None and not self.storage_ref.strip():
            raise ValueError("lesson storage_ref must not be empty")


@dataclass(frozen=True)
class ScheduleRule:
    weekday: int
    lessons: tuple[Lesson, ...]
    week_parity: Parity = Parity.ANY
    date_parity: Parity = Parity.ANY
    valid_from: date | None = None
    valid_to: date | None = None

    def __post_init__(self) -> None:
        if not 0 <= self.weekday <= 6:
            raise ValueError("weekday must be in range 0..6")
        if self.valid_from and self.valid_to and self.valid_from > self.valid_to:
            raise ValueError("valid_from must not be after valid_to")
        require_quota(
            code="max_lessons_per_rule",
            actual=len(self.lessons),
            limit=MAX_LESSONS_PER_RULE,
        )
        _validate_lessons(self.lessons)

    @property
    def specificity(self) -> int:
        if self.week_parity is not Parity.ANY and self.date_parity is not Parity.ANY:
            return 4
        if self.date_parity is not Parity.ANY:
            return 3
        if self.week_parity is not Parity.ANY:
            return 2
        return 1

    def matches(self, day: date) -> bool:
        return (
            day.weekday() == self.weekday
            and (self.valid_from is None or day >= self.valid_from)
            and (self.valid_to is None or day <= self.valid_to)
            and self.week_parity.matches(day.isocalendar().week)
            and self.date_parity.matches(day.day)
        )


@dataclass(frozen=True)
class DateOverride:
    day: date
    lessons: tuple[Lesson, ...] = ()
    day_off: bool = False

    def __post_init__(self) -> None:
        if self.day_off and self.lessons:
            raise ValueError("a day-off override cannot contain lessons")
        require_quota(
            code="max_lessons_per_override",
            actual=len(self.lessons),
            limit=MAX_LESSONS_PER_OVERRIDE,
        )
        _validate_lessons(self.lessons)


@dataclass(frozen=True)
class ScheduleProfile:
    profile_id: str
    name: str
    rules: tuple[ScheduleRule, ...] = ()
    overrides: tuple[DateOverride, ...] = ()
    active: bool = False
    timezone: str = "Europe/Kyiv"

    def __post_init__(self) -> None:
        if not self.profile_id.strip():
            raise ValueError("profile_id must not be empty")
        if not self.name.strip():
            raise ValueError("profile name must not be empty")
        require_length(
            code="profile_id_too_long",
            value=self.profile_id,
            limit=MAX_PROFILE_ID_LENGTH,
        )
        require_length(
            code="profile_name_too_long",
            value=self.name,
            limit=MAX_PROFILE_NAME_LENGTH,
        )
        if self.timezone != "Europe/Kyiv":
            raise ValueError("v1 supports only Europe/Kyiv")
        days = [item.day for item in self.overrides]
        if len(days) != len(set(days)):
            raise ValueError("only one override is allowed per date")
        require_quota(
            code="max_rules_per_profile",
            actual=len(self.rules),
            limit=MAX_RULES_PER_PROFILE,
        )
        require_quota(
            code="max_recurring_lessons_per_profile",
            actual=sum(len(rule.lessons) for rule in self.rules),
            limit=MAX_RECURRING_LESSONS_PER_PROFILE,
        )
        require_quota(
            code="max_overrides_per_profile",
            actual=len(self.overrides),
            limit=MAX_OVERRIDES_PER_PROFILE,
        )
        require_quota(
            code="max_override_lessons_per_profile",
            actual=sum(len(override.lessons) for override in self.overrides),
            limit=MAX_OVERRIDE_LESSONS_PER_PROFILE,
        )


def _validate_lessons(lessons: Iterable[Lesson]) -> None:
    enabled = sorted((lesson for lesson in lessons if lesson.enabled), key=lambda x: x.start)
    for previous, current in zip(enabled, enabled[1:], strict=False):
        if current.start < previous.end:
            raise ValueError("enabled lessons must not overlap")

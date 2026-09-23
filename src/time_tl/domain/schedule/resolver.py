from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum

from .models import Lesson, ScheduleProfile


class ResolutionConflict(ValueError):
    pass


class GapKind(StrEnum):
    BREAK = "BREAK"
    FREE_WINDOW = "FREE_WINDOW"


@dataclass(frozen=True)
class Gap:
    after_title: str
    before_title: str
    minutes: int
    kind: GapKind


@dataclass(frozen=True)
class ResolvedDay:
    day: date
    lessons: tuple[Lesson, ...]
    source: str
    gaps: tuple[Gap, ...]

    @property
    def enabled_lessons(self) -> tuple[Lesson, ...]:
        return tuple(lesson for lesson in self.lessons if lesson.enabled)


def resolve_day(profile: ScheduleProfile, day: date) -> ResolvedDay:
    override = next((item for item in profile.overrides if item.day == day), None)
    if override is not None:
        lessons = () if override.day_off else override.lessons
        return _resolved(day, lessons, "date_override")
    matching = [rule for rule in profile.rules if rule.matches(day)]
    if not matching:
        return _resolved(day, (), "no_rule")
    top_specificity = max(rule.specificity for rule in matching)
    selected = [rule for rule in matching if rule.specificity == top_specificity]
    if len(selected) != 1:
        raise ResolutionConflict(
            f"{len(selected)} equal-specificity rules match {day.isoformat()} "
            f"at specificity {top_specificity}"
        )
    return _resolved(day, selected[0].lessons, f"rule:{top_specificity}")


def preview(profile: ScheduleProfile, start: date, days: int = 14) -> tuple[ResolvedDay, ...]:
    if days < 1 or days > 366:
        raise ValueError("preview days must be in range 1..366")
    return tuple(resolve_day(profile, start + timedelta(days=offset)) for offset in range(days))


def _resolved(day: date, lessons: tuple[Lesson, ...], source: str) -> ResolvedDay:
    enabled = sorted((lesson for lesson in lessons if lesson.enabled), key=lambda x: x.start)
    gaps = []
    for previous, current in zip(enabled, enabled[1:], strict=False):
        minutes = (
            current.start.hour * 60
            + current.start.minute
            - previous.end.hour * 60
            - previous.end.minute
        )
        gaps.append(
            Gap(
                after_title=previous.title,
                before_title=current.title,
                minutes=minutes,
                kind=GapKind.BREAK if minutes <= 20 else GapKind.FREE_WINDOW,
            )
        )
    return ResolvedDay(day=day, lessons=tuple(lessons), source=source, gaps=tuple(gaps))

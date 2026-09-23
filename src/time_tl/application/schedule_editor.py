from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import date, time

from time_tl.domain.schedule import DateOverride, Lesson, Parity, ScheduleProfile, ScheduleRule


@dataclass
class RuleDraft:
    weekday: int
    week_parity: Parity = Parity.ANY
    date_parity: Parity = Parity.ANY
    lessons: list[Lesson] = field(default_factory=list)


class ScheduleEditor:
    """Mutable input model which validates every result through the schedule domain."""

    def __init__(self, profile_id: str, name: str) -> None:
        self.profile_id = profile_id
        self.name = name
        self._rules: dict[tuple[int, Parity, Parity], RuleDraft] = {}
        self._overrides: dict[date, DateOverride] = {}

    @classmethod
    def from_profile(cls, profile: ScheduleProfile) -> ScheduleEditor:
        editor = cls(profile.profile_id, profile.name)
        for rule in profile.rules:
            editor._rules[(rule.weekday, rule.week_parity, rule.date_parity)] = RuleDraft(
                weekday=rule.weekday,
                week_parity=rule.week_parity,
                date_parity=rule.date_parity,
                lessons=list(rule.lessons),
            )
        editor._overrides = {item.day: item for item in profile.overrides}
        return editor

    def add_lesson(
        self,
        *,
        weekday: int,
        start: time,
        end: time,
        title: str,
        enabled: bool = True,
        week_parity: Parity = Parity.ANY,
        date_parity: Parity = Parity.ANY,
        join_url: str | None = None,
    ) -> int:
        key = (weekday, week_parity, date_parity)
        draft = self._rules.setdefault(key, RuleDraft(weekday, week_parity, date_parity))
        draft.lessons.append(Lesson(start, end, title, enabled, join_url))
        try:
            self.build(active=False)
        except Exception:
            draft.lessons.pop()
            if not draft.lessons:
                del self._rules[key]
            raise
        return len(draft.lessons) - 1

    def set_lesson_enabled(
        self,
        *,
        weekday: int,
        lesson_index: int,
        enabled: bool,
        week_parity: Parity = Parity.ANY,
        date_parity: Parity = Parity.ANY,
    ) -> None:
        draft = self._rules[(weekday, week_parity, date_parity)]
        previous = draft.lessons[lesson_index]
        draft.lessons[lesson_index] = replace(previous, enabled=enabled)
        try:
            self.build(active=False)
        except Exception:
            draft.lessons[lesson_index] = previous
            raise

    def set_day_off(self, day: date) -> None:
        self._overrides[day] = DateOverride(day=day, day_off=True)

    def set_date_lessons(self, day: date, lessons: Iterable[Lesson]) -> None:
        self._overrides[day] = DateOverride(day=day, lessons=tuple(lessons))

    def remove_override(self, day: date) -> None:
        self._overrides.pop(day, None)

    def build(self, *, active: bool) -> ScheduleProfile:
        rules = tuple(
            ScheduleRule(
                weekday=draft.weekday,
                week_parity=draft.week_parity,
                date_parity=draft.date_parity,
                lessons=tuple(draft.lessons),
            )
            for _, draft in sorted(
                self._rules.items(),
                key=lambda item: (item[0][0], item[0][1].value, item[0][2].value),
            )
        )
        return ScheduleProfile(
            profile_id=self.profile_id,
            name=self.name,
            rules=rules,
            overrides=tuple(sorted(self._overrides.values(), key=lambda item: item.day)),
            active=active,
        )

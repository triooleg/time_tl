from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import date, time

from time_tl.domain.schedule import DateOverride, Lesson, Parity, ScheduleProfile, ScheduleRule


@dataclass
class RuleDraft:
    weekday: int
    week_parity: Parity = Parity.ANY
    date_parity: Parity = Parity.ANY
    valid_from: date | None = None
    valid_to: date | None = None
    lessons: list[Lesson] = field(default_factory=list)


class StaleLessonSelection(ValueError):
    """The lesson selected by an earlier UI view is no longer the same lesson."""


def recurring_lesson_fingerprint(
    profile: ScheduleProfile, rule_index: int, lesson_index: int
) -> str:
    """Return a compact, non-reversible selector tag for one recurring lesson.

    The tag covers both the rule identity and mutable lesson data. A UI must
    submit it with an edit/delete request so a stale positional selector fails
    closed instead of mutating a neighbouring lesson.
    """

    if rule_index < 0 or lesson_index < 0:
        raise StaleLessonSelection("lesson selection is missing or stale")
    try:
        rule = profile.rules[rule_index]
        lesson = rule.lessons[lesson_index]
    except IndexError as exc:
        raise StaleLessonSelection("lesson selection is missing or stale") from exc
    payload = json.dumps(
        {
            "rule": (
                rule.weekday,
                rule.week_parity.value,
                rule.date_parity.value,
                rule.valid_from.isoformat() if rule.valid_from else None,
                rule.valid_to.isoformat() if rule.valid_to else None,
            ),
            "lesson": (
                lesson.storage_ref,
                lesson.start.isoformat(timespec="minutes"),
                lesson.end.isoformat(timespec="minutes"),
                lesson.title,
                lesson.enabled,
                lesson.join_url,
            ),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.blake2s(payload, digest_size=6).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


class ScheduleEditor:
    """Mutable input model which validates every result through the schedule domain."""

    def __init__(self, profile_id: str, name: str) -> None:
        self.profile_id = profile_id
        self.name = name
        self._rules: list[RuleDraft] = []
        self._overrides: dict[date, DateOverride] = {}

    @classmethod
    def from_profile(cls, profile: ScheduleProfile) -> ScheduleEditor:
        editor = cls(profile.profile_id, profile.name)
        for rule in profile.rules:
            editor._rules.append(
                RuleDraft(
                    weekday=rule.weekday,
                    week_parity=rule.week_parity,
                    date_parity=rule.date_parity,
                    valid_from=rule.valid_from,
                    valid_to=rule.valid_to,
                    lessons=list(rule.lessons),
                )
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
        draft = next(
            (
                item
                for item in self._rules
                if item.weekday == weekday
                and item.week_parity is week_parity
                and item.date_parity is date_parity
                and item.valid_from is None
                and item.valid_to is None
            ),
            None,
        )
        created = draft is None
        if draft is None:
            draft = RuleDraft(weekday, week_parity, date_parity)
            self._rules.append(draft)
        draft.lessons.append(Lesson(start, end, title, enabled, join_url))
        try:
            self.build(active=False)
        except Exception:
            draft.lessons.pop()
            if created:
                self._rules.remove(draft)
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
        matches = [
            item
            for item in self._rules
            if item.weekday == weekday
            and item.week_parity is week_parity
            and item.date_parity is date_parity
        ]
        if len(matches) != 1:
            raise StaleLessonSelection("lesson rule is missing or ambiguous")
        draft = matches[0]
        previous = draft.lessons[lesson_index]
        draft.lessons[lesson_index] = replace(previous, enabled=enabled)
        try:
            self.build(active=False)
        except Exception:
            draft.lessons[lesson_index] = previous
            raise

    def set_lesson_enabled_by_selector(
        self,
        *,
        rule_index: int,
        lesson_index: int,
        expected_fingerprint: str,
        enabled: bool,
    ) -> None:
        draft, previous = self._selected_lesson(rule_index, lesson_index, expected_fingerprint)
        draft.lessons[lesson_index] = replace(previous, enabled=enabled)
        try:
            self.build(active=False)
        except Exception:
            draft.lessons[lesson_index] = previous
            raise

    def edit_lesson(
        self,
        *,
        rule_index: int,
        lesson_index: int,
        expected_fingerprint: str,
        start: time,
        end: time,
        title: str,
    ) -> None:
        """Edit title/time while retaining identity, state, and encrypted-link source."""

        draft, previous = self._selected_lesson(rule_index, lesson_index, expected_fingerprint)
        replacement = replace(previous, start=start, end=end, title=title)
        draft.lessons[lesson_index] = replacement
        try:
            self.build(active=False)
        except Exception:
            draft.lessons[lesson_index] = previous
            raise

    def delete_lesson(
        self,
        *,
        rule_index: int,
        lesson_index: int,
        expected_fingerprint: str,
    ) -> None:
        """Delete exactly one lesson, retaining an empty rule as an explicit schedule."""

        draft, _ = self._selected_lesson(rule_index, lesson_index, expected_fingerprint)
        previous = draft.lessons.pop(lesson_index)
        try:
            self.build(active=False)
        except Exception:
            draft.lessons.insert(lesson_index, previous)
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
                valid_from=draft.valid_from,
                valid_to=draft.valid_to,
                lessons=tuple(draft.lessons),
            )
            for draft in self._rules
        )
        return ScheduleProfile(
            profile_id=self.profile_id,
            name=self.name,
            rules=rules,
            overrides=tuple(sorted(self._overrides.values(), key=lambda item: item.day)),
            active=active,
        )

    def _selected_lesson(
        self, rule_index: int, lesson_index: int, expected_fingerprint: str
    ) -> tuple[RuleDraft, Lesson]:
        profile = self.build(active=False)
        actual = recurring_lesson_fingerprint(profile, rule_index, lesson_index)
        if not hmac.compare_digest(actual, expected_fingerprint):
            raise StaleLessonSelection("lesson selection is missing or stale")
        draft = self._rules[rule_index]
        lesson = draft.lessons[lesson_index]
        return draft, lesson

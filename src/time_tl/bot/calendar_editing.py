"""Bounded, link-free input for the independent weekly calendar editor."""

from __future__ import annotations

import re
from datetime import time

from time_tl.domain.schedule import Lesson, Parity, ScheduleProfile, ScheduleRule
from time_tl.domain.schedule.quotas import MAX_PROFILE_NAME_LENGTH

MAX_CALENDAR_INPUT = 3500
MAX_CALENDAR_LINES = 70
DAY_LABELS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд")
_DAYS = {label.casefold(): index for index, label in enumerate(DAY_LABELS)}
_DAYS.update({str(index + 1): index for index in range(7)})
_LINE = re.compile(r"([^\s]+)\s+([0-9]{2}:[0-9]{2})\s*[-–—]\s*([0-9]{2}:[0-9]{2})\s+(.+)")
_LINK = re.compile(
    r"(?:[a-z][a-z0-9+.-]*://|www\.|(?:https?|tg|mailto):|"
    r"\b[a-z0-9][a-z0-9.-]*\.[a-z]{2,63}\b)",
    re.I | re.ASCII,
)


class CalendarInputError(ValueError):
    """Safe user feedback: never include the submitted text in exceptions."""


def _text(value: str, *, multiline: bool = False) -> str:
    if not isinstance(value, str) or len(value) > MAX_CALENDAR_INPUT:
        raise CalendarInputError("Забагато тексту: максимум 3500 символів.")
    if any(not char.isprintable() and not (multiline and char in "\r\n") for char in value):
        raise CalendarInputError("Текст містить недопустимі символи.")
    if _LINK.search(value):
        raise CalendarInputError("Посилання в цьому редакторі не зберігаються.")
    return value.strip()


def calendar_name(value: str) -> str:
    name = " ".join(_text(value).split())
    if not name or len(name) > MAX_PROFILE_NAME_LENGTH:
        raise CalendarInputError("Назва має містити від 1 до 120 символів.")
    return name


def weekly_rules(value: str) -> tuple[ScheduleRule, ...]:
    text = _text(value, multiline=True)
    if text == "-":
        return ()
    lines = text.splitlines()
    if not lines or len(lines) > MAX_CALENDAR_LINES or any(not line.strip() for line in lines):
        raise CalendarInputError("Надішліть 1–70 рядків занять або «-» для порожнього календаря.")
    grouped: dict[int, list[Lesson]] = {}
    try:
        for line in lines:
            match = _LINE.fullmatch(line.strip())
            if match is None:
                raise ValueError()
            day, start, end, title = match.groups()
            weekday = _DAYS[day.casefold().rstrip(".")]
            start_time = time.fromisoformat(start)
            end_time = time.fromisoformat(end)
            grouped.setdefault(weekday, []).append(
                Lesson(start_time, end_time, " ".join(title.split()))
            )
        return tuple(
            ScheduleRule(day, tuple(sorted(lessons, key=lambda lesson: lesson.start)))
            for day, lessons in sorted(grouped.items())
        )
    except (ValueError, KeyError):
        raise CalendarInputError(
            "Перевірте формат: Пн 08:30–09:15 Математика. "
            "Дні: Пн Вт Ср Чт Пт Сб Нд; час 00:00–23:59, "
            "початок раніше кінця, без накладень; до 16 занять на день."
        ) from None


def weekly_text(profile: ScheduleProfile) -> str:
    """Reject complex rules rather than silently flattening parity or disabled lessons."""
    lines = []
    for rule in profile.rules:
        if (
            rule.week_parity is not Parity.ANY
            or rule.date_parity is not Parity.ANY
            or rule.valid_from is not None
            or rule.valid_to is not None
            or any(not lesson.enabled or lesson.join_url is not None for lesson in rule.lessons)
        ):
            raise CalendarInputError(
                "Цей календар має складні правила, посилання або вимкнені заняття. "
                "Тижневий редактор їх не змінює; назву можна змінити окремо."
            )
        for lesson in rule.lessons:
            lines.append(
                f"{DAY_LABELS[rule.weekday]} {lesson.start:%H:%M}–{lesson.end:%H:%M} {lesson.title}"
            )
    rendered = "\n".join(lines) if lines else "-"
    # Revalidate representability, including cross-rule conflicts, before exposing editing.
    weekly_rules(rendered)
    return rendered

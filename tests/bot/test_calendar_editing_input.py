from datetime import date, time

import pytest

from time_tl.bot.calendar_editing import (
    CalendarInputError,
    calendar_name,
    weekly_rules,
    weekly_text,
)
from time_tl.domain.schedule import Lesson, Parity, ScheduleProfile, ScheduleRule


def test_name_normalizes_spaces() -> None:
    assert calendar_name("  Мій   календар  ") == "Мій календар"


@pytest.mark.parametrize("value", ["", " " * 4, "a" * 121, "Назва\nще", "a\x00", "https://x"])
def test_invalid_name_has_safe_error(value: str) -> None:
    with pytest.raises(CalendarInputError) as error:
        calendar_name(value)
    assert value not in str(error.value) or not value


def test_all_days_and_adjacent_lessons() -> None:
    raw = "\n".join(f"{day} 08:30-09:15 Заняття" for day in range(1, 8))
    rules = weekly_rules(raw + "\nПн 09:15–10:00 Інше")
    assert [rule.weekday for rule in rules] == list(range(7))
    assert len(rules[0].lessons) == 2
    assert all(lesson.join_url is None for rule in rules for lesson in rule.lessons)


@pytest.mark.parametrize(
    "raw",
    [
        "Пн 9:00-10:00 Назва",
        "Пн 24:00-25:00 Назва",
        "Пн 09:00-09:00 Назва",
        "Пн 10:00-09:00 Назва",
        "Пн 09:00-10:00",
        "8 09:00-10:00 Назва",
        "Пн 09:00-10:00 https://example.invalid",
        "Пн 09:00-10:00 Назва\x00",
        "Пн 09:00-10:00 Перше\nПн 09:30-10:30 Друге",
        "Пн 09:00-10:00 Перше\n\nВт 09:00-10:00 Друге",
        "x" * 3501,
        "\n".join("Пн 09:00-10:00 Назва" for _ in range(71)),
        "Пн 09:00-10:00 " + "a" * 161,
    ],
)
def test_invalid_weekly_text(raw: str) -> None:
    with pytest.raises(CalendarInputError):
        weekly_rules(raw)


def test_empty_and_sorted_roundtrip() -> None:
    assert weekly_rules(" - ") == ()
    profile = ScheduleProfile(
        "local", "Назва", weekly_rules("Нд 12:00—13:00 Друге\nпн. 08:00-09:00 Перше")
    )
    assert weekly_rules(weekly_text(profile)) == profile.rules


@pytest.mark.parametrize(
    "kwargs",
    [{"week_parity": Parity.ODD}, {"date_parity": Parity.EVEN}, {"valid_from": date(2026, 1, 1)}],
)
def test_complex_rules_not_silently_flattened(kwargs: dict) -> None:
    profile = ScheduleProfile(
        "id", "Назва", (ScheduleRule(0, (Lesson(time(9), time(10), "Назва"),), **kwargs),)
    )
    with pytest.raises(CalendarInputError):
        weekly_text(profile)


def test_disabled_lesson_and_cross_rule_overlap_rejected() -> None:
    for rules in (
        (ScheduleRule(0, (Lesson(time(9), time(10), "Назва", enabled=False),)),),
        tuple(ScheduleRule(0, (Lesson(time(9), time(10), "Назва"),)) for _ in range(2)),
    ):
        with pytest.raises(CalendarInputError):
            weekly_text(ScheduleProfile("id", "Назва", rules))


def test_daily_quota_even_without_overlap() -> None:
    raw = "\n".join(f"Пн {hour:02}:00-{hour:02}:30 Назва" for hour in range(17))
    with pytest.raises(CalendarInputError):
        weekly_rules(raw)


def test_per_day_maximum_and_empty_roundtrip() -> None:
    raw = "\n".join(f"Пн {hour:02}:00-{hour:02}:30 Назва" for hour in range(16))
    assert len(weekly_rules(raw)[0].lessons) == 16
    assert weekly_text(ScheduleProfile("empty", "Назва")) == "-"


@pytest.mark.parametrize("link", ["t.me/private", "meet.google.com/private", "www.example.invalid"])
def test_bare_domain_links_are_not_stored(link: str) -> None:
    with pytest.raises(CalendarInputError):
        weekly_rules(f"Пн 09:00-10:00 {link}")
    with pytest.raises(CalendarInputError):
        calendar_name(link)


def test_link_bearing_profile_not_silently_flattened() -> None:
    profile = ScheduleProfile(
        "id",
        "Назва",
        (
            ScheduleRule(
                0, (Lesson(time(9), time(10), "Назва", join_url="https://example.invalid/private"),)
            ),
        ),
    )
    with pytest.raises(CalendarInputError):
        weekly_text(profile)

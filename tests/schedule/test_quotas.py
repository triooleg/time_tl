from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

import pytest

from time_tl.domain.schedule import (
    MAX_LESSON_JOIN_URL_LENGTH,
    MAX_LESSON_TITLE_LENGTH,
    MAX_LESSONS_PER_OVERRIDE,
    MAX_LESSONS_PER_RULE,
    MAX_OVERRIDE_LESSONS_PER_PROFILE,
    MAX_OVERRIDES_PER_PROFILE,
    MAX_PROFILE_NAME_LENGTH,
    MAX_RECURRING_LESSONS_PER_PROFILE,
    MAX_RULES_PER_PROFILE,
    DateOverride,
    Lesson,
    Parity,
    ScheduleFieldTooLong,
    ScheduleProfile,
    ScheduleQuotaExceeded,
    ScheduleRule,
    propose_import,
)


def lesson(index: int = 0) -> Lesson:
    start_minute = index * 2
    return Lesson(
        time(8 + start_minute // 60, start_minute % 60),
        time(8 + (start_minute + 1) // 60, (start_minute + 1) % 60),
        "Математика",
    )


def test_lesson_fields_have_typed_persistence_bounds() -> None:
    with pytest.raises(ScheduleFieldTooLong) as title_error:
        Lesson(time(8), time(9), "x" * (MAX_LESSON_TITLE_LENGTH + 1))
    assert title_error.value.code == "lesson_title_too_long"

    with pytest.raises(ScheduleFieldTooLong) as url_error:
        Lesson(
            time(8),
            time(9),
            "Урок",
            join_url="https://example.test/" + "x" * MAX_LESSON_JOIN_URL_LENGTH,
        )
    assert url_error.value.code == "lesson_join_url_too_long"


def test_nested_schedule_shapes_are_bounded() -> None:
    with pytest.raises(ScheduleQuotaExceeded) as rule_error:
        ScheduleRule(0, tuple(lesson(index) for index in range(MAX_LESSONS_PER_RULE + 1)))
    assert rule_error.value.code == "max_lessons_per_rule"

    with pytest.raises(ScheduleQuotaExceeded) as override_error:
        DateOverride(
            date(2026, 1, 1),
            tuple(lesson(index) for index in range(MAX_LESSONS_PER_OVERRIDE + 1)),
        )
    assert override_error.value.code == "max_lessons_per_override"


def test_profile_totals_are_bounded() -> None:
    rules = tuple(ScheduleRule(index % 7, ()) for index in range(MAX_RULES_PER_PROFILE + 1))
    with pytest.raises(ScheduleQuotaExceeded) as rules_error:
        ScheduleProfile("profile", "Розклад", rules=rules)
    assert rules_error.value.code == "max_rules_per_profile"

    recurring_rules = tuple(
        ScheduleRule(index % 7, tuple(lesson(item) for item in range(10)), week_parity=Parity.ANY)
        for index in range(MAX_RECURRING_LESSONS_PER_PROFILE // 10 + 1)
    )
    with pytest.raises(ScheduleQuotaExceeded) as recurring_error:
        ScheduleProfile("profile", "Розклад", rules=recurring_rules)
    assert recurring_error.value.code == "max_recurring_lessons_per_profile"

    start = date(2026, 1, 1)
    overrides = tuple(
        DateOverride(start + timedelta(days=index), day_off=True)
        for index in range(MAX_OVERRIDES_PER_PROFILE + 1)
    )
    with pytest.raises(ScheduleQuotaExceeded) as overrides_error:
        ScheduleProfile("profile", "Розклад", overrides=overrides)
    assert overrides_error.value.code == "max_overrides_per_profile"

    lesson_overrides = tuple(
        DateOverride(
            start + timedelta(days=index),
            tuple(lesson(item) for item in range(MAX_LESSONS_PER_OVERRIDE)),
        )
        for index in range(MAX_OVERRIDE_LESSONS_PER_PROFILE // MAX_LESSONS_PER_OVERRIDE + 1)
    )
    with pytest.raises(ScheduleQuotaExceeded) as total_error:
        ScheduleProfile("profile", "Розклад", overrides=lesson_overrides)
    assert total_error.value.code == "max_override_lessons_per_profile"


def test_import_quota_rejects_before_pending_session_write() -> None:
    class Sessions:
        created = 0

        def create(self, session: object) -> None:
            self.created += 1

    sessions = Sessions()
    payload = {
        "schema_version": "1.0",
        "metadata": {
            "template_name": "Клас",
            "created_at": datetime(2026, 1, 1, tzinfo=UTC).isoformat(),
            "source_display_label": "Однокласник",
            "included_fields": [],
        },
        "profile": {
            "name": "Розклад",
            "timezone": "Europe/Kyiv",
            "rules": [
                {
                    "weekday": index % 7,
                    "lessons": [],
                }
                for index in range(MAX_RULES_PER_PROFILE + 1)
            ],
            "overrides": [],
        },
    }

    with pytest.raises(ScheduleQuotaExceeded) as error:
        propose_import(payload, sessions=sessions)  # type: ignore[arg-type]
    assert error.value.code == "max_rules_per_profile"
    assert sessions.created == 0


def test_profile_name_bound_matches_database_contract() -> None:
    with pytest.raises(ScheduleFieldTooLong) as error:
        ScheduleProfile("profile", "x" * (MAX_PROFILE_NAME_LENGTH + 1))
    assert error.value.code == "profile_name_too_long"

from __future__ import annotations

from datetime import UTC, date, datetime, time
from uuid import uuid4

import pytest

from time_tl.domain.homework import (
    HomeworkItem,
    HomeworkSourceType,
    HomeworkSubject,
    HomeworkValidationError,
    find_next_subject_lesson,
    normalize_subject_name,
    validate_homework_text,
)
from time_tl.domain.schedule import DateOverride, Lesson, ScheduleProfile, ScheduleRule


def test_subject_normalization_is_stable_for_schedule_titles() -> None:
    assert normalize_subject_name("  МАТЕМАТИКА\t ") == "математика"
    assert normalize_subject_name("Українська   мова") == "українська мова"


def test_homework_text_is_bounded_and_nonempty() -> None:
    with pytest.raises(HomeworkValidationError, match="empty"):
        validate_homework_text(" \n ")
    with pytest.raises(HomeworkValidationError, match="too long"):
        validate_homework_text("x" * 2_001)
    assert validate_homework_text("  №428, 429  ") == "№428, 429"


def test_next_lesson_uses_existing_resolver_and_date_override() -> None:
    profile = ScheduleProfile(
        profile_id="school",
        name="5-А",
        active=True,
        rules=(
            ScheduleRule(
                weekday=0,
                lessons=(Lesson(time(9), time(9, 45), "Математика"),),
            ),
        ),
        overrides=(
            DateOverride(
                day=date(2026, 9, 28),
                lessons=(Lesson(time(11), time(11, 45), "Математика"),),
            ),
        ),
    )

    result = find_next_subject_lesson(
        profile,
        "математика",
        datetime(2026, 9, 28, 7, tzinfo=UTC),
    )

    assert result == datetime(2026, 9, 28, 11, tzinfo=result.tzinfo)
    assert result is not None
    assert result.utcoffset() is not None


def test_next_lesson_skips_disabled_and_past_lessons() -> None:
    profile = ScheduleProfile(
        profile_id="school",
        name="5-А",
        active=True,
        rules=(
            ScheduleRule(
                weekday=0,
                lessons=(
                    Lesson(time(8), time(8, 45), "Математика", enabled=False),
                    Lesson(time(9), time(9, 45), "Українська мова"),
                ),
            ),
            ScheduleRule(
                weekday=1,
                lessons=(Lesson(time(10), time(10, 45), "Математика"),),
            ),
        ),
    )

    result = find_next_subject_lesson(
        profile,
        "Математика",
        datetime(2026, 9, 28, 12, tzinfo=UTC),
    )

    assert result is not None
    assert result.date() == date(2026, 9, 29)
    assert result.time().replace(tzinfo=None) == time(10)


def test_completion_timestamp_must_match_personal_status() -> None:
    subject = HomeworkSubject(id=uuid4(), profile_id=uuid4(), name="Математика")
    with pytest.raises(HomeworkValidationError, match="must agree"):
        HomeworkItem(
            id=uuid4(),
            subject=subject,
            text="№428",
            due_at=datetime(2026, 9, 30, tzinfo=UTC),
            completed=True,
            completed_at=None,
            version=1,
            source_type=HomeworkSourceType.MANUAL,
        )

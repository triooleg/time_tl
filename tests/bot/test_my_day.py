from __future__ import annotations

from dataclasses import fields
from datetime import UTC, date, datetime, timedelta

import pytest

from time_tl.bot.models import View
from time_tl.bot.my_day import KYIV, SAFETY_NOTE, projection_unavailable_view, render_my_day
from time_tl.domain.calendars import CalendarKind, MyDay, TimelineEvent

DAY = date(2026, 10, 2)
AT = datetime(2026, 10, 2, 10, 0, tzinfo=KYIV)


def event(
    number: int,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    title: str = "Математика",
    calendar_name: str = "Школа",
) -> TimelineEvent:
    return TimelineEvent(
        f"event:{number:03d}", f"calendar:{number}", calendar_name, CalendarKind.SCHOOL,
        title, start or AT, end or AT + timedelta(hours=1),
    )


def day(*events: TimelineEvent, conflicts: bool = False, selected: date = DAY) -> MyDay:
    pairs = ((events[0], events[1]),) if conflicts else ()
    # Deliberately empty cached groups: the renderer evaluates its own fresh instant.
    return MyDay(selected, events, (), (), pairs)


def callbacks(view: View) -> set[str]:
    return {
        button.callback_data for row in view.rows for button in row
        if button.callback_data is not None
    }


def test_all_overlapping_current_lessons_are_visible_without_choosing_primary() -> None:
    view = render_my_day(
        day(event(1, title="Алгебра"), event(2, title="Гурток"), conflicts=True), at=AT,
    )
    summary = view.text.split("Розклад ·")[0]
    assert "Зараз · занять: 2" in summary
    assert "Алгебра" in summary and "Гурток" in summary
    assert "Перетини занять: 1" in summary
    assert "самостійно" in summary
    assert SAFETY_NOTE in view.text


def test_remaining_minutes_round_up_from_utc_instant() -> None:
    view = render_my_day(day(event(1, end=AT + timedelta(seconds=61))), at=AT)
    assert "ще 2 хв" in view.text
    other_timezone = render_my_day(
        day(event(1, end=AT + timedelta(seconds=61))), at=AT.astimezone(UTC),
    )
    assert other_timezone == view


def test_current_start_is_inclusive_end_exclusive_and_past_is_not_completed() -> None:
    previous = event(1, title="Перший", start=AT - timedelta(hours=1), end=AT)
    current = event(2, title="Другий")
    view = render_my_day(day(previous, current), at=AT)
    summary = view.text.split("Розклад ·")[0]
    assert "Зараз · занять: 1" in summary
    assert "Перший" not in summary
    assert "Другий" in summary
    assert "минуло" in view.text
    assert "виконано" not in view.text and "відвідано" not in view.text


def test_next_group_includes_all_same_instant_without_including_later_lessons() -> None:
    upcoming = AT + timedelta(hours=1)
    events = (
        event(1, title="А", start=upcoming, end=upcoming + timedelta(hours=1)),
        event(2, title="Б", start=upcoming.astimezone(UTC), end=upcoming + timedelta(hours=1)),
        event(3, title="Пізніше", start=AT + timedelta(hours=3), end=AT + timedelta(hours=4)),
    )
    summary = render_my_day(day(*events), at=AT).text.split("Розклад ·")[0]
    assert "Найближчі · 02.10.2026 · 11:00 · занять: 2" in summary
    assert "• А ·" in summary and "• Б ·" in summary
    assert "Пізніше" not in summary


def test_repeated_subject_occurrences_remain_on_timeline() -> None:
    events = tuple(
        event(number, start=AT + timedelta(hours=number), end=AT + timedelta(hours=number + 1))
        for number in range(3)
    )
    timeline_text = render_my_day(day(*events), at=AT).text.split("Розклад ·")[1]
    assert timeline_text.count("Математика") == 3


def test_empty_day_wording_only_describes_enabled_calendars() -> None:
    view = render_my_day(day(), at=AT)
    assert "На вибраний день немає занять з увімкнених календарів." in view.text
    assert "скасовано" not in view.text
    assert SAFETY_NOTE in view.text


def test_pagination_clamps_and_keeps_every_occurrence_accessible() -> None:
    events = tuple(event(number, title=f"Предмет-{number:02d}") for number in range(17))
    first = render_my_day(day(*events), at=AT, page=-999)
    last = render_my_day(day(*events), at=AT, page=10**100)
    assert "сторінка 1/3" in first.text
    assert "myday:page:1" in callbacks(first)
    assert "сторінка 3/3" in last.text
    assert "myday:page:1" in callbacks(last)
    assert "myday:page:3" not in callbacks(last)
    for page_number in range(3):
        text = render_my_day(day(*events), at=AT, page=page_number).text.split("Розклад ·")[1]
        expected = events[page_number * 8:(page_number + 1) * 8]
        assert all(item.title in text for item in expected)
        assert sum(item.title in text for item in events) == len(expected)


def test_large_current_and_next_summaries_have_explicit_omitted_counts() -> None:
    events = tuple(event(number, title=f"Зараз-{number}") for number in range(13))
    events += tuple(
        event(
            number + 20, title=f"Потім-{number}", start=AT + timedelta(hours=2),
            end=AT + timedelta(hours=3),
        )
        for number in range(13)
    )
    text = render_my_day(day(*events), at=AT).text
    assert "Зараз · занять: 13" in text
    assert "Ще 1 зараз" in text
    assert "Найближчі · 02.10.2026 · 12:00 · занять: 13" in text
    assert "Ще 1 одночасно" in text


def test_worst_quota_emoji_names_are_bounded_in_utf16_and_callbacks_in_bytes() -> None:
    current = tuple(
        event(number, title="🧑" * 160, calendar_name="🧑" * 120)
        for number in range(192)
    )
    future = tuple(
        event(
            number + 192, title="🧑" * 160, calendar_name="🧑" * 120,
            start=AT + timedelta(hours=2), end=AT + timedelta(hours=3),
        )
        for number in range(192)
    )
    conflicts = tuple((current[0], item) for item in current[1:])
    timeline = MyDay(DAY, current + future, current, future, conflicts)
    for page in range(48):
        view = render_my_day(timeline, at=AT, page=page, homework_enabled=True, homework_count=100)
        assert len(view.text.encode("utf-16-le")) // 2 <= 4096
        assert all(len(item.encode("utf-8")) <= 64 for item in callbacks(view))
        assert view.text.endswith(SAFETY_NOTE)


def test_untrusted_titles_remove_angles_controls_and_multiline_injection() -> None:
    view = render_my_day(
        day(event(1, title="<b>Алгебра</b>\n\u202eтекст", calendar_name="<script>Школа")),
        at=AT,
    )
    assert "<" not in view.text and ">" not in view.text
    assert "\u202e" not in view.text
    assert "‹b›Алгебра‹/b› текст" in view.text


def test_kyiv_day_and_weekday_apply_at_utc_date_boundary() -> None:
    at = datetime(2026, 10, 1, 22, 15, tzinfo=UTC)
    upcoming = event(1, start=at + timedelta(minutes=15), end=at + timedelta(hours=1))
    view = render_my_day(day(upcoming), at=at)
    assert "Найближчі · 02.10.2026 · 01:30 · занять: 1" in view.text
    assert "02.10.2026 · п’ятниця" in view.text
    with pytest.raises(ValueError, match="Kyiv day"):
        render_my_day(day(selected=date(2026, 10, 1)), at=at)


@pytest.mark.parametrize("invalid", [0.5, "1", None, True])
def test_page_must_be_integer(invalid: object) -> None:
    with pytest.raises(ValueError, match="integer"):
        render_my_day(day(), at=AT, page=invalid)


def test_naive_time_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        render_my_day(day(), at=AT.replace(tzinfo=None))


def test_unavailable_homework_is_never_reported_as_zero() -> None:
    view = render_my_day(day(), at=AT, homework_enabled=True)
    assert "Список тимчасово недоступний" in view.text
    assert "Невиконаних у поточному списку: 0" not in view.text
    assert "menu:homework" in callbacks(view)
    disabled = render_my_day(day(), at=AT, homework_count=3)
    assert "Домашні завдання" not in disabled.text
    assert "menu:homework" not in callbacks(disabled)
    available = render_my_day(day(), at=AT, homework_enabled=True, homework_count=3)
    assert "Невиконаних у поточному списку: 3" in available.text


@pytest.mark.parametrize("invalid", [-1, 101, True, 0.5])
def test_homework_count_is_a_bounded_preview(invalid: object) -> None:
    with pytest.raises(ValueError, match="preview count"):
        render_my_day(day(), at=AT, homework_enabled=True, homework_count=invalid)


def test_projection_failure_keeps_safety_note_and_navigation() -> None:
    view = projection_unavailable_view(homework_enabled=True)
    assert "Розклад тимчасово недоступний" in view.text
    assert SAFETY_NOTE in view.text
    assert callbacks(view) == {"menu:myday", "menu:schedules", "menu:homework", "menu:home"}


def test_event_contract_contains_no_join_url() -> None:
    assert not any("url" in item.name or "join" in item.name for item in fields(TimelineEvent))

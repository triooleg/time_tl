from dataclasses import fields, replace
from datetime import UTC, date, datetime, time
from itertools import repeat
from zoneinfo import ZoneInfo

import pytest

from time_tl.domain.calendars import (
    MAX_CALENDAR_SOURCES,
    Calendar,
    CalendarKind,
    CalendarMembership,
    CalendarSource,
    MyDay,
    ProjectionTimeError,
    TimelineEvent,
    build_my_day,
)
from time_tl.domain.schedule.models import (
    DateOverride,
    Lesson,
    Parity,
    ScheduleProfile,
    ScheduleRule,
)
from time_tl.domain.schedule.quotas import ScheduleFieldTooLong, ScheduleQuotaExceeded
from time_tl.domain.schedule.resolver import ResolutionConflict

KYIV = ZoneInfo("Europe/Kyiv")
MONDAY = date(2026, 9, 21)
AT = datetime(2026, 9, 21, 9, tzinfo=KYIV)


def lesson(title="Math", start="09:00", end="09:45", **kwargs):
    return Lesson(time.fromisoformat(start), time.fromisoformat(end), title, **kwargs)


def source(calendar_id="school", lessons=None, *, user_id=7, visible=True, profile=None):
    if profile is None:
        profile = ScheduleProfile(
            "profile", "Schedule", (ScheduleRule(0, lessons or (lesson(),)),)
        )
    return CalendarSource(
        Calendar(calendar_id, "School", CalendarKind.SCHOOL, profile),
        CalendarMembership(user_id, calendar_id, visible),
    )


def test_empty_projection():
    assert build_my_day(7, (), AT) == MyDay(MONDAY, (), (), (), ())


def test_hidden_and_foreign_memberships_are_omitted_without_resolving():
    conflict = ScheduleProfile(
        "p", "Schedule", (ScheduleRule(0, (lesson(),)), ScheduleRule(0, (lesson(),)))
    )
    sources = (
        source("hidden", visible=False, profile=conflict),
        source("foreign", user_id=8, profile=conflict),
    )
    assert build_my_day(7, sources, AT).events == ()


@pytest.mark.parametrize("user_id", [0, -1, True, "7", 7.0])
def test_invalid_user_ids_rejected(user_id):
    with pytest.raises(ValueError, match="positive integer"):
        CalendarMembership(user_id, "calendar")
    with pytest.raises(ValueError, match="positive integer"):
        build_my_day(user_id, (), AT)


def test_membership_calendar_mismatch_rejected():
    with pytest.raises(ValueError, match="must match"):
        CalendarSource(source().calendar, CalendarMembership(7, "different"))


@pytest.mark.parametrize("calendar_id", ["", " "])
def test_empty_calendar_ids_rejected(calendar_id):
    with pytest.raises(ValueError, match="must not be empty"):
        source(calendar_id)


def test_calendar_and_event_text_is_bounded():
    with pytest.raises(ScheduleFieldTooLong):
        source("c" * 81)
    with pytest.raises(ScheduleFieldTooLong):
        replace(source().calendar, name="n" * 121)
    event = build_my_day(7, (source(),), AT).events[0]
    with pytest.raises(ScheduleFieldTooLong):
        replace(event, title="t" * 161)


def test_typed_calendar_kind_and_visibility_rejected_if_invalid():
    with pytest.raises(ValueError, match="CalendarKind"):
        replace(source().calendar, kind="SCHOOL")
    with pytest.raises(ValueError, match="boolean"):
        CalendarMembership(7, "school", visible=1)


def test_duplicate_sources_rejected_even_when_hidden_or_foreign():
    with pytest.raises(ValueError, match="duplicate source"):
        build_my_day(7, (source(), source(user_id=8, visible=False)), AT)


def test_all_sources_including_hidden_count_towards_named_quota():
    sources = tuple(source(str(index), visible=False) for index in range(MAX_CALENDAR_SOURCES))
    assert build_my_day(7, sources, AT).events == ()
    with pytest.raises(ScheduleQuotaExceeded, match="max_calendar_sources"):
        build_my_day(7, (*sources, source("extra", visible=False)), AT)


def test_input_iteration_stops_at_quota_before_duplicate_validation():
    def sources():
        for index in range(MAX_CALENDAR_SOURCES):
            yield source(str(index))
        yield from repeat(source("extra"))

    with pytest.raises(ScheduleQuotaExceeded):
        build_my_day(7, sources(), AT)


def test_naive_projection_datetime_rejected():
    with pytest.raises(ValueError, match="timezone-aware"):
        build_my_day(7, (), AT.replace(tzinfo=None))


def test_local_day_is_derived_from_instant_not_utc_date():
    projected = build_my_day(7, (source(),), datetime(2026, 9, 20, 22, tzinfo=UTC))
    assert projected.day == MONDAY
    assert len(projected.events) == 1
    assert projected.events[0].start == AT


def test_explicit_inactive_calendar_projects_without_activation_or_copying():
    item = source()
    assert item.calendar.profile.active is False
    assert len(build_my_day(7, (item,), AT).events) == 1
    assert item.calendar.profile.active is False


def test_repeated_subjects_preserved_and_disabled_lessons_omitted():
    item = source(
        lessons=(
            lesson("Math", "11:00", "11:45"),
            lesson("disabled", "10:00", "10:45", enabled=False),
            lesson("Math"),
        )
    )
    events = build_my_day(7, (item,), AT).events
    assert [event.title for event in events] == ["Math", "Math"]
    assert events[0].event_id != events[1].event_id
    assert events[0].start < events[1].start


def test_projection_uses_iso_week_date_parity_and_specificity():
    profile = ScheduleProfile(
        "p",
        "School",
        (
            ScheduleRule(0, (lesson("base"),)),
            ScheduleRule(0, (lesson("week"),), week_parity=Parity.ODD),
            ScheduleRule(0, (lesson("date"),), date_parity=Parity.ODD),
            ScheduleRule(
                0, (lesson("combined"),), week_parity=Parity.ODD, date_parity=Parity.ODD
            ),
        ),
    )
    events = build_my_day(7, (source(profile=profile),), AT).events
    assert [event.title for event in events] == ["combined"]


def test_iso_week_parity_at_calendar_year_boundary():
    # 2027-01-01 belongs to odd ISO week 53 of 2026.
    profile = ScheduleProfile(
        "p", "School", (ScheduleRule(4, (lesson(),), week_parity=Parity.ODD),)
    )
    result = build_my_day(7, (source(profile=profile),), datetime(2027, 1, 1, tzinfo=KYIV))
    assert len(result.events) == 1


def test_specific_date_override_replaces_recurring_schedule():
    profile = replace(
        source().calendar.profile,
        overrides=(DateOverride(MONDAY, (lesson("replacement", "12:00", "12:40"),)),),
    )
    assert [event.title for event in build_my_day(7, (source(profile=profile),), AT).events] == [
        "replacement"
    ]


def test_day_off_and_unconfigured_weekend_have_no_events():
    profile = replace(source().calendar.profile, overrides=(DateOverride(MONDAY, day_off=True),))
    assert build_my_day(7, (source(profile=profile),), AT).events == ()
    assert build_my_day(7, (source(),), datetime(2026, 9, 26, tzinfo=KYIV)).events == ()


def test_working_weekend_supported():
    profile = ScheduleProfile("p", "School", (ScheduleRule(5, (lesson("Saturday"),)),))
    result = build_my_day(7, (source(profile=profile),), datetime(2026, 9, 26, tzinfo=KYIV))
    assert [event.title for event in result.events] == ["Saturday"]


def test_equal_specificity_conflict_propagates_and_returns_no_partial_result():
    profile = ScheduleProfile(
        "p", "School", (ScheduleRule(0, (lesson(),)), ScheduleRule(0, (lesson(),)))
    )
    with pytest.raises(ResolutionConflict):
        build_my_day(7, (source("valid"), source("conflict", profile=profile)), AT)


def test_order_and_ids_are_stable_regardless_of_source_order():
    sources = (source("z"), source("a"))
    first = build_my_day(7, sources, AT)
    second = build_my_day(7, reversed(sources), AT)
    assert first == second
    assert [event.calendar_id for event in first.events] == ["a", "z"]
    assert first.events[0].event_id != first.events[1].event_id


def test_persisted_occurrence_identity_survives_rename_time_edit_and_reordering():
    first = lesson(storage_ref="lesson:rule:fixture-id")
    original = build_my_day(7, (source(lessons=(first,)),), AT).events[0]
    edited = replace(first, title="New title", start=time(10), end=time(10, 45))
    changed = build_my_day(7, (source(lessons=(lesson(), edited)),), AT).events[1]
    assert changed.event_id == original.event_id


def test_occurrence_identity_is_scoped_to_local_date():
    item = source(lessons=(lesson(storage_ref="lesson:rule:fixture-id"),))
    first = build_my_day(7, (item,), AT).events[0]
    later = build_my_day(7, (item,), datetime(2026, 9, 28, tzinfo=KYIV)).events[0]
    assert first.event_id != later.event_id


def test_duplicate_persisted_occurrences_rejected():
    lessons = (
        lesson(storage_ref="same"),
        lesson(start="10:00", end="10:45", storage_ref="same"),
    )
    with pytest.raises(ValueError, match="duplicate lesson occurrence"):
        build_my_day(7, (source(lessons=lessons),), AT)


def test_timeline_contains_no_lesson_or_join_url():
    # Reserved example.invalid is synthetic and never represents a student's link.
    item = source(lessons=(lesson(join_url="https://example.invalid/private"),))
    result = build_my_day(7, (item,), AT)
    assert {field.name for field in fields(TimelineEvent)} == {
        "event_id", "calendar_id", "calendar_name", "calendar_kind", "title", "start", "end"
    }
    assert "example.invalid" not in repr(result)
    assert "example.invalid" not in repr(item)


def test_current_is_half_open_and_returns_all_simultaneous_events():
    sources = (source("a"), source("b"))
    assert len(build_my_day(7, sources, AT).current_events) == 2
    assert len(build_my_day(7, sources, AT.replace(minute=44, second=59)).current_events) == 2
    assert build_my_day(7, sources, AT.replace(minute=45)).current_events == ()


def test_next_returns_all_events_at_the_earliest_future_start():
    sources = (
        source("a", (lesson(start="10:00", end="10:45"),)),
        source("b", (lesson(start="10:00", end="11:00"),)),
        source("c", (lesson(start="11:00", end="11:45"),)),
    )
    result = build_my_day(7, sources, AT)
    assert [event.calendar_id for event in result.next_events] == ["a", "b"]
    assert build_my_day(7, sources, AT.replace(hour=12)).next_events == ()


def test_nested_triple_reports_every_overlap_pair_in_deterministic_order():
    sources = (
        source("outer", (lesson(start="09:00", end="12:00"),)),
        source("middle", (lesson(start="10:00", end="11:00"),)),
        source("inner", (lesson(start="10:15", end="10:45"),)),
    )
    result = build_my_day(7, sources, AT.replace(hour=10, minute=30))
    assert len(result.current_events) == 3
    assert [(a.calendar_id, b.calendar_id) for a, b in result.conflicts] == [
        ("outer", "middle"), ("outer", "inner"), ("middle", "inner")
    ]


def test_adjacent_events_and_variable_gaps_do_not_conflict():
    sources = (
        source("a", (lesson(start="09:00", end="09:45"),)),
        source("b", (lesson(start="09:45", end="10:30"),)),
        source("c", (lesson(start="11:30", end="12:00"),)),
    )
    result = build_my_day(7, sources, AT.replace(minute=45))
    assert result.conflicts == ()
    assert [event.calendar_id for event in result.current_events] == ["b"]


@pytest.mark.parametrize("day", [date(2026, 3, 29), date(2026, 10, 25)])
@pytest.mark.parametrize("start,end", [("03:15", "04:00"), ("02:30", "03:30")])
def test_nonexistent_or_ambiguous_dst_endpoint_rejected(day, start, end):
    profile = ScheduleProfile("p", "School", (ScheduleRule(6, (lesson(start=start, end=end),)),))
    with pytest.raises(ProjectionTimeError):
        build_my_day(7, (source(profile=profile),), datetime.combine(day, time(), KYIV))


@pytest.mark.parametrize("day,minutes", [(date(2026, 3, 29), 60), (date(2026, 10, 25), 180)])
def test_valid_endpoints_spanning_dst_have_correct_elapsed_duration(day, minutes):
    profile = ScheduleProfile(
        "p", "School", (ScheduleRule(6, (lesson(start="02:30", end="04:30"),)),)
    )
    event = build_my_day(
        7, (source(profile=profile),), datetime.combine(day, time(), KYIV)
    ).events[0]
    assert (event.end.astimezone(UTC) - event.start.astimezone(UTC)).total_seconds() == minutes * 60


def test_timeline_event_requires_aware_ordered_endpoints():
    event = build_my_day(7, (source(),), AT).events[0]
    with pytest.raises(ValueError, match="timezone-aware"):
        replace(event, start=event.start.replace(tzinfo=None))
    with pytest.raises(ValueError, match="after start"):
        replace(event, end=event.start)

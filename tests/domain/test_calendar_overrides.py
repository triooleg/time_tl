from __future__ import annotations

import itertools
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID, uuid4

import pytest

from time_tl.domain.calendar_memberships import (
    CalendarConflict,
    CalendarLimitExceeded,
    deserialize_profile,
    serialize_profile,
)
from time_tl.domain.calendar_overrides import (
    OverrideAction,
    PersonalEventChange,
    PersonalEventChoice,
    PersonalOverride,
    apply_personal_overrides,
    event_choices,
    occurrence_id,
)
from time_tl.domain.calendars import (
    KYIV,
    Calendar,
    CalendarKind,
    CalendarMembership,
    CalendarSource,
    ProjectionTimeError,
    build_my_day,
)
from time_tl.domain.schedule.models import DateOverride, Lesson, ScheduleProfile, ScheduleRule

DAY = date(2026, 10, 5)
IDENTITY = UUID("70f2fdb8-e283-4424-b1dd-b6308da5804c")
AT = datetime(2026, 10, 5, 8, 30, tzinfo=KYIV)


def profile() -> ScheduleProfile:
    return ScheduleProfile(
        "independent",
        "Навчання",
        rules=(
            ScheduleRule(
                0, (Lesson(time(9), time(10), "Математика"), Lesson(time(10, 15), time(11), "Мова"))
            ),
            ScheduleRule(1, (Lesson(time(9), time(10), "Історія"),)),
        ),
    )


def source(
    value: ScheduleProfile | None = None,
    *,
    identity: UUID = IDENTITY,
    actor: int = 1,
    visible: bool = True,
) -> CalendarSource:
    return CalendarSource(
        Calendar(str(identity), "Календар", CalendarKind.PERSONAL, value or profile()),
        CalendarMembership(actor, str(identity), visible),
    )


def choices(
    value: ScheduleProfile | None = None, *, day: date = DAY
) -> tuple[PersonalEventChoice, ...]:
    return event_choices(IDENTITY, 4, 2, value or profile(), day, user_id=1)


def change(
    action: OverrideAction,
    choice: PersonalEventChoice | None = None,
    *,
    actor: int = 1,
    target: date | None = None,
    start: time | None = None,
    end: time | None = None,
) -> PersonalOverride:
    item = choice or choices()[0]
    return PersonalOverride(
        actor,
        item.calendar_id,
        item.snapshot_version,
        item.source_day,
        item.event_id,
        action,
        1,
        target,
        start,
        end,
    )


def project(
    rows: tuple[PersonalOverride, ...],
    *,
    at: datetime = AT,
    sources: tuple[CalendarSource, ...] | None = None,
    versions: dict[UUID, int] | None = None,
):
    return apply_personal_overrides(
        1, sources or (source(),), versions if versions is not None else {IDENTITY: 2}, rows, at
    )


def test_uuid_identity_survives_reload_rule_lesson_reorder_name_and_position_refs() -> None:
    original = profile()
    reordered = replace(
        original,
        name="Інша назва",
        rules=tuple(
            reversed(
                (
                    replace(original.rules[0], lessons=tuple(reversed(original.rules[0].lessons))),
                    original.rules[1],
                )
            )
        ),
    )
    loaded = deserialize_profile(serialize_profile(reordered), IDENTITY, reordered.name)
    assert {c.title: c.event_id for c in choices()} == {
        c.title: c.event_id for c in choices(loaded)
    }
    assert choices() == choices()
    assert choices()[0].event_id.version == 5
    events = build_my_day(1, (source(),), AT).events
    altered = replace(events[0], event_id="positional-other", calendar_name="Renamed")
    assert occurrence_id(IDENTITY, events[0]) == occurrence_id(IDENTITY, altered)


def test_semantic_edit_creates_new_identity_and_old_change_is_stale_not_rebound() -> None:
    old = choices()[0]
    value = profile()
    updated = replace(
        value,
        rules=(
            replace(
                value.rules[0],
                lessons=(
                    replace(value.rules[0].lessons[0], title="Алгебра"),
                    value.rules[0].lessons[1],
                ),
            ),
            value.rules[1],
        ),
    )
    assert choices(updated)[0].event_id != old.event_id
    cancel = change(OverrideAction.CANCEL, old)
    actual = project((cancel,), sources=(source(updated),), versions={IDENTITY: 3})
    assert [e.title for e in actual.events] == ["Алгебра", "Мова"]
    with pytest.raises(CalendarConflict):
        project((cancel,), sources=(source(updated),))


def test_cancel_and_reset_are_actor_only_and_preserve_base_event_ids() -> None:
    cancel = change(OverrideAction.CANCEL)
    actual = project((cancel,))
    assert [e.title for e in actual.events] == ["Мова"]
    assert project((replace(cancel, action=OverrideAction.RESET, version=2),)) == build_my_day(
        1, (source(),), AT
    )
    assert project((replace(cancel, user_id=2),)) == build_my_day(1, (source(),), AT)
    assert actual.events[0].event_id == build_my_day(1, (source(),), AT).events[1].event_id
    assert not profile().active


@pytest.mark.parametrize("visibility", ["hidden", "foreign", "left"])
def test_unavailable_calendar_never_leaks_moved_in_event(visibility: str) -> None:
    moved = change(
        OverrideAction.MOVE, target=DAY + timedelta(days=1), start=time(13), end=time(14)
    )
    item = source(visible=visibility != "hidden", actor=2 if visibility == "foreign" else 1)
    if visibility == "left":
        item = source(identity=uuid4())
    actual = project(
        (moved,),
        sources=(item,),
        versions={UUID(item.calendar.calendar_id): 2},
        at=AT + timedelta(days=1),
    )
    assert all(e.title != "Математика" for e in actual.events)


def test_move_cross_date_resolves_original_even_when_not_on_viewed_day() -> None:
    moved = change(
        OverrideAction.MOVE, target=DAY + timedelta(days=1), start=time(9, 30), end=time(10, 30)
    )
    assert [e.title for e in project((moved,)).events] == ["Мова"]
    next_day = project((moved,), at=datetime(2026, 10, 6, 9, 45, tzinfo=KYIV))
    assert [e.title for e in next_day.events] == ["Історія", "Математика"]
    assert len(next_day.current_events) == 2 and len(next_day.conflicts) == 1
    assert next_day.events[1].start.date() == DAY + timedelta(days=1)
    assert next_day.events[1].event_id == "personal:" + str(moved.event_id)
    # No duplicate shared Monday event manufactured on Tuesday.
    assert len(next_day.events) == 2


def test_move_same_day_recomputes_current_next_and_keeps_overlap_visible() -> None:
    moved = change(OverrideAction.MOVE, target=DAY, start=time(10, 30), end=time(11, 30))
    actual = project((moved,), at=AT.replace(hour=10, minute=45))
    assert len(actual.events) == 2 and len(actual.current_events) == 2
    assert len(actual.conflicts) == 1 and not actual.next_events
    early = project((moved,), at=AT)
    assert [e.title for e in early.next_events] == ["Мова"]


def test_date_override_working_weekend_disabled_and_day_off_use_resolver() -> None:
    sunday = DAY - timedelta(days=1)
    value = replace(
        profile(),
        overrides=(
            DateOverride(
                sunday,
                (
                    Lesson(time(8), time(9), "Активний"),
                    Lesson(time(8), time(9), "Вимкнений", enabled=False),
                ),
            ),
            DateOverride(DAY, day_off=True),
        ),
    )
    assert [c.title for c in choices(value, day=sunday)] == ["Активний"]
    assert choices(value) == ()
    assert profile().rules[0].lessons[0].enabled


@pytest.mark.parametrize("day,hour", [(date(2026, 3, 29), 3), (date(2026, 10, 25), 3)])
def test_dst_nonexistent_or_ambiguous_move_rejected(day: date, hour: int) -> None:
    with pytest.raises(ProjectionTimeError):
        PersonalEventChange(choices()[0], OverrideAction.MOVE, day, time(hour), time(5))
    with pytest.raises(ProjectionTimeError):
        change(OverrideAction.MOVE, target=day, start=time(hour), end=time(5))


def test_dst_valid_times_and_utc_view_resolve_kyiv_local_date() -> None:
    target = date(2026, 10, 25)
    moved = change(OverrideAction.MOVE, target=target, start=time(4), end=time(5))
    utc_at = datetime(2026, 10, 24, 22, 30, tzinfo=UTC)
    assert utc_at.astimezone(KYIV).date() == target
    actual = project((moved,), at=utc_at)
    assert actual.day == target and len(actual.events) == 1
    assert actual.events[0].start.utcoffset() == timedelta(hours=2)
    with pytest.raises(ValueError):
        project((), at=AT.replace(tzinfo=None))


def test_duplicate_base_events_remain_but_personal_selection_rejects_ambiguity() -> None:
    # Existing projection must not deduplicate a legacy representation. Create
    # the otherwise-forbidden duplicate via an inert historical corruption fixture.
    value = profile()
    first = value.rules[0].lessons[0]
    rule = ScheduleRule(0, (first,))
    object.__setattr__(
        rule, "lessons", (replace(first, storage_ref="one"), replace(first, storage_ref="two"))
    )
    value = replace(value, rules=(rule,))
    base = build_my_day(1, (source(value),), AT)
    assert len(base.events) == len(project((), sources=(source(value),)).events) == 2
    assert len(base.conflicts) == 1
    with pytest.raises(CalendarConflict):
        choices(value)
    with pytest.raises(CalendarConflict):
        project((change(OverrideAction.CANCEL),), sources=(source(value),))


def test_duplicate_overrides_and_unknown_current_identity_reject() -> None:
    row = change(OverrideAction.CANCEL)
    with pytest.raises(CalendarConflict):
        project((row, row))
    with pytest.raises(CalendarConflict):
        project((replace(row, event_id=uuid4()),))


def test_legacy_and_eleven_calendars_allowed_but_no_additional_sources() -> None:
    empty = ScheduleProfile("empty", "Порожній")
    legacy = CalendarSource(
        Calendar("legacy:test", "Legacy", CalendarKind.SCHOOL, profile()),
        CalendarMembership(1, "legacy:test"),
    )
    ids = [uuid4() for _ in range(11)]
    items = tuple(source(empty, identity=i) for i in ids)
    actual = project((), sources=(legacy, *items), versions=dict.fromkeys(ids, 1))
    assert actual == build_my_day(1, (legacy,), AT)
    with pytest.raises(CalendarLimitExceeded):
        project(
            (), sources=(*items, source(empty, identity=uuid4())), versions=dict.fromkeys(ids, 1)
        )
    with pytest.raises(CalendarLimitExceeded):
        project((), sources=(legacy, legacy))


def test_override_iterable_limit_including_foreign_and_reset_rows() -> None:
    row = replace(change(OverrideAction.RESET), user_id=2)
    actual = apply_personal_overrides(1, (source(),), {IDENTITY: 2}, itertools.repeat(row, 500), AT)
    assert actual == build_my_day(1, (source(),), AT)
    with pytest.raises(CalendarLimitExceeded):
        apply_personal_overrides(1, (source(),), {IDENTITY: 2}, itertools.repeat(row), AT)


@pytest.mark.parametrize(
    "field,value",
    [
        ("user_id", True),
        ("user_id", 0),
        ("calendar_id", str(IDENTITY)),
        ("snapshot_version", True),
        ("snapshot_version", 0),
        ("version", 0),
        ("version", True),
        ("source_day", AT),
        ("event_id", "not-uuid"),
        ("action", "CANCEL"),
        ("target_day", DAY),
        ("start", time(9)),
    ],
)
def test_override_strict_fields(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        replace(change(OverrideAction.CANCEL), **{field: value})


@pytest.mark.parametrize(
    "field,value",
    [
        ("calendar_version", True),
        ("snapshot_version", False),
        ("override_version", -1),
        ("source_day", AT),
        ("title", ""),
        ("title", "x" * 1000),
        ("event_id", uuid4()),
        ("start", AT.replace(tzinfo=None)),
        ("end", AT),
    ],
)
def test_choice_strict_fields(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        replace(choices()[0], **{field: value})


@pytest.mark.parametrize(
    "start,end",
    [
        (time(10), time(9)),
        (time(9), time(9)),
        (time(9, 0, 1), time(10)),
        (time(9, tzinfo=UTC), time(10)),
        (time(9, fold=1), time(10)),
    ],
)
def test_move_strict_minute_same_day_times(start: time, end: time) -> None:
    with pytest.raises(ValueError):
        PersonalEventChange(choices()[0], OverrideAction.MOVE, DAY, start, end)


def test_wrong_versions_inputs_and_sources_reject_without_fallback() -> None:
    with pytest.raises(ValueError):
        project((), versions={})
    with pytest.raises(ValueError):
        project((), versions={IDENTITY: True})
    with pytest.raises(ValueError):
        apply_personal_overrides(True, (), {}, (), AT)
    with pytest.raises(ValueError):
        apply_personal_overrides(1, ("not-source",), {}, (), AT)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        apply_personal_overrides(1, (source(), source()), {IDENTITY: 2}, (), AT)
    with pytest.raises(ValueError):
        occurrence_id(uuid4(), build_my_day(1, (source(),), AT).events[0])


def test_link_and_identity_private_state_never_enters_choices() -> None:
    value = profile()
    lesson = replace(value.rules[0].lessons[0], join_url="https://example.invalid/private")
    value = replace(value, rules=(replace(value.rules[0], lessons=(lesson,)),))
    selected = choices(value)[0]
    assert "private" not in repr(selected) and "join_url" not in repr(selected)
    row = change(OverrideAction.CANCEL, selected, actor=987654321)
    assert "987654321" not in repr(row)

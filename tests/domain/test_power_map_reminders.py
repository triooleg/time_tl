from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta

import pytest

from time_tl.domain.power_outages.map_reminders import (
    OUTAGE,
    RESTORATION,
    MapPowerReminder,
    map_reminder_evidence_hashes,
    plan_map_reminders,
    reminder_due,
)
from time_tl.domain.power_outages.models import (
    PowerDaySchedule,
    PowerInterval,
    PowerProvider,
    PowerPublicationStatus,
    PowerScheduleView,
    PowerSourceKind,
    PowerState,
    ScheduleKind,
    SourceStatus,
    aware,
)
from time_tl.domain.power_outages.normalization import KYIV, day_bounds

DAY = date(2026, 11, 4)


def at(hour: int, minute: int = 0, *, day: date = DAY) -> datetime:
    return aware(datetime(day.year, day.month, day.day, hour, minute, tzinfo=KYIV))


def view(now: datetime, *, day: date = DAY, intervals=None) -> PowerScheduleView:
    lower, upper = day_bounds(day)
    if intervals is None:
        intervals = (
            PowerInterval("6.1", lower, at(8, day=day), PowerState.POWER_EXPECTED),
            PowerInterval("6.1", at(8, day=day), at(10, day=day), PowerState.OUTAGE_CONFIRMED),
            PowerInterval("6.1", at(10, day=day), at(14, day=day), PowerState.POWER_EXPECTED),
            PowerInterval("6.1", at(14, day=day), at(15, day=day), PowerState.OUTAGE_CONFIRMED),
            PowerInterval("6.1", at(15, day=day), upper, PowerState.POWER_EXPECTED),
        )
    schedule = PowerDaySchedule(
        PowerProvider.DTEK,
        "6.1",
        day,
        ScheduleKind.CONFIRMED_DAY,
        intervals,
    )
    return PowerScheduleView(
        PowerProvider.DTEK,
        "6.1",
        SourceStatus.FRESH,
        now,
        now,
        schedule,
        source_kind=PowerSourceKind.ALERTS_ENERGY_API,
    )


def test_default_ten_minute_outage_includes_duration_without_unused_future_claims():
    now = at(7, 50)
    (event,) = plan_map_reminders((view(now),), now)
    assert event.event_type == OUTAGE
    assert event.transition_at == at(8)
    assert event.planned_send_at == now
    assert event.outage_duration_minutes == 120
    assert event.next_outage_starts_at is None
    assert event.next_outage_ends_at is None
    assert event.restoration_confirmed


def test_restoration_ten_minutes_before_requires_explicit_power_expected():
    now = at(9, 50)
    (event,) = plan_map_reminders((view(now),), now)
    assert event.event_type == RESTORATION
    assert event.transition_at == at(10)
    assert event.next_outage_starts_at == at(14)


@pytest.mark.parametrize("lead", [1, 5, 30, 60, 180])
def test_custom_outage_lead(lead):
    now = at(8) - timedelta(minutes=lead)
    (event,) = plan_map_reminders((view(now),), now, outage_lead_minutes=lead)
    assert event.planned_send_at == now


@pytest.mark.parametrize("lead", [-1, 181, True, 1.5, "10"])
def test_invalid_lead_rejected(lead):
    now = at(7, 50)
    with pytest.raises(ValueError):
        plan_map_reminders((view(now),), now, outage_lead_minutes=lead)


def test_due_window_drops_late_restart_and_does_not_replay():
    now = at(7, 50)
    (event,) = plan_map_reminders((view(now),), now)
    assert reminder_due(event, now + timedelta(seconds=59))
    assert not reminder_due(event, now + timedelta(seconds=60))
    assert not reminder_due(event, at(8))
    assert not reminder_due(event, at(8, 1))
    assert not reminder_due(event, now - timedelta(microseconds=1))


def test_zero_lead_has_only_bounded_schedule_qualified_grace():
    now = at(8)
    (event,) = plan_map_reminders((view(now),), now, outage_lead_minutes=0)
    assert reminder_due(event, now)
    assert reminder_due(event, now + timedelta(seconds=59))
    assert not reminder_due(event, now + timedelta(seconds=60))
    assert event.expires_at == now + timedelta(seconds=60)
    assert plan_map_reminders(
        (view(now + timedelta(seconds=59)),), now + timedelta(seconds=59), outage_lead_minutes=0
    )


@pytest.mark.parametrize(
    "status", [SourceStatus.STALE, SourceStatus.INVALID, SourceStatus.UNAVAILABLE]
)
def test_unfresh_views_do_not_plan(status):
    now = at(7, 50)
    assert not plan_map_reminders((replace(view(now), source_status=status),), now)


@pytest.mark.parametrize("age", [1801, -1])
def test_stale_or_future_acquisition_never_plans(age):
    now = at(7, 50)
    candidate = replace(view(now), last_success_at=now - timedelta(seconds=age))
    assert not plan_map_reminders((candidate,), now)


@pytest.mark.parametrize(
    "kind",
    [PowerSourceKind.OFFICIAL, PowerSourceKind.ALERTS_ENERGY_HTML, PowerSourceKind.BEZSVITLA],
)
def test_no_implicit_source_substitution(kind):
    now = at(7, 50)
    assert not plan_map_reminders((replace(view(now), source_kind=kind),), now)


def test_unknown_after_outage_does_not_claim_restoration_or_next_cut():
    now = at(7, 50)
    candidate = view(now)
    day = candidate.schedule
    assert day is not None
    items = tuple(
        replace(item, state=PowerState.UNKNOWN) if item.starts_at == at(10) else item
        for item in day.intervals
    )
    candidate = replace(candidate, schedule=replace(day, intervals=items))
    (event,) = plan_map_reminders((candidate,), now)
    assert not event.restoration_confirmed
    assert event.next_outage_starts_at is None
    assert not plan_map_reminders((replace(candidate, last_success_at=at(9, 50)),), at(9, 50))


def test_unknown_before_outage_does_not_invent_start_boundary():
    now = at(7, 50)
    candidate = view(now)
    day = candidate.schedule
    assert day is not None
    items = (replace(day.intervals[0], state=PowerState.UNKNOWN),) + day.intervals[1:]
    assert not plan_map_reminders(
        (replace(candidate, schedule=replace(day, intervals=items)),), now
    )


def test_midnight_adjacent_outages_merge_and_show_real_duration():
    now = at(22, 50)
    tomorrow = DAY + timedelta(days=1)
    lower, upper = day_bounds(DAY)
    _, tomorrow_upper = day_bounds(tomorrow)
    first = view(
        now,
        intervals=(
            PowerInterval("6.1", lower, at(23), PowerState.POWER_EXPECTED),
            PowerInterval("6.1", at(23), upper, PowerState.OUTAGE_CONFIRMED),
        ),
    )
    second = view(
        now,
        day=tomorrow,
        intervals=(
            PowerInterval("6.1", upper, at(2, day=tomorrow), PowerState.OUTAGE_CONFIRMED),
            PowerInterval("6.1", at(2, day=tomorrow), tomorrow_upper, PowerState.POWER_EXPECTED),
        ),
    )
    (event,) = plan_map_reminders((first, second), now)
    assert event.outage_duration_minutes == 180
    assert event.outage_ends_at == at(2, day=tomorrow)
    assert event.restoration_confirmed
    assert len(event.schedule_hashes) == 2


def test_missing_tomorrow_does_not_turn_midnight_into_restoration():
    now = at(22, 50)
    lower, upper = day_bounds(DAY)
    first = view(
        now,
        intervals=(
            PowerInterval("6.1", lower, at(23), PowerState.POWER_EXPECTED),
            PowerInterval("6.1", at(23), upper, PowerState.OUTAGE_CONFIRMED),
        ),
    )
    (event,) = plan_map_reminders((first,), now)
    assert not event.restoration_confirmed
    assert event.outage_duration_minutes == 60


def test_transition_key_does_not_depend_on_fetch_timestamp():
    now = at(7, 50)
    (first,) = plan_map_reminders((view(now),), now)
    (second,) = plan_map_reminders((view(now + timedelta(seconds=1)),), now + timedelta(seconds=1))
    assert first.transition_key == second.transition_key
    assert first.schedule_hashes == second.schedule_hashes


def test_duplicate_date_and_mixed_identity_rejected():
    now = at(7, 50)
    with pytest.raises(ValueError, match="duplicate"):
        plan_map_reminders((view(now), view(now)), now)
    with pytest.raises(ValueError, match="one_subscription"):
        plan_map_reminders((view(now), replace(view(now), provider=PowerProvider.CEK)), now)


def test_nonplanned_publications_never_plan():
    now = at(7, 50)
    candidate = view(now)
    assert candidate.schedule is not None
    uncertain = PowerDaySchedule(
        PowerProvider.DTEK,
        "6.1",
        DAY,
        ScheduleKind.CONFIRMED_DAY,
        (),
        publication_status=PowerPublicationStatus.EMERGENCY,
    )
    assert not plan_map_reminders((replace(candidate, schedule=uncertain),), now)


def test_wire_roundtrip_and_tamper_rejection():
    now = at(7, 50)
    (event,) = plan_map_reminders((view(now),), now)
    assert MapPowerReminder.from_wire(event.to_wire()) == event
    for changed in (
        {"extra": True},
        {"version": True},
        {"source_kind": "official"},
        {"transition_key": "a" * 64},
        {"transition_at": "2026-11-04T08:00:00"},
        {"outage_start_confirmed": 1},
        {"schedule_hashes": [["2026-11-04", "bad"]]},
        {"next_outage_starts_at": "2026-11-04T00:00:00+00:00"},
    ):
        with pytest.raises(ValueError):
            MapPowerReminder.from_wire(event.to_wire() | changed)


def test_fall_dst_elapsed_duration_uses_utc_offsets():
    fall = date(2026, 10, 25)
    now = at(1, 50, day=fall)
    lower, upper = day_bounds(fall)
    candidate = view(
        now,
        day=fall,
        intervals=(
            PowerInterval("6.1", lower, at(2, day=fall), PowerState.POWER_EXPECTED),
            PowerInterval("6.1", at(2, day=fall), at(5, day=fall), PowerState.OUTAGE_CONFIRMED),
            PowerInterval("6.1", at(5, day=fall), upper, PowerState.POWER_EXPECTED),
        ),
    )
    (event,) = plan_map_reminders((candidate,), now)
    assert event.outage_duration_minutes == 240


def test_unknown_gap_before_next_outage_omits_next_time():
    now = at(9, 50)
    lower, upper = day_bounds(DAY)
    candidate = view(
        now,
        intervals=(
            PowerInterval("6.1", lower, at(8), PowerState.POWER_EXPECTED),
            PowerInterval("6.1", at(8), at(10), PowerState.OUTAGE_CONFIRMED),
            PowerInterval("6.1", at(10), at(12), PowerState.POWER_EXPECTED),
            PowerInterval("6.1", at(12), at(13), PowerState.UNKNOWN),
            PowerInterval("6.1", at(13), at(14), PowerState.OUTAGE_CONFIRMED),
            PowerInterval("6.1", at(14), upper, PowerState.POWER_EXPECTED),
        ),
    )
    (event,) = plan_map_reminders((candidate,), now)
    assert event.event_type == RESTORATION
    assert event.next_outage_starts_at is None


def test_restoration_with_missing_previous_day_has_lower_bound_start():
    now = at(1, 50)
    lower, upper = day_bounds(DAY)
    candidate = view(
        now,
        intervals=(
            PowerInterval("6.1", lower, at(2), PowerState.OUTAGE_CONFIRMED),
            PowerInterval("6.1", at(2), upper, PowerState.POWER_EXPECTED),
        ),
    )
    (event,) = plan_map_reminders((candidate,), now)
    assert event.event_type == RESTORATION
    assert not event.outage_start_confirmed
    assert event.restoration_confirmed


@pytest.mark.parametrize("grace", [0, 61, True, "60"])
def test_invalid_grace_rejected_even_without_views(grace):
    with pytest.raises(ValueError, match="grace"):
        plan_map_reminders((), at(7), due_grace_seconds=grace)


def test_malformed_view_rejected_without_attribute_error():
    with pytest.raises(ValueError, match="views"):
        plan_map_reminders(({},), at(7))


def test_positive_lead_expiry_is_strict_before_transition():
    now = at(7, 59)
    (event,) = plan_map_reminders((view(now),), now, outage_lead_minutes=1)
    assert event.expires_at == at(8)
    assert not reminder_due(event, event.expires_at)


def test_spring_dst_duration_uses_elapsed_utc_hours():
    spring = date(2026, 3, 29)
    now = at(1, 50, day=spring)
    lower, upper = day_bounds(spring)
    candidate = view(
        now,
        day=spring,
        intervals=(
            PowerInterval("6.1", lower, at(2, day=spring), PowerState.POWER_EXPECTED),
            PowerInterval("6.1", at(2, day=spring), at(5, day=spring), PowerState.OUTAGE_CONFIRMED),
            PowerInterval("6.1", at(5, day=spring), upper, PowerState.POWER_EXPECTED),
        ),
    )
    (event,) = plan_map_reminders((candidate,), now)
    assert event.outage_duration_minutes == 120


def test_today_outage_evidence_ignores_new_and_changed_unrelated_tomorrow():
    now = at(7, 50)
    today = view(now)
    (old,) = plan_map_reminders((today,), now)
    tomorrow = view(now, day=DAY + timedelta(days=1))
    (fresh,) = plan_map_reminders((today, tomorrow), now)
    assert fresh.to_wire() == old.to_wire()
    assert map_reminder_evidence_hashes(old, (today, tomorrow), now) == old.schedule_hashes
    assert tomorrow.schedule is not None
    lower, upper = day_bounds(tomorrow.schedule.local_date)
    changed = replace(
        tomorrow,
        schedule=replace(
            tomorrow.schedule,
            intervals=(PowerInterval("6.1", lower, upper, PowerState.POWER_EXPECTED),),
        ),
    )
    (current,) = plan_map_reminders((today, changed), now)
    assert current.to_wire() == old.to_wire()
    assert map_reminder_evidence_hashes(old, (today, changed), now) == old.schedule_hashes
    assert {day for day, _ in old.schedule_hashes} == {DAY.isoformat()}


def test_restoration_old_conservative_claim_scope_survives_new_next_day_info():
    now = at(14, 50)
    today = view(now)
    (old,) = plan_map_reminders((today,), now)
    assert old.next_outage_starts_at is None
    tomorrow = view(now, day=DAY + timedelta(days=1))
    (current,) = plan_map_reminders((today, tomorrow), now)
    assert current.next_outage_starts_at == at(8, day=DAY + timedelta(days=1))
    assert old.schedule_hashes != current.schedule_hashes
    assert map_reminder_evidence_hashes(old, (today, tomorrow), now) == old.schedule_hashes


def test_relevant_today_outage_change_fails_old_claim_verification():
    now = at(7, 50)
    today = view(now)
    (old,) = plan_map_reminders((today,), now)
    assert today.schedule is not None
    changed = replace(
        today,
        schedule=replace(
            today.schedule,
            intervals=(
                replace(today.schedule.intervals[0], ends_at=at(8, 30)),
                replace(today.schedule.intervals[1], starts_at=at(8, 30)),
            )
            + today.schedule.intervals[2:],
        ),
    )
    with pytest.raises(ValueError, match="not_verified"):
        map_reminder_evidence_hashes(old, (changed,), now)


def test_relevant_midnight_restore_change_fails_old_claim_verification():
    now = at(22, 50)
    tomorrow = DAY + timedelta(days=1)
    lower, upper = day_bounds(DAY)
    _, tomorrow_upper = day_bounds(tomorrow)
    first = view(
        now,
        intervals=(
            PowerInterval("6.1", lower, at(23), PowerState.POWER_EXPECTED),
            PowerInterval("6.1", at(23), upper, PowerState.OUTAGE_CONFIRMED),
        ),
    )
    second = view(
        now,
        day=tomorrow,
        intervals=(
            PowerInterval("6.1", upper, at(2, day=tomorrow), PowerState.OUTAGE_CONFIRMED),
            PowerInterval("6.1", at(2, day=tomorrow), tomorrow_upper, PowerState.POWER_EXPECTED),
        ),
    )
    (old,) = plan_map_reminders((first, second), now)
    assert second.schedule is not None
    changed = replace(
        second,
        schedule=replace(
            second.schedule,
            intervals=(
                PowerInterval("6.1", upper, at(3, day=tomorrow), PowerState.OUTAGE_CONFIRMED),
                PowerInterval(
                    "6.1", at(3, day=tomorrow), tomorrow_upper, PowerState.POWER_EXPECTED
                ),
            ),
        ),
    )
    with pytest.raises(ValueError, match="not_verified"):
        map_reminder_evidence_hashes(old, (first, changed), now)


def test_stored_next_outage_positive_claim_must_remain_verified():
    now = at(9, 50)
    today = view(now)
    (old,) = plan_map_reminders((today,), now)
    assert old.next_outage_starts_at == at(14)
    assert today.schedule is not None
    changed = replace(
        today,
        schedule=replace(
            today.schedule,
            intervals=(
                *today.schedule.intervals[:2],
                replace(today.schedule.intervals[2], ends_at=at(14, 30)),
                replace(today.schedule.intervals[3], starts_at=at(14, 30)),
                today.schedule.intervals[4],
            ),
        ),
    )
    with pytest.raises(ValueError, match="not_verified"):
        map_reminder_evidence_hashes(old, (changed,), now)


def test_stored_claims_require_fresh_covering_dates():
    now = at(7, 50)
    today = view(now)
    (old,) = plan_map_reminders((today,), now)
    tomorrow = view(now, day=DAY + timedelta(days=1))
    with pytest.raises(ValueError, match="not_covered"):
        map_reminder_evidence_hashes(
            old,
            (
                replace(today, source_status=SourceStatus.STALE),
                tomorrow,
            ),
            now,
        )

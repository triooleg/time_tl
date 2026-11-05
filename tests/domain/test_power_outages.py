from datetime import UTC, date, datetime, timedelta

import pytest

from time_tl.domain.power_outages.decisions import upcoming_transitions
from time_tl.domain.power_outages.models import (
    GROUPS,
    PowerDaySchedule,
    PowerInterval,
    PowerProvider,
    PowerScheduleSnapshot,
    PowerState,
    PowerSubscription,
    ScheduleKind,
    SourceStatus,
    validate_lead,
)
from time_tl.domain.power_outages.normalization import (
    day_bounds,
    local_boundary,
    map_group,
    normalize_intervals,
)

DAY = date(2026, 11, 4)


def schedule(kind: ScheduleKind = ScheduleKind.CONFIRMED_DAY) -> PowerDaySchedule:
    values = (
        PowerInterval(
            "3.2",
            local_boundary(DAY, "18:00"),
            local_boundary(DAY, "22:00"),
            PowerState.OUTAGE_CONFIRMED,
        ),
        PowerInterval(
            "3.2",
            local_boundary(DAY, "22:00"),
            local_boundary(DAY, "24:00"),
            PowerState.POWER_EXPECTED,
        ),
    )
    return PowerDaySchedule(PowerProvider.DTEK, "3.2", DAY, kind, values)


def test_all_groups_and_independent_regions() -> None:
    assert len(GROUPS) == 12 and len(PowerProvider) == 6
    assert len({item.official_url for item in PowerProvider}) == 6
    mapping = {f"GPV{item}": item for item in GROUPS}
    for group in GROUPS:
        assert map_group(f"GPV{group}", mapping, version="1") == group
    with pytest.raises(ValueError):
        map_group("GPV7.1", mapping, version="1")
    with pytest.raises(ValueError):
        map_group("GPV3.2", mapping, version="2")


@pytest.mark.parametrize("value", [True, False, -1, 181, 1.0, "30", None])
def test_strict_lead(value: object) -> None:
    with pytest.raises(ValueError):
        validate_lead(value)  # type: ignore[arg-type]


def test_preferences_are_independent_and_default_off() -> None:
    value = PowerSubscription("user", PowerProvider.DTEK, "3.2")
    assert not value.enabled
    assert value.outage_lead_minutes == 30 and value.restoration_lead_minutes == 15


def test_dst_civil_day_lengths() -> None:
    for day, hours in ((date(2026, 3, 29), 23), (date(2026, 10, 25), 25), (DAY, 24)):
        lower, upper = day_bounds(day)
        assert (upper - lower).total_seconds() == hours * 3600
        assert local_boundary(day, "24:00") == upper


@pytest.mark.parametrize("day", [date(2026, 3, 29), date(2026, 10, 25)])
def test_source_without_offsets_cannot_guess_dst_hour(day: date) -> None:
    with pytest.raises(ValueError, match="ambiguous_or_nonexistent"):
        local_boundary(day, "03:30")


def test_gaps_are_unknown_not_power_expected_and_adjacent_states_merge() -> None:
    start, _ = day_bounds(DAY)
    rows = (
        PowerInterval(
            "1.1",
            start + timedelta(hours=1),
            start + timedelta(hours=2),
            PowerState.OUTAGE_CONFIRMED,
        ),
        PowerInterval(
            "1.1",
            start + timedelta(hours=2),
            start + timedelta(hours=3),
            PowerState.OUTAGE_CONFIRMED,
        ),
    )
    result = normalize_intervals("1.1", rows, DAY)
    assert [item.state for item in result] == [
        PowerState.UNKNOWN,
        PowerState.OUTAGE_CONFIRMED,
        PowerState.UNKNOWN,
    ]
    assert result[1].ends_at - result[1].starts_at == timedelta(hours=2)


def test_semantic_hash_independent_of_timezone_representation() -> None:
    original = schedule()
    utc_rows = tuple(
        PowerInterval(
            item.group_id, item.starts_at.astimezone(UTC), item.ends_at.astimezone(UTC), item.state
        )
        for item in original.intervals
    )
    assert (
        original.content_hash
        == PowerDaySchedule(
            original.provider, original.group_id, DAY, original.schedule_type, utc_rows
        ).content_hash
    )


def test_outage_and_restoration_use_independent_leads() -> None:
    before_outage = local_boundary(DAY, "17:30")
    (outage,) = upcoming_transitions((schedule(),), before_outage)
    assert outage.event_type == "POWER_OUTAGE_UPCOMING"
    assert outage.transition_at == local_boundary(DAY, "18:00")
    (restore,) = upcoming_transitions((schedule(),), local_boundary(DAY, "21:45"))
    assert restore.event_type == "POWER_RESTORATION_UPCOMING"
    assert restore.transition_at == local_boundary(DAY, "22:00")


def test_late_planning_uses_actual_remaining_time_but_past_is_not_replayed() -> None:
    actual = local_boundary(DAY, "17:55")
    (event,) = upcoming_transitions((schedule(),), actual)
    assert event.transition_at - actual == timedelta(minutes=5)
    assert not upcoming_transitions((schedule(),), local_boundary(DAY, "23:00"))
    assert not upcoming_transitions(
        (schedule(),), local_boundary(DAY, "18:01"), restoration_lead_minutes=0
    )


def test_unknown_following_outage_does_not_confirm_restoration() -> None:
    value = schedule()
    last = value.intervals[-1]
    rows = (
        value.intervals[0],
        PowerInterval("3.2", last.starts_at, last.ends_at, PowerState.UNKNOWN),
    )
    unknown = PowerDaySchedule(value.provider, "3.2", DAY, value.schedule_type, rows)
    assert not upcoming_transitions((unknown,), local_boundary(DAY, "21:45"))
    assert not upcoming_transitions(
        (schedule(ScheduleKind.FORECAST_WEEK),), local_boundary(DAY, "17:30")
    )


def test_zero_lead_strict_boundary_and_no_grace() -> None:
    boundary = local_boundary(DAY, "18:00")
    assert upcoming_transitions((schedule(),), boundary, outage_lead_minutes=0)
    assert not upcoming_transitions(
        (schedule(),),
        boundary + timedelta(microseconds=1),
        outage_lead_minutes=0,
        restoration_lead_minutes=0,
    )


def test_empty_or_failure_snapshots_cannot_claim_fresh_schedules() -> None:
    now = datetime(2026, 11, 4, tzinfo=UTC)
    with pytest.raises(ValueError):
        PowerScheduleSnapshot(PowerProvider.DTEK, now, None, (), SourceStatus.FRESH)
    with pytest.raises(ValueError):
        PowerScheduleSnapshot(PowerProvider.DTEK, now, None, (schedule(),), SourceStatus.INVALID)


def test_cross_midnight_outage_has_no_false_restoration() -> None:
    value = schedule()
    following = DAY + timedelta(days=1)
    midnight = local_boundary(DAY, "24:00")
    first = PowerDaySchedule(
        value.provider,
        "3.2",
        DAY,
        value.schedule_type,
        (
            PowerInterval(
                "3.2", local_boundary(DAY, "23:00"), midnight, PowerState.OUTAGE_CONFIRMED
            ),
        ),
    )
    second = PowerDaySchedule(
        value.provider,
        "3.2",
        following,
        value.schedule_type,
        (
            PowerInterval(
                "3.2", midnight, local_boundary(following, "01:00"), PowerState.OUTAGE_CONFIRMED
            ),
            PowerInterval(
                "3.2",
                local_boundary(following, "01:00"),
                local_boundary(following, "24:00"),
                PowerState.POWER_EXPECTED,
            ),
        ),
    )
    events = upcoming_transitions((first, second), midnight - timedelta(minutes=15))
    assert not events
    (event,) = upcoming_transitions((first, second), local_boundary(following, "00:45"))
    assert event.event_type == "POWER_RESTORATION_UPCOMING"

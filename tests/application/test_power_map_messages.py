from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta

import pytest

from time_tl.application.power_map_messages import (
    render_map_reminder,
    render_map_schedule_change,
)
from time_tl.domain.power_outages.map_reminders import plan_map_reminders
from time_tl.domain.power_outages.models import (
    PowerDaySchedule,
    PowerInterval,
    PowerProvider,
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


def view(now: datetime, *, day: date = DAY, zero=False, partial=False) -> PowerScheduleView:
    lower, upper = day_bounds(day)
    if zero:
        intervals = (PowerInterval("6.1", lower, upper, PowerState.POWER_EXPECTED),)
    elif partial:
        intervals = (PowerInterval("6.1", lower, at(8, day=day), PowerState.POWER_EXPECTED),)
    else:
        intervals = (
            PowerInterval("6.1", lower, at(8, 30, day=day), PowerState.POWER_EXPECTED),
            PowerInterval("6.1", at(8, 30, day=day), at(10, day=day), PowerState.OUTAGE_CONFIRMED),
            PowerInterval("6.1", at(10, day=day), at(14, day=day), PowerState.POWER_EXPECTED),
            PowerInterval("6.1", at(14, day=day), at(15, 30, day=day), PowerState.OUTAGE_CONFIRMED),
            PowerInterval("6.1", at(15, 30, day=day), upper, PowerState.POWER_EXPECTED),
        )
    return PowerScheduleView(
        PowerProvider.DTEK,
        "6.1",
        SourceStatus.FRESH,
        now,
        now,
        PowerDaySchedule(PowerProvider.DTEK, "6.1", day, ScheduleKind.CONFIRMED_DAY, intervals),
        source_kind=PowerSourceKind.ALERTS_ENERGY_API,
        source_url="https://alerts.energy/api/v1/source-registry/areas/dnipropetrovska-oblast/shutdowns",
    )


def test_outage_reminder_has_time_duration_restoration_without_source():
    now = at(8, 20)
    (event,) = plan_map_reminders((view(now),), now)
    text = render_map_reminder(event, now)
    assert "За 10 хв за графіком" in text
    assert "Початок відключення: 08:30" in text
    assert "Тривалість за графіком: 1 год 30 хв" in text
    assert "Відновлення за графіком: 10:00" in text
    assert "Зарядіть телефони й повербанки" in text
    assert "⏰ Початок відключення:" in text
    assert "⏳ Тривалість за графіком:" in text
    assert "🟢 Відновлення за графіком:" in text
    assert "🔋📱 Зарядіть" in text
    assert not any(label in text for label in ("https://", "Alerts Energy", "Джерело:", "FRESH"))


def test_restoration_reminder_has_next_cut_and_charge_after_power_appears():
    now = at(9, 50)
    (event,) = plan_map_reminders((view(now),), now)
    text = render_map_reminder(event, now)
    assert "Відновлення за графіком: 10:00" in text
    assert "Наступне відключення за графіком: 14:00" in text
    assert "Наступне відновлення за графіком: 15:30" in text
    assert "Після появи світла зарядіть телефони й повербанки до наступного відключення" in text
    assert "Фактична наявність світла може відрізнятися" in text
    assert "⏰ Відновлення за графіком:" in text
    assert "⏳ Період відключення:" in text
    assert "🔴 Наступне відключення" in text
    assert "🟢 Наступне відновлення" in text
    assert "🔋📱 Після появи світла" in text


def test_missing_next_schedule_does_not_say_no_more_outages():
    now = at(15, 20)
    (event,) = plan_map_reminders((view(now),), now)
    text = render_map_reminder(event, now)
    assert "Час наступного відключення поки не підтверджено" in text
    assert "відключень немає" not in text


def test_late_reminder_is_not_rendered():
    now = at(8, 20)
    (event,) = plan_map_reminders((view(now),), now)
    with pytest.raises(ValueError, match="not_due"):
        render_map_reminder(event, now + timedelta(minutes=1))


def test_zero_lead_qualified_now_not_physical_power_claim():
    now = at(8, 30)
    (event,) = plan_map_reminders((view(now),), now, outage_lead_minutes=0)
    assert "Зараз за графіком" in render_map_reminder(event, now + timedelta(seconds=59))
    with pytest.raises(ValueError, match="not_due"):
        render_map_reminder(event, now + timedelta(seconds=60))


def test_full_tomorrow_change_shows_every_interval_and_date():
    now = at(20)
    tomorrow = DAY + timedelta(days=1)
    text = render_map_schedule_change(view(now, day=tomorrow), now)
    assert "05.11.2026" in text
    assert "08:30–10:00" in text
    assert "14:00–15:30" in text
    assert not any(label in text for label in ("https://", "Alerts Energy", "Джерело:", "FRESH"))


def test_complete_zero_plan_and_missing_data_are_distinct():
    now = at(20)
    tomorrow = DAY + timedelta(days=1)
    candidate = view(now, day=tomorrow, zero=True)
    assert "Завтра планових відключень немає" in render_map_schedule_change(candidate, now)
    missing = replace(candidate, schedule=None)
    assert "Актуальний графік поки недоступний" in render_map_schedule_change(missing, now)
    assert "відключень немає" not in render_map_schedule_change(missing, now)


def test_partial_expected_plan_never_says_no_outages():
    now = at(20)
    candidate = view(now, day=DAY + timedelta(days=1), partial=True)
    text = render_map_schedule_change(candidate, now)
    assert "відключень немає" not in text
    assert "недоступний" in text


def test_stale_day_does_not_publish_no_outages():
    now = at(20)
    candidate = replace(
        view(now, day=DAY + timedelta(days=1), zero=True), source_status=SourceStatus.STALE
    )
    text = render_map_schedule_change(candidate, now)
    assert "відключень немає" not in text
    assert "недоступний" in text


def test_today_is_not_mislabeled_as_tomorrow():
    now = at(20)
    text = render_map_schedule_change(view(now), now)
    assert "Графік на сьогодні, 04.11.2026" in text
    assert "08:30–10:00" in text
    assert "14:00–15:30" in text
    assert "завтра" not in text


@pytest.mark.parametrize("offset", [-1, 2])
def test_changes_outside_today_tomorrow_are_rejected(offset):
    now = at(20)
    with pytest.raises(ValueError, match="requires_today_or_tomorrow"):
        render_map_schedule_change(view(now, day=DAY + timedelta(days=offset)), now)


def test_today_complete_zero_uses_correct_day_label():
    now = at(20)
    text = render_map_schedule_change(view(now, zero=True), now)
    assert "Сьогодні планових відключень немає" in text
    assert "Завтра" not in text


def test_day_end_unknown_does_not_promise_midnight_restoration():
    now = at(22, 50)
    candidate = view(now)
    assert candidate.schedule is not None
    lower, upper = day_bounds(DAY)
    candidate = replace(
        candidate,
        schedule=replace(
            candidate.schedule,
            intervals=(
                PowerInterval("6.1", lower, at(23), PowerState.POWER_EXPECTED),
                PowerInterval("6.1", at(23), upper, PowerState.OUTAGE_CONFIRMED),
            ),
        ),
    )
    (event,) = plan_map_reminders((candidate,), now)
    text = render_map_reminder(event, now)
    assert "щонайменше 1 год" in text
    assert "Час відновлення ще не підтверджений" in text
    assert "Відновлення за графіком: 24:00" not in text


def test_full_dense_tomorrow_plan_fits_without_silent_truncation():
    now = at(20)
    tomorrow = DAY + timedelta(days=1)
    candidate = view(now, day=tomorrow)
    assert candidate.schedule is not None
    lower, _ = day_bounds(tomorrow)
    intervals = tuple(
        PowerInterval(
            "6.1",
            lower + timedelta(minutes=15 * index),
            lower + timedelta(minutes=15 * (index + 1)),
            PowerState.OUTAGE_CONFIRMED if index % 2 else PowerState.POWER_EXPECTED,
        )
        for index in range(96)
    )
    candidate = replace(candidate, schedule=replace(candidate.schedule, intervals=intervals))
    text = render_map_schedule_change(candidate, now)
    assert text.count("–") == 48
    assert "00:15–00:30" in text
    assert "23:45–24:00" in text
    assert len(text.encode("utf-16-le")) // 2 <= 4096

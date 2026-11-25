"""Plain Ukrainian schedule-qualified API map messages without source metadata."""

from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta

from time_tl.domain.power_outages.map_reminders import (
    OUTAGE,
    MapPowerReminder,
    reminder_due,
    usable_map_day,
)
from time_tl.domain.power_outages.models import PowerProvider, PowerScheduleView, PowerState, aware
from time_tl.domain.power_outages.normalization import KYIV


def _clock(value: datetime, *, reference_day: date | None = None) -> str:
    local = aware(value).astimezone(KYIV)
    if (
        reference_day is not None
        and local.date() == reference_day + timedelta(days=1)
        and local.time() == time.min
    ):
        return "24:00"
    label = local.strftime("%H:%M")
    if local.replace(fold=0).utcoffset() != local.replace(fold=1).utcoffset():
        label += local.strftime(" (UTC%z)")
    if reference_day is not None and local.date() != reference_day:
        label = local.strftime("%d.%m ") + label
    return label


def _duration(minutes: int) -> str:
    hours, remaining = divmod(minutes, 60)
    return (
        " ".join(
            part
            for part in (
                f"{hours} год" if hours else "",
                f"{remaining} хв" if remaining else "",
            )
            if part
        )
        or "менше хвилини"
    )


def _bounded(text: str) -> str:
    # No silent truncation of a complete day. The caller can fail closed.
    if len(text.encode("utf-16-le")) // 2 > 4096:
        raise ValueError("map_power_message_exceeds_telegram_limit")
    return text


def render_map_reminder(event: MapPowerReminder, now: datetime) -> str:
    """Revalidation of cache and user preferences belongs to final dispatch policy."""
    clock = aware(now)
    if not reminder_due(event, clock):
        raise ValueError("map_power_reminder_not_due")
    boundary = aware(event.transition_at)
    minutes = math.ceil(max(0, (boundary - clock).total_seconds()) / 60)
    when = "Зараз за графіком" if not minutes else f"За {minutes} хв за графіком"
    local_day = clock.astimezone(KYIV).date()
    start = _clock(event.outage_starts_at, reference_day=local_day)
    end = _clock(event.outage_ends_at, reference_day=local_day)
    period = _duration(event.outage_duration_minutes)
    exact_period = event.outage_start_confirmed and event.restoration_confirmed
    duration = period if exact_period else "щонайменше " + period
    title = (
        ("🔴 Скоро відключення світла" if minutes else "🔴 Відключення за графіком")
        if event.event_type == OUTAGE
        else ("🟢 Скоро відновлення за графіком" if minutes else "🟢 Відновлення за графіком")
    )
    lines = [
        title,
        f"{event.provider.label} · група {event.group_id}",
        when,
    ]
    if event.event_type == OUTAGE:
        lines.extend((f"⏰ Початок відключення: {start}", f"⏳ Тривалість за графіком: {duration}"))
        if event.restoration_confirmed:
            lines.append(f"🟢 Відновлення за графіком: {end}")
        else:
            lines.append("Час відновлення ще не підтверджений графіком.")
        lines.append("🔋📱 Зарядіть телефони й повербанки до відключення.")
    else:
        lines.extend((f"⏰ Відновлення за графіком: {end}", f"⏳ Період відключення: {duration}"))
        if event.next_outage_starts_at is not None:
            next_start = _clock(event.next_outage_starts_at, reference_day=local_day)
            lines.append(f"🔴 Наступне відключення за графіком: {next_start}")
            if event.next_outage_ends_at is not None:
                lines.append(
                    "🟢 Наступне відновлення за графіком: "
                    + _clock(event.next_outage_ends_at, reference_day=local_day)
                )
            lines.append(
                "🔋📱 Після появи світла зарядіть телефони й повербанки до наступного відключення."
            )
        else:
            lines.append("Час наступного відключення поки не підтверджено.")
            lines.append("🔋📱 Після появи світла зарядіть телефони й повербанки.")
    lines.append("⚠️ Графік може змінитися. Фактична наявність світла може відрізнятися.")
    return _bounded("\n\n".join(lines))


def render_map_schedule_change(
    view: PowerScheduleView, now: datetime, *, stale_after_seconds: int = 1800
) -> str:
    """Render the complete changed today/tomorrow graph, never only fragments."""
    today = aware(now).astimezone(KYIV).date()
    selected = view.schedule.local_date if view.schedule is not None else today + timedelta(days=1)
    if selected not in {today, today + timedelta(days=1)}:
        raise ValueError("map_schedule_change_requires_today_or_tomorrow")
    label = "сьогодні" if selected == today else "завтра"
    day = usable_map_day(view, now, stale_after_seconds=stale_after_seconds)
    lines = [
        f"📅 Графік на {label}, {selected:%d.%m.%Y}, змінено",
        f"{view.provider.label} · група {view.group_id}",
    ]
    if day is None:
        lines.append("⚠️ Актуальний графік поки недоступний.")
        return _bounded("\n\n".join(lines))
    if all(interval.state is PowerState.POWER_EXPECTED for interval in day.intervals):
        lines.append(f"{label.capitalize()} планових відключень немає.")
    else:
        rendered = False
        for state, title in (
            (PowerState.OUTAGE_CONFIRMED, "🔴 Планові відключення:"),
            (PowerState.POSSIBLE_OUTAGE, "🟡 Можливі відключення:"),
        ):
            intervals = [
                f"{_clock(interval.starts_at, reference_day=selected)}–"
                f"{_clock(interval.ends_at, reference_day=selected)}"
                for interval in day.intervals
                if interval.state is state
            ]
            if intervals:
                lines.append(title + "\n" + "\n".join(intervals))
                rendered = True
        if any(
            interval.state in {PowerState.UNKNOWN, PowerState.NO_SCHEDULE}
            for interval in day.intervals
        ):
            lines.append(
                "⚠️ Частину графіка ще не підтверджено."
                if rendered
                else "⚠️ Актуальний графік поки недоступний."
            )
    lines.append(
        "Час за Києвом. Графік може змінитися. "
        "Аварійні відключення можуть відбуватися поза графіком."
    )
    return _bounded("\n\n".join(lines))


def render_map_status(provider: PowerProvider, group: str, day: date, status: str) -> str:
    messages = {
        "ScheduleApplies": (
            "📅 Погодинний графік діє", "Відключення заплановано за опублікованим графіком."
        ),
        "NoShutdowns": (
            "✅ Планових відключень немає", "На цю дату для вашої групи планових відключень не передбачено."
        ),
        "WaitingForSchedule": (
            "⏳ Очікуємо графік", "Графік для вашої групи поки не опубліковано."
        ),
        "EmergencyShutdowns": (
            "🚨 Діють аварійні відключення", "Погодинний графік може не діяти. Час відновлення невідомий."
        ),
    }
    if type(day) is not date or status not in messages:
        raise ValueError("invalid_map_status_message")
    title, note = messages[status]
    return _bounded(
        f"{title}\n{provider.label} · група {group}\n📅 {day:%d.%m.%Y}\n\n{note}\n\n"
        "Фактична наявність світла може відрізнятися."
    )


def render_map_emergency(provider: PowerProvider, group: str, active: bool) -> str:
    if active:
        title = "🚨 Запроваджено аварійні відключення"
        note = "Погодинний графік може не діяти. Час відновлення невідомий."
    else:
        title = "✅ Аварійні обмеження завершилися"
        note = "Знову доступний погодинний графік. Фактичне відновлення світла не підтверджено."
    return _bounded(f"{title}\n{provider.label} · група {group}\n{note}")

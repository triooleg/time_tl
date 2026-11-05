"""Pure, bounded Ukrainian calendar presentation without delivery or join links."""

from __future__ import annotations

import math
import unicodedata
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from time_tl.domain.calendars import MyDay, TimelineEvent

from .models import Button, View

KYIV = ZoneInfo("Europe/Kyiv")
PAGE_SIZE = 8
GROUP_LIMIT = 12
WEEKDAYS = ("понеділок", "вівторок", "середа", "четвер", "п’ятниця", "субота", "неділя")
SAFETY_NOTE = (
    "⚠️ Розклад не є дозволом приєднатися до заняття. "
    "Стежте за офіційними повідомленнями про тривоги та вказівками щодо безпеки."
)


def _compact(value: str, limit: int) -> str:
    """Bound UTF-16 units, remove markup delimiters and invisible controls."""
    value = " ".join(
        "".join(
            character if not unicodedata.category(character).startswith("C") else " "
            for character in value.replace("<", "‹").replace(">", "›")
        ).split()
    )
    if len(value.encode("utf-16-le")) // 2 <= limit:
        return value
    result = []
    units = 0
    for character in value:
        size = len(character.encode("utf-16-le")) // 2
        if units + size > limit - 1:
            break
        result.append(character)
        units += size
    return "".join(result) + "…"


def _clock(value: datetime) -> str:
    return value.astimezone(KYIV).strftime("%H:%M")


def _name(event: TimelineEvent, *, title_limit: int, calendar_limit: int) -> str:
    return (
        f"{_compact(event.title, title_limit)} "
        f"· {_compact(event.calendar_name, calendar_limit)}"
    )


def _rows(page: int, pages: int, homework_enabled: bool) -> tuple[tuple[Button, ...], ...]:
    rows: list[tuple[Button, ...]] = []
    navigation = []
    if page > 0:
        navigation.append(Button("←", f"myday:page:{page - 1}"))
    if page + 1 < pages:
        navigation.append(Button("→", f"myday:page:{page + 1}"))
    if navigation:
        rows.append(tuple(navigation))
    rows.append((Button("🔄 Оновити", "menu:myday"),))
    rows.append((Button("📅 Розклади", "menu:schedules"),))
    if homework_enabled:
        rows.append((Button("📝 Домашні завдання", "menu:homework"),))
    rows.append((Button("🏠 Головне меню", "menu:home"),))
    return tuple(rows)


def render_my_day(
    timeline: MyDay,
    *,
    at: datetime,
    homework_count: int | None = None,
    homework_enabled: bool = False,
    page: int = 0,
) -> View:
    """Render the selected Kyiv day; refresh status from aware UTC instants."""
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("at must be timezone-aware")
    if at.astimezone(KYIV).date() != timeline.day:
        raise ValueError("at must belong to the selected Kyiv day")
    if not isinstance(page, int) or isinstance(page, bool):
        raise ValueError("page must be an integer")
    if homework_count is not None and (
        not isinstance(homework_count, int)
        or isinstance(homework_count, bool)
        or not 0 <= homework_count <= 100
    ):
        raise ValueError("homework_count must be a bounded preview count")

    instant = at.astimezone(UTC)
    events = sorted(
        timeline.events,
        key=lambda event: (event.start.astimezone(UTC), event.end.astimezone(UTC), event.event_id),
    )
    pages = max(1, math.ceil(len(events) / PAGE_SIZE))
    page = min(max(0, page), pages - 1)
    lines = [
        f"🕐 Мій день · {timeline.day:%d.%m.%Y} · {WEEKDAYS[timeline.day.weekday()]}",
        "Час: Київ",
    ]
    if timeline.conflicts:
        lines.append(
            f"⚠️ Перетини занять: {len(timeline.conflicts)}. "
            "Оберіть потрібне заняття самостійно."
        )

    current = [
        event for event in events
        if event.start.astimezone(UTC) <= instant < event.end.astimezone(UTC)
    ]
    lines.extend(("", f"Зараз · занять: {len(current)}"))
    for event in current[:GROUP_LIMIT]:
        remaining = math.ceil((event.end.astimezone(UTC) - instant).total_seconds() / 60)
        lines.append(
            f"• {_name(event, title_limit=32, calendar_limit=16)} · ще {remaining} хв"
        )
    if len(current) > GROUP_LIMIT:
        lines.append(f"Ще {len(current) - GROUP_LIMIT} зараз — дивіться всі сторінки розкладу.")
    if not current:
        lines.append("За розкладом зараз немає занять.")

    future = [event for event in events if event.start.astimezone(UTC) > instant]
    if future:
        first_start = future[0].start.astimezone(UTC)
        next_group = [event for event in future if event.start.astimezone(UTC) == first_start]
        local_start = first_start.astimezone(KYIV)
        lines.extend((
            "",
            f"Найближчі · {local_start:%d.%m.%Y · %H:%M} · занять: {len(next_group)}",
        ))
        for event in next_group[:GROUP_LIMIT]:
            lines.append(f"• {_name(event, title_limit=24, calendar_limit=12)}")
        if len(next_group) > GROUP_LIMIT:
            lines.append(f"Ще {len(next_group) - GROUP_LIMIT} одночасно — на сторінках розкладу.")
    else:
        lines.extend(("", "Наступних занять на цей день у розкладі немає."))

    lines.extend(("", f"Розклад · сторінка {page + 1}/{pages}"))
    if not events:
        lines.append("На вибраний день немає занять з увімкнених календарів.")
    for event in events[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]:
        status = (
            "минуло" if event.end.astimezone(UTC) <= instant
            else "зараз" if event.start.astimezone(UTC) <= instant
            else _clock(event.start)
        )
        lines.append(
            f"• {_clock(event.start)}–{_clock(event.end)} · {status}\n"
            f"  {_name(event, title_limit=48, calendar_limit=20)}"
        )
    if homework_enabled:
        lines.extend(("", "📝 Домашні завдання"))
        lines.append(
            "Список тимчасово недоступний. Спробуйте оновити пізніше."
            if homework_count is None
            else f"Невиконаних у поточному списку: {homework_count}"
        )
    lines.extend(("", SAFETY_NOTE))
    return View("\n".join(lines), _rows(page, pages, homework_enabled))


def projection_unavailable_view(*, homework_enabled: bool = False) -> View:
    return View(
        "🕐 Мій день\nРозклад тимчасово недоступний. Спробуйте оновити пізніше.\n\n"
        + SAFETY_NOTE,
        _rows(0, 1, homework_enabled),
    )

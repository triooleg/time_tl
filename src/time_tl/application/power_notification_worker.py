"""Independent power planning and schedule-qualified Ukrainian rendering."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Protocol
from zoneinfo import ZoneInfo

from time_tl.domain.power_outages.models import PowerProvider, aware, validate_group

KYIV = ZoneInfo("Europe/Kyiv")


class PowerPlanningRepository(Protocol):
    async def plan_notifications(self, now: datetime, *, limit: int = 100) -> int: ...


class PowerNotificationWorker:
    def __init__(self, repository: PowerPlanningRepository) -> None:
        self.repository = repository

    async def run_once(self, now: datetime) -> int:
        aware(now)
        return await self.repository.plan_notifications(now)


def _stamp(value: object) -> datetime:
    if type(value) is not str:
        raise ValueError("invalid_power_notification_time")
    return aware(datetime.fromisoformat(value))


def render_power_message(event_type: str, payload: dict[str, object], now: datetime) -> str:
    if payload.get("source_kind") != "official":
        raise ValueError("unapproved_power_notification_source")
    provider = PowerProvider(str(payload["provider_id"]))
    group = validate_group(str(payload["group_id"]))
    boundary = _stamp(payload["valid_until"])
    if aware(now) > boundary:
        raise ValueError("power_notification_expired")
    title = f"⚡ {provider.label}\n🔢 Група: {group}\n\n"
    warning = (
        "\n\nДжерело: офіційний графік оператора.\n"
        "⚠️ Час може змінитися. Фактичне перемикання може відрізнятися від графіка."
    )
    if event_type == "POWER_SCHEDULE_CHANGED":
        raw = payload.get("power_windows")
        if type(raw) is not list or len(raw) > 2688:
            raise ValueError("invalid_power_correction")
        windows: list[str] = []
        for item in raw:
            if type(item) is not list or len(item) != 2:
                raise ValueError("invalid_power_correction")
            start, end = _stamp(item[0]), _stamp(item[1])
            if end <= start:
                raise ValueError("invalid_power_correction")
            windows.append(f"🔴 {start.astimezone(KYIV):%H:%M}–{end.astimezone(KYIV):%H:%M}")
        summary = (
            "\n".join(windows[:32])
            if windows
            else ("На решту доби підтверджених планових відключень у цьому графіку немає.")
        )
        if len(windows) > 32:
            summary += (
                f"\n… ще {len(windows) - 32} інтервалів. Повний графік — у меню електроенергії."
            )
        return (
            "⚠️ ГРАФІК ЗМІНЕНО\n\n"
            + title
            + (
                "Попереднє повідомлення більше не відповідає актуальному графіку.\n"
                "Оновлені інтервали за графіком:\n" + summary + warning
            )
        )
    transition = _stamp(payload["transition_at"])
    start, end = _stamp(payload["outage_starts_at"]), _stamp(payload["outage_ends_at"])
    if end <= start or transition != boundary:
        raise ValueError("invalid_power_notification")
    minutes = math.ceil(max(0, (transition - aware(now)).total_seconds()) / 60)
    when = "Зараз за графіком" if minutes == 0 else f"Орієнтовно через {minutes} хв"
    if event_type == "POWER_OUTAGE_UPCOMING" and transition == start:
        return (
            "🔴 СКОРО ВІДКЛЮЧЕННЯ СВІТЛА\n\n"
            + title
            + (
                f"⏰ {when}\nПочаток за графіком: {start.astimezone(KYIV):%H:%M}\n"
                f"Очікуване відновлення: {end.astimezone(KYIV):%H:%M}" + warning
            )
        )
    if event_type == "POWER_RESTORATION_UPCOMING" and transition == end:
        return (
            "🟢 ЗА ГРАФІКОМ СКОРО ВКЛЮЧЕННЯ\n\n"
            + title
            + (f"⏰ {when}\nОчікуване включення: {end.astimezone(KYIV):%H:%M}" + warning)
        )
    raise ValueError("invalid_power_notification")

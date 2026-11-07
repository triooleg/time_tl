"""Presentation of observed alert durations; never an all-clear decision."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import cast
from zoneinfo import ZoneInfo


def duration_text(seconds: object) -> str | None:
    if type(seconds) is not int or not 0 <= seconds <= 366 * 24 * 3600:
        return None
    if seconds < 60:
        return "менше 1 хв"
    hours, minutes = divmod(seconds // 60, 60)
    return " ".join(
        value for value in (f"{hours} год" if hours else "", f"{minutes} хв" if minutes else "")
        if value
    )


def completed_alert_duration(payload: Mapping[str, object]) -> str:
    formatted = duration_text(payload.get("duration_seconds"))
    if formatted is None:
        return "⏱ Тривалість тривоги: немає повних даних"
    prefix = "≈" if payload.get("duration_is_approximate") is True else ""
    return f"⏱ Тривалість тривоги: {prefix}{formatted}"


def render_alert_report(payload: Mapping[str, object], region_name: str) -> str:
    """Render a bounded reporting period, including explicitly incomplete history."""
    from time_tl.domain.alert_statistics import validate_report_payload

    value = validate_report_payload(payload)
    zone = ZoneInfo("Europe/Kyiv")
    start = datetime.fromisoformat(cast(str, value["period_start"])).astimezone(zone)
    end = datetime.fromisoformat(cast(str, value["period_end"])).astimezone(zone)
    partial = value["is_partial"]
    label = "Зафіксована тривалість" if partial else "Загальна тривалість"
    duration = duration_text(value["duration_seconds"])
    duration = f"≈{duration}" if value["duration_seconds"] else "0 хв"
    lines = [
        "📊 ПІДСУМОК ТРИВОГ",
        f"📍 {region_name}",
        f"📅 {start:%d.%m %H:%M} — {end:%d.%m %H:%M}",
        "",
        f"🚨 Нових тривог: {value['alert_count']}",
        f"⏱ {label}: {duration}",
    ]
    if value["carried_over_count"]:
        lines.append(f"🔁 З попереднього періоду: {value['carried_over_count']}")
    if value.get("unknown_start_count"):
        lines.append(f"🔎 Тривог із невідомим початком: {value['unknown_start_count']}")
    if value["ongoing_count"]:
        lines.append(
            f"🔴 Тривог без підтвердженого завершення: {value['ongoing_count']}"
            if partial else "🔴 На час підсумку тривога ще триває"
        )
    if partial:
        lines.append("⚠️ Історія неповна — враховано лише зафіксовані дані.")
    return "\n".join(lines)

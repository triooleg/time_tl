"""Pure weather notification planning and strict deterministic forecast rendering."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from html import escape
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from time_tl.domain.weather import (
    ForecastSnapshot,
    WeatherChangeEvent,
    WeatherThresholds,
    _value,
)

KYIV = ZoneInfo("Europe/Kyiv")
DEFAULT_THRESHOLDS = WeatherThresholds()
EVENT_TYPES = {"RAIN", "SNOW", "WIND", "TEMPERATURE", "FROST"}
KINDS = {"UPCOMING", "CORRECTION", "CANCELLATION"}
FIELDS = {
    "version",
    "configuration_revision",
    "user_id",
    "region_id",
    "point_id",
    "preference_revision",
    "snapshot_hash",
    "source_updated_at",
    "expires_at",
    "kind",
    "locality",
    "event_keys",
    "summary",
    "events",
}
EVENT_FIELDS = {
    "event_key",
    "event_type",
    "kind",
    "start",
    "end",
    "previous_value",
    "current_value",
    "initial",
}
SUMMARY_FIELDS = {"forecast_date", "temperature_min_c", "temperature_max_c", "interval_count"}
DAILY_FORECAST_HOURS = (7, 12, 18)
DAILY_FORECAST_WINDOW = timedelta(minutes=10)
DAILY_FORECAST_TITLES = {
    7: "🌅 Прогноз на день",
    12: "☀️ Прогноз на обід",
    18: "🌆 Прогноз на вечір",
}


@dataclass(frozen=True)
class WeatherNotificationPreferences:
    weather_enabled: bool = False
    forecast_requested: bool = False
    daily_forecast_enabled: bool = False
    daily_forecast_time: time = time(7)
    weather_changes_enabled: bool = True
    rain_alert_enabled: bool = True
    snow_alert_enabled: bool = True
    wind_alert_enabled: bool = True
    temperature_alert_enabled: bool = True
    frost_alert_enabled: bool = True
    notification_lead_minutes: int = 120
    revision: int = 0


@dataclass(frozen=True)
class PlannedWeatherNotification:
    kind: str
    key: str
    payload: dict[str, object]
    available_at: datetime
    expires_at: datetime
    event_keys: tuple[str, ...]


def _instant(raw: object) -> datetime:
    if not isinstance(raw, str) or len(raw) > 40:
        raise ValueError("invalid_weather_instant")
    result = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("invalid_weather_instant")
    return result.astimezone(UTC)


def _uuid(raw: object) -> None:
    if not isinstance(raw, str) or str(UUID(raw)) != raw:
        raise ValueError("invalid_weather_identity")


def _digest(raw: object) -> None:
    if not isinstance(raw, str) or len(raw) != 64 or any(c not in "0123456789abcdef" for c in raw):
        raise ValueError("invalid_weather_digest")


def _number(raw: object, *, optional: bool = False) -> None:
    if optional and raw is None:
        return
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw):
        raise ValueError("invalid_weather_number")


def _validate_weather_notification_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping) or set(payload) != FIELDS:
        raise ValueError("invalid_weather_payload")
    value = dict(payload)
    if type(value["version"]) is not int or value["version"] != 1:
        raise ValueError("invalid_weather_version")
    if type(value["configuration_revision"]) is not int or value["configuration_revision"] < 0:
        raise ValueError("invalid_weather_configuration_revision")
    _uuid(value["user_id"])
    _uuid(value["point_id"])
    _digest(value["snapshot_hash"])
    for field, maximum in (("region_id", 64), ("locality", 160)):
        raw = value[field]
        if (
            not isinstance(raw, str)
            or not raw.strip()
            or len(raw) > maximum
            or any(ord(c) < 32 for c in raw)
        ):
            raise ValueError("invalid_weather_label")
    if type(value["preference_revision"]) is not int or value["preference_revision"] < 1:
        raise ValueError("invalid_weather_revision")
    updated, expiry = _instant(value["source_updated_at"]), _instant(value["expires_at"])
    if expiry <= updated:
        raise ValueError("invalid_weather_expiry")
    keys, events = value["event_keys"], value["events"]
    if not isinstance(keys, list) or len(keys) > 24 or len(set(keys)) != len(keys):
        raise ValueError("invalid_weather_keys")
    for key in keys:
        _digest(key)
    if not isinstance(events, list) or len(events) > 24:
        raise ValueError("invalid_weather_events")
    for event in events:
        if not isinstance(event, dict) or set(event) != EVENT_FIELDS:
            raise ValueError("invalid_weather_event")
        _digest(event["event_key"])
        if type(event["initial"]) is not bool or (
            event["kind"] == "CANCELLATION" and event["initial"]
        ):
            raise ValueError("invalid_weather_initial")
        if event["event_type"] not in EVENT_TYPES or event["kind"] not in KINDS:
            raise ValueError("invalid_weather_event")
        if _instant(event["end"]) <= _instant(event["start"]):
            raise ValueError("invalid_weather_event_interval")
        _number(event["previous_value"], optional=True)
        _number(event["current_value"], optional=True)
    if [e["event_key"] for e in events] != keys:
        raise ValueError("invalid_weather_key_binding")
    if value["kind"] == "WEATHER_EVENTS":
        if not events or value["summary"] is not None:
            raise ValueError("invalid_weather_events")
    elif value["kind"] == "WEATHER_DAILY":
        summary = value["summary"]
        if events or keys or not isinstance(summary, dict) or set(summary) != SUMMARY_FIELDS:
            raise ValueError("invalid_weather_summary")
        from datetime import date

        date.fromisoformat(summary["forecast_date"])
        _number(summary["temperature_min_c"])
        _number(summary["temperature_max_c"])
        if summary["temperature_min_c"] > summary["temperature_max_c"]:
            raise ValueError("invalid_weather_summary")
        if type(summary["interval_count"]) is not int or not 1 <= summary["interval_count"] <= 96:
            raise ValueError("invalid_weather_summary")
    else:
        raise ValueError("unknown_weather_notification")
    return value


def validate_weather_notification_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    try:
        return _validate_weather_notification_payload(payload)
    except (ValueError, TypeError, OverflowError, RecursionError):
        raise ValueError("invalid_weather_payload") from None


def event_supported(
    snapshot: ForecastSnapshot,
    event: WeatherChangeEvent,
    thresholds: WeatherThresholds,
    now: datetime,
) -> bool:
    if (
        not snapshot.usable(now)
        or snapshot.provider != event.provider
        or snapshot.point_id != event.point_id
        or event.event_type not in EVENT_TYPES
        or event.kind not in KINDS
        or event.expected_end <= now
    ):
        return False
    hours = {h.forecast_at: h for h in snapshot.hours}
    hour = hours.get(event.expected_start)
    if hour is None:
        return False
    lag = timedelta(hours=24 if event.event_type == "TEMPERATURE" else 1)
    active, value = _value(
        hour, event.event_type, thresholds, hours.get(event.expected_start - lag)
    )
    if value is None or active is None:
        return False
    if event.kind == "CANCELLATION":
        return active is False and event.current_value == value
    # Dispatch tolerates an unrelated future-hour change, not this event's changed facts.
    return (
        active is True
        and event.current_value is not None
        and abs(event.current_value - value) < thresholds.revision_delta
    )


def render_weather_notification(payload: Mapping[str, Any]) -> str:
    value = validate_weather_notification_payload(payload)
    lines = ["🌤 Прогноз погоди", f"📍 {escape(value['locality'])}"]
    if value["kind"] == "WEATHER_DAILY":
        summary = value["summary"]
        slot = forecast_slot(summary["forecast_date"], value["expires_at"])
        if slot is not None:
            lines[0] = DAILY_FORECAST_TITLES[slot]
        lines += [
            date.fromisoformat(summary["forecast_date"]).strftime("%d.%m.%Y"),
            f"🌡 {summary['temperature_min_c']:+g}…{summary['temperature_max_c']:+g}°C",
        ]
    else:
        labels = {
            "RAIN": "🌧 Дощ",
            "SNOW": "❄️ Сніг",
            "WIND": "💨 Сильні пориви вітру",
            "TEMPERATURE": "🌡 Зміна температури",
            "FROST": "🥶 Заморозки",
        }
        units = {"RAIN": "мм/год", "SNOW": "см", "WIND": "м/с", "TEMPERATURE": "°C", "FROST": "°C"}
        for event in value["events"]:
            prefix = {
                "UPCOMING": "Очікується",
                "CORRECTION": "Оновлення прогнозу",
                "CANCELLATION": "Попереднє очікування не підтверджується",
            }[event["kind"]]
            at = _instant(event["start"]).astimezone(KYIV).strftime("%d.%m %H:%M")
            amount = (
                ""
                if event["current_value"] is None
                else (f" · близько {event['current_value']:g} {units[event['event_type']]}")
            )
            if event["initial"]:
                prefix = "\u041e\u0447\u0456\u043a\u0443\u0454\u0442\u044c\u0441\u044f"
            lines.append(f"{prefix}: {labels[event['event_type']]} · {at}{amount}")
    lines += [
        "Прогнозна рекомендація, не фактичне спостереження та не офіційне попередження.",
        "Джерело: MET Norway (CC BY 4.0), нормалізовано Time TL.",
        "https://api.met.no/",
        "https://creativecommons.org/licenses/by/4.0/",
    ]
    text = "\n".join(lines)
    if len(text) > 3500:
        raise ValueError("weather_notification_too_large")
    return text


def due_forecast_slot(now: datetime) -> datetime | None:
    local = now.astimezone(KYIV)
    if local.hour not in DAILY_FORECAST_HOURS:
        return None
    due = local.replace(minute=0, second=0, microsecond=0)
    return due if local < due + DAILY_FORECAST_WINDOW else None


def forecast_slot(date_iso: str, expiry: object) -> int | None:
    # Same-day bulletins expire within their slot. Legacy messages cover the
    # next day, so the existing outbox shape remains readable during rollback.
    due = due_forecast_slot(_instant(expiry) - timedelta(microseconds=1))
    return due.hour if due is not None and due.date().isoformat() == date_iso else None


def forecast_delivery_key(date_iso: str, expiry: object) -> str:
    slot = forecast_slot(date_iso, expiry)
    return f"{date_iso}:{slot:02d}" if slot is not None else date_iso


def daily_summary(
    snapshot: ForecastSnapshot, date_iso: str, *, slot: int | None = None
) -> dict[str, object] | None:
    target = date.fromisoformat(date_iso)
    if target.isoformat() != date_iso:
        raise ValueError("invalid_weather_forecast_date")
    end_hour = 18 if slot == 12 else 24
    temperatures = []
    for hour in snapshot.hours:
        local = hour.forecast_at.astimezone(KYIV)
        if local.date() == target and (slot is None or slot <= local.hour < end_hour):
            if hour.temperature_c is not None:
                temperatures.append(hour.temperature_c)
    if not temperatures:
        return None
    return {
        "forecast_date": date_iso,
        "temperature_min_c": float(min(temperatures)),
        "temperature_max_c": float(max(temperatures)),
        "interval_count": len(temperatures),
    }


class WeatherNotificationPlanner:
    def __init__(self, thresholds: WeatherThresholds | None = None) -> None:
        self.thresholds = thresholds or DEFAULT_THRESHOLDS

    def plan(
        self,
        *,
        user_id: str,
        region_id: str,
        locality: str,
        snapshot: ForecastSnapshot,
        events: tuple[WeatherChangeEvent, ...],
        preferences: WeatherNotificationPreferences,
        delivered_event_keys: frozenset[str],
        known_notification_keys: frozenset[str],
        now: datetime,
        configuration_revision: int = 0,
        delivered_forecast_dates: frozenset[str] = frozenset(),
    ) -> tuple[PlannedWeatherNotification, ...]:
        prefs = preferences
        if (
            not prefs.weather_enabled
            or prefs.revision < 1
            or not snapshot.usable(now)
            or snapshot.provider != "MET Norway"
            or not 0 <= prefs.notification_lead_minutes <= 1440
        ):
            return ()
        selected: list[WeatherChangeEvent] = []
        enabled = {
            "RAIN": prefs.rain_alert_enabled,
            "SNOW": prefs.snow_alert_enabled,
            "WIND": prefs.wind_alert_enabled,
            "TEMPERATURE": prefs.temperature_alert_enabled,
            "FROST": prefs.frost_alert_enabled,
        }
        for event in sorted(events, key=lambda e: (e.expected_start, e.event_type, e.event_key)):
            if event.event_key in delivered_event_keys:
                continue
            if not enabled.get(event.event_type, False) or not event_supported(
                snapshot, event, self.thresholds, now
            ):
                continue
            already_delivered = any(key[:32] == event.identity_key for key in delivered_event_keys)
            initial = event.kind == "UPCOMING" or (
                event.kind == "CORRECTION" and not already_delivered
            )
            if initial:
                if already_delivered:
                    continue
                due = event.expected_start - timedelta(minutes=prefs.notification_lead_minutes)
                if not due <= now < due + timedelta(minutes=10):
                    continue
            elif not prefs.weather_changes_enabled or not already_delivered:
                continue
            selected.append(event)
        groups: list[list[WeatherChangeEvent]] = []
        for event in selected:
            if groups and event.expected_start < max(e.expected_end for e in groups[-1]):
                groups[-1].append(event)
            else:
                groups.append([event])
        result: list[PlannedWeatherNotification] = []

        def emit(
            kind: str,
            keys: tuple[str, ...],
            summary: dict[str, object] | None,
            selected_events: list[WeatherChangeEvent],
            expiry: datetime,
            identity: object,
        ) -> None:
            key = (
                "weather:"
                + hashlib.sha256(
                    repr(
                        (
                            user_id,
                            region_id,
                            snapshot.point_id,
                            kind,
                            identity,
                            prefs.revision,
                            configuration_revision,
                        )
                    ).encode()
                ).hexdigest()
            )
            if key in known_notification_keys or expiry <= now:
                return
            payload: dict[str, object] = {
                "version": 1,
                "configuration_revision": configuration_revision,
                "user_id": user_id,
                "region_id": region_id,
                "point_id": snapshot.point_id,
                "preference_revision": prefs.revision,
                "snapshot_hash": snapshot.forecast_hash,
                "source_updated_at": snapshot.provider_updated_at.isoformat(),
                "expires_at": expiry.isoformat(),
                "kind": kind,
                "locality": locality,
                "event_keys": list(keys),
                "summary": summary,
                "events": [
                    {
                        "event_key": e.event_key,
                        "event_type": e.event_type,
                        "kind": e.kind,
                        "initial": (
                            e.kind == "UPCOMING"
                            or (
                                e.kind == "CORRECTION"
                                and not any(
                                    key[:32] == e.identity_key for key in delivered_event_keys
                                )
                            )
                        ),
                        "start": e.expected_start.isoformat(),
                        "end": e.expected_end.isoformat(),
                        "previous_value": e.previous_value,
                        "current_value": e.current_value,
                    }
                    for e in selected_events
                ],
            }
            render_weather_notification(payload)  # Strict bounded contract before outbox admission.
            result.append(PlannedWeatherNotification(kind, key, payload, now, expiry, keys))

        for group in groups:
            keys = tuple(dict.fromkeys(e.event_key for e in group))
            # At most one fact for each key; input duplicates cannot grow a message.
            unique = list({e.event_key: e for e in group}.values())
            expiry = min(
                snapshot.expires_at,
                min(e.expected_end for e in unique),
                now + timedelta(minutes=10),
            )
            emit("WEATHER_EVENTS", keys, None, unique, expiry, keys)
        forecast_due = due_forecast_slot(now)
        if prefs.daily_forecast_enabled and forecast_due is not None:
            today = forecast_due.date().isoformat()
            expiry = min(snapshot.expires_at, forecast_due + DAILY_FORECAST_WINDOW)
            key = forecast_delivery_key(today, expiry.isoformat())
            if key not in delivered_forecast_dates:
                summary = daily_summary(snapshot, today, slot=forecast_due.hour)
                if summary is not None:
                    emit("WEATHER_DAILY", (), summary, [], expiry, key)
        return tuple(result)

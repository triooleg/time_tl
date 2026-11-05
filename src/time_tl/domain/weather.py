"""Immutable forecast facts; recommendations are never official warnings."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo


def aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone_required")


@dataclass(frozen=True)
class WeatherPoint:
    point_id: str
    latitude: float
    longitude: float

    def __post_init__(self) -> None:
        if not self.point_id or not (-90 <= self.latitude <= 90 and -180 <= self.longitude <= 180):
            raise ValueError("invalid_point")


@dataclass(frozen=True)
class ForecastHour:
    forecast_at: datetime
    temperature_c: float | None = None
    precipitation_mm: float | None = None
    precipitation_probability: float | None = None
    snowfall_cm: float | None = None
    wind_speed_ms: float | None = None
    wind_gust_ms: float | None = None
    weather_code: str | None = None
    cloud_cover_percent: float | None = None
    feels_like_c: float | None = None

    def __post_init__(self) -> None:
        aware(self.forecast_at)
        object.__setattr__(self, "forecast_at", self.forecast_at.astimezone(UTC))
        for key, value in asdict(self).items():
            if key in {"forecast_at", "weather_code"} or value is None:
                continue
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
            ):
                raise ValueError("invalid_parameter")
            if key not in {"temperature_c", "feels_like_c"} and value < 0:
                raise ValueError("negative_parameter")
        if self.cloud_cover_percent is not None and self.cloud_cover_percent > 100:
            raise ValueError("invalid_cloud_cover")
        if self.weather_code is not None and not isinstance(self.weather_code, str):
            raise ValueError("invalid_symbol")
        if self.precipitation_probability is not None and self.precipitation_probability > 100:
            raise ValueError("invalid_probability")


class ForecastStatus(StrEnum):
    FRESH = "FRESH"
    STALE = "STALE"
    PARTIAL = "PARTIAL"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ForecastSnapshot:
    provider: str
    point_id: str
    fetched_at: datetime
    provider_updated_at: datetime
    expires_at: datetime
    hours: tuple[ForecastHour, ...]
    status: ForecastStatus
    source_url: str
    last_modified: str | None = None
    cache_expires_at: datetime | None = None

    def __post_init__(self) -> None:
        for value in (self.fetched_at, self.provider_updated_at, self.expires_at):
            aware(value)
        for name in ("fetched_at", "provider_updated_at", "expires_at"):
            object.__setattr__(self, name, getattr(self, name).astimezone(UTC))
        if self.cache_expires_at is not None:
            aware(self.cache_expires_at)
        times = [h.forecast_at for h in self.hours]
        if times != sorted(set(times)):
            raise ValueError("unordered_hours")
        if not self.hours and self.status == ForecastStatus.FRESH:
            raise ValueError("empty_forecast")

    @property
    def forecast_hash(self) -> str:
        body = [{**asdict(h), "forecast_at": h.forecast_at.isoformat()} for h in self.hours]
        return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()

    def usable(self, now: datetime) -> bool:
        aware(now)
        return (
            self.status == ForecastStatus.FRESH
            and self.expires_at > now
            and self.provider_updated_at <= now + timedelta(minutes=5)
            and now - self.provider_updated_at <= timedelta(hours=24)
        )


@dataclass(frozen=True)
class ForecastRun:
    run_id: str
    started_at: datetime
    completed_at: datetime
    requested_locations: int
    successful_locations: int
    failed_locations: int
    status: str = "PASS"


@dataclass(frozen=True)
class WeatherThresholds:
    rain_mm: float = 1.0
    rain_probability: float = 70.0
    gust_ms: float = 15.0
    temperature_change_c: float = 8.0
    snow_cm: float = 0.1
    revision_delta: float = 1.0

    def __post_init__(self) -> None:
        if any(
            isinstance(v, bool) or not math.isfinite(v) or v <= 0 for v in asdict(self).values()
        ):
            raise ValueError("invalid_threshold")
        if self.rain_probability > 100:
            raise ValueError("invalid_threshold")


@dataclass(frozen=True)
class WeatherChangeEvent:
    point_id: str
    event_type: str
    kind: str  # UPCOMING / CORRECTION / CANCELLATION
    expected_start: datetime
    expected_end: datetime
    previous_value: float | None
    current_value: float | None
    detected_at: datetime
    provider: str

    source_revision: str = ""

    @property
    def identity_key(self) -> str:
        payload = (
            self.point_id,
            self.provider,
            self.event_type,
            self.expected_start.isoformat(),
            self.expected_end.isoformat(),
        )
        return hashlib.sha256(repr(payload).encode()).hexdigest()[:32]

    @property
    def event_key(self) -> str:
        # Stable identity prefix lets a restarted detector recognise an event even
        # after corrections. A new source revision can correct a repeated value.
        revision = (
            "0" * 32
            if self.kind == "UPCOMING"
            else hashlib.sha256(
                repr(
                    (self.kind, self.source_revision, self.previous_value, self.current_value)
                ).encode()
            ).hexdigest()[:32]
        )
        return self.identity_key + revision


def _value(
    hour: ForecastHour, event: str, thresholds: WeatherThresholds, before: ForecastHour | None
) -> tuple[bool | None, float | None]:
    if event == "RAIN":
        value = hour.precipitation_mm
        probability = hour.precipitation_probability
        if value is None or probability is None:
            return None, value
        return value >= thresholds.rain_mm and probability >= thresholds.rain_probability, value
    if event == "WIND":
        v = hour.wind_gust_ms
        return (None if v is None else v >= thresholds.gust_ms), v
    if event == "SNOW":
        v = hour.snowfall_cm
        return (None if v is None else v >= thresholds.snow_cm), v
    if before is None or before.temperature_c is None or hour.temperature_c is None:
        return None, None
    if event == "FROST":
        return before.temperature_c >= 0 > hour.temperature_c, hour.temperature_c
    delta = hour.temperature_c - before.temperature_c
    return abs(delta) >= thresholds.temperature_change_c, delta


class WeatherChangeDetector:
    def __init__(self, thresholds: WeatherThresholds | None = None) -> None:
        self.thresholds = thresholds or WeatherThresholds()

    def detect(
        self,
        previous: ForecastSnapshot | None,
        current: ForecastSnapshot,
        now: datetime,
        known_event_keys: frozenset[str] = frozenset(),
    ) -> tuple[WeatherChangeEvent, ...]:
        if not current.usable(now):
            return ()
        if previous is not None and (
            previous.point_id != current.point_id or previous.provider != current.provider
        ):
            return ()  # No automatic cross-source comparison/fallback.
        old = (
            {}
            if previous is None or not previous.usable(now)
            else {h.forecast_at: h for h in previous.hours}
        )
        new = {h.forecast_at: h for h in current.hours}
        events: list[WeatherChangeEvent] = []
        for at, hour in new.items():
            if not now <= at <= now + timedelta(hours=24):
                continue
            for event in ("RAIN", "WIND", "SNOW", "FROST", "TEMPERATURE"):
                lag = timedelta(hours=24 if event == "TEMPERATURE" else 1)
                active, value = _value(hour, event, self.thresholds, new.get(at - lag))
                prior = old.get(at)
                was, before = (
                    (None, None)
                    if prior is None
                    else _value(prior, event, self.thresholds, old.get(at - lag))
                )
                kind: str | None = None
                if active is True:
                    if was is True and value is not None and before is not None:
                        kind = (
                            "CORRECTION"
                            if abs(value - before) >= self.thresholds.revision_delta
                            else "UPCOMING"
                        )
                    else:
                        kind = "UPCOMING"
                elif active is False and was is True:
                    kind = "CANCELLATION"
                if kind is None:
                    continue  # Missing parameters never cancel an event.
                item = WeatherChangeEvent(
                    current.point_id,
                    event,
                    kind,
                    at,
                    at + timedelta(hours=1),
                    before if kind != "UPCOMING" else None,
                    value,
                    now,
                    current.provider,
                    current.provider_updated_at.isoformat(),
                )
                seen_identity = any(key[:32] == item.identity_key for key in known_event_keys)
                if kind == "UPCOMING" and seen_identity:
                    if was is False:
                        # Explicit reappearance after a prior cancellation is a correction.
                        item = WeatherChangeEvent(
                            current.point_id,
                            event,
                            "CORRECTION",
                            at,
                            at + timedelta(hours=1),
                            before,
                            value,
                            now,
                            current.provider,
                            current.provider_updated_at.isoformat(),
                        )
                    else:
                        continue
                if item.event_key not in known_event_keys:
                    events.append(item)
        return tuple(events)


def next_weather_collection(now: datetime, cycles_per_day: int) -> datetime:
    """Next future Kyiv slot; never replay missed slots (including a DST restart)."""
    aware(now)
    if cycles_per_day not in (4, 6):
        raise ValueError("unsupported_frequency")
    zone = ZoneInfo("Europe/Kyiv")
    local = now.astimezone(zone)
    hours = (2, 8, 14, 20) if cycles_per_day == 4 else (2, 6, 10, 14, 18, 22)
    choices: set[datetime] = set()
    for day in (local.date(), local.date() + timedelta(days=1)):
        for hour in hours:
            candidate = datetime(day.year, day.month, day.day, hour, tzinfo=zone)
            utc = candidate.astimezone(UTC)
            if utc > now and utc.astimezone(zone).replace(tzinfo=None) == candidate.replace(
                tzinfo=None
            ):
                choices.add(utc)
    return min(choices)


def render_weather_event(event: WeatherChangeEvent, locality: str) -> str:
    names = {
        "RAIN": "🌧 Очікується дощ",
        "SNOW": "❄️ Очікується сніг",
        "WIND": "💨 Очікуються сильні пориви вітру",
        "FROST": "🥶 Можливі заморозки",
        "TEMPERATURE": "🌡 Очікується різка зміна температури",
    }
    heading = names[event.event_type]
    if event.kind == "CANCELLATION":
        heading = "Оновлення прогнозу: попереднє очікування не підтверджується"
    elif event.kind == "CORRECTION":
        heading = "Оновлення прогнозу: " + heading
    at = event.expected_start.astimezone(ZoneInfo("Europe/Kyiv")).strftime("%d.%m %H:%M")
    return (
        f"{heading}\n📍 {locality}\n⏰ Орієнтовно {at}\n"
        f"Прогнозна рекомендація, не офіційне попередження.\n"
        f"Джерело: {event.provider}; MET Norway — CC BY 4.0, нормалізовано Time TL.\n"
        "https://www.met.no/en/free-meteorological-data/Licensing-and-crediting"
    )

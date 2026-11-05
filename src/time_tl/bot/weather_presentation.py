"""Presentation of cached weather hours in Telegram."""

import re
from collections import Counter
from datetime import UTC, date, datetime, time, timedelta
from html import escape
from math import floor
from zoneinfo import ZoneInfo

from time_tl.domain.weather import ForecastHour, ForecastSnapshot, WeatherThresholds

KYIV = ZoneInfo("Europe/Kyiv")
WEEKDAYS = ("Понеділок", "Вівторок", "Середа", "Четвер", "П’ятниця", "Субота", "Неділя")
MONTHS = (
    "січня",
    "лютого",
    "березня",
    "квітня",
    "травня",
    "червня",
    "липня",
    "серпня",
    "вересня",
    "жовтня",
    "листопада",
    "грудня",
)
TEMPERATURE_PENDING = "🌡 Прогноз температури уточнюється"
DEFAULT_GUST_DISPLAY_MS = 12.0
THRESHOLDS = WeatherThresholds()
ONE_HOUR = timedelta(hours=1)
PAIR_WIDTH = 32
PRECIPITATION_CODE = re.compile(r"(light|heavy)?(rain|snow|sleet)(showers)?(andthunder)?")
SKY_ICONS = {"clearsky": "☀️", "fair": "🌤", "partlycloudy": "⛅", "cloudy": "☁️", "fog": "🌫"}
DAY_PERIODS = (
    (0, "🌙", "Ніч", "уночі"),
    (6, "🌅", "Ранок", "вранці"),
    (12, "☀️", "День", "вдень"),
    (18, "🌆", "Вечір", "увечері"),
)
PRECIPITATION_LABELS = {
    "thunder": "⛈ Гроза", "rain": "🌧 Дощ", "light_rain": "🌦 Невеликий дощ",
    "snow": "❄️ Сніг", "heavy_snow": "🌨 Снігопад",
    "sleet": "🌨 Мокрий сніг", "other": "💧 Опади",
}


def short_weather_locality(name: str) -> str:
    compact = " ".join(name.split())
    locality, separator, community = compact.rpartition(" та ")
    if separator and locality and community.endswith(" територіальна громада"):
        compact = locality
    return compact.removeprefix("м. ").replace(" територіальна громада", " громада")


def _weather_code(hour: ForecastHour) -> str:
    code = (hour.weather_code or "").lower()
    code = code.removesuffix("_day").removesuffix("_night").removesuffix("_polartwilight")
    # MET retains these two misspellings for backwards compatibility.
    if code in {"lightssleetshowersandthunder", "lightssnowshowersandthunder"}:
        code = "light" + code[6:]
    return code


def _precipitation(hour: ForecastHour) -> str | None:
    match = PRECIPITATION_CODE.fullmatch(_weather_code(hour))
    if match:
        intensity, kind, showers, thunder = match.groups()
        if thunder:
            return "thunder"
        if kind == "rain" and intensity == "light":
            return "light_rain"
        if kind == "snow" and (showers or intensity == "heavy"):
            return "heavy_snow"
        return kind
    if (hour.snowfall_cm or 0) > 0:
        return "snow"
    return "other" if (hour.precipitation_mm or 0) > 0 else None


def _condition(hour: ForecastHour, gust_threshold: float) -> str:
    if (kind := _precipitation(hour)) is not None and kind != "other":
        return PRECIPITATION_LABELS[kind].split()[0]
    if hour.wind_gust_ms is not None and hour.wind_gust_ms >= gust_threshold:
        return "💨"
    return SKY_ICONS.get(_weather_code(hour), "🌡")


def _daily_range(hours: list[ForecastHour], day: date) -> tuple[float, float] | None:
    start = datetime.combine(day, time.min, KYIV).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), time.min, KYIV).astimezone(UTC)
    expected = {start + n * ONE_HOUR for n in range((end - start) // ONE_HOUR)}
    temperatures = {hour.forecast_at: hour.temperature_c for hour in hours}
    if not expected.issubset(temperatures):
        return None
    values = [value for at in expected if (value := temperatures[at]) is not None]
    if len(values) != len(expected):
        return None
    return min(values), max(values)


def _temperature(value: float) -> str:
    magnitude = floor(abs(value) + 0.5)
    if magnitude == 0:
        return "0"
    return f"{'−' if value < 0 else '+'}{magnitude}"


def _temperature_range(low: float, high: float) -> str:
    first, last = _temperature(low), _temperature(high)
    return f"{first}°C" if first == last else f"{first}…{last}°C"


def _period_lines(hours: list[ForecastHour], *, compact: bool) -> list[str]:
    periods = []
    for start, icon, name, _ in DAY_PERIODS:
        values = [
            hour for hour in hours
            if hour.temperature_c is not None
            and start <= hour.forecast_at.astimezone(KYIV).hour < start + 6
        ]
        if not values:
            continue
        # Use the forecast nearest the middle of the period, never a synthetic reading.
        sample = min(
            values, key=lambda hour: abs(hour.forecast_at.astimezone(KYIV).hour - start - 3)
        )
        value = sample.temperature_c
        assert value is not None
        label = icon if compact else f"{icon} {name}"
        periods.append(f"{label}\u00a0{_temperature(value)}°")
    if compact and periods and len(" · ".join(periods)) <= PAIR_WIDTH:
        return [" · ".join(periods)]
    lines = []
    for index in range(0, len(periods), 2):
        pair = periods[index:index + 2]
        line = " · ".join(pair)
        lines.extend([line] if len(line) <= PAIR_WIDTH else pair)
    return lines


def _precipitation_lines(hours: list[ForecastHour], *, detailed: bool) -> list[str]:
    # Consolidate light/regular rain and snow/snow showers into a single event each.
    events: dict[str, tuple[str, ForecastHour]] = {}
    for hour in hours:
        if (kind := _precipitation(hour)) is None:
            continue
        group = {"light_rain": "rain", "heavy_snow": "snow"}.get(kind, kind)
        events.setdefault(group, (kind, hour))
    if len(events) > 1:
        events.pop("other", None)
    lines = []
    ordered = sorted(events.values(), key=lambda event: event[0] != "thunder")
    for kind, hour in ordered:
        local = hour.forecast_at.astimezone(KYIV)
        label = PRECIPITATION_LABELS[kind]
        if detailed and kind == "light_rain":
            label += f" {DAY_PERIODS[local.hour // 6][3]}"
        else:
            label += f" від {local:%H:%M}"
        if hour.precipitation_probability is not None:
            label += f" · {hour.precipitation_probability:g}%"
        lines.append(label)
    return lines[:2] if detailed else lines[:1]


def _gust(hours: list[ForecastHour], threshold: float) -> float | None:
    return max(
        (
            hour.wind_gust_ms for hour in hours
            if hour.wind_gust_ms is not None and hour.wind_gust_ms >= threshold
        ),
        default=None,
    )


def _tips(
    hours: list[ForecastHour], gust_threshold: float, *, cooling: bool
) -> list[str]:
    kinds = {_precipitation(hour) for hour in hours}
    windy = _gust(hours, gust_threshold) is not None
    tips = []
    if "thunder" in kinds:
        tips.append("⛈ Можлива гроза — будьте обережні надворі.")
    elif kinds & {"rain", "light_rain"}:
        tips.append(
            "🧥 Візьміть дощовик і зважайте на сильний вітер."
            if windy else "☂️ Візьміть парасольку."
        )
    elif windy:
        tips.append("💨 Будьте обережні через сильний вітер.")
    if kinds & {"snow", "heavy_snow", "sleet"}:
        tips.append("❄️ Оберіть тепле взуття.")
    if cooling:
        tips.append("🥶 Завтра суттєво похолодає.")
    elif not kinds & {"snow", "heavy_snow", "sleet"} and not tips and any(
        hour.temperature_c is not None and hour.temperature_c < 8 for hour in hours
    ):
        tips.append("🧥 Одягайтеся тепліше.")
    return tips[:2]


def render_forecast(
    snapshot: ForecastSnapshot,
    locality: str,
    now: datetime,
    mode: str,
    *,
    gust_threshold: float = DEFAULT_GUST_DISPLAY_MS,
) -> str:
    today = now.astimezone(KYIV).date()
    tomorrow = today + timedelta(days=1)
    days = {
        "tomorrow": (tomorrow,),
        "three": (today, tomorrow, tomorrow + timedelta(days=1)),
    }.get(mode, (today,))
    by_day: dict[date, list[ForecastHour]] = {}
    for hour in snapshot.hours:
        by_day.setdefault(hour.forecast_at.astimezone(KYIV).date(), []).append(hour)
    name = short_weather_locality(locality)
    if len(name) > 120:
        name = name[:119].rstrip() + "…"
    blocks = [f"🌤 <b>ПРОГНОЗ ПОГОДИ</b>\n📍 {escape(name)}"]
    upcoming: list[ForecastHour] = []

    for day in days:
        hours = by_day.get(day, [])
        visible = [hour for hour in hours if hour.forecast_at + ONE_HOUR > now]
        if mode == "now":
            visible = [hour for hour in visible if hour.forecast_at <= now]
        icons = Counter(_condition(hour, gust_threshold) for hour in visible)
        icons.pop("🌡", None)
        icon = icons.most_common(1)[0][0] if icons else "🌡"
        temperature = TEMPERATURE_PENDING
        if day == today:
            label = "Прогноз на зараз" if mode == "now" else "Сьогодні"
            value = next(
                (hour.temperature_c for hour in visible if hour.temperature_c is not None), None
            )
            if value is not None:
                prefix = "" if mode == "now" else "Найближчі години: "
                temperature = f"🌡 {prefix}<b>{_temperature(value)}°C</b>"
        else:
            label = "Завтра" if day == tomorrow else WEEKDAYS[day.weekday()]
            if (bounds := _daily_range(hours, day)) is not None:
                temperature = f"🌡 <b>{_temperature_range(*bounds)}</b>"
        lines = [f"{icon} <b>{label}, {day.day} {MONTHS[day.month - 1]}</b>", temperature]
        if mode != "now" and (day != today or mode == "today"):
            lines.extend(_period_lines(visible, compact=day > tomorrow))
        lines.extend(_precipitation_lines(visible, detailed=day == tomorrow))
        if (gust := _gust(visible, gust_threshold)) is not None:
            lines.append(f"💨 Пориви до {gust:g} м/с")
        blocks.append("\n".join(lines))
        upcoming.extend(visible)

    today_range = _daily_range(by_day.get(today, []), today) if tomorrow in days else None
    tomorrow_range = _daily_range(by_day.get(tomorrow, []), tomorrow) if tomorrow in days else None
    cooling = (
        today_range is not None and tomorrow_range is not None
        and today_range[1] - tomorrow_range[1] >= THRESHOLDS.temperature_change_c
    )
    if tips := _tips(upcoming, gust_threshold, cooling=cooling):
        blocks.append("\n".join(tips))
    return "\n\n".join(blocks)

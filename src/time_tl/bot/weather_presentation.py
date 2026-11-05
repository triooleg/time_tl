"""Presentation of cached weather hours in Telegram."""

import re
from collections import Counter
from datetime import UTC, date, datetime, time, timedelta
from html import escape
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
THRESHOLDS = WeatherThresholds()
ONE_HOUR = timedelta(hours=1)
PRECIPITATION_CODE = re.compile(r"(light|heavy)?(rain|snow|sleet)(showers)?(andthunder)?")
PRECIPITATION_LABELS = {"rain": "🌧 Дощ", "snow": "❄️ Сніг", "sleet": "🌨 Мокрий сніг"}
SKY_ICONS = {"clearsky": "☀️", "fair": "🌤", "partlycloudy": "⛅", "cloudy": "☁️", "fog": "🌫"}
DAY_PERIODS = (
    (0, 6, "🌙 Ніч"),
    (6, 12, "🌅 Ранок"),
    (12, 18, "☀️ Обід"),
    (18, 24, "🌆 Вечір"),
)


def short_weather_locality(name: str) -> str:
    compact = " ".join(name.split())
    locality, separator, community = compact.rpartition(" та ")
    if separator and locality and community.endswith(" територіальна громада"):
        compact = locality
    return compact.removeprefix("м. ").replace(" територіальна громада", " громада")


def _strong_wind(hour: ForecastHour) -> bool:
    return any(
        value is not None and value >= THRESHOLDS.gust_ms
        for value in (hour.wind_speed_ms, hour.wind_gust_ms)
    )


def _weather_code(hour: ForecastHour) -> str:
    code = (hour.weather_code or "").lower()
    code = code.removesuffix("_day").removesuffix("_night").removesuffix("_polartwilight")
    # MET retains these two misspellings for backwards compatibility.
    if code in {"lightssleetshowersandthunder", "lightssnowshowersandthunder"}:
        code = "light" + code[6:]
    return code


def _condition(hour: ForecastHour) -> str:
    code = _weather_code(hour)
    precipitation = PRECIPITATION_CODE.fullmatch(code)
    if precipitation is not None:
        intensity, kind, showers, thunder = precipitation.groups()
        if thunder:
            return "⛈"
        if kind == "rain":
            return "🌦" if intensity == "light" else "🌧"
        return "🌨" if showers or intensity == "heavy" or kind == "sleet" else "❄️"
    icon = SKY_ICONS.get(code)
    if icon is None:
        return "🌡"
    return "💨" if _strong_wind(hour) else icon


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


def _temperature_range(low: float, high: float) -> str:
    value = f"{low:+g}" if low == high else f"{low:+g}…{high:+g}"
    return f"{value}°C"


def _precipitation_labels(hours: list[ForecastHour]) -> list[str]:
    kinds = {
        match[2] for hour in hours
        if (match := PRECIPITATION_CODE.fullmatch(_weather_code(hour))) is not None
    }
    if any((hour.snowfall_cm or 0) > 0 for hour in hours):
        kinds.add("snow")
    labels = [label for kind, label in PRECIPITATION_LABELS.items() if kind in kinds]
    if not labels and any((hour.precipitation_mm or 0) > 0 for hour in hours):
        labels.append("💧 Опади")
    return labels


def _period_lines(hours: list[ForecastHour], day: date, now: datetime) -> list[str]:
    if not hours:
        return []
    local_now = now.astimezone(KYIV)
    by_hour = [(hour.forecast_at.astimezone(KYIV).hour, hour) for hour in hours]
    lines = []
    for start, end, label in DAY_PERIODS:
        if day == local_now.date() and local_now.hour >= end:
            continue
        period = [hour for local_hour, hour in by_hour if start <= local_hour < end]
        values = [hour.temperature_c for hour in period if hour.temperature_c is not None]
        temperature = (
            f"🌡 {_temperature_range(min(values), max(values))}" if values else TEMPERATURE_PENDING
        )
        lines.append(" · ".join((f"<b>{label}</b>", temperature, *_precipitation_labels(period))))
    return lines


def render_forecast(snapshot: ForecastSnapshot, locality: str, now: datetime, mode: str) -> str:
    today = now.astimezone(KYIV).date()
    tomorrow = today + timedelta(days=1)
    days: tuple[date, ...]
    if mode == "tomorrow":
        days = (tomorrow,)
    elif mode == "three":
        days = (today, tomorrow, tomorrow + timedelta(days=1))
    else:
        days = (today,)
    by_day: dict[date, list[ForecastHour]] = {}
    for hour in snapshot.hours:
        by_day.setdefault(hour.forecast_at.astimezone(KYIV).date(), []).append(hour)
    blocks = [f"🌤 <b>ПРОГНОЗ ПОГОДИ</b>\n📍 {escape(short_weather_locality(locality))}"]
    upcoming: list[ForecastHour] = []
    ranges = (
        {day: _daily_range(by_day.get(day, []), day) for day in {*days, today}}
        if mode != "now"
        else {}
    )
    for day in days:
        hours = by_day.get(day, [])
        visible = [hour for hour in hours if hour.forecast_at + ONE_HOUR > now]
        current = next((hour for hour in visible if hour.forecast_at <= now), None)
        if mode == "now":
            visible = [current] if current is not None else []
            label = "Зараз"
        else:
            label = (
                "Сьогодні"
                if day == today
                else ("Завтра" if day == tomorrow else WEEKDAYS[day.weekday()])
            )
        temperatures = []
        if day == today:
            if current is None or current.temperature_c is None:
                temperatures.append(TEMPERATURE_PENDING)
            else:
                prefix = "Зараз: " if mode != "now" else ""
                temperatures.append(f"🌡 {prefix}{current.temperature_c:+g}°C")
        if mode != "now" and (bounds := ranges[day]) is not None:
            prefix = "За добу: " if day == today else ""
            temperatures.append(f"🌡 {prefix}{_temperature_range(*bounds)}")
        if not temperatures:
            temperatures.append(TEMPERATURE_PENDING)
        icons = Counter(_condition(hour) for hour in visible)
        icons.pop("🌡", None)
        icon = _condition(current) if current is not None else (
            icons.most_common(1)[0][0] if icons else "🌡"
        )
        lines = [f"{icon} <b>{label}, {day.day} {MONTHS[day.month - 1]}</b>", *temperatures]
        if mode == "now":
            if precipitation := _precipitation_labels(visible):
                lines.append(" · ".join(precipitation))
        else:
            lines.extend(_period_lines(visible, day, now))
        blocks.append("\n".join(lines))
        upcoming.extend(visible)

    tips = []
    conditions = {_condition(hour) for hour in upcoming}
    if "⛈" in conditions:
        tips.append("⛈ Можлива гроза — будьте обережні надворі.")
    elif conditions & {"🌧", "🌦"}:
        tips.append("☂️ Очікується дощ — візьміть парасольку.")
    if conditions & {"❄️", "🌨"}:
        tips.append("❄️ Можливий сніг — оберіть тепле взуття.")
    if any(_strong_wind(hour) for hour in upcoming):
        tips.append("💨 Сильний вітер — будьте обережні надворі.")
    today_range, tomorrow_range = ranges.get(today), ranges.get(tomorrow)
    if (
        today_range is not None
        and tomorrow_range is not None
        and (today_range[1] - tomorrow_range[1] >= THRESHOLDS.temperature_change_c)
    ):
        tips.append("🥶 Завтра значно похолодає.")
    elif any(hour.temperature_c is not None and hour.temperature_c < 8 for hour in upcoming):
        tips.append("🧥 Буде прохолодно — одягайтеся тепліше.")
    if tips:
        blocks.append("\n".join(tips[:2]))
    return "\n\n".join(blocks)

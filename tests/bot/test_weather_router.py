from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Router

from time_tl.bot.weather_router import (
    SETTING_FLAGS,
    WeatherContext,
    WeatherUiPreferences,
    WeatherUx,
    build_weather_router,
)
from time_tl.domain.weather import ForecastHour, ForecastSnapshot, ForecastStatus

NOW = datetime(2026, 11, 5, 10, tzinfo=UTC)
TODAY_START = datetime(2026, 11, 4, 22, tzinfo=UTC)


def snapshot(*hours: ForecastHour) -> ForecastSnapshot:
    return ForecastSnapshot(
        "MET Norway",
        "p1",
        NOW,
        NOW,
        NOW + timedelta(hours=8),
        hours,
        ForecastStatus.FRESH,
        "https://example.test/technical-model",
    )


class Port:
    def __init__(self) -> None:
        self.prefs = WeatherUiPreferences()
        self.ctx = WeatherContext("Locality", True, "p1")
        self.value: ForecastSnapshot | None = None
        self.writes = 0

    async def context(self, actor: int) -> WeatherContext:
        assert actor == 1
        return self.ctx

    async def preferences(self, actor: int) -> WeatherUiPreferences:
        return self.prefs

    async def snapshot(self, actor: int) -> ForecastSnapshot | None:
        return self.value

    async def request_forecast(self, actor: int) -> WeatherUiPreferences:
        self.writes += 1
        self.prefs = replace(self.prefs, forecast_requested=True, revision=self.prefs.revision + 1)
        return self.prefs

    async def save_notifications(
        self, actor: int, enabled: bool, expected_revision: int
    ) -> WeatherUiPreferences:
        assert expected_revision == self.prefs.revision
        self.prefs = replace(self.prefs, weather_enabled=enabled, revision=expected_revision + 1)
        return self.prefs

    async def disable_weather(self, actor: int, expected_revision: int) -> WeatherUiPreferences:
        self.prefs = WeatherUiPreferences(revision=expected_revision + 1)
        return self.prefs

    async def save_settings(
        self, actor: int, expected_revision: int, **changes: object
    ) -> WeatherUiPreferences:
        assert expected_revision == self.prefs.revision
        self.prefs = replace(self.prefs, **changes, revision=expected_revision + 1)
        return self.prefs


def callbacks(view: object) -> set[str | None]:
    from time_tl.bot.models import View

    assert isinstance(view, View)
    return {button.callback_data for row in view.rows for button in row}


async def test_opening_menu_no_implicit_consent_or_notifications() -> None:
    port = Port()
    view = await WeatherUx(port).home(1)
    assert "wt:request" in callbacks(view)
    assert "wt:sources" in callbacks(view) and "https://" not in view.text
    assert port.writes == 0 and not port.prefs.weather_enabled
    await port.request_forecast(1)
    assert not port.prefs.weather_enabled
    assert {"wt:today", "wt:tomorrow"} <= callbacks(await WeatherUx(port).home(1))


async def test_oblast_or_unverified_point_requires_finer_selection() -> None:
    port = Port()
    port.ctx = WeatherContext("Oblast", False)
    assert "menu:region" in callbacks(await WeatherUx(port).home(1))
    assert "wt:request" not in callbacks(await WeatherUx(port).home(1))


async def test_pending_then_stale_never_means_no_weather_hazards() -> None:
    port = Port()
    await port.request_forecast(1)
    ux = WeatherUx(port, clock=lambda: NOW)
    assert "https://" not in (await ux.forecast(1, "now")).text
    port.value = ForecastSnapshot(
        "MET Norway",
        "p1",
        NOW,
        NOW,
        NOW,
        (ForecastHour(NOW, temperature_c=4),),
        ForecastStatus.FRESH,
        "url",
    )
    assert (await ux.forecast(1, "now")).text != (await ux.home(1)).text
    assert port.writes == 1


async def test_current_is_forecast_attributed_not_observation_and_point_change_rejected() -> None:
    port = Port()
    await port.request_forecast(1)
    port.value = ForecastSnapshot(
        "MET Norway",
        "p1",
        NOW,
        NOW,
        NOW + timedelta(hours=8),
        (
            ForecastHour(
                NOW, temperature_c=4, feels_like_c=1, precipitation_probability=0, wind_speed_ms=3
            ),
        ),
        ForecastStatus.FRESH,
        "url",
    )
    ux = WeatherUx(port, clock=lambda: NOW)
    card = await ux.forecast(1, "now")
    assert "🌡 <b>Прогноз на зараз, 5 листопада</b>\n🌡 <b>+4°C</b>" in card.text
    assert card.text.endswith("🧥 Одягайтеся тепліше.")
    assert card.parse_mode == "HTML"
    assert "https://" not in card.text and "wt:sources" in callbacks(card)
    port.value = replace(port.value, point_id="different")
    assert "+4" not in (await ux.forecast(1, "now")).text


async def test_multiday_card_groups_days_and_keeps_missing_data_visible() -> None:
    port = Port()
    await port.request_forecast(1)
    port.value = snapshot(
        ForecastHour(NOW, temperature_c=4, weather_code="partlycloudy_day"),
        *(
            ForecastHour(
                TODAY_START + timedelta(days=1, hours=n),
                temperature_c=6 if n else -1,
                weather_code="fair_day",
            )
            for n in range(24)
        ),
    )
    ux = WeatherUx(port, clock=lambda: NOW)
    tomorrow = await ux.forecast(1, "tomorrow")
    assert "🌤 <b>Завтра, 6 листопада</b>\n🌡 <b>−1…+6°C</b>" in tomorrow.text
    assert "Сьогодні" not in tomorrow.text and "https://" not in tomorrow.text
    three = await ux.forecast(1, "three")
    current = await ux.forecast(1, "now")
    assert "⛅ <b>Прогноз на зараз, 5 листопада</b>\n🌡 <b>+4°C</b>" in current.text
    assert "⛅ <b>Сьогодні, 5 листопада</b>\n🌡 Найближчі години: <b>+4°C</b>" in three.text
    assert "За добу:" not in three.text
    assert "\n\n🌡 <b>Субота, 7 листопада</b>\n🌡 Прогноз температури уточнюється" in three.text
    assert "https://" not in three.text and "wt:sources" in callbacks(three)


async def test_multiday_card_labels_precipitation_on_its_own_local_date() -> None:
    port = Port()
    await port.request_forecast(1)
    port.value = snapshot(
        ForecastHour(NOW - timedelta(hours=2), weather_code="rain"),
        ForecastHour(NOW, weather_code="cloudy"),
        ForecastHour(TODAY_START + timedelta(days=1), weather_code="lightrainshowers_night"),
        ForecastHour(TODAY_START + timedelta(days=1, hours=1), weather_code="cloudy"),
        ForecastHour(TODAY_START + timedelta(days=1, hours=2), weather_code="cloudy"),
        ForecastHour(TODAY_START + timedelta(days=2), weather_code="snow"),
        ForecastHour(TODAY_START + timedelta(days=2, hours=1), weather_code="sleet"),
    )
    card = await WeatherUx(port, clock=lambda: NOW).forecast(1, "three")
    _, today, tomorrow, next_day, _ = card.text.split("\n\n")
    assert "5 листопада" in today and "🌧 Дощ" not in today
    assert "☁️ <b>Завтра, 6 листопада" in tomorrow and "🌦 Невеликий дощ уночі" in tomorrow
    assert "7 листопада" in next_day and "❄️ Сніг від 00:00" in next_day
    assert "🌧 Дощ" not in next_day and "❄️ Сніг" not in tomorrow


async def test_day_parts_use_local_hours_and_only_show_expected_precipitation() -> None:
    port = Port()
    await port.request_forecast(1)
    tomorrow = TODAY_START + timedelta(days=1)
    port.value = snapshot(
        ForecastHour(NOW - timedelta(hours=1), temperature_c=-12, weather_code="snow"),
        ForecastHour(NOW, temperature_c=6, weather_code="cloudy"),
        ForecastHour(NOW + timedelta(hours=1), temperature_c=8, weather_code="rain"),
        ForecastHour(NOW + timedelta(hours=6), precipitation_mm=0.4),
        ForecastHour(tomorrow, temperature_c=-2, weather_code="sleet"),
        ForecastHour(tomorrow + timedelta(hours=6), temperature_c=1, weather_code="snow"),
        ForecastHour(tomorrow + timedelta(hours=12), temperature_c=7, weather_code="fair_day"),
        ForecastHour(tomorrow + timedelta(hours=18), temperature_c=2, weather_code="lightrain"),
    )
    ux = WeatherUx(port, clock=lambda: NOW)
    today = await ux.forecast(1, "today")
    assert "Найближчі години: <b>+6°C</b>" in today.text
    assert "☀️ День\u00a0+8°" in today.text and "🌧 Дощ від 13:00" in today.text
    assert all(word not in today.text for word in ("Ніч", "Ранок", "Вечір", "−12", "❄️ Сніг"))
    next_day = await ux.forecast(1, "tomorrow")
    assert "🌙 Ніч\u00a0−2° · 🌅 Ранок\u00a0+1°" in next_day.text
    assert "☀️ День\u00a0+7° · 🌆 Вечір\u00a0+2°" in next_day.text
    assert "🌨 Мокрий сніг від 00:00" in next_day.text and "❄️ Сніг від 06:00" in next_day.text
    current = await ux.forecast(1, "now")
    assert "+6°C" in current.text and "День" not in current.text


@pytest.mark.parametrize(
    ("code", "gust", "icon", "tip"),
    [
        ("clearsky_day", None, "☀️", None),
        ("fair_day", None, "🌤", None),
        ("partlycloudy_day", None, "⛅", None),
        ("cloudy", None, "☁️", None),
        ("fog", None, "🌫", None),
        ("rain", None, "🌧", "Візьміть парасольку"),
        ("lightrainshowers_night", None, "🌦", "Візьміть парасольку"),
        ("rainandthunder", None, "⛈", "Можлива гроза"),
        ("snow", None, "❄️", "Оберіть тепле взуття"),
        ("heavysnowshowers_day", None, "🌨", "Оберіть тепле взуття"),
        ("cloudy", 19, "💨", "сильний вітер"),
        (None, None, "🌡", None),
        ("unknown_rain", None, "🌡", None),
    ],
)
async def test_weather_codes_and_supported_tips(
    code: str | None, gust: float | None, icon: str, tip: str | None
) -> None:
    port = Port()
    await port.request_forecast(1)
    port.value = snapshot(ForecastHour(NOW, temperature_c=20, weather_code=code, wind_gust_ms=gust))
    card = await WeatherUx(port, clock=lambda: NOW).forecast(1, "now")
    assert f"{icon} <b>Прогноз на зараз, 5 листопада</b>" in card.text
    if tip:
        assert tip in card.text
    else:
        assert card.text.endswith("🌡 <b>+20°C</b>")
        assert len(card.text.split("\n\n")) == 2


async def test_cooling_requires_two_complete_days_and_tips_are_bounded() -> None:
    port = Port()
    await port.request_forecast(1)
    port.value = snapshot(
        *(
            ForecastHour(TODAY_START + timedelta(hours=n), temperature_c=15 if n < 24 else 5)
            for n in range(48)
        )
    )
    ux = WeatherUx(port, clock=lambda: NOW)
    assert "🥶 Завтра суттєво похолодає." in (await ux.forecast(1, "tomorrow")).text
    three = await ux.forecast(1, "three")
    assert "🌡 Найближчі години: <b>+15°C</b>" in three.text
    hours = list(port.value.hours)
    hours[24] = replace(hours[24], temperature_c=None)
    hours[-1] = replace(hours[-1], weather_code="rain", wind_gust_ms=20)
    port.value = replace(port.value, hours=tuple(hours))
    text = (await ux.forecast(1, "three")).text
    assert "Прогноз температури уточнюється" in text
    assert "Завтра суттєво похолодає" not in text
    assert "Візьміть дощовик" in text and "сильний вітер" in text
    assert "Одягайтеся тепліше" not in text


async def test_telegram_callback_formats_and_escapes_forecast_with_accessible_licences() -> None:
    port = Port()
    port.ctx = replace(port.ctx, locality="м. Кам'янське <&> та Кам'янська територіальна громада")
    await port.request_forecast(1)
    port.value = snapshot(ForecastHour(NOW, temperature_c=9, weather_code="fair_day"))
    router = Router()
    build_weather_router(WeatherUx(port, clock=lambda: NOW), router=router)
    handler = router.callback_query.handlers[0].callback
    message = SimpleNamespace(chat=SimpleNamespace(id=1, type="private"), edit_text=AsyncMock())
    callback = SimpleNamespace(
        data="wt:now", from_user=SimpleNamespace(id=1), message=message, answer=AsyncMock()
    )
    state = SimpleNamespace(get_state=AsyncMock(return_value=None))
    await handler(callback, state)
    args, kwargs = message.edit_text.await_args
    assert kwargs["parse_mode"] == "HTML"
    assert "<b>ПРОГНОЗ ПОГОДИ</b>" in args[0]
    assert "📍 Кам&#x27;янське &lt;&amp;&gt;\n" in args[0]
    assert "Кам'янська територіальна громада" not in args[0]
    assert args[0].endswith("🌡 <b>+9°C</b>")
    assert not any(label in args[0] for label in ("https://", "MET Norway", "OpenStreetMap"))
    buttons = [button for row in kwargs["reply_markup"].inline_keyboard for button in row]
    assert [(button.text, button.callback_data) for button in buttons] == [
        ("ℹ️ Джерела та ліцензії", "wt:sources"),
        ("⬅️ Погода", "menu:weather"),
    ]
    callback.data = "wt:sources"
    await handler(callback, state)
    info = message.edit_text.await_args.args[0]
    assert "MET Norway" in info and "https://creativecommons.org/licenses/by/4.0/" in info
    assert "ODbL" in info and "https://www.openstreetmap.org/copyright" in info
    assert port.writes == 1


async def test_unavailable_hour_not_synthesized_from_six_hour_period() -> None:
    port = Port()
    await port.request_forecast(1)
    port.value = ForecastSnapshot(
        "MET Norway",
        "p1",
        NOW,
        NOW,
        NOW + timedelta(hours=8),
        (ForecastHour(NOW - timedelta(hours=3), temperature_c=4),),
        ForecastStatus.FRESH,
        "url",
    )
    assert "+4" not in (await WeatherUx(port, clock=lambda: NOW).forecast(1, "now")).text


async def test_compact_three_day_rounding_timing_and_gust_threshold() -> None:
    port = Port()
    await port.request_forecast(1)
    tomorrow = TODAY_START + timedelta(days=1)
    port.value = snapshot(
        ForecastHour(NOW, temperature_c=-2.4, wind_gust_ms=11.9),
        *(
            ForecastHour(
                tomorrow + timedelta(hours=n),
                temperature_c=9.5 if n >= 12 else -2.5,
                weather_code="rain" if n == 14 else "fair",
                precipitation_probability=80 if n == 14 else None,
                wind_gust_ms=12 if n == 15 else None,
            )
            for n in range(24)
        ),
        *(
            ForecastHour(tomorrow + timedelta(days=1, hours=n), temperature_c=3.2)
            for n in range(24)
        ),
    )
    default = await WeatherUx(port, clock=lambda: NOW).forecast(1, "three")
    _, today, next_day, later, tips = default.text.split("\n\n")
    assert "Найближчі години: <b>−2°C</b>" in today and "Пориви" not in today
    assert "🌡 <b>−3…+10°C</b>" in next_day
    assert "🌧 Дощ від 14:00 · 80%" in next_day
    assert "💨 Пориви до 12 м/с" in next_day
    assert "🌙\u00a0+3° · 🌅\u00a0+3° · ☀️\u00a0+3° · 🌆\u00a0+3°" in later
    assert "Візьміть дощовик" in tips and len(default.text.splitlines()) <= 18
    higher = await WeatherUx(port, clock=lambda: NOW, gust_threshold=15).forecast(1, "tomorrow")
    assert "Пориви" not in higher.text and "сильний вітер" not in higher.text
    assert 5 <= len(higher.text.splitlines()) <= 10


async def test_evening_shows_next_forecast_and_shortens_long_locality() -> None:
    port = Port()
    await port.request_forecast(1)
    port.ctx = replace(port.ctx, locality="Назва " * 40 + "<i>тег</i>")
    evening = TODAY_START + timedelta(hours=20)
    port.value = replace(
        snapshot(
            ForecastHour(evening - timedelta(hours=5), temperature_c=18, weather_code="rain"),
            ForecastHour(evening + timedelta(hours=1), temperature_c=0.1, precipitation_mm=0.4),
        ),
        expires_at=evening + timedelta(hours=2),
    )
    card = await WeatherUx(port, clock=lambda: evening).forecast(1, "today")
    assert card.text.splitlines()[1].endswith("…")
    assert "Найближчі години: <b>0°C</b>" in card.text
    assert "🌆 Вечір\u00a00°" in card.text and "💧 Опади від 21:00" in card.text
    assert all(word not in card.text for word in ("День", "Дощ", "+18", "%", "<i>"))


async def test_settings_transport_capability_off_no_enable_button() -> None:
    port = Port()
    view = await WeatherUx(port).settings(1)
    assert not any((item or "").startswith("wt:notify") for item in callbacks(view))
    enabled = await WeatherUx(port, notifications_available=True).settings(1)
    assert "wt:notify:1:0" in callbacks(enabled)


async def test_full_independent_preferences_controls_visible_without_auto_enable() -> None:
    port = Port()
    view = await WeatherUx(port).settings(1)
    actions = callbacks(view)
    assert {
        "wt:flag:daily:0",
        "wt:flag:change:0",
        "wt:flag:rain:0",
        "wt:flag:snow:0",
        "wt:flag:wind:0",
        "wt:flag:temp:0",
        "wt:flag:frost:0",
        "wt:lead:120:0",
        "wt:time:0700:0",
    } <= actions
    assert port.writes == 0 and not port.prefs.weather_enabled


async def test_settings_describe_every_type_and_use_the_confirmed_locality() -> None:
    port = Port()
    port.ctx = replace(port.ctx, locality="м. Кам'янське та Кам'янська територіальна громада")
    port.prefs = replace(
        port.prefs, weather_enabled=True, forecast_requested=True,
        **dict.fromkeys((field for _, field, _ in SETTING_FLAGS), False),
    )
    ux = WeatherUx(port, notifications_available=True)
    view = await ux.settings(1)
    assert "Загальний перемикач: увімкнено." in view.text
    assert "Усі типи вимкнено. Погодні повідомлення не надходитимуть." in view.text
    assert "📍 Кам'янське" in view.text
    assert "07:00 (місцевий час)" in view.text and "Київ" not in view.text
    for _, _, label in SETTING_FLAGS:
        assert f"{label} — вимкнено" in view.text
    port.ctx = replace(port.ctx, locality="м. Львів")
    port.prefs = replace(port.prefs, weather_enabled=False, daily_forecast_enabled=True)
    view = await ux.settings(1)
    assert "📍 Львів" in view.text
    assert "Щоденний прогноз — увімкнено" in view.text
    assert "Погодні повідомлення призупинено." in view.text


async def test_disabled_collection_never_claims_forecast_preparing() -> None:
    port = Port()
    await port.request_forecast(1)
    port.ctx = replace(port.ctx, collection_enabled=False)
    ux = WeatherUx(port, clock=lambda: NOW)
    disabled = (await ux.forecast(1, "now")).text
    port.ctx = replace(port.ctx, collection_enabled=True)
    preparing = (await ux.forecast(1, "now")).text
    assert disabled != preparing
    port.value = ForecastSnapshot(
        "MET Norway",
        "p1",
        NOW,
        NOW,
        NOW + timedelta(hours=8),
        (ForecastHour(NOW, temperature_c=4),),
        ForecastStatus.FRESH,
        "url",
    )
    port.ctx = replace(port.ctx, collection_enabled=False)
    cached = (await ux.forecast(1, "now")).text
    assert "+4" in cached

"""The requested visual grouping preserves callback identity and feature gates."""

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import CallbackQuery, Chat, Message, User

from time_tl.bot.async_service import AsyncTelegramApplication, build_async_router
from time_tl.bot.models import NotificationMode
from time_tl.bot.service import TelegramApplication, UserAccount
from time_tl.bot.weather_router import WeatherContext, WeatherUiPreferences
from time_tl.domain.power_outages.models import PowerProvider, PowerSubscription

POWER_LINE = "⚡ Сповіщення про графіки відключень — увімкнено"
WEATHER_LINE = "🌤 Сповіщення про погоду — увімкнено"
CALENDAR_LINE = "📅 Нагадування календаря — увімкнено"


def notification_menu_app(
    *, delivery_available=True, onboarding_complete=True, calendar_reminder_port=None
):
    account = UserAccount(
        7,
        mode=NotificationMode.SIRENS,
        region_id="31" if onboarding_complete else None,
        region_name="Київ" if onboarding_complete else None,
    )
    repository = SimpleNamespace(
        get_existing=AsyncMock(return_value=account),
        get_or_create=AsyncMock(return_value=account),
        save=AsyncMock(),
    )
    power = SimpleNamespace(
        subscription=AsyncMock(
            return_value=PowerSubscription(
                "actor",
                PowerProvider.DTEK,
                "6.1",
                enabled=True,
                outage_notifications_enabled=False,
                restoration_notifications_enabled=False,
            )
        )
    )
    weather = SimpleNamespace(
        preferences=AsyncMock(
            return_value=WeatherUiPreferences(forecast_requested=True, weather_enabled=True)
        ),
        context=AsyncMock(return_value=WeatherContext("Кам’янське", True)),
    )
    app = AsyncTelegramApplication(
        repository=repository,
        regions=(),
        resolver=None,
        my_day_enabled=True,
        calendars_enabled=True,
        calendar_port=SimpleNamespace(),
        calendar_reminder_port=calendar_reminder_port,
        power_outages_enabled=True,
        power_port=power,
        power_supported_providers=(PowerProvider.DTEK,),
        power_notifications_available=delivery_available,
        weather_enabled=True,
        weather_port=weather,
        weather_notifications_available=delivery_available,
    )
    return app, repository, power, weather


def test_main_menu_has_four_paired_rows():
    app = TelegramApplication(
        regions=(),
        resolver=None,
        my_day_enabled=True,
        calendars_enabled=True,
        homework_enabled=True,
        power_outages_enabled=True,
        weather_enabled=True,
    )
    assert [[b.callback_data for b in row] for row in app.menu(7).rows] == [
        ["menu:myday", "menu:calendars"],
        ["menu:schedules", "menu:homework"],
        ["menu:power", "menu:weather"],
        ["menu:settings", "menu:info"],
    ]


def test_disabled_weather_has_no_dead_button_or_empty_row():
    app = TelegramApplication(regions=(), resolver=None, power_outages_enabled=True)
    rows = app.menu(7).rows
    assert all(rows) and all(len(row) <= 2 for row in rows)
    assert "menu:weather" not in [b.callback_data for row in rows for b in row]


@pytest.mark.asyncio
async def test_start_and_home_show_current_electricity_and_weather_preferences():
    app, repository, power, weather = notification_menu_app()

    (start,) = await app.start_sequence(7)
    home = await app.menu(7)
    assert start == home
    assert "🔔 Сповіщення: Лише тривоги" in home.text
    assert home.text.endswith(f"{POWER_LINE}\n{WEATHER_LINE}")
    assert "календар" not in home.text.casefold()
    assert any(button.callback_data == "menu:calendars" for row in home.rows for button in row)

    power.subscription.return_value = replace(power.subscription.return_value, enabled=False)
    weather.preferences.return_value = replace(
        weather.preferences.return_value, forecast_requested=False
    )
    updated = await app.menu(7)
    assert POWER_LINE not in updated.text and WEATHER_LINE not in updated.text
    assert updated.rows == home.rows
    repository.save.assert_not_awaited()


@pytest.mark.asyncio
async def test_menu_omits_unavailable_delivery_without_loading_preferences():
    app, _, power, weather = notification_menu_app(delivery_available=False)
    home = await app.menu(7)
    assert POWER_LINE not in home.text and WEATHER_LINE not in home.text
    power.subscription.assert_not_awaited()
    weather.preferences.assert_not_awaited()
    weather.context.assert_not_awaited()


@pytest.mark.asyncio
async def test_menu_keeps_independent_weather_status_when_power_read_fails():
    app, _, power, weather = notification_menu_app()
    power.subscription.side_effect = OSError("unavailable")

    home = await app.menu(7)
    assert POWER_LINE not in home.text and WEATHER_LINE in home.text

    weather.context.return_value = WeatherContext("Кам’янське", True, collection_enabled=False)
    assert WEATHER_LINE not in (await app.menu(7)).text


@pytest.mark.asyncio
async def test_correction_only_weather_and_unsupported_power_selection_are_omitted():
    app, _, power, weather = notification_menu_app()
    power.subscription.return_value = replace(
        power.subscription.return_value, provider=PowerProvider.CEK
    )
    weather.preferences.return_value = WeatherUiPreferences(
        forecast_requested=True,
        weather_enabled=True,
        weather_changes_enabled=True,
        rain_alert_enabled=False,
        snow_alert_enabled=False,
        wind_alert_enabled=False,
        temperature_alert_enabled=False,
        frost_alert_enabled=False,
    )
    home = await app.menu(7)
    assert POWER_LINE not in home.text and WEATHER_LINE not in home.text
    weather.context.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_keeps_initial_onboarding_without_optional_preference_reads():
    app, _, power, weather = notification_menu_app(onboarding_complete=False)
    views = await app.start_sequence(7)
    assert len(views) == 2
    assert "Спочатку оберіть режим сповіщень" in views[-1].text
    power.subscription.assert_not_awaited()
    weather.preferences.assert_not_awaited()


@pytest.mark.asyncio
async def test_calendar_reminder_opt_in_and_home_status_use_committed_preferences(monkeypatch):
    reminders = SimpleNamespace(preferences=AsyncMock(return_value=False), set_enabled=AsyncMock())

    async def save(actor, enabled):
        assert actor == 7
        reminders.preferences.return_value = enabled
        return enabled

    reminders.set_enabled.side_effect = save
    app, _, _, _ = notification_menu_app(calendar_reminder_port=reminders)
    sink = AsyncMock()
    monkeypatch.setattr("time_tl.bot.aiogram_adapter._edit", sink)
    router = build_async_router(app)
    user = User(id=7, is_bot=False, first_name="Test")
    message = Message(
        message_id=1, date=datetime.now(UTC), chat=Chat(id=7, type="private"), from_user=user
    )

    async def tap(data):
        callback = CallbackQuery(
            id="local", from_user=user, chat_instance="local", message=message, data=data
        )
        for handler in router.callback_query.handlers:
            matched, _ = await handler.check(callback)
            if matched:
                await handler.callback(callback)
                return sink.await_args.args[1]
        pytest.fail(f"Missing calendar reminder callback {data}")

    assert any(
        b.callback_data == "calendar-reminders:settings"
        for row in app.notifications_menu().rows
        for b in row
    )
    assert CALENDAR_LINE not in (await app.menu(7)).text

    settings = await tap("calendar-reminders:settings")
    assert "вимкнено" in settings.text and "за 10 хв" in settings.text
    assert "власних особистих подій із зазначеним часом" in settings.text
    assert settings.rows[0][0].callback_data == "calendar-reminders:on"
    reminders.set_enabled.assert_not_awaited()

    opted_in = await tap("calendar-reminders:on")
    assert CALENDAR_LINE in opted_in.text
    assert opted_in.rows[0][0].callback_data == "calendar-reminders:off"
    assert CALENDAR_LINE in (await app.menu(7)).text

    await tap("calendar-reminders:off")
    reminders.set_enabled.assert_awaited_with(7, False)
    assert CALENDAR_LINE not in (await app.menu(7)).text


@pytest.mark.asyncio
async def test_calendar_reminder_gate_handles_an_old_settings_button_without_activation():
    app, _, _, _ = notification_menu_app()
    assert "Для подій календаря окремих сповіщень поки немає." in app.notifications_menu().text
    assert "недоступні" in (await app.calendar_reminder_settings(7, enabled=True)).text
    assert CALENDAR_LINE not in (await app.menu(7)).text

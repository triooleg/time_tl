"""LOCAL menu placement and compatibility; no network or Telegram delivery."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import CallbackQuery, Chat, Message, User

from time_tl.bot.async_service import AsyncTelegramApplication, build_async_router
from time_tl.bot.models import Button, NotificationMode, View
from time_tl.bot.service import TelegramApplication, UserAccount


def callbacks(view):
    return [button.callback_data for row in view.rows for button in row]


@pytest.mark.parametrize("homework", [False, True])
@pytest.mark.parametrize("my_day", [False, True])
@pytest.mark.parametrize("calendars", [False, True])
def test_main_menu_keeps_learning_sections_and_settings_without_duplicates(
    homework, my_day, calendars
):
    app = TelegramApplication(
        regions=(),
        resolver=SimpleNamespace(),
        homework_enabled=homework,
        my_day_enabled=my_day,
        calendars_enabled=calendars,
    )
    expected = ["menu:myday"] if my_day else []
    expected += ["menu:calendars"] if calendars else []
    expected += ["menu:schedules"]
    expected += ["menu:homework"] if homework else []
    expected += ["menu:settings", "menu:info"]
    home = app.menu(1)
    assert callbacks(home) == expected
    assert len(expected) == len(set(expected))
    assert home.text.startswith("🏠 Головне меню")
    labels = [button.label for row in home.rows for button in row]
    assert len(labels) == len(set(labels))
    assert not {"menu:notifications", "menu:region", "menu:reminder", "settings:privacy"} & set(
        expected
    )


def test_configuration_controls_have_one_entry_each_under_settings():
    settings = TelegramApplication.settings_menu()
    assert callbacks(settings) == [
        "menu:notifications",
        "menu:region",
        "menu:reminder",
        "settings:privacy",
        "menu:home",
    ]
    notifications = TelegramApplication.notifications_menu()
    assert callbacks(notifications) == ["menu:mode", "menu:settings", "menu:home"]
    assert "menu:reminder" not in callbacks(notifications)
    app = TelegramApplication(regions=(), resolver=SimpleNamespace())
    assert "menu:settings" in callbacks(app.mode_prompt())
    assert "menu:settings" in callbacks(app.reminder_prompt())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ("menu:settings", "menu:region"),
        ("menu:notifications", "menu:mode"),
        ("menu:region", "local:territory"),
        ("menu:reminder", "reminder:0"),
        ("menu:home", "menu:settings"),
    ],
)
async def test_existing_callbacks_still_dispatch_without_changing_preferences(
    monkeypatch, data, expected
):
    sink = AsyncMock()
    monkeypatch.setattr("time_tl.bot.aiogram_adapter._edit", sink)
    account = UserAccount(1, mode=NotificationMode.SIRENS, region_id="31", region_name="Київ")
    repo = SimpleNamespace(
        get_existing=AsyncMock(return_value=account),
        get_or_create=AsyncMock(return_value=account),
        save=AsyncMock(),
    )
    app = AsyncTelegramApplication(repository=repo, regions=(), resolver=SimpleNamespace())
    app.territory_top = AsyncMock(
        return_value=View(
            "LOCAL territory selector", ((Button("LOCAL region", "local:territory"),),)
        )
    )
    router = build_async_router(app)
    user = User(id=1, is_bot=False, first_name="Test")
    message = Message(
        message_id=1, date=datetime.now(UTC), chat=Chat(id=1, type="private"), from_user=user
    )
    event = CallbackQuery(
        id="local", from_user=user, chat_instance="local", message=message, data=data
    )
    for handler in router.callback_query.handlers:
        matched, _ = await handler.check(event)
        if matched:
            await handler.callback(event)
            break
    else:
        pytest.fail("Existing menu callback is not registered")
    view = sink.await_args.args[1]
    assert expected in callbacks(view)
    repo.save.assert_not_awaited()
    assert account.mode == NotificationMode.SIRENS
    assert account.region_id == "31"

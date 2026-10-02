from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Message, User

from time_tl.bot.async_service import (
    AsyncTelegramApplication,
    ExistingAccountMiddleware,
    build_async_router,
)
from time_tl.bot.service import UserAccount
from time_tl.domain.calendar_memberships import CalendarRole, CalendarSummary
from time_tl.domain.calendars import CalendarKind


@pytest.mark.asyncio
@pytest.mark.parametrize("navigation", ["menu:home", "/menu", "/start"])
async def test_root_router_navigation_exits_calendar_target_prompt(monkeypatch, navigation):
    sink = AsyncMock()
    monkeypatch.setattr("time_tl.bot.aiogram_adapter._edit", sink)
    monkeypatch.setattr("time_tl.bot.aiogram_adapter._answer", sink)
    account = UserAccount(1)
    repository = SimpleNamespace(
        get_existing=AsyncMock(return_value=account),
        get_or_create=AsyncMock(return_value=account),
        save=AsyncMock(),
    )
    identity = uuid4()
    summary = CalendarSummary(identity, "Клас", CalendarKind.PERSONAL, CalendarRole.OWNER, True, 1)
    port = SimpleNamespace(list_calendars=AsyncMock(return_value=(summary,)))
    application = AsyncTelegramApplication(
        repository=repository,
        regions=(),
        resolver=SimpleNamespace(),
        my_day_enabled=True,
        calendars_enabled=True,
        calendar_port=port,
    )
    router = build_async_router(application)
    state = FSMContext(MemoryStorage(), StorageKey(bot_id=42, chat_id=1, user_id=1))
    user = User(id=1, is_bot=False, first_name="Test")
    message = Message(
        message_id=1, date=datetime.now(UTC), chat=Chat(id=1, type="private"), from_user=user
    )
    invite = CallbackQuery(
        id="invite",
        from_user=user,
        chat_instance="local",
        data=f"cal:invite:{identity.hex}:1",
        message=message,
    )
    await router.propagate_event("callback_query", invite, state=state, raw_state=None)
    assert await state.get_state() == "CalendarFlow:target"
    raw_state = await state.get_state()
    if navigation.startswith("/"):
        event = message.model_copy(update={"text": navigation})
        await router.propagate_event(
            "message", event, state=state, raw_state=raw_state, bot=SimpleNamespace()
        )
    else:
        event = invite.model_copy(update={"id": "home", "data": navigation})
        await router.propagate_event("callback_query", event, state=state, raw_state=raw_state)
    assert await state.get_state() is None
    assert await state.get_data() == {}
    # Subsequent ordinary text cannot match the earlier calendar-owned FSM filter.
    target = next(h for h in router.message.handlers if h.callback.__name__ == "calendar_target")
    matched, _ = await target.check(message.model_copy(update={"text": "стор. 24"}), raw_state=None)
    assert not matched


@pytest.mark.asyncio
async def test_calendar_account_read_failure_is_bounded_generic(monkeypatch):
    app = SimpleNamespace(
        calendars_enabled=True,
        my_day_enabled=True,
        account_exists=AsyncMock(side_effect=RuntimeError("private database")),
    )
    event = type("CallbackQuery", (), {})()
    event.from_user = SimpleNamespace(id=1)
    event.data = "menu:calendars"
    event.message = SimpleNamespace(edit_text=AsyncMock())
    event.answer = AsyncMock()
    handler = AsyncMock()
    await ExistingAccountMiddleware(app)(handler, event, {})
    handler.assert_not_awaited()
    text = event.message.edit_text.call_args.args[0]
    assert "недоступні" in text and "private" not in text

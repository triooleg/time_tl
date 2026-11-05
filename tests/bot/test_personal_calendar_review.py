"""Independent CAL29 regression checks; no network or production adapter."""

from datetime import UTC, date, datetime, time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from aiogram import Router
from aiogram.types import CallbackQuery, Chat, Message, User

from time_tl.bot.personal_calendar_router import (
    PersonalCalendarUx,
    build_personal_calendar_router,
)
from time_tl.bot.service import UserAccount
from time_tl.domain.calendar_memberships import CalendarAccessDenied
from time_tl.domain.calendar_overrides import event_choices
from time_tl.domain.schedule import Lesson, ScheduleProfile, ScheduleRule

NOW = datetime(2026, 10, 4, 8, tzinfo=UTC)


def setup():
    identity = uuid4()
    profile = ScheduleProfile(
        "fixture", "Calendar", rules=(ScheduleRule(6, (Lesson(time(11), time(12), "Lesson"),)),)
    )
    choice = event_choices(identity, 1, 1, profile, date(2026, 10, 4), user_id=1)[0]
    port = SimpleNamespace(
        list_event_choices=AsyncMock(return_value=(choice,)),
        save_override=AsyncMock(return_value=1),
    )
    accounts = SimpleNamespace(get_existing=AsyncMock(return_value=UserAccount(1)))
    return PersonalCalendarUx(port, accounts, now=lambda: NOW), port, identity


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["choose", "preview", "move_preview"])
async def test_cached_details_revalidate_visibility_before_rendering(stage):
    ux, port, identity = setup()
    handle, _ = await ux.begin(1, identity)
    await ux.input(1, handle, "2026-10-04")
    if stage != "choose":
        await ux.action(1, handle, "choose", "0")
    if stage == "move_preview":
        await ux.action(1, handle, "move")
    # Membership removed/hidden after the choice list was legitimately read.
    port.list_event_choices.side_effect = CalendarAccessDenied()
    with pytest.raises(CalendarAccessDenied):
        if stage == "move_preview":
            await ux.input(1, handle, "2026-10-05 12:00-13:00")
        else:
            await ux.action(1, handle, "choose" if stage == "choose" else "drop", "0")
    port.save_override.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", ["data", "state"])
async def test_navigation_during_fsm_publication_cannot_resurrect_input(interruption):
    ux, port, identity = setup()
    values = {"state": None, "data": {}}

    async def clear():
        values.update(state=None, data={})

    async def set_data(data):
        values["data"] = data
        # Home invalidates the draft while callback publication awaits storage.
        if interruption == "data":
            ux.abort(1)
            await clear()

    async def set_state(state):
        values["state"] = state.state
        if interruption == "state":
            ux.abort(1)
            await clear()

    state = SimpleNamespace(
        clear=clear,
        set_data=set_data,
        set_state=set_state,
        get_state=AsyncMock(return_value=None),
    )
    event = CallbackQuery(
        id="fixture",
        from_user=User(id=1, is_bot=False, first_name="Actor"),
        chat_instance="fixture",
        data=f"pcal:open:{identity.hex}",
        message=Message(message_id=1, date=NOW, chat=Chat(id=1, type="private")),
    )
    with (
        patch.object(CallbackQuery, "answer", new_callable=AsyncMock),
        patch("time_tl.bot.aiogram_adapter._edit", new_callable=AsyncMock),
    ):
        router = build_personal_calendar_router(ux, router=Router(), enabled=True)
        await router.propagate_event("callback_query", event, state=state)
    assert not ux.pending
    assert values == {"state": None, "data": {}}
    port.save_override.assert_not_awaited()

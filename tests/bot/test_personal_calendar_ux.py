from __future__ import annotations

from datetime import UTC, date, datetime, time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from time_tl.bot.personal_calendar_router import PersonalCalendarUx
from time_tl.bot.service import UserAccount
from time_tl.domain.calendar_memberships import CalendarAccessDenied, CalendarDataUnavailable
from time_tl.domain.calendar_overrides import OverrideAction, event_choices
from time_tl.domain.schedule import Lesson, ScheduleProfile, ScheduleRule

NOW = datetime(2026, 10, 4, 8, tzinfo=UTC)


def setup():
    identity = uuid4()
    profile = ScheduleProfile(
        "fixture", "Calendar", rules=(ScheduleRule(6, (Lesson(time(11), time(12), "Математика"),)),)
    )
    choice = event_choices(identity, 1, 1, profile, date(2026, 10, 4), user_id=1)[0]
    port = SimpleNamespace(
        list_event_choices=AsyncMock(return_value=(choice,)),
        save_override=AsyncMock(return_value=1),
    )
    accounts = SimpleNamespace(get_existing=AsyncMock(return_value=UserAccount(1)))
    return PersonalCalendarUx(port, accounts, now=lambda: NOW), port, identity


async def select(ux, identity):
    handle, _ = await ux.begin(1, identity)
    await ux.input(1, handle, "2026-10-04")
    await ux.action(1, handle, "choose", "0")
    return handle


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action,expected", [("drop", OverrideAction.CANCEL), ("reset", OverrideAction.RESET)]
)
async def test_preview_requires_explicit_save_and_replay_fails(action, expected):
    ux, port, identity = setup()
    handle = await select(ux, identity)
    view = await ux.action(1, handle, action)
    assert "лише для вас" in view.text
    port.save_override.assert_not_awaited()
    await ux.action(1, handle, "save")
    assert port.save_override.await_args.args[1].action is expected
    with pytest.raises(CalendarDataUnavailable):
        await ux.action(1, handle, "save")
    assert port.save_override.await_count == 1


@pytest.mark.asyncio
async def test_move_and_abort_preserve_shared_calendar():
    ux, port, identity = setup()
    handle = await select(ux, identity)
    await ux.action(1, handle, "move")
    await ux.input(1, handle, "2026-10-05 12:00-13:00")
    await ux.action(1, handle, "abort")
    port.save_override.assert_not_awaited()
    assert not ux.pending


@pytest.mark.asyncio
async def test_foreign_actor_cannot_use_draft():
    ux, port, identity = setup()
    handle = await select(ux, identity)
    with pytest.raises(CalendarDataUnavailable):
        await ux.action(2, handle, "drop")
    port.save_override.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["2026-10-03", "2026-11-04", "04.10.2026", "garbage"])
async def test_invalid_date_does_not_query_or_write(text):
    ux, port, identity = setup()
    handle, _ = await ux.begin(1, identity)
    with pytest.raises((CalendarDataUnavailable, ValueError)):
        await ux.input(1, handle, text)
    port.list_event_choices.assert_not_awaited()
    port.save_override.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_account_rejects_begin():
    ux, port, identity = setup()
    ux.accounts.get_existing.return_value = None
    with pytest.raises(CalendarAccessDenied):
        await ux.begin(1, identity)
    port.save_override.assert_not_awaited()


@pytest.mark.asyncio
async def test_timeout_consumes_confirmation_even_if_write_outcome_is_unknown():
    ux, port, identity = setup()
    handle = await select(ux, identity)
    await ux.action(1, handle, "drop")
    port.save_override.side_effect = TimeoutError()
    with pytest.raises(TimeoutError):
        await ux.action(1, handle, "save")
    assert not ux.pending
    with pytest.raises(CalendarDataUnavailable):
        await ux.action(1, handle, "save")


@pytest.mark.asyncio
async def test_cancel_during_choice_read_cannot_resurrect_draft():
    ux, port, identity = setup()
    handle, _ = await ux.begin(1, identity)
    original = port.list_event_choices.return_value

    async def cancelled(*args):
        ux.abort(1)
        return original

    port.list_event_choices.side_effect = cancelled
    with pytest.raises(CalendarDataUnavailable):
        await ux.input(1, handle, "2026-10-04")
    assert not ux.pending

from __future__ import annotations

import asyncio
import copy
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
from aiogram import Bot, Dispatcher, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from time_tl.bot.calendar_editing import CalendarInputError
from time_tl.bot.calendar_router import CalendarUx, _callback, build_calendar_router
from time_tl.bot.models import View
from time_tl.bot.service import UserAccount
from time_tl.domain.calendar_memberships import (
    CalendarAccessDenied,
    CalendarConflict,
    CalendarDataUnavailable,
    CalendarLimitExceeded,
    CalendarRole,
    CalendarSummary,
    may_edit,
)
from time_tl.domain.calendars import CalendarKind
from time_tl.domain.schedule import DateOverride, Lesson, Parity, ScheduleProfile, ScheduleRule

NOW = datetime(2026, 10, 4, 9, tzinfo=UTC)
LINES = "Пн 08:30–09:15 Математика\nСб 10:00-10:45 Музика"


class Accounts:
    def __init__(self) -> None:
        self.items = {
            actor: UserAccount(
                actor,
                profiles={
                    "active": ScheduleProfile(
                        "active",
                        "Школа",
                        active=True,
                        rules=(ScheduleRule(1, (Lesson(time(9), time(10), "Історія"),)),),
                    )
                },
            )
            for actor in (1, 2)
        }

    async def get_existing(self, actor: int) -> UserAccount | None:
        return copy.deepcopy(self.items.get(actor))


class Port:
    def __init__(self) -> None:
        self.rows: dict[UUID, CalendarSummary] = {}
        self.profiles: dict[UUID, ScheduleProfile] = {}
        self.role = CalendarRole.OWNER
        self.writes: list[str] = []

    async def list_calendars(self, actor: int) -> tuple[CalendarSummary, ...]:
        return (
            tuple(replace(row, role=self.role) for row in self.rows.values()) if actor == 1 else ()
        )

    async def get_snapshot(
        self, actor: int, identity: UUID
    ) -> tuple[CalendarSummary, ScheduleProfile]:
        if actor != 1:
            raise CalendarAccessDenied()
        return replace(self.rows[identity], role=self.role), self.profiles[identity]

    async def create_calendar(
        self, actor: int, profile: ScheduleProfile, kind: CalendarKind
    ) -> CalendarSummary:
        identity = uuid4()
        row = CalendarSummary(identity, profile.name, kind, CalendarRole.OWNER, True, 1)
        self.rows[identity] = row
        self.profiles[identity] = profile
        self.writes.append("create")
        return row

    async def update_calendar(
        self, actor: int, identity: UUID, expected_version: int, profile: ScheduleProfile
    ) -> CalendarSummary:
        if not may_edit(self.role):
            raise CalendarAccessDenied()
        if self.rows[identity].version != expected_version:
            raise CalendarConflict()
        row = replace(self.rows[identity], name=profile.name, version=expected_version + 1)
        self.rows[identity] = row
        self.profiles[identity] = profile
        self.writes.append("update")
        return replace(row, role=self.role)


def setup() -> tuple[CalendarUx, Port, Accounts]:
    port, accounts = Port(), Accounts()
    return CalendarUx(port, accounts, now=lambda: NOW), port, accounts  # type: ignore[arg-type]


def handle(view: View) -> UUID:
    raw = view.rows[0][0].callback_data
    assert raw is not None
    return UUID(hex=raw.split(":")[2])


async def existing(ux: CalendarUx, port: Port) -> CalendarSummary:
    preview = await ux.create_preview(1)
    await ux.confirm(1, handle(preview))
    return next(iter(port.rows.values()))


def event(actor: int, raw: str, *, group: bool = False) -> CallbackQuery:
    return CallbackQuery(
        id="calendar-test",
        from_user=User(id=actor, is_bot=False, first_name="U"),
        chat_instance="test",
        data=raw,
        message=Message(
            message_id=1,
            date=NOW,
            chat=Chat(id=-1 if group else actor, type="group" if group else "private"),
        ),
    )


def state(actor: int = 1) -> FSMContext:
    return FSMContext(MemoryStorage(), StorageKey(bot_id=10, chat_id=actor, user_id=actor))


def message(text: str | None, *, actor: int = 1, group: bool = False) -> Message:
    return Message(
        message_id=2,
        date=NOW,
        text=text,
        from_user=User(id=actor, is_bot=False, first_name="U"),
        chat=Chat(id=-1 if group else actor, type="group" if group else "private"),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [LINES, "-"])
async def test_blank_create_requires_confirmation_independent_of_active(text: str) -> None:
    ux, port, accounts = setup()
    before = copy.deepcopy(accounts.items)
    draft, _ = await ux.blank_prompt(1)
    complete, _ = await ux.editor_input(1, draft, "Особистий")
    assert not complete
    complete, preview = await ux.editor_input(1, draft, text)
    assert complete and "Активний розклад" in preview.text
    assert not port.writes
    assert "Особистий" not in repr(ux._pending)
    await ux.confirm(1, draft)
    profile = next(iter(port.profiles.values()))
    assert not profile.active and profile.timezone == "Europe/Kyiv"
    assert profile.profile_id != "active" and not profile.overrides
    assert [rule.weekday for rule in profile.rules] == ([0, 5] if text == LINES else [])
    assert accounts.items == before
    with pytest.raises(CalendarDataUnavailable):
        await ux.confirm(1, draft)
    assert port.writes == ["create"]


@pytest.mark.asyncio
async def test_creation_does_not_require_active_schedule() -> None:
    ux, port, accounts = setup()
    accounts.items[1] = UserAccount(1)
    draft, _ = await ux.blank_prompt(1)
    await ux.editor_input(1, draft, "Новий")
    await ux.editor_input(1, draft, "-")
    await ux.confirm(1, draft)
    assert port.writes == ["create"]


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [CalendarRole.OWNER, CalendarRole.ADMIN, CalendarRole.EDITOR])
@pytest.mark.parametrize("rename", [True, False])
async def test_edit_authorized_role_preserves_overrides_and_active(
    role: CalendarRole, rename: bool
) -> None:
    ux, port, accounts = setup()
    row = await existing(ux, port)
    override = DateOverride(date(2026, 10, 10), day_off=True)
    port.profiles[row.calendar_id] = replace(port.profiles[row.calendar_id], overrides=(override,))
    before = copy.deepcopy(accounts.items)
    old_rules = port.profiles[row.calendar_id].rules
    port.role = role
    draft, _ = await ux.edit_prompt(1, row.calendar_id, row.version, rename=rename)
    complete, preview = await ux.editor_input(1, draft, "Нова назва" if rename else LINES)
    assert complete and "Підтвердити" in preview.text
    assert port.writes == ["create"]
    await ux.confirm(1, draft)
    after = port.profiles[row.calendar_id]
    assert after.overrides == (override,) and accounts.items == before
    assert (after.rules == old_rules) if rename else [r.weekday for r in after.rules] == [0, 5]
    assert port.rows[row.calendar_id].version == 2


@pytest.mark.asyncio
async def test_member_and_foreign_calendar_edit_denied() -> None:
    ux, port, _ = setup()
    row = await existing(ux, port)
    port.role = CalendarRole.MEMBER
    for actor in (1, 2):
        with pytest.raises(CalendarAccessDenied):
            await ux.edit_prompt(actor, row.calendar_id, row.version, rename=True)
    assert "✏️ Назва календаря" not in str(await ux.detail(1, row.calendar_id, row.version))
    assert port.writes == ["create"]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["expiry", "foreign", "cancel", "navigation", "stale", "role"])
async def test_pending_cannot_be_replayed_or_saved_after_scope_changes(case: str) -> None:
    ux, port, _ = setup()
    row = await existing(ux, port)
    draft, _ = await ux.edit_prompt(1, row.calendar_id, row.version, rename=True)
    await ux.editor_input(1, draft, "Нова назва")
    actor = 1
    if case == "expiry":
        ux.now = lambda: NOW + timedelta(minutes=10)
    elif case == "foreign":
        actor = 2
    elif case == "cancel":
        await ux.cancel(1, draft)
    elif case == "navigation":
        ux.abandon_editor(1)
    elif case == "stale":
        port.rows[row.calendar_id] = replace(row, version=2)
    else:
        port.role = CalendarRole.MEMBER
    with pytest.raises((CalendarDataUnavailable, CalendarAccessDenied, CalendarConflict)):
        await ux.confirm(actor, draft)
    assert port.writes == ["create"]


@pytest.mark.asyncio
async def test_complex_week_rejected_but_rename_preserves_exact_rules() -> None:
    ux, port, _ = setup()
    row = await existing(ux, port)
    complex_rules = (
        ScheduleRule(
            0, (Lesson(time(9), time(10), "Урок", enabled=False),), week_parity=Parity.ODD
        ),
    )
    port.profiles[row.calendar_id] = replace(port.profiles[row.calendar_id], rules=complex_rules)
    with pytest.raises(CalendarInputError):
        await ux.edit_prompt(1, row.calendar_id, row.version, rename=False)
    draft, _ = await ux.edit_prompt(1, row.calendar_id, row.version, rename=True)
    await ux.editor_input(1, draft, "Назва")
    await ux.confirm(1, draft)
    assert port.profiles[row.calendar_id].rules == complex_rules


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    [
        "",
        "x" * 3501,
        "bad",
        "Пн 09:00-08:00 Урок",
        "Пн 09:00-10:00 A\nПн 09:30-10:30 B",
        "Пн 09:00-10:00 https://example.invalid/private",
    ],
)
async def test_invalid_week_can_be_corrected_without_extending_expiry(bad: str) -> None:
    ux, port, _ = setup()
    draft, _ = await ux.blank_prompt(1)
    await ux.editor_input(1, draft, "Назва")
    expiry = ux._pending[draft].expires_at
    with pytest.raises(CalendarInputError):
        await ux.editor_input(1, draft, bad)
    assert ux._pending[draft].action == "blank_week"
    await ux.editor_input(1, draft, LINES)
    assert ux._pending[draft].expires_at == expiry and not port.writes


@pytest.mark.asyncio
async def test_creation_limits_and_account_disappearance_fail_closed() -> None:
    ux, port, accounts = setup()
    for _ in range(3):
        await ux.blank_prompt(1)
    with pytest.raises(CalendarLimitExceeded):
        await ux.blank_prompt(1)
    ux.abandon_editor(1)
    draft, _ = await ux.blank_prompt(1)
    await ux.editor_input(1, draft, "Назва")
    await ux.editor_input(1, draft, "-")
    del accounts.items[1]
    with pytest.raises(CalendarDataUnavailable):
        await ux.confirm(1, draft)
    assert not port.writes


@pytest.mark.asyncio
async def test_actual_router_fsm_recovers_invalid_text_and_keeps_only_opaque_handle() -> None:
    ux, port, _ = setup()
    context = state()
    with (
        patch("time_tl.bot.aiogram_adapter._edit", new_callable=AsyncMock),
        patch("time_tl.bot.aiogram_adapter._answer", new_callable=AsyncMock) as answer,
    ):
        router = build_calendar_router(ux, enabled=True)
        assert router is not None
        await router.propagate_event("callback_query", event(1, "cal:blank"), state=context)
        assert set(await context.get_data()) == {"calendar_handle"}
        await router.propagate_event(
            "message", message("Особистий"), state=context, raw_state=await context.get_state()
        )
        await router.propagate_event(
            "message", message("bad"), state=context, raw_state=await context.get_state()
        )
        assert await context.get_state()
        assert "Перевірте формат" in answer.call_args.args[1].text
        await router.propagate_event(
            "message", message(LINES), state=context, raw_state=await context.get_state()
        )
        preview = answer.call_args.args[1]
        assert await context.get_state() is None and not await context.get_data()
        assert not port.writes
        await router.propagate_event(
            "callback_query", event(1, preview.rows[0][0].callback_data), state=context
        )
        assert port.writes == ["create"]


@pytest.mark.asyncio
@pytest.mark.parametrize("group", [True, False])
async def test_router_private_scope_and_foreign_actor_cannot_consume_draft(group: bool) -> None:
    ux, port, _ = setup()
    context = state()
    with (
        patch("time_tl.bot.aiogram_adapter._edit", new_callable=AsyncMock) as edit,
        patch("time_tl.bot.aiogram_adapter._answer", new_callable=AsyncMock),
    ):
        router = build_calendar_router(ux, enabled=True)
        assert router is not None
        if group:
            await router.propagate_event(
                "callback_query", event(1, "cal:blank", group=True), state=context
            )
            assert not ux._pending and "недоступна" in edit.call_args.args[1].text
        else:
            await router.propagate_event("callback_query", event(1, "cal:blank"), state=context)
            await router.propagate_event(
                "message",
                message("Назва", actor=2),
                state=context,
                raw_state=await context.get_state(),
            )
            assert next(iter(ux._pending.values())).action == "blank_name"
        assert not port.writes


@pytest.mark.asyncio
async def test_confirm_concurrency_and_timeout_never_retries() -> None:
    ux, port, _ = setup()
    draft, _ = await ux.blank_prompt(1)
    await ux.editor_input(1, draft, "Назва")
    await ux.editor_input(1, draft, "-")
    entered = asyncio.Event()

    async def stuck(*args: object) -> CalendarSummary:
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError()

    port.create_calendar = AsyncMock(side_effect=stuck)  # type: ignore[method-assign]
    with patch("time_tl.bot.calendar_router._OPERATION_TIMEOUT_SECONDS", 0.02):
        task = asyncio.create_task(ux.confirm(1, draft))
        await entered.wait()
        with pytest.raises(CalendarDataUnavailable):
            await ux.confirm(1, draft)
        with pytest.raises(TimeoutError):
            await task
    assert port.create_calendar.call_count == 1 and draft not in ux._pending


@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", ["cancel", "navigation", "expiry", "new_input"])
async def test_awaited_edit_cannot_resurrect_cancelled_expired_or_replaced_draft(
    interruption: str,
) -> None:
    ux, port, _ = setup()
    row = await existing(ux, port)
    draft, _ = await ux.edit_prompt(1, row.calendar_id, row.version, rename=True)
    entered, release = asyncio.Event(), asyncio.Event()
    original = ux._summary

    async def paused(*args: object) -> CalendarSummary:
        entered.set()
        await release.wait()
        return await original(*args)

    ux._summary = paused  # type: ignore[method-assign, assignment]
    task = asyncio.create_task(ux.editor_input(1, draft, "Пізній текст"))
    await entered.wait()
    if interruption == "cancel":
        await ux.cancel(1, draft)
    elif interruption == "navigation":
        ux.abandon_editor(1)
    elif interruption == "expiry":
        ux.now = lambda: NOW + timedelta(minutes=10)
    else:
        ux._pending[draft] = replace(ux._pending[draft], action="update")
    release.set()
    with pytest.raises((CalendarDataUnavailable, CalendarConflict)):
        await task
    assert port.writes == ["create"]
    assert draft not in ux._pending or ux._pending[draft].profile.name != "Пізній текст"


@pytest.mark.asyncio
@pytest.mark.parametrize("navigation", ["/start", "/menu", "menu:home", "menu:schedule"])
async def test_native_dispatcher_navigation_leaves_editor_and_reaches_normal_handlers(
    navigation: str,
) -> None:
    ux, port, _ = setup()
    router = Router()
    dp = Dispatcher(storage=MemoryStorage())
    bot = Bot("123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ123456789")
    received: list[str] = []
    with (
        patch("time_tl.bot.aiogram_adapter._edit", new_callable=AsyncMock),
        patch("time_tl.bot.aiogram_adapter._answer", new_callable=AsyncMock),
    ):
        build_calendar_router(ux, enabled=True, router=router)

        @router.message(F.text)
        async def ordinary_message(event: Message) -> None:
            received.append(event.text or "")

        @router.callback_query(F.data.startswith("menu:"))
        async def ordinary_callback(event: CallbackQuery) -> None:
            received.append(event.data or "")

        dp.include_router(router)
        context = dp.fsm.get_context(bot=bot, chat_id=1, user_id=1)
        await dp.feed_update(bot, Update(update_id=1, callback_query=event(1, "cal:blank")))
        assert await context.get_state()
        if navigation.startswith("/"):
            update = Update(update_id=2, message=message(navigation))
        else:
            update = Update(update_id=2, callback_query=event(1, navigation))
        await dp.feed_update(bot, update)
        assert received == [navigation]
        assert await context.get_state() is None and not await context.get_data()
        assert not ux._pending
        await dp.feed_update(bot, Update(update_id=3, message=message("Звичайний текст")))
        assert received[-1] == "Звичайний текст" and not port.writes
    await bot.session.close()


@pytest.mark.asyncio
async def test_preview_escapes_titles_and_empty_replacement_warns() -> None:
    ux, port, _ = setup()
    draft, _ = await ux.blank_prompt(1)
    await ux.editor_input(1, draft, "<b>Назва & текст</b>")
    _, preview = await ux.editor_input(1, draft, "Пн 09:00-10:00 <b>A & B</b>")
    assert "<" not in preview.text and ">" not in preview.text and "&" not in preview.text
    await ux.confirm(1, draft)
    row = next(iter(port.rows.values()))
    draft, _ = await ux.edit_prompt(1, row.calendar_id, row.version, rename=False)
    _, preview = await ux.editor_input(1, draft, "-")
    assert "Тиждень буде порожнім" in preview.text
    assert port.profiles[row.calendar_id].rules


@pytest.mark.asyncio
async def test_all_seven_weekdays_and_bounded_callbacks() -> None:
    ux, port, _ = setup()
    draft, _ = await ux.blank_prompt(1)
    await ux.editor_input(1, draft, "Тиждень")
    _, preview = await ux.editor_input(
        1, draft, "\n".join(f"{day} 08:30-09:15 Урок" for day in range(1, 8))
    )
    for row in preview.rows:
        for button in row:
            assert button.callback_data and len(button.callback_data.encode()) <= 64
    await ux.confirm(1, draft)
    assert [r.weekday for r in next(iter(port.profiles.values())).rules] == list(range(7))


@pytest.mark.asyncio
@pytest.mark.parametrize("editing", [True, False])
async def test_native_dispatcher_home_after_preview_invalidates_old_save(editing: bool) -> None:
    ux, port, _ = setup()
    row = await existing(ux, port) if editing else None
    router = Router()
    dp = Dispatcher(storage=MemoryStorage())
    bot = Bot("123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ123456789")
    with (
        patch("time_tl.bot.aiogram_adapter._edit", new_callable=AsyncMock) as edit,
        patch("time_tl.bot.aiogram_adapter._answer", new_callable=AsyncMock) as answer,
    ):
        build_calendar_router(ux, enabled=True, router=router)

        @router.callback_query(F.data == "menu:home")
        async def home(event: CallbackQuery) -> None:
            pass

        dp.include_router(router)
        first = _callback("rename", row.calendar_id, row.version) if row else "cal:blank"
        await dp.feed_update(bot, Update(update_id=1, callback_query=event(1, first)))
        await dp.feed_update(bot, Update(update_id=2, message=message("Новий")))
        if not editing:
            await dp.feed_update(bot, Update(update_id=3, message=message("-")))
        old_save = answer.call_args.args[1].rows[0][0].callback_data
        assert ux._pending and old_save
        context = dp.fsm.get_context(bot=bot, chat_id=1, user_id=1)
        assert await context.get_state() is None
        # Preserve an unrelated flow while discarding only calendar proposals.
        await context.set_state("OtherFlow:input")
        await dp.feed_update(bot, Update(update_id=4, callback_query=event(1, "menu:home")))
        assert not ux._pending and await context.get_state() == "OtherFlow:input"
        await dp.feed_update(bot, Update(update_id=5, callback_query=event(1, old_save)))
        assert "недоступна" in edit.call_args.args[1].text
        assert port.writes == (["create"] if editing else [])
    await bot.session.close()


@pytest.mark.asyncio
async def test_router_invalid_name_and_media_keep_correctable_draft() -> None:
    ux, port, _ = setup()
    context = state()
    with (
        patch("time_tl.bot.aiogram_adapter._edit", new_callable=AsyncMock),
        patch("time_tl.bot.aiogram_adapter._answer", new_callable=AsyncMock) as answer,
    ):
        router = build_calendar_router(ux, enabled=True)
        assert router is not None
        await router.propagate_event("callback_query", event(1, "cal:blank"), state=context)
        await router.propagate_event(
            "message", message("x" * 121), state=context, raw_state=await context.get_state()
        )
        assert "120" in answer.call_args.args[1].text and await context.get_state()
        await router.propagate_event(
            "message", message(None), state=context, raw_state=await context.get_state()
        )
        assert "Надішліть текст" in answer.call_args.args[1].text
        assert await context.get_state() and not port.writes

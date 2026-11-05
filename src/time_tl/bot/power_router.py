"""Private-chat power preferences; no network/provider calls or automatic account creation."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from datetime import date
from typing import Any

from time_tl.domain.power_outages.models import PowerProvider, ScheduleKind

from .models import Button, View
from .power_service import PowerPreviewExpired, PowerUx, unavailable_view


def build_power_router(ux: PowerUx, *, router: Any) -> None:
    from aiogram import BaseMiddleware, F, Router
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.state import State, StatesGroup
    from aiogram.types import CallbackQuery, Message

    from .aiogram_adapter import _answer, _callback_user_id, _edit, _message_user_id

    selected: Router = router
    operations: OrderedDict[int, object] = OrderedDict()

    def begin_operation(actor: int) -> object:
        ticket = object()
        operations[actor] = ticket
        operations.move_to_end(actor)
        while len(operations) > 256:
            operations.popitem(last=False)
        return ticket

    def current_operation(actor: int, ticket: object) -> bool:
        return operations.get(actor) is ticket

    class PowerFlow(StatesGroup):
        custom = State()

    class Navigation(BaseMiddleware):
        async def __call__(
            self,
            handler: Callable[..., Awaitable[Any]],
            event: Any,
            data: dict[str, Any],
        ) -> Any:
            away = isinstance(event, CallbackQuery) and not (
                event.data == "menu:power" or (event.data or "").startswith("pw:")
            )
            command = isinstance(event, Message) and bool(event.text and event.text.startswith("/"))
            if away or command:
                if event.from_user is not None:
                    begin_operation(event.from_user.id)
                    ux.abandon(event.from_user.id)
                context = data.get("state")
                if context is not None and await context.get_state() == PowerFlow.custom.state:
                    await context.clear()
                    data["raw_state"] = None
            return await handler(event, data)

    selected.message.outer_middleware(Navigation())
    selected.callback_query.outer_middleware(Navigation())

    @selected.callback_query(F.data == "menu:power")
    @selected.callback_query(F.data.startswith("pw:"))
    async def power_action(callback: CallbackQuery, state: FSMContext) -> None:
        actor = _callback_user_id(callback)
        ticket = begin_operation(actor)
        try:
            if (
                callback.message is None
                or callback.message.chat.type != "private"
                or callback.message.chat.id != actor
            ):
                raise ValueError("private power action required")
            raw = callback.data or ""
            if len(raw.encode("utf-8")) > 64:
                raise ValueError("power callback too long")
            if await state.get_state() not in (None, PowerFlow.custom.state):
                await _edit(
                    callback,
                    View(
                        "Спочатку завершіть або скасуйте поточну дію.",
                        ((Button("🏠 Головне меню", "menu:home"),),),
                    ),
                )
                return
            await state.clear()
            async with asyncio.timeout(5):
                if raw in {"menu:power", "pw:cancel"}:
                    view = await ux.home(actor)
                elif raw == "pw:setup":
                    ux.abandon(actor)
                    view = ux.providers()
                elif raw == "pw:settings":
                    view = await ux.settings(actor)
                elif raw == "pw:forecast":
                    view = ux.forecast()
                else:
                    pieces = raw.split(":")
                    action = pieces[1]
                    if action == "provider" and len(pieces) == 3:
                        view = await ux.provider(actor, PowerProvider(pieces[2]))
                    elif action == "groups" and len(pieces) == 4:
                        view = ux.groups(actor, pieces[2], int(pieces[3]))
                    elif action == "group" and len(pieces) == 4:
                        view = ux.group(actor, pieces[2], pieces[3])
                    elif action == "notify" and len(pieces) == 4 and pieces[3] in {"0", "1"}:
                        view = ux.notifications(actor, pieces[2], pieces[3] == "1")
                    elif action == "leads" and len(pieces) == 4:
                        view = ux.leads(actor, pieces[2], pieces[3])
                    elif action == "lead" and len(pieces) == 5:
                        view = ux.custom(actor, pieces[2], pieces[3], pieces[4])
                    elif action == "toggle" and len(pieces) == 4:
                        view = ux.toggle(actor, pieces[2], pieces[3])
                    elif action == "save" and len(pieces) == 3:
                        view = await ux.confirm(actor, pieces[2])
                    elif action == "custom" and len(pieces) == 4:
                        ux.leads(actor, pieces[2], pieces[3])  # validate ownership/type/expiry
                        await state.set_state(PowerFlow.custom)
                        await state.update_data(power_handle=pieces[2], power_kind=pieces[3])
                        view = View(
                            "Введіть ціле число хвилин від 0 до 180.",
                            ((Button("Скасувати", "pw:cancel"),),),
                        )
                    elif action == "day" and len(pieces) == 5 and pieces[3] in {"d", "f"}:
                        view = await ux.schedule(
                            actor,
                            date.fromisoformat(pieces[2]),
                            ScheduleKind.CONFIRMED_DAY
                            if pieces[3] == "d"
                            else ScheduleKind.FORECAST_WEEK,
                            int(pieces[4]),
                        )
                    else:
                        raise ValueError("unknown power action")
        except Exception:
            if not current_operation(actor, ticket):
                await callback.answer()
                return
            view = unavailable_view()
        if not current_operation(actor, ticket):
            await callback.answer()
            return
        await _edit(callback, view)

    @selected.message(PowerFlow.custom, F.text, ~F.text.startswith("/"))
    async def custom_lead(message: Message, state: FSMContext) -> None:
        actor = _message_user_id(message)
        ticket = begin_operation(actor)
        try:
            if message.chat.type != "private" or message.chat.id != actor:
                raise ValueError("private power input required")
            data = await state.get_data()
            if not current_operation(actor, ticket):
                return
            view = ux.custom(actor, data["power_handle"], data["power_kind"], message.text or "")
        except PowerPreviewExpired:
            view = unavailable_view()
        except ValueError:
            if not current_operation(actor, ticket):
                return
            await _answer(
                message,
                View(
                    "Введіть ціле число від 0 до 180 або скасуйте дію.",
                    ((Button("Скасувати", "pw:cancel"),),),
                ),
            )
            return
        except Exception:
            view = unavailable_view()
        if not current_operation(actor, ticket):
            return
        await state.clear()
        if not current_operation(actor, ticket):
            return
        await _answer(message, view)

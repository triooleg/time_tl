"""Address wizard owns only its FSM state and private actor callbacks."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any

from .models import Button, View
from .power_address_service import PowerAddressExpired, PowerAddressUx, unavailable_address


def build_power_address_router(ux: PowerAddressUx, *, router: Any) -> None:
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

    class AddressFlow(StatesGroup):
        query = State()

    class Navigation(BaseMiddleware):
        async def __call__(
            self, handler: Callable[..., Awaitable[Any]], event: Any, data: dict[str, Any]
        ) -> Any:
            away = isinstance(event, CallbackQuery) and not (event.data or "").startswith("pa:")
            command = isinstance(event, Message) and bool(event.text and event.text.startswith("/"))
            if away or command:
                if event.from_user is not None:
                    begin_operation(event.from_user.id)
                    ux.abandon(event.from_user.id)
                context = data.get("state")
                if context is not None and await context.get_state() == AddressFlow.query.state:
                    await context.clear()
                    data["raw_state"] = None
            return await handler(event, data)

    selected.message.outer_middleware(Navigation())
    selected.callback_query.outer_middleware(Navigation())

    async def set_query(actor: int, state: FSMContext) -> None:
        try:
            handle = ux.handle(actor)
            if ux.expects_text(actor, handle):
                await state.set_state(AddressFlow.query)
                await state.set_data({"power_address_handle": handle})
                return
        except PowerAddressExpired:
            pass
        if await state.get_state() == AddressFlow.query.state:
            await state.clear()

    @selected.callback_query(F.data.startswith("pa:"))
    async def address_action(callback: CallbackQuery, state: FSMContext) -> None:
        actor = _callback_user_id(callback)
        if (
            callback.message is None
            or callback.message.chat.type != "private"
            or callback.message.chat.id != actor
            or len((callback.data or "").encode()) > 64
        ):
            await callback.answer()
            return
        raw = callback.data or ""
        if raw not in {"pa:start", "pa:cancel"}:
            pieces = raw.split(":")
            try:
                if len(pieces) < 3 or ux.handle(actor) != pieces[2]:
                    await callback.answer()
                    return
            except PowerAddressExpired:
                await callback.answer()
                return
        ticket = begin_operation(actor)
        try:
            current_state = await state.get_state()
            if current_state not in (None, AddressFlow.query.state):
                await _edit(
                    callback,
                    View(
                        "Спочатку завершіть або скасуйте поточну дію.",
                        ((Button("🏠 Головне меню", "menu:home"),),),
                    ),
                )
                return
            async with asyncio.timeout(12):
                if raw == "pa:start":
                    view = await ux.start(actor)
                elif raw == "pa:cancel":
                    ux.abandon(actor)
                    view = View(
                        "Пошук адреси скасовано.", ((Button("⬅️ Електроенергія", "menu:power"),),)
                    )
                else:
                    parts = raw.split(":")
                    if (
                        len(parts) == 4
                        and parts[1] == "pick"
                        and parts[3].isascii()
                        and parts[3].isdecimal()
                    ):
                        view = await ux.pick(actor, parts[2], int(parts[3]))
                    elif (
                        len(parts) == 4
                        and parts[1] == "page"
                        and parts[3].isascii()
                        and parts[3].isdecimal()
                    ):
                        view = ux.page(actor, parts[2], int(parts[3]))
                    elif len(parts) == 3 and parts[1] == "save":
                        view = await ux.confirm(actor, parts[2])
                    elif len(parts) == 3 and parts[1] == "back":
                        view = await ux.back(actor, parts[2])
                    else:
                        raise PowerAddressExpired
            if not current_operation(actor, ticket):
                await callback.answer()
                return
            await set_query(actor, state)
        except Exception:
            if not current_operation(actor, ticket):
                await callback.answer()
                return
            ux.abandon(actor)
            if await state.get_state() == AddressFlow.query.state:
                await state.clear()
            view = unavailable_address()
        if not current_operation(actor, ticket):
            await callback.answer()
            return
        await _edit(callback, view)

    @selected.message(AddressFlow.query, F.text, ~F.text.startswith("/"))
    async def address_text(message: Message, state: FSMContext) -> None:
        actor = _message_user_id(message)
        ticket = begin_operation(actor)
        try:
            if message.chat.type != "private" or message.chat.id != actor:
                raise PowerAddressExpired
            values = await state.get_data()
            handle = values.get("power_address_handle")
            if type(handle) is not str:
                raise PowerAddressExpired
            async with asyncio.timeout(12):
                view = await ux.search(actor, handle, message.text or "")
            if not current_operation(actor, ticket):
                return
            await set_query(actor, state)
        except PowerAddressExpired:
            if not current_operation(actor, ticket):
                return
            ux.abandon(actor)
            await state.clear()
            view = unavailable_address()
        except ValueError:
            if not current_operation(actor, ticket):
                return
            try:
                view = ux.prompt(actor, ux.handle(actor))
            except PowerAddressExpired:
                ux.abandon(actor)
                await state.clear()
                view = unavailable_address()
        except Exception:
            if not current_operation(actor, ticket):
                return
            ux.abandon(actor)
            await state.clear()
            view = unavailable_address()
        if not current_operation(actor, ticket):
            return
        await _answer(message, view)

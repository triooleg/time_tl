from __future__ import annotations

from typing import Any

from .models import NotificationMode, View
from .service import TelegramApplication

MANUAL_REGION_LABEL = "🗺 Обрати вручну"


def build_router(application: TelegramApplication) -> Any:
    """Build aiogram handlers without creating a Bot or reading a token."""
    from aiogram import F, Router
    from aiogram.filters import Command, CommandStart
    from aiogram.types import CallbackQuery, Message

    router = Router(name="time_tl_public_bot")

    @router.message(CommandStart())
    async def start(message: Message) -> None:
        await _answer(message, application.start(_message_user_id(message)))

    @router.message(Command("menu"))
    async def menu(message: Message) -> None:
        await _answer(message, application.menu(_message_user_id(message)))

    @router.message(F.location)
    async def location(message: Message) -> None:
        assert message.location is not None
        view = await application.submit_location(
            _message_user_id(message), message.location.latitude, message.location.longitude
        )
        await _answer(message, view)

    @router.message(F.text == MANUAL_REGION_LABEL)
    async def manual_region(message: Message) -> None:
        await _answer(message, application.list_regions())

    @router.callback_query(F.data.startswith("mode:"))
    async def choose_mode(callback: CallbackQuery) -> None:
        mode = NotificationMode(_callback_data(callback).split(":", 1)[1])
        await _edit(callback, application.choose_mode(_callback_user_id(callback), mode))

    @router.callback_query(F.data.in_({"region:list", "menu:region"}))
    async def list_regions(callback: CallbackQuery) -> None:
        await _edit(callback, application.list_regions())

    @router.callback_query(F.data == "menu:mode")
    async def modes(callback: CallbackQuery) -> None:
        await _edit(callback, application.mode_prompt())

    @router.callback_query(F.data.startswith("region:pick:"))
    async def pick_region(callback: CallbackQuery) -> None:
        region_id = _callback_data(callback).split(":", 2)[2]
        await _edit(
            callback, application.propose_manual_region(_callback_user_id(callback), region_id)
        )

    @router.callback_query(F.data.in_({"region:confirm", "region:reject"}))
    async def confirm_region(callback: CallbackQuery) -> None:
        await _edit(
            callback,
            application.confirm_region(
                _callback_user_id(callback), confirmed=_callback_data(callback) == "region:confirm"
            ),
        )

    @router.callback_query(F.data.startswith("activate:confirm:"))
    async def activate(callback: CallbackQuery) -> None:
        await _edit(
            callback,
            application.confirm_activation(
                _callback_user_id(callback), _callback_data(callback).split(":", 2)[2]
            ),
        )

    @router.callback_query(F.data.startswith("replace:confirm:"))
    async def replace_profile(callback: CallbackQuery) -> None:
        await _edit(
            callback,
            application.confirm_import_replacement(
                _callback_user_id(callback), _callback_data(callback).split(":", 2)[2]
            ),
        )

    @router.callback_query(F.data.startswith("import:confirm:"))
    async def import_profile(callback: CallbackQuery) -> None:
        await _edit(
            callback,
            application.confirm_import(
                _callback_user_id(callback), _callback_data(callback).split(":", 2)[2]
            ),
        )

    @router.callback_query(F.data == "menu:schedules")
    async def schedules(callback: CallbackQuery) -> None:
        await _edit(callback, application.schedules_menu(_callback_user_id(callback)))

    @router.callback_query(F.data == "menu:reminder")
    async def reminder_prompt(callback: CallbackQuery) -> None:
        await _edit(callback, application.reminder_prompt())

    @router.callback_query(F.data.startswith("reminder:"))
    async def set_reminder(callback: CallbackQuery) -> None:
        minutes = int(_callback_data(callback).split(":", 1)[1])
        await _edit(callback, application.set_reminder_lead(_callback_user_id(callback), minutes))

    @router.callback_query(F.data.startswith("activate:request:"))
    async def request_activation(callback: CallbackQuery) -> None:
        profile_id = _callback_data(callback).split(":", 2)[2]
        _, view = application.request_activation(_callback_user_id(callback), profile_id)
        await _edit(callback, view)

    return router


async def _answer(message: Any, view: View) -> None:
    from aiogram.types import (
        InlineKeyboardButton,
        InlineKeyboardMarkup,
        KeyboardButton,
        ReplyKeyboardMarkup,
    )

    parse_mode = getattr(view, "parse_mode", None)
    formatting = {"parse_mode": parse_mode} if parse_mode else {}
    reply_rows = [
        [KeyboardButton(text=b.label, request_location=b.request_location) for b in row]
        for row in view.rows
        if any(b.request_location or b.callback_data is None for b in row)
    ]
    if reply_rows:
        await message.answer(
            view.text,
            reply_markup=ReplyKeyboardMarkup(
                keyboard=reply_rows,
                resize_keyboard=True,
                one_time_keyboard=True,
            ),
            **formatting,
        )
        return

    rows = [
        [
            InlineKeyboardButton(text=b.label, callback_data=b.callback_data)
            for b in row
            if b.callback_data
        ]
        for row in view.rows
        if any(b.callback_data for b in row)
    ]
    markup = InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
    await message.answer(view.text, reply_markup=markup, **formatting)


async def _edit(callback: Any, view: View) -> None:
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    reply_rows = [
        row for row in view.rows if any(b.request_location or b.callback_data is None for b in row)
    ]
    rows = [
        [
            InlineKeyboardButton(text=b.label, callback_data=b.callback_data)
            for b in row
            if b.callback_data
        ]
        for row in view.rows
        if any(b.callback_data for b in row)
    ]
    markup = InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
    if callback.message is not None:
        parse_mode = getattr(view, "parse_mode", None)
        formatting = {"parse_mode": parse_mode} if parse_mode else {}
        await callback.message.edit_text(view.text, reply_markup=markup, **formatting)
        if reply_rows:
            # Reply-keyboard buttons cannot be attached to editMessageText.
            # Send the keyboard as a short follow-up after preserving the
            # explanatory and safety text in the edited message.
            from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

            await callback.message.answer(
                "Оберіть спосіб нижче:",
                reply_markup=ReplyKeyboardMarkup(
                    keyboard=[
                        [
                            KeyboardButton(text=b.label, request_location=b.request_location)
                            for b in row
                        ]
                        for row in reply_rows
                    ],
                    resize_keyboard=True,
                    one_time_keyboard=True,
                ),
            )
    await callback.answer()


def _message_user_id(message: Any) -> int:
    user = message.from_user
    if user is None:
        raise ValueError("Telegram message has no sender")
    return int(user.id)


def _callback_user_id(callback: Any) -> int:
    user = callback.from_user
    if user is None:
        raise ValueError("Telegram callback has no sender")
    return int(user.id)


def _callback_data(callback: Any) -> str:
    data = callback.data
    if data is None:
        raise ValueError("Telegram callback has no data")
    return str(data)

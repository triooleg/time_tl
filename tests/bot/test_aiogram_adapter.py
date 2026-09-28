from __future__ import annotations

import unittest
from unittest.mock import AsyncMock

from aiogram.types import ReplyKeyboardMarkup

from time_tl.bot.aiogram_adapter import MANUAL_REGION_LABEL, _answer, _edit
from time_tl.bot.models import Button, View


class AiogramAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_region_prompt_renders_command_free_location_keyboard(self) -> None:
        message = type("MessageFake", (), {})()
        message.answer = AsyncMock()
        view = View(
            "Оберіть спосіб",
            (
                (Button("📍 Надіслати геолокацію", request_location=True),),
                (Button(MANUAL_REGION_LABEL),),
            ),
        )

        await _answer(message, view)

        message.answer.assert_awaited_once()
        markup = message.answer.await_args.kwargs["reply_markup"]
        self.assertIsInstance(markup, ReplyKeyboardMarkup)
        self.assertTrue(markup.keyboard[0][0].request_location)
        self.assertEqual(MANUAL_REGION_LABEL, markup.keyboard[1][0].text)
        self.assertTrue(markup.one_time_keyboard)

    async def test_callback_transition_sends_reply_keyboard_as_follow_up(self) -> None:
        message = type("EditableMessageFake", (), {})()
        message.edit_text = AsyncMock()
        message.answer = AsyncMock()
        callback = type("CallbackFake", (), {})()
        callback.message = message
        callback.answer = AsyncMock()
        view = View(
            "Оберіть спосіб",
            (
                (Button("📍 Надіслати геолокацію", request_location=True),),
                (Button(MANUAL_REGION_LABEL),),
            ),
        )

        await _edit(callback, view)

        message.edit_text.assert_awaited_once_with(view.text, reply_markup=None)
        message.answer.assert_awaited_once()
        markup = message.answer.await_args.kwargs["reply_markup"]
        self.assertIsInstance(markup, ReplyKeyboardMarkup)
        self.assertTrue(markup.keyboard[0][0].request_location)
        callback.answer.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()

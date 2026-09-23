from __future__ import annotations

import unittest

from pydantic import SecretStr

from time_tl.config import Environment, Settings, TelegramTransport
from time_tl.infrastructure.telegram import (
    AiogramTelegramTransport,
    AllowlistedTelegramTransport,
    TelegramMessage,
    TelegramSendError,
)
from time_tl.runtime.services import build_sender_transport


class RecordingClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def send_message(self, **kwargs: object) -> None:
        self.calls.append(kwargs)


def sender_settings(environment: Environment, allowlist: str = "") -> Settings:
    return Settings(
        TIME_TL_ENV=environment,
        SERVICE_ROLE="sender-worker",
        TELEGRAM_TRANSPORT=TelegramTransport.LIVE,
        TELEGRAM_BOT_TOKEN=SecretStr("telegram-secret"),
        LESSON_LINK_FERNET_KEY=SecretStr("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="),
        TEST_CHAT_ALLOWLIST=allowlist,
        DATABASE_URL=SecretStr("postgresql+asyncpg://user:pass@postgres/time_tl"),
        _env_file=None,
    )


class SenderAllowlistTests(unittest.IsolatedAsyncioTestCase):
    async def test_staging_exact_blocked_target_never_reaches_client(self) -> None:
        client = RecordingClient()
        transport = build_sender_transport(
            sender_settings(Environment.STAGING, "1001"), lambda: client
        )
        self.assertIsInstance(transport, AllowlistedTelegramTransport)

        with self.assertRaises(TelegramSendError) as caught:
            await transport.send(TelegramMessage(chat_id=1002, text="blocked"))

        self.assertEqual("telegram_target_not_allowlisted", caught.exception.code)
        self.assertFalse(caught.exception.retryable)
        self.assertEqual([], client.calls)

    async def test_staging_allowed_target_is_sent(self) -> None:
        client = RecordingClient()
        transport = build_sender_transport(
            sender_settings(Environment.STAGING, "1001"), lambda: client
        )
        await transport.send(TelegramMessage(chat_id=1001, text="allowed"))
        self.assertEqual(1001, client.calls[0]["chat_id"])

    def test_production_transport_is_not_allowlist_wrapped(self) -> None:
        client = RecordingClient()
        transport = build_sender_transport(sender_settings(Environment.PRODUCTION), lambda: client)
        self.assertIsInstance(transport, AiogramTelegramTransport)
        self.assertNotIsInstance(transport, AllowlistedTelegramTransport)


if __name__ == "__main__":
    unittest.main()

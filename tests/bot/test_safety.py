from __future__ import annotations

import unittest
from types import SimpleNamespace

from time_tl.bot.safety import AiogramAllowlistMiddleware, TelegramAllowlistGuard


class TelegramAllowlistTests(unittest.IsolatedAsyncioTestCase):
    async def test_inbound_update_is_dropped_before_handler_for_actor_or_chat_mismatch(
        self,
    ) -> None:
        guard = TelegramAllowlistGuard.from_ids((100,))
        middleware = AiogramAllowlistMiddleware(guard)
        calls: list[int] = []

        async def handler(event: object, data: dict[str, object]) -> str:
            calls.append(1)
            return "handled"

        denied = SimpleNamespace(from_user=SimpleNamespace(id=999), chat=SimpleNamespace(id=999))
        self.assertIsNone(await middleware(handler, denied, {}))
        self.assertEqual([], calls)

        allowed = SimpleNamespace(from_user=SimpleNamespace(id=100), chat=SimpleNamespace(id=100))
        self.assertEqual("handled", await middleware(handler, allowed, {}))
        self.assertEqual([1], calls)

    def test_same_guard_rejects_outbound_target(self) -> None:
        guard = TelegramAllowlistGuard.from_ids((100,))
        guard.require_target(100)
        with self.assertRaises(PermissionError):
            guard.require_target(101)

    def test_empty_allowlist_is_public(self) -> None:
        guard = TelegramAllowlistGuard.from_ids(())
        self.assertTrue(guard.allows(123))


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.fsm.storage.base import StorageKey

from time_tl.bot.safety import (
    AiogramAllowlistMiddleware,
    AiogramIngressSafetyMiddleware,
    BoundedMemoryStorage,
    BoundedTelegramIngressGuard,
    TelegramAllowlistGuard,
)


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

    def test_ingress_guard_is_rate_limited_and_bounded(self) -> None:
        now = [0.0]
        guard = BoundedTelegramIngressGuard(
            per_user_rate=1.0,
            per_user_burst=2,
            global_rate=100.0,
            global_burst=100,
            max_users=2,
            idle_ttl_seconds=10.0,
            monotonic=lambda: now[0],
        )
        self.assertTrue(guard.allows(1))
        self.assertTrue(guard.allows(1))
        self.assertFalse(guard.allows(1))
        self.assertTrue(guard.allows(2))
        self.assertTrue(guard.allows(3))
        self.assertEqual(2, guard.tracked_users)
        now[0] = 11.0
        self.assertTrue(guard.allows(4))
        self.assertEqual(1, guard.tracked_users)

    def test_per_user_rejection_does_not_consume_global_capacity(self) -> None:
        guard = BoundedTelegramIngressGuard(
            per_user_rate=1.0,
            per_user_burst=1,
            global_rate=1.0,
            global_burst=2,
            monotonic=lambda: 0.0,
        )
        self.assertTrue(guard.allows(1))
        self.assertFalse(guard.allows(1))
        self.assertTrue(guard.allows(2))
        self.assertFalse(guard.allows(3))

    async def test_ingress_accepts_private_and_drops_rate_limited_update(self) -> None:
        guard = BoundedTelegramIngressGuard(
            per_user_rate=1.0,
            per_user_burst=1,
            global_rate=10.0,
            global_burst=10,
            monotonic=lambda: 0.0,
        )
        middleware = AiogramIngressSafetyMiddleware(guard)
        handler = AsyncMock(return_value="handled")
        private = SimpleNamespace(
            from_user=SimpleNamespace(id=10),
            chat=SimpleNamespace(id=10, type="private"),
        )
        self.assertEqual("handled", await middleware(handler, private, {}))
        self.assertIsNone(await middleware(handler, private, {}))
        handler.assert_awaited_once()

    async def test_groups_redirect_once_and_channels_are_silent(self) -> None:
        notice_clock = [0.0]
        notice = BoundedTelegramIngressGuard(
            per_user_rate=1 / 60,
            per_user_burst=1,
            global_rate=10,
            global_burst=10,
            monotonic=lambda: notice_clock[0],
        )
        middleware = AiogramIngressSafetyMiddleware(
            BoundedTelegramIngressGuard(), notice_guard=notice
        )
        handler = AsyncMock()
        group = SimpleNamespace(
            from_user=SimpleNamespace(id=10),
            chat=SimpleNamespace(id=-20, type="group"),
            answer=AsyncMock(),
        )
        await middleware(handler, group, {})
        await middleware(handler, group, {})
        group.answer.assert_awaited_once_with(middleware.PRIVATE_REDIRECT)
        channel = SimpleNamespace(
            from_user=None,
            chat=SimpleNamespace(id=-30, type="channel"),
            answer=AsyncMock(),
        )
        await middleware(handler, channel, {})
        channel.answer.assert_not_awaited()
        handler.assert_not_awaited()


class BoundedMemoryStorageTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _key(value: int) -> StorageKey:
        return StorageKey(bot_id=1, chat_id=value, user_id=value)

    async def test_lru_and_ttl_eviction_keep_storage_bounded(self) -> None:
        now = [0.0]
        storage = BoundedMemoryStorage(
            max_records=2,
            idle_ttl_seconds=10.0,
            monotonic=lambda: now[0],
        )
        first, second, third = (self._key(value) for value in (1, 2, 3))
        await storage.set_state(first, "first")
        await storage.set_state(second, "second")
        self.assertEqual("first", await storage.get_state(first))
        await storage.set_state(third, "third")
        self.assertNotIn(second, storage.storage)
        self.assertEqual(2, storage.tracked_records)
        now[0] = 11.0
        await storage.set_data(second, {"step": 1})
        self.assertEqual(1, storage.tracked_records)
        self.assertEqual({"step": 1}, await storage.get_data(second))
        self.assertEqual(
            {"step": 1, "confirmed": True},
            await storage.update_data(second, {"confirmed": True}),
        )
        self.assertTrue(await storage.get_value(second, "confirmed"))
        await storage.close()
        self.assertEqual(0, storage.tracked_records)

    async def test_concurrent_distinct_keys_never_cross_hard_limit(self) -> None:
        storage = BoundedMemoryStorage(max_records=3, idle_ttl_seconds=60.0)
        await asyncio.gather(
            *(storage.set_state(self._key(value), "active") for value in range(20))
        )
        self.assertEqual(3, storage.tracked_records)
        missing = self._key(99)
        self.assertIsNone(await storage.get_state(missing))
        self.assertEqual(3, storage.tracked_records)


if __name__ == "__main__":
    unittest.main()

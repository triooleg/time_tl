from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime

from time_tl.application.sender_worker import ClaimedOutboxMessage, DeliveryFailure, SenderWorker
from time_tl.infrastructure.telegram import AiogramTelegramTransport, TelegramMessage


class Repo:
    def __init__(self, messages):
        self.messages = tuple(messages)
        self.delivered = []
        self.retries = []
        self.dead = []
        self.limit = None
        self.authorized = True

    async def claim_batch_skip_locked(self, now, *, limit):
        self.limit = limit
        return self.messages

    async def deliver_if_authorized(self, message_id, delivered_at, deliver):
        if not self.authorized:
            return False
        await deliver()
        self.delivered.append(message_id)
        return True

    async def mark_retry(self, message_id, *, available_at, error_code):
        self.retries.append((message_id, available_at, error_code))

    async def mark_dead(self, message_id, *, error_code):
        self.dead.append((message_id, error_code))


class Transport:
    def __init__(self, failure=None):
        self.failure = failure
        self.sent = []

    async def send(self, message):
        if self.failure:
            raise self.failure
        self.sent.append(message)


def record(attempts=0, event_type="LESSON_REMINDER"):
    return ClaimedOutboxMessage(
        "m1", event_type, {"telegram_chat_id": 42, "lesson_title": "Алгебра"}, attempts
    )


class SenderWorkerTests(unittest.IsolatedAsyncioTestCase):
    NOW = datetime(2026, 9, 21, 6, tzinfo=UTC)

    async def test_delivers_and_rate_limits_batch_without_body_logging(self):
        repo = Repo(
            (record(), ClaimedOutboxMessage("m2", "LESSON_REMINDER", {"telegram_chat_id": 43}, 0))
        )
        transport = Transport()
        sleeps = []

        async def sleep(value):
            sleeps.append(value)

        delivered = await SenderWorker(
            repository=repo, transport=transport, messages_per_second=10
        ).run_once(self.NOW, sleep=sleep)
        self.assertEqual(delivered, 2)
        self.assertEqual(repo.delivered, ["m1", "m2"])
        self.assertEqual(sleeps, [0.1])

    async def test_retry_after_wins_over_exponential_backoff(self):
        repo = Repo((record(),))
        transport = Transport(
            DeliveryFailure("telegram_429", retryable=True, retry_after_seconds=30)
        )

        async def sleep(_):
            pass

        await SenderWorker(repository=repo, transport=transport).run_once(self.NOW, sleep=sleep)
        self.assertEqual(repo.retries[0][2], "telegram_429")
        self.assertEqual((repo.retries[0][1] - self.NOW).total_seconds(), 30)

    async def test_final_authorization_rejects_after_claim_without_transport_io(self):
        repo = Repo((record(),))
        repo.authorized = False
        transport = Transport()

        async def sleep(_):
            pass

        delivered = await SenderWorker(repository=repo, transport=transport).run_once(
            self.NOW, sleep=sleep
        )

        self.assertEqual(delivered, 0)
        self.assertEqual(transport.sent, [])
        self.assertEqual(repo.delivered, [])

    async def test_permanent_and_exhausted_failures_go_dead(self):
        async def sleep(_):
            pass

        scenarios = (
            (record(event_type="OTHER"), None),
            (record(attempts=5), DeliveryFailure("network", retryable=True)),
        )
        for item, failure in scenarios:
            repo = Repo((item,))
            transport = Transport(failure)
            await SenderWorker(repository=repo, transport=transport, max_attempts=6).run_once(
                self.NOW, sleep=sleep
            )
            self.assertEqual(len(repo.dead), 1)

    async def test_expired_return_message_is_dead_and_never_sent(self):
        expired = ClaimedOutboxMessage(
            "expired",
            "RETURN_TO_ACTIVE_LESSON",
            {
                "telegram_chat_id": 42,
                "lesson_title": "Алгебра",
                "valid_until": "2026-09-21T05:59:59+00:00",
            },
            0,
        )
        repo = Repo((expired,))
        transport = Transport()

        async def sleep(_):
            pass

        delivered = await SenderWorker(repository=repo, transport=transport).run_once(
            self.NOW, sleep=sleep
        )
        self.assertEqual(delivered, 0)
        self.assertEqual(transport.sent, [])
        self.assertEqual(repo.dead, [("expired", "delivery_expired")])

    async def test_return_without_expiry_fails_closed(self):
        repo = Repo((record(event_type="RETURN_TO_ACTIVE_LESSON"),))
        transport = Transport()

        async def sleep(_):
            pass

        await SenderWorker(repository=repo, transport=transport).run_once(self.NOW, sleep=sleep)
        self.assertEqual(transport.sent, [])
        self.assertEqual(repo.dead, [("m1", "missing_delivery_expiry")])

    async def test_hung_transport_times_out_retries_and_releases_repository_scope(self):
        class LockingRepo(Repo):
            def __init__(self, messages):
                super().__init__(messages)
                self.locked = False

            async def deliver_if_authorized(self, message_id, delivered_at, deliver):
                self.locked = True
                try:
                    await deliver()
                finally:
                    self.locked = False
                return True

        class HungTransport:
            def __init__(self):
                self.cancelled = False

            async def send(self, _message):
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.cancelled = True
                    raise

        repo = LockingRepo((record(),))
        transport = HungTransport()

        async def sleep(_):
            pass

        delivered = await SenderWorker(
            repository=repo,
            transport=transport,  # type: ignore[arg-type]
            transport_timeout_seconds=0.01,
        ).run_once(self.NOW, sleep=sleep)

        self.assertEqual(delivered, 0)
        self.assertFalse(repo.locked)
        self.assertTrue(transport.cancelled)
        self.assertEqual(repo.retries[0][2], "telegram_timeout")
        self.assertEqual((repo.retries[0][1] - self.NOW).total_seconds(), 2)

    def test_transport_timeout_cannot_exceed_lock_safety_cap(self):
        with self.assertRaisesRegex(ValueError, "invalid sender limits"):
            SenderWorker(repository=Repo(()), transport=Transport(), transport_timeout_seconds=10.1)

    async def test_aiogram_adapter_is_lazy(self):
        made = []

        class Client:
            def __init__(self):
                self.calls = []

            async def send_message(self, **kwargs):
                self.calls.append(kwargs)

        client = Client()
        adapter = AiogramTelegramTransport(lambda: made.append(True) or client)
        self.assertEqual(made, [])
        await adapter.send(TelegramMessage(1, "hello", "https://example.test/join"))
        self.assertEqual(made, [True])
        self.assertEqual(client.calls[0]["chat_id"], 1)


if __name__ == "__main__":
    unittest.main()

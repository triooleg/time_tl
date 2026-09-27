from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime

from time_tl.application.sender_worker import (
    ClaimedOutboxMessage,
    DeliveryFailure,
    DeliveryRecipient,
    SenderWorker,
)
from time_tl.infrastructure.telegram import (
    AiogramTelegramTransport,
    TelegramMessage,
    TelegramSendError,
)


class Repo:
    def __init__(self, messages):
        self.messages = tuple(messages)
        self.delivered = []
        self.retries = []
        self.released = []
        self.dead = []
        self.limit = None
        self.authorized = True

    async def claim_batch_skip_locked(self, now, *, limit):
        self.limit = limit
        return self.messages

    async def deliver_if_authorized(self, message_id, delivered_at, deliver, *, expected_recipient):
        del expected_recipient
        if not self.authorized:
            return False
        await deliver()
        self.delivered.append(message_id)
        return True

    async def mark_retry(self, message_id, *, available_at, error_code):
        self.retries.append((message_id, available_at, error_code))

    async def release_unattempted(self, message_ids, *, claimed_at, available_at, error_code):
        self.released.append((message_ids, claimed_at, available_at, error_code))

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
    payload = {
        "telegram_chat_id": 42,
        "lesson_title": "Алгебра",
        "starts_at": "2026-09-21T06:30:00+00:00",
        "ends_at": "2026-09-21T07:15:00+00:00",
        "confirmed_region_id": "9",
        "confirmed_region_name": "Дніпропетровська область",
    }
    if event_type == "RETURN_TO_ACTIVE_LESSON":
        payload["valid_until"] = "2026-09-21T07:15:00+00:00"
    return ClaimedOutboxMessage("m1", event_type, payload, attempts)


class SenderWorkerTests(unittest.IsolatedAsyncioTestCase):
    NOW = datetime(2026, 9, 21, 6, tzinfo=UTC)

    async def test_delivers_and_rate_limits_batch_without_body_logging(self):
        repo = Repo(
            (
                record(),
                ClaimedOutboxMessage(
                    "m2",
                    "LESSON_REMINDER",
                    {
                        "telegram_chat_id": 43,
                        "starts_at": "2026-09-21T06:30:00+00:00",
                    },
                    0,
                ),
            )
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

    async def test_telegram_429_stops_batch_and_releases_unattempted_tail(self):
        messages = (
            record(),
            ClaimedOutboxMessage("m2", "LESSON_REMINDER", record().payload, 1),
            ClaimedOutboxMessage("m3", "LESSON_REMINDER", record().payload, 2),
        )
        repo = Repo(messages)

        class RateLimitedTransport:
            def __init__(self):
                self.attempted = []

            async def send(self, message):
                self.attempted.append(message.chat_id)
                raise TelegramSendError("telegram_429", retryable=True, retry_after_seconds=30)

        transport = RateLimitedTransport()
        sleeps = []

        async def sleep(value):
            sleeps.append(value)

        delivered = await SenderWorker(
            repository=repo,
            transport=transport,  # type: ignore[arg-type]
        ).run_once(self.NOW, sleep=sleep)

        retry_at = self.NOW.replace(second=30)
        self.assertEqual(delivered, 0)
        self.assertEqual(transport.attempted, [42])
        self.assertEqual(sleeps, [])
        self.assertEqual(repo.retries, [("m1", retry_at, "telegram_429")])
        self.assertEqual(
            repo.released,
            [
                (
                    ("m2", "m3"),
                    self.NOW,
                    retry_at,
                    "telegram_429_batch_deferred",
                )
            ],
        )
        self.assertEqual(repo.delivered, [])

    async def test_telegram_429_remains_retryable_at_normal_attempt_limit(self):
        repo = Repo((record(attempts=5),))
        transport = Transport(
            DeliveryFailure("telegram_429", retryable=True, retry_after_seconds=30)
        )

        async def sleep(_):
            pass

        await SenderWorker(repository=repo, transport=transport, max_attempts=6).run_once(
            self.NOW, sleep=sleep
        )
        self.assertEqual(repo.dead, [])
        self.assertEqual(repo.retries[0][2], "telegram_429")

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
        item = record(event_type="RETURN_TO_ACTIVE_LESSON")
        del item.payload["valid_until"]
        repo = Repo((item,))
        transport = Transport()

        async def sleep(_):
            pass

        await SenderWorker(repository=repo, transport=transport).run_once(self.NOW, sleep=sleep)
        self.assertEqual(transport.sent, [])
        self.assertEqual(repo.dead, [("m1", "missing_delivery_expiry")])

    async def test_alert_messages_name_exact_confirmed_region(self):
        async def sleep(_):
            pass

        cases = (
            (
                "ALERT_STARTED",
                "🚨 ПОВІТРЯНА ТРИВОГА\n\n"
                "📍 Територія: Кам'янська територіальна громада\n\n"
                "Прямуйте до укриття та виконуйте офіційні вказівки. "
                "Бот не визначає, чи зупинено ваш урок.",
            ),
            (
                "ALL_CLEAR_CONFIRMED",
                "✅ ВІДБІЙ ПОВІТРЯНОЇ ТРИВОГИ\n\n"
                "📍 Територія: Кам'янська територіальна громада\n\n"
                "Відбій підтверджено офіційним джерелом. "
                "Продовжуйте виконувати актуальні офіційні вказівки.",
            ),
        )
        for event_type, expected in cases:
            item = record(event_type=event_type)
            item.payload["confirmed_region_name"] = "Кам'янська територіальна громада"
            repo = Repo((item,))
            transport = Transport()
            delivered = await SenderWorker(repository=repo, transport=transport).run_once(
                self.NOW, sleep=sleep
            )
            self.assertEqual(delivered, 1)
            self.assertEqual(transport.sent[0].text, expected)

    async def test_return_message_names_region_and_current_lesson(self):
        async def sleep(_):
            pass

        item = record(event_type="RETURN_TO_ACTIVE_LESSON")
        item.payload["confirmed_region_name"] = "Кам'янська територіальна громада"
        repo = Repo((item,))
        transport = Transport()
        delivered = await SenderWorker(repository=repo, transport=transport).run_once(
            self.NOW, sleep=sleep
        )
        self.assertEqual(delivered, 1)
        self.assertEqual(
            transport.sent[0].text,
            "📚 МОЖНА ПОВЕРНУТИСЯ ДО УРОКУ\n\n"
            "📍 Територія: Кам'янська територіальна громада\n"
            "📖 Поточний урок: Алгебра\n"
            "🕐 Триває до: 10:15\n\n"
            "Якщо це не суперечить поточним офіційним вказівкам, "
            "підключайтеся до уроку.",
        )

    async def test_alert_without_confirmed_region_fails_closed(self):
        async def sleep(_):
            pass

        item = record(event_type="ALERT_STARTED")
        del item.payload["confirmed_region_name"]
        repo = Repo((item,))
        transport = Transport()
        await SenderWorker(repository=repo, transport=transport).run_once(self.NOW, sleep=sleep)
        self.assertEqual(transport.sent, [])
        self.assertEqual(repo.dead, [("m1", "missing_confirmed_region")])

    async def test_hung_transport_times_out_retries_and_releases_repository_scope(self):
        class LockingRepo(Repo):
            def __init__(self, messages):
                super().__init__(messages)
                self.locked = False

            async def deliver_if_authorized(
                self, message_id, delivered_at, deliver, *, expected_recipient
            ):
                self.expected_recipient = expected_recipient
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
        self.assertEqual(
            repo.expected_recipient,
            DeliveryRecipient(42, "9", "Дніпропетровська область"),
        )
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

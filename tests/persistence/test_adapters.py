from __future__ import annotations

import unittest
from datetime import time
from uuid import uuid4

from time_tl.domain.schedule import Lesson, ScheduleProfile, ScheduleRule
from time_tl.infrastructure.db.accounts import AsyncPostgresAccountRepository
from time_tl.infrastructure.db.admin import AsyncPostgresAdminRepository
from time_tl.infrastructure.db.engine import create_engine
from time_tl.infrastructure.db.lease import DedicatedAdvisoryLease
from time_tl.infrastructure.db.models import RuleLesson
from time_tl.infrastructure.db.workers import (
    PostgresJoinLinkResolver,
    PostgresScheduleWorkerRepository,
)


class ReverseCodec:
    def encrypt(self, value: str) -> bytes:
        return value[::-1].encode()

    def decrypt(self, value: bytes) -> str:
        return value.decode()[::-1]


class AddOnlySession:
    def __init__(self):
        self.rows = []

    def add(self, row):
        self.rows.append(row)


class FakeConnection:
    def __init__(self):
        self.closed = False
        self.calls = []

    def is_closed(self):
        return self.closed

    async def fetchval(self, sql, *args):
        self.calls.append((sql, args))
        return True


class ScalarValueResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class LinkSession:
    def __init__(self, value):
        self.value = value
        self.statements = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def execute(self, statement):
        self.statements.append(statement)
        return ScalarValueResult(self.value)


class LinkFactory:
    def __init__(self, session):
        self.session = session

    def __call__(self):
        return self.session


class AdapterContractTests(unittest.IsolatedAsyncioTestCase):
    def test_account_profile_encrypts_lesson_link(self):
        codec = ReverseCodec()
        repo = AsyncPostgresAccountRepository(None, codec)  # type: ignore[arg-type]
        profile = ScheduleProfile(
            "p",
            "School",
            active=True,
            rules=(
                ScheduleRule(
                    0, (Lesson(time(9), time(10), "Math", join_url="https://example.test/join"),)
                ),
            ),
        )
        session = AddOnlySession()
        repo._add_profile(session, uuid4(), profile)  # type: ignore[arg-type]
        lesson = next(row for row in session.rows if isinstance(row, RuleLesson))
        self.assertNotIn(b"https://", lesson.join_url_ciphertext)
        self.assertEqual(codec.decrypt(lesson.join_url_ciphertext), "https://example.test/join")

    def test_account_profile_fails_closed_without_link_codec(self):
        repo = AsyncPostgresAccountRepository(None)  # type: ignore[arg-type]
        profile = ScheduleProfile(
            "p",
            "School",
            active=True,
            rules=(
                ScheduleRule(
                    0, (Lesson(time(9), time(10), "Math", join_url="https://example.test/join"),)
                ),
            ),
        )
        with self.assertRaisesRegex(RuntimeError, "encryption adapter"):
            repo._add_profile(AddOnlySession(), uuid4(), profile)  # type: ignore[arg-type]

    async def test_dedicated_lease_acquire_renew_release(self):
        connection = FakeConnection()
        lease = DedicatedAdvisoryLease(connection, 123)
        self.assertTrue(await lease.acquire())
        self.assertTrue(await lease.renew())
        await lease.release()
        self.assertIn("pg_try_advisory_lock", connection.calls[0][0])
        self.assertIn("pg_advisory_unlock", connection.calls[-1][0])

    def test_admin_ip_is_keyed_and_not_plaintext(self):
        repo = AsyncPostgresAdminRepository(None, ip_hash_key=b"x" * 32)  # type: ignore[arg-type]
        value = repo._ip_hash("192.0.2.5")
        self.assertEqual(len(value), 64)
        self.assertNotIn("192.0.2.5", value)

    def test_schedule_outbox_rejects_plaintext_links(self):
        with self.assertRaisesRegex(ValueError, "plaintext links"):
            PostgresScheduleWorkerRepository._validate_payload(
                {"telegram_chat_id": 1, "join_url": "https://secret.test"}
            )
        PostgresScheduleWorkerRepository._validate_payload(
            {
                "telegram_chat_id": 1,
                "join_link_ref": "lesson:rule:123e4567-e89b-12d3-a456-426614174000",
            }
        )

    async def test_join_link_resolver_rejects_malformed_reference_without_db(self):
        resolver = PostgresJoinLinkResolver(None, ReverseCodec())  # type: ignore[arg-type]
        self.assertIsNone(await resolver.resolve("https://secret.test"))
        self.assertIsNone(await resolver.resolve("profile:abc:lesson:1"))
        self.assertIsNone(await resolver.resolve("lesson:rule:123e4567-e89b-12d3-a456-not-a-uuid"))

    async def test_join_link_resolver_fetches_only_exact_immutable_lesson_row(self):
        session = LinkSession(b"nioj/tset.elpmaxe//:sptth")
        resolver = PostgresJoinLinkResolver(LinkFactory(session), ReverseCodec())  # type: ignore[arg-type]

        value = await resolver.resolve("lesson:rule:123e4567-e89b-12d3-a456-426614174000")

        self.assertEqual(value, "https://example.test/join")
        sql = str(session.statements[0])
        self.assertIn("rule_lessons.id =", sql)
        self.assertNotIn("position", sql)

    def test_engine_factory_rejects_non_postgres(self):
        with self.assertRaisesRegex(ValueError, "postgresql"):
            create_engine("sqlite+aiosqlite:///local.db")


if __name__ == "__main__":
    unittest.main()

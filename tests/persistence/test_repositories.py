from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from sqlalchemy.dialects import postgresql

from time_tl.infrastructure.db.import_sessions import PostgresImportSessionRepository, digest_secret
from time_tl.infrastructure.db.models import PendingImportSession
from time_tl.infrastructure.db.outbox import pending_outbox_query


class ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class ImportRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_consume_uses_row_lock_and_marks_consumed_after_operation(self):
        now = datetime.now(UTC)
        row = PendingImportSession(
            token_hash=digest_secret("secret"),
            user_id=uuid4(),
            fingerprint_hash=digest_secret("fingerprint"),
            expires_at=now + timedelta(minutes=5),
        )
        session = SimpleNamespace(
            execute=AsyncMock(return_value=ScalarResult(row)), flush=AsyncMock()
        )
        repo = PostgresImportSessionRepository(session)
        called = []

        async def operation():
            called.append(True)
            return "ok"

        self.assertEqual(
            await repo.consume_atomically(
                token="secret", fingerprint="fingerprint", now=now, operation=operation
            ),
            "ok",
        )
        self.assertEqual(called, [True])
        self.assertEqual(row.consumed_at, now)
        self.assertIn(
            "FOR UPDATE",
            str(session.execute.await_args.args[0].compile(dialect=postgresql.dialect())),
        )
        session.flush.assert_awaited_once()

    async def test_failed_operation_does_not_mark_consumed(self):
        now = datetime.now(UTC)
        row = PendingImportSession(
            token_hash=digest_secret("s"), user_id=uuid4(), fingerprint_hash=digest_secret("f")
        )
        session = SimpleNamespace(
            execute=AsyncMock(return_value=ScalarResult(row)), flush=AsyncMock()
        )
        repo = PostgresImportSessionRepository(session)

        async def operation():
            raise RuntimeError("rollback")

        with self.assertRaises(RuntimeError):
            await repo.consume_atomically(token="s", fingerprint="f", now=now, operation=operation)
        self.assertIsNone(row.consumed_at)
        session.flush.assert_not_awaited()

    def test_outbox_claim_uses_skip_locked(self):
        self.assertIn(
            "FOR UPDATE SKIP LOCKED",
            str(pending_outbox_query(datetime.now(UTC)).compile(dialect=postgresql.dialect())),
        )


if __name__ == "__main__":
    unittest.main()

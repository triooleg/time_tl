from __future__ import annotations

import unittest
from datetime import UTC, datetime, time, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from cryptography.fernet import Fernet
from sqlalchemy.dialects import postgresql

from time_tl.domain.schedule import Lesson, ScheduleProfile, ScheduleRule
from time_tl.domain.schedule.profiles import ConfirmationError
from time_tl.domain.schedule.snapshots import export_snapshot
from time_tl.infrastructure.db import import_sessions as import_session_module
from time_tl.infrastructure.db.codec import FernetLessonLinkCodec
from time_tl.infrastructure.db.import_sessions import (
    AsyncPostgresImportSessionAdapter,
    PostgresImportSessionRepository,
    digest_secret,
    sanitize_proposal_payload,
)
from time_tl.infrastructure.db.models import PendingImportSession


class ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class ScalarRows:
    def __init__(self, values):
        self.values = values

    def scalars(self):
        return self

    def all(self):
        return self.values


class OwnerAwareSession:
    def __init__(self, row):
        self.row = row
        self.flush = AsyncMock()
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        params = statement.compile(dialect=postgresql.dialect()).params.values()
        owner_ids = [value for value in params if isinstance(value, type(self.row.user_id))]
        allowed = not owner_ids or self.row.user_id in owner_ids
        return ScalarResult(self.row if allowed else None)


class AsyncContext:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *_args):
        return False


class ReplacementSession:
    def __init__(self, user, row, profile):
        self.user = user
        self.row = row
        self.profile = profile
        self.flush = AsyncMock()

    def begin(self):
        return AsyncContext(None)

    async def execute(self, statement):
        sql = str(statement.compile(dialect=postgresql.dialect()))
        if "FROM users" in sql:
            return ScalarResult(self.user)
        params = statement.compile(dialect=postgresql.dialect()).params.values()
        allowed = self.row.user_id in params
        return ScalarResult(self.row if allowed else None)

    async def get(self, _model, key):
        return self.profile if key == self.profile.id else None


class CleanupSession:
    def __init__(self, rows):
        self.rows = rows
        self.statements = []
        self.deleted = []
        self.flush = AsyncMock()

    async def execute(self, statement):
        self.statements.append(statement)
        return ScalarRows(self.rows)

    async def delete(self, row):
        self.deleted.append(row)


def proposal_payload(*, with_link: bool = True):
    lessons = (
        Lesson(
            time(9),
            time(9, 45),
            "Math",
            join_url="https://school.example/SENSITIVE-MARKER",
        ),
    )
    return export_snapshot(
        ScheduleProfile(
            "local-only",
            "School",
            rules=(ScheduleRule(0, lessons),) if with_link else (),
            active=True,
        ),
        source_display_label="Class 8A",
        created_at=datetime(2026, 9, 21, tzinfo=UTC),
        include_join_links=with_link,
        confirm_include_join_links=with_link,
    )


class DurableImportRepositoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.codec = FernetLessonLinkCodec(Fernet.generate_key())

    def encrypted(self, payload=None):
        return import_session_module._encrypt_proposal_payload(
            payload or proposal_payload(), self.codec
        )

    async def test_payload_can_be_loaded_after_repository_restart(self):
        now = datetime(2026, 9, 21, 8, tzinfo=UTC)
        owner = uuid4()
        payload = sanitize_proposal_payload(proposal_payload())
        row = PendingImportSession(
            token_hash=digest_secret("token"),
            user_id=owner,
            fingerprint_hash=digest_secret("fingerprint"),
            proposal_payload=self.encrypted(payload),
            expires_at=now + timedelta(minutes=10),
        )

        first = PostgresImportSessionRepository(OwnerAwareSession(row), self.codec)
        before_restart = await first.load(token="token", now=now, expected_user_id=owner)
        second = PostgresImportSessionRepository(OwnerAwareSession(row), self.codec)
        after_restart = await second.load(token="token", now=now, expected_user_id=owner)

        self.assertEqual(before_restart.payload, payload)
        self.assertEqual(after_restart.payload, payload)
        self.assertNotIn("SENSITIVE-MARKER", str(row.proposal_payload))
        self.assertNotIn("https://", str(row.proposal_payload))
        self.assertNotIn("SENSITIVE-MARKER", repr(before_restart))

    async def test_consumed_proposal_rejects_replay_after_restart(self):
        now = datetime(2026, 9, 21, 8, tzinfo=UTC)
        owner = uuid4()
        row = PendingImportSession(
            token_hash=digest_secret("token"),
            user_id=owner,
            fingerprint_hash=digest_secret("fingerprint"),
            proposal_payload=self.encrypted(),
            expires_at=now + timedelta(minutes=10),
        )
        session = OwnerAwareSession(row)
        repo = PostgresImportSessionRepository(session, self.codec)

        async def operation():
            return "imported"

        result = await repo.consume_atomically(
            token="token",
            fingerprint="fingerprint",
            now=now,
            operation=operation,
            expected_user_id=owner,
        )
        self.assertEqual(result, "imported")
        self.assertEqual(row.proposal_payload.get("state"), "scrubbed")
        self.assertNotIn("SENSITIVE-MARKER", str(row.proposal_payload))
        with self.assertRaisesRegex(ConfirmationError, "already consumed"):
            await PostgresImportSessionRepository(OwnerAwareSession(row), self.codec).load(
                token="token", now=now, expected_user_id=owner
            )

    async def test_cross_user_token_is_indistinguishable_from_missing(self):
        now = datetime(2026, 9, 21, 8, tzinfo=UTC)
        owner = uuid4()
        row = PendingImportSession(
            token_hash=digest_secret("token"),
            user_id=owner,
            fingerprint_hash=digest_secret("fingerprint"),
            proposal_payload=self.encrypted(),
        )
        with self.assertRaisesRegex(ConfirmationError, "proposal is missing"):
            await PostgresImportSessionRepository(OwnerAwareSession(row), self.codec).load(
                token="token", now=now, expected_user_id=uuid4()
            )

    async def test_replacement_token_consumes_atomically_after_restart(self):
        now = datetime(2026, 9, 21, 8, tzinfo=UTC)
        user = SimpleNamespace(id=uuid4(), telegram_user_id=42)
        profile = SimpleNamespace(id=uuid4(), user_id=user.id, public_id="old-school")
        row = PendingImportSession(
            token_hash=digest_secret("original-not-available-after-restart"),
            user_id=user.id,
            fingerprint_hash=digest_secret("fingerprint"),
            proposal_payload=self.encrypted(),
            replacement_token_hash=digest_secret("replacement-secret"),
            replace_profile_id=profile.id,
            expires_at=now + timedelta(minutes=10),
        )
        session = ReplacementSession(user, row, profile)
        adapter = AsyncPostgresImportSessionAdapter(
            lambda: AsyncContext(session), telegram_user_id=42, payload_codec=self.codec
        )
        callback_sessions = []

        async def operation(callback_session, durable):
            callback_sessions.append(callback_session)
            self.assertEqual(durable.replace_profile_id, "old-school")
            self.assertEqual(durable.payload["schema_version"], "1.0")
            return "replaced"

        self.assertEqual(
            await adapter.consume_by_replacement_token("replacement-secret", now, operation),
            "replaced",
        )
        self.assertEqual(callback_sessions, [session])
        self.assertEqual(row.consumed_at, now)
        self.assertEqual(row.proposal_payload.get("state"), "scrubbed")
        with self.assertRaisesRegex(ConfirmationError, "already consumed"):
            await AsyncPostgresImportSessionAdapter(
                lambda: AsyncContext(session), telegram_user_id=42, payload_codec=self.codec
            ).consume_by_replacement_token("replacement-secret", now, operation)

    async def test_replacement_token_is_user_bound(self):
        now = datetime(2026, 9, 21, 8, tzinfo=UTC)
        actual_owner = uuid4()
        other_user = SimpleNamespace(id=uuid4(), telegram_user_id=99)
        profile = SimpleNamespace(id=uuid4(), user_id=actual_owner, public_id="old")
        row = PendingImportSession(
            token_hash=digest_secret("original"),
            user_id=actual_owner,
            fingerprint_hash=digest_secret("fingerprint"),
            proposal_payload=self.encrypted(),
            replacement_token_hash=digest_secret("replacement-secret"),
            replace_profile_id=profile.id,
        )
        session = ReplacementSession(other_user, row, profile)
        adapter = AsyncPostgresImportSessionAdapter(
            lambda: AsyncContext(session), telegram_user_id=99, payload_codec=self.codec
        )

        async def operation(_session, _durable):
            self.fail("cross-user callback must not run")

        with self.assertRaisesRegex(ConfirmationError, "replacement proposal is missing"):
            await adapter.consume_by_replacement_token("replacement-secret", now, operation)

    def test_payload_boundary_rejects_prohibited_and_oversized_data(self):
        prohibited = proposal_payload()
        prohibited["metadata"]["telegram_token"] = "must-not-persist"
        with self.assertRaisesRegex(ConfirmationError, "prohibited"):
            sanitize_proposal_payload(prohibited)

        oversized = proposal_payload()
        oversized["profile"]["name"] = "x" * 256_001
        with self.assertRaisesRegex(ConfirmationError, "oversized"):
            sanitize_proposal_payload(oversized)

    async def test_create_stores_hashes_and_detached_sanitized_payload(self):
        added = []
        session = SimpleNamespace(add=added.append)
        repo = PostgresImportSessionRepository(session, self.codec)
        owner = uuid4()
        payload = proposal_payload()
        before = datetime.now(UTC)
        await repo.create(
            token="secret-token",
            user_id=owner,
            fingerprint="fingerprint",
            expires_at=None,
            proposal_payload=payload,
        )
        stored = added[0]
        self.assertEqual(stored.token_hash, digest_secret("secret-token"))
        self.assertEqual(stored.fingerprint_hash, digest_secret("fingerprint"))
        self.assertNotIn("secret-token", str(stored.proposal_payload))
        self.assertNotIn("SENSITIVE-MARKER", str(stored.proposal_payload))
        self.assertNotIn("https://", str(stored.proposal_payload))
        self.assertIsNot(stored.proposal_payload, payload)
        self.assertGreaterEqual(stored.expires_at, before + timedelta(hours=24))
        self.assertLessEqual(stored.expires_at, datetime.now(UTC) + timedelta(hours=24))

    async def test_tampered_ciphertext_fails_closed_without_sensitive_error(self):
        now = datetime(2026, 9, 21, 8, tzinfo=UTC)
        owner = uuid4()
        envelope = self.encrypted()
        ciphertext = envelope["ciphertext"]
        envelope["ciphertext"] = ("A" if ciphertext[0] != "A" else "B") + ciphertext[1:]
        row = PendingImportSession(
            token_hash=digest_secret("token"),
            user_id=owner,
            fingerprint_hash=digest_secret("fingerprint"),
            proposal_payload=envelope,
            expires_at=now + timedelta(minutes=10),
        )

        with self.assertRaises(ConfirmationError) as raised:
            await PostgresImportSessionRepository(
                OwnerAwareSession(row), self.codec
            ).load(token="token", now=now, expected_user_id=owner)

        self.assertEqual(str(raised.exception), "import proposal could not be verified")
        self.assertNotIn("SENSITIVE-MARKER", str(raised.exception))
        self.assertNotIn("ciphertext", str(raised.exception))

    async def test_revoke_scrubs_encrypted_payload_and_preserves_replay_state(self):
        now = datetime(2026, 9, 21, 8, tzinfo=UTC)
        owner = uuid4()
        row = PendingImportSession(
            token_hash=digest_secret("token"),
            user_id=owner,
            fingerprint_hash=digest_secret("fingerprint"),
            proposal_payload=self.encrypted(),
            expires_at=now + timedelta(minutes=10),
        )
        repo = PostgresImportSessionRepository(OwnerAwareSession(row), self.codec)

        await repo.revoke(token="token", now=now, expected_user_id=owner)

        self.assertEqual(row.proposal_payload.get("state"), "scrubbed")
        self.assertNotIn("SENSITIVE-MARKER", str(row.proposal_payload))
        with self.assertRaisesRegex(ConfirmationError, "revoked"):
            await repo.load(token="token", now=now, expected_user_id=owner)

    async def test_proposal_without_links_is_encrypted_and_restored(self):
        now = datetime.now(UTC)
        owner = uuid4()
        payload = proposal_payload(with_link=False)
        added = []
        await PostgresImportSessionRepository(
            SimpleNamespace(add=added.append), self.codec
        ).create(
            token="token",
            user_id=owner,
            fingerprint="fingerprint",
            expires_at=now + timedelta(minutes=10),
            proposal_payload=payload,
        )
        row = added[0]

        restored = await PostgresImportSessionRepository(
            OwnerAwareSession(row), self.codec
        ).load(token="token", now=now, expected_user_id=owner)

        self.assertEqual(restored.payload, sanitize_proposal_payload(payload))
        self.assertNotIn("join_url", str(row.proposal_payload))

    async def test_expired_cleanup_is_bounded_and_deletes_selected_rows(self):
        now = datetime(2026, 9, 21, 8, tzinfo=UTC)
        rows = [SimpleNamespace(id=uuid4()), SimpleNamespace(id=uuid4())]
        session = CleanupSession(rows)
        repo = PostgresImportSessionRepository(session, self.codec)

        self.assertEqual(await repo.purge_expired(now, limit=2), 2)

        self.assertEqual(session.deleted, rows)
        session.flush.assert_awaited_once()
        sql = str(session.statements[0].compile(dialect=postgresql.dialect()))
        self.assertIn("LIMIT", sql)
        self.assertIn("FOR UPDATE SKIP LOCKED", sql)
        with self.assertRaisesRegex(ValueError, "between 1 and 100"):
            await repo.purge_expired(now, limit=101)


if __name__ == "__main__":
    unittest.main()

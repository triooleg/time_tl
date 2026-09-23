from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from time_tl.admin.repository import LocalAdminRepository
from time_tl.admin.security import (
    LOCKOUT_AFTER,
    SESSION_ABSOLUTE_TTL,
    PasswordPolicyError,
    hash_password,
    utcnow,
    validate_new_password,
    verify_password,
)


class SecurityTests(unittest.TestCase):
    def test_argon2id_hash_and_verify(self) -> None:
        encoded = hash_password("correct horse battery staple")
        self.assertIn("$argon2id$", encoded)
        self.assertTrue(verify_password(encoded, "correct horse battery staple"))
        self.assertFalse(verify_password(encoded, "wrong password"))

    def test_password_policy_exact_messages(self) -> None:
        with self.assertRaisesRegex(PasswordPolicyError, "не менее 14"):
            validate_new_password("short", "short")
        with self.assertRaisesRegex(PasswordPolicyError, "не совпадают"):
            validate_new_password("a-long-enough-password", "different-password")
        with self.assertRaisesRegex(PasswordPolicyError, "слишком распространён"):
            validate_new_password("12345678901234", "12345678901234")

    def test_lockout_session_and_invalidation(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            repo = LocalAdminRepository(folder)
            now = utcnow()
            repo.create_bootstrap(
                hash_password("temporary-long-password"),
                now + timedelta(hours=1),
                str(Path(folder) / "secret"),
            )
            for _ in range(LOCKOUT_AFTER):
                repo.record_failure("admin", "127.0.0.1", now)
            self.assertIsNotNone(repo.lockout_remaining("admin", "127.0.0.1", now))
            self.assertIsNone(repo.lockout_remaining("admin", "127.0.0.2", now))
            repo.reset_failures("admin", "127.0.0.1")
            session = repo.create_session("admin", True, now)
            self.assertEqual(session.absolute_expires_at, now + SESSION_ABSOLUTE_TTL)
            repo.change_password("admin", hash_password("new permanent passphrase"))
            self.assertIsNone(repo.get_session(session.token, now))


if __name__ == "__main__":
    unittest.main()

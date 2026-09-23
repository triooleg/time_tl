from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from time_tl.admin.async_bootstrap import bootstrap_async_admin
from time_tl.admin.security import verify_password


class FakeBootstrapRepository:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.password_hash = ""
        self.expires_at: datetime | None = None
        self.username = ""

    async def create_bootstrap(
        self, password_hash: str, expires_at: datetime, username: str = "admin"
    ) -> None:
        if self.fail:
            raise RuntimeError("database unavailable")
        self.password_hash = password_hash
        self.expires_at = expires_at
        self.username = username


class AsyncBootstrapTests(unittest.IsolatedAsyncioTestCase):
    async def test_writes_protected_file_and_persists_only_hash(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            repo = FakeBootstrapRepository()
            path = Path(folder) / "owner-password.txt"
            returned = await bootstrap_async_admin(repo, path)
            password = path.read_text(encoding="utf-8")
            self.assertEqual(returned, path.resolve())
            self.assertEqual(repo.username, "admin")
            self.assertNotEqual(repo.password_hash, password)
            self.assertTrue(repo.password_hash.startswith("$argon2id$"))
            self.assertTrue(verify_password(repo.password_hash, password))
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    async def test_removes_plaintext_file_when_repository_fails(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "owner-password.txt"
            with self.assertRaisesRegex(RuntimeError, "database unavailable"):
                await bootstrap_async_admin(FakeBootstrapRepository(fail=True), path)
            self.assertFalse(path.exists())

    async def test_refuses_to_overwrite_existing_output(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "owner-password.txt"
            path.write_text("existing", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                await bootstrap_async_admin(FakeBootstrapRepository(), path)
            self.assertEqual(path.read_text(encoding="utf-8"), "existing")


if __name__ == "__main__":
    unittest.main()

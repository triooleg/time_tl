from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import asyncpg  # type: ignore[import-untyped]


class DedicatedAdvisoryLease:
    """Session-level advisory lock on a connection never returned while held."""

    ACQUIRE_SQL = "SELECT pg_try_advisory_lock($1)"
    RELEASE_SQL = "SELECT pg_advisory_unlock($1)"

    def __init__(self, connection: Any, lock_key: int) -> None:
        self._connection = connection
        self._lock_key = lock_key
        self._held = False

    async def acquire(self) -> bool:
        if self._connection.is_closed():
            return False
        self._held = bool(await self._connection.fetchval(self.ACQUIRE_SQL, self._lock_key))
        return self._held

    async def renew(self) -> bool:
        if not self._held or self._connection.is_closed():
            return False
        try:
            return bool(await self._connection.fetchval("SELECT true"))
        except (OSError, asyncpg.PostgresError):
            self._held = False
            return False

    async def release(self) -> None:
        if self._held and not self._connection.is_closed():
            try:
                await self._connection.fetchval(self.RELEASE_SQL, self._lock_key)
            finally:
                self._held = False


@asynccontextmanager
async def dedicated_advisory_lease(
    database_url: str, lock_key: int
) -> AsyncIterator[DedicatedAdvisoryLease]:
    """Open/close the dedicated connection; caller must call acquire()."""
    dsn = database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    connection = await asyncpg.connect(dsn)
    lease = DedicatedAdvisoryLease(connection, lock_key)
    try:
        yield lease
    finally:
        await lease.release()
        await connection.close()

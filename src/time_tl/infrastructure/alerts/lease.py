"""Singleton lease ports and PostgreSQL advisory-lock contract."""

from __future__ import annotations

import asyncio
from typing import Protocol


class SingletonLease(Protocol):
    async def acquire(self) -> bool: ...
    async def renew(self) -> bool: ...
    async def release(self) -> None: ...


class InMemorySingletonLease:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._held = False

    async def acquire(self) -> bool:
        if self._lock.locked():
            return False
        await self._lock.acquire()
        self._held = True
        return True

    async def renew(self) -> bool:
        return self._held and self._lock.locked()

    async def release(self) -> None:
        if self._held:
            self._held = False
            self._lock.release()


class AdvisoryLockSession(Protocol):
    """Dedicated PostgreSQL session; never return it to a pool while held."""

    async def fetch_value(self, sql: str, *args: object) -> object: ...


class PostgresAdvisoryLease:
    ACQUIRE_SQL = "SELECT pg_try_advisory_lock($1)"
    RELEASE_SQL = "SELECT pg_advisory_unlock($1)"

    def __init__(self, session: AdvisoryLockSession, lock_key: int) -> None:
        self._session, self._lock_key, self._held = session, lock_key, False

    async def acquire(self) -> bool:
        self._held = bool(await self._session.fetch_value(self.ACQUIRE_SQL, self._lock_key))
        return self._held

    async def renew(self) -> bool:
        return self._held

    async def release(self) -> None:
        if self._held:
            await self._session.fetch_value(self.RELEASE_SQL, self._lock_key)
            self._held = False

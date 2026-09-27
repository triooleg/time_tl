from __future__ import annotations

import hmac
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from threading import RLock
from typing import Protocol, TypeVar

from .profiles import ConfirmationError

T = TypeVar("T")

# Production must implement this repository with durable PostgreSQL rows and
# transaction/row locking. The in-memory implementation is for local tests only.
PRODUCTION_PENDING_STORE = "durable PostgreSQL pending_import_sessions"
MAX_PENDING_IMPORTS_PER_USER = 3
MAX_PENDING_IMPORTS_GLOBAL = 1_000


class ImportSessionLimitExceeded(ConfirmationError):
    """Typed, user-safe rejection when pending import capacity is exhausted."""

    def __init__(self, *, scope: str, limit: int) -> None:
        if scope not in {"user", "global"}:
            raise ValueError("unknown import-session limit scope")
        self.scope = scope
        self.limit = limit
        message = (
            f"У вас уже є {limit} незавершені імпорти. Підтвердьте або скасуйте один із них."
            if scope == "user"
            else "Імпорт розкладу тимчасово перевантажений. Спробуйте пізніше."
        )
        super().__init__(message)


@dataclass(frozen=True)
class PendingImportSession:
    token: str
    fingerprint: str
    expires_at: datetime | None
    revoked: bool = False
    consumed: bool = False
    replacement_token: str | None = None
    replace_profile_id: str | None = None


class ImportSessionRepository(Protocol):
    """Atomic pending-import store; production implementation must use PostgreSQL."""

    def create(self, session: PendingImportSession) -> None: ...

    def assert_pending(self, token: str, fingerprint: str, now: datetime) -> None: ...

    def bind_replacement(
        self,
        token: str,
        fingerprint: str,
        replacement_token: str,
        replace_profile_id: str,
        now: datetime,
    ) -> None: ...

    def consume_atomically(
        self,
        token: str,
        fingerprint: str,
        now: datetime,
        operation: Callable[[], T],
        *,
        replacement_token: str | None = None,
        replace_profile_id: str | None = None,
    ) -> T: ...

    def revoke(self, token: str) -> None: ...


class InMemoryImportSessionRepository:
    """Thread-safe atomic test double. Never use as the production pending store."""

    def __init__(self) -> None:
        self._sessions: dict[str, PendingImportSession] = {}
        self._lock = RLock()

    def create(self, session: PendingImportSession) -> None:
        with self._lock:
            if session.token in self._sessions:
                raise ConfirmationError("duplicate import proposal token")
            self._sessions[session.token] = session

    def assert_pending(self, token: str, fingerprint: str, now: datetime) -> None:
        with self._lock:
            self._validate(token, fingerprint, now)

    def bind_replacement(
        self,
        token: str,
        fingerprint: str,
        replacement_token: str,
        replace_profile_id: str,
        now: datetime,
    ) -> None:
        with self._lock:
            session = self._validate(token, fingerprint, now)
            if session.replacement_token is not None:
                raise ConfirmationError("an import replacement is already pending")
            self._sessions[token] = replace(
                session,
                replacement_token=replacement_token,
                replace_profile_id=replace_profile_id,
            )

    def consume_atomically(
        self,
        token: str,
        fingerprint: str,
        now: datetime,
        operation: Callable[[], T],
        *,
        replacement_token: str | None = None,
        replace_profile_id: str | None = None,
    ) -> T:
        with self._lock:
            session = self._validate(token, fingerprint, now)
            expected = (session.replacement_token, session.replace_profile_id)
            supplied = (replacement_token, replace_profile_id)
            if expected != supplied:
                raise ConfirmationError("replacement proposal is missing, stale, or altered")
            result = operation()
            self._sessions[token] = replace(session, consumed=True)
            return result

    def revoke(self, token: str) -> None:
        with self._lock:
            session = self._sessions.get(token)
            if session is None:
                raise ConfirmationError("import proposal does not exist")
            if session.consumed:
                raise ConfirmationError("consumed import proposal cannot be revoked")
            self._sessions[token] = replace(session, revoked=True)

    def get(self, token: str) -> PendingImportSession:
        with self._lock:
            session = self._sessions.get(token)
            if session is None:
                raise ConfirmationError("import proposal does not exist")
            return session

    def _validate(self, token: str, fingerprint: str, now: datetime) -> PendingImportSession:
        if now.tzinfo is None:
            raise ConfirmationError("confirmation time must be timezone-aware")
        session = self._sessions.get(token)
        if session is None:
            raise ConfirmationError("import proposal is missing")
        if not hmac.compare_digest(session.fingerprint, fingerprint):
            raise ConfirmationError("import proposal was altered")
        if session.revoked:
            raise ConfirmationError("import proposal was revoked")
        if session.consumed:
            raise ConfirmationError("import proposal was already consumed")
        if session.expires_at is not None and now >= session.expires_at:
            raise ConfirmationError("import proposal has expired")
        return session

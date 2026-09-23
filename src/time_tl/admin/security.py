"""Password, session, CSRF and lockout primitives for the owner panel."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from argon2.low_level import Type

PASSWORD_MIN_LENGTH = 14
FAILURE_WINDOW = timedelta(minutes=15)
LOCKOUT_AFTER = 5
LOCKOUT_DURATION = timedelta(minutes=30)
SESSION_IDLE_TTL = timedelta(minutes=30)
SESSION_ABSOLUTE_TTL = timedelta(hours=12)
BOOTSTRAP_TTL = timedelta(hours=24)

_COMMON_PASSWORDS = {
    "passwordpassword",
    "adminadminadmin",
    "qwertyqwerty12",
    "12345678901234",
    "парольпарольпароль",
    "letmeinletmein12",
    "time-tl-time-tl",
}

_hasher = PasswordHasher(
    time_cost=3,
    memory_cost=65536,
    parallelism=2,
    hash_len=32,
    salt_len=16,
    type=Type.ID,
)


class PasswordPolicyError(ValueError):
    pass


def validate_new_password(password: str, confirmation: str) -> None:
    if len(password) < PASSWORD_MIN_LENGTH:
        raise PasswordPolicyError("Пароль должен содержать не менее 14 символов.")
    if not hmac.compare_digest(password, confirmation):
        raise PasswordPolicyError("Пароли не совпадают.")
    normalized = password.casefold().strip()
    if normalized in _COMMON_PASSWORDS or normalized.isdigit():
        raise PasswordPolicyError(
            "Этот пароль слишком распространён или найден в утечках. Выберите другой пароль."
        )


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(stored_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(stored_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(32)


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class Session:
    token: str
    csrf_token: str
    username: str
    must_change_password: bool
    created_at: datetime
    last_seen_at: datetime
    absolute_expires_at: datetime

    def expired(self, now: datetime) -> bool:
        return now >= self.absolute_expires_at or now - self.last_seen_at >= SESSION_IDLE_TTL

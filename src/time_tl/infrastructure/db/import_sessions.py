from __future__ import annotations

import hashlib
import hmac
import json
from base64 import b64decode, b64encode
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, TypeVar
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from time_tl.domain.schedule.profiles import ConfirmationError
from time_tl.domain.schedule.sessions import (
    MAX_PENDING_IMPORTS_GLOBAL,
    MAX_PENDING_IMPORTS_PER_USER,
    ImportSessionLimitExceeded,
)
from time_tl.domain.schedule.sessions import (
    PendingImportSession as DomainPendingImportSession,
)

from .codec import LessonLinkCodec
from .models import PendingImportSession, ScheduleProfile, User

T = TypeVar("T")
MAX_PROPOSAL_BYTES = 256_000
MAX_PENDING_RETENTION = timedelta(hours=24)
MAX_PURGE_BATCH = 100
_IMPORT_CREATE_ADVISORY_LOCK = 7_413_001
_METADATA_LABEL_MAX_LENGTH = 160
_METADATA_TIMESTAMP_MAX_LENGTH = 64
_SCHEMA_VERSION = "1.0"
_STORAGE_SCHEMA_VERSION = "fernet-json-v1"
_STORAGE_CIPHERTEXT_FIELDS = {"storage_schema_version", "ciphertext"}
_STORAGE_TOMBSTONE = {"storage_schema_version": _STORAGE_SCHEMA_VERSION, "state": "scrubbed"}
_ROOT_FIELDS = {"schema_version", "metadata", "profile", "expires_at"}
_METADATA_FIELDS = {"template_name", "created_at", "source_display_label", "included_fields"}
_PROFILE_FIELDS = {"name", "timezone", "rules", "overrides"}
_RULE_FIELDS = {"weekday", "week_parity", "date_parity", "valid_from", "valid_to", "lessons"}
_OVERRIDE_FIELDS = {"day", "day_off", "lessons"}
_LESSON_FIELDS = {"start", "end", "title", "enabled", "join_url"}
_FORBIDDEN_KEY_PARTS = (
    "region",
    "coordinate",
    "alert",
    "setting",
    "secret",
    "telegram",
    "password",
)
_FORBIDDEN_KEYS = {
    "id",
    "identity",
    "user_id",
    "chat_id",
    "owner_id",
    "profile_id",
    "token",
    "api_token",
    "latitude",
    "longitude",
    "lat",
    "lon",
    "lng",
    "authorization",
}


@dataclass(frozen=True)
class DurableImportProposal:
    """Sanitized proposal restored without exposing token or database identity."""

    payload: dict[str, Any] = field(repr=False)
    expires_at: datetime | None
    replace_profile_id: str | None = None


def digest_secret(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _encrypt_proposal_payload(payload: Mapping[str, Any], codec: LessonLinkCodec) -> dict[str, str]:
    sanitized = sanitize_proposal_payload(payload)
    serialized = json.dumps(
        sanitized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    ciphertext = codec.encrypt(serialized)
    return {
        "storage_schema_version": _STORAGE_SCHEMA_VERSION,
        "ciphertext": b64encode(ciphertext).decode("ascii"),
    }


def _decrypt_proposal_payload(
    envelope: Mapping[str, Any], codec: LessonLinkCodec
) -> dict[str, Any]:
    """Authenticate and restore a proposal without exposing ciphertext on failure."""
    try:
        if set(envelope) != _STORAGE_CIPHERTEXT_FIELDS:
            raise ValueError
        if envelope.get("storage_schema_version") != _STORAGE_SCHEMA_VERSION:
            raise ValueError
        encoded = envelope.get("ciphertext")
        if not isinstance(encoded, str):
            raise ValueError
        ciphertext = b64decode(encoded.encode("ascii"), validate=True)
        plaintext = codec.decrypt(ciphertext)
        decoded = json.loads(plaintext)
        if not isinstance(decoded, dict):
            raise ValueError
        return sanitize_proposal_payload(decoded)
    except Exception:
        raise ConfirmationError("import proposal could not be verified") from None


def sanitize_proposal_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return a detached, strict, size-bounded versioned schedule snapshot."""
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ConfirmationError("import proposal payload is not valid JSON") from exc
    if len(encoded) > MAX_PROPOSAL_BYTES:
        raise ConfirmationError("import proposal payload is oversized")
    decoded = json.loads(encoded)
    if not isinstance(decoded, dict):
        raise ConfirmationError("import proposal payload must be an object")
    required_root = {"schema_version", "metadata", "profile"}
    if set(decoded) - _ROOT_FIELDS or not required_root.issubset(decoded):
        raise ConfirmationError("import proposal payload has unknown or missing fields")
    if decoded["schema_version"] != _SCHEMA_VERSION:
        raise ConfirmationError("unsupported import proposal schema version")
    _reject_prohibited_keys(decoded)
    metadata = _require_exact_mapping(decoded["metadata"], _METADATA_FIELDS, "metadata")
    _validate_metadata(metadata)
    profile = _require_mapping(decoded["profile"], "profile")
    if set(profile) - _PROFILE_FIELDS or not {"name", "timezone"}.issubset(profile):
        raise ConfirmationError("import proposal profile has unknown or missing fields")
    rules = profile.get("rules", [])
    overrides = profile.get("overrides", [])
    if not isinstance(rules, list) or not isinstance(overrides, list):
        raise ConfirmationError("import proposal rules and overrides must be lists")
    for index, rule in enumerate(rules):
        mapped = _require_exact_mapping(rule, _RULE_FIELDS, f"rule[{index}]")
        _validate_lessons(mapped["lessons"], f"rule[{index}]")
    for index, override in enumerate(overrides):
        mapped = _require_exact_mapping(override, _OVERRIDE_FIELDS, f"override[{index}]")
        _validate_lessons(mapped["lessons"], f"override[{index}]")
    return decoded


def _validate_metadata(metadata: Mapping[str, Any]) -> None:
    label = metadata["source_display_label"]
    if (
        not isinstance(label, str)
        or not label.strip()
        or label != label.strip()
        or len(label) > _METADATA_LABEL_MAX_LENGTH
    ):
        raise ConfirmationError("import proposal source label is invalid")
    created_at = metadata["created_at"]
    if (
        not isinstance(created_at, str)
        or not created_at
        or created_at != created_at.strip()
        or len(created_at) > _METADATA_TIMESTAMP_MAX_LENGTH
        or "T" not in created_at
    ):
        raise ConfirmationError("import proposal created_at is invalid")
    try:
        parsed = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ConfirmationError("import proposal created_at is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ConfirmationError("import proposal created_at must be timezone-aware")


def _validate_lessons(value: Any, path: str) -> None:
    if not isinstance(value, list):
        raise ConfirmationError(f"{path} lessons must be a list")
    for index, lesson in enumerate(value):
        mapped = _require_mapping(lesson, f"{path}.lesson[{index}]")
        required_lesson = {"start", "end", "title", "enabled"}
        if set(mapped) - _LESSON_FIELDS or not required_lesson.issubset(mapped):
            raise ConfirmationError(
                f"import proposal {path}.lesson[{index}] has unknown or missing fields"
            )


def _require_mapping(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfirmationError(f"import proposal {path} must be an object")
    return value


def _require_exact_mapping(value: Any, fields: set[str], path: str) -> dict[str, Any]:
    mapped = _require_mapping(value, path)
    if set(mapped) != fields:
        raise ConfirmationError(f"import proposal {path} has unknown or missing fields")
    return mapped


def _reject_prohibited_keys(value: Any) -> None:
    if isinstance(value, dict):
        for raw_key, child in value.items():
            key = str(raw_key).strip().lower().replace("-", "_")
            if (
                key in _FORBIDDEN_KEYS
                or key.endswith("_id")
                or key.endswith("_token")
                or any(part in key for part in _FORBIDDEN_KEY_PARTS)
            ):
                raise ConfirmationError("import proposal contains prohibited fields")
            _reject_prohibited_keys(child)
    elif isinstance(value, list):
        for child in value:
            _reject_prohibited_keys(child)


class PostgresImportSessionRepository:
    """Row-locked one-time confirmations; caller owns commit/rollback."""

    def __init__(self, session: AsyncSession, payload_codec: LessonLinkCodec | None = None) -> None:
        self._session = session
        self._payload_codec = payload_codec

    async def create(
        self,
        *,
        token: str,
        user_id: UUID,
        fingerprint: str,
        expires_at: datetime | None,
        proposal_payload: Mapping[str, Any],
    ) -> None:
        codec = self._require_codec()
        created_at = datetime.now(UTC)
        await self._lock_create_capacity(user_id=user_id, now=created_at)
        retention_deadline = created_at + MAX_PENDING_RETENTION
        if expires_at is not None:
            self._validate_time(expires_at)
            effective_expiry = min(expires_at, retention_deadline)
        else:
            effective_expiry = retention_deadline
        self._session.add(
            PendingImportSession(
                token_hash=digest_secret(token),
                user_id=user_id,
                fingerprint_hash=digest_secret(fingerprint),
                proposal_payload=_encrypt_proposal_payload(proposal_payload, codec),
                expires_at=effective_expiry,
            )
        )

    async def _lock_create_capacity(self, *, user_id: UUID, now: datetime) -> None:
        """Serialize public import creation and enforce bounded durable state."""
        self._validate_time(now)
        await self._session.execute(
            select(func.pg_advisory_xact_lock(_IMPORT_CREATE_ADVISORY_LOCK))
        )
        owner = (
            await self._session.execute(select(User.id).where(User.id == user_id).with_for_update())
        ).scalar_one_or_none()
        if owner is None:
            raise ConfirmationError("import proposal owner is missing")
        active = (
            PendingImportSession.revoked_at.is_(None),
            PendingImportSession.consumed_at.is_(None),
            or_(
                PendingImportSession.expires_at.is_(None),
                PendingImportSession.expires_at > now,
            ),
        )
        user_tokens = (
            (
                await self._session.execute(
                    select(PendingImportSession.token_hash)
                    .where(PendingImportSession.user_id == user_id, *active)
                    .order_by(PendingImportSession.created_at, PendingImportSession.token_hash)
                    .limit(MAX_PENDING_IMPORTS_PER_USER)
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        if len(user_tokens) >= MAX_PENDING_IMPORTS_PER_USER:
            raise ImportSessionLimitExceeded(scope="user", limit=MAX_PENDING_IMPORTS_PER_USER)
        global_count = int(
            (
                await self._session.execute(
                    select(func.count(PendingImportSession.token_hash)).where(*active)
                )
            ).scalar_one()
        )
        if global_count >= MAX_PENDING_IMPORTS_GLOBAL:
            raise ImportSessionLimitExceeded(scope="global", limit=MAX_PENDING_IMPORTS_GLOBAL)

    async def load(
        self, *, token: str, now: datetime, expected_user_id: UUID
    ) -> DurableImportProposal:
        row = await self._locked(token, expected_user_id=expected_user_id)
        self._validate_pending(row, now)
        return self._record(row)

    async def assert_pending(
        self,
        *,
        token: str,
        fingerprint: str,
        now: datetime,
        expected_user_id: UUID | None = None,
    ) -> None:
        await self._validate(token, fingerprint, now, expected_user_id=expected_user_id)

    async def bind_replacement(
        self,
        *,
        token: str,
        fingerprint: str,
        replacement_token: str,
        replace_profile_id: UUID,
        now: datetime,
        expected_user_id: UUID | None = None,
    ) -> None:
        row = await self._validate(token, fingerprint, now, expected_user_id=expected_user_id)
        if row.replacement_token_hash is not None:
            raise ConfirmationError("an import replacement is already pending")
        row.replacement_token_hash = digest_secret(replacement_token)
        row.replace_profile_id = replace_profile_id
        await self._session.flush()

    async def revoke(
        self, *, token: str, now: datetime, expected_user_id: UUID | None = None
    ) -> None:
        row = await self._locked(token, expected_user_id=expected_user_id)
        self._validate_time(now)
        if row.consumed_at is not None:
            raise ConfirmationError("consumed import proposal cannot be revoked")
        if row.revoked_at is not None:
            raise ConfirmationError("import proposal was already revoked")
        row.revoked_at = now
        self._scrub(row)
        await self._session.flush()

    async def consume_atomically(
        self,
        *,
        token: str,
        fingerprint: str,
        now: datetime,
        operation: Callable[[], Awaitable[T]],
        replacement_token: str | None = None,
        replace_profile_id: UUID | None = None,
        expected_user_id: UUID | None = None,
    ) -> T:
        row = await self._validate(token, fingerprint, now, expected_user_id=expected_user_id)
        supplied = digest_secret(replacement_token) if replacement_token else None
        if row.replacement_token_hash != supplied or row.replace_profile_id != replace_profile_id:
            raise ConfirmationError("replacement proposal is missing, stale, or altered")
        result = await operation()
        row.consumed_at = now
        self._scrub(row)
        await self._session.flush()
        return result

    async def purge_expired(
        self,
        now: datetime,
        *,
        limit: int = MAX_PURGE_BATCH,
        exclude_token_hash: str | None = None,
    ) -> int:
        """Delete one bounded batch of expired or old terminal proposals."""
        self._validate_time(now)
        if limit < 1 or limit > MAX_PURGE_BATCH:
            raise ValueError(f"purge limit must be between 1 and {MAX_PURGE_BATCH}")
        terminal_cutoff = now - MAX_PENDING_RETENTION
        conditions = [
            or_(
                and_(
                    PendingImportSession.expires_at.is_not(None),
                    PendingImportSession.expires_at <= now,
                ),
                and_(
                    or_(
                        PendingImportSession.revoked_at.is_not(None),
                        PendingImportSession.consumed_at.is_not(None),
                    ),
                    PendingImportSession.created_at <= terminal_cutoff,
                ),
            )
        ]
        if exclude_token_hash is not None:
            conditions.append(PendingImportSession.token_hash != exclude_token_hash)
        stmt = (
            select(PendingImportSession)
            .where(*conditions)
            .order_by(PendingImportSession.expires_at, PendingImportSession.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        rows = list((await self._session.execute(stmt)).scalars().all())
        for row in rows:
            await self._session.delete(row)
        if rows:
            await self._session.flush()
        return len(rows)

    async def _validate(
        self,
        token: str,
        fingerprint: str,
        now: datetime,
        *,
        expected_user_id: UUID | None = None,
    ) -> PendingImportSession:
        row = await self._locked(token, expected_user_id=expected_user_id)
        self._validate_pending(row, now)
        if not hmac.compare_digest(row.fingerprint_hash, digest_secret(fingerprint)):
            raise ConfirmationError("import proposal was altered")
        return row

    async def _locked(
        self, token: str, *, expected_user_id: UUID | None = None
    ) -> PendingImportSession:
        stmt = (
            select(PendingImportSession)
            .where(
                PendingImportSession.token_hash == digest_secret(token),
                *([PendingImportSession.user_id == expected_user_id] if expected_user_id else []),
            )
            .with_for_update()
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is None:
            raise ConfirmationError("import proposal is missing")
        return row

    async def _locked_by_replacement(
        self, replacement_token: str, *, expected_user_id: UUID
    ) -> PendingImportSession:
        stmt = (
            select(PendingImportSession)
            .where(
                PendingImportSession.replacement_token_hash == digest_secret(replacement_token),
                PendingImportSession.user_id == expected_user_id,
            )
            .with_for_update()
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is None:
            raise ConfirmationError("replacement proposal is missing")
        return row

    def _record(self, row: PendingImportSession) -> DurableImportProposal:
        return DurableImportProposal(
            payload=_decrypt_proposal_payload(row.proposal_payload, self._require_codec()),
            expires_at=row.expires_at,
        )

    def _require_codec(self) -> LessonLinkCodec:
        if self._payload_codec is None:
            raise ConfirmationError("import proposal encryption is unavailable")
        return self._payload_codec

    @staticmethod
    def _scrub(row: PendingImportSession) -> None:
        row.proposal_payload = dict(_STORAGE_TOMBSTONE)

    @classmethod
    def _validate_pending(cls, row: PendingImportSession, now: datetime) -> None:
        cls._validate_time(now)
        if row.revoked_at is not None:
            raise ConfirmationError("import proposal was revoked")
        if row.consumed_at is not None:
            raise ConfirmationError("import proposal was already consumed")
        if row.expires_at is not None and now >= row.expires_at:
            raise ConfirmationError("import proposal has expired")

    @staticmethod
    def _validate_time(now: datetime) -> None:
        if now.tzinfo is None:
            raise ConfirmationError("confirmation time must be timezone-aware")


class AsyncImportSessionRepository(Protocol):
    async def create(
        self, pending: DomainPendingImportSession, proposal_payload: Mapping[str, Any]
    ) -> None: ...
    async def load(self, token: str, now: datetime) -> DurableImportProposal: ...
    async def assert_pending(self, token: str, fingerprint: str, now: datetime) -> None: ...
    async def bind_replacement(
        self,
        token: str,
        fingerprint: str,
        replacement_token: str,
        replace_profile_id: str,
        now: datetime,
    ) -> None: ...
    async def consume_atomically(
        self,
        token: str,
        fingerprint: str,
        now: datetime,
        operation: Callable[[AsyncSession, DurableImportProposal], Awaitable[T]],
        *,
        replacement_token: str | None = None,
        replace_profile_id: str | None = None,
    ) -> T: ...
    async def consume_by_replacement_token(
        self,
        replacement_token: str,
        now: datetime,
        operation: Callable[[AsyncSession, DurableImportProposal], Awaitable[T]],
    ) -> T: ...
    async def revoke(self, token: str, now: datetime) -> None: ...
    async def purge_expired(self, now: datetime, *, limit: int = MAX_PURGE_BATCH) -> int: ...


class AsyncPostgresImportSessionAdapter:
    """Durable, user-bound adapter whose consume callback shares its transaction."""

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        *,
        telegram_user_id: int,
        payload_codec: LessonLinkCodec,
    ) -> None:
        self._factory = factory
        self._telegram_user_id = telegram_user_id
        self._payload_codec = payload_codec

    async def create(
        self,
        pending: DomainPendingImportSession,
        proposal_payload: Mapping[str, Any],
    ) -> None:
        # Cleanup has its own bounded transaction so it never holds proposal rows
        # while the user row is locked by the create transaction.
        await self.purge_expired(datetime.now(UTC))
        async with self._factory() as session:
            async with session.begin():
                repo = self._repository(session)
                user = (
                    await session.execute(
                        select(User).where(User.telegram_user_id == self._telegram_user_id)
                    )
                ).scalar_one_or_none()
                if user is None:
                    raise ConfirmationError("import proposal owner is missing")
                await repo.create(
                    token=pending.token,
                    user_id=user.id,
                    fingerprint=pending.fingerprint,
                    expires_at=pending.expires_at,
                    proposal_payload=proposal_payload,
                )

    async def load(self, token: str, now: datetime) -> DurableImportProposal:
        async with self._factory() as session:
            async with session.begin():
                user = await self._locked_user(session)
                return await self._repository(session).load(
                    token=token, now=now, expected_user_id=user.id
                )

    async def assert_pending(self, token: str, fingerprint: str, now: datetime) -> None:
        async with self._factory() as session:
            async with session.begin():
                user = await self._locked_user(session)
                await self._repository(session).assert_pending(
                    token=token,
                    fingerprint=fingerprint,
                    now=now,
                    expected_user_id=user.id,
                )

    async def bind_replacement(
        self,
        token: str,
        fingerprint: str,
        replacement_token: str,
        replace_profile_id: str,
        now: datetime,
    ) -> None:
        async with self._factory() as session:
            async with session.begin():
                user = await self._locked_user(session)
                profile = (
                    await session.execute(
                        select(ScheduleProfile)
                        .where(
                            ScheduleProfile.user_id == user.id,
                            ScheduleProfile.public_id == replace_profile_id,
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if profile is None:
                    raise ConfirmationError("replacement profile is missing")
                await self._repository(session).bind_replacement(
                    token=token,
                    fingerprint=fingerprint,
                    replacement_token=replacement_token,
                    replace_profile_id=profile.id,
                    now=now,
                    expected_user_id=user.id,
                )

    async def consume_atomically(
        self,
        token: str,
        fingerprint: str,
        now: datetime,
        operation: Callable[[AsyncSession, DurableImportProposal], Awaitable[T]],
        *,
        replacement_token: str | None = None,
        replace_profile_id: str | None = None,
    ) -> T:
        async with self._factory() as session:
            async with session.begin():
                user = await self._locked_user(session)
                profile_uuid = None
                if replace_profile_id is not None:
                    profile_uuid = (
                        await session.execute(
                            select(ScheduleProfile.id).where(
                                ScheduleProfile.user_id == user.id,
                                ScheduleProfile.public_id == replace_profile_id,
                            )
                        )
                    ).scalar_one_or_none()
                    if profile_uuid is None:
                        raise ConfirmationError("replacement profile is missing")
                repo = self._repository(session)
                row = await repo._validate(token, fingerprint, now, expected_user_id=user.id)
                supplied = digest_secret(replacement_token) if replacement_token else None
                if row.replacement_token_hash != supplied or row.replace_profile_id != profile_uuid:
                    raise ConfirmationError("replacement proposal is missing, stale, or altered")
                durable = repo._record(row)
                result = await operation(session, durable)
                row.consumed_at = now
                repo._scrub(row)
                await session.flush()
                return result

    async def consume_by_replacement_token(
        self,
        replacement_token: str,
        now: datetime,
        operation: Callable[[AsyncSession, DurableImportProposal], Awaitable[T]],
    ) -> T:
        """Consume a replacement using only its restart-surviving secret token."""
        async with self._factory() as session:
            async with session.begin():
                user = await self._locked_user(session)
                repo = self._repository(session)
                row = await repo._locked_by_replacement(replacement_token, expected_user_id=user.id)
                repo._validate_pending(row, now)
                if row.replace_profile_id is None:
                    raise ConfirmationError("replacement profile is missing")
                profile = await session.get(ScheduleProfile, row.replace_profile_id)
                if profile is None or profile.user_id != user.id:
                    raise ConfirmationError("replacement profile is missing")
                restored = repo._record(row)
                durable = DurableImportProposal(
                    payload=restored.payload,
                    expires_at=row.expires_at,
                    replace_profile_id=profile.public_id,
                )
                result = await operation(session, durable)
                row.consumed_at = now
                repo._scrub(row)
                await session.flush()
                return result

    async def revoke(self, token: str, now: datetime) -> None:
        async with self._factory() as session:
            async with session.begin():
                user = await self._locked_user(session)
                await self._repository(session).revoke(
                    token=token, now=now, expected_user_id=user.id
                )

    async def purge_expired(self, now: datetime, *, limit: int = MAX_PURGE_BATCH) -> int:
        async with self._factory() as session:
            async with session.begin():
                return await self._repository(session).purge_expired(now, limit=limit)

    def _repository(self, session: AsyncSession) -> PostgresImportSessionRepository:
        return PostgresImportSessionRepository(session, self._payload_codec)

    async def _locked_user(self, session: AsyncSession) -> User:
        row = (
            await session.execute(
                select(User)
                .where(User.telegram_user_id == self._telegram_user_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            raise ConfirmationError("import proposal owner is missing")
        return row

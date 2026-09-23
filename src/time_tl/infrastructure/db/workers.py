from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from time_tl.application.schedule_worker import (
    RETURN_TO_LESSON_MAX_AGE,
    ConfirmedClear,
    PlannedOutboxMessage,
    ScheduleTarget,
)
from time_tl.application.sender_worker import ClaimedOutboxMessage
from time_tl.domain.alerts import AlertState
from time_tl.domain.alerts.decisions import NotificationMode

from .accounts import AsyncPostgresAccountRepository
from .codec import LessonLinkCodec
from .models import (
    AlertPollState as PollRow,
)
from .models import (
    AlertScope,
    AlertSnapshot,
    OutboxMessage,
    OverrideLesson,
    RuleLesson,
    ScheduleProfile,
    User,
    WorkerHeartbeat,
)
from .outbox import pending_outbox_query

_MODE = {
    "LESSONS_ONLY": NotificationMode.LESSONS_ONLY,
    "LESSONS_AND_ALERTS": NotificationMode.LESSONS_AND_ALERTS,
    "ALERTS_ONLY": NotificationMode.ALERTS_ONLY,
}
_REF = re.compile(
    r"^lesson:(rule|override):([0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})$",
    re.IGNORECASE,
)


def _provider_is_fresh(
    last_success_at: datetime | None,
    authorization_at: datetime,
    stale_after: timedelta,
) -> bool:
    """Return true through the exact TTL boundary, fail closed otherwise."""
    if authorization_at.tzinfo is None or stale_after <= timedelta(0):
        raise ValueError("freshness check requires aware time and positive TTL")
    if last_success_at is None or last_success_at.tzinfo is None:
        return False
    age = authorization_at - last_success_at
    return timedelta(0) <= age <= stale_after


class PostgresScheduleWorkerRepository:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        *,
        link_codec: LessonLinkCodec | None = None,
        stale_after_seconds: int,
    ) -> None:
        if stale_after_seconds < 1:
            raise ValueError("stale threshold must be positive")
        self._factory = factory
        self._accounts = AsyncPostgresAccountRepository(factory, link_codec)
        self._stale_after = timedelta(seconds=stale_after_seconds)

    async def list_schedule_targets(self, now: datetime) -> tuple[ScheduleTarget, ...]:
        if now.tzinfo is None:
            raise ValueError("worker clock must be timezone-aware")
        async with self._factory() as session:
            poll = await session.get(PollRow, 1)
            provider_fresh = _provider_is_fresh(
                poll.last_success_at if poll is not None else None,
                now,
                self._stale_after,
            )
            statement = (
                select(User, ScheduleProfile, AlertSnapshot.state)
                .join(ScheduleProfile, ScheduleProfile.user_id == User.id)
                .outerjoin(AlertScope, AlertScope.user_id == User.id)
                .outerjoin(AlertSnapshot, AlertSnapshot.scope_id == AlertScope.id)
                .where(
                    ScheduleProfile.is_active.is_(True),
                    User.mode.in_(("LESSONS_ONLY", "LESSONS_AND_ALERTS")),
                )
                .order_by(User.id)
            )
            rows = (await session.execute(statement)).all()
            result = []
            for user, profile, state in rows:
                alert_state = AlertState(state) if state else AlertState.UNKNOWN
                if user.mode == "LESSONS_AND_ALERTS" and not provider_fresh:
                    alert_state = AlertState.STALE
                result.append(
                    ScheduleTarget(
                        user_id=str(user.id),
                        telegram_chat_id=user.telegram_user_id,
                        profile=await self._accounts._load_profile(session, profile),
                        mode=_MODE[user.mode],
                        reminder_lead_minutes=user.reminder_lead_minutes,
                        alert_state=alert_state,
                        profile_revision=profile.version,
                    )
                )
            return tuple(result)

    async def list_confirmed_clears(self, now: datetime) -> tuple[ConfirmedClear, ...]:
        if now.tzinfo is None:
            raise ValueError("worker clock must be timezone-aware")
        cutoff = now - RETURN_TO_LESSON_MAX_AGE
        result = []
        async with self._factory() as session:
            poll = await session.get(PollRow, 1)
            if not _provider_is_fresh(
                poll.last_success_at if poll is not None else None,
                now,
                self._stale_after,
            ):
                return ()
            messages = (
                (
                    await session.execute(
                        select(OutboxMessage)
                        .where(
                            OutboxMessage.event_type == "ALL_CLEAR_CONFIRMED",
                            OutboxMessage.created_at >= cutoff,
                            OutboxMessage.created_at <= now,
                        )
                        .order_by(OutboxMessage.created_at)
                    )
                )
                .scalars()
                .all()
            )
            for message in messages:
                try:
                    scope_id = UUID(message.aggregate_id)
                except ValueError:
                    continue
                scope = await session.get(AlertScope, scope_id)
                if scope is None:
                    continue
                snapshot = await session.get(AlertSnapshot, scope.id)
                if snapshot is None or snapshot.state != AlertState.CLEAR_CONFIRMED.value:
                    continue
                user = await session.get(User, scope.user_id)
                if user is None or user.mode != "LESSONS_AND_ALERTS":
                    continue
                profile = (
                    await session.execute(
                        select(ScheduleProfile).where(
                            ScheduleProfile.user_id == user.id, ScheduleProfile.is_active.is_(True)
                        )
                    )
                ).scalar_one_or_none()
                if profile is None:
                    continue
                event_id = message.payload.get("event_id")
                if not isinstance(event_id, str) or not event_id:
                    continue
                target = ScheduleTarget(
                    user_id=str(user.id),
                    telegram_chat_id=user.telegram_user_id,
                    profile=await self._accounts._load_profile(session, profile),
                    mode=NotificationMode.LESSONS_AND_ALERTS,
                    reminder_lead_minutes=user.reminder_lead_minutes,
                    alert_state=AlertState.CLEAR_CONFIRMED,
                    profile_revision=profile.version,
                )
                result.append(ConfirmedClear(event_id, message.created_at, target))
        return tuple(result)

    async def enqueue_deduplicated(self, messages: tuple[PlannedOutboxMessage, ...]) -> int:
        if any(message.available_at.tzinfo is None for message in messages):
            raise ValueError("outbox time must be timezone-aware")
        async with self._factory() as session:
            async with session.begin():
                inserted = 0
                for message in messages:
                    self._validate_payload(message.payload)
                    statement = (
                        insert(OutboxMessage)
                        .values(
                            event_key=message.event_key,
                            aggregate_type="schedule",
                            aggregate_id=message.aggregate_id,
                            event_type=message.event_type,
                            payload=message.payload,
                            available_at=message.available_at,
                        )
                        .on_conflict_do_nothing(index_elements=[OutboxMessage.event_key])
                        .returning(OutboxMessage.id)
                    )
                    if (await session.execute(statement)).scalar_one_or_none() is not None:
                        inserted += 1
                return inserted

    @staticmethod
    def _validate_payload(payload: dict[str, object]) -> None:
        def inspect(value: object, key: str | None = None) -> None:
            normalized = key.casefold() if key else ""
            if normalized == "join_link_ref":
                if value is None:
                    return
                if not isinstance(value, str) or _REF.fullmatch(value) is None:
                    raise ValueError("invalid opaque join link reference")
                return
            if (
                key
                and normalized != "join_link_ref"
                and ("url" in normalized or "link" in normalized)
            ):
                raise ValueError("plaintext links are forbidden in outbox")
            if isinstance(value, str):
                if "http://" in value.casefold() or "https://" in value.casefold():
                    raise ValueError("plaintext links are forbidden in outbox")
                if normalized == "join_link_ref" and _REF.fullmatch(value) is None:
                    raise ValueError("invalid opaque join link reference")
            elif isinstance(value, dict):
                for child_key, child in value.items():
                    inspect(child, str(child_key))
            elif isinstance(value, (list, tuple)):
                for child in value:
                    inspect(child)

        inspect(payload)


class PostgresSenderRepository:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        *,
        visibility_timeout: timedelta = timedelta(minutes=5),
        stale_after_seconds: int,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if stale_after_seconds < 1:
            raise ValueError("stale threshold must be positive")
        self._factory = factory
        self._visibility_timeout = visibility_timeout
        self._stale_after = timedelta(seconds=stale_after_seconds)
        self._clock = clock or (lambda: datetime.now(UTC))

    def _current_time(self) -> datetime:
        current = self._clock()
        if current.tzinfo is None:
            raise ValueError("repository clock must be timezone-aware")
        return current

    async def claim_batch_skip_locked(
        self, now: datetime, *, limit: int
    ) -> tuple[ClaimedOutboxMessage, ...]:
        if now.tzinfo is None or limit < 1:
            raise ValueError("invalid claim parameters")
        async with self._factory() as session:
            async with session.begin():
                rows = (
                    (
                        await session.execute(
                            pending_outbox_query(
                                now, limit=limit, visibility_timeout=self._visibility_timeout
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                deliverable = []
                for row in rows:
                    if await self._is_currently_deliverable(
                        session,
                        row,
                        authorization_at=now,
                        stale_after=self._stale_after,
                    ):
                        row.claimed_at = now
                        deliverable.append(row)
                    else:
                        # A mode/profile/state change revokes queued permission.
                        # Quarantine instead of retrying forever or sending stale
                        # safety guidance.
                        row.dead_at = now
                        row.claimed_at = None
                        row.last_error_code = "delivery_policy_changed"
                return tuple(
                    ClaimedOutboxMessage(
                        str(row.id), row.event_type, dict(row.payload), row.attempts
                    )
                    for row in deliverable
                )

    @staticmethod
    async def _is_currently_deliverable(
        session: AsyncSession,
        row: OutboxMessage,
        *,
        authorization_at: datetime,
        stale_after: timedelta,
        lock_policy_rows: bool = False,
    ) -> bool:
        if authorization_at.tzinfo is None:
            raise ValueError("authorization time must be timezone-aware")
        alert_dependent = row.event_type in {
            "ALERT_STARTED",
            "ALL_CLEAR_CONFIRMED",
            "RETURN_TO_ACTIVE_LESSON",
        }
        if row.event_type == "LESSON_REMINDER":
            try:
                lesson_user_id = UUID(row.aggregate_id)
            except ValueError:
                return False
            lesson_user = await session.get(User, lesson_user_id)
            alert_dependent = lesson_user is not None and lesson_user.mode == "LESSONS_AND_ALERTS"
        if alert_dependent:
            if lock_policy_rows:
                poll = (
                    await session.execute(
                        select(PollRow)
                        .where(PollRow.singleton_id == 1)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
            else:
                poll = await session.get(PollRow, 1)
            if not _provider_is_fresh(
                poll.last_success_at if poll is not None else None,
                authorization_at,
                stale_after,
            ):
                return False
        if row.event_type in {"ALERT_STARTED", "ALL_CLEAR_CONFIRMED"}:
            if row.aggregate_type != "alert_scope":
                return False
            try:
                scope_id = UUID(row.aggregate_id)
            except ValueError:
                return False
            if lock_policy_rows:
                scope_hint = await session.get(AlertScope, scope_id)
                if scope_hint is None:
                    return False
                user = (
                    await session.execute(
                        select(User).where(User.id == scope_hint.user_id).with_for_update()
                    )
                ).scalar_one_or_none()
                scope = (
                    await session.execute(
                        select(AlertScope)
                        .where(AlertScope.id == scope_id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
            else:
                scope = await session.get(AlertScope, scope_id)
            if scope is None:
                return False
            if lock_policy_rows:
                if user is None or user.id != scope.user_id:
                    return False
                snapshot = (
                    await session.execute(
                        select(AlertSnapshot)
                        .where(AlertSnapshot.scope_id == scope.id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
            else:
                user = await session.get(User, scope.user_id)
                snapshot = await session.get(AlertSnapshot, scope.id)
            if user is None or snapshot is None or user.mode not in {
                "LESSONS_AND_ALERTS",
                "ALERTS_ONLY",
            }:
                return False
            required = (
                AlertState.ACTIVE.value
                if row.event_type == "ALERT_STARTED"
                else AlertState.CLEAR_CONFIRMED.value
            )
            return snapshot.state == required

        if row.event_type in {"LESSON_REMINDER", "RETURN_TO_ACTIVE_LESSON"}:
            if row.aggregate_type != "schedule":
                return False
            try:
                user_id = UUID(row.aggregate_id)
            except ValueError:
                return False
            if lock_policy_rows:
                user = (
                    await session.execute(
                        select(User).where(User.id == user_id).with_for_update()
                    )
                ).scalar_one_or_none()
            else:
                user = await session.get(User, user_id)
            if user is None or user.mode not in {"LESSONS_ONLY", "LESSONS_AND_ALERTS"}:
                return False
            if user.mode == "LESSONS_AND_ALERTS":
                scope_query = select(AlertScope).where(AlertScope.user_id == user.id)
                if lock_policy_rows:
                    scope_query = scope_query.with_for_update()
                scope = (await session.execute(scope_query)).scalar_one_or_none()
                if scope is None:
                    snapshot = None
                elif lock_policy_rows:
                    snapshot = (
                        await session.execute(
                            select(AlertSnapshot)
                            .where(AlertSnapshot.scope_id == scope.id)
                            .with_for_update()
                        )
                    ).scalar_one_or_none()
                else:
                    snapshot = await session.get(AlertSnapshot, scope.id)
                if snapshot is None or snapshot.state != AlertState.CLEAR_CONFIRMED.value:
                    return False
            if row.event_type == "RETURN_TO_ACTIVE_LESSON" and user.mode != "LESSONS_AND_ALERTS":
                return False
            revision = row.payload.get("profile_revision")
            if not isinstance(revision, int):
                return False
            profile_query = select(ScheduleProfile).where(
                ScheduleProfile.user_id == user.id,
                ScheduleProfile.is_active.is_(True),
            )
            if lock_policy_rows:
                profile_query = profile_query.with_for_update()
            profile = (await session.execute(profile_query)).scalar_one_or_none()
            if profile is None or profile.version != revision:
                return False
            return True
        return True

    async def deliver_if_authorized(
        self,
        message_id: str,
        delivered_at: datetime,
        deliver: Callable[[], Awaitable[None]],
    ) -> bool:
        """Revalidate and deliver while policy rows remain locked.

        Holding the bounded transaction across the external Telegram call is
        intentional: a concurrent mode, alert-state, region, or active-profile
        change cannot commit between final authorization and transport I/O.
        Telegram has no idempotency key, so a successful send followed by a
        database commit failure can still cause a duplicate on retry; the
        outbox event key prevents duplicate planning, but cannot remove that
        provider-level ambiguity.
        """
        if delivered_at.tzinfo is None:
            raise ValueError("delivery time must be timezone-aware")
        async with self._factory() as session:
            async with session.begin():
                row = await self._locked(session, message_id)
                if row.claimed_at is None:
                    raise LookupError(message_id)
                authorization_at = self._current_time()
                if not await self._is_currently_deliverable(
                    session,
                    row,
                    authorization_at=authorization_at,
                    stale_after=self._stale_after,
                    lock_policy_rows=True,
                ):
                    row.dead_at = authorization_at
                    row.claimed_at = None
                    row.last_error_code = "final_delivery_policy_changed"
                    return False
                valid_until: datetime | None = None
                if row.event_type == "RETURN_TO_ACTIVE_LESSON":
                    raw_valid_until = row.payload.get("valid_until")
                    if isinstance(raw_valid_until, str):
                        try:
                            valid_until = datetime.fromisoformat(raw_valid_until)
                        except ValueError:
                            valid_until = None
                    if valid_until is not None and valid_until.tzinfo is None:
                        valid_until = None
                requires_fresh_provider = row.event_type in {
                    "ALERT_STARTED",
                    "ALL_CLEAR_CONFIRMED",
                    "RETURN_TO_ACTIVE_LESSON",
                }
                if row.event_type == "LESSON_REMINDER":
                    try:
                        lesson_user_id = UUID(row.aggregate_id)
                    except ValueError:
                        lesson_user_id = None
                    lesson_user = (
                        await session.get(User, lesson_user_id)
                        if lesson_user_id is not None
                        else None
                    )
                    requires_fresh_provider = (
                        lesson_user is not None
                        and lesson_user.mode == "LESSONS_AND_ALERTS"
                    )
                locked_poll = (
                    await session.get(PollRow, 1) if requires_fresh_provider else None
                )
                # Read the clock only after all policy rows are locked. For a
                # return-to-lesson message this is the exact no-send boundary:
                # equality with valid_until is expired.
                send_at = self._current_time()
                if requires_fresh_provider and not _provider_is_fresh(
                    locked_poll.last_success_at if locked_poll is not None else None,
                    send_at,
                    self._stale_after,
                ):
                    row.dead_at = send_at
                    row.claimed_at = None
                    row.last_error_code = "final_delivery_policy_changed"
                    return False
                if valid_until is None and row.event_type == "RETURN_TO_ACTIVE_LESSON":
                    row.dead_at = send_at
                    row.claimed_at = None
                    row.last_error_code = "final_delivery_invalid_expiry"
                    return False
                if valid_until is not None and send_at >= valid_until:
                    row.dead_at = send_at
                    row.claimed_at = None
                    row.last_error_code = "final_delivery_expired"
                    return False
                await deliver()
                row.delivered_at = send_at
                row.claimed_at = None
                row.last_error_code = None
                return True

    async def mark_delivered(self, message_id: str, delivered_at: datetime) -> None:
        async with self._factory() as session:
            async with session.begin():
                row = await self._locked(session, message_id)
                row.delivered_at = delivered_at
                row.claimed_at = None
                row.last_error_code = None

    async def mark_retry(self, message_id: str, *, available_at: datetime, error_code: str) -> None:
        async with self._factory() as session:
            async with session.begin():
                row = await self._locked(session, message_id)
                row.attempts += 1
                row.available_at = available_at
                row.claimed_at = None
                row.last_error_code = error_code[:64]

    async def mark_dead(self, message_id: str, *, error_code: str) -> None:
        async with self._factory() as session:
            async with session.begin():
                row = await self._locked(session, message_id)
                row.attempts += 1
                row.dead_at = datetime.now(UTC)
                row.claimed_at = None
                row.last_error_code = error_code[:64]

    @staticmethod
    async def _locked(session: AsyncSession, message_id: str) -> OutboxMessage:
        try:
            identifier = UUID(message_id)
        except ValueError as exc:
            raise LookupError(message_id) from exc
        row = (
            await session.execute(
                select(OutboxMessage)
                .where(
                    OutboxMessage.id == identifier,
                    OutboxMessage.delivered_at.is_(None),
                    OutboxMessage.dead_at.is_(None),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            raise LookupError(message_id)
        return row


class PostgresJoinLinkResolver:
    def __init__(self, factory: async_sessionmaker[AsyncSession], codec: LessonLinkCodec) -> None:
        self._factory = factory
        self._codec = codec

    async def resolve(self, opaque_reference: str) -> str | None:
        match = _REF.fullmatch(opaque_reference)
        if match is None:
            return None
        kind, raw_id = match.group(1).lower(), match.group(2)
        lesson_id = UUID(raw_id)
        async with self._factory() as session:
            model = RuleLesson if kind == "rule" else OverrideLesson
            value = (
                await session.execute(
                    select(model.join_url_ciphertext).where(
                        model.id == lesson_id,
                        model.join_url_ciphertext.is_not(None),
                    )
                )
            ).scalar_one_or_none()
        if value is None:
            return None
        try:
            return self._codec.decrypt(value)
        except Exception:
            return None


class PostgresWorkerHeartbeatRepository:
    """Small restart-safe upsert adapter for worker liveness reporting."""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    async def upsert(
        self,
        *,
        worker_name: str,
        instance_id: str,
        status: str,
        heartbeat_at: datetime,
        details: dict[str, object] | None = None,
    ) -> None:
        if not worker_name.strip() or not instance_id.strip() or not status.strip():
            raise ValueError("heartbeat identity and status must not be empty")
        if heartbeat_at.tzinfo is None:
            raise ValueError("heartbeat time must be timezone-aware")
        values = {
            "worker_name": worker_name[:80],
            "instance_id": instance_id[:120],
            "status": status[:24],
            "heartbeat_at": heartbeat_at,
            "details": dict(details or {}),
        }
        statement = insert(WorkerHeartbeat).values(**values)
        statement = statement.on_conflict_do_update(
            index_elements=[WorkerHeartbeat.worker_name],
            set_={key: value for key, value in values.items() if key != "worker_name"},
        )
        async with self._factory() as session:
            async with session.begin():
                await session.execute(statement)

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from time_tl.application.schedule_worker import (
    RETURN_TO_LESSON_MAX_AGE,
    ConfirmedClear,
    ConfirmedClearPage,
    PlannedOutboxMessage,
    ScheduleTarget,
)
from time_tl.application.sender_worker import ClaimedOutboxMessage, DeliveryRecipient
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
    RuntimeSetting,
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
_SENDER_COOLDOWN_WORKER_NAME = "sender-global-cooldown"
TERMINAL_OUTBOX_RETENTION = timedelta(days=90)
MAX_TERMINAL_OUTBOX_PRUNE_BATCH = 100

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


def _scope_binding_matches(payload: dict[str, object], scope: AlertScope) -> bool:
    binding = payload.get("scope_binding")
    provider = (scope.territory_provider or "").strip()
    catalog_version = (scope.territory_catalog_version or "").strip()
    if not provider or not catalog_version or not isinstance(binding, dict):
        return False
    expected: dict[str, object] = {
        "provider": provider,
        "catalog_version": catalog_version,
        "selected_region_id": scope.selected_region_id,
        "applicable_region_ids": sorted(set(scope.applicable_region_ids)),
    }
    return binding == expected


def _delivery_deadline(row: OutboxMessage) -> datetime | None:
    key = {
        "LESSON_REMINDER": "starts_at",
        "RETURN_TO_ACTIVE_LESSON": "valid_until",
    }.get(row.event_type)
    if key is None:
        return None
    raw = row.payload.get(key)
    if not isinstance(raw, str):
        return None
    try:
        value = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return value if value.tzinfo is not None else None


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

    async def list_schedule_targets_page(
        self,
        now: datetime,
        *,
        after_user_id: str | None,
        limit: int,
    ) -> tuple[ScheduleTarget, ...]:
        """Return one deterministic keyset page of active schedule targets."""
        if now.tzinfo is None:
            raise ValueError("worker clock must be timezone-aware")
        if not 1 <= limit <= 500:
            raise ValueError("schedule page limit must be in range 1..500")
        try:
            cursor = UUID(after_user_id) if after_user_id is not None else None
        except ValueError as exc:
            raise ValueError("invalid schedule target cursor") from exc
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
                .limit(limit)
            )
            if cursor is not None:
                statement = statement.where(User.id > cursor)
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
        """Compatibility collector; the production worker uses bounded pages."""
        result: list[ConfirmedClear] = []
        cursor: tuple[datetime, str] | None = None
        while True:
            page = await self.list_confirmed_clears_page(
                now,
                after_confirmed_at=(cursor[0] if cursor else None),
                after_message_id=(cursor[1] if cursor else None),
                limit=100,
            )
            result.extend(page.items)
            if page.next_confirmed_at is None or page.next_message_id is None:
                return tuple(result)
            cursor = (page.next_confirmed_at, page.next_message_id)

    async def list_confirmed_clears_page(
        self,
        now: datetime,
        *,
        after_confirmed_at: datetime | None,
        after_message_id: str | None,
        limit: int,
    ) -> ConfirmedClearPage:
        if now.tzinfo is None:
            raise ValueError("worker clock must be timezone-aware")
        if not 1 <= limit <= 500:
            raise ValueError("confirmed-clear page limit must be in range 1..500")
        if (after_confirmed_at is None) is not (after_message_id is None):
            raise ValueError("confirmed-clear cursor must be complete")
        if after_confirmed_at is not None and after_confirmed_at.tzinfo is None:
            raise ValueError("confirmed-clear cursor time must be timezone-aware")
        try:
            cursor_id = UUID(after_message_id) if after_message_id is not None else None
        except ValueError as exc:
            raise ValueError("invalid confirmed-clear cursor") from exc

        cutoff = now - RETURN_TO_LESSON_MAX_AGE
        async with self._factory() as session:
            poll = await session.get(PollRow, 1)
            if not _provider_is_fresh(
                poll.last_success_at if poll is not None else None,
                now,
                self._stale_after,
            ):
                return ConfirmedClearPage((), None, None)
            statement = (
                select(OutboxMessage)
                .where(
                    OutboxMessage.event_type == "ALL_CLEAR_CONFIRMED",
                    OutboxMessage.created_at >= cutoff,
                    OutboxMessage.created_at <= now,
                )
                .order_by(OutboxMessage.created_at, OutboxMessage.id)
                .limit(limit)
            )
            if after_confirmed_at is not None and cursor_id is not None:
                statement = statement.where(
                    or_(
                        OutboxMessage.created_at > after_confirmed_at,
                        and_(
                            OutboxMessage.created_at == after_confirmed_at,
                            OutboxMessage.id > cursor_id,
                        ),
                    )
                )
            messages = (await session.execute(statement)).scalars().all()
            if not messages:
                return ConfirmedClearPage((), None, None)
            items: list[ConfirmedClear] = []
            for message in messages:
                clear = await self._load_confirmed_clear(session, message)
                if clear is not None:
                    items.append(clear)
            last = messages[-1]
            return ConfirmedClearPage(
                tuple(items),
                last.created_at,
                str(last.id),
            )

    async def _load_confirmed_clear(
        self,
        session: AsyncSession,
        message: OutboxMessage,
    ) -> ConfirmedClear | None:
        try:
            scope_id = UUID(message.aggregate_id)
        except ValueError:
            return None
        scope = await session.get(AlertScope, scope_id)
        if scope is None or not _scope_binding_matches(message.payload, scope):
            return None
        snapshot = await session.get(AlertSnapshot, scope.id)
        if snapshot is None or snapshot.state != AlertState.CLEAR_CONFIRMED.value:
            return None
        user = await session.get(User, scope.user_id)
        if user is None or user.mode != "LESSONS_AND_ALERTS":
            return None
        profile = (
            await session.execute(
                select(ScheduleProfile).where(
                    ScheduleProfile.user_id == user.id,
                    ScheduleProfile.is_active.is_(True),
                )
            )
        ).scalar_one_or_none()
        if profile is None:
            return None
        event_id = message.payload.get("event_id")
        binding = message.payload.get("scope_binding")
        if not isinstance(event_id, str) or not event_id or not isinstance(binding, dict):
            return None
        target = ScheduleTarget(
            user_id=str(user.id),
            telegram_chat_id=user.telegram_user_id,
            profile=await self._accounts._load_profile(session, profile),
            mode=NotificationMode.LESSONS_AND_ALERTS,
            reminder_lead_minutes=user.reminder_lead_minutes,
            alert_state=AlertState.CLEAR_CONFIRMED,
            profile_revision=profile.version,
        )
        return ConfirmedClear(
            event_id,
            message.created_at,
            target,
            scope_binding=dict(binding),
        )

    async def enqueue_deduplicated(self, messages: tuple[PlannedOutboxMessage, ...]) -> int:
        if any(message.available_at.tzinfo is None for message in messages):
            raise ValueError("outbox time must be timezone-aware")
        async with self._factory() as session:
            async with session.begin():
                inserted = 0
                owner_exists: dict[UUID, bool] = {}
                for message in messages:
                    self._validate_payload(message.payload)
                    try:
                        owner_id = UUID(message.aggregate_id)
                    except ValueError as exc:
                        raise ValueError("invalid schedule aggregate id") from exc
                    if owner_id not in owner_exists:
                        owner_exists[owner_id] = (
                            await session.execute(
                                select(User.id).where(User.id == owner_id).with_for_update()
                            )
                        ).scalar_one_or_none() is not None
                    if not owner_exists[owner_id]:
                        continue
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
    _TRANSIENT_RECHECK_DELAY = timedelta(seconds=15)

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        *,
        visibility_timeout: timedelta = timedelta(minutes=5),
        stale_after_seconds: int,
        expected_owner_config_revision: int | None = None,
        expected_staging_allowlist: tuple[int, ...] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if stale_after_seconds < 1:
            raise ValueError("stale threshold must be positive")
        if expected_owner_config_revision is not None and expected_owner_config_revision < 1:
            raise ValueError("owner configuration revision must be positive")
        if expected_owner_config_revision is None and expected_staging_allowlist is not None:
            raise ValueError("staging allowlist requires an owner configuration revision")
        if expected_staging_allowlist is not None and (
            not expected_staging_allowlist
            or any(item <= 0 for item in expected_staging_allowlist)
            or len(set(expected_staging_allowlist)) != len(expected_staging_allowlist)
        ):
            raise ValueError("staging allowlist must contain unique positive chat IDs")
        self._factory = factory
        self._visibility_timeout = visibility_timeout
        self._stale_after = timedelta(seconds=stale_after_seconds)
        self._expected_owner_config_revision = expected_owner_config_revision
        self._expected_staging_allowlist = expected_staging_allowlist
        self._clock = clock or (lambda: datetime.now(UTC))

    def _current_time(self) -> datetime:
        current = self._clock()
        if current.tzinfo is None:
            raise ValueError("repository clock must be timezone-aware")
        return current

    async def _active_owner_config_authorizes(
        self,
        session: AsyncSession,
        *,
        expected_recipient: DeliveryRecipient | None = None,
        lock: bool,
    ) -> bool:
        """Pin delivery to the process' exact activated configuration.

        Unit-only repositories may explicitly pass ``None`` for the expected
        revision. Every live sender is wired with a positive revision. At the
        final boundary the state row is locked until after Telegram I/O, so a
        new activation cannot commit between this check and the send.
        """

        revision = self._expected_owner_config_revision
        if revision is None:
            return True
        state_query = select(RuntimeSetting).where(
            RuntimeSetting.key == "owner_configuration_state"
        )
        active_query = select(RuntimeSetting).where(
            RuntimeSetting.key == "owner_configuration_active"
        )
        if lock:
            state_query = state_query.with_for_update()
            active_query = active_query.with_for_update()
        state = (await session.execute(state_query)).scalar_one_or_none()
        active = (await session.execute(active_query)).scalar_one_or_none()
        if state is None or active is None:
            return False
        raw_state_revision = state.value.get("active_revision")
        raw_payload_revision = active.value.get("revision")
        if (
            not isinstance(raw_state_revision, int)
            or isinstance(raw_state_revision, bool)
            or not isinstance(raw_payload_revision, int)
            or isinstance(raw_payload_revision, bool)
        ):
            return False
        state_revision = raw_state_revision
        payload_revision = raw_payload_revision
        if state_revision != revision or payload_revision != revision:
            return False
        allowlist = self._expected_staging_allowlist
        if allowlist is None:
            return True
        raw_allowlist = active.value.get("staging_allowlist")
        if not isinstance(raw_allowlist, list) or any(
            not isinstance(item, int) or isinstance(item, bool) or item <= 0
            for item in raw_allowlist
        ):
            return False
        current_allowlist = tuple(raw_allowlist)
        if current_allowlist != allowlist:
            return False
        return expected_recipient is None or expected_recipient.chat_id in current_allowlist

    async def prune_terminal_outbox(
        self,
        now: datetime,
        *,
        retention: timedelta = TERMINAL_OUTBOX_RETENTION,
        limit: int = MAX_TERMINAL_OUTBOX_PRUNE_BATCH,
    ) -> int:
        """Delete one bounded batch of terminal rows older than 90 days.

        Pending, retryable, claimed and inconsistent dual-terminal rows are
        deliberately excluded. ``SKIP LOCKED`` keeps cleanup independent from
        delivery and account-deletion transactions.
        """

        if now.tzinfo is None or retention <= timedelta(0):
            raise ValueError("retention requires aware time and a positive duration")
        if not 1 <= limit <= MAX_TERMINAL_OUTBOX_PRUNE_BATCH:
            raise ValueError("retention limit must be between 1 and 100")
        cutoff = now - retention
        deleted = 0
        async with self._factory() as session:
            async with session.begin():
                for terminal_at, other_terminal_at in (
                    (OutboxMessage.delivered_at, OutboxMessage.dead_at),
                    (OutboxMessage.dead_at, OutboxMessage.delivered_at),
                ):
                    remaining = limit - deleted
                    if remaining <= 0:
                        break
                    rows = (
                        (
                            await session.execute(
                                select(OutboxMessage)
                                .where(
                                    OutboxMessage.claimed_at.is_(None),
                                    terminal_at.is_not(None),
                                    terminal_at <= cutoff,
                                    other_terminal_at.is_(None),
                                )
                                .order_by(terminal_at, OutboxMessage.id)
                                .limit(remaining)
                                .with_for_update(skip_locked=True)
                            )
                        )
                        .scalars()
                        .all()
                    )
                    for row in rows:
                        await session.delete(row)
                    deleted += len(rows)
        return deleted

    @staticmethod
    async def _lock_sender_cooldown(session: AsyncSession, now: datetime) -> WorkerHeartbeat:
        statement = (
            insert(WorkerHeartbeat)
            .values(
                worker_name=_SENDER_COOLDOWN_WORKER_NAME,
                instance_id="shared",
                status="READY",
                heartbeat_at=now,
                details={},
            )
            .on_conflict_do_nothing(index_elements=[WorkerHeartbeat.worker_name])
        )
        await session.execute(statement)
        return (
            await session.execute(
                select(WorkerHeartbeat)
                .where(WorkerHeartbeat.worker_name == _SENDER_COOLDOWN_WORKER_NAME)
                .with_for_update()
            )
        ).scalar_one()

    @staticmethod
    def _sender_cooldown_until(row: WorkerHeartbeat) -> datetime | None:
        raw = row.details.get("cooldown_until")
        if not isinstance(raw, str):
            return None
        try:
            value = datetime.fromisoformat(raw)
        except ValueError:
            return None
        return value if value.tzinfo is not None else None

    @staticmethod
    async def _current_user_for_row(session: AsyncSession, row: OutboxMessage) -> User | None:
        try:
            aggregate_id = UUID(row.aggregate_id)
        except ValueError:
            return None
        if row.aggregate_type == "alert_scope":
            scope = await session.get(AlertScope, aggregate_id)
            return await session.get(User, scope.user_id) if scope is not None else None
        if row.aggregate_type == "schedule":
            return await session.get(User, aggregate_id)
        return None

    @staticmethod
    def _recipient_matches(
        expected: DeliveryRecipient, user: User, *, require_region: bool
    ) -> bool:
        if expected.chat_id != user.telegram_user_id:
            return False
        if not require_region:
            return True
        region_id = (user.confirmed_region_id or "").strip()
        region_name = (user.region_name or "").strip()
        return bool(
            region_id
            and region_name
            and expected.confirmed_region_id == region_id
            and expected.confirmed_region_name == region_name
        )

    async def claim_batch_skip_locked(
        self, now: datetime, *, limit: int
    ) -> tuple[ClaimedOutboxMessage, ...]:
        if now.tzinfo is None or limit < 1:
            raise ValueError("invalid claim parameters")
        async with self._factory() as session:
            async with session.begin():
                cooldown_row = await self._lock_sender_cooldown(session, now)
                cooldown_until = self._sender_cooldown_until(cooldown_row)
                if cooldown_until is not None and now < cooldown_until:
                    return ()
                if cooldown_until is not None:
                    cooldown_row.status = "READY"
                    cooldown_row.heartbeat_at = now
                    cooldown_row.details = {}
                if not await self._active_owner_config_authorizes(session, lock=False):
                    return ()
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
                deliverable: list[ClaimedOutboxMessage] = []
                for row in rows:
                    if await self._is_currently_deliverable(
                        session,
                        row,
                        authorization_at=now,
                        stale_after=self._stale_after,
                    ):
                        supported = row.event_type in {
                            "LESSON_REMINDER",
                            "RETURN_TO_ACTIVE_LESSON",
                            "ALERT_STARTED",
                            "ALL_CLEAR_CONFIRMED",
                        }
                        user = await self._current_user_for_row(session, row) if supported else None
                        require_region = row.event_type in {
                            "RETURN_TO_ACTIVE_LESSON",
                            "ALERT_STARTED",
                            "ALL_CLEAR_CONFIRMED",
                        }
                        region_id = (user.confirmed_region_id or "").strip() if user else ""
                        region_name = (user.region_name or "").strip() if user else ""
                        if supported and (
                            user is None or (require_region and not (region_id and region_name))
                        ):
                            row.dead_at = now
                            row.claimed_at = None
                            row.last_error_code = "delivery_recipient_unavailable"
                            continue
                        payload = dict(row.payload)
                        recipient = None
                        if user is not None:
                            payload["telegram_chat_id"] = user.telegram_user_id
                            if require_region:
                                payload["confirmed_region_id"] = region_id
                                payload["confirmed_region_name"] = region_name
                            recipient = DeliveryRecipient(
                                user.telegram_user_id,
                                region_id or None,
                                region_name or None,
                            )
                        row.claimed_at = now
                        deliverable.append(
                            ClaimedOutboxMessage(
                                str(row.id), row.event_type, payload, row.attempts, recipient
                            )
                        )
                    else:
                        if await self._should_defer_alert_event(
                            session,
                            row,
                            authorization_at=now,
                            stale_after=self._stale_after,
                        ):
                            self._defer_transient(row, now)
                        else:
                            row.dead_at = now
                            row.claimed_at = None
                            row.last_error_code = "delivery_policy_changed"
                return tuple(deliverable)

    @classmethod
    def _defer_transient(cls, row: OutboxMessage, at: datetime) -> None:
        row.dead_at = None
        row.claimed_at = None
        row.available_at = at + cls._TRANSIENT_RECHECK_DELAY
        row.last_error_code = "delivery_policy_temporarily_unavailable"

    @staticmethod
    async def _should_defer_alert_event(
        session: AsyncSession,
        row: OutboxMessage,
        *,
        authorization_at: datetime,
        stale_after: timedelta,
        lock_policy_rows: bool = False,
        expected_recipient: DeliveryRecipient | None = None,
    ) -> bool:
        if row.event_type == "RETURN_TO_ACTIVE_LESSON":
            if row.aggregate_type != "schedule":
                return False
            try:
                user_id = UUID(row.aggregate_id)
            except ValueError:
                return False
            raw_valid_until = row.payload.get("valid_until")
            if not isinstance(raw_valid_until, str):
                return False
            try:
                valid_until = datetime.fromisoformat(raw_valid_until)
            except ValueError:
                return False
            if valid_until.tzinfo is None or authorization_at >= valid_until:
                return False

            if lock_policy_rows:
                poll = (
                    await session.execute(
                        select(PollRow).where(PollRow.singleton_id == 1).with_for_update()
                    )
                ).scalar_one_or_none()
                user = (
                    await session.execute(select(User).where(User.id == user_id).with_for_update())
                ).scalar_one_or_none()
                scope_query = (
                    select(AlertScope).where(AlertScope.user_id == user_id).with_for_update()
                )
                scope = (await session.execute(scope_query)).scalar_one_or_none()
                snapshot = (
                    (
                        await session.execute(
                            select(AlertSnapshot)
                            .where(AlertSnapshot.scope_id == scope.id)
                            .with_for_update()
                        )
                    ).scalar_one_or_none()
                    if scope is not None
                    else None
                )
                profile = (
                    await session.execute(
                        select(ScheduleProfile)
                        .where(
                            ScheduleProfile.user_id == user_id,
                            ScheduleProfile.is_active.is_(True),
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
            else:
                poll = await session.get(PollRow, 1)
                user = await session.get(User, user_id)
                scope = (
                    await session.execute(select(AlertScope).where(AlertScope.user_id == user_id))
                ).scalar_one_or_none()
                snapshot = await session.get(AlertSnapshot, scope.id) if scope is not None else None
                profile = (
                    await session.execute(
                        select(ScheduleProfile).where(
                            ScheduleProfile.user_id == user_id,
                            ScheduleProfile.is_active.is_(True),
                        )
                    )
                ).scalar_one_or_none()

            revision = row.payload.get("profile_revision")
            if (
                user is None
                or user.mode != "LESSONS_AND_ALERTS"
                or scope is None
                or snapshot is None
                or profile is None
                or not isinstance(revision, int)
                or profile.version != revision
                or not (user.confirmed_region_id or "").strip()
                or not (user.region_name or "").strip()
                or not _scope_binding_matches(row.payload, scope)
            ):
                return False
            if lock_policy_rows and (
                expected_recipient is None
                or not PostgresSenderRepository._recipient_matches(
                    expected_recipient, user, require_region=True
                )
            ):
                return False
            if snapshot.state in {AlertState.STALE.value, AlertState.UNKNOWN.value}:
                return True
            if snapshot.state != AlertState.CLEAR_CONFIRMED.value:
                return False
            return not _provider_is_fresh(
                poll.last_success_at if poll is not None else None,
                authorization_at,
                stale_after,
            )

        if row.event_type not in {"ALERT_STARTED", "ALL_CLEAR_CONFIRMED"}:
            return False
        if row.aggregate_type != "alert_scope":
            return False
        try:
            scope_id = UUID(row.aggregate_id)
        except ValueError:
            return False

        if lock_policy_rows:
            poll = (
                await session.execute(
                    select(PollRow).where(PollRow.singleton_id == 1).with_for_update()
                )
            ).scalar_one_or_none()
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
                    select(AlertScope).where(AlertScope.id == scope_id).with_for_update()
                )
            ).scalar_one_or_none()
            snapshot = (
                await session.execute(
                    select(AlertSnapshot)
                    .where(AlertSnapshot.scope_id == scope_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
        else:
            poll = await session.get(PollRow, 1)
            scope = await session.get(AlertScope, scope_id)
            user = await session.get(User, scope.user_id) if scope is not None else None
            snapshot = await session.get(AlertSnapshot, scope_id)

        if (
            scope is None
            or user is None
            or snapshot is None
            or user.id != scope.user_id
            or user.mode not in {"LESSONS_AND_ALERTS", "ALERTS_ONLY"}
            or not (user.confirmed_region_id or "").strip()
            or not (user.region_name or "").strip()
            or not _scope_binding_matches(row.payload, scope)
        ):
            return False
        if lock_policy_rows and (
            expected_recipient is None
            or not PostgresSenderRepository._recipient_matches(
                expected_recipient, user, require_region=True
            )
        ):
            return False

        required = (
            AlertState.ACTIVE.value
            if row.event_type == "ALERT_STARTED"
            else AlertState.CLEAR_CONFIRMED.value
        )
        if snapshot.state in {AlertState.STALE.value, AlertState.UNKNOWN.value}:
            return True
        if snapshot.state != required:
            return False
        return not _provider_is_fresh(
            poll.last_success_at if poll is not None else None,
            authorization_at,
            stale_after,
        )

    @staticmethod
    async def _is_currently_deliverable(
        session: AsyncSession,
        row: OutboxMessage,
        *,
        authorization_at: datetime,
        stale_after: timedelta,
        lock_policy_rows: bool = False,
        expected_recipient: DeliveryRecipient | None = None,
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
                        select(PollRow).where(PollRow.singleton_id == 1).with_for_update()
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
                        select(AlertScope).where(AlertScope.id == scope_id).with_for_update()
                    )
                ).scalar_one_or_none()
            else:
                scope = await session.get(AlertScope, scope_id)
            if scope is None:
                return False
            if not _scope_binding_matches(row.payload, scope):
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
            if (
                user is None
                or snapshot is None
                or user.mode
                not in {
                    "LESSONS_AND_ALERTS",
                    "ALERTS_ONLY",
                }
            ):
                return False
            if lock_policy_rows and (
                expected_recipient is None
                or not PostgresSenderRepository._recipient_matches(
                    expected_recipient, user, require_region=True
                )
            ):
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
                    await session.execute(select(User).where(User.id == user_id).with_for_update())
                ).scalar_one_or_none()
            else:
                user = await session.get(User, user_id)
            if user is None or user.mode not in {"LESSONS_ONLY", "LESSONS_AND_ALERTS"}:
                return False
            if lock_policy_rows and (
                expected_recipient is None
                or not PostgresSenderRepository._recipient_matches(
                    expected_recipient,
                    user,
                    require_region=row.event_type == "RETURN_TO_ACTIVE_LESSON",
                )
            ):
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
                if (
                    scope is None
                    or snapshot is None
                    or snapshot.state != AlertState.CLEAR_CONFIRMED.value
                ):
                    return False
                if not _scope_binding_matches(row.payload, scope):
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
            deadline = _delivery_deadline(row)
            if deadline is None or authorization_at >= deadline:
                return False
            return True
        return True

    async def deliver_if_authorized(
        self,
        message_id: str,
        delivered_at: datetime,
        deliver: Callable[[], Awaitable[None]],
        *,
        expected_recipient: DeliveryRecipient,
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
                if not await self._active_owner_config_authorizes(
                    session,
                    expected_recipient=expected_recipient,
                    lock=True,
                ):
                    self._defer_transient(row, authorization_at)
                    return False
                if not await self._is_currently_deliverable(
                    session,
                    row,
                    authorization_at=authorization_at,
                    stale_after=self._stale_after,
                    lock_policy_rows=True,
                    expected_recipient=expected_recipient,
                ):
                    if await self._should_defer_alert_event(
                        session,
                        row,
                        authorization_at=authorization_at,
                        stale_after=self._stale_after,
                        lock_policy_rows=True,
                        expected_recipient=expected_recipient,
                    ):
                        self._defer_transient(row, authorization_at)
                    else:
                        row.dead_at = authorization_at
                        row.claimed_at = None
                        row.last_error_code = "final_delivery_policy_changed"
                    return False
                valid_until = _delivery_deadline(row)
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
                        lesson_user is not None and lesson_user.mode == "LESSONS_AND_ALERTS"
                    )
                locked_poll = await session.get(PollRow, 1) if requires_fresh_provider else None
                # Read the clock only after all policy rows are locked. For a
                # return-to-lesson message this is the exact no-send boundary:
                # equality with valid_until is expired.
                send_at = self._current_time()
                if valid_until is None and row.event_type in {
                    "LESSON_REMINDER",
                    "RETURN_TO_ACTIVE_LESSON",
                }:
                    row.dead_at = send_at
                    row.claimed_at = None
                    row.last_error_code = "final_delivery_invalid_expiry"
                    return False
                if valid_until is not None and send_at >= valid_until:
                    row.dead_at = send_at
                    row.claimed_at = None
                    row.last_error_code = "final_delivery_expired"
                    return False
                if requires_fresh_provider and not _provider_is_fresh(
                    locked_poll.last_success_at if locked_poll is not None else None,
                    send_at,
                    self._stale_after,
                ):
                    if row.event_type in {
                        "ALERT_STARTED",
                        "ALL_CLEAR_CONFIRMED",
                        "RETURN_TO_ACTIVE_LESSON",
                    }:
                        self._defer_transient(row, send_at)
                    else:
                        row.dead_at = send_at
                        row.claimed_at = None
                        row.last_error_code = "final_delivery_policy_changed"
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
                if error_code == "telegram_429":
                    cooldown_row = await self._lock_sender_cooldown(session, available_at)
                    current = self._sender_cooldown_until(cooldown_row)
                    cooldown_until = max(available_at, current or available_at)
                    cooldown_row.status = "COOLDOWN"
                    cooldown_row.heartbeat_at = self._current_time()
                    cooldown_row.details = {
                        "cooldown_until": cooldown_until.isoformat(),
                    }
                row.attempts += 1
                row.available_at = available_at
                row.claimed_at = None
                row.last_error_code = error_code[:64]

    async def release_unattempted(
        self,
        message_ids: tuple[str, ...],
        *,
        claimed_at: datetime,
        available_at: datetime,
        error_code: str,
    ) -> None:
        """Release an unsent claimed tail without spending delivery attempts."""
        if claimed_at.tzinfo is None or available_at.tzinfo is None:
            raise ValueError("release times must be timezone-aware")
        identifiers: list[UUID] = []
        for message_id in dict.fromkeys(message_ids):
            try:
                identifiers.append(UUID(message_id))
            except ValueError as exc:
                raise LookupError(message_id) from exc
        if not identifiers:
            return
        async with self._factory() as session:
            async with session.begin():
                rows = (
                    (
                        await session.execute(
                            select(OutboxMessage)
                            .where(
                                OutboxMessage.id.in_(identifiers),
                                OutboxMessage.delivered_at.is_(None),
                                OutboxMessage.dead_at.is_(None),
                                OutboxMessage.claimed_at == claimed_at,
                            )
                            .with_for_update()
                        )
                    )
                    .scalars()
                    .all()
                )
                for row in rows:
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

"""Independent power persistence and final transactional notification policy.

Publication never waits for sender-owned outbox/event rows while holding provider
locks: stale claims are logically invalidated by the final revision check, and
physical cancellation uses SKIP LOCKED. User mutations and planning take one
user/subscription at a time before the provider. Telegram keeps these policy
locks until its bounded transport operation completes.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from time_tl.application.sender_worker import DeliveryRecipient
from time_tl.domain.power_outages.models import (
    PowerDaySchedule,
    PowerInterval,
    PowerProvider,
    PowerPublicationStatus,
    PowerScheduleSnapshot,
    PowerScheduleView,
    PowerState,
    PowerSubscription,
    ScheduleKind,
    SourceStatus,
    aware,
    validate_group,
)

from .models import (
    AuditLog,
    OutboxMessage,
    PowerNotificationEvent,
    PowerProviderState,
    PowerScheduleIntervalRow,
    PowerScheduleSnapshotRow,
    User,
    UserPowerSubscription,
)

POWER_EVENT_TYPES = frozenset(
    {
        "POWER_OUTAGE_UPCOMING",
        "POWER_RESTORATION_UPCOMING",
        "POWER_SCHEDULE_CHANGED",
    }
)
KYIV = ZoneInfo("Europe/Kyiv")


def _fresh(stamp: datetime | None, now: datetime, seconds: int) -> bool:
    return stamp is not None and timedelta(0) <= aware(now) - aware(stamp) <= timedelta(
        seconds=seconds
    )


def _subscription(row: UserPowerSubscription) -> PowerSubscription:
    return PowerSubscription(
        str(row.user_id),
        PowerProvider(row.provider_id),
        row.group_id,
        row.enabled,
        row.outage_notifications_enabled,
        row.restoration_notifications_enabled,
        row.outage_lead_minutes,
        row.restoration_lead_minutes,
        row.revision,
    )


def _timestamp(raw: object) -> datetime | None:
    if type(raw) is not str:
        return None
    try:
        return aware(datetime.fromisoformat(raw))
    except ValueError:
        return None


def _windows(schedule: PowerDaySchedule, now: datetime) -> list[list[str]]:
    return [
        [aware(item.starts_at).isoformat(), aware(item.ends_at).isoformat()]
        for item in schedule.intervals
        if item.state is PowerState.OUTAGE_CONFIRMED and aware(item.ends_at) > aware(now)
    ]


def _joined_windows(schedules: tuple[PowerDaySchedule, ...], now: datetime) -> list[list[str]]:
    """Join an outage through midnight without inventing an intermediate restore."""
    intervals = sorted(
        (
            item
            for day in schedules
            for item in day.intervals
            if item.state is PowerState.OUTAGE_CONFIRMED
        ),
        key=lambda item: aware(item.starts_at),
    )
    joined: list[list[datetime]] = []
    for item in intervals:
        start, end = aware(item.starts_at), aware(item.ends_at)
        if joined and joined[-1][1] == start:
            joined[-1][1] = end
        else:
            joined.append([start, end])
    return [[start.isoformat(), end.isoformat()] for start, end in joined if end > aware(now)]


def _future_windows(raw: object, now: datetime) -> list[list[str]] | None:
    if type(raw) is not list or len(raw) > 2688:
        return None
    result: list[list[str]] = []
    for item in raw:
        if type(item) is not list or len(item) != 2:
            return None
        start, end = _timestamp(item[0]), _timestamp(item[1])
        if start is None or end is None or end <= start:
            return None
        if end > aware(now):
            result.append([start.isoformat(), end.isoformat()])
    return result


def _known_coverage(days: tuple[PowerDaySchedule, ...], windows: object, now: datetime) -> bool:
    targets = _future_windows(windows, now)
    if not targets:
        return False
    known = sorted(
        (aware(item.starts_at), aware(item.ends_at))
        for day in days
        if day.publication_status is PowerPublicationStatus.PLANNED
        for item in day.intervals
        if item.state in (PowerState.OUTAGE_CONFIRMED, PowerState.POWER_EXPECTED)
    )
    for raw_start, raw_end in targets:
        target_start, stop = _timestamp(raw_start), _timestamp(raw_end)
        if target_start is None or stop is None:
            return False
        cursor = max(aware(now), target_start)
        for start, end in known:
            if start > cursor:
                break
            if end > cursor:
                cursor = end
            if cursor >= stop:
                break
        if cursor < stop:
            return False
    return True


class PostgresPowerRepository:
    def __init__(
        self, factory: async_sessionmaker[AsyncSession], *, stale_after_seconds: int = 1800
    ) -> None:
        if type(stale_after_seconds) is not int or stale_after_seconds < 1:
            raise ValueError("invalid_power_stale_threshold")
        self._factory = factory
        self.stale_after_seconds = stale_after_seconds

    async def initialize(self) -> None:
        async with self._factory() as session, session.begin():
            await self._seed(session)

    @staticmethod
    async def _seed(session: AsyncSession) -> None:
        for provider in PowerProvider:
            await session.execute(
                insert(PowerProviderState)
                .values(
                    provider_id=provider.value,
                    name=provider.label,
                    official_url=provider.official_url,
                    enabled=False,
                    source_status="UNAVAILABLE",
                    configuration_revision=1,
                    consecutive_failures=0,
                    refresh_requested=False,
                    last_error_code="SOURCE_GATE_BLOCKED",
                )
                .on_conflict_do_nothing(index_elements=[PowerProviderState.provider_id])
            )

    async def get_subscription(self, user_id: str) -> PowerSubscription | None:
        identity = UUID(user_id)
        async with self._factory() as session:
            row = await session.get(UserPowerSubscription, identity)
            return _subscription(row) if row is not None else None

    async def save_subscription(
        self, subscription: PowerSubscription, expected_revision: int | None = None
    ) -> PowerSubscription:
        identity = UUID(subscription.user_id)
        async with self._factory() as session, session.begin():
            await self._seed(session)
            user = (
                await session.execute(select(User).where(User.id == identity).with_for_update())
            ).scalar_one_or_none()
            if user is None:
                raise LookupError("power_user_unavailable")
            row = (
                await session.execute(
                    select(UserPowerSubscription)
                    .where(UserPowerSubscription.user_id == identity)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None:
                if expected_revision not in (None, 0):
                    raise ValueError("power_subscription_conflict")
                row = UserPowerSubscription(user_id=identity, revision=1)
                session.add(row)
            else:
                if expected_revision is None or row.revision != expected_revision:
                    raise ValueError("power_subscription_conflict")
                if _subscription(row) == subscription:
                    return _subscription(row)
                row.revision += 1
            row.provider_id = subscription.provider.value
            row.group_id = subscription.group_id
            row.enabled = subscription.enabled
            row.outage_notifications_enabled = subscription.outage_notifications_enabled
            row.restoration_notifications_enabled = subscription.restoration_notifications_enabled
            row.outage_lead_minutes = subscription.outage_lead_minutes
            row.restoration_lead_minutes = subscription.restoration_lead_minutes
            await self._cancel_pending(session, user_id=identity)
            await session.flush()
            return _subscription(row)

    async def read_schedule(
        self,
        provider: PowerProvider,
        group_id: str,
        day: date,
        schedule_type: ScheduleKind = ScheduleKind.CONFIRMED_DAY,
        *,
        now: datetime,
    ) -> PowerScheduleView:
        validate_group(group_id)
        aware(now)
        async with self._factory() as session:
            state = await session.get(PowerProviderState, provider.value)
            row = (
                await session.execute(
                    select(PowerScheduleSnapshotRow)
                    .where(
                        PowerScheduleSnapshotRow.provider_id == provider.value,
                        PowerScheduleSnapshotRow.group_id == group_id,
                        PowerScheduleSnapshotRow.valid_for_date == day,
                        PowerScheduleSnapshotRow.schedule_type == schedule_type.value,
                    )
                    .order_by(PowerScheduleSnapshotRow.revision.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            status = self._status(state, now)
            if row is not None and (
                not row.is_current or not _fresh(row.fetched_at, now, self.stale_after_seconds)
            ):
                status = SourceStatus.STALE
            return PowerScheduleView(
                provider,
                group_id,
                status,
                state.last_success_at if state else None,
                state.last_attempt_at if state else None,
                await self._day(session, row) if row is not None else None,
                row.revision if row else None,
                state.last_error_code if state else "SOURCE_GATE_BLOCKED",
            )

    def _status(self, row: PowerProviderState | None, now: datetime) -> SourceStatus:
        if row is None or not row.enabled:
            return SourceStatus.UNAVAILABLE
        status = SourceStatus(row.source_status)
        if status is SourceStatus.FRESH and not _fresh(
            row.last_success_at, now, self.stale_after_seconds
        ):
            return SourceStatus.STALE
        return status

    @staticmethod
    async def _day(session: AsyncSession, row: PowerScheduleSnapshotRow) -> PowerDaySchedule:
        intervals = (
            (
                await session.execute(
                    select(PowerScheduleIntervalRow)
                    .where(PowerScheduleIntervalRow.snapshot_id == row.id)
                    .order_by(PowerScheduleIntervalRow.starts_at)
                )
            )
            .scalars()
            .all()
        )
        return PowerDaySchedule(
            PowerProvider(row.provider_id),
            row.group_id,
            row.valid_for_date,
            ScheduleKind(row.schedule_type),
            tuple(
                PowerInterval(item.group_id, item.starts_at, item.ends_at, PowerState(item.state))
                for item in intervals
            ),
            row.mapping_version,
            publication_status=PowerPublicationStatus(row.publication_status),
        )

    async def acquire_poll_slot(
        self, provider: PowerProvider, now: datetime, interval_seconds: int = 300
    ) -> int | None:
        aware(now)
        if type(interval_seconds) is not int or interval_seconds < 1:
            raise ValueError("invalid_power_poll_interval")
        async with self._factory() as session, session.begin():
            await self._seed(session)
            row = (
                await session.execute(
                    select(PowerProviderState)
                    .where(PowerProviderState.provider_id == provider.value)
                    .with_for_update()
                )
            ).scalar_one()
            if not row.enabled or (row.next_attempt_at is not None and now < row.next_attempt_at):
                return None
            row.last_attempt_at = now
            # Reserve before the network call; a crash cannot create a restart burst.
            row.next_attempt_at = now + timedelta(seconds=interval_seconds)
            row.refresh_requested = False
            return row.configuration_revision

    async def store_snapshot(
        self,
        snapshot: PowerScheduleSnapshot,
        now: datetime,
        interval_seconds: int = 300,
        *,
        expected_configuration_revision: int | None = None,
        retry_after_seconds: float | None = None,
        permanent: bool = False,
    ) -> None:
        aware(now)
        if getattr(snapshot, "source_kind", "official") != "official":
            raise ValueError("nonofficial_power_snapshot_cannot_replace_official")
        async with self._factory() as session, session.begin():
            await self._seed(session)
            provider = (
                await session.execute(
                    select(PowerProviderState)
                    .where(PowerProviderState.provider_id == snapshot.provider.value)
                    .with_for_update()
                )
            ).scalar_one()
            if not provider.enabled or (
                expected_configuration_revision is not None
                and provider.configuration_revision != expected_configuration_revision
            ):
                return
            if snapshot.status is not SourceStatus.FRESH:
                provider.consecutive_failures += 1
                provider.source_status = snapshot.status.value
                provider.last_error_code = snapshot.error_code or "FETCH_UNAVAILABLE"
                delay = max(
                    interval_seconds,
                    min(86400, 60 * 2 ** min(provider.consecutive_failures - 1, 10)),
                    retry_after_seconds or 0,
                )
                provider.next_attempt_at = now + timedelta(seconds=delay)
                if permanent:
                    # Operator action is required to close an access/policy circuit.
                    provider.enabled = False
                    provider.configuration_revision += 1
                    session.add(
                        AuditLog(
                            actor="power-worker",
                            action="power.circuit_open",
                            result="BLOCKED",
                            target_type="power_provider",
                            target_id=provider.provider_id,
                            redacted_details={"code": provider.last_error_code},
                        )
                    )
                return
            if aware(snapshot.fetched_at) > aware(now):
                raise ValueError("future_power_snapshot")
            provider.source_status = "FRESH"
            provider.last_success_at = snapshot.fetched_at
            provider.last_error_code = None
            provider.consecutive_failures = 0
            provider.next_attempt_at = now + timedelta(seconds=interval_seconds)
            rows = (
                (
                    await session.execute(
                        select(PowerScheduleSnapshotRow)
                        .where(
                            PowerScheduleSnapshotRow.provider_id == snapshot.provider.value,
                            PowerScheduleSnapshotRow.is_current.is_(True),
                        )
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            current = {
                (item.group_id, item.valid_for_date, item.schedule_type): item for item in rows
            }
            seen: set[tuple[str, date, str]] = set()
            for day in snapshot.days:
                key = day.group_id, day.local_date, day.schedule_type.value
                seen.add(key)
                prior = current.get(key)
                if prior is not None and prior.content_hash == day.content_hash:
                    prior.fetched_at = snapshot.fetched_at
                    prior.source_updated_at = snapshot.source_updated_at
                    continue
                if prior is not None:
                    prior.is_current = False
                    await self._cancel_pending(session, snapshot_id=prior.id)
                maximum = (
                    await session.execute(
                        select(func.max(PowerScheduleSnapshotRow.revision)).where(
                            PowerScheduleSnapshotRow.provider_id == snapshot.provider.value,
                            PowerScheduleSnapshotRow.group_id == day.group_id,
                            PowerScheduleSnapshotRow.valid_for_date == day.local_date,
                            PowerScheduleSnapshotRow.schedule_type == day.schedule_type.value,
                        )
                    )
                ).scalar_one()
                await session.flush()  # release old partial-unique current key before insert
                row = PowerScheduleSnapshotRow(
                    id=uuid4(),
                    provider_id=snapshot.provider.value,
                    group_id=day.group_id,
                    valid_for_date=day.local_date,
                    schedule_type=day.schedule_type.value,
                    mapping_version=day.mapping_version,
                    revision=(maximum or 0) + 1,
                    content_hash=day.content_hash,
                    source_updated_at=snapshot.source_updated_at,
                    fetched_at=snapshot.fetched_at,
                    source_kind="official",
                    publication_status=day.publication_status.value,
                    schema_version=1,
                    is_current=True,
                )
                session.add(row)
                await session.flush()
                for item in day.intervals:
                    session.add(
                        PowerScheduleIntervalRow(
                            id=uuid4(),
                            snapshot_id=row.id,
                            group_id=day.group_id,
                            starts_at=item.starts_at,
                            ends_at=item.ends_at,
                            state=item.state.value,
                        )
                    )
            for key, old in current.items():
                if key not in seen:
                    # Published deletion/partial coverage never reuses an old fresh day.
                    old.is_current = False
                    await self._cancel_pending(session, snapshot_id=old.id)

    @staticmethod
    async def _cancel_pending(
        session: AsyncSession, *, user_id: UUID | None = None, snapshot_id: UUID | None = None
    ) -> None:
        query = select(PowerNotificationEvent).where(PowerNotificationEvent.status == "PENDING")
        if user_id is not None:
            query = query.where(PowerNotificationEvent.user_id == user_id)
        if snapshot_id is not None:
            query = query.where(PowerNotificationEvent.snapshot_id == snapshot_id)
        events = (
            (
                await session.execute(
                    query.order_by(PowerNotificationEvent.id)
                    .limit(500)
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )
        for event in events:
            event.status = "CANCELED"
            if event.outbox_id is None:
                continue
            outbox = (
                await session.execute(
                    select(OutboxMessage)
                    .where(
                        OutboxMessage.id == event.outbox_id,
                        OutboxMessage.delivered_at.is_(None),
                        OutboxMessage.dead_at.is_(None),
                        OutboxMessage.dispatch_started_at.is_(None),
                        OutboxMessage.claimed_at.is_(None),
                    )
                    .with_for_update(skip_locked=True)
                )
            ).scalar_one_or_none()
            if outbox is not None:
                outbox.dead_at = datetime.now(UTC)
                outbox.claimed_at = None
                outbox.last_error_code = "power_policy_changed"

    async def request_refresh(
        self, provider: PowerProvider, now: datetime, actor_id: str = "owner"
    ) -> bool:
        aware(now)
        async with self._factory() as session, session.begin():
            await self._seed(session)
            row = (
                await session.execute(
                    select(PowerProviderState)
                    .where(PowerProviderState.provider_id == provider.value)
                    .with_for_update()
                )
            ).scalar_one()
            if not row.enabled or row.refresh_requested:
                return False
            row.refresh_requested = True
            # Never pull next_attempt_at earlier; the same worker/lease/cooldown handles it.
            session.add(
                AuditLog(
                    actor=str(actor_id)[:80],
                    action="power.refresh_requested",
                    result="ACCEPTED",
                    target_type="power_provider",
                    target_id=provider.value,
                    redacted_details={},
                )
            )
            return True

    async def set_provider_enabled(
        self,
        provider: PowerProvider,
        enabled: bool,
        expected_revision: int,
        actor_id: str,
        now: datetime,
    ) -> dict[str, object]:
        aware(now)
        if type(enabled) is not bool or type(expected_revision) is not int:
            raise ValueError("invalid_power_provider_setting")
        async with self._factory() as session, session.begin():
            await self._seed(session)
            row = (
                await session.execute(
                    select(PowerProviderState)
                    .where(PowerProviderState.provider_id == provider.value)
                    .with_for_update()
                )
            ).scalar_one()
            if row.configuration_revision != expected_revision:
                raise ValueError("power_provider_conflict")
            row.enabled = enabled
            row.configuration_revision += 1
            row.source_status = "UNAVAILABLE"
            row.last_error_code = "AWAITING_SOURCE_VALIDATION" if enabled else "SOURCE_DISABLED"
            row.refresh_requested = False
            session.add(
                AuditLog(
                    actor=str(actor_id)[:80],
                    action="power.provider_enabled",
                    result="SUCCESS",
                    target_type="power_provider",
                    target_id=provider.value,
                    redacted_details={"enabled": enabled, "revision": row.configuration_revision},
                )
            )
            return {"revision": row.configuration_revision, "enabled": enabled}

    async def provider_states(self, now: datetime) -> tuple[dict[str, object], ...]:
        aware(now)
        async with self._factory() as session, session.begin():
            await self._seed(session)
            result: list[dict[str, object]] = []
            for provider in PowerProvider:
                state = await session.get(PowerProviderState, provider.value)
                assert state is not None
                schedules = (
                    (
                        await session.execute(
                            select(PowerScheduleSnapshotRow).where(
                                PowerScheduleSnapshotRow.provider_id == provider.value,
                                PowerScheduleSnapshotRow.is_current.is_(True),
                                PowerScheduleSnapshotRow.fetched_at
                                >= now - timedelta(seconds=self.stale_after_seconds),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                subscribers = (
                    await session.execute(
                        select(func.count())
                        .select_from(UserPowerSubscription)
                        .where(UserPowerSubscription.provider_id == provider.value)
                    )
                ).scalar_one()
                pending = (
                    await session.execute(
                        select(func.count())
                        .select_from(PowerNotificationEvent)
                        .join(PowerScheduleSnapshotRow)
                        .where(
                            PowerScheduleSnapshotRow.provider_id == provider.value,
                            PowerNotificationEvent.status == "PENDING",
                        )
                    )
                ).scalar_one()
                result.append(
                    {
                        "provider_id": provider.value,
                        "enabled": state.enabled,
                        "source_status": self._status(state, now).value,
                        "last_success_at": state.last_success_at,
                        "last_attempt_at": state.last_attempt_at,
                        "last_error_code": state.last_error_code,
                        "group_count": len({item.group_id for item in schedules}),
                        "schedule_count": len(schedules),
                        "subscriber_count": int(subscribers),
                        "pending_notifications": int(pending),
                        "revision": state.configuration_revision,
                    }
                )
            return tuple(result)

    async def plan_notifications(self, now: datetime, *, limit: int = 100) -> int:
        aware(now)
        if not 1 <= limit <= 1000:
            raise ValueError("invalid_power_planning_limit")
        total = 0
        cursor: UUID | None = None
        while True:
            async with self._factory() as session:
                query = (
                    select(UserPowerSubscription.user_id)
                    .where(UserPowerSubscription.enabled.is_(True))
                    .order_by(UserPowerSubscription.user_id)
                    .limit(limit)
                )
                if cursor is not None:
                    query = query.where(UserPowerSubscription.user_id > cursor)
                users = (await session.execute(query)).scalars().all()
            if not users:
                return total
            for user_id in users:
                total += await self._plan_user(user_id, now)
            if cursor == users[-1]:
                raise RuntimeError("power_subscription_cursor_did_not_advance")
            cursor = users[-1]

    async def _plan_user(self, user_id: UUID, now: datetime) -> int:
        from time_tl.domain.power_outages.decisions import upcoming_transitions

        async with self._factory() as session, session.begin():
            user = (
                await session.execute(select(User).where(User.id == user_id).with_for_update())
            ).scalar_one_or_none()
            if user is None:
                return 0
            row = (
                await session.execute(
                    select(UserPowerSubscription)
                    .where(UserPowerSubscription.user_id == user_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None or not row.enabled:
                return 0
            provider = (
                await session.execute(
                    select(PowerProviderState)
                    .where(PowerProviderState.provider_id == row.provider_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if self._status(provider, now) is not SourceStatus.FRESH:
                return 0
            snapshots = (
                (
                    await session.execute(
                        select(PowerScheduleSnapshotRow)
                        .where(
                            PowerScheduleSnapshotRow.provider_id == row.provider_id,
                            PowerScheduleSnapshotRow.group_id == row.group_id,
                            PowerScheduleSnapshotRow.schedule_type == "CONFIRMED_DAY",
                            PowerScheduleSnapshotRow.is_current.is_(True),
                        )
                        .order_by(PowerScheduleSnapshotRow.valid_for_date)
                        .limit(14)
                    )
                )
                .scalars()
                .all()
            )
            inserted = 0
            subscription = _subscription(row)
            fresh_snapshots = [
                item
                for item in snapshots
                if _fresh(item.fetched_at, now, self.stale_after_seconds)
                and item.publication_status == "PLANNED"
            ]
            days = tuple([await self._day(session, item) for item in fresh_snapshots])
            bindings = [{"id": str(item.id), "revision": item.revision} for item in fresh_snapshots]
            transitions = upcoming_transitions(
                days,
                now,
                outage_lead_minutes=row.outage_lead_minutes,
                restoration_lead_minutes=row.restoration_lead_minutes,
            )
            future_owner = next(
                (
                    item
                    for item in fresh_snapshots
                    if item.valid_for_date >= now.astimezone(KYIV).date()
                ),
                None,
            )
            uncertain_dates = {
                item.valid_for_date for item in snapshots if item.publication_status != "PLANNED"
            }
            history = await self._history(session, subscription)
            uncertain_warning = any(
                event.status == "SENT"
                and outbox.delivered_at is not None
                and event.transition_at > now
                and event.transition_at.astimezone(KYIV).date() in uncertain_dates
                for event, outbox in history
            )
            if future_owner is not None and not uncertain_warning:
                inserted += await self._correction(
                    session, subscription, future_owner, days, bindings, now
                )
                await session.flush()
            history = await self._history(session, subscription)
            for transition in transitions:
                transition_day = transition.transition_at.astimezone(KYIV).date()
                snapshot = next(
                    (item for item in fresh_snapshots if item.valid_for_date == transition_day),
                    None,
                )
                if snapshot is None:
                    continue
                if self._already_warned(history, transition, bindings):
                    continue
                if (
                    transition.event_type == "POWER_OUTAGE_UPCOMING"
                    and not row.outage_notifications_enabled
                ) or (
                    transition.event_type == "POWER_RESTORATION_UPCOMING"
                    and not row.restoration_notifications_enabled
                ):
                    continue
                payload: dict[str, Any] = {
                    "provider_id": row.provider_id,
                    "group_id": row.group_id,
                    "schedule_revision": snapshot.revision,
                    "subscription_revision": row.revision,
                    "transition_at": aware(transition.transition_at).isoformat(),
                    "outage_starts_at": aware(transition.outage_starts_at).isoformat(),
                    "outage_ends_at": aware(transition.outage_ends_at).isoformat(),
                    "schedule_bindings": bindings,
                }
                binding_key = hashlib.sha256(
                    json.dumps(bindings, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                inserted += await self._enqueue(
                    session,
                    subscription,
                    snapshot,
                    transition.event_type,
                    transition.transition_at,
                    transition.planned_send_at,
                    transition.transition_key + binding_key,
                    payload,
                )
            return inserted

    @staticmethod
    async def _enqueue(
        session: AsyncSession,
        subscription: PowerSubscription,
        snapshot: PowerScheduleSnapshotRow,
        kind: str,
        transition_at: datetime,
        planned_send_at: datetime,
        identity: str,
        payload: dict[str, Any],
    ) -> int:
        key = hashlib.sha256(
            json.dumps(
                [
                    subscription.user_id,
                    subscription.revision,
                    str(snapshot.id),
                    snapshot.revision,
                    kind,
                    identity,
                ],
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        event_id, outbox_id = uuid4(), uuid4()
        event_key = "power:" + key
        exists = (
            await session.execute(
                select(PowerNotificationEvent.id).where(
                    PowerNotificationEvent.event_key == event_key
                )
            )
        ).scalar_one_or_none()
        if exists is not None:
            return 0
        event = PowerNotificationEvent(
            id=event_id,
            user_id=UUID(subscription.user_id),
            snapshot_id=snapshot.id,
            event_key=event_key,
            outbox_id=outbox_id,
            subscription_revision=subscription.revision,
            schedule_revision=snapshot.revision,
            transition_type=kind,
            transition_at=transition_at,
            planned_send_at=planned_send_at,
            status="PENDING",
        )
        payload["power_event_id"] = str(event_id)
        payload["source_kind"] = "official"
        payload["valid_until"] = aware(transition_at).isoformat()
        outbox = OutboxMessage(
            id=outbox_id,
            event_key=event_key,
            aggregate_type="power",
            aggregate_id=subscription.user_id,
            event_type=kind,
            payload=payload,
            available_at=planned_send_at,
        )
        session.add(outbox)
        await session.flush()
        session.add(event)
        return 1

    @staticmethod
    async def _history(session: AsyncSession, subscription: PowerSubscription) -> list[Any]:
        return list(
            (
                await session.execute(
                    select(PowerNotificationEvent, OutboxMessage)
                    .join(OutboxMessage, PowerNotificationEvent.outbox_id == OutboxMessage.id)
                    .join(
                        PowerScheduleSnapshotRow,
                        PowerNotificationEvent.snapshot_id == PowerScheduleSnapshotRow.id,
                    )
                    .where(
                        PowerNotificationEvent.user_id == UUID(subscription.user_id),
                        PowerNotificationEvent.subscription_revision == subscription.revision,
                        PowerNotificationEvent.status.in_(("SENT", "PENDING")),
                        PowerScheduleSnapshotRow.provider_id == subscription.provider.value,
                        PowerScheduleSnapshotRow.group_id == subscription.group_id,
                    )
                    .order_by(
                        OutboxMessage.delivered_at.desc().nulls_last(),
                        OutboxMessage.created_at.desc(),
                    )
                    .limit(200)
                )
            ).all()
        )

    @staticmethod
    def _already_warned(
        history: list[Any], transition: Any, bindings: list[dict[str, Any]]
    ) -> bool:
        for event, outbox in history:
            sent = event.status == "SENT" and outbox.delivered_at is not None
            if (
                sent
                and event.transition_type == transition.event_type
                and _timestamp(outbox.payload.get("transition_at"))
                == aware(transition.transition_at)
                and _timestamp(outbox.payload.get("outage_starts_at"))
                == aware(transition.outage_starts_at)
                and _timestamp(outbox.payload.get("outage_ends_at"))
                == aware(transition.outage_ends_at)
            ):
                return True
            if event.transition_type != "POWER_SCHEDULE_CHANGED":
                continue
            if not sent and not (
                event.status == "PENDING"
                and outbox.dead_at is None
                and outbox.payload.get("schedule_bindings") == bindings
            ):
                continue
            communicated_at = outbox.delivered_at if sent else outbox.available_at
            if communicated_at is None or communicated_at < transition.planned_send_at:
                continue
            windows = outbox.payload.get("power_windows")
            if (
                type(windows) is list
                and [
                    aware(transition.outage_starts_at).isoformat(),
                    aware(transition.outage_ends_at).isoformat(),
                ]
                in windows
            ):
                return True
        return False

    async def _correction(
        self,
        session: AsyncSession,
        subscription: PowerSubscription,
        snapshot: PowerScheduleSnapshotRow,
        days: tuple[PowerDaySchedule, ...],
        bindings: list[dict[str, Any]],
        now: datetime,
    ) -> int:
        if not (
            subscription.outage_notifications_enabled
            or subscription.restoration_notifications_enabled
        ):
            return 0
        sent = [
            (event, outbox)
            for event, outbox in await self._history(session, subscription)
            if event.status == "SENT" and outbox.delivered_at is not None
        ]
        current = _joined_windows(days, now)
        coverage = list(current)
        for previous_event, previous_outbox in sent:
            if previous_event.transition_type == "POWER_SCHEDULE_CHANGED":
                previous = _future_windows(previous_outbox.payload.get("power_windows"), now)
                if previous is None:
                    return 0
                coverage.extend(previous)
                break
            start = _timestamp(previous_outbox.payload.get("outage_starts_at"))
            end = _timestamp(previous_outbox.payload.get("outage_ends_at"))
            if end is not None and end > now and start is not None:
                coverage.append([start.isoformat(), end.isoformat()])
        if not _known_coverage(days, coverage, now):
            return 0
        relevant = False
        deadlines: list[datetime] = []
        for event, outbox in sent:
            if outbox.payload.get("schedule_bindings") == bindings:
                return 0  # the current exact schedule has already been communicated
            if event.transition_type == "POWER_SCHEDULE_CHANGED":
                previous = _future_windows(outbox.payload.get("power_windows"), now)
                if previous is None or previous == current:
                    return 0
                deadlines.extend(
                    end for _, raw in previous if (end := _timestamp(raw)) is not None and end > now
                )
                deadlines.extend(
                    start
                    for raw, _ in current
                    if (start := _timestamp(raw)) is not None and start > now
                )
                relevant = bool(deadlines)
                break
            if event.transition_at <= now:
                continue
            start = _timestamp(outbox.payload.get("outage_starts_at"))
            end = _timestamp(outbox.payload.get("outage_ends_at"))
            if start is None or end is None:
                continue
            same = [start.isoformat(), end.isoformat()] in current
            if not same:
                relevant = True
                deadlines.append(event.transition_at)
        if not relevant or not deadlines:
            return 0
        deadline = min(deadlines)
        return await self._enqueue(
            session,
            subscription,
            snapshot,
            "POWER_SCHEDULE_CHANGED",
            deadline,
            now,
            "schedule_changed:"
            + hashlib.sha256(
                json.dumps(bindings, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            {
                "provider_id": subscription.provider.value,
                "group_id": subscription.group_id,
                "schedule_revision": snapshot.revision,
                "subscription_revision": subscription.revision,
                "power_windows": current,
                "coverage_windows": coverage,
                "schedule_bindings": bindings,
                "schedule_date": snapshot.valid_for_date.isoformat(),
            },
        )


@dataclass(frozen=True)
class PowerDeliveryFence:
    provider: PowerProviderState
    snapshots: tuple[PowerScheduleSnapshotRow, ...]
    user: User
    subscription: UserPowerSubscription
    event: PowerNotificationEvent
    expires_at: datetime
    stale_after_seconds: int

    def valid_at(self, now: datetime) -> bool:
        """No DB IO: all actual policy rows remain locked through transport IO."""
        return (
            aware(now) <= self.expires_at
            and self.provider.enabled
            and self.provider.source_status == "FRESH"
            and _fresh(self.provider.last_success_at, now, self.stale_after_seconds)
            and all(
                _fresh(item.fetched_at, now, self.stale_after_seconds) for item in self.snapshots
            )
        )


async def power_delivery_policy(
    session: AsyncSession,
    row: OutboxMessage,
    now: datetime,
    *,
    stale_after_seconds: int,
    lock: bool,
    expected_recipient: DeliveryRecipient | None = None,
    fences: list[PowerDeliveryFence] | None = None,
) -> str:
    """AUTHORIZED, TEMPORARY source loss, or REJECTED immutable policy change."""
    if (
        row.event_type not in POWER_EVENT_TYPES
        or row.aggregate_type != "power"
        or row.payload.get("source_kind") != "official"
    ):
        return "REJECTED"
    try:
        identity, event_id = UUID(row.aggregate_id), UUID(str(row.payload["power_event_id"]))
    except (ValueError, KeyError, TypeError):
        return "REJECTED"

    async def get(model: Any, predicate: Any) -> Any:
        query = select(model).where(predicate)
        if lock:
            query = query.with_for_update()
        return (await session.execute(query)).scalar_one_or_none()

    # Shared ordering with subscriptions/planner: user -> subscription -> provider.
    user = await get(User, User.id == identity)
    subscription = await get(UserPowerSubscription, UserPowerSubscription.user_id == identity)
    if user is None or subscription is None or not subscription.enabled:
        return "REJECTED"
    if expected_recipient is not None and expected_recipient.chat_id != user.telegram_user_id:
        return "REJECTED"
    provider = await get(
        PowerProviderState, PowerProviderState.provider_id == subscription.provider_id
    )
    event = await get(PowerNotificationEvent, PowerNotificationEvent.id == event_id)
    if provider is None or event is None or not provider.enabled:
        return "REJECTED"
    snapshot = await get(PowerScheduleSnapshotRow, PowerScheduleSnapshotRow.id == event.snapshot_id)
    if (
        snapshot is None
        or not snapshot.is_current
        or snapshot.source_kind != "official"
        or snapshot.publication_status != "PLANNED"
        or snapshot.schedule_type != "CONFIRMED_DAY"
        or event.user_id != identity
        or event.status != "PENDING"
        or event.outbox_id != row.id
        or event.transition_type != row.event_type
        or event.event_key != row.event_key
        or subscription.revision != event.subscription_revision
        or event.schedule_revision != snapshot.revision
        or snapshot.provider_id != subscription.provider_id
        or snapshot.group_id != subscription.group_id
        or row.payload.get("provider_id") != subscription.provider_id
        or row.payload.get("group_id") != subscription.group_id
        or type(row.payload.get("subscription_revision")) is not int
        or row.payload.get("subscription_revision") != subscription.revision
        or type(row.payload.get("schedule_revision")) is not int
        or row.payload.get("schedule_revision") != snapshot.revision
    ):
        return "REJECTED"
    if (
        (
            row.event_type == "POWER_OUTAGE_UPCOMING"
            and not subscription.outage_notifications_enabled
        )
        or (
            row.event_type == "POWER_RESTORATION_UPCOMING"
            and not subscription.restoration_notifications_enabled
        )
        or (
            row.event_type == "POWER_SCHEDULE_CHANGED"
            and not (
                subscription.outage_notifications_enabled
                or subscription.restoration_notifications_enabled
            )
        )
    ):
        return "REJECTED"
    expiry = _timestamp(row.payload.get("valid_until"))
    if expiry is None or expiry != aware(event.transition_at) or aware(now) > expiry:
        return "REJECTED"
    if event.planned_send_at > now:
        return "REJECTED"
    if (
        provider.source_status != "FRESH"
        or not _fresh(provider.last_success_at, now, stale_after_seconds)
        or not _fresh(snapshot.fetched_at, now, stale_after_seconds)
    ):
        return "TEMPORARY"
    bindings = row.payload.get("schedule_bindings")
    schedules: list[PowerDaySchedule] = []
    bound_rows = [snapshot]
    if row.event_type in POWER_EVENT_TYPES:
        if type(bindings) is not list or not 1 <= len(bindings) <= 14:
            return "REJECTED"
        seen: set[UUID] = set()
        for binding in bindings:
            if type(binding) is not dict or set(binding) != {"id", "revision"}:
                return "REJECTED"
            try:
                identifier = UUID(str(binding["id"]))
            except (ValueError, TypeError):
                return "REJECTED"
            if identifier in seen or type(binding["revision"]) is not int:
                return "REJECTED"
            seen.add(identifier)
            bound = await get(PowerScheduleSnapshotRow, PowerScheduleSnapshotRow.id == identifier)
            if (
                bound is None
                or not bound.is_current
                or bound.revision != binding["revision"]
                or bound.source_kind != "official"
                or bound.publication_status != "PLANNED"
                or bound.provider_id != subscription.provider_id
                or bound.group_id != subscription.group_id
                or bound.schedule_type != "CONFIRMED_DAY"
            ):
                return "REJECTED"
            if not _fresh(bound.fetched_at, now, stale_after_seconds):
                return "TEMPORARY"
            schedules.append(await PostgresPowerRepository._day(session, bound))
            bound_rows.append(bound)
        if snapshot.id not in seen:
            return "REJECTED"
        # A newly published neighboring date can change midnight continuity
        # even while every old bound row remains current. Bind the entire fresh
        # authoritative set, not merely the rows the old payload happened to know.
        latest_query = (
            select(PowerScheduleSnapshotRow)
            .where(
                PowerScheduleSnapshotRow.provider_id == subscription.provider_id,
                PowerScheduleSnapshotRow.group_id == subscription.group_id,
                PowerScheduleSnapshotRow.schedule_type == "CONFIRMED_DAY",
                PowerScheduleSnapshotRow.is_current.is_(True),
                PowerScheduleSnapshotRow.publication_status == "PLANNED",
            )
            .order_by(PowerScheduleSnapshotRow.valid_for_date)
            .limit(15)
        )
        if lock:
            latest_query = latest_query.with_for_update()
        latest = (await session.execute(latest_query)).scalars().all()
        latest_ids = {
            item.id for item in latest if _fresh(item.fetched_at, now, stale_after_seconds)
        }
        if latest_ids != seen:
            return "REJECTED"
        from time_tl.domain.power_outages.decisions import upcoming_transitions

        matches = [
            item
            for item in upcoming_transitions(
                tuple(schedules),
                now,
                outage_lead_minutes=subscription.outage_lead_minutes,
                restoration_lead_minutes=subscription.restoration_lead_minutes,
            )
            if item.event_type == row.event_type
            and aware(item.transition_at) == expiry
            and aware(item.outage_starts_at) == _timestamp(row.payload.get("outage_starts_at"))
            and aware(item.outage_ends_at) == _timestamp(row.payload.get("outage_ends_at"))
            and aware(item.transition_at) == _timestamp(row.payload.get("transition_at"))
        ]
        if row.event_type != "POWER_SCHEDULE_CHANGED" and len(matches) != 1:
            return "REJECTED"
    if row.event_type == "POWER_SCHEDULE_CHANGED":
        if _future_windows(row.payload.get("power_windows"), now) != _joined_windows(
            tuple(schedules), now
        ):
            return "REJECTED"
        if not _known_coverage(tuple(schedules), row.payload.get("coverage_windows"), now):
            return "REJECTED"
    if lock and fences is not None:
        fences.append(
            PowerDeliveryFence(
                provider, tuple(bound_rows), user, subscription, event, expiry, stale_after_seconds
            )
        )
    return "AUTHORIZED"


async def mark_power_delivered(session: AsyncSession, row: OutboxMessage) -> None:
    event = (
        await session.execute(
            select(PowerNotificationEvent)
            .where(PowerNotificationEvent.outbox_id == row.id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if event is not None:
        event.status = "SENT"

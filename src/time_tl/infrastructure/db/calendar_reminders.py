"""Opt-in reminders for timed events in owned personal calendars."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from time_tl.application.sender_worker import DeliveryRecipient
from time_tl.domain.calendar_memberships import MAX_PERSISTENT_CALENDARS, deserialize_profile
from time_tl.domain.calendar_overrides import (
    PersonalOverride,
    apply_personal_overrides,
    occurrence_id,
)
from time_tl.domain.calendars import (
    KYIV,
    Calendar,
    CalendarKind,
    CalendarMembership,
    CalendarSource,
    TimelineEvent,
)

from .calendar_overrides import AsyncPostgresPersonalCalendarRepository
from .models import Calendar as CalendarRow
from .models import OutboxMessage, User

CALENDAR_REMINDER = "CALENDAR_REMINDER"
REMINDER_LEAD = timedelta(minutes=10)
_USER_PAGE_SIZE = 100
_REVISION_SEPARATOR = "|"


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("calendar reminder time must be timezone-aware")
    return value.astimezone(UTC)


def _current_personal_calendar(row: CalendarRow, user: User) -> bool:
    return (
        row.owner_user_id == user.id
        and row.kind == CalendarKind.PERSONAL.value
        and row.owner_visible is True
    )


def _due_events(
    calendar: CalendarRow, user: User, overrides: Sequence[PersonalOverride], now: datetime
) -> tuple[TimelineEvent, ...]:
    identity = str(calendar.id)
    profile = deserialize_profile(calendar.snapshot_payload, calendar.id, calendar.name)
    source = CalendarSource(
        Calendar(identity, calendar.name, CalendarKind.PERSONAL, profile),
        CalendarMembership(user.telegram_user_id, identity),
    )
    end = now + REMINDER_LEAD
    versions = {calendar.id: calendar.snapshot_version}
    clocks = (now,) if now.astimezone(KYIV).date() == end.astimezone(KYIV).date() else (now, end)
    return tuple(
        event
        for clock in clocks
        for event in apply_personal_overrides(
            user.telegram_user_id, (source,), versions, overrides, clock
        ).events
        if now < event.start.astimezone(UTC) <= end
    )


def _locator(
    user: User, calendar: CalendarRow, event: TimelineEvent
) -> tuple[str, dict[str, object]]:
    # A moved occurrence keeps the original semantic id supplied by the
    # personal projection. A different start is a distinct scheduled reminder.
    moved_prefix = "personal:"
    occurrence = (
        UUID(event.event_id.removeprefix(moved_prefix))
        if event.event_id.startswith(moved_prefix)
        else occurrence_id(calendar.id, event)
    )
    starts_at = event.start.astimezone(UTC).isoformat()
    semantic_key = f"calendar-reminder:{user.id}:{calendar.id}:{occurrence}:{starts_at}"
    return (
        f"{semantic_key}{_REVISION_SEPARATOR}{calendar.version}:{user.calendar_reminder_generation}",
        {
            "calendar_id": str(calendar.id),
            "calendar_version": calendar.version,
            "occurrence_id": str(occurrence),
            "calendar_reminder_generation": str(user.calendar_reminder_generation),
            "event_title": event.title,
            "calendar_name": calendar.name,
            "starts_at": starts_at,
        },
    )


class PostgresCalendarReminderRepository:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    async def preferences(self, actor: int) -> bool:
        self._check_actor(actor)
        async with self._factory() as session:
            generation = await session.scalar(
                select(User.calendar_reminder_generation).where(User.telegram_user_id == actor)
            )
            return isinstance(generation, UUID)

    async def set_enabled(self, actor: int, enabled: bool) -> bool:
        self._check_actor(actor)
        if type(enabled) is not bool:
            raise ValueError("calendar reminder enabled flag must be boolean")
        async with self._factory() as session, session.begin():
            user = await session.scalar(
                select(User).where(User.telegram_user_id == actor).with_for_update()
            )
            if user is None:
                return False
            if enabled and user.calendar_reminder_generation is None:
                user.calendar_reminder_generation = uuid4()
            elif not enabled:
                user.calendar_reminder_generation = None
            return enabled

    @staticmethod
    def _check_actor(actor: int) -> None:
        if type(actor) is not int or actor <= 0:
            raise ValueError("calendar reminder actor must be a positive integer")

    async def plan(self, now: datetime) -> int:
        now = _aware_utc(now)
        planned = 0
        cursor = None
        while True:
            async with self._factory() as session:
                statement = (
                    select(User.id)
                    .where(User.calendar_reminder_generation.is_not(None))
                    .order_by(User.id)
                    .limit(_USER_PAGE_SIZE)
                )
                if cursor is not None:
                    statement = statement.where(User.id > cursor)
                user_ids = (await session.scalars(statement)).all()
            for user_id in user_ids:
                planned += await self._plan_user(user_id, now)
            if len(user_ids) < _USER_PAGE_SIZE:
                return planned
            cursor = user_ids[-1]

    async def _plan_user(self, user_id: UUID, now: datetime) -> int:
        async with self._factory() as session, session.begin():
            user = await session.get(User, user_id, with_for_update=True, populate_existing=True)
            if user is None or user.calendar_reminder_generation is None:
                return 0
            calendars = (
                await session.scalars(
                    select(CalendarRow)
                    .where(
                        CalendarRow.owner_user_id == user_id,
                        CalendarRow.owner_visible.is_(True),
                        CalendarRow.kind == CalendarKind.PERSONAL.value,
                    )
                    .order_by(CalendarRow.id)
                    .limit(MAX_PERSISTENT_CALENDARS + 1)
                    .with_for_update()
                )
            ).all()
            if not calendars or len(calendars) > MAX_PERSISTENT_CALENDARS:
                return 0
            try:
                overrides = await AsyncPostgresPersonalCalendarRepository.load_personal_overrides(
                    session, user.id, user.telegram_user_id, {calendar.id for calendar in calendars}
                )
            except ValueError:
                return 0
            pending: dict[str, dict[str, object]] = {}
            for calendar in calendars:
                if not _current_personal_calendar(calendar, user):
                    continue
                try:
                    events = _due_events(calendar, user, overrides, now)
                    locators = tuple(_locator(user, calendar, event) for event in events)
                except ValueError:
                    continue
                pending.update(
                    {
                        key: {
                            "event_key": key,
                            "aggregate_type": "schedule",
                            "aggregate_id": str(user_id),
                            "event_type": CALENDAR_REMINDER,
                            "payload": payload,
                            "available_at": now,
                        }
                        for key, payload in locators
                    }
                )
            if not pending:
                return 0
            # The sender takes the outbox lock before the actor lock. Its
            # durable dispatch marker is visible without taking that lock.
            # With this actor and the calendars locked, an older revision
            # cannot pass final authorization while planning a replacement.
            semantic_keys = {key.partition(_REVISION_SEPARATOR)[0] for key in pending}
            blocked = await session.scalars(
                select(OutboxMessage.event_key).where(
                    OutboxMessage.aggregate_type == "schedule",
                    OutboxMessage.aggregate_id == str(user_id),
                    OutboxMessage.event_type == CALENDAR_REMINDER,
                    func.split_part(OutboxMessage.event_key, _REVISION_SEPARATOR, 1).in_(
                        semantic_keys
                    ),
                    or_(
                        OutboxMessage.event_key.in_(pending),
                        OutboxMessage.delivered_at.is_not(None),
                        OutboxMessage.dispatch_started_at.is_not(None),
                        OutboxMessage.indeterminate_at.is_not(None),
                        OutboxMessage.attempts.is_distinct_from(0),
                    ),
                )
            )
            blocked_semantics = {key.partition(_REVISION_SEPARATOR)[0] for key in blocked.all()}
            insertable = [
                item
                for key, item in pending.items()
                if key.partition(_REVISION_SEPARATOR)[0] not in blocked_semantics
            ]
            if not insertable:
                return 0
            rows = await session.scalars(
                insert(OutboxMessage)
                .values(insertable)
                .on_conflict_do_nothing(index_elements=[OutboxMessage.event_key])
                .returning(OutboxMessage.id)
            )
            return len(rows.all())


async def calendar_reminder_deliverable(
    session: AsyncSession,
    row: OutboxMessage,
    now: datetime,
    *,
    lock: bool = False,
    expected_recipient: DeliveryRecipient | None = None,
) -> bool:
    """Verify the queued occurrence against the current opt-in and personal calendar."""
    now = _aware_utc(now)
    if (
        row.event_type != CALENDAR_REMINDER
        or row.aggregate_type != "schedule"
        or row.dead_at is not None
        or row.delivered_at is not None
        or row.indeterminate_at is not None
        or type(row.payload) is not dict
    ):
        return False
    payload = row.payload
    calendar_id = payload.get("calendar_id")
    if not isinstance(calendar_id, str) or type(payload.get("calendar_version")) is not int:
        return False
    try:
        identity = UUID(calendar_id)
        user_id = UUID(row.aggregate_id)
    except ValueError:
        return False
    if row.aggregate_id != str(user_id):
        return False
    user = await session.get(User, user_id, with_for_update=lock, populate_existing=lock)
    if user is None or user.calendar_reminder_generation is None:
        return False
    if payload.get("calendar_reminder_generation") != str(user.calendar_reminder_generation):
        return False
    if lock and (expected_recipient is None or expected_recipient.chat_id != user.telegram_user_id):
        return False
    calendar = await session.get(
        CalendarRow, identity, with_for_update=lock, populate_existing=lock
    )
    if (
        calendar is None
        or not _current_personal_calendar(calendar, user)
        or payload["calendar_version"] != calendar.version
    ):
        return False
    try:
        overrides = await AsyncPostgresPersonalCalendarRepository.load_personal_overrides(
            session, user.id, user.telegram_user_id, {calendar.id}
        )
        return any(
            (row.event_key, payload) == _locator(user, calendar, event)
            for event in _due_events(calendar, user, overrides, now)
        )
    except ValueError:
        return False

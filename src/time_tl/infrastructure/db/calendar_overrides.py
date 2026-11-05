"""Transactional actor-only occurrence edits; no shared or delivery writes."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import date, datetime, timedelta
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from time_tl.domain.calendar_memberships import (
    MAX_PERSISTENT_CALENDARS,
    CalendarAccessDenied,
    CalendarConflict,
    CalendarLimitExceeded,
    deserialize_profile,
)
from time_tl.domain.calendar_overrides import (
    OverrideAction,
    PersonalEventChange,
    PersonalEventChoice,
    PersonalOverride,
    apply_personal_overrides,
    event_choices,
)
from time_tl.domain.calendars import (
    KYIV,
    Calendar,
    CalendarKind,
    CalendarMembership,
    CalendarSource,
    MyDay,
)

from .calendars import AsyncPostgresCalendarRepository, _calendar_id, _now, _user_id
from .models import Calendar as CalendarRow
from .models import CalendarMembership as MembershipRow
from .models import PersonalCalendarOverride as OverrideRow

MAX_PERSONAL_OVERRIDES = 500
MAX_VERSION = 2**31 - 1


def _day(day: date, now: datetime) -> None:
    today = _now(now).astimezone(KYIV).date()
    if type(day) is not date or not today <= day <= today + timedelta(days=30):
        raise ValueError("personal change date is outside the confirmed window")


class AsyncPostgresPersonalCalendarRepository:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    @staticmethod
    def _visible(row: CalendarRow, member: MembershipRow | None, user_id: UUID) -> None:
        summary = AsyncPostgresCalendarRepository._summary(row, member, user_id)
        if summary.visible is not True:
            raise CalendarAccessDenied()

    @staticmethod
    async def _overrides(session: AsyncSession, user_id: UUID) -> list[OverrideRow]:
        rows = list(
            (
                await session.execute(
                    select(OverrideRow)
                    .where(OverrideRow.user_id == user_id)
                    .order_by(
                        OverrideRow.calendar_id,
                        OverrideRow.snapshot_version,
                        OverrideRow.source_day,
                        OverrideRow.event_id,
                    )
                    .limit(MAX_PERSONAL_OVERRIDES + 1)
                )
            )
            .scalars()
            .all()
        )
        if len(rows) > MAX_PERSONAL_OVERRIDES:
            raise CalendarLimitExceeded()
        return rows

    @classmethod
    async def load_personal_overrides(
        cls, session: AsyncSession, user_id: UUID, actor: int, calendars: set[UUID]
    ) -> tuple[PersonalOverride, ...]:
        """Read under the caller's actor and calendar locks when dispatching."""
        return tuple(
            PersonalOverride(
                user_id=actor,
                calendar_id=item.calendar_id,
                snapshot_version=item.snapshot_version,
                source_day=item.source_day,
                event_id=item.event_id,
                action=OverrideAction(item.action),
                version=item.version,
                target_day=item.target_day,
                start=item.start_time,
                end=item.end_time,
            )
            for item in await cls._overrides(session, user_id)
            if item.calendar_id in calendars
        )

    async def list_event_choices(
        self,
        actor: int,
        calendar_id: UUID,
        day: date,
        now: datetime,
    ) -> tuple[PersonalEventChoice, ...]:
        _user_id(actor)
        _calendar_id(calendar_id)
        _day(day, now)
        async with self._factory() as session, session.begin():
            user = await AsyncPostgresCalendarRepository._user(session, actor, lock=True)
            row = await AsyncPostgresCalendarRepository._calendar(session, calendar_id)
            member = await AsyncPostgresCalendarRepository._membership(session, row.id, user.id)
            self._visible(row, member, user.id)
            stored = await self._overrides(session, user.id)
            versions = {
                item.event_id: item.version
                for item in stored
                if item.calendar_id == row.id
                and item.snapshot_version == row.snapshot_version
                and item.source_day == day
            }
            profile = deserialize_profile(row.snapshot_payload, row.id, row.name)
            return tuple(
                replace(choice, override_version=versions.get(choice.event_id, 0))
                for choice in event_choices(
                    row.id, row.version, row.snapshot_version, profile, day, user_id=actor
                )
            )

    async def export_personal_overrides(self, actor: int) -> str:
        """Self-only bounded JSON, including hidden/stale changes and tombstones.

        No user identifiers, event titles, shared payloads or lesson links are
        exported. Do not use visibility filtering: the actor owns every row here.
        """
        _user_id(actor)
        async with self._factory() as session, session.begin():
            user = await AsyncPostgresCalendarRepository._user(session, actor, lock=True)
            exported: list[dict[str, object]] = []
            for row in await self._overrides(session, user.id):
                value = PersonalOverride(
                    user_id=actor,
                    calendar_id=row.calendar_id,
                    snapshot_version=row.snapshot_version,
                    source_day=row.source_day,
                    event_id=row.event_id,
                    action=OverrideAction(row.action),
                    version=row.version,
                    target_day=row.target_day,
                    start=row.start_time,
                    end=row.end_time,
                )
                exported.append(
                    {
                        "calendar_id": str(value.calendar_id),
                        "snapshot_version": value.snapshot_version,
                        "source_day": value.source_day.isoformat(),
                        "event_id": str(value.event_id),
                        "action": value.action.value,
                        "version": value.version,
                        "target_day": value.target_day.isoformat() if value.target_day else None,
                        "start": value.start.isoformat(timespec="minutes") if value.start else None,
                        "end": value.end.isoformat(timespec="minutes") if value.end else None,
                    }
                )
            return json.dumps(
                {"schema_version": "1.0", "overrides": exported},
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )

    async def save_override(
        self,
        actor: int,
        change: PersonalEventChange,
        now: datetime,
    ) -> int:
        _user_id(actor)
        if not isinstance(change, PersonalEventChange):
            raise ValueError("personal change requires a confirmed choice")
        # Re-run strict domain construction even for tampered frozen DTOs.
        change = replace(change, choice=replace(change.choice))
        choice = change.choice
        _day(choice.source_day, now)
        if change.target_day is not None:
            _day(change.target_day, now)
        async with self._factory() as session, session.begin():
            user = await AsyncPostgresCalendarRepository._user(session, actor, lock=True)
            row = await AsyncPostgresCalendarRepository._calendar(session, choice.calendar_id)
            member = await AsyncPostgresCalendarRepository._membership(session, row.id, user.id)
            self._visible(row, member, user.id)
            if (
                row.version != choice.calendar_version
                or row.snapshot_version != choice.snapshot_version
            ):
                raise CalendarConflict()
            profile = deserialize_profile(row.snapshot_payload, row.id, row.name)
            actual = event_choices(
                row.id, row.version, row.snapshot_version, profile, choice.source_day, user_id=actor
            )
            matches = [item for item in actual if item.event_id == choice.event_id]
            if (
                len(matches) != 1
                or replace(matches[0], override_version=choice.override_version) != choice
            ):
                raise CalendarConflict()
            stored = await self._overrides(session, user.id)
            matching = [
                item
                for item in stored
                if item.calendar_id == row.id
                and item.snapshot_version == row.snapshot_version
                and item.source_day == choice.source_day
                and item.event_id == choice.event_id
            ]
            previous = matching[0] if matching else None
            version = previous.version if previous is not None else 0
            if (
                type(version) is not int
                or version != choice.override_version
                or version >= MAX_VERSION - 1
            ):
                raise CalendarConflict()
            if previous is None:
                if len(stored) >= MAX_PERSONAL_OVERRIDES:
                    raise CalendarLimitExceeded()
                previous = OverrideRow(
                    user_id=user.id,
                    calendar_id=row.id,
                    snapshot_version=row.snapshot_version,
                    source_day=choice.source_day,
                    event_id=choice.event_id,
                )
                session.add(previous)
            previous.action, previous.version = change.action.value, version + 1
            previous.target_day, previous.start_time, previous.end_time = (
                change.target_day,
                change.start,
                change.end,
            )
            await session.flush()
            return previous.version

    async def read_personal_day(
        self,
        actor: int,
        at: datetime,
        *,
        extra_sources: tuple[CalendarSource, ...] = (),
    ) -> MyDay:
        _user_id(actor)
        _now(at)
        if type(extra_sources) is not tuple or len(extra_sources) > 1:
            raise ValueError("personal day supports one explicit legacy source")
        for source in extra_sources:
            if (
                not isinstance(source, CalendarSource)
                or source.membership.user_id != actor
                or source.membership.visible is not True
                or re.fullmatch(r"legacy:[a-f0-9]{64}", source.calendar.calendar_id) is None
            ):
                raise CalendarAccessDenied()
        async with self._factory() as session, session.begin():
            user = await AsyncPostgresCalendarRepository._user(session, actor, lock=True)
            # All shared snapshots are locked in UUID order. Calendar writers and
            # membership changes use the same calendar lock; personal writers use
            # the same actor lock. Source and override reads therefore cannot mix.
            pairs = (
                await session.execute(
                    select(CalendarRow, MembershipRow)
                    .outerjoin(
                        MembershipRow,
                        and_(
                            MembershipRow.calendar_id == CalendarRow.id,
                            MembershipRow.user_id == user.id,
                        ),
                    )
                    .where(
                        or_(CalendarRow.owner_user_id == user.id, MembershipRow.user_id == user.id)
                    )
                    .order_by(CalendarRow.id)
                    .limit(MAX_PERSISTENT_CALENDARS + 1)
                    .with_for_update(of=CalendarRow)
                    .execution_options(populate_existing=True)
                )
            ).all()
            if len(pairs) > MAX_PERSISTENT_CALENDARS:
                raise CalendarLimitExceeded()
            sources = list(extra_sources)
            versions: dict[UUID, int] = {}
            for row, member in pairs:
                summary = AsyncPostgresCalendarRepository._summary(row, member, user.id)
                if summary.visible is not True:
                    continue
                profile = deserialize_profile(row.snapshot_payload, row.id, row.name)
                identity = str(row.id)
                sources.append(
                    CalendarSource(
                        Calendar(identity, row.name, CalendarKind(row.kind), profile),
                        CalendarMembership(actor, identity),
                    )
                )
                versions[row.id] = row.snapshot_version
            changes = await self.load_personal_overrides(session, user.id, actor, set(versions))
            return apply_personal_overrides(actor, tuple(sources), versions, changes, at)

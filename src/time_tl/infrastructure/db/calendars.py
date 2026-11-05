"""Explicit calendar persistence. No legacy profile, alert or outbox writes."""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from time_tl.domain.calendar_memberships import (
    MAX_PERSISTENT_CALENDARS,
    CalendarAccessDenied,
    CalendarConflict,
    CalendarDataUnavailable,
    CalendarLimitExceeded,
    CalendarRole,
    CalendarSummary,
    InvitationPreview,
    deserialize_profile,
    serialize_profile,
)
from time_tl.domain.calendars import Calendar, CalendarKind, CalendarMembership, CalendarSource
from time_tl.domain.schedule import ScheduleProfile

from .models import Calendar as CalendarRow
from .models import CalendarInvitation, User
from .models import CalendarMembership as MembershipRow

if TYPE_CHECKING:
    from .calendar_overrides import AsyncPostgresPersonalCalendarRepository
    from .calendar_reminders import PostgresCalendarReminderRepository


def _user_id(value: int) -> None:
    if type(value) is not int or not 0 < value <= 2**63 - 1:
        raise ValueError("invalid user identity")


def _version(value: int) -> None:
    if type(value) is not int or not 0 < value < 2**31 - 1:
        raise ValueError("invalid calendar version")


def _calendar_id(value: UUID) -> None:
    if not isinstance(value, UUID):
        raise ValueError("invalid calendar identity")


def _now(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be aware")
    return value.astimezone(UTC)


def digest_invitation_token(value: str) -> str:
    if (
        not isinstance(value, str)
        or not 32 <= len(value) <= 96
        or any(
            not (character.isascii() and (character.isalnum() or character in "_-"))
            for character in value
        )
    ):
        raise ValueError("invalid invitation token")
    return hashlib.sha256(value.encode("ascii")).hexdigest()


class AsyncPostgresCalendarRepository:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    def make_personal_calendar_repository(self) -> AsyncPostgresPersonalCalendarRepository:
        """Share the session factory without changing the legacy port or queries."""
        from .calendar_overrides import AsyncPostgresPersonalCalendarRepository

        return AsyncPostgresPersonalCalendarRepository(self._factory)

    def make_calendar_reminder_repository(self) -> PostgresCalendarReminderRepository:
        from .calendar_reminders import PostgresCalendarReminderRepository

        return PostgresCalendarReminderRepository(self._factory)

    async def list_calendars(self, telegram_user_id: int) -> tuple[CalendarSummary, ...]:
        _user_id(telegram_user_id)
        async with self._factory() as session:
            user = await self._user(session, telegram_user_id)
            return tuple(
                self._summary(row, member, user.id)
                for row, member in await self._rows(session, user.id)
            )

    async def get_snapshot(
        self,
        actor: int,
        calendar_id: UUID,
    ) -> tuple[CalendarSummary, ScheduleProfile]:
        """Read one authorized snapshot, including personally hidden calendars."""
        _user_id(actor)
        _calendar_id(calendar_id)
        async with self._factory() as session:
            user = await self._user(session, actor)
            # One joined read avoids mixing a new snapshot with an old role/summary.
            result = (
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
                        CalendarRow.id == calendar_id,
                        or_(CalendarRow.owner_user_id == user.id, MembershipRow.user_id == user.id),
                    )
                )
            ).one_or_none()
            if result is None:
                raise CalendarDataUnavailable()
            row, member = result
            summary = self._summary(row, member, user.id)
            return summary, deserialize_profile(row.snapshot_payload, row.id, row.name)

    async def export_calendars(self, actor: int) -> tuple[dict[str, object], ...]:
        """Self-only export; no other identities, invitation hashes or tokens."""
        _user_id(actor)
        async with self._factory() as session:
            user = await self._user(session, actor)
            result: list[dict[str, object]] = []
            for row, member in await self._rows(session, user.id):
                summary = self._summary(row, member, user.id)
                profile = deserialize_profile(row.snapshot_payload, row.id, row.name)
                result.append(
                    {
                        "calendar_id": str(summary.calendar_id),
                        "name": summary.name,
                        "kind": summary.kind.value,
                        "role": summary.role.value,
                        "visible": summary.visible,
                        "version": summary.version,
                        "snapshot": serialize_profile(profile),
                    }
                )
            return tuple(result)

    async def cleanup_invitations(self, now: datetime, *, limit: int = 200) -> int:
        """Delete expired tokens in bounded batches. No runtime scheduling here.

        Consumed/revoked rows retain their single-use tombstone only until their
        original expiry (at most 24h from creation). Cleanup changes no calendars,
        roles or CAS versions. Caller is an explicit maintenance command, not UI.
        """
        timestamp = _now(now)
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("invalid invitation cleanup batch")
        async with self._factory() as session, session.begin():
            tokens = (
                (
                    await session.execute(
                        select(CalendarInvitation.token_hash)
                        .where(CalendarInvitation.expires_at <= timestamp)
                        .order_by(CalendarInvitation.expires_at, CalendarInvitation.token_hash)
                        .limit(limit)
                        .with_for_update(skip_locked=True)
                    )
                )
                .scalars()
                .all()
            )
            if not tokens:
                return 0
            deleted = (
                (
                    await session.execute(
                        delete(CalendarInvitation)
                        .where(CalendarInvitation.token_hash.in_(tokens))
                        .returning(CalendarInvitation.token_hash)
                    )
                )
                .scalars()
                .all()
            )
            return len(deleted)

    async def list_sources(self, telegram_user_id: int) -> tuple[CalendarSource, ...]:
        _user_id(telegram_user_id)
        async with self._factory() as session:
            user = await self._user(session, telegram_user_id)
            result = []
            for row, member in await self._rows(session, user.id):
                summary = self._summary(row, member, user.id)
                if not summary.visible:
                    continue
                profile = deserialize_profile(row.snapshot_payload, row.id, row.name)
                identity = f"calendar:{row.id}"
                result.append(
                    CalendarSource(
                        Calendar(identity, summary.name, summary.kind, profile),
                        CalendarMembership(telegram_user_id, identity),
                    )
                )
            return tuple(result)

    async def create_calendar(
        self,
        actor: int,
        profile: ScheduleProfile,
        kind: CalendarKind,
    ) -> CalendarSummary:
        _user_id(actor)
        if not isinstance(kind, CalendarKind):
            raise ValueError("invalid calendar kind")
        payload = serialize_profile(profile)
        async with self._factory() as session, session.begin():
            user = await self._user(session, actor, lock=True)
            await self._quota(session, user.id)
            row = CalendarRow(
                id=uuid4(),
                owner_user_id=user.id,
                name=profile.name,
                kind=kind.value,
                snapshot_payload=payload,
                owner_visible=True,
                version=1,
                snapshot_version=1,
            )
            session.add(row)
            await session.flush()
            return self._summary(row, None, user.id)

    async def update_calendar(
        self,
        actor: int,
        calendar_id: UUID,
        expected_version: int,
        profile: ScheduleProfile,
    ) -> CalendarSummary:
        """Replace one independent snapshot atomically for an authorized editor.

        The caller must carry any existing date overrides in ``profile``. This
        is a CAS replacement, not a merge or activation of the legacy schedule.
        Validation precedes the transaction; rejected edits change no rows.
        """
        self._inputs(actor, calendar_id, expected_version)
        payload = serialize_profile(profile)
        async with self._factory() as session, session.begin():
            user, row, member = await self._access(session, actor, calendar_id, expected_version)
            if self._summary(row, member, user.id).role not in (
                CalendarRole.OWNER,
                CalendarRole.EDITOR,
                CalendarRole.ADMIN,
            ):
                raise CalendarAccessDenied()
            row.name, row.snapshot_payload = profile.name, payload
            row.version += 1
            row.snapshot_version += 1
            await session.flush()
            return self._summary(row, member, user.id)

    async def set_visible(
        self,
        actor: int,
        calendar_id: UUID,
        expected_version: int,
        visible: bool,
    ) -> CalendarSummary:
        self._inputs(actor, calendar_id, expected_version)
        if type(visible) is not bool:
            raise ValueError("visibility must be boolean")
        async with self._factory() as session, session.begin():
            user, row, member = await self._access(session, actor, calendar_id, expected_version)
            if row.owner_user_id == user.id:
                row.owner_visible = visible
            else:
                assert member is not None
                member.visible = visible
            row.version += 1
            await session.flush()
            return self._summary(row, member, user.id)

    async def delete_calendar(self, actor: int, calendar_id: UUID, expected_version: int) -> None:
        self._inputs(actor, calendar_id, expected_version)
        async with self._factory() as session, session.begin():
            user, row, _ = await self._access(session, actor, calendar_id, expected_version)
            self._owner(row, user.id)
            await session.delete(row)
            await session.flush()

    async def leave_calendar(self, actor: int, calendar_id: UUID, expected_version: int) -> None:
        self._inputs(actor, calendar_id, expected_version)
        async with self._factory() as session, session.begin():
            user, row, member = await self._access(session, actor, calendar_id, expected_version)
            if row.owner_user_id == user.id or member is None:
                raise CalendarAccessDenied()
            await session.delete(member)
            row.version += 1
            await session.flush()

    async def set_role(
        self,
        actor: int,
        calendar_id: UUID,
        target: int,
        expected_version: int,
        role: CalendarRole,
    ) -> CalendarSummary:
        self._inputs(actor, calendar_id, expected_version)
        _user_id(target)
        if not isinstance(role, CalendarRole) or role is CalendarRole.OWNER:
            raise ValueError("owner role cannot be granted")
        async with self._factory() as session, session.begin():
            user, row, own_member = await self._access(
                session, actor, calendar_id, expected_version
            )
            self._owner(row, user.id)
            recipient = await self._user(session, target)
            if recipient.id == row.owner_user_id:
                raise CalendarAccessDenied()
            member = await self._membership(session, calendar_id, recipient.id)
            if member is None:
                raise CalendarDataUnavailable()
            member.role = role.value
            row.version += 1
            await session.flush()
            return self._summary(row, own_member, user.id)

    async def create_invitation(
        self,
        actor: int,
        calendar_id: UUID,
        target: int,
        expected_version: int,
        now: datetime,
    ) -> str:
        self._inputs(actor, calendar_id, expected_version)
        _user_id(target)
        timestamp = _now(now)
        token = secrets.token_urlsafe(32)
        async with self._factory() as session, session.begin():
            # Every multi-user lock is sorted, independent of invitation direction.
            users = (
                (
                    await session.execute(
                        select(User)
                        .where(User.telegram_user_id.in_(sorted({actor, target})))
                        .order_by(User.telegram_user_id)
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            by_id = {user.telegram_user_id: user for user in users}
            if actor not in by_id or target not in by_id:
                raise CalendarDataUnavailable()
            user, recipient = by_id[actor], by_id[target]
            row = await self._calendar(session, calendar_id)
            self._owner(row, user.id)
            self._cas(row, expected_version)
            if (
                recipient.id == row.owner_user_id
                or await self._membership(session, calendar_id, recipient.id) is not None
            ):
                raise CalendarConflict()
            await self._quota(session, recipient.id)
            pending = await session.scalar(
                select(func.count())
                .select_from(CalendarInvitation)
                .where(
                    CalendarInvitation.calendar_id == calendar_id,
                    CalendarInvitation.revoked_at.is_(None),
                    CalendarInvitation.consumed_at.is_(None),
                    CalendarInvitation.expires_at > timestamp,
                )
            )
            if pending is None or pending >= 20:
                raise CalendarLimitExceeded()
            row.version += 1
            session.add(
                CalendarInvitation(
                    token_hash=digest_invitation_token(token),
                    calendar_id=calendar_id,
                    target_user_id=recipient.id,
                    issuer_user_id=user.id,
                    calendar_version=row.snapshot_version,
                    created_at=timestamp,
                    expires_at=timestamp + timedelta(hours=24),
                )
            )
            await session.flush()
        return token

    async def preview_invitation(
        self,
        target: int,
        token: str,
        now: datetime,
    ) -> InvitationPreview:
        _user_id(target)
        digest, timestamp = digest_invitation_token(token), _now(now)
        async with self._factory() as session:
            user = await self._user(session, target)
            result = (
                await session.execute(
                    select(CalendarRow, CalendarInvitation)
                    .join(CalendarInvitation, CalendarInvitation.calendar_id == CalendarRow.id)
                    .where(CalendarInvitation.token_hash == digest)
                )
            ).one_or_none()
            if result is None:
                raise CalendarDataUnavailable()
            row, invite = result
            self._invitation(row, invite, user.id, timestamp)
            return InvitationPreview(
                CalendarSummary(
                    row.id, row.name, CalendarKind(row.kind), CalendarRole.MEMBER, True, row.version
                ),
                invite.expires_at,
            )

    async def accept_invitation(
        self,
        target: int,
        token: str,
        expected_version: int,
        now: datetime,
    ) -> CalendarSummary:
        _user_id(target)
        _version(expected_version)
        digest, timestamp = digest_invitation_token(token), _now(now)
        async with self._factory() as session, session.begin():
            user = await self._user(session, target, lock=True)
            invite = await session.get(CalendarInvitation, digest)
            if invite is None:
                raise CalendarDataUnavailable()
            row = await self._calendar(session, invite.calendar_id)
            # Re-read after calendar lock; a concurrent consume/revoke cannot slip through.
            invite = (
                await session.execute(
                    select(CalendarInvitation)
                    .where(CalendarInvitation.token_hash == digest)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            if invite is None:
                raise CalendarDataUnavailable()
            self._invitation(row, invite, user.id, timestamp)
            self._cas(row, expected_version)
            if (
                row.owner_user_id == user.id
                or await self._membership(session, row.id, user.id) is not None
            ):
                raise CalendarConflict()
            await self._quota(session, user.id)
            member = MembershipRow(
                calendar_id=row.id, user_id=user.id, role=CalendarRole.MEMBER.value, visible=True
            )
            session.add(member)
            invite.consumed_at = timestamp
            row.version += 1
            await session.flush()
            return self._summary(row, member, user.id)

    async def revoke_invitation(self, actor: int, token: str, expected_version: int) -> None:
        _user_id(actor)
        _version(expected_version)
        digest = digest_invitation_token(token)
        async with self._factory() as session, session.begin():
            user = await self._user(session, actor, lock=True)
            invite = await session.get(CalendarInvitation, digest)
            if invite is None:
                raise CalendarDataUnavailable()
            row = await self._calendar(session, invite.calendar_id)
            self._owner(row, user.id)
            self._cas(row, expected_version)
            invite = (
                await session.execute(
                    select(CalendarInvitation)
                    .where(CalendarInvitation.token_hash == digest)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            if invite is None or invite.consumed_at is not None or invite.revoked_at is not None:
                raise CalendarConflict()
            invite.revoked_at = datetime.now(UTC)
            row.version += 1
            await session.flush()

    @staticmethod
    def _inputs(actor: int, identity: UUID, version: int) -> None:
        _user_id(actor)
        _calendar_id(identity)
        _version(version)

    @staticmethod
    async def _user(session: AsyncSession, telegram_id: int, *, lock: bool = False) -> User:
        statement = select(User).where(User.telegram_user_id == telegram_id)
        if lock:
            statement = statement.with_for_update()
        user = (await session.execute(statement)).scalar_one_or_none()
        if user is None:
            raise CalendarDataUnavailable()
        return user

    @staticmethod
    async def _calendar(session: AsyncSession, identity: UUID) -> CalendarRow:
        row = (
            await session.execute(
                select(CalendarRow).where(CalendarRow.id == identity).with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            raise CalendarDataUnavailable()
        return row

    @staticmethod
    async def _membership(
        session: AsyncSession,
        calendar_id: UUID,
        user_id: UUID,
    ) -> MembershipRow | None:
        return (
            await session.execute(
                select(MembershipRow).where(
                    MembershipRow.calendar_id == calendar_id,
                    MembershipRow.user_id == user_id,
                )
            )
        ).scalar_one_or_none()

    @classmethod
    async def _access(
        cls,
        session: AsyncSession,
        actor: int,
        identity: UUID,
        expected_version: int,
    ) -> tuple[User, CalendarRow, MembershipRow | None]:
        user = await cls._user(session, actor, lock=True)
        row = await cls._calendar(session, identity)
        member = await cls._membership(session, identity, user.id)
        if row.owner_user_id != user.id and member is None:
            raise CalendarAccessDenied()
        cls._cas(row, expected_version)
        return user, row, member

    @staticmethod
    def _owner(row: CalendarRow, user_id: UUID) -> None:
        if row.owner_user_id != user_id:
            raise CalendarAccessDenied()

    @staticmethod
    def _cas(row: CalendarRow, version: int) -> None:
        if row.version != version or row.version >= 2**31 - 1:
            raise CalendarConflict()

    @staticmethod
    def _summary(
        row: CalendarRow,
        member: MembershipRow | None,
        user_id: UUID,
    ) -> CalendarSummary:
        if row.owner_user_id == user_id:
            role, visible = CalendarRole.OWNER, row.owner_visible
        elif member is not None and member.user_id == user_id and member.calendar_id == row.id:
            role, visible = CalendarRole(member.role), member.visible
        else:
            raise CalendarAccessDenied()
        return CalendarSummary(row.id, row.name, CalendarKind(row.kind), role, visible, row.version)

    @staticmethod
    async def _rows(
        session: AsyncSession,
        user_id: UUID,
    ) -> list[tuple[CalendarRow, MembershipRow | None]]:
        result = (
            await session.execute(
                select(CalendarRow, MembershipRow)
                .outerjoin(
                    MembershipRow,
                    and_(
                        MembershipRow.calendar_id == CalendarRow.id,
                        MembershipRow.user_id == user_id,
                    ),
                )
                .where(or_(CalendarRow.owner_user_id == user_id, MembershipRow.user_id == user_id))
                .order_by(CalendarRow.id)
                .limit(MAX_PERSISTENT_CALENDARS + 1)
            )
        ).all()
        if len(result) > MAX_PERSISTENT_CALENDARS:
            raise CalendarLimitExceeded()
        return [(row, member) for row, member in result]

    @classmethod
    async def _quota(cls, session: AsyncSession, user_id: UUID) -> None:
        if len(await cls._rows(session, user_id)) >= MAX_PERSISTENT_CALENDARS:
            raise CalendarLimitExceeded()

    @staticmethod
    def _invitation(
        row: CalendarRow,
        invite: CalendarInvitation,
        user_id: UUID,
        now: datetime,
    ) -> None:
        if (
            invite.target_user_id != user_id
            or invite.issuer_user_id != row.owner_user_id
            or invite.calendar_id != row.id
            or invite.revoked_at is not None
            or invite.consumed_at is not None
            or not _now(invite.created_at) <= now < _now(invite.expires_at)
            or _now(invite.expires_at) > _now(invite.created_at) + timedelta(hours=24)
            or invite.calendar_version != row.snapshot_version
        ):
            raise CalendarConflict()

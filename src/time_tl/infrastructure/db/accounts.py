from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Sequence
from uuid import UUID, uuid4

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from time_tl.bot.models import NotificationMode
from time_tl.bot.service import UserAccount
from time_tl.domain.schedule import DateOverride, Lesson, Parity, ScheduleProfile, ScheduleRule

from .codec import LessonLinkCodec
from .models import (
    AlertEpisode,
    AlertScope,
    AlertSnapshot,
    OutboxMessage,
    OverrideLesson,
    RuleLesson,
    RuntimeSetting,
    User,
)
from .models import DateOverride as DateOverrideRow
from .models import (
    ScheduleProfile as ProfileRow,
)
from .models import (
    ScheduleRule as RuleRow,
)

_MODE_TO_DB = {
    NotificationMode.LESSONS: "LESSONS_ONLY",
    NotificationMode.LESSONS_AND_SIRENS: "LESSONS_AND_ALERTS",
    NotificationMode.SIRENS: "ALERTS_ONLY",
}
_DB_TO_MODE = {value: key for key, value in _MODE_TO_DB.items()}


class _AccountDeleteRetry(RuntimeError):
    pass


class AsyncPostgresAccountRepository:
    def __init__(
        self, factory: async_sessionmaker[AsyncSession], link_codec: LessonLinkCodec | None = None
    ) -> None:
        self._factory = factory
        self._link_codec = link_codec

    async def get_or_create(self, user_id: int) -> UserAccount:
        async with self._factory() as session:
            async with session.begin():
                row = (
                    await session.execute(
                        select(User).where(User.telegram_user_id == user_id).with_for_update()
                    )
                ).scalar_one_or_none()
                if row is None:
                    row = User(telegram_user_id=user_id, mode=None)
                    session.add(row)
                    await session.flush()
                return await self._load(session, row)

    async def get_existing(self, telegram_user_id: int) -> UserAccount | None:
        """Hydrate an existing account without locking or creating any row."""
        async with self._factory() as session:
            row = (
                await session.execute(select(User).where(User.telegram_user_id == telegram_user_id))
            ).scalar_one_or_none()
            if row is None:
                return None
            return await self._load(session, row)

    async def save(self, account: UserAccount) -> None:
        async with self._factory() as session:
            async with session.begin():
                await self.save_in_session(session, account)

    async def delete_account(self, telegram_user_id: int) -> bool:
        """Delete one account using the sender-compatible outbox -> owner lock order.

        A sender already holding an outbox row is allowed to finish before
        deletion. If a concurrent mutation owns the user row, NOWAIT aborts
        this transaction (releasing outbox locks) and retries instead of
        creating an inverse user -> outbox deadlock.
        """
        for attempt in range(3):
            try:
                return await self._delete_account_once(telegram_user_id)
            except _AccountDeleteRetry:
                if attempt == 2:
                    raise RuntimeError("account changed during deletion") from None
                await asyncio.sleep(0.05 * (attempt + 1))
            except DBAPIError as exc:
                if not self._is_lock_not_available(exc) or attempt == 2:
                    raise
                await asyncio.sleep(0.05 * (attempt + 1))
        raise AssertionError("unreachable account deletion retry state")

    async def _delete_account_once(self, telegram_user_id: int) -> bool:
        async with self._factory() as session:
            async with session.begin():
                user_id = (
                    await session.execute(
                        select(User.id).where(User.telegram_user_id == telegram_user_id)
                    )
                ).scalar_one_or_none()
                if user_id is None:
                    return False
                scope_id = (
                    await session.execute(
                        select(AlertScope.id).where(AlertScope.user_id == user_id)
                    )
                ).scalar_one_or_none()
                targets = [
                    and_(
                        OutboxMessage.aggregate_type == "schedule",
                        OutboxMessage.aggregate_id == str(user_id),
                    ),
                    and_(
                        OutboxMessage.aggregate_type == "power",
                        OutboxMessage.aggregate_id == str(user_id),
                    ),
                    and_(
                        OutboxMessage.aggregate_type == "power_map",
                        OutboxMessage.aggregate_id == str(user_id),
                    ),
                    and_(
                        OutboxMessage.aggregate_type == "weather",
                        OutboxMessage.aggregate_id == str(user_id),
                    ),
                ]
                if scope_id is not None:
                    targets.append(
                        and_(
                            OutboxMessage.aggregate_type == "alert_scope",
                            OutboxMessage.aggregate_id == str(scope_id),
                        )
                    )
                predicate = or_(*targets)
                await session.execute(
                    select(OutboxMessage.id)
                    .where(predicate)
                    .order_by(OutboxMessage.id)
                    .with_for_update()
                )
                user = (
                    await session.execute(
                        select(User).where(User.id == user_id).with_for_update(nowait=True)
                    )
                ).scalar_one_or_none()
                if user is None:
                    return False
                locked_scope_id = (
                    await session.execute(
                        select(AlertScope.id)
                        .where(AlertScope.user_id == user.id)
                        .with_for_update(nowait=True)
                    )
                ).scalar_one_or_none()
                if locked_scope_id != scope_id:
                    raise _AccountDeleteRetry
                await session.execute(delete(OutboxMessage).where(predicate))
                await session.execute(
                    delete(RuntimeSetting).where(
                        RuntimeSetting.key.in_(
                            (
                                "power_api_user_pref:" + str(user.id),
                                "power_api_reminder_state:" + str(user.id),
                                "power_api_emergency_state:" + str(user.id),
                            )
                        )
                    )
                )
                await session.execute(delete(User).where(User.id == user.id))
                return True

    @staticmethod
    def _is_lock_not_available(error: DBAPIError) -> bool:
        pending: list[BaseException] = [error]
        seen: set[int] = set()
        while pending:
            current = pending.pop()
            if id(current) in seen:
                continue
            seen.add(id(current))
            if (
                getattr(current, "sqlstate", None) == "55P03"
                or getattr(current, "pgcode", None) == "55P03"
            ):
                return True
            for candidate in (
                getattr(current, "orig", None),
                current.__cause__,
                current.__context__,
            ):
                if isinstance(candidate, BaseException):
                    pending.append(candidate)
        return False

    async def load_for_update_in_session(
        self, session: AsyncSession, telegram_user_id: int
    ) -> UserAccount:
        """Load an existing account under row lock inside the caller transaction."""
        row = (
            await session.execute(
                select(User).where(User.telegram_user_id == telegram_user_id).with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            raise LookupError(telegram_user_id)
        return await self._load(session, row)

    async def save_in_session(self, session: AsyncSession, account: UserAccount) -> None:
        """Persist account and profile diff without owning commit/rollback."""
        row = (
            await session.execute(
                select(User).where(User.telegram_user_id == account.user_id).with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            row = User(telegram_user_id=account.user_id)
            session.add(row)
            await session.flush()
        next_mode = _MODE_TO_DB.get(account.mode) if account.mode else None
        next_alerts_enabled = account.mode in {
            NotificationMode.LESSONS_AND_SIRENS,
            NotificationMode.SIRENS,
        }
        await self._apply_alert_mode_transition(
            session,
            row,
            next_mode=next_mode,
            next_alerts_enabled=next_alerts_enabled,
        )
        row.mode = next_mode
        row.confirmed_region_id = account.region_id
        row.region_name = account.region_name
        row.reminder_lead_minutes = account.reminder_lead_minutes
        row.alerts_enabled = next_alerts_enabled
        row.lessons_enabled = account.mode in {
            NotificationMode.LESSONS,
            NotificationMode.LESSONS_AND_SIRENS,
        }
        await self._sync_profiles(session, row.id, account.profiles)

    async def _apply_alert_mode_transition(
        self,
        session: AsyncSession,
        user: User,
        *,
        next_mode: str | None,
        next_alerts_enabled: bool,
    ) -> None:
        previous_alerts_enabled = bool(user.alerts_enabled)
        alerts_changed = previous_alerts_enabled != next_alerts_enabled
        return_changed = (user.mode == "LESSONS_AND_ALERTS") != (next_mode == "LESSONS_AND_ALERTS")
        if not alerts_changed and not return_changed:
            return

        scope = None
        if alerts_changed:
            scope = (
                await session.execute(
                    select(AlertScope).where(AlertScope.user_id == user.id).with_for_update()
                )
            ).scalar_one_or_none()

        invalidation_targets = []
        if alerts_changed and scope is not None:
            invalidation_targets.append(
                and_(
                    OutboxMessage.aggregate_type == "alert_scope",
                    OutboxMessage.aggregate_id == str(scope.id),
                    OutboxMessage.event_type.in_(("ALERT_STARTED", "ALL_CLEAR_CONFIRMED")),
                )
            )
        if return_changed:
            invalidation_targets.append(
                and_(
                    OutboxMessage.aggregate_type == "schedule",
                    OutboxMessage.aggregate_id == str(user.id),
                    OutboxMessage.event_type == "RETURN_TO_ACTIVE_LESSON",
                )
            )
        if invalidation_targets:
            await session.execute(
                update(OutboxMessage)
                .where(
                    OutboxMessage.delivered_at.is_(None),
                    OutboxMessage.dead_at.is_(None),
                    or_(*invalidation_targets),
                )
                .values(
                    dead_at=func.now(),
                    last_error_code="mode_generation_invalidated",
                )
            )

        if previous_alerts_enabled or not next_alerts_enabled or scope is None:
            return
        await session.execute(delete(AlertEpisode).where(AlertEpisode.scope_id == scope.id))
        snapshot = (
            await session.execute(
                select(AlertSnapshot).where(AlertSnapshot.scope_id == scope.id).with_for_update()
            )
        ).scalar_one_or_none()
        if snapshot is not None:
            snapshot.state = "UNKNOWN"
            snapshot.last_fetch_revision = None
            snapshot.transition_revision = None
            snapshot.last_full_active_episodes = None
            snapshot.processed_update_ids = []
            snapshot.pending_clear_candidate = False

    async def sync_profiles_in_session(
        self,
        session: AsyncSession,
        telegram_user_id: int,
        profiles: dict[str, ScheduleProfile],
    ) -> None:
        """Sync only profiles within an already-open import transaction."""
        row = (
            await session.execute(
                select(User).where(User.telegram_user_id == telegram_user_id).with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            raise LookupError(telegram_user_id)
        await self._sync_profiles(session, row.id, profiles)

    async def _sync_profiles(
        self,
        session: AsyncSession,
        user_id: UUID,
        desired: dict[str, ScheduleProfile],
    ) -> None:
        """Diff profiles without invalidating IDs referenced by shares/imports."""
        existing = {
            item.public_id: item
            for item in (
                await session.execute(
                    select(ProfileRow).where(ProfileRow.user_id == user_id).with_for_update()
                )
            ).scalars()
        }
        desired_active = {key for key, profile in desired.items() if profile.active}
        existing_active = {key for key, item in existing.items() if item.is_active}
        if desired_active != existing_active:
            for item in existing.values():
                item.is_active = False
            await session.flush()

        for public_id, profile in desired.items():
            row = existing.pop(public_id, None)
            if row is None:
                await self._add_profile(session, user_id, profile)
                continue
            current = await self._load_profile(session, row)
            metadata_changed = (
                row.name != profile.name
                or row.timezone != profile.timezone
                or row.is_active != profile.active
            )
            contents_changed = (
                current.rules != profile.rules or current.overrides != profile.overrides
            )
            if not metadata_changed and not contents_changed:
                continue
            row.name = profile.name
            row.timezone = profile.timezone
            row.is_active = profile.active
            row.version = (row.version or 0) + 1
            if contents_changed:
                await self._replace_profile_contents(session, row.id, profile)

        if existing:
            await session.execute(
                delete(ProfileRow).where(ProfileRow.id.in_([item.id for item in existing.values()]))
            )

    async def _replace_profile_contents(
        self, session: AsyncSession, profile_id: UUID, profile: ScheduleProfile
    ) -> None:
        """Synchronize children while retaining IDs carried by loaded lessons.

        Outbox messages contain opaque lesson-row references. Replacing every
        child on a harmless edit made those references disappear before the
        sender could resolve them. A reference is reused only when it belongs
        to the same, locked parent row; caller-supplied/cross-parent IDs are
        never trusted.
        """
        existing_rules = (
            (
                await session.execute(
                    select(RuleRow)
                    .where(RuleRow.profile_id == profile_id)
                    .order_by(RuleRow.position)
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        rule_by_signature: dict[tuple[object, ...], list[RuleRow]] = defaultdict(list)
        for existing_rule in existing_rules:
            rule_by_signature[self._rule_signature_row(existing_rule)].append(existing_rule)
            existing_rule.position = -1 - existing_rule.position
        await session.flush()
        used_rule_ids: set[UUID] = set()
        for position, rule in enumerate(profile.rules):
            candidates = rule_by_signature.get(self._rule_signature(rule), [])
            rule_row = next((item for item in candidates if item.id not in used_rule_ids), None)
            carries_owned_reference = any(
                self._lesson_reference(lesson.storage_ref, "rule") is not None
                for lesson in rule.lessons
            )
            if (
                rule_row is None
                and carries_owned_reference
                and position < len(existing_rules)
                and existing_rules[position].id not in used_rule_ids
            ):
                # An edit may change the rule signature. Position is used only
                # for a hydrated rule carrying opaque DB references, so a new
                # imported rule cannot steal an unrelated row identity.
                rule_row = existing_rules[position]
            if rule_row is None:
                rule_row = RuleRow(id=uuid4(), profile_id=profile_id, position=position)
                session.add(rule_row)
            used_rule_ids.add(rule_row.id)
            rule_row.weekday = rule.weekday
            rule_row.week_parity = rule.week_parity.value
            rule_row.date_parity = rule.date_parity.value
            rule_row.valid_from = rule.valid_from
            rule_row.valid_to = rule.valid_to
            rule_row.position = position
            await session.flush()
            await self._sync_rule_lessons(session, rule_row.id, rule.lessons)
        unused_rule_ids = [item.id for item in existing_rules if item.id not in used_rule_ids]
        if unused_rule_ids:
            await session.execute(delete(RuleRow).where(RuleRow.id.in_(unused_rule_ids)))

        existing_overrides = (
            (
                await session.execute(
                    select(DateOverrideRow)
                    .where(DateOverrideRow.profile_id == profile_id)
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        override_by_day = {item.day: item for item in existing_overrides}
        used_override_ids: set[UUID] = set()
        for override in profile.overrides:
            override_row = override_by_day.get(override.day)
            if override_row is None:
                override_row = DateOverrideRow(id=uuid4(), profile_id=profile_id, day=override.day)
                session.add(override_row)
            used_override_ids.add(override_row.id)
            override_row.day_off = override.day_off
            await session.flush()
            await self._sync_override_lessons(session, override_row.id, override.lessons)
        unused_override_ids = [
            item.id for item in existing_overrides if item.id not in used_override_ids
        ]
        if unused_override_ids:
            await session.execute(
                delete(DateOverrideRow).where(DateOverrideRow.id.in_(unused_override_ids))
            )

    @staticmethod
    def _rule_signature(rule: ScheduleRule) -> tuple[object, ...]:
        return (
            rule.weekday,
            rule.week_parity.value,
            rule.date_parity.value,
            rule.valid_from,
            rule.valid_to,
        )

    @staticmethod
    def _rule_signature_row(rule: RuleRow) -> tuple[object, ...]:
        return (
            rule.weekday,
            rule.week_parity,
            rule.date_parity,
            rule.valid_from,
            rule.valid_to,
        )

    async def _sync_rule_lessons(
        self, session: AsyncSession, rule_id: UUID, lessons: tuple[Lesson, ...]
    ) -> None:
        rows = (
            (
                await session.execute(
                    select(RuleLesson)
                    .where(RuleLesson.rule_id == rule_id)
                    .order_by(RuleLesson.position)
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        await self._sync_lessons(session, rows, lessons, parent_id=rule_id, kind="rule")

    async def _sync_override_lessons(
        self, session: AsyncSession, override_id: UUID, lessons: tuple[Lesson, ...]
    ) -> None:
        rows = (
            (
                await session.execute(
                    select(OverrideLesson)
                    .where(OverrideLesson.override_id == override_id)
                    .order_by(OverrideLesson.position)
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        await self._sync_lessons(session, rows, lessons, parent_id=override_id, kind="override")

    async def _sync_lessons(
        self,
        session: AsyncSession,
        existing: Sequence[RuleLesson | OverrideLesson],
        desired: tuple[Lesson, ...],
        *,
        parent_id: UUID,
        kind: str,
    ) -> None:
        by_id: dict[UUID, RuleLesson | OverrideLesson] = {item.id: item for item in existing}
        for index, existing_lesson in enumerate(existing):
            existing_lesson.position = -1 - index
        await session.flush()
        used: set[UUID] = set()
        for position, lesson in enumerate(desired):
            referenced = self._lesson_reference(lesson.storage_ref, kind)
            lesson_row: RuleLesson | OverrideLesson | None = (
                by_id.get(referenced) if referenced is not None and referenced not in used else None
            )
            if lesson_row is None:
                if kind == "rule":
                    lesson_row = RuleLesson(id=uuid4(), rule_id=parent_id)
                else:
                    lesson_row = OverrideLesson(id=uuid4(), override_id=parent_id)
                session.add(lesson_row)
            used.add(lesson_row.id)
            lesson_row.position = position
            lesson_row.start_time = lesson.start
            lesson_row.end_time = lesson.end
            lesson_row.title = lesson.title
            lesson_row.enabled = lesson.enabled
            lesson_row.join_url_ciphertext = self._encrypt_link(lesson.join_url)
        unused = [item.id for item in existing if item.id not in used]
        if unused:
            model = RuleLesson if kind == "rule" else OverrideLesson
            await session.execute(delete(model).where(model.id.in_(unused)))

    @staticmethod
    def _lesson_reference(storage_ref: str | None, expected_kind: str) -> UUID | None:
        if storage_ref is None:
            return None
        prefix = f"lesson:{expected_kind}:"
        if not storage_ref.startswith(prefix):
            return None
        try:
            return UUID(storage_ref.removeprefix(prefix))
        except ValueError:
            return None

    async def _load(self, session: AsyncSession, row: User) -> UserAccount:
        profiles = (
            (
                await session.execute(
                    select(ProfileRow)
                    .where(ProfileRow.user_id == row.id)
                    .order_by(ProfileRow.created_at)
                )
            )
            .scalars()
            .all()
        )
        result: dict[str, ScheduleProfile] = {}
        for profile in profiles:
            result[profile.public_id] = await self._load_profile(session, profile)
        return UserAccount(
            user_id=row.telegram_user_id,
            mode=_DB_TO_MODE.get(row.mode) if row.mode is not None else None,
            region_id=row.confirmed_region_id,
            region_name=row.region_name,
            reminder_lead_minutes=row.reminder_lead_minutes,
            profiles=result,
        )

    async def _load_profile(self, session: AsyncSession, profile: ProfileRow) -> ScheduleProfile:
        rules = (
            (
                await session.execute(
                    select(RuleRow)
                    .where(RuleRow.profile_id == profile.id)
                    .order_by(RuleRow.position)
                )
            )
            .scalars()
            .all()
        )
        rule_lessons: defaultdict[UUID, list[Lesson]] = defaultdict(list)
        if rules:
            rule_lesson_rows = (
                (
                    await session.execute(
                        select(RuleLesson)
                        .where(RuleLesson.rule_id.in_([r.id for r in rules]))
                        .order_by(RuleLesson.position)
                    )
                )
                .scalars()
                .all()
            )
            for rule_item in rule_lesson_rows:
                rule_lessons[rule_item.rule_id].append(self._lesson(rule_item))
        overrides = (
            (
                await session.execute(
                    select(DateOverrideRow)
                    .where(DateOverrideRow.profile_id == profile.id)
                    .order_by(DateOverrideRow.day)
                )
            )
            .scalars()
            .all()
        )
        override_lessons: defaultdict[UUID, list[Lesson]] = defaultdict(list)
        if overrides:
            override_lesson_rows = (
                (
                    await session.execute(
                        select(OverrideLesson)
                        .where(OverrideLesson.override_id.in_([o.id for o in overrides]))
                        .order_by(OverrideLesson.position)
                    )
                )
                .scalars()
                .all()
            )
            for override_item in override_lesson_rows:
                override_lessons[override_item.override_id].append(self._lesson(override_item))
        return ScheduleProfile(
            profile_id=profile.public_id,
            name=profile.name,
            timezone=profile.timezone,
            active=profile.is_active,
            rules=tuple(
                ScheduleRule(
                    weekday=r.weekday,
                    week_parity=Parity(r.week_parity),
                    date_parity=Parity(r.date_parity),
                    valid_from=r.valid_from,
                    valid_to=r.valid_to,
                    lessons=tuple(rule_lessons[r.id]),
                )
                for r in rules
            ),
            overrides=tuple(
                DateOverride(day=o.day, day_off=o.day_off, lessons=tuple(override_lessons[o.id]))
                for o in overrides
            ),
        )

    def _lesson(self, row: RuleLesson | OverrideLesson) -> Lesson:
        join_url = None
        if row.join_url_ciphertext is not None:
            if self._link_codec is None:
                raise RuntimeError("lesson URL decryption adapter is not configured")
            join_url = self._link_codec.decrypt(row.join_url_ciphertext)
        return Lesson(
            start=row.start_time,
            end=row.end_time,
            title=row.title,
            enabled=row.enabled,
            join_url=join_url,
            storage_ref=(
                f"lesson:rule:{row.id}"
                if isinstance(row, RuleLesson)
                else f"lesson:override:{row.id}"
            ),
        )

    async def _add_profile(
        self,
        session: AsyncSession,
        user_id: UUID,
        profile: ScheduleProfile,
        *,
        version: int = 1,
    ) -> None:
        row = ProfileRow(
            id=uuid4(),
            user_id=user_id,
            public_id=profile.profile_id,
            name=profile.name,
            timezone=profile.timezone,
            is_active=profile.active,
            version=version,
        )
        session.add(row)
        await session.flush()
        await self._add_profile_contents(session, row.id, profile)

    async def _add_profile_contents(
        self, session: AsyncSession, profile_id: UUID, profile: ScheduleProfile
    ) -> None:
        for rp, rule in enumerate(profile.rules):
            rr = RuleRow(
                id=uuid4(),
                profile_id=profile_id,
                weekday=rule.weekday,
                week_parity=rule.week_parity.value,
                date_parity=rule.date_parity.value,
                valid_from=rule.valid_from,
                valid_to=rule.valid_to,
                position=rp,
            )
            session.add(rr)
            await session.flush()
            for lp, lesson in enumerate(rule.lessons):
                session.add(
                    RuleLesson(
                        id=uuid4(),
                        rule_id=rr.id,
                        position=lp,
                        start_time=lesson.start,
                        end_time=lesson.end,
                        title=lesson.title,
                        enabled=lesson.enabled,
                        join_url_ciphertext=self._encrypt_link(lesson.join_url),
                    )
                )
        for _op, override in enumerate(profile.overrides):
            oo = DateOverrideRow(
                id=uuid4(), profile_id=profile_id, day=override.day, day_off=override.day_off
            )
            session.add(oo)
            await session.flush()
            for lp, lesson in enumerate(override.lessons):
                session.add(
                    OverrideLesson(
                        id=uuid4(),
                        override_id=oo.id,
                        position=lp,
                        start_time=lesson.start,
                        end_time=lesson.end,
                        title=lesson.title,
                        enabled=lesson.enabled,
                        join_url_ciphertext=self._encrypt_link(lesson.join_url),
                    )
                )

    def _encrypt_link(self, value: str | None) -> bytes | None:
        if value is None:
            return None
        if self._link_codec is None:
            raise RuntimeError("lesson URL encryption adapter is required before persistence")
        return self._link_codec.encrypt(value)

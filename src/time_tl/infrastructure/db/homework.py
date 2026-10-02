from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from time_tl.domain.homework import (
    HomeworkConflict,
    HomeworkDraft,
    HomeworkItem,
    HomeworkNotFound,
    HomeworkSourceType,
    HomeworkSubject,
    HomeworkValidationError,
    find_next_subject_lesson,
    normalize_subject_name,
    require_aware_datetime,
    validate_homework_text,
    validate_subject_name,
)
from time_tl.domain.schedule import DateOverride, Lesson, Parity, ScheduleProfile, ScheduleRule

from .models import DateOverride as DateOverrideRow
from .models import (
    HomeworkAssignment,
    HomeworkUserState,
    OverrideLesson,
    PendingHomeworkDraft,
    RuleLesson,
    ScheduleSubject,
    User,
)
from .models import (
    ScheduleProfile as ProfileRow,
)
from .models import (
    ScheduleRule as RuleRow,
)

MAX_HOMEWORK_LIST_ITEMS = 100
MAX_HOMEWORK_ASSIGNMENTS_PER_USER = 500
MAX_DRAFT_TOKEN_LENGTH = 512
MIN_DRAFT_TOKEN_LENGTH = 32


def digest_homework_token(value: str) -> str:
    if not MIN_DRAFT_TOKEN_LENGTH <= len(value) <= MAX_DRAFT_TOKEN_LENGTH:
        raise HomeworkValidationError("homework draft token length is invalid")
    if value.strip() != value or any(character.isspace() for character in value):
        raise HomeworkValidationError("homework draft token is invalid")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_homework_source(value: HomeworkSourceType | str) -> HomeworkSourceType:
    try:
        source = HomeworkSourceType(value)
    except ValueError as exc:
        raise HomeworkValidationError("unsupported homework source type") from exc
    if source is not HomeworkSourceType.MANUAL:
        raise HomeworkValidationError("unsupported homework source type")
    return source


class AsyncPostgresHomeworkRepository:
    """Personal homework persistence isolated from alerts and the message outbox."""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    async def list_subjects(self, telegram_user_id: int) -> tuple[HomeworkSubject, ...]:
        async with self._factory() as session:
            async with session.begin():
                user = await self._locked_user(session, telegram_user_id)
                profile = await self._active_profile(session, user.id)
                current_titles = await self._current_subject_titles(session, profile.id)
                current_by_normalized: dict[str, str] = {}
                for raw_title in sorted(
                    current_titles,
                    key=lambda title: (
                        normalize_subject_name(title),
                        validate_subject_name(title).casefold() != normalize_subject_name(title),
                        title.casefold(),
                        title,
                    ),
                ):
                    original_name = validate_subject_name(raw_title)
                    display_name = original_name.rstrip(". ") or original_name
                    current_by_normalized.setdefault(
                        normalize_subject_name(display_name), display_name
                    )
                if not current_by_normalized:
                    return ()

                existing: defaultdict[str, list[ScheduleSubject]] = defaultdict(list)
                for item in (
                    await session.execute(
                        select(ScheduleSubject)
                        .where(ScheduleSubject.profile_id == profile.id)
                        .with_for_update()
                    )
                ).scalars():
                    # Older rows may have keys created before alias/dot normalization.
                    # Keep every row and its foreign keys; group by its current name.
                    existing[normalize_subject_name(item.name)].append(item)
                selected: list[ScheduleSubject] = []
                for normalized, name in current_by_normalized.items():
                    candidates = existing.get(normalized, [])
                    row = min(
                        candidates,
                        key=lambda item: (item.normalized_name != normalized, item.id.int),
                        default=None,
                    )
                    if row is None:
                        row = ScheduleSubject(
                            id=uuid4(),
                            profile_id=profile.id,
                            normalized_name=normalized,
                            name=name,
                        )
                        session.add(row)
                    elif row.name != name:
                        row.name = name
                    selected.append(row)
                await session.flush()
                return tuple(
                    self._subject(item) for item in sorted(selected, key=lambda item: item.name)
                )

    async def suggest_next_lesson(
        self,
        telegram_user_id: int,
        subject_id: UUID,
        after: datetime,
    ) -> datetime | None:
        require_aware_datetime(after, field="after")
        async with self._factory() as session:
            user = await self._user(session, telegram_user_id)
            profile_row = await self._active_profile(session, user.id)
            subject = await self._owned_subject(session, profile_row.id, subject_id)
            await self._require_current_subject(session, profile_row.id, subject)
            profile = await self._load_profile(session, profile_row)
            return find_next_subject_lesson(profile, subject.name, after)

    async def create_draft(
        self,
        telegram_user_id: int,
        token: str,
        subject_id: UUID,
        text: str,
        due_at: datetime,
        expires_at: datetime,
        source_type: HomeworkSourceType | str = HomeworkSourceType.MANUAL,
    ) -> HomeworkDraft:
        token_hash = digest_homework_token(token)
        body = validate_homework_text(text)
        require_aware_datetime(due_at, field="due_at")
        require_aware_datetime(expires_at, field="expires_at")
        now = datetime.now(UTC)
        if expires_at <= now:
            raise HomeworkValidationError("homework draft expiry must be in the future")
        source = normalize_homework_source(source_type)
        async with self._factory() as session:
            async with session.begin():
                user = await self._locked_user(session, telegram_user_id)
                profile = await self._active_profile(session, user.id)
                subject = await self._owned_subject(session, profile.id, subject_id)
                await self._require_current_subject(session, profile.id, subject)
                duplicate = await session.get(PendingHomeworkDraft, token_hash)
                if duplicate is not None:
                    raise HomeworkConflict("homework draft token is already in use")
                await session.execute(
                    update(PendingHomeworkDraft)
                    .where(
                        PendingHomeworkDraft.user_id == user.id,
                        PendingHomeworkDraft.revoked_at.is_(None),
                        PendingHomeworkDraft.consumed_at.is_(None),
                    )
                    .values(revoked_at=now)
                )
                row = PendingHomeworkDraft(
                    token_hash=token_hash,
                    user_id=user.id,
                    profile_id=profile.id,
                    subject_id=subject.id,
                    text=body,
                    due_at=due_at,
                    source_type=source.value,
                    expires_at=expires_at,
                )
                session.add(row)
                await session.flush()
                return self._draft(row, subject, token)

    async def get_draft(self, telegram_user_id: int, token: str, now: datetime) -> HomeworkDraft:
        require_aware_datetime(now, field="now")
        async with self._factory() as session:
            user = await self._user(session, telegram_user_id)
            row = await self._pending_draft(session, user.id, token, now, lock=False)
            subject = await self._owned_subject(session, row.profile_id, row.subject_id)
            return self._draft(row, subject, token)

    async def change_draft_due(
        self,
        telegram_user_id: int,
        token: str,
        due_at: datetime,
        now: datetime,
    ) -> HomeworkDraft:
        require_aware_datetime(due_at, field="due_at")
        require_aware_datetime(now, field="now")
        async with self._factory() as session:
            async with session.begin():
                user = await self._locked_user(session, telegram_user_id)
                row = await self._pending_draft(session, user.id, token, now, lock=True)
                row.due_at = due_at
                subject = await self._owned_subject(session, row.profile_id, row.subject_id)
                await session.flush()
                return self._draft(row, subject, token)

    async def discard_draft(self, telegram_user_id: int, token: str, now: datetime) -> bool:
        require_aware_datetime(now, field="now")
        async with self._factory() as session:
            async with session.begin():
                user = await self._locked_user(session, telegram_user_id)
                row = (
                    await session.execute(
                        select(PendingHomeworkDraft)
                        .where(
                            PendingHomeworkDraft.token_hash == digest_homework_token(token),
                            PendingHomeworkDraft.user_id == user.id,
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if row is None or row.consumed_at is not None:
                    return False
                if row.revoked_at is not None:
                    return True
                row.revoked_at = now
                return True

    async def confirm_draft(self, telegram_user_id: int, token: str, now: datetime) -> HomeworkItem:
        require_aware_datetime(now, field="now")
        async with self._factory() as session:
            async with session.begin():
                user = await self._locked_user(session, telegram_user_id)
                token_hash = digest_homework_token(token)
                row = (
                    await session.execute(
                        select(PendingHomeworkDraft)
                        .where(
                            PendingHomeworkDraft.token_hash == token_hash,
                            PendingHomeworkDraft.user_id == user.id,
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if row is None:
                    raise HomeworkNotFound("homework draft was not found")
                if row.consumed_at is not None:
                    if row.confirmed_homework_id is None:
                        raise HomeworkConflict("homework draft has no confirmed result")
                    return await self._owned_item(session, user.id, row.confirmed_homework_id)
                self._validate_pending(row, now)
                active_profile = await self._active_profile(session, user.id)
                if active_profile.id != row.profile_id:
                    raise HomeworkConflict("homework draft schedule is no longer active")
                subject = await self._owned_subject(session, row.profile_id, row.subject_id)
                await self._require_current_subject(session, active_profile.id, subject)
                await self._require_assignment_capacity(session, user.id)
                assignment = HomeworkAssignment(
                    id=uuid4(),
                    owner_user_id=user.id,
                    profile_id=row.profile_id,
                    subject_id=row.subject_id,
                    text=row.text,
                    due_at=row.due_at,
                    source_type=row.source_type,
                    version=1,
                )
                state = HomeworkUserState(
                    homework_id=assignment.id,
                    user_id=user.id,
                    completed=False,
                    completed_at=None,
                )
                session.add_all((assignment, state))
                row.consumed_at = now
                row.confirmed_homework_id = assignment.id
                await session.flush()
                return self._item(assignment, subject, state)

    async def list_homework(
        self,
        telegram_user_id: int,
        *,
        include_past: bool = False,
        now: datetime | None = None,
    ) -> tuple[HomeworkItem, ...]:
        reference = now or datetime.now(UTC)
        require_aware_datetime(reference, field="now")
        async with self._factory() as session:
            user = await self._user(session, telegram_user_id)
            statement = select(HomeworkAssignment).where(
                HomeworkAssignment.owner_user_id == user.id
            )
            if not include_past:
                statement = statement.where(HomeworkAssignment.due_at >= reference)
            rows = (
                (
                    await session.execute(
                        statement.order_by(
                            HomeworkAssignment.due_at, HomeworkAssignment.created_at
                        ).limit(MAX_HOMEWORK_LIST_ITEMS)
                    )
                )
                .scalars()
                .all()
            )
            items: list[HomeworkItem] = []
            for row in rows:
                items.append(await self._item_for_user(session, row, user.id))
            return tuple(items)

    async def get_homework(self, telegram_user_id: int, homework_id: UUID) -> HomeworkItem:
        async with self._factory() as session:
            user = await self._user(session, telegram_user_id)
            return await self._owned_item(session, user.id, homework_id)

    async def update_homework(
        self,
        telegram_user_id: int,
        homework_id: UUID,
        *,
        expected_version: int,
        text: str | None = None,
        due_at: datetime | None = None,
    ) -> HomeworkItem:
        if expected_version < 1:
            raise HomeworkValidationError("expected version must be positive")
        body = validate_homework_text(text) if text is not None else None
        if due_at is not None:
            require_aware_datetime(due_at, field="due_at")
        if body is None and due_at is None:
            raise HomeworkValidationError("homework update contains no changes")
        async with self._factory() as session:
            async with session.begin():
                user = await self._locked_user(session, telegram_user_id)
                row = await self._owned_assignment(session, user.id, homework_id, lock=True)
                if row.version != expected_version:
                    raise HomeworkConflict("homework was changed by another request")
                changed = False
                if body is not None and row.text != body:
                    row.text = body
                    changed = True
                if due_at is not None and row.due_at != due_at:
                    row.due_at = due_at
                    changed = True
                if changed:
                    row.version += 1
                await session.flush()
                return await self._item_for_user(session, row, user.id)

    async def delete_homework(
        self, telegram_user_id: int, homework_id: UUID, *, expected_version: int
    ) -> None:
        if expected_version < 1:
            raise HomeworkValidationError("expected version must be positive")
        async with self._factory() as session:
            async with session.begin():
                user = await self._locked_user(session, telegram_user_id)
                row = await self._owned_assignment(session, user.id, homework_id, lock=True)
                if row.version != expected_version:
                    raise HomeworkConflict("homework was changed by another request")
                await session.execute(
                    delete(HomeworkAssignment).where(HomeworkAssignment.id == row.id)
                )

    async def set_completed(
        self,
        telegram_user_id: int,
        homework_id: UUID,
        *,
        completed: bool,
        now: datetime | None = None,
    ) -> HomeworkItem:
        changed_at = now or datetime.now(UTC)
        require_aware_datetime(changed_at, field="now")
        async with self._factory() as session:
            async with session.begin():
                user = await self._locked_user(session, telegram_user_id)
                row = await self._owned_assignment(session, user.id, homework_id, lock=True)
                state = (
                    await session.execute(
                        select(HomeworkUserState)
                        .where(
                            HomeworkUserState.homework_id == homework_id,
                            HomeworkUserState.user_id == user.id,
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if state is None:
                    state = HomeworkUserState(homework_id=homework_id, user_id=user.id)
                    session.add(state)
                if bool(state.completed) != completed:
                    state.completed = completed
                    state.completed_at = changed_at if completed else None
                    state.updated_at = changed_at
                await session.flush()
                subject = await self._owned_subject(session, row.profile_id, row.subject_id)
                return self._item(row, subject, state)

    async def purge_expired_drafts(self, now: datetime, *, limit: int = 100) -> int:
        require_aware_datetime(now, field="now")
        if limit < 1 or limit > 1_000:
            raise HomeworkValidationError("draft cleanup limit is invalid")
        async with self._factory() as session:
            async with session.begin():
                token_hashes = (
                    (
                        await session.execute(
                            select(PendingHomeworkDraft.token_hash)
                            .where(
                                PendingHomeworkDraft.expires_at <= now,
                                PendingHomeworkDraft.consumed_at.is_(None),
                            )
                            .order_by(PendingHomeworkDraft.expires_at)
                            .limit(limit)
                            .with_for_update(skip_locked=True)
                        )
                    )
                    .scalars()
                    .all()
                )
                if not token_hashes:
                    return 0
                await session.execute(
                    delete(PendingHomeworkDraft).where(
                        PendingHomeworkDraft.token_hash.in_(token_hashes)
                    )
                )
                return len(token_hashes)

    async def _user(self, session: AsyncSession, telegram_user_id: int) -> User:
        row = (
            await session.execute(select(User).where(User.telegram_user_id == telegram_user_id))
        ).scalar_one_or_none()
        if row is None:
            raise HomeworkNotFound("homework owner was not found")
        return row

    async def _locked_user(self, session: AsyncSession, telegram_user_id: int) -> User:
        row = (
            await session.execute(
                select(User).where(User.telegram_user_id == telegram_user_id).with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            raise HomeworkNotFound("homework owner was not found")
        return row

    async def _active_profile(self, session: AsyncSession, user_id: UUID) -> ProfileRow:
        row = (
            await session.execute(
                select(ProfileRow).where(
                    ProfileRow.user_id == user_id, ProfileRow.is_active.is_(True)
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise HomeworkNotFound("active schedule profile was not found")
        return row

    async def _owned_subject(
        self, session: AsyncSession, profile_id: UUID, subject_id: UUID
    ) -> ScheduleSubject:
        row = (
            await session.execute(
                select(ScheduleSubject).where(
                    ScheduleSubject.id == subject_id,
                    ScheduleSubject.profile_id == profile_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise HomeworkNotFound("homework subject was not found")
        return row

    async def _owned_assignment(
        self, session: AsyncSession, user_id: UUID, homework_id: UUID, *, lock: bool
    ) -> HomeworkAssignment:
        statement = select(HomeworkAssignment).where(
            HomeworkAssignment.id == homework_id,
            HomeworkAssignment.owner_user_id == user_id,
        )
        if lock:
            statement = statement.with_for_update()
        row = (await session.execute(statement)).scalar_one_or_none()
        if row is None:
            raise HomeworkNotFound("homework was not found")
        return row

    async def _owned_item(
        self, session: AsyncSession, user_id: UUID, homework_id: UUID
    ) -> HomeworkItem:
        row = await self._owned_assignment(session, user_id, homework_id, lock=False)
        return await self._item_for_user(session, row, user_id)

    async def _item_for_user(
        self, session: AsyncSession, row: HomeworkAssignment, user_id: UUID
    ) -> HomeworkItem:
        subject = await self._owned_subject(session, row.profile_id, row.subject_id)
        state = (
            await session.execute(
                select(HomeworkUserState).where(
                    HomeworkUserState.homework_id == row.id,
                    HomeworkUserState.user_id == user_id,
                )
            )
        ).scalar_one_or_none()
        return self._item(row, subject, state)

    async def _pending_draft(
        self,
        session: AsyncSession,
        user_id: UUID,
        token: str,
        now: datetime,
        *,
        lock: bool,
    ) -> PendingHomeworkDraft:
        statement = select(PendingHomeworkDraft).where(
            PendingHomeworkDraft.token_hash == digest_homework_token(token),
            PendingHomeworkDraft.user_id == user_id,
        )
        if lock:
            statement = statement.with_for_update()
        row = (await session.execute(statement)).scalar_one_or_none()
        if row is None:
            raise HomeworkNotFound("homework draft was not found")
        self._validate_pending(row, now)
        return row

    @staticmethod
    def _validate_pending(row: PendingHomeworkDraft, now: datetime) -> None:
        if row.revoked_at is not None:
            raise HomeworkConflict("homework draft was cancelled")
        if row.consumed_at is not None:
            raise HomeworkConflict("homework draft was already confirmed")
        if row.expires_at <= now:
            raise HomeworkConflict("homework draft has expired")

    async def _current_subject_titles(
        self, session: AsyncSession, profile_id: UUID
    ) -> tuple[str, ...]:
        recurring = (
            (
                await session.execute(
                    select(RuleLesson.title)
                    .join(RuleRow, RuleLesson.rule_id == RuleRow.id)
                    .where(RuleRow.profile_id == profile_id, RuleLesson.enabled.is_(True))
                )
            )
            .scalars()
            .all()
        )
        overrides = (
            (
                await session.execute(
                    select(OverrideLesson.title)
                    .join(DateOverrideRow, OverrideLesson.override_id == DateOverrideRow.id)
                    .where(
                        DateOverrideRow.profile_id == profile_id,
                        DateOverrideRow.day_off.is_(False),
                        OverrideLesson.enabled.is_(True),
                    )
                )
            )
            .scalars()
            .all()
        )
        return tuple([*recurring, *overrides])

    async def _require_assignment_capacity(self, session: AsyncSession, user_id: UUID) -> None:
        """Enforce the owner quota while the caller holds the user row lock."""

        count = (
            await session.execute(
                select(func.count(HomeworkAssignment.id)).where(
                    HomeworkAssignment.owner_user_id == user_id
                )
            )
        ).scalar_one()
        if count >= MAX_HOMEWORK_ASSIGNMENTS_PER_USER:
            raise HomeworkConflict("homework assignment limit was reached")

    async def _require_current_subject(
        self,
        session: AsyncSession,
        profile_id: UUID,
        subject: ScheduleSubject,
    ) -> None:
        current = {
            normalize_subject_name(title)
            for title in await self._current_subject_titles(session, profile_id)
        }
        if normalize_subject_name(subject.name) not in current:
            raise HomeworkConflict("homework subject is no longer enabled in the schedule")

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
            lesson_rows = (
                (
                    await session.execute(
                        select(RuleLesson)
                        .where(RuleLesson.rule_id.in_([item.id for item in rules]))
                        .order_by(RuleLesson.position)
                    )
                )
                .scalars()
                .all()
            )
            for item in lesson_rows:
                rule_lessons[item.rule_id].append(self._lesson(item))
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
            override_rows = (
                (
                    await session.execute(
                        select(OverrideLesson)
                        .where(OverrideLesson.override_id.in_([item.id for item in overrides]))
                        .order_by(OverrideLesson.position)
                    )
                )
                .scalars()
                .all()
            )
            for override_item in override_rows:
                override_lessons[override_item.override_id].append(self._lesson(override_item))
        return ScheduleProfile(
            profile_id=profile.public_id,
            name=profile.name,
            timezone=profile.timezone,
            active=profile.is_active,
            rules=tuple(
                ScheduleRule(
                    weekday=item.weekday,
                    week_parity=Parity(item.week_parity),
                    date_parity=Parity(item.date_parity),
                    valid_from=item.valid_from,
                    valid_to=item.valid_to,
                    lessons=tuple(rule_lessons[item.id]),
                )
                for item in rules
            ),
            overrides=tuple(
                DateOverride(
                    day=item.day,
                    day_off=item.day_off,
                    lessons=tuple(override_lessons[item.id]),
                )
                for item in overrides
            ),
        )

    @staticmethod
    def _lesson(row: RuleLesson | OverrideLesson) -> Lesson:
        return Lesson(
            start=row.start_time,
            end=row.end_time,
            title=row.title,
            enabled=row.enabled,
        )

    @staticmethod
    def _subject(row: ScheduleSubject) -> HomeworkSubject:
        return HomeworkSubject(id=row.id, profile_id=row.profile_id, name=row.name)

    def _draft(
        self, row: PendingHomeworkDraft, subject: ScheduleSubject, token: str
    ) -> HomeworkDraft:
        return HomeworkDraft(
            token=token,
            subject=self._subject(subject),
            text=row.text,
            due_at=row.due_at,
            expires_at=row.expires_at,
            source_type=HomeworkSourceType(row.source_type),
        )

    def _item(
        self,
        row: HomeworkAssignment,
        subject: ScheduleSubject,
        state: HomeworkUserState | None,
    ) -> HomeworkItem:
        return HomeworkItem(
            id=row.id,
            subject=self._subject(subject),
            text=row.text,
            due_at=row.due_at,
            completed=bool(state.completed) if state is not None else False,
            completed_at=state.completed_at if state is not None else None,
            version=row.version,
            source_type=HomeworkSourceType(row.source_type),
        )


__all__ = [
    "AsyncPostgresHomeworkRepository",
    "MAX_HOMEWORK_ASSIGNMENTS_PER_USER",
    "MAX_HOMEWORK_LIST_ITEMS",
    "digest_homework_token",
    "normalize_homework_source",
]

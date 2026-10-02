from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from time_tl.domain.homework import (
    HomeworkConflict,
    HomeworkItem,
    HomeworkSourceType,
    HomeworkValidationError,
)
from time_tl.infrastructure.db.homework import (
    MAX_HOMEWORK_ASSIGNMENTS_PER_USER,
    AsyncPostgresHomeworkRepository,
    digest_homework_token,
    normalize_homework_source,
)
from time_tl.infrastructure.db.models import (
    HomeworkAssignment,
    HomeworkUserState,
    PendingHomeworkDraft,
    ScheduleSubject,
    User,
)


class ScalarResult:
    def __init__(self, value: object) -> None:
        self.value = value

    def scalar_one_or_none(self) -> object:
        return self.value

    def scalar_one(self) -> object:
        return self.value

    def scalars(self) -> object:
        return self.value


class FakeSession:
    def __init__(self, results: list[object] | None = None) -> None:
        self.results = list(results or [])
        self.added: list[object] = []
        self.statements: list[object] = []

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def begin(self) -> FakeSession:
        return self

    async def execute(self, statement: object) -> ScalarResult:
        self.statements.append(statement)
        return ScalarResult(self.results.pop(0) if self.results else None)

    def add(self, value: object) -> None:
        self.added.append(value)

    def add_all(self, values: tuple[object, ...]) -> None:
        self.added.extend(values)

    async def flush(self) -> None:
        return None


class FakeFactory:
    def __init__(self, session: FakeSession) -> None:
        self.session = session

    def __call__(self) -> FakeSession:
        return self.session


def rows() -> tuple[User, ScheduleSubject, PendingHomeworkDraft]:
    user_id = uuid4()
    profile_id = uuid4()
    subject_id = uuid4()
    now = datetime.now(UTC)
    user = User(id=user_id, telegram_user_id=17)
    subject = ScheduleSubject(
        id=subject_id,
        profile_id=profile_id,
        name="Математика",
        normalized_name="математика",
    )
    draft = PendingHomeworkDraft(
        token_hash=digest_homework_token("t" * 32),
        user_id=user_id,
        profile_id=profile_id,
        subject_id=subject_id,
        text="№428",
        due_at=now + timedelta(days=1),
        expires_at=now + timedelta(minutes=30),
        source_type="MANUAL",
    )
    return user, subject, draft


def test_source_type_accepts_manual_enum_or_literal_only() -> None:
    assert normalize_homework_source(HomeworkSourceType.MANUAL) is HomeworkSourceType.MANUAL
    assert normalize_homework_source("MANUAL") is HomeworkSourceType.MANUAL
    with pytest.raises(HomeworkValidationError, match="unsupported homework source"):
        normalize_homework_source("OCR")


@pytest.mark.asyncio
async def test_subject_picker_groups_legacy_rows_without_rewriting_keys_or_references() -> None:
    user, subject, draft = rows()
    subject.name = "Математика."
    subject.normalized_name = "математика."
    canonical = ScheduleSubject(
        id=uuid4(), profile_id=subject.profile_id, name="Математика", normalized_name="математика"
    )
    language = ScheduleSubject(
        id=uuid4(), profile_id=subject.profile_id, name="Англійська", normalized_name="англійська"
    )
    language_variant = ScheduleSubject(
        id=uuid4(),
        profile_id=subject.profile_id,
        name="Англійська мова.",
        normalized_name="англійська мова.",
    )
    legacy_rows = [subject, canonical, language, language_variant]
    before = [(item.id, item.normalized_name) for item in legacy_rows]
    assignment = HomeworkAssignment(
        id=uuid4(),
        owner_user_id=user.id,
        profile_id=subject.profile_id,
        subject_id=subject.id,
        text="existing homework",
        due_at=datetime.now(UTC),
        source_type="MANUAL",
        version=1,
    )
    session = FakeSession([legacy_rows])
    repo = AsyncPostgresHomeworkRepository(FakeFactory(session))  # type: ignore[arg-type]
    repo._locked_user = AsyncMock(return_value=user)  # type: ignore[method-assign]
    repo._active_profile = AsyncMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(id=subject.profile_id)
    )
    repo._current_subject_titles = AsyncMock(  # type: ignore[method-assign]
        return_value=("Математика.", "Математика", "Математика", "Англійська", "Англійська мова.")
    )
    selected = await repo.list_subjects(17)
    assert len(selected) == 2
    assert next(item for item in selected if item.name == "Математика").id == canonical.id
    assert [(item.id, item.normalized_name) for item in legacy_rows] == before
    assert session.added == []
    assert draft.subject_id == subject.id  # Existing foreign keys remain valid.
    assert assignment.subject_id == subject.id
    assert assignment.text == "existing homework"
    for old in legacy_rows:
        await repo._require_current_subject(session, subject.profile_id, old)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_subject_picker_creates_one_row_per_subject_not_per_lesson() -> None:
    user, subject, _draft = rows()
    session = FakeSession([[]])
    repo = AsyncPostgresHomeworkRepository(FakeFactory(session))  # type: ignore[arg-type]
    repo._locked_user = AsyncMock(return_value=user)  # type: ignore[method-assign]
    repo._active_profile = AsyncMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(id=subject.profile_id)
    )
    titles = ("Математика.", "Математика", "Англійська", "Англійська мова", "Українська література")
    repo._current_subject_titles = AsyncMock(return_value=titles)  # type: ignore[method-assign]
    selected = await repo.list_subjects(17)
    assert {item.name for item in selected} == {
        "Математика",
        "Англійська мова",
        "Українська література",
    }
    assert len(session.added) == 3
    assert {
        item.normalized_name for item in session.added if isinstance(item, ScheduleSubject)
    } == {"математика", "англійська мова", "українська література"}
    assert len(titles) == 5


@pytest.mark.asyncio
async def test_subject_picker_without_canonical_key_reuses_deterministic_old_id() -> None:
    user, subject, _draft = rows()
    subject.id = UUID(int=2)
    subject.name = "Математика."
    subject.normalized_name = "математика."
    other = ScheduleSubject(
        id=UUID(int=1),
        profile_id=subject.profile_id,
        name="МАТЕМАТИКА..",
        normalized_name="математика..",
    )
    session = FakeSession([[subject, other]])
    repo = AsyncPostgresHomeworkRepository(FakeFactory(session))  # type: ignore[arg-type]
    repo._locked_user = AsyncMock(return_value=user)  # type: ignore[method-assign]
    repo._active_profile = AsyncMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(id=subject.profile_id)
    )
    repo._current_subject_titles = AsyncMock(return_value=("Математика.",))  # type: ignore[method-assign]
    selected = await repo.list_subjects(17)
    assert len(selected) == 1
    assert selected[0].id == other.id
    assert selected[0].name == "Математика"
    assert other.normalized_name == "математика.."
    assert subject.name == "Математика."
    assert session.added == []


@pytest.mark.asyncio
async def test_retained_subject_is_rejected_after_lesson_is_removed_or_disabled() -> None:
    _user, subject, _draft = rows()
    repo = AsyncPostgresHomeworkRepository(None)  # type: ignore[arg-type]
    repo._current_subject_titles = AsyncMock(  # type: ignore[method-assign]
        return_value=("Українська мова",)
    )

    with pytest.raises(HomeworkConflict, match="no longer enabled"):
        await repo._require_current_subject(  # type: ignore[arg-type]
            FakeSession(), subject.profile_id, subject
        )


@pytest.mark.asyncio
async def test_confirm_draft_creates_one_assignment_and_personal_state_atomically() -> None:
    user, subject, draft = rows()
    session = FakeSession([draft, MAX_HOMEWORK_ASSIGNMENTS_PER_USER - 1])
    repo = AsyncPostgresHomeworkRepository(FakeFactory(session))  # type: ignore[arg-type]
    repo._locked_user = AsyncMock(return_value=user)  # type: ignore[method-assign]
    repo._active_profile = AsyncMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(id=subject.profile_id)
    )
    repo._owned_subject = AsyncMock(return_value=subject)  # type: ignore[method-assign]
    repo._require_current_subject = AsyncMock()  # type: ignore[method-assign]
    now = datetime.now(UTC)

    item = await repo.confirm_draft(17, "t" * 32, now)

    assignment = next(value for value in session.added if isinstance(value, HomeworkAssignment))
    state = next(value for value in session.added if isinstance(value, HomeworkUserState))
    assert assignment.owner_user_id == user.id
    assert state.user_id == user.id
    assert state.homework_id == assignment.id
    assert item.completed is False
    assert draft.consumed_at == now
    assert draft.confirmed_homework_id == assignment.id


@pytest.mark.asyncio
async def test_confirm_draft_rejects_assignment_at_per_user_quota_boundary() -> None:
    user, subject, draft = rows()
    session = FakeSession([draft, MAX_HOMEWORK_ASSIGNMENTS_PER_USER])
    repo = AsyncPostgresHomeworkRepository(FakeFactory(session))  # type: ignore[arg-type]
    repo._locked_user = AsyncMock(return_value=user)  # type: ignore[method-assign]
    repo._active_profile = AsyncMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(id=subject.profile_id)
    )
    repo._owned_subject = AsyncMock(return_value=subject)  # type: ignore[method-assign]
    repo._require_current_subject = AsyncMock()  # type: ignore[method-assign]

    with pytest.raises(HomeworkConflict, match="limit was reached"):
        await repo.confirm_draft(17, "t" * 32, datetime.now(UTC))

    assert session.added == []
    assert draft.consumed_at is None
    assert draft.confirmed_homework_id is None
    quota_sql = str(session.statements[1])
    assert "WHERE homework_assignments.owner_user_id =" in quota_sql


@pytest.mark.asyncio
async def test_confirm_draft_rejects_profile_that_is_no_longer_active() -> None:
    user, subject, draft = rows()
    session = FakeSession([draft])
    repo = AsyncPostgresHomeworkRepository(FakeFactory(session))  # type: ignore[arg-type]
    repo._locked_user = AsyncMock(return_value=user)  # type: ignore[method-assign]
    repo._active_profile = AsyncMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(id=uuid4())
    )

    with pytest.raises(HomeworkConflict, match="no longer active"):
        await repo.confirm_draft(17, "t" * 32, datetime.now(UTC))

    assert session.added == []
    assert draft.consumed_at is None
    assert draft.confirmed_homework_id is None


@pytest.mark.asyncio
async def test_confirm_draft_rejects_subject_disabled_after_preview() -> None:
    user, subject, draft = rows()
    session = FakeSession([draft])
    repo = AsyncPostgresHomeworkRepository(FakeFactory(session))  # type: ignore[arg-type]
    repo._locked_user = AsyncMock(return_value=user)  # type: ignore[method-assign]
    repo._active_profile = AsyncMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(id=subject.profile_id)
    )
    repo._owned_subject = AsyncMock(return_value=subject)  # type: ignore[method-assign]
    repo._require_current_subject = AsyncMock(  # type: ignore[method-assign]
        side_effect=HomeworkConflict("homework subject is no longer enabled")
    )

    with pytest.raises(HomeworkConflict, match="no longer enabled"):
        await repo.confirm_draft(17, "t" * 32, datetime.now(UTC))

    assert session.added == []
    assert draft.consumed_at is None
    assert draft.confirmed_homework_id is None


@pytest.mark.asyncio
async def test_confirm_draft_retry_returns_existing_result_without_duplicate() -> None:
    user, _subject, draft = rows()
    draft.consumed_at = datetime.now(UTC)
    draft.confirmed_homework_id = uuid4()
    session = FakeSession([draft])
    repo = AsyncPostgresHomeworkRepository(FakeFactory(session))  # type: ignore[arg-type]
    repo._locked_user = AsyncMock(return_value=user)  # type: ignore[method-assign]
    existing = AsyncMock()
    repo._owned_item = existing  # type: ignore[method-assign]
    existing.return_value = object()

    result = await repo.confirm_draft(17, "t" * 32, datetime.now(UTC))

    assert result is existing.return_value
    assert session.added == []
    existing.assert_awaited_once_with(session, user.id, draft.confirmed_homework_id)


@pytest.mark.asyncio
async def test_update_uses_optimistic_version_and_rejects_stale_edit() -> None:
    user, subject, _draft = rows()
    assignment = HomeworkAssignment(
        id=uuid4(),
        owner_user_id=user.id,
        profile_id=subject.profile_id,
        subject_id=subject.id,
        text="old",
        due_at=datetime.now(UTC) + timedelta(days=1),
        source_type="MANUAL",
        version=2,
    )
    session = FakeSession()
    repo = AsyncPostgresHomeworkRepository(FakeFactory(session))  # type: ignore[arg-type]
    repo._locked_user = AsyncMock(return_value=user)  # type: ignore[method-assign]
    repo._owned_assignment = AsyncMock(return_value=assignment)  # type: ignore[method-assign]
    repo._item_for_user = AsyncMock(  # type: ignore[method-assign]
        return_value=HomeworkItem(
            id=assignment.id,
            subject=repo._subject(subject),
            text="new",
            due_at=assignment.due_at,
            completed=False,
            completed_at=None,
            version=3,
            source_type=HomeworkSourceType.MANUAL,
        )
    )

    await repo.update_homework(17, assignment.id, expected_version=2, text="new")
    assert assignment.text == "new"
    assert assignment.version == 3

    with pytest.raises(HomeworkConflict, match="another request"):
        await repo.update_homework(17, assignment.id, expected_version=2, text="newer")


def test_personal_completion_states_do_not_change_assignment_content() -> None:
    user, subject, _draft = rows()
    assignment = HomeworkAssignment(
        id=uuid4(),
        owner_user_id=user.id,
        profile_id=subject.profile_id,
        subject_id=subject.id,
        text="№428",
        due_at=datetime.now(UTC) + timedelta(days=1),
        source_type="MANUAL",
        version=1,
    )
    other_user_id = uuid4()
    completed_at = datetime.now(UTC)
    first = HomeworkUserState(
        homework_id=assignment.id,
        user_id=user.id,
        completed=True,
        completed_at=completed_at,
    )
    second = HomeworkUserState(
        homework_id=assignment.id,
        user_id=other_user_id,
        completed=False,
        completed_at=None,
    )
    repo = AsyncPostgresHomeworkRepository(None)  # type: ignore[arg-type]

    assert repo._item(assignment, subject, first).completed is True
    assert repo._item(assignment, subject, second).completed is False
    assert assignment.text == "№428"

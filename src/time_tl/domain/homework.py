from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID
from zoneinfo import ZoneInfo

from time_tl.domain.schedule import ResolutionConflict, ScheduleProfile, resolve_day

MAX_HOMEWORK_TEXT_LENGTH = 2_000
MAX_SUBJECT_NAME_LENGTH = 160
MAX_NEXT_LESSON_SEARCH_DAYS = 366
KYIV = ZoneInfo("Europe/Kyiv")


class HomeworkError(ValueError):
    """Base class for safe, user-correctable homework failures."""


class HomeworkValidationError(HomeworkError):
    pass


class HomeworkNotFound(HomeworkError):
    pass


class HomeworkConflict(HomeworkError):
    pass


class HomeworkSourceType(StrEnum):
    MANUAL = "MANUAL"


@dataclass(frozen=True, slots=True)
class HomeworkSubject:
    id: UUID
    profile_id: UUID
    name: str

    def __post_init__(self) -> None:
        validate_subject_name(self.name)


@dataclass(frozen=True, slots=True)
class HomeworkDraft:
    token: str
    subject: HomeworkSubject
    text: str
    due_at: datetime
    expires_at: datetime
    source_type: HomeworkSourceType = HomeworkSourceType.MANUAL

    def __post_init__(self) -> None:
        validate_homework_text(self.text)
        require_aware_datetime(self.due_at, field="due_at")
        require_aware_datetime(self.expires_at, field="expires_at")


@dataclass(frozen=True, slots=True)
class HomeworkItem:
    id: UUID
    subject: HomeworkSubject
    text: str
    due_at: datetime
    completed: bool
    completed_at: datetime | None
    version: int
    source_type: HomeworkSourceType = HomeworkSourceType.MANUAL

    def __post_init__(self) -> None:
        validate_homework_text(self.text)
        require_aware_datetime(self.due_at, field="due_at")
        if self.version < 1:
            raise HomeworkValidationError("homework version must be positive")
        if self.completed != (self.completed_at is not None):
            raise HomeworkValidationError("completion flag and timestamp must agree")
        if self.completed_at is not None:
            require_aware_datetime(self.completed_at, field="completed_at")


def validate_subject_name(value: str) -> str:
    display_name = " ".join(value.split())
    if not display_name:
        raise HomeworkValidationError("subject name must not be empty")
    if len(display_name) > MAX_SUBJECT_NAME_LENGTH:
        raise HomeworkValidationError("subject name is too long")
    if "\x00" in display_name:
        raise HomeworkValidationError("subject name contains prohibited characters")
    return display_name


def normalize_subject_name(value: str) -> str:
    display_name = validate_subject_name(value)
    normalized = unicodedata.normalize("NFKC", display_name).casefold()
    normalized = re.sub(r"\s+", " ", normalized).strip()
    # Terminal dots are an import/display variation, not a different subject.
    # Retain punctuation-only names rather than collapsing them to an empty key.
    normalized = normalized.rstrip(". ") or normalized
    language_aliases = {
        "англійська": "англійська мова",
        "українська": "українська мова",
        "французька": "французька мова",
        "німецька": "німецька мова",
    }
    return language_aliases.get(normalized, normalized)


def validate_homework_text(value: str) -> str:
    body = value.strip()
    if not body:
        raise HomeworkValidationError("homework text must not be empty")
    if len(body) > MAX_HOMEWORK_TEXT_LENGTH:
        raise HomeworkValidationError("homework text is too long")
    if "\x00" in body:
        raise HomeworkValidationError("homework text contains prohibited characters")
    return body


def require_aware_datetime(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise HomeworkValidationError(f"{field} must be timezone-aware")
    return value


def find_next_subject_lesson(
    profile: ScheduleProfile,
    subject_name: str,
    after: datetime,
    *,
    search_days: int = MAX_NEXT_LESSON_SEARCH_DAYS,
) -> datetime | None:
    """Resolve the next enabled matching lesson using the existing schedule rules.

    The search uses Kyiv local dates and wall times, while the returned value is
    timezone-aware.  Overrides, parity, gaps and rule conflicts therefore keep
    exactly the same semantics as the production schedule resolver.
    """

    require_aware_datetime(after, field="after")
    if search_days < 1 or search_days > MAX_NEXT_LESSON_SEARCH_DAYS:
        raise HomeworkValidationError("lesson search range must be between 1 and 366 days")
    wanted = normalize_subject_name(subject_name)
    local_after = after.astimezone(KYIV)
    for offset in range(search_days):
        day = local_after.date() + timedelta(days=offset)
        try:
            resolved = resolve_day(profile, day)
        except ResolutionConflict as exc:
            raise HomeworkConflict("schedule contains conflicting rules") from exc
        for lesson in resolved.enabled_lessons:
            if normalize_subject_name(lesson.title) != wanted:
                continue
            candidate = datetime.combine(day, lesson.start, tzinfo=KYIV)
            if candidate > local_after:
                return candidate
    return None


__all__ = [
    "HomeworkConflict",
    "HomeworkDraft",
    "HomeworkError",
    "HomeworkItem",
    "HomeworkNotFound",
    "HomeworkSourceType",
    "HomeworkSubject",
    "HomeworkValidationError",
    "MAX_HOMEWORK_TEXT_LENGTH",
    "MAX_NEXT_LESSON_SEARCH_DAYS",
    "MAX_SUBJECT_NAME_LENGTH",
    "find_next_subject_lesson",
    "normalize_subject_name",
    "require_aware_datetime",
    "validate_homework_text",
    "validate_subject_name",
]

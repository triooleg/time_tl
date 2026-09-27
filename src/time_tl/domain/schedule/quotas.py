"""Central bounded-input contract for public schedule storage."""

from __future__ import annotations

from dataclasses import dataclass

MAX_RULES_PER_PROFILE = 32
MAX_LESSONS_PER_RULE = 16
MAX_RECURRING_LESSONS_PER_PROFILE = 100
MAX_OVERRIDES_PER_PROFILE = 90
MAX_LESSONS_PER_OVERRIDE = 16
MAX_OVERRIDE_LESSONS_PER_PROFILE = 256

MAX_LESSON_TITLE_LENGTH = 160
MAX_LESSON_JOIN_URL_LENGTH = 2048
MAX_PROFILE_NAME_LENGTH = 120
MAX_PROFILE_ID_LENGTH = 80


@dataclass(frozen=True)
class ScheduleValidationError(ValueError):
    """Typed, presentation-neutral schedule validation failure."""

    code: str
    limit: int
    actual: int

    def __str__(self) -> str:
        return f"{self.code}: maximum is {self.limit}, received {self.actual}"


class ScheduleQuotaExceeded(ScheduleValidationError):
    """The schedule shape would expand beyond a bounded row count."""


class ScheduleFieldTooLong(ScheduleValidationError):
    """A user-controlled text field exceeds its persistence bound."""


def require_quota(*, code: str, actual: int, limit: int) -> None:
    if actual > limit:
        raise ScheduleQuotaExceeded(code, limit, actual)


def require_length(*, code: str, value: str, limit: int) -> None:
    actual = len(value)
    if actual > limit:
        raise ScheduleFieldTooLong(code, limit, actual)

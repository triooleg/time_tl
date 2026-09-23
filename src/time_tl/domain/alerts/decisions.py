"""Pure notification decisions for Time TL's three operating modes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from .model import AlertState, Transition, TransitionKind


class NotificationMode(StrEnum):
    LESSONS_ONLY = "LESSONS_ONLY"
    LESSONS_AND_ALERTS = "LESSONS_AND_ALERTS"
    ALERTS_ONLY = "ALERTS_ONLY"


class NotificationKind(StrEnum):
    LESSON_REMINDER = "LESSON_REMINDER"
    SEEK_SHELTER = "SEEK_SHELTER"
    ALL_CLEAR = "ALL_CLEAR"
    RETURN_TO_ACTIVE_LESSON = "RETURN_TO_ACTIVE_LESSON"
    NEXT_LESSON_AFTER_CLEAR = "NEXT_LESSON_AFTER_CLEAR"


@dataclass(frozen=True)
class LessonContext:
    active_lesson_id: str | None = None
    next_lesson_id: str | None = None
    next_lesson_starts_at: datetime | None = None

    def __post_init__(self) -> None:
        if (self.next_lesson_id is None) != (self.next_lesson_starts_at is None):
            raise ValueError(
                "next lesson ID and start time must either both be set or both be absent"
            )
        if self.next_lesson_starts_at is not None and self.next_lesson_starts_at.tzinfo is None:
            raise ValueError("next lesson start must be timezone-aware")


@dataclass(frozen=True)
class NotificationIntent:
    kind: NotificationKind
    transition_event_id: str | None = None
    lesson_id: str | None = None
    lesson_starts_at: datetime | None = None


def decide_alert_notifications(
    mode: NotificationMode,
    transition: Transition,
    lesson_context: LessonContext | None = None,
) -> tuple[NotificationIntent, ...]:
    if mode is NotificationMode.LESSONS_ONLY:
        return ()
    if transition.kind is TransitionKind.ALERT_STARTED:
        return (
            NotificationIntent(
                kind=NotificationKind.SEEK_SHELTER,
                transition_event_id=transition.event_id,
            ),
        )
    if transition.kind is not TransitionKind.ALL_CLEAR_CONFIRMED:
        return ()
    if mode is NotificationMode.ALERTS_ONLY:
        return (
            NotificationIntent(
                kind=NotificationKind.ALL_CLEAR,
                transition_event_id=transition.event_id,
            ),
        )

    context = lesson_context or LessonContext()
    if context.active_lesson_id is not None:
        return (
            NotificationIntent(
                kind=NotificationKind.RETURN_TO_ACTIVE_LESSON,
                transition_event_id=transition.event_id,
                lesson_id=context.active_lesson_id,
            ),
        )
    if context.next_lesson_id is not None:
        return (
            NotificationIntent(
                kind=NotificationKind.NEXT_LESSON_AFTER_CLEAR,
                transition_event_id=transition.event_id,
                lesson_id=context.next_lesson_id,
                lesson_starts_at=context.next_lesson_starts_at,
            ),
        )
    return (
        NotificationIntent(
            kind=NotificationKind.ALL_CLEAR,
            transition_event_id=transition.event_id,
        ),
    )


def decide_lesson_reminder(
    mode: NotificationMode,
    lesson_id: str,
    starts_at: datetime,
    effective_alert_state: AlertState | None = None,
) -> tuple[NotificationIntent, ...]:
    """Gate combined-mode lesson reminders on confirmed effective safety state."""

    if starts_at.tzinfo is None:
        raise ValueError("lesson start must be timezone-aware")
    if mode is NotificationMode.ALERTS_ONLY:
        return ()
    if mode is NotificationMode.LESSONS_AND_ALERTS:
        if effective_alert_state is AlertState.ACTIVE:
            return (NotificationIntent(kind=NotificationKind.SEEK_SHELTER),)
        if effective_alert_state is not AlertState.CLEAR_CONFIRMED:
            return ()
    return (
        NotificationIntent(
            kind=NotificationKind.LESSON_REMINDER,
            lesson_id=lesson_id,
            lesson_starts_at=starts_at,
        ),
    )

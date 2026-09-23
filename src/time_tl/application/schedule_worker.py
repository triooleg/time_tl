"""Deterministic schedule-to-outbox planning."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from time_tl.domain.alerts.decisions import NotificationMode
from time_tl.domain.alerts.model import AlertState
from time_tl.domain.schedule.models import Lesson, ScheduleProfile
from time_tl.domain.schedule.resolver import resolve_day

KYIV = ZoneInfo("Europe/Kyiv")
RETURN_TO_LESSON_MAX_AGE = timedelta(minutes=2)


@dataclass(frozen=True)
class ScheduleTarget:
    user_id: str
    telegram_chat_id: int
    profile: ScheduleProfile
    mode: NotificationMode
    reminder_lead_minutes: int = 5
    alert_state: AlertState = AlertState.UNKNOWN
    profile_revision: int = 1

    def __post_init__(self) -> None:
        if not 0 <= self.reminder_lead_minutes <= 120:
            raise ValueError("reminder lead must be in range 0..120")
        if not self.profile.active:
            raise ValueError("schedule worker accepts only the active profile")
        if self.profile_revision < 1:
            raise ValueError("profile revision must be positive")


@dataclass(frozen=True)
class ConfirmedClear:
    event_id: str
    confirmed_at: datetime
    target: ScheduleTarget

    def __post_init__(self) -> None:
        if not self.event_id:
            raise ValueError("clear event id must not be empty")
        if self.confirmed_at.tzinfo is None:
            raise ValueError("confirmed_at must be timezone-aware")
        if self.target.alert_state is not AlertState.CLEAR_CONFIRMED:
            raise ValueError("return-to-lesson requires CLEAR_CONFIRMED")


@dataclass(frozen=True)
class PlannedOutboxMessage:
    event_key: str
    aggregate_id: str
    event_type: str
    payload: dict[str, object]
    available_at: datetime


class ScheduleWorkerRepository(Protocol):
    async def list_schedule_targets(self, now: datetime) -> tuple[ScheduleTarget, ...]: ...
    async def list_confirmed_clears(self, now: datetime) -> tuple[ConfirmedClear, ...]: ...
    async def enqueue_deduplicated(self, messages: tuple[PlannedOutboxMessage, ...]) -> int: ...


class ScheduleWorker:
    def __init__(self, repository: ScheduleWorkerRepository) -> None:
        self._repository = repository

    async def run_once(self, now: datetime) -> int:
        if now.tzinfo is None:
            raise ValueError("worker clock must be timezone-aware")
        messages: list[PlannedOutboxMessage] = []
        for target in await self._repository.list_schedule_targets(now):
            messages.extend(self._due_reminders(target, now))
        for clear in await self._repository.list_confirmed_clears(now):
            message = self._return_to_lesson(clear, now)
            if message is not None:
                messages.append(message)
        return await self._repository.enqueue_deduplicated(tuple(messages))

    @staticmethod
    def _due_reminders(target: ScheduleTarget, now: datetime) -> list[PlannedOutboxMessage]:
        if target.mode is NotificationMode.ALERTS_ONLY:
            return []
        if (
            target.mode is NotificationMode.LESSONS_AND_ALERTS
            and target.alert_state is not AlertState.CLEAR_CONFIRMED
        ):
            return []
        local_now = now.astimezone(KYIV)
        resolved = resolve_day(target.profile, local_now.date())
        result: list[PlannedOutboxMessage] = []
        for position, lesson in enumerate(resolved.lessons, start=1):
            if not lesson.enabled:
                continue
            starts_at = datetime.combine(resolved.day, lesson.start, KYIV)
            due_at = starts_at - timedelta(minutes=target.reminder_lead_minutes)
            if not due_at <= local_now < starts_at:
                continue
            lesson_key = _lesson_key(position, lesson)
            result.append(
                PlannedOutboxMessage(
                    event_key=f"lesson-reminder:{target.user_id}:{target.profile.profile_id}:{resolved.day.isoformat()}:{lesson_key}",
                    aggregate_id=target.user_id,
                    event_type="LESSON_REMINDER",
                    payload={
                        "telegram_chat_id": target.telegram_chat_id,
                        "lesson_title": lesson.title,
                        "starts_at": starts_at.isoformat(),
                        "join_link_ref": _join_link_ref(lesson),
                        "profile_revision": target.profile_revision,
                    },
                    available_at=due_at,
                )
            )
        return result

    @staticmethod
    def _return_to_lesson(clear: ConfirmedClear, now: datetime) -> PlannedOutboxMessage | None:
        target = clear.target
        if target.mode is not NotificationMode.LESSONS_AND_ALERTS:
            return None
        age = now - clear.confirmed_at
        if age < timedelta(0) or age > RETURN_TO_LESSON_MAX_AGE:
            return None
        local_now = now.astimezone(KYIV)
        resolved = resolve_day(target.profile, local_now.date())
        for lesson in resolved.lessons:
            if not lesson.enabled:
                continue
            starts_at = datetime.combine(resolved.day, lesson.start, KYIV)
            ends_at = datetime.combine(resolved.day, lesson.end, KYIV)
            if starts_at <= local_now < ends_at:
                valid_until = min(
                    clear.confirmed_at + RETURN_TO_LESSON_MAX_AGE,
                    ends_at,
                )
                return PlannedOutboxMessage(
                    # One clear event may create at most one return message per user,
                    # even if a profile changes while the fresh-event window is open.
                    event_key=f"return-to-lesson:{clear.event_id}:{target.user_id}",
                    aggregate_id=target.user_id,
                    event_type="RETURN_TO_ACTIVE_LESSON",
                    payload={
                        "telegram_chat_id": target.telegram_chat_id,
                        "lesson_title": lesson.title,
                        "ends_at": ends_at.isoformat(),
                        "valid_until": valid_until.isoformat(),
                        "profile_revision": target.profile_revision,
                        "join_link_ref": _join_link_ref(lesson),
                    },
                    available_at=clear.confirmed_at,
                )
        return None


def _lesson_key(position: int, lesson: Lesson) -> str:
    return f"{position}:{lesson.start.strftime('%H%M')}"


def _join_link_ref(lesson: Lesson) -> str | None:
    if lesson.join_url is None or lesson.storage_ref is None:
        return None
    return lesson.storage_ref


def build_schedule_worker(repository: ScheduleWorkerRepository) -> ScheduleWorker:
    """Exact runtime factory for wire_runtime."""
    return ScheduleWorker(repository)

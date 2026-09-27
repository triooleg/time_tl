from __future__ import annotations

import unittest
from datetime import UTC, datetime, time

from time_tl.application.schedule_worker import (
    ConfirmedClear,
    ScheduleTarget,
    ScheduleWorker,
    _lesson_key,
)
from time_tl.domain.alerts import AlertState, NotificationMode
from time_tl.domain.schedule import Lesson, ScheduleProfile, ScheduleRule


class MemoryRepo:
    def __init__(self, targets=(), clears=()):
        self.targets, self.clears, self.events = tuple(targets), tuple(clears), {}

    async def list_schedule_targets(self, now):
        return self.targets

    async def list_confirmed_clears(self, now):
        return self.clears

    async def enqueue_deduplicated(self, messages):
        count = 0
        for message in messages:
            if message.event_key not in self.events:
                self.events[message.event_key] = message
                count += 1
        return count


def profile(*lessons):
    return ScheduleProfile(
        "school", "School", active=True, rules=(ScheduleRule(weekday=0, lessons=tuple(lessons)),)
    )


def target(
    *, state=AlertState.CLEAR_CONFIRMED, mode=NotificationMode.LESSONS_AND_ALERTS, lessons=None
):
    lessons = lessons or (Lesson(time(9), time(9, 45), "Математика"),)
    return ScheduleTarget("u1", 42, profile(*lessons), mode, 5, state)


class ScheduleWorkerTests(unittest.IsolatedAsyncioTestCase):
    NOW = datetime(2026, 9, 21, 5, 56, tzinfo=UTC)  # 08:56 Europe/Kyiv

    def test_persisted_lesson_key_survives_edit_and_sibling_position_shift(self):
        storage_ref = "lesson:rule:123e4567-e89b-12d3-a456-426614174000"
        original = Lesson(time(9), time(10), "Math", storage_ref=storage_ref)
        edited = Lesson(time(11), time(12), "Algebra", storage_ref=storage_ref)

        first_key = _lesson_key(1, original)
        shifted_key = _lesson_key(7, edited)

        self.assertEqual(first_key, shifted_key)
        self.assertNotIn(storage_ref, first_key)
        self.assertNotEqual(
            _lesson_key(1, Lesson(time(9), time(10), "Math")),
            _lesson_key(2, Lesson(time(9), time(10), "Math")),
        )

    async def test_due_reminder_is_deduplicated_across_restart(self):
        repo = MemoryRepo((target(),))
        self.assertEqual(await ScheduleWorker(repo).run_once(self.NOW), 1)
        self.assertEqual(await ScheduleWorker(repo).run_once(self.NOW), 0)
        self.assertEqual(len(repo.events), 1)
        event = next(iter(repo.events.values()))
        self.assertEqual(event.payload["profile_revision"], 1)

    async def test_active_and_stale_suppress_combined_reminder(self):
        for state in (AlertState.ACTIVE, AlertState.STALE, AlertState.UNKNOWN):
            repo = MemoryRepo((target(state=state),))
            self.assertEqual(await ScheduleWorker(repo).run_once(self.NOW), 0)

    async def test_lessons_only_does_not_require_alert_state(self):
        repo = MemoryRepo((target(state=AlertState.STALE, mode=NotificationMode.LESSONS_ONLY),))
        self.assertEqual(await ScheduleWorker(repo).run_once(self.NOW), 1)

    async def test_disabled_lesson_is_never_planned(self):
        repo = MemoryRepo((target(lessons=(Lesson(time(9), time(9, 45), "X", enabled=False),)),))
        self.assertEqual(await ScheduleWorker(repo).run_once(self.NOW), 0)

    async def test_confirmed_clear_returns_only_to_active_lesson(self):
        clear_at = datetime(2026, 9, 21, 6, 15, tzinfo=UTC)
        active = target()
        binding = {
            "provider": "alerts.in.ua",
            "catalog_version": "2026-09-24",
            "selected_region_id": "31",
            "applicable_region_ids": ["31"],
        }
        repo = MemoryRepo(clears=(ConfirmedClear("clear-7", clear_at, active, binding),))
        self.assertEqual(await ScheduleWorker(repo).run_once(clear_at), 1)
        event = next(iter(repo.events.values()))
        self.assertEqual(event.event_type, "RETURN_TO_ACTIVE_LESSON")
        self.assertNotIn("join_url", event.payload)
        self.assertEqual(event.payload["scope_binding"], binding)

    async def test_historical_clear_never_creates_late_return_message(self):
        clear_at = datetime(2026, 9, 21, 6, 15, tzinfo=UTC)
        much_later = datetime(2026, 9, 21, 6, 30, tzinfo=UTC)
        repo = MemoryRepo(clears=(ConfirmedClear("old-clear", clear_at, target()),))

        self.assertEqual(await ScheduleWorker(repo).run_once(much_later), 0)
        self.assertEqual(repo.events, {})

    async def test_return_is_evaluated_at_current_time_not_clear_time(self):
        short_lesson = Lesson(time(9), time(9, 16), "Short")
        clear_at = datetime(2026, 9, 21, 6, 15, tzinfo=UTC)
        after_lesson = datetime(2026, 9, 21, 6, 16, 30, tzinfo=UTC)
        repo = MemoryRepo(
            clears=(ConfirmedClear("fresh-but-ended", clear_at, target(lessons=(short_lesson,))),)
        )

        self.assertEqual(await ScheduleWorker(repo).run_once(after_lesson), 0)

    async def test_return_carries_current_revision_expiry_and_exact_lesson_reference(self):
        lesson = Lesson(
            time(9),
            time(9, 45),
            "Math",
            join_url="https://secret.test/join",
            storage_ref="lesson:rule:123e4567-e89b-12d3-a456-426614174000",
        )
        current = target(lessons=(lesson,))
        current = ScheduleTarget(
            current.user_id,
            current.telegram_chat_id,
            current.profile,
            current.mode,
            current.reminder_lead_minutes,
            current.alert_state,
            7,
        )
        clear_at = datetime(2026, 9, 21, 6, 15, tzinfo=UTC)
        repo = MemoryRepo(clears=(ConfirmedClear("clear-revision", clear_at, current),))

        self.assertEqual(await ScheduleWorker(repo).run_once(clear_at), 1)
        event = next(iter(repo.events.values()))
        self.assertEqual(event.event_key, "return-to-lesson:clear-revision:u1")
        self.assertEqual(event.payload["profile_revision"], 7)
        self.assertEqual(
            event.payload["join_link_ref"],
            "lesson:rule:123e4567-e89b-12d3-a456-426614174000",
        )
        self.assertEqual(event.payload["valid_until"], "2026-09-21T06:17:00+00:00")

    async def test_one_clear_event_is_idempotent_across_profile_revision_change(self):
        clear_at = datetime(2026, 9, 21, 6, 15, tzinfo=UTC)
        first = target()
        revised = ScheduleTarget(
            first.user_id,
            first.telegram_chat_id,
            first.profile,
            first.mode,
            first.reminder_lead_minutes,
            first.alert_state,
            2,
        )
        repo = MemoryRepo(clears=(ConfirmedClear("same-clear", clear_at, first),))
        worker = ScheduleWorker(repo)
        self.assertEqual(await worker.run_once(clear_at), 1)
        repo.clears = (ConfirmedClear("same-clear", clear_at, revised),)
        self.assertEqual(await worker.run_once(clear_at), 0)
        self.assertEqual(len(repo.events), 1)

    async def test_non_confirmed_clear_is_rejected(self):
        with self.assertRaises(ValueError):
            ConfirmedClear("bad", self.NOW, target(state=AlertState.STALE))


if __name__ == "__main__":
    unittest.main()

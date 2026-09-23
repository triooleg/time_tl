import unittest
from datetime import UTC, datetime

from time_tl.domain.alerts import (
    AlertState,
    LessonContext,
    NotificationKind,
    NotificationMode,
    Transition,
    TransitionKind,
    decide_alert_notifications,
    decide_lesson_reminder,
)

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


def transition(kind: TransitionKind) -> Transition:
    target = (
        AlertState.ACTIVE if kind is TransitionKind.ALERT_STARTED else AlertState.CLEAR_CONFIRMED
    )
    return Transition(AlertState.ACTIVE, target, kind, event_id="official-1")


class NotificationDecisionTests(unittest.TestCase):
    def test_lessons_only_suppresses_all_alert_notifications(self) -> None:
        for kind in (
            TransitionKind.ALERT_STARTED,
            TransitionKind.ALL_CLEAR_CONFIRMED,
            TransitionKind.DEGRADED,
        ):
            with self.subTest(kind=kind):
                self.assertEqual(
                    (),
                    decide_alert_notifications(NotificationMode.LESSONS_ONLY, transition(kind)),
                )

    def test_lessons_only_reminder_does_not_depend_on_alert_state(self) -> None:
        for state in AlertState:
            with self.subTest(state=state):
                intents = decide_lesson_reminder(NotificationMode.LESSONS_ONLY, "math", NOW, state)
                self.assertEqual(NotificationKind.LESSON_REMINDER, intents[0].kind)

    def test_combined_reminder_requires_clear_confirmed(self) -> None:
        allowed = decide_lesson_reminder(
            NotificationMode.LESSONS_AND_ALERTS,
            "math",
            NOW,
            AlertState.CLEAR_CONFIRMED,
        )
        self.assertEqual(NotificationKind.LESSON_REMINDER, allowed[0].kind)
        for state in (AlertState.UNKNOWN, AlertState.STALE):
            with self.subTest(state=state):
                self.assertEqual(
                    (),
                    decide_lesson_reminder(NotificationMode.LESSONS_AND_ALERTS, "math", NOW, state),
                )
        self.assertEqual(
            (),
            decide_lesson_reminder(NotificationMode.LESSONS_AND_ALERTS, "math", NOW, None),
        )

    def test_combined_active_lesson_boundary_returns_safety_intent(self) -> None:
        intents = decide_lesson_reminder(
            NotificationMode.LESSONS_AND_ALERTS,
            "math",
            NOW,
            AlertState.ACTIVE,
        )
        self.assertEqual((NotificationKind.SEEK_SHELTER,), tuple(i.kind for i in intents))
        self.assertTrue(all(i.lesson_id is None for i in intents))

    def test_alerts_only_has_no_lesson_reminders(self) -> None:
        self.assertEqual(
            (),
            decide_lesson_reminder(
                NotificationMode.ALERTS_ONLY, "math", NOW, AlertState.CLEAR_CONFIRMED
            ),
        )

    def test_alert_modes_issue_shelter_intent_on_alert_start(self) -> None:
        for mode in (NotificationMode.LESSONS_AND_ALERTS, NotificationMode.ALERTS_ONLY):
            with self.subTest(mode=mode):
                intents = decide_alert_notifications(mode, transition(TransitionKind.ALERT_STARTED))
                self.assertEqual(NotificationKind.SEEK_SHELTER, intents[0].kind)

    def test_alerts_only_all_clear_does_not_consult_lesson_context(self) -> None:
        intents = decide_alert_notifications(
            NotificationMode.ALERTS_ONLY,
            transition(TransitionKind.ALL_CLEAR_CONFIRMED),
            LessonContext(active_lesson_id="secret-lesson"),
        )
        self.assertEqual(NotificationKind.ALL_CLEAR, intents[0].kind)
        self.assertIsNone(intents[0].lesson_id)

    def test_combined_all_clear_returns_to_active_lesson(self) -> None:
        intents = decide_alert_notifications(
            NotificationMode.LESSONS_AND_ALERTS,
            transition(TransitionKind.ALL_CLEAR_CONFIRMED),
            LessonContext(active_lesson_id="lesson-5"),
        )
        self.assertEqual(NotificationKind.RETURN_TO_ACTIVE_LESSON, intents[0].kind)
        self.assertEqual("lesson-5", intents[0].lesson_id)

    def test_combined_all_clear_reports_next_lesson(self) -> None:
        intents = decide_alert_notifications(
            NotificationMode.LESSONS_AND_ALERTS,
            transition(TransitionKind.ALL_CLEAR_CONFIRMED),
            LessonContext(next_lesson_id="lesson-6", next_lesson_starts_at=NOW),
        )
        self.assertEqual(NotificationKind.NEXT_LESSON_AFTER_CLEAR, intents[0].kind)
        self.assertEqual(NOW, intents[0].lesson_starts_at)

    def test_degraded_duplicate_and_recovery_transitions_never_notify(self) -> None:
        for kind in (TransitionKind.DEGRADED, TransitionKind.NONE, TransitionKind.RECOVERED):
            with self.subTest(kind=kind):
                intents = decide_alert_notifications(
                    NotificationMode.LESSONS_AND_ALERTS, transition(kind)
                )
                self.assertEqual((), intents)


if __name__ == "__main__":
    unittest.main()

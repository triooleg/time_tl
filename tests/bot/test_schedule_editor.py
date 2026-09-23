from __future__ import annotations

import unittest
from datetime import date, time

from time_tl.application.schedule_editor import ScheduleEditor
from time_tl.domain.schedule import Lesson, Parity, resolve_day


class ScheduleEditorTests(unittest.TestCase):
    def test_rejects_overlap_without_corrupting_previous_draft(self) -> None:
        editor = ScheduleEditor("p1", "Школа")
        editor.add_lesson(weekday=0, start=time(9), end=time(10), title="Перший")
        with self.assertRaises(ValueError):
            editor.add_lesson(weekday=0, start=time(9, 30), end=time(10, 30), title="Другий")
        self.assertEqual(len(editor.build(active=True).rules[0].lessons), 1)

    def test_custom_date_schedule_replaces_recurring_schedule(self) -> None:
        editor = ScheduleEditor("p1", "Школа")
        editor.add_lesson(weekday=6, start=time(9), end=time(10), title="Звичайний")
        editor.set_date_lessons(date(2026, 9, 27), (Lesson(time(12), time(13), "Перенесений"),))
        resolved = resolve_day(editor.build(active=True), date(2026, 9, 27))
        self.assertEqual([item.title for item in resolved.lessons], ["Перенесений"])

    def test_combined_date_and_iso_week_parity_is_preserved(self) -> None:
        editor = ScheduleEditor("p1", "Школа")
        editor.add_lesson(
            weekday=1,
            start=time(8),
            end=time(9),
            title="Паритет",
            week_parity=Parity.ODD,
            date_parity=Parity.EVEN,
        )
        rule = editor.build(active=False).rules[0]
        self.assertEqual(rule.week_parity, Parity.ODD)
        self.assertEqual(rule.date_parity, Parity.EVEN)


if __name__ == "__main__":
    unittest.main()

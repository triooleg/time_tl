from __future__ import annotations

import unittest
from datetime import date, time

from time_tl.application.schedule_editor import (
    ScheduleEditor,
    StaleLessonSelection,
    recurring_lesson_fingerprint,
)
from time_tl.domain.schedule import Lesson, Parity, ScheduleProfile, ScheduleRule, resolve_day


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

    def test_from_profile_preserves_ordered_duplicate_rules_and_date_ranges(self) -> None:
        first = ScheduleRule(
            weekday=0,
            week_parity=Parity.EVEN,
            date_parity=Parity.ANY,
            valid_from=date(2026, 9, 1),
            valid_to=date(2026, 9, 30),
            lessons=(Lesson(time(9), time(10), "Вересень", storage_ref="lesson:rule:first"),),
        )
        second = ScheduleRule(
            weekday=0,
            week_parity=Parity.EVEN,
            date_parity=Parity.ANY,
            valid_from=date(2026, 10, 1),
            valid_to=date(2026, 10, 31),
            lessons=(Lesson(time(10), time(11), "Жовтень", storage_ref="lesson:rule:second"),),
        )
        profile = ScheduleProfile("p1", "Імпорт", (first, second), active=True)

        rebuilt = ScheduleEditor.from_profile(profile).build(active=True)

        self.assertEqual(rebuilt, profile)

    def test_add_lesson_creates_base_rule_without_mutating_imported_range(self) -> None:
        ranged = ScheduleRule(
            weekday=0,
            lessons=(Lesson(time(9), time(10), "Тимчасовий"),),
            valid_from=date(2026, 9, 1),
            valid_to=date(2026, 9, 30),
        )
        editor = ScheduleEditor.from_profile(
            ScheduleProfile("p1", "Імпорт", (ranged,), active=True)
        )

        editor.add_lesson(weekday=0, start=time(11), end=time(12), title="Постійний")
        rebuilt = editor.build(active=True)

        self.assertEqual(rebuilt.rules[0], ranged)
        self.assertEqual((rebuilt.rules[1].valid_from, rebuilt.rules[1].valid_to), (None, None))

    def test_edit_preserves_identity_state_link_rule_and_active_flag(self) -> None:
        lesson = Lesson(
            time(9),
            time(10),
            "Стара назва",
            enabled=False,
            join_url="https://school.example/join",
            storage_ref="lesson:rule:stable",
        )
        rule = ScheduleRule(
            weekday=2,
            week_parity=Parity.ODD,
            date_parity=Parity.EVEN,
            valid_from=date(2026, 9, 1),
            valid_to=date(2026, 12, 31),
            lessons=(lesson,),
        )
        profile = ScheduleProfile("p1", "Школа", (rule,), active=True)
        fingerprint = recurring_lesson_fingerprint(profile, 0, 0)
        editor = ScheduleEditor.from_profile(profile)

        editor.edit_lesson(
            rule_index=0,
            lesson_index=0,
            expected_fingerprint=fingerprint,
            start=time(10),
            end=time(11),
            title="Нова назва",
        )
        rebuilt = editor.build(active=True)
        edited = rebuilt.rules[0].lessons[0]

        self.assertFalse(edited.enabled)
        self.assertEqual(edited.join_url, lesson.join_url)
        self.assertEqual(edited.storage_ref, lesson.storage_ref)
        self.assertEqual(
            (edited.start, edited.end, edited.title), (time(10), time(11), "Нова назва")
        )
        self.assertEqual(rebuilt.rules[0].week_parity, Parity.ODD)
        self.assertEqual(rebuilt.rules[0].date_parity, Parity.EVEN)
        self.assertEqual(rebuilt.rules[0].valid_from, date(2026, 9, 1))
        self.assertTrue(rebuilt.active)
        with self.assertRaises(StaleLessonSelection):
            editor.delete_lesson(
                rule_index=0,
                lesson_index=0,
                expected_fingerprint=fingerprint,
            )

    def test_edit_overlap_rolls_back_atomically(self) -> None:
        profile = ScheduleProfile(
            "p1",
            "Школа",
            (
                ScheduleRule(
                    0,
                    (
                        Lesson(time(9), time(10), "Перший", storage_ref="lesson:rule:first"),
                        Lesson(time(10), time(11), "Другий", storage_ref="lesson:rule:second"),
                    ),
                ),
            ),
            active=True,
        )
        editor = ScheduleEditor.from_profile(profile)
        fingerprint = recurring_lesson_fingerprint(profile, 0, 1)

        with self.assertRaisesRegex(ValueError, "overlap"):
            editor.edit_lesson(
                rule_index=0,
                lesson_index=1,
                expected_fingerprint=fingerprint,
                start=time(9, 30),
                end=time(10, 30),
                title="Конфлікт",
            )

        self.assertEqual(editor.build(active=True), profile)

    def test_delete_middle_preserves_siblings_and_last_delete_retains_rule(self) -> None:
        lessons = (
            Lesson(time(8), time(9), "A", storage_ref="lesson:rule:a"),
            Lesson(time(9), time(10), "B", storage_ref="lesson:rule:b"),
            Lesson(time(10), time(11), "C", storage_ref="lesson:rule:c"),
        )
        profile = ScheduleProfile(
            "p1", "Школа", (ScheduleRule(1, lessons, week_parity=Parity.EVEN),), active=True
        )
        editor = ScheduleEditor.from_profile(profile)
        editor.delete_lesson(
            rule_index=0,
            lesson_index=1,
            expected_fingerprint=recurring_lesson_fingerprint(profile, 0, 1),
        )
        remaining = editor.build(active=True)
        self.assertEqual(
            [item.storage_ref for item in remaining.rules[0].lessons],
            ["lesson:rule:a", "lesson:rule:c"],
        )

        for index in (1, 0):
            current = editor.build(active=True)
            editor.delete_lesson(
                rule_index=0,
                lesson_index=index,
                expected_fingerprint=recurring_lesson_fingerprint(current, 0, index),
            )
        empty = editor.build(active=True)
        self.assertEqual(len(empty.rules), 1)
        self.assertEqual(empty.rules[0].lessons, ())
        self.assertEqual(empty.rules[0].week_parity, Parity.EVEN)

    def test_selector_toggle_targets_one_duplicate_rule_and_rejects_negative_index(self) -> None:
        rules = (
            ScheduleRule(
                0,
                (Lesson(time(9), time(10), "Перший", storage_ref="lesson:rule:first"),),
                valid_from=date(2026, 9, 1),
                valid_to=date(2026, 9, 30),
            ),
            ScheduleRule(
                0,
                (Lesson(time(9), time(10), "Другий", storage_ref="lesson:rule:second"),),
                valid_from=date(2026, 10, 1),
                valid_to=date(2026, 10, 31),
            ),
        )
        profile = ScheduleProfile("p1", "Імпорт", rules, active=True)
        editor = ScheduleEditor.from_profile(profile)
        editor.set_lesson_enabled_by_selector(
            rule_index=1,
            lesson_index=0,
            expected_fingerprint=recurring_lesson_fingerprint(profile, 1, 0),
            enabled=False,
        )
        rebuilt = editor.build(active=True)
        self.assertTrue(rebuilt.rules[0].lessons[0].enabled)
        self.assertFalse(rebuilt.rules[1].lessons[0].enabled)
        with self.assertRaises(StaleLessonSelection):
            recurring_lesson_fingerprint(rebuilt, -1, 0)


if __name__ == "__main__":
    unittest.main()

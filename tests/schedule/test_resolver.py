import unittest
from datetime import date, time

from time_tl.domain.schedule import (
    DateOverride,
    GapKind,
    Lesson,
    Parity,
    ResolutionConflict,
    ScheduleProfile,
    ScheduleRule,
    preview,
    resolve_day,
)


def lesson(title, start, end, enabled=True):
    return Lesson(time.fromisoformat(start), time.fromisoformat(end), title, enabled)


class ScheduleResolverTests(unittest.TestCase):
    def test_specificity_order_combined_date_week_base(self):
        target = date(2026, 9, 21)  # Monday, ISO week 39, odd date.
        base = ScheduleRule(0, (lesson("base", "08:00", "08:45"),))
        week = ScheduleRule(0, (lesson("week", "09:00", "09:45"),), week_parity=Parity.ODD)
        date_rule = ScheduleRule(0, (lesson("date", "10:00", "10:45"),), date_parity=Parity.ODD)
        combined = ScheduleRule(
            0,
            (lesson("combined", "11:00", "11:45"),),
            week_parity=Parity.ODD,
            date_parity=Parity.ODD,
        )
        profile = ScheduleProfile("p", "School", (base, week, date_rule, combined))
        self.assertEqual(resolve_day(profile, target).lessons[0].title, "combined")

    def test_date_parity_beats_week_parity(self):
        target = date(2026, 9, 21)
        profile = ScheduleProfile(
            "p",
            "School",
            (
                ScheduleRule(0, (lesson("week", "09:00", "09:45"),), week_parity=Parity.ODD),
                ScheduleRule(0, (lesson("date", "10:00", "10:45"),), date_parity=Parity.ODD),
            ),
        )
        self.assertEqual(resolve_day(profile, target).lessons[0].title, "date")

    def test_date_override_day_off_and_custom_weekend(self):
        monday = date(2026, 9, 21)
        saturday = date(2026, 9, 26)
        profile = ScheduleProfile(
            "p",
            "School",
            (ScheduleRule(5, (lesson("Saturday class", "09:00", "09:45"),)),),
            (
                DateOverride(monday, day_off=True),
                DateOverride(saturday, (lesson("Replacement", "12:00", "12:40"),)),
            ),
        )
        self.assertEqual(resolve_day(profile, monday).lessons, ())
        self.assertEqual(resolve_day(profile, saturday).lessons[0].title, "Replacement")

    def test_disabled_lessons_are_preserved_but_not_active_and_gap_is_derived(self):
        lessons = (
            lesson("1", "08:00", "08:45"),
            lesson("2", "08:55", "09:40"),
            lesson("3", "09:50", "10:35", False),
            lesson("4", "10:45", "11:30", False),
            lesson("5", "11:50", "12:35"),
        )
        resolved = resolve_day(
            ScheduleProfile("p", "School", (ScheduleRule(0, lessons),)), date(2026, 9, 21)
        )
        self.assertEqual([item.title for item in resolved.lessons], ["1", "2", "3", "4", "5"])
        self.assertEqual([item.title for item in resolved.enabled_lessons], ["1", "2", "5"])
        self.assertEqual(resolved.gaps[1].minutes, 130)
        self.assertEqual(resolved.gaps[1].kind, GapKind.FREE_WINDOW)

    def test_equal_specificity_conflict_is_rejected(self):
        rules = (
            ScheduleRule(0, (lesson("A", "08:00", "08:45"),)),
            ScheduleRule(0, (lesson("B", "09:00", "09:45"),)),
        )
        with self.assertRaises(ResolutionConflict):
            resolve_day(ScheduleProfile("p", "School", rules), date(2026, 9, 21))

    def test_preview_returns_requested_consecutive_days(self):
        result = preview(ScheduleProfile("p", "School"), date(2026, 9, 21), 7)
        self.assertEqual(len(result), 7)
        self.assertEqual(result[-1].day, date(2026, 9, 27))


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest
from datetime import time

from time_tl.bot.schedule_router import (
    _lesson_callback,
    _parse_lesson,
    _parse_lesson_time,
    _parse_lesson_title,
    _parse_parity,
    _schedule_validation_message,
)
from time_tl.domain.schedule import (
    Parity,
    ScheduleFieldTooLong,
    ScheduleQuotaExceeded,
)


class ScheduleRouterParsingTests(unittest.TestCase):
    def test_arbitrary_times_and_gap_friendly_lesson_input(self) -> None:
        parsed = _parse_lesson("13:17-14:02 Фізика")
        self.assertEqual((time(13, 17), time(14, 2), "Фізика", None), parsed)

    def test_https_link_is_separated_but_http_is_not_accepted_as_link(self) -> None:
        parsed = _parse_lesson("09:00-09:45 Математика https://school.example/join")
        self.assertEqual("https://school.example/join", parsed[3])
        self.assertIsNone(_parse_lesson("09:45-09:00 Назад у часі"))

    def test_edit_title_is_trimmed_and_limited_to_database_capacity(self) -> None:
        self.assertEqual("Математика", _parse_lesson_title("  Математика  "))
        self.assertEqual("У" * 160, _parse_lesson_title("У" * 160))
        self.assertIsNone(_parse_lesson_title("   "))
        self.assertIsNone(_parse_lesson_title("У" * 161))
        self.assertIsNone(_parse_lesson_title("Математика\nФізика"))

    def test_edit_time_requires_strict_forward_hh_mm_range(self) -> None:
        self.assertEqual((time(9), time(9, 45)), _parse_lesson_time("09:00-09:45"))
        self.assertIsNone(_parse_lesson_time("9:00-09:45"))
        self.assertIsNone(_parse_lesson_time("24:00-25:00"))
        self.assertIsNone(_parse_lesson_time("09:45-09:00"))
        self.assertIsNone(_parse_lesson_time("09:00-09:00"))
        self.assertIsNone(_parse_lesson_time("09:00-09:45 Математика"))

    def test_lesson_callbacks_fit_telegram_limit(self) -> None:
        callback = _lesson_callback("ledc", "a" * 32, 99, 999, "f" * 12)
        self.assertLessEqual(len(callback.encode("utf-8")), 64)
        with self.assertRaises(ValueError):
            _lesson_callback("ledc", "a" * 32, 9999, 9999, "f" * 20)

    def test_typed_schedule_limits_have_clear_ukrainian_messages(self) -> None:
        quota = _schedule_validation_message(ScheduleQuotaExceeded("max_lessons_per_rule", 16, 17))
        too_long = _schedule_validation_message(
            ScheduleFieldTooLong("lesson_title_too_long", 160, 161)
        )

        self.assertIn("максимум уроків", quota)
        self.assertIn("Дозволено: 16", quota)
        self.assertIn("Назва уроку задовга", too_long)
        self.assertIn("Максимум: 160", too_long)

    def test_lowercase_callback_parity_values_are_normalized(self) -> None:
        self.assertIs(_parse_parity("any"), Parity.ANY)
        self.assertIs(_parse_parity("odd"), Parity.ODD)
        self.assertIs(_parse_parity("even"), Parity.EVEN)

    def test_invalid_callback_parity_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _parse_parity("weekly")


if __name__ == "__main__":
    unittest.main()

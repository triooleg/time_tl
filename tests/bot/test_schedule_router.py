from __future__ import annotations

import unittest
from datetime import time

from time_tl.bot.schedule_router import _parse_lesson, _parse_parity
from time_tl.domain.schedule import Parity


class ScheduleRouterParsingTests(unittest.TestCase):
    def test_arbitrary_times_and_gap_friendly_lesson_input(self) -> None:
        parsed = _parse_lesson("13:17-14:02 Фізика")
        self.assertEqual((time(13, 17), time(14, 2), "Фізика", None), parsed)

    def test_https_link_is_separated_but_http_is_not_accepted_as_link(self) -> None:
        parsed = _parse_lesson("09:00-09:45 Математика https://school.example/join")
        self.assertEqual("https://school.example/join", parsed[3])
        self.assertIsNone(_parse_lesson("09:45-09:00 Назад у часі"))

    def test_lowercase_callback_parity_values_are_normalized(self) -> None:
        self.assertIs(_parse_parity("any"), Parity.ANY)
        self.assertIs(_parse_parity("odd"), Parity.ODD)
        self.assertIs(_parse_parity("even"), Parity.EVEN)

    def test_invalid_callback_parity_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _parse_parity("weekly")


if __name__ == "__main__":
    unittest.main()

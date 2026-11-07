import unittest
from datetime import datetime, timedelta

from time_tl.domain.alert_statistics import (
    KYIV,
    ObservedPeriod,
    ObservedSegment,
    report_window,
    summarize,
    validate_report_payload,
)


class AlertStatisticsTests(unittest.TestCase):
    def test_anchored_22_window_before_midnight_and_after_restart(self):
        first = datetime(2026, 11, 7, 22, 0, tzinfo=KYIV)
        expected = (datetime(2026, 11, 6, 22, tzinfo=KYIV), first)
        self.assertEqual(report_window(first), expected)
        self.assertEqual(report_window(first + timedelta(minutes=9)), expected)
        self.assertEqual(report_window(first + timedelta(hours=3)), expected)

    def test_midnight_and_carryover_count_new_starts_only(self):
        start, end = report_window(datetime(2026, 11, 7, 22, tzinfo=KYIV))
        prior = start - timedelta(hours=1)
        after = start + timedelta(hours=1)
        new = start + timedelta(hours=4)
        periods = (
            ObservedPeriod("carry", prior, True, after),
            ObservedPeriod("new", new, True, end),
        )
        segments = (
            ObservedSegment(start, after, True, "carry"),
            ObservedSegment(after, new, False),
            ObservedSegment(new, end, True, "new"),
        )
        result = summarize(start, end, segments, periods)
        self.assertEqual(result.alert_count, 1)
        self.assertEqual(result.carried_over_count, 1)
        self.assertEqual(result.total_duration_seconds, 21 * 3600)
        self.assertTrue(result.history_complete)

    def test_gap_never_becomes_clear_or_active_duration(self):
        start, end = report_window(datetime(2026, 11, 7, 22, tzinfo=KYIV))
        gap_start, gap_end = start + timedelta(hours=1), start + timedelta(hours=2)
        segments = (
            ObservedSegment(start, gap_start, True, "period"),
            ObservedSegment(gap_end, end, True, "period"),
        )
        result = summarize(start, end, segments, (ObservedPeriod("period", start, True),))
        self.assertEqual(result.total_duration_seconds, 23 * 3600)
        self.assertEqual(result.ongoing_count, 1)
        self.assertFalse(result.history_complete)

    def test_overlapping_parent_and_child_do_not_double_duration(self):
        start, end = report_window(datetime(2026, 11, 7, 22, tzinfo=KYIV))
        segments = (ObservedSegment(start, end, True, "aggregate"),) * 2
        result = summarize(start, end, segments, (ObservedPeriod("aggregate", start, True, end),))
        self.assertEqual(result.alert_count, 1)
        self.assertEqual(result.total_duration_seconds, 86400)

    def test_bootstrap_active_is_not_claimed_as_a_new_known_start(self):
        start, end = report_window(datetime(2026, 11, 7, 22, tzinfo=KYIV))
        result = summarize(
            start,
            end,
            (ObservedSegment(start, end, True, "unknown"),),
            (ObservedPeriod("unknown", start, False),),
        )
        self.assertEqual(result.alert_count, 0)
        self.assertEqual(result.unknown_start_count, 1)
        self.assertFalse(result.history_complete)

    def test_known_start_without_a_second_sample_still_counts_once(self):
        start, end = report_window(datetime(2026, 11, 7, 22, tzinfo=KYIV))
        began = end - timedelta(seconds=1)
        result = summarize(
            start,
            end,
            (ObservedSegment(began, began, True, "new"),),
            (ObservedPeriod("new", began, True),),
        )
        self.assertEqual(result.alert_count, 1)
        self.assertEqual(result.total_duration_seconds, 0)
        self.assertEqual(result.ongoing_count, 1)
        self.assertFalse(result.history_complete)

    def test_dst_report_uses_local_22_not_fixed_24_hours(self):
        start, end = report_window(datetime(2026, 10, 25, 22, tzinfo=KYIV))
        self.assertEqual((end - start).total_seconds(), 25 * 3600)

    def test_no_history_and_invalid_payload_fail_conservatively(self):
        start, end = report_window(datetime(2026, 11, 7, 22, tzinfo=KYIV))
        self.assertFalse(summarize(start, end, (), ()).history_complete)
        payload = {
            "period_start": start.isoformat(),
            "period_end": end.isoformat(),
            "alert_count": 0,
            "duration_seconds": 0,
            "carried_over_count": 0,
            "ongoing_count": 0,
            "is_partial": True,
        }
        self.assertEqual(validate_report_payload(payload), payload)
        for changes in (
            {"alert_count": True},
            {"duration_seconds": 86401},
            {"is_partial": "false"},
            {"period_end": (end + timedelta(minutes=1)).isoformat()},
        ):
            with self.assertRaises(ValueError):
                validate_report_payload({**payload, **changes})


if __name__ == "__main__":
    unittest.main()

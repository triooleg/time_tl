import unittest
from datetime import UTC, datetime, time

from time_tl.domain.schedule import (
    PRODUCTION_PENDING_STORE,
    InMemoryImportSessionRepository,
    Lesson,
    PendingImportSession,
    ScheduleProfile,
    SnapshotError,
    export_snapshot,
    propose_import,
)


class LessonValidationTests(unittest.TestCase):
    def test_direct_model_rejects_non_https_join_url(self):
        with self.assertRaisesRegex(ValueError, "https"):
            Lesson(time(9), time(9, 45), "Math", join_url="http://unsafe.invalid")

    def test_direct_model_rejects_timezone_offsets_and_subminute_precision(self):
        with self.assertRaisesRegex(ValueError, "naive"):
            Lesson(time(9, tzinfo=UTC), time(9, 45), "Math")
        with self.assertRaisesRegex(ValueError, "HH:MM"):
            Lesson(time(9, 0, 1), time(9, 45), "Math")

    def test_snapshot_rejects_offset_and_seconds_time_strings(self):
        base = ScheduleProfile("p", "School", active=True)
        snapshot = export_snapshot(base, source_display_label="Class")
        snapshot["profile"]["rules"] = [
            {
                "weekday": 0,
                "week_parity": "ANY",
                "date_parity": "ANY",
                "valid_from": None,
                "valid_to": None,
                "lessons": [
                    {"start": "09:00+03:00", "end": "09:45", "title": "Math", "enabled": True}
                ],
            }
        ]
        with self.assertRaisesRegex(SnapshotError, "naive HH:MM"):
            propose_import(snapshot, sessions=InMemoryImportSessionRepository())
        snapshot["profile"]["rules"][0]["lessons"][0]["start"] = "09:00:30"
        with self.assertRaisesRegex(SnapshotError, "naive HH:MM"):
            propose_import(snapshot, sessions=InMemoryImportSessionRepository())


class ImportSessionRepositoryTests(unittest.TestCase):
    def test_in_memory_consume_is_atomic_when_operation_fails(self):
        repository = InMemoryImportSessionRepository()
        now = datetime(2026, 9, 20, tzinfo=UTC)
        repository.create(PendingImportSession("token", "fingerprint", None))

        def fail():
            raise RuntimeError("write failed")

        with self.assertRaisesRegex(RuntimeError, "write failed"):
            repository.consume_atomically("token", "fingerprint", now, fail)
        self.assertFalse(repository.get("token").consumed)
        self.assertIn("PostgreSQL", PRODUCTION_PENDING_STORE)


if __name__ == "__main__":
    unittest.main()

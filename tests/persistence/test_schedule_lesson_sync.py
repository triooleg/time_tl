from __future__ import annotations

import unittest
from datetime import time
from unittest.mock import AsyncMock
from uuid import uuid4

from time_tl.domain.schedule import Lesson
from time_tl.infrastructure.db.accounts import AsyncPostgresAccountRepository
from time_tl.infrastructure.db.models import RuleLesson


class MutationSession:
    def __init__(self) -> None:
        self.added: list[object] = []
        self.statements: list[object] = []
        self.flush = AsyncMock()

    def add(self, value: object) -> None:
        self.added.append(value)

    async def execute(self, statement: object) -> None:
        self.statements.append(statement)


class ScheduleLessonSyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_edit_reuses_lesson_row_identity(self) -> None:
        rule_id = uuid4()
        lesson_id = uuid4()
        existing = RuleLesson(
            id=lesson_id,
            rule_id=rule_id,
            position=0,
            start_time=time(9),
            end_time=time(10),
            title="Old",
            enabled=False,
        )
        desired = Lesson(
            time(10),
            time(11),
            "Edited",
            enabled=False,
            storage_ref=f"lesson:rule:{lesson_id}",
        )
        session = MutationSession()
        repo = AsyncPostgresAccountRepository(None)  # type: ignore[arg-type]

        await repo._sync_lessons(  # noqa: SLF001
            session,  # type: ignore[arg-type]
            [existing],
            (desired,),
            parent_id=rule_id,
            kind="rule",
        )

        self.assertEqual(existing.id, lesson_id)
        self.assertEqual((existing.start_time, existing.end_time), (time(10), time(11)))
        self.assertEqual(existing.title, "Edited")
        self.assertFalse(existing.enabled)
        self.assertEqual(session.added, [])
        self.assertEqual(session.statements, [])

    async def test_delete_middle_keeps_sibling_row_identities(self) -> None:
        rule_id = uuid4()
        first_id, removed_id, last_id = uuid4(), uuid4(), uuid4()
        existing = [
            RuleLesson(
                id=first_id,
                rule_id=rule_id,
                position=0,
                start_time=time(8),
                end_time=time(9),
                title="A",
                enabled=True,
            ),
            RuleLesson(
                id=removed_id,
                rule_id=rule_id,
                position=1,
                start_time=time(9),
                end_time=time(10),
                title="B",
                enabled=True,
            ),
            RuleLesson(
                id=last_id,
                rule_id=rule_id,
                position=2,
                start_time=time(10),
                end_time=time(11),
                title="C",
                enabled=True,
            ),
        ]
        desired = (
            Lesson(time(8), time(9), "A", storage_ref=f"lesson:rule:{first_id}"),
            Lesson(time(10), time(11), "C", storage_ref=f"lesson:rule:{last_id}"),
        )
        session = MutationSession()
        repo = AsyncPostgresAccountRepository(None)  # type: ignore[arg-type]

        await repo._sync_lessons(  # noqa: SLF001
            session,  # type: ignore[arg-type]
            existing,
            desired,
            parent_id=rule_id,
            kind="rule",
        )

        self.assertEqual(existing[0].id, first_id)
        self.assertEqual(existing[0].position, 0)
        self.assertEqual(existing[2].id, last_id)
        self.assertEqual(existing[2].position, 1)
        self.assertEqual(session.added, [])
        self.assertEqual(len(session.statements), 1)


if __name__ == "__main__":
    unittest.main()

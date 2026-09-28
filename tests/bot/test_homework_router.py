from __future__ import annotations

import unittest
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

from time_tl.bot.homework_router import HomeworkUx, _due_on_date, _parse_date
from time_tl.domain.homework import HomeworkSourceType


@dataclass(frozen=True)
class Subject:
    id: UUID
    profile_id: UUID
    name: str


@dataclass(frozen=True)
class Draft:
    token: str
    subject: Subject
    text: str
    due_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class Item:
    id: UUID
    subject: Subject
    text: str
    due_at: datetime
    completed: bool = False
    completed_at: datetime | None = None
    version: int = 1


class FakeHomeworkPort:
    def __init__(self) -> None:
        self.subject = Subject(
            UUID("11111111-1111-1111-1111-111111111111"),
            UUID("22222222-2222-2222-2222-222222222222"),
            "Математика",
        )
        self.suggestion = datetime(2026, 9, 29, 9, tzinfo=UTC)
        self.drafts: dict[str, Draft] = {}
        self.items: dict[UUID, Item] = {}
        self.calls: list[tuple[object, ...]] = []

    async def list_subjects(self, telegram_user_id: int) -> tuple[Subject, ...]:
        self.calls.append(("subjects", telegram_user_id))
        return (self.subject,)

    async def suggest_next_lesson(
        self, telegram_user_id: int, subject_id: UUID, after: datetime
    ) -> datetime | None:
        self.calls.append(("suggest", telegram_user_id, subject_id, after))
        return self.suggestion

    async def create_draft(
        self,
        telegram_user_id: int,
        token: str,
        subject_id: UUID,
        text: str,
        due_at: datetime,
        expires_at: datetime,
        *,
        source_type: HomeworkSourceType = HomeworkSourceType.MANUAL,
    ) -> Draft:
        assert subject_id == self.subject.id
        assert source_type is HomeworkSourceType.MANUAL
        draft = Draft(token, self.subject, text, due_at, expires_at)
        self.drafts[token] = draft
        self.calls.append(("draft", telegram_user_id, token))
        return draft

    async def discard_draft(
        self, telegram_user_id: int, token: str, now: datetime
    ) -> bool:
        self.calls.append(("discard", telegram_user_id, token, now))
        self.drafts.pop(token)
        return True

    async def change_draft_due(
        self,
        telegram_user_id: int,
        token: str,
        due_at: datetime,
        now: datetime,
    ) -> Draft:
        current = self.drafts[token]
        updated = replace(current, due_at=due_at)
        self.drafts[token] = updated
        self.calls.append(("change-draft-due", telegram_user_id, token, due_at, now))
        return updated

    async def confirm_draft(self, telegram_user_id: int, token: str, now: datetime) -> Item:
        draft = self.drafts.pop(token)
        item = Item(
            UUID("33333333-3333-3333-3333-333333333333"),
            draft.subject,
            draft.text,
            draft.due_at,
        )
        self.items[item.id] = item
        self.calls.append(("confirm", telegram_user_id, token, now))
        return item

    async def list_homework(
        self, telegram_user_id: int, *, include_past: bool = False
    ) -> tuple[Item, ...]:
        self.calls.append(("list", telegram_user_id, include_past))
        return tuple(self.items.values())

    async def get_homework(self, telegram_user_id: int, homework_id: UUID) -> Item:
        self.calls.append(("get", telegram_user_id, homework_id))
        return self.items[homework_id]

    async def update_homework(
        self,
        telegram_user_id: int,
        homework_id: UUID,
        *,
        expected_version: int,
        text: str | None = None,
        due_at: datetime | None = None,
    ) -> Item:
        current = self.items[homework_id]
        assert current.version == expected_version
        updated = replace(
            current,
            text=text if text is not None else current.text,
            due_at=due_at if due_at is not None else current.due_at,
            version=current.version + 1,
        )
        self.items[homework_id] = updated
        self.calls.append(("update", telegram_user_id, homework_id))
        return updated

    async def delete_homework(
        self, telegram_user_id: int, homework_id: UUID, *, expected_version: int
    ) -> None:
        assert self.items[homework_id].version == expected_version
        del self.items[homework_id]
        self.calls.append(("delete", telegram_user_id, homework_id))

    async def set_completed(
        self, telegram_user_id: int, homework_id: UUID, *, completed: bool
    ) -> Item:
        current = self.items[homework_id]
        updated = replace(current, completed=completed)
        self.items[homework_id] = updated
        self.calls.append(("completed", telegram_user_id, homework_id, completed))
        return updated


class HomeworkUxTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 9, 28, 12, tzinfo=UTC)
        self.port = FakeHomeworkPort()
        self.ux = HomeworkUx(
            self.port,
            now=lambda: self.now,
            token_factory=lambda: "safe-draft-token-0000000000000000",
        )

    async def test_subjects_come_only_from_backend_port(self) -> None:
        view = await self.ux.subjects(41)

        self.assertIn("увімкнених уроків активного розкладу", view.text)
        self.assertEqual("Математика", view.rows[0][0].label)
        self.assertEqual(f"hw:s:{self.port.subject.id}", view.rows[0][0].callback_data)
        self.assertEqual([("subjects", 41)], self.port.calls)

    async def test_empty_subjects_require_active_schedule(self) -> None:
        async def empty(_: int) -> tuple[Subject, ...]:
            return ()

        self.port.list_subjects = empty  # type: ignore[method-assign]
        view = await self.ux.subjects(41)

        self.assertIn("створіть і активуйте розклад", view.text)
        self.assertEqual("menu:schedules", view.rows[0][0].callback_data)

    async def test_create_is_previewed_then_confirmed_with_durable_token(self) -> None:
        preview = await self.ux.preview_create(
            41,
            self.port.subject.id,
            "  Вправи   1–5  ",
            self.port.suggestion,
        )

        self.assertIn("Перевірте завдання перед збереженням", preview.text)
        self.assertIn("Вправи 1–5", preview.text)
        token = "safe-draft-token-0000000000000000"
        self.assertEqual(f"hw:new:ok:{token}", preview.rows[0][0].callback_data)
        self.assertEqual(f"hw:draft:date:{token}", preview.rows[1][0].callback_data)
        draft = self.port.drafts[token]
        self.assertEqual(self.now + timedelta(minutes=30), draft.expires_at)

        detail = await self.ux.confirm_create(41, token)

        self.assertIn("Завдання збережено", detail.text)
        self.assertEqual(1, len(self.port.items))

    async def test_preview_date_change_updates_same_durable_draft(self) -> None:
        token = "safe-draft-token-0000000000000000"
        await self.ux.preview_create(
            41,
            self.port.subject.id,
            "Вправи 1–5",
            self.port.suggestion,
        )
        changed_due = datetime(2026, 10, 1, 20, 59, tzinfo=UTC)

        preview = await self.ux.change_draft_due(41, token, changed_due)

        self.assertEqual(changed_due, self.port.drafts[token].due_at)
        self.assertIn("01.10.2026", preview.text)
        self.assertEqual(f"hw:new:ok:{token}", preview.rows[0][0].callback_data)
        self.assertEqual(f"hw:draft:date:{token}", preview.rows[1][0].callback_data)
        for row in preview.rows:
            for button in row:
                if button.callback_data is not None:
                    self.assertLessEqual(len(button.callback_data.encode("utf-8")), 64)

    async def test_list_detail_complete_edit_and_delete_are_owner_scoped(self) -> None:
        await self.ux.preview_create(41, self.port.subject.id, "Сторінка 10", self.port.suggestion)
        await self.ux.confirm_create(41, "safe-draft-token-0000000000000000")
        item = next(iter(self.port.items.values()))

        listing = await self.ux.listing(41)
        self.assertIn("Мої домашні завдання", listing.text)
        self.assertEqual(f"hw:v:{item.id}", listing.rows[0][0].callback_data)

        completed = await self.ux.toggle(41, item.id, completed=True)
        self.assertIn("виконано", completed.text)

        changed = await self.ux.update_text(41, item.id, 1, "Нове завдання")
        self.assertIn("Нове завдання", changed.text)
        current = self.port.items[item.id]

        prompt = await self.ux.delete_prompt(41, item.id, current.version)
        callback = prompt.rows[0][0].callback_data
        assert callback is not None
        self.assertLessEqual(len(callback.encode("utf-8")), 64)

        deleted = await self.ux.delete(41, item.id, current.version)
        self.assertIn("видалено", deleted.text)
        self.assertEqual({}, self.port.items)

    async def test_every_generated_callback_fits_telegram_limit(self) -> None:
        await self.ux.preview_create(41, self.port.subject.id, "Текст", self.port.suggestion)
        await self.ux.confirm_create(41, "safe-draft-token-0000000000000000")
        views = [
            self.ux.menu(),
            await self.ux.subjects(41),
            await self.ux.listing(41),
            await self.ux.get_detail(41, next(iter(self.port.items))),
        ]
        for view in views:
            for row in view.rows:
                for button in row:
                    if button.callback_data is not None:
                        self.assertLessEqual(len(button.callback_data.encode("utf-8")), 64)

    def test_manual_date_is_strict_and_preserves_suggested_lesson_time(self) -> None:
        selected = _parse_date("2026-10-01")
        assert selected is not None
        due = _due_on_date(selected, self.port.suggestion)

        self.assertEqual("2026-10-01T12:00:00+03:00", due.isoformat())
        self.assertIsNone(_parse_date("01.10.2026"))

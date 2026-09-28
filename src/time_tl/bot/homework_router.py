from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, time, timedelta
from secrets import token_urlsafe
from typing import Any, Protocol
from uuid import UUID
from zoneinfo import ZoneInfo

from time_tl.domain.homework import HomeworkSourceType

from .models import Button, View

_KYIV = ZoneInfo("Europe/Kyiv")
_DRAFT_LIFETIME = timedelta(minutes=30)
_MAX_TEXT_LENGTH = 2_000


class HomeworkSubjectView(Protocol):
    id: UUID
    name: str


class HomeworkDraftView(Protocol):
    token: str
    subject: HomeworkSubjectView
    text: str
    due_at: datetime


class HomeworkItemView(Protocol):
    id: UUID
    subject: HomeworkSubjectView
    text: str
    due_at: datetime
    completed: bool
    version: int


class AsyncHomeworkPort(Protocol):
    async def list_subjects(self, telegram_user_id: int) -> Sequence[HomeworkSubjectView]: ...

    async def suggest_next_lesson(
        self, telegram_user_id: int, subject_id: UUID, after: datetime
    ) -> datetime | None: ...

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
    ) -> HomeworkDraftView: ...

    async def discard_draft(
        self, telegram_user_id: int, token: str, now: datetime
    ) -> bool: ...

    async def change_draft_due(
        self,
        telegram_user_id: int,
        token: str,
        due_at: datetime,
        now: datetime,
    ) -> HomeworkDraftView: ...

    async def confirm_draft(
        self, telegram_user_id: int, token: str, now: datetime
    ) -> HomeworkItemView: ...

    async def list_homework(
        self, telegram_user_id: int, *, include_past: bool = False
    ) -> Sequence[HomeworkItemView]: ...

    async def get_homework(
        self, telegram_user_id: int, homework_id: UUID
    ) -> HomeworkItemView: ...

    async def update_homework(
        self,
        telegram_user_id: int,
        homework_id: UUID,
        *,
        expected_version: int,
        text: str | None = None,
        due_at: datetime | None = None,
    ) -> HomeworkItemView: ...

    async def delete_homework(
        self, telegram_user_id: int, homework_id: UUID, *, expected_version: int
    ) -> None: ...

    async def set_completed(
        self, telegram_user_id: int, homework_id: UUID, *, completed: bool
    ) -> HomeworkItemView: ...


def _callback(*parts: object) -> str:
    value = ":".join(str(part) for part in parts)
    if len(value.encode("utf-8")) > 64:
        raise ValueError("homework callback exceeds Telegram limit")
    return value


def _compact(raw: str, *, limit: int) -> str:
    value = " ".join(raw.split()).replace("<", "‹").replace(">", "›")
    return value if len(value) <= limit else f"{value[: limit - 1]}…"


def _parse_date(raw: str) -> date | None:
    try:
        return date.fromisoformat(raw.strip())
    except ValueError:
        return None


def _parse_uuid(raw: str) -> UUID | None:
    try:
        return UUID(raw)
    except (ValueError, AttributeError):
        return None


def _due_on_date(selected: date, previous: datetime | None = None) -> datetime:
    due_time = (
        previous.astimezone(_KYIV).timetz().replace(tzinfo=None) if previous else time(23, 59)
    )
    return datetime.combine(selected, due_time, _KYIV)


def _error_view() -> View:
    return View(
        "Не вдалося виконати дію. Запис не змінено: він міг застаріти або належати "
        "іншому користувачу. Відкрийте домашні завдання з меню ще раз.",
        ((Button("📝 Домашні завдання", "menu:homework"),),),
    )


class HomeworkUx:
    def __init__(
        self,
        port: AsyncHomeworkPort,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        token_factory: Callable[[], str] = lambda: token_urlsafe(24),
    ) -> None:
        self.port = port
        self._now = now
        self._token_factory = token_factory

    @staticmethod
    def menu() -> View:
        return View(
            "📝 Домашні завдання\n\n"
            "Додавайте завдання вручну до предметів активного розкладу. "
            "Завдання бачите лише ви.\n\n"
            "⚠️ Цей розділ не замінює офіційні сповіщення про небезпеку.",
            (
                (Button("➕ Додати завдання", "hw:add"),),
                (Button("📋 Мої завдання", "hw:list"),),
                (Button("🏠 Головне меню", "menu:home"),),
            ),
        )

    async def subjects(self, user_id: int) -> View:
        subjects = await self.port.list_subjects(user_id)
        if not subjects:
            return View(
                "Немає доступних предметів. Спочатку створіть і активуйте розклад "
                "та додайте до нього хоча б один увімкнений урок.",
                (
                    (Button("📚 Відкрити розклад", "menu:schedules"),),
                    (Button("⬅️ Назад", "menu:homework"),),
                ),
            )
        return View(
            "Оберіть предмет з увімкнених уроків активного розкладу:",
            tuple(
                (Button(_compact(subject.name, limit=42), _callback("hw", "s", subject.id)),)
                for subject in subjects
            )
            + ((Button("Скасувати", "menu:homework"),),),
        )

    async def suggested_due(self, user_id: int, subject_id: UUID) -> datetime | None:
        return await self.port.suggest_next_lesson(user_id, subject_id, self._now())

    async def preview_create(
        self, user_id: int, subject_id: UUID, text: str, due_at: datetime
    ) -> View:
        cleaned = text.strip()
        if not 1 <= len(cleaned) <= _MAX_TEXT_LENGTH:
            raise ValueError("invalid homework text")
        now = self._now()
        draft = await self.port.create_draft(
            user_id,
            self._token_factory(),
            subject_id,
            cleaned,
            due_at,
            now + _DRAFT_LIFETIME,
            source_type=HomeworkSourceType.MANUAL,
        )
        return self.preview(draft)

    @staticmethod
    def preview(draft: HomeworkDraftView) -> View:
        return View(
            "Перевірте завдання перед збереженням:\n\n"
            f"📚 {_compact(draft.subject.name, limit=80)}\n"
            f"📅 {draft.due_at.astimezone(_KYIV):%d.%m.%Y о %H:%M}\n"
            f"📝 {_compact(draft.text, limit=500)}",
            (
                (Button("✅ Зберегти", _callback("hw", "new", "ok", draft.token)),),
                (
                    Button(
                        "📅 Змінити дату",
                        _callback("hw", "draft", "date", draft.token),
                    ),
                ),
                (Button("Скасувати", _callback("hw", "draft", "no", draft.token)),),
            ),
        )

    async def change_draft_due(self, user_id: int, token: str, due_at: datetime) -> View:
        draft = await self.port.change_draft_due(user_id, token, due_at, self._now())
        return self.preview(draft)

    async def confirm_create(self, user_id: int, token: str) -> View:
        item = await self.port.confirm_draft(user_id, token, self._now())
        return self.detail(item, prefix="Завдання збережено.")

    async def discard(self, user_id: int, token: str) -> View:
        await self.port.discard_draft(user_id, token, self._now())
        return View(
            "Додавання завдання скасовано.",
            ((Button("📝 Домашні завдання", "menu:homework"),),),
        )

    async def listing(self, user_id: int) -> View:
        items = await self.port.list_homework(user_id, include_past=True)
        if not items:
            return View(
                "Збережених домашніх завдань ще немає.",
                (
                    (Button("➕ Додати завдання", "hw:add"),),
                    (Button("⬅️ Назад", "menu:homework"),),
                ),
            )
        rows = tuple(
            (
                Button(
                    ("✅ " if item.completed else "▫️ ")
                    + f"{item.due_at.astimezone(_KYIV):%d.%m} "
                    + _compact(item.subject.name, limit=25),
                    _callback("hw", "v", item.id),
                ),
            )
            for item in items[:30]
        )
        return View(
            "📋 Мої домашні завдання\n\n✅ — виконано, ▫️ — ще потрібно зробити.",
            rows
            + (
                (Button("➕ Додати завдання", "hw:add"),),
                (Button("⬅️ Назад", "menu:homework"),),
            ),
        )

    async def get_detail(self, user_id: int, homework_id: UUID) -> View:
        return self.detail(await self.port.get_homework(user_id, homework_id))

    @staticmethod
    def detail(item: HomeworkItemView, *, prefix: str = "") -> View:
        status = "✅ виконано" if item.completed else "▫️ не виконано"
        text = "\n\n".join(
            part
            for part in (
                prefix,
                "📝 Домашнє завдання\n"
                f"📚 {_compact(item.subject.name, limit=80)}\n"
                f"📅 {item.due_at.astimezone(_KYIV):%d.%m.%Y о %H:%M}\n"
                f"Стан: {status}\n\n"
                f"{_compact(item.text, limit=1_500)}",
            )
            if part
        )
        return View(
            text,
            (
                (
                    Button(
                        "↩️ Позначити невиконаним" if item.completed else "✅ Позначити виконаним",
                        _callback("hw", "done", item.id, 0 if item.completed else 1),
                    ),
                ),
                (Button("✏️ Змінити текст", _callback("hw", "edit", "t", item.id)),),
                (Button("📅 Змінити дату", _callback("hw", "edit", "d", item.id)),),
                (Button("🗑 Видалити", _callback("hw", "del", item.id, item.version)),),
                (Button("⬅️ До списку", "hw:list"),),
            ),
        )

    async def update_text(
        self, user_id: int, homework_id: UUID, expected_version: int, text: str
    ) -> View:
        cleaned = text.strip()
        if not 1 <= len(cleaned) <= _MAX_TEXT_LENGTH:
            raise ValueError("invalid homework text")
        item = await self.port.update_homework(
            user_id, homework_id, expected_version=expected_version, text=cleaned
        )
        return self.detail(item, prefix="Текст оновлено.")

    async def update_due(
        self, user_id: int, homework_id: UUID, expected_version: int, due_at: datetime
    ) -> View:
        item = await self.port.update_homework(
            user_id, homework_id, expected_version=expected_version, due_at=due_at
        )
        return self.detail(item, prefix="Дату оновлено.")

    async def toggle(self, user_id: int, homework_id: UUID, *, completed: bool) -> View:
        item = await self.port.set_completed(user_id, homework_id, completed=completed)
        return self.detail(item, prefix="Стан завдання оновлено.")

    async def delete_prompt(
        self, user_id: int, homework_id: UUID, expected_version: int
    ) -> View:
        item = await self.port.get_homework(user_id, homework_id)
        if item.version != expected_version:
            raise ValueError("stale homework")
        return View(
            "Видалити це домашнє завдання? Дію неможливо скасувати.\n\n"
            f"📚 {_compact(item.subject.name, limit=80)}\n"
            f"📝 {_compact(item.text, limit=300)}",
            (
                (
                    Button(
                        "🗑 Так, видалити",
                        _callback("hw", "del", "ok", item.id, item.version),
                    ),
                ),
                (Button("Скасувати", _callback("hw", "v", item.id)),),
            ),
        )

    async def delete(self, user_id: int, homework_id: UUID, expected_version: int) -> View:
        await self.port.delete_homework(
            user_id, homework_id, expected_version=expected_version
        )
        return View(
            "Домашнє завдання видалено.",
            (
                (Button("📋 До списку", "hw:list"),),
                (Button("🏠 Головне меню", "menu:home"),),
            ),
        )


def build_homework_router(ux: HomeworkUx) -> Any:
    from aiogram import F, Router
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.state import State, StatesGroup
    from aiogram.types import CallbackQuery, Message

    from .aiogram_adapter import _answer, _callback_data, _callback_user_id, _edit, _message_user_id

    class HomeworkFlow(StatesGroup):
        text = State()
        due = State()
        edit_text = State()
        edit_due = State()
        draft_due = State()

    router = Router(name="time_tl_homework")

    async def edit_safely(callback: CallbackQuery, operation: Any) -> None:
        try:
            view = await operation
        except (KeyError, PermissionError, RuntimeError, ValueError):
            view = _error_view()
        await _edit(callback, view)

    @router.callback_query(F.data == "menu:homework")
    async def menu(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await _edit(callback, ux.menu())

    @router.callback_query(F.data == "hw:add")
    async def add(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await edit_safely(callback, ux.subjects(_callback_user_id(callback)))

    @router.callback_query(F.data.startswith("hw:s:"))
    async def subject(callback: CallbackQuery, state: FSMContext) -> None:
        subject_id = _parse_uuid(_callback_data(callback).split(":", 2)[2])
        if subject_id is None:
            await _edit(callback, _error_view())
            return
        await state.clear()
        await state.update_data(subject_id=str(subject_id))
        await state.set_state(HomeworkFlow.text)
        await _edit(
            callback,
            View(
                "Надішліть текст домашнього завдання (до 2000 символів).",
                ((Button("Скасувати", "menu:homework"),),),
            ),
        )

    @router.message(HomeworkFlow.text, F.text)
    async def receive_text(message: Message, state: FSMContext) -> None:
        text_value = (message.text or "").strip()
        if not 1 <= len(text_value) <= _MAX_TEXT_LENGTH:
            await message.answer("Текст має містити від 1 до 2000 символів.")
            return
        data = await state.get_data()
        subject_id = UUID(data["subject_id"])
        try:
            suggestion = await ux.suggested_due(_message_user_id(message), subject_id)
        except (KeyError, PermissionError, RuntimeError, ValueError):
            await state.clear()
            await _answer(message, _error_view())
            return
        await state.update_data(
            text=text_value,
            suggested_due=suggestion.isoformat() if suggestion else "",
        )
        if suggestion is None:
            await state.set_state(HomeworkFlow.due)
            await _answer(
                message,
                View(
                    "Наступний урок не знайдено. Введіть дату виконання у форматі РРРР-ММ-ДД.",
                    ((Button("Скасувати", "menu:homework"),),),
                ),
            )
            return
        await _answer(
            message,
            View(
                "Запропонована дата — наступний відповідний урок: "
                f"{suggestion.astimezone(_KYIV):%d.%m.%Y о %H:%M}. Підтвердити?",
                (
                    (Button("✅ Використати цю дату", "hw:due:use"),),
                    (Button("📅 Ввести іншу дату", "hw:due:change"),),
                    (Button("Скасувати", "menu:homework"),),
                ),
            ),
        )

    @router.callback_query(F.data == "hw:due:change")
    async def change_due(callback: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(HomeworkFlow.due)
        await _edit(callback, View("Введіть дату у форматі РРРР-ММ-ДД."))

    async def show_preview(
        event: Message | CallbackQuery, state: FSMContext, due_at: datetime
    ) -> None:
        data = await state.get_data()
        try:
            view = await ux.preview_create(
                _message_user_id(event) if isinstance(event, Message) else _callback_user_id(event),
                UUID(data["subject_id"]),
                data["text"],
                due_at,
            )
        except (KeyError, PermissionError, RuntimeError, ValueError):
            view = _error_view()
        await state.clear()
        if isinstance(event, Message):
            await _answer(event, view)
        else:
            await _edit(event, view)

    @router.callback_query(F.data == "hw:due:use")
    async def use_due(callback: CallbackQuery, state: FSMContext) -> None:
        data = await state.get_data()
        raw_due = data.get("suggested_due", "")
        if not raw_due:
            await state.set_state(HomeworkFlow.due)
            await _edit(callback, View("Пропозиція застаріла. Введіть дату РРРР-ММ-ДД."))
            return
        await show_preview(callback, state, datetime.fromisoformat(raw_due))

    @router.message(HomeworkFlow.due, F.text)
    async def manual_due(message: Message, state: FSMContext) -> None:
        selected = _parse_date(message.text or "")
        if selected is None:
            await message.answer("Некоректна дата. Приклад: 2026-09-29")
            return
        data = await state.get_data()
        previous = (
            datetime.fromisoformat(data["suggested_due"])
            if data.get("suggested_due")
            else None
        )
        await show_preview(message, state, _due_on_date(selected, previous))

    @router.callback_query(F.data.startswith("hw:new:ok:"))
    async def confirm(callback: CallbackQuery) -> None:
        await edit_safely(
            callback,
            ux.confirm_create(
                _callback_user_id(callback), _callback_data(callback).split(":", 3)[3]
            ),
        )

    @router.callback_query(F.data.startswith("hw:draft:no:"))
    async def discard(callback: CallbackQuery) -> None:
        await edit_safely(
            callback,
            ux.discard(_callback_user_id(callback), _callback_data(callback).split(":", 3)[3]),
        )

    @router.callback_query(F.data.startswith("hw:draft:date:"))
    async def change_draft_date(callback: CallbackQuery, state: FSMContext) -> None:
        token = _callback_data(callback).split(":", 3)[3]
        await state.clear()
        await state.update_data(draft_token=token)
        await state.set_state(HomeworkFlow.draft_due)
        await _edit(
            callback,
            View(
                "Введіть нову дату у форматі РРРР-ММ-ДД.",
                ((Button("Скасувати", _callback("hw", "draft", "no", token)),),),
            ),
        )

    @router.message(HomeworkFlow.draft_due, F.text)
    async def save_draft_date(message: Message, state: FSMContext) -> None:
        selected = _parse_date(message.text or "")
        if selected is None:
            await message.answer("Некоректна дата. Приклад: 2026-09-29")
            return
        data = await state.get_data()
        try:
            view = await ux.change_draft_due(
                _message_user_id(message), data["draft_token"], _due_on_date(selected)
            )
        except (KeyError, PermissionError, RuntimeError, ValueError):
            view = _error_view()
        await state.clear()
        await _answer(message, view)

    @router.callback_query(F.data == "hw:list")
    async def listing(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await edit_safely(callback, ux.listing(_callback_user_id(callback)))

    @router.callback_query(F.data.startswith("hw:v:"))
    async def detail(callback: CallbackQuery) -> None:
        item_id = _parse_uuid(_callback_data(callback).split(":", 2)[2])
        if item_id is None:
            await _edit(callback, _error_view())
            return
        await edit_safely(
            callback,
            ux.get_detail(_callback_user_id(callback), item_id),
        )

    @router.callback_query(F.data.startswith("hw:done:"))
    async def toggle(callback: CallbackQuery) -> None:
        parts = _callback_data(callback).split(":")
        if len(parts) != 4:
            await _edit(callback, _error_view())
            return
        _, _, item_id, completed = parts
        parsed_id = _parse_uuid(item_id)
        if parsed_id is None or completed not in {"0", "1"}:
            await _edit(callback, _error_view())
            return
        await edit_safely(
            callback,
            ux.toggle(_callback_user_id(callback), parsed_id, completed=completed == "1"),
        )

    @router.callback_query(F.data.startswith("hw:edit:t:"))
    async def edit_text(callback: CallbackQuery, state: FSMContext) -> None:
        item_id = _parse_uuid(_callback_data(callback).split(":", 3)[3])
        if item_id is None:
            await _edit(callback, _error_view())
            return
        try:
            item = await ux.port.get_homework(_callback_user_id(callback), item_id)
        except (KeyError, PermissionError, RuntimeError, ValueError):
            await _edit(callback, _error_view())
            return
        await state.clear()
        await state.update_data(item_id=str(item.id), version=item.version)
        await state.set_state(HomeworkFlow.edit_text)
        await _edit(callback, View("Надішліть новий текст завдання (до 2000 символів)."))

    @router.message(HomeworkFlow.edit_text, F.text)
    async def save_text(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        try:
            view = await ux.update_text(
                _message_user_id(message),
                UUID(data["item_id"]),
                int(data["version"]),
                message.text or "",
            )
        except (KeyError, PermissionError, RuntimeError, ValueError):
            view = _error_view()
        await state.clear()
        await _answer(message, view)

    @router.callback_query(F.data.startswith("hw:edit:d:"))
    async def edit_due(callback: CallbackQuery, state: FSMContext) -> None:
        item_id = _parse_uuid(_callback_data(callback).split(":", 3)[3])
        if item_id is None:
            await _edit(callback, _error_view())
            return
        try:
            item = await ux.port.get_homework(_callback_user_id(callback), item_id)
        except (KeyError, PermissionError, RuntimeError, ValueError):
            await _edit(callback, _error_view())
            return
        await state.clear()
        await state.update_data(
            item_id=str(item.id), version=item.version, previous_due=item.due_at.isoformat()
        )
        await state.set_state(HomeworkFlow.edit_due)
        await _edit(callback, View("Введіть нову дату у форматі РРРР-ММ-ДД."))

    @router.message(HomeworkFlow.edit_due, F.text)
    async def save_due(message: Message, state: FSMContext) -> None:
        selected = _parse_date(message.text or "")
        if selected is None:
            await message.answer("Некоректна дата. Приклад: 2026-09-29")
            return
        data = await state.get_data()
        try:
            view = await ux.update_due(
                _message_user_id(message),
                UUID(data["item_id"]),
                int(data["version"]),
                _due_on_date(selected, datetime.fromisoformat(data["previous_due"])),
            )
        except (KeyError, PermissionError, RuntimeError, ValueError):
            view = _error_view()
        await state.clear()
        await _answer(message, view)

    @router.callback_query(F.data.startswith("hw:del:ok:"))
    async def delete(callback: CallbackQuery) -> None:
        parts = _callback_data(callback).split(":")
        if len(parts) != 5:
            await _edit(callback, _error_view())
            return
        _, _, _, item_id, version = parts
        parsed_id = _parse_uuid(item_id)
        if parsed_id is None or not version.isdecimal():
            await _edit(callback, _error_view())
            return
        await edit_safely(
            callback,
            ux.delete(_callback_user_id(callback), parsed_id, int(version)),
        )

    @router.callback_query(F.data.startswith("hw:del:"))
    async def delete_prompt(callback: CallbackQuery) -> None:
        parts = _callback_data(callback).split(":")
        if len(parts) != 4:
            await _edit(callback, _error_view())
            return
        _, _, item_id, version = parts
        parsed_id = _parse_uuid(item_id)
        if parsed_id is None or not version.isdecimal():
            await _edit(callback, _error_view())
            return
        await edit_safely(
            callback,
            ux.delete_prompt(_callback_user_id(callback), parsed_id, int(version)),
        )

    return router

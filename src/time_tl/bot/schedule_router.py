from __future__ import annotations

import re
from datetime import date, time

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from time_tl.domain.schedule import (
    ConfirmationError,
    Lesson,
    Parity,
    ProfileLimitReached,
    SnapshotError,
)

from .aiogram_adapter import (
    _answer,
    _callback_data,
    _callback_user_id,
    _edit,
    _message_user_id,
)
from .async_service import AsyncTelegramApplication
from .models import Button, View

_TIME_RE = re.compile(r"^(?P<start>[0-2]\d:[0-5]\d)-(?P<end>[0-2]\d:[0-5]\d)\s+(?P<title>.+)$")
_WEEKDAYS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд")


class ScheduleFlow(StatesGroup):
    profile_name = State()
    lesson_weekday = State()
    lesson_parity = State()
    lesson_details = State()
    override_date = State()
    override_kind = State()
    override_lesson = State()
    import_payload = State()


def build_schedule_router(application: AsyncTelegramApplication) -> Router:
    router = Router(name="time_tl_schedule_fsm")

    @router.callback_query(F.data == "schedule:create")
    async def create_start(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await state.set_state(ScheduleFlow.profile_name)
        await _edit(callback, View("Назвіть розклад (наприклад, 8-А або Парний тиждень)."))

    @router.message(ScheduleFlow.profile_name, F.text)
    async def create_name(message: Message, state: FSMContext) -> None:
        name = (message.text or "").strip()
        if not 1 <= len(name) <= 80:
            await message.answer("Назва має містити від 1 до 80 символів.")
            return
        try:
            profile_id = await application.create_profile(_message_user_id(message), name)
        except ProfileLimitReached:
            await state.clear()
            await message.answer(
                "Можна зберігати не більше трьох розкладів. Видаліть або замініть один."
            )
            return
        await state.clear()
        await _answer(message, _editor_view(profile_id, name))

    @router.callback_query(F.data.startswith("schedule:edit:"))
    async def edit_profile(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        profile_id = _callback_data(callback).split(":", 2)[2]
        await _edit(
            callback, await application.profile_editor_view(_callback_user_id(callback), profile_id)
        )

    @router.callback_query(F.data.startswith("lesson:add:"))
    async def add_lesson_start(callback: CallbackQuery, state: FSMContext) -> None:
        profile_id = _callback_data(callback).split(":", 2)[2]
        await state.clear()
        await state.update_data(profile_id=profile_id)
        await state.set_state(ScheduleFlow.lesson_weekday)
        rows = tuple(
            (Button(label, f"lesson:weekday:{index}"),) for index, label in enumerate(_WEEKDAYS)
        )
        await _edit(
            callback, View("Оберіть день. Субота й неділя доступні як звичайні робочі дні.", rows)
        )

    @router.callback_query(ScheduleFlow.lesson_weekday, F.data.startswith("lesson:weekday:"))
    async def choose_weekday(callback: CallbackQuery, state: FSMContext) -> None:
        await state.update_data(weekday=int(_callback_data(callback).rsplit(":", 1)[1]))
        await state.set_state(ScheduleFlow.lesson_parity)
        rows = (
            (Button("Щотижня / будь-яка дата", "lesson:parity:any:any"),),
            (
                Button("Парний тиждень", "lesson:parity:even:any"),
                Button("Непарний тиждень", "lesson:parity:odd:any"),
            ),
            (
                Button("Парна дата", "lesson:parity:any:even"),
                Button("Непарна дата", "lesson:parity:any:odd"),
            ),
            (
                Button("Парний тиждень + парна дата", "lesson:parity:even:even"),
                Button("Непарний тиждень + непарна дата", "lesson:parity:odd:odd"),
            ),
        )
        await _edit(callback, View("Коли діє цей урок?", rows))

    @router.callback_query(ScheduleFlow.lesson_parity, F.data.startswith("lesson:parity:"))
    async def choose_parity(callback: CallbackQuery, state: FSMContext) -> None:
        _, _, week, day = _callback_data(callback).split(":")
        await state.update_data(week_parity=week, date_parity=day)
        await state.set_state(ScheduleFlow.lesson_details)
        await _edit(
            callback,
            View(
                "Надішліть: HH:MM-HH:MM Назва уроку. Час довільний; пропуски між уроками "
                "і різні перерви визначаються просто часом початку та завершення.\n"
                "За бажанням додайте HTTPS-посилання останнім словом."
            ),
        )

    @router.message(ScheduleFlow.lesson_details, F.text)
    async def save_lesson(message: Message, state: FSMContext) -> None:
        parsed = _parse_lesson(message.text or "")
        if parsed is None:
            await message.answer("Формат: 09:00-09:45 Математика")
            return
        start, end, title, join_url = parsed
        data = await state.get_data()
        try:
            await application.add_lesson(
                _message_user_id(message),
                data["profile_id"],
                weekday=data["weekday"],
                start=start,
                end=end,
                title=title,
                week_parity=Parity(data["week_parity"]),
                date_parity=Parity(data["date_parity"]),
                join_url=join_url,
            )
        except ValueError as exc:
            await message.answer(f"Не вдалося додати урок: {exc}")
            return
        await state.clear()
        await _answer(
            message,
            await application.profile_editor_view(
                _message_user_id(message), data["profile_id"], prefix="Урок збережено."
            ),
        )

    @router.callback_query(F.data.startswith("lt:"))
    async def toggle_lesson(callback: CallbackQuery) -> None:
        _, profile_id, weekday, index, week, day, enabled = _callback_data(callback).split(":")
        await application.set_lesson_enabled(
            _callback_user_id(callback),
            profile_id,
            weekday=int(weekday),
            lesson_index=int(index),
            enabled=enabled == "1",
            week_parity=Parity(week),
            date_parity=Parity(day),
        )
        await callback.answer("Урок увімкнено" if enabled == "1" else "Урок вимкнено")

    @router.callback_query(F.data.startswith("override:add:"))
    async def override_start(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await state.update_data(profile_id=_callback_data(callback).split(":", 2)[2])
        await state.set_state(ScheduleFlow.override_date)
        await _edit(callback, View("Введіть дату винятку у форматі РРРР-ММ-ДД."))

    @router.message(ScheduleFlow.override_date, F.text)
    async def override_date(message: Message, state: FSMContext) -> None:
        try:
            selected = date.fromisoformat((message.text or "").strip())
        except ValueError:
            await message.answer("Некоректна дата. Приклад: 2026-09-26")
            return
        await state.update_data(override_date=selected.isoformat())
        await state.set_state(ScheduleFlow.override_kind)
        await _answer(
            message,
            View(
                "Що зробити цього дня?",
                (
                    (Button("Вихідний / уроків немає", "override:off"),),
                    (Button("Особливий урок", "override:lesson"),),
                ),
            ),
        )

    @router.callback_query(ScheduleFlow.override_kind, F.data == "override:off")
    async def override_off(callback: CallbackQuery, state: FSMContext) -> None:
        data = await state.get_data()
        await application.set_day_off(
            _callback_user_id(callback),
            data["profile_id"],
            date.fromisoformat(data["override_date"]),
        )
        await state.clear()
        await _edit(
            callback,
            await application.profile_editor_view(
                _callback_user_id(callback), data["profile_id"], prefix="Вихідний збережено."
            ),
        )

    @router.callback_query(ScheduleFlow.override_kind, F.data == "override:lesson")
    async def override_lesson_start(callback: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(ScheduleFlow.override_lesson)
        await _edit(callback, View("Надішліть особливий урок: HH:MM-HH:MM Назва."))

    @router.message(ScheduleFlow.override_lesson, F.text)
    async def override_lesson_save(message: Message, state: FSMContext) -> None:
        parsed = _parse_lesson(message.text or "")
        if parsed is None:
            await message.answer("Формат: 10:00-10:45 Фізика")
            return
        start, end, title, join_url = parsed
        data = await state.get_data()
        await application.set_date_lessons(
            _message_user_id(message),
            data["profile_id"],
            date.fromisoformat(data["override_date"]),
            (Lesson(start, end, title, True, join_url),),
        )
        await state.clear()
        await _answer(
            message,
            await application.profile_editor_view(
                _message_user_id(message), data["profile_id"], prefix="Виняток збережено."
            ),
        )

    @router.callback_query(F.data == "schedule:import")
    async def import_start(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await state.set_state(ScheduleFlow.import_payload)
        await _edit(
            callback,
            View(
                "Надішліть текст налаштувань або файл .time-tl.json. Дані буде лише перевірено "
                "і показано перед підтвердженням."
            ),
        )

    @router.message(ScheduleFlow.import_payload, F.text)
    async def import_text(message: Message, state: FSMContext) -> None:
        await _preview_import(message, state, application, (message.text or "").strip())

    @router.message(ScheduleFlow.import_payload, F.document)
    async def import_file(message: Message, state: FSMContext) -> None:
        document = message.document
        assert document is not None
        if document.file_size and document.file_size > 256_000:
            await message.answer("Файл завеликий (максимум 256 КБ).")
            return
        bot = message.bot
        if bot is None:
            raise RuntimeError("Telegram bot is unavailable")
        stream = await bot.download(document)
        if stream is None:
            raise RuntimeError("Telegram download returned no stream")
        raw = stream.read(256_001)
        await _preview_import(message, state, application, raw)

    @router.callback_query(F.data.startswith("import:links:"))
    async def import_with_links(callback: CallbackQuery) -> None:
        token = _callback_data(callback).split(":", 2)[2]
        await confirm_import_or_offer_replacement(
            callback, application, token, confirm_links=True
        )

    @router.callback_query(F.data.startswith("import:replace:"))
    async def choose_import_replacement(callback: CallbackQuery) -> None:
        _, _, token, index, links = _callback_data(callback).split(":")
        try:
            _, view = await application.request_import_replacement_by_index(
                _callback_user_id(callback),
                token,
                int(index),
                confirm_links=links == "1",
            )
        except (ConfirmationError, KeyError, ValueError):
            view = _expired_action_view()
        await _edit(callback, view)

    @router.callback_query(F.data.startswith("delete:request:"))
    async def request_delete(callback: CallbackQuery) -> None:
        profile_id = _callback_data(callback).split(":", 2)[2]
        try:
            view = await application.request_profile_deletion(
                _callback_user_id(callback), profile_id
            )
        except KeyError:
            view = _expired_action_view()
        await _edit(callback, view)

    @router.callback_query(F.data.startswith("delete:confirm:"))
    async def confirm_delete(callback: CallbackQuery) -> None:
        profile_id = _callback_data(callback).split(":", 2)[2]
        try:
            view = await application.delete_profile(_callback_user_id(callback), profile_id)
        except KeyError:
            view = _expired_action_view()
        await _edit(callback, view)

    @router.callback_query(F.data.startswith("export:request:"))
    async def request_export(callback: CallbackQuery) -> None:
        profile_id = _callback_data(callback).split(":", 2)[2]
        try:
            view = await application.request_profile_export(
                _callback_user_id(callback), profile_id
            )
        except KeyError:
            view = _expired_action_view()
        await _edit(callback, view)

    @router.callback_query(F.data.startswith("export:file:"))
    async def export_file(callback: CallbackQuery) -> None:
        from aiogram.types import BufferedInputFile

        _, _, profile_id, links = _callback_data(callback).split(":")
        try:
            payload = await application.export_profile(
                _callback_user_id(callback), profile_id, include_links=links == "1"
            )
        except KeyError:
            await _edit(callback, _expired_action_view())
            return
        if callback.message is not None:
            await callback.message.answer_document(
                BufferedInputFile(payload, filename="time-tl-schedule.time-tl.json"),
                caption=(
                    "Файл готовий. Перешліть його однокласнику; бот покаже розклад "
                    "перед імпортом. Посилання на уроки додано."
                    if links == "1"
                    else "Файл готовий без посилань на уроки. Його можна безпечніше "
                    "переслати однокласнику."
                ),
            )
        await callback.answer()

    return router


async def _preview_import(
    message: Message, state: FSMContext, application: AsyncTelegramApplication, raw: str | bytes
) -> None:
    try:
        token, view = await application.preview_import(_message_user_id(message), raw)
    except (SnapshotError, ValueError) as exc:
        await message.answer(f"Файл не пройшов перевірку: {exc}")
        return
    await state.clear()
    if "окреме підтвердження" in view.text:
        view = View(
            view.text,
            (
                (Button("Імпортувати без посилань", f"import:confirm:{token}"),),
                (Button("Окремо підтверджую посилання", f"import:links:{token}"),),
                (Button("Скасувати", "menu:schedules"),),
            ),
        )
    await _answer(message, view)


async def confirm_import_or_offer_replacement(
    callback: CallbackQuery,
    application: AsyncTelegramApplication,
    token: str,
    *,
    confirm_links: bool,
) -> None:
    user_id = _callback_user_id(callback)
    try:
        view = await application.confirm_import(user_id, token, confirm_links=confirm_links)
    except ProfileLimitReached:
        view = await application.import_replacement_picker(
            user_id, token, confirm_links=confirm_links
        )
    except (ConfirmationError, KeyError, ValueError):
        view = _expired_action_view()
    await _edit(callback, view)


def _expired_action_view() -> View:
    return View(
        "Ця дія вже виконана, застаріла або належить іншому користувачу. "
        "Почніть операцію знову з меню розкладів.",
        ((Button("До розкладів", "menu:schedules"),),),
    )


def _parse_lesson(raw: str) -> tuple[time, time, str, str | None] | None:
    match = _TIME_RE.fullmatch(raw.strip())
    if match is None:
        return None
    try:
        start = time.fromisoformat(match.group("start"))
        end = time.fromisoformat(match.group("end"))
    except ValueError:
        return None
    if end <= start:
        return None
    title = match.group("title").strip()
    parts = title.rsplit(" ", 1)
    join_url = None
    if len(parts) == 2 and parts[1].startswith("https://"):
        title, join_url = parts[0].strip(), parts[1]
    if not title:
        return None
    return start, end, title, join_url


def _editor_view(profile_id: str, name: str, prefix: str = "") -> View:
    text = (
        f"{prefix}\n\n" if prefix else ""
    ) + f"Редактор «{name}». Додайте уроки в будь-який час, вихідний або окремий день."
    return View(
        text,
        (
            (Button("➕ Додати урок", f"lesson:add:{profile_id}"),),
            (Button("📅 Виняток на дату", f"override:add:{profile_id}"),),
            (Button("📤 Поділитися / експорт", f"export:request:{profile_id}"),),
            (Button("🗑 Видалити", f"delete:request:{profile_id}"),),
            (Button("✅ Готово", "menu:schedules"),),
        ),
    )

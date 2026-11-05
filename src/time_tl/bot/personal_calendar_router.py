"""Actor-only, explicitly confirmed occurrence edits; no delivery changes."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Protocol
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from time_tl.domain.calendar_memberships import (
    CalendarAccessDenied,
    CalendarDataUnavailable,
    CalendarLimitExceeded,
)
from time_tl.domain.calendar_overrides import (
    OverrideAction,
    PersonalEventChange,
    PersonalEventChoice,
)
from time_tl.domain.calendars import CalendarSource, MyDay

from .calendar_router import ExistingCalendarAccounts, _bounded, _label
from .models import Button, View

NOTICE = "Зміни лише для вас у «Мій день». Спільний календар та наявні сповіщення не змінюються."
KYIV = ZoneInfo("Europe/Kyiv")


class PersonalCalendarPort(Protocol):
    async def list_event_choices(
        self,
        actor: int,
        calendar_id: UUID,
        day: date,
        now: datetime,
    ) -> tuple[PersonalEventChoice, ...]: ...

    async def read_personal_day(
        self,
        actor: int,
        at: datetime,
        *,
        extra_sources: tuple[CalendarSource, ...] = (),
    ) -> MyDay: ...

    async def save_override(
        self,
        actor: int,
        change: PersonalEventChange,
        now: datetime,
    ) -> int: ...

    async def export_personal_overrides(self, actor: int) -> str: ...


@dataclass(frozen=True, repr=False)
class _Draft:
    actor: int
    calendar_id: UUID
    expires_at: datetime
    stage: str = "date"
    choices: tuple[PersonalEventChoice, ...] = ()
    choice: PersonalEventChoice | None = None
    change: PersonalEventChange | None = None


def _button(text: str, action: str, handle: UUID, suffix: str = "") -> Button:
    value = f"pcal:{action}:{handle.hex}" + (f":{suffix}" if suffix else "")
    if len(value.encode()) > 64:
        raise CalendarDataUnavailable()
    return Button(text, value)


def _day(text: str, now: datetime) -> date:
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", text):
        raise CalendarDataUnavailable()
    result = date.fromisoformat(text)
    today = now.astimezone(KYIV).date()
    if not today <= result <= today + timedelta(days=30):
        raise CalendarDataUnavailable()
    return result


class PersonalCalendarUx:
    def __init__(
        self,
        port: PersonalCalendarPort,
        accounts: ExistingCalendarAccounts,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.port, self.accounts, self.now = port, accounts, now
        self.pending: dict[UUID, _Draft] = {}

    def _now(self) -> datetime:
        now = self.now()
        if now.tzinfo is None or now.utcoffset() is None:
            raise CalendarDataUnavailable()
        return now.astimezone(UTC)

    def abort(self, actor: int) -> None:
        for handle, draft in tuple(self.pending.items()):
            if draft.actor == actor or draft.expires_at <= self._now():
                del self.pending[handle]

    def _get(self, actor: int, handle: UUID) -> _Draft:
        if type(actor) is not int or actor <= 0:
            raise CalendarAccessDenied()
        item = self.pending.get(handle)
        if item is None or item.actor != actor or item.expires_at <= self._now():
            raise CalendarDataUnavailable()
        return item

    def _replace(self, actor: int, handle: UUID, old: _Draft, new: _Draft) -> None:
        if self._get(actor, handle) is not old:
            raise CalendarDataUnavailable()
        self.pending[handle] = new

    async def _account(self, actor: int) -> None:
        if type(actor) is not int or actor <= 0:
            raise CalendarAccessDenied()
        account = await self.accounts.get_existing(actor)
        if account is None or account.user_id != actor:
            raise CalendarAccessDenied()

    async def _current_choice(
        self, actor: int, handle: UUID, draft: _Draft, choice: PersonalEventChoice,
    ) -> None:
        current = await self.port.list_event_choices(
            actor, draft.calendar_id, choice.source_day, self._now(),
        )
        if self._get(actor, handle) is not draft or choice not in current:
            raise CalendarDataUnavailable()

    @staticmethod
    def _view(text: str, handle: UUID, rows: tuple[tuple[Button, ...], ...] = ()) -> View:
        return View(text + "\n\n" + NOTICE, rows + ((_button("Скасувати", "abort", handle),),))

    @_bounded
    async def begin(self, actor: int, calendar_id: UUID) -> tuple[UUID, View]:
        self.abort(actor)
        if len(self.pending) >= 1024:
            raise CalendarLimitExceeded()
        handle = uuid4()
        draft = _Draft(actor, calendar_id, self._now() + timedelta(minutes=10))
        self.pending[handle] = draft
        await self._account(actor)
        self._get(actor, handle)
        return handle, self._view(
            "🗓 Мої зміни\nНадішліть дату: РРРР-ММ-ДД (сьогодні або до 30 днів).", handle
        )

    @_bounded
    async def input(self, actor: int, handle: UUID, text: str) -> View:
        draft = self._get(actor, handle)
        if type(text) is not str or len(text) > 64:
            raise CalendarDataUnavailable()
        if draft.stage == "date":
            day = _day(text.strip(), self._now())
            choices = await self.port.list_event_choices(actor, draft.calendar_id, day, self._now())
            if (
                type(choices) is not tuple
                or len(choices) > 96
                or any(
                    not isinstance(c, PersonalEventChoice)
                    or c.calendar_id != draft.calendar_id
                    or c.source_day != day
                    for c in choices
                )
            ):
                raise CalendarDataUnavailable()
            self._replace(actor, handle, draft, replace(draft, stage="choices", choices=choices))
            rows = tuple(
                (
                    _button(
                        f"{c.start.astimezone(KYIV):%H:%M} · {_label(c.title, 40)}",
                        "choose",
                        handle,
                        str(index),
                    ),
                )
                for index, c in enumerate(choices)
            )
            return self._view(
                "Оберіть заняття." if choices else "На цю дату занять немає.", handle, rows
            )
        if draft.stage != "move" or draft.choice is None:
            raise CalendarDataUnavailable()
        match = re.fullmatch(
            r"([0-9]{4}-[0-9]{2}-[0-9]{2}) ([0-9]{2}:[0-9]{2})[–-]([0-9]{2}:[0-9]{2})", text.strip()
        )
        if match is None:
            raise CalendarDataUnavailable()
        target = _day(match[1], self._now())
        start, end = time.fromisoformat(match[2]), time.fromisoformat(match[3])
        if start >= end:
            raise CalendarDataUnavailable()
        change = PersonalEventChange(draft.choice, OverrideAction.MOVE, target, start, end)
        await self._current_choice(actor, handle, draft, draft.choice)
        return self._preview(actor, handle, draft, change)

    def _preview(
        self, actor: int, handle: UUID, draft: _Draft, change: PersonalEventChange
    ) -> View:
        self._replace(actor, handle, draft, replace(draft, stage="preview", change=change))
        summary = {
            OverrideAction.CANCEL: "Скасувати лише для мене",
            OverrideAction.RESET: "Повернути початкове заняття",
            OverrideAction.MOVE: f"Перенести: {change.target_day} · {change.start}–{change.end}",
        }[change.action]
        return self._view(
            f"Підтвердження\n{_label(change.choice.title)} · {change.choice.source_day}\n{summary}",
            handle,
            ((_button("✅ Зберегти", "save", handle),),),
        )

    @_bounded
    async def action(self, actor: int, handle: UUID, action: str, suffix: str = "") -> View:
        draft = self._get(actor, handle)
        if action == "abort":
            self.abort(actor)
            return View("Зміни скасовано.", ((Button("Мої календарі", "menu:calendars"),),))
        if action == "choose":
            if draft.stage != "choices" or not re.fullmatch(r"[0-9]{1,3}", suffix):
                raise CalendarDataUnavailable()
            index = int(suffix)
            if index >= len(draft.choices):
                raise CalendarDataUnavailable()
            choice = draft.choices[index]
            await self._current_choice(actor, handle, draft, choice)
            self._replace(actor, handle, draft, replace(draft, stage="action", choice=choice))
            rows: tuple[tuple[Button, ...], ...] = (
                (_button("🚫 Скасувати заняття для мене", "drop", handle),),
                (_button("⏰ Перенести для мене", "move", handle),),
            )
            if choice.override_version:
                rows += ((_button("↩️ Повернути початкове", "reset", handle),),)
            return self._view(f"{_label(choice.title)} · {choice.source_day}", handle, rows)
        if action == "save":
            if draft.stage != "preview" or draft.change is None:
                raise CalendarDataUnavailable()
            await self._account(actor)
            if self._get(actor, handle) is not draft:
                raise CalendarDataUnavailable()
            del self.pending[handle]  # Consume BEFORE the write, including an ambiguous timeout.
            await self.port.save_override(actor, draft.change, self._now())
            return View(
                "✅ Особисту зміну збережено.\n" + NOTICE,
                (
                    (Button("🕐 Мій день", "menu:myday"),),
                    (Button("Мої календарі", "menu:calendars"),),
                ),
            )
        if draft.stage != "action" or draft.choice is None:
            raise CalendarDataUnavailable()
        await self._current_choice(actor, handle, draft, draft.choice)
        if action == "move":
            self._replace(actor, handle, draft, replace(draft, stage="move"))
            return self._view("Надішліть нову дату та час: РРРР-ММ-ДД ГГ:ХХ-ГГ:ХХ", handle)
        if action not in {"drop", "reset"}:
            raise CalendarDataUnavailable()
        return self._preview(
            actor,
            handle,
            draft,
            PersonalEventChange(
                draft.choice,
                OverrideAction.CANCEL if action == "drop" else OverrideAction.RESET,
            ),
        )


def build_personal_calendar_router(
    ux: PersonalCalendarUx, *, router: Any, enabled: bool = False
) -> Any:
    if not enabled:
        return router
    from aiogram import BaseMiddleware, F, Router
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.state import State, StatesGroup
    from aiogram.types import CallbackQuery, Message

    from .aiogram_adapter import _answer, _edit

    selected: Router = router

    class Flow(StatesGroup):
        input = State()

    class Navigation(BaseMiddleware):
        async def __call__(self, handler: Any, event: Any, data: dict[str, Any]) -> Any:
            personal = isinstance(event, CallbackQuery) and (event.data or "").startswith("pcal:")
            leave = (
                isinstance(event, CallbackQuery)
                and not personal
                or (isinstance(event, Message) and bool(event.text and event.text.startswith("/")))
            )
            if leave and event.from_user:
                ux.abort(event.from_user.id)
                state = data.get("state")
                if state is not None and await state.get_state() == Flow.input.state:
                    await state.clear()
            return await handler(event, data)

    selected.message.outer_middleware(Navigation())
    selected.callback_query.outer_middleware(Navigation())

    def error() -> View:
        return View(
            "Дія недоступна або застаріла. Перевірте «Мій день», перш ніж повторювати. "
            "Формат дати: РРРР-ММ-ДД; часу: ГГ:ХХ-ГГ:ХХ.",
            ((Button("Мої календарі", "menu:calendars"),),),
        )

    @selected.callback_query(F.data.startswith("pcal:"))
    async def callback(event: CallbackQuery, state: FSMContext) -> None:
        try:
            if event.from_user is None or event.message is None:
                raise CalendarAccessDenied()
            parts = (event.data or "").split(":")
            if len(parts) not in (3, 4) or not re.fullmatch("[a-f0-9]{32}", parts[2]):
                raise CalendarDataUnavailable()
            actor, handle = event.from_user.id, UUID(hex=parts[2])
            if parts[1] == "open" and len(parts) == 3:
                await state.clear()
                handle, view = await ux.begin(actor, handle)
            else:
                view = await ux.action(actor, handle, parts[1], parts[3] if len(parts) == 4 else "")
            pending = ux.pending.get(handle)
            if pending is not None and pending.stage in {"date", "move"}:
                await state.set_data({"personal_handle": handle.hex})
                if ux._get(actor, handle) is not pending:
                    raise CalendarDataUnavailable()
                await state.set_state(Flow.input)
                if ux._get(actor, handle) is not pending:
                    raise CalendarDataUnavailable()
            else:
                await state.clear()
        except Exception:
            if event.from_user is not None:
                ux.abort(event.from_user.id)
            await state.clear()
            view = error()
        await event.answer()
        await _edit(event, view)

    @selected.message(Flow.input, ~F.text.startswith("/"))
    async def message(event: Message, state: FSMContext) -> None:
        try:
            if event.from_user is None:
                raise CalendarAccessDenied()
            data = await state.get_data()
            handle = UUID(hex=data.get("personal_handle", ""))
            view = await ux.input(event.from_user.id, handle, event.text or "")
            if ux._get(event.from_user.id, handle).stage != "move":
                await state.clear()
        except Exception:
            view = error()
        await _answer(event, view)

    return router

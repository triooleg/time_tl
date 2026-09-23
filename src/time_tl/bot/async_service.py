from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import replace
from datetime import date, time
from typing import Any, Protocol

from time_tl.domain.schedule import (
    ImportSessionRepository,
    Lesson,
    Parity,
    ScheduleProfile,
)

from .models import Button, NotificationMode, View
from .service import (
    InMemoryAccountRepository,
    Region,
    TelegramApplication,
    UserAccount,
)


class AsyncAccountRepository(Protocol):
    async def get_or_create(self, user_id: int) -> UserAccount: ...

    async def save(self, account: UserAccount) -> None: ...


class AsyncRegionConfirmationPort(Protocol):
    """Atomically persist a confirmed account region and its AlertScope."""

    async def confirm_region(self, account: UserAccount) -> None: ...


class AsyncRegionResolver(Protocol):
    """Resolve coordinates to a supported region without retaining them."""

    async def resolve(self, latitude: float, longitude: float) -> Region | None: ...


class AsyncTelegramApplication:
    """Async facade for the pure Telegram application and async persistence.

    Presentation and schedule decisions stay in ``TelegramApplication``. Before
    each operation this facade hydrates its private synchronous unit-of-work,
    then awaits persistence after every mutation. Raw coordinates are passed
    directly to the resolver and never copied into an account or pending state.
    """

    def __init__(
        self,
        *,
        regions: Sequence[Region],
        resolver: AsyncRegionResolver,
        repository: AsyncAccountRepository,
        import_sessions: ImportSessionRepository | None = None,
        region_confirmation: AsyncRegionConfirmationPort | None = None,
        allow_process_local_imports: bool = True,
    ) -> None:
        self.repository = repository
        self.region_confirmation = region_confirmation
        self._memory = InMemoryAccountRepository()
        self._app = TelegramApplication(
            regions=regions,
            resolver=resolver,
            repository=self._memory,
            import_sessions=import_sessions,
            allow_in_memory_import_sessions=allow_process_local_imports,
        )
        self._locks: dict[int, asyncio.Lock] = {}

    @property
    def regions(self) -> tuple[Region, ...]:
        return self._app.regions

    def _lock(self, user_id: int) -> asyncio.Lock:
        return self._locks.setdefault(user_id, asyncio.Lock())

    async def _hydrate(self, user_id: int) -> UserAccount:
        account = await self.repository.get_or_create(user_id)
        self._memory.accounts[user_id] = account
        return account

    async def _persist(self, user_id: int) -> None:
        await self.repository.save(self._memory.accounts[user_id])

    async def start(self, user_id: int) -> View:
        await self._hydrate(user_id)
        return self._app.start(user_id)

    def mode_prompt(self) -> View:
        return self._app.mode_prompt()

    async def choose_mode(self, user_id: int, mode: NotificationMode) -> View:
        async with self._lock(user_id):
            await self._hydrate(user_id)
            view = self._app.choose_mode(user_id, mode)
            await self._persist(user_id)
            return view

    def region_prompt(self) -> View:
        return self._app.region_prompt()

    async def submit_location(self, user_id: int, latitude: float, longitude: float) -> View:
        # The wrapped service retains only a Region candidate, never coordinates.
        return await self._app.submit_location(user_id, latitude, longitude)

    def list_regions(self) -> View:
        return self._app.list_regions()

    async def propose_manual_region(self, user_id: int, region_id: str) -> View:
        await self._hydrate(user_id)
        return self._app.propose_manual_region(user_id, region_id)

    async def confirm_region(self, user_id: int, *, confirmed: bool) -> View:
        async with self._lock(user_id):
            await self._hydrate(user_id)
            view = self._app.confirm_region(user_id, confirmed=confirmed)
            if confirmed and self._memory.accounts[user_id].region_id is not None:
                account = self._memory.accounts[user_id]
                if self.region_confirmation is not None:
                    await self.region_confirmation.confirm_region(account)
                else:
                    await self._persist(user_id)
            return view

    async def menu(self, user_id: int, *, prefix: str = "") -> View:
        await self._hydrate(user_id)
        return self._app.menu(user_id, prefix=prefix)

    async def profile_editor_view(self, user_id: int, profile_id: str, *, prefix: str = "") -> View:
        account = await self._hydrate(user_id)
        profile = account.profiles[profile_id]
        lines = [item for item in (prefix, f"Редактор «{profile.name}»:") if item]
        rows: list[tuple[Any, ...]] = []
        for rule in profile.rules:
            parity = f"тиждень:{rule.week_parity.value}, дата:{rule.date_parity.value}"
            for index, lesson in enumerate(rule.lessons):
                mark = "✅" if lesson.enabled else "⏸"
                lines.append(
                    f"{mark} {rule.weekday + 1} день {lesson.start:%H:%M}-{lesson.end:%H:%M} "
                    f"{lesson.title} ({parity})"
                )
                target = 0 if lesson.enabled else 1
                rows.append(
                    (
                        Button(
                            "Вимкнути" if lesson.enabled else "Увімкнути",
                            f"lt:{profile_id}:{rule.weekday}:{index}:"
                            f"{rule.week_parity.value}:{rule.date_parity.value}:{target}",
                        ),
                    )
                )
        rows.extend(
            (
                (Button("➕ Додати урок", f"lesson:add:{profile_id}"),),
                (Button("📅 Виняток на дату", f"override:add:{profile_id}"),),
                (Button("📤 Поділитися / експорт", f"export:request:{profile_id}"),),
                (Button("🗑 Видалити", f"delete:request:{profile_id}"),),
                (Button("✅ Готово", "menu:schedules"),),
            )
        )
        return View("\n".join(lines), tuple(rows))

    async def schedules_menu(self, user_id: int) -> View:
        await self._hydrate(user_id)
        return self._app.schedules_menu(user_id)

    async def request_profile_export(self, user_id: int, profile_id: str) -> View:
        await self._hydrate(user_id)
        return self._app.request_profile_export(user_id, profile_id)

    async def export_profile(
        self, user_id: int, profile_id: str, *, include_links: bool
    ) -> bytes:
        await self._hydrate(user_id)
        return self._app.export_profile(user_id, profile_id, include_links=include_links)

    def reminder_prompt(self) -> View:
        return self._app.reminder_prompt()

    async def set_reminder_lead(self, user_id: int, minutes: int) -> View:
        async with self._lock(user_id):
            await self._hydrate(user_id)
            view = self._app.set_reminder_lead(user_id, minutes)
            await self._persist(user_id)
            return view

    async def create_profile(self, user_id: int, name: str) -> str:
        async with self._lock(user_id):
            await self._hydrate(user_id)
            profile_id = self._app.create_profile(user_id, name)
            await self._persist(user_id)
            return profile_id

    async def delete_profile(self, user_id: int, profile_id: str) -> View:
        async with self._lock(user_id):
            account = await self._hydrate(user_id)
            removed = account.profiles.pop(profile_id)
            self._app._editors.pop((user_id, profile_id), None)
            if removed.active and account.profiles:
                first_id = next(iter(account.profiles))
                account.profiles = {
                    key: replace(profile, active=key == first_id)
                    for key, profile in account.profiles.items()
                }
            await self._persist(user_id)
            return self._app.menu(user_id, prefix=f"Розклад «{removed.name}» видалено.")

    async def request_profile_deletion(self, user_id: int, profile_id: str) -> View:
        account = await self._hydrate(user_id)
        profile = account.profiles[profile_id]
        active_note = (
            " Це активний розклад; після видалення інший розклад стане активним."
            if profile.active and len(account.profiles) > 1
            else ""
        )
        return View(
            f"Видалити розклад «{profile.name}»? Дію неможливо скасувати.{active_note}",
            (
                (Button("🗑 Так, видалити", f"delete:confirm:{profile_id}"),),
                (Button("Скасувати", f"schedule:edit:{profile_id}"),),
            ),
        )

    async def add_lesson(
        self,
        user_id: int,
        profile_id: str,
        *,
        weekday: int,
        start: time,
        end: time,
        title: str,
        enabled: bool = True,
        week_parity: Parity = Parity.ANY,
        date_parity: Parity = Parity.ANY,
        join_url: str | None = None,
    ) -> ScheduleProfile:
        async with self._lock(user_id):
            await self._hydrate(user_id)
            profile = self._app.add_lesson(
                user_id,
                profile_id,
                weekday=weekday,
                start=start,
                end=end,
                title=title,
                enabled=enabled,
                week_parity=week_parity,
                date_parity=date_parity,
                join_url=join_url,
            )
            await self._persist(user_id)
            return profile

    async def set_lesson_enabled(
        self,
        user_id: int,
        profile_id: str,
        *,
        weekday: int,
        lesson_index: int,
        enabled: bool,
        week_parity: Parity = Parity.ANY,
        date_parity: Parity = Parity.ANY,
    ) -> ScheduleProfile:
        async with self._lock(user_id):
            await self._hydrate(user_id)
            profile = self._app.set_lesson_enabled(
                user_id,
                profile_id,
                weekday=weekday,
                lesson_index=lesson_index,
                enabled=enabled,
                week_parity=week_parity,
                date_parity=date_parity,
            )
            await self._persist(user_id)
            return profile

    async def set_date_lessons(
        self, user_id: int, profile_id: str, day: date, lessons: Sequence[Lesson]
    ) -> ScheduleProfile:
        async with self._lock(user_id):
            await self._hydrate(user_id)
            profile = self._app.set_date_lessons(user_id, profile_id, day, lessons)
            await self._persist(user_id)
            return profile

    async def set_day_off(self, user_id: int, profile_id: str, day: date) -> ScheduleProfile:
        async with self._lock(user_id):
            await self._hydrate(user_id)
            profile = self._app.set_day_off(user_id, profile_id, day)
            await self._persist(user_id)
            return profile

    async def request_activation(self, user_id: int, profile_id: str) -> tuple[str, View]:
        await self._hydrate(user_id)
        return self._app.request_activation(user_id, profile_id)

    async def confirm_activation(self, user_id: int, token: str) -> View:
        async with self._lock(user_id):
            await self._hydrate(user_id)
            view = self._app.confirm_activation(user_id, token)
            await self._persist(user_id)
            return view

    async def preview_import(self, user_id: int, raw: str | bytes) -> tuple[str, View]:
        await self._hydrate(user_id)
        return self._app.preview_import(user_id, raw)

    async def confirm_import(
        self, user_id: int, token: str, *, confirm_links: bool = False
    ) -> View:
        async with self._lock(user_id):
            await self._hydrate(user_id)
            view = self._app.confirm_import(user_id, token, confirm_links=confirm_links)
            await self._persist(user_id)
            return view

    async def import_replacement_picker(
        self, user_id: int, import_token: str, *, confirm_links: bool
    ) -> View:
        account = await self._hydrate(user_id)
        rows = tuple(
            (
                Button(
                    f"Замінити «{profile.name}»",
                    f"import:replace:{import_token}:{index}:{int(confirm_links)}",
                ),
            )
            for index, profile in enumerate(account.profiles.values())
        )
        return View(
            "Уже збережено три розклади. Оберіть, який із них замінити. "
            "Заміна відбудеться лише після ще одного підтвердження.",
            (*rows, (Button("Скасувати", "menu:schedules"),)),
        )

    async def request_import_replacement_by_index(
        self,
        user_id: int,
        import_token: str,
        profile_index: int,
        *,
        confirm_links: bool,
    ) -> tuple[str, View]:
        account = await self._hydrate(user_id)
        profiles = tuple(account.profiles.values())
        if profile_index < 0 or profile_index >= len(profiles):
            raise ValueError("replacement profile is no longer available")
        return await self.request_import_replacement(
            user_id,
            import_token,
            profiles[profile_index].profile_id,
            confirm_links=confirm_links,
        )

    async def request_import_replacement(
        self,
        user_id: int,
        import_token: str,
        replace_profile_id: str,
        *,
        confirm_links: bool = False,
    ) -> tuple[str, View]:
        await self._hydrate(user_id)
        return self._app.request_import_replacement(
            user_id, import_token, replace_profile_id, confirm_links=confirm_links
        )

    async def confirm_import_replacement(self, user_id: int, token: str) -> View:
        async with self._lock(user_id):
            await self._hydrate(user_id)
            view = self._app.confirm_import_replacement(user_id, token)
            await self._persist(user_id)
            return view


def build_async_router(
    application: AsyncTelegramApplication, *, allowlist_guard: Any | None = None
) -> Any:
    """Build aiogram handlers without starting polling or sending live messages."""
    from aiogram import F, Router
    from aiogram.filters import Command, CommandStart
    from aiogram.types import CallbackQuery, Message

    from .aiogram_adapter import (
        MANUAL_REGION_LABEL,
        _answer,
        _callback_data,
        _callback_user_id,
        _edit,
        _message_user_id,
    )

    router = Router(name="time_tl_async_public_bot")
    if allowlist_guard is not None:
        from .safety import AiogramAllowlistMiddleware

        middleware = AiogramAllowlistMiddleware(allowlist_guard)
        router.message.outer_middleware(middleware)
        router.callback_query.outer_middleware(middleware)

    @router.message(CommandStart())
    async def start(message: Message) -> None:
        await _answer(message, await application.start(_message_user_id(message)))

    @router.message(Command("menu"))
    async def menu(message: Message) -> None:
        await _answer(message, await application.menu(_message_user_id(message)))

    @router.message(F.location)
    async def location(message: Message) -> None:
        assert message.location is not None
        await _answer(
            message,
            await application.submit_location(
                _message_user_id(message), message.location.latitude, message.location.longitude
            ),
        )

    @router.message(F.text == MANUAL_REGION_LABEL)
    async def manual_region(message: Message) -> None:
        await _answer(message, application.list_regions())

    @router.callback_query(F.data.startswith("mode:"))
    async def choose_mode(callback: CallbackQuery) -> None:
        await _edit(
            callback,
            await application.choose_mode(
                _callback_user_id(callback),
                NotificationMode(_callback_data(callback).split(":", 1)[1]),
            ),
        )

    @router.callback_query(F.data.in_({"region:list", "menu:region"}))
    async def list_regions(callback: CallbackQuery) -> None:
        await _edit(callback, application.list_regions())

    @router.callback_query(F.data == "menu:mode")
    async def modes(callback: CallbackQuery) -> None:
        await _edit(callback, application.mode_prompt())

    @router.callback_query(F.data.startswith("region:pick:"))
    async def pick_region(callback: CallbackQuery) -> None:
        await _edit(
            callback,
            await application.propose_manual_region(
                _callback_user_id(callback), _callback_data(callback).split(":", 2)[2]
            ),
        )

    @router.callback_query(F.data.in_({"region:confirm", "region:reject"}))
    async def confirm_region(callback: CallbackQuery) -> None:
        await _edit(
            callback,
            await application.confirm_region(
                _callback_user_id(callback), confirmed=_callback_data(callback) == "region:confirm"
            ),
        )

    @router.callback_query(F.data == "menu:schedules")
    async def schedules(callback: CallbackQuery) -> None:
        await _edit(callback, await application.schedules_menu(_callback_user_id(callback)))

    @router.callback_query(F.data == "menu:reminder")
    async def reminder_prompt(callback: CallbackQuery) -> None:
        await _edit(callback, application.reminder_prompt())

    @router.callback_query(F.data.startswith("reminder:"))
    async def set_reminder(callback: CallbackQuery) -> None:
        await _edit(
            callback,
            await application.set_reminder_lead(
                _callback_user_id(callback), int(_callback_data(callback).split(":", 1)[1])
            ),
        )

    @router.callback_query(F.data.startswith("activate:request:"))
    async def request_activation(callback: CallbackQuery) -> None:
        _, view = await application.request_activation(
            _callback_user_id(callback), _callback_data(callback).split(":", 2)[2]
        )
        await _edit(callback, view)

    @router.callback_query(F.data.startswith("activate:confirm:"))
    async def activate(callback: CallbackQuery) -> None:
        await _edit(
            callback,
            await application.confirm_activation(
                _callback_user_id(callback), _callback_data(callback).split(":", 2)[2]
            ),
        )

    @router.callback_query(F.data.startswith("import:confirm:"))
    async def import_profile(callback: CallbackQuery) -> None:
        from .schedule_router import confirm_import_or_offer_replacement

        await confirm_import_or_offer_replacement(
            callback,
            application,
            _callback_data(callback).split(":", 2)[2],
            confirm_links=False,
        )

    @router.callback_query(F.data.startswith("replace:confirm:"))
    async def replace_profile(callback: CallbackQuery) -> None:
        from .schedule_router import _expired_action_view

        try:
            view = await application.confirm_import_replacement(
                _callback_user_id(callback), _callback_data(callback).split(":", 2)[2]
            )
        except (KeyError, ValueError):
            view = _expired_action_view()
        await _edit(callback, view)

    from .schedule_router import build_schedule_router

    router.include_router(build_schedule_router(application))
    return router

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import date, time
from time import monotonic
from typing import Any, Protocol

from time_tl.application.schedule_editor import (
    StaleLessonSelection,
    recurring_lesson_fingerprint,
)
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
    _compact_profile_name,
    _recurring_lesson_counts,
)

_WEEKDAYS_UA = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд")


def _parity_label(week: Parity, day: Parity) -> str:
    labels = {
        Parity.ANY: "",
        Parity.ODD: "непарні",
        Parity.EVEN: "парні",
    }
    parts: list[str] = []
    if week is not Parity.ANY:
        parts.append(f"{labels[week]} тижні")
    if day is not Parity.ANY:
        parts.append(f"{labels[day]} дати")
    return " · ".join(parts) if parts else "щотижня"


def _compact_lesson_title(title: str, *, limit: int = 24) -> str:
    normalized = " ".join(title.split())
    return normalized if len(normalized) <= limit else f"{normalized[: limit - 1]}…"


def _lesson_callback(
    action: str,
    profile_id: str,
    rule_index: int,
    lesson_index: int,
    fingerprint: str,
) -> str:
    callback = f"{action}:{profile_id}:{rule_index}:{lesson_index}:{fingerprint}"
    if len(callback.encode("utf-8")) > 64:
        raise ValueError("lesson callback exceeds Telegram limit")
    return callback


def _recurring_lesson_page(profile: ScheduleProfile, rule_index: int, lesson_index: int) -> int:
    position = 0
    for current_rule_index, rule in enumerate(profile.rules):
        for current_lesson_index, _ in enumerate(rule.lessons):
            if current_rule_index == rule_index and current_lesson_index == lesson_index:
                return position // _LESSON_PAGE_SIZE
            position += 1
    raise StaleLessonSelection("lesson selection is stale")


def _selected_recurring_lesson(
    account: UserAccount,
    profile_id: str,
    rule_index: int,
    lesson_index: int,
    expected_fingerprint: str,
) -> tuple[ScheduleProfile, Any, Lesson]:
    try:
        profile = account.profiles[profile_id]
        rule = profile.rules[rule_index]
        lesson = rule.lessons[lesson_index]
        actual = recurring_lesson_fingerprint(profile, rule_index, lesson_index)
    except (KeyError, IndexError) as exc:
        raise StaleLessonSelection("lesson selection is stale") from exc
    if actual != expected_fingerprint:
        raise StaleLessonSelection("lesson selection is stale")
    return profile, rule, lesson


class AsyncAccountRepository(Protocol):
    async def get_or_create(self, user_id: int) -> UserAccount: ...

    async def get_existing(self, user_id: int) -> UserAccount | None: ...

    async def save(self, account: UserAccount) -> None: ...

    async def delete_account(self, user_id: int) -> bool: ...


class AsyncRegionConfirmationPort(Protocol):
    """Atomically persist a confirmed account region and its AlertScope."""

    async def confirm_region(self, account: UserAccount) -> None: ...


class AsyncRegionResolver(Protocol):
    """Resolve coordinates to a supported region without retaining them."""

    async def resolve(self, latitude: float, longitude: float) -> Region | None: ...


class TerritoryCatalogNode(Protocol):
    """Minimal DB read-model shape used by the Telegram presentation layer."""

    @property
    def region_id(self) -> str: ...

    @property
    def name(self) -> str: ...

    @property
    def kind(self) -> str: ...

    @property
    def parent_region_id(self) -> str | None: ...


class AsyncTerritoryCatalog(Protocol):
    """Read the activated territory tree; never performs provider I/O."""

    async def list_top(self, *, provider: str) -> Sequence[TerritoryCatalogNode]: ...

    async def list_children(
        self, *, provider: str, parent_region_id: str
    ) -> Sequence[TerritoryCatalogNode]: ...

    async def get_path(
        self, *, provider: str, region_id: str
    ) -> Sequence[TerritoryCatalogNode]: ...


_TERRITORY_PROVIDER = "alerts.in.ua"
_TERRITORY_PAGE_SIZE = 8
_TERRITORY_SAFETY_NOTE = (
    "⚠️ Бот є допоміжним. Завжди виконуйте офіційні вказівки та прямуйте до укриття."
)
_PRIVACY_NOTICE = (
    "🔒 Конфіденційність\n"
    "Бот зберігає ваш Telegram ID, обрану територію, налаштування та розклади. "
    "Надіслані координати після визначення території не зберігаються. "
    "Посилання на уроки зберігаються зашифрованими й не показуються іншим користувачам. "
    "Керувати цими даними можна через головне меню."
)
_LESSON_PAGE_SIZE = 8


class AccountDataMissing(RuntimeError):
    """The account was deleted or has not been created through /start."""


class BotStateCapacityExceeded(RuntimeError):
    """No idle per-user process-state slot can be safely evicted."""


@dataclass
class _UserLockEntry:
    lock: asyncio.Lock
    touched_at: float
    borrowers: int = 0


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
        territory_catalog: AsyncTerritoryCatalog | None = None,
        allow_process_local_imports: bool = True,
        coordinate_suggestions_enabled: bool = False,
        onboarding_text: str = "",
        process_state_max_users: int = 256,
        process_state_ttl_seconds: float = 900.0,
        process_state_clock: Callable[[], float] = monotonic,
    ) -> None:
        if process_state_max_users < 1 or process_state_ttl_seconds <= 0:
            raise ValueError("invalid process-state limits")
        self.repository = repository
        self.region_confirmation = region_confirmation
        self.territory_catalog = territory_catalog
        self._process_state_max_users = process_state_max_users
        self._process_state_ttl_seconds = process_state_ttl_seconds
        self._process_state_clock = process_state_clock
        self._memory = InMemoryAccountRepository(
            max_accounts=process_state_max_users,
            ttl_seconds=process_state_ttl_seconds,
            clock=process_state_clock,
        )
        self._app = TelegramApplication(
            regions=regions,
            resolver=resolver,
            repository=self._memory,
            import_sessions=import_sessions,
            allow_in_memory_import_sessions=allow_process_local_imports,
            coordinate_suggestions_enabled=coordinate_suggestions_enabled,
            process_state_max_users=process_state_max_users,
            process_state_ttl_seconds=process_state_ttl_seconds,
            process_state_clock=process_state_clock,
        )
        self._locks: OrderedDict[int, _UserLockEntry] = OrderedDict()
        self._onboarding_text = onboarding_text.strip()

    @property
    def regions(self) -> tuple[Region, ...]:
        return self._app.regions

    def _prune_lock_cache(self, now: float | None = None) -> None:
        current = self._process_state_clock() if now is None else now
        expired = [
            user_id
            for user_id, entry in self._locks.items()
            if entry.borrowers == 0
            and not entry.lock.locked()
            and current - entry.touched_at >= self._process_state_ttl_seconds
        ]
        for user_id in expired:
            self._locks.pop(user_id, None)

    def _evict_idle_lock(self) -> bool:
        for user_id, entry in tuple(self._locks.items()):
            if entry.borrowers == 0 and not entry.lock.locked():
                self._locks.pop(user_id, None)
                return True
        return False

    @asynccontextmanager
    async def _user_lock(self, user_id: int) -> AsyncIterator[None]:
        now = self._process_state_clock()
        self._prune_lock_cache(now)
        entry = self._locks.get(user_id)
        if entry is None:
            while len(self._locks) >= self._process_state_max_users:
                if not self._evict_idle_lock():
                    raise BotStateCapacityExceeded("all user state slots are busy")
            entry = _UserLockEntry(asyncio.Lock(), now)
            self._locks[user_id] = entry
        else:
            entry.touched_at = now
            self._locks.move_to_end(user_id)
        entry.borrowers += 1
        try:
            async with entry.lock:
                yield
        finally:
            entry.borrowers -= 1
            entry.touched_at = self._process_state_clock()
            if self._locks.get(user_id) is entry:
                self._locks.move_to_end(user_id)

    def _lock(self, user_id: int) -> Any:
        """Compatibility shim for durable import facade subclasses."""
        return self._user_lock(user_id)

    def _drop_user_lock_if_idle(self, user_id: int) -> None:
        entry = self._locks.get(user_id)
        if entry is not None and entry.borrowers == 0 and not entry.lock.locked():
            self._locks.pop(user_id, None)

    async def _hydrate(self, user_id: int) -> UserAccount:
        account = await self.repository.get_existing(user_id)
        if account is None:
            self._app.clear_user_process_state(user_id)
            raise AccountDataMissing(user_id)
        self._memory.accounts[user_id] = account
        return account

    async def account_exists(self, user_id: int) -> bool:
        account = await self.repository.get_existing(user_id)
        if account is None:
            self._app.clear_user_process_state(user_id)
            return False
        self._memory.accounts[user_id] = account
        return True

    async def _persist(self, user_id: int) -> None:
        await self.repository.save(self._memory.accounts[user_id])

    async def start(self, user_id: int) -> View:
        account = await self.repository.get_or_create(user_id)
        self._memory.accounts[user_id] = account
        return self._app.start(user_id)

    def onboarding(self) -> View:
        text = (
            f"{self._onboarding_text}\n\n{_PRIVACY_NOTICE}"
            if self._onboarding_text
            else _PRIVACY_NOTICE
        )
        return View(text)

    async def start_sequence(self, user_id: int) -> tuple[View, ...]:
        onboarding = self.onboarding()
        menu = await self.start(user_id)
        return (onboarding, menu)

    def mode_prompt(self) -> View:
        return self._app.mode_prompt()

    def info(self) -> View:
        return self._app.info()

    async def choose_mode(self, user_id: int, mode: NotificationMode) -> View:
        async with self._user_lock(user_id):
            await self._hydrate(user_id)
            view = self._app.choose_mode(user_id, mode)
            await self._persist(user_id)
            return view

    def region_prompt(self) -> View:
        return self._app.region_prompt()

    async def submit_location(self, user_id: int, latitude: float, longitude: float) -> View:
        # The wrapped service retains only a Region candidate, never coordinates.
        async with self._user_lock(user_id):
            await self._hydrate(user_id)
            return await self._app.submit_location(user_id, latitude, longitude)

    def list_regions(self) -> View:
        return self._app.list_regions()

    async def territory_top(self, *, page: int = 0) -> View:
        if self.territory_catalog is None:
            return self.list_regions()
        try:
            nodes = await self.territory_catalog.list_top(provider=_TERRITORY_PROVIDER)
        except (LookupError, RuntimeError):
            return self._territory_unavailable_view()
        return self._territory_list_view(
            title="🇺🇦 Оберіть область",
            nodes=nodes,
            page=page,
            page_callback="territory:top:{page}",
            back_callback="menu:home",
        )

    async def territory_open(self, region_id: str, *, page: int = 0) -> View:
        """Open a DB node and either list its children or ask for confirmation."""

        if self.territory_catalog is None:
            return self.list_regions()
        try:
            path = tuple(
                await self.territory_catalog.get_path(
                    provider=_TERRITORY_PROVIDER, region_id=region_id
                )
            )
            children = tuple(
                await self.territory_catalog.list_children(
                    provider=_TERRITORY_PROVIDER, parent_region_id=region_id
                )
            )
        except (LookupError, RuntimeError):
            return self._territory_expired_view()
        if not path or path[-1].region_id != region_id:
            return self._territory_expired_view()

        if children:
            child_kind = children[0].kind
            title = "Оберіть район" if child_kind == "RAION" else "Оберіть громаду"
            back_callback = (
                "territory:top:0" if len(path) == 1 else f"territory:open:{path[-2].region_id}:0"
            )
            return self._territory_list_view(
                title=title,
                nodes=children,
                page=page,
                page_callback=f"territory:open:{region_id}:{{page}}",
                back_callback=back_callback,
                path=path,
            )

        path_lines = "\n".join(f"📍 {node.name}" for node in path)
        back_callback = (
            "territory:top:0" if len(path) == 1 else f"territory:open:{path[-2].region_id}:0"
        )
        return View(
            "Підтвердьте територію\n\n"
            f"{path_lines}\n\n"
            "Сповіщення надходитимуть для цієї території та тривог, "
            "що поширюються на неї з вищих рівнів.\n\n"
            f"{_TERRITORY_SAFETY_NOTE}",
            (
                (Button("✅ Підтвердити", f"territory:confirm:{region_id}"),),
                (Button("⬅️ Назад", back_callback),),
                (Button("🏠 Головне меню", "menu:home"),),
            ),
        )

    async def confirm_catalog_territory(self, user_id: int, region_id: str) -> View:
        """Re-read a leaf and its path before persisting; callbacks carry no trust."""

        if self.territory_catalog is None:
            return self._territory_expired_view()
        async with self._user_lock(user_id):
            try:
                path = tuple(
                    await self.territory_catalog.get_path(
                        provider=_TERRITORY_PROVIDER, region_id=region_id
                    )
                )
                children = tuple(
                    await self.territory_catalog.list_children(
                        provider=_TERRITORY_PROVIDER, parent_region_id=region_id
                    )
                )
            except (LookupError, RuntimeError):
                return self._territory_expired_view()
            if not path or path[-1].region_id != region_id or children:
                return self._territory_expired_view()

            account = await self._hydrate(user_id)
            account.region_id = path[-1].region_id
            account.region_name = path[-1].name
            self._memory.accounts[user_id] = account
            if self.region_confirmation is not None:
                await self.region_confirmation.confirm_region(account)
            else:
                await self._persist(user_id)

            path_lines = "\n".join(f"📍 {node.name}" for node in path)
            return View(
                "✅ Територію обрано\n\n"
                f"{path_lines}\n\n"
                "Сповіщення про тривоги надходитимуть для обраної території.\n\n"
                f"{_TERRITORY_SAFETY_NOTE}",
                ((Button("🏠 Головне меню", "menu:home"),),),
            )

    @staticmethod
    def _territory_unavailable_view() -> View:
        return View(
            "Дерево територій тимчасово недоступне. Налаштування не змінено. "
            "Спробуйте ще раз пізніше.\n\n"
            f"{_TERRITORY_SAFETY_NOTE}",
            ((Button("🏠 Головне меню", "menu:home"),),),
        )

    @staticmethod
    def _territory_expired_view() -> View:
        return View(
            "Ця територія більше недоступна або довідник оновився. "
            "Налаштування не змінено — оберіть територію ще раз.\n\n"
            f"{_TERRITORY_SAFETY_NOTE}",
            (
                (Button("🔄 Обрати територію", "territory:top:0"),),
                (Button("🏠 Головне меню", "menu:home"),),
            ),
        )

    @staticmethod
    def _territory_list_view(
        *,
        title: str,
        nodes: Sequence[TerritoryCatalogNode],
        page: int,
        page_callback: str,
        back_callback: str,
        path: Sequence[TerritoryCatalogNode] = (),
    ) -> View:
        page_count = max(1, (len(nodes) + _TERRITORY_PAGE_SIZE - 1) // _TERRITORY_PAGE_SIZE)
        safe_page = min(max(page, 0), page_count - 1)
        start = safe_page * _TERRITORY_PAGE_SIZE
        visible = nodes[start : start + _TERRITORY_PAGE_SIZE]
        path_text = "\n".join(f"📍 {node.name}" for node in path)
        text = title
        if path_text:
            text += f"\n\n{path_text}"
        if page_count > 1:
            text += f"\n\nСторінка {safe_page + 1} з {page_count}"
        text += f"\n\n{_TERRITORY_SAFETY_NOTE}"
        rows: list[tuple[Button, ...]] = [
            (Button(node.name, f"territory:open:{node.region_id}:0"),) for node in visible
        ]
        pagination: list[Button] = []
        if safe_page > 0:
            pagination.append(Button("◀️", page_callback.format(page=safe_page - 1)))
        if safe_page + 1 < page_count:
            pagination.append(Button("▶️", page_callback.format(page=safe_page + 1)))
        if pagination:
            rows.append(tuple(pagination))
        rows.extend(
            (
                (Button("⬅️ Назад", back_callback),),
                (Button("🏠 Головне меню", "menu:home"),),
            )
        )
        return View(text, tuple(rows))

    async def propose_manual_region(self, user_id: int, region_id: str) -> View:
        await self._hydrate(user_id)
        return self._app.propose_manual_region(user_id, region_id)

    async def confirm_region(self, user_id: int, *, confirmed: bool) -> View:
        async with self._user_lock(user_id):
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

    async def profile_editor_view(
        self,
        user_id: int,
        profile_id: str,
        *,
        prefix: str = "",
        page: int = 0,
        focus_rule_index: int | None = None,
        focus_lesson_index: int | None = None,
    ) -> View:
        account = await self._hydrate(user_id)
        profile = account.profiles[profile_id]
        profiles = tuple(account.profiles.values())
        slot = next(
            index for index, item in enumerate(profiles, start=1) if item.profile_id == profile_id
        )
        enabled, disabled = _recurring_lesson_counts(profile)
        status = (
            "✅ Активний — нагадування надсилаються за цим розкладом."
            if profile.active
            else "▫️ Неактивний — нагадування з нього не надсилаються."
        )
        lessons = [
            (rule_index, rule, lesson_index, lesson)
            for rule_index, rule in enumerate(profile.rules)
            for lesson_index, lesson in enumerate(rule.lessons)
        ]
        page_count = max(1, (len(lessons) + _LESSON_PAGE_SIZE - 1) // _LESSON_PAGE_SIZE)
        if focus_rule_index is not None and focus_lesson_index is not None:
            focus_position = next(
                (
                    position
                    for position, (rule_index, _, lesson_index, _) in enumerate(lessons)
                    if rule_index == focus_rule_index and lesson_index == focus_lesson_index
                ),
                0,
            )
            page = focus_position // _LESSON_PAGE_SIZE
        safe_page = min(max(page, 0), page_count - 1)
        start = safe_page * _LESSON_PAGE_SIZE
        visible = lessons[start : start + _LESSON_PAGE_SIZE]
        lines = [
            item
            for item in (
                prefix,
                f"Розклад {slot} з {len(profiles)}: «{_compact_profile_name(profile.name)}»",
                status,
                f"Повторювані записи: {enabled} увімкнено, {disabled} вимкнено.",
                f"Винятки на дати: {len(profile.overrides)}.",
                f"Сторінка уроків {safe_page + 1} з {page_count}" if page_count > 1 else "",
            )
            if item
        ]
        rows: list[tuple[Any, ...]] = []
        if not profile.active:
            rows.append((Button("✅ Зробити активним", f"activate:request:{profile.profile_id}"),))
        for rule_index, rule, lesson_index, lesson in visible:
            condition = _parity_label(rule.week_parity, rule.date_parity)
            mark = "✅" if lesson.enabled else "⏸"
            lines.append(
                f"{_WEEKDAYS_UA[rule.weekday]} · {condition}\n"
                f"{mark} {lesson.start:%H:%M}–{lesson.end:%H:%M} {lesson.title}"
            )
            fingerprint = recurring_lesson_fingerprint(profile, rule_index, lesson_index)
            rows.append(
                (
                    Button(
                        (
                            f"✏️ {_WEEKDAYS_UA[rule.weekday]} {lesson.start:%H:%M} "
                            f"{_compact_lesson_title(lesson.title)}"
                        ),
                        _lesson_callback("le", profile_id, rule_index, lesson_index, fingerprint),
                    ),
                    Button(
                        "⏸ Вимкнути" if lesson.enabled else "▶️ Увімкнути",
                        _lesson_callback("leg", profile_id, rule_index, lesson_index, fingerprint),
                    ),
                )
            )
        pagination: list[Button] = []
        if safe_page > 0:
            pagination.append(Button("◀️", f"schedule:page:{profile_id}:{safe_page - 1}"))
        if safe_page + 1 < page_count:
            pagination.append(Button("▶️", f"schedule:page:{profile_id}:{safe_page + 1}"))
        if pagination:
            rows.append(tuple(pagination))
        rows.extend(
            (
                (Button("➕ Додати урок", f"lesson:add:{profile_id}"),),
                (Button("📅 Виняток на дату", f"override:add:{profile_id}"),),
                (Button("📤 Поділитися / експорт", f"export:request:{profile_id}"),),
                (Button("🗑 Видалити", f"delete:request:{profile_id}"),),
                (Button("⬅️ До розкладів", "menu:schedules"),),
                (Button("🏠 Головне меню", "menu:home"),),
            )
        )
        return View("\n".join(lines), tuple(rows))

    async def lesson_editor_view(
        self,
        user_id: int,
        profile_id: str,
        *,
        rule_index: int,
        lesson_index: int,
        expected_fingerprint: str,
        prefix: str = "",
    ) -> View:
        account = await self._hydrate(user_id)
        profile, rule, lesson = _selected_recurring_lesson(
            account,
            profile_id,
            rule_index,
            lesson_index,
            expected_fingerprint,
        )
        fingerprint = recurring_lesson_fingerprint(profile, rule_index, lesson_index)
        status = "✅ увімкнений" if lesson.enabled else "⏸ вимкнений"
        text = "\n".join(
            item
            for item in (
                prefix,
                "✏️ Повторюваний урок",
                (
                    f"📅 {_WEEKDAYS_UA[rule.weekday]} · "
                    f"{_parity_label(rule.week_parity, rule.date_parity)}"
                ),
                f"🕒 {lesson.start:%H:%M}–{lesson.end:%H:%M}",
                f"📚 {lesson.title}",
                f"Стан: {status}",
            )
            if item
        )
        target = (profile_id, rule_index, lesson_index, fingerprint)
        lesson_page = _recurring_lesson_page(profile, rule_index, lesson_index)
        return View(
            text,
            (
                (Button("✏️ Змінити назву", _lesson_callback("len", *target)),),
                (Button("🕒 Змінити час", _lesson_callback("let", *target)),),
                (
                    Button(
                        "⏸ Вимкнути" if lesson.enabled else "▶️ Увімкнути",
                        _lesson_callback("leg", *target),
                    ),
                ),
                (Button("🗑 Видалити урок", _lesson_callback("led", *target)),),
                (
                    Button(
                        "⬅️ До розкладу",
                        f"schedule:page:{profile_id}:{lesson_page}",
                    ),
                ),
                (Button("🏠 Головне меню", "lesson:home"),),
            ),
        )

    async def schedules_menu(self, user_id: int) -> View:
        await self._hydrate(user_id)
        return self._app.schedules_menu(user_id)

    async def request_profile_export(self, user_id: int, profile_id: str) -> View:
        await self._hydrate(user_id)
        return self._app.request_profile_export(user_id, profile_id)

    async def export_profile(self, user_id: int, profile_id: str, *, include_links: bool) -> bytes:
        await self._hydrate(user_id)
        return self._app.export_profile(user_id, profile_id, include_links=include_links)

    def reminder_prompt(self) -> View:
        return self._app.reminder_prompt()

    async def set_reminder_lead(self, user_id: int, minutes: int) -> View:
        async with self._user_lock(user_id):
            await self._hydrate(user_id)
            view = self._app.set_reminder_lead(user_id, minutes)
            await self._persist(user_id)
            return view

    async def create_profile(self, user_id: int, name: str) -> str:
        async with self._user_lock(user_id):
            await self._hydrate(user_id)
            profile_id = self._app.create_profile(user_id, name)
            await self._persist(user_id)
            return profile_id

    async def delete_profile(self, user_id: int, profile_id: str) -> View:
        async with self._user_lock(user_id):
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
        async with self._user_lock(user_id):
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
        async with self._user_lock(user_id):
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

    async def edit_lesson_title(
        self,
        user_id: int,
        profile_id: str,
        *,
        rule_index: int,
        lesson_index: int,
        expected_fingerprint: str,
        title: str,
    ) -> ScheduleProfile:
        async with self._user_lock(user_id):
            account = await self._hydrate(user_id)
            _, _, lesson = _selected_recurring_lesson(
                account, profile_id, rule_index, lesson_index, expected_fingerprint
            )
            profile = self._app.edit_lesson(
                user_id,
                profile_id,
                rule_index=rule_index,
                lesson_index=lesson_index,
                expected_fingerprint=expected_fingerprint,
                start=lesson.start,
                end=lesson.end,
                title=title,
            )
            await self._persist(user_id)
            return profile

    async def edit_lesson_time(
        self,
        user_id: int,
        profile_id: str,
        *,
        rule_index: int,
        lesson_index: int,
        expected_fingerprint: str,
        start: time,
        end: time,
    ) -> ScheduleProfile:
        async with self._user_lock(user_id):
            account = await self._hydrate(user_id)
            _, _, lesson = _selected_recurring_lesson(
                account, profile_id, rule_index, lesson_index, expected_fingerprint
            )
            profile = self._app.edit_lesson(
                user_id,
                profile_id,
                rule_index=rule_index,
                lesson_index=lesson_index,
                expected_fingerprint=expected_fingerprint,
                start=start,
                end=end,
                title=lesson.title,
            )
            await self._persist(user_id)
            return profile

    async def toggle_selected_lesson(
        self,
        user_id: int,
        profile_id: str,
        *,
        rule_index: int,
        lesson_index: int,
        expected_fingerprint: str,
    ) -> ScheduleProfile:
        async with self._user_lock(user_id):
            account = await self._hydrate(user_id)
            _, _, lesson = _selected_recurring_lesson(
                account, profile_id, rule_index, lesson_index, expected_fingerprint
            )
            profile = self._app.set_lesson_enabled_by_selector(
                user_id,
                profile_id,
                rule_index=rule_index,
                lesson_index=lesson_index,
                expected_fingerprint=expected_fingerprint,
                enabled=not lesson.enabled,
            )
            await self._persist(user_id)
            return profile

    async def delete_lesson(
        self,
        user_id: int,
        profile_id: str,
        *,
        rule_index: int,
        lesson_index: int,
        expected_fingerprint: str,
    ) -> ScheduleProfile:
        async with self._user_lock(user_id):
            await self._hydrate(user_id)
            profile = self._app.delete_lesson(
                user_id,
                profile_id,
                rule_index=rule_index,
                lesson_index=lesson_index,
                expected_fingerprint=expected_fingerprint,
            )
            await self._persist(user_id)
            return profile

    async def set_date_lessons(
        self, user_id: int, profile_id: str, day: date, lessons: Sequence[Lesson]
    ) -> ScheduleProfile:
        async with self._user_lock(user_id):
            await self._hydrate(user_id)
            profile = self._app.set_date_lessons(user_id, profile_id, day, lessons)
            await self._persist(user_id)
            return profile

    async def set_day_off(self, user_id: int, profile_id: str, day: date) -> ScheduleProfile:
        async with self._user_lock(user_id):
            await self._hydrate(user_id)
            profile = self._app.set_day_off(user_id, profile_id, day)
            await self._persist(user_id)
            return profile

    async def request_activation(self, user_id: int, profile_id: str) -> tuple[str, View]:
        await self._hydrate(user_id)
        return self._app.request_activation(user_id, profile_id)

    async def confirm_activation(self, user_id: int, token: str) -> View:
        async with self._user_lock(user_id):
            await self._hydrate(user_id)
            view = self._app.confirm_activation(user_id, token)
            await self._persist(user_id)
            return view

    async def preview_import(self, user_id: int, raw: str | bytes) -> tuple[str, View]:
        await self._hydrate(user_id)
        return self._app.preview_import(user_id, raw)

    async def revoke_import_preview(self, user_id: int, token: str) -> None:
        self._app.revoke_import_preview(user_id, token)

    async def confirm_import(
        self, user_id: int, token: str, *, confirm_links: bool = False
    ) -> View:
        async with self._user_lock(user_id):
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
                    f"{'✅ ' if profile.active else ''}Замінити №{index + 1} "
                    f"«{_compact_profile_name(profile.name)}»",
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
        async with self._user_lock(user_id):
            await self._hydrate(user_id)
            view = self._app.confirm_import_replacement(user_id, token)
            await self._persist(user_id)
            return view

    async def request_data_deletion(self, user_id: int) -> View:
        async with self._user_lock(user_id):
            await self._hydrate(user_id)
            return self._app.request_data_deletion(user_id)

    async def review_data_deletion(self, user_id: int, token: str) -> View:
        async with self._user_lock(user_id):
            return self._app.review_data_deletion(user_id, token)

    async def cancel_data_deletion(self, user_id: int) -> View:
        async with self._user_lock(user_id):
            await self._hydrate(user_id)
            return self._app.cancel_data_deletion(user_id)

    async def confirm_data_deletion(self, user_id: int, token: str) -> View:
        async with self._user_lock(user_id):
            if not self._app.data_deletion_is_confirmed(user_id, token):
                view = self._app._expired_data_deletion_view()
            else:
                await self.repository.delete_account(user_id)
                self._app.clear_user_process_state(user_id)
                view = View(
                    "✅ Ваші дані видалено.\n\n"
                    "Telegram ID, територію, налаштування, розклади та посилання "
                    "на уроки видалено. Наступний /start створить нові чисті "
                    "налаштування."
                )
        self._drop_user_lock_if_idle(user_id)
        return view


_DELETED_ACCOUNT_TEXT = (
    "Ваші дані видалено або обліковий запис ще не створено. Надішліть /start, щоб почати заново."
)


class ExistingAccountMiddleware:
    """Ensure only /start can create an account row."""

    def __init__(self, application: AsyncTelegramApplication) -> None:
        self.application = application

    async def __call__(self, handler: Any, event: Any, data: dict[str, Any]) -> Any:
        if self._is_start_message(event):
            return await handler(event, data)
        actor = getattr(event, "from_user", None)
        user_id = getattr(actor, "id", None)
        if user_id is None:
            return None
        if not await self.application.account_exists(int(user_id)):
            await self._respond_missing(event)
            return None
        try:
            return await handler(event, data)
        except AccountDataMissing:
            await self._respond_missing(event)
            return None

    @staticmethod
    def _is_start_message(event: Any) -> bool:
        if event.__class__.__name__ != "Message":
            return False
        text = getattr(event, "text", None)
        if not isinstance(text, str) or not text.strip():
            return False
        command = text.strip().split(maxsplit=1)[0].split("@", 1)[0]
        return command == "/start"

    @staticmethod
    async def _respond_missing(event: Any) -> None:
        if event.__class__.__name__ == "CallbackQuery":
            message = getattr(event, "message", None)
            edit_text = getattr(message, "edit_text", None)
            if callable(edit_text):
                await edit_text(_DELETED_ACCOUNT_TEXT)
            answer = getattr(event, "answer", None)
            if callable(answer):
                await answer()
            return
        answer = getattr(event, "answer", None)
        if callable(answer):
            await answer(_DELETED_ACCOUNT_TEXT)


def build_async_router(
    application: AsyncTelegramApplication,
    *,
    allowlist_guard: Any | None = None,
    ingress_guard: Any | None = None,
) -> Any:
    """Build aiogram handlers without starting polling or sending live messages."""
    from aiogram import F, Router
    from aiogram.filters import Command, CommandStart
    from aiogram.fsm.context import FSMContext
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
    if ingress_guard is not None:
        from .safety import AiogramIngressSafetyMiddleware

        ingress = AiogramIngressSafetyMiddleware(ingress_guard)
        router.message.outer_middleware(ingress)
        router.callback_query.outer_middleware(ingress)
    if allowlist_guard is not None:
        from .safety import AiogramAllowlistMiddleware

        middleware = AiogramAllowlistMiddleware(allowlist_guard)
        router.message.outer_middleware(middleware)
        router.callback_query.outer_middleware(middleware)
    existing_account = ExistingAccountMiddleware(application)
    router.message.outer_middleware(existing_account)
    router.callback_query.outer_middleware(existing_account)

    @router.message(CommandStart())
    async def start(message: Message) -> None:
        for view in await application.start_sequence(_message_user_id(message)):
            await _answer(message, view)

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
        await _answer(message, await application.territory_top())

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
        await _edit(callback, await application.territory_top())

    @router.callback_query(F.data == "menu:home")
    async def home(callback: CallbackQuery) -> None:
        await _edit(callback, await application.menu(_callback_user_id(callback)))

    @router.callback_query(F.data.startswith("territory:top:"))
    async def territory_top(callback: CallbackQuery) -> None:
        await _edit(
            callback,
            await application.territory_top(page=int(_callback_data(callback).rsplit(":", 1)[1])),
        )

    @router.callback_query(F.data.startswith("territory:open:"))
    async def territory_open(callback: CallbackQuery) -> None:
        _, _, region_id, page = _callback_data(callback).split(":", 3)
        await _edit(
            callback,
            await application.territory_open(region_id, page=int(page)),
        )

    @router.callback_query(F.data.startswith("territory:confirm:"))
    async def territory_confirm(callback: CallbackQuery) -> None:
        await _edit(
            callback,
            await application.confirm_catalog_territory(
                _callback_user_id(callback),
                _callback_data(callback).split(":", 2)[2],
            ),
        )

    @router.callback_query(F.data == "menu:info")
    async def info(callback: CallbackQuery) -> None:
        await _edit(callback, application.info())

    @router.callback_query(F.data == "account:delete:request")
    async def request_data_deletion(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await _edit(
            callback,
            await application.request_data_deletion(_callback_user_id(callback)),
        )

    @router.callback_query(F.data.startswith("account:delete:review:"))
    async def review_data_deletion(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await _edit(
            callback,
            await application.review_data_deletion(
                _callback_user_id(callback),
                _callback_data(callback).rsplit(":", 1)[1],
            ),
        )

    @router.callback_query(F.data == "account:delete:cancel")
    async def cancel_data_deletion(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await _edit(
            callback,
            await application.cancel_data_deletion(_callback_user_id(callback)),
        )

    @router.callback_query(F.data.startswith("account:delete:confirm:"))
    async def confirm_data_deletion(callback: CallbackQuery, state: FSMContext) -> None:
        view = await application.confirm_data_deletion(
            _callback_user_id(callback),
            _callback_data(callback).rsplit(":", 1)[1],
        )
        await state.clear()
        await _edit(callback, view)

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

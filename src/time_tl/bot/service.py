from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Iterator, MutableMapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from time import monotonic
from typing import Protocol, TypeVar, cast
from uuid import uuid4

from time_tl.application.schedule_editor import ScheduleEditor
from time_tl.domain.schedule import (
    ActivationProposal,
    ConfirmationError,
    ImportProposal,
    ImportSessionRepository,
    InMemoryImportSessionRepository,
    Lesson,
    Parity,
    ProfileCollection,
    ProfileLimitReached,
    ReplacementProposal,
    ScheduleProfile,
    encode_snapshot,
    export_snapshot,
    propose_import,
)
from time_tl.domain.schedule import (
    confirm_import as domain_confirm_import,
)
from time_tl.domain.schedule import (
    confirm_import_replacement as domain_confirm_import_replacement,
)
from time_tl.domain.schedule import (
    request_import_replacement as domain_request_import_replacement,
)
from time_tl.domain.schedule.resolver import ResolvedDay

from .models import Button, NotificationMode, View

_IMPORT_PREVIEW_MAX_CHARS = 3_500
_IMPORT_PREVIEW_LESSONS_PER_DAY = 6
HOME_NAVIGATION = (
    Button("⚙️ Налаштування", "menu:settings"),
    Button("ℹ️ Допомога", "menu:info"),
)


def _safe_preview_value(raw: object, *, limit: int = 120) -> str:
    value = " ".join(str(raw or "").split())
    value = value.replace("&", "＆").replace("<", "‹").replace(">", "›")
    if not value:
        return "не вказано"
    return value if len(value) <= limit else f"{value[: limit - 1]}…"


_K = TypeVar("_K")
_V = TypeVar("_V")


class _BoundedTTLMap(MutableMapping[_K, _V]):
    """Small process-local LRU cache; durable data always remains authoritative."""

    def __init__(
        self,
        *,
        max_entries: int,
        ttl_seconds: float,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if max_entries < 1 or ttl_seconds <= 0:
            raise ValueError("invalid process-state cache limits")
        self._max_entries = max_entries
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._items: OrderedDict[_K, tuple[float, _V]] = OrderedDict()

    def _prune(self, now: float | None = None) -> None:
        current = self._clock() if now is None else now
        expired = [
            key
            for key, (touched_at, _) in self._items.items()
            if current - touched_at >= self._ttl_seconds
        ]
        for key in expired:
            self._items.pop(key, None)

    def __getitem__(self, key: _K) -> _V:
        now = self._clock()
        self._prune(now)
        _, value = self._items.pop(key)
        self._items[key] = (now, value)
        return value

    def __setitem__(self, key: _K, value: _V) -> None:
        now = self._clock()
        self._prune(now)
        self._items.pop(key, None)
        self._items[key] = (now, value)
        while len(self._items) > self._max_entries:
            self._items.popitem(last=False)

    def __delitem__(self, key: _K) -> None:
        del self._items[key]

    def __iter__(self) -> Iterator[_K]:
        self._prune()
        return iter(tuple(self._items))

    def __len__(self) -> int:
        self._prune()
        return len(self._items)


@dataclass(frozen=True)
class Region:
    region_id: str
    name: str


class RegionResolver(Protocol):
    async def resolve(self, latitude: float, longitude: float) -> Region | None: ...


@dataclass
class UserAccount:
    user_id: int
    mode: NotificationMode | None = None
    region_id: str | None = None
    region_name: str | None = None
    reminder_lead_minutes: int = 5
    profiles: dict[str, ScheduleProfile] = field(default_factory=dict)

    @property
    def onboarding_complete(self) -> bool:
        return self.region_id is not None


class AccountRepository(Protocol):
    def get_or_create(self, user_id: int) -> UserAccount: ...
    def save(self, account: UserAccount) -> None: ...


class InMemoryAccountRepository:
    """Test/local adapter. Production wiring must replace it with PostgreSQL."""

    def __init__(
        self,
        *,
        max_accounts: int = 256,
        ttl_seconds: float = 900.0,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self.accounts: MutableMapping[int, UserAccount] = _BoundedTTLMap(
            max_entries=max_accounts, ttl_seconds=ttl_seconds, clock=clock
        )

    def get_or_create(self, user_id: int) -> UserAccount:
        return self.accounts.setdefault(user_id, UserAccount(user_id))

    def save(self, account: UserAccount) -> None:
        self.accounts[account.user_id] = account


class DisabledImportSessionRepository:
    """Fail-closed adapter used when durable import persistence is unavailable."""

    def _blocked(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError("schedule import requires durable import-session persistence")

    create = assert_pending = bind_replacement = consume_atomically = revoke = _blocked


def _compact_profile_name(name: str, *, limit: int = 30) -> str:
    compact = " ".join(name.split())
    return compact if len(compact) <= limit else compact[: limit - 1].rstrip() + "…"


def _friendly_territory_name(name: str | None) -> str:
    if not name:
        return "не обрано"
    compact = " ".join(name.split())
    if " та " in compact and "територіальна громада" in compact:
        compact = compact.rsplit(" та ", 1)[-1]
    return compact.replace(" територіальна громада", " громада")


def _recurring_lesson_counts(profile: ScheduleProfile) -> tuple[int, int]:
    lessons = tuple(lesson for rule in profile.rules for lesson in rule.lessons)
    enabled = sum(lesson.enabled for lesson in lessons)
    return enabled, len(lessons) - enabled


class TelegramApplication:
    """Telegram use cases which return presentation data and never send messages."""

    def __init__(
        self,
        *,
        regions: Sequence[Region],
        resolver: RegionResolver,
        repository: AccountRepository | None = None,
        import_sessions: ImportSessionRepository | None = None,
        allow_in_memory_import_sessions: bool = True,
        coordinate_suggestions_enabled: bool = False,
        homework_enabled: bool = False,
        my_day_enabled: bool = False,
        calendars_enabled: bool = False,
        power_outages_enabled: bool = False,
        weather_enabled: bool = False,
        process_state_max_users: int = 256,
        process_state_ttl_seconds: float = 900.0,
        process_state_clock: Callable[[], float] = monotonic,
    ) -> None:
        self.regions = tuple(regions)
        self._region_by_id = {item.region_id: item for item in regions}
        self.resolver = resolver
        self.coordinate_suggestions_enabled = coordinate_suggestions_enabled
        self.homework_enabled = homework_enabled
        self.weather_enabled = weather_enabled
        self.my_day_enabled = my_day_enabled
        self.calendars_enabled = calendars_enabled
        self.power_outages_enabled = power_outages_enabled
        self.repository = repository or InMemoryAccountRepository()
        if import_sessions is not None:
            selected_import_sessions = import_sessions
        elif allow_in_memory_import_sessions:
            selected_import_sessions = InMemoryImportSessionRepository()
        else:
            selected_import_sessions = cast(
                ImportSessionRepository, DisabledImportSessionRepository()
            )
        self.import_sessions = selected_import_sessions
        self._pending_regions: MutableMapping[int, Region] = _BoundedTTLMap(
            max_entries=process_state_max_users,
            ttl_seconds=process_state_ttl_seconds,
            clock=process_state_clock,
        )
        self._editors: MutableMapping[tuple[int, str], ScheduleEditor] = _BoundedTTLMap(
            max_entries=process_state_max_users * ProfileCollection.MAX_PROFILES,
            ttl_seconds=process_state_ttl_seconds,
            clock=process_state_clock,
        )
        proposal_limit = process_state_max_users * 2
        self._activations: MutableMapping[
            tuple[int, str], tuple[ProfileCollection, ActivationProposal]
        ] = _BoundedTTLMap(
            max_entries=proposal_limit,
            ttl_seconds=process_state_ttl_seconds,
            clock=process_state_clock,
        )
        self._imports: MutableMapping[tuple[int, str], tuple[ProfileCollection, ImportProposal]] = (
            _BoundedTTLMap(
                max_entries=proposal_limit,
                ttl_seconds=process_state_ttl_seconds,
                clock=process_state_clock,
            )
        )
        self._replacements: MutableMapping[
            tuple[int, str], tuple[ProfileCollection, ReplacementProposal]
        ] = _BoundedTTLMap(
            max_entries=proposal_limit,
            ttl_seconds=process_state_ttl_seconds,
            clock=process_state_clock,
        )
        self._deletions: MutableMapping[int, tuple[str, int]] = _BoundedTTLMap(
            max_entries=process_state_max_users,
            ttl_seconds=process_state_ttl_seconds,
            clock=process_state_clock,
        )

    def start(self, user_id: int) -> View:
        account = self.repository.get_or_create(user_id)
        if account.onboarding_complete:
            return self.menu(user_id)
        return View(
            "Вітаємо! Time TL нагадує про онлайн-уроки та офіційні повітряні "
            "тривоги у вашому підтвердженому регіоні. Бот не замінює офіційні "
            "канали безпеки: під час тривоги прямуйте в укриття.\n\n"
            "Спочатку оберіть режим сповіщень:",
            self._mode_rows(),
        )

    def mode_prompt(self) -> View:
        return View(
            "Оберіть режим сповіщень:",
            self._mode_rows() + ((Button("⬅️ До налаштувань", "menu:settings"),),),
        )

    def choose_mode(self, user_id: int, mode: NotificationMode | None) -> View:
        account = self.repository.get_or_create(user_id)
        account.mode = mode
        self.repository.save(account)
        if mode is None:
            return self.menu(user_id, prefix="🔕 Сповіщення про тривоги й уроки вимкнено.")
        return self.menu(user_id) if account.region_id else self.region_prompt()

    def region_prompt(self) -> View:
        if not self.coordinate_suggestions_enabled:
            return View(
                "Оберіть область, район і громаду вручну. Геолокація стане доступною "
                "лише після окремої перевірки безпечного визначення території.\n\n"
                "⚠️ Бот є допоміжним. Завжди виконуйте офіційні вказівки та прямуйте "
                "до укриття.",
                ((Button("🗺 Обрати територію", "region:list"),),),
            )
        return View(
            "Оберіть спосіб визначення території. Геолокація потрібна лише для "
            "одноразової пропозиції та не зберігається. Запропоновану територію "
            "потрібно підтвердити. Також завжди можна обрати область, район і "
            "громаду вручну.\n\n"
            "⚠️ Бот є допоміжним. Завжди виконуйте офіційні вказівки та прямуйте "
            "до укриття.",
            (
                (Button("📍 Надіслати геолокацію", request_location=True),),
                (Button("🗺 Обрати вручну"),),
            ),
        )

    async def submit_location(self, user_id: int, latitude: float, longitude: float) -> View:
        # Coordinates are deliberately scoped to this await. They are never
        # copied into account or pending state, nor included in user-visible
        # errors where they could later reach logs.
        if not self.coordinate_suggestions_enabled:
            return View(
                "Геолокація ще не підтримується. Оберіть область, район і громаду "
                "вручну. Налаштування не змінено.",
                ((Button("🗺 Обрати територію", "region:list"),),),
            )
        try:
            candidate = await self.resolver.resolve(latitude, longitude)
        except (LookupError, RuntimeError, TimeoutError):
            candidate = None
        if candidate is None or candidate.region_id not in self._region_by_id:
            return View(
                "Не вдалося безпечно визначити підтримувану територію. "
                "Оберіть область, район і громаду вручну. Налаштування не змінено.\n\n"
                "⚠️ Бот є допоміжним. Завжди виконуйте офіційні вказівки.",
                ((Button("🗺 Обрати територію", "region:list"),),),
            )
        self.repository.get_or_create(user_id)
        canonical = self._region_by_id[candidate.region_id]
        self._pending_regions[user_id] = canonical
        confirmation = self._region_confirmation(canonical)
        return View(
            "Геолокацію оброблено одноразово й не збережено.\n\n" + confirmation.text,
            confirmation.rows,
        )

    def list_regions(self) -> View:
        rows = tuple((Button(item.name, f"region:pick:{item.region_id}"),) for item in self.regions)
        return View("Оберіть область або місто зі списку:", rows)

    def propose_manual_region(self, user_id: int, region_id: str) -> View:
        self.repository.get_or_create(user_id)
        candidate = self._region_by_id[region_id]
        self._pending_regions[user_id] = candidate
        return self._region_confirmation(candidate)

    def confirm_region(self, user_id: int, *, confirmed: bool) -> View:
        candidate = self._pending_regions.pop(user_id, None)
        if candidate is None:
            return View("Пропозиція регіону застаріла. Оберіть регіон ще раз.")
        if not confirmed:
            return self.region_prompt()
        account = self.repository.get_or_create(user_id)
        account.region_id, account.region_name = candidate.region_id, candidate.name
        self.repository.save(account)
        if account.mode is None:
            return View("Регіон збережено. Тепер оберіть режим:", self._mode_rows())
        return self.menu(user_id, prefix="Налаштування завершено.")

    def menu(self, user_id: int, *, prefix: str = "") -> View:
        account = self.repository.get_or_create(user_id)
        active = next((profile for profile in account.profiles.values() if profile.active), None)
        mode_label = (
            f"🔔 Сповіщення: {account.mode.label}" if account.mode else
            "🔕 Тривоги й уроки: вимкнено" if account.onboarding_complete else
            "🔔 Сповіщення: не обрано"
        )
        summary = (
            "🏠 Головне меню\n\n"
            f"{mode_label}\n"
            f"📍 Територія: {_friendly_territory_name(account.region_name)}\n"
            f"📚 Розклад: {active.name if active else 'ще немає'}"
        )
        text = f"{prefix}\n\n{summary}" if prefix else summary
        rows: list[tuple[Button, ...]] = []
        day_row: list[Button] = []
        if self.my_day_enabled:
            day_row.append(Button("🕐 Мій день", "menu:myday"))
        if self.calendars_enabled:
            day_row.append(Button("📅 Мої календарі", "menu:calendars"))
        if day_row:
            rows.append(tuple(day_row))
        study_row = [Button("📚 Розклад", "menu:schedules")]
        if self.homework_enabled:
            study_row.append(Button("📝 Домашні завдання", "menu:homework"))
        rows.append(tuple(study_row))
        utility_row: list[Button] = []
        if self.power_outages_enabled:
            utility_row.append(Button("⚡ Електроенергія", "menu:power"))
        if self.weather_enabled:
            utility_row.append(Button("🌤 Погода", "menu:weather"))
        if utility_row:
            rows.append(tuple(utility_row))
        rows.append(HOME_NAVIGATION)
        return View(text, tuple(rows))

    @staticmethod
    def notifications_menu() -> View:
        return View(
            "🔔 Сповіщення\n\n"
            "Оберіть режим тривог і уроків. Час нагадування "
            "можна змінити в налаштуваннях.",
            (
                (Button("🔔 Режим сповіщень", "menu:mode"),),
                (Button("⬅️ До налаштувань", "menu:settings"),),
                (Button("🏠 Головне меню", "menu:home"),),
            ),
        )

    @staticmethod
    def settings_menu() -> View:
        return View(
            "⚙️ Налаштування",
            (
                (Button("🔔 Сповіщення", "menu:notifications"),),
                (Button("📍 Моя територія", "menu:region"),),
                (Button("⏰ Час нагадування", "menu:reminder"),),
                (Button("🔐 Конфіденційність", "settings:privacy"),),
                (Button("🏠 Головне меню", "menu:home"),),
            ),
        )

    def privacy(self) -> View:
        homework_line = (
            "\n• ваші особисті домашні завдання та стан їх виконання;"
            if self.homework_enabled
            else ""
        )
        power_line = (
            "\n• оператора та групу графіків електроенергії; вулиця й номер будинку "
            "використовуються лише під час пошуку та не зберігаються;"
            if self.power_outages_enabled
            else ""
        )
        return View(
            "🔐 Конфіденційність\n\n"
            "Time TL зберігає лише дані, потрібні для роботи бота:\n"
            "• ваш Telegram ID;\n"
            "• підтверджену територію;\n"
            "• режим сповіщень і час нагадування;\n"
            "• ваші розклади."
            f"{homework_line}{power_line}\n\n"
            "Координати після пропозиції території не зберігаються. "
            "Посилання на уроки зберігаються зашифрованими та не показуються "
            "іншим користувачам.",
            (
                (Button("🗑 Видалити мої дані", "account:delete:request"),),
                (Button("⬅️ До налаштувань", "menu:settings"),),
                (Button("🏠 Головне меню", "menu:home"),),
            ),
        )

    @staticmethod
    def info() -> View:
        return View(
            "ℹ️ Допомога\n\nЯк працює Time TL\n\n"
            "🚨 Під час офіційної повітряної тривоги бот повідомляє про небезпеку "
            "для обраної території та радить прямувати до укриття. Бот не стверджує, "
            "що фактичний урок зупинено, і не пропонує підключатися під час тривоги.\n\n"
            "✅ Після підтвердженого офіційного відбою бот перевіряє активний розклад. "
            "Якщо зараз триває або незабаром почнеться урок, бот надішле його назву й час, "
            "коли ці дані доступні. Окреме нагадування до уроку надходить лише тоді, "
            "коли воно ввімкнене в налаштуваннях.\n\n"
            "⚠️ Бот є допоміжним сервісом. Завжди виконуйте офіційні повідомлення "
            "та вказівки служб безпеки.",
            ((Button("🏠 Головне меню", "menu:home"),),),
        )

    def request_data_deletion(self, user_id: int) -> View:
        self.repository.get_or_create(user_id)
        token = uuid4().hex[:12]
        self._deletions[user_id] = (token, 1)
        homework_line = (
            "• усі домашні завдання та позначки виконання;\n" if self.homework_enabled else ""
        )
        return View(
            "⚠️ Видалення особистих даних\n\n"
            "Буде безповоротно видалено:\n"
            "• ваш Telegram ID;\n"
            "• обрану територію;\n"
            "• налаштування сповіщень і нагадувань;\n"
            "• усі розклади та збережені посилання на уроки;\n"
            f"{homework_line}\n"
            "Це не вплине на дані інших користувачів. Продовжити?",
            (
                (Button("Продовжити", f"account:delete:review:{token}"),),
                (Button("Скасувати", "account:delete:cancel"),),
            ),
        )

    def review_data_deletion(self, user_id: int, token: str) -> View:
        if self._deletions.get(user_id) != (token, 1):
            return self._expired_data_deletion_view()
        self._deletions[user_id] = (token, 2)
        return View(
            "🛑 Останнє підтвердження\n\n"
            "Дію неможливо скасувати або відновити. Натисніть "
            "«Так, видалити назавжди», лише якщо справді хочете видалити "
            "ВСІ МОЇ ДАНІ.",
            (
                (
                    Button(
                        "🗑 Так, видалити назавжди",
                        f"account:delete:confirm:{token}",
                    ),
                ),
                (Button("Скасувати", "account:delete:cancel"),),
            ),
        )

    def data_deletion_is_confirmed(self, user_id: int, token: str) -> bool:
        return self._deletions.get(user_id) == (token, 2)

    def cancel_data_deletion(self, user_id: int) -> View:
        self._deletions.pop(user_id, None)
        return self.menu(user_id, prefix="Видалення даних скасовано.")

    def clear_user_process_state(self, user_id: int) -> None:
        import_tokens = [token for owner_id, token in tuple(self._imports) if owner_id == user_id]
        for token in import_tokens:
            try:
                self.import_sessions.revoke(token)
            except (ConfirmationError, KeyError):
                pass
        self._pending_regions.pop(user_id, None)
        self._deletions.pop(user_id, None)
        for state in (
            self._editors,
            self._activations,
            self._imports,
            self._replacements,
        ):
            for key in tuple(state):
                if key[0] == user_id:
                    state.pop(key, None)
        if isinstance(self.repository, InMemoryAccountRepository):
            self.repository.accounts.pop(user_id, None)

    @staticmethod
    def _expired_data_deletion_view() -> View:
        return View(
            "Це підтвердження видалення застаріло або належить іншому користувачу. "
            "Дані не видалено.",
            ((Button("🏠 Головне меню", "menu:home"),),),
        )

    def schedules_menu(self, user_id: int) -> View:
        account = self.repository.get_or_create(user_id)
        if not account.profiles:
            return View(
                "У вас ще немає розкладів. Створіть перший профіль або імпортуйте файл.",
                (
                    (Button("➕ Створити", "schedule:create"),),
                    (Button("📥 Імпортувати", "schedule:import"),),
                ),
            )
        profile_count = len(account.profiles)
        lines = [
            f"🗓 Ваші розклади — {profile_count} з {ProfileCollection.MAX_PROFILES}.",
            "✅ — активний розклад для нагадувань.",
            "Натисніть розклад, щоб переглянути або змінити його.",
        ]
        rows: list[tuple[Button, ...]] = []
        if profile_count < ProfileCollection.MAX_PROFILES:
            rows.append(
                (
                    Button("➕ Створити", "schedule:create"),
                    Button("📥 Імпортувати", "schedule:import"),
                )
            )
        else:
            lines.append("Ліміт заповнено: для нового розкладу видаліть один із наявних.")
            rows.append((Button("📥 Імпортувати із заміною", "schedule:import"),))
        for index, profile in enumerate(account.profiles.values(), start=1):
            enabled, disabled = _recurring_lesson_counts(profile)
            total = enabled + disabled
            lesson_state = "порожній" if total == 0 else f"{enabled}/{total} увімк."
            marker = "✅" if profile.active else "▫️"
            rows.append(
                (
                    Button(
                        f"{marker} {index}. {_compact_profile_name(profile.name)} · {lesson_state}",
                        f"schedule:edit:{profile.profile_id}",
                    ),
                )
            )
        return View("\n".join(lines), tuple(rows))

    def request_profile_export(self, user_id: int, profile_id: str) -> View:
        account = self.repository.get_or_create(user_id)
        profile = account.profiles[profile_id]
        return View(
            f"Експортувати «{profile.name}»? За замовчуванням посилання на уроки "
            "не додаються. Файл можна переслати однокласнику; перед застосуванням "
            "бот обов'язково покаже попередній перегляд.",
            (
                (Button("📄 Без посилань", f"export:file:{profile_id}:0"),),
                (
                    Button(
                        "🔗 Додати посилання — підтверджую",
                        f"export:file:{profile_id}:1",
                    ),
                ),
                (Button("Скасувати", f"schedule:edit:{profile_id}"),),
            ),
        )

    def export_profile(self, user_id: int, profile_id: str, *, include_links: bool) -> bytes:
        account = self.repository.get_or_create(user_id)
        profile = account.profiles[profile_id]
        snapshot = export_snapshot(
            profile,
            source_display_label="Time TL",
            created_at=datetime.now(UTC),
            include_join_links=include_links,
            confirm_include_join_links=include_links,
        )
        return encode_snapshot(snapshot).encode("utf-8")

    def reminder_prompt(self) -> View:
        values = (0, 5, 10, 15, 30)
        return View(
            "За скільки хвилин нагадати про урок?",
            tuple((Button(f"{value} хв", f"reminder:{value}"),) for value in values)
            + ((Button("⬅️ До налаштувань", "menu:settings"),),),
        )

    def set_reminder_lead(self, user_id: int, minutes: int) -> View:
        if minutes not in {0, 5, 10, 15, 30}:
            raise ValueError("unsupported reminder lead")
        account = self.repository.get_or_create(user_id)
        account.reminder_lead_minutes = minutes
        self.repository.save(account)
        return self.menu(user_id, prefix=f"Нагадування: за {minutes} хв.")

    def create_profile(self, user_id: int, name: str) -> str:
        account = self.repository.get_or_create(user_id)
        if len(account.profiles) >= ProfileCollection.MAX_PROFILES:
            raise ProfileLimitReached("a user can store at most three profiles")
        profile_id = uuid4().hex
        editor = ScheduleEditor(profile_id, name)
        self._editors[(user_id, profile_id)] = editor
        account.profiles[profile_id] = editor.build(active=not account.profiles)
        self.repository.save(account)
        return profile_id

    def _editor_for_mutation(self, user_id: int, profile: ScheduleProfile) -> ScheduleEditor:
        """Build from the latest repository state, including DB lesson references.

        PostgreSQL assigns ``storage_ref`` values only after persistence. Reusing
        a pre-save editor would drop those references on the next mutation and
        rotate lesson identity, links, and reminder dedupe keys.
        """

        editor = ScheduleEditor.from_profile(profile)
        self._editors[(user_id, profile.profile_id)] = editor
        return editor

    def add_lesson(
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
        account = self.repository.get_or_create(user_id)
        current = account.profiles[profile_id]
        editor = self._editor_for_mutation(user_id, current)

        editor.add_lesson(
            weekday=weekday,
            start=start,
            end=end,
            title=title,
            enabled=enabled,
            week_parity=week_parity,
            date_parity=date_parity,
            join_url=join_url,
        )
        account.profiles[profile_id] = editor.build(active=current.active)
        self.repository.save(account)
        return account.profiles[profile_id]

    def set_lesson_enabled(
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
        account = self.repository.get_or_create(user_id)
        current = account.profiles[profile_id]
        editor = self._editor_for_mutation(user_id, current)

        editor.set_lesson_enabled(
            weekday=weekday,
            lesson_index=lesson_index,
            enabled=enabled,
            week_parity=week_parity,
            date_parity=date_parity,
        )
        account.profiles[profile_id] = editor.build(active=current.active)
        self.repository.save(account)
        return account.profiles[profile_id]

    def set_lesson_enabled_by_selector(
        self,
        user_id: int,
        profile_id: str,
        *,
        rule_index: int,
        lesson_index: int,
        expected_fingerprint: str,
        enabled: bool,
    ) -> ScheduleProfile:
        account = self.repository.get_or_create(user_id)
        current = account.profiles[profile_id]
        editor = self._editor_for_mutation(user_id, current)

        editor.set_lesson_enabled_by_selector(
            rule_index=rule_index,
            lesson_index=lesson_index,
            expected_fingerprint=expected_fingerprint,
            enabled=enabled,
        )
        account.profiles[profile_id] = editor.build(active=current.active)
        self.repository.save(account)
        return account.profiles[profile_id]

    def edit_lesson(
        self,
        user_id: int,
        profile_id: str,
        *,
        rule_index: int,
        lesson_index: int,
        expected_fingerprint: str,
        start: time,
        end: time,
        title: str,
    ) -> ScheduleProfile:
        account = self.repository.get_or_create(user_id)
        current = account.profiles[profile_id]
        editor = self._editor_for_mutation(user_id, current)

        editor.edit_lesson(
            rule_index=rule_index,
            lesson_index=lesson_index,
            expected_fingerprint=expected_fingerprint,
            start=start,
            end=end,
            title=title,
        )
        account.profiles[profile_id] = editor.build(active=current.active)
        self.repository.save(account)
        return account.profiles[profile_id]

    def delete_lesson(
        self,
        user_id: int,
        profile_id: str,
        *,
        rule_index: int,
        lesson_index: int,
        expected_fingerprint: str,
    ) -> ScheduleProfile:
        account = self.repository.get_or_create(user_id)
        current = account.profiles[profile_id]
        editor = self._editor_for_mutation(user_id, current)

        editor.delete_lesson(
            rule_index=rule_index,
            lesson_index=lesson_index,
            expected_fingerprint=expected_fingerprint,
        )
        account.profiles[profile_id] = editor.build(active=current.active)
        self.repository.save(account)
        return account.profiles[profile_id]

    def set_date_lessons(
        self,
        user_id: int,
        profile_id: str,
        day: date,
        lessons: Sequence[Lesson],
    ) -> ScheduleProfile:
        account = self.repository.get_or_create(user_id)
        current = account.profiles[profile_id]
        editor = self._editor_for_mutation(user_id, current)

        editor.set_date_lessons(day, lessons)
        account.profiles[profile_id] = editor.build(active=current.active)
        self.repository.save(account)
        return account.profiles[profile_id]

    def set_day_off(self, user_id: int, profile_id: str, day: date) -> ScheduleProfile:
        account = self.repository.get_or_create(user_id)
        current = account.profiles[profile_id]
        editor = self._editor_for_mutation(user_id, current)

        editor.set_day_off(day)
        account.profiles[profile_id] = editor.build(active=current.active)
        self.repository.save(account)
        return account.profiles[profile_id]

    def request_activation(self, user_id: int, profile_id: str) -> tuple[str, View]:
        account = self.repository.get_or_create(user_id)
        collection = ProfileCollection(tuple(account.profiles.values()))
        proposal = collection.request_activation(profile_id)
        self._activations[(user_id, proposal.token)] = (collection, proposal)
        lines = [
            f"Активувати «{account.profiles[profile_id].name}»?",
            "Попередній перегляд:",
            *self._preview_lines(proposal.resolved_preview),
        ]
        return proposal.token, View(
            "\n".join(lines),
            (
                (
                    Button("✅ Активувати", f"activate:confirm:{proposal.token}"),
                    Button("Скасувати", "menu:schedules"),
                ),
            ),
        )

    def confirm_activation(self, user_id: int, token: str) -> View:
        collection, proposal = self._activations.pop((user_id, token))
        active = collection.confirm_activation(proposal, confirmed=True)
        account = self.repository.get_or_create(user_id)
        account.profiles = {item.profile_id: item for item in collection.profiles}
        self.repository.save(account)
        return self.menu(user_id, prefix=f"Активовано розклад «{active.name}».")

    def preview_import(self, user_id: int, raw: str | bytes) -> tuple[str, View]:
        account = self.repository.get_or_create(user_id)
        collection = ProfileCollection(tuple(account.profiles.values()))
        proposal = propose_import(raw, sessions=self.import_sessions)
        self._imports[(user_id, proposal.token)] = (collection, proposal)
        return proposal.token, self.import_preview_view(proposal)

    def import_preview_view(self, proposal: ImportProposal) -> View:
        links = "так — потрібне окреме підтвердження" if proposal.links_present else "ні"
        lines = [
            f"Шаблон: {_safe_preview_value(proposal.profile.name)}",
            f"Джерело: {_safe_preview_value(proposal.source_display_label)}",
            f"Створено: {_safe_preview_value(proposal.created_at)}",
            f"Посилання на уроки: {links}",
            "Попередній перегляд 14 днів:",
            *self._bounded_import_preview_lines(proposal.resolved_preview),
        ]
        text = "\n".join(lines)
        if len(text) > _IMPORT_PREVIEW_MAX_CHARS:
            text = (
                text[: _IMPORT_PREVIEW_MAX_CHARS - 80].rstrip()
                + "\n… Перегляд скорочено. Повний розклад буде збережено після підтвердження."
            )
        confirm_callback = f"import:confirm:{proposal.token}"
        if len(confirm_callback.encode("utf-8")) > 64:
            raise ValueError("import callback exceeds Telegram limit")
        return View(
            text,
            (
                (
                    Button("✅ Імпортувати", confirm_callback),
                    Button("Скасувати", "menu:schedules"),
                ),
            ),
        )

    @staticmethod
    def _bounded_import_preview_lines(days: Sequence[ResolvedDay]) -> tuple[str, ...]:
        result: list[str] = []
        for item in days[:14]:
            enabled = [lesson for lesson in item.lessons if lesson.enabled]
            visible = enabled[:_IMPORT_PREVIEW_LESSONS_PER_DAY]
            labels = [
                f"{lesson.start:%H:%M} {_safe_preview_value(lesson.title, limit=48)}"
                for lesson in visible
            ]
            if len(enabled) > len(visible):
                labels.append(f"ще {len(enabled) - len(visible)}")
            gaps = TelegramApplication._gap_preview(item)
            suffix = f"; {gaps}" if gaps else ""
            result.append(f"• {item.day:%d.%m}: {', '.join(labels) or 'вихідний'}{suffix}")
        return tuple(result)

    def revoke_import_preview(self, user_id: int, token: str) -> None:
        self._imports.pop((user_id, token), None)
        try:
            self.import_sessions.revoke(token)
        except (ConfirmationError, KeyError, RuntimeError):
            pass

    def confirm_import(self, user_id: int, token: str, *, confirm_links: bool = False) -> View:
        collection, proposal = self._imports[(user_id, token)]
        stored = domain_confirm_import(
            collection,
            proposal,
            sessions=self.import_sessions,
            confirmed=True,
            confirm_include_join_links=confirm_links,
        )
        del self._imports[(user_id, token)]
        account = self.repository.get_or_create(user_id)
        account.profiles = {item.profile_id: item for item in collection.profiles}
        self.repository.save(account)
        return self.menu(user_id, prefix=f"Імпортовано незалежну копію «{stored.name}».")

    def request_import_replacement(
        self,
        user_id: int,
        import_token: str,
        replace_profile_id: str,
        *,
        confirm_links: bool = False,
    ) -> tuple[str, View]:
        collection, proposal = self._imports[(user_id, import_token)]
        replacement = domain_request_import_replacement(
            collection,
            proposal,
            sessions=self.import_sessions,
            replace_profile_id=replace_profile_id,
            confirm_include_join_links=confirm_links,
        )
        self._replacements[(user_id, replacement.token)] = (collection, replacement)
        profile_name = next(
            item.name for item in collection.profiles if item.profile_id == replace_profile_id
        )
        return replacement.token, View(
            f"Замінити «{profile_name}» імпортованим розкладом «{proposal.profile.name}»?",
            (
                (
                    Button("✅ Замінити", f"replace:confirm:{replacement.token}"),
                    Button("Скасувати", "menu:schedules"),
                ),
            ),
        )

    def confirm_import_replacement(self, user_id: int, token: str) -> View:
        collection, replacement = self._replacements[(user_id, token)]
        stored = domain_confirm_import_replacement(
            collection,
            replacement,
            sessions=self.import_sessions,
            confirmed=True,
        )
        del self._replacements[(user_id, token)]
        del self._imports[(user_id, replacement.import_proposal.token)]
        account = self.repository.get_or_create(user_id)
        account.profiles = {item.profile_id: item for item in collection.profiles}
        self.repository.save(account)
        return self.menu(user_id, prefix=f"Замінено розклад на «{stored.name}».")

    @staticmethod
    def _preview_lines(days: Sequence[ResolvedDay]) -> tuple[str, ...]:
        result = []
        for item in days:
            day, lessons = item.day, item.lessons
            enabled = [lesson for lesson in lessons if lesson.enabled]
            label = (
                ", ".join(f"{lesson.start:%H:%M} {lesson.title}" for lesson in enabled)
                or "вихідний"
            )
            gaps = TelegramApplication._gap_preview(item)
            suffix = f"; {gaps}" if gaps else ""
            result.append(f"• {day:%d.%m}: {label}{suffix}")
        return tuple(result)

    @staticmethod
    def _gap_preview(day: ResolvedDay) -> str:
        labels = []
        for gap in day.gaps:
            kind = "перерва" if gap.kind.value == "BREAK" else "вільне вікно"
            labels.append(f"{kind} {gap.minutes} хв")
        return ", ".join(labels)

    @staticmethod
    def _mode_rows() -> tuple[tuple[Button, ...], ...]:
        return tuple((Button(mode.label, f"mode:{mode.value}"),) for mode in NotificationMode)

    @staticmethod
    def _region_confirmation(candidate: Region) -> View:
        return View(
            f"Підтвердьте регіон: {candidate.name}",
            ((Button("✅ Підтвердити", "region:confirm"), Button("Змінити", "region:reject")),),
        )

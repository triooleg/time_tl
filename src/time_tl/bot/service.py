from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from typing import Protocol, cast
from uuid import uuid4

from time_tl.application.schedule_editor import ScheduleEditor
from time_tl.domain.schedule import (
    ActivationProposal,
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
        return self.mode is not None and self.region_id is not None


class AccountRepository(Protocol):
    def get_or_create(self, user_id: int) -> UserAccount: ...
    def save(self, account: UserAccount) -> None: ...


class InMemoryAccountRepository:
    """Test/local adapter. Production wiring must replace it with PostgreSQL."""

    def __init__(self) -> None:
        self.accounts: dict[int, UserAccount] = {}

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
    ) -> None:
        self.regions = tuple(regions)
        self._region_by_id = {item.region_id: item for item in regions}
        self.resolver = resolver
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
        self._pending_regions: dict[int, Region] = {}
        self._editors: dict[tuple[int, str], ScheduleEditor] = {}
        self._activations: dict[tuple[int, str], tuple[ProfileCollection, ActivationProposal]] = {}
        self._imports: dict[tuple[int, str], tuple[ProfileCollection, ImportProposal]] = {}
        self._replacements: dict[
            tuple[int, str], tuple[ProfileCollection, ReplacementProposal]
        ] = {}

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
        return View("Оберіть режим сповіщень:", self._mode_rows())

    def choose_mode(self, user_id: int, mode: NotificationMode) -> View:
        account = self.repository.get_or_create(user_id)
        account.mode = mode
        self.repository.save(account)
        return self.menu(user_id) if account.region_id else self.region_prompt()

    def region_prompt(self) -> View:
        return View(
            "Вкажіть регіон. Геолокація потрібна лише один раз для підказки і не "
            "зберігається. Регіон буде записано тільки після підтвердження.",
            (
                (Button("📍 Надіслати геолокацію", request_location=True),),
                (Button("🗺 Обрати вручну", "region:list"),),
            ),
        )

    async def submit_location(self, user_id: int, latitude: float, longitude: float) -> View:
        candidate = await self.resolver.resolve(latitude, longitude)
        # Coordinates deliberately never enter an account or pending state.
        if candidate is None or candidate.region_id not in self._region_by_id:
            return View(
                "Не вдалося визначити підтримуваний регіон. Оберіть його вручну.",
                ((Button("🗺 Обрати вручну", "region:list"),),),
            )
        candidate = self._region_by_id[candidate.region_id]
        self._pending_regions[user_id] = candidate
        return self._region_confirmation(candidate)

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
        active = next((p for p in account.profiles.values() if p.active), None)
        summary = (
            f"Режим: {account.mode.label if account.mode else 'не обрано'}\n"
            f"Регіон: {account.region_name or 'не обрано'}\n"
            f"Активний розклад: {active.name if active else 'ще немає'}"
        )
        text = f"{prefix}\n\n{summary}" if prefix else summary
        return View(
            text,
            (
                (Button("🗓 Розклади", "menu:schedules"),),
                (Button("🔔 Режим сповіщень", "menu:mode"),),
                (Button("📍 Змінити регіон", "menu:region"),),
                (Button("⏱ Нагадування", "menu:reminder"),),
            ),
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
        lines = ["Ваші розклади:"]
        rows: list[tuple[Button, ...]] = [
            (Button("➕ Створити", "schedule:create"), Button("📥 Імпортувати", "schedule:import")),
        ]
        for profile in account.profiles.values():
            marker = "✅" if profile.active else "○"
            lines.append(f"{marker} {profile.name}")
            rows.append((Button(f"✏️ {profile.name}", f"schedule:edit:{profile.profile_id}"),))
            if not profile.active:
                rows.append(
                    (
                        Button(
                            f"Активувати {profile.name}", f"activate:request:{profile.profile_id}"
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
            tuple((Button(f"{value} хв", f"reminder:{value}"),) for value in values),
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
        editor = self._editors.setdefault(
            (user_id, profile_id), ScheduleEditor.from_profile(current)
        )
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
        editor = self._editors.setdefault(
            (user_id, profile_id), ScheduleEditor.from_profile(current)
        )
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

    def set_date_lessons(
        self,
        user_id: int,
        profile_id: str,
        day: date,
        lessons: Sequence[Lesson],
    ) -> ScheduleProfile:
        account = self.repository.get_or_create(user_id)
        current = account.profiles[profile_id]
        editor = self._editors.setdefault(
            (user_id, profile_id), ScheduleEditor.from_profile(current)
        )
        editor.set_date_lessons(day, lessons)
        account.profiles[profile_id] = editor.build(active=current.active)
        self.repository.save(account)
        return account.profiles[profile_id]

    def set_day_off(self, user_id: int, profile_id: str, day: date) -> ScheduleProfile:
        account = self.repository.get_or_create(user_id)
        current = account.profiles[profile_id]
        editor = self._editors.setdefault(
            (user_id, profile_id), ScheduleEditor.from_profile(current)
        )
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
        links = "так — потрібне окреме підтвердження" if proposal.links_present else "ні"
        lines = [
            f"Шаблон: {proposal.profile.name}",
            f"Джерело: {proposal.source_display_label or 'не вказано'}",
            f"Створено: {proposal.created_at or 'не вказано'}",
            f"Посилання на уроки: {links}",
            "Попередній перегляд 14 днів:",
            *self._preview_lines(proposal.resolved_preview),
        ]
        return proposal.token, View(
            "\n".join(lines),
            (
                (
                    Button("✅ Імпортувати", f"import:confirm:{proposal.token}"),
                    Button("Скасувати", "menu:schedules"),
                ),
            ),
        )

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
            result.append(f"• {day:%d.%m}: {label}")
        return tuple(result)

    @staticmethod
    def _mode_rows() -> tuple[tuple[Button, ...], ...]:
        return tuple((Button(mode.label, f"mode:{mode.value}"),) for mode in NotificationMode)

    @staticmethod
    def _region_confirmation(candidate: Region) -> View:
        return View(
            f"Підтвердьте регіон: {candidate.name}",
            ((Button("✅ Підтвердити", "region:confirm"), Button("Змінити", "region:reject")),),
        )

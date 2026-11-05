"""Cache-only power schedule views and bounded actor-owned settings previews."""

from __future__ import annotations

import secrets
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from time_tl.domain.power_outages.models import (
    GROUPS,
    LEADS,
    PowerInterval,
    PowerProvider,
    PowerPublicationStatus,
    PowerScheduleView,
    PowerState,
    PowerSubscription,
    ScheduleKind,
    SourceStatus,
    aware,
    validate_group,
    validate_lead,
)

from .models import Button, View

KYIV = ZoneInfo("Europe/Kyiv")
HOME = ((Button("🏠 Головне меню", "menu:home"),),)
BACK = ((Button("⬅️ Електроенергія", "menu:power"),),)


def _time_range(interval: PowerInterval, selected: date) -> str:
    left = interval.starts_at.astimezone(KYIV)
    right = interval.ends_at.astimezone(KYIV)
    ambiguous = left.fold == 1 or right.fold == 1 or left.utcoffset() != right.utcoffset()
    start = left.strftime("%H:%M UTC%z" if ambiguous else "%H:%M")
    end = (
        "24:00"
        if right.date() > selected and right.hour == right.minute == 0
        else right.strftime("%H:%M")
    )
    if ambiguous:
        end += right.strftime(" UTC%z")
    return f"{start}–{end}"


class PowerPort(Protocol):
    async def subscription(self, telegram_user_id: int) -> PowerSubscription | None: ...

    async def save(
        self,
        telegram_user_id: int,
        provider: PowerProvider,
        group_id: str,
        *,
        enabled: bool,
        outage_enabled: bool,
        restoration_enabled: bool,
        outage_lead: int,
        restoration_lead: int,
        expected_revision: int | None,
    ) -> PowerSubscription: ...

    async def day(
        self,
        telegram_user_id: int,
        day: date,
        kind: ScheduleKind,
    ) -> PowerScheduleView: ...


class PowerPreviewExpired(ValueError):
    """The actor-owned settings preview expired or was abandoned."""


@dataclass(frozen=True)
class _Draft:
    handle: str
    expires_at: datetime
    provider: PowerProvider
    group: str
    expected_revision: int | None
    enabled: bool = False
    outage_enabled: bool = True
    restoration_enabled: bool = True
    outage_lead: int = 10
    restoration_lead: int = 10


class PowerUx:
    """No provider or transport is accepted: only the central persistence/cache port."""

    def __init__(
        self,
        port: PowerPort,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        stale_after_seconds: int = 1800,
        capacity: int = 256,
        default_outage_lead: int = 10,
        default_restore_lead: int = 10,
        address_lookup_enabled: bool = False,
        supported_providers: tuple[PowerProvider, ...] = tuple(PowerProvider),
        notifications_available: bool = True,
        forecast_available: bool = True,
    ) -> None:
        if capacity < 1 or stale_after_seconds < 1:
            raise ValueError("invalid power UX limits")
        self.port, self.clock = port, clock
        self.stale_after_seconds, self.capacity = stale_after_seconds, capacity
        self.default_outage_lead = validate_lead(default_outage_lead)
        self.default_restore_lead = validate_lead(default_restore_lead)
        self._drafts: OrderedDict[int, _Draft] = OrderedDict()
        self._starts: OrderedDict[int, tuple[str, datetime]] = OrderedDict()
        self.address_lookup_enabled = address_lookup_enabled
        if (
            type(supported_providers) is not tuple
            or not supported_providers
            or len(set(supported_providers)) != len(supported_providers)
            or any(not isinstance(provider, PowerProvider) for provider in supported_providers)
            or type(notifications_available) is not bool
            or type(forecast_available) is not bool
        ):
            raise ValueError("invalid power UX capabilities")
        self.supported_providers = supported_providers
        self.notifications_available = notifications_available
        self.forecast_available = forecast_available

    def abandon(self, actor: int) -> None:
        self._drafts.pop(actor, None)
        self._starts.pop(actor, None)

    def _prune(self) -> None:
        now = aware(self.clock())
        for actor, value in tuple(self._drafts.items()):
            if aware(value.expires_at) <= now:
                self.abandon(actor)
        for actor, (_, deadline) in tuple(self._starts.items()):
            if deadline <= now:
                self._starts.pop(actor, None)

    def _claim(self, actor: int) -> tuple[str, datetime]:
        if type(actor) is not int or actor <= 0:
            raise PowerPreviewExpired("invalid power actor")
        self.abandon(actor)
        self._prune()
        if len(self._drafts) + len(self._starts) >= self.capacity:
            if self._drafts:
                self._drafts.popitem(last=False)
            else:
                self._starts.popitem(last=False)
        ticket = secrets.token_hex(6), aware(self.clock()) + timedelta(minutes=10)
        self._starts[actor] = ticket
        return ticket

    def _claimed(self, actor: int, ticket: tuple[str, datetime]) -> None:
        if self._starts.get(actor) != ticket or ticket[1] <= aware(self.clock()):
            raise PowerPreviewExpired("power operation abandoned")

    def _put(self, actor: int, value: _Draft) -> _Draft:
        self._prune()
        self._starts.pop(actor, None)
        if actor not in self._drafts and len(self._drafts) + len(self._starts) >= self.capacity:
            if self._drafts:
                self._drafts.popitem(last=False)
            else:
                self._starts.popitem(last=False)
        self._drafts[actor] = value
        self._drafts.move_to_end(actor)
        return value

    def _draft(self, actor: int, handle: str) -> _Draft:
        self._prune()
        value = self._drafts.get(actor)
        if value is None or not secrets.compare_digest(value.handle, handle):
            raise PowerPreviewExpired("power preview expired")
        return value

    async def _begin(self, actor: int, provider: PowerProvider | None = None) -> _Draft:
        ticket = self._claim(actor)
        current = await self.port.subscription(actor)
        self._claimed(actor, ticket)
        value = _Draft(
            secrets.token_hex(6),
            aware(self.clock()) + timedelta(minutes=10),
            provider or (current.provider if current else self.supported_providers[0]),
            current.group_id if current else GROUPS[0],
            current.revision if current else None,
            bool(current and current.enabled and self.notifications_available),
            current.outage_notifications_enabled
            if current and self.notifications_available
            else self.notifications_available,
            current.restoration_notifications_enabled
            if current and self.notifications_available
            else self.notifications_available,
            current.outage_lead_minutes if current else self.default_outage_lead,
            current.restoration_lead_minutes if current else self.default_restore_lead,
        )
        return self._put(actor, value)

    async def home(self, actor: int) -> View:
        ticket = self._claim(actor)
        current = await self.port.subscription(actor)
        self._claimed(actor, ticket)
        self._starts.pop(actor, None)
        if current is None:
            manual = self.providers()
            return (
                View(
                    manual.text,
                    ((Button("🏠 Знайти групу за адресою", "pa:start"),),) + manual.rows,
                )
                if self.address_lookup_enabled
                else manual
            )
        today = aware(self.clock()).astimezone(KYIV).date().isoformat()
        tomorrow = (date.fromisoformat(today) + timedelta(days=1)).isoformat()
        return View(
            "⚡ Електроенергія\n\n"
            f"Оператор: {current.provider.label}\nГрупа: {current.group_id}\n"
            + (
                f"🔔 Сповіщення: {'Увімкнено' if current.enabled else 'Вимкнено'}\n\n"
                if self.notifications_available
                else "🔕 Автоматичні сповіщення про електроенергію ще не підключені.\n\n"
            )
            + "Показуємо час планових відключень. "
            "Графік не підтверджує фактичну наявність світла. "
            "Коли актуального графіка немає, час відключень не вгадується.",
            (
                (Button("📅 Графік на сьогодні", f"pw:day:{today}:d:0"),),
                (Button("📆 Графік на завтра", f"pw:day:{tomorrow}:d:0"),),
                *(
                    ((Button("📋 Прогноз на тиждень", "pw:forecast"),),)
                    if self.forecast_available
                    else ()
                ),
                *(
                    ((Button("🔔 Налаштування сповіщень", "pw:settings"),),)
                    if self.notifications_available
                    else ()
                ),
                (Button("⚙️ Змінити оператора / групу", "pw:setup"),),
                *(
                    ((Button("🏠 Знайти групу за адресою", "pa:start"),),)
                    if self.address_lookup_enabled
                    else ()
                ),
                *HOME,
            ),
        )

    def providers(self) -> View:
        return View(
            "⚡ Оберіть оператора електромережі.\n"
            "Оберіть відомі вам оператор і групу або знайдіть групу за адресою. "
            "Територія повітряних тривог при цьому не зміниться.\n\n"
            "Доступність графіка перевіряється окремо; "
            "збереження групи не означає, що актуальний графік уже опубліковано."
            + (
                "\n\nПоки доступні лише перевірені групи ДТЕК і ЦЕК "
                "Дніпропетровської області. Інші регіони ще не підключені до цього меню."
                if self.supported_providers == (PowerProvider.DTEK, PowerProvider.CEK)
                else ""
            ),
            tuple((Button(p.label, f"pw:provider:{p.value}"),) for p in self.supported_providers)
            + HOME,
        )

    def forecast(self) -> View:
        if not self.forecast_available:
            return View(
                "📋 Тижневий прогноз ще не підключено. "
                "Доступні тільки опубліковані графіки на сьогодні та завтра.",
                BACK + HOME,
            )
        today = aware(self.clock()).astimezone(KYIV).date()
        days = (today + timedelta(days=n) for n in range(7))
        return View(
            "📋 Орієнтовний прогноз на тиждень\n\n"
            "Оберіть день. Прогноз не є підтвердженим добовим графіком. "
            "Якщо оператор його не опублікував, дані не вигадуються.",
            tuple((Button(day.strftime("%d.%m.%Y"), f"pw:day:{day}:f:0"),) for day in days)
            + BACK
            + HOME,
        )

    async def provider(self, actor: int, provider: PowerProvider) -> View:
        if not isinstance(provider, PowerProvider) or provider not in self.supported_providers:
            raise ValueError("power provider not available")
        draft = await self._begin(actor, provider)
        return self.groups(actor, draft.handle)

    def groups(self, actor: int, handle: str, page: int = 0) -> View:
        draft = self._draft(actor, handle)
        if type(page) is not int or page not in (0, 1):
            raise ValueError("invalid power group page")
        groups = GROUPS[page * 6 : (page + 1) * 6]
        rows = tuple(
            tuple(Button(g, f"pw:group:{handle}:{g}") for g in groups[i : i + 2])
            for i in range(0, len(groups), 2)
        )
        return View(
            f"⚡ {draft.provider.label}\nОберіть відому вам групу ({page + 1}/2).",
            rows
            + (
                (Button("◀️" if page else "▶️", f"pw:groups:{handle}:{1 - page}"),),
                (Button("Скасувати", "pw:cancel"),),
            )
            + HOME,
        )

    def group(self, actor: int, handle: str, group: str) -> View:
        draft = self._put(actor, replace(self._draft(actor, handle), group=validate_group(group)))
        if not self.notifications_available:
            return self.review(actor, handle)
        return View(
            "🔔 Увімкнути нагадування за графіком?\n"
            "Нагадаємо перед плановим відключенням і включенням. "
            f"Перед відключенням — за {draft.outage_lead} хв, "
            f"перед включенням — за {draft.restoration_lead} хв; час можна змінити. "
            "Фактичне перемикання може відрізнятися від графіка.",
            (
                (Button("✅ Так", f"pw:notify:{handle}:1"), Button("Ні", f"pw:notify:{handle}:0")),
                (Button("Скасувати", "pw:cancel"),),
            ),
        )

    def notifications(self, actor: int, handle: str, enabled: bool) -> View:
        if not self.notifications_available:
            raise ValueError("power notifications not available")
        if type(enabled) is not bool:
            raise ValueError("invalid power notification preference")
        self._put(actor, replace(self._draft(actor, handle), enabled=enabled))
        return self.leads(actor, handle, "outage") if enabled else self.review(actor, handle)

    def leads(self, actor: int, handle: str, kind: str) -> View:
        if not self.notifications_available:
            raise ValueError("power notifications not available")
        self._draft(actor, handle)
        if kind not in {"outage", "restoration"}:
            raise ValueError("invalid power lead kind")
        title = "🔴 За скільки хвилин до відключення?" if kind == "outage" else (
            "🟢 За скільки хвилин до включення за графіком?"
        )
        choices = tuple(
            (Button("У момент події" if n == 0 else f"За {n} хв", f"pw:lead:{handle}:{kind}:{n}"),)
            for n in LEADS
        )
        return View(
            title,
            choices
            + (
                (Button("✏️ Власне значення (0–180)", f"pw:custom:{handle}:{kind}"),),
                (Button("Скасувати", "pw:cancel"),),
            ),
        )

    def lead(self, actor: int, handle: str, kind: str, minutes: int) -> View:
        if not self.notifications_available:
            raise ValueError("power notifications not available")
        validate_lead(minutes)
        draft = self._draft(actor, handle)
        if kind == "outage":
            draft = replace(draft, outage_lead=minutes)
        elif kind == "restoration":
            draft = replace(draft, restoration_lead=minutes)
        else:
            raise ValueError("invalid power lead kind")
        self._put(actor, draft)
        return (
            self.leads(actor, handle, "restoration")
            if kind == "outage"
            else self.review(actor, handle)
        )

    def custom(self, actor: int, handle: str, kind: str, raw: str) -> View:
        self._draft(actor, handle)
        if not raw.isascii() or not raw.isdecimal() or len(raw) > 3:
            raise ValueError("lead must be integer 0..180")
        return self.lead(actor, handle, kind, int(raw))

    async def settings(self, actor: int) -> View:
        return self.review(actor, (await self._begin(actor)).handle)

    def review(self, actor: int, handle: str) -> View:
        value = self._draft(actor, handle)
        if not self.notifications_available:
            return View(
                "⚡ Підтвердіть налаштування графіка\n\n"
                f"{value.provider.label} · група {value.group}\n\n"
                "Показуватимемо графік для обраної групи. "
                "Автоматичні сповіщення про електроенергію ще не підключені. "
                "Налаштування повітряних тривог і уроків не зміняться. "
                "Графік не підтверджує фактичну наявність світла.",
                (
                    (Button("✅ Підтвердити", f"pw:save:{handle}"),),
                    (Button("Скасувати", "pw:cancel"),),
                ),
            )
        return View(
            "🔔 СПОВІЩЕННЯ ПРО СВІТЛО\n\n"
            f"⚡ {value.provider.label} · {value.group}\n"
            f"🔴 До відключення: {value.outage_lead} хв\n"
            f"🟢 До включення за графіком: {value.restoration_lead} хв\n"
            f"Сповіщення: {'увімкнено' if value.enabled else 'вимкнено'}\n"
            f"Відключення: {'увімкнено' if value.outage_enabled else 'вимкнено'}\n"
            f"Включення: {'увімкнено' if value.restoration_enabled else 'вимкнено'}\n\n"
            "Зміни набудуть чинності лише після підтвердження. "
            "Коли сповіщення ввімкнені, зміни графіка на сьогодні й завтра покажемо повністю. "
            "Нагадування надсилаються лише за актуальним графіком. "
            "Час орієнтовний; фактичне перемикання може відрізнятися.",
            (
                (Button("🔴 Час до відключення", f"pw:leads:{handle}:outage"),),
                (Button("🟢 Час до включення", f"pw:leads:{handle}:restoration"),),
                (Button("🔔 Увімкнути / вимкнути", f"pw:toggle:{handle}:enabled"),),
                (Button("🔴 Сповіщення відключення", f"pw:toggle:{handle}:outage_enabled"),),
                (Button("🟢 Сповіщення включення", f"pw:toggle:{handle}:restoration_enabled"),),
                (Button("✅ Підтвердити", f"pw:save:{handle}"),),
                (Button("Скасувати", "pw:cancel"),),
            ),
        )

    def toggle(self, actor: int, handle: str, field: str) -> View:
        if not self.notifications_available:
            raise ValueError("power notifications not available")
        value = self._draft(actor, handle)
        if field not in {"enabled", "outage_enabled", "restoration_enabled"}:
            raise ValueError("invalid power preference")
        if field == "enabled":
            value = replace(value, enabled=not value.enabled)
        elif field == "outage_enabled":
            value = replace(value, outage_enabled=not value.outage_enabled)
        else:
            value = replace(value, restoration_enabled=not value.restoration_enabled)
        self._put(actor, value)
        return self.review(actor, handle)

    async def confirm(self, actor: int, handle: str) -> View:
        value = self._draft(actor, handle)
        if value.provider not in self.supported_providers:
            raise ValueError("power provider not available")
        self.abandon(actor)  # A failed/conflicting write never leaves a reusable confirmation.
        await self.port.save(
            actor,
            value.provider,
            value.group,
            enabled=value.enabled and self.notifications_available,
            outage_enabled=value.outage_enabled and self.notifications_available,
            restoration_enabled=value.restoration_enabled and self.notifications_available,
            outage_lead=value.outage_lead,
            restoration_lead=value.restoration_lead,
            expected_revision=value.expected_revision,
        )
        home = await self.home(actor)
        return replace(home, text="✅ Налаштування електроенергії збережено.\n\n" + home.text)

    async def schedule(self, actor: int, selected: date, kind: ScheduleKind, page: int = 0) -> View:
        now = aware(self.clock())
        if kind is ScheduleKind.FORECAST_WEEK and not self.forecast_available:
            raise ValueError("power forecast not available")
        today = now.astimezone(KYIV).date()
        if not self.forecast_available and selected not in {today, today + timedelta(days=1)}:
            raise ValueError("power date not available")
        if type(page) is not int or not 0 <= page <= 10 or abs((selected - today).days) > 14:
            raise ValueError("invalid power schedule page")
        value = await self.port.day(actor, selected, kind)
        if not isinstance(value, PowerScheduleView):
            raise ValueError("invalid power schedule view")
        status = value.source_status
        if status is SourceStatus.FRESH and (
            value.last_success_at is None
            or not 0
            <= (now - aware(value.last_success_at)).total_seconds()
            <= self.stale_after_seconds
        ):
            status = SourceStatus.STALE
        day_label = (
            "сьогодні"
            if selected == today
            else "завтра"
            if selected == today + timedelta(days=1)
            else selected.strftime("%d.%m.%Y")
        )
        heading = (
            "📋 Орієнтовний тижневий прогноз"
            if kind is ScheduleKind.FORECAST_WEEK
            else f"⚡ Графік на {day_label}"
        )
        lines = [
            heading,
            f"{selected:%d.%m.%Y} · група {value.group_id}\n{value.provider.label}",
        ]
        schedule = value.schedule
        if schedule is not None and (
            schedule.local_date != selected
            or schedule.schedule_type is not kind
            or schedule.provider is not value.provider
            or schedule.group_id != value.group_id
        ):
            raise ValueError("power cache scope mismatch")
        display: tuple[PowerInterval, ...] = ()
        if schedule is None and value.error_code == "MAP_GROUP_PENDING":
            lines.append(
                "⏳ Графік для цієї групи ще завантажується. Спробуйте оновити трохи пізніше."
            )
        elif status is not SourceStatus.FRESH or schedule is None:
            lines.append("⚠️ Актуальний графік поки недоступний.")
        elif schedule.publication_status is PowerPublicationStatus.EMERGENCY:
            lines.append(
                "🚨 Діють аварійні відключення.\n"
                "Точний час відключень і відновлення наразі невідомий."
            )
        elif (
            schedule.publication_status is not PowerPublicationStatus.PLANNED
            or not schedule.intervals
        ):
            lines.append("⚠️ Актуальний графік поки недоступний.")
        else:
            display = tuple(
                interval
                for interval in schedule.intervals
                if interval.state
                in {
                    PowerState.OUTAGE_CONFIRMED,
                    PowerState.POSSIBLE_OUTAGE,
                }
            )
            all_expected = all(
                interval.state is PowerState.POWER_EXPECTED for interval in schedule.intervals
            )
            if display:
                selected_intervals = display[page * 20 : (page + 1) * 20]
                for state, title in (
                    (PowerState.OUTAGE_CONFIRMED, "🔴 Планові відключення:"),
                    (PowerState.POSSIBLE_OUTAGE, "🟡 Можливі відключення:"),
                ):
                    intervals = [
                        _time_range(interval, selected)
                        for interval in selected_intervals
                        if interval.state is state
                    ]
                    if intervals:
                        lines.append(title + "\n" + "\n".join(intervals))
                if any(
                    interval.state in {PowerState.UNKNOWN, PowerState.NO_SCHEDULE}
                    for interval in schedule.intervals
                ):
                    lines.append("⚠️ Частину графіка ще не підтверджено.")
                lines.append("Час може змінюватися. Графік не гарантує фактичної наявності світла.")
            elif all_expected:
                label = (
                    "Сьогодні"
                    if selected == today
                    else "Завтра"
                    if selected == today + timedelta(days=1)
                    else "На цю дату"
                )
                lines.append(f"{label} планових відключень немає.")
                lines.append("Аварійні відключення можуть відбуватися поза графіком.")
            else:
                lines.append("⚠️ Актуальний графік поки недоступний.")
        if page and page * 20 >= len(display):
            raise ValueError("power interval page expired")
        key = "f" if kind is ScheduleKind.FORECAST_WEEK else "d"
        rows: list[tuple[Button, ...]] = [
            (Button("🔄 Оновити", f"pw:day:{selected}:{key}:{page}"),),
            (
                Button("◀️ Попередній день", f"pw:day:{selected - timedelta(days=1)}:{key}:0"),
                Button("▶️ Наступний день", f"pw:day:{selected + timedelta(days=1)}:{key}:0"),
            )
            if self.forecast_available
            else (
                Button("📅 Сьогодні", f"pw:day:{today}:d:0"),
                Button("📆 Завтра", f"pw:day:{today + timedelta(days=1)}:d:0"),
            ),
        ]
        if len(display) > 20:
            navigation = []
            if page:
                navigation.append(Button("◀️ Інтервали", f"pw:day:{selected}:{key}:{page - 1}"))
            if (page + 1) * 20 < len(display):
                navigation.append(Button("Інтервали ▶️", f"pw:day:{selected}:{key}:{page + 1}"))
            if navigation:
                rows.append(tuple(navigation))
        return View("\n\n".join(lines), tuple(rows) + BACK + HOME)


def unavailable_view() -> View:
    return View(
        "⚠️ Розділ електроенергії тимчасово недоступний або дія застаріла. "
        "Зміни не підтверджено. Відкрийте розділ знову.",
        BACK + HOME,
    )

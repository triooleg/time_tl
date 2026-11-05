"""Explicit calendar management behind a default-off router.

Invitation secrets live only in bounded repr-hidden process memory. Only opaque
handles enter callbacks/FSM; restart or expiry invalidates access, never joins.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from functools import wraps
from typing import Any, Protocol
from uuid import UUID, uuid4

from time_tl.domain.calendar_memberships import (
    MAX_CALENDAR_PAYLOAD_BYTES,
    MAX_PERSISTENT_CALENDARS,
    CalendarAccessDenied,
    CalendarConflict,
    CalendarDataUnavailable,
    CalendarLimitExceeded,
    CalendarRole,
    CalendarSummary,
    InvitationPreview,
    deserialize_profile,
    may_edit,
    serialize_profile,
)
from time_tl.domain.calendars import CalendarKind
from time_tl.domain.schedule import ScheduleProfile

from .calendar_editing import CalendarInputError, calendar_name, weekly_rules, weekly_text
from .models import Button, View
from .service import UserAccount

_TTL = timedelta(minutes=10)
_GLOBAL_LIMIT = 1_024
_PER_USER_LIMIT = 3
_OPERATION_TIMEOUT_SECONDS = 5.0
_MAX_EXPORT_BYTES = MAX_PERSISTENT_CALENDARS * MAX_CALENDAR_PAYLOAD_BYTES
_ROLE = {
    CalendarRole.OWNER: "власник",
    CalendarRole.ADMIN: "адміністратор",
    CalendarRole.EDITOR: "редактор",
    CalendarRole.MEMBER: "учасник",
}


class ExistingCalendarAccounts(Protocol):
    async def get_existing(self, user_id: int) -> UserAccount | None: ...


class CalendarManagementPort(Protocol):
    async def list_calendars(self, actor: int) -> tuple[CalendarSummary, ...]: ...

    async def export_calendars(self, actor: int) -> tuple[dict[str, object], ...]: ...

    async def get_snapshot(
        self, actor: int, calendar_id: UUID
    ) -> tuple[CalendarSummary, ScheduleProfile]: ...

    async def create_calendar(
        self, actor: int, profile: ScheduleProfile, kind: CalendarKind
    ) -> CalendarSummary: ...

    async def update_calendar(
        self, actor: int, calendar_id: UUID, expected_version: int, profile: ScheduleProfile
    ) -> CalendarSummary: ...

    async def set_visible(
        self, actor: int, calendar_id: UUID, expected_version: int, visible: bool
    ) -> CalendarSummary: ...

    async def delete_calendar(
        self, actor: int, calendar_id: UUID, expected_version: int
    ) -> None: ...

    async def leave_calendar(
        self, actor: int, calendar_id: UUID, expected_version: int
    ) -> None: ...

    async def create_invitation(
        self, actor: int, calendar_id: UUID, target: int, expected_version: int, now: datetime
    ) -> str: ...

    async def preview_invitation(
        self, target: int, token: str, now: datetime
    ) -> InvitationPreview: ...

    async def accept_invitation(
        self, target: int, token: str, expected_version: int, now: datetime
    ) -> CalendarSummary: ...

    async def revoke_invitation(self, actor: int, token: str, expected_version: int) -> None: ...


@dataclass(frozen=True)
class _Pending:
    action: str
    actor: int = field(repr=False)
    expires_at: datetime
    calendar_id: UUID | None = None
    version: int = 0
    profile: ScheduleProfile | None = field(default=None, repr=False)
    fingerprint: str = field(default="", repr=False)
    target: int | None = field(default=None, repr=False)
    invitation: UUID | None = None


@dataclass(frozen=True)
class _Invitation:
    issuer: int = field(repr=False)
    target: int = field(repr=False)
    token: str = field(repr=False)
    calendar_id: UUID
    expires_at: datetime


@dataclass(frozen=True)
class _Reservation:
    issuer: int = field(repr=False)
    target: int = field(repr=False)
    expires_at: datetime


def _label(value: str, limit: int = 80) -> str:
    cleaned = (
        " ".join("".join(character for character in value if character.isprintable()).split())
        .replace("&", "＆")
        .replace("<", "‹")
        .replace(">", "›")
    )
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 1] + "…"


def _callback(action: str, identity: UUID, version: int | None = None) -> str:
    value = f"cal:{action}:{identity.hex}" + (f":{version}" if version is not None else "")
    if len(value.encode("utf-8")) > 64:
        raise ValueError("calendar callback exceeds limit")
    return value


def _parse(raw: str, *, versioned: bool = False) -> tuple[UUID, int]:
    parts = raw.split(":")
    if len(parts) != (4 if versioned else 3):
        raise CalendarDataUnavailable()
    identifier = parts[2]
    if len(identifier) != 32 or any(
        character not in "0123456789abcdef" for character in identifier
    ):
        raise CalendarDataUnavailable()
    version = 0
    if versioned:
        if not parts[3].isascii() or not parts[3].isdigit() or not 1 <= len(parts[3]) <= 10:
            raise CalendarDataUnavailable()
        version = int(parts[3])
        if not 0 < version < 2**31 - 1:
            raise CalendarDataUnavailable()
    return UUID(hex=identifier), version


def _error_view() -> View:
    return View(
        "Дія недоступна або підтвердження застаріло. Оновіть список. "
        "Якщо ви вже підтвердили дію, вона могла виконатися — спершу перевірте результат.",
        ((Button("📚 Мої календарі", "menu:calendars"),), (Button("🏠 Меню", "menu:home"),)),
    )


def _bounded[**P, R](operation: Callable[P, Awaitable[R]]) -> Callable[P, Coroutine[Any, Any, R]]:
    @wraps(operation)
    async def execute(*args: P.args, **kwargs: P.kwargs) -> R:
        async with asyncio.timeout(_OPERATION_TIMEOUT_SECONDS):
            return await operation(*args, **kwargs)

    return execute


class CalendarUx:
    def __init__(
        self,
        port: CalendarManagementPort,
        accounts: ExistingCalendarAccounts,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        personal_changes_enabled: bool = False,
        personal_export: Callable[[int], Awaitable[str]] | None = None,
    ) -> None:
        self.port = port
        self.accounts = accounts
        self.now = now
        self.personal_changes_enabled = personal_changes_enabled
        self.personal_export = personal_export if personal_changes_enabled else None
        self._pending: dict[UUID, _Pending] = {}
        self._invitations: dict[UUID, _Invitation] = {}
        self._reserved: dict[UUID, _Reservation] = {}

    def _now(self) -> datetime:
        instant = self.now()
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise CalendarDataUnavailable()
        return instant.astimezone(UTC)

    def _prune(self) -> None:
        instant = self._now()
        for identity, pending in tuple(self._pending.items()):
            if instant >= pending.expires_at:
                del self._pending[identity]
        for identity, invitation in tuple(self._invitations.items()):
            if instant >= invitation.expires_at:
                del self._invitations[identity]

    def _capacity(self, actor: int, *, invitation: bool = False) -> None:
        self._prune()
        if len(self._pending) + len(self._invitations) + len(self._reserved) >= _GLOBAL_LIMIT:
            raise CalendarLimitExceeded()
        own = sum(item.actor == actor for item in self._pending.values())
        if invitation:
            own += sum(item.issuer == actor for item in self._invitations.values())
            own += sum(item.issuer == actor for item in self._reserved.values())
        if own >= _PER_USER_LIMIT:
            raise CalendarLimitExceeded()

    def _proposal(self, pending: _Pending) -> UUID:
        self._capacity(pending.actor)
        identity = uuid4()
        self._pending[identity] = pending
        return identity

    def _peek(self, actor: int, identity: UUID) -> _Pending:
        self._prune()
        item = self._pending.get(identity)
        if item is None or item.actor != actor:
            raise CalendarDataUnavailable()
        return item

    async def _account(self, actor: int) -> UserAccount:
        if type(actor) is not int or not 0 < actor <= 2**63 - 1:
            raise CalendarAccessDenied()
        item = await self.accounts.get_existing(actor)
        if item is None or item.user_id != actor:
            raise CalendarDataUnavailable()
        return item

    async def _summaries(self, actor: int) -> tuple[CalendarSummary, ...]:
        await self._account(actor)
        items = await self.port.list_calendars(actor)
        if len(items) > MAX_PERSISTENT_CALENDARS:
            raise CalendarLimitExceeded()
        if len({item.calendar_id for item in items}) != len(items):
            raise CalendarDataUnavailable()
        return items

    async def _summary(self, actor: int, identity: UUID, version: int) -> CalendarSummary:
        item = next(
            (item for item in await self._summaries(actor) if item.calendar_id == identity), None
        )
        if item is None:
            raise CalendarAccessDenied()
        if item.version != version:
            raise CalendarConflict()
        return item

    @_bounded
    async def listing(self, actor: int) -> View:
        items = await self._summaries(actor)
        self._prune()
        rows = [
            (
                Button(
                    ("✅ " if item.visible else "▫️ ") + _label(item.name, 35),
                    _callback("d", item.calendar_id, item.version),
                ),
            )
            for item in items
        ]
        invitations = [
            (identity, item) for identity, item in self._invitations.items() if item.target == actor
        ]
        if len(invitations) > _PER_USER_LIMIT:
            raise CalendarLimitExceeded()
        rows.extend(
            (Button("📩 Переглянути запрошення", _callback("ip", identity)),)
            for identity, _ in invitations
        )
        rows.extend(
            [
                (Button("➕ Створити календар", "cal:blank"),),
                (Button("➕ Створити з активного розкладу", "cal:new"),),
                (Button("📤 Мої календарі — JSON", "cal:export"),),
                (Button("🕐 Мій день", "menu:myday"),),
                (Button("🏠 Меню", "menu:home"),),
            ]
        )
        return View(
            "📚 Мої календарі\n\n"
            + (
                f"Збережено: {len(items)} / {MAX_PERSISTENT_CALENDARS}."
                if items
                else "Календарів ще немає."
            )
            + "\n✅ — показано у «Мій день», ▫️ — приховано. "
            "Спільний календар оновлюється для учасників; нагадування про уроки не змінюються. "
            "Доступ до нових запрошень: до 10 хвилин або перезапуску бота; відкриття не приєднує.",
            tuple(rows),
        )

    @_bounded
    async def detail(self, actor: int, identity: UUID, version: int) -> View:
        item = await self._summary(actor, identity, version)
        summary, snapshot = await self.port.get_snapshot(actor, identity)
        if summary != item:
            raise CalendarConflict()
        lesson_count = sum(len(rule.lessons) for rule in snapshot.rules)
        rows = [
            (
                Button(
                    "🙈 Приховати" if item.visible else "👁 Показати",
                    _callback("vis", identity, version),
                ),
            ),
            (
                Button(
                    "🗑 Видалити для всіх" if item.role is CalendarRole.OWNER else "🚪 Вийти",
                    _callback("remove", identity, version),
                ),
            ),
        ]
        if item.role is CalendarRole.OWNER:
            rows.append((Button("📩 Запросити учасника", _callback("invite", identity, version)),))
            self._prune()
            rows.extend(
                (Button("Скасувати запрошення", _callback("rp", handle)),)
                for handle, invitation in self._invitations.items()
                if invitation.issuer == actor and invitation.calendar_id == identity
            )
        if may_edit(item.role):
            rows.extend(
                [
                    (Button("✏️ Назва календаря", _callback("rename", identity, version)),),
                    (Button("🗓 Дні, уроки та час", _callback("week", identity, version)),),
                ]
            )
        if self.personal_changes_enabled and item.visible:
            rows.append((Button("🗓 Мої зміни на дату", f"pcal:open:{identity.hex}"),))
        rows.append((Button("⬅️ Календарі", "menu:calendars"),))
        return View(
            f"📚 {_label(item.name)}\nРоль: {_ROLE[item.role]}.\n"
            f"Повторюваних уроків у знімку: {lesson_count}. "
            f"Дат-винятків: {len(snapshot.overrides)}.\n"
            "Посилання на уроки не копіюються. Відображення календаря не вмикає нових сповіщень.",
            tuple(rows),
        )

    @staticmethod
    def _active(account: UserAccount) -> ScheduleProfile:
        profiles = [profile for profile in account.profiles.values() if profile.active]
        if len(profiles) != 1:
            raise CalendarDataUnavailable()
        return profiles[0]

    @staticmethod
    def _fingerprint(profile: ScheduleProfile) -> str:
        data = [profile.profile_id, profile.name, serialize_profile(profile)]
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()

    def abandon_editor(self, actor: int) -> None:
        """Navigation invalidates this actor's unconfirmed editor drafts only."""
        for handle, pending in tuple(self._pending.items()):
            if pending.actor == actor and pending.action in {
                "blank_name",
                "blank_week",
                "create_blank",
                "rename",
                "week",
                "update",
            }:
                del self._pending[handle]

    @staticmethod
    def _week_prompt(handle: UUID, *, existing: str = "") -> View:
        return View(
            "Надішліть усі щотижневі уроки, по одному в рядку:\n"
            "Пн 08:30–09:15 Математика\nВт 10:00–10:45 Українська мова\n"
            "Дні: Пн, Вт, Ср, Чт, Пт, Сб, Нд (або 1–7). "
            "До 3500 символів / 70 рядків. Посилання не додавайте.\n"
            "Надішліть «-», щоб залишити тиждень порожнім. "
            "Новий список замінить усі тижневі уроки; дат-винятки збережуться."
            + (
                "\n\nПоточні уроки:\n"
                + "\n".join(_label(line, 250) for line in existing[:2800].splitlines())
                + ("\n… Попередній перегляд скорочено." if len(existing) > 2800 else "")
                if existing
                else ""
            ),
            ((Button("Скасувати", _callback("no", handle)),),),
        )

    @_bounded
    async def blank_prompt(self, actor: int) -> tuple[UUID, View]:
        if len(await self._summaries(actor)) >= MAX_PERSISTENT_CALENDARS:
            raise CalendarLimitExceeded()
        handle = self._proposal(_Pending("blank_name", actor, self._now() + _TTL))
        return handle, View(
            "Надішліть назву нового особистого календаря (до 120 символів). "
            "Він буде окремим від активного розкладу.",
            ((Button("Скасувати", _callback("no", handle)),),),
        )

    @_bounded
    async def edit_prompt(
        self, actor: int, identity: UUID, version: int, *, rename: bool
    ) -> tuple[UUID, View]:
        item = await self._summary(actor, identity, version)
        if not may_edit(item.role):
            raise CalendarAccessDenied()
        summary, profile = await self.port.get_snapshot(actor, identity)
        if summary != item:
            raise CalendarConflict()
        # Refuse complex weekly rules rather than flatten parity or validity.
        existing = "" if rename else weekly_text(profile)
        handle = self._proposal(
            _Pending(
                "rename" if rename else "week",
                actor,
                self._now() + _TTL,
                identity,
                version,
                profile=profile,
            )
        )
        if not rename:
            return handle, self._week_prompt(handle, existing=existing)
        return handle, View(
            f"Поточна назва: {_label(item.name)}.\nНадішліть нову назву (до 120 символів). "
            "Уроки та дат-винятки не зміняться.",
            ((Button("Скасувати", _callback("no", handle)),),),
        )

    @_bounded
    async def editor_input(self, actor: int, handle: UUID, text: str) -> tuple[bool, View]:
        await self._account(actor)
        pending = self._peek(actor, handle)
        if pending.action == "blank_name":
            profile = ScheduleProfile(uuid4().hex, calendar_name(text))
            self._pending[handle] = replace(pending, action="blank_week", profile=profile)
            return False, self._week_prompt(handle)
        if pending.action not in {"blank_week", "rename", "week"} or pending.profile is None:
            raise CalendarDataUnavailable()
        if pending.calendar_id is not None:
            summary = await self._summary(actor, pending.calendar_id, pending.version)
            if not may_edit(summary.role):
                raise CalendarAccessDenied()
            if self._peek(actor, handle) is not pending:
                raise CalendarConflict()
        if pending.action == "rename":
            profile = replace(pending.profile, name=calendar_name(text))
            description = "Зміниться лише назва. Уроки та дат-винятки збережуться."
        else:
            profile = replace(pending.profile, rules=weekly_rules(text), active=False)
            preview = weekly_text(profile)
            description = (
                "Щотижневі уроки:\n"
                + (
                    "Тиждень буде порожнім."
                    if preview == "-"
                    else "\n".join(_label(line, 250) for line in preview.splitlines())
                )
                + f"\nДат-винятків збережено: {len(profile.overrides)}."
            )
        self._pending[handle] = replace(
            pending,
            action="create_blank" if pending.action == "blank_week" else "update",
            profile=profile,
        )
        return True, View(
            f"📚 {_label(profile.name)}\n{description}\n\n"
            "Активний розклад та його нагадування не зміняться. "
            "Для спільного календаря зміни побачать усі учасники. Підтвердити?",
            (
                (Button("✅ Зберегти", _callback("ok", handle)),),
                (Button("Скасувати", _callback("no", handle)),),
            ),
        )

    @_bounded
    async def create_preview(self, actor: int) -> View:
        account = await self._account(actor)
        if len(await self._summaries(actor)) >= MAX_PERSISTENT_CALENDARS:
            raise CalendarLimitExceeded()
        source = self._active(account)
        snapshot = deserialize_profile(serialize_profile(source), uuid4(), source.name)
        handle = self._proposal(
            _Pending(
                "create",
                actor,
                self._now() + _TTL,
                profile=snapshot,
                fingerprint=self._fingerprint(source),
            )
        )
        return View(
            "Створити окремий особистий календар зі знімка активного розкладу "
            f"«{_label(source.name)}»?\n"
            "Посилання, територія та налаштування не копіюються. Активний розклад не зміниться. "
            "Новий календар також з'явиться у «Мій день» — уроки можуть повторитися, "
            "доки ви не приховаєте календар.",
            (
                (Button("✅ Створити цей знімок", _callback("ok", handle)),),
                (Button("Скасувати", _callback("no", handle)),),
            ),
        )

    @_bounded
    async def visibility(self, actor: int, identity: UUID, version: int) -> View:
        item = await self._summary(actor, identity, version)
        await self.port.set_visible(actor, identity, version, not item.visible)
        return await self.listing(actor)

    @_bounded
    async def removal_preview(self, actor: int, identity: UUID, version: int) -> View:
        item = await self._summary(actor, identity, version)
        owner = item.role is CalendarRole.OWNER
        handle = self._proposal(
            _Pending("delete" if owner else "leave", actor, self._now() + _TTL, identity, version)
        )
        return View(
            f"{'Видалити' if owner else 'Залишити'} календар «{_label(item.name)}»?\n"
            + (
                "Календар і всі членства/запрошення буде видалено для всіх. "
                "Відновити цю дію не можна."
                if owner
                else "Календар залишиться в інших учасників. Ваше членство буде видалено."
            ),
            (
                (
                    Button(
                        "🗑 Так, видалити для всіх" if owner else "🚪 Так, вийти",
                        _callback("ok", handle),
                    ),
                ),
                (Button("Скасувати", _callback("no", handle)),),
            ),
        )

    @_bounded
    async def invitation_target_prompt(
        self, actor: int, identity: UUID, version: int
    ) -> tuple[UUID, View]:
        item = await self._summary(actor, identity, version)
        if item.role is not CalendarRole.OWNER:
            raise CalendarAccessDenied()
        handle = self._proposal(_Pending("target", actor, self._now() + _TTL, identity, version))
        return handle, View(
            "Надішліть Telegram ID лише одного майбутнього учасника приватно. "
            "Користувач має вже відкрити бот через /start. ID не буде показано у відповіді. "
            "Запрошення не надсилається автоматично.",
            ((Button("Скасувати", _callback("no", handle)),),),
        )

    @_bounded
    async def invitation_target(self, actor: int, handle: UUID, raw_target: str) -> View:
        await self._account(actor)
        pending = self._peek(actor, handle)
        if pending.action != "target" or pending.calendar_id is None:
            raise CalendarDataUnavailable()
        if not raw_target.isascii() or not raw_target.isdigit() or not 1 <= len(raw_target) <= 19:
            raise CalendarDataUnavailable()
        target = int(raw_target)
        if target == actor:
            raise CalendarAccessDenied()
        await self._account(target)
        await self._summary(actor, pending.calendar_id, pending.version)
        self._pending[handle] = _Pending(
            "invite", actor, pending.expires_at, pending.calendar_id, pending.version, target=target
        )
        return View(
            "Створити запрошення для обраного користувача? Роль — учасник, без права редагувати. "
            "Приєднання — лише після його перегляду і підтвердження. "
            "Він зможе відкрити запрошення у «Календарі» до 10 хвилин або перезапуску бота.",
            (
                (Button("✅ Створити запрошення", _callback("ok", handle)),),
                (Button("Скасувати", _callback("no", handle)),),
            ),
        )

    def _invitation(self, actor: int, handle: UUID, *, owner: bool = False) -> _Invitation:
        self._prune()
        item = self._invitations.get(handle)
        if item is None or (item.issuer if owner else item.target) != actor:
            raise CalendarAccessDenied()
        return item

    @_bounded
    async def invitation_preview(self, actor: int, handle: UUID) -> View:
        await self._account(actor)
        item = self._invitation(actor, handle)
        preview = await self.port.preview_invitation(actor, item.token, self._now())
        if (
            preview.summary.calendar_id != item.calendar_id
            or preview.summary.role is not CalendarRole.MEMBER
        ):
            raise CalendarDataUnavailable()
        identity = self._proposal(
            _Pending(
                "accept",
                actor,
                min(item.expires_at, preview.expires_at),
                item.calendar_id,
                preview.summary.version,
                invitation=handle,
            )
        )
        return View(
            f"📩 Календар «{_label(preview.summary.name)}»\n"
            "Роль: учасник. Спільний календар оновлюється власником/редакторами; "
            "це не незалежна копія. Посилання та чужі налаштування не передаються. "
            "Нових нагадувань це не вмикає.\nПриєднатися?",
            (
                (Button("✅ Приєднатися", _callback("ok", identity)),),
                (Button("Не зараз", _callback("no", identity)),),
            ),
        )

    @_bounded
    async def revoke_preview(self, actor: int, handle: UUID) -> View:
        await self._account(actor)
        invitation = self._invitation(actor, handle, owner=True)
        summaries = await self._summaries(actor)
        item = next(
            (item for item in summaries if item.calendar_id == invitation.calendar_id), None
        )
        if item is None or item.role is not CalendarRole.OWNER:
            raise CalendarAccessDenied()
        identity = self._proposal(
            _Pending(
                "revoke",
                actor,
                min(self._now() + _TTL, invitation.expires_at),
                item.calendar_id,
                item.version,
                invitation=handle,
            )
        )
        return View(
            "Скасувати це непідтверджене запрошення?",
            (
                (Button("✅ Скасувати запрошення", _callback("ok", identity)),),
                (Button("Назад", _callback("no", identity)),),
            ),
        )

    @_bounded
    async def cancel(self, actor: int, handle: UUID) -> View:
        await self._account(actor)
        self._peek(actor, handle)
        del self._pending[handle]
        return await self.listing(actor)

    @_bounded
    async def confirm(self, actor: int, handle: UUID) -> View:
        account = await self._account(actor)
        item = self._peek(actor, handle)
        # Consume before the first awaited write: concurrent/replayed confirms do
        # not repeat creation even though that operation has no existing row CAS.
        del self._pending[handle]
        if item.action == "create":
            source = self._active(account)
            if item.profile is None or self._fingerprint(source) != item.fingerprint:
                raise CalendarConflict()
            await self.port.create_calendar(actor, item.profile, CalendarKind.PERSONAL)
        elif item.action == "create_blank":
            if item.profile is None:
                raise CalendarDataUnavailable()
            if len(await self._summaries(actor)) >= MAX_PERSISTENT_CALENDARS:
                raise CalendarLimitExceeded()
            await self.port.create_calendar(actor, item.profile, CalendarKind.PERSONAL)
        elif item.action == "update":
            if item.profile is None or item.calendar_id is None:
                raise CalendarDataUnavailable()
            summary = await self._summary(actor, item.calendar_id, item.version)
            if not may_edit(summary.role):
                raise CalendarAccessDenied()
            await self.port.update_calendar(actor, item.calendar_id, item.version, item.profile)
        elif item.action in {"delete", "leave", "invite"}:
            if item.calendar_id is None:
                raise CalendarDataUnavailable()
            summary = await self._summary(actor, item.calendar_id, item.version)
            if item.action == "delete":
                if summary.role is not CalendarRole.OWNER:
                    raise CalendarAccessDenied()
                await self.port.delete_calendar(actor, item.calendar_id, item.version)
            elif item.action == "leave":
                if summary.role is CalendarRole.OWNER:
                    raise CalendarAccessDenied()
                await self.port.leave_calendar(actor, item.calendar_id, item.version)
            else:
                if summary.role is not CalendarRole.OWNER or item.target is None:
                    raise CalendarAccessDenied()
                await self._account(item.target)
                self._capacity(actor, invitation=True)
                # Target capacity is checked before persisting the invitation.
                if (
                    sum(inv.target == item.target for inv in self._invitations.values())
                    + sum(inv.target == item.target for inv in self._reserved.values())
                    >= _PER_USER_LIMIT
                ):
                    raise CalendarLimitExceeded()
                reservation = uuid4()
                self._reserved[reservation] = _Reservation(actor, item.target, self._now() + _TTL)
                try:
                    token = await self.port.create_invitation(
                        actor, item.calendar_id, item.target, item.version, self._now()
                    )
                    if (
                        not isinstance(token, str)
                        or not 32 <= len(token) <= 96
                        or any(
                            character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
                            "0123456789_-"
                            for character in token
                        )
                    ):
                        raise CalendarDataUnavailable()
                    self._invitations[reservation] = _Invitation(
                        actor,
                        item.target,
                        token,
                        item.calendar_id,
                        self._reserved[reservation].expires_at,
                    )
                finally:
                    self._reserved.pop(reservation, None)
        elif item.action in {"accept", "revoke"}:
            if item.invitation is None:
                raise CalendarDataUnavailable()
            invitation = self._invitation(actor, item.invitation, owner=item.action == "revoke")
            if item.action == "accept":
                await self.port.accept_invitation(
                    actor, invitation.token, item.version, self._now()
                )
            else:
                await self.port.revoke_invitation(actor, invitation.token, item.version)
            del self._invitations[item.invitation]
        else:
            raise CalendarDataUnavailable()
        view = await self.listing(actor)
        return View("✅ Дію виконано.\n\n" + view.text, view.rows)

    @_bounded
    async def export(self, actor: int) -> bytes:
        summaries = {item.calendar_id: item for item in await self._summaries(actor)}
        values = await self.port.export_calendars(actor)
        if len(values) > MAX_PERSISTENT_CALENDARS or len(values) != len(summaries):
            raise CalendarDataUnavailable()
        checked = []
        seen: set[UUID] = set()
        for item in values:
            if set(item) != {
                "calendar_id",
                "name",
                "kind",
                "role",
                "visible",
                "version",
                "snapshot",
            }:
                raise CalendarDataUnavailable()
            if (
                type(item["calendar_id"]) is not str
                or type(item["kind"]) is not str
                or type(item["role"]) is not str
            ):
                raise CalendarDataUnavailable()
            identity = UUID(item["calendar_id"])
            if (
                type(item["name"]) is not str
                or type(item["visible"]) is not bool
                or type(item["version"]) is not int
            ):
                raise CalendarDataUnavailable()
            summary = CalendarSummary(
                identity,
                item["name"],
                CalendarKind(item["kind"]),
                CalendarRole(item["role"]),
                item["visible"],
                item["version"],
            )
            if identity in seen or summaries.get(identity) != summary:
                raise CalendarDataUnavailable()
            seen.add(identity)
            profile = deserialize_profile(item["snapshot"], identity, summary.name)
            checked.append({**item, "snapshot": serialize_profile(profile)})
        payload: dict[str, object] = {
            "schema_version": "time-tl-calendars/1", "calendars": checked,
        }
        if self.personal_export is not None:
            personal = await self.personal_export(actor)
            if type(personal) is not str or len(personal.encode("utf-8")) > 512_000:
                raise CalendarDataUnavailable()
            payload["personal_changes"] = json.loads(personal)
        raw = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        if len(raw) > _MAX_EXPORT_BYTES:
            raise CalendarLimitExceeded()
        return raw


def build_calendar_router(
    ux: CalendarUx, *, enabled: bool = False, router: Any | None = None
) -> Any | None:
    """Register before generic parent text handlers, or return a standalone router."""
    if not enabled:
        return None
    from aiogram import BaseMiddleware, F, Router
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.state import State, StatesGroup
    from aiogram.types import BufferedInputFile, CallbackQuery, Message

    from .aiogram_adapter import _answer, _callback_data, _callback_user_id, _edit, _message_user_id

    class CalendarFlow(StatesGroup):
        target = State()
        editor = State()

    selected: Router = router if router is not None else Router(name="time_tl_calendars")

    class EditorNavigation(BaseMiddleware):
        async def __call__(
            self, handler: Callable[..., Awaitable[Any]], event: Any, data: dict[str, Any]
        ) -> Any:
            context = data.get("state")
            slash = isinstance(event, Message) and bool(event.text and event.text.startswith("/"))
            away = isinstance(event, CallbackQuery) and not (
                (event.data or "").startswith("cal:") or event.data == "menu:calendars"
            )
            if slash or away:
                actor = event.from_user.id if event.from_user is not None else 0
                # Confirmation previews intentionally have no FSM state, but
                # leaving them must invalidate their old Save buttons too.
                ux.abandon_editor(actor)
                if context is not None and await context.get_state() == CalendarFlow.editor.state:
                    await context.clear()
                    data["raw_state"] = None
            return await handler(event, data)

    selected.message.outer_middleware(EditorNavigation())
    selected.callback_query.outer_middleware(EditorNavigation())

    def private_callback(callback: CallbackQuery) -> None:
        actor = _callback_user_id(callback)
        if (
            callback.message is None
            or callback.message.chat.type != "private"
            or callback.message.chat.id != actor
        ):
            raise CalendarAccessDenied()

    @selected.callback_query(F.data == "menu:calendars")
    async def calendars(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        try:
            private_callback(callback)
            ux.abandon_editor(_callback_user_id(callback))
            view = await ux.listing(_callback_user_id(callback))
        except Exception:
            view = _error_view()
        await _edit(callback, view)

    @selected.callback_query(F.data.startswith("cal:"))
    async def calendar_action(callback: CallbackQuery, state: FSMContext) -> None:
        try:
            private_callback(callback)
            raw = _callback_data(callback)
            actor = _callback_user_id(callback)
            await state.clear()
            if not raw.startswith(("cal:ok:", "cal:no:")):
                ux.abandon_editor(actor)
            if raw == "cal:new":
                view = await ux.create_preview(actor)
            elif raw == "cal:blank":
                handle, view = await ux.blank_prompt(actor)
                await state.update_data(calendar_handle=handle.hex)
                await state.set_state(CalendarFlow.editor)
            elif raw == "cal:export":
                if (
                    callback.message is None
                    or callback.message.chat.type != "private"
                    or callback.message.chat.id != actor
                ):
                    raise CalendarAccessDenied()
                raw_export = await ux.export(actor)
                async with asyncio.timeout(_OPERATION_TIMEOUT_SECONDS):
                    await callback.message.answer_document(
                        BufferedInputFile(raw_export, filename="time-tl-calendars.json"),
                        caption="Ваші календарі без посилань, чужих ID та запрошень.",
                    )
                view = View(
                    "📤 Ваші календарі експортовано.", ((Button("⬅️ Календарі", "menu:calendars"),),)
                )
            else:
                action = raw.split(":")[1]
                identity, version = _parse(
                    raw, versioned=action in {"d", "vis", "remove", "invite", "rename", "week"}
                )
                if action == "d":
                    view = await ux.detail(actor, identity, version)
                elif action == "vis":
                    view = await ux.visibility(actor, identity, version)
                elif action == "remove":
                    view = await ux.removal_preview(actor, identity, version)
                elif action == "invite":
                    handle, view = await ux.invitation_target_prompt(actor, identity, version)
                    await state.update_data(calendar_handle=handle.hex)
                    await state.set_state(CalendarFlow.target)
                elif action in {"rename", "week"}:
                    handle, view = await ux.edit_prompt(
                        actor, identity, version, rename=action == "rename"
                    )
                    await state.update_data(calendar_handle=handle.hex)
                    await state.set_state(CalendarFlow.editor)
                elif action == "ip":
                    view = await ux.invitation_preview(actor, identity)
                elif action == "rp":
                    view = await ux.revoke_preview(actor, identity)
                elif action == "ok":
                    view = await ux.confirm(actor, identity)
                elif action == "no":
                    view = await ux.cancel(actor, identity)
                else:
                    raise CalendarDataUnavailable()
        except Exception:
            await state.clear()
            view = _error_view()
        await _edit(callback, view)

    @selected.message(CalendarFlow.target, F.text, ~F.text.startswith("/"))
    async def calendar_target(message: Message, state: FSMContext) -> None:
        try:
            actor = _message_user_id(message)
            if message.chat.type != "private" or message.chat.id != actor:
                raise CalendarAccessDenied()
            data = await state.get_data()
            identity, _ = _parse("cal:target:" + data.get("calendar_handle", ""))
            view = await ux.invitation_target(actor, identity, message.text or "")
        except Exception:
            view = _error_view()
        await state.clear()
        await _answer(message, view)

    @selected.message(CalendarFlow.editor, ~F.text.startswith("/"))
    async def calendar_editor(message: Message, state: FSMContext) -> None:
        try:
            actor = _message_user_id(message)
            if message.chat.type != "private" or message.chat.id != actor:
                raise CalendarAccessDenied()
            data = await state.get_data()
            identity, _ = _parse("cal:editor:" + data.get("calendar_handle", ""))
            if message.text and message.text.startswith("/"):
                await ux.cancel(actor, identity)
                await state.clear()
                view = await ux.listing(actor)
            elif message.text is None:
                async with asyncio.timeout(_OPERATION_TIMEOUT_SECONDS):
                    await ux._account(actor)
                    ux._peek(actor, identity)
                view = View(
                    "Надішліть текст у вказаному форматі або скасуйте дію.",
                    ((Button("Скасувати", _callback("no", identity)),),),
                )
            else:
                complete, view = await ux.editor_input(actor, identity, message.text)
                if complete:
                    await state.clear()
        except CalendarInputError as error:
            # Only fixed parser messages are rendered; retain the opaque draft
            # and the original expiry so the user can correct invalid input.
            view = View(str(error), ((Button("Скасувати", _callback("no", identity)),),))
        except Exception:
            await state.clear()
            view = _error_view()
        await _answer(message, view)

    return selected

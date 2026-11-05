"""Transient actor-bound address lookup; persist only explicit operator/group choice."""

from __future__ import annotations

import secrets
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Protocol, TypedDict, Unpack

from time_tl.domain.power_outages.address import (
    AssignmentChoice,
    HouseChoice,
    SettlementChoice,
    StreetChoice,
)
from time_tl.domain.power_outages.location import PowerLocationSeed
from time_tl.domain.power_outages.models import PowerSubscription, aware, validate_group

from .models import Button, View
from .power_service import PowerPort

MANUAL = (
    (Button("⚙️ Обрати оператора / групу вручну", "pw:setup"),),
    (Button("⬅️ Електроенергія", "menu:power"),),
)


class PowerAddressPort(Protocol):
    async def seed(self, telegram_user_id: int) -> PowerLocationSeed | None: ...
    async def settlements(
        self, seed: PowerLocationSeed, q: str = ""
    ) -> tuple[SettlementChoice, ...]: ...
    async def streets(
        self, seed: PowerLocationSeed, settlement: SettlementChoice, q: str
    ) -> tuple[StreetChoice, ...]: ...
    async def houses(self, street: StreetChoice) -> tuple[HouseChoice, ...]: ...
    async def assignments(
        self,
        seed: PowerLocationSeed,
        settlement: SettlementChoice,
        street: StreetChoice,
        house: HouseChoice,
    ) -> tuple[AssignmentChoice, ...]: ...


class PowerAddressExpired(ValueError):
    def __init__(self) -> None:
        super().__init__("power_address_selection_expired")


def unavailable_address() -> View:
    return View(
        "⚠️ Адресний довідник недоступний або вибір застарів. "
        "Групу не визначено. Можна почати знову або обрати її вручну.",
        ((Button("Спробувати знову", "pa:start"),),) + MANUAL,
    )


Choice = SettlementChoice | StreetChoice | HouseChoice | AssignmentChoice


@dataclass(frozen=True, repr=False)
class _AddressDraft:
    handle: str
    expires_at: datetime
    seed: PowerLocationSeed
    subscription: PowerSubscription | None
    stage: str
    choices: tuple[Choice, ...] = ()
    settlement: SettlementChoice | None = None
    street: StreetChoice | None = None
    house: HouseChoice | None = None
    assignment: AssignmentChoice | None = None


class _Changes(TypedDict, total=False):
    stage: str
    choices: tuple[Choice, ...]
    settlement: SettlementChoice | None
    street: StreetChoice | None
    house: HouseChoice | None
    assignment: AssignmentChoice | None


class PowerAddressUx:
    def __init__(
        self,
        directory: PowerAddressPort,
        subscriptions: PowerPort,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        capacity: int = 256,
        default_outage_lead: int = 10,
        default_restore_lead: int = 10,
        notifications_available: bool = True,
    ) -> None:
        if type(capacity) is not int or not 1 <= capacity <= 256:
            raise ValueError("invalid address preview capacity")
        self.directory, self.subscriptions, self.clock = directory, subscriptions, clock
        self.capacity = capacity
        self.defaults = default_outage_lead, default_restore_lead
        if type(notifications_available) is not bool:
            raise ValueError("invalid power notification capability")
        self.notifications_available = notifications_available
        self._drafts: OrderedDict[int, _AddressDraft] = OrderedDict()
        self._starts: OrderedDict[int, tuple[str, datetime]] = OrderedDict()

    def abandon(self, actor: int) -> None:
        self._drafts.pop(actor, None)
        self._starts.pop(actor, None)

    def _put(self, actor: int, draft: _AddressDraft) -> _AddressDraft:
        self._starts.pop(actor, None)
        for owner, value in tuple(self._drafts.items()):
            if value.expires_at <= aware(self.clock()):
                self.abandon(owner)
        if actor not in self._drafts and len(self._drafts) + len(self._starts) >= self.capacity:
            if self._drafts:
                self._drafts.popitem(last=False)
            else:
                self._starts.popitem(last=False)
        self._drafts[actor] = draft
        self._drafts.move_to_end(actor)
        return draft

    def _get(self, actor: int, handle: str) -> _AddressDraft:
        draft = self._drafts.get(actor)
        if draft is None or draft.expires_at <= aware(self.clock()):
            self.abandon(actor)
            raise PowerAddressExpired
        if not secrets.compare_digest(draft.handle, handle):
            raise PowerAddressExpired
        return draft

    def _step(self, actor: int, draft: _AddressDraft, **changes: Unpack[_Changes]) -> _AddressDraft:
        self._get(actor, draft.handle)
        return self._put(actor, replace(draft, handle=secrets.token_hex(6), **changes))

    def prompt(self, actor: int, handle: str) -> View:
        draft = self._get(actor, handle)
        text = {
            "settlement_query": "Введіть назву населеного пункту (2–20 символів).",
            "street_query": "Введіть назву вулиці (2–80 символів).",
        }.get(draft.stage)
        if text is None:
            raise PowerAddressExpired
        return View(
            text + "\n\nАдресні дані потрібні лише для пошуку. "
            "До профілю зберігаються тільки оператор і група.",
            self._controls(draft),
        )

    @staticmethod
    def _controls(draft: _AddressDraft) -> tuple[tuple[Button, ...], ...]:
        return (
            (Button("⬅️ Почати адресу заново", f"pa:back:{draft.handle}"),),
            (Button("Скасувати", "pa:cancel"),),
        )

    def expects_text(self, actor: int, handle: str) -> bool:
        return self._get(actor, handle).stage in {"settlement_query", "street_query"}

    def handle(self, actor: int) -> str:
        value = self._drafts.get(actor)
        if value is None:
            raise PowerAddressExpired
        return self._get(actor, value.handle).handle

    async def start(self, actor: int) -> View:
        if type(actor) is not int or actor <= 0:
            raise PowerAddressExpired
        self.abandon(actor)
        ticket, deadline = secrets.token_hex(6), aware(self.clock()) + timedelta(minutes=10)
        for owner, (_, expiry) in tuple(self._starts.items()):
            if expiry <= aware(self.clock()):
                self._starts.pop(owner, None)
        if len(self._drafts) + len(self._starts) >= self.capacity:
            if self._drafts:
                self._drafts.popitem(last=False)
            else:
                self._starts.popitem(last=False)
        self._starts[actor] = ticket, deadline
        seed = await self.directory.seed(actor)
        if self._starts.get(actor) != (ticket, deadline) or deadline <= aware(self.clock()):
            raise PowerAddressExpired
        if seed is None:
            self.abandon(actor)
            return View(
                "📍 Спочатку підтвердьте свою територію в налаштуваннях. "
                "Адреса електропостачання не змінює територію повітряних тривог.",
                ((Button("📍 Моя територія", "menu:region"),),) + MANUAL,
            )
        subscription = await self.subscriptions.subscription(actor)
        if self._starts.get(actor) != (ticket, deadline) or deadline <= aware(self.clock()):
            raise PowerAddressExpired
        draft = self._put(
            actor,
            _AddressDraft(
                secrets.token_hex(6),
                aware(self.clock()) + timedelta(minutes=10),
                seed,
                subscription,
                "settlement_query",
            ),
        )
        if seed.needs_settlement:
            return self.prompt(actor, draft.handle)
        choices = await self.directory.settlements(seed, "")
        self._get(actor, draft.handle)
        if (
            len(choices) != 1
            or choices[0].katottg != seed.katottg
            or choices[0].seed_katottg != seed.katottg
            or choices[0].catalog_version != seed.catalog_version
        ):
            self.abandon(actor)
            return unavailable_address()
        return self.prompt(
            actor, self._step(actor, draft, stage="street_query", settlement=choices[0]).handle
        )

    def _choices(
        self, actor: int, draft: _AddressDraft, stage: str, choices: tuple[Choice, ...], title: str
    ) -> View:
        if type(choices) is not tuple or not 1 <= len(choices) <= 256:
            self.abandon(actor)
            return unavailable_address()
        current = self._step(actor, draft, stage=stage, choices=choices)
        return self.page(actor, current.handle, 0)

    def page(self, actor: int, handle: str, page: int) -> View:
        current = self._get(actor, handle)
        titles = {
            "settlement_pick": "Оберіть населений пункт у своїй території:",
            "street_pick": "Оберіть вулицю:",
            "house_pick": "Оберіть будинок і корпус із довідника:",
            "assignment_pick": "Оберіть оператора та групу, знайдені для адреси:",
        }
        if (
            current.stage not in titles
            or type(page) is not int
            or not 0 <= page < (len(current.choices) + 7) // 8
        ):
            raise PowerAddressExpired
        rows = tuple(
            (
                Button(
                    item.name if isinstance(item, SettlementChoice) else item.label,
                    f"pa:pick:{current.handle}:{index}",
                ),
            )
            for index, item in enumerate(current.choices)
            if page * 8 <= index < (page + 1) * 8
        )
        navigation = []
        if page:
            navigation.append(Button("◀️", f"pa:page:{current.handle}:{page - 1}"))
        if (page + 1) * 8 < len(current.choices):
            navigation.append(Button("▶️", f"pa:page:{current.handle}:{page + 1}"))
        return View(
            titles[current.stage],
            rows + ((tuple(navigation),) if navigation else ()) + self._controls(current),
        )

    async def back(self, actor: int, handle: str) -> View:
        self._get(actor, handle)
        return await self.start(actor)

    async def search(self, actor: int, handle: str, query: str) -> View:
        draft = self._get(actor, handle)
        bound = 20 if draft.stage == "settlement_query" else 80
        if draft.stage not in {"settlement_query", "street_query"}:
            raise PowerAddressExpired
        if (
            type(query) is not str
            or not 2 <= len(query.strip()) <= bound
            or any(ord(c) < 32 for c in query)
        ):
            raise ValueError("invalid_address_query")
        current = self._step(actor, draft, stage="busy", choices=())
        if draft.stage == "settlement_query":
            settlements = await self.directory.settlements(draft.seed, query.strip())
            if any(
                item.seed_katottg != draft.seed.katottg
                or item.catalog_version != draft.seed.catalog_version
                for item in settlements
            ):
                raise PowerAddressExpired
            return self._choices(
                actor,
                current,
                "settlement_pick",
                settlements,
                "Оберіть населений пункт у своїй території:",
            )
        if draft.settlement is None:
            raise PowerAddressExpired
        streets = await self.directory.streets(draft.seed, draft.settlement, query.strip())
        if any(item.settlement != draft.settlement for item in streets):
            raise PowerAddressExpired
        return self._choices(actor, current, "street_pick", streets, "Оберіть вулицю:")

    async def pick(self, actor: int, handle: str, index: int) -> View:
        draft = self._get(actor, handle)
        if type(index) is not int or not 0 <= index < len(draft.choices):
            raise PowerAddressExpired
        selected = draft.choices[index]
        if draft.stage == "settlement_pick" and isinstance(selected, SettlementChoice):
            current = self._step(
                actor, draft, stage="street_query", choices=(), settlement=selected
            )
            return self.prompt(actor, current.handle)
        if draft.stage == "street_pick" and isinstance(selected, StreetChoice):
            if selected.settlement != draft.settlement:
                raise PowerAddressExpired
            current = self._step(actor, draft, stage="busy", street=selected, choices=())
            houses = await self.directory.houses(selected)
            if any(item.street != selected for item in houses):
                raise PowerAddressExpired
            return self._choices(
                actor, current, "house_pick", houses, "Оберіть будинок і корпус із довідника:"
            )
        if draft.stage == "house_pick" and isinstance(selected, HouseChoice):
            if selected.street != draft.street or draft.street is None or draft.settlement is None:
                raise PowerAddressExpired
            current = self._step(actor, draft, stage="busy", house=selected, choices=())
            candidates = await self.directory.assignments(
                draft.seed, draft.settlement, draft.street, selected
            )
            for candidate in candidates:
                validate_group(candidate.group_id)
            return self._choices(
                actor,
                current,
                "assignment_pick",
                candidates,
                "Оберіть оператора та групу, знайдені для адреси:",
            )
        if draft.stage == "assignment_pick" and isinstance(selected, AssignmentChoice):
            validate_group(selected.group_id)
            current = self._step(actor, draft, stage="confirm", choices=(), assignment=selected)
            return View(
                "Підтвердіть зміну електропостачання:\n"
                f"{selected.provider.label}\nГрупа: {selected.group_id}\n\n"
                "Адреса використовується лише для пошуку й не зберігається. "
                + (
                    "Налаштування сповіщень залишаться поточними. "
                    if self.notifications_available
                    else "Автоматичні сповіщення про електроенергію ще не підключені. "
                    "Налаштування повітряних тривог і уроків не зміняться. "
                )
                + "Це не підтверджує фактичну наявність світла.",
                ((Button("✅ Підтвердити оператора / групу", f"pa:save:{current.handle}"),),)
                + self._controls(current),
            )
        raise PowerAddressExpired

    async def confirm(self, actor: int, handle: str) -> View:
        draft = self._get(actor, handle)
        if draft.stage != "confirm" or draft.assignment is None:
            raise PowerAddressExpired
        # Keep the actor-owned ticket cancellable through the last read-only await.
        # Cancellation or a replacement wizard invalidates this exact handle.
        current_seed = await self.directory.seed(actor)
        self._get(actor, handle)
        if current_seed != draft.seed:
            self.abandon(actor)
            raise PowerAddressExpired
        # Consumption is the commit boundary: from here the CAS transaction may
        # already be executing. A repeated confirmation cannot initiate a second save.
        self.abandon(actor)
        old, choice = draft.subscription, draft.assignment
        await self.subscriptions.save(
            actor,
            choice.provider,
            choice.group_id,
            enabled=bool(old and old.enabled and self.notifications_available),
            outage_enabled=old.outage_notifications_enabled
            if old and self.notifications_available
            else self.notifications_available,
            restoration_enabled=old.restoration_notifications_enabled
            if old and self.notifications_available
            else self.notifications_available,
            outage_lead=old.outage_lead_minutes if old else self.defaults[0],
            restoration_lead=old.restoration_lead_minutes if old else self.defaults[1],
            expected_revision=old.revision if old else None,
        )
        return View("✅ Оператор і група збережені. Адресу видалено з тимчасового вибору.", MANUAL)

"""Offline actor isolation, confirmation and address-only FSM coverage."""

import asyncio
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot, Dispatcher, F, Router
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from tests.bot.test_power_ux import NOW, FakePowerPort
from time_tl.bot.async_service import AsyncTelegramApplication
from time_tl.bot.power_address_router import build_power_address_router
from time_tl.bot.power_address_service import PowerAddressExpired, PowerAddressUx
from time_tl.bot.safety import BoundedMemoryStorage
from time_tl.domain.power_outages.address import (
    AssignmentChoice,
    HouseChoice,
    SettlementChoice,
    StreetChoice,
)
from time_tl.domain.power_outages.location import PowerLocationSeed
from time_tl.domain.power_outages.models import PowerProvider, PowerSubscription

SEED = PowerLocationSeed("UA12010000000000000", "HROMADA", "Тестова громада", "catalog-1")
PRIVATE_STREET = "Синтетична приватна вулиця"
PRIVATE_HOUSE = "999 корпус тест"


class Directory:
    def __init__(self, seed=SEED):
        self.location = seed
        self.calls = []
        self.empty = False
        self.wrong = False

    async def seed(self, actor):
        self.calls.append(("seed", actor))
        return self.location

    async def settlements(self, seed, q=""):
        self.calls.append(("settlements", q))
        if self.empty:
            return ()
        return (
            SettlementChoice(
                seed.katottg if not seed.needs_settlement else "UA12010010000000000",
                "Тестове місто",
                seed.katottg,
                seed.catalog_version,
                "UA12000000000090473",
            ),
        )

    async def streets(self, seed, settlement, q):
        self.calls.append(("streets", q))
        if self.wrong:
            settlement = replace(settlement, katottg="UA12010020000000000")
        return (StreetChoice("a" * 24, PRIVATE_STREET, settlement),)

    async def houses(self, street):
        self.calls.append(("houses",))
        return (HouseChoice("synthetic-value", "999", "тест", PRIVATE_HOUSE, street),)

    async def assignments(self, seed, settlement, street, house):
        self.calls.append(("assignments",))
        return (AssignmentChoice("synthetic-assignment", PowerProvider.CEK, "3.2", "ЦЕК · 3.2"),)


def callbacks(view):
    return [button.callback_data for row in view.rows for button in row if button.callback_data]


async def preview(ux, actor=1):
    views = [await ux.start(actor)]
    if ux._drafts[actor].seed.needs_settlement:
        views += [
            await ux.search(actor, ux.handle(actor), "Тест"),
            await ux.pick(actor, ux.handle(actor), 0),
        ]
    views += [
        await ux.search(actor, ux.handle(actor), "Синтетична"),
        await ux.pick(actor, ux.handle(actor), 0),
        await ux.pick(actor, ux.handle(actor), 0),
        await ux.pick(actor, ux.handle(actor), 0),
    ]
    return views


async def test_explicit_confirmation_preserves_preferences_and_address_privacy():
    port, directory = FakePowerPort(), Directory()
    old = PowerSubscription("1", PowerProvider.DTEK, "1.1", True, False, True, 47, 22, 9)
    port.subscriptions[1] = old
    ux = PowerAddressUx(directory, port, clock=lambda: NOW)
    views = await preview(ux)
    assert not port.saved
    assert PRIVATE_STREET not in repr(ux._drafts) and PRIVATE_HOUSE not in repr(ux._drafts)
    for view in views:
        for data in callbacks(view):
            assert len(data.encode()) <= 64
            assert PRIVATE_STREET not in data and PRIVATE_HOUSE not in data
            assert "UA120" not in data and "synthetic-value" not in data
    handle = ux.handle(1)
    await ux.confirm(1, handle)
    result = port.subscriptions[1]
    assert (result.provider, result.group_id) == (PowerProvider.CEK, "3.2")
    assert (
        result.enabled,
        result.outage_notifications_enabled,
        result.restoration_notifications_enabled,
        result.outage_lead_minutes,
        result.restoration_lead_minutes,
    ) == (True, False, True, 47, 22)
    assert 1 not in ux._drafts
    assert set(result.__dict__) == set(old.__dict__)
    with pytest.raises(PowerAddressExpired):
        await ux.confirm(1, handle)


async def test_cache_only_address_confirmation_disables_power_delivery_and_preserves_leads():
    port = FakePowerPort()
    port.subscriptions[1] = PowerSubscription(
        "1", PowerProvider.DTEK, "1.1", True, True, True, 47, 22, 9
    )
    ux = PowerAddressUx(Directory(), port, clock=lambda: NOW, notifications_available=False)
    views = await preview(ux)
    assert "ще не підключені" in views[-1].text
    assert "повітряних тривог і уроків не зміняться" in views[-1].text
    assert not port.saved
    await ux.confirm(1, ux.handle(1))
    result = port.subscriptions[1]
    assert not result.enabled
    assert not result.outage_notifications_enabled
    assert not result.restoration_notifications_enabled
    assert (result.outage_lead_minutes, result.restoration_lead_minutes) == (47, 22)
    assert (result.provider, result.group_id) == (PowerProvider.CEK, "3.2")


async def test_special_city_uses_exact_seed_issued_settlement_without_search_step():
    seed = replace(SEED, territory_kind="SPECIAL_STATUS_CITY")
    directory, port = Directory(seed), FakePowerPort()
    ux = PowerAddressUx(directory, port, clock=lambda: NOW)
    view = await ux.start(1)
    assert "назву вулиці" in view.text
    assert directory.calls == [("seed", 1), ("settlements", "")]
    assert ux._drafts[1].settlement.katottg == seed.katottg


async def test_unseeded_or_unavailable_directory_has_explicit_manual_fallback():
    directory, port = Directory(None), FakePowerPort()
    ux = PowerAddressUx(directory, port, clock=lambda: NOW)
    view = await ux.start(1)
    assert "menu:region" in callbacks(view) and "pw:setup" in callbacks(view)
    assert not ux._drafts and not port.saved
    directory.location, directory.empty = SEED, True
    await ux.start(1)
    missing = await ux.search(1, ux.handle(1), "Тест")
    assert "Групу не визначено" in missing.text and "pw:setup" in callbacks(missing)
    assert not ux._drafts and not port.saved


async def test_actor_expiry_replay_wrong_chain_and_subscription_cas_fail_closed():
    clock = [NOW]
    directory, port = Directory(), FakePowerPort()
    ux = PowerAddressUx(directory, port, clock=lambda: clock[0])
    await ux.start(1)
    old_handle = ux.handle(1)
    with pytest.raises(PowerAddressExpired):
        await ux.search(2, old_handle, "Тест")
    await ux.search(1, old_handle, "Тест")
    with pytest.raises(PowerAddressExpired):
        await ux.pick(1, old_handle, 0)
    with pytest.raises(PowerAddressExpired):
        await ux.pick(1, ux.handle(1), -1)
    await ux.pick(1, ux.handle(1), 0)
    directory.wrong = True
    with pytest.raises(PowerAddressExpired):
        await ux.search(1, ux.handle(1), "Тестова")
    directory.wrong = False
    await preview(ux)
    handle = ux.handle(1)
    port.subscriptions[1] = PowerSubscription("1", PowerProvider.DTEK, "1.1", revision=2)
    with pytest.raises(ValueError):
        await ux.confirm(1, handle)
    assert not ux._drafts and not port.saved
    await ux.start(1)
    handle = ux.handle(1)
    clock[0] += timedelta(minutes=10)
    with pytest.raises(PowerAddressExpired):
        await ux.search(1, handle, "Тест")


async def test_back_and_seed_change_never_save_and_capacity_is_bounded():
    directory, port = Directory(), FakePowerPort()
    ux = PowerAddressUx(directory, port, clock=lambda: NOW, capacity=2)
    await preview(ux)
    old = ux.handle(1)
    await ux.back(1, old)
    assert ux.expects_text(1, ux.handle(1)) and ux._drafts[1].street is None
    with pytest.raises(PowerAddressExpired):
        await ux.pick(1, old, 0)
    await preview(ux)
    directory.location = replace(SEED, catalog_version="changed")
    with pytest.raises(PowerAddressExpired):
        await ux.confirm(1, ux.handle(1))
    for actor in (1, 2, 3):
        await ux.start(actor)
    assert len(ux._drafts) == 2 and 1 not in ux._drafts
    assert not port.saved


async def test_cancel_during_await_cannot_resurrect_transient_address_draft():
    entered, finish = asyncio.Event(), asyncio.Event()

    class Waiting(Directory):
        async def seed(self, actor):
            entered.set()
            await finish.wait()
            return SEED

    ux = PowerAddressUx(Waiting(), FakePowerPort(), clock=lambda: NOW)
    running = asyncio.create_task(ux.start(1))
    await entered.wait()
    ux.abandon(1)
    finish.set()
    with pytest.raises(PowerAddressExpired):
        await running
    assert not ux._drafts and not ux._starts


async def test_pagination_covers_only_returned_house_choices_and_rejects_invalid_page():
    class ManyHouses(Directory):
        async def houses(self, street):
            return tuple(HouseChoice(str(i), str(i), "", f"Будинок {i}", street) for i in range(25))

    ux = PowerAddressUx(ManyHouses(), FakePowerPort(), clock=lambda: NOW)
    await ux.start(1)
    await ux.search(1, ux.handle(1), "Тест")
    await ux.pick(1, ux.handle(1), 0)
    await ux.search(1, ux.handle(1), "Вулиця")
    await ux.pick(1, ux.handle(1), 0)
    handle = ux.handle(1)
    seen = []
    for page in range(4):
        view = ux.page(1, handle, page)
        selected = [data for data in callbacks(view) if data.startswith("pa:pick:")]
        assert len(selected) <= 8
        seen.extend(int(data.rsplit(":", 1)[-1]) for data in selected)
    assert seen == list(range(25))
    with pytest.raises(PowerAddressExpired):
        ux.page(1, handle, 4)
    await ux.pick(1, handle, 24)
    assert ux._drafts[1].house.number == "24"


async def test_cancel_during_street_query_cannot_retain_late_address_response():
    entered, finish = asyncio.Event(), asyncio.Event()

    class WaitingStreet(Directory):
        async def streets(self, seed, settlement, q):
            entered.set()
            await finish.wait()
            return await super().streets(seed, settlement, q)

    ux = PowerAddressUx(WaitingStreet(), FakePowerPort(), clock=lambda: NOW)
    await ux.start(1)
    await ux.search(1, ux.handle(1), "Тест")
    await ux.pick(1, ux.handle(1), 0)
    running = asyncio.create_task(ux.search(1, ux.handle(1), "Вулиця"))
    await entered.wait()
    ux.abandon(1)
    finish.set()
    with pytest.raises(PowerAddressExpired):
        await running
    assert not ux._drafts and not ux._starts


@pytest.mark.parametrize("replacement", [False, True])
async def test_confirm_cancel_or_replacement_during_seed_read_never_saves(replacement):
    entered, finish = asyncio.Event(), asyncio.Event()

    class WaitingConfirmation(Directory):
        block_next_seed = False

        async def seed(self, actor):
            if self.block_next_seed:
                self.block_next_seed = False
                entered.set()
                await finish.wait()
            return self.location

    directory, port = WaitingConfirmation(), FakePowerPort()
    ux = PowerAddressUx(directory, port, clock=lambda: NOW)
    await preview(ux)
    handle = ux.handle(1)
    directory.block_next_seed = True
    pending = asyncio.create_task(ux.confirm(1, handle))
    await entered.wait()
    if replacement:
        await ux.start(1)
        replacement_handle = ux.handle(1)
        assert replacement_handle != handle
    else:
        ux.abandon(1)
    finish.set()
    with pytest.raises(PowerAddressExpired):
        await pending
    assert not port.saved
    if replacement:
        assert ux.handle(1) == replacement_handle
    else:
        assert not ux._drafts


async def test_concurrent_confirmations_consume_ticket_before_exactly_one_cas_save():
    entered, finish = asyncio.Event(), asyncio.Event()

    class WaitingSave(FakePowerPort):
        async def save(self, *args, **kwargs):
            entered.set()
            await finish.wait()
            return await super().save(*args, **kwargs)

    directory, port = Directory(), WaitingSave()
    ux = PowerAddressUx(directory, port, clock=lambda: NOW)
    await preview(ux)
    handle = ux.handle(1)
    first = asyncio.create_task(ux.confirm(1, handle))
    await entered.wait()
    with pytest.raises(PowerAddressExpired):
        await ux.confirm(1, handle)
    finish.set()
    await first
    assert len(port.saved) == 1
    with pytest.raises(PowerAddressExpired):
        await ux.confirm(1, handle)


@pytest.mark.parametrize("next_action", ["pa:start", "pa:cancel", "menu:home"])
async def test_real_dispatcher_late_confirm_never_erases_new_flow_or_overwrites_ui(
    monkeypatch, next_action
):
    from time_tl.bot import aiogram_adapter

    entered, finish = asyncio.Event(), asyncio.Event()

    class WaitingConfirmation(Directory):
        block_next_seed = False

        async def seed(self, actor):
            if self.block_next_seed:
                self.block_next_seed = False
                entered.set()
                await finish.wait()
            return self.location

    directory, port = WaitingConfirmation(), FakePowerPort()
    ux = PowerAddressUx(directory, port, clock=lambda: NOW)
    await preview(ux)
    old_handle = ux.handle(1)
    edit = AsyncMock()
    monkeypatch.setattr(aiogram_adapter, "_edit", edit)
    router = Router()
    build_power_address_router(ux, router=router)

    @router.callback_query(F.data == "menu:home")
    async def home(callback):
        await edit(callback, SimpleNamespace(text="NEW_MAIN_MENU"))

    storage = BoundedMemoryStorage()
    dispatcher = Dispatcher(storage=storage)
    dispatcher.include_router(router)
    bot = Bot("123456:LOCAL_TEST_ONLY_NOT_REAL", session=AsyncMock(return_value=True))
    actor = User(id=1, is_bot=False, first_name="Test")
    message = Message(message_id=1, date=NOW, chat=Chat(id=1, type="private"), from_user=actor)
    old = CallbackQuery(
        id="old-confirm",
        from_user=actor,
        chat_instance="local",
        message=message,
        data=f"pa:save:{old_handle}",
    )
    directory.block_next_seed = True
    pending = asyncio.create_task(
        dispatcher.feed_update(bot, Update(update_id=101, callback_query=old))
    )
    await entered.wait()
    new = old.model_copy(update={"id": "new-action", "data": next_action})
    await dispatcher.feed_update(bot, Update(update_id=102, callback_query=new))
    key = StorageKey(bot_id=bot.id, chat_id=1, user_id=1)
    new_state = await storage.get_state(key)
    new_data = await storage.get_data(key)
    expected_handle = ux.handle(1) if next_action == "pa:start" else None
    finish.set()
    await pending
    assert not port.saved
    assert await storage.get_state(key) == new_state
    assert await storage.get_data(key) == new_data
    if next_action == "pa:start":
        assert ux.handle(1) == expected_handle and expected_handle != old_handle
        # A later click on the old callback cannot erase the replacement either.
        await dispatcher.feed_update(bot, Update(update_id=103, callback_query=old))
        assert ux.handle(1) == expected_handle
    else:
        assert not ux._drafts and not ux._starts
    assert edit.await_count == 1
    assert edit.await_args.args[0].id == "new-action"
    assert "застарів" not in edit.await_args.args[1].text
    await storage.close()


async def test_real_dispatcher_late_address_text_cannot_clear_replacement_query(monkeypatch):
    from time_tl.bot import aiogram_adapter

    entered, finish = asyncio.Event(), asyncio.Event()

    class WaitingStreet(Directory):
        async def streets(self, seed, settlement, q):
            entered.set()
            await finish.wait()
            return await super().streets(seed, settlement, q)

    directory, port = WaitingStreet(), FakePowerPort()
    ux = PowerAddressUx(directory, port, clock=lambda: NOW)
    await ux.start(1)
    await ux.search(1, ux.handle(1), "Тест")
    await ux.pick(1, ux.handle(1), 0)
    old_handle = ux.handle(1)
    edit, answer = AsyncMock(), AsyncMock()
    monkeypatch.setattr(aiogram_adapter, "_edit", edit)
    monkeypatch.setattr(aiogram_adapter, "_answer", answer)
    router = Router()
    build_power_address_router(ux, router=router)
    storage = BoundedMemoryStorage()
    dispatcher = Dispatcher(storage=storage)
    dispatcher.include_router(router)
    bot = Bot("123456:LOCAL_TEST_ONLY_NOT_REAL", session=AsyncMock(return_value=True))
    actor = User(id=1, is_bot=False, first_name="Test")
    message = Message(
        message_id=1,
        date=NOW,
        chat=Chat(id=1, type="private"),
        from_user=actor,
        text="Синтетична вулиця",
    )
    key = StorageKey(bot_id=bot.id, chat_id=1, user_id=1)
    await storage.set_state(key, "AddressFlow:query")
    await storage.set_data(key, {"power_address_handle": old_handle})
    pending = asyncio.create_task(
        dispatcher.feed_update(bot, Update(update_id=201, message=message))
    )
    await entered.wait()
    replacement = CallbackQuery(
        id="replacement", from_user=actor, chat_instance="local", message=message, data="pa:start"
    )
    await dispatcher.feed_update(bot, Update(update_id=202, callback_query=replacement))
    new_handle = ux.handle(1)
    finish.set()
    await pending
    assert ux.handle(1) == new_handle and new_handle != old_handle
    assert await storage.get_state(key) == "AddressFlow:query"
    assert (await storage.get_data(key))["power_address_handle"] == new_handle
    answer.assert_not_awaited()
    assert edit.await_count == 1 and not port.saved
    await storage.close()


@pytest.mark.parametrize("query", ["a", "x" * 21, "a\nb"])
async def test_invalid_settlement_query_does_not_call_directory_or_consume_preview(query):
    directory = Directory()
    ux = PowerAddressUx(directory, FakePowerPort(), clock=lambda: NOW)
    await ux.start(1)
    handle = ux.handle(1)
    before = list(directory.calls)
    with pytest.raises(ValueError):
        await ux.search(1, handle, query)
    assert ux.handle(1) == handle and directory.calls == before


async def test_real_dispatcher_does_not_steal_other_fsm_and_clears_only_own_navigation():
    directory, port = Directory(), FakePowerPort()
    ux = PowerAddressUx(directory, port, clock=lambda: NOW)
    router = Router()
    build_power_address_router(ux, router=router)

    @router.message(F.text.startswith("/"))
    async def command(message):
        pass

    @router.callback_query(F.data == "menu:home")
    async def home(callback):
        pass

    storage = BoundedMemoryStorage()
    dispatcher = Dispatcher(storage=storage)
    dispatcher.include_router(router)
    bot = Bot("123456:LOCAL_TEST_ONLY_NOT_REAL", session=AsyncMock(return_value=True))
    actor = User(id=1, is_bot=False, first_name="Test")
    message = Message(message_id=1, date=NOW, chat=Chat(id=1, type="private"), from_user=actor)
    callback = CallbackQuery(
        id="address", from_user=actor, chat_instance="local", message=message, data="pa:start"
    )
    key = StorageKey(bot_id=bot.id, chat_id=1, user_id=1)
    await storage.set_state(key, "HomeworkFlow:text")
    await dispatcher.feed_update(bot, Update(update_id=1, callback_query=callback))
    assert await storage.get_state(key) == "HomeworkFlow:text" and not directory.calls
    await storage.set_state(key, None)
    await dispatcher.feed_update(bot, Update(update_id=2, callback_query=callback))
    assert "AddressFlow" in await storage.get_state(key)
    state_data = await storage.get_data(key)
    assert set(state_data) == {"power_address_handle"}
    await dispatcher.feed_update(
        bot, Update(update_id=3, message=message.model_copy(update={"text": "a"}))
    )
    assert "AddressFlow" in await storage.get_state(key)
    await dispatcher.feed_update(
        bot, Update(update_id=4, message=message.model_copy(update={"text": "/start"}))
    )
    assert await storage.get_state(key) is None and not ux._drafts
    group = callback.model_copy(
        update={"message": message.model_copy(update={"chat": Chat(id=-1, type="group")})}
    )
    before = list(directory.calls)
    await dispatcher.feed_update(bot, Update(update_id=5, callback_query=group))
    assert directory.calls == before and not port.saved
    await storage.close()


async def test_optional_facade_port_keeps_default_menu_unchanged():
    accounts = SimpleNamespace(get_existing=AsyncMock(return_value=None))
    plain = AsyncTelegramApplication(
        regions=(),
        resolver=SimpleNamespace(),
        repository=accounts,
        power_outages_enabled=True,
        power_port=FakePowerPort(),
    )
    enabled = AsyncTelegramApplication(
        regions=(),
        resolver=SimpleNamespace(),
        repository=accounts,
        power_outages_enabled=True,
        power_port=FakePowerPort(),
        power_address_port=Directory(),
    )
    assert plain.power_address_ux is None
    assert "pa:start" not in callbacks(await plain.power_ux.home(1))
    assert "pa:start" in callbacks(await enabled.power_ux.home(1))

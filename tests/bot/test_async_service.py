from __future__ import annotations

import asyncio
import copy
import json
import unittest
from dataclasses import dataclass
from datetime import UTC, datetime, time
from types import SimpleNamespace
from unittest.mock import AsyncMock

from time_tl.application.schedule_editor import (
    StaleLessonSelection,
    recurring_lesson_fingerprint,
)
from time_tl.bot.async_service import (
    AsyncTelegramApplication,
    BotStateCapacityExceeded,
    ExistingAccountMiddleware,
    build_async_router,
)
from time_tl.bot.models import NotificationMode
from time_tl.bot.safety import BoundedTelegramIngressGuard, TelegramAllowlistGuard
from time_tl.bot.service import Region, UserAccount
from time_tl.domain.schedule import ConfirmationError, Parity, encode_snapshot, export_snapshot


class AsyncMemoryRepository:
    def __init__(self) -> None:
        self.accounts: dict[int, UserAccount] = {}
        self.get_calls = 0
        self.existing_calls = 0
        self.save_calls = 0
        self.delete_calls: list[int] = []

    async def get_or_create(self, user_id: int) -> UserAccount:
        self.get_calls += 1
        account = self.accounts.setdefault(user_id, UserAccount(user_id))
        return copy.deepcopy(account)

    async def get_existing(self, user_id: int) -> UserAccount | None:
        self.existing_calls += 1
        account = self.accounts.get(user_id)
        return copy.deepcopy(account) if account is not None else None

    async def save(self, account: UserAccount) -> None:
        self.save_calls += 1
        self.accounts[account.user_id] = copy.deepcopy(account)

    async def delete_account(self, user_id: int) -> bool:
        self.delete_calls.append(user_id)
        return self.accounts.pop(user_id, None) is not None


class Resolver:
    def __init__(self, result: Region | None) -> None:
        self.result = result
        self.calls: list[tuple[float, float]] = []

    async def resolve(self, latitude: float, longitude: float) -> Region | None:
        self.calls.append((latitude, longitude))
        return self.result


class RegionConfirmationRecorder:
    def __init__(self) -> None:
        self.accounts: list[UserAccount] = []

    async def confirm_region(self, account: UserAccount) -> None:
        self.accounts.append(copy.deepcopy(account))


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


@dataclass(frozen=True)
class TerritoryNodeFake:
    region_id: str
    name: str
    kind: str
    parent_region_id: str | None


class TerritoryCatalogFake:
    def __init__(self) -> None:
        self.nodes = {
            "oblast-1": TerritoryNodeFake("oblast-1", "Дніпропетровська область", "OBLAST", None),
            "raion-1": TerritoryNodeFake("raion-1", "Кам’янський район", "RAION", "oblast-1"),
            "hromada-1": TerritoryNodeFake(
                "hromada-1", "Кам’янська територіальна громада", "HROMADA", "raion-1"
            ),
        }

    async def list_top(self, *, provider: str) -> tuple[TerritoryNodeFake, ...]:
        assert provider == "alerts.in.ua"
        return tuple(node for node in self.nodes.values() if node.parent_region_id is None)

    async def list_children(
        self, *, provider: str, parent_region_id: str
    ) -> tuple[TerritoryNodeFake, ...]:
        assert provider == "alerts.in.ua"
        return tuple(
            node for node in self.nodes.values() if node.parent_region_id == parent_region_id
        )

    async def get_path(self, *, provider: str, region_id: str) -> tuple[TerritoryNodeFake, ...]:
        assert provider == "alerts.in.ua"
        current = self.nodes.get(region_id)
        if current is None:
            raise LookupError(region_id)
        path = []
        while current is not None:
            path.append(current)
            current = (
                self.nodes.get(current.parent_region_id)
                if current.parent_region_id is not None
                else None
            )
        return tuple(reversed(path))


class AsyncTelegramApplicationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.kyiv = Region("31", "м. Київ")
        self.lviv = Region("46", "Львівська область")
        self.repo = AsyncMemoryRepository()
        self.repo.accounts[1] = UserAccount(1)
        self.resolver = Resolver(self.kyiv)
        self.app = AsyncTelegramApplication(
            regions=(self.kyiv, self.lviv),
            resolver=self.resolver,
            repository=self.repo,
            coordinate_suggestions_enabled=True,
        )

    async def onboard(self, user_id: int = 1) -> None:
        await self.app.start(user_id)
        await self.app.choose_mode(user_id, NotificationMode.LESSONS_AND_SIRENS)
        await self.app.propose_manual_region(user_id, "31")
        await self.app.confirm_region(user_id, confirmed=True)

    async def test_ingress_and_allowlist_run_before_account_database_lookup(self) -> None:
        ingress_guard = BoundedTelegramIngressGuard(
            per_user_rate=1.0,
            per_user_burst=1,
            global_rate=100.0,
            global_burst=100,
            monotonic=lambda: 0.0,
        )
        router = build_async_router(
            self.app,
            allowlist_guard=TelegramAllowlistGuard.from_ids((1,)),
            ingress_guard=ingress_guard,
        )
        middlewares = list(router.message.outer_middleware)
        self.assertEqual(
            [
                "AiogramIngressSafetyMiddleware",
                "AiogramAllowlistMiddleware",
                "ExistingAccountMiddleware",
            ],
            [type(item).__name__ for item in middlewares],
        )
        handler = AsyncMock(return_value="handled")
        wrapped = router.message.outer_middleware.wrap_middlewares(middlewares, handler)

        class Message:
            def __init__(self, user_id: int, chat_id: int, chat_type: str) -> None:
                self.from_user = SimpleNamespace(id=user_id)
                self.chat = SimpleNamespace(id=chat_id, type=chat_type)
                self.text = "menu"
                self.answer = AsyncMock()

        group = Message(1, -10, "group")
        unauthorized = Message(99, 99, "private")
        allowed = Message(1, 1, "private")

        self.assertIsNone(await wrapped(group, {}))
        self.assertIsNone(await wrapped(unauthorized, {}))
        self.assertEqual(0, self.repo.existing_calls)
        self.assertEqual("handled", await wrapped(allowed, {}))
        self.assertEqual(1, self.repo.existing_calls)
        self.assertIsNone(await wrapped(allowed, {}))
        self.assertEqual(1, self.repo.existing_calls)

    async def test_start_sequence_puts_instruction_before_existing_onboarding(self) -> None:
        instruction = "👋 Для чого потрібен бот\n⚠️ Дотримуйтеся офіційних вказівок."
        app = AsyncTelegramApplication(
            regions=(self.kyiv, self.lviv),
            resolver=self.resolver,
            repository=self.repo,
            onboarding_text=instruction,
        )

        first = await app.start_sequence(77)
        repeated = await app.start_sequence(77)

        self.assertEqual(2, len(first))
        self.assertTrue(first[0].text.startswith(instruction))
        self.assertIn("Telegram ID", first[0].text)
        self.assertIn("координати", first[0].text)
        self.assertIn("не зберігаються", first[0].text)
        self.assertIn("Спочатку оберіть", first[1].text)
        self.assertTrue(repeated[0].text.startswith(instruction))

    async def test_start_sequence_without_instruction_shows_privacy_before_setup(self) -> None:
        sequence = await self.app.start_sequence(78)
        self.assertEqual(2, len(sequence))
        self.assertIn("Конфіденційність", sequence[0].text)
        self.assertIn("Telegram ID", sequence[0].text)
        self.assertIn("Спочатку оберіть", sequence[1].text)

    async def test_process_state_cache_is_bounded_and_expires_idle_users(self) -> None:
        clock = FakeClock()
        app = AsyncTelegramApplication(
            regions=(self.kyiv,),
            resolver=self.resolver,
            repository=self.repo,
            process_state_max_users=2,
            process_state_ttl_seconds=10,
            process_state_clock=clock,
        )
        await app.start(1)
        await app.choose_mode(1, NotificationMode.SIRENS)
        await app.start(2)
        await app.choose_mode(2, NotificationMode.LESSONS)
        self.assertEqual(2, len(app._locks))
        self.assertEqual(2, len(app._memory.accounts))

        clock.advance(11)
        await app.start(3)
        await app.choose_mode(3, NotificationMode.LESSONS_AND_SIRENS)

        self.assertEqual((3,), tuple(app._locks))
        self.assertEqual((3,), tuple(app._memory.accounts))
        self.assertEqual(NotificationMode.SIRENS, self.repo.accounts[1].mode)
        self.assertEqual(NotificationMode.LESSONS, self.repo.accounts[2].mode)

    async def test_busy_user_lock_is_never_evicted_for_another_user(self) -> None:
        app = AsyncTelegramApplication(
            regions=(self.kyiv,),
            resolver=self.resolver,
            repository=self.repo,
            process_state_max_users=1,
        )
        async with app._user_lock(1):
            with self.assertRaises(BotStateCapacityExceeded):
                async with app._user_lock(2):
                    self.fail("capacity guard must fail before entering")
        async with app._user_lock(2):
            pass
        self.assertEqual((2,), tuple(app._locks))

    async def test_two_users_keep_independent_settings(self) -> None:
        await self.app.start(101)
        await self.app.choose_mode(101, NotificationMode.SIRENS)
        await self.app.start(202)
        await self.app.choose_mode(202, NotificationMode.LESSONS)

        self.assertEqual(NotificationMode.SIRENS, self.repo.accounts[101].mode)
        self.assertEqual(NotificationMode.LESSONS, self.repo.accounts[202].mode)

    async def test_main_menu_and_info_use_clear_ukrainian_copy(self) -> None:
        account = UserAccount(
            303,
            mode=NotificationMode.LESSONS_AND_SIRENS,
            region_id="hromada-1",
            region_name=("м. Кам’янське та Кам’янська територіальна громада"),
        )
        self.repo.accounts[303] = account

        menu = await self.app.menu(303)
        labels = [button.label for row in menu.rows for button in row]
        self.assertIn("⚙️ Ваші налаштування", menu.text)
        self.assertIn("🔔 Сповіщення: Уроки + тривоги", menu.text)
        self.assertIn("📍 Територія: Кам’янська громада", menu.text)
        self.assertIn("📚 Розклад: ще немає", menu.text)
        self.assertEqual(
            [
                "📚 Розклад",
                "🔔 Сповіщення",
                "📍 Моя територія",
                "⚙️ Налаштування",
                "ℹ️ Допомога",
            ],
            labels,
        )
        info = self.app.info()
        self.assertIn("не пропонує підключатися під час тривоги", info.text)
        self.assertIn("підтвердженого офіційного відбою", info.text)
        self.assertIn("лише тоді", info.text)
        self.assertEqual("menu:home", info.rows[0][0].callback_data)

        notifications = self.app.notifications_menu()
        self.assertEqual("menu:mode", notifications.rows[0][0].callback_data)
        self.assertEqual("menu:reminder", notifications.rows[1][0].callback_data)
        settings = self.app.settings_menu()
        self.assertEqual("settings:privacy", settings.rows[2][0].callback_data)
        privacy = self.app.privacy()
        self.assertIn("Telegram ID", privacy.text)
        self.assertIn("не зберігаються", privacy.text)
        self.assertEqual("account:delete:request", privacy.rows[0][0].callback_data)

    async def test_homework_menu_is_hidden_by_default(self) -> None:
        menu = await self.app.menu(1)

        callbacks = [button.callback_data for row in menu.rows for button in row]
        self.assertNotIn("menu:homework", callbacks)

    async def test_homework_menu_requires_flag_and_injected_port(self) -> None:
        without_port = AsyncTelegramApplication(
            regions=(self.kyiv,),
            resolver=self.resolver,
            repository=self.repo,
            homework_enabled=True,
        )
        hidden = await without_port.menu(1)
        self.assertNotIn(
            "menu:homework",
            [button.callback_data for row in hidden.rows for button in row],
        )

        with_port = AsyncTelegramApplication(
            regions=(self.kyiv,),
            resolver=self.resolver,
            repository=self.repo,
            homework_enabled=True,
            homework_port=object(),  # type: ignore[arg-type]
        )
        visible = await with_port.menu(1)
        self.assertIn(
            "menu:homework",
            [button.callback_data for row in visible.rows for button in row],
        )

    async def test_homework_count_is_optional_and_failure_is_isolated(self) -> None:
        class HomeworkPort:
            def __init__(self, *, fail: bool = False) -> None:
                self.fail = fail

            async def list_homework(
                self, user_id: int, *, include_past: bool = False
            ) -> tuple[SimpleNamespace, ...]:
                del user_id
                self.last_include_past = include_past
                if self.fail:
                    raise RuntimeError("homework unavailable")
                return (
                    SimpleNamespace(completed=False),
                    SimpleNamespace(completed=True),
                    SimpleNamespace(completed=False),
                )

        counted = AsyncTelegramApplication(
            regions=(self.kyiv,),
            resolver=self.resolver,
            repository=self.repo,
            homework_enabled=True,
            homework_port=HomeworkPort(),  # type: ignore[arg-type]
        )
        counted_menu = await counted.menu(1)
        counted_label = next(
            button.label
            for row in counted_menu.rows
            for button in row
            if button.callback_data == "menu:homework"
        )
        self.assertEqual("📝 Домашні завдання · 2", counted_label)

        isolated = AsyncTelegramApplication(
            regions=(self.kyiv,),
            resolver=self.resolver,
            repository=self.repo,
            homework_enabled=True,
            homework_port=HomeworkPort(fail=True),  # type: ignore[arg-type]
        )
        isolated_menu = await isolated.menu(1)
        isolated_label = next(
            button.label
            for row in isolated_menu.rows
            for button in row
            if button.callback_data == "menu:homework"
        )
        self.assertEqual("📝 Домашні завдання", isolated_label)

    async def test_data_deletion_requires_two_steps_and_clears_only_owner(self) -> None:
        await self.onboard(1)
        await self.onboard(2)
        profile_id = await self.app.create_profile(1, "Приватний")
        await self.app.add_lesson(
            1,
            profile_id,
            weekday=0,
            start=time(9),
            end=time(9, 45),
            title="Математика",
            join_url="https://secret.example/lesson",
        )

        first = await self.app.request_data_deletion(1)
        first_callback = first.rows[0][0].callback_data
        assert first_callback is not None
        token = first_callback.rsplit(":", 1)[1]
        self.assertIn("Telegram ID", first.text)
        self.assertIn("обрану територію", first.text)
        self.assertIn("налаштування", first.text)
        self.assertIn("розклади", first.text)
        self.assertIn("посилання на уроки", first.text)
        self.assertNotIn("secret.example", first.text)
        self.assertLessEqual(len(first_callback.encode("utf-8")), 64)

        premature = await self.app.confirm_data_deletion(1, token)
        self.assertIn("Дані не видалено", premature.text)
        self.assertIn(1, self.repo.accounts)
        self.assertEqual([], self.repo.delete_calls)

        first = await self.app.request_data_deletion(1)
        first_callback = first.rows[0][0].callback_data
        assert first_callback is not None
        token = first_callback.rsplit(":", 1)[1]
        wrong_owner = await self.app.review_data_deletion(2, token)
        self.assertIn("належить іншому користувачу", wrong_owner.text)
        self.assertIn(2, self.repo.accounts)

        final = await self.app.review_data_deletion(1, token)
        self.assertIn("Останнє підтвердження", final.text)
        self.assertIn("неможливо скасувати", final.text)
        self.assertNotIn("secret.example", final.text)
        confirm_callback = final.rows[0][0].callback_data
        assert confirm_callback is not None
        self.assertLessEqual(len(confirm_callback.encode("utf-8")), 64)

        deleted = await self.app.confirm_data_deletion(1, token)
        self.assertIn("Ваші дані видалено", deleted.text)
        self.assertNotIn(1, self.repo.accounts)
        self.assertIn(2, self.repo.accounts)
        self.assertEqual([1], self.repo.delete_calls)
        self.assertNotIn(1, self.app._memory.accounts)
        self.assertNotIn(1, self.app._locks)
        self.assertNotIn(1, self.app._app._deletions)
        self.assertFalse(any(key[0] == 1 for key in self.app._app._editors))
        self.assertFalse(any(key[0] == 1 for key in self.app._app._activations))
        self.assertFalse(any(key[0] == 1 for key in self.app._app._imports))
        self.assertFalse(any(key[0] == 1 for key in self.app._app._replacements))

        restarted = await self.app.start_sequence(1)
        self.assertIn("Конфіденційність", restarted[0].text)
        self.assertIn("Спочатку оберіть", restarted[1].text)
        fresh = self.repo.accounts[1]
        self.assertIsNone(fresh.mode)
        self.assertIsNone(fresh.region_id)
        self.assertEqual({}, fresh.profiles)

    async def test_data_deletion_cancel_and_absent_account_are_safe(self) -> None:
        await self.onboard(10)
        first = await self.app.request_data_deletion(10)
        token = first.rows[0][0].callback_data.rsplit(":", 1)[1]  # type: ignore[union-attr]
        cancelled = await self.app.cancel_data_deletion(10)
        self.assertIn("скасовано", cancelled.text)
        self.assertIn(10, self.repo.accounts)
        stale = await self.app.confirm_data_deletion(10, token)
        self.assertIn("Дані не видалено", stale.text)

        await self.onboard(404)
        first = await self.app.request_data_deletion(404)
        token = first.rows[0][0].callback_data.rsplit(":", 1)[1]  # type: ignore[union-attr]
        await self.app.review_data_deletion(404, token)
        self.repo.accounts.pop(404)
        result = await self.app.confirm_data_deletion(404, token)
        self.assertIn("Ваші дані видалено", result.text)
        self.assertEqual(404, self.repo.delete_calls[-1])

    async def test_pending_menu_race_and_old_callback_never_recreate_deleted_account(
        self,
    ) -> None:
        user_id = 55
        await self.onboard(user_id)
        first = await self.app.request_data_deletion(user_id)
        callback_data = first.rows[0][0].callback_data
        assert callback_data is not None
        token = callback_data.rsplit(":", 1)[1]
        await self.app.review_data_deletion(user_id, token)
        middleware = ExistingAccountMiddleware(self.app)
        handler_started = asyncio.Event()
        allow_handler = asyncio.Event()

        class EditableMessage:
            def __init__(self) -> None:
                self.text: str | None = None

            async def edit_text(self, text: str) -> None:
                self.text = text

        class CallbackQuery:
            def __init__(self) -> None:
                self.from_user = SimpleNamespace(id=user_id)
                self.message = EditableMessage()
                self.answered = False

            async def answer(self) -> None:
                self.answered = True

        async def pending_menu(_event: object, _data: dict[str, object]) -> None:
            handler_started.set()
            await allow_handler.wait()
            await self.app.menu(user_id)

        pending_callback = CallbackQuery()
        pending = asyncio.create_task(middleware(pending_menu, pending_callback, {}))
        await handler_started.wait()
        await self.app.confirm_data_deletion(user_id, token)
        allow_handler.set()
        await pending

        self.assertNotIn(user_id, self.repo.accounts)
        self.assertIn("Ваші дані видалено", pending_callback.message.text or "")
        self.assertTrue(pending_callback.answered)

        old_callback = CallbackQuery()
        called = False

        async def old_handler(_event: object, _data: dict[str, object]) -> None:
            nonlocal called
            called = True

        await middleware(old_handler, old_callback, {})
        self.assertFalse(called)
        self.assertIn("/start", old_callback.message.text or "")
        self.assertNotIn(user_id, self.repo.accounts)

    async def test_repository_coroutines_are_awaited_across_onboarding(self) -> None:
        start = await self.app.start(1)
        self.assertIn("Спочатку оберіть", start.text)
        await self.onboard()
        account = self.repo.accounts[1]
        self.assertEqual(NotificationMode.LESSONS_AND_SIRENS, account.mode)
        self.assertEqual("31", account.region_id)
        self.assertGreaterEqual(self.repo.save_calls, 2)
        menu = await self.app.start(1)
        self.assertIn("Уроки + тривоги", menu.text)

    async def test_unresolved_or_unsupported_geo_never_guesses_or_persists_coordinates(
        self,
    ) -> None:
        await self.app.start(7)
        self.resolver.result = None
        view = await self.app.submit_location(7, 1.25, 2.5)
        self.assertIn("Не вдалося", view.text)
        self.assertIn("вручну", view.text)
        self.assertIn(7, self.repo.accounts)
        self.assertNotIn(7, self.app._app._pending_regions)

        self.resolver.result = Region("unknown", "Невідомий")
        view = await self.app.submit_location(7, 3.5, 4.5)
        self.assertIn("вручну", view.text)
        self.assertEqual([(1.25, 2.5), (3.5, 4.5)], self.resolver.calls)
        self.assertIsNone(self.repo.accounts[7].region_id)
        self.assertNotIn("latitude", repr(self.app.__dict__))
        self.assertNotIn("longitude", repr(self.app.__dict__))

    async def test_geo_suggestion_requires_confirmation_and_persists_only_region(self) -> None:
        await self.app.start(8)
        await self.app.choose_mode(8, NotificationMode.LESSONS_AND_SIRENS)
        view = await self.app.submit_location(8, 50.4501, 30.5234)
        self.assertIn("не збережено", view.text)
        self.assertIn("Підтвердьте регіон", view.text)
        self.assertIsNone(self.repo.accounts[8].region_id)
        self.assertEqual(self.kyiv, self.app._app._pending_regions[8])

        rejected = await self.app.confirm_region(8, confirmed=False)
        self.assertIn("вручну", rejected.text)
        self.assertIsNone(self.repo.accounts[8].region_id)

        await self.app.submit_location(8, 50.4501, 30.5234)
        confirmed = await self.app.confirm_region(8, confirmed=True)
        self.assertIn("Налаштування завершено", confirmed.text)
        self.assertEqual("31", self.repo.accounts[8].region_id)
        self.assertNotIn("50.4501", repr(self.repo.accounts[8]))
        self.assertNotIn("30.5234", repr(self.repo.accounts[8]))

    async def test_confirmed_region_invokes_atomic_scope_coordinator(self) -> None:
        coordinator = RegionConfirmationRecorder()
        app = AsyncTelegramApplication(
            regions=(self.kyiv,),
            resolver=self.resolver,
            repository=self.repo,
            region_confirmation=coordinator,
        )
        await app.start(20)
        await app.choose_mode(20, NotificationMode.LESSONS_AND_SIRENS)
        await app.propose_manual_region(20, "31")
        await app.confirm_region(20, confirmed=True)
        self.assertEqual("31", coordinator.accounts[0].region_id)
        self.assertEqual(1, len(coordinator.accounts))

    async def test_manual_region_requires_confirmation(self) -> None:
        await self.app.start(2)
        await self.app.propose_manual_region(2, "46")
        await self.app.confirm_region(2, confirmed=False)
        self.assertIsNone(self.repo.accounts[2].region_id)
        await self.app.start(2)
        await self.app.propose_manual_region(2, "46")
        await self.app.confirm_region(2, confirmed=True)
        self.assertEqual("46", self.repo.accounts[2].region_id)

    async def test_catalog_navigation_shows_hierarchy_back_home_and_confirmation(self) -> None:
        catalog = TerritoryCatalogFake()
        app = AsyncTelegramApplication(
            regions=(self.kyiv,),
            resolver=self.resolver,
            repository=self.repo,
            territory_catalog=catalog,
        )

        oblasts = await app.territory_top()
        self.assertIn("Оберіть область", oblasts.text)
        self.assertIn("офіційні вказівки", oblasts.text)
        self.assertEqual("territory:open:oblast-1:0", oblasts.rows[0][0].callback_data)
        self.assertEqual("menu:home", oblasts.rows[-1][0].callback_data)

        raions = await app.territory_open("oblast-1")
        self.assertIn("Оберіть район", raions.text)
        self.assertIn("Дніпропетровська область", raions.text)
        self.assertEqual("territory:top:0", raions.rows[-2][0].callback_data)

        hromadas = await app.territory_open("raion-1")
        self.assertIn("Оберіть громаду", hromadas.text)
        self.assertEqual("territory:open:oblast-1:0", hromadas.rows[-2][0].callback_data)

        confirmation = await app.territory_open("hromada-1")
        self.assertIn("Підтвердьте територію", confirmation.text)
        self.assertIn("Дніпропетровська область", confirmation.text)
        self.assertIn("Кам’янський район", confirmation.text)
        self.assertIn("Кам’янська територіальна громада", confirmation.text)
        self.assertEqual("territory:confirm:hromada-1", confirmation.rows[0][0].callback_data)
        self.assertEqual("territory:open:raion-1:0", confirmation.rows[1][0].callback_data)

    async def test_catalog_oblast_list_is_paginated_with_telegram_safe_callbacks(self) -> None:
        catalog = TerritoryCatalogFake()
        catalog.nodes = {
            f"00000000-0000-0000-0000-{index:012d}": TerritoryNodeFake(
                f"00000000-0000-0000-0000-{index:012d}",
                f"Область {index}",
                "OBLAST",
                None,
            )
            for index in range(10)
        }
        app = AsyncTelegramApplication(
            regions=(self.kyiv,),
            resolver=self.resolver,
            repository=self.repo,
            territory_catalog=catalog,
        )

        first = await app.territory_top(page=0)
        second = await app.territory_top(page=1)
        self.assertIn("Сторінка 1 з 2", first.text)
        self.assertIn("Сторінка 2 з 2", second.text)
        self.assertEqual(8, sum(1 for row in first.rows if row[0].label.startswith("Область")))
        self.assertEqual(2, sum(1 for row in second.rows if row[0].label.startswith("Область")))
        for view in (first, second):
            for row in view.rows:
                for button in row:
                    if button.callback_data is not None:
                        self.assertLessEqual(len(button.callback_data.encode("utf-8")), 64)

    async def test_static_region_picker_remains_fallback_without_catalog(self) -> None:
        view = await self.app.territory_top()
        self.assertIn("Оберіть область", view.text)
        self.assertEqual("region:pick:31", view.rows[0][0].callback_data)

    async def test_catalog_confirmation_is_restart_safe_and_reloads_path(self) -> None:
        catalog = TerritoryCatalogFake()
        first = AsyncTelegramApplication(
            regions=(self.kyiv,),
            resolver=self.resolver,
            repository=self.repo,
            territory_catalog=catalog,
        )
        await first.start(40)
        await first.choose_mode(40, NotificationMode.LESSONS_AND_SIRENS)
        preview = await first.territory_open("hromada-1")
        self.assertIn("Підтвердьте", preview.text)

        restarted = AsyncTelegramApplication(
            regions=(self.kyiv,),
            resolver=self.resolver,
            repository=self.repo,
            territory_catalog=catalog,
        )
        result = await restarted.confirm_catalog_territory(40, "hromada-1")

        self.assertIn("✅ Територію обрано", result.text)
        self.assertEqual("hromada-1", self.repo.accounts[40].region_id)
        self.assertEqual("Кам’янська територіальна громада", self.repo.accounts[40].region_name)

    async def test_catalog_rejects_stale_or_tampered_uid_without_changing_account(self) -> None:
        catalog = TerritoryCatalogFake()
        app = AsyncTelegramApplication(
            regions=(self.kyiv,),
            resolver=self.resolver,
            repository=self.repo,
            territory_catalog=catalog,
        )
        await app.start(41)
        await app.choose_mode(41, NotificationMode.SIRENS)
        self.repo.accounts[41].region_id = "31"
        self.repo.accounts[41].region_name = "м. Київ"

        tampered = await app.confirm_catalog_territory(41, "does-not-exist")
        self.assertIn("Налаштування не змінено", tampered.text)
        self.assertEqual("31", self.repo.accounts[41].region_id)

        preview = await app.territory_open("hromada-1")
        self.assertIn("Підтвердьте", preview.text)
        del catalog.nodes["hromada-1"]
        stale = await app.confirm_catalog_territory(41, "hromada-1")
        self.assertIn("довідник оновився", stale.text)
        self.assertEqual("31", self.repo.accounts[41].region_id)

    async def test_profile_crud_lesson_update_and_activation_are_persisted(self) -> None:
        await self.onboard()
        first = await self.app.create_profile(1, "Основний")
        second = await self.app.create_profile(1, "Субота")
        profile = await self.app.add_lesson(
            1, second, weekday=5, start=time(9), end=time(9, 45), title="Математика"
        )
        self.assertEqual("Математика", profile.rules[0].lessons[0].title)
        profile = await self.app.set_lesson_enabled(
            1, second, weekday=5, lesson_index=0, enabled=False
        )
        self.assertFalse(profile.rules[0].lessons[0].enabled)
        token, preview = await self.app.request_activation(1, second)
        self.assertIn("Попередній перегляд", preview.text)
        await self.app.confirm_activation(1, token)
        self.assertTrue(self.repo.accounts[1].profiles[second].active)
        await self.app.delete_profile(1, second)
        self.assertTrue(self.repo.accounts[1].profiles[first].active)

    async def test_recurring_lesson_detail_edit_toggle_delete_and_stale_guard(self) -> None:
        profile_id = await self.app.create_profile(1, "Основний")
        profile = await self.app.add_lesson(
            1,
            profile_id,
            weekday=0,
            start=time(9),
            end=time(9, 45),
            title="Математика",
            week_parity=Parity.EVEN,
            date_parity=Parity.ODD,
            join_url="https://school.example/private",
        )
        fingerprint = recurring_lesson_fingerprint(profile, 0, 0)

        detail = await self.app.lesson_editor_view(
            1,
            profile_id,
            rule_index=0,
            lesson_index=0,
            expected_fingerprint=fingerprint,
        )
        self.assertIn("Повторюваний урок", detail.text)
        self.assertIn("Пн · парні тижні · непарні дати", detail.text)
        self.assertIn("09:00–09:45", detail.text)
        self.assertIn("Математика", detail.text)
        self.assertNotIn("school.example", detail.text)
        callbacks = [button.callback_data for row in detail.rows for button in row]
        self.assertNotIn("school.example", " ".join(item or "" for item in callbacks))
        self.assertTrue(any((item or "").startswith("len:") for item in callbacks))
        self.assertTrue(any((item or "").startswith("let:") for item in callbacks))
        self.assertTrue(any((item or "").startswith("leg:") for item in callbacks))
        self.assertTrue(any((item or "").startswith("led:") for item in callbacks))
        self.assertTrue(all(item is None or len(item.encode("utf-8")) <= 64 for item in callbacks))

        profile = await self.app.edit_lesson_title(
            1,
            profile_id,
            rule_index=0,
            lesson_index=0,
            expected_fingerprint=fingerprint,
            title="Алгебра",
        )
        lesson = profile.rules[0].lessons[0]
        self.assertEqual("Алгебра", lesson.title)
        self.assertEqual("https://school.example/private", lesson.join_url)
        self.assertEqual(time(9), lesson.start)

        with self.assertRaises(StaleLessonSelection):
            await self.app.edit_lesson_time(
                1,
                profile_id,
                rule_index=0,
                lesson_index=0,
                expected_fingerprint=fingerprint,
                start=time(10),
                end=time(10, 45),
            )

        fingerprint = recurring_lesson_fingerprint(profile, 0, 0)
        profile = await self.app.edit_lesson_time(
            1,
            profile_id,
            rule_index=0,
            lesson_index=0,
            expected_fingerprint=fingerprint,
            start=time(10),
            end=time(10, 45),
        )
        lesson = profile.rules[0].lessons[0]
        self.assertEqual((time(10), time(10, 45)), (lesson.start, lesson.end))
        self.assertEqual("Алгебра", lesson.title)
        self.assertEqual("https://school.example/private", lesson.join_url)

        fingerprint = recurring_lesson_fingerprint(profile, 0, 0)
        profile = await self.app.toggle_selected_lesson(
            1,
            profile_id,
            rule_index=0,
            lesson_index=0,
            expected_fingerprint=fingerprint,
        )
        self.assertFalse(profile.rules[0].lessons[0].enabled)

        fingerprint = recurring_lesson_fingerprint(profile, 0, 0)
        profile = await self.app.delete_lesson(
            1,
            profile_id,
            rule_index=0,
            lesson_index=0,
            expected_fingerprint=fingerprint,
        )
        self.assertEqual((), profile.rules[0].lessons)
        self.assertEqual((), self.repo.accounts[1].profiles[profile_id].rules[0].lessons)

    async def test_profile_editor_paginates_maximum_recurring_lesson_list(self) -> None:
        profile_id = await self.app.create_profile(1, "Великий розклад")
        for index in range(16):
            hour, minute = divmod(index * 30, 60)
            await self.app.add_lesson(
                1,
                profile_id,
                weekday=0,
                start=time(hour, minute),
                end=time(hour, minute + 20),
                title=f"Урок {index + 1}",
            )
        await self.app.add_lesson(
            1,
            profile_id,
            weekday=1,
            start=time(9),
            end=time(9, 45),
            title="Урок 17",
        )

        pages = tuple(
            [await self.app.profile_editor_view(1, profile_id, page=page) for page in range(3)]
        )
        self.assertIn("Сторінка уроків 1 з 3", pages[0].text)
        self.assertIn("Сторінка уроків 2 з 3", pages[1].text)
        self.assertIn("Сторінка уроків 3 з 3", pages[2].text)
        edit_counts = [
            sum(
                1
                for row in view.rows
                for button in row
                if (button.callback_data or "").startswith("le:")
            )
            for view in pages
        ]
        self.assertEqual([8, 8, 1], edit_counts)
        for view in pages:
            for row in view.rows:
                for button in row:
                    if button.callback_data is not None:
                        self.assertLessEqual(len(button.callback_data.encode("utf-8")), 64)

    async def test_recurring_lesson_detail_rejects_stale_selection(self) -> None:
        profile_id = await self.app.create_profile(1, "Основний")
        profile = await self.app.add_lesson(
            1, profile_id, weekday=0, start=time(9), end=time(9, 45), title="Урок"
        )
        fingerprint = recurring_lesson_fingerprint(profile, 0, 0)
        await self.app.edit_lesson_title(
            1,
            profile_id,
            rule_index=0,
            lesson_index=0,
            expected_fingerprint=fingerprint,
            title="Нова назва",
        )
        with self.assertRaises(StaleLessonSelection):
            await self.app.lesson_editor_view(
                1,
                profile_id,
                rule_index=0,
                lesson_index=0,
                expected_fingerprint=fingerprint,
            )

    async def test_schedule_menu_distinguishes_duplicates_and_moves_activation_into_editor(
        self,
    ) -> None:
        first = await self.app.create_profile(1, "Парний тиждень")
        second = await self.app.create_profile(1, "Парний тиждень")
        third = await self.app.create_profile(1, "5-А")
        await self.app.add_lesson(
            1,
            second,
            weekday=0,
            start=time(9),
            end=time(9, 45),
            title="Математика",
            week_parity=Parity.EVEN,
            date_parity=Parity.ODD,
        )
        await self.app.add_lesson(
            1, third, weekday=1, start=time(10), end=time(10, 45), title="Фізика"
        )
        await self.app.add_lesson(
            1, third, weekday=1, start=time(11), end=time(11, 45), title="Історія"
        )
        await self.app.set_lesson_enabled(1, third, weekday=1, lesson_index=1, enabled=False)

        menu = await self.app.schedules_menu(1)
        labels = [button.label for row in menu.rows for button in row]
        callbacks = [
            button.callback_data
            for row in menu.rows
            for button in row
            if button.callback_data is not None
        ]
        self.assertIn("3 з 3", menu.text)
        self.assertIn("ліміт", menu.text.lower())
        self.assertIn("✅ 1. Парний тиждень · порожній", labels)
        self.assertIn("▫️ 2. Парний тиждень · 1/1 увімк.", labels)
        self.assertIn("▫️ 3. 5-А · 1/2 увімк.", labels)
        self.assertNotIn("schedule:create", callbacks)
        self.assertFalse(any(item.startswith("activate:request:") for item in callbacks))
        self.assertEqual(
            {
                f"schedule:edit:{first}",
                f"schedule:edit:{second}",
                f"schedule:edit:{third}",
            },
            {item for item in callbacks if item.startswith("schedule:edit:")},
        )

        active_editor = await self.app.profile_editor_view(1, first)
        inactive_editor = await self.app.profile_editor_view(1, second)
        active_callbacks = {button.callback_data for row in active_editor.rows for button in row}
        inactive_callbacks = {
            button.callback_data for row in inactive_editor.rows for button in row
        }
        self.assertIn("Розклад 1 з 3", active_editor.text)
        self.assertIn("✅ Активний", active_editor.text)
        self.assertFalse(
            any((item or "").startswith("activate:request:") for item in active_callbacks)
        )
        self.assertIn("Розклад 2 з 3", inactive_editor.text)
        self.assertIn("▫️ Неактивний", inactive_editor.text)
        self.assertIn("Пн · парні тижні · непарні дати", inactive_editor.text)
        self.assertNotIn("EVEN", inactive_editor.text)
        self.assertIn(f"activate:request:{second}", inactive_callbacks)

    async def test_profile_deletion_requires_named_confirmation_view(self) -> None:
        first = await self.app.create_profile(1, "Основний")
        second = await self.app.create_profile(1, "Резервний")
        preview = await self.app.request_profile_deletion(1, first)
        self.assertIn("Основний", preview.text)
        self.assertIn("активний", preview.text)
        self.assertIn(first, self.repo.accounts[1].profiles)
        result = await self.app.delete_profile(1, first)
        self.assertIn("видалено", result.text)
        self.assertTrue(self.repo.accounts[1].profiles[second].active)

    async def test_export_excludes_links_by_default_and_requires_explicit_opt_in(self) -> None:
        profile_id = await self.app.create_profile(1, "Спільний")
        await self.app.add_lesson(
            1,
            profile_id,
            weekday=0,
            start=time(9),
            end=time(9, 45),
            title="Урок",
            join_url="https://school.example/private",
        )
        preview = await self.app.request_profile_export(1, profile_id)
        self.assertIn("не додаються", preview.text)
        without_links = json.loads(
            await self.app.export_profile(1, profile_id, include_links=False)
        )
        with_links = json.loads(await self.app.export_profile(1, profile_id, include_links=True))
        lesson_without_links = without_links["profile"]["rules"][0]["lessons"][0]
        lesson_with_links = with_links["profile"]["rules"][0]["lessons"][0]
        self.assertNotIn("join_url", lesson_without_links)
        self.assertEqual("https://school.example/private", lesson_with_links["join_url"])

    async def test_all_generated_callback_data_fits_telegram_limit(self) -> None:
        profile_id = await self.app.create_profile(1, "Callback")
        await self.app.add_lesson(
            1, profile_id, weekday=0, start=time(9), end=time(9, 45), title="Урок"
        )
        views = (
            await self.app.profile_editor_view(1, profile_id),
            await self.app.request_profile_deletion(1, profile_id),
            await self.app.request_profile_export(1, profile_id),
        )
        for view in views:
            for row in view.rows:
                for button in row:
                    if button.callback_data is not None:
                        self.assertLessEqual(len(button.callback_data.encode("utf-8")), 64)

    async def test_live_composition_can_forbid_process_local_import_sessions(self) -> None:
        app = AsyncTelegramApplication(
            regions=(self.kyiv,),
            resolver=self.resolver,
            repository=self.repo,
            allow_process_local_imports=False,
        )
        with self.assertRaisesRegex(RuntimeError, "durable import-session"):
            await app.preview_import(
                1,
                (
                    '{"schema_version":"1.0","metadata":{"template_name":"x",'
                    '"created_at":"2026-09-20T00:00:00+00:00","source_display_label":"x",'
                    '"included_fields":[]},"profile":{"name":"x","timezone":"Europe/Kyiv"}}'
                ),
            )

    async def test_import_requires_preview_and_creates_independent_profile(self) -> None:
        source = AsyncTelegramApplication(
            regions=(self.kyiv,), resolver=self.resolver, repository=AsyncMemoryRepository()
        )
        await source.start(8)
        source_id = await source.create_profile(8, "8-А")
        source_profile = await source.add_lesson(
            8, source_id, weekday=0, start=time(8), end=time(8, 45), title="Мова"
        )
        snapshot = encode_snapshot(
            export_snapshot(
                source_profile,
                source_display_label="Однокласник",
                created_at=datetime(2026, 9, 20, tzinfo=UTC),
            )
        )
        await self.app.start(9)
        token, preview = await self.app.preview_import(9, snapshot)
        self.assertIn("Однокласник", preview.text)
        self.assertEqual({}, self.repo.accounts[9].profiles)
        await self.app.confirm_import(9, token)
        imported = next(iter(self.repo.accounts[9].profiles.values()))
        self.assertNotEqual(source_id, imported.profile_id)
        self.assertTrue(imported.active)

    async def test_imported_lesson_links_are_stripped_without_separate_confirmation(self) -> None:
        source = AsyncTelegramApplication(
            regions=(self.kyiv,), resolver=self.resolver, repository=AsyncMemoryRepository()
        )
        await source.start(8)
        source_id = await source.create_profile(8, "Посилання")
        profile = await source.add_lesson(
            8,
            source_id,
            weekday=0,
            start=time(10),
            end=time(10, 45),
            title="Урок",
            join_url="https://school.example/lesson",
        )
        raw = encode_snapshot(
            export_snapshot(
                profile,
                source_display_label="Учень",
                include_join_links=True,
                confirm_include_join_links=True,
            )
        )
        await self.app.start(9)
        token, _ = await self.app.preview_import(9, raw)
        await self.app.confirm_import(9, token)
        imported = next(iter(self.repo.accounts[9].profiles.values()))
        self.assertIsNone(imported.rules[0].lessons[0].join_url)

    async def test_full_import_offers_explicit_replacement_and_preserves_active_slot(self) -> None:
        await self.app.start(9)
        profile_ids = [await self.app.create_profile(9, name) for name in ("A", "B", "C")]
        source = AsyncTelegramApplication(
            regions=(self.kyiv,), resolver=self.resolver, repository=AsyncMemoryRepository()
        )
        await source.start(8)
        source_id = await source.create_profile(8, "Новий")
        source_profile = await source.add_lesson(
            8, source_id, weekday=0, start=time(8), end=time(8, 45), title="Мова"
        )
        raw = encode_snapshot(export_snapshot(source_profile, source_display_label="Клас"))
        import_token, _ = await self.app.preview_import(9, raw)
        with self.assertRaisesRegex(ValueError, "profile limit"):
            await self.app.confirm_import(9, import_token)
        picker = await self.app.import_replacement_picker(9, import_token, confirm_links=False)
        self.assertEqual(3, len(picker.rows) - 1)
        picker_labels = [button.label for row in picker.rows[:-1] for button in row]
        self.assertIn("✅ Замінити №1 «A»", picker_labels)
        self.assertIn("Замінити №2 «B»", picker_labels)
        self.assertIn("Замінити №3 «C»", picker_labels)
        self.assertTrue(
            all(
                button.callback_data is None or len(button.callback_data.encode("utf-8")) <= 64
                for row in picker.rows
                for button in row
            )
        )
        replacement_token, preview = await self.app.request_import_replacement_by_index(
            9, import_token, 1, confirm_links=False
        )
        self.assertIn("B", preview.text)
        await self.app.confirm_import_replacement(9, replacement_token)
        account = self.repo.accounts[9]
        self.assertEqual(3, len(account.profiles))
        self.assertNotIn(profile_ids[1], account.profiles)
        self.assertTrue(account.profiles[profile_ids[0]].active)
        with self.assertRaises((ConfirmationError, KeyError)):
            await self.app.confirm_import_replacement(9, replacement_token)


if __name__ == "__main__":
    unittest.main()

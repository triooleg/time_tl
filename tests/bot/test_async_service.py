from __future__ import annotations

import copy
import json
import unittest
from datetime import UTC, datetime, time

from time_tl.bot.async_service import AsyncTelegramApplication
from time_tl.bot.models import NotificationMode
from time_tl.bot.service import Region, UserAccount
from time_tl.domain.schedule import ConfirmationError, encode_snapshot, export_snapshot


class AsyncMemoryRepository:
    def __init__(self) -> None:
        self.accounts: dict[int, UserAccount] = {}
        self.get_calls = 0
        self.save_calls = 0

    async def get_or_create(self, user_id: int) -> UserAccount:
        self.get_calls += 1
        account = self.accounts.setdefault(user_id, UserAccount(user_id))
        return copy.deepcopy(account)

    async def save(self, account: UserAccount) -> None:
        self.save_calls += 1
        self.accounts[account.user_id] = copy.deepcopy(account)


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


class AsyncTelegramApplicationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.kyiv = Region("31", "м. Київ")
        self.lviv = Region("46", "Львівська область")
        self.repo = AsyncMemoryRepository()
        self.resolver = Resolver(self.kyiv)
        self.app = AsyncTelegramApplication(
            regions=(self.kyiv, self.lviv), resolver=self.resolver, repository=self.repo
        )

    async def onboard(self, user_id: int = 1) -> None:
        await self.app.choose_mode(user_id, NotificationMode.LESSONS_AND_SIRENS)
        await self.app.submit_location(user_id, 50.45, 30.52)
        await self.app.confirm_region(user_id, confirmed=True)

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
        self.resolver.result = None
        view = await self.app.submit_location(7, 1.25, 2.5)
        self.assertIn("вручну", view.text)
        self.assertNotIn(7, self.repo.accounts)
        self.assertNotIn(7, self.app._app._pending_regions)

        self.resolver.result = Region("unknown", "Невідомий")
        view = await self.app.submit_location(7, 3.5, 4.5)
        self.assertIn("вручну", view.text)
        self.assertNotIn(7, self.repo.accounts)
        self.assertNotIn("latitude", repr(self.app.__dict__))
        self.assertNotIn("longitude", repr(self.app.__dict__))

    async def test_confirmed_region_invokes_atomic_scope_coordinator(self) -> None:
        coordinator = RegionConfirmationRecorder()
        app = AsyncTelegramApplication(
            regions=(self.kyiv,),
            resolver=self.resolver,
            repository=self.repo,
            region_confirmation=coordinator,
        )
        await app.choose_mode(20, NotificationMode.LESSONS_AND_SIRENS)
        await app.propose_manual_region(20, "31")
        await app.confirm_region(20, confirmed=True)
        self.assertEqual("31", coordinator.accounts[0].region_id)
        self.assertEqual(1, len(coordinator.accounts))

    async def test_manual_region_requires_confirmation(self) -> None:
        await self.app.propose_manual_region(2, "46")
        await self.app.confirm_region(2, confirmed=False)
        self.assertIsNone(self.repo.accounts[2].region_id)
        await self.app.propose_manual_region(2, "46")
        await self.app.confirm_region(2, confirmed=True)
        self.assertEqual("46", self.repo.accounts[2].region_id)

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
        self.assertEqual(
            "https://school.example/private", lesson_with_links["join_url"]
        )

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
        token, preview = await self.app.preview_import(9, snapshot)
        self.assertIn("Однокласник", preview.text)
        self.assertEqual({}, self.repo.accounts[9].profiles)
        await self.app.confirm_import(9, token)
        imported = next(iter(self.repo.accounts[9].profiles.values()))
        self.assertNotEqual(source_id, imported.profile_id)
        self.assertTrue(imported.active)

    async def test_imported_lesson_links_need_separate_confirmation(self) -> None:
        source = AsyncTelegramApplication(
            regions=(self.kyiv,), resolver=self.resolver, repository=AsyncMemoryRepository()
        )
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
        token, _ = await self.app.preview_import(9, raw)
        with self.assertRaises(ConfirmationError):
            await self.app.confirm_import(9, token)
        self.assertEqual({}, self.repo.accounts[9].profiles)

    async def test_full_import_offers_explicit_replacement_and_preserves_active_slot(self) -> None:
        profile_ids = [await self.app.create_profile(9, name) for name in ("A", "B", "C")]
        source = AsyncTelegramApplication(
            regions=(self.kyiv,), resolver=self.resolver, repository=AsyncMemoryRepository()
        )
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
        self.assertTrue(
            all(
                button.callback_data is None
                or len(button.callback_data.encode("utf-8")) <= 64
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

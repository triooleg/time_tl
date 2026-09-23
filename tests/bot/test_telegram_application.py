from __future__ import annotations

import unittest
from datetime import UTC, date, datetime, time

from time_tl.bot.models import NotificationMode
from time_tl.bot.service import InMemoryAccountRepository, Region, TelegramApplication
from time_tl.domain.schedule import (
    ConfirmationError,
    Parity,
    ProfileLimitReached,
    encode_snapshot,
    export_snapshot,
    resolve_day,
)


class FakeResolver:
    def __init__(self, result: Region | None) -> None:
        self.result = result
        self.calls: list[tuple[float, float]] = []

    async def resolve(self, latitude: float, longitude: float) -> Region | None:
        self.calls.append((latitude, longitude))
        return self.result


class TelegramApplicationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.kyiv = Region("31", "м. Київ")
        self.lviv = Region("46", "Львівська область")
        self.repo = InMemoryAccountRepository()
        self.resolver = FakeResolver(self.kyiv)
        self.app = TelegramApplication(
            regions=(self.kyiv, self.lviv), resolver=self.resolver, repository=self.repo
        )

    async def _onboard(self, user_id: int = 1) -> None:
        self.app.choose_mode(user_id, NotificationMode.LESSONS_AND_SIRENS)
        await self.app.submit_location(user_id, 50.4501, 30.5234)
        self.app.confirm_region(user_id, confirmed=True)

    def test_first_start_explains_safety_and_offers_exact_three_modes(self) -> None:
        view = self.app.start(1)
        self.assertIn("не замінює офіційні", view.text)
        callbacks = [button.callback_data for row in view.rows for button in row]
        self.assertEqual(callbacks, ["mode:lessons", "mode:lessons_and_sirens", "mode:sirens"])

    async def test_location_only_persists_confirmed_region_not_coordinates(self) -> None:
        self.app.choose_mode(7, NotificationMode.SIRENS)
        view = await self.app.submit_location(7, 49.8397, 24.0297)
        self.assertIn("м. Київ", view.text)
        account_before = self.repo.accounts[7]
        self.assertIsNone(account_before.region_id)
        self.assertNotIn("latitude", vars(account_before))
        self.assertNotIn("longitude", vars(account_before))
        self.app.confirm_region(7, confirmed=True)
        account = self.repo.accounts[7]
        self.assertEqual(account.region_id, "31")
        self.assertEqual(account.region_name, "м. Київ")
        self.assertNotIn("50", repr(account))

    def test_manual_region_requires_confirmation_and_can_be_rejected(self) -> None:
        self.app.propose_manual_region(3, "46")
        view = self.app.confirm_region(3, confirmed=False)
        self.assertIsNone(self.repo.accounts[3].region_id)
        self.assertIn("Геолокація", view.text)
        self.app.propose_manual_region(3, "46")
        self.app.confirm_region(3, confirmed=True)
        self.assertEqual(self.repo.accounts[3].region_id, "46")

    async def test_completed_user_returns_to_menu(self) -> None:
        await self._onboard()
        view = self.app.start(1)
        self.assertIn("Уроки + тривоги", view.text)
        self.assertIn("м. Київ", view.text)

    async def test_profile_limit_is_three(self) -> None:
        await self._onboard()
        for name in ("Клас 8-А", "Гурток", "Канікули"):
            self.app.create_profile(1, name)
        with self.assertRaises(ProfileLimitReached):
            self.app.create_profile(1, "Четвертий")

    async def test_arbitrary_lessons_parity_weekend_toggle_and_override(self) -> None:
        await self._onboard()
        profile_id = self.app.create_profile(1, "8-А")
        self.app.add_lesson(
            1,
            profile_id,
            weekday=5,
            start=time(9),
            end=time(9, 45),
            title="Математика",
            week_parity=Parity.EVEN,
        )
        profile = self.app.add_lesson(
            1,
            profile_id,
            weekday=5,
            start=time(11),
            end=time(11, 45),
            title="Фізика",
            week_parity=Parity.EVEN,
            date_parity=Parity.ODD,
        )
        self.assertEqual(len(profile.rules), 2)
        # Saturday 2026-09-26 is ISO week 39 (odd): even-week lessons do not apply.
        self.assertEqual(resolve_day(profile, date(2026, 9, 26)).lessons, ())
        # The combined parity rule is more specific and replaces the base rule.
        resolved = resolve_day(profile, date(2026, 10, 3))
        self.assertEqual([lesson.title for lesson in resolved.lessons], ["Фізика"])
        profile = self.app.set_lesson_enabled(
            1,
            profile_id,
            weekday=5,
            lesson_index=0,
            enabled=False,
            week_parity=Parity.EVEN,
            date_parity=Parity.ODD,
        )
        self.assertFalse(profile.rules[1].lessons[0].enabled)
        profile = self.app.set_day_off(1, profile_id, date(2026, 10, 3))
        self.assertEqual(resolve_day(profile, date(2026, 10, 3)).lessons, ())

    async def test_activation_requires_preview_then_confirmation(self) -> None:
        await self._onboard()
        first = self.app.create_profile(1, "Основний")
        second = self.app.create_profile(1, "Парний тиждень")
        self.assertTrue(self.repo.accounts[1].profiles[first].active)
        token, view = self.app.request_activation(1, second)
        self.assertIn("Попередній перегляд", view.text)
        self.assertTrue(self.repo.accounts[1].profiles[first].active)
        result = self.app.confirm_activation(1, token)
        self.assertIn("Активовано", result.text)
        self.assertTrue(self.repo.accounts[1].profiles[second].active)
        self.assertFalse(self.repo.accounts[1].profiles[first].active)

    async def test_import_shows_metadata_and_creates_independent_copy(self) -> None:
        await self._onboard()
        source_id = self.app.create_profile(1, "Розклад класу")
        source = self.app.add_lesson(
            1, source_id, weekday=0, start=time(8, 30), end=time(9, 15), title="Українська мова"
        )
        snapshot = export_snapshot(
            source, source_display_label="Однокласник", created_at=datetime(2026, 9, 20, tzinfo=UTC)
        )
        target_repo = InMemoryAccountRepository()
        target = TelegramApplication(
            regions=(self.kyiv,), resolver=self.resolver, repository=target_repo
        )
        target.choose_mode(2, NotificationMode.LESSONS)
        target.propose_manual_region(2, "31")
        target.confirm_region(2, confirmed=True)
        token, preview = target.preview_import(2, encode_snapshot(snapshot))
        self.assertIn("Однокласник", preview.text)
        self.assertIn("Попередній перегляд 14 днів", preview.text)
        self.assertEqual(target_repo.accounts[2].profiles, {})
        target.confirm_import(2, token)
        imported = next(iter(target_repo.accounts[2].profiles.values()))
        self.assertNotEqual(imported.profile_id, source.profile_id)
        self.assertEqual(imported.name, source.name)
        self.assertTrue(imported.active)

    async def test_import_with_join_link_demands_separate_confirmation(self) -> None:
        await self._onboard()
        source_id = self.app.create_profile(1, "Посилання")
        source = self.app.add_lesson(
            1,
            source_id,
            weekday=0,
            start=time(10),
            end=time(10, 45),
            title="Урок",
            join_url="https://school.example/lesson",
        )
        raw = encode_snapshot(
            export_snapshot(
                source,
                source_display_label="Учень",
                include_join_links=True,
                confirm_include_join_links=True,
            )
        )
        token, preview = self.app.preview_import(9, raw)
        self.assertIn("окреме підтвердження", preview.text)
        with self.assertRaises(ConfirmationError):
            self.app.confirm_import(9, token)

    def test_reminder_lead_is_limited_to_safe_choices(self) -> None:
        self.app.set_reminder_lead(1, 15)
        self.assertEqual(self.repo.accounts[1].reminder_lead_minutes, 15)
        with self.assertRaises(ValueError):
            self.app.set_reminder_lead(1, 17)

    async def test_export_preview_separates_link_opt_in(self) -> None:
        await self._onboard()
        profile_id = self.app.create_profile(1, "8-А")
        view = self.app.request_profile_export(1, profile_id)
        callbacks = [button.callback_data for row in view.rows for button in row]
        self.assertIn(f"export:file:{profile_id}:0", callbacks)
        self.assertIn(f"export:file:{profile_id}:1", callbacks)
        self.assertTrue(all(item is None or len(item.encode("utf-8")) <= 64 for item in callbacks))


if __name__ == "__main__":
    unittest.main()

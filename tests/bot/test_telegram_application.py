from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, date, datetime, time
from uuid import uuid4

from time_tl.application.schedule_editor import (
    StaleLessonSelection,
    recurring_lesson_fingerprint,
)
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


class AssigningStorageRefRepository(InMemoryAccountRepository):
    """Model PostgreSQL assigning opaque lesson references on every insert."""

    def save(self, account):
        profiles = {}
        for profile_id, profile in account.profiles.items():
            rules = []
            for rule in profile.rules:
                lessons = tuple(
                    replace(
                        lesson,
                        storage_ref=(lesson.storage_ref or f"lesson:rule:{uuid4()}"),
                    )
                    for lesson in rule.lessons
                )
                rules.append(replace(rule, lessons=lessons))
            profiles[profile_id] = replace(profile, rules=tuple(rules))
        account.profiles = profiles
        super().save(account)


class TelegramApplicationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.kyiv = Region("31", "м. Київ")
        self.lviv = Region("46", "Львівська область")
        self.repo = InMemoryAccountRepository()
        self.resolver = FakeResolver(self.kyiv)
        self.app = TelegramApplication(
            regions=(self.kyiv, self.lviv),
            resolver=self.resolver,
            repository=self.repo,
            coordinate_suggestions_enabled=True,
        )

    async def _onboard(self, user_id: int = 1) -> None:
        self.app.choose_mode(user_id, NotificationMode.LESSONS_AND_SIRENS)
        self.app.propose_manual_region(user_id, "31")
        self.app.confirm_region(user_id, confirmed=True)

    def test_first_start_explains_safety_and_offers_exact_three_modes(self) -> None:
        view = self.app.start(1)
        self.assertIn("не замінює офіційні", view.text)
        callbacks = [button.callback_data for row in view.rows for button in row]
        self.assertEqual(callbacks, ["mode:lessons", "mode:lessons_and_sirens", "mode:sirens"])

    def test_settings_privacy_and_notification_hubs_use_existing_controls(self) -> None:
        notifications = self.app.notifications_menu()
        self.assertEqual(
            ["menu:mode", "menu:reminder", "menu:home"],
            [button.callback_data for row in notifications.rows for button in row],
        )

        settings = self.app.settings_menu()
        self.assertEqual(
            ["menu:notifications", "menu:reminder", "settings:privacy", "menu:home"],
            [button.callback_data for row in settings.rows for button in row],
        )

        privacy = self.app.privacy()
        self.assertIn("лише дані, потрібні", privacy.text)
        self.assertIn("Координати", privacy.text)
        self.assertEqual("account:delete:request", privacy.rows[0][0].callback_data)

    def test_region_prompt_offers_location_and_command_free_manual_fallback(self) -> None:
        view = self.app.choose_mode(6, NotificationMode.SIRENS)
        buttons = [button for row in view.rows for button in row]
        self.assertIn("вручну", view.text)
        self.assertIn("не зберігається", view.text)
        self.assertEqual(
            ["📍 Надіслати геолокацію", "🗺 Обрати вручну"],
            [button.label for button in buttons],
        )
        self.assertTrue(buttons[0].request_location)
        self.assertTrue(all(button.callback_data is None for button in buttons))

    async def test_location_suggests_region_without_coordinate_storage_and_requires_confirmation(
        self,
    ) -> None:
        self.app.choose_mode(7, NotificationMode.SIRENS)
        view = await self.app.submit_location(7, 49.8397, 24.0297)
        self.assertIn("не збережено", view.text)
        self.assertIn("Підтвердьте регіон: м. Київ", view.text)
        self.assertEqual([(49.8397, 24.0297)], self.resolver.calls)
        account = self.repo.accounts[7]
        self.assertIsNone(account.region_id)
        self.assertEqual(self.kyiv, self.app._pending_regions[7])
        self.assertNotIn("latitude", vars(account))
        self.assertNotIn("longitude", vars(account))
        self.app.confirm_region(7, confirmed=True)
        self.assertEqual("31", account.region_id)

    async def test_location_capability_is_fail_closed_by_default(self) -> None:
        resolver = FakeResolver(self.kyiv)
        app = TelegramApplication(
            regions=(self.kyiv,),
            resolver=resolver,
            repository=InMemoryAccountRepository(),
        )

        prompt = app.choose_mode(12, NotificationMode.SIRENS)
        self.assertFalse(any(button.request_location for row in prompt.rows for button in row))
        response = await app.submit_location(12, 49.8397, 24.0297)

        self.assertIn("ще не підтримується", response.text)
        self.assertEqual([], resolver.calls)
        self.assertIsNone(app.repository.get_or_create(12).region_id)

    def test_manual_region_requires_confirmation_and_can_be_rejected(self) -> None:
        self.app.propose_manual_region(3, "46")
        view = self.app.confirm_region(3, confirmed=False)
        self.assertIsNone(self.repo.accounts[3].region_id)
        self.assertIn("вручну", view.text)
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

    def test_maximum_import_preview_is_bounded_sanitized_and_revocable(self) -> None:
        source = TelegramApplication(
            regions=(self.kyiv,),
            resolver=self.resolver,
            repository=InMemoryAccountRepository(),
        )
        profile_id = source.create_profile(80, "Максимальний")
        for index in range(100):
            weekday, hour = divmod(index, 15)
            source.add_lesson(
                80,
                profile_id,
                weekday=weekday,
                start=time(hour),
                end=time(hour, 30),
                title=f"Дуже довга назва уроку {index + 1} " + "А" * 100,
            )
        profile = source.repository.get_or_create(80).profiles[profile_id]
        raw = encode_snapshot(
            export_snapshot(
                profile,
                source_display_label="<небезпечне & джерело>" * 5,
            )
        )

        token, preview = self.app.preview_import(90, raw)

        self.assertLessEqual(len(preview.text), 3_500)
        self.assertNotIn("<", preview.text)
        self.assertNotIn(">", preview.text)
        self.assertNotIn("&", preview.text)
        self.assertIn("Попередній перегляд", preview.text)
        for row in preview.rows:
            for button in row:
                if button.callback_data is not None:
                    self.assertLessEqual(len(button.callback_data.encode("utf-8")), 64)
        self.assertIn((90, token), self.app._imports)
        self.app.revoke_import_preview(90, token)
        self.assertNotIn((90, token), self.app._imports)
        with self.assertRaises(ConfirmationError):
            self.app.import_sessions.assert_pending(token, "not-used", datetime.now(UTC))

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

    async def test_edit_toggle_and_delete_use_fresh_selector_and_preserve_active(self) -> None:
        await self._onboard()
        profile_id = self.app.create_profile(1, "8-А")
        profile = self.app.add_lesson(
            1,
            profile_id,
            weekday=0,
            start=time(9),
            end=time(10),
            title="Математика",
            join_url="https://school.example/join",
        )
        fingerprint = recurring_lesson_fingerprint(profile, 0, 0)

        edited = self.app.edit_lesson(
            1,
            profile_id,
            rule_index=0,
            lesson_index=0,
            expected_fingerprint=fingerprint,
            start=time(10),
            end=time(11),
            title="Алгебра",
        )
        lesson = edited.rules[0].lessons[0]
        self.assertTrue(edited.active)
        self.assertEqual((lesson.start, lesson.end, lesson.title), (time(10), time(11), "Алгебра"))
        self.assertEqual(lesson.join_url, "https://school.example/join")
        with self.assertRaises(StaleLessonSelection):
            self.app.delete_lesson(
                1,
                profile_id,
                rule_index=0,
                lesson_index=0,
                expected_fingerprint=fingerprint,
            )

        fingerprint = recurring_lesson_fingerprint(edited, 0, 0)
        toggled = self.app.set_lesson_enabled_by_selector(
            1,
            profile_id,
            rule_index=0,
            lesson_index=0,
            expected_fingerprint=fingerprint,
            enabled=False,
        )
        self.assertFalse(toggled.rules[0].lessons[0].enabled)
        fingerprint = recurring_lesson_fingerprint(toggled, 0, 0)
        deleted = self.app.delete_lesson(
            1,
            profile_id,
            rule_index=0,
            lesson_index=0,
            expected_fingerprint=fingerprint,
        )
        self.assertTrue(deleted.active)
        self.assertEqual(len(deleted.rules), 1)
        self.assertEqual(deleted.rules[0].lessons, ())

    def test_each_mutation_rehydrates_db_assigned_lesson_identity(self) -> None:
        repository = AssigningStorageRefRepository()
        app = TelegramApplication(
            regions=(self.kyiv,), resolver=self.resolver, repository=repository
        )
        profile_id = app.create_profile(77, "DB identity")
        first = app.add_lesson(
            77,
            profile_id,
            weekday=0,
            start=time(9),
            end=time(10),
            title="A",
            join_url="https://school.example/a",
        )
        first_ref = first.rules[0].lessons[0].storage_ref
        self.assertIsNotNone(first_ref)

        second = app.add_lesson(
            77,
            profile_id,
            weekday=0,
            start=time(10),
            end=time(11),
            title="B",
        )
        second_ref = second.rules[0].lessons[1].storage_ref
        self.assertEqual(second.rules[0].lessons[0].storage_ref, first_ref)
        self.assertIsNotNone(second_ref)

        first_fingerprint = recurring_lesson_fingerprint(second, 0, 0)
        edited = app.edit_lesson(
            77,
            profile_id,
            rule_index=0,
            lesson_index=0,
            expected_fingerprint=first_fingerprint,
            start=time(8),
            end=time(9),
            title="A edited",
        )
        self.assertEqual(edited.rules[0].lessons[0].storage_ref, first_ref)
        self.assertEqual(edited.rules[0].lessons[0].join_url, "https://school.example/a")
        self.assertEqual(edited.rules[0].lessons[1].storage_ref, second_ref)

        second_fingerprint = recurring_lesson_fingerprint(edited, 0, 1)
        deleted = app.delete_lesson(
            77,
            profile_id,
            rule_index=0,
            lesson_index=1,
            expected_fingerprint=second_fingerprint,
        )
        self.assertEqual(deleted.rules[0].lessons[0].storage_ref, first_ref)

        first_fingerprint = recurring_lesson_fingerprint(deleted, 0, 0)
        toggled = app.set_lesson_enabled_by_selector(
            77,
            profile_id,
            rule_index=0,
            lesson_index=0,
            expected_fingerprint=first_fingerprint,
            enabled=False,
        )
        self.assertEqual(toggled.rules[0].lessons[0].storage_ref, first_ref)
        self.assertFalse(toggled.rules[0].lessons[0].enabled)

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

    async def test_activation_preview_labels_breaks_and_free_windows(self) -> None:
        await self._onboard()
        self.app.create_profile(1, "Основний")
        candidate = self.app.create_profile(1, "З проміжками")
        for weekday in range(7):
            self.app.add_lesson(
                1,
                candidate,
                weekday=weekday,
                start=time(9),
                end=time(9, 45),
                title="Перший",
            )
            self.app.add_lesson(
                1,
                candidate,
                weekday=weekday,
                start=time(10),
                end=time(10, 15),
                title="Другий",
            )
            self.app.add_lesson(
                1,
                candidate,
                weekday=weekday,
                start=time(11),
                end=time(11, 15),
                title="Третій",
            )

        _, preview = self.app.request_activation(1, candidate)

        self.assertIn("перерва 15 хв", preview.text)
        self.assertIn("вільне вікно 45 хв", preview.text)

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

    async def test_import_without_link_confirmation_strips_join_link(self) -> None:
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
        self.app.confirm_import(9, token)
        imported = next(iter(self.repo.accounts[9].profiles.values()))
        self.assertIsNone(imported.rules[0].lessons[0].join_url)

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

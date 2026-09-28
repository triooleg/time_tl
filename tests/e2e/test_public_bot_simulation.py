from __future__ import annotations

import copy
import unittest
from dataclasses import dataclass
from datetime import date, time

from time_tl.application.schedule_editor import recurring_lesson_fingerprint
from time_tl.bot.async_service import AsyncTelegramApplication
from time_tl.bot.models import NotificationMode
from time_tl.bot.service import Region, UserAccount
from time_tl.domain.schedule import Lesson, Parity, resolve_day


class MemoryAccounts:
    def __init__(self) -> None:
        self.accounts: dict[int, UserAccount] = {}

    async def get_or_create(self, user_id: int) -> UserAccount:
        return copy.deepcopy(self.accounts.setdefault(user_id, UserAccount(user_id)))

    async def get_existing(self, user_id: int) -> UserAccount | None:
        account = self.accounts.get(user_id)
        return copy.deepcopy(account) if account is not None else None

    async def save(self, account: UserAccount) -> None:
        self.accounts[account.user_id] = copy.deepcopy(account)

    async def delete_account(self, user_id: int) -> bool:
        return self.accounts.pop(user_id, None) is not None


class PrivacyResolver:
    """Simulation resolver that deliberately never retains raw coordinates."""

    def __init__(self, result: Region) -> None:
        self.result = result
        self.call_count = 0

    async def resolve(self, latitude: float, longitude: float) -> Region:
        del latitude, longitude
        self.call_count += 1
        return self.result


@dataclass(frozen=True)
class Territory:
    region_id: str
    name: str
    kind: str
    parent_region_id: str | None


class TerritoryTree:
    def __init__(self) -> None:
        self.nodes = {
            "o": Territory("o", "Дніпропетровська область", "OBLAST", None),
            "r": Territory("r", "Кам’янський район", "RAION", "o"),
            "h": Territory("h", "Кам’янська територіальна громада", "HROMADA", "r"),
        }

    async def list_top(self, *, provider: str) -> tuple[Territory, ...]:
        assert provider == "alerts.in.ua"
        return (self.nodes["o"],)

    async def list_children(
        self, *, provider: str, parent_region_id: str
    ) -> tuple[Territory, ...]:
        assert provider == "alerts.in.ua"
        return tuple(
            item for item in self.nodes.values() if item.parent_region_id == parent_region_id
        )

    async def get_path(self, *, provider: str, region_id: str) -> tuple[Territory, ...]:
        assert provider == "alerts.in.ua"
        current = self.nodes[region_id]
        path = []
        while current is not None:
            path.append(current)
            current = (
                self.nodes[current.parent_region_id]
                if current.parent_region_id is not None
                else None
            )
        return tuple(reversed(path))


class PublicBotSimulationE2E(unittest.IsolatedAsyncioTestCase):
    async def test_command_free_onboarding_schedule_restart_import_and_deletion(self) -> None:
        repository = MemoryAccounts()
        suggested = Region("31", "м. Київ")
        resolver = PrivacyResolver(suggested)
        tree = TerritoryTree()
        app = AsyncTelegramApplication(
            regions=(suggested,),
            resolver=resolver,
            repository=repository,
            territory_catalog=tree,
            coordinate_suggestions_enabled=True,
        )
        user_id = 101

        onboarding, modes = await app.start_sequence(user_id)
        self.assertIn("Конфіденційність", onboarding.text)
        self.assertIn("не зберігаються", onboarding.text)
        self.assertIn("не замінює офіційні", modes.text)
        self.assertEqual(
            {"mode:lessons", "mode:lessons_and_sirens", "mode:sirens"},
            {button.callback_data for row in modes.rows for button in row},
        )

        location_prompt = await app.choose_mode(user_id, NotificationMode.LESSONS_AND_SIRENS)
        prompt_buttons = [button for row in location_prompt.rows for button in row]
        self.assertTrue(any(button.request_location for button in prompt_buttons))
        self.assertTrue(any(button.label == "🗺 Обрати вручну" for button in prompt_buttons))

        suggestion = await app.submit_location(user_id, 50.4501, 30.5234)
        self.assertIn("Підтвердьте регіон: м. Київ", suggestion.text)
        self.assertIsNone(repository.accounts[user_id].region_id)
        await app.confirm_region(user_id, confirmed=False)
        self.assertIsNone(repository.accounts[user_id].region_id)
        self.assertEqual(1, resolver.call_count)
        self.assertNotIn("50.4501", repr(app.__dict__))
        self.assertNotIn("30.5234", repr(app.__dict__))
        self.assertNotIn("latitude", repr(repository.accounts[user_id]))
        self.assertNotIn("longitude", repr(repository.accounts[user_id]))

        self.assertIn("Оберіть область", (await app.territory_top()).text)
        self.assertIn("Оберіть район", (await app.territory_open("o")).text)
        self.assertIn("Оберіть громаду", (await app.territory_open("r")).text)
        manual_confirmation = await app.territory_open("h")
        self.assertIn("Підтвердьте територію", manual_confirmation.text)
        self.assertIsNone(repository.accounts[user_id].region_id)
        saved = await app.confirm_catalog_territory(user_id, "h")
        self.assertIn("Територію обрано", saved.text)
        self.assertEqual("h", repository.accounts[user_id].region_id)

        await app.set_reminder_lead(user_id, 15)
        primary = await app.create_profile(user_id, "Основний")
        alternate = await app.create_profile(user_id, "Робоча субота")
        private_link = "https://private.example/lesson-token"
        profile = await app.add_lesson(
            user_id,
            alternate,
            weekday=5,
            start=time(9),
            end=time(9, 45),
            title="Математика",
            week_parity=Parity.EVEN,
            date_parity=Parity.ODD,
            join_url=private_link,
        )
        await app.add_lesson(
            user_id,
            alternate,
            weekday=5,
            start=time(10),
            end=time(10, 15),
            title="Фізика",
            week_parity=Parity.EVEN,
            date_parity=Parity.ODD,
        )
        await app.add_lesson(
            user_id,
            alternate,
            weekday=5,
            start=time(11),
            end=time(11, 30),
            title="Вимкнений",
            enabled=False,
            week_parity=Parity.EVEN,
            date_parity=Parity.ODD,
        )
        fingerprint = recurring_lesson_fingerprint(profile, 0, 0)
        detail = await app.lesson_editor_view(
            user_id,
            alternate,
            rule_index=0,
            lesson_index=0,
            expected_fingerprint=fingerprint,
        )
        self.assertNotIn(private_link, detail.text)
        edited = await app.edit_lesson_title(
            user_id,
            alternate,
            rule_index=0,
            lesson_index=0,
            expected_fingerprint=fingerprint,
            title="Алгебра",
        )
        self.assertEqual("Алгебра", edited.rules[0].lessons[0].title)

        working_saturday = date(2026, 10, 3)  # ISO week 40, odd calendar date.
        recurring = resolve_day(edited, working_saturday)
        self.assertEqual(["Алгебра", "Фізика"], [item.title for item in recurring.enabled_lessons])
        self.assertEqual(15, recurring.gaps[0].minutes)
        override = await app.set_date_lessons(
            user_id,
            alternate,
            working_saturday,
            (Lesson(time(12), time(12, 45), "Перенесений"),),
        )
        self.assertEqual("Перенесений", resolve_day(override, working_saturday).lessons[0].title)
        day_off = date(2026, 10, 10)
        override = await app.set_day_off(user_id, alternate, day_off)
        self.assertEqual((), resolve_day(override, day_off).lessons)

        token, activation = await app.request_activation(user_id, alternate)
        self.assertIn("Попередній перегляд", activation.text)
        self.assertTrue(repository.accounts[user_id].profiles[primary].active)
        await app.confirm_activation(user_id, token)
        self.assertTrue(repository.accounts[user_id].profiles[alternate].active)
        self.assertEqual(15, repository.accounts[user_id].reminder_lead_minutes)

        restarted = AsyncTelegramApplication(
            regions=(suggested,),
            resolver=PrivacyResolver(suggested),
            repository=repository,
            territory_catalog=tree,
            coordinate_suggestions_enabled=True,
        )
        menu = await restarted.menu(user_id)
        self.assertIn("Робоча субота", menu.text)
        self.assertIn("Уроки + тривоги", menu.text)
        self.assertIn("Кам’янська громада", menu.text)

        recipient = 202
        await restarted.start(recipient)
        await restarted.choose_mode(recipient, NotificationMode.LESSONS)
        await restarted.confirm_catalog_territory(recipient, "h")
        exported = await restarted.export_profile(user_id, alternate, include_links=False)
        import_token, import_preview = await restarted.preview_import(recipient, exported)
        self.assertIn("Попередній перегляд 14 днів", import_preview.text)
        self.assertNotIn(private_link, import_preview.text)
        self.assertEqual({}, repository.accounts[recipient].profiles)
        await restarted.confirm_import(recipient, import_token)
        self.assertEqual(1, len(repository.accounts[recipient].profiles))

        deletion = await restarted.request_data_deletion(user_id)
        delete_token = deletion.rows[0][0].callback_data
        assert delete_token is not None
        delete_token = delete_token.rsplit(":", 1)[1]
        await restarted.review_data_deletion(user_id, delete_token)
        result = await restarted.confirm_data_deletion(user_id, delete_token)
        self.assertIn("Ваші дані видалено", result.text)
        self.assertNotIn(user_id, repository.accounts)

        public_copy = "\n".join(
            (onboarding.text, modes.text, location_prompt.text, manual_confirmation.text, menu.text)
        )
        self.assertNotIn("Настройка", public_copy)
        self.assertNotIn("Расписание", public_copy)
        self.assertNotIn("Удалить", public_copy)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import copy
import unittest
from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace
from typing import Any

from time_tl.bot.service import Region, UserAccount
from time_tl.domain.schedule import ConfirmationError, ScheduleProfile
from time_tl.runtime.imports import (
    DurableAsyncTelegramApplication,
    DurableImportCoordinator,
)


class AsyncAccounts:
    def __init__(self) -> None:
        self.accounts: dict[int, UserAccount] = {}

    async def get_or_create(self, user_id: int) -> UserAccount:
        return copy.deepcopy(self.accounts.setdefault(user_id, UserAccount(user_id)))

    async def save(self, account: UserAccount) -> None:
        self.accounts[account.user_id] = copy.deepcopy(account)


@dataclass
class Stored:
    user_id: int
    fingerprint: str
    payload: dict[str, Any]
    consumed: bool = False
    replacement_token: str | None = None
    replace_profile_id: str | None = None


class DurableSessions:
    def __init__(self, shared: dict[str, Stored], user_id: int) -> None:
        self.shared = shared
        self.user_id = user_id

    async def create(self, pending: Any, proposal_payload: dict[str, Any]) -> None:
        self.shared[pending.token] = Stored(
            self.user_id, pending.fingerprint, copy.deepcopy(proposal_payload)
        )

    async def load(self, token: str, now: datetime) -> Any:
        del now
        row = self.shared.get(token)
        if row is None or row.user_id != self.user_id:
            raise ConfirmationError("import proposal is missing")
        if row.consumed:
            raise ConfirmationError("import proposal was already consumed")
        return SimpleNamespace(payload=copy.deepcopy(row.payload))

    async def bind_replacement(
        self,
        token: str,
        fingerprint: str,
        replacement_token: str,
        replace_profile_id: str,
        now: datetime,
    ) -> None:
        row = self.shared.get(token)
        if row is None or row.user_id != self.user_id:
            raise ConfirmationError("import proposal is missing")
        if row.fingerprint != fingerprint or row.consumed:
            raise ConfirmationError("import proposal is stale")
        row.replacement_token = replacement_token
        row.replace_profile_id = replace_profile_id

    async def consume_by_replacement_token(
        self, replacement_token: str, now: datetime, operation: Any
    ) -> Any:
        row = next(
            (
                item
                for item in self.shared.values()
                if item.user_id == self.user_id and item.replacement_token == replacement_token
            ),
            None,
        )
        if row is None or row.consumed:
            raise ConfirmationError("replacement proposal is missing")
        durable = SimpleNamespace(
            payload=copy.deepcopy(row.payload),
            replace_profile_id=row.replace_profile_id,
        )
        result = await operation(None, durable)
        row.consumed = True
        return result

    async def consume_atomically(
        self, token: str, fingerprint: str, now: datetime, operation: Any, **_kwargs: Any
    ) -> Any:
        durable = await self.load(token, now)
        row = self.shared[token]
        if row.fingerprint != fingerprint:
            raise ConfirmationError("import proposal was altered")
        result = await operation(None, durable)
        row.consumed = True
        return result


class TransactionalProfiles:
    def __init__(self, accounts: AsyncAccounts) -> None:
        self.accounts = accounts

    async def load(self, _session: Any, user_id: int) -> UserAccount:
        return await self.accounts.get_or_create(user_id)

    async def save_profiles(self, _session: Any, user_id: int, profiles: dict[str, Any]) -> None:
        account = await self.accounts.get_or_create(user_id)
        account.profiles = copy.deepcopy(profiles)
        await self.accounts.save(account)


class Resolver:
    async def resolve(self, _latitude: float, _longitude: float) -> None:
        return None


SNAPSHOT = (
    '{"schema_version":"1.0","metadata":{"template_name":"8-A",'
    '"created_at":"2026-09-20T00:00:00+00:00",'
    '"source_display_label":"classmate","included_fields":[]},'
    '"profile":{"name":"8-A","timezone":"Europe/Kyiv","rules":[],"overrides":[]}}'
)


class DurableImportRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.accounts = AsyncAccounts()
        self.sessions: dict[str, Stored] = {}

    def app(self) -> DurableAsyncTelegramApplication:
        coordinator = DurableImportCoordinator(
            lambda user_id: DurableSessions(self.sessions, user_id),
            TransactionalProfiles(self.accounts),  # type: ignore[arg-type]
        )
        return DurableAsyncTelegramApplication(
            regions=(Region("31", "Київ"),),
            resolver=Resolver(),
            repository=self.accounts,
            durable_imports=coordinator,
        )

    async def test_confirmation_survives_application_restart(self) -> None:
        token, _ = await self.app().preview_import(101, SNAPSHOT)
        result = await self.app().confirm_import(101, token)
        self.assertIn("Імпортовано", result.text)
        self.assertEqual(1, len(self.accounts.accounts[101].profiles))

    async def test_replay_is_rejected_after_restart(self) -> None:
        token, _ = await self.app().preview_import(101, SNAPSHOT)
        await self.app().confirm_import(101, token)
        with self.assertRaisesRegex(ConfirmationError, "already consumed"):
            await self.app().confirm_import(101, token)
        self.assertEqual(1, len(self.accounts.accounts[101].profiles))

    async def test_replacement_confirmation_survives_restart(self) -> None:
        self.accounts.accounts[101] = UserAccount(
            101,
            profiles={
                f"p{i}": ScheduleProfile(f"p{i}", f"Profile {i}", active=i == 0) for i in range(3)
            },
        )
        first = self.app()
        import_token, _ = await first.preview_import(101, SNAPSHOT)
        replacement_token, _ = await first.request_import_replacement(101, import_token, "p1")
        result = await self.app().confirm_import_replacement(101, replacement_token)
        self.assertIn("Замінено", result.text)
        names = {item.name for item in self.accounts.accounts[101].profiles.values()}
        self.assertNotIn("Profile 1", names)
        self.assertIn("8-A", names)

    async def test_token_is_bound_to_telegram_user(self) -> None:
        token, _ = await self.app().preview_import(101, SNAPSHOT)
        with self.assertRaisesRegex(ConfirmationError, "missing"):
            await self.app().confirm_import(202, token)
        self.assertNotIn(202, self.accounts.accounts)


if __name__ == "__main__":
    unittest.main()

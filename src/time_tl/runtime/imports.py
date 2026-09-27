"""Durable async import boundary used by the live Telegram application."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

from time_tl.bot.async_service import AsyncTelegramApplication
from time_tl.bot.models import Button, View
from time_tl.domain.schedule import (
    ImportProposal,
    InMemoryImportSessionRepository,
    PendingImportSession,
    ProfileCollection,
    ReplacementProposal,
    confirm_import,
    confirm_import_replacement,
    export_snapshot,
    propose_import,
    request_import_replacement,
)
from time_tl.domain.schedule.profiles import ConfirmationError


class AsyncImportSessions(Protocol):
    async def create(
        self, pending: PendingImportSession, proposal_payload: Mapping[str, Any]
    ) -> None: ...
    async def load(self, token: str, now: datetime) -> Any: ...
    async def bind_replacement(
        self,
        token: str,
        fingerprint: str,
        replacement_token: str,
        replace_profile_id: str,
        now: datetime,
    ) -> None: ...
    async def consume_atomically(
        self,
        token: str,
        fingerprint: str,
        now: datetime,
        operation: Any,
        *,
        replacement_token: str | None = None,
        replace_profile_id: str | None = None,
    ) -> Any: ...
    async def consume_by_replacement_token(
        self, replacement_token: str, now: datetime, operation: Any
    ) -> Any: ...


class _CaptureSessionRepository:
    """Capture parsing output only; it is never an authorization store."""

    def __init__(self) -> None:
        self.pending: PendingImportSession | None = None

    def create(self, pending: PendingImportSession) -> None:
        self.pending = pending

    def assert_pending(self, *_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("capture repository only supports create")

    def bind_replacement(self, *_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("capture repository only supports create")

    def consume_atomically(self, *_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("capture repository only supports create")

    def revoke(self, *_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("capture repository only supports create")


def _decoded_payload(raw: str | bytes | Mapping[str, Any]) -> Mapping[str, Any]:
    if isinstance(raw, Mapping):
        return copy.deepcopy(dict(raw))
    payload = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    decoded = json.loads(payload)
    if not isinstance(decoded, dict):
        raise ValueError("snapshot root must be an object")
    return decoded


def _proposal_from_payload(
    payload: Mapping[str, Any],
    *,
    token: str | None = None,
    now: datetime | None = None,
) -> tuple[ImportProposal, PendingImportSession]:
    capture = _CaptureSessionRepository()
    proposal = propose_import(payload, sessions=capture, now=now)
    assert capture.pending is not None
    if token is None:
        return proposal, capture.pending
    return replace(proposal, token=token), replace(capture.pending, token=token)


def _unsigned_payload(proposal: ImportProposal) -> Mapping[str, Any]:
    """Rebuild only the reviewed share fields after domain validation."""
    created_at = datetime.fromisoformat(proposal.created_at)
    return export_snapshot(
        proposal.profile,
        source_display_label=proposal.source_display_label,
        created_at=created_at,
        expires_at=proposal.expires_at,
        include_join_links=proposal.links_present,
        confirm_include_join_links=proposal.links_present,
    )


class PostgresTransactionalProfileStore:
    """Persist imported profiles in the import-consumption transaction."""

    def __init__(self, account_repository: Any) -> None:
        self._accounts = account_repository

    async def load(self, session: Any, telegram_user_id: int) -> Any:
        return await self._accounts.load_for_update_in_session(session, telegram_user_id)

    async def save_profiles(
        self, session: Any, telegram_user_id: int, profiles: dict[str, Any]
    ) -> None:
        await self._accounts.sync_profiles_in_session(session, telegram_user_id, profiles)


class DurableImportCoordinator:
    """DB authorization and profile mutation commit in the same transaction."""

    def __init__(
        self,
        session_factory: Callable[[int], AsyncImportSessions],
        profile_store: PostgresTransactionalProfileStore,
    ) -> None:
        self._sessions = session_factory
        self._profiles = profile_store

    async def preview(self, user_id: int, raw: str | bytes | Mapping[str, Any]) -> ImportProposal:
        proposal, pending = _proposal_from_payload(_decoded_payload(raw))
        await self._sessions(user_id).create(pending, _unsigned_payload(proposal))
        return proposal

    async def confirm(self, user_id: int, token: str, *, confirm_links: bool = False) -> Any:
        sessions = self._sessions(user_id)
        checked_at = datetime.now(UTC)
        durable = await sessions.load(token, checked_at)
        restored, _ = _proposal_from_payload(durable.payload, token=token, now=checked_at)

        async def operation(session: Any, locked: Any) -> Any:
            proposal, pending = _proposal_from_payload(locked.payload, token=token, now=checked_at)
            local = InMemoryImportSessionRepository()
            local.create(pending)
            account = await self._profiles.load(session, user_id)
            collection = ProfileCollection(tuple(account.profiles.values()))
            stored = confirm_import(
                collection,
                proposal,
                sessions=local,
                confirmed=True,
                confirm_include_join_links=confirm_links,
                now=checked_at,
            )
            account.profiles = {item.profile_id: item for item in collection.profiles}
            await self._profiles.save_profiles(session, user_id, account.profiles)
            return stored

        return await sessions.consume_atomically(token, restored.fingerprint, checked_at, operation)

    async def request_replacement(
        self,
        user_id: int,
        import_token: str,
        replace_profile_id: str,
        *,
        confirm_links: bool = False,
    ) -> Any:
        sessions = self._sessions(user_id)
        checked_at = datetime.now(UTC)
        durable = await sessions.load(import_token, checked_at)
        proposal, pending = _proposal_from_payload(
            durable.payload, token=import_token, now=checked_at
        )
        del pending
        choice = "links" if confirm_links else "nolinks"
        replacement = ReplacementProposal(
            f"{choice}-{uuid4().hex}",
            proposal,
            replace_profile_id,
            include_join_links=confirm_links,
        )
        await sessions.bind_replacement(
            import_token,
            proposal.fingerprint,
            replacement.token,
            replace_profile_id,
            checked_at,
        )
        return replacement

    async def confirm_replacement(self, user_id: int, replacement_token: str) -> Any:
        sessions = self._sessions(user_id)
        checked_at = datetime.now(UTC)
        if replacement_token.startswith("links-"):
            include_join_links = True
        elif replacement_token.startswith("nolinks-"):
            include_join_links = False
        else:
            raise ConfirmationError("replacement proposal choice is invalid")

        async def operation(session: Any, locked: Any) -> Any:
            if locked.replace_profile_id is None:
                raise RuntimeError("durable replacement target is missing")
            proposal, pending = _proposal_from_payload(
                locked.payload, token="restored-import", now=checked_at
            )
            local = InMemoryImportSessionRepository()
            local.create(pending)
            account = await self._profiles.load(session, user_id)
            collection = ProfileCollection(tuple(account.profiles.values()))
            local_replacement = request_import_replacement(
                collection,
                proposal,
                sessions=local,
                replace_profile_id=locked.replace_profile_id,
                confirm_include_join_links=include_join_links,
                now=checked_at,
            )
            stored = confirm_import_replacement(
                collection,
                local_replacement,
                sessions=local,
                confirmed=True,
                now=checked_at,
            )
            account.profiles = {item.profile_id: item for item in collection.profiles}
            await self._profiles.save_profiles(session, user_id, account.profiles)
            return stored

        return await sessions.consume_by_replacement_token(replacement_token, checked_at, operation)


class DurableAsyncTelegramApplication(AsyncTelegramApplication):
    """Async Telegram facade whose live schedule imports survive restarts."""

    def __init__(self, *, durable_imports: DurableImportCoordinator, **kwargs: Any) -> None:
        super().__init__(allow_process_local_imports=False, **kwargs)
        self._durable_imports = durable_imports

    async def preview_import(self, user_id: int, raw: str | bytes) -> tuple[str, View]:
        await self._hydrate(user_id)
        proposal = await self._durable_imports.preview(user_id, raw)
        links = "так — потрібне окреме підтвердження" if proposal.links_present else "ні"
        lines = [
            f"Шаблон: {proposal.profile.name}",
            f"Джерело: {proposal.source_display_label or 'не вказано'}",
            f"Створено: {proposal.created_at or 'не вказано'}",
            f"Посилання на уроки: {links}",
            "Попередній перегляд 14 днів:",
            *self._app._preview_lines(proposal.resolved_preview),
        ]
        return proposal.token, View(
            "\n".join(lines),
            (
                (
                    Button("✅ Імпортувати", f"import:confirm:{proposal.token}"),
                    Button("Скасувати", "menu:schedules"),
                ),
            ),
        )

    async def confirm_import(
        self, user_id: int, token: str, *, confirm_links: bool = False
    ) -> View:
        async with self._lock(user_id):
            stored = await self._durable_imports.confirm(
                user_id, token, confirm_links=confirm_links
            )
            return await self.menu(user_id, prefix=f"Імпортовано незалежну копію «{stored.name}».")

    async def request_import_replacement(
        self,
        user_id: int,
        import_token: str,
        replace_profile_id: str,
        *,
        confirm_links: bool = False,
    ) -> tuple[str, View]:
        replacement = await self._durable_imports.request_replacement(
            user_id,
            import_token,
            replace_profile_id,
            confirm_links=confirm_links,
        )
        account = await self._hydrate(user_id)
        profile_name = account.profiles[replace_profile_id].name
        return replacement.token, View(
            f"Замінити «{profile_name}» імпортованим розкладом "
            f"«{replacement.import_proposal.profile.name}»?",
            (
                (
                    Button("✅ Замінити", f"replace:confirm:{replacement.token}"),
                    Button("Скасувати", "menu:schedules"),
                ),
            ),
        )

    async def confirm_import_replacement(self, user_id: int, token: str) -> View:
        async with self._lock(user_id):
            stored = await self._durable_imports.confirm_replacement(user_id, token)
            return await self.menu(user_id, prefix=f"Замінено розклад на «{stored.name}».")

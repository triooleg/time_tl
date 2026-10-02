from __future__ import annotations

import asyncio
import copy
from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
from aiogram import Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Message, User

from time_tl.bot.calendar_router import (
    CalendarUx,
    _callback,
    _Invitation,
    _parse,
    _Pending,
    build_calendar_router,
)
from time_tl.bot.models import View
from time_tl.bot.service import UserAccount
from time_tl.domain.calendar_memberships import (
    CalendarAccessDenied,
    CalendarConflict,
    CalendarDataUnavailable,
    CalendarLimitExceeded,
    CalendarRole,
    CalendarSummary,
    InvitationPreview,
    deserialize_profile,
    serialize_profile,
)
from time_tl.domain.calendars import CalendarKind
from time_tl.domain.schedule import Lesson, ScheduleProfile, ScheduleRule

NOW = datetime(2026, 10, 3, 8, tzinfo=UTC)
TARGET = 123456789
SECRET = "private_invitation_secret_12345678901234567890"


class Accounts:
    def __init__(self) -> None:
        self.items = {
            actor: UserAccount(
                actor,
                profiles={
                    "active": ScheduleProfile(
                        "active",
                        "Математика",
                        active=True,
                        rules=(
                            ScheduleRule(
                                0,
                                (
                                    Lesson(
                                        time(9),
                                        time(9, 45),
                                        "Урок",
                                        join_url="https://example.invalid/private",
                                    ),
                                ),
                            ),
                        ),
                    )
                },
            )
            for actor in (1, 2, 3, TARGET)
        }
        self.calls: list[int] = []

    async def get_existing(self, actor: int) -> UserAccount | None:
        self.calls.append(actor)
        return copy.deepcopy(self.items.get(actor))


class Port:
    def __init__(self) -> None:
        self.rows: dict[UUID, CalendarSummary] = {}
        self.profiles: dict[UUID, ScheduleProfile] = {}
        self.members: dict[tuple[int, UUID], CalendarRole] = {}
        self.invites: dict[str, tuple[int, UUID]] = {}
        self.writes: list[str] = []

    def row(self, actor: int, identity: UUID, version: int | None = None) -> CalendarSummary:
        item = self.rows[identity]
        role = CalendarRole.OWNER if actor == 1 else self.members.get((actor, identity))
        if role is None:
            raise CalendarAccessDenied()
        if version is not None and item.version != version:
            raise CalendarConflict()
        return replace(item, role=role)

    async def list_calendars(self, actor: int) -> tuple[CalendarSummary, ...]:
        return tuple(
            self.row(actor, identity)
            for identity in self.rows
            if actor == 1 or (actor, identity) in self.members
        )

    async def export_calendars(self, actor: int) -> tuple[dict[str, object], ...]:
        return tuple(
            {
                "calendar_id": str(item.calendar_id),
                "name": item.name,
                "kind": item.kind.value,
                "role": item.role.value,
                "visible": item.visible,
                "version": item.version,
                "snapshot": serialize_profile(self.profiles[item.calendar_id]),
            }
            for item in await self.list_calendars(actor)
        )

    async def get_snapshot(
        self, actor: int, identity: UUID
    ) -> tuple[CalendarSummary, ScheduleProfile]:
        return self.row(actor, identity), self.profiles[identity]

    async def create_calendar(
        self, actor: int, profile: ScheduleProfile, kind: CalendarKind
    ) -> CalendarSummary:
        assert actor == 1
        identity = uuid4()
        item = CalendarSummary(identity, profile.name, kind, CalendarRole.OWNER, True, 1)
        self.rows[identity] = item
        self.profiles[identity] = deserialize_profile(
            serialize_profile(profile), identity, profile.name
        )
        self.writes.append("create")
        return item

    async def set_visible(
        self, actor: int, identity: UUID, expected_version: int, visible: bool
    ) -> CalendarSummary:
        self.row(actor, identity, expected_version)
        self.rows[identity] = replace(
            self.rows[identity], visible=visible, version=expected_version + 1
        )
        self.writes.append("visibility")
        return self.row(actor, identity)

    async def delete_calendar(self, actor: int, identity: UUID, expected_version: int) -> None:
        assert self.row(actor, identity, expected_version).role is CalendarRole.OWNER
        del self.rows[identity]
        self.writes.append("delete")

    async def leave_calendar(self, actor: int, identity: UUID, expected_version: int) -> None:
        assert self.row(actor, identity, expected_version).role is not CalendarRole.OWNER
        del self.members[actor, identity]
        self.rows[identity] = replace(self.rows[identity], version=expected_version + 1)
        self.writes.append("leave")

    async def create_invitation(
        self, actor: int, identity: UUID, target: int, expected_version: int, now: datetime
    ) -> str:
        assert self.row(actor, identity, expected_version).role is CalendarRole.OWNER
        self.invites[SECRET] = (target, identity)
        self.rows[identity] = replace(self.rows[identity], version=expected_version + 1)
        self.writes.append("invite")
        return SECRET

    async def preview_invitation(self, target: int, token: str, now: datetime) -> InvitationPreview:
        expected, identity = self.invites[token]
        if target != expected:
            raise CalendarAccessDenied()
        return InvitationPreview(
            replace(self.rows[identity], role=CalendarRole.MEMBER), now + timedelta(hours=24)
        )

    async def accept_invitation(
        self, target: int, token: str, expected_version: int, now: datetime
    ) -> CalendarSummary:
        expected, identity = self.invites[token]
        assert expected == target
        if self.rows[identity].version != expected_version:
            raise CalendarConflict()
        del self.invites[token]
        self.members[target, identity] = CalendarRole.MEMBER
        self.rows[identity] = replace(self.rows[identity], version=expected_version + 1)
        self.writes.append("accept")
        return self.row(target, identity)

    async def revoke_invitation(self, actor: int, token: str, expected_version: int) -> None:
        _, identity = self.invites[token]
        assert self.row(actor, identity, expected_version).role is CalendarRole.OWNER
        del self.invites[token]
        self.rows[identity] = replace(self.rows[identity], version=expected_version + 1)
        self.writes.append("revoke")


def ux() -> tuple[CalendarUx, Port, Accounts]:
    accounts, port = Accounts(), Port()
    return CalendarUx(port, accounts, now=lambda: NOW), port, accounts


def button(view: View, action: str) -> str:
    return next(
        item.callback_data
        for row in view.rows
        for item in row
        if item.callback_data and item.callback_data.startswith("cal:" + action + ":")
    )


def handle(view: View) -> UUID:
    return _parse(button(view, "ok"))[0]


async def created(instance: CalendarUx, port: Port) -> CalendarSummary:
    await instance.confirm(1, handle(await instance.create_preview(1)))
    return next(iter(port.rows.values()))


async def invited(instance: CalendarUx, port: Port) -> UUID:
    item = await created(instance, port)
    identity, _ = await instance.invitation_target_prompt(1, item.calendar_id, item.version)
    preview = await instance.invitation_target(1, identity, str(TARGET))
    await instance.confirm(1, handle(preview))
    return next(iter(instance._invitations))


@pytest.mark.asyncio
async def test_creation_requires_preview_confirmation_and_independent_link_free_copy() -> None:
    instance, port, accounts = ux()
    original = copy.deepcopy(accounts.items)
    preview = await instance.create_preview(1)
    assert not port.writes
    assert "не зміниться" in preview.text
    assert "повторитися" in preview.text
    await instance.confirm(1, handle(preview))
    assert port.writes == ["create"]
    assert accounts.items == original
    profile = next(iter(port.profiles.values()))
    assert profile.active is False
    assert profile.rules[0].lessons[0].join_url is None
    with pytest.raises(CalendarDataUnavailable):
        await instance.confirm(1, handle(preview))
    assert port.writes == ["create"]


@pytest.mark.asyncio
async def test_stale_active_source_fingerprint_rejects_without_writes() -> None:
    instance, port, accounts = ux()
    preview = await instance.create_preview(1)
    old = accounts.items[1].profiles["active"]
    accounts.items[1].profiles["active"] = replace(old, name="Змінений")
    with pytest.raises(CalendarConflict):
        await instance.confirm(1, handle(preview))
    assert not port.writes


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["missing", "zero", "multiple", "wrong_owner"])
async def test_account_and_active_source_fail_closed(case: str) -> None:
    instance, port, accounts = ux()
    if case == "missing":
        del accounts.items[1]
    elif case == "zero":
        accounts.items[1].profiles.clear()
    elif case == "multiple":
        accounts.items[1].profiles["other"] = replace(
            accounts.items[1].profiles["active"], profile_id="other"
        )
    else:
        accounts.items[1].user_id = 2
    with pytest.raises(CalendarDataUnavailable):
        await instance.create_preview(1)
    assert not port.writes


@pytest.mark.asyncio
async def test_foreign_confirmation_does_not_consume_owner_pending() -> None:
    instance, port, _ = ux()
    preview = await instance.create_preview(1)
    with pytest.raises(CalendarDataUnavailable):
        await instance.confirm(2, handle(preview))
    await instance.confirm(1, handle(preview))
    assert port.writes == ["create"]


@pytest.mark.asyncio
async def test_concurrent_creation_confirm_is_single_use() -> None:
    instance, port, _ = ux()
    preview = await instance.create_preview(1)
    results = await asyncio.gather(
        instance.confirm(1, handle(preview)),
        instance.confirm(1, handle(preview)),
        return_exceptions=True,
    )
    assert len([item for item in results if isinstance(item, View)]) == 1
    assert port.writes == ["create"]


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", [True, False])
async def test_removal_requires_distinct_warning_confirmation_and_cas(owner: bool) -> None:
    instance, port, _ = ux()
    item = await created(instance, port)
    port.members[2, item.calendar_id] = CalendarRole.MEMBER
    actor = 1 if owner else 2
    view = await instance.removal_preview(actor, item.calendar_id, item.version)
    assert "для всіх" in view.text if owner else "інших учасників" in view.text
    assert port.writes == ["create"]
    await instance.confirm(actor, handle(view))
    assert port.writes[-1] == ("delete" if owner else "leave")


@pytest.mark.asyncio
async def test_visibility_stale_and_foreign_callbacks_reject() -> None:
    instance, port, _ = ux()
    item = await created(instance, port)
    with pytest.raises(CalendarAccessDenied):
        await instance.visibility(2, item.calendar_id, item.version)
    await instance.visibility(1, item.calendar_id, item.version)
    assert not port.rows[item.calendar_id].visible
    with pytest.raises(CalendarConflict):
        await instance.visibility(1, item.calendar_id, item.version)
    assert port.writes == ["create", "visibility"]


@pytest.mark.asyncio
async def test_invitation_secret_target_hidden_and_accept_requires_own_preview() -> None:
    instance, port, _ = ux()
    invitation = await invited(instance, port)
    assert port.writes == ["create", "invite"]
    listing = await instance.listing(TARGET)
    for exposed in (
        listing.text,
        repr(listing.rows),
        repr(instance._invitations),
        repr(instance._pending),
    ):
        assert SECRET not in exposed
        assert str(TARGET) not in exposed
    assert "до 10 хвилин" in listing.text
    preview = await instance.invitation_preview(TARGET, invitation)
    assert port.writes == ["create", "invite"]
    assert "не незалежна копія" in preview.text
    assert "учасник" in preview.text
    with pytest.raises(CalendarAccessDenied):
        await instance.invitation_preview(2, invitation)
    await instance.confirm(TARGET, handle(preview))
    assert port.writes == ["create", "invite", "accept"]
    with pytest.raises(CalendarDataUnavailable):
        await instance.confirm(TARGET, handle(preview))


@pytest.mark.asyncio
async def test_invitation_expiry_restart_and_stale_accept_fail_closed() -> None:
    instance, port, accounts = ux()
    identity = await invited(instance, port)
    preview = await instance.invitation_preview(TARGET, identity)
    row = next(iter(port.rows.values()))
    port.rows[row.calendar_id] = replace(row, version=row.version + 1)
    with pytest.raises(CalendarConflict):
        await instance.confirm(TARGET, handle(preview))
    restarted = CalendarUx(port, accounts, now=lambda: NOW)
    with pytest.raises(CalendarAccessDenied):
        await restarted.invitation_preview(TARGET, identity)
    instance.now = lambda: NOW + timedelta(minutes=10)
    with pytest.raises(CalendarAccessDenied):
        await instance.invitation_preview(TARGET, identity)
    assert "accept" not in port.writes
    assert not instance._invitations


@pytest.mark.asyncio
async def test_owner_revoke_requires_confirmation_and_recipient_cannot_revoke() -> None:
    instance, port, _ = ux()
    invitation = await invited(instance, port)
    with pytest.raises(CalendarAccessDenied):
        await instance.revoke_preview(TARGET, invitation)
    view = await instance.revoke_preview(1, invitation)
    assert "revoke" not in port.writes
    await instance.confirm(1, handle(view))
    assert port.writes[-1] == "revoke"
    with pytest.raises(CalendarAccessDenied):
        await instance.invitation_preview(TARGET, invitation)


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["999", "1", "-1", "１２３", "0", "x" * 100, str(2**63)])
async def test_target_must_be_existing_other_user_without_echo(target: str) -> None:
    instance, port, _ = ux()
    item = await created(instance, port)
    identity, _ = await instance.invitation_target_prompt(1, item.calendar_id, item.version)
    with pytest.raises((CalendarDataUnavailable, CalendarAccessDenied)):
        await instance.invitation_target(1, identity, target)
    assert port.writes == ["create"]


@pytest.mark.asyncio
async def test_non_owner_cannot_invite_and_cancellation_does_not_mutate() -> None:
    instance, port, _ = ux()
    item = await created(instance, port)
    port.members[2, item.calendar_id] = CalendarRole.ADMIN
    with pytest.raises(CalendarAccessDenied):
        await instance.invitation_target_prompt(2, item.calendar_id, item.version)
    preview = await instance.removal_preview(1, item.calendar_id, item.version)
    await instance.cancel(1, handle(preview))
    with pytest.raises(CalendarDataUnavailable):
        await instance.confirm(1, handle(preview))
    assert port.writes == ["create"]


@pytest.mark.asyncio
async def test_pending_per_user_global_caps_and_expired_eviction() -> None:
    instance, _, _ = ux()
    for _ in range(3):
        await instance.create_preview(1)
    with pytest.raises(CalendarLimitExceeded):
        await instance.create_preview(1)
    instance.now = lambda: NOW + timedelta(minutes=10)
    await instance.create_preview(1)
    assert len(instance._pending) == 1
    with patch("time_tl.bot.calendar_router._GLOBAL_LIMIT", 1):
        with pytest.raises(CalendarLimitExceeded):
            await instance.create_preview(2)


@pytest.mark.parametrize(
    "raw", ["cal:ok:bad", "cal:ok:" + "a" * 33, "cal:ok:" + "A" * 32, "cal:ok:" + "a" * 32 + ":x"]
)
def test_malformed_handles_reject(raw: str) -> None:
    with pytest.raises(CalendarDataUnavailable):
        _parse(raw)


@pytest.mark.parametrize("version", ["0", "-1", "١", "2147483647", "000000000001", "x"])
def test_version_parser_is_strict_bounded_ascii(version: str) -> None:
    with pytest.raises(CalendarDataUnavailable):
        _parse("cal:d:" + "a" * 32 + ":" + version, versioned=True)


def test_callbacks_fit_64_bytes_even_max_cas() -> None:
    for action in ("d", "vis", "remove", "invite", "ok", "no", "ip", "rp"):
        assert len(_callback(action, uuid4(), 2**31 - 2).encode()) <= 64


def event(actor: int, data: str) -> CallbackQuery:
    return CallbackQuery(
        id="test",
        from_user=User(id=actor, is_bot=False, first_name="User"),
        chat_instance="test",
        data=data,
        message=Message(message_id=1, date=NOW, chat=Chat(id=actor, type="private")),
    )


def state(actor: int = 1) -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=actor, user_id=actor)
    )


@pytest.mark.asyncio
async def test_default_off_factory_registers_nothing() -> None:
    instance, _, _ = ux()
    router = Router()
    assert build_calendar_router(instance, router=router) is None
    assert not router.callback_query.handlers
    assert not router.message.handlers


@pytest.mark.asyncio
async def test_actual_router_creation_confirm_and_replay() -> None:
    instance, port, _ = ux()
    with patch("time_tl.bot.aiogram_adapter._edit", new_callable=AsyncMock) as edit:
        router = build_calendar_router(instance, enabled=True)
        assert router is not None
        context = state()
        await router.propagate_event("callback_query", event(1, "cal:new"), state=context)
        preview = edit.call_args.args[1]
        assert not port.writes
        await router.propagate_event(
            "callback_query", event(1, button(preview, "ok")), state=context
        )
        assert port.writes == ["create"]
        await router.propagate_event(
            "callback_query", event(1, button(preview, "ok")), state=context
        )
        assert "застаріло" in edit.call_args.args[1].text
        assert port.writes == ["create"]


@pytest.mark.asyncio
async def test_actual_invitation_target_fsm_keeps_only_opaque_handle() -> None:
    instance, port, _ = ux()
    item = await created(instance, port)
    with (
        patch("time_tl.bot.aiogram_adapter._edit", new_callable=AsyncMock),
        patch("time_tl.bot.aiogram_adapter._answer", new_callable=AsyncMock) as answer,
    ):
        router = build_calendar_router(instance, enabled=True)
        assert router is not None
        context = state()
        await router.propagate_event(
            "callback_query",
            event(1, _callback("invite", item.calendar_id, item.version)),
            state=context,
        )
        assert set(await context.get_data()) == {"calendar_handle"}
        assert str(TARGET) not in repr(await context.get_data())
        assert SECRET not in repr(await context.get_data())
        message = Message(
            message_id=2,
            date=NOW,
            chat=Chat(id=1, type="private"),
            from_user=User(id=1, is_bot=False, first_name="User"),
            text=str(TARGET),
        )
        await router.propagate_event(
            "message", message, state=context, raw_state=await context.get_state()
        )
        assert await context.get_data() == {}
        assert await context.get_state() is None
        assert str(TARGET) not in answer.call_args.args[1].text
        assert port.writes == ["create"]


@pytest.mark.asyncio
async def test_actual_router_read_failure_missing_account_and_malformed_callback_are_generic() -> (
    None
):
    instance, port, accounts = ux()
    del accounts.items[1]
    with patch("time_tl.bot.aiogram_adapter._edit", new_callable=AsyncMock) as edit:
        router = build_calendar_router(instance, enabled=True)
        assert router is not None
        for data in ("menu:calendars", "cal:ok:bad"):
            await router.propagate_event("callback_query", event(1, data), state=state())
            assert "недоступна" in edit.call_args.args[1].text
        accounts.items[1] = UserAccount(1)
        port.list_calendars = AsyncMock(side_effect=RuntimeError("private-secret"))  # type: ignore[method-assign]
        await router.propagate_event("callback_query", event(1, "menu:calendars"), state=state())
        assert "private-secret" not in edit.call_args.args[1].text
        assert "Календарів ще немає" not in edit.call_args.args[1].text


@pytest.mark.asyncio
async def test_atomic_target_reservation_prevents_parallel_owner_capacity_race() -> None:
    instance, port, _ = ux()
    identity = uuid4()
    owner = CalendarSummary(identity, "Name", CalendarKind.PERSONAL, CalendarRole.OWNER, True, 1)
    port.list_calendars = AsyncMock(return_value=(owner,))  # type: ignore[method-assign]
    for actor in (1, 2):
        instance._invitations[uuid4()] = _Invitation(
            actor, TARGET, SECRET, identity, NOW + timedelta(minutes=10)
        )
    started, release = asyncio.Event(), asyncio.Event()

    async def create(*args: object) -> str:
        started.set()
        await release.wait()
        return SECRET

    port.create_invitation = AsyncMock(side_effect=create)  # type: ignore[method-assign]
    first = instance._proposal(
        _Pending("invite", 1, NOW + timedelta(minutes=10), identity, 1, target=TARGET)
    )
    second = instance._proposal(
        _Pending("invite", 2, NOW + timedelta(minutes=10), identity, 1, target=TARGET)
    )
    task = asyncio.create_task(instance.confirm(1, first))
    await started.wait()
    with pytest.raises(CalendarLimitExceeded):
        await instance.confirm(2, second)
    assert len(instance._reserved) == 1
    release.set()
    await task
    assert len(instance._invitations) == 3
    assert not instance._reserved
    assert port.create_invitation.call_count == 1


@pytest.mark.asyncio
async def test_inflight_global_reservation_cannot_be_occupied_by_another_preview() -> None:
    instance, port, _ = ux()
    item = await created(instance, port)
    handle, _ = await instance.invitation_target_prompt(1, item.calendar_id, item.version)
    await instance.invitation_target(1, handle, str(TARGET))
    await instance.create_preview(2)
    started, release = asyncio.Event(), asyncio.Event()

    async def create(*args: object) -> str:
        started.set()
        await release.wait()
        return SECRET

    port.create_invitation = AsyncMock(side_effect=create)  # type: ignore[method-assign]
    with patch("time_tl.bot.calendar_router._GLOBAL_LIMIT", 2):
        task = asyncio.create_task(instance.confirm(1, handle))
        await started.wait()
        with pytest.raises(CalendarLimitExceeded):
            await instance.create_preview(3)
        assert len(instance._pending) + len(instance._reserved) == 2
        release.set()
        await task
    assert len(instance._pending) + len(instance._invitations) == 2


@pytest.mark.asyncio
async def test_timeout_consumes_confirmation_and_releases_invitation_reservation() -> None:
    instance, port, _ = ux()
    item = await created(instance, port)
    handle, _ = await instance.invitation_target_prompt(1, item.calendar_id, item.version)
    await instance.invitation_target(1, handle, str(TARGET))
    cancelled = asyncio.Event()

    async def stuck(*args: object) -> str:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        return SECRET

    port.create_invitation = AsyncMock(side_effect=stuck)  # type: ignore[method-assign]
    with patch("time_tl.bot.calendar_router._OPERATION_TIMEOUT_SECONDS", 0.01):
        with pytest.raises(TimeoutError):
            await instance.confirm(1, handle)
    assert cancelled.is_set()
    assert not instance._reserved
    assert not instance._invitations
    with pytest.raises(CalendarDataUnavailable):
        await instance.confirm(1, handle)


@pytest.mark.asyncio
async def test_router_read_timeout_is_generic_unavailable_not_empty_or_retry() -> None:
    instance, port, _ = ux()

    async def stuck(*args: object) -> tuple[CalendarSummary, ...]:
        await asyncio.Event().wait()
        return ()

    port.list_calendars = AsyncMock(side_effect=stuck)  # type: ignore[method-assign]
    with (
        patch("time_tl.bot.calendar_router._OPERATION_TIMEOUT_SECONDS", 0.01),
        patch("time_tl.bot.aiogram_adapter._edit", new_callable=AsyncMock) as edit,
    ):
        router = build_calendar_router(instance, enabled=True)
        assert router is not None
        await router.propagate_event("callback_query", event(1, "menu:calendars"), state=state())
        assert "недоступна" in edit.call_args.args[1].text
        assert "могла виконатися" in edit.call_args.args[1].text
        assert "Календарів ще немає" not in edit.call_args.args[1].text


@pytest.mark.asyncio
async def test_export_is_actor_scoped_link_free_without_private_fields() -> None:
    instance, port, _ = ux()
    await created(instance, port)
    raw = await instance.export(1)
    assert b"join_url" not in raw
    assert b"private" not in raw
    assert SECRET.encode() not in raw
    assert b"owner_user_id" not in raw
    raw_other = await instance.export(2)
    assert b'"calendars": []' in raw_other


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["foreign", "private", "links", "oversized"])
async def test_export_rejects_unexpected_privacy_fields_foreign_rows_and_bound(case: str) -> None:
    instance, port, _ = ux()
    await created(instance, port)
    values = list(await port.export_calendars(1))
    if case == "foreign":
        values[0]["calendar_id"] = str(uuid4())
    elif case == "private":
        values[0]["owner_user_id"] = TARGET
    elif case == "links":
        values[0]["snapshot"] = {"join_url": "https://example.invalid/private"}
    port.export_calendars = AsyncMock(return_value=tuple(values))  # type: ignore[method-assign]
    with patch(
        "time_tl.bot.calendar_router._MAX_EXPORT_BYTES", 1 if case == "oversized" else 2816000
    ):
        with pytest.raises((CalendarDataUnavailable, CalendarLimitExceeded, ValueError)):
            await instance.export(1)


@pytest.mark.asyncio
@pytest.mark.parametrize("private", [True, False])
async def test_export_router_document_only_to_requesting_private_account(private: bool) -> None:
    instance, port, _ = ux()
    await created(instance, port)
    callback = event(1, "cal:export")
    if not private:
        callback = callback.model_copy(
            update={"message": Message(message_id=1, date=NOW, chat=Chat(id=-10, type="group"))}
        )
    with (
        patch("time_tl.bot.aiogram_adapter._edit", new_callable=AsyncMock),
        patch.object(Message, "answer_document", new_callable=AsyncMock) as send,
    ):
        router = build_calendar_router(instance, enabled=True)
        assert router is not None
        await router.propagate_event("callback_query", callback, state=state())
        assert send.call_count == (1 if private else 0)
        if private:
            assert send.call_args.args[0].filename == "time-tl-calendars.json"


@pytest.mark.asyncio
async def test_calendar_router_never_solicits_private_id_in_group_chat() -> None:
    instance, port, _ = ux()
    item = await created(instance, port)
    callback = event(1, _callback("invite", item.calendar_id, item.version))
    callback = callback.model_copy(
        update={"message": Message(message_id=1, date=NOW, chat=Chat(id=-10, type="group"))}
    )
    with patch("time_tl.bot.aiogram_adapter._edit", new_callable=AsyncMock) as edit:
        router = build_calendar_router(instance, enabled=True)
        assert router is not None
        context = state()
        await router.propagate_event("callback_query", callback, state=context)
        assert "Надішліть Telegram ID" not in edit.call_args.args[1].text
        assert await context.get_state() is None
        assert not instance._pending

"""LOCAL read-only My Day integration; no database or real Telegram adapter."""

from __future__ import annotations

import copy
from dataclasses import replace
from datetime import UTC, date, datetime, time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from time_tl.bot.async_service import (
    AccountDataMissing,
    AsyncTelegramApplication,
    ExistingAccountMiddleware,
    build_async_router,
)
from time_tl.bot.models import NotificationMode
from time_tl.bot.service import UserAccount
from time_tl.config import Settings
from time_tl.domain.schedule import DateOverride, Lesson, Parity, ScheduleProfile, ScheduleRule

NOW = datetime(2026, 9, 28, 6, 35, tzinfo=UTC)


class ReadRepository:
    def __init__(self) -> None:
        self.accounts = {1: UserAccount(1), 2: UserAccount(2)}
        self.reads: list[int] = []
        self.writes = 0

    async def get_existing(self, user_id: int) -> UserAccount | None:
        self.reads.append(user_id)
        return copy.deepcopy(self.accounts.get(user_id))

    async def get_or_create(self, user_id: int) -> UserAccount:
        raise AssertionError("read view must not create an account")

    async def save(self, account: UserAccount) -> None:
        self.writes += 1
        raise AssertionError("read view must not save an account")

    async def delete_account(self, user_id: int) -> bool:
        raise AssertionError("read view must not delete an account")


def profile(name: str, *, active: bool = True) -> ScheduleProfile:
    return ScheduleProfile(
        name,
        name,
        rules=(ScheduleRule(0, (Lesson(time(9), time(9, 45), name),)),),
        active=active,
    )


def application(repo: ReadRepository, **kwargs: object) -> AsyncTelegramApplication:
    return AsyncTelegramApplication(
        regions=(),
        resolver=SimpleNamespace(resolve=AsyncMock()),
        repository=repo,
        my_day_clock=lambda: NOW,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_flag_off_preserves_menu_and_does_not_read_for_stale_callback() -> None:
    repo = ReadRepository()
    app = application(repo)
    assert "menu:myday" not in {
        button.callback_data for row in (await app.menu(1)).rows for button in row
    }
    repo.reads.clear()
    view = await app.my_day_view(1)
    assert "ще не ввімкнено" in view.text
    assert repo.reads == [] and repo.writes == 0


@pytest.mark.asyncio
async def test_active_only_bridge_is_read_only_and_owner_scoped() -> None:
    repo = ReadRepository()
    repo.accounts[1].profiles = {
        "Математика": profile("Математика"),
        "Прихований": profile("Прихований", active=False),
    }
    repo.accounts[2].profiles = {"Чужий": profile("Чужий")}
    original = copy.deepcopy(repo.accounts)
    app = application(repo, my_day_enabled=True)
    view = await app.my_day_view(1)
    assert "Математика" in view.text and "ще 10 хв" in view.text
    assert "Прихований" not in view.text and "Чужий" not in view.text
    assert "відбій" not in view.text.lower()
    assert all("join" not in (button.callback_data or "") for row in view.rows for button in row)
    assert repo.accounts == original and repo.writes == 0
    assert repo.reads == [1]
    menu = await app.menu(1)
    assert menu.rows[0][0].callback_data == "menu:myday"


@pytest.mark.asyncio
async def test_feature_menu_survives_mode_change_and_restart_without_writes() -> None:
    repo = ReadRepository()
    repo.accounts[1].mode = NotificationMode.LESSONS
    repo.accounts[1].region_id = "31"
    app = application(repo, my_day_enabled=True)
    assert app._app.menu(1).rows[0][0].callback_data == "menu:myday"
    # Synchronous menu remains used by onboarding/mode confirmation paths.
    await app.menu(1)
    assert app._app.start(1).rows[0][0].callback_data == "menu:myday"
    restarted = application(repo, my_day_enabled=True)
    assert (await restarted.menu(1)).rows[0][0].callback_data == "menu:myday"


@pytest.mark.asyncio
@pytest.mark.parametrize("configured", [False, True])
async def test_no_active_profile_and_multiple_active_are_not_silently_selected(
    configured: bool,
) -> None:
    repo = ReadRepository()
    if configured:
        repo.accounts[1].profiles = {"А": profile("А"), "Б": profile("Б")}
    view = await application(repo, my_day_enabled=True).my_day_view(1)
    assert ("однозначно" if configured else "ще не налаштовано") in view.text
    assert repo.writes == 0


@pytest.mark.asyncio
async def test_override_empty_day_is_read_without_altering_profile() -> None:
    repo = ReadRepository()
    chosen = replace(profile("Школа"), overrides=(DateOverride(date(2026, 9, 28), day_off=True),))
    repo.accounts[1].profiles = {chosen.profile_id: chosen}
    view = await application(repo, my_day_enabled=True).my_day_view(1)
    assert "На вибраний день немає занять" in view.text
    assert repo.accounts[1].profiles[chosen.profile_id] == chosen


@pytest.mark.asyncio
async def test_rule_conflict_is_unavailable_not_false_empty_day() -> None:
    repo = ReadRepository()
    chosen = profile("Школа")
    chosen = replace(chosen, rules=chosen.rules + chosen.rules)
    repo.accounts[1].profiles = {chosen.profile_id: chosen}
    view = await application(repo, my_day_enabled=True).my_day_view(1)
    assert "Не вдалося безпечно визначити" in view.text
    assert "немає занять" not in view.text and repo.writes == 0


@pytest.mark.asyncio
async def test_deleted_account_is_not_recreated_and_wrong_owner_is_not_exposed() -> None:
    repo = ReadRepository()
    app = application(repo, my_day_enabled=True)
    with pytest.raises(AccountDataMissing):
        await app.my_day_view(99)
    repo.accounts[1] = repo.accounts[2]
    repo.accounts[2].profiles = {"Чужий": profile("Чужий")}
    view = await app.my_day_view(1)
    assert "тимчасово недоступний" in view.text and "Чужий" not in view.text


@pytest.mark.asyncio
async def test_database_read_failure_is_explicit_unavailable() -> None:
    repo = ReadRepository()
    repo.get_existing = AsyncMock(side_effect=RuntimeError("private error"))  # type: ignore[method-assign]
    view = await application(repo, my_day_enabled=True).my_day_view(1)
    assert "тимчасово недоступний" in view.text and "private error" not in view.text


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["exception", "oversized", "timeout"])
async def test_optional_homework_failure_does_not_hide_schedule_or_assert_zero(
    failure: str,
) -> None:
    repo = ReadRepository()
    repo.accounts[1].profiles = {"Математика": profile("Математика")}
    port = SimpleNamespace(list_homework=AsyncMock())
    app = application(repo, my_day_enabled=True, homework_enabled=True, homework_port=port)
    if failure == "exception":
        port.list_homework.side_effect = RuntimeError("private")
    elif failure == "oversized":
        port.list_homework.return_value = [SimpleNamespace(completed=False)] * 101
    else:
        port.list_homework.side_effect = TimeoutError
    view = await app.my_day_view(1)
    assert "Математика" in view.text and "Список тимчасово недоступний" in view.text
    assert "списку: 0" not in view.text and "private" not in view.text
    port.list_homework.assert_awaited_once_with(1, include_past=False)
    assert repo.writes == 0


@pytest.mark.asyncio
async def test_homework_preview_count_does_not_claim_full_history() -> None:
    repo = ReadRepository()
    repo.accounts[1].profiles = {"Школа": profile("Школа")}
    port = SimpleNamespace(
        list_homework=AsyncMock(
            return_value=[SimpleNamespace(completed=False), SimpleNamespace(completed=True)]
        )
    )
    view = await application(
        repo, my_day_enabled=True, homework_enabled=True, homework_port=port
    ).my_day_view(1)
    assert "Невиконаних у поточному списку: 1" in view.text
    assert any(button.callback_data == "menu:homework" for row in view.rows for button in row)


@pytest.mark.asyncio
async def test_kyiv_midnight_and_parity_use_existing_resolver() -> None:
    repo = ReadRepository()
    chosen = ScheduleProfile(
        "Школа",
        "Школа",
        (ScheduleRule(1, (Lesson(time(0), time(1), "Вівторок"),), date_parity=Parity.ODD),),
        active=True,
    )
    repo.accounts[1].profiles = {chosen.profile_id: chosen}
    app = application(repo, my_day_enabled=True)
    app._my_day_clock = lambda: datetime(2026, 9, 28, 21, 30, tzinfo=UTC)
    view = await app.my_day_view(1)
    assert "29.09.2026" in view.text and "Вівторок" in view.text


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["-1", "abc", "99999", "1:2", "١"])
async def test_malformed_page_callback_is_safe_and_clears_fsm(raw: str) -> None:
    app = application(ReadRepository(), my_day_enabled=True)
    router = build_async_router(app)
    handler = next(
        h.callback for h in router.callback_query.handlers if h.callback.__name__ == "my_day_page"
    )
    callback = SimpleNamespace(
        from_user=SimpleNamespace(id=1),
        data="myday:page:" + raw,
        message=SimpleNamespace(edit_text=AsyncMock()),
        answer=AsyncMock(),
    )
    state = SimpleNamespace(clear=AsyncMock())
    await handler(callback, state)
    assert "Сторінка застаріла" in callback.message.edit_text.call_args.args[0]
    state.clear.assert_awaited_once()
    callback.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_valid_page_and_command_route_to_read_view() -> None:
    app = application(ReadRepository(), my_day_enabled=True)
    app.my_day_view = AsyncMock(return_value=SimpleNamespace(text="day", rows=()))  # type: ignore[method-assign]
    router = build_async_router(app)
    page_handler = next(
        h.callback for h in router.callback_query.handlers if h.callback.__name__ == "my_day_page"
    )
    command_handler = next(
        h.callback for h in router.message.handlers if h.callback.__name__ == "my_day_command"
    )
    state = SimpleNamespace(clear=AsyncMock())
    callback = SimpleNamespace(
        from_user=SimpleNamespace(id=1),
        data="myday:page:2",
        message=SimpleNamespace(edit_text=AsyncMock()),
        answer=AsyncMock(),
    )
    await page_handler(callback, state)
    app.my_day_view.assert_awaited_with(1, page=2)
    message = SimpleNamespace(from_user=SimpleNamespace(id=1), answer=AsyncMock())
    await command_handler(message, state)
    app.my_day_view.assert_awaited_with(1)


@pytest.mark.parametrize("role", ["alert-worker", "schedule-worker", "sender-worker", "admin-web"])
def test_my_day_flag_is_default_off_and_bot_only(role: str) -> None:
    settings = Settings(_env_file=None, DATABASE_URL="postgresql+asyncpg://example.invalid/test")
    assert settings.my_day_enabled is False
    with pytest.raises(ValueError, match="restricted to the bot role"):
        Settings(
            _env_file=None,
            DATABASE_URL="postgresql+asyncpg://example.invalid/test",
            SERVICE_ROLE=role,
            MY_DAY_ENABLED=True,
        )
    assert Settings(
        _env_file=None,
        DATABASE_URL="postgresql+asyncpg://example.invalid/test",
        SERVICE_ROLE="bot",
        MY_DAY_ENABLED=True,
    ).my_day_enabled


def test_live_composition_propagates_feature_flag() -> None:
    # Do not execute live runtime or instantiate a real Bot; inspect the composition contract.
    import inspect

    from time_tl.runtime.services import run_bot

    assert "my_day_enabled=settings.my_day_enabled" in inspect.getsource(run_bot)


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/today", "/now"])
async def test_disabled_commands_reach_existing_homework_text_flow(command, monkeypatch) -> None:
    from aiogram.types import Chat, Message, User

    answer = AsyncMock()
    monkeypatch.setattr("time_tl.bot.aiogram_adapter._answer", answer)
    port = SimpleNamespace(suggest_next_lesson=AsyncMock(return_value=None))
    app = application(ReadRepository(), homework_enabled=True, homework_port=port)
    router = build_async_router(app)
    assert not any(h.callback.__name__ == "my_day_command" for h in router.message.handlers)
    state = SimpleNamespace(
        get_data=AsyncMock(return_value={"subject_id": "11111111-1111-1111-1111-111111111111"}),
        update_data=AsyncMock(),
        set_state=AsyncMock(),
        clear=AsyncMock(),
    )
    message = Message(
        message_id=1,
        date=NOW,
        chat=Chat(id=1, type="private"),
        from_user=User(id=1, is_bot=False, first_name="Test"),
        text=command,
    )
    await router.propagate_event(
        "message", message, state=state, raw_state="HomeworkFlow:text", bot=SimpleNamespace()
    )
    state.clear.assert_not_awaited()
    state.update_data.assert_awaited_once_with(text=command, suggested_due="")
    assert state.set_state.call_args.args[0].state == "HomeworkFlow:due"
    assert port.suggest_next_lesson.call_args.args[:2] == (
        1,
        UUID("11111111-1111-1111-1111-111111111111"),
    )
    answer.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event_kind,payload",
    [
        ("Message", "/today"),
        ("Message", "/now@TestBot"),
        ("CallbackQuery", "menu:myday"),
        ("CallbackQuery", "myday:page:1"),
    ],
)
async def test_enabled_day_middleware_reports_read_failure_without_mutations(event_kind, payload):
    repo = ReadRepository()
    repo.get_existing = AsyncMock(side_effect=RuntimeError("private database failure"))
    app = application(repo, my_day_enabled=True)
    event = type(event_kind, (), {})()
    event.from_user = SimpleNamespace(id=1)
    event.text = payload
    event.data = payload
    event.answer = AsyncMock()
    event.message = SimpleNamespace(edit_text=AsyncMock())
    handler = AsyncMock()
    await ExistingAccountMiddleware(app)(handler, event, {})
    handler.assert_not_awaited()
    response = event.message.edit_text if event_kind == "CallbackQuery" else event.answer
    assert "тимчасово недоступний" in response.call_args.args[0]
    assert "private" not in response.call_args.args[0]
    assert repo.writes == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled,command", [(False, "/today"), (True, "/menu")])
async def test_day_middleware_does_not_change_other_failure_paths(enabled, command):
    repo = ReadRepository()
    repo.get_existing = AsyncMock(side_effect=RuntimeError("unavailable"))
    app = application(repo, my_day_enabled=enabled)
    event = type("Message", (), {})()
    event.from_user = SimpleNamespace(id=1)
    event.text = command
    event.answer = AsyncMock()
    with pytest.raises(RuntimeError, match="unavailable"):
        await ExistingAccountMiddleware(app)(AsyncMock(), event, {})
    event.answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_day_middleware_keeps_deleted_account_missing_and_never_creates_it():
    repo = ReadRepository()
    del repo.accounts[1]
    app = application(repo, my_day_enabled=True)
    event = type("Message", (), {})()
    event.from_user = SimpleNamespace(id=1)
    event.text = "/today"
    event.answer = AsyncMock()
    handler = AsyncMock()
    await ExistingAccountMiddleware(app)(handler, event, {})
    assert "дані видалено" in event.answer.call_args.args[0]
    handler.assert_not_awaited()
    assert 1 not in repo.accounts and repo.writes == 0

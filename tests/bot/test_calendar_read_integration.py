"""LOCAL optional-calendar read bridge, never real DB/Telegram delivery."""

from datetime import UTC, datetime, time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from time_tl.bot.async_service import AccountDataMissing, AsyncTelegramApplication
from time_tl.bot.service import UserAccount
from time_tl.config import Settings
from time_tl.domain.calendars import Calendar, CalendarKind, CalendarMembership, CalendarSource
from time_tl.domain.schedule import Lesson, ScheduleProfile, ScheduleRule

NOW = datetime(2026, 9, 28, 6, 35, tzinfo=UTC)


def profile(name):
    return ScheduleProfile(
        name, name, (ScheduleRule(0, (Lesson(time(9), time(10), name),)),), active=True
    )


def source(number=1, *, user_id=1, visible=True):
    calendar_id = f"calendar:{number}"
    return CalendarSource(
        Calendar(calendar_id, f"Клас {number}", CalendarKind.SCHOOL, profile(f"Заняття {number}")),
        CalendarMembership(user_id, calendar_id, visible),
    )


def app(*, enabled=True, legacy=True, sources=()):
    account = UserAccount(1)
    if legacy:
        account.profiles = {"Особисте": profile("Особисте")}
    repo = SimpleNamespace(get_existing=AsyncMock(return_value=account), save=AsyncMock())
    port = SimpleNamespace(list_sources=AsyncMock(return_value=sources))
    application = AsyncTelegramApplication(
        regions=(),
        resolver=SimpleNamespace(),
        repository=repo,
        my_day_enabled=True,
        calendars_enabled=enabled,
        calendar_port=port,
        my_day_clock=lambda: NOW,
    )
    return application, repo, port


@pytest.mark.asyncio
async def test_off_does_not_read_new_schema_and_preserves_legacy():
    application, repo, port = app(enabled=False, sources=(source(),))
    port.list_sources.side_effect = AssertionError("must not read new schema")
    view = await application.my_day_view(1)
    assert "Особисте" in view.text and "Заняття 1" not in view.text
    port.list_sources.assert_not_awaited()
    repo.save.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_sources_and_legacy_are_combined_without_writes():
    application, repo, port = app(sources=(source(), source(2)))
    view = await application.my_day_view(1)
    assert "Особисте" in view.text and "Заняття 1" in view.text and "Заняття 2" in view.text
    assert "Зараз · занять: 3" in view.text and "Перетини занять: 3" in view.text
    port.list_sources.assert_awaited_once_with(1)
    repo.save.assert_not_awaited()


@pytest.mark.asyncio
async def test_joined_calendar_without_legacy_active_still_works():
    application, _, _ = app(legacy=False, sources=(source(),))
    view = await application.my_day_view(1)
    assert "Заняття 1" in view.text and "ще не налаштовано" not in view.text


@pytest.mark.asyncio
async def test_no_active_and_no_sources_is_explicit_setup_not_unknown():
    application, _, _ = app(legacy=False)
    assert "ще не налаштовано" in (await application.my_day_view(1)).text


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("private error"), TimeoutError()])
async def test_source_failure_never_silently_returns_legacy_only(failure):
    application, repo, port = app()
    port.list_sources.side_effect = failure
    view = await application.my_day_view(1)
    assert "тимчасово недоступний" in view.text
    assert "Особисте" not in view.text and "немає занять" not in view.text
    assert "private" not in view.text
    repo.save.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sources",
    [
        tuple(source(n) for n in range(12)),
        (source(user_id=2),),
        (source(visible=False),),
        (source(), source()),
        (SimpleNamespace(),),
        [source()],
    ],
)
async def test_invalid_oversized_foreign_hidden_or_duplicate_sources_fail_closed(sources):
    application, _, _ = app(sources=sources)
    view = await application.my_day_view(1)
    assert "недоступний" in view.text or "Не вдалося безпечно" in view.text
    assert "Особисте" not in view.text and "немає занять" not in view.text


@pytest.mark.asyncio
async def test_source_reads_do_not_resurrect_deleted_account():
    application, repo, port = app(sources=(source(),))
    repo.get_existing.return_value = None
    with pytest.raises(AccountDataMissing):
        await application.my_day_view(1)
    port.list_sources.assert_not_awaited()


@pytest.mark.asyncio
async def test_ambiguous_legacy_active_never_ignored_for_calendar_sources():
    application, repo, _ = app(sources=(source(),))
    repo.get_existing.return_value.profiles["Другий"] = profile("Другий")
    assert "однозначно" in (await application.my_day_view(1)).text


def test_calendar_port_requires_explicit_flag_and_my_day():
    with pytest.raises(ValueError, match="require My Day"):
        AsyncTelegramApplication(
            regions=(),
            resolver=SimpleNamespace(),
            repository=SimpleNamespace(),
            calendars_enabled=True,
            my_day_enabled=False,
        )
    with pytest.raises(ValueError, match="durable calendar port"):
        AsyncTelegramApplication(
            regions=(),
            resolver=SimpleNamespace(),
            repository=SimpleNamespace(),
            calendars_enabled=True,
            my_day_enabled=True,
        )


@pytest.mark.parametrize("role", ["admin-web", "sender-worker", "schedule-worker", "alert-worker"])
def test_config_default_off_and_bot_only(role):
    base = {"_env_file": None, "DATABASE_URL": "postgresql+asyncpg://example.invalid/test"}
    assert Settings(**base).calendars_enabled is False
    with pytest.raises(ValueError, match="restricted to the bot"):
        Settings(**base, SERVICE_ROLE=role, CALENDARS_ENABLED=True)


def test_config_requires_my_day_and_accepts_bot_opt_in():
    base = {"_env_file": None, "DATABASE_URL": "postgresql+asyncpg://example.invalid/test"}
    with pytest.raises(ValueError, match="require MY_DAY_ENABLED"):
        Settings(**base, SERVICE_ROLE="bot", CALENDARS_ENABLED=True)
    assert Settings(
        **base, SERVICE_ROLE="bot", CALENDARS_ENABLED=True, MY_DAY_ENABLED=True
    ).calendars_enabled


@pytest.mark.asyncio
async def test_enabled_privacy_and_both_deletion_prompts_explain_shared_calendar_loss():
    application, _, port = app()
    assert "для всіх учасників" in application.privacy().text
    request = await application.request_data_deletion(1)
    assert "для всіх учасників" in request.text
    # Only presentation decoration; existing deletion token checks remain authoritative.
    review = await application.review_data_deletion(1, "invalid-token")
    assert "для всіх учасників" in review.text
    port.list_sources.assert_not_awaited()


def test_disabled_calendar_privacy_and_deletion_text_is_unchanged():
    from time_tl.bot.models import View

    application, _, _ = app(enabled=False)
    assert "підписки" not in application.privacy().text
    original = View("legacy warning", ())
    assert application._calendar_deletion_warning(original) is original


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_calendar_menu_opt_in_does_not_query_sources(enabled):
    application, _, port = app(enabled=enabled)
    view = await application.menu(1)
    callbacks = [button.callback_data for row in view.rows for button in row]
    assert ("menu:calendars" in callbacks) is enabled
    port.list_sources.assert_not_awaited()

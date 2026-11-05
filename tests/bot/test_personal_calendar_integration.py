import json
from datetime import UTC, datetime, time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from time_tl.bot.async_service import AsyncTelegramApplication, build_async_router
from time_tl.bot.calendar_router import CalendarUx
from time_tl.bot.personal_calendar_router import NOTICE
from time_tl.bot.service import UserAccount
from time_tl.config import Settings
from time_tl.domain.calendars import KYIV, MyDay
from time_tl.domain.schedule import Lesson, ScheduleProfile, ScheduleRule

NOW = datetime(2026, 10, 4, 8, tzinfo=UTC)


def application(enabled=True, personal=None):
    account = UserAccount(
        1,
        profiles={
            "active": ScheduleProfile(
                "active",
                "Old schedule",
                active=True,
                rules=(ScheduleRule(6, (Lesson(time(11), time(12), "Base lesson"),)),),
            )
        },
    )
    personal = personal or SimpleNamespace(
        export_personal_overrides=AsyncMock(return_value="{}"),
        read_personal_day=AsyncMock(
            return_value=MyDay(NOW.astimezone(KYIV).date(), (), (), (), ()),
        ),
    )
    app = AsyncTelegramApplication(
        regions=(),
        resolver=SimpleNamespace(resolve=AsyncMock()),
        repository=SimpleNamespace(get_existing=AsyncMock(return_value=account)),
        my_day_enabled=True,
        calendars_enabled=True,
        calendar_port=SimpleNamespace(list_sources=AsyncMock(return_value=())),
        personal_calendar_changes_enabled=enabled,
        personal_calendar_port=personal,
        my_day_clock=lambda: NOW,
    )
    return app, personal


@pytest.mark.asyncio
async def test_flag_off_keeps_base_day_and_never_reads_personal_tables():
    app, personal = application(False)
    view = await app.my_day_view(1)
    assert "Base lesson" in view.text
    personal.read_personal_day.assert_not_awaited()


@pytest.mark.asyncio
async def test_personal_cancellation_is_rendered_with_scope_notice_and_legacy_bridge():
    app, personal = application()
    view = await app.my_day_view(1)
    assert "Base lesson" not in view.text
    assert NOTICE in view.text
    extra = personal.read_personal_day.await_args.kwargs["extra_sources"]
    assert len(extra) == 1 and extra[0].calendar.calendar_id.startswith("legacy:")
    assert len(view.text.encode("utf-16-le")) // 2 <= 4096


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", [TimeoutError(), ValueError(), RuntimeError()])
async def test_personal_read_failure_never_falls_back_to_uncancelled_base(fault):
    app, personal = application()
    personal.read_personal_day.side_effect = fault
    view = await app.my_day_view(1)
    assert "Base lesson" not in view.text and "тимчасово недоступні" in view.text


@pytest.mark.asyncio
async def test_invalid_personal_projection_fails_closed():
    app, personal = application()
    personal.read_personal_day.return_value = None
    assert "тимчасово недоступні" in (await app.my_day_view(1)).text


def test_optional_router_builds_without_network():
    app, _ = application()
    assert build_async_router(app) is not None


def test_settings_default_off_and_require_calendar_prerequisites():
    options = {"_env_file": None, "DATABASE_URL": "postgresql+asyncpg://example.invalid/test"}
    assert not Settings(**options).personal_calendar_changes_enabled
    with pytest.raises(ValueError):
        Settings(**options, PERSONAL_CALENDAR_CHANGES_ENABLED=True)
    settings = Settings(
        **options,
        PERSONAL_CALENDAR_CHANGES_ENABLED=True,
        CALENDARS_ENABLED=True,
        MY_DAY_ENABLED=True,
    )
    assert settings.personal_calendar_changes_enabled


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_actor_only_export_hook_and_default_off(enabled):
    port = SimpleNamespace(
        list_calendars=AsyncMock(return_value=()),
        export_calendars=AsyncMock(return_value=()),
    )
    exporter = AsyncMock(return_value='{"schema_version":"1.0","overrides":[]}')
    ux = CalendarUx(
        port,
        SimpleNamespace(get_existing=AsyncMock(return_value=UserAccount(1))),
        personal_changes_enabled=enabled,
        personal_export=exporter,
    )
    result = json.loads(await ux.export(1))
    assert ("personal_changes" in result) is enabled
    if enabled:
        exporter.assert_awaited_once_with(1)
    else:
        exporter.assert_not_awaited()

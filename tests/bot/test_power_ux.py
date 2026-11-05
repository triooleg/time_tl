"""LOCAL power UX: fake persistence and Telegram, never official-provider requests."""

import asyncio
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from time_tl.bot.async_service import AsyncTelegramApplication, build_async_router
from time_tl.bot.models import NotificationMode
from time_tl.bot.power_service import PowerUx
from time_tl.bot.safety import BoundedMemoryStorage
from time_tl.bot.service import TelegramApplication, UserAccount
from time_tl.domain.power_outages.models import (
    GROUPS,
    PowerDaySchedule,
    PowerInterval,
    PowerProvider,
    PowerPublicationStatus,
    PowerScheduleView,
    PowerSourceKind,
    PowerState,
    PowerSubscription,
    ScheduleKind,
    SourceStatus,
)
from time_tl.domain.power_outages.normalization import day_bounds

NOW = datetime(2026, 11, 4, 12, tzinfo=UTC)


class FakePowerPort:
    def __init__(self):
        self.subscriptions = {}
        self.saved = []
        self.days = []
        self.view = None

    async def subscription(self, actor):
        return self.subscriptions.get(actor)

    async def save(self, actor, provider, group_id, **values):
        old = self.subscriptions.get(actor)
        if values["expected_revision"] != (old.revision if old else None):
            raise ValueError("changed")
        result = PowerSubscription(
            str(actor),
            provider,
            group_id,
            values["enabled"],
            values["outage_enabled"],
            values["restoration_enabled"],
            values["outage_lead"],
            values["restoration_lead"],
            old.revision + 1 if old else 1,
        )
        self.subscriptions[actor] = result
        self.saved.append((actor, result))
        return result

    async def day(self, actor, selected, kind):
        self.days.append((actor, selected, kind))
        if self.view is not None:
            return self.view
        current = self.subscriptions[actor]
        return PowerScheduleView(
            current.provider,
            current.group_id,
            SourceStatus.UNAVAILABLE,
            None,
            NOW,
            None,
        )


def callbacks(view):
    return [b.callback_data for row in view.rows for b in row]


async def test_group_waiting_for_cache_has_specific_message():
    port = FakePowerPort()
    port.view = PowerScheduleView(
        PowerProvider.DTEK, "6.1", SourceStatus.UNAVAILABLE, None, NOW, None,
        error_code="MAP_GROUP_PENDING",
    )
    card = await PowerUx(port, clock=lambda: NOW).schedule(
        1, NOW.date(), ScheduleKind.CONFIRMED_DAY
    )
    assert "Графік для цієї групи ще завантажується" in card.text
    assert "відключень немає" not in card.text


@pytest.fixture
def ux():
    return PowerUx(FakePowerPort(), clock=lambda: NOW)


@pytest.mark.asyncio
async def test_all_regional_operators_twelve_groups_and_short_callbacks(ux):
    providers = await ux.home(1)
    assert len([v for v in callbacks(providers) if v.startswith("pw:provider:")]) == 6
    group_views = []
    for provider in PowerProvider:
        first = await ux.provider(1, provider)
        handle = next(iter(ux._drafts.values())).handle
        group_views.extend((first, ux.groups(1, handle, 1)))
        buttons = callbacks(first) + callbacks(group_views[-1])
        assert {v.rsplit(":", 1)[1] for v in buttons if v.startswith("pw:group:")} == set(GROUPS)
    for view in [providers, *group_views]:
        assert all(len(v.encode("utf-8")) <= 64 for v in callbacks(view))
    assert not ux.port.saved


@pytest.mark.asyncio
async def test_wizard_saves_only_explicit_confirmation_and_preserves_separate_leads(ux):
    await ux.provider(1, PowerProvider.DTEK_ODESA)
    handle = ux._drafts[1].handle
    ux.group(1, handle, "6.2")
    ux.notifications(1, handle, True)
    ux.custom(1, handle, "outage", "120")
    final = ux.custom(1, handle, "restoration", "5")
    assert "ДТЕК Одеські" in final.text and "120 хв" in final.text
    assert not ux.port.saved
    result = await ux.confirm(1, handle)
    subscription = ux.port.subscriptions[1]
    assert subscription.provider is PowerProvider.DTEK_ODESA
    assert subscription.group_id == "6.2" and subscription.enabled
    assert subscription.outage_lead_minutes == 120 and subscription.restoration_lead_minutes == 5
    assert "збережено" in result.text
    with pytest.raises(ValueError):
        await ux.confirm(1, handle)
    assert len(ux.port.saved) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["-1", "181", "1.5", "True", " 5", "٥", "9999", ""])
async def test_custom_lead_rejects_invalid_input_without_saving(ux, raw):
    await ux.provider(1, PowerProvider.CEK)
    handle = ux._drafts[1].handle
    with pytest.raises(ValueError):
        ux.custom(1, handle, "outage", raw)
    assert not ux.port.saved


@pytest.mark.asyncio
async def test_preview_actor_expiry_capacity_and_cancel(ux):
    await ux.provider(1, PowerProvider.CEK)
    handle = ux._drafts[1].handle
    with pytest.raises(ValueError):
        ux.group(2, handle, "1.1")
    preview = ux.review(1, handle)
    assert "До відключення: 10 хв" in preview.text
    assert "До включення за графіком: 10 хв" in preview.text
    await ux.home(1)
    with pytest.raises(ValueError):
        await ux.confirm(1, handle)
    clock = [NOW]
    bounded = PowerUx(ux.port, clock=lambda: clock[0], capacity=1)
    await bounded.provider(1, PowerProvider.CEK)
    first = bounded._drafts[1].handle
    await bounded.provider(2, PowerProvider.DTEK)
    assert len(bounded._drafts) == 1
    with pytest.raises(ValueError):
        bounded.review(1, first)
    second = bounded._drafts[2].handle
    clock[0] += timedelta(minutes=11)
    with pytest.raises(ValueError):
        bounded.review(2, second)
    assert not ux.port.saved


@pytest.mark.asyncio
async def test_revision_conflict_cannot_be_replayed(ux):
    ux.port.subscriptions[1] = PowerSubscription("1", PowerProvider.CEK, "1.1")
    await ux.settings(1)
    handle = ux._drafts[1].handle
    ux.port.subscriptions[1] = replace(ux.port.subscriptions[1], revision=2)
    with pytest.raises(ValueError):
        await ux.confirm(1, handle)
    with pytest.raises(ValueError):
        await ux.confirm(1, handle)
    assert not ux.port.saved


@pytest.mark.asyncio
async def test_configured_new_defaults_do_not_overwrite_saved_preferences():
    port = FakePowerPort()
    ux = PowerUx(port, clock=lambda: NOW, default_outage_lead=45, default_restore_lead=10)
    await ux.provider(1, PowerProvider.CEK)
    first = ux._drafts[1]
    assert (first.outage_lead, first.restoration_lead) == (45, 10)
    port.subscriptions[2] = PowerSubscription(
        "2",
        PowerProvider.CEK,
        "2.1",
        outage_lead_minutes=120,
        restoration_lead_minutes=0,
    )
    await ux.settings(2)
    saved = ux._drafts[2]
    assert (saved.outage_lead, saved.restoration_lead) == (120, 0)


def test_weekly_forecast_exposes_seven_distinct_dates_without_fetching(ux):
    view = ux.forecast()
    dates = [value for value in callbacks(view) if value.startswith("pw:day:")]
    assert len(dates) == len(set(dates)) == 7
    assert dates[0] == "pw:day:2026-11-04:f:0"
    assert dates[-1] == "pw:day:2026-11-10:f:0"
    assert not ux.port.days


@pytest.mark.asyncio
async def test_facade_requires_injected_port_and_passes_power_defaults():
    repository = SimpleNamespace(get_existing=AsyncMock(return_value=UserAccount(1)))
    missing = AsyncTelegramApplication(
        regions=(),
        resolver=SimpleNamespace(),
        repository=repository,
        power_outages_enabled=True,
    )
    assert "menu:power" not in callbacks(await missing.menu(1))
    configured = AsyncTelegramApplication(
        regions=(),
        resolver=SimpleNamespace(),
        repository=repository,
        power_outages_enabled=True,
        power_port=FakePowerPort(),
        power_default_outage_lead=60,
        power_default_restore_lead=20,
        power_stale_after_seconds=300,
    )
    assert "menu:power" in callbacks(await configured.menu(1))
    assert configured.power_ux.stale_after_seconds == 300
    await configured.power_ux.provider(1, PowerProvider.CEK)
    preview = configured.power_ux._drafts[1]
    assert (preview.outage_lead, preview.restoration_lead) == (60, 20)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", list(SourceStatus))
async def test_schedules_cache_only_status_and_planned_not_actual_power(ux, status):
    selected = date(2026, 11, 4)
    start, end = day_bounds(selected)
    schedule = PowerDaySchedule(
        PowerProvider.DTEK,
        "3.2",
        selected,
        ScheduleKind.CONFIRMED_DAY,
        (PowerInterval("3.2", start, end, PowerState.POWER_EXPECTED),),
    )
    ux.port.view = PowerScheduleView(PowerProvider.DTEK, "3.2", status, NOW, NOW, schedule)
    view = await ux.schedule(1, selected, ScheduleKind.CONFIRMED_DAY)
    assert ux.port.days == [(1, selected, ScheduleKind.CONFIRMED_DAY)]
    assert "00:00–24:00" not in view.text
    assert "світло вже є" not in view.text
    if status is SourceStatus.FRESH:
        assert "Сьогодні планових відключень немає" in view.text
        assert "Аварійні відключення можуть" in view.text
    else:
        assert "Актуальний графік поки недоступний" in view.text
        assert "планових відключень немає" not in view.text
    assert all(len(v.encode("utf-8")) <= 64 for v in callbacks(view))


@pytest.mark.asyncio
async def test_cache_missing_or_old_never_means_power_available(ux):
    ux.port.subscriptions[1] = PowerSubscription("1", PowerProvider.CEK, "2.1")
    empty = await ux.schedule(1, NOW.date(), ScheduleKind.CONFIRMED_DAY)
    assert "Актуальний графік поки недоступний" in empty.text
    assert "планових відключень немає" not in empty.text
    ux.port.view = PowerScheduleView(
        PowerProvider.CEK,
        "2.1",
        SourceStatus.FRESH,
        NOW - timedelta(hours=2),
        NOW,
        None,
    )
    stale = await ux.schedule(1, NOW.date(), ScheduleKind.CONFIRMED_DAY)
    assert "Актуальний графік поки недоступний" in stale.text
    forecast = await ux.schedule(1, NOW.date(), ScheduleKind.FORECAST_WEEK)
    assert "Орієнтовний тижневий прогноз" in forecast.text


@pytest.mark.asyncio
async def test_emergency_plain_language_hides_source_and_never_claims_restoration(ux):
    lower, upper = day_bounds(NOW.date())
    schedule = PowerDaySchedule(
        PowerProvider.CEK,
        "1.1",
        NOW.date(),
        ScheduleKind.CONFIRMED_DAY,
        (PowerInterval("1.1", lower, upper, PowerState.UNKNOWN),),
        publication_status=PowerPublicationStatus.EMERGENCY,
    )
    ux.port.view = PowerScheduleView(
        PowerProvider.CEK,
        "1.1",
        SourceStatus.FRESH,
        NOW,
        NOW,
        schedule,
        source_kind=PowerSourceKind.ALERTS_ENERGY_HTML,
        source_url="https://alerts.energy/dnipropetrovska-oblast",
    )
    view = await ux.schedule(1, NOW.date(), ScheduleKind.CONFIRMED_DAY)
    assert "аварійні відключення" in view.text
    assert "Точний час відключень і відновлення наразі невідомий" in view.text
    assert "Alerts Energy" not in view.text and "http" not in view.text
    assert "00:00–24:00" not in view.text and "UNKNOWN" not in view.text
    assert "EMERGENCY" not in view.text and "агрегатор" not in view.text
    assert "світло очікується" not in view.text


@pytest.mark.asyncio
@pytest.mark.parametrize("source_kind", list(PowerSourceKind))
async def test_customer_card_shows_only_merged_outage_times_without_source_details(ux, source_kind):
    lower, upper = day_bounds(NOW.date())
    schedule = PowerDaySchedule(
        PowerProvider.CEK,
        "3.2",
        NOW.date(),
        ScheduleKind.CONFIRMED_DAY,
        (
            PowerInterval("3.2", lower, lower + timedelta(hours=6), PowerState.POWER_EXPECTED),
            PowerInterval(
                "3.2",
                lower + timedelta(hours=6),
                lower + timedelta(hours=7),
                PowerState.OUTAGE_CONFIRMED,
            ),
            PowerInterval(
                "3.2",
                lower + timedelta(hours=7),
                lower + timedelta(hours=8),
                PowerState.OUTAGE_CONFIRMED,
            ),
            PowerInterval(
                "3.2",
                lower + timedelta(hours=8),
                lower + timedelta(hours=18),
                PowerState.POWER_EXPECTED,
            ),
            PowerInterval(
                "3.2",
                lower + timedelta(hours=18),
                lower + timedelta(hours=19.5),
                PowerState.OUTAGE_CONFIRMED,
            ),
            PowerInterval("3.2", lower + timedelta(hours=19.5), upper, PowerState.POWER_EXPECTED),
        ),
    )
    ux.port.view = PowerScheduleView(
        PowerProvider.CEK,
        "3.2",
        SourceStatus.FRESH,
        NOW,
        NOW,
        schedule,
        source_kind=source_kind,
        source_url="https://alerts.energy/synthetic-source",
    )
    view = await ux.schedule(1, NOW.date(), ScheduleKind.CONFIRMED_DAY)
    assert "06:00–08:00" in view.text and "18:00–19:30" in view.text
    assert "06:00–07:00" not in view.text and "07:00–08:00" not in view.text
    assert "00:00–06:00" not in view.text and "19:30–24:00" not in view.text
    assert "група 3.2" in view.text and "04.11.2026" in view.text
    assert "фактичної наявності" in view.text
    for hidden in (
        "http",
        "Джерело",
        "Alerts Energy",
        "Bezsvitla",
        "агрегатор",
        "успішне оновлення",
        "FRESH",
        "UNKNOWN",
        "NO_SCHEDULE",
    ):
        assert hidden not in view.text
    assert (
        next(b.label for row in view.rows for b in row if b.callback_data.startswith("pw:day:"))
        == "🔄 Оновити"
    )
    assert ux.port.days == [(1, NOW.date(), ScheduleKind.CONFIRMED_DAY)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "publication",
    [
        PowerPublicationStatus.NO_SCHEDULE,
        PowerPublicationStatus.UNKNOWN,
        PowerPublicationStatus.PLANNED,
    ],
)
async def test_missing_or_unknown_plan_is_never_labelled_no_outages(ux, publication):
    lower, upper = day_bounds(NOW.date())
    intervals = (
        ()
        if publication is PowerPublicationStatus.NO_SCHEDULE
        else (PowerInterval("1.1", lower, upper, PowerState.UNKNOWN),)
    )
    schedule = PowerDaySchedule(
        PowerProvider.CEK,
        "1.1",
        NOW.date(),
        ScheduleKind.CONFIRMED_DAY,
        intervals,
        publication_status=publication,
    )
    ux.port.view = PowerScheduleView(
        PowerProvider.CEK, "1.1", SourceStatus.FRESH, NOW, NOW, schedule
    )
    view = await ux.schedule(1, NOW.date(), ScheduleKind.CONFIRMED_DAY)
    assert "Актуальний графік поки недоступний" in view.text
    assert "відключень немає" not in view.text
    assert "00:00–24:00" not in view.text
    assert "UNKNOWN" not in view.text and "NO_SCHEDULE" not in view.text


@pytest.mark.asyncio
async def test_partial_and_possible_intervals_never_imply_electricity_available(ux):
    lower, _ = day_bounds(NOW.date())
    schedule = PowerDaySchedule(
        PowerProvider.CEK,
        "1.1",
        NOW.date(),
        ScheduleKind.CONFIRMED_DAY,
        (
            PowerInterval(
                "1.1",
                lower + timedelta(hours=6),
                lower + timedelta(hours=7),
                PowerState.OUTAGE_CONFIRMED,
            ),
            PowerInterval(
                "1.1",
                lower + timedelta(hours=8),
                lower + timedelta(hours=9),
                PowerState.POSSIBLE_OUTAGE,
            ),
        ),
    )
    ux.port.view = PowerScheduleView(
        PowerProvider.CEK, "1.1", SourceStatus.FRESH, NOW, NOW, schedule
    )
    view = await ux.schedule(1, NOW.date(), ScheduleKind.CONFIRMED_DAY)
    assert "Планові відключення:\n06:00–07:00" in view.text
    assert "Можливі відключення:\n08:00–09:00" in view.text
    assert "Частину графіка ще не підтверджено" in view.text
    assert "00:00–06:00" not in view.text and "09:00–24:00" not in view.text
    assert "відключень немає" not in view.text


@pytest.mark.asyncio
async def test_explicit_zero_plan_labels_tomorrow_without_claiming_actual_supply(ux):
    tomorrow = NOW.date() + timedelta(days=1)
    lower, upper = day_bounds(tomorrow)
    schedule = PowerDaySchedule(
        PowerProvider.CEK,
        "1.1",
        tomorrow,
        ScheduleKind.CONFIRMED_DAY,
        (PowerInterval("1.1", lower, upper, PowerState.POWER_EXPECTED),),
    )
    ux.port.view = PowerScheduleView(
        PowerProvider.CEK, "1.1", SourceStatus.FRESH, NOW, NOW, schedule
    )
    view = await ux.schedule(1, tomorrow, ScheduleKind.CONFIRMED_DAY)
    assert "Завтра планових відключень немає" in view.text
    assert "Аварійні відключення можуть" in view.text
    assert "00:00–24:00" not in view.text


@pytest.mark.asyncio
async def test_partial_expected_only_coverage_is_not_an_explicit_zero_plan(ux):
    lower, _ = day_bounds(NOW.date())
    schedule = PowerDaySchedule(
        PowerProvider.CEK,
        "1.1",
        NOW.date(),
        ScheduleKind.CONFIRMED_DAY,
        (
            PowerInterval(
                "1.1",
                lower + timedelta(hours=6),
                lower + timedelta(hours=7),
                PowerState.POWER_EXPECTED,
            ),
        ),
    )
    ux.port.view = PowerScheduleView(
        PowerProvider.CEK, "1.1", SourceStatus.FRESH, NOW, NOW, schedule
    )
    view = await ux.schedule(1, NOW.date(), ScheduleKind.CONFIRMED_DAY)
    assert "Актуальний графік поки недоступний" in view.text
    assert "відключень немає" not in view.text
    assert "06:00–07:00" not in view.text


@pytest.mark.asyncio
async def test_dst_rendering_distinguishes_repeated_clock_hour(ux):
    selected = date(2026, 10, 25)
    schedule = PowerDaySchedule(
        PowerProvider.DTEK,
        "3.2",
        selected,
        ScheduleKind.CONFIRMED_DAY,
        (
            PowerInterval(
                "3.2",
                datetime(2026, 10, 25, 0, tzinfo=UTC),
                datetime(2026, 10, 25, 1, tzinfo=UTC),
                PowerState.OUTAGE_CONFIRMED,
            ),
        ),
    )
    ux.port.view = PowerScheduleView(
        PowerProvider.DTEK,
        "3.2",
        SourceStatus.FRESH,
        NOW,
        NOW,
        schedule,
    )
    view = await ux.schedule(1, selected, ScheduleKind.CONFIRMED_DAY)
    assert "03:00 UTC+0300–03:00 UTC+0200" in view.text
    with pytest.raises(ValueError):
        await ux.schedule(1, selected, ScheduleKind.CONFIRMED_DAY, page=1)


@pytest.mark.parametrize("mode", list(NotificationMode))
def test_power_main_menu_independent_from_alert_mode_and_default_off(mode):
    account = UserAccount(1, mode=mode)
    repository = SimpleNamespace(get_or_create=lambda _: account)
    old = TelegramApplication(regions=(), resolver=SimpleNamespace(), repository=repository)
    new = TelegramApplication(
        regions=(),
        resolver=SimpleNamespace(),
        repository=repository,
        power_outages_enabled=True,
    )
    assert "menu:power" not in callbacks(old.menu(1))
    assert "menu:power" in callbacks(new.menu(1))
    assert account.mode is mode


@pytest.mark.asyncio
async def test_custom_lead_real_dispatcher_state_navigation_and_private_actor(ux):
    account = UserAccount(1)
    accounts = SimpleNamespace(get_existing=AsyncMock(return_value=account))
    application = AsyncTelegramApplication(
        regions=(),
        resolver=SimpleNamespace(),
        repository=accounts,
        power_outages_enabled=True,
        power_port=ux.port,
    )
    application.power_ux = ux
    await ux.provider(1, PowerProvider.CEK)
    handle = ux._drafts[1].handle
    storage = BoundedMemoryStorage()
    dispatcher = Dispatcher(storage=storage)
    dispatcher.include_router(build_async_router(application))
    session = AsyncMock(return_value=True)
    bot = Bot("123456:LOCAL_TEST_ONLY_NOT_REAL", session=session)
    actor = User(id=1, is_bot=False, first_name="Test")
    message = Message(message_id=1, date=NOW, chat=Chat(id=1, type="private"), from_user=actor)
    callback = CallbackQuery(
        id="power",
        from_user=actor,
        chat_instance="local",
        message=message,
        data=f"pw:custom:{handle}:outage",
    )
    await dispatcher.feed_update(bot, Update(update_id=1, callback_query=callback))
    key = StorageKey(bot_id=bot.id, chat_id=1, user_id=1)
    assert "PowerFlow" in (await storage.get_state(key) or "")
    await dispatcher.feed_update(
        bot,
        Update(update_id=2, message=message.model_copy(update={"text": "25"})),
    )
    assert await storage.get_state(key) is None
    assert ux._drafts[1].outage_lead == 25 and not ux.port.saved
    away = callback.model_copy(update={"id": "away", "data": "menu:info"})
    await dispatcher.feed_update(bot, Update(update_id=3, callback_query=away))
    assert 1 not in ux._drafts
    await ux.provider(1, PowerProvider.CEK)
    handle = ux._drafts[1].handle
    foreign = callback.model_copy(
        update={
            "id": "foreign",
            "data": f"pw:save:{handle}",
            "message": message.model_copy(update={"chat": Chat(id=-1, type="group")}),
        }
    )
    await dispatcher.feed_update(bot, Update(update_id=4, callback_query=foreign))
    assert not ux.port.saved
    await storage.close()


@pytest.mark.asyncio
async def test_api_cache_mode_only_offers_verified_groups_and_today_tomorrow():
    port = FakePowerPort()
    ux = PowerUx(
        port,
        clock=lambda: NOW,
        supported_providers=(PowerProvider.DTEK, PowerProvider.CEK),
        notifications_available=False,
        forecast_available=False,
    )
    setup = await ux.home(1)
    assert {value for value in callbacks(setup) if value.startswith("pw:provider:")} == {
        "pw:provider:dtek_dnipro",
        "pw:provider:cek_dnipro",
    }
    assert "Дніпропетровської" in setup.text and "Інші регіони" in setup.text
    with pytest.raises(ValueError, match="provider not available"):
        await ux.provider(1, PowerProvider.DTEK_ODESA)
    await ux.provider(1, PowerProvider.CEK)
    handle = ux._drafts[1].handle
    review = ux.group(1, handle, "3.2")
    assert not any("notify" in key or "lead" in key or "toggle" in key for key in callbacks(review))
    assert "ще не підключені" in review.text
    assert "локальної бази" not in review.text and "кеш" not in review.text
    assert port.saved == []
    await ux.confirm(1, handle)
    subscription = port.subscriptions[1]
    assert not subscription.enabled
    assert not subscription.outage_notifications_enabled
    assert not subscription.restoration_notifications_enabled
    home = await ux.home(1)
    assert "pw:forecast" not in callbacks(home) and "pw:settings" not in callbacks(home)
    assert "Увімкнено" not in home.text
    assert "pw:day:2026-11-04:d:0" in callbacks(home)
    assert "pw:day:2026-11-05:d:0" in callbacks(home)
    assert not [key for key in callbacks(ux.forecast()) if key.startswith("pw:day:")]
    with pytest.raises(ValueError, match="forecast not available"):
        await ux.schedule(1, NOW.date(), ScheduleKind.FORECAST_WEEK)
    assert port.days == []
    for day in (NOW.date() - timedelta(days=1), NOW.date() + timedelta(days=2)):
        with pytest.raises(ValueError, match="date not available"):
            await ux.schedule(1, day, ScheduleKind.CONFIRMED_DAY)
    assert port.days == []
    shown = await ux.schedule(1, NOW.date(), ScheduleKind.CONFIRMED_DAY)
    assert {value.split(":")[2] for value in callbacks(shown) if value.startswith("pw:day:")} == {
        "2026-11-04",
        "2026-11-05",
    }


@pytest.mark.asyncio
async def test_api_cache_mode_rejects_forged_notification_actions_and_clamps_saved_flags():
    port = FakePowerPort()
    port.subscriptions[1] = PowerSubscription("1", PowerProvider.CEK, "1.1", True)
    ux = PowerUx(port, clock=lambda: NOW, notifications_available=False)
    await ux.settings(1)
    handle = ux._drafts[1].handle
    forbidden = [
        lambda: ux.notifications(1, handle, True),
        lambda: ux.notifications(1, handle, False),
        lambda: ux.leads(1, handle, "outage"),
        lambda: ux.lead(1, handle, "outage", 30),
        lambda: ux.custom(1, handle, "restoration", "5"),
        lambda: ux.toggle(1, handle, "enabled"),
        lambda: ux.toggle(1, handle, "outage_enabled"),
        lambda: ux.toggle(1, handle, "restoration_enabled"),
    ]
    for action in forbidden:
        with pytest.raises(ValueError, match="notifications not available"):
            action()
    assert not port.saved
    await ux.confirm(1, handle)
    assert not port.subscriptions[1].enabled
    assert not port.subscriptions[1].outage_notifications_enabled
    assert not port.subscriptions[1].restoration_notifications_enabled


@pytest.mark.asyncio
async def test_api_reminder_capability_defaults_to_ten_minutes_and_requires_opt_in_confirmation():
    port = FakePowerPort()
    ux = PowerUx(
        port,
        clock=lambda: NOW,
        supported_providers=(PowerProvider.DTEK, PowerProvider.CEK),
        notifications_available=True,
        forecast_available=False,
    )
    await ux.provider(1, PowerProvider.DTEK)
    handle = ux._drafts[1].handle
    prompt = ux.group(1, handle, "6.1")
    assert "за 10 хв" in prompt.text
    assert "офіційн" not in prompt.text and "джерел" not in prompt.text
    assert not ux._drafts[1].enabled
    assert not port.saved
    lead_choices = ux.notifications(1, handle, True)
    assert f"pw:lead:{handle}:outage:10" in callbacks(lead_choices)
    assert f"pw:custom:{handle}:outage" in callbacks(lead_choices)
    preview = ux.review(1, handle)
    assert "До відключення: 10 хв" in preview.text
    assert "До включення за графіком: 10 хв" in preview.text
    assert "графіка на сьогодні й завтра покажемо повністю" in preview.text
    assert "лише після підтвердження" in preview.text
    assert not port.saved
    await ux.confirm(1, handle)
    selected = port.subscriptions[1]
    assert selected.enabled
    assert selected.outage_notifications_enabled and selected.restoration_notifications_enabled
    assert (selected.outage_lead_minutes, selected.restoration_lead_minutes) == (10, 10)
    home = await ux.home(1)
    assert "pw:settings" in callbacks(home)
    assert "pw:forecast" not in callbacks(home)


@pytest.mark.asyncio
@pytest.mark.parametrize("outage,restore", [("0", "180"), ("17", "10")])
async def test_api_reminder_custom_times_and_individual_toggle_persist_only_after_confirmation(
    outage, restore,
):
    port = FakePowerPort()
    port.subscriptions[2] = PowerSubscription("2", PowerProvider.CEK, "2.1", False)
    original_other_actor = port.subscriptions[2]
    ux = PowerUx(
        port,
        clock=lambda: NOW,
        supported_providers=(PowerProvider.DTEK, PowerProvider.CEK),
        notifications_available=True,
        forecast_available=False,
    )
    await ux.provider(1, PowerProvider.CEK)
    handle = ux._drafts[1].handle
    ux.group(1, handle, "3.2")
    ux.notifications(1, handle, True)
    ux.custom(1, handle, "outage", outage)
    ux.custom(1, handle, "restoration", restore)
    ux.toggle(1, handle, "restoration_enabled")
    assert not port.saved
    await ux.confirm(1, handle)
    selected = port.subscriptions[1]
    assert selected.enabled and selected.outage_notifications_enabled
    assert not selected.restoration_notifications_enabled
    assert (selected.outage_lead_minutes, selected.restoration_lead_minutes) == (
        int(outage), int(restore)
    )
    assert port.subscriptions[2] is original_other_actor
    await ux.settings(1)
    edited = ux._drafts[1]
    assert (edited.outage_lead, edited.restoration_lead) == (int(outage), int(restore))


@pytest.mark.asyncio
async def test_opening_and_confirming_disabled_reminder_settings_never_auto_enables():
    port = FakePowerPort()
    port.subscriptions[1] = PowerSubscription(
        "1", PowerProvider.CEK, "1.1", False, False, False,
        outage_lead_minutes=42, restoration_lead_minutes=7,
    )
    ux = PowerUx(port, clock=lambda: NOW, notifications_available=True)
    await ux.settings(1)
    handle = ux._drafts[1].handle
    ux.custom(1, handle, "outage", "23")
    assert not port.saved
    await ux.confirm(1, handle)
    saved = port.subscriptions[1]
    assert not saved.enabled
    assert not saved.outage_notifications_enabled and not saved.restoration_notifications_enabled
    assert (saved.outage_lead_minutes, saved.restoration_lead_minutes) == (23, 7)


@pytest.mark.asyncio
async def test_stale_power_button_cannot_erase_another_feature_fsm_or_save():
    from aiogram import Router

    from time_tl.bot.power_router import build_power_router

    ux = PowerUx(FakePowerPort(), clock=lambda: NOW)
    await ux.provider(1, PowerProvider.CEK)
    handle = ux._drafts[1].handle
    storage = BoundedMemoryStorage()
    dispatcher, router = Dispatcher(storage=storage), Router()
    build_power_router(ux, router=router)
    dispatcher.include_router(router)
    session = AsyncMock(return_value=True)
    bot = Bot("123456:LOCAL_TEST_ONLY_NOT_REAL", session=session)
    actor = User(id=1, is_bot=False, first_name="Test")
    message = Message(message_id=1, date=NOW, chat=Chat(id=1, type="private"), from_user=actor)
    key = StorageKey(bot_id=bot.id, chat_id=1, user_id=1)
    await storage.set_state(key, "HomeworkFlow:text")
    await storage.set_data(key, {"draft": "synthetic-homework"})
    for index, raw in enumerate(("menu:power", f"pw:save:{handle}", "pw:setup")):
        callback = CallbackQuery(
            id=str(index), from_user=actor, chat_instance="local", message=message, data=raw
        )
        await dispatcher.feed_update(bot, Update(update_id=index, callback_query=callback))
        assert await storage.get_state(key) == "HomeworkFlow:text"
        assert await storage.get_data(key) == {"draft": "synthetic-homework"}
    assert not ux.port.saved
    await storage.close()


@pytest.mark.asyncio
async def test_cancelled_manual_subscription_read_cannot_resurrect_preview():
    port, started, release = FakePowerPort(), asyncio.Event(), asyncio.Event()
    original = port.subscription

    async def slow_subscription(actor):
        started.set()
        await release.wait()
        return await original(actor)

    port.subscription = slow_subscription
    ux = PowerUx(port, clock=lambda: NOW)
    task = asyncio.create_task(ux.provider(1, PowerProvider.CEK))
    await started.wait()
    ux.abandon(1)
    release.set()
    with pytest.raises(ValueError, match="operation abandoned"):
        await task
    assert not ux._drafts and not port.saved


@pytest.mark.asyncio
async def test_replacement_manual_operation_wins_over_late_subscription_read():
    port, started, release = FakePowerPort(), asyncio.Event(), asyncio.Event()
    original = port.subscription
    calls = 0

    async def first_slow_subscription(actor):
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            await release.wait()
        return await original(actor)

    port.subscription = first_slow_subscription
    ux = PowerUx(port, clock=lambda: NOW)
    task = asyncio.create_task(ux.provider(1, PowerProvider.CEK))
    await started.wait()
    await ux.provider(1, PowerProvider.DTEK)
    current = ux._drafts[1]
    release.set()
    with pytest.raises(ValueError, match="operation abandoned"):
        await task
    assert ux._drafts[1] is current and current.provider is PowerProvider.DTEK


@pytest.mark.asyncio
async def test_late_manual_route_response_cannot_overwrite_new_address_screen():
    from aiogram import F, Router

    from time_tl.bot.power_router import build_power_router

    port, started, release = FakePowerPort(), asyncio.Event(), asyncio.Event()

    async def slow_subscription(actor):
        started.set()
        await release.wait()
        return None

    port.subscription = slow_subscription
    ux = PowerUx(port, clock=lambda: NOW)
    storage = BoundedMemoryStorage()
    dispatcher, router = Dispatcher(storage=storage), Router()
    build_power_router(ux, router=router)

    @router.callback_query(F.data == "pa:start")
    async def address_action(callback):
        await callback.message.edit_text("synthetic-new-address-screen")

    dispatcher.include_router(router)
    session = AsyncMock(return_value=True)
    bot = Bot("123456:LOCAL_TEST_ONLY_NOT_REAL", session=session)
    actor = User(id=1, is_bot=False, first_name="Test")
    message = Message(message_id=1, date=NOW, chat=Chat(id=1, type="private"), from_user=actor)
    callback = CallbackQuery(
        id="old",
        from_user=actor,
        chat_instance="local",
        message=message,
        data="pw:provider:cek_dnipro",
    )
    task = asyncio.create_task(
        dispatcher.feed_update(bot, Update(update_id=1, callback_query=callback))
    )
    await started.wait()
    await dispatcher.feed_update(
        bot,
        Update(
            update_id=2,
            callback_query=callback.model_copy(update={"id": "new", "data": "pa:start"}),
        ),
    )
    release.set()
    await task
    texts = [
        call.args[1].text
        for call in session.await_args_list
        if getattr(call.args[1], "__api_method__", None) == "editMessageText"
    ]
    assert texts == ["synthetic-new-address-screen"]
    assert not ux._drafts and not port.saved
    await storage.close()

"""Cache-only known operator projections; no HTTP even when cache is unavailable."""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from time_tl.bot.power_service import PowerUx
from time_tl.domain.power_outages.map import MapArea, RegionMap
from time_tl.domain.power_outages.models import (
    GROUPS,
    PowerProvider,
    PowerPublicationStatus,
    PowerSourceKind,
    PowerState,
    ScheduleKind,
    SourceStatus,
)
from time_tl.infrastructure.db.power_map import PowerMapView
from time_tl.runtime.power_map import PowerMapScheduleReader
from time_tl.runtime.power_map_bot import PostgresMapPowerBotPort

NOW = datetime(2026, 11, 5, 12, tzinfo=UTC)
DAY = date(2026, 11, 5)
AREA = MapArea("dnipropetrovska-oblast", "UA" + "1" * 17, "Synthetic region")
DTEK = "dnipropetrovska_oblast_oblenergo"
CEK = "dnipropetrovska_oblast_cek"


def rows(supplier=DTEK):
    return [{"initiator": supplier, "queue": group, "updated": "2026-11-04T15:00:00Z",
             "today_date": "2026-11-05", "tomorrow_date": "2026-11-06",
             "today": [0, 1, 2, 3] + [0] * 20, "tomorrow": [],
             "today_status": "ScheduleApplies", "tomorrow_status": "WaitingForSchedule",
             "today_checked_at": "2026-11-05T11:58:00Z",
             "tomorrow_checked_at": "2026-11-05T11:58:00Z"} for group in GROUPS]


class Repository:
    stale_after_seconds = 1800

    def __init__(self, value=None):
        data = RegionMap(AREA, tuple(rows() if value is None else value), NOW)
        self.view = PowerMapView(AREA.slug, data.status, 0, data, 7, NOW, NOW,
                                 data.error_code, last_fetch_succeeded=True)
        self.calls = []

    async def read_region(self, slug, now):
        self.calls.append((slug, now))
        return self.view


async def read(repository, provider=PowerProvider.DTEK, day=DAY, now=NOW, **kwargs):
    return await PowerMapScheduleReader(repository, **kwargs).read_schedule(
        provider, "1.1", day, schedule_type=ScheduleKind.CONFIRMED_DAY, now=now,
    )


@pytest.fixture(autouse=True)
def no_http(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("user cache reads must never construct an HTTP client")
    monkeypatch.setattr("httpx.AsyncClient", forbidden)


async def test_known_provider_projection_reads_one_region_and_preserves_provenance():
    repository = Repository()
    value = await read(repository)
    assert value.source_status is SourceStatus.FRESH
    assert value.source_kind is PowerSourceKind.ALERTS_ENERGY_API
    assert value.schedule.provider is PowerProvider.DTEK and value.schedule.group_id == "1.1"
    assert value.schedule.intervals[0].state is PowerState.POWER_EXPECTED
    assert value.revision == 7 and value.last_success_at == NOW
    assert repository.calls == [(AREA.slug, NOW)]


async def test_other_operator_staleness_does_not_hide_selected_healthy_operator():
    other = rows(CEK)
    for row in other:
        row["today_checked_at"] = "2026-11-05T10:00:00Z"
    repository = Repository(rows() + other)
    repository.view = replace(repository.view, last_success_at=None)
    assert repository.view.source_status is SourceStatus.STALE
    value = await read(repository)
    assert value.source_status is SourceStatus.FRESH and value.last_success_at == NOW
    assert (await read(repository, PowerProvider.CEK)).source_status is SourceStatus.STALE


async def test_cek_projection_never_uses_dtek_rows():
    repository = Repository(rows(DTEK) + rows(CEK))
    value = await read(repository, PowerProvider.CEK)
    assert value.source_status is SourceStatus.FRESH
    assert value.schedule.provider is PowerProvider.CEK
    assert (await read(Repository(rows(DTEK)), PowerProvider.CEK)).schedule is None


@pytest.mark.parametrize("provider", [PowerProvider.DTEK_KYIV, PowerProvider.DTEK_KYIV_REGION,
                                     PowerProvider.DTEK_ODESA, PowerProvider.DTEK_DONETSK])
async def test_unverified_regional_mapping_never_guessed(provider):
    repository = Repository()
    value = await read(repository, provider)
    assert value.source_status is SourceStatus.UNAVAILABLE and value.schedule is None
    assert repository.calls == []


@pytest.mark.parametrize("code", ["HTTP_ERROR", "REGION_MAPPING_CHANGED", "REGION_REMOVED"])
async def test_failed_fetch_or_mapping_change_cannot_reuse_old_fresh_data(code):
    repository = Repository()
    repository.view = replace(repository.view, source_status=SourceStatus.STALE,
                              error_code=code, last_fetch_succeeded=False)
    value = await read(repository)
    assert value.source_status is SourceStatus.STALE and value.schedule is None


async def test_missing_cache_is_unavailable_without_provider_or_http_fallback():
    repository = Repository()
    repository.view = replace(repository.view, data=None, last_fetch_succeeded=False,
                              source_status=SourceStatus.UNAVAILABLE)
    value = await read(repository)
    assert value.source_status is SourceStatus.UNAVAILABLE and value.schedule is None


@pytest.mark.parametrize("now", [NOW + timedelta(minutes=40), NOW - timedelta(seconds=1)])
async def test_fetch_cache_age_and_future_clock_fail_closed(now):
    value = await read(Repository(), now=now)
    assert value.source_status is SourceStatus.STALE and value.schedule is None


async def test_repository_shorter_ttl_is_honored_by_default():
    repository = Repository()
    repository.stale_after_seconds = 60
    value = await read(repository)
    assert value.source_status is SourceStatus.STALE and value.schedule is None


@pytest.mark.parametrize("status", ["EmergencyShutdowns", "WaitingForSchedule"])
async def test_emergency_and_no_schedule_are_not_positive_power_plans(status):
    value = rows()
    for row in value:
        row["today_status"], row["today"] = status, []
        row["today_published_schedule"] = {"hours": [0] * 24, "date_slug": "2026-11-04",
                                           "status": "NoShutdowns"}
    result = await read(Repository(value))
    assert result.source_status is SourceStatus.FRESH
    publication = PowerPublicationStatus.EMERGENCY if status == "EmergencyShutdowns" \
        else PowerPublicationStatus.NO_SCHEDULE
    assert result.schedule.publication_status is publication
    assert all(item.state is PowerState.UNKNOWN for item in result.schedule.intervals)


async def test_unclassified_local_announcement_suppresses_zero_plan_as_unknown():
    value = rows()
    for row in value:
        row["today_announcements"] = [{"kind": "Emergency", "state": "active",
                                       "scope": "local"}]
    result = await read(Repository(value))
    assert result.schedule.publication_status is PowerPublicationStatus.UNKNOWN
    assert result.schedule.intervals[0].state is PowerState.UNKNOWN


async def test_source_check_ttl_is_distinct_from_old_publication_time():
    value = rows()
    for row in value:
        row["today_checked_at"] = "2026-11-05T10:00:00Z"
    assert (await read(Repository(value))).source_status is SourceStatus.STALE
    assert (await read(Repository())).source_status is SourceStatus.FRESH


async def test_missing_group_or_partial_known_operator_never_synthesizes_plan():
    value = await read(Repository(rows()[:-1]))
    assert value.source_status is SourceStatus.INVALID and value.schedule is None


async def test_telegram_shows_selected_group_from_dynamic_cache(monkeypatch):
    row = next(row for row in rows() if row["queue"] == "6.1")
    row["today"] = [2, 0, 0, 0, 0, 3, 1, 1, 1, 2, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 0, 0, 0, 0]
    row["today_announcements"] = [{"kind": "GPV", "state": "active", "scope": "supplier"}]
    repository = Repository([row])
    monkeypatch.setattr(
        "time_tl.runtime.power_map_bot.PostgresPowerMapRepository",
        lambda *args, **kwargs: repository,
    )
    monkeypatch.setattr(
        "time_tl.runtime.power_map_bot.datetime", SimpleNamespace(now=lambda _: NOW)
    )
    port = PostgresMapPowerBotPort(None)
    monkeypatch.setattr(
        port, "subscription", AsyncMock(return_value=SimpleNamespace(
            provider=PowerProvider.DTEK, group_id="6.1",
        )),
    )
    card = await PowerUx(port, clock=lambda: NOW).schedule(1, DAY, ScheduleKind.CONFIRMED_DAY)
    for interval in ("00:00–00:30", "05:30–09:30", "16:00–20:00"):
        assert interval in card.text
    assert "Актуальний графік поки недоступний" not in card.text
    assert repository.calls == [(AREA.slug, NOW)]

    # The stricter projection used by reminder workers still requires all groups.
    strict = await PowerMapScheduleReader(repository).read_schedule(
        PowerProvider.DTEK, "6.1", DAY, schedule_type=ScheduleKind.CONFIRMED_DAY, now=NOW
    )
    assert strict.source_status is SourceStatus.INVALID and strict.schedule is None


@pytest.mark.parametrize("scenario", ["stale", "missing_group", "unchecked_day", "failed_fetch"])
async def test_partial_display_requires_fresh_exact_group_and_day(scenario):
    row = next(row for row in rows() if row["queue"] == "1.1")
    day = DAY
    if scenario == "stale":
        row["today_checked_at"] = "2026-11-05T10:00:00Z"
    elif scenario == "missing_group":
        row["queue"] = "6.1"
    elif scenario == "unchecked_day":
        day += timedelta(days=1)
        del row["tomorrow_checked_at"]
    repository = Repository([row])
    if scenario == "failed_fetch":
        repository.view = replace(repository.view, last_fetch_succeeded=False)
    view = await PowerMapScheduleReader(repository).read_schedule(
        PowerProvider.DTEK, "1.1", day, schedule_type=ScheduleKind.CONFIRMED_DAY,
        now=NOW, display_only=True,
    )
    assert view.source_status is not SourceStatus.FRESH and view.schedule is None
    if scenario == "missing_group":
        assert view.error_code == "MAP_GROUP_PENDING"


async def test_forecast_not_fabricated_from_daily_cache():
    repository = Repository()
    value = await PowerMapScheduleReader(repository).read_schedule(
        PowerProvider.DTEK, "1.1", DAY, schedule_type=ScheduleKind.FORECAST_WEEK, now=NOW,
    )
    assert value.source_status is SourceStatus.UNAVAILABLE and repository.calls == []


async def test_requested_day_must_be_cached_and_have_own_source_check():
    repository = Repository()
    assert (await read(repository, day=DAY + timedelta(days=2))).source_status \
        is SourceStatus.UNAVAILABLE
    assert (await read(repository, day=DAY + timedelta(days=1))).schedule.publication_status \
        is PowerPublicationStatus.NO_SCHEDULE


@pytest.mark.parametrize("status", [SourceStatus.INVALID, SourceStatus.UNAVAILABLE])
async def test_explicit_failed_region_status_cannot_be_overridden_by_payload(status):
    repository = Repository()
    repository.view = replace(repository.view, source_status=status)
    result = await read(repository)
    assert result.source_status is status and result.schedule is None

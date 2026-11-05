"""Offline source gates and explicitly SYNTHETIC provider script tests."""

from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from time_tl.domain.power_outages.models import (
    PowerDaySchedule,
    PowerInterval,
    PowerProvider,
    PowerPublicationStatus,
    PowerScheduleSnapshot,
    PowerSourceKind,
    PowerState,
    ScheduleKind,
    SourceStatus,
)
from time_tl.domain.power_outages.normalization import day_bounds
from time_tl.infrastructure.power_outages import (
    CekPowerScheduleProvider,
    DtekPowerScheduleProvider,
    FakePowerScheduleProvider,
    PowerFetchError,
    PowerFetchErrorCode,
    PowerSourceGate,
)

NOW = datetime(2026, 11, 4, 10, 5, tzinfo=UTC)


@pytest.mark.parametrize("provider_type,provider_id,reason", [
    (DtekPowerScheduleProvider, PowerProvider.DTEK, "SOURCE_POLICY_UNVERIFIED"),
    (CekPowerScheduleProvider, PowerProvider.CEK, "SOURCE_DATE_CONTRACT_UNVERIFIED"),
])
async def test_default_adapters_are_explicitly_unavailable_without_network(
    monkeypatch, provider_type, provider_id, reason
):
    def forbidden(*args, **kwargs):
        raise AssertionError("official gate must refuse before network construction")

    monkeypatch.setattr("httpx.AsyncClient", forbidden)
    provider = provider_type(clock=lambda: NOW)
    for _ in range(2):
        snapshot = await provider.fetch_schedules()
        assert snapshot.provider is provider_id
        assert snapshot.status is SourceStatus.UNAVAILABLE
        assert snapshot.days == () and snapshot.source_updated_at is None
        assert snapshot.error_code == reason and snapshot.fetched_at == NOW
    assert provider.source_gate.live_access_approved is False
    assert provider.source_gate.verified_endpoint is None
    assert len(provider.source_gate.supported_internal_groups) == 12
    with pytest.raises(FrozenInstanceError):
        provider.source_gate.live_access_approved = True


async def test_cancelled_unavailable_fetch_is_not_turned_into_schedule():
    task = asyncio.create_task(DtekPowerScheduleProvider(clock=lambda: NOW).fetch_schedules())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.parametrize("environment", ["production", "staging", "", "PRODUCTION", None, 0])
def test_fake_provider_rejects_nonoffline_environment(environment):
    value = PowerScheduleSnapshot(PowerProvider.DTEK, NOW, None, (), SourceStatus.UNAVAILABLE)
    with pytest.raises(ValueError, match="forbidden"):
        FakePowerScheduleProvider(PowerProvider.DTEK, [value], environment=environment)


@pytest.mark.parametrize("environment", ["local", "test", "simulation"])
async def test_scripted_fake_is_synthetic_bounded_and_does_not_implicitly_repeat(environment):
    value = PowerScheduleSnapshot(PowerProvider.DTEK, NOW, None, (), SourceStatus.UNAVAILABLE)
    error = PowerFetchError(
        PowerFetchErrorCode.RATE_LIMITED, status_code=429, retry_after_seconds=30
    )
    provider = FakePowerScheduleProvider(
        PowerProvider.DTEK, [value, error], environment=environment, clock=lambda: NOW
    )
    assert provider.fixture_kind == "SYNTHETIC"
    assert await provider.fetch_schedules() == value
    with pytest.raises(PowerFetchError) as raised:
        await provider.fetch_schedules()
    assert raised.value is error
    exhausted = await provider.fetch_schedules()
    assert exhausted.status is SourceStatus.UNAVAILABLE
    assert exhausted.days == () and exhausted.error_code == "FAKE_SCRIPT_EXHAUSTED"


async def test_fake_script_cancellation_propagates_and_is_not_positive_data():
    provider = FakePowerScheduleProvider(
        PowerProvider.DTEK, [asyncio.CancelledError()], environment="test", clock=lambda: NOW
    )
    with pytest.raises(asyncio.CancelledError):
        await provider.fetch_schedules()
    assert (await provider.fetch_schedules()).status is SourceStatus.UNAVAILABLE


@pytest.mark.parametrize("script", [[], [object()], [RuntimeError("PRIVATE")], [None]])
def test_fake_rejects_unvalidated_script_items(script):
    with pytest.raises(ValueError):
        FakePowerScheduleProvider(PowerProvider.DTEK, script, environment="test")


def test_fake_rejects_other_provider_and_unmarked_fixture():
    value = PowerScheduleSnapshot(PowerProvider.CEK, NOW, None, (), SourceStatus.UNAVAILABLE)
    with pytest.raises(ValueError):
        FakePowerScheduleProvider(PowerProvider.DTEK, [value], environment="test")
    with pytest.raises(ValueError):
        FakePowerScheduleProvider(
            PowerProvider.CEK, [value], environment="test", fixture_kind="OFFICIAL"
        )


@pytest.mark.parametrize("retry", [True, -1, 86401, float("nan"), float("inf"), "30"])
def test_error_rejects_invalid_retry_after_instead_of_silently_clamping(retry):
    with pytest.raises(ValueError):
        PowerFetchError(PowerFetchErrorCode.RATE_LIMITED, retry_after_seconds=retry)


@pytest.mark.parametrize("status", [True, 200, 399, 600, "429"])
def test_error_rejects_invalid_http_status(status):
    with pytest.raises(ValueError):
        PowerFetchError(PowerFetchErrorCode.HTTP_ERROR, status_code=status)


def test_error_is_closed_and_access_denial_is_permanent():
    with pytest.raises(ValueError):
        PowerFetchError("PRIVATE response body")
    error = PowerFetchError(PowerFetchErrorCode.ACCESS_DENIED, status_code=403)
    assert str(error) == "ACCESS_DENIED" and error.permanent is True
    with pytest.raises(ValueError):
        PowerFetchError(PowerFetchErrorCode.RATE_LIMITED, status_code=500)
    with pytest.raises(ValueError):
        PowerSourceGate(PowerProvider.DTEK, "SOURCE_POLICY_UNVERIFIED")


async def test_observed_empty_cek_fixture_never_becomes_expected_power():
    fixture = Path(__file__).parents[1] / "fixtures/power_sources/cek-no-records.observed.html"
    assert "На цей день відключень немає." in fixture.read_text(encoding="utf-8")
    result = await CekPowerScheduleProvider(clock=lambda: NOW).fetch_schedules()
    assert result.status is SourceStatus.UNAVAILABLE and not result.days


async def test_naive_clock_is_rejected():
    provider = CekPowerScheduleProvider(clock=lambda: NOW.replace(tzinfo=None))
    with pytest.raises(ValueError):
        await provider.fetch_schedules()


@pytest.mark.parametrize("publication", [
    PowerPublicationStatus.EMERGENCY, PowerPublicationStatus.NO_SCHEDULE,
])
@pytest.mark.parametrize("source,url", [
    (PowerSourceKind.ALERTS_ENERGY_HTML, "https://alerts.energy/"),
    (PowerSourceKind.BEZSVITLA, "https://bezsvitla.com.ua/"),
])
async def test_fake_preserves_publication_and_aggregator_provenance(publication, source, url):
    day = date(2026, 11, 4)
    lower, upper = day_bounds(day)
    intervals = () if publication is PowerPublicationStatus.NO_SCHEDULE else (
        PowerInterval("1.1", lower, upper, PowerState.UNKNOWN),
    )
    schedule = PowerDaySchedule(
        PowerProvider.DTEK, "1.1", day, ScheduleKind.CONFIRMED_DAY, intervals,
        publication_status=publication,
    )
    value = PowerScheduleSnapshot(
        PowerProvider.DTEK, NOW, NOW, (schedule,), SourceStatus.FRESH,
        source_kind=source, source_url=url, locality="SYNTHETIC locality", source_checked_at=NOW,
    )
    provider = FakePowerScheduleProvider(PowerProvider.DTEK, [value], environment="test")
    result = await provider.fetch_schedules()
    assert result == value
    assert result.source_kind is source and result.source_url == url
    assert result.locality == "SYNTHETIC locality"
    assert result.days[0].publication_status is publication

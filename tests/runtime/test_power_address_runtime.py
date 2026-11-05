"""Lazy credentials, directory capability lifetime and process-wide test pacing."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import SecretStr

from time_tl.infrastructure.db.energy_api_credentials import EnergyApiCheckStatus, EnergyApiStatus
from time_tl.infrastructure.power_outages.alerts_energy_api import AlertsEnergyApiClient
from time_tl.infrastructure.power_outages.base import PowerFetchError, PowerFetchErrorCode
from time_tl.runtime import power_address as module

NOW = datetime(2026, 11, 4, 15, tzinfo=UTC)


def credentials(status):
    return Mock(
        status=AsyncMock(return_value=status), load=AsyncMock(return_value=SecretStr("synthetic"))
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [
        EnergyApiStatus(False, 0, None, EnergyApiCheckStatus.NOT_CHECKED),
        EnergyApiStatus(True, 1, None, EnergyApiCheckStatus.NOT_CHECKED),
        EnergyApiStatus(True, 1, NOW, EnergyApiCheckStatus.RATE_LIMITED),
    ],
)
async def test_no_key_load_or_network_before_verified_access(status):
    store = credentials(status)
    port = module.PostgresPowerAddressPort(Mock(), store, clock=lambda: NOW)
    with pytest.raises(ValueError, match="access_unverified"):
        await port._current()
    store.load.assert_not_awaited()


@pytest.mark.asyncio
async def test_one_directory_preserves_choices_until_key_revision_changes(monkeypatch):
    store = credentials(EnergyApiStatus(True, 1, NOW, EnergyApiCheckStatus.VALID))
    factory = Mock(side_effect=[Mock(), Mock()])
    monkeypatch.setattr(module, "AlertsEnergyAddressDirectory", factory)
    port = module.PostgresPowerAddressPort(Mock(), store, clock=lambda: NOW)
    first = await port._current()
    assert await port._current() is first
    store.load.assert_awaited_once()
    store.status.return_value = EnergyApiStatus(True, 2, NOW, EnergyApiCheckStatus.VALID)
    assert await port._current() is not first
    assert store.load.await_count == 2
    store.status.return_value = EnergyApiStatus(False, 3, None, EnergyApiCheckStatus.NOT_CHECKED)
    with pytest.raises(ValueError):
        await port._current()
    assert port._directory is None


@pytest.mark.asyncio
async def test_rotated_key_during_load_never_issues_directory(monkeypatch):
    store = credentials(EnergyApiStatus(True, 1, NOW, EnergyApiCheckStatus.VALID))
    store.status.side_effect = [
        EnergyApiStatus(True, 1, NOW, EnergyApiCheckStatus.VALID),
        EnergyApiStatus(True, 2, NOW, EnergyApiCheckStatus.VALID),
    ]
    factory = Mock()
    monkeypatch.setattr(module, "AlertsEnergyAddressDirectory", factory)
    port = module.PostgresPowerAddressPort(Mock(), store, clock=lambda: NOW)
    with pytest.raises(ValueError, match="access_changed"):
        await port._current()
    factory.assert_not_called()


@pytest.mark.asyncio
async def test_quota_pause_is_shared_across_process_clients_even_after_key_change(monkeypatch):
    parent = AsyncMock(
        side_effect=PowerFetchError(
            PowerFetchErrorCode.RATE_LIMITED,
            status_code=429,
            retry_after_seconds=3600,
        )
    )
    monkeypatch.setattr(AlertsEnergyApiClient, "_get", parent)
    monkeypatch.setattr(module, "monotonic", lambda: 100.0)
    pacer = module.AddressRequestPacer()
    first = module._AddressApiClient(api_key=SecretStr("synthetic-old"), pacer=pacer)
    second = module._AddressApiClient(api_key=SecretStr("synthetic-new"), pacer=pacer)
    with pytest.raises(PowerFetchError):
        await first._get("address-directory/street-search")
    with pytest.raises(PowerFetchError) as error:
        await second._get("address-directory/street-search")
    assert error.value.retry_after_seconds == 3600
    assert parent.await_count == 1

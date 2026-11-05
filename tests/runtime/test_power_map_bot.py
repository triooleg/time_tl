"""API map user selections cannot enable notifications or invoke HTTP."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from time_tl.admin.settings import reject_sensitive_generic_settings
from time_tl.domain.power_outages.models import PowerProvider
from time_tl.infrastructure.db.energy_api_credentials import EnergyApiCheckStatus, EnergyApiStatus
from time_tl.infrastructure.db.power_map_preferences import decode, preference_key
from time_tl.infrastructure.power_outages.alerts_energy_api import AlertsEnergyApiClient
from time_tl.runtime.power_address import PostgresPowerAddressPort
from time_tl.runtime.power_map_bot import PostgresMapPowerBotPort


@pytest.mark.parametrize(
    "flags", [(True, False, False), (False, True, False), (False, False, True)]
)
async def test_forged_notification_flags_rejected_before_db(flags):
    factory = Mock(side_effect=AssertionError("DB must not be touched"))
    port = PostgresMapPowerBotPort(factory)
    with pytest.raises(ValueError, match="notifications_unavailable"):
        await port.save(
            42,
            PowerProvider.DTEK,
            "1.1",
            enabled=flags[0],
            outage_enabled=flags[1],
            restoration_enabled=flags[2],
            outage_lead=30,
            restoration_lead=15,
            expected_revision=None,
        )


def test_private_preference_shape_and_legacy_generic_settings_guard():
    user = str(uuid4())
    value = decode({"version": 1, "provider": "dtek_dnipro", "group": "1.1", "revision": 1}, user)
    assert not value.enabled and not value.outage_notifications_enabled
    assert not value.restoration_notifications_enabled
    with pytest.raises(ValueError):
        reject_sensitive_generic_settings((preference_key(user),))
    with pytest.raises(ValueError):
        decode({"version": 1, "provider": "dtek_kyiv", "group": "1.1", "revision": 1}, user)
    with pytest.raises(ValueError):
        decode(
            {
                "version": 1,
                "provider": "dtek_dnipro",
                "group": "1.1",
                "revision": 1,
                "street": "must never persist",
            },
            user,
        )


async def test_revoked_key_while_budget_waits_never_sends_address_http(monkeypatch):
    now = datetime(2026, 11, 4, 12, tzinfo=UTC)
    from pydantic import SecretStr

    store = Mock(
        status=AsyncMock(return_value=EnergyApiStatus(True, 1, now, EnergyApiCheckStatus.VALID)),
        load=AsyncMock(return_value=SecretStr("fake")),
    )

    async def reserve(_):
        store.status.return_value = EnergyApiStatus(
            False, 2, None, EnergyApiCheckStatus.NOT_CHECKED
        )
        return 0

    budget = Mock(reserve=AsyncMock(side_effect=reserve))
    parent = AsyncMock(side_effect=AssertionError("HTTP forbidden after revocation"))
    monkeypatch.setattr(AlertsEnergyApiClient, "_get", parent)
    port = PostgresPowerAddressPort(Mock(), store, clock=lambda: now, budget=budget)
    directory = await port._current()
    # Exercise its issued client directly: no address contents are required to
    # prove the successful-but-queued quota reservation is fenced afterwards.
    with pytest.raises(ValueError, match="access_changed"):
        await directory._client._get("atus/settlement-search")
    parent.assert_not_awaited()

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import SecretStr

from time_tl.infrastructure.db.energy_api_budget import QuotaDeferred
from time_tl.infrastructure.power_outages.base import PowerFetchError
from time_tl.infrastructure.power_outages.budget_client import BudgetedEnergyApiClient

NOW = datetime(2026, 11, 4, 12, tzinfo=UTC)


@pytest.mark.asyncio
async def test_local_spacing_retries_admission_without_http_or_charging_twice():
    budget = SimpleNamespace(
        reserve=AsyncMock(side_effect=[QuotaDeferred(0.25, "quota_spacing"), 0]), defer=AsyncMock()
    )
    calls = []
    client = BudgetedEnergyApiClient(
        api_key=SecretStr("synthetic"),
        budget=budget,
        clock=lambda: NOW,
        sleep=AsyncMock(),
        transport=httpx.MockTransport(
            lambda r: calls.append(r.url.path) or httpx.Response(200, json={"ok": True})
        ),
    )
    assert await client.check_access()
    assert calls == ["/api/v1/check"] and budget.reserve.await_count == 2


@pytest.mark.asyncio
async def test_shared_quota_denial_never_calls_network():
    budget = SimpleNamespace(
        reserve=AsyncMock(side_effect=QuotaDeferred(600, "quota_blocked")), defer=AsyncMock()
    )
    client = BudgetedEnergyApiClient(
        api_key=SecretStr("synthetic"),
        budget=budget,
        clock=lambda: NOW,
        transport=httpx.MockTransport(lambda r: pytest.fail("network forbidden")),
    )
    with pytest.raises(PowerFetchError) as error:
        await client.check_access()
    assert error.value.retry_after_seconds == 600


@pytest.mark.asyncio
async def test_provider_429_persists_shared_cooldown():
    budget = SimpleNamespace(reserve=AsyncMock(return_value=0), defer=AsyncMock())
    client = BudgetedEnergyApiClient(
        api_key=SecretStr("synthetic"),
        budget=budget,
        clock=lambda: NOW,
        transport=httpx.MockTransport(
            lambda r: httpx.Response(429, headers={"Retry-After": "600"})
        ),
    )
    with pytest.raises(PowerFetchError):
        await client.check_access()
    budget.defer.assert_awaited_once_with(NOW, 600)

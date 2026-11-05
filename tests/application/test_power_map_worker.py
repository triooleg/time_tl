"""Central collection cadence/failures without network, Telegram or secrets."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr

from time_tl.application.power_map_worker import PowerMapWorker
from time_tl.domain.power_outages.map import MapArea
from time_tl.infrastructure.db.energy_api_credentials import EnergyApiCheckStatus
from time_tl.infrastructure.power_outages.base import PowerFetchError, PowerFetchErrorCode

NOW = datetime(2026, 11, 4, 12, tzinfo=UTC)
AREAS = (
    MapArea("area-one", "UA12000000000090473", "Область 1"),
    MapArea("area-two", "UA32000000000030281", "Область 2"),
)


def setup_worker(*, begin="cycle", valid=True):
    repo = SimpleNamespace(
        begin_cycle=AsyncMock(return_value=begin),
        cycle_current=AsyncMock(return_value=True),
        finish_cycle=AsyncMock(return_value=True),
        fail_cycle=AsyncMock(),
    )
    status = SimpleNamespace(
        configured=True,
        revision=1,
        check_status=(EnergyApiCheckStatus.VALID if valid else EnergyApiCheckStatus.INVALID),
    )
    creds = SimpleNamespace(
        status=AsyncMock(return_value=status),
        load=AsyncMock(return_value=SecretStr("synthetic-test")),
    )
    lease = SimpleNamespace(renew=AsyncMock(return_value=True))
    api = SimpleNamespace(
        overview=AsyncMock(return_value=AREAS), region=AsyncMock(side_effect=["map1", "map2"])
    )
    clients = []

    def client_factory(**kwargs):
        clients.append(kwargs)
        return object()

    worker = PowerMapWorker(
        repo,
        creds,
        object(),
        lease,
        clock=lambda: NOW,
        api_factory=lambda *a, **k: api,
        client_factory=client_factory,
    )
    return worker, repo, creds, api, clients, lease


@pytest.mark.asyncio
async def test_due_cycle_fetches_each_region_once_then_commits_all():
    worker, repo, _, api, clients, _ = setup_worker()
    assert await worker.run_once()
    assert api.overview.await_count == 1 and api.region.await_count == 2
    args = repo.finish_cycle.await_args.args
    assert args[1] == AREAS and args[2] == {"area-one": "map1", "area-two": "map2"}
    await clients[0]["before_request"]()
    repo.cycle_current.assert_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("begin,valid", [(None, True), ("cycle", False)])
async def test_no_key_or_not_due_never_loads_key_or_calls_api(begin, valid):
    worker, _, creds, api, clients, _ = setup_worker(begin=begin, valid=valid)
    assert not await worker.run_once()
    creds.load.assert_not_awaited()
    api.overview.assert_not_awaited()
    assert clients == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code", [PowerFetchErrorCode.RATE_LIMITED, PowerFetchErrorCode.ACCESS_DENIED]
)
async def test_provider_quota_or_auth_denial_stops_remaining_region_calls(code):
    worker, repo, _, api, _, _ = setup_worker()
    api.region.side_effect = PowerFetchError(code)
    assert await worker.run_once()
    assert api.region.await_count == 1
    assert repo.finish_cycle.await_args.args[3] == {
        "area-one": code.value,
        "area-two": "NOT_FETCHED",
    }


@pytest.mark.asyncio
async def test_one_bad_region_does_not_poison_collected_other_region():
    worker, repo, _, api, _, _ = setup_worker()
    api.region.side_effect = [PowerFetchError(PowerFetchErrorCode.INVALID_RESPONSE), "map2"]
    assert await worker.run_once()
    args = repo.finish_cycle.await_args.args
    assert args[2] == {"area-two": "map2"}
    assert args[3] == {"area-one": "INVALID_RESPONSE"}


@pytest.mark.asyncio
async def test_failed_overview_marks_whole_existing_cache_unavailable():
    worker, repo, _, api, _, _ = setup_worker()
    api.overview.side_effect = PowerFetchError(PowerFetchErrorCode.TIMEOUT)
    assert not await worker.run_once()
    repo.finish_cycle.assert_not_awaited()
    assert repo.fail_cycle.await_args.args[1] == "TIMEOUT"


@pytest.mark.asyncio
async def test_excess_country_coverage_is_not_silently_truncated():
    worker, repo, _, api, _, _ = setup_worker()
    api.overview.return_value = AREAS * 17
    assert not await worker.run_once()
    api.region.assert_not_awaited()
    assert repo.fail_cycle.await_args.args[1] == "QUOTA_DEFERRED"


@pytest.mark.asyncio
async def test_lost_lease_before_commit_never_commits():
    worker, repo, _, _, _, lease = setup_worker()
    lease.renew.side_effect = [True, False]
    assert not await worker.run_once()
    repo.finish_cycle.assert_not_awaited()


@pytest.mark.asyncio
async def test_rotated_key_fences_before_next_http():
    worker, repo, creds, _, clients, _ = setup_worker()
    assert await worker.run_once()
    creds.status.return_value = SimpleNamespace(
        configured=True, revision=2, check_status=EnergyApiCheckStatus.VALID
    )
    with pytest.raises(RuntimeError, match="credential_changed"):
        await clients[0]["before_request"]()
    repo.cycle_current.return_value = False
    with pytest.raises(RuntimeError, match="cycle_lost"):
        await clients[0]["before_request"]()

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from tests.application.test_power_map_worker import NOW, setup_worker
from tests.infrastructure.test_power_map_repository import region
from time_tl.application.power_coverage import demanded_areas, retain_demanded_groups
from time_tl.domain.power_outages.map import MapArea, RegionMap
from time_tl.domain.power_outages.models import GROUPS as OPERATOR_GROUPS
from time_tl.domain.power_outages.models import PowerProvider
from time_tl.infrastructure.db.power_coverage import locked_power_groups
from time_tl.infrastructure.db.power_map_preferences import preference_key

AREA = MapArea("dnipropetrovska-oblast", "UA12000000000090473", "Область")
GROUPS = frozenset({(PowerProvider.DTEK, "1.1")})


def test_filter_retains_only_exact_operator_group_not_air_region_or_names():
    first = region(area=AREA, supplier="dnipropetrovska_oblast_oblenergo")
    wire = first.to_wire()
    wire["rows"].append({**wire["rows"][0], "queue": "6.1"})
    wire["rows"].append({**wire["rows"][0], "initiator": "dnipropetrovska_oblast_cek"})
    snapshot = RegionMap.from_wire(wire)
    filtered = retain_demanded_groups(snapshot, GROUPS)
    assert len(filtered.rows) == 1
    assert filtered.rows[0]["initiator"] == "dnipropetrovska_oblast_oblenergo"
    assert filtered.rows[0]["queue"] == "1.1"
    assert not retain_demanded_groups(
        replace(snapshot, area=MapArea("other-oblast", "UA32000000000030281", "Область")), GROUPS
    ).rows
    assert demanded_areas(GROUPS) == {AREA.slug}


@pytest.mark.asyncio
async def test_notifications_retain_full_operator_without_expanding_area_demand():
    actor = uuid4()
    row = SimpleNamespace(
        key=preference_key(str(actor)),
        value={
            "version": 2, "provider": PowerProvider.DTEK.value, "group": "1.1",
            "revision": 1, "enabled": True, "outage_enabled": True,
            "restoration_enabled": True, "outage_lead": 10, "restoration_lead": 10,
        },
    )
    for enabled in (True, False):
        row.value["enabled"] = enabled
        session = SimpleNamespace(execute=AsyncMock(side_effect=[
            SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [actor])),
            SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [row])),
        ]))
        demand = await locked_power_groups(session)
        expected = OPERATOR_GROUPS if enabled else ("1.1",)
        assert demand == {(PowerProvider.DTEK, group) for group in expected}
        assert demanded_areas(demand) == {AREA.slug}


@pytest.mark.asyncio
async def test_zero_demand_prunes_cache_and_makes_zero_source_calls():
    worker, repo, credentials, api, clients, _ = setup_worker()
    worker._coverage = SimpleNamespace(
        snapshot=AsyncMock(return_value=SimpleNamespace(power_group_keys=frozenset()))
    )
    repo.prune_unused_groups = AsyncMock()
    assert not await worker.run_once()
    repo.prune_unused_groups.assert_awaited_once_with(
        NOW, recent_maps={}, credential_revision=None
    )
    credentials.load.assert_not_awaited()
    api.overview.assert_not_awaited()
    assert not clients


@pytest.mark.asyncio
async def test_only_demanded_area_is_requested_once_for_shared_group():
    worker, repo, _, api, _, _ = setup_worker()
    worker._coverage = SimpleNamespace(
        snapshot=AsyncMock(return_value=SimpleNamespace(power_group_keys=GROUPS))
    )
    repo.prune_unused_groups = AsyncMock()
    api.overview.return_value = (AREA, MapArea("other-oblast", "UA32000000000030281", "Інша"))
    api.region.side_effect = [region(area=AREA, supplier="dnipropetrovska_oblast_oblenergo")]
    assert await worker.run_once()
    api.region.assert_awaited_once_with(AREA)
    assert len(repo.finish_cycle.await_args.args[2][AREA.slug].rows) == 1


async def test_switch_reuses_latest_region_fetch_without_another_api_call():
    worker, repo, credentials, api, _, _ = setup_worker()
    worker._coverage = SimpleNamespace(
        snapshot=AsyncMock(return_value=SimpleNamespace(power_group_keys=GROUPS))
    )
    repo.prune_unused_groups = AsyncMock()
    api.overview.return_value = (AREA,)
    initial = region(area=AREA, supplier="dnipropetrovska_oblast_oblenergo")
    wire = initial.to_wire()
    wire["rows"].append({**wire["rows"][0], "queue": "6.1"})
    full = RegionMap.from_wire(wire)

    async def fetch(_):
        worker._coverage.snapshot.return_value.power_group_keys = frozenset(
            {(PowerProvider.DTEK, "6.1")}
        )
        return full

    api.region.side_effect = fetch
    assert await worker.run_once()
    assert repo.finish_cycle.await_args.args[2] == {AREA.slug: full}
    repo.begin_cycle.return_value = None
    assert not await worker.run_once()
    repo.prune_unused_groups.assert_awaited_with(
        NOW, recent_maps={AREA.slug: full}, credential_revision=1
    )
    api.region.assert_awaited_once_with(AREA)
    credentials.load.assert_awaited_once()


@pytest.mark.parametrize("reason", ["empty_demand", "credential_changed", "expired", "future"])
async def test_reprojection_buffer_is_discarded_when_no_longer_eligible(reason):
    worker, repo, credentials, api, _, _ = setup_worker(begin=None)
    worker._coverage = SimpleNamespace(
        snapshot=AsyncMock(return_value=SimpleNamespace(power_group_keys=GROUPS))
    )
    repo.prune_unused_groups = AsyncMock()
    worker._recent_maps = {AREA.slug: region(area=AREA)}
    worker._recent_revision = 1
    if reason == "empty_demand":
        worker._coverage.snapshot.return_value.power_group_keys = frozenset()
    elif reason == "credential_changed":
        credentials.status.return_value.revision = 2
    else:
        worker._clock = lambda: NOW + timedelta(seconds=1801 if reason == "expired" else -1)
    assert not await worker.run_once()
    assert not worker._recent_maps
    assert repo.prune_unused_groups.await_args.kwargs["recent_maps"] == {}
    credentials.load.assert_not_awaited()
    api.overview.assert_not_awaited()


@pytest.mark.asyncio
async def test_final_before_request_fence_revokes_http_after_demand_disappears():
    worker, repo, _, api, clients, _ = setup_worker()
    demand = SimpleNamespace(power_group_keys=GROUPS)
    worker._coverage = SimpleNamespace(snapshot=AsyncMock(return_value=demand))
    repo.prune_unused_groups = AsyncMock()

    async def overview():
        # Simulate last subscriber disappearing during quota/spacing wait.
        worker._coverage.snapshot.return_value = SimpleNamespace(power_group_keys=frozenset())
        await clients[0]["before_request"]()
        pytest.fail("source HTTP must not run after this fence")

    api.overview.side_effect = overview
    assert not await worker.run_once()
    api.region.assert_not_awaited()
    repo.finish_cycle.assert_not_awaited()

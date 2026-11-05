"""Synthetic cache cycles and an opt-in isolated schema9 PostgreSQL fixture."""

import asyncio
import copy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, or_, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.infrastructure.test_energy_api_budget import MemoryFactory, MemorySession, isolated_url
from time_tl.domain.power_outages.map import MapArea, RegionMap
from time_tl.domain.power_outages.models import PowerProvider, SourceStatus
from time_tl.infrastructure.db.models import RuntimeSetting
from time_tl.infrastructure.db.power_map import (
    REGION_PREFIX,
    PostgresPowerMapRepository,
    _state,
)

NOW = datetime(2026, 11, 4, 12, tzinfo=UTC)
AREA = MapArea("synthetic-oblast", "UA00000000000000001", "Synthetic area")
OTHER = MapArea("other-oblast", "UA00000000000000002", "Other synthetic area")
CREDENTIAL_KEY = "power_alerts_energy_api_credentials"


def region(area=AREA, at=NOW, outage=1, supplier="synthetic_supplier", checked=None):
    return RegionMap(
        area,
        (
            {
                "initiator": supplier,
                "queue": "1.1",
                "updated": (at - timedelta(hours=20)).isoformat(),
                "today": [outage] + [0] * 23,
                "tomorrow": [],
                "today_date": at.date().isoformat(),
                "tomorrow_date": (at + timedelta(days=1)).date().isoformat(),
                "today_status": "ScheduleApplies",
                "tomorrow_status": "WaitingForSchedule",
                "today_checked_at": (checked or at).isoformat(),
            },
        ),
        at,
    )


class MemoryMaps(PostgresPowerMapRepository):
    @staticmethod
    async def _locked(session, key, initial):
        if key not in session.store.rows:
            session.store.rows[key] = RuntimeSetting(key=key, value=copy.deepcopy(initial))
        return session.store.rows[key]

    @staticmethod
    async def _credential_valid(session, revision):
        return session.store.credential_revision == revision


async def finish(repo, areas=(AREA,), maps=None, failures=None, at=NOW, revision=1):
    cycle = await repo.begin_cycle(at, revision)
    assert cycle
    return await repo.finish_cycle(
        cycle,
        areas,
        maps if maps is not None else {AREA.slug: region(at=at)},
        failures or {},
        at + timedelta(seconds=1),
        revision,
    )


async def test_cadence_restart_lease_recovery_and_expired_fence():
    store = MemoryFactory()
    repo = MemoryMaps(store)
    first = await repo.begin_cycle(NOW, 1)
    assert first
    assert await MemoryMaps(store).begin_cycle(NOW + timedelta(seconds=300), 1) is None
    second = await MemoryMaps(store).begin_cycle(NOW + timedelta(seconds=600), 1)
    assert second and first != second
    assert not await repo.finish_cycle(
        first, (AREA,), {AREA.slug: region()}, {}, NOW + timedelta(seconds=601), 1
    )
    assert await repo.fail_cycle(second, "HTTP_ERROR", NOW + timedelta(seconds=601), 1)
    assert await repo.begin_cycle(NOW + timedelta(seconds=899), 1) is None


async def test_concurrent_collectors_only_one_cycle():
    store = MemoryFactory()
    results = await asyncio.gather(*[MemoryMaps(store).begin_cycle(NOW, 1) for _ in range(8)])
    assert sum(value is not None for value in results) == 1


@pytest.mark.parametrize("failure", [None, "revoked", "stale", "failed_fetch"])
async def test_switch_reprojects_current_group_from_same_successful_fetch(monkeypatch, failure):
    area = MapArea("dnipropetrovska-oblast", "UA12000000000090473", "Dnipro")
    wire = region(area=area, supplier="dnipropetrovska_oblast_oblenergo").to_wire()
    wire["rows"].append({**wire["rows"][0], "queue": "6.1"})
    full = RegionMap.from_wire(wire)
    selected = "1.1"

    async def groups(_):
        return frozenset({(PowerProvider.DTEK, selected)})

    async def regions(session, _):
        rows = [row for key, row in session.store.rows.items() if key.startswith(REGION_PREFIX)]
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))

    monkeypatch.setattr("time_tl.infrastructure.db.power_coverage.locked_power_groups", groups)
    monkeypatch.setattr(MemorySession, "execute", regions, raising=False)
    store = MemoryFactory()
    repo = MemoryMaps(store, dynamic_coverage=True)
    assert await finish(repo, areas=(area,), maps={area.slug: full})
    view = await repo.read_region(area.slug, NOW + timedelta(seconds=1))
    assert [row["queue"] for row in view.data.rows] == ["1.1"]
    selected = "6.1"
    now = NOW + timedelta(seconds=2 if failure != "stale" else 1801)
    if failure == "revoked":
        store.credential_revision = 2
    if failure == "failed_fetch":
        store.rows[REGION_PREFIX + area.slug].value.update(
            last_fetch_succeeded=False, source_status="STALE", error_code="HTTP_ERROR"
        )
    await repo.prune_unused_groups(now, recent_maps={area.slug: full}, credential_revision=1)
    view = await repo.read_region(area.slug, now)
    assert view.data.fetched_at == full.fetched_at
    if failure:
        assert not view.data.rows and view.source_status is not SourceStatus.FRESH
    else:
        assert [row["queue"] for row in view.data.rows] == ["6.1"]
        assert view.source_status is SourceStatus.FRESH
        selected = "1.1"
        await repo.prune_unused_groups(now, recent_maps={area.slug: full}, credential_revision=1)
        view = await repo.read_region(area.slug, now)
        assert [row["queue"] for row in view.data.rows] == ["1.1"]


async def test_unchanged_timestamps_refresh_metadata_without_revision_and_a_b_a_changes():
    store = MemoryFactory()
    repo = MemoryMaps(store)
    assert await finish(repo)
    for index, outage, expected in [(1, 1, 1), (2, 0, 2), (3, 1, 3)]:
        at = NOW + timedelta(seconds=300 * index)
        assert await finish(repo, maps={AREA.slug: region(at=at, outage=outage)}, at=at)
        view = await repo.read_region(AREA.slug, at + timedelta(seconds=1))
        assert view.revision == expected and view.source_status is SourceStatus.FRESH
        assert view.data.fetched_at == at
    assert (await repo.statistics(NOW + timedelta(seconds=901)))["complete"] is True


async def test_partial_failure_retains_stale_data_and_country_incomplete():
    store = MemoryFactory()
    repo = MemoryMaps(store)
    await finish(repo, (AREA, OTHER), {AREA.slug: region(), OTHER.slug: region(OTHER)})
    at = NOW + timedelta(seconds=300)
    await finish(repo, (AREA, OTHER), {AREA.slug: region(at=at)}, {OTHER.slug: "HTTP_ERROR"}, at)
    assert (await repo.read_region(AREA.slug, at)).source_status is SourceStatus.FRESH
    failed = await repo.read_region(OTHER.slug, at)
    assert failed.data is not None and failed.source_status is SourceStatus.STALE
    assert not (await repo.statistics(at))["complete"]
    old = await repo.read_record(OTHER.slug, "synthetic_supplier", "1.1", NOW.date(), at)
    assert old.source_status is SourceStatus.STALE
    assert await repo.fail_cycle(
        await repo.begin_cycle(at + timedelta(seconds=300), 1),
        "TIMEOUT",
        at + timedelta(seconds=301),
        1,
    )
    assert (
        await repo.read_region(AREA.slug, at + timedelta(seconds=301))
    ).source_status is SourceStatus.STALE


async def test_removed_region_and_same_slug_identity_change_never_relabel_history():
    store = MemoryFactory()
    repo = MemoryMaps(store)
    await finish(repo, (AREA, OTHER), {AREA.slug: region(), OTHER.slug: region(OTHER)})
    at = NOW + timedelta(seconds=300)
    changed = replace(AREA, katottg="UA00000000000000009")
    await finish(repo, (changed,), {}, {changed.slug: "HTTP_ERROR"}, at)
    view = await repo.read_region(AREA.slug, at)
    assert view.data is None and view.source_status is SourceStatus.UNAVAILABLE
    assert view.error_code == "REGION_MAPPING_CHANGED" and view.revision == 1
    removed = await repo.read_region(OTHER.slug, at)
    assert removed.source_status is SourceStatus.UNAVAILABLE and removed.data is not None
    assert removed.error_code == "REGION_REMOVED"
    at += timedelta(seconds=300)
    await finish(repo, (changed,), {changed.slug: region(changed, at)}, at=at)
    assert (await repo.read_region(changed.slug, at)).revision == 2


async def test_credential_rotation_rejects_old_cycle_result_and_no_callback_http():
    store = MemoryFactory()
    repo = MemoryMaps(store)
    cycle = await repo.begin_cycle(NOW, 1)
    store.credential_revision = 2
    assert not await repo.cycle_current(cycle, 1, NOW)
    assert not await repo.finish_cycle(cycle, (AREA,), {AREA.slug: region()}, {}, NOW, 1)
    assert REGION_PREFIX + AREA.slug not in store.rows
    assert (await repo.read_region(AREA.slug, NOW)).source_status is SourceStatus.UNAVAILABLE


async def test_healthy_record_survives_other_operator_bad_check_without_authority():
    store = MemoryFactory()
    repo = MemoryMaps(store)
    good = region()
    bad = region(supplier="other", checked=NOW - timedelta(hours=1))
    mixed = RegionMap(AREA, good.rows + bad.rows, NOW)
    await finish(repo, maps={AREA.slug: mixed})
    assert (await repo.read_region(AREA.slug, NOW)).source_status is SourceStatus.STALE
    view = await repo.read_record(AREA.slug, "synthetic_supplier", "1.1", NOW.date(), NOW)
    assert view.source_status is SourceStatus.FRESH and view.scope == "REGION_CACHE_ONLY"
    assert (
        await repo.read_record(AREA.slug, "other", "1.1", NOW.date(), NOW)
    ).source_status is SourceStatus.STALE


async def test_empty_region_is_collected_but_not_a_fresh_no_outages_claim():
    repo = MemoryMaps(MemoryFactory())
    await finish(repo, maps={AREA.slug: RegionMap(AREA, (), NOW)})
    stats = await repo.statistics(NOW)
    assert stats["collected_complete"] is True
    assert stats["fresh_complete"] is False and stats["complete"] is False
    assert stats["regions"][0]["group_count"] == 0
    assert stats["regions"][0]["operator_count"] == 0
    assert stats["regions"][0]["error_code"] == "NO_SCHEDULE"
    assert "rows" not in repr(stats)


@pytest.mark.parametrize(
    "change",
    [
        {"revision": True},
        {"complete": 1},
        {"active_cycle": "bad"},
        {"expected_regions": ["a", "a"]},
        {"extra": 1},
    ],
)
def test_corrupt_cycle_state_closed(change):
    from time_tl.infrastructure.db.power_map import _state_empty

    with pytest.raises(ValueError):
        _state({**_state_empty(), **change})


async def test_native_pg_map_cycle_cas_restart_revisions_partial_and_credential_rotation():
    engine = create_async_engine(isolated_url())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    owned = False
    try:
        async with factory() as session, session.begin():
            assert (
                await session.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one() == "0009_calendar_foundation"
            existing = (
                await session.scalars(
                    select(RuntimeSetting.key).where(
                        or_(
                            RuntimeSetting.key.like("power_map:%"),
                            RuntimeSetting.key == CREDENTIAL_KEY,
                        )
                    )
                )
            ).all()
            assert not existing
            owned = True
            session.add(
                RuntimeSetting(
                    key=CREDENTIAL_KEY,
                    value={"revision": 1, "check_status": "VALID"},
                    updated_by="powermap-test",
                )
            )
        repo = PostgresPowerMapRepository(factory)
        cycles = await asyncio.gather(
            *[PostgresPowerMapRepository(factory).begin_cycle(NOW, 1) for _ in range(4)]
        )
        cycle = next(value for value in cycles if value is not None)
        assert sum(value is not None for value in cycles) == 1
        assert await repo.finish_cycle(
            cycle, (AREA, OTHER), {AREA.slug: region(), OTHER.slug: region(OTHER)}, {}, NOW, 1
        )
        restarted = PostgresPowerMapRepository(factory)
        at = NOW + timedelta(seconds=300)
        await finish(
            restarted, (AREA, OTHER), {AREA.slug: region(at=at)}, {OTHER.slug: "TIMEOUT"}, at
        )
        assert (await repo.read_region(AREA.slug, at)).revision == 1
        assert (await repo.read_region(OTHER.slug, at)).source_status is SourceStatus.STALE
        assert not (await repo.statistics(at))["complete"]
        cycle = await repo.begin_cycle(at + timedelta(seconds=300), 1)
        async with factory() as session, session.begin():
            row = await session.get(RuntimeSetting, CREDENTIAL_KEY, with_for_update=True)
            row.value = {"revision": 2, "check_status": "VALID"}
        assert not await repo.finish_cycle(
            cycle, (AREA,), {AREA.slug: region(at=at)}, {}, at + timedelta(seconds=301), 1
        )
    finally:
        if owned:
            async with factory() as session, session.begin():
                await session.execute(
                    delete(RuntimeSetting).where(
                        or_(
                            RuntimeSetting.key.like("power_map:%"),
                            RuntimeSetting.key == CREDENTIAL_KEY,
                        )
                    )
                )
        await engine.dispose()

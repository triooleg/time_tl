"""SYNTHETIC observations: agreement never proves a source contract."""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

from time_tl.domain.power_outages.models import (
    GROUPS,
    PowerDaySchedule,
    PowerInterval,
    PowerProvider,
    PowerScheduleSnapshot,
    PowerSourceKind,
    PowerState,
    ScheduleKind,
    SourceStatus,
)
from time_tl.domain.power_outages.normalization import day_bounds
from time_tl.domain.power_outages.sources import (
    ComparisonStatus,
    compare_snapshots,
    effective_content_hash,
    next_effective_revision,
    select_snapshot,
)

NOW = datetime(2026, 11, 4, 12, tzinfo=UTC)


def synthetic_snapshot(
    source: PowerSourceKind = PowerSourceKind.OFFICIAL,
    state: PowerState = PowerState.POWER_EXPECTED,
) -> PowerScheduleSnapshot:
    day = date(2026, 11, 4)
    lower, upper = day_bounds(day)
    return PowerScheduleSnapshot(
        PowerProvider.DTEK,
        NOW,
        NOW - timedelta(minutes=1),
        tuple(
            PowerDaySchedule(
                PowerProvider.DTEK,
                group,
                day,
                ScheduleKind.CONFIRMED_DAY,
                (PowerInterval(group, lower, upper, state),),
            )
            for group in GROUPS
        ),
        SourceStatus.FRESH,
        source_kind=source,
    )


def test_matching_synthetic_data_does_not_self_verify() -> None:
    official = synthetic_snapshot()
    candidate = replace(official, source_kind=PowerSourceKind.ALERTS_ENERGY_HTML)
    value = compare_snapshots(candidate, official, NOW)
    assert value.structural_status is ComparisonStatus.MATCH
    assert value.structural_compared_count == 12
    assert value.verification_status is ComparisonStatus.UNVERIFIED
    assert (value.compared_count, value.matched_count, value.match_percent) == (0, 0, None)


def test_conflict_prefers_independently_verified_official_whole_snapshot() -> None:
    official = synthetic_snapshot()
    candidate = synthetic_snapshot(PowerSourceKind.ALERTS_ENERGY_HTML, PowerState.OUTAGE_CONFIRMED)
    selection = select_snapshot(
        PowerProvider.DTEK,
        [candidate, official],
        NOW,
        approved_sources=frozenset({PowerSourceKind.OFFICIAL}),
    )
    assert selection.snapshot is official
    assert selection.comparison.structural_status is ComparisonStatus.CONFLICT
    assert selection.notification_authorized is False


def test_same_content_source_switch_does_not_advance_revision() -> None:
    official = synthetic_snapshot()
    candidate = replace(
        official,
        source_kind=PowerSourceKind.ALERTS_ENERGY_HTML,
        fetched_at=NOW + timedelta(seconds=1),
    )
    digest = effective_content_hash(official)
    assert effective_content_hash(candidate) == digest
    assert next_effective_revision(digest, effective_content_hash(candidate), 3) == 3
    changed = effective_content_hash(synthetic_snapshot(state=PowerState.OUTAGE_CONFIRMED))
    changed_revision = next_effective_revision(digest, changed, 3)
    assert next_effective_revision(changed, digest, changed_revision) == 5


@pytest.mark.parametrize("state", [PowerState.UNKNOWN, PowerState.NO_SCHEDULE])
def test_unknown_never_counts_as_verified_match(state: PowerState) -> None:
    official = synthetic_snapshot(state=state)
    value = compare_snapshots(
        official, official, NOW, official_contract_verified=True, candidate_contract_verified=True
    )
    assert value.verification_status is ComparisonStatus.INCOMPLETE
    assert value.match_percent is None


def test_partial_and_old_and_region_mismatch_fail_closed() -> None:
    official = synthetic_snapshot()
    assert (
        compare_snapshots(
            replace(official, days=official.days[:1]), official, NOW
        ).structural_status
        is ComparisonStatus.INCOMPLETE
    )
    assert (
        compare_snapshots(official, official, NOW + timedelta(hours=1)).structural_status
        is ComparisonStatus.STALE
    )
    assert (
        compare_snapshots(replace(official, locality="other"), official, NOW).structural_status
        is ComparisonStatus.INCOMPLETE
    )
    with pytest.raises(ValueError, match="localities"):
        select_snapshot(
            PowerProvider.DTEK,
            [
                official,
                replace(official, locality="other", source_kind=PowerSourceKind.ALERTS_ENERGY_HTML),
            ],
            NOW,
        )


def test_verified_counts_require_both_external_contracts_and_pair_is_stable() -> None:
    official = synthetic_snapshot()
    candidate = replace(official, source_kind=PowerSourceKind.ALERTS_ENERGY_HTML)
    value = compare_snapshots(
        candidate, official, NOW, official_contract_verified=True, candidate_contract_verified=True
    )
    assert (value.compared_count, value.matched_count, value.match_percent) == (12, 12, 100)
    updated = replace(candidate, fetched_at=NOW + timedelta(seconds=1))
    refreshed = compare_snapshots(updated, official, NOW + timedelta(seconds=1))
    assert refreshed.pair_key == value.pair_key


@pytest.mark.asyncio
async def test_bezsvitla_closed_stub() -> None:
    from time_tl.infrastructure.power_outages.bezsvitla_provider import BezSvitlaProvider

    adapter = BezSvitlaProvider(PowerProvider.DTEK, clock=lambda: NOW)
    result = await adapter.fetch_schedules()
    assert result.source_kind is PowerSourceKind.BEZSVITLA
    assert result.status is SourceStatus.UNAVAILABLE and not result.days
    assert adapter.collection_allowed is False


@pytest.mark.asyncio
async def test_manager_uses_cached_only_and_preserves_closed_approval() -> None:
    from time_tl.application.power_source_manager import PowerSourceManager

    official = synthetic_snapshot()
    candidate = replace(official, source_kind=PowerSourceKind.ALERTS_ENERGY_HTML)

    class CachedRepository:
        recorded: list[PowerScheduleSnapshot] = []

        async def source_states(self, now: datetime) -> tuple[dict[str, object], ...]:
            return tuple(
                {
                    "provider_id": PowerProvider.DTEK.value,
                    "source_kind": source.value,
                    "enabled": True,
                    "priority": index,
                }
                for index, source in enumerate(
                    [
                        PowerSourceKind.ALERTS_ENERGY_API,
                        PowerSourceKind.ALERTS_ENERGY_HTML,
                        PowerSourceKind.OFFICIAL,
                        PowerSourceKind.BEZSVITLA,
                    ]
                )
            )

        async def cached_snapshots(
            self,
            provider: PowerProvider,
        ) -> tuple[PowerScheduleSnapshot, ...]:
            return candidate, official

        async def record_observation(
            self,
            snapshot: PowerScheduleSnapshot,
            official_snapshot: PowerScheduleSnapshot | None,
            now: datetime,
        ) -> None:
            self.recorded.append(snapshot)

    repository = CachedRepository()
    manager = PowerSourceManager(repository)
    result = await manager.select_cached(PowerProvider.DTEK, NOW)
    assert result.snapshot is candidate
    assert result.comparison.verification_status is ComparisonStatus.UNVERIFIED
    assert result.notification_authorized is False
    await manager.record_observation(candidate, official, NOW)
    assert repository.recorded == [candidate]

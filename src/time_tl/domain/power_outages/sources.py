"""Independent source comparison: content agreement is not source approval."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from .models import (
    GROUPS,
    PowerProvider,
    PowerPublicationStatus,
    PowerScheduleSnapshot,
    PowerSourceKind,
    PowerState,
    SourceStatus,
    aware,
)
from .normalization import day_bounds

SOURCE_ORDER = (
    PowerSourceKind.ALERTS_ENERGY_API,
    PowerSourceKind.ALERTS_ENERGY_HTML,
    PowerSourceKind.OFFICIAL,
    PowerSourceKind.BEZSVITLA,
)


class ComparisonStatus(StrEnum):
    MATCH = "MATCH"
    CONFLICT = "CONFLICT"
    INCOMPLETE = "INCOMPLETE"
    UNVERIFIED = "UNVERIFIED"
    STALE = "STALE"


@dataclass(frozen=True)
class SourceComparison:
    verification_status: ComparisonStatus
    structural_status: ComparisonStatus
    compared_count: int = 0
    matched_count: int = 0
    structural_compared_count: int = 0
    structural_matched_count: int = 0
    publication_lag_seconds: float | None = None
    pair_key: str | None = None

    @property
    def match_percent(self) -> float | None:
        return None if self.compared_count == 0 else 100 * self.matched_count / self.compared_count


@dataclass(frozen=True)
class SourceSelection:
    snapshot: PowerScheduleSnapshot | None
    comparison: SourceComparison
    reason: str
    content_hash: str | None
    notification_authorized: bool = field(default=False, init=False)


def effective_content_hash(snapshot: PowerScheduleSnapshot) -> str:
    """No source-kind/URL/acquisition timestamp in event semantic identity."""
    value = {
        "provider": snapshot.provider.value,
        "locality": snapshot.locality,
        "days": sorted(day.content_hash for day in snapshot.days),
    }
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode("utf-8")).hexdigest()


def next_effective_revision(previous_hash: str | None, current_hash: str, revision: int) -> int:
    """A->B->A advances twice; observing A again after A does not advance."""
    if type(revision) is not int or revision < 0 or len(current_hash) != 64:
        raise ValueError("invalid_effective_power_revision")
    return revision if previous_hash == current_hash else revision + 1


def _fresh(snapshot: PowerScheduleSnapshot, now: datetime, stale_after_seconds: int) -> bool:
    stamp = aware(now)
    if snapshot.status is not SourceStatus.FRESH:
        return False
    fetched_age = (stamp - aware(snapshot.fetched_at)).total_seconds()
    evidence = (
        snapshot.source_checked_at
        if snapshot.source_kind is PowerSourceKind.ALERTS_ENERGY_API
        else snapshot.source_updated_at
    )
    if not 0 <= fetched_age <= stale_after_seconds or evidence is None:
        return False
    source_age = (stamp - aware(evidence)).total_seconds()
    return 0 <= source_age <= stale_after_seconds


def _complete(snapshot: PowerScheduleSnapshot) -> bool:
    if not snapshot.days:
        return False
    bundles: dict[tuple[object, ...], set[str]] = {}
    for day in snapshot.days:
        if day.publication_status is not PowerPublicationStatus.PLANNED or not day.intervals:
            return False
        lower, upper = day_bounds(day.local_date)
        cursor = lower
        for interval in day.intervals:
            if aware(interval.starts_at) != cursor or interval.state in {
                PowerState.UNKNOWN,
                PowerState.NO_SCHEDULE,
            }:
                return False
            cursor = aware(interval.ends_at)
        if cursor != upper:
            return False
        key = day.local_date, day.schedule_type, day.mapping_version
        bundles.setdefault(key, set()).add(day.group_id)
    return all(groups == set(GROUPS) for groups in bundles.values())


def compare_snapshots(
    candidate: PowerScheduleSnapshot,
    official: PowerScheduleSnapshot | None,
    now: datetime,
    *,
    stale_after_seconds: int = 1800,
    official_contract_verified: bool = False,
    candidate_contract_verified: bool = False,
) -> SourceComparison:
    """Trust flags come only from an independently admitted contract registry.

    Current runtime repository supplies neither: structural equality cannot
    manufacture verified samples or open an access/notification gate.
    """
    aware(now)
    if type(stale_after_seconds) is not int or stale_after_seconds < 1:
        raise ValueError("invalid_power_source_stale_limit")
    if (
        type(official_contract_verified) is not bool
        or type(candidate_contract_verified) is not bool
    ):
        raise ValueError("invalid_power_source_verification")
    empty = SourceComparison(ComparisonStatus.UNVERIFIED, ComparisonStatus.UNVERIFIED)
    if official is None or official.source_kind is not PowerSourceKind.OFFICIAL:
        return empty
    if candidate.provider is not official.provider or candidate.locality != official.locality:
        return SourceComparison(ComparisonStatus.INCOMPLETE, ComparisonStatus.INCOMPLETE)
    if not _fresh(candidate, now, stale_after_seconds) or not _fresh(
        official, now, stale_after_seconds
    ):
        return SourceComparison(ComparisonStatus.STALE, ComparisonStatus.STALE)
    if not _complete(candidate) or not _complete(official):
        return SourceComparison(ComparisonStatus.INCOMPLETE, ComparisonStatus.INCOMPLETE)
    left = {
        (day.local_date, day.group_id, day.schedule_type, day.mapping_version): day.content_hash
        for day in candidate.days
    }
    right = {
        (day.local_date, day.group_id, day.schedule_type, day.mapping_version): day.content_hash
        for day in official.days
    }
    if set(left) != set(right) or candidate.schema_version != official.schema_version:
        return SourceComparison(ComparisonStatus.INCOMPLETE, ComparisonStatus.INCOMPLETE)
    compared, matched = len(left), sum(left[key] == right[key] for key in left)
    structural = ComparisonStatus.MATCH if matched == compared else ComparisonStatus.CONFLICT
    verified = official_contract_verified and candidate_contract_verified
    lag = (
        (aware(candidate.source_updated_at) - aware(official.source_updated_at)).total_seconds()
        if candidate.source_updated_at is not None and official.source_updated_at is not None
        else None
    )
    pair = hashlib.sha256(
        (effective_content_hash(candidate) + ":" + effective_content_hash(official)).encode("ascii")
    ).hexdigest()
    return SourceComparison(
        structural if verified else ComparisonStatus.UNVERIFIED,
        structural,
        compared if verified else 0,
        matched if verified else 0,
        compared,
        matched,
        lag,
        pair,
    )


def select_snapshot(
    provider: PowerProvider,
    snapshots: Sequence[PowerScheduleSnapshot],
    now: datetime,
    *,
    enabled_sources: frozenset[PowerSourceKind] | None = None,
    priorities: Mapping[PowerSourceKind, int] | None = None,
    approved_sources: frozenset[PowerSourceKind] = frozenset(),
    stale_after_seconds: int = 1800,
) -> SourceSelection:
    """Return one whole cached preview. Never merge intervals or authorize sends."""
    aware(now)
    if not isinstance(provider, PowerProvider):
        raise ValueError("invalid_power_source_provider")
    if type(stale_after_seconds) is not int or stale_after_seconds < 1:
        raise ValueError("invalid_power_source_stale_limit")
    if len(snapshots) > len(PowerSourceKind) or any(
        snapshot.provider is not provider for snapshot in snapshots
    ):
        raise ValueError("mixed_power_provider_sources")
    localities = {snapshot.locality for snapshot in snapshots}
    if len(localities) > 1:
        raise ValueError("mixed_power_source_localities")
    if len({snapshot.source_kind for snapshot in snapshots}) != len(snapshots):
        raise ValueError("ambiguous_power_source_snapshot")
    enabled = frozenset(PowerSourceKind) if enabled_sources is None else enabled_sources
    if any(not isinstance(kind, PowerSourceKind) for kind in enabled | approved_sources):
        raise ValueError("invalid_power_source_kind")
    order = {kind: index for index, kind in enumerate(SOURCE_ORDER)}
    if priorities is not None:
        if set(priorities) != set(PowerSourceKind) or any(
            type(value) is not int or not 0 <= value <= 100 for value in priorities.values()
        ):
            raise ValueError("invalid_power_source_priority")
        order = dict(priorities)
    available = sorted(
        (
            snapshot
            for snapshot in snapshots
            if snapshot.source_kind in enabled and _fresh(snapshot, now, stale_after_seconds)
        ),
        key=lambda snapshot: (
            order[snapshot.source_kind],
            SOURCE_ORDER.index(snapshot.source_kind),
        ),
    )
    official = next(
        (snapshot for snapshot in available if snapshot.source_kind is PowerSourceKind.OFFICIAL),
        None,
    )
    candidate = available[0] if available else None
    if candidate is None:
        return SourceSelection(
            None,
            SourceComparison(ComparisonStatus.STALE, ComparisonStatus.STALE),
            "NO_FRESH_SOURCE",
            None,
        )
    comparison = compare_snapshots(
        candidate,
        official,
        now,
        stale_after_seconds=stale_after_seconds,
        official_contract_verified=PowerSourceKind.OFFICIAL in approved_sources,
        candidate_contract_verified=candidate.source_kind in approved_sources,
    )
    if (
        official is not None
        and PowerSourceKind.OFFICIAL in approved_sources
        and _complete(official)
    ):
        if candidate.source_kind not in approved_sources or comparison.structural_status in {
            ComparisonStatus.CONFLICT,
            ComparisonStatus.INCOMPLETE,
            ComparisonStatus.UNVERIFIED,
        }:
            return SourceSelection(
                official,
                comparison,
                "VERIFIED_OFFICIAL_PREFERRED",
                effective_content_hash(official),
            )
    return SourceSelection(
        candidate,
        comparison,
        "CACHED_PREVIEW_ONLY",
        effective_content_hash(candidate),
    )

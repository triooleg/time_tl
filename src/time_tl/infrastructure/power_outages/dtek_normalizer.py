"""Half-hour interpretation of the unverified technical-reference wire profile."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from time_tl.domain.power_outages.models import (
    PowerDaySchedule,
    PowerInterval,
    PowerProvider,
    PowerScheduleSnapshot,
    PowerState,
    ScheduleKind,
    SourceStatus,
    aware,
)
from time_tl.domain.power_outages.normalization import KYIV, local_boundary, normalize_intervals

from .dtek_parser import REFERENCE_MAPPING_VERSION, DtekDataError, DtekFact


def normalize_dtek_fact(
    fact: DtekFact,
    *,
    provider_id: PowerProvider,
    fetched_at: datetime,
    synthetic_profile: bool = False,
    stale_after_seconds: int = 1800,
) -> PowerScheduleSnapshot:
    # No official semantics/date profile is approved yet. Runtime must not call
    # this with unverified technical data and pretend it is an admitted source.
    if synthetic_profile is not True or fact.mapping_version != REFERENCE_MAPPING_VERSION:
        raise DtekDataError("UNVERIFIED_DTEK_SEMANTICS")
    if not isinstance(provider_id, PowerProvider) or provider_id not in {
        PowerProvider.DTEK, PowerProvider.DTEK_KYIV, PowerProvider.DTEK_KYIV_REGION,
        PowerProvider.DTEK_ODESA, PowerProvider.DTEK_DONETSK,
    }:
        raise DtekDataError("INVALID_DTEK_REGION")
    if type(stale_after_seconds) is not int or not 1 <= stale_after_seconds <= 86400:
        raise DtekDataError("INVALID_DTEK_STALE_LIMIT")
    observed, updated = aware(fetched_at), aware(fact.source_updated_at)
    if updated > observed:
        raise DtekDataError("FUTURE_DTEK_UPDATE")
    if fact.today != observed.astimezone(KYIV).date() or (
        observed - updated
    ).total_seconds() > stale_after_seconds:
        return PowerScheduleSnapshot(
            provider_id, observed, updated, (), SourceStatus.STALE, "STALE_DTEK_FACT"
        )
    mapping = {
        "yes": (PowerState.POWER_EXPECTED, PowerState.POWER_EXPECTED),
        "no": (PowerState.OUTAGE_CONFIRMED, PowerState.OUTAGE_CONFIRMED),
        "first": (PowerState.OUTAGE_CONFIRMED, PowerState.POWER_EXPECTED),
        "second": (PowerState.POWER_EXPECTED, PowerState.OUTAGE_CONFIRMED),
    }
    schedules: list[PowerDaySchedule] = []
    for day in fact.days:
        for group in day.groups:
            intervals: list[PowerInterval] = []
            for hour, raw_state in enumerate(group.hours):
                start = local_boundary(day.day, f"{hour:02d}:00")
                states = mapping.get(raw_state, (PowerState.UNKNOWN, PowerState.UNKNOWN))
                for half, state in enumerate(states):
                    lower = start + timedelta(minutes=30 * half)
                    intervals.append(PowerInterval(
                        group.group_id, lower, lower + timedelta(minutes=30), state
                    ))
            schedules.append(PowerDaySchedule(
                provider_id, group.group_id, day.day, ScheduleKind.CONFIRMED_DAY,
                normalize_intervals(group.group_id, tuple(intervals), day.day),
            ))
    return PowerScheduleSnapshot(
        provider_id, fetched_at.astimezone(UTC), updated, tuple(schedules), SourceStatus.FRESH
    )

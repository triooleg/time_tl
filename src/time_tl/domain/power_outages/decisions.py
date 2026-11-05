"""Deterministic future schedule reminders, never confirmation of physical supply."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta

from time_tl.domain.power_outages.models import (
    PowerDaySchedule,
    PowerInterval,
    PowerProvider,
    PowerPublicationStatus,
    PowerState,
    ScheduleKind,
    aware,
    validate_lead,
)


@dataclass(frozen=True)
class PowerTransition:
    provider: PowerProvider
    group_id: str
    event_type: str
    transition_at: datetime
    planned_send_at: datetime
    outage_starts_at: datetime
    outage_ends_at: datetime
    transition_key: str


def upcoming_transitions(
    schedules: tuple[PowerDaySchedule, ...],
    now: datetime,
    *,
    outage_lead_minutes: int = 30,
    restoration_lead_minutes: int = 15,
) -> tuple[PowerTransition, ...]:
    """Only due, non-past boundaries from explicit confirmed daily intervals.

    Zero lead is deliberately strict: a delayed job may be dropped. Processing
    latency is not permission to replay an already elapsed transition.
    """
    clock = aware(now)
    validate_lead(outage_lead_minutes)
    validate_lead(restoration_lead_minutes)
    daily = tuple(
        item for item in schedules
        if item.schedule_type is ScheduleKind.CONFIRMED_DAY
        and item.publication_status is PowerPublicationStatus.PLANNED
    )
    identities = {(item.provider, item.group_id) for item in daily}
    if len(identities) > 1:
        raise ValueError("power_transition_requires_one_subscription")
    if not daily:
        return ()
    provider, group = next(iter(identities))
    intervals = sorted(
        (item for day in daily for item in day.intervals),
        key=lambda item: (aware(item.starts_at), aware(item.ends_at)),
    )
    merged: list[PowerInterval] = []
    for item in intervals:
        if merged and aware(item.starts_at) < aware(merged[-1].ends_at):
            raise ValueError("overlapping_power_schedule_dates")
        if (
            merged
            and merged[-1].state is item.state
            and aware(merged[-1].ends_at) == aware(item.starts_at)
        ):
            old = merged.pop()
            merged.append(PowerInterval(group, old.starts_at, item.ends_at, item.state))
        else:
            merged.append(item)
    result: list[PowerTransition] = []
    for index, item in enumerate(merged):
        if item.state is not PowerState.OUTAGE_CONFIRMED:
            continue
        start, end = aware(item.starts_at), aware(item.ends_at)
        choices = [("POWER_OUTAGE_UPCOMING", start, outage_lead_minutes)]
        if index + 1 < len(merged):
            after = merged[index + 1]
            if after.state is PowerState.POWER_EXPECTED and aware(after.starts_at) == end:
                choices.append(("POWER_RESTORATION_UPCOMING", end, restoration_lead_minutes))
        for event_type, transition, lead in choices:
            planned = transition - timedelta(minutes=lead)
            if planned <= clock <= transition:
                key = hashlib.sha256(
                    f"{provider.value}|{group}|{event_type}|{transition.isoformat()}".encode()
                ).hexdigest()
                result.append(
                    PowerTransition(
                        provider, group, event_type, transition, planned, start, end, key
                    )
                )
    return tuple(sorted(result, key=lambda event: (event.planned_send_at, event.transition_key)))

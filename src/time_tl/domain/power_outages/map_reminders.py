"""Pure reminders from fresh API map plans, never physical supply telemetry."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .models import (
    PowerDaySchedule,
    PowerInterval,
    PowerProvider,
    PowerPublicationStatus,
    PowerScheduleView,
    PowerSourceKind,
    PowerState,
    ScheduleKind,
    SourceStatus,
    aware,
    validate_group,
    validate_lead,
)
from .normalization import KYIV, day_bounds

OUTAGE = "POWER_MAP_OUTAGE_UPCOMING"
RESTORATION = "POWER_MAP_RESTORATION_UPCOMING"
SUPPORTED_PROVIDERS = frozenset({PowerProvider.DTEK, PowerProvider.CEK})


def _key(provider: PowerProvider, group: str, event: str, boundary: datetime) -> str:
    return hashlib.sha256(
        f"{provider.value}|{group}|{event}|{aware(boundary).isoformat()}".encode()
    ).hexdigest()


def _time(value: object) -> datetime:
    if type(value) is not str or len(value) > 40:
        raise ValueError("invalid_map_reminder_timestamp")
    try:
        return aware(datetime.fromisoformat(value))
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid_map_reminder_timestamp") from exc


@dataclass(frozen=True)
class MapPowerReminder:
    provider: PowerProvider
    group_id: str
    event_type: str
    transition_at: datetime
    planned_send_at: datetime
    outage_starts_at: datetime
    outage_ends_at: datetime
    outage_start_confirmed: bool
    restoration_confirmed: bool
    next_outage_starts_at: datetime | None
    next_outage_ends_at: datetime | None
    transition_key: str
    schedule_hashes: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        validate_group(self.group_id)
        if (
            not isinstance(self.provider, PowerProvider)
            or self.provider not in SUPPORTED_PROVIDERS
            or self.event_type not in {OUTAGE, RESTORATION}
            or type(self.outage_start_confirmed) is not bool
            or type(self.restoration_confirmed) is not bool
        ):
            raise ValueError("invalid_map_reminder_identity")
        start, end = aware(self.outage_starts_at), aware(self.outage_ends_at)
        boundary, planned = aware(self.transition_at), aware(self.planned_send_at)
        lead = (boundary - planned).total_seconds()
        if (
            end <= start
            or (end - start).total_seconds() > 3 * 25 * 3600
            or not 0 <= lead <= 180 * 60
            or lead % 60
            or self.event_type == OUTAGE
            and (boundary != start or not self.outage_start_confirmed)
            or self.event_type == RESTORATION
            and (boundary != end or not self.restoration_confirmed)
            or self.transition_key != _key(self.provider, self.group_id, self.event_type, boundary)
        ):
            raise ValueError("invalid_map_reminder_boundary")
        next_start, next_end = self.next_outage_starts_at, self.next_outage_ends_at
        if (
            next_start is None
            and next_end is not None
            or next_start is not None
            and (
                aware(next_start) <= end
                or aware(next_start) > end + timedelta(days=3)
                or next_end is not None
                and not aware(next_start) < aware(next_end) <= aware(next_start) + timedelta(days=3)
            )
        ):
            raise ValueError("invalid_map_reminder_next_outage")
        if type(self.schedule_hashes) is not tuple or not 1 <= len(self.schedule_hashes) <= 3:
            raise ValueError("invalid_map_reminder_hashes")
        previous: str | None = None
        for pair in self.schedule_hashes:
            if type(pair) is not tuple or len(pair) != 2:
                raise ValueError("invalid_map_reminder_hashes")
            day, content_hash = pair
            if (
                type(day) is not str
                or type(content_hash) is not str
                or re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) is None
                or date.fromisoformat(day).isoformat() != day
                or re.fullmatch(r"[0-9a-f]{64}", content_hash) is None
                or previous is not None
                and day <= previous
            ):
                raise ValueError("invalid_map_reminder_hashes")
            previous = day

    @property
    def outage_duration_minutes(self) -> int:
        return int(
            (aware(self.outage_ends_at) - aware(self.outage_starts_at)).total_seconds() // 60
        )

    @property
    def expires_at(self) -> datetime:
        boundary, planned = aware(self.transition_at), aware(self.planned_send_at)
        if boundary == planned:
            return boundary + timedelta(seconds=60)
        return min(boundary, planned + timedelta(seconds=60))

    def to_wire(self) -> dict[str, object]:
        return {
            "version": 1,
            "source_kind": "alerts_energy_api",
            "provider": self.provider.value,
            "group": self.group_id,
            "event_type": self.event_type,
            "transition_at": aware(self.transition_at).isoformat(),
            "planned_send_at": aware(self.planned_send_at).isoformat(),
            "outage_starts_at": aware(self.outage_starts_at).isoformat(),
            "outage_ends_at": aware(self.outage_ends_at).isoformat(),
            "outage_start_confirmed": self.outage_start_confirmed,
            "restoration_confirmed": self.restoration_confirmed,
            "next_outage_starts_at": (
                aware(self.next_outage_starts_at).isoformat()
                if self.next_outage_starts_at is not None
                else None
            ),
            "next_outage_ends_at": (
                aware(self.next_outage_ends_at).isoformat()
                if self.next_outage_ends_at is not None
                else None
            ),
            "transition_key": self.transition_key,
            "schedule_hashes": [list(pair) for pair in self.schedule_hashes],
        }

    @classmethod
    def from_wire(cls, value: object) -> MapPowerReminder:
        keys = {
            "version",
            "source_kind",
            "provider",
            "group",
            "event_type",
            "transition_at",
            "planned_send_at",
            "outage_starts_at",
            "outage_ends_at",
            "outage_start_confirmed",
            "restoration_confirmed",
            "next_outage_starts_at",
            "next_outage_ends_at",
            "transition_key",
            "schedule_hashes",
        }
        if (
            type(value) is not dict
            or set(value) != keys
            or type(value["version"]) is not int
            or value["version"] != 1
            or value["source_kind"] != "alerts_energy_api"
            or any(
                type(value[key]) is not str
                for key in (
                    "provider",
                    "group",
                    "event_type",
                    "transition_key",
                )
            )
            or type(value["schedule_hashes"]) is not list
            or not 1 <= len(value["schedule_hashes"]) <= 3
            or any(type(pair) is not list or len(pair) != 2 for pair in value["schedule_hashes"])
        ):
            raise ValueError("invalid_map_reminder_wire")
        return cls(
            PowerProvider(value["provider"]),
            value["group"],
            value["event_type"],
            _time(value["transition_at"]),
            _time(value["planned_send_at"]),
            _time(value["outage_starts_at"]),
            _time(value["outage_ends_at"]),
            value["outage_start_confirmed"],
            value["restoration_confirmed"],
            _time(value["next_outage_starts_at"])
            if value["next_outage_starts_at"] is not None
            else None,
            _time(value["next_outage_ends_at"])
            if value["next_outage_ends_at"] is not None
            else None,
            value["transition_key"],
            tuple(tuple(pair) for pair in value["schedule_hashes"]),
        )


def usable_map_day(
    view: PowerScheduleView, now: datetime, *, stale_after_seconds: int = 1800
) -> PowerDaySchedule | None:
    """Return only a fresh, known mapping, directly published API daily plan."""
    clock = aware(now)
    if type(stale_after_seconds) is not int or not 1 <= stale_after_seconds <= 86400:
        raise ValueError("invalid_map_reminder_ttl")
    if not isinstance(view, PowerScheduleView):
        raise ValueError("invalid_map_reminder_view")
    validate_group(view.group_id)
    if view.provider not in SUPPORTED_PROVIDERS:
        return None
    day = view.schedule
    if (
        view.source_kind is not PowerSourceKind.ALERTS_ENERGY_API
        or view.source_status is not SourceStatus.FRESH
        or view.last_success_at is None
        or not 0 <= (clock - aware(view.last_success_at)).total_seconds() <= stale_after_seconds
        or day is None
        or day.provider is not view.provider
        or day.group_id != view.group_id
        or day.schedule_type is not ScheduleKind.CONFIRMED_DAY
        or day.publication_status is not PowerPublicationStatus.PLANNED
        or not day.intervals
    ):
        return None
    return day


def _merged_intervals(days: tuple[PowerDaySchedule, ...]) -> list[PowerInterval]:
    intervals = sorted(
        (interval for day in days for interval in day.intervals),
        key=lambda item: (aware(item.starts_at), aware(item.ends_at)),
    )
    merged: list[PowerInterval] = []
    for interval in intervals:
        if merged and aware(interval.starts_at) < aware(merged[-1].ends_at):
            raise ValueError("overlapping_map_reminder_days")
        if (
            merged
            and interval.state is merged[-1].state
            and aware(interval.starts_at) == aware(merged[-1].ends_at)
        ):
            old = merged.pop()
            merged.append(
                PowerInterval(
                    interval.group_id,
                    old.starts_at,
                    interval.ends_at,
                    interval.state,
                )
            )
        else:
            merged.append(interval)
    return merged


def _claim_windows(
    start: datetime,
    end: datetime,
    start_confirmed: bool,
    restored: bool,
    next_start: datetime | None,
    next_end: datetime | None,
) -> tuple[tuple[PowerState, datetime, datetime], ...]:
    epsilon = timedelta(microseconds=1)
    claims = [(PowerState.OUTAGE_CONFIRMED, aware(start), aware(end))]
    if start_confirmed:
        claims.append((PowerState.POWER_EXPECTED, aware(start) - epsilon, aware(start)))
    if restored:
        claims.append((PowerState.POWER_EXPECTED, aware(end), aware(end) + epsilon))
    if next_start is not None:
        claims.append((PowerState.POWER_EXPECTED, aware(end), aware(next_start)))
        claims.append(
            (
                PowerState.OUTAGE_CONFIRMED,
                aware(next_start),
                aware(next_end) if next_end is not None else aware(next_start) + epsilon,
            )
        )
        if next_end is not None:
            claims.append((PowerState.POWER_EXPECTED, aware(next_end), aware(next_end) + epsilon))
    return tuple(claims)


def _claim_hashes(
    provider: PowerProvider,
    group: str,
    days: tuple[PowerDaySchedule, ...],
    claims: tuple[tuple[PowerState, datetime, datetime], ...],
) -> tuple[tuple[str, str], ...]:
    """Validate every claimed segment, hashing only those fixed segments per day."""
    intervals = _merged_intervals(days)
    evidence: dict[str, set[tuple[str, str, str]]] = {}
    for state, start, end in claims:
        if end <= start:
            raise ValueError("invalid_map_reminder_claim")
        cursor = start
        for interval in intervals:
            lower, upper = max(start, aware(interval.starts_at)), min(end, aware(interval.ends_at))
            if upper <= lower:
                continue
            if lower != cursor or interval.state is not state:
                raise ValueError("map_reminder_claim_not_verified")
            cursor = upper
        if cursor != end:
            raise ValueError("map_reminder_claim_not_covered")
        for day in days:
            lower, upper = day_bounds(day.local_date)
            lower, upper = max(start, lower), min(end, upper)
            if lower < upper:
                evidence.setdefault(day.local_date.isoformat(), set()).add(
                    (lower.isoformat(), upper.isoformat(), state.value)
                )
    return tuple(
        (
            day,
            hashlib.sha256(
                json.dumps(
                    ["map-reminder-claim-v1", provider.value, group, day, sorted(segments)],
                    separators=(",", ":"),
                    ensure_ascii=True,
                ).encode()
            ).hexdigest(),
        )
        for day, segments in sorted(evidence.items())
    )


def map_reminder_evidence_hashes(
    event: MapPowerReminder,
    views: tuple[PowerScheduleView, ...],
    now: datetime,
    *,
    stale_after_seconds: int = 1800,
) -> tuple[tuple[str, str], ...]:
    """Recheck stored positive claims using current fresh views, ignoring other data.

    A newer plan may add an optional next outage to a freshly planned reminder.
    Rechecking the stored reminder preserves its conservative omission while
    still verifying every time/boundary that its message actually asserts.
    """
    clock = aware(now)
    if (
        not isinstance(event, MapPowerReminder)
        or type(views) is not tuple
        or len(views) > 3
        or any(not isinstance(view, PowerScheduleView) for view in views)
        or any((view.provider, view.group_id) != (event.provider, event.group_id) for view in views)
    ):
        raise ValueError("invalid_map_reminder_evidence_scope")
    days = tuple(
        day
        for view in views
        if (day := usable_map_day(view, clock, stale_after_seconds=stale_after_seconds)) is not None
    )
    today = clock.astimezone(KYIV).date()
    if (
        not days
        or len({day.local_date for day in days}) != len(days)
        or any(abs((day.local_date - today).days) > 1 for day in days)
    ):
        raise ValueError("invalid_map_reminder_evidence_dates")
    return _claim_hashes(
        event.provider,
        event.group_id,
        days,
        _claim_windows(
            event.outage_starts_at,
            event.outage_ends_at,
            event.outage_start_confirmed,
            event.restoration_confirmed,
            event.next_outage_starts_at,
            event.next_outage_ends_at,
        ),
    )


def reminder_due(event: MapPowerReminder, now: datetime, *, due_grace_seconds: int = 60) -> bool:
    clock = aware(now)
    if type(due_grace_seconds) is not int or not 1 <= due_grace_seconds <= 60:
        raise ValueError("invalid_map_reminder_grace")
    boundary, planned = aware(event.transition_at), aware(event.planned_send_at)
    if boundary == planned:
        return boundary <= clock < boundary + timedelta(seconds=due_grace_seconds)
    return planned <= clock < boundary and (clock - planned).total_seconds() < due_grace_seconds


def plan_map_reminders(
    views: tuple[PowerScheduleView, ...],
    now: datetime,
    *,
    outage_lead_minutes: int = 10,
    restoration_lead_minutes: int = 10,
    stale_after_seconds: int = 1800,
    due_grace_seconds: int = 60,
) -> tuple[MapPowerReminder, ...]:
    clock = aware(now)
    validate_lead(outage_lead_minutes)
    validate_lead(restoration_lead_minutes)
    if (
        type(views) is not tuple
        or len(views) > 3
        or any(not isinstance(view, PowerScheduleView) for view in views)
    ):
        raise ValueError("invalid_map_reminder_views")
    if type(due_grace_seconds) is not int or not 1 <= due_grace_seconds <= 60:
        raise ValueError("invalid_map_reminder_grace")
    identities = {(view.provider, view.group_id) for view in views}
    if len(identities) > 1:
        raise ValueError("map_reminder_requires_one_subscription")
    days = tuple(
        day
        for view in views
        if (day := usable_map_day(view, clock, stale_after_seconds=stale_after_seconds)) is not None
    )
    if not days:
        return ()
    local_today = clock.astimezone(KYIV).date()
    if any(abs((day.local_date - local_today).days) > 1 for day in days):
        raise ValueError("map_reminder_date_out_of_range")
    if len({day.local_date for day in days}) != len(days):
        raise ValueError("duplicate_map_reminder_date")
    provider, group = days[0].provider, days[0].group_id
    intervals = _merged_intervals(days)
    result: list[MapPowerReminder] = []
    for index, interval in enumerate(intervals):
        if interval.state is not PowerState.OUTAGE_CONFIRMED:
            continue
        start, end = aware(interval.starts_at), aware(interval.ends_at)
        before = intervals[index - 1] if index else None
        after = intervals[index + 1] if index + 1 < len(intervals) else None
        start_confirmed = bool(
            before is not None
            and before.state is PowerState.POWER_EXPECTED
            and aware(before.ends_at) == start
        )
        restored = bool(
            after is not None
            and after.state is PowerState.POWER_EXPECTED
            and aware(after.starts_at) == end
        )
        next_start: datetime | None = None
        next_end: datetime | None = None
        if restored and index + 2 < len(intervals):
            following = intervals[index + 2]
            if (
                following.state is PowerState.OUTAGE_CONFIRMED
                and after is not None
                and aware(after.ends_at) == aware(following.starts_at)
            ):
                next_start = aware(following.starts_at)
                next_after = intervals[index + 3] if index + 3 < len(intervals) else None
                if (
                    next_after is not None
                    and next_after.state is PowerState.POWER_EXPECTED
                    and aware(next_after.starts_at) == aware(following.ends_at)
                ):
                    next_end = aware(following.ends_at)
        choices = []
        if start_confirmed:
            choices.append((OUTAGE, start, outage_lead_minutes))
        if restored:
            choices.append((RESTORATION, end, restoration_lead_minutes))
        for kind, boundary, lead in choices:
            # Outage messages never display next-cut metadata; do not bind them
            # to unrelated future publication merely because it is available.
            stored_next_start = next_start if kind == RESTORATION else None
            stored_next_end = next_end if kind == RESTORATION else None
            hashes = _claim_hashes(
                provider,
                group,
                days,
                _claim_windows(
                    start,
                    end,
                    start_confirmed,
                    restored,
                    stored_next_start,
                    stored_next_end,
                ),
            )
            event = MapPowerReminder(
                provider,
                group,
                kind,
                boundary,
                boundary - timedelta(minutes=lead),
                start,
                end,
                start_confirmed,
                restored,
                stored_next_start,
                stored_next_end,
                _key(provider, group, kind, boundary),
                hashes,
            )
            if reminder_due(event, clock, due_grace_seconds=due_grace_seconds):
                result.append(event)
    return tuple(sorted(result, key=lambda item: (item.planned_send_at, item.transition_key)))

"""Explicit group mappings and Kyiv civil days, including 23/25-hour days."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from time_tl.domain.power_outages.models import GROUPS, PowerInterval, PowerState, aware

KYIV = ZoneInfo("Europe/Kyiv")


def day_bounds(day: date) -> tuple[datetime, datetime]:
    return (
        datetime.combine(day, time.min, KYIV).astimezone(UTC),
        datetime.combine(day + timedelta(days=1), time.min, KYIV).astimezone(UTC),
    )


def map_group(external: str, mapping: dict[str, str], *, version: str) -> str:
    # No fuzzy matching, naked +offset rule, or cross-operator identifier reuse.
    if version != "1" or type(external) is not str:
        raise ValueError("unsupported_power_group_mapping")
    value = mapping.get(external)
    if value not in GROUPS:
        raise ValueError("unknown_external_power_group")
    return str(value)


def local_boundary(day: date, label: str) -> datetime:
    if label == "24:00":
        return day_bounds(day)[1]
    if len(label) != 5 or label[2] != ":" or not label.replace(":", "").isascii():
        raise ValueError("invalid_power_clock")
    try:
        wall = datetime.combine(day, time.fromisoformat(label))
    except ValueError as exc:
        raise ValueError("invalid_power_clock") from exc
    choices = {
        candidate.astimezone(UTC)
        for fold in (0, 1)
        if (candidate := wall.replace(tzinfo=KYIV, fold=fold))
        .astimezone(UTC)
        .astimezone(KYIV)
        .replace(tzinfo=None)
        == wall
    }
    if len(choices) != 1:
        # A source without offsets cannot disambiguate a skipped/repeated DST hour.
        raise ValueError("ambiguous_or_nonexistent_power_clock")
    return choices.pop()


def normalize_intervals(
    group_id: str, values: tuple[PowerInterval, ...], day: date
) -> tuple[PowerInterval, ...]:
    lower, upper = day_bounds(day)
    ordered = sorted(values, key=lambda item: (aware(item.starts_at), aware(item.ends_at)))
    result: list[PowerInterval] = []
    cursor = lower
    for value in ordered:
        start, end = aware(value.starts_at), aware(value.ends_at)
        if value.group_id != group_id or start < cursor or end > upper:
            raise ValueError("invalid_power_interval_coverage")
        if start > cursor:
            result.append(PowerInterval(group_id, cursor, start, PowerState.UNKNOWN))
        if result and result[-1].state == value.state and aware(result[-1].ends_at) == start:
            old = result.pop()
            result.append(PowerInterval(group_id, old.starts_at, end, value.state))
        else:
            result.append(PowerInterval(group_id, start, end, value.state))
        cursor = end
    if cursor < upper:
        result.append(PowerInterval(group_id, cursor, upper, PowerState.UNKNOWN))
    return tuple(result)

"""Documented Alerts Energy API v1, conservative published-plan normalization."""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from typing import Any

from time_tl.domain.power_outages.models import (
    GROUPS,
    PowerDaySchedule,
    PowerInterval,
    PowerProvider,
    PowerPublicationStatus,
    PowerScheduleSnapshot,
    PowerSourceKind,
    PowerState,
    ScheduleKind,
    SourceStatus,
    aware,
)
from time_tl.domain.power_outages.normalization import KYIV, day_bounds

MAX_API_BYTES = 2_097_152
STATUSES = {"ScheduleApplies", "NoShutdowns", "WaitingForSchedule", "EmergencyShutdowns"}
# Public regional registry identities, additionally checked against the API suppliers
# response before each live fetch. No inferred IDs for other regional operators.
KNOWN_BINDINGS = {
    PowerProvider.DTEK: ("dnipropetrovska-oblast", "dnipropetrovska_oblast_oblenergo"),
    PowerProvider.CEK: ("dnipropetrovska-oblast", "dnipropetrovska_oblast_cek"),
}


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_api_key")
        result[key] = value
    return result


def _nonfinite(value: str) -> None:
    raise ValueError("nonfinite_api_number")


def parse_api_json(payload: bytes) -> object:
    if type(payload) is not bytes or not 0 < len(payload) <= MAX_API_BYTES:
        raise ValueError("invalid_api_size")
    try:
        result = json.loads(
            payload.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_nonfinite
        )
        nodes = [(result, 0)]
        count = 0
        while nodes:
            node, depth = nodes.pop()
            count += 1
            if depth > 16 or count > 30000:
                raise ValueError("api_structure_limit")
            if type(node) is dict:
                nodes.extend((item, depth + 1) for item in node.values())
            elif type(node) is list:
                nodes.extend((item, depth + 1) for item in node)
        return result
    except (UnicodeError, RecursionError, json.JSONDecodeError):
        raise ValueError("invalid_api_json") from None


def validate_binding(provider: PowerProvider, area_slug: str, supplier_key: str) -> None:
    if not isinstance(provider, PowerProvider) or any(
        type(value) is not str or re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,127}", value) is None
        for value in (area_slug, supplier_key)
    ):
        raise ValueError("invalid_api_operator_binding")
    if KNOWN_BINDINGS.get(provider) != (area_slug, supplier_key):
        raise ValueError("unverified_api_operator_binding")


def validate_supplier_registry(value: object, *, area_slug: str, supplier_key: str) -> None:
    if type(value) is not list or not 1 <= len(value) <= 128:
        raise ValueError("invalid_api_supplier_registry")
    found = 0
    seen: set[str] = set()
    for row in value:
        if type(row) is not dict or not {"supplier_key", "area_slug"} <= set(row):
            raise ValueError("invalid_api_supplier")
        key = row["supplier_key"]
        if type(key) is not str or key in seen or row["area_slug"] != area_slug:
            raise ValueError("ambiguous_api_supplier")
        seen.add(key)
        found += key == supplier_key
    if found != 1:
        raise ValueError("unverified_api_supplier")


def _timestamp(value: object, fetched: datetime) -> datetime | None:
    if value is None:
        return None
    if type(value) is not str or len(value) > 40:
        raise ValueError("invalid_api_timestamp")
    stamp = aware(datetime.fromisoformat(value.replace("Z", "+00:00")))
    if stamp > fetched:
        raise ValueError("future_api_timestamp")
    return stamp


def _slots(value: object) -> list[int]:
    if (
        type(value) is not list
        or len(value) not in {0, 24}
        or any(type(item) is not int or item not in {0, 1, 2, 3} for item in value)
    ):
        raise ValueError("invalid_api_slots")
    return value


def _announcement_blocks(
    value: object,
    fetched: datetime,
    lower: datetime,
    upper: datetime,
    *,
    ordinary_plan_verified: bool,
) -> bool:
    if type(value) is not list or len(value) > 64:
        raise ValueError("invalid_api_announcements")
    blocked = False
    for item in value:
        if (
            type(item) is not dict
            or not {"kind", "state", "scope"} <= set(item)
            or set(item)
            - {
                "kind",
                "state",
                "scope",
                "published_at",
                "effective_at",
                "expires_at",
            }
            or type(item["kind"]) is not str
            or not 1 <= len(item["kind"]) <= 64
            or any(ord(char) < 32 or ord(char) == 127 for char in item["kind"])
        ):
            raise ValueError("invalid_api_announcement")
        if (
            type(item["state"]) is not str
            or type(item["scope"]) is not str
            or item["state"] not in {"active", "cancelled", "possible"}
            or item["scope"] not in {"supplier", "local", "regional"}
        ):
            raise ValueError("unknown_api_announcement")
        stamps: dict[str, datetime] = {}
        for key in ("published_at", "effective_at", "expires_at"):
            if item.get(key) is not None:
                # Expiry/effective may be in the future; publication may not.
                if type(item[key]) is not str or len(item[key]) > 40:
                    raise ValueError("invalid_api_announcement_timestamp")
                stamp = aware(datetime.fromisoformat(item[key].replace("Z", "+00:00")))
                if key == "published_at" and stamp > fetched:
                    raise ValueError("future_api_announcement")
                stamps[key] = stamp
        effective, expires = stamps.get("effective_at"), stamps.get("expires_at")
        if effective is not None and expires is not None and effective >= expires:
            raise ValueError("invalid_api_announcement_window")
        # Only a proven disjoint notice can be ignored. An overlapping or undated
        # unknown restriction suppresses the entire day; no address scope is guessed.
        if expires is not None and expires <= lower or effective is not None and effective >= upper:
            continue
        # Exact observed supplier GPV notice acknowledges the independently
        # validated complete/fresh ScheduleApplies plan. GPV is not a published
        # kind enum; do not extend this conjunction to aliases, scopes or states.
        if (
            ordinary_plan_verified
            and item["kind"] == "GPV"
            and item["state"] == "active"
            and item["scope"] == "supplier"
        ):
            continue
        blocked = True
    return blocked


def _day(
    row: dict[str, Any],
    prefix: str,
    day: date,
    provider: PowerProvider,
    fetched: datetime,
    checked: datetime | None,
    stale_after_seconds: int,
) -> PowerDaySchedule:
    values = _slots(row.get(prefix))
    possible = _slots(row.get(prefix + "_possible", []))
    status = row.get(prefix + "_status")
    if status not in STATUSES:
        raise ValueError("unknown_api_status")
    lower, upper = day_bounds(day)
    blocked = _announcement_blocks(
        row.get(prefix + "_announcements", []),
        fetched,
        lower,
        upper,
        ordinary_plan_verified=(
            status == "ScheduleApplies"
            and len(values) == 24
            and checked is not None
            and 0 <= (fetched - checked).total_seconds() <= stale_after_seconds
        ),
    )
    group = row["queue"]
    intervals: tuple[PowerInterval, ...]
    if status == "EmergencyShutdowns":
        publication = PowerPublicationStatus.EMERGENCY
        intervals = (PowerInterval(group, lower, upper, PowerState.UNKNOWN),)
    elif blocked:
        publication = PowerPublicationStatus.UNKNOWN
        intervals = (PowerInterval(group, lower, upper, PowerState.UNKNOWN),)
    elif status == "WaitingForSchedule" or not values:
        if status == "WaitingForSchedule" and values:
            raise ValueError("inconsistent_api_waiting_schedule")
        publication, intervals = PowerPublicationStatus.NO_SCHEDULE, ()
    else:
        if status == "NoShutdowns" and any(values):
            raise ValueError("inconsistent_api_no_shutdowns")
        if upper - lower != timedelta(hours=24):
            raise ValueError("ambiguous_api_dst_day")
        publication = PowerPublicationStatus.PLANNED
        parts: list[PowerInterval] = []
        for hour, slot in enumerate(values):
            for half in range(2):
                start = lower + timedelta(hours=hour, minutes=half * 30)
                outage = slot == 1 or (slot == 2 and half == 0) or (slot == 3 and half == 1)
                state = PowerState.OUTAGE_CONFIRMED if outage else PowerState.POWER_EXPECTED
                if possible and possible[hour] and not outage:
                    # Possible numeric subdivision is not documented: no positive guess.
                    state = PowerState.UNKNOWN
                parts.append(PowerInterval(group, start, start + timedelta(minutes=30), state))
        intervals = tuple(parts)
    return PowerDaySchedule(
        provider, group, day, ScheduleKind.CONFIRMED_DAY, intervals, publication_status=publication
    )


def normalize_cached_api_day(
    row: dict[str, Any],
    *,
    provider: PowerProvider,
    area_slug: str,
    day: date,
    fetched_at: datetime,
    stale_after_seconds: int,
) -> PowerDaySchedule:
    """Project one previously verified cache record for an interactive forecast."""
    validate_binding(provider, area_slug, row["initiator"])
    if row["queue"] not in GROUPS:
        raise ValueError("unknown_api_group")
    fetched = aware(fetched_at)
    offset = (day - fetched.astimezone(KYIV).date()).days
    if offset not in (0, 1):
        raise ValueError("mismatched_api_date")
    prefix = "today" if offset == 0 else "tomorrow"
    if row[prefix + "_date"] != day.isoformat():
        raise ValueError("mismatched_api_date")
    checked = _timestamp(row.get(prefix + "_checked_at"), fetched)
    return _day(row, prefix, day, provider, fetched, checked, stale_after_seconds)


def normalize_api_schedules(
    value: object,
    *,
    provider: PowerProvider,
    area_slug: str,
    supplier_key: str,
    fetched_at: datetime,
    stale_after_seconds: int = 1800,
) -> PowerScheduleSnapshot:
    validate_binding(provider, area_slug, supplier_key)
    fetched = aware(fetched_at)
    if type(stale_after_seconds) is not int or stale_after_seconds < 1:
        raise ValueError("invalid_api_ttl")
    if type(value) is not list or not 1 <= len(value) <= 1536:
        raise ValueError("invalid_api_rows")
    rows = []
    for row in value:
        if type(row) is not dict or type(row.get("initiator")) is not str:
            raise ValueError("invalid_api_row")
        if row["initiator"] == supplier_key:
            if type(row.get("queue")) is not str or row["queue"] not in GROUPS:
                raise ValueError("unknown_api_group")
            rows.append(row)
    if len(rows) != len(GROUPS) or {row.get("queue") for row in rows} != set(GROUPS):
        raise ValueError("incomplete_api_groups")
    dates = fetched.astimezone(KYIV).date()
    days: list[PowerDaySchedule] = []
    updates = []
    missing_update = False
    planned_checks: list[datetime | None] = []
    current_checks: list[datetime | None] = []
    for row in rows:
        updated = _timestamp(row.get("updated"), fetched)
        if updated is None:
            missing_update = True
        else:
            updates.append(updated)
        for prefix, offset in (("today", 0), ("tomorrow", 1)):
            local_date = dates + timedelta(days=offset)
            if row.get(prefix + "_date") != local_date.isoformat():
                raise ValueError("mismatched_api_date")
            checked = _timestamp(row.get(prefix + "_checked_at"), fetched)
            normalized = _day(
                row, prefix, local_date, provider, fetched, checked, stale_after_seconds
            )
            days.append(normalized)
            if normalized.publication_status is PowerPublicationStatus.PLANNED:
                planned_checks.append(checked)
            if prefix == "today":
                current_checks.append(checked)
    updated_at = min(updates) if updates and not missing_update else None
    required_checks = planned_checks or current_checks
    checked_at = (
        min(check for check in required_checks if check is not None)
        if required_checks and all(check is not None for check in required_checks)
        else None
    )
    stale = (
        updated_at is None
        or checked_at is None
        or (fetched - checked_at).total_seconds() > stale_after_seconds
    )
    return PowerScheduleSnapshot(
        provider,
        fetched,
        updated_at,
        () if stale else tuple(days),
        SourceStatus.STALE if stale else SourceStatus.FRESH,
        "SOURCE_TIMESTAMP_UNVERIFIED"
        if updated_at is None or checked_at is None
        else "STALE_SOURCE"
        if stale
        else None,
        source_kind=PowerSourceKind.ALERTS_ENERGY_API,
        source_url=f"https://alerts.energy/api/v1/source-registry/areas/{area_slug}/shutdowns",
        source_checked_at=checked_at,
    )

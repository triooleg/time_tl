"""Conservative normalization; unverified slot semantics cannot trigger sends."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from time_tl.domain.power_outages.models import (
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
    validate_group,
)
from time_tl.domain.power_outages.normalization import KYIV, day_bounds
from time_tl.infrastructure.power_outages.alerts_energy_parser import ParsedAlertsEnergyPage

REGION_PATHS = {
    PowerProvider.DTEK: "dnipropetrovska-oblast",
    PowerProvider.CEK: "dnipropetrovska-oblast",
    PowerProvider.DTEK_KYIV: "kyiv",
    PowerProvider.DTEK_KYIV_REGION: "kyivska-oblast",
    PowerProvider.DTEK_ODESA: "odeska-oblast",
    PowerProvider.DTEK_DONETSK: "donetska-oblast",
}
# Only identities actually observed in public regional supplier metadata.
VERIFIED_IDENTITIES = {
    PowerProvider.DTEK: "dnipropetrovska_oblast_oblenergo",
    PowerProvider.CEK: "dnipropetrovska_oblast_cek",
}


def _timestamp(value: object, fetched: datetime) -> datetime:
    if type(value) is not str or len(value) > 40:
        raise ValueError("invalid_alerts_energy_timestamp")
    try:
        stamp = aware(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        raise ValueError("invalid_alerts_energy_timestamp") from None
    if stamp > fetched:
        raise ValueError("future_alerts_energy_timestamp")
    return stamp


def _slots(value: object, *, optional: bool = False) -> list[int]:
    if type(value) is not list or (len(value) not in (0, 24) if optional else len(value) != 24):
        raise ValueError("invalid_alerts_energy_slots")
    if any(type(slot) is not int or slot not in (0, 1, 2, 3) for slot in value):
        raise ValueError("invalid_alerts_energy_slots")
    return value


def _publication(record: dict[str, object], prefix: str) -> PowerPublicationStatus:
    # A locality-aware policy is not established: any active/unclassified emergency
    # suppresses the entire day. A previous plan never overrides an announcement.
    announcements = record.get(f"{prefix}Announcements")
    if type(announcements) is not list:
        return PowerPublicationStatus.UNKNOWN
    for item in announcements:
        if type(item) is not dict:
            raise ValueError("invalid_alerts_energy_announcement")
        kind, state = item.get("kind"), item.get("state")
        if kind == "Emergency" and state not in ("cancelled", "inactive"):
            return PowerPublicationStatus.EMERGENCY
        if kind == "GPV" and state == "cancelled":
            return PowerPublicationStatus.EMERGENCY
    # Even a published plan has no verified 0..3 slot semantic reference yet.
    return PowerPublicationStatus.UNKNOWN


def normalize_alerts_energy_page(
    page: ParsedAlertsEnergyPage,
    *,
    provider: PowerProvider,
    fetched_at: datetime,
) -> PowerScheduleSnapshot:
    fetched = aware(fetched_at)
    if page.region_slug != REGION_PATHS.get(provider):
        raise ValueError("mismatched_alerts_energy_region")
    identity = VERIFIED_IDENTITIES.get(provider)
    if identity is None:
        raise ValueError("unverified_alerts_energy_operator")
    rows = [row for row in page.records if row.get("initiator") == identity]
    if not rows:
        raise ValueError("missing_alerts_energy_operator")
    days: list[PowerDaySchedule] = []
    updated: list[datetime] = []
    seen: set[tuple[str, date]] = set()
    groups: set[str] = set()
    local_today = fetched.astimezone(KYIV).date()
    for row in rows:
        group = row.get("queue")
        if type(group) is not str:
            raise ValueError("invalid_alerts_energy_group")
        validate_group(group)
        if group in groups:
            raise ValueError("duplicate_alerts_energy_group")
        groups.add(group)
        updated.append(_timestamp(row.get("updated"), fetched))
        for prefix, offset in (("today", 0), ("tomorrow", 1)):
            raw_date = row.get(f"{prefix}Date")
            if type(raw_date) is not str:
                raise ValueError("missing_alerts_energy_date")
            try:
                day = date.fromisoformat(raw_date)
            except ValueError:
                raise ValueError("invalid_alerts_energy_date") from None
            if raw_date != day.isoformat() or day != local_today + timedelta(days=offset):
                raise ValueError("stale_alerts_energy_date")
            key = (group, day)
            if key in seen:
                raise ValueError("duplicate_alerts_energy_day")
            seen.add(key)
            values = _slots(row.get(prefix), optional=True)
            possible = row.get(f"{prefix}Possible")
            if possible is not None:
                _slots(possible, optional=True)
            status = row.get(f"{prefix}Status")
            if status not in ("ScheduleApplies", "WaitingForSchedule"):
                raise ValueError("unknown_alerts_energy_status")
            if status == "WaitingForSchedule" and values:
                raise ValueError("inconsistent_alerts_energy_status")
            publication = _publication(row, prefix)
            if (
                status == "WaitingForSchedule"
                and not values
                and publication is not PowerPublicationStatus.EMERGENCY
            ):
                publication = PowerPublicationStatus.NO_SCHEDULE
            lower, upper = day_bounds(day)
            # Never guess either full-hour or half-hour status from numeric slots.
            days.append(
                PowerDaySchedule(
                    provider,
                    group,
                    day,
                    ScheduleKind.CONFIRMED_DAY,
                    ()
                    if publication is PowerPublicationStatus.NO_SCHEDULE
                    else (PowerInterval(group, lower, upper, PowerState.UNKNOWN),),
                    publication_status=publication,
                )
            )
    return PowerScheduleSnapshot(
        provider,
        fetched,
        min(updated),
        tuple(days),
        SourceStatus.FRESH,
        error_code="REFERENCE_UNVERIFIED",
        source_kind=PowerSourceKind.ALERTS_ENERGY_HTML,
        source_url="https://alerts.energy/" + page.region_slug,
    )

"""Validated, provider-neutral scheduled power data (never physical telemetry)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from urllib.parse import urlsplit

GROUPS = tuple(f"{number}.{part}" for number in range(1, 7) for part in (1, 2))
LEADS = (0, 5, 10, 15, 30, 60, 120)


class PowerProvider(StrEnum):
    DTEK = "dtek_dnipro"
    DTEK_KYIV = "dtek_kyiv"
    DTEK_KYIV_REGION = "dtek_kyiv_region"
    DTEK_ODESA = "dtek_odesa"
    DTEK_DONETSK = "dtek_donetsk"
    CEK = "cek_dnipro"

    @property
    def label(self) -> str:
        return {
            self.DTEK: "ДТЕК Дніпровські електромережі",
            self.DTEK_KYIV: "ДТЕК Київські електромережі",
            self.DTEK_KYIV_REGION: "ДТЕК Київські регіональні електромережі",
            self.DTEK_ODESA: "ДТЕК Одеські електромережі",
            self.DTEK_DONETSK: "ДТЕК Донецькі електромережі",
            self.CEK: "Центральна енергетична компанія (ЦЕК)",
        }[self]

    @property
    def official_url(self) -> str:
        return {
            self.DTEK: "https://www.dtek-dnem.com.ua/ua/shutdowns",
            self.DTEK_KYIV: "https://www.dtek-kem.com.ua/ua/shutdowns",
            self.DTEK_KYIV_REGION: "https://www.dtek-krem.com.ua/ua/shutdowns",
            self.DTEK_ODESA: "https://www.dtek-oem.com.ua/ua/shutdowns",
            self.DTEK_DONETSK: "https://www.dtek-dem.com.ua/ua/shutdowns",
            self.CEK: "https://cek.dp.ua/index.php/cpojivaham/vidkliuchennia.html",
        }[self]


class PowerState(StrEnum):
    OUTAGE_CONFIRMED = "OUTAGE_CONFIRMED"
    POWER_EXPECTED = "POWER_EXPECTED"
    POSSIBLE_OUTAGE = "POSSIBLE_OUTAGE"
    UNKNOWN = "UNKNOWN"
    NO_SCHEDULE = "NO_SCHEDULE"


class SourceStatus(StrEnum):
    FRESH = "FRESH"
    STALE = "STALE"
    UNAVAILABLE = "UNAVAILABLE"
    INVALID = "INVALID"


class PowerSourceKind(StrEnum):
    OFFICIAL = "official"
    BEZSVITLA = "bezsvitla"
    ALERTS_ENERGY_HTML = "alerts_energy_html"
    ALERTS_ENERGY_API = "alerts_energy_api"


class PowerPublicationStatus(StrEnum):
    PLANNED = "PLANNED"
    NO_SCHEDULE = "NO_SCHEDULE"
    EMERGENCY = "EMERGENCY"
    UNKNOWN = "UNKNOWN"


class ScheduleKind(StrEnum):
    CONFIRMED_DAY = "CONFIRMED_DAY"
    FORECAST_WEEK = "FORECAST_WEEK"


def aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("power_timestamp_requires_timezone")
    return value.astimezone(UTC)


def validate_group(value: str) -> str:
    if type(value) is not str or value not in GROUPS:
        raise ValueError("invalid_power_group")
    return value


def validate_lead(value: int) -> int:
    if type(value) is not int or not 0 <= value <= 180:
        raise ValueError("invalid_power_lead")
    return value


@dataclass(frozen=True)
class PowerInterval:
    group_id: str
    starts_at: datetime
    ends_at: datetime
    state: PowerState

    def __post_init__(self) -> None:
        validate_group(self.group_id)
        if aware(self.starts_at) >= aware(self.ends_at) or not isinstance(self.state, PowerState):
            raise ValueError("invalid_power_interval")


@dataclass(frozen=True)
class PowerDaySchedule:
    provider: PowerProvider
    group_id: str
    local_date: date
    schedule_type: ScheduleKind
    intervals: tuple[PowerInterval, ...]
    mapping_version: str = "1"
    publication_status: PowerPublicationStatus = PowerPublicationStatus.PLANNED

    def __post_init__(self) -> None:
        from time_tl.domain.power_outages.normalization import day_bounds, normalize_intervals

        validate_group(self.group_id)
        if not isinstance(self.provider, PowerProvider) or not isinstance(
            self.schedule_type, ScheduleKind
        ):
            raise ValueError("invalid_power_schedule_type")
        if (
            type(self.local_date) is not date
            or type(self.mapping_version) is not str
            or self.mapping_version != "1"
        ):
            raise ValueError("invalid_power_schedule_date_or_mapping")
        if type(self.intervals) is not tuple or len(self.intervals) > 192:
            raise ValueError("invalid_power_interval_count")
        if not isinstance(self.publication_status, PowerPublicationStatus):
            raise ValueError("invalid_power_publication_status")
        if self.publication_status is PowerPublicationStatus.NO_SCHEDULE and self.intervals:
            raise ValueError("no_schedule_cannot_have_intervals")
        if self.publication_status is not PowerPublicationStatus.PLANNED and any(
            item.state not in {PowerState.UNKNOWN, PowerState.NO_SCHEDULE}
            for item in self.intervals
        ):
            raise ValueError("unconfirmed_publication_cannot_claim_power_state")
        lower, upper = day_bounds(self.local_date)
        if self.intervals:
            object.__setattr__(
                self,
                "intervals",
                normalize_intervals(self.group_id, self.intervals, self.local_date),
            )
        previous = lower
        for item in self.intervals:
            start, end = aware(item.starts_at), aware(item.ends_at)
            if item.group_id != self.group_id or start < previous or end > upper:
                raise ValueError("overlapping_or_out_of_day_power_interval")
            previous = end

    @property
    def content_hash(self) -> str:
        # Acquisition/source timestamps deliberately do not define semantic content.
        value = {
            "provider": self.provider.value,
            "group": self.group_id,
            "date": self.local_date.isoformat(),
            "kind": self.schedule_type.value,
            "mapping_version": self.mapping_version,
            "publication_status": self.publication_status.value,
            "intervals": [
                [
                    aware(item.starts_at).isoformat(),
                    aware(item.ends_at).isoformat(),
                    item.state.value,
                ]
                for item in self.intervals
            ],
        }
        return hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()


@dataclass(frozen=True)
class PowerScheduleSnapshot:
    provider: PowerProvider
    fetched_at: datetime
    source_updated_at: datetime | None
    days: tuple[PowerDaySchedule, ...]
    status: SourceStatus
    error_code: str | None = None
    schema_version: int = 1
    source_kind: PowerSourceKind = PowerSourceKind.OFFICIAL
    source_url: str | None = None
    locality: str | None = None
    source_checked_at: datetime | None = None

    def __post_init__(self) -> None:
        observed = aware(self.fetched_at)
        if not isinstance(self.provider, PowerProvider) or not isinstance(
            self.status, SourceStatus
        ):
            raise ValueError("invalid_power_source_type")
        if not isinstance(self.source_kind, PowerSourceKind):
            raise ValueError("invalid_power_source_kind")
        if self.source_url is not None:
            if type(self.source_url) is not str or len(self.source_url) > 2048:
                raise ValueError("invalid_power_source_url")
            parsed = urlsplit(self.source_url)
            allowed_hosts: dict[PowerSourceKind, set[str | None]] = {
                PowerSourceKind.OFFICIAL: {urlsplit(self.provider.official_url).hostname},
                PowerSourceKind.BEZSVITLA: {"bezsvitla.com.ua", "www.bezsvitla.com.ua"},
                PowerSourceKind.ALERTS_ENERGY_HTML: {"alerts.energy"},
                PowerSourceKind.ALERTS_ENERGY_API: {"alerts.energy"},
            }
            if (
                parsed.scheme != "https"
                or parsed.hostname not in allowed_hosts[self.source_kind]
                or parsed.username is not None
                or parsed.password is not None
                or parsed.fragment
                or parsed.port not in {None, 443}
            ):
                raise ValueError("invalid_power_source_url")
        if self.locality is not None and (
            type(self.locality) is not str
            or not 0 < len(self.locality) <= 128
            or any(ord(char) < 32 for char in self.locality)
        ):
            raise ValueError("invalid_power_locality")
        if self.source_updated_at is not None and aware(self.source_updated_at) > observed:
            raise ValueError("future_power_source_timestamp")
        if self.source_checked_at is not None:
            if not isinstance(self.source_checked_at, datetime):
                raise ValueError("invalid_power_source_check_timestamp")
            if aware(self.source_checked_at) > observed:
                raise ValueError("future_power_source_check_timestamp")
        if not isinstance(self.provider, PowerProvider) or not isinstance(
            self.status, SourceStatus
        ):
            raise ValueError("invalid_power_source_type")
        if (
            type(self.days) is not tuple
            or len(self.days) > 168
            or type(self.schema_version) is not int
            or self.schema_version != 1
        ):
            raise ValueError("invalid_power_snapshot")
        keys = [(item.local_date, item.group_id, item.schedule_type) for item in self.days]
        if len(keys) != len(set(keys)) or any(item.provider != self.provider for item in self.days):
            raise ValueError("duplicate_or_mismatched_power_day")
        if self.status is SourceStatus.FRESH and not self.days:
            raise ValueError("empty_power_snapshot_is_not_fresh")
        if self.status is not SourceStatus.FRESH and self.days:
            raise ValueError("failed_power_fetch_cannot_publish_schedules")
        if self.error_code is not None and (
            not self.error_code.isascii()
            or len(self.error_code) > 64
            or not self.error_code.replace("_", "").isalnum()
        ):
            raise ValueError("invalid_power_error_code")


@dataclass(frozen=True)
class PowerSubscription:
    user_id: str
    provider: PowerProvider
    group_id: str
    enabled: bool = False
    outage_notifications_enabled: bool = True
    restoration_notifications_enabled: bool = True
    outage_lead_minutes: int = 30
    restoration_lead_minutes: int = 15
    revision: int = 1

    def __post_init__(self) -> None:
        if type(self.user_id) is not str or not 0 < len(self.user_id) <= 64:
            raise ValueError("invalid_power_user")
        validate_group(self.group_id)
        validate_lead(self.outage_lead_minutes)
        validate_lead(self.restoration_lead_minutes)
        if not isinstance(self.provider, PowerProvider) or type(self.revision) is not int:
            raise ValueError("invalid_power_subscription")
        if self.revision < 1 or not all(
            type(item) is bool
            for item in (
                self.enabled,
                self.outage_notifications_enabled,
                self.restoration_notifications_enabled,
            )
        ):
            raise ValueError("invalid_power_preferences")


@dataclass(frozen=True)
class PowerScheduleView:
    provider: PowerProvider
    group_id: str
    source_status: SourceStatus
    last_success_at: datetime | None
    last_attempt_at: datetime | None
    schedule: PowerDaySchedule | None
    revision: int | None = None
    error_code: str | None = None
    source_kind: PowerSourceKind = PowerSourceKind.OFFICIAL
    source_url: str | None = None

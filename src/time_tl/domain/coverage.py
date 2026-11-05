"""Derived geographic demand: shared work items, never alert/source authority.

Coordinates come only from explicitly approved imported weather points. User
coordinates, names and Telegram IDs are not inputs or exported by this model.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from .power_outages.models import PowerProvider, aware, validate_group

ALERT_PROVIDER = "alerts.in.ua"
_ROOT_KINDS = frozenset({"OBLAST", "SPECIAL_STATUS_CITY"})
_PARENT = {"RAION": "OBLAST", "HROMADA": "RAION"}
_KINDS = _ROOT_KINDS | _PARENT.keys()


def _katottg(value: object) -> bool:
    return type(value) is str and re.fullmatch(r"UA[0-9]{17}", value) is not None


@dataclass(frozen=True)
class CoverageTerritoryNode:
    region_id: str
    katottg: str | None
    name: str
    kind: str
    parent_region_id: str | None
    selectable: bool = True


@dataclass(frozen=True)
class CoverageAlertScope:
    provider: str | None
    catalog_version: str | None
    selected_region_id: str
    applicable_region_ids: tuple[str, ...]


@dataclass(frozen=True)
class CoveragePowerSelection:
    provider: PowerProvider
    group_id: str
    notifications_enabled: bool = False

    def __post_init__(self) -> None:
        if (
            not isinstance(self.provider, PowerProvider)
            or type(self.notifications_enabled) is not bool
        ):
            raise ValueError("invalid_coverage_power_selection")
        validate_group(self.group_id)


@dataclass(frozen=True, repr=False)
class CoverageSubscriber:
    user_key: str
    confirmed_region_id: str | None
    mode: str | None
    alerts_enabled: bool
    changed_at: datetime
    alert_scope: CoverageAlertScope | None = None
    power: CoveragePowerSelection | None = None
    power_changed_at: datetime | None = None
    weather_enabled: bool = False
    forecast_requested: bool = False
    weather_changed_at: datetime | None = None
    issues: tuple[str, ...] = ()


@dataclass(frozen=True)
class WeatherSourceReference:
    url: str = field(repr=False)
    sha256: str
    version: str
    approved_at: datetime

    def valid(self) -> bool:
        try:
            parsed = urlsplit(self.url)
            return bool(
                parsed.scheme == "https"
                and parsed.hostname
                and not parsed.username
                and not parsed.password
                and not parsed.query
                and not parsed.fragment
                and re.fullmatch(r"[a-f0-9]{64}", self.sha256)
                and type(self.version) is str
                and 1 <= len(self.version) <= 160
                and all(ord(char) >= 32 for char in self.version)
                and aware(self.approved_at)
            )
        except (TypeError, ValueError, AttributeError):
            return False


@dataclass(frozen=True)
class CoverageWeatherLocation:
    location_id: str
    katottg: str
    region_id: str
    locality_name: str
    latitude: float = field(repr=False)
    longitude: float = field(repr=False)
    coverage_type: str
    enabled: bool
    source: WeatherSourceReference | None

    def approved(self) -> bool:
        try:
            valid_id = str(UUID(self.location_id)) == self.location_id
            return (
                valid_id
                and _katottg(self.katottg)
                and self.enabled is True
                and self.coverage_type in {"BASE", "EXTENDED"}
                and type(self.latitude) in {float, int}
                and type(self.longitude) in {float, int}
                and math.isfinite(self.latitude)
                and math.isfinite(self.longitude)
                and 44 <= self.latitude <= 53.5
                and 22 <= self.longitude <= 41
                and self.source is not None
                and self.source.valid()
            )
        except (TypeError, ValueError, AttributeError):
            return False


@dataclass(frozen=True)
class ActiveTerritoryDemand:
    territory: CoverageTerritoryNode
    oblast: CoverageTerritoryNode
    raion: CoverageTerritoryNode | None
    hromada: CoverageTerritoryNode | None
    user_count: int
    alert_user_count: int
    power_user_count: int
    weather_user_count: int
    weather_notification_user_count: int
    last_subscription_change: datetime


@dataclass(frozen=True)
class ActivePowerGroupDemand:
    provider: PowerProvider
    group_id: str
    user_count: int
    notification_user_count: int
    last_subscription_change: datetime


@dataclass(frozen=True)
class ActiveWeatherPointDemand:
    location: CoverageWeatherLocation
    user_count: int
    notification_user_count: int
    last_subscription_change: datetime


@dataclass(frozen=True)
class CoverageExclusion:
    reason: str
    user_count: int


@dataclass(frozen=True)
class ActiveGeographicCoverage:
    observed_at: datetime
    catalog_version: str | None
    territories: tuple[ActiveTerritoryDemand, ...]
    power_groups: tuple[ActivePowerGroupDemand, ...]
    weather_points: tuple[ActiveWeatherPointDemand, ...]
    active_alert_region_ids: frozenset[str]
    excluded: tuple[CoverageExclusion, ...]
    requested_user_count: int
    active_user_count: int

    @property
    def power_group_keys(self) -> frozenset[tuple[PowerProvider, str]]:
        return frozenset((item.provider, item.group_id) for item in self.power_groups)

    @property
    def weather_location_ids(self) -> frozenset[str]:
        return frozenset(item.location.location_id for item in self.weather_points)

    @property
    def fingerprint(self) -> str:
        def wire(value: Any) -> Any:
            if isinstance(value, datetime):
                return aware(value).isoformat()
            if isinstance(value, (set, frozenset)):
                return sorted(value)
            if isinstance(value, dict):
                return {key: wire(item) for key, item in value.items() if key != "observed_at"}
            if isinstance(value, (tuple, list)):
                return [wire(item) for item in value]
            return value

        return hashlib.sha256(
            json.dumps(wire(asdict(self)), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


class _Catalog:
    def __init__(self, rows: tuple[CoverageTerritoryNode, ...], version: str | None) -> None:
        self.version, self.by_id = version, {row.region_id: row for row in rows}
        self.valid = bool(version and rows and len(self.by_id) == len(rows))
        self.code_counts = Counter(row.katottg for row in rows if _katottg(row.katottg))
        self.children: dict[str, list[CoverageTerritoryNode]] = defaultdict(list)
        for row in rows:
            if row.parent_region_id is not None:
                self.children[row.parent_region_id].append(row)

    def path(self, region_id: str | None) -> tuple[CoverageTerritoryNode, ...] | None:
        if not self.valid or type(region_id) is not str or region_id not in self.by_id:
            return None
        seen, path = set(), []
        row = self.by_id[region_id]
        while True:
            if (
                row.region_id in seen
                or row.kind not in _KINDS
                or row.selectable is not True
                or not _katottg(row.katottg)
                or self.code_counts[row.katottg] != 1
            ):
                return None
            seen.add(row.region_id)
            path.append(row)
            if row.parent_region_id is None:
                if row.kind not in _ROOT_KINDS:
                    return None
                break
            parent = self.by_id.get(row.parent_region_id)
            if parent is None or _PARENT.get(row.kind) != parent.kind:
                return None
            row = parent
        return tuple(reversed(path))

    def alert_closure(
        self, path: tuple[CoverageTerritoryNode, ...], scope: CoverageAlertScope | None
    ) -> frozenset[str] | None:
        if (
            scope is None
            or scope.provider != ALERT_PROVIDER
            or scope.catalog_version != self.version
            or scope.selected_region_id != path[-1].region_id
            or type(scope.applicable_region_ids) is not tuple
            or not 1 <= len(scope.applicable_region_ids) <= 4096
            or any(type(item) is not str for item in scope.applicable_region_ids)
            or len(set(scope.applicable_region_ids)) != len(scope.applicable_region_ids)
        ):
            return None
        expected = {item.region_id for item in path}
        pending = list(self.children.get(path[-1].region_id, ()))
        while pending:
            row = pending.pop()
            if row.region_id in expected or len(expected) >= 4096:
                return None
            parent = self.by_id.get(row.parent_region_id or "")
            if parent is None or _PARENT.get(row.kind) != parent.kind:
                return None
            expected.add(row.region_id)
            pending.extend(self.children.get(row.region_id, ()))
        return frozenset(expected) if expected == set(scope.applicable_region_ids) else None


def derive_active_coverage(
    now: datetime,
    *,
    catalog_version: str | None,
    territories: tuple[CoverageTerritoryNode, ...],
    subscribers: tuple[CoverageSubscriber, ...],
    weather_locations: tuple[CoverageWeatherLocation, ...] = (),
) -> ActiveGeographicCoverage:
    """Idempotent desired work from one consistent database observation.

    Explicit manual power demand remains valid without an alert-territory
    mapping. Nothing infers the power location from the independent air region.
    """
    clock = aware(now)
    catalog = _Catalog(territories, catalog_version)
    users: dict[str, CoverageSubscriber] = {}
    conflicts: set[str] = set()
    for user in subscribers:
        if user.user_key in users and users[user.user_key] != user:
            conflicts.add(user.user_key)
        else:
            users[user.user_key] = user
    reasons: dict[str, set[str]] = defaultdict(set)
    selected: dict[str, list[tuple[CoverageSubscriber, bool, bool, bool]]] = defaultdict(list)
    paths: dict[str, tuple[CoverageTerritoryNode, ...]] = {}
    powers: dict[tuple[PowerProvider, str], list[CoverageSubscriber]] = defaultdict(list)
    points: dict[str, list[CoverageSubscriber]] = defaultdict(list)
    approved: dict[str, CoverageWeatherLocation] = {}
    locations_by_code: dict[str, list[CoverageWeatherLocation]] = defaultdict(list)
    location_counts = Counter(item.location_id for item in weather_locations)
    for point in weather_locations:
        if (
            point.approved()
            and location_counts[point.location_id] == 1
            and point.source is not None
            and aware(point.source.approved_at) <= clock
        ):
            locations_by_code[point.katottg].append(point)
    requested: set[str] = set()
    active: set[str] = set()
    alert_regions: set[str] = set()
    for key, user in users.items():
        if key in conflicts:
            reasons["CONFLICTING_USER_OBSERVATION"].add(key)
            continue
        for issue in user.issues:
            if issue in {"INVALID_POWER_PREFERENCE", "INVALID_WEATHER_PREFERENCE"}:
                reasons[issue].add(key)
        alert_intent = user.alerts_enabled is True and user.mode in {
            "LESSONS_AND_ALERTS",
            "ALERTS_ONLY",
        }
        weather_intent = user.weather_enabled is True or user.forecast_requested is True
        power_intent = user.power is not None
        if not (alert_intent or weather_intent or power_intent):
            continue
        requested.add(key)
        aware(user.changed_at)
        if power_intent:
            assert user.power is not None
            powers[user.power.provider, user.power.group_id].append(user)
            active.add(key)
        path = catalog.path(user.confirmed_region_id)
        if path is None:
            reasons[
                "UNMAPPED_CONFIRMED_TERRITORY" if catalog.valid else "ACTIVE_CATALOG_UNAVAILABLE"
            ].add(key)
            continue
        paths[path[-1].region_id] = path
        closure = catalog.alert_closure(path, user.alert_scope) if alert_intent else None
        if alert_intent and closure is None:
            reasons["UNVERIFIED_ALERT_SCOPE"].add(key)
        alerts = closure is not None
        if closure is not None:
            alert_regions.update(closure)
        if alerts or weather_intent or power_intent:
            selected[path[-1].region_id].append((user, alerts, power_intent, weather_intent))
            active.add(key)
        if weather_intent:
            matches = [
                point
                for point in locations_by_code.get(path[-1].katottg or "", ())
                if point.region_id == path[-1].region_id
            ]
            # Multiple BASE centres cannot be resolved by names or coordinates.
            if sum(point.coverage_type == "BASE" for point in matches) > 1:
                reasons["AMBIGUOUS_WEATHER_BASE_POINT"].add(key)
                matches = [point for point in matches if point.coverage_type != "BASE"]
            if not matches:
                reasons["NO_APPROVED_WEATHER_POINT"].add(key)
            for point in matches:
                approved[point.location_id] = point
                points[point.location_id].append(user)

    def changed(user: CoverageSubscriber) -> datetime:
        return max(
            aware(value)
            for value in (
                user.changed_at,
                user.power_changed_at,
                user.weather_changed_at,
            )
            if value is not None
        )

    territory_demands = []
    for region_id, records in sorted(selected.items()):
        path = paths[region_id]
        territory_demands.append(
            ActiveTerritoryDemand(
                path[-1],
                path[0],
                next((node for node in path if node.kind == "RAION"), None),
                next((node for node in path if node.kind == "HROMADA"), None),
                len(records),
                sum(alert for _, alert, _, _ in records),
                sum(power for _, _, power, _ in records),
                sum(weather for _, _, _, weather in records),
                sum(user.weather_enabled is True for user, _, _, weather in records if weather),
                max(changed(user) for user, _, _, _ in records),
            )
        )
    power_demands = tuple(
        ActivePowerGroupDemand(
            provider,
            group,
            len(records),
            sum(user.power is not None and user.power.notifications_enabled for user in records),
            max(aware(user.power_changed_at or user.changed_at) for user in records),
        )
        for (provider, group), records in sorted(powers.items())
    )
    weather_demands = tuple(
        ActiveWeatherPointDemand(
            approved[identifier],
            len(records),
            sum(user.weather_enabled is True for user in records),
            max(aware(user.weather_changed_at or user.changed_at) for user in records),
        )
        for identifier, records in sorted(points.items())
    )
    return ActiveGeographicCoverage(
        clock,
        catalog_version,
        tuple(territory_demands),
        power_demands,
        weather_demands,
        frozenset(alert_regions),
        tuple(CoverageExclusion(reason, len(keys)) for reason, keys in sorted(reasons.items())),
        len(requested),
        len(active),
    )

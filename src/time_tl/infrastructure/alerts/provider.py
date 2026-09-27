"""Strict alerts.in.ua active-alert snapshot adapter."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from hashlib import sha256

from time_tl.domain.alerts import (
    AlertEpisode,
    OfficialAlertSnapshot,
    ProviderSnapshotKind,
    SourceFailure,
    SourceFailureKind,
)

from .http import AsyncHttpTransport, HttpRequest


def _parse_retry_after(value: str | None, now: datetime) -> float:
    if not value:
        return 60.0
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=UTC)
            return max(0.0, (retry_at - now).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return 60.0


def _parse_http_date(value: str) -> datetime:
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError("invalid HTTP date") from error
    if parsed.tzinfo is None:
        raise ValueError("HTTP date must include a timezone")
    return parsed.astimezone(UTC)


class OperatorCircuitOpen(RuntimeError):
    """401/403 circuit can only be closed by an operator action."""


def _content_bound_update_id(
    prefix: str,
    revision: int,
    kind: ProviderSnapshotKind,
    episodes: tuple[AlertEpisode, ...],
) -> str:
    """Bind replay identity to both fetch attempt and canonical snapshot content.

    The revision still lets consecutive identical complete observations advance
    the all-clear confirmation state machine. The digest prevents a changed
    response from being discarded when a crash committed the snapshot but not
    the poll-state revision and the same revision is therefore reused.
    """
    canonical = json.dumps(
        [
            [episode.location_uid, episode.alert_type, episode.episode_id]
            for episode in sorted(episodes)
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    digest = sha256(canonical).hexdigest()[:24]
    return f"{prefix}:{revision}:{kind.value}:{digest}"


@dataclass(frozen=True)
class FetchResult:
    snapshot: OfficialAlertSnapshot | None = None
    failure: SourceFailure | None = None
    last_modified: str | None = None
    retry_after_seconds: float | None = None
    open_operator_circuit: bool = False

    def __post_init__(self) -> None:
        if (self.snapshot is None) == (self.failure is None):
            raise ValueError("fetch result requires exactly one of snapshot/failure")


class AlertsInUaAdapter:
    """Strict complete-snapshot adapter for the official active-alert list.

    Every row is validated before non-air alerts are filtered out.  The
    byte-pinned location catalogue supplies both the complete coverage set and
    the expected provider location type for each UID.
    """

    ENDPOINT = "https://api.alerts.in.ua/v1/alerts/active.json"
    MAX_RESPONSE_BYTES = 1_048_576

    _LOCATION_TYPES = frozenset({"oblast", "raion", "city", "hromada", "unknown"})
    _ALERT_TYPES = frozenset(
        {"air_raid", "artillery_shelling", "urban_fights", "chemical", "nuclear"}
    )
    _ALERT_LEVELS = frozenset({"red", "yellow"})
    _THREAT_TYPES = frozenset(
        {
            "tactic_aircraft_activity",
            "strategic_aircraft_activity",
            "mig31k_departure",
            "ballistic_missiles",
            "cruise_missiles",
            "unspecified_missiles",
            "drones",
            "guided_aerial_bombs",
            "air_defense",
            "unknown",
        }
    )
    _PINNED_TO_PROVIDER_TYPES = {
        "Область": frozenset({"oblast"}),
        "Місто з спеціальним статусом": frozenset({"city"}),
        "Район": frozenset({"raion"}),
        # alerts.in.ua documents both city and hromada values; city rows in the
        # official UID sheet are published under the broader Громада type.
        "Громада": frozenset({"city", "hromada"}),
    }
    _REQUIRED_ALERT_FIELDS = frozenset(
        {
            "id",
            "location_title",
            "location_type",
            "started_at",
            "finished_at",
            "updated_at",
            "alert_type",
            "location_uid",
            "alert_level",
        }
    )
    _ALLOWED_ALERT_FIELDS = _REQUIRED_ALERT_FIELDS | frozenset(
        {
            "location_title_en",
            "location_oblast",
            "location_oblast_uid",
            "location_raion",
            "notes",
            "threats",
        }
    )
    _REQUIRED_THREAT_FIELDS = frozenset({"threat_type", "level", "started_at"})
    _ALLOWED_THREAT_FIELDS = _REQUIRED_THREAT_FIELDS | frozenset({"source_message"})

    def __init__(
        self,
        *,
        token: str,
        transport: AsyncHttpTransport,
        region_types: Mapping[str, str],
        timeout_seconds: float = 10.0,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not token or not token.strip():
            raise ValueError("alerts.in.ua token is required")
        if not region_types:
            raise ValueError("verified region type registry is required")
        if any(not self._is_canonical_uid(uid) for uid in region_types):
            raise ValueError("region registry UIDs must be canonical positive decimals")
        unknown_types = set(region_types.values()) - set(self._PINNED_TO_PROVIDER_TYPES)
        if unknown_types:
            raise ValueError("region registry contains an unsupported pinned type")
        self._token, self._transport = token, transport
        self._region_types = dict(region_types)
        self._covered_region_ids = frozenset(region_types)
        self._timeout_seconds = timeout_seconds
        self._now = now or (lambda: datetime.now(UTC))
        self._validated_last_modified: str | None = None
        self._validated_last_modified_at: datetime | None = None
        self._validated_content_digest: str | None = None

    async def fetch(self, *, fetch_revision: int, last_modified: str | None) -> FetchResult:
        headers = {"Accept": "application/json", "Authorization": f"Bearer {self._token}"}
        conditional_marker = (
            last_modified
            if last_modified is not None and last_modified == self._validated_last_modified
            else None
        )
        if conditional_marker is not None:
            headers["If-Modified-Since"] = conditional_marker
        try:
            response = await self._transport.send(
                HttpRequest("GET", self.ENDPOINT, headers, self._timeout_seconds)
            )
        except (TimeoutError, OSError):
            return FetchResult(failure=SourceFailure(SourceFailureKind.TIMEOUT))
        normalized = {key.lower(): value for key, value in response.headers.items()}
        if response.status == 304:
            if conditional_marker is None:
                return self._invalid(SourceFailureKind.INVALID_RESPONSE)
            response_marker = normalized.get("last-modified")
            if response_marker is not None:
                try:
                    _parse_http_date(response_marker)
                except ValueError:
                    return self._invalid(SourceFailureKind.INVALID_RESPONSE)
                if response_marker != conditional_marker:
                    return self._invalid(SourceFailureKind.INVALID_RESPONSE)
            return FetchResult(
                snapshot=self._snapshot(fetch_revision, ProviderSnapshotKind.NOT_MODIFIED_304, ()),
                last_modified=conditional_marker,
            )
        if response.status == 200:
            marker = normalized.get("last-modified")
            if not marker:
                return self._invalid(SourceFailureKind.PARTIAL_RESPONSE)
            if not response.body:
                return self._invalid(SourceFailureKind.EMPTY_RESPONSE)
            try:
                marker_at = _parse_http_date(marker)
                persisted_floor = (
                    _parse_http_date(last_modified) if last_modified is not None else None
                )
                episodes, content_digest = self._parse_active_payload(response.body)
            except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
                return self._invalid(SourceFailureKind.INVALID_RESPONSE)
            if (
                self._validated_last_modified_at is not None
                and marker_at < self._validated_last_modified_at
            ) or (persisted_floor is not None and marker_at < persisted_floor):
                return self._invalid(SourceFailureKind.INVALID_RESPONSE)
            if (
                self._validated_last_modified_at is not None
                and marker_at == self._validated_last_modified_at
                and content_digest != self._validated_content_digest
            ):
                return self._invalid(SourceFailureKind.INVALID_RESPONSE)
            self._validated_last_modified = marker
            self._validated_last_modified_at = marker_at
            self._validated_content_digest = content_digest
            return FetchResult(
                snapshot=self._snapshot(fetch_revision, ProviderSnapshotKind.FULL_200, episodes),
                last_modified=marker,
            )
        if response.status in (401, 403):
            return FetchResult(
                failure=SourceFailure(SourceFailureKind.HTTP_ERROR), open_operator_circuit=True
            )
        if response.status == 429:
            return FetchResult(
                failure=SourceFailure(SourceFailureKind.HTTP_ERROR),
                retry_after_seconds=self._retry_after(normalized.get("retry-after")),
            )
        return FetchResult(failure=SourceFailure(SourceFailureKind.HTTP_ERROR))

    def _snapshot(
        self, revision: int, kind: ProviderSnapshotKind, episodes: tuple[AlertEpisode, ...]
    ) -> OfficialAlertSnapshot:
        return OfficialAlertSnapshot(
            update_id=_content_bound_update_id("alerts-in-ua", revision, kind, episodes),
            fetch_revision=revision,
            kind=kind,
            covered_region_ids=self._covered_region_ids,
            active_episodes=episodes,
            fetched_at=self._now(),
            is_complete=True,
            is_fresh=True,
        )

    @staticmethod
    def _invalid(kind: SourceFailureKind) -> FetchResult:
        return FetchResult(failure=SourceFailure(kind))

    def _parse_active_payload(self, body: bytes) -> tuple[tuple[AlertEpisode, ...], str]:
        root = json.loads(body.decode("utf-8"))
        if not isinstance(root, dict) or set(root) != {"alerts"}:
            raise ValueError("root must contain only alerts")
        alerts = root["alerts"]
        if not isinstance(alerts, list):
            raise TypeError("alerts must be a list")
        parsed: list[AlertEpisode] = []
        alert_ids: set[int] = set()
        for item in alerts:
            if not isinstance(item, dict):
                raise TypeError("alert must be an object")
            keys = frozenset(item)
            if not self._REQUIRED_ALERT_FIELDS.issubset(keys):
                raise ValueError("partial alert object")
            if not keys.issubset(self._ALLOWED_ALERT_FIELDS):
                raise ValueError("alert object contains undocumented fields")

            alert_id = item["id"]
            if isinstance(alert_id, bool) or not isinstance(alert_id, int) or alert_id <= 0:
                raise TypeError("id must be a positive integer")
            if alert_id in alert_ids:
                raise ValueError("duplicate alert ID")
            alert_ids.add(alert_id)

            location_uid = item["location_uid"]
            if not isinstance(location_uid, str) or not self._is_canonical_uid(location_uid):
                raise TypeError("location_uid must be a canonical positive decimal string")
            pinned_type = self._region_types.get(location_uid)
            if pinned_type is None:
                raise ValueError("alert references an unknown provider UID")

            location_type = item["location_type"]
            if not isinstance(location_type, str) or location_type not in self._LOCATION_TYPES:
                raise ValueError("unknown location_type")
            if location_type not in self._PINNED_TO_PROVIDER_TYPES[pinned_type]:
                raise ValueError("location_type disagrees with the pinned UID catalogue")

            alert_type = item["alert_type"]
            if not isinstance(alert_type, str) or alert_type not in self._ALERT_TYPES:
                raise ValueError("unknown alert_type")
            alert_level = item["alert_level"]
            if not isinstance(alert_level, str) or alert_level not in self._ALERT_LEVELS:
                raise ValueError("unknown alert_level")

            started_at = self._parse_api_timestamp(item["started_at"], "started_at")
            updated_at = self._parse_api_timestamp(item["updated_at"], "updated_at")
            if updated_at < started_at:
                raise ValueError("updated_at precedes started_at")
            if item["finished_at"] is not None:
                raise ValueError("active alert must not be finished")

            self._validate_required_text(item["location_title"], "location_title")
            for optional_text in (
                "location_title_en",
                "location_oblast",
                "location_raion",
                "notes",
            ):
                if optional_text in item and item[optional_text] is not None:
                    self._validate_text(item[optional_text], optional_text)
            if "location_oblast_uid" in item and item["location_oblast_uid"] is not None:
                oblast_uid = item["location_oblast_uid"]
                if not isinstance(oblast_uid, str) or not self._is_canonical_uid(oblast_uid):
                    raise TypeError("location_oblast_uid must be a canonical decimal string")
                oblast_type = self._region_types.get(oblast_uid)
                if oblast_type not in {"Область", "Місто з спеціальним статусом"}:
                    raise ValueError("location_oblast_uid is not a known top-level UID")
            if "threats" in item:
                self._validate_threats(item["threats"])

            if alert_type == "air_raid":
                # Both documented red and yellow levels represent an active air raid.
                parsed.append(AlertEpisode(location_uid, alert_type, str(alert_id)))
        if len(parsed) != len(set(parsed)):
            raise ValueError("duplicate alert episode")
        canonical = json.dumps(
            root,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return tuple(sorted(parsed)), sha256(canonical).hexdigest()

    @staticmethod
    def _is_canonical_uid(value: object) -> bool:
        return (
            isinstance(value, str)
            and value.isdecimal()
            and int(value) > 0
            and str(int(value)) == value
        )

    @staticmethod
    def _parse_api_timestamp(value: object, field: str) -> datetime:
        if not isinstance(value, str) or not value or value != value.strip():
            raise TypeError(f"{field} must be a non-empty date-time string")
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError as error:
            raise ValueError(f"{field} must be an ISO-8601 date-time") from error
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError(f"{field} must include a timezone")
        return parsed.astimezone(UTC)

    @staticmethod
    def _validate_required_text(value: object, field: str) -> None:
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            raise TypeError(f"{field} must be a non-empty trimmed string")

    @staticmethod
    def _validate_text(value: object, field: str) -> None:
        if not isinstance(value, str) or value != value.strip():
            raise TypeError(f"{field} must be a trimmed string")

    def _validate_threats(self, value: object) -> None:
        if not isinstance(value, list) or not value:
            raise TypeError("threats must be a non-empty list when present")
        for threat in value:
            if not isinstance(threat, dict):
                raise TypeError("threat must be an object")
            keys = frozenset(threat)
            if not self._REQUIRED_THREAT_FIELDS.issubset(keys):
                raise ValueError("partial threat object")
            if not keys.issubset(self._ALLOWED_THREAT_FIELDS):
                raise ValueError("threat object contains undocumented fields")
            if threat["threat_type"] not in self._THREAT_TYPES:
                raise ValueError("unknown threat_type")
            if threat["level"] not in self._ALERT_LEVELS:
                raise ValueError("unknown threat level")
            self._parse_api_timestamp(threat["started_at"], "threat.started_at")
            if "source_message" in threat and threat["source_message"] is not None:
                self._validate_text(threat["source_message"], "threat.source_message")

    def _retry_after(self, value: str | None) -> float:
        return _parse_retry_after(value, self._now())


class FullTerritoryAlertsInUaAdapter:
    """Strict adapter for the official all-territory IoT snapshot.

    Character positions are numeric provider UIDs, not CSV row positions.
    The supplied UIDs must come from the byte-pinned official catalogue.
    A persisted Last-Modified value is never trusted as a 304 baseline after
    restart, but it remains a monotonic floor for the first unconditional 200.
    """

    ENDPOINT = "https://api.alerts.in.ua/v1/iot/active_air_raid_alerts.json"

    def __init__(
        self,
        *,
        token: str,
        transport: AsyncHttpTransport,
        region_uids: tuple[str, ...],
        timeout_seconds: float = 10.0,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not token or not token.strip():
            raise ValueError("alerts.in.ua token is required")
        if not region_uids:
            raise ValueError("verified region_uids registry is required")
        if any(not uid.isdecimal() for uid in region_uids):
            raise ValueError("region_uids must contain decimal provider UIDs")
        numeric_uids = tuple(int(uid) for uid in region_uids)
        if len(set(numeric_uids)) != len(numeric_uids):
            raise ValueError("region_uids must be unique provider UIDs")

        self._token, self._transport = token, transport
        self._region_uid_by_index = {int(uid): uid for uid in region_uids}
        self._covered_region_ids = frozenset(region_uids)
        self._expected_length = max(numeric_uids) + 1
        self._timeout_seconds = timeout_seconds
        self._now = now or (lambda: datetime.now(UTC))
        self._validated_last_modified: str | None = None
        self._validated_last_modified_at: datetime | None = None
        self._validated_content_digest: str | None = None

    async def fetch(self, *, fetch_revision: int, last_modified: str | None) -> FetchResult:
        headers = {"Accept": "application/json", "Authorization": f"Bearer {self._token}"}
        conditional_marker = (
            last_modified
            if last_modified is not None and last_modified == self._validated_last_modified
            else None
        )
        if conditional_marker is not None:
            headers["If-Modified-Since"] = conditional_marker
        try:
            response = await self._transport.send(
                HttpRequest("GET", self.ENDPOINT, headers, self._timeout_seconds)
            )
        except (TimeoutError, OSError):
            return FetchResult(failure=SourceFailure(SourceFailureKind.TIMEOUT))

        normalized = {key.lower(): value for key, value in response.headers.items()}
        if response.status == 304:
            if conditional_marker is None:
                return self._invalid(SourceFailureKind.INVALID_RESPONSE)
            response_marker = normalized.get("last-modified")
            if response_marker is not None:
                try:
                    _parse_http_date(response_marker)
                except ValueError:
                    return self._invalid(SourceFailureKind.INVALID_RESPONSE)
                if response_marker != conditional_marker:
                    return self._invalid(SourceFailureKind.INVALID_RESPONSE)
            return FetchResult(
                snapshot=self._snapshot(fetch_revision, ProviderSnapshotKind.NOT_MODIFIED_304, ()),
                last_modified=conditional_marker,
            )
        if response.status == 200:
            marker = normalized.get("last-modified")
            if not marker:
                return self._invalid(SourceFailureKind.PARTIAL_RESPONSE)
            try:
                marker_at = _parse_http_date(marker)
                persisted_floor = (
                    _parse_http_date(last_modified) if last_modified is not None else None
                )
                episodes = self._parse_payload(response.body)
            except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
                return self._invalid(SourceFailureKind.INVALID_RESPONSE)
            if (
                self._validated_last_modified_at is not None
                and marker_at < self._validated_last_modified_at
            ) or (persisted_floor is not None and marker_at < persisted_floor):
                return self._invalid(SourceFailureKind.INVALID_RESPONSE)
            content_digest = self._episodes_digest(episodes)
            if (
                self._validated_last_modified_at is not None
                and marker_at == self._validated_last_modified_at
                and self._validated_content_digest != content_digest
            ):
                return self._invalid(SourceFailureKind.INVALID_RESPONSE)
            self._validated_last_modified = marker
            self._validated_last_modified_at = marker_at
            self._validated_content_digest = content_digest
            return FetchResult(
                snapshot=self._snapshot(fetch_revision, ProviderSnapshotKind.FULL_200, episodes),
                last_modified=marker,
            )
        if response.status in (401, 403):
            return FetchResult(
                failure=SourceFailure(SourceFailureKind.HTTP_ERROR), open_operator_circuit=True
            )
        if response.status == 429:
            return FetchResult(
                failure=SourceFailure(SourceFailureKind.HTTP_ERROR),
                retry_after_seconds=self._retry_after(normalized.get("retry-after")),
            )
        return FetchResult(failure=SourceFailure(SourceFailureKind.HTTP_ERROR))

    def _snapshot(
        self, revision: int, kind: ProviderSnapshotKind, episodes: tuple[AlertEpisode, ...]
    ) -> OfficialAlertSnapshot:
        return OfficialAlertSnapshot(
            update_id=_content_bound_update_id("alerts-in-ua-full-iot", revision, kind, episodes),
            fetch_revision=revision,
            kind=kind,
            covered_region_ids=self._covered_region_ids,
            active_episodes=episodes,
            fetched_at=self._now(),
            is_complete=True,
            is_fresh=True,
        )

    def _parse_payload(self, body: bytes) -> tuple[AlertEpisode, ...]:
        if not body:
            raise ValueError("empty response")
        statuses = json.loads(body.decode("utf-8"))
        if not isinstance(statuses, str):
            raise TypeError("full territory snapshot must be a JSON string")
        if len(statuses) != self._expected_length:
            raise ValueError("full territory snapshot length differs from the pinned UID range")
        if any(status not in {" ", "A", "P", "N"} for status in statuses):
            raise ValueError("full territory snapshot contains an unknown status")

        episodes: list[AlertEpisode] = []
        for index, status in enumerate(statuses):
            uid = self._region_uid_by_index.get(index)
            if uid is None:
                if status != " ":
                    raise ValueError("unknown UID position does not contain the space sentinel")
                continue
            if status == " ":
                raise ValueError("known UID position contains the space sentinel")
            if status in {"A", "P"}:
                alert_type = "air_raid" if status == "A" else "air_raid_partial"
                episodes.append(AlertEpisode(uid, alert_type, f"air-raid:{uid}"))
        return tuple(episodes)

    @staticmethod
    def _episodes_digest(episodes: tuple[AlertEpisode, ...]) -> str:
        canonical = json.dumps(
            [
                [episode.location_uid, episode.alert_type, episode.episode_id]
                for episode in episodes
            ],
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("ascii")
        return sha256(canonical).hexdigest()

    @staticmethod
    def _invalid(kind: SourceFailureKind) -> FetchResult:
        return FetchResult(failure=SourceFailure(kind))

    def _retry_after(self, value: str | None) -> float:
        return _parse_retry_after(value, self._now())


class OblastAlertsInUaAdapter:
    """Strict adapter for the official 27-character oblast IoT snapshot.

    The endpoint already folds raion/community alerts into the oblast status:
    both ``A`` (whole oblast) and ``P`` (partial) therefore mean ACTIVE for
    that oblast.  A stable synthetic episode identity deliberately ignores the
    A/P distinction so a partial-to-whole transition cannot emit a second
    alert-start event.

    Conditional requests are used only after this adapter instance has parsed
    a complete 200 response.  After a process restart the first request is
    unconditional, even when the repository has a persisted Last-Modified
    value.  This prevents an otherwise unverifiable 304 from becoming a clear
    or active baseline.
    """

    ENDPOINT = "https://api.alerts.in.ua/v1/iot/active_air_raid_alerts_by_oblast.json"
    OBLAST_UIDS = (
        "29",  # Autonomous Republic of Crimea
        "8",  # Volyn
        "4",  # Vinnytsia
        "9",  # Dnipropetrovsk
        "28",  # Donetsk
        "10",  # Zhytomyr
        "11",  # Zakarpattia
        "12",  # Zaporizhzhia
        "13",  # Ivano-Frankivsk
        "31",  # Kyiv city
        "14",  # Kyiv oblast
        "15",  # Kirovohrad
        "16",  # Luhansk
        "27",  # Lviv
        "17",  # Mykolaiv
        "18",  # Odesa
        "19",  # Poltava
        "5",  # Rivne
        "30",  # Sevastopol
        "20",  # Sumy
        "21",  # Ternopil
        "22",  # Kharkiv
        "23",  # Kherson
        "3",  # Khmelnytskyi
        "24",  # Cherkasy
        "26",  # Chernivtsi
        "25",  # Chernihiv
    )
    COVERED_REGION_IDS = frozenset(OBLAST_UIDS)

    def __init__(
        self,
        *,
        token: str,
        transport: AsyncHttpTransport,
        timeout_seconds: float = 10.0,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not token or not token.strip():
            raise ValueError("alerts.in.ua token is required")
        self._token, self._transport = token, transport
        self._timeout_seconds = timeout_seconds
        self._now = now or (lambda: datetime.now(UTC))
        self._validated_last_modified: str | None = None

    async def fetch(self, *, fetch_revision: int, last_modified: str | None) -> FetchResult:
        headers = {"Accept": "application/json", "Authorization": f"Bearer {self._token}"}
        conditional_marker = (
            last_modified
            if last_modified is not None and last_modified == self._validated_last_modified
            else None
        )
        if conditional_marker is not None:
            headers["If-Modified-Since"] = conditional_marker
        try:
            response = await self._transport.send(
                HttpRequest("GET", self.ENDPOINT, headers, self._timeout_seconds)
            )
        except (TimeoutError, OSError):
            return FetchResult(failure=SourceFailure(SourceFailureKind.TIMEOUT))

        normalized = {key.lower(): value for key, value in response.headers.items()}
        if response.status == 304:
            if conditional_marker is None:
                return self._invalid(SourceFailureKind.INVALID_RESPONSE)
            return FetchResult(
                snapshot=self._snapshot(fetch_revision, ProviderSnapshotKind.NOT_MODIFIED_304, ()),
                last_modified=normalized.get("last-modified", conditional_marker),
            )
        if response.status == 200:
            marker = normalized.get("last-modified")
            if not marker:
                return self._invalid(SourceFailureKind.PARTIAL_RESPONSE)
            try:
                episodes = self._parse_oblast_payload(response.body)
            except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
                return self._invalid(SourceFailureKind.INVALID_RESPONSE)
            self._validated_last_modified = marker
            return FetchResult(
                snapshot=self._snapshot(fetch_revision, ProviderSnapshotKind.FULL_200, episodes),
                last_modified=marker,
            )
        if response.status in (401, 403):
            return FetchResult(
                failure=SourceFailure(SourceFailureKind.HTTP_ERROR), open_operator_circuit=True
            )
        if response.status == 429:
            return FetchResult(
                failure=SourceFailure(SourceFailureKind.HTTP_ERROR),
                retry_after_seconds=self._retry_after(normalized.get("retry-after")),
            )
        return FetchResult(failure=SourceFailure(SourceFailureKind.HTTP_ERROR))

    def _snapshot(
        self, revision: int, kind: ProviderSnapshotKind, episodes: tuple[AlertEpisode, ...]
    ) -> OfficialAlertSnapshot:
        return OfficialAlertSnapshot(
            update_id=_content_bound_update_id("alerts-in-ua-oblast-iot", revision, kind, episodes),
            fetch_revision=revision,
            kind=kind,
            covered_region_ids=self.COVERED_REGION_IDS,
            active_episodes=episodes,
            fetched_at=self._now(),
            is_complete=True,
            is_fresh=True,
        )

    @classmethod
    def _parse_oblast_payload(cls, body: bytes) -> tuple[AlertEpisode, ...]:
        if not body:
            raise ValueError("empty response")
        statuses = json.loads(body.decode("utf-8"))
        if not isinstance(statuses, str):
            raise TypeError("oblast snapshot must be a JSON string")
        if len(statuses) != len(cls.OBLAST_UIDS):
            raise ValueError("oblast snapshot must contain exactly 27 statuses")
        if any(status not in {"A", "P", "N"} for status in statuses):
            raise ValueError("oblast snapshot contains an unknown status")
        return tuple(
            AlertEpisode(uid, "air_raid", f"oblast-air-raid:{uid}")
            for uid, status in zip(cls.OBLAST_UIDS, statuses, strict=True)
            if status in {"A", "P"}
        )

    @staticmethod
    def _invalid(kind: SourceFailureKind) -> FetchResult:
        return FetchResult(failure=SourceFailure(kind))

    def _retry_after(self, value: str | None) -> float:
        return _parse_retry_after(value, self._now())

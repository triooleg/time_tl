"""Strict alerts.in.ua active-alert snapshot adapter."""

from __future__ import annotations

import json
from collections.abc import Callable
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
    ENDPOINT = "https://api.alerts.in.ua/v1/alerts/active.json"

    def __init__(
        self,
        *,
        token: str,
        transport: AsyncHttpTransport,
        covered_region_ids: frozenset[str],
        timeout_seconds: float = 10.0,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not token or not token.strip():
            raise ValueError("alerts.in.ua token is required")
        if not covered_region_ids:
            raise ValueError("verified covered_region_ids registry is required")
        self._token, self._transport = token, transport
        self._covered_region_ids, self._timeout_seconds = covered_region_ids, timeout_seconds
        self._now = now or (lambda: datetime.now(UTC))

    async def fetch(self, *, fetch_revision: int, last_modified: str | None) -> FetchResult:
        headers = {"Accept": "application/json", "Authorization": f"Bearer {self._token}"}
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        try:
            response = await self._transport.send(
                HttpRequest("GET", self.ENDPOINT, headers, self._timeout_seconds)
            )
        except (TimeoutError, OSError):
            return FetchResult(failure=SourceFailure(SourceFailureKind.TIMEOUT))
        normalized = {key.lower(): value for key, value in response.headers.items()}
        if response.status == 304:
            if last_modified is None:
                return self._invalid(SourceFailureKind.INVALID_RESPONSE)
            return FetchResult(
                snapshot=self._snapshot(fetch_revision, ProviderSnapshotKind.NOT_MODIFIED_304, ()),
                last_modified=normalized.get("last-modified", last_modified),
            )
        if response.status == 200:
            marker = normalized.get("last-modified")
            if not marker:
                return self._invalid(SourceFailureKind.PARTIAL_RESPONSE)
            try:
                episodes = self._parse_active_payload(response.body)
            except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
                return self._invalid(SourceFailureKind.INVALID_RESPONSE)
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

    @staticmethod
    def _parse_active_payload(body: bytes) -> tuple[AlertEpisode, ...]:
        if not body:
            raise ValueError("empty response")
        root = json.loads(body.decode("utf-8"))
        if not isinstance(root, dict) or set(root) != {"alerts"}:
            raise ValueError("root must contain only alerts")
        alerts = root["alerts"]
        if not isinstance(alerts, list):
            raise TypeError("alerts must be a list")
        parsed: list[AlertEpisode] = []
        for item in alerts:
            if not isinstance(item, dict):
                raise TypeError("alert must be an object")
            required = ("id", "location_uid", "alert_type", "started_at", "finished_at")
            if any(key not in item for key in required):
                raise ValueError("partial alert object")
            alert_id, location_uid, alert_type = (
                item["id"],
                item["location_uid"],
                item["alert_type"],
            )
            if isinstance(alert_id, bool) or not isinstance(alert_id, (int, str)):
                raise TypeError("id must be int or string")
            if not isinstance(location_uid, str) or not location_uid.strip():
                raise TypeError("location_uid must be non-empty string")
            if not isinstance(alert_type, str) or not alert_type.strip():
                raise TypeError("alert_type must be non-empty string")
            if not isinstance(item["started_at"], str) or not item["started_at"]:
                raise TypeError("started_at must be non-empty string")
            if item["finished_at"] is not None:
                raise ValueError("active alert must not be finished")
            parsed.append(AlertEpisode(location_uid, alert_type, str(alert_id)))
        if len(parsed) != len(set(parsed)):
            raise ValueError("duplicate alert episode")
        return tuple(sorted(parsed))

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
            update_id=_content_bound_update_id(
                "alerts-in-ua-oblast-iot", revision, kind, episodes
            ),
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

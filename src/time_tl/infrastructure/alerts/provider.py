"""Strict alerts.in.ua active-alert snapshot adapter."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Iterable, Mapping
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


class SnapshotValidationError(ValueError):
    """Internal redacted validation result; never carries provider values."""

    def __init__(self, code: str, count: int = 1) -> None:
        if (
            not code
            or len(code) > 64
            or any(character not in "abcdefghijklmnopqrstuvwxyz_" for character in code)
        ):
            raise ValueError("diagnostic code must be lowercase ASCII with underscores")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError("diagnostic count must be non-negative")
        super().__init__(code)
        self.code = code
        self.count = count


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
    diagnostic_code: str | None = None
    diagnostic_count: int | None = None

    def __post_init__(self) -> None:
        if (self.snapshot is None) == (self.failure is None):
            raise ValueError("fetch result requires exactly one of snapshot/failure")
        if self.snapshot is not None and (
            self.diagnostic_code is not None or self.diagnostic_count is not None
        ):
            raise ValueError("successful fetch cannot carry a failure diagnostic")
        if (self.diagnostic_code is None) != (self.diagnostic_count is None):
            raise ValueError("diagnostic code and count must be provided together")
        if self.diagnostic_code is not None:
            SnapshotValidationError(self.diagnostic_code, self.diagnostic_count or 0)

    def sanitized_failure(self) -> str | None:
        """Return only bounded taxonomy metadata, never provider content."""

        if self.failure is None:
            return None
        summary = f"kind={self.failure.kind.value}"
        if self.diagnostic_code is not None:
            summary += f" code={self.diagnostic_code} count={self.diagnostic_count}"
        return summary


class AlertsInUaAdapter:
    """Strict complete-snapshot adapter for the official active-alert list.

    Every row is validated before non-air alerts are filtered out.  The
    byte-pinned location catalogue supplies both the complete coverage set and
    the expected provider location type for each UID.
    """

    ENDPOINT = "https://api.alerts.in.ua/v1/alerts/active.json"
    MAX_RESPONSE_BYTES = 1_048_576
    MAX_ALERT_ID = (1 << 63) - 1
    MAX_DISCLAIMER_CHARS = 4_096
    MAX_META_EXTRA_FIELDS = 8
    MAX_META_EXTRA_DEPTH = 3
    MAX_META_EXTRA_CONTAINER_ELEMENTS = 32
    MAX_META_EXTRA_NODES = 128
    MAX_META_EXTRA_STRING_CHARS = 2_048
    MAX_META_EXTRA_SERIALIZED_BYTES = 32_768
    MAX_ALERT_EXTRA_FIELDS = 1
    MAX_ALERT_EXTRA_SERIALIZED_BYTES = 32_768

    _BARE_ROOT_FIELDS = frozenset({"alerts"})
    _ENVELOPE_ROOT_FIELDS = frozenset({"alerts", "meta", "disclaimer"})
    _META_FIELDS = frozenset({"last_updated_at"})
    _META_TIMESTAMP_FORMAT = "%Y/%m/%d %H:%M:%S %z"
    _META_TIMESTAMP_PATTERN = re.compile(
        r"\A\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2} [+-]\d{4}\Z"
    )
    _META_EXTRA_KEY_PATTERN = re.compile(
        r"\A[a-z][a-z0-9]*(?:_[a-z0-9]+)*\Z",
        re.ASCII,
    )
    _META_REQUIRED_KEY_SIGNATURES = frozenset(
        key.replace("_", "") for key in _META_FIELDS
    )

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
        expected_oblast_uids: Mapping[str, str],
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
        if set(expected_oblast_uids) != set(region_types):
            raise ValueError("expected oblast registry must cover every region UID exactly")
        if any(
            not self._is_canonical_uid(oblast_uid)
            for oblast_uid in expected_oblast_uids.values()
        ):
            raise ValueError("expected oblast UIDs must be canonical positive decimals")
        top_level_types = {"Область", "Місто з спеціальним статусом"}
        for region_uid, oblast_uid in expected_oblast_uids.items():
            if region_types.get(oblast_uid) not in top_level_types:
                raise ValueError("expected oblast UID must reference a top-level region")
            if region_types[region_uid] in top_level_types and region_uid != oblast_uid:
                raise ValueError("top-level region must map to itself as expected oblast")
        self._token, self._transport = token, transport
        self._region_types = dict(region_types)
        self._expected_oblast_uids = dict(expected_oblast_uids)
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
                return self._invalid(
                    SourceFailureKind.INVALID_RESPONSE,
                    "not_modified_without_baseline",
                )
            response_marker = normalized.get("last-modified")
            if response_marker is not None:
                try:
                    _parse_http_date(response_marker)
                except ValueError:
                    return self._invalid(
                        SourceFailureKind.INVALID_RESPONSE,
                        "not_modified_marker_invalid",
                    )
                if response_marker != conditional_marker:
                    return self._invalid(
                        SourceFailureKind.INVALID_RESPONSE,
                        "not_modified_marker_mismatch",
                    )
            return FetchResult(
                snapshot=self._snapshot(fetch_revision, ProviderSnapshotKind.NOT_MODIFIED_304, ()),
                last_modified=conditional_marker,
            )
        if response.status == 200:
            marker = normalized.get("last-modified")
            if not marker:
                return self._invalid(
                    SourceFailureKind.PARTIAL_RESPONSE,
                    "last_modified_missing",
                )
            if not response.body:
                return self._invalid(SourceFailureKind.EMPTY_RESPONSE, "body_empty", 0)
            try:
                marker_at = _parse_http_date(marker)
            except ValueError:
                return self._invalid(
                    SourceFailureKind.INVALID_RESPONSE,
                    "last_modified_invalid",
                )
            try:
                persisted_floor = (
                    _parse_http_date(last_modified) if last_modified is not None else None
                )
            except ValueError:
                return self._invalid(
                    SourceFailureKind.INVALID_RESPONSE,
                    "persisted_marker_invalid",
                )
            try:
                episodes, content_digest = self._parse_active_payload(response.body)
            except SnapshotValidationError as error:
                return self._invalid(
                    SourceFailureKind.INVALID_RESPONSE,
                    error.code,
                    error.count,
                )
            except (
                UnicodeDecodeError,
                json.JSONDecodeError,
                TypeError,
                ValueError,
                RecursionError,
            ):
                return self._invalid(
                    SourceFailureKind.INVALID_RESPONSE,
                    "payload_unclassified",
                )
            if (
                self._validated_last_modified_at is not None
                and marker_at < self._validated_last_modified_at
            ) or (persisted_floor is not None and marker_at < persisted_floor):
                return self._invalid(
                    SourceFailureKind.INVALID_RESPONSE,
                    "last_modified_regression",
                )
            if (
                self._validated_last_modified_at is not None
                and marker_at == self._validated_last_modified_at
                and content_digest != self._validated_content_digest
            ):
                return self._invalid(
                    SourceFailureKind.INVALID_RESPONSE,
                    "last_modified_content_mismatch",
                )
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
    def _invalid(
        kind: SourceFailureKind,
        diagnostic_code: str,
        diagnostic_count: int = 1,
    ) -> FetchResult:
        return FetchResult(
            failure=SourceFailure(kind),
            diagnostic_code=diagnostic_code,
            diagnostic_count=diagnostic_count,
        )

    def _parse_active_payload(self, body: bytes) -> tuple[tuple[AlertEpisode, ...], str]:
        try:
            decoded = body.decode("utf-8")
        except UnicodeDecodeError as error:
            raise SnapshotValidationError("body_encoding_invalid") from error
        try:
            root = json.loads(decoded, object_pairs_hook=self._strict_json_object)
        except json.JSONDecodeError as error:
            raise SnapshotValidationError("json_invalid") from error
        except RecursionError as error:
            raise SnapshotValidationError("json_depth_unprocessable") from error
        if not isinstance(root, dict):
            raise SnapshotValidationError("root_not_object")
        root_keys = frozenset(root)
        if "alerts" not in root_keys:
            raise SnapshotValidationError("root_fields_missing")
        unknown_root_fields = root_keys - self._ENVELOPE_ROOT_FIELDS
        if unknown_root_fields:
            raise SnapshotValidationError(
                "root_fields_unknown",
                len(unknown_root_fields),
            )
        if root_keys not in {self._BARE_ROOT_FIELDS, self._ENVELOPE_ROOT_FIELDS}:
            raise SnapshotValidationError(
                "root_fields_missing",
                len(self._ENVELOPE_ROOT_FIELDS - root_keys),
            )
        if root_keys == self._ENVELOPE_ROOT_FIELDS:
            self._validate_envelope_metadata(root["meta"], root["disclaimer"])
        alerts = root["alerts"]
        if not isinstance(alerts, list):
            raise SnapshotValidationError("alerts_field_type_invalid")
        parsed: list[AlertEpisode] = []
        canonical_alerts: list[dict[str, object]] = []
        alert_ids: set[int] = set()
        ignored_alert_extra_key: str | None = None
        for item in alerts:
            if not isinstance(item, dict):
                raise SnapshotValidationError("alert_row_type_invalid")
            keys = frozenset(item)
            if not self._REQUIRED_ALERT_FIELDS.issubset(keys):
                raise SnapshotValidationError(
                    "alert_fields_missing",
                    len(self._REQUIRED_ALERT_FIELDS - keys),
                )
            unknown_fields = keys - self._ALLOWED_ALERT_FIELDS
            if unknown_fields:
                extra_key = self._validate_ignored_alert_extras(item, unknown_fields)
                if ignored_alert_extra_key is None:
                    ignored_alert_extra_key = extra_key
                elif extra_key != ignored_alert_extra_key:
                    raise SnapshotValidationError("alert_extra_key_inconsistent")

            alert_id = item["id"]
            if (
                isinstance(alert_id, bool)
                or not isinstance(alert_id, int)
                or not 1 <= alert_id <= self.MAX_ALERT_ID
            ):
                raise SnapshotValidationError("alert_id_invalid")
            if alert_id in alert_ids:
                raise SnapshotValidationError("alert_id_duplicate")
            alert_ids.add(alert_id)

            location_uid = item["location_uid"]
            if not isinstance(location_uid, str) or not self._is_canonical_uid(location_uid):
                raise SnapshotValidationError("location_uid_format_invalid")
            pinned_type = self._region_types.get(location_uid)
            if pinned_type is None:
                raise SnapshotValidationError("location_uid_unknown")

            location_type = item["location_type"]
            if not isinstance(location_type, str) or location_type not in self._LOCATION_TYPES:
                raise SnapshotValidationError("location_type_enum_invalid")
            if location_type not in self._PINNED_TO_PROVIDER_TYPES[pinned_type]:
                raise SnapshotValidationError("location_type_catalogue_mismatch")

            alert_type = item["alert_type"]
            if not isinstance(alert_type, str) or alert_type not in self._ALERT_TYPES:
                raise SnapshotValidationError("alert_type_enum_invalid")
            alert_level = item["alert_level"]
            if not isinstance(alert_level, str) or alert_level not in self._ALERT_LEVELS:
                raise SnapshotValidationError("alert_level_enum_invalid")

            started_at = self._parse_api_timestamp(item["started_at"], "started_at")
            updated_at = self._parse_api_timestamp(item["updated_at"], "updated_at")
            if updated_at < started_at:
                raise SnapshotValidationError("timestamp_order_invalid")
            if item["finished_at"] is not None:
                raise SnapshotValidationError("finished_at_not_null")

            self._validate_required_text(item["location_title"], "location_title")
            for optional_text in (
                "location_title_en",
                "location_oblast",
                "location_raion",
                "notes",
            ):
                if optional_text in item and item[optional_text] is not None:
                    self._validate_text(item[optional_text], optional_text)
            normalized_oblast_uid: str | None = None
            if "location_oblast_uid" in item and item["location_oblast_uid"] is not None:
                oblast_value = item["location_oblast_uid"]
                if isinstance(oblast_value, bool):
                    raise SnapshotValidationError("location_oblast_uid_boolean")
                if isinstance(oblast_value, int):
                    if oblast_value <= 0:
                        raise SnapshotValidationError("location_oblast_uid_integer")
                    oblast_uid = str(oblast_value)
                elif isinstance(oblast_value, str):
                    if not self._is_canonical_uid(oblast_value):
                        raise SnapshotValidationError(
                            "location_oblast_uid_noncanonical_string"
                        )
                    oblast_uid = oblast_value
                else:
                    raise SnapshotValidationError("location_oblast_uid_other")
                oblast_type = self._region_types.get(oblast_uid)
                if oblast_type not in {"Область", "Місто з спеціальним статусом"}:
                    raise SnapshotValidationError("location_oblast_uid_unknown")
                if oblast_uid != self._expected_oblast_uids[location_uid]:
                    raise SnapshotValidationError("location_oblast_uid_hierarchy_mismatch")
                normalized_oblast_uid = oblast_uid
            if "threats" in item:
                self._validate_threats(item["threats"])

            if alert_type == "air_raid":
                # Both documented red and yellow levels represent an active air raid.
                parsed.append(AlertEpisode(location_uid, alert_type, str(alert_id)))
            canonical_alert = {
                key: item[key] for key in self._ALLOWED_ALERT_FIELDS if key in item
            }
            if normalized_oblast_uid is not None:
                canonical_alert["location_oblast_uid"] = normalized_oblast_uid
            canonical_alerts.append(canonical_alert)
        if len(parsed) != len(set(parsed)):
            raise SnapshotValidationError("alert_episode_duplicate")
        canonical_root = dict(root)
        canonical_root["alerts"] = canonical_alerts
        canonical = json.dumps(
            canonical_root,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return tuple(sorted(parsed)), sha256(canonical).hexdigest()

    @classmethod
    def _validate_ignored_alert_extras(
        cls,
        item: Mapping[str, object],
        extra_keys: frozenset[str],
    ) -> str:
        """Bound undocumented row data, then discard it without interpretation.

        These fields are deliberately excluded from the canonical digest and
        never leave this validation frame. Only generic structural limits are
        enforced so provider-controlled names and values cannot enter state,
        persistence, or diagnostics.
        """

        if len(extra_keys) > cls.MAX_ALERT_EXTRA_FIELDS:
            raise SnapshotValidationError("alert_extra_fields_limit", len(extra_keys))
        invalid_key_count = sum(len(key) > 64 for key in extra_keys)
        if invalid_key_count:
            raise SnapshotValidationError("alert_extra_key_invalid", invalid_key_count)

        extra_nodes = [0]
        try:
            for key in sorted(extra_keys):
                cls._validate_meta_extra_value(
                    item[key],
                    container_depth=0,
                    nodes=extra_nodes,
                )
            encoded = json.dumps(
                {key: item[key] for key in extra_keys},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except SnapshotValidationError as error:
            raise SnapshotValidationError("alert_extra_value_invalid") from error
        if len(encoded) > cls.MAX_ALERT_EXTRA_SERIALIZED_BYTES:
            raise SnapshotValidationError("alert_extra_serialized_size")
        return next(iter(extra_keys))

    @staticmethod
    def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        parsed: dict[str, object] = {}
        duplicate_count = 0
        for key, value in pairs:
            if key in parsed:
                duplicate_count += 1
            else:
                parsed[key] = value
        if duplicate_count:
            raise SnapshotValidationError("json_object_keys_duplicate", duplicate_count)
        return parsed

    @classmethod
    def _validate_envelope_metadata(cls, meta: object, disclaimer: object) -> None:
        """Validate the exact envelope consumed by the official Python SDK.

        Metadata is deliberately not returned to the alert state machine. It is
        validation and content-digest input only, and can never synthesize an
        ACTIVE or CLEAR observation.
        """

        if not isinstance(meta, dict):
            raise SnapshotValidationError("meta_type_invalid")
        meta_keys = frozenset(meta)
        if not cls._META_FIELDS.issubset(meta_keys):
            raise SnapshotValidationError(
                "meta_fields_missing",
                len(cls._META_FIELDS - meta_keys),
            )
        extra_keys = meta_keys - cls._META_FIELDS
        if len(extra_keys) > cls.MAX_META_EXTRA_FIELDS:
            raise SnapshotValidationError(
                "meta_extra_fields_limit",
                len(extra_keys),
            )
        invalid_key_count = sum(
            len(key) > 64 or cls._META_EXTRA_KEY_PATTERN.fullmatch(key) is None
            for key in extra_keys
        )
        if invalid_key_count:
            raise SnapshotValidationError("meta_extra_key_invalid", invalid_key_count)
        conflicting_key_count = sum(
            key.replace("_", "") in cls._META_REQUIRED_KEY_SIGNATURES
            for key in extra_keys
        )
        if conflicting_key_count:
            raise SnapshotValidationError("meta_extra_key_conflict", conflicting_key_count)
        last_updated_at = meta["last_updated_at"]
        if not isinstance(last_updated_at, str):
            raise SnapshotValidationError("meta_last_updated_at_type_invalid")
        if cls._META_TIMESTAMP_PATTERN.fullmatch(last_updated_at) is None:
            raise SnapshotValidationError("meta_last_updated_at_format_invalid")
        try:
            parsed = datetime.strptime(last_updated_at, cls._META_TIMESTAMP_FORMAT)
        except ValueError as error:
            raise SnapshotValidationError("meta_last_updated_at_format_invalid") from error
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise SnapshotValidationError("meta_last_updated_at_timezone_missing")

        extra_nodes = [0]
        for key in sorted(extra_keys):
            cls._validate_meta_extra_value(meta[key], container_depth=0, nodes=extra_nodes)
        canonical_extras = json.dumps(
            {key: meta[key] for key in extra_keys},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if len(canonical_extras) > cls.MAX_META_EXTRA_SERIALIZED_BYTES:
            raise SnapshotValidationError("meta_extra_serialized_size")

        if not isinstance(disclaimer, str):
            raise SnapshotValidationError("disclaimer_type_invalid")
        if not 1 <= len(disclaimer) <= cls.MAX_DISCLAIMER_CHARS:
            raise SnapshotValidationError("disclaimer_length_invalid")

    @classmethod
    def _validate_meta_extra_value(
        cls,
        value: object,
        *,
        container_depth: int,
        nodes: list[int],
    ) -> None:
        nodes[0] += 1
        if nodes[0] > cls.MAX_META_EXTRA_NODES:
            raise SnapshotValidationError("meta_extra_nodes_limit")

        if value is None or isinstance(value, bool):
            return
        if isinstance(value, int):
            if not -(1 << 63) <= value <= (1 << 63) - 1:
                raise SnapshotValidationError("meta_extra_integer_range")
            return
        if isinstance(value, float):
            if not math.isfinite(value):
                raise SnapshotValidationError("meta_extra_float_non_finite")
            return
        if isinstance(value, str):
            if len(value) > cls.MAX_META_EXTRA_STRING_CHARS:
                raise SnapshotValidationError("meta_extra_string_length")
            return
        if isinstance(value, (list, dict)):
            next_depth = container_depth + 1
            if next_depth > cls.MAX_META_EXTRA_DEPTH:
                raise SnapshotValidationError("meta_extra_depth_limit")
            if len(value) > cls.MAX_META_EXTRA_CONTAINER_ELEMENTS:
                raise SnapshotValidationError("meta_extra_container_limit")
            children: Iterable[object]
            if isinstance(value, list):
                children = value
            else:
                for key in value:
                    if not isinstance(key, str):
                        raise SnapshotValidationError("meta_extra_value_type_invalid")
                    if len(key) > cls.MAX_META_EXTRA_STRING_CHARS:
                        raise SnapshotValidationError("meta_extra_string_length")
                children = value.values()
            for child in children:
                cls._validate_meta_extra_value(
                    child,
                    container_depth=next_depth,
                    nodes=nodes,
                )
            return
        raise SnapshotValidationError("meta_extra_value_type_invalid")

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
        code_prefix = field.replace(".", "_")
        if not isinstance(value, str) or not value or value != value.strip():
            raise SnapshotValidationError(f"{code_prefix}_type_invalid")
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError as error:
            raise SnapshotValidationError(f"{code_prefix}_format_invalid") from error
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise SnapshotValidationError(f"{code_prefix}_timezone_missing")
        return parsed.astimezone(UTC)

    @staticmethod
    def _validate_required_text(value: object, field: str) -> None:
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            raise SnapshotValidationError(f"{field}_invalid")

    @staticmethod
    def _validate_text(value: object, field: str) -> None:
        if not isinstance(value, str) or value != value.strip():
            raise SnapshotValidationError(f"{field}_invalid")

    def _validate_threats(self, value: object) -> None:
        if not isinstance(value, list) or not value:
            raise SnapshotValidationError("threats_field_invalid")
        for threat in value:
            if not isinstance(threat, dict):
                raise SnapshotValidationError("threat_row_type_invalid")
            keys = frozenset(threat)
            if not self._REQUIRED_THREAT_FIELDS.issubset(keys):
                raise SnapshotValidationError(
                    "threat_fields_missing",
                    len(self._REQUIRED_THREAT_FIELDS - keys),
                )
            if not keys.issubset(self._ALLOWED_THREAT_FIELDS):
                raise SnapshotValidationError(
                    "threat_fields_unknown",
                    len(keys - self._ALLOWED_THREAT_FIELDS),
                )
            threat_type = threat["threat_type"]
            if not isinstance(threat_type, str) or threat_type not in self._THREAT_TYPES:
                raise SnapshotValidationError("threat_type_enum_invalid")
            threat_level = threat["level"]
            if not isinstance(threat_level, str) or threat_level not in self._ALERT_LEVELS:
                raise SnapshotValidationError("threat_level_enum_invalid")
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

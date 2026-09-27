from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time
from typing import Any, Protocol
from uuid import uuid4

from .models import DateOverride, Lesson, Parity, ScheduleProfile, ScheduleRule
from .profiles import ConfirmationError, ProfileCollection, ProfileLimitReached, kyiv_today
from .quotas import ScheduleValidationError
from .resolver import ResolvedDay, preview
from .sessions import ImportSessionRepository, PendingImportSession

SCHEMA_VERSION = "1.0"
MAX_SNAPSHOT_BYTES = 256_000
MAX_SOURCE_DISPLAY_LABEL_LENGTH = 160
MAX_METADATA_TIMESTAMP_LENGTH = 64
_METADATA_FIELDS = {"template_name", "created_at", "source_display_label", "included_fields"}
_FORBIDDEN_KEY_PARTS = (
    "region",
    "coordinate",
    "alert",
    "setting",
    "secret",
    "telegram",
    "password",
)
_FORBIDDEN_KEYS = {
    "id",
    "identity",
    "user_id",
    "chat_id",
    "owner_id",
    "profile_id",
    "token",
    "api_token",
    "latitude",
    "longitude",
    "lat",
    "lon",
    "lng",
    "authorization",
}
_HH_MM = re.compile(r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")


class SnapshotError(ValueError):
    pass


class IntegrityVerifier(Protocol):
    def verify(self, payload: bytes, integrity: Mapping[str, Any]) -> bool:
        """Return True only when integrity authenticates the canonical payload."""


@dataclass(frozen=True)
class ImportProposal:
    token: str
    fingerprint: str
    profile: ScheduleProfile
    resolved_preview: tuple[ResolvedDay, ...]
    included_fields: tuple[str, ...]
    links_present: bool
    source_display_label: str
    created_at: str
    expires_at: datetime | None
    requires_confirmation: bool = True


@dataclass(frozen=True)
class ReplacementProposal:
    token: str
    import_proposal: ImportProposal
    replace_profile_id: str
    include_join_links: bool = False
    requires_confirmation: bool = True


def export_snapshot(
    profile: ScheduleProfile,
    *,
    source_display_label: str,
    created_at: datetime | None = None,
    expires_at: datetime | None = None,
    include_join_links: bool = False,
    confirm_include_join_links: bool = False,
    integrity: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if include_join_links and not confirm_include_join_links:
        raise ConfirmationError("including lesson links requires explicit confirmation")
    created = created_at or datetime.now(UTC)
    if created.tzinfo is None:
        raise ValueError("created_at must be timezone-aware")
    source_label = _validated_source_display_label(source_display_label, error_type=ValueError)
    if expires_at is not None:
        if expires_at.tzinfo is None:
            raise ValueError("expires_at must be timezone-aware")
        if expires_at <= created:
            raise ValueError("expires_at must be after created_at")
    links_present = include_join_links and _profile_has_links(profile)
    included = ["name", "timezone", "rules", "overrides", "lesson_labels"]
    if links_present:
        included.append("join_links")
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "metadata": {
            "template_name": profile.name,
            "created_at": created.astimezone(UTC).isoformat(),
            "source_display_label": source_label,
            "included_fields": included,
        },
        "profile": _profile_to_data(profile, include_join_links),
    }
    if expires_at is not None:
        result["expires_at"] = expires_at.astimezone(UTC).isoformat()
    if integrity is not None:
        result["integrity"] = copy.deepcopy(dict(integrity))
    return copy.deepcopy(result)


def encode_snapshot(snapshot: Mapping[str, Any]) -> str:
    return json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def snapshot_signing_payload(snapshot: Mapping[str, Any]) -> bytes:
    unsigned = copy.deepcopy(dict(snapshot))
    unsigned.pop("integrity", None)
    return encode_snapshot(unsigned).encode("utf-8")


def propose_import(
    raw: str | bytes | Mapping[str, Any],
    *,
    sessions: ImportSessionRepository,
    start: date | None = None,
    verifier: IntegrityVerifier | None = None,
    now: datetime | None = None,
) -> ImportProposal:
    checked_at = now or datetime.now(UTC)
    data = _decode(raw)
    _reject_forbidden_keys(data)
    if data.get("schema_version") != SCHEMA_VERSION:
        raise SnapshotError("unsupported snapshot schema version")
    allowed_top = {"schema_version", "metadata", "profile", "expires_at", "integrity"}
    if not set(data).issubset(allowed_top) or not {
        "schema_version",
        "metadata",
        "profile",
    }.issubset(data):
        raise SnapshotError("snapshot contains unknown or missing top-level fields")
    expires_at = _parse_expiry(data.get("expires_at"))
    _verify_expiry(expires_at, checked_at)
    integrity = data.get("integrity")
    if integrity is not None:
        integrity_data = _mapping(integrity, "integrity")
        if verifier is None:
            raise SnapshotError("signed snapshot requires an integrity verifier")
        try:
            verified = verifier.verify(snapshot_signing_payload(data), integrity_data)
        except Exception as exc:
            raise SnapshotError("snapshot integrity verification failed") from exc
        if not verified:
            raise SnapshotError("snapshot integrity verification failed")
    metadata = _validate_metadata(data["metadata"])
    profile_data = _mapping(data["profile"], "profile")
    allowed_profile = {"name", "timezone", "rules", "overrides"}
    if not set(profile_data).issubset(allowed_profile) or not {"name", "timezone"}.issubset(
        profile_data
    ):
        raise SnapshotError("snapshot profile contains prohibited or missing fields")
    try:
        profile = ScheduleProfile(
            profile_id=f"imported-{uuid4().hex}",
            name=str(profile_data["name"]),
            timezone=str(profile_data["timezone"]),
            rules=tuple(_rule_from_data(item) for item in profile_data.get("rules", [])),
            overrides=tuple(
                _override_from_data(item) for item in profile_data.get("overrides", [])
            ),
            active=False,
        )
        resolved = preview(profile, start or kyiv_today(), 14)
    except ScheduleValidationError:
        raise
    except (TypeError, ValueError) as exc:
        if isinstance(exc, SnapshotError):
            raise
        raise SnapshotError("invalid snapshot profile") from exc
    links_present = _profile_has_links(profile)
    included: tuple[str, ...] = (
        "name",
        "timezone",
        "rules",
        "overrides",
        "lesson_labels",
    )
    if links_present:
        included += ("join_links",)
    source_label = _validated_source_display_label(metadata["source_display_label"])
    created_at = str(metadata["created_at"])
    fingerprint = _proposal_fingerprint(profile, source_label, created_at, expires_at)
    proposal = ImportProposal(
        token=uuid4().hex,
        fingerprint=fingerprint,
        profile=profile,
        resolved_preview=resolved,
        included_fields=included,
        links_present=links_present,
        source_display_label=source_label,
        created_at=created_at,
        expires_at=expires_at,
    )
    sessions.create(PendingImportSession(proposal.token, fingerprint, expires_at))
    return proposal


def confirm_import(
    collection: ProfileCollection,
    proposal: ImportProposal,
    *,
    sessions: ImportSessionRepository,
    confirmed: bool,
    confirm_include_join_links: bool = False,
    now: datetime | None = None,
) -> ScheduleProfile:
    checked_at = now or datetime.now(UTC)
    fingerprint = _validate_proposal(proposal, confirmed, confirm_include_join_links, checked_at)
    if len(collection.profiles) >= collection.MAX_PROFILES:
        raise ProfileLimitReached("profile limit reached; use the explicit replacement operation")
    return sessions.consume_atomically(
        proposal.token,
        fingerprint,
        checked_at,
        lambda: collection.add(
            _independent_profile(
                _profile_for_import(
                    proposal.profile,
                    include_join_links=confirm_include_join_links,
                )
            )
        ),
    )


def request_import_replacement(
    collection: ProfileCollection,
    proposal: ImportProposal,
    *,
    sessions: ImportSessionRepository,
    replace_profile_id: str,
    confirm_include_join_links: bool = False,
    now: datetime | None = None,
) -> ReplacementProposal:
    checked_at = now or datetime.now(UTC)
    fingerprint = _validate_proposal(proposal, True, confirm_include_join_links, checked_at)
    sessions.assert_pending(proposal.token, fingerprint, checked_at)
    if len(collection.profiles) < collection.MAX_PROFILES:
        raise ProfileLimitReached("replacement is only available at the three-profile limit")
    if not any(item.profile_id == replace_profile_id for item in collection.profiles):
        raise KeyError(replace_profile_id)
    replacement = ReplacementProposal(
        uuid4().hex,
        proposal,
        replace_profile_id,
        include_join_links=confirm_include_join_links,
    )
    sessions.bind_replacement(
        proposal.token,
        fingerprint,
        replacement.token,
        replace_profile_id,
        checked_at,
    )
    return replacement


def confirm_import_replacement(
    collection: ProfileCollection,
    replacement: ReplacementProposal,
    *,
    sessions: ImportSessionRepository,
    confirmed: bool,
    now: datetime | None = None,
) -> ScheduleProfile:
    if not confirmed:
        raise ConfirmationError("profile replacement was not confirmed")
    proposal = replacement.import_proposal
    checked_at = now or datetime.now(UTC)
    fingerprint = _validate_proposal(
        proposal,
        True,
        replacement.include_join_links,
        checked_at,
    )
    return sessions.consume_atomically(
        proposal.token,
        fingerprint,
        checked_at,
        lambda: collection.replace(
            replacement.replace_profile_id,
            _independent_profile(
                _profile_for_import(
                    proposal.profile,
                    include_join_links=replacement.include_join_links,
                )
            ),
        ),
        replacement_token=replacement.token,
        replace_profile_id=replacement.replace_profile_id,
    )


def _validate_proposal(
    proposal: ImportProposal,
    confirmed: bool,
    confirm_include_join_links: bool,
    now: datetime,
) -> str:
    if not confirmed:
        raise ConfirmationError("import was not confirmed")
    _verify_expiry(proposal.expires_at, now, error_type=ConfirmationError)
    fingerprint = _proposal_fingerprint(
        proposal.profile,
        proposal.source_display_label,
        proposal.created_at,
        proposal.expires_at,
    )
    if fingerprint != proposal.fingerprint:
        raise ConfirmationError("import proposal was altered")
    return fingerprint


def _proposal_fingerprint(
    profile: ScheduleProfile,
    source_display_label: str,
    created_at: str,
    expires_at: datetime | None,
) -> str:
    payload = {
        "profile": _profile_to_data(profile, True),
        "source_display_label": source_display_label,
        "created_at": created_at,
        "expires_at": expires_at.astimezone(UTC).isoformat() if expires_at else None,
    }
    return hashlib.sha256(encode_snapshot(payload).encode("utf-8")).hexdigest()


def _independent_profile(profile: ScheduleProfile) -> ScheduleProfile:
    return copy.deepcopy(replace(profile, profile_id=f"imported-{uuid4().hex}", active=False))


def _profile_for_import(
    profile: ScheduleProfile,
    *,
    include_join_links: bool,
) -> ScheduleProfile:
    if include_join_links:
        return profile

    def without_link(lesson: Lesson) -> Lesson:
        return replace(lesson, join_url=None, storage_ref=None)

    return replace(
        profile,
        rules=tuple(
            replace(rule, lessons=tuple(without_link(lesson) for lesson in rule.lessons))
            for rule in profile.rules
        ),
        overrides=tuple(
            replace(
                override,
                lessons=tuple(without_link(lesson) for lesson in override.lessons),
            )
            for override in profile.overrides
        ),
    )


def _parse_expiry(raw_expiry: Any) -> datetime | None:
    if raw_expiry is None:
        return None
    try:
        expires_at = datetime.fromisoformat(str(raw_expiry))
    except ValueError as exc:
        raise SnapshotError("invalid expires_at") from exc
    if expires_at.tzinfo is None:
        raise SnapshotError("expires_at must be timezone-aware")
    return expires_at


def _verify_expiry(
    expires_at: datetime | None,
    now: datetime,
    *,
    error_type: type[ValueError] = SnapshotError,
) -> None:
    if now.tzinfo is None:
        raise error_type("verification time must be timezone-aware")
    if expires_at is not None and now >= expires_at:
        raise error_type("snapshot has expired")


def _decode(raw: str | bytes | Mapping[str, Any]) -> Mapping[str, Any]:
    if isinstance(raw, Mapping):
        encoded = encode_snapshot(raw)
        if len(encoded.encode("utf-8")) > MAX_SNAPSHOT_BYTES:
            raise SnapshotError("snapshot is oversized")
        return copy.deepcopy(raw)
    payload = raw.encode("utf-8") if isinstance(raw, str) else raw
    if len(payload) > MAX_SNAPSHOT_BYTES:
        raise SnapshotError("snapshot is oversized")
    try:
        decoded = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SnapshotError("malformed snapshot") from exc
    if not isinstance(decoded, dict):
        raise SnapshotError("snapshot root must be an object")
    return decoded


def _validate_metadata(value: Any) -> Mapping[str, Any]:
    metadata = _mapping(value, "metadata")
    if set(metadata) != _METADATA_FIELDS:
        raise SnapshotError("metadata contains unknown or missing fields")
    if not isinstance(metadata["template_name"], str) or not metadata["template_name"].strip():
        raise SnapshotError("metadata template_name must be a non-empty string")
    _parse_metadata_created_at(metadata["created_at"])
    _validated_source_display_label(metadata["source_display_label"])
    included = metadata["included_fields"]
    if not isinstance(included, list) or not all(isinstance(item, str) for item in included):
        raise SnapshotError("metadata included_fields must be a string list")
    return metadata


def _parse_metadata_created_at(value: Any) -> datetime:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > MAX_METADATA_TIMESTAMP_LENGTH
        or "T" not in value
    ):
        raise SnapshotError("metadata created_at must be a bounded ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SnapshotError("metadata created_at must be a bounded ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SnapshotError("metadata created_at must be timezone-aware")
    return parsed


def _validated_source_display_label(
    value: Any, *, error_type: type[ValueError] = SnapshotError
) -> str:
    if not isinstance(value, str) or not value.strip():
        raise error_type("metadata source_display_label must be a non-empty string")
    normalized = value.strip()
    if len(normalized) > MAX_SOURCE_DISPLAY_LABEL_LENGTH:
        raise error_type(
            "metadata source_display_label must not exceed "
            f"{MAX_SOURCE_DISPLAY_LABEL_LENGTH} characters"
        )
    return normalized


def _reject_forbidden_keys(value: Any, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key).strip().lower().replace("-", "_")
            if (
                key in _FORBIDDEN_KEYS
                or key.endswith("_id")
                or key.endswith("_token")
                or any(part in key for part in _FORBIDDEN_KEY_PARTS)
            ):
                raise SnapshotError(f"prohibited field at {path}.{raw_key}")
            _reject_forbidden_keys(child, f"{path}.{raw_key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _reject_forbidden_keys(child, f"{path}[{index}]")


def _profile_to_data(profile: ScheduleProfile, links: bool) -> dict[str, Any]:
    return {
        "name": profile.name,
        "timezone": profile.timezone,
        "rules": [_rule_to_data(rule, links) for rule in profile.rules],
        "overrides": [_override_to_data(item, links) for item in profile.overrides],
    }


def _lesson_to_data(lesson: Lesson, links: bool) -> dict[str, Any]:
    data: dict[str, Any] = {
        "start": lesson.start.isoformat(timespec="minutes"),
        "end": lesson.end.isoformat(timespec="minutes"),
        "title": lesson.title,
        "enabled": lesson.enabled,
    }
    if links and lesson.join_url:
        data["join_url"] = lesson.join_url
    return data


def _lesson_from_data(data: Any) -> Lesson:
    item = _mapping(data, "lesson")
    if not set(item).issubset({"start", "end", "title", "enabled", "join_url"}):
        raise SnapshotError("lesson contains prohibited fields")
    if "enabled" in item and not isinstance(item["enabled"], bool):
        raise SnapshotError("lesson enabled must be boolean")
    start_raw = item.get("start")
    end_raw = item.get("end")
    if not isinstance(start_raw, str) or not _HH_MM.fullmatch(start_raw):
        raise SnapshotError("lesson start must use naive HH:MM")
    if not isinstance(end_raw, str) or not _HH_MM.fullmatch(end_raw):
        raise SnapshotError("lesson end must use naive HH:MM")
    try:
        return Lesson(
            start=time.fromisoformat(start_raw),
            end=time.fromisoformat(end_raw),
            title=str(item["title"]),
            enabled=item.get("enabled", True),
            join_url=(str(item["join_url"]) if item.get("join_url") else None),
        )
    except ScheduleValidationError:
        raise
    except (KeyError, ValueError, TypeError) as exc:
        raise SnapshotError("invalid lesson") from exc


def _rule_to_data(rule: ScheduleRule, links: bool) -> dict[str, Any]:
    return {
        "weekday": rule.weekday,
        "week_parity": rule.week_parity.value,
        "date_parity": rule.date_parity.value,
        "valid_from": rule.valid_from.isoformat() if rule.valid_from else None,
        "valid_to": rule.valid_to.isoformat() if rule.valid_to else None,
        "lessons": [_lesson_to_data(item, links) for item in rule.lessons],
    }


def _rule_from_data(data: Any) -> ScheduleRule:
    item = _mapping(data, "rule")
    if not set(item).issubset(
        {"weekday", "week_parity", "date_parity", "valid_from", "valid_to", "lessons"}
    ):
        raise SnapshotError("rule contains prohibited fields")
    try:
        return ScheduleRule(
            weekday=int(item["weekday"]),
            week_parity=Parity(str(item.get("week_parity", "ANY"))),
            date_parity=Parity(str(item.get("date_parity", "ANY"))),
            valid_from=date.fromisoformat(item["valid_from"]) if item.get("valid_from") else None,
            valid_to=date.fromisoformat(item["valid_to"]) if item.get("valid_to") else None,
            lessons=tuple(_lesson_from_data(value) for value in item.get("lessons", [])),
        )
    except SnapshotError:
        raise
    except ScheduleValidationError:
        raise
    except (KeyError, ValueError, TypeError) as exc:
        raise SnapshotError("invalid rule") from exc


def _override_to_data(item: DateOverride, links: bool) -> dict[str, Any]:
    return {
        "day": item.day.isoformat(),
        "day_off": item.day_off,
        "lessons": [_lesson_to_data(lesson, links) for lesson in item.lessons],
    }


def _override_from_data(data: Any) -> DateOverride:
    item = _mapping(data, "override")
    if not set(item).issubset({"day", "day_off", "lessons"}):
        raise SnapshotError("override contains prohibited fields")
    if "day_off" in item and not isinstance(item["day_off"], bool):
        raise SnapshotError("override day_off must be boolean")
    try:
        return DateOverride(
            day=date.fromisoformat(str(item["day"])),
            day_off=item.get("day_off", False),
            lessons=tuple(_lesson_from_data(value) for value in item.get("lessons", [])),
        )
    except SnapshotError:
        raise
    except ScheduleValidationError:
        raise
    except (KeyError, ValueError, TypeError) as exc:
        raise SnapshotError("invalid override") from exc


def _profile_has_links(profile: ScheduleProfile) -> bool:
    rule_links = any(lesson.join_url for rule in profile.rules for lesson in rule.lessons)
    override_links = any(
        lesson.join_url for override in profile.overrides for lesson in override.lessons
    )
    return rule_links or override_links


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SnapshotError(f"{label} must be an object")
    return value

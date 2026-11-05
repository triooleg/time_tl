"""Additional signed public map capabilities; never a replacement for base release policy."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from . import production_release
from .contracts import (
    PRODUCTION_RELEASE_MANIFEST,
    PRODUCTION_SCHEMA_HEAD,
    ProductionReleaseManifest,
    RuntimeContractError,
)

FEATURE_PATH = Path("/run/time-tl-power-map/feature-manifest.json")
FEATURE_SIGNATURE_PATH = Path("/run/time-tl-power-map/feature-manifest.ed25519")
BASE_PATH = Path("/run/time-tl-release/release-manifest.json")
BASE_SIGNATURE_PATH = Path("/run/time-tl-release/release-manifest.ed25519")
MIGRATION_HEAD = PRODUCTION_SCHEMA_HEAD
OWNER_REVISION = 15
ROLES = frozenset({"bot", "map-planner", "sender-worker"})
CAPABILITIES = frozenset(
    {
        "bot_power_ui",
        "map_preferences",
        "map_planner",
        "map_dispatch",
        "address_lookup",
    }
)
_ROLE_CAPABILITIES = {
    "bot": frozenset({"bot_power_ui", "map_preferences", "address_lookup"}),
    "map-planner": frozenset({"map_planner"}),
    "sender-worker": frozenset({"map_dispatch"}),
}
_FIELDS = {
    "schema_version",
    "kind",
    "mode",
    "source_commit",
    "archive_sha256",
    "image_digest",
    "native_check_sha256",
    "base_release_sha256",
    "migration_head",
    "owner_revision",
    "roles",
    "capabilities",
    "installed_files",
}
_REQUIRED_FILES = frozenset(
    "time_tl/" + name
    for name in (
        "config.py",
        "runtime/services.py",
        "runtime/power_map_release.py",
        "runtime/power_map_service.py",
        "runtime/power_map_notifications.py",
        "runtime/power_map_bot.py",
        "bot/async_service.py",
        "bot/power_service.py",
        "application/sender_worker.py",
        "application/power_map_messages.py",
        "domain/power_outages/map_reminders.py",
        "infrastructure/db/workers.py",
        "infrastructure/db/accounts.py",
        "infrastructure/db/power_map_preferences.py",
        "infrastructure/db/power_map_notifications.py",
        "admin/settings.py",
    )
)
_MAX_FEATURE_BYTES = 32768


def _fail(reason: str) -> RuntimeContractError:
    return RuntimeContractError("power map release " + reason)


def _read_bounded(path: Path, maximum: int) -> bytes:
    with path.open("rb") as stream:
        return stream.read(maximum + 1)


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _fail("contains duplicate fields")
        result[key] = value
    return result


def _nonfinite(value: str) -> None:
    raise _fail("contains a nonfinite number")


def _parse(payload: bytes) -> dict[str, Any]:
    if type(payload) is not bytes or not 0 < len(payload) <= _MAX_FEATURE_BYTES:
        raise _fail("manifest size is invalid")
    try:
        value = json.loads(
            payload.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_nonfinite
        )
        pending, count = [(value, 0)], 0
        while pending:
            node, depth = pending.pop()
            count += 1
            if depth > 8 or count > 1024:
                raise _fail("manifest structure is invalid")
            if type(node) is dict:
                pending.extend((item, depth + 1) for item in node.values())
            elif type(node) is list:
                pending.extend((item, depth + 1) for item in node)
    except (UnicodeError, ValueError, RecursionError, OverflowError):
        raise _fail("manifest is malformed") from None
    if type(value) is not dict:
        raise _fail("manifest fields are invalid")
    version = value.get("schema_version")
    expected_fields = _FIELDS | ({"weather_release_sha256"} if type(version) is int
                                 and version == 2 else set())
    if set(value) != expected_fields:
        raise _fail("manifest fields are invalid")
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] not in {1, 2}
        or value["kind"] != "time-tl-power-map-public-release"
        or value["mode"] != "public"
    ):
        raise _fail("scope is unsupported")
    if (
        type(value["owner_revision"]) is not int
        or value["owner_revision"] != OWNER_REVISION
        or value["migration_head"] != (
            "0009_calendar_foundation" if version == 1 else MIGRATION_HEAD)
    ):
        raise _fail("owner or schema binding is invalid")
    patterns = {"source_commit": r"[a-f0-9]{40}", "image_digest": r"sha256:[a-f0-9]{64}"}
    patterns.update(
        {
            key: r"[a-f0-9]{64}"
            for key in (
                "archive_sha256",
                "native_check_sha256",
                "base_release_sha256",
            )
        }
    )
    if version == 2:
        patterns["weather_release_sha256"] = r"[a-f0-9]{64}"
    if any(
        type(value[key]) is not str or re.fullmatch(pattern, value[key]) is None
        for key, pattern in patterns.items()
    ):
        raise _fail("artifact binding is invalid")
    roles, caps, files = value["roles"], value["capabilities"], value["installed_files"]
    if (
        type(roles) is not list
        or len(roles) != 3
        or any(type(role) is not str for role in roles)
        or frozenset(roles) != ROLES
    ):
        raise _fail("roles are invalid")
    if (
        type(caps) is not dict
        or set(caps) != CAPABILITIES
        or any(type(flag) is not bool for flag in caps.values())
        or caps["address_lookup"]
        or caps["map_preferences"]
        and not caps["bot_power_ui"]
        or caps["map_dispatch"]
        and not caps["map_planner"]
    ):
        raise _fail("capabilities are invalid")
    if type(files) is not dict or not 27 <= len(files) <= 64 or not _REQUIRED_FILES <= files.keys():
        raise _fail("installed file scope is invalid")
    for name, digest in files.items():
        if (
            type(name) is not str
            or len(name) > 200
            or re.fullmatch(r"time_tl/(?:[a-z][a-z0-9_]*\/)*[a-z_][a-z0-9_]*\.py", name) is None
            or type(digest) is not str
            or re.fullmatch(r"[a-f0-9]{64}", digest) is None
        ):
            raise _fail("installed file binding is invalid")
    if version == 2 and "time_tl/runtime/weather_release.py" not in files:
        raise _fail("weather admission source binding is absent")
    return value


def _setting_flag(settings: Any, name: str) -> bool:
    flag = getattr(settings, name, False)
    if type(flag) is not bool:
        raise _fail("environment capability type is invalid")
    return flag


def _requested(settings: Any) -> dict[str, bool]:
    ui = _setting_flag(settings, "power_outages_enabled") and _setting_flag(
        settings, "power_alerts_energy_api_enabled"
    )
    reminders = ui and _setting_flag(settings, "power_map_reminders_enabled")
    return {
        "bot_power_ui": ui,
        "map_preferences": reminders,
        "map_planner": reminders,
        "map_dispatch": reminders,
        "address_lookup": ui and _setting_flag(settings, "power_address_lookup_enabled"),
    }


def _settings_scope(role: str, settings: Any) -> None:
    def value(name: str) -> object:
        item = getattr(settings, name, None)
        return getattr(item, "value", item)

    if (
        role not in ROLES
        or value("service_role") != role
        or value("environment") != "production"
        or value("owner_config_source") != "database"
        or value("owner_config_revision") is not None
        and (
            type(value("owner_config_revision")) is not int
            or value("owner_config_revision") != OWNER_REVISION
        )
    ):
        raise _fail("runtime scope is invalid")
    allowlist = getattr(settings, "test_chat_allowlist", None)
    if type(allowlist) is not tuple or allowlist:
        raise _fail("public scope requires an empty staging allowlist")


@dataclass(frozen=True)
class MapFeatureAuthority:
    role: str
    manifest_sha256: str
    source_commit: str
    archive_sha256: str
    image_digest: str
    native_check_sha256: str
    base_release_sha256: str
    base_fingerprint: str
    owner_revision: int
    migration_head: str
    effective_capabilities: tuple[tuple[str, bool], ...]
    installed_files: tuple[tuple[str, str], ...]

    def enabled(self, capability: str, settings: Any | None = None) -> bool:
        if capability not in CAPABILITIES:
            raise _fail("capability is unknown")
        enabled = dict(self.effective_capabilities)[capability]
        if settings is not None:
            _settings_scope(self.role, settings)
            enabled = enabled and _requested(settings)[capability]
        return enabled


def _installed_bytes(name: str) -> bytes:
    package_root = Path(__file__).resolve().parents[2]
    path = package_root.joinpath(*name.split("/"))
    if path.is_symlink() or not path.resolve().is_relative_to(package_root):
        raise _fail("installed source path is invalid")
    return _read_bounded(path, 2_097_152)


def verify_feature_scope(
    role: str,
    settings: Any,
    *,
    production_manifest: ProductionReleaseManifest | None = None,
    read_manifest_bytes: Callable[[], bytes] | None = None,
    read_signature_bytes: Callable[[], bytes] | None = None,
    read_installed_file_bytes: Callable[[str], bytes] | None = None,
) -> MapFeatureAuthority:
    """Verify base+feature signatures and installed bytes before admitting a capability.

    Fixed paths and the existing baked public key are production defaults. Read
    callbacks support isolated negative tests; they are never environment options.
    The actual container image digest remains an external deployment proof.
    """
    _settings_scope(role, settings)
    if getattr(settings, "owner_config_revision", None) is None and production_manifest is None:
        raise _fail("normal production gate is required for unresolved revision")
    try:
        raw = (
            read_manifest_bytes()
            if read_manifest_bytes
            else _read_bounded(FEATURE_PATH, _MAX_FEATURE_BYTES)
        )
        signature = (
            read_signature_bytes()
            if read_signature_bytes
            else _read_bounded(FEATURE_SIGNATURE_PATH, 256)
        )
        if type(signature) is not bytes or not 0 < len(signature) <= 256:
            raise _fail("signature size is invalid")
        decoded = base64.b64decode(signature.strip(), validate=True)
        if len(decoded) != 64:
            raise _fail("signature is malformed")
        value = _parse(raw)
        production_release._public_key().verify(decoded, raw)
        base_raw = _read_bounded(BASE_PATH, 4096)
        base_signature = _read_bounded(BASE_SIGNATURE_PATH, 256)
        base, fingerprint = production_release.load_verified_release_attestation(
            expected_config_revision=OWNER_REVISION,
            read_manifest_bytes=lambda: base_raw,
            read_signature_bytes=lambda: base_signature,
        )
        if production_manifest is not None and production_manifest != base:
            raise _fail("base production gate mismatch")
        if value["migration_head"] != base.migration_head:
            raise _fail("base schema binding mismatch")
        if value["schema_version"] == 2:
            from .weather_release import load_verified_weather_manifest

            weather, weather_digest, weather_base = load_verified_weather_manifest()
            if (weather_digest != value["weather_release_sha256"] or weather_base != base
                or weather["image_digest"] != value["image_digest"]
                or weather["source_commit"] != value["source_commit"]):
                raise _fail("weather release binding mismatch")
        if hashlib.sha256(base_raw).hexdigest() != value["base_release_sha256"] or (
            base.commit_sha,
            base.archive_sha256,
            base.image_digest,
            base.config_revision,
            base.migration_head,
            base.provider,
        ) != (
            value["source_commit"],
            value["archive_sha256"],
            value["image_digest"],
            OWNER_REVISION,
            MIGRATION_HEAD,
            PRODUCTION_RELEASE_MANIFEST["provider"],
        ):
            raise _fail("base artifact binding mismatch")
        reader = read_installed_file_bytes or _installed_bytes
        for name, expected in value["installed_files"].items():
            data = reader(name)
            if (
                type(data) is not bytes
                or len(data) > 2_097_152
                or hashlib.sha256(data).hexdigest() != expected
            ):
                raise _fail("installed source mismatch")
    except (
        OSError,
        UnicodeError,
        ValueError,
        TypeError,
        RecursionError,
        binascii.Error,
        InvalidSignature,
    ):
        raise _fail("verification failed") from None
    requested = _requested(settings)
    effective = {}
    for capability, flag in value["capabilities"].items():
        applicable = capability in _ROLE_CAPABILITIES[role]
        if applicable and requested[capability] and not flag:
            raise _fail("environment cannot escalate an unsigned capability")
        effective[capability] = applicable and flag and requested[capability]
    return MapFeatureAuthority(
        role,
        hashlib.sha256(raw).hexdigest(),
        value["source_commit"],
        value["archive_sha256"],
        value["image_digest"],
        value["native_check_sha256"],
        value["base_release_sha256"],
        fingerprint,
        OWNER_REVISION,
        MIGRATION_HEAD,
        tuple(sorted(effective.items())),
        tuple(sorted(value["installed_files"].items())),
    )


def feature_authority_guard(
    authority: MapFeatureAuthority,
    admission_settings: Any,
) -> Callable[[AsyncSession, datetime], Awaitable[None]]:
    """Create a final transaction callback using the original DB-managed settings."""
    capability = {
        "bot": "bot_power_ui",
        "map-planner": "map_planner",
        "sender-worker": "map_dispatch",
    }[authority.role]

    async def guard(session: AsyncSession, now: datetime) -> None:
        if now.tzinfo is None or not authority.enabled(capability, admission_settings):
            raise _fail("transaction capability is disabled")
        # A caller without an applied revision already completed the mandatory
        # normal gate at admission. Revalidation reads that signed base again;
        # keep the exact authority as the comparison boundary.
        if getattr(admission_settings, "owner_config_revision", None) is None:
            from copy import copy

            verification_settings = copy(admission_settings)
            verification_settings.owner_config_revision = authority.owner_revision
        else:
            verification_settings = admission_settings
        current = verify_feature_scope(authority.role, verification_settings)
        if current != authority:
            raise _fail("transaction authority changed")
        await validate_feature_database(session, authority, lock=True)

    return guard


async def validate_feature_database(
    session: AsyncSession, authority: MapFeatureAuthority, *, lock: bool = False
) -> None:
    """Project public owner metadata only; never select the encrypted ACTIVE value.

    Lock order is owner state -> ACTIVE -> migration row, then the caller's user
    and cache locks. Keep this transaction open through planning commit.
    """
    suffix = " FOR UPDATE" if lock else ""
    try:
        state = (
            (
                await session.execute(
                    text(
                        "SELECT value->'active_revision' AS revision, "
                        "value->'pending_revision' AS pending, "
                        "value ? 'pending_revision' AS has_pending "
                        "FROM runtime_settings WHERE key='owner_configuration_state'" + suffix
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        active = (
            (
                await session.execute(
                    text(
                        "SELECT value->'revision' AS revision, "
                        "value->'environment' AS environment, "
                        "value->'alert_provider' AS provider, "
                        "value->'telegram_transport' AS transport, "
                        "value->'staging_allowlist' AS allowlist FROM runtime_settings "
                        "WHERE key='owner_configuration_active'" + suffix
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        heads = (
            (
                await session.execute(
                    text(
                        "SELECT version_num FROM alembic_version ORDER BY version_num"
                        + (" FOR SHARE" if lock else "")
                    )
                )
            )
            .scalars()
            .all()
        )
        if (
            state is None
            or active is None
            or type(state["revision"]) is not int
            or type(active["revision"]) is not int
            or state["has_pending"] is not True
            or state["revision"] != authority.owner_revision
            or active["revision"] != authority.owner_revision
            or state["pending"] is not None
            or active["environment"] != "production"
            or active["provider"] != "alerts_in_ua"
            or active["transport"] != "live"
            or type(active["allowlist"]) is not list
            or active["allowlist"] != []
            or list(heads) != [authority.migration_head]
        ):
            raise _fail("public database scope changed")
    except RuntimeContractError:
        raise
    except Exception:
        raise _fail("public database facts are unavailable") from None

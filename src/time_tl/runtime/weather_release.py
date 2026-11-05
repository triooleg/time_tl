"""Signed capabilities. Environment settings can only narrow authority.

The existing Ed25519 trust anchor is reused. No credential or key is created,
loaded from environment, exported or changed by this module.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from cryptography.exceptions import InvalidSignature
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from . import production_release
from .contracts import PRODUCTION_SCHEMA_HEAD, ProductionReleaseManifest, RuntimeContractError

HEAD = PRODUCTION_SCHEMA_HEAD
OWNER_REVISION = 15
PATH = Path("/run/time-tl-weather/weather-manifest.json")
SIGNATURE_PATH = Path("/run/time-tl-weather/weather-manifest.ed25519")
GEO_PATH = Path("/run/time-tl-weather/geography.csv")
GEO_MANIFEST_PATH = Path("/run/time-tl-weather/geography-manifest.json")
CAPABILITIES = frozenset(
    {
        "weather_ui",
        "weather_preferences",
        "weather_dispatch",
        "weather_collect",
        "weather_plan",
        "weather_admin",
        "dynamic_power_coverage",
    }
)
ROLE_CAPABILITIES = {
    "bot": frozenset({"weather_ui", "weather_preferences"}),
    "sender-worker": frozenset({"weather_dispatch"}),
    "admin-web": frozenset({"weather_admin"}),
    "weather-worker": frozenset({"weather_collect"}),
    "weather-planner": frozenset({"weather_plan"}),
    "power-map-collector": frozenset({"dynamic_power_coverage"}),
}
FIELDS = {
    "schema_version",
    "kind",
    "profile",
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
    "geography_sha256",
    "geography_manifest_sha256",
    "source",
}
REQUIRED_FILES = frozenset(
    "time_tl/" + name
    for name in (
        "config.py",
        "runtime/contracts.py",
        "runtime/production_release.py",
        "runtime/weather_release.py",
        "runtime/services.py",
        "runtime/weather_service.py",
        "runtime/weather_notification_service.py",
        "runtime/weather_worker.py",
        "runtime/weather_bot.py",
        "runtime/weather_admin.py",
        "runtime/power_map_release.py",
        "runtime/power_map_worker.py",
        "application/sender_worker.py",
        "application/weather_collection.py",
        "application/weather_notifications.py",
        "application/power_map_worker.py",
        "application/power_coverage.py",
        "bot/service.py",
        "bot/async_service.py",
        "bot/weather_router.py",
        "infrastructure/db/models.py",
        "infrastructure/db/accounts.py",
        "infrastructure/db/workers.py",
        "infrastructure/db/outbox.py",
        "infrastructure/db/weather.py",
        "infrastructure/db/weather_notifications.py",
        "infrastructure/db/weather_configuration.py",
        "infrastructure/db/weather_locations.py",
        "infrastructure/db/weather_retention.py",
        "infrastructure/db/geographic_coverage.py",
        "infrastructure/db/power_coverage.py",
        "infrastructure/db/power_map.py",
        "infrastructure/weather/met_norway.py",
        "admin/weather.py",
        "admin/settings.py",
    )
)
REQUIRED_FILES |= frozenset(
    {
        "time_tl/domain/weather.py",
        "time_tl/domain/coverage.py",
        "time_tl/application/notification_priority.py",
        "time_tl/admin/async_app.py",
        "time_tl/runtime/weather_health.py",
    }
)


def _fail() -> RuntimeContractError:
    return RuntimeContractError("weather release authority is unavailable or changed")


def _read(path: Path, bound: int) -> bytes:
    with path.open("rb") as stream:
        data = stream.read(bound + 1)
    if not 0 < len(data) <= bound:
        raise _fail()
    return data


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _fail()
        result[key] = value
    return result


def _parse(raw: bytes) -> dict[str, Any]:
    if not 0 < len(raw) <= 65536:
        raise _fail()
    value = json.loads(
        raw, object_pairs_hook=_pairs, parse_constant=lambda _: (_ for _ in ()).throw(_fail())
    )
    if type(value) is not dict or set(value) != FIELDS:
        raise _fail()
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or value["kind"] != "time-tl-weather-geography-release"
        or value["profile"] not in {"primary", "weather-off-fallback"}
        or value["mode"] != "public"
        or value["migration_head"] != HEAD
        or type(value["owner_revision"]) is not int
        or value["owner_revision"] != OWNER_REVISION
        or value["source"] != "MET Norway"
    ):
        raise _fail()
    for key in ("archive_sha256", "native_check_sha256", "base_release_sha256"):
        if type(value[key]) is not str or not re.fullmatch(r"[a-f0-9]{64}", value[key]):
            raise _fail()
    if not re.fullmatch(r"[a-f0-9]{40}", str(value["source_commit"])) or not re.fullmatch(
        r"sha256:[a-f0-9]{64}", str(value["image_digest"])
    ):
        raise _fail()
    roles, caps, files = value["roles"], value["capabilities"], value["installed_files"]
    if (
        type(roles) is not list
        or len(roles) != len(ROLE_CAPABILITIES)
        or any(type(role) is not str for role in roles)
        or set(roles) != set(ROLE_CAPABILITIES)
        or type(caps) is not dict
        or set(caps) != CAPABILITIES
        or any(type(flag) is not bool for flag in caps.values())
        or value["profile"] == "weather-off-fallback"
        and any(caps.values())
        or caps["weather_preferences"]
        and not caps["weather_ui"]
        or caps["weather_dispatch"]
        and not (caps["weather_plan"] and caps["weather_collect"])
    ):
        raise _fail()
    if (
        type(files) is not dict
        or not len(REQUIRED_FILES) <= len(files) <= 256
        or not (REQUIRED_FILES <= files.keys())
    ):
        raise _fail()
    for name, digest in files.items():
        if (
            type(name) is not str
            or len(name) > 200
            or not re.fullmatch(r"time_tl/(?:[a-z][a-z0-9_]*/)*[a-z_][a-z0-9_]*\.py", name)
            or type(digest) is not str
            or not re.fullmatch(r"[a-f0-9]{64}", digest)
        ):
            raise _fail()
    for key in ("geography_sha256", "geography_manifest_sha256"):
        if value["profile"] == "weather-off-fallback":
            if value[key] is not None:
                raise _fail()
        elif type(value[key]) is not str or not re.fullmatch(r"[a-f0-9]{64}", value[key]):
            raise _fail()
    return value


def requested_capabilities(role: str, settings: Any) -> dict[str, bool]:
    names = {
        "bot": {
            "weather_ui": "weather_enabled",
            "weather_preferences": "weather_notifications_enabled",
        },
        "sender-worker": {"weather_dispatch": "weather_notifications_enabled"},
        "admin-web": {"weather_admin": "weather_admin_enabled"},
        "weather-worker": {"weather_collect": "weather_enabled"},
        "weather-planner": {"weather_plan": "enabled"},
        "power-map-collector": {"dynamic_power_coverage": "dynamic_coverage"},
    }
    if role not in names:
        raise _fail()
    result = {}
    for cap, field in names[role].items():
        flag = getattr(settings, field, False)
        if type(flag) is not bool:
            raise _fail()
        result[cap] = flag
    return result


def _installed(name: str) -> bytes:
    return _read(Path(__file__).resolve().parents[2].joinpath(*name.split("/")), 2_097_152)


def load_verified_weather_manifest(
    *,
    read_manifest: Callable[[], bytes] | None = None,
    read_signature: Callable[[], bytes] | None = None,
    read_installed: Callable[[str], bytes] | None = None,
    read_base_manifest: Callable[[], bytes] | None = None,
    read_base_signature: Callable[[], bytes] | None = None,
    read_geo: Callable[[], bytes] | None = None,
    read_geo_manifest: Callable[[], bytes] | None = None,
) -> tuple[dict[str, Any], str, ProductionReleaseManifest]:
    try:
        raw = read_manifest() if read_manifest else _read(PATH, 65536)
        signature = read_signature() if read_signature else _read(SIGNATURE_PATH, 256)
        if len(signature) > 256:
            raise _fail()
        production_release._public_key().verify(
            base64.b64decode(signature.strip(), validate=True), raw
        )
        value = _parse(raw)
        base_raw = (
            read_base_manifest()
            if read_base_manifest
            else _read(Path("/run/time-tl-release/release-manifest.json"), 4096)
        )
        base_sig = (
            read_base_signature()
            if read_base_signature
            else _read(Path("/run/time-tl-release/release-manifest.ed25519"), 256)
        )
        base, _ = production_release.load_verified_release_attestation(
            expected_config_revision=OWNER_REVISION,
            read_manifest_bytes=lambda: base_raw,
            read_signature_bytes=lambda: base_sig,
        )
        if hashlib.sha256(base_raw).hexdigest() != value["base_release_sha256"] or (
            base.commit_sha,
            base.archive_sha256,
            base.image_digest,
            base.migration_head,
        ) != (value["source_commit"], value["archive_sha256"], value["image_digest"], HEAD):
            raise _fail()
        for name, expected in value["installed_files"].items():
            if hashlib.sha256((read_installed or _installed)(name)).hexdigest() != expected:
                raise _fail()
        if read_installed is None:
            package = Path(__file__).resolve().parents[1]
            actual_files = {
                "time_tl/" + path.relative_to(package).as_posix() for path in package.rglob("*.py")
            }
            if actual_files != set(value["installed_files"]):
                raise _fail()
        if value["profile"] == "primary":
            geo = read_geo() if read_geo else _read(GEO_PATH, 2_097_152)
            metadata = read_geo_manifest() if read_geo_manifest else _read(GEO_MANIFEST_PATH, 16384)
            if (
                hashlib.sha256(geo).hexdigest() != value["geography_sha256"]
                or hashlib.sha256(metadata).hexdigest() != value["geography_manifest_sha256"]
            ):
                raise _fail()
            geo_value = json.loads(metadata, object_pairs_hook=_pairs)
            if (
                type(geo_value) is not dict
                or geo_value.get("license") != "ODbL-1.0"
                or geo_value.get("coordinate_kind") != "approximate-community-representative"
                or geo_value.get("normalized_sha256") != value["geography_sha256"]
                or type(geo_value.get("catalog_version")) is not str
                or not geo_value["catalog_version"]
                or not re.fullmatch(r"[a-f0-9]{64}", str(geo_value.get("catalog_sha256")))
                or not re.fullmatch(r"[a-f0-9]{64}", str(geo_value.get("catalog_source_sha256")))
            ):
                raise _fail()
        return value, hashlib.sha256(raw).hexdigest(), base
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        RecursionError,
        OverflowError,
        InvalidSignature,
    ):
        raise _fail() from None


@dataclass(frozen=True)
class WeatherFeatureAuthority:
    role: str
    manifest_sha256: str
    image_digest: str
    source_commit: str
    owner_revision: int
    migration_head: str
    effective_capabilities: tuple[tuple[str, bool], ...]
    geography_sha256: str | None
    geography_manifest_sha256: str | None

    def enabled(self, capability: str) -> bool:
        return dict(self.effective_capabilities).get(capability, False)


def verify_weather_scope(
    role: str,
    settings: Any,
    *,
    production_manifest: ProductionReleaseManifest | None = None,
    loader: Callable[
        ..., tuple[dict[str, Any], str, ProductionReleaseManifest]
    ] = load_verified_weather_manifest,
) -> WeatherFeatureAuthority:
    environment = getattr(settings, "environment", None)
    if getattr(environment, "value", environment) != "production" or (
        getattr(settings, "service_role", None) != role
    ):
        raise _fail()
    owner_source = getattr(settings, "owner_config_source", None)
    if owner_source is not None and getattr(owner_source, "value", owner_source) != "database":
        raise _fail()
    revision = getattr(settings, "owner_config_revision", None)
    if revision is not None and (type(revision) is not int or revision != OWNER_REVISION):
        raise _fail()
    allowlist = getattr(settings, "test_chat_allowlist", ())
    if type(allowlist) is not tuple or allowlist:
        raise _fail()
    requested = requested_capabilities(role, settings)
    value, digest, base = loader()
    if production_manifest is not None and production_manifest != base:
        raise _fail()
    if any(flag and not value["capabilities"][cap] for cap, flag in requested.items()):
        raise _fail()
    effective = tuple(
        sorted(
            (cap, cap in requested and requested[cap] and value["capabilities"][cap])
            for cap in CAPABILITIES
        )
    )
    return WeatherFeatureAuthority(
        role,
        digest,
        value["image_digest"],
        value["source_commit"],
        OWNER_REVISION,
        HEAD,
        effective,
        value["geography_sha256"],
        value["geography_manifest_sha256"],
    )


def weather_authority_guard(
    authority: WeatherFeatureAuthority, settings: Any
) -> Callable[[AsyncSession, datetime], Awaitable[None]]:
    async def guard(session: AsyncSession, now: datetime) -> None:
        if now.tzinfo is None or not any(flag for _, flag in authority.effective_capabilities):
            raise _fail()
        if verify_weather_scope(authority.role, settings) != authority:
            raise _fail()
        # Same public owner state/ACTIVE/head projection and ordering as map.
        from .power_map_release import MapFeatureAuthority, validate_feature_database

        await validate_feature_database(session, cast(MapFeatureAuthority, authority), lock=True)
        if authority.geography_sha256 is not None:
            raw = _read(GEO_MANIFEST_PATH, 16384)
            metadata = json.loads(raw, object_pairs_hook=_pairs)
            catalog = (
                await session.execute(
                    text(
                        "SELECT a.catalog_version,v.source_sha256 FROM active_territory_catalogs a "
                        "JOIN territory_catalog_versions v ON v.provider=a.provider AND "
                        "v.catalog_version=a.catalog_version WHERE a.provider='alerts.in.ua' "
                        "AND v.status='ACTIVE'"
                    )
                )
            ).one_or_none()
            if catalog is None or tuple(catalog) != (
                metadata["catalog_version"],
                metadata["catalog_source_sha256"],
            ):
                raise _fail()
            # A signed file beside a mutable database is not point authority.
            # Every enabled row must be an exact member of that signed artifact.
            content = _read(GEO_PATH, 2_097_152)
            if hashlib.sha256(content).hexdigest() != authority.geography_sha256:
                raise _fail()
            reader = csv.DictReader(io.StringIO(content.decode("utf-8")))
            if reader.fieldnames != [
                "katottg",
                "region_id",
                "latitude",
                "longitude",
                "coverage_type",
            ]:
                raise _fail()
            points = {
                (
                    row["katottg"],
                    row["region_id"],
                    float(row["latitude"]),
                    float(row["longitude"]),
                    row["coverage_type"],
                )
                for row in reader
            }
            rows = (
                await session.execute(
                    text(
                        "SELECT katottg_code,region_id,latitude,longitude,"
                        "coverage_type,provenance_sha256,provenance_version,"
                        "provenance_url,approved_at "
                        "FROM weather_locations WHERE enabled=true"
                    )
                )
            ).all()
            for row in rows:
                if (
                    tuple(row[:5]) not in points
                    or row[5] != authority.geography_sha256
                    or row[6] != metadata["publisher_commit"]
                    or row[7] != metadata["normalized_url"]
                    or row[8] is None
                ):
                    raise _fail()

    return guard

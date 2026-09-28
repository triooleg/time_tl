"""Fail-closed startup contracts for each process role."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from hashlib import sha256

from time_tl.config import (
    AdminBackend,
    AlertProvider,
    Environment,
    Settings,
    TelegramTransport,
)
from time_tl.runtime.region_registry_full import EXPECTED_SHA256


class RuntimeContractError(RuntimeError):
    pass


# This is the single compiled release switch. Independent safety review must change
# it only for an exact approved release; all production role gates stay closed together.
PRODUCTION_PUBLIC_RELEASE_APPROVED = True
PRODUCTION_RELEASE_MANIFEST = {
    "provider": "alerts.in.ua/full-territory",
    "territory_selection": "manual-oblast-raion-hromada",
    "catalog_sha256": EXPECTED_SHA256,
    "migration_head": "0007_outbox_indeterminate",
    "replicas": 1,
}

_GIT_SHA_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_TAG_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{2,127}\Z")


@dataclass(frozen=True)
class ProductionReleaseManifest:
    tag: str
    commit_sha: str
    tree_sha: str
    archive_sha256: str
    image_digest: str
    config_revision: int
    provider: str
    catalog_sha256: str
    migration_head: str
    replicas: int
    territory_selection: str

    def fingerprint(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True, separators=(",", ":")).encode()
        return sha256(payload).hexdigest()


def validate_production_release_manifest(
    manifest: ProductionReleaseManifest,
    *,
    expected_config_revision: int,
) -> str:
    """Validate signed release metadata and immutable production policy."""

    if not _TAG_PATTERN.fullmatch(manifest.tag):
        raise RuntimeContractError("production release manifest tag is invalid")
    if not _GIT_SHA_PATTERN.fullmatch(manifest.commit_sha):
        raise RuntimeContractError("production release manifest commit is invalid")
    if not _GIT_SHA_PATTERN.fullmatch(manifest.tree_sha):
        raise RuntimeContractError("production release manifest tree is invalid")
    if not _SHA256_PATTERN.fullmatch(manifest.archive_sha256):
        raise RuntimeContractError("production release manifest archive digest is invalid")
    if not manifest.image_digest.startswith("sha256:") or not _SHA256_PATTERN.fullmatch(
        manifest.image_digest.removeprefix("sha256:")
    ):
        raise RuntimeContractError("production release manifest image digest is invalid")
    if (
        isinstance(manifest.config_revision, bool)
        or manifest.config_revision <= 0
        or manifest.config_revision != expected_config_revision
    ):
        raise RuntimeContractError("production release config revision mismatch")
    if manifest.provider != PRODUCTION_RELEASE_MANIFEST["provider"]:
        raise RuntimeContractError("production release provider mismatch")
    if manifest.catalog_sha256 != PRODUCTION_RELEASE_MANIFEST["catalog_sha256"]:
        raise RuntimeContractError("production release catalogue mismatch")
    if manifest.migration_head != PRODUCTION_RELEASE_MANIFEST["migration_head"]:
        raise RuntimeContractError("production release migration head mismatch")
    if isinstance(manifest.replicas, bool) or manifest.replicas != 1:
        raise RuntimeContractError("production release requires exactly one replica")
    if manifest.territory_selection != PRODUCTION_RELEASE_MANIFEST["territory_selection"]:
        raise RuntimeContractError("production release territory selection mismatch")
    return manifest.fingerprint()


@dataclass(frozen=True)
class RuntimeCapabilities:
    postgres_alert_repository: bool = True
    postgres_alert_lease: bool = True
    reviewed_full_territory_provider: bool = True
    production_full_territory_alert_worker: bool = PRODUCTION_PUBLIC_RELEASE_APPROVED
    postgres_bot_repository: bool = True
    async_bot_application: bool = True
    reviewed_manual_territory_bot: bool = True
    production_manual_territory_bot: bool = PRODUCTION_PUBLIC_RELEASE_APPROVED
    staging_manual_region_bot: bool = True
    postgres_admin_repository: bool = True
    async_admin_application: bool = True
    staging_schedule_worker: bool = True
    staging_sender_worker: bool = True
    production_schedule_worker: bool = PRODUCTION_PUBLIC_RELEASE_APPROVED
    production_sender_worker: bool = PRODUCTION_PUBLIC_RELEASE_APPROVED


DEFAULT_CAPABILITIES = RuntimeCapabilities()


def validate_runtime_contract(
    settings: Settings,
    service: str,
    capabilities: RuntimeCapabilities = DEFAULT_CAPABILITIES,
) -> None:
    """Reject any role that would otherwise look live without required adapters."""
    production_like = settings.environment in {Environment.STAGING, Environment.PRODUCTION}
    if production_like and service in {
        "bot",
        "alert-worker",
        "schedule-worker",
        "sender-worker",
    }:
        missing = []
        if settings.owner_config_revision is None:
            missing.append("applied ACTIVE database revision")
        if settings.alert_provider is not AlertProvider.ALERTS_IN_UA:
            missing.append("official alerts.in.ua provider")
        if settings.telegram_transport is not TelegramTransport.LIVE:
            missing.append("live Telegram transport")
        if settings.environment is Environment.STAGING and not settings.test_chat_allowlist:
            missing.append("non-empty Telegram allowlist")
        if settings.environment is Environment.PRODUCTION and settings.test_chat_allowlist:
            missing.append("empty STAGING allowlist")
        if missing:
            raise RuntimeContractError(
                f"{settings.environment.value} runtime unavailable: " + ", ".join(missing)
            )
    if service == "alert-worker" and production_like:
        missing = []
        if not capabilities.postgres_alert_repository:
            missing.append("PostgreSQL alert repository")
        if not capabilities.postgres_alert_lease:
            missing.append("PostgreSQL singleton lease")
        if not capabilities.reviewed_full_territory_provider:
            missing.append("reviewed full-territory alert provider")
        if (
            settings.environment is Environment.PRODUCTION
            and not capabilities.production_full_territory_alert_worker
        ):
            missing.append("production full-territory alert worker")
        if missing:
            raise RuntimeContractError("alert-worker unavailable: " + ", ".join(missing))
    if service == "bot" and settings.telegram_transport is TelegramTransport.LIVE:
        missing = []
        if not capabilities.postgres_bot_repository:
            missing.append("PostgreSQL bot repository")
        if not capabilities.async_bot_application:
            missing.append("async-compatible Telegram application service")
        if not capabilities.reviewed_manual_territory_bot:
            missing.append("reviewed manual territory hierarchy")
        if (
            settings.environment is Environment.STAGING
            and not capabilities.staging_manual_region_bot
        ):
            missing.append("reviewed manual-territory staging bot")
        if (
            settings.environment is Environment.PRODUCTION
            and not capabilities.production_manual_territory_bot
        ):
            missing.append("production manual-territory bot")
        if missing:
            raise RuntimeContractError("live bot unavailable: " + ", ".join(missing))
    if service == "admin-web" and settings.admin_backend is AdminBackend.POSTGRES:
        missing = []
        if not capabilities.postgres_admin_repository:
            missing.append("PostgreSQL admin repository")
        if not capabilities.async_admin_application:
            missing.append("async-compatible admin application service")
        if missing:
            raise RuntimeContractError("production admin-web unavailable: " + ", ".join(missing))
    if service == "schedule-worker" and production_like:
        released = (
            capabilities.staging_schedule_worker
            if settings.environment is Environment.STAGING
            else capabilities.production_schedule_worker
        )
        if not released:
            raise RuntimeContractError(
                f"{settings.environment.value} schedule-worker capability is not released"
            )
    if service == "sender-worker" and production_like:
        released = (
            capabilities.staging_sender_worker
            if settings.environment is Environment.STAGING
            else capabilities.production_sender_worker
        )
        if not released:
            raise RuntimeContractError(
                f"{settings.environment.value} sender-worker capability is not released"
            )

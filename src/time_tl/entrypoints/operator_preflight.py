"""Read-only operator preflight for a pending owner configuration."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from time_tl.admin.settings import ActivationCandidate, OwnerSettingsService
from time_tl.config import AdminBackend, Environment, OwnerConfigSource, Settings
from time_tl.domain.alerts import ProviderSnapshotKind
from time_tl.infrastructure.alerts import AlertsInUaAdapter
from time_tl.runtime.contracts import (
    ProductionReleaseManifest,
    RuntimeContractError,
    validate_production_release_manifest,
)
from time_tl.runtime.httpx_transport import HttpxTransport
from time_tl.runtime.production_release import production_preflight_correlation
from time_tl.runtime.region_registry_full import EXPECTED_RECORD_COUNT, load_catalog


class OperatorPreflightError(RuntimeError):
    """A deliberately secret-free preflight failure."""


def _active_json_transport() -> HttpxTransport:
    return HttpxTransport(max_response_bytes=AlertsInUaAdapter.MAX_RESPONSE_BYTES)


@dataclass(frozen=True)
class OperatorPreflightEvidence:
    revision: int
    allowlist_count: int
    covered_region_count: int
    environment: str = Environment.STAGING.value
    release_manifest_sha256: str | None = None
    host_metrics_ok: bool = False

    def sanitized_summary(self) -> str:
        summary = (
            f"preflight passed: revision={self.revision} "
            f"allowlist_count={self.allowlist_count} telegram=get_me_ok "
            f"alerts=active_json_full_200 covered_regions={self.covered_region_count} "
            "last_modified=present persisted=no"
        )
        if self.release_manifest_sha256 is not None:
            correlation_id = production_preflight_correlation(
                self.revision, self.release_manifest_sha256
            )
            summary += (
                f" environment={self.environment} "
                f"release_manifest_sha256={self.release_manifest_sha256} "
                f"node_exporter={'ok' if self.host_metrics_ok else 'unverified'} "
                f"correlation_id={correlation_id}"
            )
        return summary


async def probe_candidate(
    candidate: ActivationCandidate,
    approved_chat_id: int,
    *,
    bot_factory: Callable[[str], Any],
    transport_factory: Callable[[], Any] = _active_json_transport,
) -> OperatorPreflightEvidence:
    """Probe credentials and official schema without sending or persisting anything."""

    if approved_chat_id <= 0:
        raise OperatorPreflightError("approved Telegram chat ID must be positive")
    if candidate.environment != Environment.STAGING.value:
        raise OperatorPreflightError("pending configuration is not STAGING")
    if candidate.alert_provider != "alerts_in_ua":
        raise OperatorPreflightError("pending configuration does not use alerts.in.ua")
    if candidate.telegram_transport != "live":
        raise OperatorPreflightError("pending configuration does not use live Telegram")
    if candidate.staging_allowlist != (approved_chat_id,):
        raise OperatorPreflightError("pending allowlist does not exactly match approved chat ID")
    return await _probe_read_only_candidate(
        candidate,
        bot_factory=bot_factory,
        transport_factory=transport_factory,
        environment=Environment.STAGING,
    )


async def probe_production_candidate(
    candidate: ActivationCandidate,
    manifest: ProductionReleaseManifest,
    *,
    bot_factory: Callable[[str], Any],
    transport_factory: Callable[[], Any] = _active_json_transport,
) -> OperatorPreflightEvidence:
    """Run a production-only read probe bound to one exact immutable release identity."""

    if candidate.environment != Environment.PRODUCTION.value:
        raise OperatorPreflightError("pending configuration is not PRODUCTION")
    if candidate.alert_provider != "alerts_in_ua":
        raise OperatorPreflightError("pending configuration does not use alerts.in.ua")
    if candidate.telegram_transport != "live":
        raise OperatorPreflightError("pending configuration does not use live Telegram")
    if candidate.staging_allowlist:
        raise OperatorPreflightError("production configuration requires empty STAGING allowlist")
    try:
        manifest_sha256 = validate_production_release_manifest(
            manifest,
            expected_config_revision=candidate.revision,
        )
    except RuntimeContractError as error:
        raise OperatorPreflightError(str(error)) from None
    return await _probe_read_only_candidate(
        candidate,
        bot_factory=bot_factory,
        transport_factory=transport_factory,
        environment=Environment.PRODUCTION,
        release_manifest_sha256=manifest_sha256,
    )


async def _probe_read_only_candidate(
    candidate: ActivationCandidate,
    *,
    bot_factory: Callable[[str], Any],
    transport_factory: Callable[[], Any],
    environment: Environment,
    release_manifest_sha256: str | None = None,
) -> OperatorPreflightEvidence:
    if candidate.telegram_token is None or candidate.alerts_in_ua_token is None:
        raise OperatorPreflightError("pending credentials are incomplete")

    bot = None
    telegram_ok = False
    try:
        bot = bot_factory(candidate.telegram_token.get_secret_value())
        identity = await bot.get_me()
        telegram_ok = bool(identity.is_bot and identity.id > 0 and identity.username)
    except asyncio.CancelledError:
        raise
    except Exception:
        raise OperatorPreflightError("Telegram getMe preflight failed") from None
    finally:
        if bot is not None:
            try:
                await bot.session.close()
            except asyncio.CancelledError:
                raise
            except Exception:
                raise OperatorPreflightError("Telegram session close failed") from None
    if not telegram_ok:
        raise OperatorPreflightError("Telegram identity is not a named bot")

    catalog_records = load_catalog().records
    expected_coverage = frozenset(item.uid for item in catalog_records)
    adapter = AlertsInUaAdapter(
        token=candidate.alerts_in_ua_token.get_secret_value(),
        transport=transport_factory(),
        region_types={item.uid: item.location_type for item in catalog_records},
    )
    result = await adapter.fetch(fetch_revision=1, last_modified=None)
    snapshot = result.snapshot
    if (
        result.failure is not None
        or snapshot is None
        or snapshot.kind is not ProviderSnapshotKind.FULL_200
        or not snapshot.is_complete
        or not snapshot.is_fresh
        or snapshot.covered_region_ids != expected_coverage
        or len(snapshot.covered_region_ids) != EXPECTED_RECORD_COUNT
        or not result.last_modified
    ):
        diagnostic = result.sanitized_failure()
        suffix = f": {diagnostic}" if diagnostic is not None else ""
        raise OperatorPreflightError(f"alerts.in.ua full-snapshot preflight failed{suffix}")
    return OperatorPreflightEvidence(
        revision=candidate.revision,
        allowlist_count=len(candidate.staging_allowlist),
        covered_region_count=len(snapshot.covered_region_ids),
        environment=environment.value,
        release_manifest_sha256=release_manifest_sha256,
    )


async def preflight_owner_settings(
    settings: Settings,
    revision: int,
    approved_chat_id: int,
    *,
    bot_factory: Callable[[str], Any] | None = None,
    transport_factory: Callable[[], Any] = _active_json_transport,
) -> OperatorPreflightEvidence:
    """Load a pending revision read-only and run the bounded external probes."""

    if settings.admin_backend is not AdminBackend.POSTGRES:
        raise RuntimeContractError("owner settings preflight requires PostgreSQL admin backend")
    if settings.environment is not Environment.STAGING:
        raise RuntimeContractError("owner settings preflight must run with TIME_TL_ENV=staging")
    if settings.owner_telegram_fernet_key is None or settings.owner_alerts_fernet_key is None:
        raise RuntimeContractError("owner settings preflight requires both encryption keys")

    from aiogram import Bot

    from time_tl.runtime.services import _postgres_admin_repository

    engine, repository = _postgres_admin_repository(settings)
    service = OwnerSettingsService(
        repository,
        settings.owner_telegram_fernet_key.get_secret_value(),
        settings.owner_alerts_fernet_key.get_secret_value(),
        default_poll=settings.alert_poll_interval_seconds,
        default_stale=settings.alert_stale_after_seconds,
    )
    try:
        candidate = await service.activation_candidate(revision)
        return await probe_candidate(
            candidate,
            approved_chat_id,
            bot_factory=bot_factory or Bot,
            transport_factory=transport_factory,
        )
    finally:
        await engine.dispose()


async def preflight_production_owner_settings(
    settings: Settings,
    revision: int,
    *,
    bot_factory: Callable[[str], Any] | None = None,
    transport_factory: Callable[[], Any] = _active_json_transport,
    metrics_fetcher: Callable[[str, float, int], bytes] | None = None,
) -> OperatorPreflightEvidence:
    """Load one pending production revision and probe it without activation or writes."""

    if settings.environment is not Environment.PRODUCTION:
        raise RuntimeContractError("production preflight requires TIME_TL_ENV=production")
    if settings.admin_backend is not AdminBackend.POSTGRES:
        raise RuntimeContractError("production preflight requires PostgreSQL admin backend")
    if settings.owner_config_source is not OwnerConfigSource.DATABASE:
        raise RuntimeContractError("production preflight requires database-managed owner config")
    required_keys = (
        settings.owner_telegram_fernet_key,
        settings.owner_alerts_fernet_key,
        settings.lesson_link_fernet_key,
        settings.admin_ip_hash_key,
    )
    if any(value is None or not value.get_secret_value().strip() for value in required_keys):
        raise RuntimeContractError("production preflight requires stable encryption keys")
    assert settings.owner_telegram_fernet_key is not None
    assert settings.owner_alerts_fernet_key is not None
    if not settings.host_metrics_url:
        raise RuntimeContractError("production preflight requires HOST_METRICS_URL")
    if not settings.admin_bootstrap_artifact_path:
        raise RuntimeContractError("production preflight requires ADMIN_BOOTSTRAP_ARTIFACT_PATH")
    from time_tl.admin.node_exporter import NodeExporterMetricsCollector
    from time_tl.runtime.production_release import (
        load_verified_release_attestation,
        validate_production_database_migration_head,
    )

    manifest, _fingerprint = load_verified_release_attestation(
        expected_config_revision=revision
    )

    try:
        collector = (
            NodeExporterMetricsCollector(settings.host_metrics_url)
            if metrics_fetcher is None
            else NodeExporterMetricsCollector(
                settings.host_metrics_url,
                fetcher=metrics_fetcher,
            )
        )
    except ValueError:
        raise RuntimeContractError("production preflight node_exporter URL is invalid") from None
    metrics = await asyncio.to_thread(collector.collect)
    if metrics.get("source_status") != "available":
        raise RuntimeContractError("production preflight node_exporter probe failed")
    await validate_production_database_migration_head(settings, manifest)

    from aiogram import Bot

    from time_tl.runtime.services import _postgres_admin_repository

    engine, repository = _postgres_admin_repository(settings)
    service = OwnerSettingsService(
        repository,
        settings.owner_telegram_fernet_key.get_secret_value(),
        settings.owner_alerts_fernet_key.get_secret_value(),
        default_poll=settings.alert_poll_interval_seconds,
        default_stale=settings.alert_stale_after_seconds,
    )
    try:
        candidate = await service.activation_candidate(revision)
        evidence = await probe_production_candidate(
            candidate,
            manifest,
            bot_factory=bot_factory or Bot,
            transport_factory=transport_factory,
        )
        return replace(evidence, host_metrics_ok=True)
    finally:
        await engine.dispose()

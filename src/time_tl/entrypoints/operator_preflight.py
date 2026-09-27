"""Read-only operator preflight for a pending owner configuration."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from time_tl.admin.settings import ActivationCandidate, OwnerSettingsService
from time_tl.config import AdminBackend, Environment, Settings
from time_tl.domain.alerts import ProviderSnapshotKind
from time_tl.infrastructure.alerts import FullTerritoryAlertsInUaAdapter
from time_tl.runtime.contracts import RuntimeContractError
from time_tl.runtime.httpx_transport import HttpxTransport
from time_tl.runtime.region_registry_full import EXPECTED_RECORD_COUNT, load_catalog


class OperatorPreflightError(RuntimeError):
    """A deliberately secret-free preflight failure."""


@dataclass(frozen=True)
class OperatorPreflightEvidence:
    revision: int
    allowlist_count: int
    covered_region_count: int

    def sanitized_summary(self) -> str:
        return (
            f"preflight passed: revision={self.revision} "
            f"allowlist_count={self.allowlist_count} telegram=get_me_ok "
            f"alerts=full_200 covered_regions={self.covered_region_count} "
            "last_modified=present persisted=no"
        )


async def probe_candidate(
    candidate: ActivationCandidate,
    approved_chat_id: int,
    *,
    bot_factory: Callable[[str], Any],
    transport_factory: Callable[[], Any] = HttpxTransport,
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

    catalog_region_uids = tuple(item.uid for item in load_catalog().records)
    expected_coverage = frozenset(catalog_region_uids)
    adapter = FullTerritoryAlertsInUaAdapter(
        token=candidate.alerts_in_ua_token.get_secret_value(),
        transport=transport_factory(),
        region_uids=catalog_region_uids,
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
        raise OperatorPreflightError("alerts.in.ua full-snapshot preflight failed")
    return OperatorPreflightEvidence(
        revision=candidate.revision,
        allowlist_count=len(candidate.staging_allowlist),
        covered_region_count=len(snapshot.covered_region_ids),
    )


async def preflight_owner_settings(
    settings: Settings,
    revision: int,
    approved_chat_id: int,
    *,
    bot_factory: Callable[[str], Any] | None = None,
    transport_factory: Callable[[], Any] = HttpxTransport,
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

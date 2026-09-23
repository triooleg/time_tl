"""Fail-closed startup contracts for each process role."""

from __future__ import annotations

from dataclasses import dataclass

from time_tl.config import (
    AdminBackend,
    AlertProvider,
    Environment,
    Settings,
    TelegramTransport,
)


class RuntimeContractError(RuntimeError):
    pass


@dataclass(frozen=True)
class RuntimeCapabilities:
    postgres_alert_repository: bool = True
    postgres_alert_lease: bool = True
    reviewed_aggregate_oblast_provider: bool = True
    production_oblast_scope_release: bool = False
    postgres_bot_repository: bool = True
    async_bot_application: bool = True
    reviewed_region_resolver: bool = False
    staging_manual_region_bot: bool = True
    postgres_admin_repository: bool = True
    async_admin_application: bool = True
    staging_schedule_worker: bool = True
    staging_sender_worker: bool = True
    schedule_worker: bool = False
    sender_worker: bool = False


DEFAULT_CAPABILITIES = RuntimeCapabilities()


def validate_runtime_contract(
    settings: Settings,
    service: str,
    capabilities: RuntimeCapabilities = DEFAULT_CAPABILITIES,
) -> None:
    """Reject any role that would otherwise look live without required adapters."""
    production_like = settings.environment in {Environment.STAGING, Environment.PRODUCTION}
    if settings.environment is Environment.STAGING and service in {
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
        if not settings.test_chat_allowlist:
            missing.append("non-empty Telegram allowlist")
        if missing:
            raise RuntimeContractError("staging runtime unavailable: " + ", ".join(missing))
    if service == "alert-worker" and production_like:
        missing = []
        if not capabilities.postgres_alert_repository:
            missing.append("PostgreSQL alert repository")
        if not capabilities.postgres_alert_lease:
            missing.append("PostgreSQL singleton lease")
        if not capabilities.reviewed_aggregate_oblast_provider:
            missing.append("reviewed aggregate-oblast alert provider")
        if (
            settings.environment is Environment.PRODUCTION
            and not capabilities.production_oblast_scope_release
        ):
            missing.append("production aggregate-oblast scope release")
        if missing:
            raise RuntimeContractError("alert-worker unavailable: " + ", ".join(missing))
    if service == "bot" and settings.telegram_transport is TelegramTransport.LIVE:
        missing = []
        if not capabilities.postgres_bot_repository:
            missing.append("PostgreSQL bot repository")
        if not capabilities.async_bot_application:
            missing.append("async-compatible Telegram application service")
        if settings.environment is Environment.STAGING:
            if not capabilities.staging_manual_region_bot:
                missing.append("reviewed manual-region staging bot")
        elif not capabilities.reviewed_region_resolver:
            missing.append("reviewed geolocation resolver")
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
            else capabilities.schedule_worker
        )
        if not released:
            raise RuntimeContractError(
                f"{settings.environment.value} schedule-worker capability is not released"
            )
    if service == "sender-worker" and production_like:
        released = (
            capabilities.staging_sender_worker
            if settings.environment is Environment.STAGING
            else capabilities.sender_worker
        )
        if not released:
            raise RuntimeContractError(
                f"{settings.environment.value} sender-worker capability is not released"
            )

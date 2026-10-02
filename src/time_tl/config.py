"""Environment configuration with safety-critical startup validation."""

from __future__ import annotations

import base64
import binascii
from enum import StrEnum
from functools import lru_cache

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    LOCAL = "local"
    SIMULATION = "simulation"
    STAGING = "staging"
    PRODUCTION = "production"


class AlertProvider(StrEnum):
    FAKE = "fake"
    ALERTS_IN_UA = "alerts_in_ua"


class TelegramTransport(StrEnum):
    DISABLED = "disabled"
    LIVE = "live"


class AdminBackend(StrEnum):
    DISABLED = "disabled"
    LOCAL_SQLITE = "local_sqlite"
    POSTGRES = "postgres"


class OwnerConfigSource(StrEnum):
    ENVIRONMENT = "environment"
    DATABASE = "database"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        hide_input_in_errors=True,
    )
    environment: Environment = Field(default=Environment.LOCAL, alias="TIME_TL_ENV")
    service_role: str = Field(default="unspecified", alias="SERVICE_ROLE")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    timezone: str = Field(default="Europe/Kyiv", alias="TZ")
    alert_provider: AlertProvider = Field(default=AlertProvider.FAKE, alias="ALERT_PROVIDER")
    alerts_in_ua_token: SecretStr | None = Field(default=None, alias="ALERTS_IN_UA_TOKEN")
    alert_poll_interval_seconds: int = Field(
        default=15, ge=15, le=300, alias="ALERT_POLL_INTERVAL_SECONDS"
    )
    alert_stale_after_seconds: int = Field(
        default=120, ge=30, le=3600, alias="ALERT_STALE_AFTER_SECONDS"
    )
    alert_rate_limit_per_minute: int = Field(
        default=5, ge=1, le=5, alias="ALERT_RATE_LIMIT_PER_MINUTE"
    )
    alert_rate_limit_burst: int = Field(default=1, ge=1, le=1, alias="ALERT_RATE_LIMIT_BURST")
    telegram_transport: TelegramTransport = Field(
        default=TelegramTransport.DISABLED, alias="TELEGRAM_TRANSPORT"
    )
    telegram_bot_token: SecretStr | None = Field(default=None, alias="TELEGRAM_BOT_TOKEN")
    bot_onboarding_text: str = Field(default="", alias="BOT_ONBOARDING_TEXT")
    homework_enabled: bool = Field(default=False, alias="HOMEWORK_ENABLED")
    lesson_link_fernet_key: SecretStr | None = Field(default=None, alias="LESSON_LINK_FERNET_KEY")
    test_chat_allowlist_raw: str = Field(default="", alias="TEST_CHAT_ALLOWLIST")
    database_url: SecretStr = Field(alias="DATABASE_URL")
    health_host: str = Field(default="127.0.0.1", alias="HEALTH_HOST")
    health_port: int = Field(default=8080, ge=1024, le=65535, alias="HEALTH_PORT")
    allow_scaffold_services: bool = Field(default=False, alias="ALLOW_SCAFFOLD_SERVICES")
    runtime_state_dir: str = Field(default=".time-tl-runtime", alias="RUNTIME_STATE_DIR")
    schedule_worker_interval_seconds: int = Field(
        default=30, ge=5, le=300, alias="SCHEDULE_WORKER_INTERVAL_SECONDS"
    )
    sender_worker_interval_seconds: int = Field(
        default=2, ge=1, le=60, alias="SENDER_WORKER_INTERVAL_SECONDS"
    )
    sender_messages_per_second: float = Field(
        default=20.0, gt=0, le=25, alias="SENDER_MESSAGES_PER_SECOND"
    )
    sender_transport_timeout_seconds: float = Field(
        default=8.0, gt=0, le=10, alias="SENDER_TRANSPORT_TIMEOUT_SECONDS"
    )
    admin_backend: AdminBackend = Field(default=AdminBackend.DISABLED, alias="ADMIN_BACKEND")
    admin_ip_hash_key: SecretStr | None = Field(default=None, alias="ADMIN_IP_HASH_KEY")
    owner_telegram_fernet_key: SecretStr | None = Field(
        default=None, alias="OWNER_TELEGRAM_FERNET_KEY"
    )
    owner_alerts_fernet_key: SecretStr | None = Field(default=None, alias="OWNER_ALERTS_FERNET_KEY")
    owner_config_source: OwnerConfigSource = Field(
        default=OwnerConfigSource.ENVIRONMENT, alias="OWNER_CONFIG_SOURCE"
    )
    owner_config_revision: int | None = Field(default=None, ge=1, alias="OWNER_CONFIG_REVISION")
    host_metrics_url: str | None = Field(default=None, alias="HOST_METRICS_URL")
    admin_host: str = Field(default="127.0.0.1", alias="ADMIN_HOST")
    admin_port: int = Field(default=8081, ge=1024, le=65535, alias="ADMIN_PORT")
    admin_state_dir: str = Field(default=".time-tl-admin", alias="ADMIN_STATE_DIR")
    admin_cookie_secure: bool = Field(default=True, alias="ADMIN_COOKIE_SECURE")
    admin_trusted_proxy_cidrs_raw: str = Field(default="", alias="ADMIN_TRUSTED_PROXY_CIDRS")
    admin_bootstrap_artifact_path: str | None = Field(
        default=None, alias="ADMIN_BOOTSTRAP_ARTIFACT_PATH"
    )

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, value: str) -> str:
        normalized = value.upper()
        if normalized not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("unsupported log level")
        return normalized

    @field_validator("lesson_link_fernet_key")
    @classmethod
    def validate_fernet_key(cls, value: SecretStr | None) -> SecretStr | None:
        if not _has_secret(value):
            return value
        assert value is not None
        try:
            decoded = base64.urlsafe_b64decode(value.get_secret_value().encode("ascii"))
        except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
            raise ValueError("LESSON_LINK_FERNET_KEY must be a Fernet base64 key") from exc
        if len(decoded) != 32:
            raise ValueError("LESSON_LINK_FERNET_KEY must decode to exactly 32 bytes")
        return value

    @field_validator("admin_ip_hash_key")
    @classmethod
    def validate_admin_ip_hash_key(cls, value: SecretStr | None) -> SecretStr | None:
        if _has_secret(value):
            assert value is not None
            if len(value.get_secret_value().encode("utf-8")) < 32:
                raise ValueError("ADMIN_IP_HASH_KEY must contain at least 32 bytes")
        return value

    @field_validator("owner_telegram_fernet_key", "owner_alerts_fernet_key")
    @classmethod
    def validate_owner_fernet_key(cls, value: SecretStr | None) -> SecretStr | None:
        if not _has_secret(value):
            return value
        assert value is not None
        try:
            decoded = base64.urlsafe_b64decode(value.get_secret_value().encode("ascii"))
        except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
            raise ValueError("owner settings key must be a Fernet base64 key") from exc
        if len(decoded) != 32:
            raise ValueError("owner settings key must decode to exactly 32 bytes")
        return value

    @property
    def test_chat_allowlist(self) -> tuple[int, ...]:
        try:
            return tuple(
                int(item.strip())
                for item in self.test_chat_allowlist_raw.split(",")
                if item.strip()
            )
        except ValueError as exc:
            raise ValueError("TEST_CHAT_ALLOWLIST must contain numeric chat IDs") from exc

    @property
    def admin_trusted_proxy_cidrs(self) -> tuple[str, ...]:
        return tuple(
            item.strip() for item in self.admin_trusted_proxy_cidrs_raw.split(",") if item.strip()
        )

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().startswith(("postgresql://", "postgresql+asyncpg://")):
            raise ValueError("DATABASE_URL must use PostgreSQL")
        return value

    @model_validator(mode="after")
    def validate_safety_boundaries(self) -> Settings:
        local = self.environment in {Environment.LOCAL, Environment.SIMULATION}
        database_managed = self.owner_config_source is OwnerConfigSource.DATABASE
        if self.homework_enabled and self.service_role not in {"bot", "unspecified"}:
            raise ValueError("manual homework capability is restricted to the bot role")
        if (
            self.alert_provider is AlertProvider.FAKE
            and not local
            and self.service_role in {"alert-worker", "unspecified"}
            and not database_managed
        ):
            raise ValueError("fake alert provider is restricted to local/simulation")
        if (
            self.service_role in {"alert-worker", "unspecified"}
            and self.alert_provider is AlertProvider.ALERTS_IN_UA
            and not _has_secret(self.alerts_in_ua_token)
            and not database_managed
        ):
            raise ValueError("alerts.in.ua provider requires ALERTS_IN_UA_TOKEN")
        messaging_role = self.service_role in {"bot", "sender-worker", "unspecified"}
        if (
            messaging_role
            and self.telegram_transport is TelegramTransport.LIVE
            and not _has_secret(self.telegram_bot_token)
            and not database_managed
        ):
            raise ValueError("live Telegram transport requires TELEGRAM_BOT_TOKEN")
        if self.service_role in {"bot", "schedule-worker", "sender-worker"} and not local:
            if not _has_secret(self.lesson_link_fernet_key):
                raise ValueError("production bot/workers require stable LESSON_LINK_FERNET_KEY")
        if self.telegram_transport is TelegramTransport.LIVE and local and not database_managed:
            raise ValueError("live Telegram transport is forbidden in local/simulation")
        if (
            self.environment is Environment.STAGING
            and self.telegram_transport is TelegramTransport.LIVE
            and messaging_role
            and not self.test_chat_allowlist
            and not database_managed
        ):
            raise ValueError("staging live Telegram transport requires TEST_CHAT_ALLOWLIST")
        if not local:
            if not self.database_url.get_secret_value().startswith("postgresql+asyncpg://"):
                raise ValueError("staging/production requires postgresql+asyncpg DATABASE_URL")
            if self.allow_scaffold_services:
                raise ValueError("scaffold services are forbidden in staging/production")
            if self.admin_backend is AdminBackend.LOCAL_SQLITE:
                raise ValueError("local SQLite admin backend is forbidden in staging/production")
        if self.service_role == "admin-web" and not local:
            if self.admin_backend is not AdminBackend.POSTGRES:
                raise ValueError("production admin-web requires ADMIN_BACKEND=postgres")
            if not _has_secret(self.admin_ip_hash_key):
                raise ValueError("production admin-web requires stable ADMIN_IP_HASH_KEY")
            if not self.host_metrics_url:
                raise ValueError("production admin-web requires HOST_METRICS_URL")
            if not self.admin_bootstrap_artifact_path:
                raise ValueError("production admin-web requires ADMIN_BOOTSTRAP_ARTIFACT_PATH")
        if not self.admin_cookie_secure and not local:
            raise ValueError("insecure admin cookies are restricted to local/simulation")
        if self.environment is Environment.PRODUCTION:
            if (
                self.service_role in {"alert-worker", "unspecified"}
                and self.alert_provider is not AlertProvider.ALERTS_IN_UA
                and not database_managed
            ):
                raise ValueError("production alert worker requires the alerts.in.ua provider")
            if (
                messaging_role
                and self.telegram_transport is not TelegramTransport.LIVE
                and not database_managed
            ):
                raise ValueError("production messaging service requires live Telegram transport")
        if database_managed and self.service_role in {"bot", "sender-worker"}:
            if not _has_secret(self.owner_telegram_fernet_key):
                raise ValueError("database-managed messaging requires OWNER_TELEGRAM_FERNET_KEY")
        if database_managed and self.service_role == "alert-worker":
            if not _has_secret(self.owner_alerts_fernet_key):
                raise ValueError("database-managed alerts require OWNER_ALERTS_FERNET_KEY")
        if self.alert_stale_after_seconds <= self.alert_poll_interval_seconds:
            raise ValueError("stale threshold must exceed the provider poll interval")
        return self


def _has_secret(value: SecretStr | None) -> bool:
    return bool(value and value.get_secret_value().strip())


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]

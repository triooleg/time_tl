"""Versioned, encrypted owner-managed service configuration.

Saving configuration never activates it.  Activation is a separate operator gate so
an accidental form submission cannot enable live Telegram delivery.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from cryptography.fernet import Fernet, InvalidToken
from pydantic import SecretStr

_SENSITIVE_SETTING_PARTS = ("token", "secret", "password", "fernet", "key", "ciphertext")
MAX_ONBOARDING_TEXT_LENGTH = 1500
PRODUCTION_PREFLIGHT_RECEIPT_TTL = timedelta(minutes=10)
DEFAULT_ONBOARDING_TEXT = (
    "👋 Вітаємо у Time TL!\n\n"
    "Бот допомагає онлайн-учням не пропустити початок повітряної тривоги, "
    "її офіційне завершення та повернення до активного уроку за власним розкладом.\n\n"
    "Як користуватися:\n"
    "1. Оберіть режим сповіщень.\n"
    "2. Вкажіть свою територію.\n"
    "3. Створіть або імпортуйте розклад уроків.\n\n"
    "🚨 Під час тривоги негайно прямуйте до укриття та виконуйте офіційні вказівки.\n"
    "⚠️ Бот є допоміжним сервісом і не замінює офіційні джерела оповіщення."
)


def reject_sensitive_generic_settings(keys: Any) -> None:
    """Keep plaintext or ciphertext out of the legacy generic settings API."""

    for key in keys:
        normalized = str(key).lower()
        if any(part in normalized for part in _SENSITIVE_SETTING_PARTS):
            raise ValueError("sensitive values require the owner configuration store")


class OwnerSettingsError(RuntimeError):
    """Base error whose message is safe to show without secret material."""


class OwnerSettingsValidationError(OwnerSettingsError):
    pass


class OwnerSettingsConflictError(OwnerSettingsError):
    pass


class OwnerSettingsCryptoError(OwnerSettingsError):
    pass


def owner_settings_payload_sha256(payload: dict[str, Any]) -> str:
    """Bind an operator check to the exact encrypted JSON payload in PostgreSQL."""

    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class OwnerConfigurationUpdate:
    expected_revision: int
    environment: str
    alert_provider: str
    telegram_transport: str
    poll_interval_seconds: int
    stale_after_seconds: int
    staging_allowlist: tuple[int, ...] | None
    telegram_token: str | None = None
    alerts_in_ua_token: str | None = None
    clear_telegram_token: bool = False
    clear_alerts_in_ua_token: bool = False
    clear_staging_allowlist: bool = False
    onboarding_text: str | None = None


@dataclass(frozen=True)
class OwnerSettingsBundle:
    draft: dict[str, Any] | None
    active: dict[str, Any] | None
    latest_revision: int
    pending_revision: int | None
    active_revision: int | None
    updated_by: str | None = None
    updated_at: datetime | None = None
    previous_active: dict[str, Any] | None = None
    activation_status: dict[str, Any] | None = None


@dataclass(frozen=True)
class OwnerConfigurationView:
    revision: int
    activation_state: str
    active_revision: int | None
    environment: str
    alert_provider: str
    telegram_transport: str
    poll_interval_seconds: int
    stale_after_seconds: int
    staging_allowlist: tuple[int, ...]
    telegram_token_configured: bool
    alerts_in_ua_token_configured: bool
    ready_for_activation: bool
    validation_issues: tuple[str, ...]
    updated_by: str | None
    updated_at: datetime | None
    onboarding_text: str = DEFAULT_ONBOARDING_TEXT
    rollback_revision: int | None = None
    last_activation_result: str | None = None

    def public_dict(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "activation_state": self.activation_state,
            "active_revision": self.active_revision,
            "environment": self.environment,
            "alert_provider": self.alert_provider,
            "telegram_transport": self.telegram_transport,
            "poll_interval_seconds": self.poll_interval_seconds,
            "stale_after_seconds": self.stale_after_seconds,
            "staging_allowlist_count": len(self.staging_allowlist),
            "telegram_token_configured": self.telegram_token_configured,
            "alerts_in_ua_token_configured": self.alerts_in_ua_token_configured,
            "ready_for_activation": self.ready_for_activation,
            "validation_issues": list(self.validation_issues),
            "onboarding_text": self.onboarding_text,
            "rollback_revision": self.rollback_revision,
            "last_activation_result": self.last_activation_result,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


@dataclass(frozen=True, repr=False)
class ActivationCandidate:
    revision: int
    environment: str
    alert_provider: str
    telegram_transport: str
    poll_interval_seconds: int
    stale_after_seconds: int
    staging_allowlist: tuple[int, ...]
    telegram_token: SecretStr | None
    alerts_in_ua_token: SecretStr | None
    onboarding_text: str = DEFAULT_ONBOARDING_TEXT


class OwnerSettingsStore(Protocol):
    async def owner_settings_bundle(self) -> OwnerSettingsBundle: ...

    async def save_owner_settings_draft(
        self, payload: dict[str, Any], expected_revision: int, actor: str
    ) -> OwnerSettingsBundle: ...

    async def request_owner_settings_activation(
        self, expected_revision: int, actor: str
    ) -> OwnerSettingsBundle: ...

    async def mark_owner_settings_active(
        self,
        revision: int,
        actor: str,
        evidence_id: str,
        evidence_summary: dict[str, int | str] | None = None,
    ) -> OwnerSettingsBundle: ...

    async def record_owner_settings_preflight_failure(
        self, revision: int, actor: str, failure_code: str
    ) -> OwnerSettingsBundle: ...

    async def store_production_preflight_receipt(
        self,
        revision: int,
        manifest_fingerprint: str,
        pending_payload_sha256: str,
        migration_head: str,
        issued_at: datetime,
        expires_at: datetime,
        actor: str,
        evidence_summary: dict[str, int | str],
    ) -> None: ...

    async def activate_with_production_preflight_receipt(
        self,
        revision: int,
        manifest_fingerprint: str,
        migration_head: str,
        actor: str,
        now: datetime,
    ) -> OwnerSettingsBundle: ...

    async def rollback_owner_settings_active(
        self, expected_active_revision: int, actor: str
    ) -> OwnerSettingsBundle: ...


class OwnerSettingsService:
    """Validate and encrypt owner configuration before persistence."""

    def __init__(
        self,
        store: OwnerSettingsStore,
        telegram_fernet_key: str | bytes | None,
        alerts_fernet_key: str | bytes | None,
        *,
        default_poll: int = 15,
        default_stale: int = 120,
    ) -> None:
        self._telegram_fernet = self._build_fernet(telegram_fernet_key)
        self._alerts_fernet = self._build_fernet(alerts_fernet_key)
        self._store = store
        self._default_poll = default_poll
        self._default_stale = default_stale

    async def view(self) -> OwnerConfigurationView:
        return self._view(await self._store.owner_settings_bundle())

    async def save(self, command: OwnerConfigurationUpdate, actor: str) -> OwnerConfigurationView:
        if command.expected_revision < 0:
            raise OwnerSettingsValidationError("Неверная версия конфигурации.")
        if command.clear_telegram_token and command.telegram_token:
            raise OwnerSettingsValidationError(
                "Нельзя одновременно заменить и удалить Telegram token."
            )
        if command.clear_alerts_in_ua_token and command.alerts_in_ua_token:
            raise OwnerSettingsValidationError(
                "Нельзя одновременно заменить и удалить alerts.in.ua token."
            )

        current = await self._store.owner_settings_bundle()
        previous = current.draft or {}
        telegram_cipher = previous.get("telegram_token_ciphertext")
        alerts_cipher = previous.get("alerts_in_ua_token_ciphertext")
        if command.clear_telegram_token:
            telegram_cipher = None
        elif command.telegram_token and command.telegram_token.strip():
            telegram_cipher = self._encrypt("telegram", command.telegram_token.strip())
        if command.clear_alerts_in_ua_token:
            alerts_cipher = None
        elif command.alerts_in_ua_token and command.alerts_in_ua_token.strip():
            alerts_cipher = self._encrypt("alerts", command.alerts_in_ua_token.strip())

        payload: dict[str, Any] = {
            "environment": command.environment,
            "alert_provider": command.alert_provider,
            "telegram_transport": command.telegram_transport,
            "poll_interval_seconds": command.poll_interval_seconds,
            "stale_after_seconds": command.stale_after_seconds,
            "staging_allowlist": (
                []
                if command.clear_staging_allowlist
                else list(command.staging_allowlist)
                if command.staging_allowlist is not None
                else list(previous.get("staging_allowlist", []))
            ),
            "telegram_token_ciphertext": telegram_cipher,
            "alerts_in_ua_token_ciphertext": alerts_cipher,
            "onboarding_text": (
                self._onboarding_text(previous)
                if command.onboarding_text is None
                else self._normalize_onboarding_text(command.onboarding_text)
            ),
        }
        # Drafts may be incomplete; validation issues are returned to the owner and
        # block activation instead of preventing safe incremental setup.
        self._issues(payload)
        saved = await self._store.save_owner_settings_draft(
            payload, command.expected_revision, actor
        )
        return self._view(saved)

    async def request_activation(
        self, expected_revision: int, actor: str
    ) -> OwnerConfigurationView:
        current = await self._store.owner_settings_bundle()
        view = self._view(current)
        if view.revision != expected_revision:
            raise OwnerSettingsConflictError("Конфигурация была изменена. Обновите страницу.")
        if not view.ready_for_activation:
            raise OwnerSettingsValidationError("Исправьте настройки перед запросом активации.")
        return self._view(
            await self._store.request_owner_settings_activation(expected_revision, actor)
        )

    async def activation_candidate(self, revision: int) -> ActivationCandidate:
        bundle = await self._store.owner_settings_bundle()
        return self._activation_candidate(bundle, revision)

    async def production_activation_candidate(
        self, revision: int
    ) -> tuple[ActivationCandidate, str]:
        """Return one candidate and the hash of the same raw encrypted payload."""

        bundle = await self._store.owner_settings_bundle()
        candidate = self._activation_candidate(bundle, revision)
        assert bundle.draft is not None
        return candidate, owner_settings_payload_sha256(bundle.draft)

    def _activation_candidate(
        self, bundle: OwnerSettingsBundle, revision: int
    ) -> ActivationCandidate:
        if bundle.pending_revision != revision or not bundle.draft:
            raise OwnerSettingsConflictError("Запрошенная версия не ожидает активации.")
        view = self._view(bundle)
        if not view.ready_for_activation:
            raise OwnerSettingsValidationError("Конфигурация не готова к активации.")
        return self._candidate(bundle.draft, revision, "operator")

    async def store_production_preflight_receipt(
        self,
        revision: int,
        manifest_fingerprint: str,
        pending_payload_sha256: str,
        migration_head: str,
        issued_at: datetime,
        expires_at: datetime,
        evidence_summary: dict[str, int | str],
    ) -> None:
        if (
            issued_at.tzinfo is None
            or expires_at.tzinfo is None
            or expires_at <= issued_at
            or expires_at - issued_at > PRODUCTION_PREFLIGHT_RECEIPT_TTL
        ):
            raise OwnerSettingsValidationError("Срок действия проверки недействителен.")
        await self._store.store_production_preflight_receipt(
            revision,
            manifest_fingerprint,
            pending_payload_sha256,
            migration_head,
            issued_at,
            expires_at,
            "operator",
            evidence_summary,
        )

    async def mark_production_active(
        self,
        revision: int,
        manifest_fingerprint: str,
        migration_head: str,
        now: datetime,
    ) -> OwnerConfigurationView:
        """Atomically consume a valid receipt while activating its exact payload."""

        return self._view(
            await self._store.activate_with_production_preflight_receipt(
                revision,
                manifest_fingerprint,
                migration_head,
                "operator",
                now,
            )
        )

    async def active_runtime_config(
        self, expected_environment: str, service_role: str
    ) -> ActivationCandidate:
        bundle = await self._store.owner_settings_bundle()
        if bundle.active_revision is None or bundle.active is None:
            raise OwnerSettingsConflictError("Нет активной конфигурации.")
        payload = bundle.active
        if str(payload.get("environment")) != expected_environment:
            raise OwnerSettingsValidationError(
                "Активная конфигурация предназначена для другой среды."
            )
        issues = self._issues(payload)
        if issues:
            raise OwnerSettingsValidationError("Активная конфигурация недействительна.")
        return self._candidate(payload, bundle.active_revision, service_role)

    async def mark_active(
        self,
        revision: int,
        actor: str,
        evidence_id: str,
        evidence_summary: dict[str, int | str] | None = None,
    ) -> OwnerConfigurationView:
        if not evidence_id.strip():
            raise OwnerSettingsValidationError("Для активации требуется идентификатор проверки.")
        await self.activation_candidate(revision)
        return self._view(
            await self._store.mark_owner_settings_active(
                revision,
                actor,
                evidence_id.strip(),
                evidence_summary,
            )
        )

    async def record_preflight_failure(
        self, revision: int, actor: str, failure_code: str
    ) -> OwnerConfigurationView:
        if failure_code not in {
            "telegram_get_me_failed",
            "alerts_full_snapshot_failed",
            "preflight_failed",
        }:
            failure_code = "preflight_failed"
        return self._view(
            await self._store.record_owner_settings_preflight_failure(revision, actor, failure_code)
        )

    async def rollback(
        self, expected_active_revision: int, expected_environment: str, actor: str
    ) -> OwnerConfigurationView:
        bundle = await self._store.owner_settings_bundle()
        if bundle.active_revision != expected_active_revision:
            raise OwnerSettingsConflictError("Активная версия изменилась. Обновите страницу.")
        previous = bundle.previous_active
        if previous is None:
            raise OwnerSettingsValidationError("Нет предыдущей активной версии.")
        if str(previous.get("environment")) != expected_environment:
            raise OwnerSettingsValidationError("Предыдущая версия относится к другой среде.")
        if self._issues(previous):
            raise OwnerSettingsValidationError("Предыдущая версия больше не действительна.")
        return self._view(
            await self._store.rollback_owner_settings_active(expected_active_revision, actor)
        )

    def _view(self, bundle: OwnerSettingsBundle) -> OwnerConfigurationView:
        payload = bundle.draft or {
            "environment": "simulation",
            "alert_provider": "fake",
            "telegram_transport": "disabled",
            "poll_interval_seconds": self._default_poll,
            "stale_after_seconds": self._default_stale,
            "staging_allowlist": [],
            "telegram_token_ciphertext": None,
            "alerts_in_ua_token_ciphertext": None,
            "onboarding_text": DEFAULT_ONBOARDING_TEXT,
        }
        # Prove the configured key can decrypt existing values, but discard plaintext.
        if payload.get("telegram_token_ciphertext") and self._telegram_fernet is not None:
            self._decrypt("telegram", str(payload["telegram_token_ciphertext"]))
        if payload.get("alerts_in_ua_token_ciphertext") and self._alerts_fernet is not None:
            self._decrypt("alerts", str(payload["alerts_in_ua_token_ciphertext"]))
        issues = self._issues(payload)
        state = "DRAFT"
        if bundle.active_revision == bundle.latest_revision and bundle.latest_revision > 0:
            state = "ACTIVE"
        elif bundle.pending_revision == bundle.latest_revision and bundle.latest_revision > 0:
            state = "PENDING_ACTIVATION"
        return OwnerConfigurationView(
            revision=bundle.latest_revision,
            activation_state=state,
            active_revision=bundle.active_revision,
            environment=str(payload["environment"]),
            alert_provider=str(payload["alert_provider"]),
            telegram_transport=str(payload["telegram_transport"]),
            poll_interval_seconds=int(payload["poll_interval_seconds"]),
            stale_after_seconds=int(payload["stale_after_seconds"]),
            staging_allowlist=tuple(int(item) for item in payload.get("staging_allowlist", [])),
            telegram_token_configured=bool(payload.get("telegram_token_ciphertext")),
            alerts_in_ua_token_configured=bool(payload.get("alerts_in_ua_token_ciphertext")),
            ready_for_activation=not issues,
            validation_issues=issues,
            updated_by=bundle.updated_by,
            updated_at=bundle.updated_at,
            onboarding_text=self._onboarding_text(payload),
            rollback_revision=(
                int(bundle.previous_active.get("revision", 0))
                if bundle.previous_active and int(bundle.previous_active.get("revision", 0)) > 0
                else None
            ),
            last_activation_result=(
                str(bundle.activation_status.get("result")) if bundle.activation_status else None
            ),
        )

    def _issues(self, payload: dict[str, Any]) -> tuple[str, ...]:
        issues: list[str] = []
        environment = str(payload.get("environment", ""))
        provider = str(payload.get("alert_provider", ""))
        transport = str(payload.get("telegram_transport", ""))
        if environment not in {"simulation", "staging", "production"}:
            issues.append("Выберите допустимую среду.")
        if provider not in {"fake", "alerts_in_ua"}:
            issues.append("Выберите допустимый источник тревог.")
        if transport not in {"disabled", "live"}:
            issues.append("Выберите допустимый режим Telegram.")
        try:
            poll = int(str(payload.get("poll_interval_seconds", "")))
            stale = int(str(payload.get("stale_after_seconds", "")))
        except (TypeError, ValueError):
            poll, stale = 0, 0
        if not 15 <= poll <= 300:
            issues.append("Интервал опроса должен быть от 15 до 300 секунд.")
        if not max(30, 2 * poll) <= stale <= 3600:
            issues.append(
                "Порог устаревания должен быть не меньше 2 x интервала и не больше 3600 секунд."
            )
        allowlist = payload.get("staging_allowlist", [])
        if not isinstance(allowlist, list) or any(
            not isinstance(item, int) or isinstance(item, bool) or item <= 0 for item in allowlist
        ):
            issues.append("Allowlist должен содержать только положительные Telegram chat ID.")
        if environment == "simulation":
            if provider != "fake":
                issues.append("В SIMULATION разрешён только fake provider.")
            if transport != "disabled":
                issues.append("В SIMULATION Telegram должен быть отключён.")
        if environment in {"staging", "production"} and provider != "alerts_in_ua":
            issues.append("STAGING и PRODUCTION требуют alerts.in.ua.")
        if provider == "alerts_in_ua" and not payload.get("alerts_in_ua_token_ciphertext"):
            issues.append("Добавьте токен alerts.in.ua.")
        if transport == "live" and not payload.get("telegram_token_ciphertext"):
            issues.append("Добавьте Telegram Bot Token.")
        if environment in {"staging", "production"} and transport != "live":
            issues.append("STAGING и PRODUCTION требуют live Telegram transport.")
        if environment == "staging" and transport == "live" and not allowlist:
            issues.append("Для STAGING требуется allowlist тестовых chat ID.")
        if environment == "production" and allowlist:
            issues.append("Для PRODUCTION очистите STAGING allowlist.")
        try:
            self._onboarding_text(payload)
        except OwnerSettingsValidationError as exc:
            issues.append(str(exc))
        return tuple(issues)

    @staticmethod
    def _normalize_onboarding_text(value: str | None) -> str:
        if value is None or not value.strip():
            return DEFAULT_ONBOARDING_TEXT
        normalized = value.replace("\r\n", "\n").replace("\r", "\n").strip()
        if len(normalized) > MAX_ONBOARDING_TEXT_LENGTH:
            raise OwnerSettingsValidationError(
                "Краткая инструкция должна содержать не более "
                f"{MAX_ONBOARDING_TEXT_LENGTH} символов."
            )
        if any(ord(char) < 32 and char not in {"\n", "\t"} for char in normalized):
            raise OwnerSettingsValidationError("Краткая инструкция содержит недопустимые символы.")
        return normalized

    @classmethod
    def _onboarding_text(cls, payload: dict[str, Any]) -> str:
        value = payload.get("onboarding_text")
        return cls._normalize_onboarding_text(value if isinstance(value, str) else None)

    @staticmethod
    def _build_fernet(value: str | bytes | None) -> Fernet | None:
        if value is None:
            return None
        try:
            key = value.encode("ascii") if isinstance(value, str) else value
            return Fernet(key)
        except (UnicodeEncodeError, ValueError) as exc:
            raise OwnerSettingsCryptoError("Ключ шифрования настроек недействителен.") from exc

    def _fernet_for(self, kind: str) -> Fernet:
        fernet = self._telegram_fernet if kind == "telegram" else self._alerts_fernet
        if fernet is None:
            raise OwnerSettingsCryptoError("Требуемый ключ шифрования недоступен.")
        return fernet

    def _encrypt(self, kind: str, value: str) -> str:
        return self._fernet_for(kind).encrypt(value.encode("utf-8")).decode("ascii")

    def _decrypt(self, kind: str, value: str) -> str:
        try:
            return self._fernet_for(kind).decrypt(value.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeDecodeError, UnicodeEncodeError) as exc:
            raise OwnerSettingsCryptoError(
                "Не удалось расшифровать сохранённые настройки."
            ) from exc

    def _secret(self, kind: str, value: Any) -> SecretStr | None:
        return SecretStr(self._decrypt(kind, str(value))) if value else None

    def _candidate(
        self, payload: dict[str, Any], revision: int, service_role: str
    ) -> ActivationCandidate:
        telegram_required = service_role in {"operator", "bot", "sender-worker"}
        alerts_required = service_role in {"operator", "alert-worker"}
        return ActivationCandidate(
            revision=revision,
            environment=str(payload["environment"]),
            alert_provider=str(payload["alert_provider"]),
            telegram_transport=str(payload["telegram_transport"]),
            poll_interval_seconds=int(payload["poll_interval_seconds"]),
            stale_after_seconds=int(payload["stale_after_seconds"]),
            staging_allowlist=tuple(int(item) for item in payload.get("staging_allowlist", [])),
            telegram_token=(
                self._secret("telegram", payload.get("telegram_token_ciphertext"))
                if telegram_required
                else None
            ),
            alerts_in_ua_token=(
                self._secret("alerts", payload.get("alerts_in_ua_token_ciphertext"))
                if alerts_required
                else None
            ),
            onboarding_text=self._onboarding_text(payload),
        )

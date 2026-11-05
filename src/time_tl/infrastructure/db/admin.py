from __future__ import annotations

import hashlib
import hmac
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from time_tl.admin.security import (
    FAILURE_WINDOW,
    LOCKOUT_AFTER,
    LOCKOUT_DURATION,
    SESSION_ABSOLUTE_TTL,
    Session,
    new_token,
    token_hash,
)
from time_tl.admin.settings import (
    PRODUCTION_PREFLIGHT_RECEIPT_TTL,
    OwnerSettingsBundle,
    OwnerSettingsConflictError,
    owner_settings_payload_sha256,
    reject_sensitive_generic_settings,
)

from .models import (
    AdminAccount,
    AdminLoginAttempt,
    AdminSession,
    AlertPollState,
    AlertSnapshot,
    AuditLog,
    OutboxMessage,
    RuntimeSetting,
    ScheduleProfile,
    User,
    WorkerHeartbeat,
)


@dataclass(frozen=True)
class AdminAccountRecord:
    username: str
    password_hash: str
    must_change_password: bool
    bootstrap_expires_at: datetime | None


class BackupControlConflict(ValueError):
    """Invalid or stale backup intent; no mutation has been committed."""


def _backup_integer(value: Any, minimum: int, maximum: int = 2**63 - 1) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise BackupControlConflict("Недопустимое значение параметра копий.")
    return value


def _backup_text(value: Any, pattern: str) -> str:
    if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
        raise BackupControlConflict("Недопустимый идентификатор копии.")
    return value


def _backup_time(value: Any) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            raise BackupControlConflict("Недопустимое время операции.") from None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise BackupControlConflict("Недопустимое время операции.")
    return value


def _backup_result(value: Any) -> dict[str, Any]:
    """Keep a small typed allowlist, never persist arbitrary host error text."""
    if not isinstance(value, dict):
        raise BackupControlConflict("Недопустимый результат проверки.")
    safe: dict[str, Any] = {}
    for key, item in value.items():
        if key == "artifact_sha256":
            safe[key] = _backup_text(item, r"[0-9a-f]{64}")
        elif key == "verified_at":
            safe[key] = _backup_time(item).isoformat()
        elif key == "schema":
            safe[key] = _backup_text(item, r"[0-9]{4}_[a-z0-9_]{1,80}")
        elif key in {"size_bytes", "table_count"}:
            safe[key] = _backup_integer(item, 0, 2**63 - 1 if key == "size_bytes" else 10000)
        elif key == "reason_code":
            if not isinstance(item, str) or item not in {
                "VERIFY_PASSED",
                "VERIFY_REJECTED",
                "RESTORE_REQUIRES_OPERATOR",
                "EXPIRED",
                "CANCELLED",
                "IDENTITY_CHANGED",
                "CATALOGUE_CHANGED",
                "ARTIFACT_INVALID",
                "INTERNAL_ERROR",
            }:
                raise BackupControlConflict("Недопустимый код результата.")
            safe[key] = item
    return safe


def _backup_control(value: Any) -> dict[str, Any]:
    if value is None:
        return {"version": 0, "retention_days": 7, "request": None}
    if not isinstance(value, dict):
        raise BackupControlConflict("Настройки копий повреждены.")
    control: dict[str, Any] = {
        "version": _backup_integer(value.get("version"), 0),
        "retention_days": _backup_integer(value.get("retention_days"), 1, 90),
        "request": None,
    }
    request = value.get("request")
    if request is None:
        return control
    if (
        not isinstance(request, dict)
        or not isinstance(request.get("kind"), str)
        or request.get("kind") not in {"verify", "restore"}
    ):
        raise BackupControlConflict("Запрос копии повреждён.")
    status = request.get("status")
    if not isinstance(status, str) or status not in {
        "PENDING",
        "CLAIMED",
        "PASS",
        "REJECTED",
        "REQUIRES_OPERATOR",
    }:
        raise BackupControlConflict("Статус копии повреждён.")
    if request["kind"] == "restore" and status == "PASS":
        raise BackupControlConflict("Восстановление требует оператора.")
    requested_at = _backup_time(request.get("requested_at"))
    expires_at = _backup_time(request.get("expires_at"))
    if expires_at - requested_at != timedelta(minutes=15):
        raise BackupControlConflict("Срок запроса копии повреждён.")
    safe_request = {
        "request_id": _backup_text(request.get("request_id"), r"[0-9a-f]{32}"),
        "kind": request["kind"],
        "artifact_id": _backup_text(request.get("artifact_id"), r"[0-9a-f]{32}"),
        "catalogue_version": _backup_text(request.get("catalogue_version"), r"[0-9a-f]{64}"),
        "expected_active_revision": _backup_integer(request.get("expected_active_revision"), 1),
        "expected_image": _backup_text(
            request.get("expected_image"),
            r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,254}@sha256:[0-9a-f]{64}",
        ),
        "requested_at": requested_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "status": status,
        "result": _backup_result(request.get("result", {})),
        "claimed_at": None,
        "finished_at": None,
    }
    for key in ("claimed_at", "finished_at"):
        if request.get(key) is not None:
            stamp = _backup_time(request[key])
            if stamp < requested_at:
                raise BackupControlConflict("Время запроса копии повреждено.")
            safe_request[key] = stamp.isoformat()
    claimed = safe_request["claimed_at"]
    finished = safe_request["finished_at"]
    if (
        (status == "PENDING" and (claimed is not None or finished is not None))
        or (status == "CLAIMED" and (claimed is None or finished is not None))
        or (status in {"PASS", "REQUIRES_OPERATOR"} and claimed is None)
        or (status in {"PASS", "REJECTED", "REQUIRES_OPERATOR"} and finished is None)
        or (status == "REQUIRES_OPERATOR" and request["kind"] != "restore")
        or (claimed is not None and _backup_time(claimed) >= expires_at)
        or (
            claimed is not None
            and finished is not None
            and _backup_time(finished) < _backup_time(claimed)
        )
    ):
        raise BackupControlConflict("Состояние запроса копии повреждено.")
    control["request"] = safe_request
    return control


class AsyncPostgresAdminRepository:
    _BACKUP_CONTROL_KEY = "backup_control"
    _OWNER_LOCK_KEY = 0x54494D45544C4346
    _OWNER_STATE_KEY = "owner_configuration_state"
    _OWNER_DRAFT_KEY = "owner_configuration_draft"
    _OWNER_ACTIVE_KEY = "owner_configuration_active"
    _OWNER_PREVIOUS_ACTIVE_KEY = "owner_configuration_previous_active"
    _OWNER_ACTIVATION_STATUS_KEY = "owner_configuration_activation_status"
    _PRODUCTION_PREFLIGHT_RECEIPT_KEY = "owner_configuration_production_preflight_receipt"

    def __init__(self, factory: async_sessionmaker[AsyncSession], *, ip_hash_key: bytes) -> None:
        if len(ip_hash_key) < 32:
            raise ValueError("admin IP hash key must contain at least 32 bytes")
        self._factory = factory
        self._ip_hash_key = ip_hash_key

    def _ip_hash(self, value: str) -> str:
        return hmac.new(self._ip_hash_key, value.encode(), hashlib.sha256).hexdigest()

    async def account(self, username: str = "admin") -> AdminAccountRecord | None:
        async with self._factory() as session:
            row = await session.get(AdminAccount, username)
            return self._account(row) if row else None

    async def create_bootstrap(
        self, password_hash: str, expires_at: datetime, username: str = "admin"
    ) -> None:
        async with self._factory() as session:
            async with session.begin():
                if await session.get(AdminAccount, username) is not None:
                    raise RuntimeError("admin account already exists")
                session.add(
                    AdminAccount(
                        username=username,
                        password_hash=password_hash,
                        must_change_password=True,
                        bootstrap_expires_at=expires_at,
                        password_changed_at=datetime.now(expires_at.tzinfo),
                    )
                )

    @staticmethod
    def bootstrap_is_valid(account: AdminAccountRecord, now: datetime) -> bool:
        return (
            not account.must_change_password
            or account.bootstrap_expires_at is None
            or now < account.bootstrap_expires_at
        )

    async def change_password(self, username: str, password_hash: str, now: datetime) -> None:
        async with self._factory() as session:
            async with session.begin():
                row = (
                    await session.execute(
                        select(AdminAccount)
                        .where(AdminAccount.username == username)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if row is None:
                    raise LookupError(username)
                row.password_hash = password_hash
                row.must_change_password = False
                row.bootstrap_expires_at = None
                row.password_changed_at = now
                await session.execute(delete(AdminSession).where(AdminSession.username == username))
                session.add(
                    AuditLog(
                        actor=username,
                        action="change_password",
                        result="success",
                        target_type="admin_account",
                        target_id=username,
                        redacted_details={},
                    )
                )

    async def lockout_remaining(
        self, username: str, source_ip: str, now: datetime
    ) -> timedelta | None:
        cutoff = now - FAILURE_WINDOW
        source = self._ip_hash(source_ip)
        async with self._factory() as session:
            attempts = (
                (
                    await session.execute(
                        select(AdminLoginAttempt.attempted_at)
                        .where(
                            AdminLoginAttempt.username == username,
                            AdminLoginAttempt.source_ip_hash == source,
                            AdminLoginAttempt.succeeded.is_(False),
                            AdminLoginAttempt.attempted_at >= cutoff,
                        )
                        .order_by(AdminLoginAttempt.attempted_at)
                    )
                )
                .scalars()
                .all()
            )
        if len(attempts) < LOCKOUT_AFTER:
            return None
        locked_until = attempts[-1] + LOCKOUT_DURATION
        return max(locked_until - now, timedelta()) if now < locked_until else None

    async def record_failure(self, username: str, source_ip: str, now: datetime) -> None:
        async with self._factory() as session:
            async with session.begin():
                session.add(
                    AdminLoginAttempt(
                        username=username,
                        source_ip_hash=self._ip_hash(source_ip),
                        succeeded=False,
                        attempted_at=now,
                    )
                )

    async def reset_failures(self, username: str, source_ip: str) -> None:
        async with self._factory() as session:
            async with session.begin():
                await session.execute(
                    delete(AdminLoginAttempt).where(
                        AdminLoginAttempt.username == username,
                        AdminLoginAttempt.source_ip_hash == self._ip_hash(source_ip),
                        AdminLoginAttempt.succeeded.is_(False),
                    )
                )

    async def create_session(self, username: str, must_change: bool, now: datetime) -> Session:
        token, csrf = new_token(), new_token()
        value = Session(token, csrf, username, must_change, now, now, now + SESSION_ABSOLUTE_TTL)
        async with self._factory() as session:
            async with session.begin():
                session.add(
                    AdminSession(
                        token_hash=token_hash(token),
                        username=username,
                        csrf_token=csrf,
                        created_at=now,
                        last_seen_at=now,
                        absolute_expires_at=value.absolute_expires_at,
                    )
                )
        return value

    async def get_session(self, token: str, now: datetime, touch: bool = True) -> Session | None:
        async with self._factory() as session:
            async with session.begin():
                row = (
                    await session.execute(
                        select(AdminSession)
                        .where(AdminSession.token_hash == token_hash(token))
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if row is None:
                    return None
                account = await session.get(AdminAccount, row.username)
                if account is None:
                    return None
                value = Session(
                    token,
                    row.csrf_token,
                    row.username,
                    account.must_change_password,
                    row.created_at,
                    row.last_seen_at,
                    row.absolute_expires_at,
                )
                if value.expired(now):
                    await session.delete(row)
                    return None
                if touch:
                    row.last_seen_at = now
                return value

    async def delete_session(self, token: str) -> None:
        async with self._factory() as session:
            async with session.begin():
                await session.execute(
                    delete(AdminSession).where(AdminSession.token_hash == token_hash(token))
                )

    async def delete_other_sessions(self, username: str, current_token: str) -> None:
        async with self._factory() as session:
            async with session.begin():
                await session.execute(
                    delete(AdminSession).where(
                        AdminSession.username == username,
                        AdminSession.token_hash != token_hash(current_token),
                    )
                )

    async def get_setting(self, key: str, default: Any = None) -> Any:
        reject_sensitive_generic_settings((key,))
        async with self._factory() as session:
            row = await session.get(RuntimeSetting, key)
            return row.value if row else default

    async def set_settings(self, values: dict[str, Any], owner: str, action: str) -> None:
        if self._BACKUP_CONTROL_KEY in values:
            raise BackupControlConflict("Используйте версионные настройки копий.")
        reject_sensitive_generic_settings(values)
        async with self._factory() as session:
            async with session.begin():
                before = {}
                for key, value in values.items():
                    row = (
                        await session.execute(
                            select(RuntimeSetting)
                            .where(RuntimeSetting.key == key)
                            .with_for_update()
                        )
                    ).scalar_one_or_none()
                    before[key] = row.value if row else None
                    if row is None:
                        session.add(RuntimeSetting(key=key, value=value, updated_by=owner))
                    else:
                        row.value = value
                        row.updated_by = owner
                session.add(
                    AuditLog(
                        actor=owner,
                        action=action,
                        result="success",
                        target_type="runtime_settings",
                        redacted_details={key: "changed" for key in values},
                    )
                )

    async def get_backup_control(self) -> dict[str, Any]:
        async with self._factory() as session:
            row = await session.get(RuntimeSetting, self._BACKUP_CONTROL_KEY)
            return _backup_control(row.value if row is not None else None)

    async def _locked_backup_control(self, session: AsyncSession) -> dict[str, Any]:
        # Row locks alone cannot serialize the first write to an absent JSONB key.
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext('time_tl:backup_control')::bigint)")
        )
        row = await session.get(RuntimeSetting, self._BACKUP_CONTROL_KEY)
        return _backup_control(row.value if row is not None else None)

    async def _save_backup_control(
        self, session: AsyncSession, control: dict[str, Any], actor: str, action: str
    ) -> dict[str, Any]:
        control["version"] += 1
        await self._upsert_runtime_setting(session, self._BACKUP_CONTROL_KEY, control, actor)
        request = control["request"]
        details = {"version": control["version"], "retention_days": control["retention_days"]}
        if request is not None:
            details.update({"kind": request["kind"], "status": request["status"]})
        session.add(
            AuditLog(
                actor=actor,
                action=action,
                result="success",
                target_type="backup_control",
                redacted_details=details,
            )
        )
        return control

    @staticmethod
    def _backup_actor(actor: str) -> str:
        return _backup_text(actor, r"[A-Za-z0-9_.@-]{1,80}")

    @staticmethod
    def _backup_cas(control: dict[str, Any], expected_version: int) -> None:
        if control["version"] != _backup_integer(expected_version, 0):
            raise BackupControlConflict("Настройки копий изменились. Обновите страницу.")

    async def set_backup_policy(
        self, retention_days: int, expected_version: int, actor: str
    ) -> dict[str, Any]:
        days = _backup_integer(retention_days, 1, 90)
        _backup_integer(expected_version, 0)
        actor = self._backup_actor(actor)
        async with self._factory() as session:
            async with session.begin():
                control = await self._locked_backup_control(session)
                self._backup_cas(control, expected_version)
                control["retention_days"] = days
                return await self._save_backup_control(
                    session, control, actor, "backup_policy_saved"
                )

    async def request_backup_operation(
        self,
        kind: str,
        artifact_id: str,
        catalogue_version: str,
        expected_active_revision: int,
        expected_image: str,
        expected_version: int,
        actor: str,
        now: datetime,
    ) -> dict[str, Any]:
        if not isinstance(kind, str) or kind not in {"verify", "restore"}:
            raise BackupControlConflict("Неизвестная операция копии.")
        now = _backup_time(now)
        actor = self._backup_actor(actor)
        _backup_integer(expected_version, 0)
        request = _backup_control(
            {
                "version": 0,
                "retention_days": 7,
                "request": {
                    "request_id": uuid.uuid4().hex,
                    "kind": kind,
                    "artifact_id": artifact_id,
                    "catalogue_version": catalogue_version,
                    "expected_active_revision": expected_active_revision,
                    "expected_image": expected_image,
                    "requested_at": now.isoformat(),
                    "expires_at": (now + timedelta(minutes=15)).isoformat(),
                    "status": "PENDING",
                },
            }
        )["request"]
        async with self._factory() as session:
            async with session.begin():
                await self._lock_owner_settings(session)
                control = await self._locked_backup_control(session)
                self._backup_cas(control, expected_version)
                current = control["request"]
                if current is not None and current["status"] in {
                    "PENDING",
                    "CLAIMED",
                    "REQUIRES_OPERATOR",
                }:
                    raise BackupControlConflict("Завершите или отмените предыдущий запрос копии.")
                await self._backup_active_revision(session, expected_active_revision)
                control["request"] = request
                return await self._save_backup_control(session, control, actor, "backup_requested")

    async def _backup_active_revision(self, session: AsyncSession, revision: int) -> None:
        state = await session.get(RuntimeSetting, self._OWNER_STATE_KEY)
        active = await session.get(RuntimeSetting, self._OWNER_ACTIVE_KEY)
        if (
            state is None
            or active is None
            or not isinstance(state.value, dict)
            or not isinstance(active.value, dict)
            or state.value.get("active_revision") != revision
            or active.value.get("revision") != revision
        ):
            raise BackupControlConflict("Активная конфигурация изменилась.")

    async def claim_backup_operation(self, request_id: str, now: datetime) -> dict[str, Any]:
        request_id = _backup_text(request_id, r"[0-9a-f]{32}")
        now = _backup_time(now)
        async with self._factory() as session:
            async with session.begin():
                await self._lock_owner_settings(session)
                control = await self._locked_backup_control(session)
                request = control["request"]
                if request is None or request["request_id"] != request_id:
                    raise BackupControlConflict("Запрос копии не найден.")
                if (
                    request["status"] != "PENDING"
                    or now < _backup_time(request["requested_at"])
                    or now >= _backup_time(request["expires_at"])
                ):
                    raise BackupControlConflict("Запрос уже принят или срок запроса истёк.")
                await self._backup_active_revision(session, request["expected_active_revision"])
                request["status"] = "CLAIMED"
                request["claimed_at"] = now.isoformat()
                await self._save_backup_control(session, control, "backup-host", "backup_claimed")
                return cast(dict[str, Any], request)

    async def finish_backup_operation(
        self, request_id: str, status: str, result: dict[str, Any], now: datetime
    ) -> dict[str, Any]:
        request_id = _backup_text(request_id, r"[0-9a-f]{32}")
        now = _backup_time(now)
        if not isinstance(status, str) or status not in {"PASS", "REJECTED", "REQUIRES_OPERATOR"}:
            raise BackupControlConflict("Недопустимый итог операции копии.")
        safe_result = _backup_result(result)
        async with self._factory() as session:
            async with session.begin():
                control = await self._locked_backup_control(session)
                request = control["request"]
                if request is None or request["request_id"] != request_id:
                    raise BackupControlConflict("Запрос копии не найден.")
                if request["status"] != "CLAIMED" and not (
                    request["status"] == "PENDING" and status == "REJECTED"
                ):
                    raise BackupControlConflict("Операция копии не принята или уже завершена.")
                if (
                    (request["kind"] == "restore" and status == "PASS")
                    or (request["kind"] == "verify" and status == "REQUIRES_OPERATOR")
                    or now < _backup_time(request["claimed_at"] or request["requested_at"])
                ):
                    raise BackupControlConflict("Недопустимый переход операции копии.")
                request.update(status=status, result=safe_result, finished_at=now.isoformat())
                return await self._save_backup_control(
                    session, control, "backup-host", "backup_finished"
                )

    async def cancel_backup_operation(
        self, request_id: str, expected_version: int, actor: str, now: datetime
    ) -> dict[str, Any]:
        request_id = _backup_text(request_id, r"[0-9a-f]{32}")
        _backup_integer(expected_version, 0)
        actor = self._backup_actor(actor)
        now = _backup_time(now)
        async with self._factory() as session:
            async with session.begin():
                control = await self._locked_backup_control(session)
                self._backup_cas(control, expected_version)
                request = control["request"]
                if (
                    request is None
                    or request["request_id"] != request_id
                    or request["status"]
                    not in {
                        "PENDING",
                        "REQUIRES_OPERATOR",
                    }
                ):
                    raise BackupControlConflict(
                        "Принятую или завершённую операцию отменить нельзя."
                    )
                if now < _backup_time(request["requested_at"]):
                    raise BackupControlConflict("Недопустимое время отмены.")
                request.update(
                    status="REJECTED",
                    result={"reason_code": "CANCELLED"},
                    finished_at=now.isoformat(),
                )
                return await self._save_backup_control(session, control, actor, "backup_cancelled")

    async def owner_settings_bundle(self) -> OwnerSettingsBundle:
        async with self._factory() as session:
            async with session.begin():
                await self._lock_owner_settings(session)
                return await self._owner_settings_bundle(session)

    async def save_owner_settings_draft(
        self, payload: dict[str, Any], expected_revision: int, actor: str
    ) -> OwnerSettingsBundle:
        async with self._factory() as session:
            async with session.begin():
                await self._lock_owner_settings(session)
                state_row = (
                    await session.execute(
                        select(RuntimeSetting)
                        .where(RuntimeSetting.key == self._OWNER_STATE_KEY)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                state = dict(state_row.value) if state_row else {}
                latest = int(state.get("latest_revision", 0))
                if latest != expected_revision:
                    raise OwnerSettingsConflictError(
                        "Конфигурация была изменена. Обновите страницу."
                    )
                previous_row = await session.get(RuntimeSetting, self._OWNER_DRAFT_KEY)
                previous = dict(previous_row.value) if previous_row is not None else {}
                revision = latest + 1
                draft = dict(payload)
                draft["revision"] = revision
                audit_names = {
                    "environment": "environment",
                    "alert_provider": "alert_provider",
                    "telegram_transport": "telegram_transport",
                    "poll_interval_seconds": "poll_interval_seconds",
                    "stale_after_seconds": "stale_after_seconds",
                    "staging_allowlist": "staging_allowlist_presence",
                    "telegram_token_ciphertext": "telegram_token_presence",
                    "alerts_in_ua_token_ciphertext": "alerts_in_ua_token_presence",
                    "onboarding_text": "onboarding_text",
                }
                changed_fields = [
                    audit_name
                    for field, audit_name in audit_names.items()
                    if previous.get(field) != draft.get(field)
                ]
                await self._upsert_runtime_setting(session, self._OWNER_DRAFT_KEY, draft, actor)
                state.update(
                    {
                        "latest_revision": revision,
                        "pending_revision": None,
                        "active_revision": state.get("active_revision"),
                    }
                )
                await self._upsert_runtime_setting(session, self._OWNER_STATE_KEY, state, actor)
                session.add(
                    AuditLog(
                        actor=actor,
                        action="owner_configuration_saved",
                        result="success",
                        target_type="owner_configuration",
                        target_id=str(revision),
                        redacted_details={"changed_fields": changed_fields},
                    )
                )
        return await self.owner_settings_bundle()

    async def request_owner_settings_activation(
        self, expected_revision: int, actor: str
    ) -> OwnerSettingsBundle:
        async with self._factory() as session:
            async with session.begin():
                await self._lock_owner_settings(session)
                state_row = (
                    await session.execute(
                        select(RuntimeSetting)
                        .where(RuntimeSetting.key == self._OWNER_STATE_KEY)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if state_row is None:
                    raise OwnerSettingsConflictError("Сначала сохраните конфигурацию.")
                state = dict(state_row.value)
                latest = int(state.get("latest_revision", 0))
                draft = await session.get(RuntimeSetting, self._OWNER_DRAFT_KEY)
                if (
                    latest != expected_revision
                    or draft is None
                    or int(draft.value.get("revision", 0)) != expected_revision
                ):
                    raise OwnerSettingsConflictError(
                        "Конфигурация была изменена. Обновите страницу."
                    )
                state["pending_revision"] = expected_revision
                state_row.value = state
                state_row.updated_by = actor
                session.add(
                    AuditLog(
                        actor=actor,
                        action="owner_configuration_activation_requested",
                        result="success",
                        target_type="owner_configuration",
                        target_id=str(expected_revision),
                        redacted_details={},
                    )
                )
        return await self.owner_settings_bundle()

    async def mark_owner_settings_active(
        self,
        revision: int,
        actor: str,
        evidence_id: str,
        evidence_summary: dict[str, int | str] | None = None,
    ) -> OwnerSettingsBundle:
        """Activate one preflighted revision and retain one rollback target."""

        async with self._factory() as session:
            async with session.begin():
                await self._lock_owner_settings(session)
                state_row = (
                    await session.execute(
                        select(RuntimeSetting)
                        .where(RuntimeSetting.key == self._OWNER_STATE_KEY)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                draft = await session.get(RuntimeSetting, self._OWNER_DRAFT_KEY)
                if state_row is None or draft is None:
                    raise OwnerSettingsConflictError("Нет конфигурации для активации.")
                state = dict(state_row.value)
                if (
                    state.get("pending_revision") != revision
                    or int(draft.value.get("revision", 0)) != revision
                ):
                    raise OwnerSettingsConflictError("Версия не ожидает активации.")
                current_active = await session.get(RuntimeSetting, self._OWNER_ACTIVE_KEY)
                if current_active is not None:
                    await self._upsert_runtime_setting(
                        session,
                        self._OWNER_PREVIOUS_ACTIVE_KEY,
                        dict(current_active.value),
                        actor,
                    )
                await self._upsert_runtime_setting(
                    session, self._OWNER_ACTIVE_KEY, dict(draft.value), actor
                )
                safe_summary = {
                    key: value
                    for key, value in (evidence_summary or {}).items()
                    if key in {"check_profile", "covered_region_count", "allowlist_count"}
                    and isinstance(value, (str, int))
                    and not isinstance(value, bool)
                }
                activation_status = {
                    "revision": revision,
                    "result": "passed",
                    "evidence_id": evidence_id,
                    **safe_summary,
                }
                await self._upsert_runtime_setting(
                    session,
                    self._OWNER_ACTIVATION_STATUS_KEY,
                    activation_status,
                    actor,
                )
                state["active_revision"] = revision
                state["pending_revision"] = None
                state_row.value = state
                state_row.updated_by = actor
                session.add(
                    AuditLog(
                        actor=actor,
                        action="owner_configuration_activated",
                        result="success",
                        target_type="owner_configuration",
                        target_id=str(revision),
                        redacted_details={
                            "evidence_id": evidence_id,
                            "evidence_id_sha256": hashlib.sha256(
                                evidence_id.encode("utf-8")
                            ).hexdigest(),
                            **safe_summary,
                        },
                    )
                )
        return await self.owner_settings_bundle()

    async def record_owner_settings_preflight_failure(
        self, revision: int, actor: str, failure_code: str
    ) -> OwnerSettingsBundle:
        safe_code = (
            failure_code
            if failure_code
            in {
                "telegram_get_me_failed",
                "alerts_full_snapshot_failed",
                "preflight_failed",
            }
            else "preflight_failed"
        )
        async with self._factory() as session:
            async with session.begin():
                await self._lock_owner_settings(session)
                await self._upsert_runtime_setting(
                    session,
                    self._OWNER_ACTIVATION_STATUS_KEY,
                    {"revision": revision, "result": "failed", "failure_code": safe_code},
                    actor,
                )
                session.add(
                    AuditLog(
                        actor=actor,
                        action="owner_configuration_preflight_failed",
                        result="failure",
                        target_type="owner_configuration",
                        target_id=str(revision),
                        redacted_details={"failure_code": safe_code},
                    )
                )
        return await self.owner_settings_bundle()

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
    ) -> None:
        """Persist a secret-free, short-lived proof for the exact pending payload."""

        lowercase_hex = frozenset("0123456789abcdef")
        if (
            len(manifest_fingerprint) != 64
            or not set(manifest_fingerprint) <= lowercase_hex
            or len(pending_payload_sha256) != 64
            or not set(pending_payload_sha256) <= lowercase_hex
            or not migration_head.strip()
            or issued_at.tzinfo is None
            or expires_at.tzinfo is None
            or expires_at <= issued_at
            or expires_at - issued_at > PRODUCTION_PREFLIGHT_RECEIPT_TTL
        ):
            raise OwnerSettingsConflictError("Квитанция проверки недействительна.")
        async with self._factory() as session:
            async with session.begin():
                await self._lock_owner_settings(session)
                state_row = await session.get(RuntimeSetting, self._OWNER_STATE_KEY)
                draft_row = await session.get(RuntimeSetting, self._OWNER_DRAFT_KEY)
                if state_row is None or draft_row is None:
                    raise OwnerSettingsConflictError("Нет конфигурации для проверки.")
                state = dict(state_row.value)
                draft = dict(draft_row.value)
                if (
                    state.get("pending_revision") != revision
                    or int(draft.get("revision", 0)) != revision
                    or owner_settings_payload_sha256(draft) != pending_payload_sha256
                ):
                    raise OwnerSettingsConflictError(
                        "Конфигурация изменилась во время проверки."
                    )
                safe_summary = {
                    key: value
                    for key, value in evidence_summary.items()
                    if key in {"check_profile", "covered_region_count", "allowlist_count"}
                    and isinstance(value, (str, int))
                    and not isinstance(value, bool)
                }
                receipt: dict[str, Any] = {
                    "revision": revision,
                    "manifest_fingerprint": manifest_fingerprint,
                    "pending_payload_sha256": pending_payload_sha256,
                    "migration_head": migration_head,
                    "status": "passed",
                    "issued_at": issued_at.isoformat(),
                    "expires_at": expires_at.isoformat(),
                    "consumed_at": None,
                    **safe_summary,
                }
                await self._upsert_runtime_setting(
                    session,
                    self._PRODUCTION_PREFLIGHT_RECEIPT_KEY,
                    receipt,
                    actor,
                )
                session.add(
                    AuditLog(
                        actor=actor,
                        action="production_preflight_receipt_stored",
                        result="success",
                        target_type="owner_configuration",
                        target_id=str(revision),
                        redacted_details={
                            "manifest_fingerprint": manifest_fingerprint,
                            "pending_payload_sha256": pending_payload_sha256,
                            "migration_head": migration_head,
                            "expires_at": expires_at.isoformat(),
                            **safe_summary,
                        },
                    )
                )

    async def activate_with_production_preflight_receipt(
        self,
        revision: int,
        manifest_fingerprint: str,
        migration_head: str,
        actor: str,
        now: datetime,
    ) -> OwnerSettingsBundle:
        """Verify and consume one receipt in the same transaction as activation."""

        async with self._factory() as session:
            async with session.begin():
                await self._lock_owner_settings(session)
                state_row = await session.get(RuntimeSetting, self._OWNER_STATE_KEY)
                draft_row = await session.get(RuntimeSetting, self._OWNER_DRAFT_KEY)
                receipt_row = (
                    await session.execute(
                        select(RuntimeSetting)
                        .where(RuntimeSetting.key == self._PRODUCTION_PREFLIGHT_RECEIPT_KEY)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if state_row is None or draft_row is None or receipt_row is None:
                    raise OwnerSettingsConflictError("Нет действующей квитанции проверки.")
                state = dict(state_row.value)
                draft = dict(draft_row.value)
                receipt = dict(receipt_row.value)
                heads = (
                    await session.execute(
                        text(
                            "SELECT array_agg(version_num ORDER BY version_num) "
                            "FROM alembic_version"
                        )
                    )
                ).scalar_one()
                try:
                    issued_at = datetime.fromisoformat(str(receipt["issued_at"]))
                    expires_at = datetime.fromisoformat(str(receipt["expires_at"]))
                except (KeyError, TypeError, ValueError):
                    raise OwnerSettingsConflictError("Квитанция проверки повреждена.") from None
                payload_hash = owner_settings_payload_sha256(draft)
                if (
                    state.get("pending_revision") != revision
                    or int(draft.get("revision", 0)) != revision
                    or receipt.get("revision") != revision
                    or receipt.get("manifest_fingerprint") != manifest_fingerprint
                    or receipt.get("pending_payload_sha256") != payload_hash
                    or receipt.get("migration_head") != migration_head
                    or receipt.get("status") != "passed"
                    or receipt.get("consumed_at") is not None
                    or issued_at.tzinfo is None
                    or expires_at.tzinfo is None
                    or issued_at > now
                    or now >= expires_at
                    or heads != [migration_head]
                ):
                    raise OwnerSettingsConflictError(
                        "Квитанция проверки не соответствует текущему релизу."
                    )

                current_active = await session.get(RuntimeSetting, self._OWNER_ACTIVE_KEY)
                if current_active is not None:
                    await self._upsert_runtime_setting(
                        session,
                        self._OWNER_PREVIOUS_ACTIVE_KEY,
                        dict(current_active.value),
                        actor,
                    )
                await self._upsert_runtime_setting(
                    session, self._OWNER_ACTIVE_KEY, draft, actor
                )
                safe_summary = {
                    key: value
                    for key, value in receipt.items()
                    if key in {"check_profile", "covered_region_count", "allowlist_count"}
                    and isinstance(value, (str, int))
                    and not isinstance(value, bool)
                }
                evidence_id = f"production-preflight-v1-r{revision}-{manifest_fingerprint}"
                await self._upsert_runtime_setting(
                    session,
                    self._OWNER_ACTIVATION_STATUS_KEY,
                    {
                        "revision": revision,
                        "result": "passed",
                        "evidence_id": evidence_id,
                        **safe_summary,
                    },
                    actor,
                )
                state["active_revision"] = revision
                state["pending_revision"] = None
                state_row.value = state
                state_row.updated_by = actor
                receipt["status"] = "consumed"
                receipt["consumed_at"] = now.isoformat()
                receipt_row.value = receipt
                receipt_row.updated_by = actor
                session.add(
                    AuditLog(
                        actor=actor,
                        action="owner_configuration_activated",
                        result="success",
                        target_type="owner_configuration",
                        target_id=str(revision),
                        redacted_details={
                            "evidence_id_sha256": hashlib.sha256(
                                evidence_id.encode("utf-8")
                            ).hexdigest(),
                            "receipt_consumed": "yes",
                            "manifest_fingerprint": manifest_fingerprint,
                            "migration_head": migration_head,
                            **safe_summary,
                        },
                    )
                )
        return await self.owner_settings_bundle()

    async def rollback_owner_settings_active(
        self, expected_active_revision: int, actor: str
    ) -> OwnerSettingsBundle:
        async with self._factory() as session:
            async with session.begin():
                await self._lock_owner_settings(session)
                state_row = await session.get(RuntimeSetting, self._OWNER_STATE_KEY)
                active_row = await session.get(RuntimeSetting, self._OWNER_ACTIVE_KEY)
                previous_row = await session.get(RuntimeSetting, self._OWNER_PREVIOUS_ACTIVE_KEY)
                if state_row is None or active_row is None or previous_row is None:
                    raise OwnerSettingsConflictError("Немає попередньої активної версії.")
                state = dict(state_row.value)
                if int(state.get("active_revision", 0)) != expected_active_revision:
                    raise OwnerSettingsConflictError("Активна версія змінилася. Оновіть сторінку.")
                current = dict(active_row.value)
                previous = dict(previous_row.value)
                previous_revision = int(previous.get("revision", 0))
                if previous_revision <= 0:
                    raise OwnerSettingsConflictError("Попередня версія пошкоджена.")
                active_row.value = previous
                active_row.updated_by = actor
                previous_row.value = current
                previous_row.updated_by = actor
                state["active_revision"] = previous_revision
                state["pending_revision"] = None
                state_row.value = state
                state_row.updated_by = actor
                await self._upsert_runtime_setting(
                    session,
                    self._OWNER_ACTIVATION_STATUS_KEY,
                    {
                        "revision": previous_revision,
                        "result": "rolled_back",
                        "from_revision": expected_active_revision,
                    },
                    actor,
                )
                session.add(
                    AuditLog(
                        actor=actor,
                        action="owner_configuration_rolled_back",
                        result="success",
                        target_type="owner_configuration",
                        target_id=str(previous_revision),
                        redacted_details={"from_revision": expected_active_revision},
                    )
                )
        return await self.owner_settings_bundle()

    @staticmethod
    async def _lock_owner_settings(session: AsyncSession) -> None:
        await session.execute(
            select(func.pg_advisory_xact_lock(AsyncPostgresAdminRepository._OWNER_LOCK_KEY))
        )

    @staticmethod
    async def _upsert_runtime_setting(
        session: AsyncSession, key: str, value: Any, actor: str
    ) -> None:
        row = await session.get(RuntimeSetting, key)
        if row is None:
            session.add(RuntimeSetting(key=key, value=value, updated_by=actor))
        else:
            row.value = value
            row.updated_by = actor

    async def _owner_settings_bundle(self, session: AsyncSession) -> OwnerSettingsBundle:
        state_row = await session.get(RuntimeSetting, self._OWNER_STATE_KEY)
        draft_row = await session.get(RuntimeSetting, self._OWNER_DRAFT_KEY)
        active_row = await session.get(RuntimeSetting, self._OWNER_ACTIVE_KEY)
        previous_active_row = await session.get(RuntimeSetting, self._OWNER_PREVIOUS_ACTIVE_KEY)
        activation_status_row = await session.get(RuntimeSetting, self._OWNER_ACTIVATION_STATUS_KEY)
        state = dict(state_row.value) if state_row else {}
        return OwnerSettingsBundle(
            draft=dict(draft_row.value) if draft_row else None,
            active=dict(active_row.value) if active_row else None,
            latest_revision=int(state.get("latest_revision", 0)),
            pending_revision=(
                int(state["pending_revision"])
                if state.get("pending_revision") is not None
                else None
            ),
            active_revision=(
                int(state["active_revision"]) if state.get("active_revision") is not None else None
            ),
            updated_by=draft_row.updated_by if draft_row else None,
            updated_at=draft_row.updated_at if draft_row else None,
            previous_active=(dict(previous_active_row.value) if previous_active_row else None),
            activation_status=(
                dict(activation_status_row.value) if activation_status_row else None
            ),
        )

    async def dashboard_aggregates(self) -> dict[str, Any]:
        async with self._factory() as session:
            users = await session.scalar(select(func.count()).select_from(User))
            profiles = await session.scalar(select(func.count()).select_from(ScheduleProfile))
            mode_rows = (
                await session.execute(select(User.mode, func.count()).group_by(User.mode))
            ).all()
            modes: dict[str | None, int] = {mode: count for mode, count in mode_rows}
            alert_rows = (
                await session.execute(
                    select(AlertSnapshot.state, func.count()).group_by(AlertSnapshot.state)
                )
            ).all()
            alerts: dict[str, int] = {state: count for state, count in alert_rows}
            poll_state = await session.get(AlertPollState, 1)
            pending_filter = (
                OutboxMessage.delivered_at.is_(None),
                OutboxMessage.dead_at.is_(None),
            )
            pending = await session.scalar(
                select(func.count()).select_from(OutboxMessage).where(*pending_filter)
            )
            oldest = await session.scalar(
                select(func.min(OutboxMessage.created_at)).where(*pending_filter)
            )
            delivered = await session.scalar(
                select(func.count())
                .select_from(OutboxMessage)
                .where(OutboxMessage.delivered_at.is_not(None))
            )
            dead = await session.scalar(
                select(func.count())
                .select_from(OutboxMessage)
                .where(OutboxMessage.dead_at.is_not(None))
            )
            retrying = await session.scalar(
                select(func.count())
                .select_from(OutboxMessage)
                .where(*pending_filter, OutboxMessage.attempts > 0)
            )
            workers = (
                (
                    await session.execute(
                        select(WorkerHeartbeat).order_by(WorkerHeartbeat.worker_name)
                    )
                )
                .scalars()
                .all()
            )
            provider: dict[str, Any]
            if poll_state is None:
                provider_state = "UNKNOWN"
                provider = {"state": provider_state, "persistence": "missing"}
            else:
                if poll_state.circuit_open or alerts.get("STALE", 0):
                    provider_state = "STALE"
                elif alerts.get("ACTIVE", 0):
                    provider_state = "ACTIVE"
                elif alerts.get("UNKNOWN", 0):
                    provider_state = "UNKNOWN"
                elif alerts.get("CLEAR_CONFIRMED", 0) and poll_state.last_success_at:
                    provider_state = "CLEAR_CONFIRMED"
                else:
                    provider_state = "UNKNOWN"
                provider = {
                    "state": provider_state,
                    "persistence": "available",
                    "fetch_revision": poll_state.fetch_revision,
                    "circuit_open": poll_state.circuit_open,
                    "consecutive_failures": poll_state.consecutive_failures,
                    "retry_not_before": (
                        poll_state.retry_not_before.isoformat()
                        if poll_state.retry_not_before
                        else None
                    ),
                    "last_success_at": (
                        poll_state.last_success_at.isoformat()
                        if poll_state.last_success_at
                        else None
                    ),
                    "http_counts": None,
                    "rate_budget": None,
                }
            return {
                "users": users or 0,
                "profiles": profiles or 0,
                "modes": modes,
                "alert_states": alerts,
                "outbox_pending": pending or 0,
                "outbox_delivered": delivered or 0,
                "outbox_dead": dead or 0,
                "outbox_retrying": retrying or 0,
                "outbox_oldest_at": oldest.isoformat() if oldest else None,
                "provider": provider,
                "workers": [
                    {
                        "name": r.worker_name,
                        "instance_id": r.instance_id,
                        "status": r.status,
                        "heartbeat_at": r.heartbeat_at.isoformat(),
                        "details": r.details,
                    }
                    for r in workers
                ],
            }

    @staticmethod
    def _account(row: AdminAccount) -> AdminAccountRecord:
        return AdminAccountRecord(
            row.username, row.password_hash, row.must_change_password, row.bootstrap_expires_at
        )

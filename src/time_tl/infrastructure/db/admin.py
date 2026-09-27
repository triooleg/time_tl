from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select
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
    OwnerSettingsBundle,
    OwnerSettingsConflictError,
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


class AsyncPostgresAdminRepository:
    _OWNER_LOCK_KEY = 0x54494D45544C4346
    _OWNER_STATE_KEY = "owner_configuration_state"
    _OWNER_DRAFT_KEY = "owner_configuration_draft"
    _OWNER_ACTIVE_KEY = "owner_configuration_active"
    _OWNER_PREVIOUS_ACTIVE_KEY = "owner_configuration_previous_active"
    _OWNER_ACTIVATION_STATUS_KEY = "owner_configuration_activation_status"

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
        async with self._factory() as session:
            row = await session.get(RuntimeSetting, key)
            return row.value if row else default

    async def set_settings(self, values: dict[str, Any], owner: str, action: str) -> None:
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

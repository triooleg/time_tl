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

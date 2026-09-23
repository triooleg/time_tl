from __future__ import annotations

import re
import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from fastapi.testclient import TestClient

from time_tl.admin.async_app import create_async_admin_app
from time_tl.admin.security import Session, hash_password, new_token, utcnow
from time_tl.config import Settings


@dataclass
class Account:
    username: str
    password_hash: str
    must_change_password: bool
    bootstrap_expires_at: datetime | None


class FakeMetrics:
    def collect(self) -> dict[str, Any]:
        return {"cpu": {"value": 12, "unit": "%", "status": "healthy"}}


class FakeAsyncRepository:
    def __init__(self, *, must_change: bool = True) -> None:
        self.owner = Account(
            "admin",
            hash_password("bootstrap-password-long"),
            must_change,
            utcnow() + timedelta(hours=1) if must_change else None,
        )
        self.sessions: dict[str, Session] = {}
        self.failures: list[tuple[str, str, datetime]] = []
        self.settings: dict[str, Any] = {}
        self.audit_calls: list[tuple[dict[str, Any], str, str]] = []

    async def account(self, username: str = "admin") -> Account | None:
        return self.owner if username == "admin" else None

    @staticmethod
    def bootstrap_is_valid(account: Account, now: datetime) -> bool:
        return account.bootstrap_expires_at is None or now < account.bootstrap_expires_at

    async def lockout_remaining(
        self, username: str, source_ip: str, now: datetime
    ) -> timedelta | None:
        recent = [
            at
            for user, ip, at in self.failures
            if user == username and ip == source_ip and at >= now - timedelta(minutes=15)
        ]
        return timedelta(minutes=30) if len(recent) >= 5 else None

    async def record_failure(self, username: str, source_ip: str, now: datetime) -> None:
        self.failures.append((username, source_ip, now))

    async def reset_failures(self, username: str, source_ip: str) -> None:
        self.failures = [
            row for row in self.failures if not (row[0] == username and row[1] == source_ip)
        ]

    async def create_session(self, username: str, must_change: bool, now: datetime) -> Session:
        value = Session(
            new_token(), new_token(), username, must_change, now, now, now + timedelta(hours=12)
        )
        self.sessions[value.token] = value
        return value

    async def get_session(self, token: str, now: datetime, touch: bool = True) -> Session | None:
        value = self.sessions.get(token)
        if value is None or value.expired(now):
            return None
        return Session(
            value.token,
            value.csrf_token,
            value.username,
            self.owner.must_change_password,
            value.created_at,
            now if touch else value.last_seen_at,
            value.absolute_expires_at,
        )

    async def delete_session(self, token: str) -> None:
        self.sessions.pop(token, None)

    async def change_password(self, username: str, password_hash: str, now: datetime) -> None:
        self.owner.password_hash = password_hash
        self.owner.must_change_password = False
        self.owner.bootstrap_expires_at = None
        self.sessions.clear()

    async def get_setting(self, key: str, default: Any = None) -> Any:
        return self.settings.get(key, default)

    async def set_settings(self, values: dict[str, Any], owner: str, action: str) -> None:
        self.settings.update(values)
        self.audit_calls.append((values, owner, action))

    async def dashboard_aggregates(self) -> dict[str, Any]:
        return {
            "users": 8,
            "profiles": 11,
            "modes": {"alerts_only": 2},
            "alert_states": {"ACTIVE": 1},
            "outbox_pending": 3,
            "outbox_oldest_at": "2026-01-01T00:00:00+00:00",
            "outbox_delivered": 7,
            "outbox_dead": 1,
            "outbox_retrying": 2,
            "provider": {
                "state": "ACTIVE",
                "fetch_revision": 42,
                "consecutive_failures": 0,
                "token": "provider-secret-must-not-leak",
            },
            "workers": [
                {
                    "name": "sender-worker",
                    "instance_id": "private-instance",
                    "status": "healthy",
                    "heartbeat_at": "2026-01-01T00:00:00+00:00",
                    "details": {"token": "must-not-leak"},
                }
            ],
            "telegram_user_ids": [123],
        }


class AsyncEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = FakeAsyncRepository()
        self.settings = Settings(
            **{
                "DATABASE_URL": "postgresql+asyncpg://u:p@db/time_tl",
                "TIME_TL_ENV": "simulation",
                "SERVICE_ROLE": "admin-web",
                "ADMIN_BACKEND": "postgres",
                "ADMIN_COOKIE_SECURE": False,
            }
        )
        self.client = TestClient(create_async_admin_app(self.settings, self.repo, FakeMetrics()))

    @staticmethod
    def csrf(page: str) -> str:
        match = re.search(r'name="csrf" value="([^"]+)"', page)
        assert match
        return match.group(1)

    def login(self, password: str = "bootstrap-password-long"):
        page = self.client.get("/login")
        return self.client.post(
            "/login",
            data={"csrf": self.csrf(page.text), "username": "admin", "password": password},
            follow_redirects=False,
        )

    def test_unauthenticated_api_is_closed_and_has_security_headers(self) -> None:
        response = self.client.get("/api/dashboard")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json(), {"detail": "unauthorized"})
        self.assertEqual(response.headers["x-frame-options"], "DENY")
        self.assertEqual(
            response.headers["permissions-policy"], "camera=(), microphone=(), geolocation=()"
        )

    def test_login_forces_password_rotation_and_csrf(self) -> None:
        response = self.login()
        self.assertEqual(response.headers["location"], "/change-password")
        page = self.client.get("/change-password")
        denied = self.client.post(
            "/change-password",
            data={
                "csrf": "wrong",
                "password": "permanent-password-long",
                "confirmation": "permanent-password-long",
            },
        )
        self.assertEqual(denied.status_code, 403)
        changed = self.client.post(
            "/change-password",
            data={
                "csrf": self.csrf(page.text),
                "password": "permanent-password-long",
                "confirmation": "permanent-password-long",
            },
            follow_redirects=False,
        )
        self.assertEqual(changed.headers["location"], "/login?changed=1")
        self.assertFalse(self.repo.owner.must_change_password)
        self.assertFalse(self.repo.sessions)

    def test_dashboard_whitelists_aggregate_data_and_audits_settings(self) -> None:
        self.repo.owner.must_change_password = False
        self.repo.owner.bootstrap_expires_at = None
        response = self.login()
        self.assertEqual(response.headers["location"], "/")
        api = self.client.get("/api/dashboard")
        self.assertEqual(api.status_code, 200)
        raw = api.text
        self.assertNotIn("private-instance", raw)
        self.assertNotIn("must-not-leak", raw)
        self.assertNotIn("telegram_user_ids", raw)
        self.assertEqual(api.json()["aggregates"]["users"], 8)
        self.assertEqual(api.json()["provider"]["state"], "UNKNOWN")
        self.assertEqual(api.json()["provider"]["freshness"], "unavailable")
        self.assertEqual(api.json()["provider"]["fetch_revision"], 42)
        self.assertEqual(api.json()["aggregates"]["outbox_delivered"], 7)
        self.assertEqual(api.json()["provider"]["settings_apply_state"], "next_cycle")
        self.assertNotIn("provider-secret-must-not-leak", raw)

        page = self.client.get("/")
        saved = self.client.post(
            "/settings/provider",
            data={"csrf": self.csrf(page.text), "poll": "20", "stale": "60"},
            follow_redirects=False,
        )
        self.assertEqual(saved.status_code, 303)
        self.assertEqual(self.repo.settings, {"poll": 20, "stale": 60})
        self.assertEqual(self.repo.audit_calls[-1][2], "provider_settings")

    def test_missing_aggregates_remain_unavailable_instead_of_zero(self) -> None:
        from time_tl.admin.async_app import _safe_aggregates

        sanitized = _safe_aggregates({})
        for key in (
            "users",
            "profiles",
            "modes",
            "alert_states",
            "outbox_pending",
            "outbox_delivered",
            "outbox_dead",
            "outbox_retrying",
            "workers",
            "provider",
        ):
            self.assertIsNone(sanitized[key])

    def test_rejects_unsafe_provider_values(self) -> None:
        self.repo.owner.must_change_password = False
        self.repo.owner.bootstrap_expires_at = None
        self.login()
        page = self.client.get("/")
        response = self.client.post(
            "/settings/provider",
            data={"csrf": self.csrf(page.text), "poll": "14", "stale": "120"},
        )
        self.assertEqual(response.status_code, 422)

    def test_lockout_after_five_failures(self) -> None:
        statuses = [self.login("wrong").status_code for _ in range(5)]
        self.assertEqual(statuses[:4], [401] * 4)
        self.assertEqual(statuses[4], 429)


if __name__ == "__main__":
    unittest.main()

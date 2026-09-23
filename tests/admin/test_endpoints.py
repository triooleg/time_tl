from __future__ import annotations

import re
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from time_tl.admin.app import create_admin_app
from time_tl.admin.repository import LocalAdminRepository
from time_tl.admin.security import hash_password, utcnow
from time_tl.config import Settings


class EndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.repo = LocalAdminRepository(self.temp.name)
        secret_path = Path(self.temp.name) / "bootstrap.txt"
        secret_path.write_text("bootstrap-password-long", encoding="utf-8")
        self.repo.create_bootstrap(
            hash_password("bootstrap-password-long"),
            utcnow() + timedelta(hours=1),
            str(secret_path),
        )
        settings = Settings(
            **{
                "DATABASE_URL": "postgresql+asyncpg://u:p@db/time_tl",
                "TIME_TL_ENV": "simulation",
                "SERVICE_ROLE": "admin-web",
                "ADMIN_BACKEND": "local_sqlite",
                "ADMIN_STATE_DIR": self.temp.name,
                "ADMIN_COOKIE_SECURE": False,
            }
        )
        self.client = TestClient(create_admin_app(settings, self.repo))

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def csrf(page: str) -> str:
        match = re.search(r'name="csrf" value="([^"]+)"', page)
        assert match
        return match.group(1)

    def test_unauthenticated_api_reveals_no_data(self) -> None:
        response = self.client.get("/api/dashboard")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json(), {"detail": "unauthorized"})
        self.assertIn("frame-ancestors 'none'", response.headers["content-security-policy"])

    def test_login_forces_change_then_dashboard_and_csrf(self) -> None:
        login_page = self.client.get("/login")
        response = self.client.post(
            "/login",
            data={
                "csrf": self.csrf(login_page.text),
                "username": "admin",
                "password": "bootstrap-password-long",
            },
            follow_redirects=False,
        )
        self.assertEqual(response.headers["location"], "/change-password")
        change = self.client.get("/change-password")
        token = self.csrf(change.text)
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
                "csrf": token,
                "password": "permanent-password-long",
                "confirmation": "permanent-password-long",
            },
            follow_redirects=False,
        )
        self.assertEqual(changed.headers["location"], "/login?changed=1")
        self.assertFalse((Path(self.temp.name) / "bootstrap.txt").exists())

    def test_provider_validation_rejects_unsafe_interval(self) -> None:
        page = self.client.get("/login")
        self.client.post(
            "/login",
            data={
                "csrf": self.csrf(page.text),
                "username": "admin",
                "password": "bootstrap-password-long",
            },
        )
        change = self.client.get("/change-password")
        self.client.post(
            "/change-password",
            data={
                "csrf": self.csrf(change.text),
                "password": "permanent-password-long",
                "confirmation": "permanent-password-long",
            },
        )
        page = self.client.get("/login")
        self.client.post(
            "/login",
            data={
                "csrf": self.csrf(page.text),
                "username": "admin",
                "password": "permanent-password-long",
            },
        )
        dashboard = self.client.get("/")
        response = self.client.post(
            "/settings/provider",
            data={"csrf": self.csrf(dashboard.text), "poll": "14", "stale": "120"},
        )
        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()

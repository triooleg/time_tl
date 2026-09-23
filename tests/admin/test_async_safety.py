from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path

from fastapi import Request
from fastapi.testclient import TestClient

from tests.admin.test_async_app import FakeAsyncRepository, FakeMetrics
from time_tl.admin.async_app import AsyncMetricsCache, ProxyPolicy, create_async_admin_app
from time_tl.admin.security import utcnow
from time_tl.config import Settings


class BlockingMetrics:
    def __init__(self) -> None:
        self.calls = 0

    def collect(self):
        self.calls += 1
        time.sleep(0.12)
        return {"source_status": "available"}


class AsyncMetricsTests(unittest.IsolatedAsyncioTestCase):
    async def test_collection_runs_off_loop_and_cache_bounds_scrapes(self) -> None:
        source = BlockingMetrics()
        cache = AsyncMetricsCache(source, ttl_seconds=10)
        completed = False

        async def ticker() -> None:
            nonlocal completed
            await asyncio.sleep(0.02)
            completed = True

        result, _ = await asyncio.gather(cache.collect(), ticker())
        self.assertTrue(completed)
        self.assertEqual(result["source_status"], "available")
        self.assertEqual((await cache.collect())["source_status"], "available")
        self.assertEqual(source.calls, 1)


class ProxyPolicyTests(unittest.TestCase):
    @staticmethod
    def request(peer: str, forwarded: str | None = None) -> Request:
        headers = []
        if forwarded is not None:
            headers.append((b"x-forwarded-for", forwarded.encode("ascii")))
        return Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/",
                "headers": headers,
                "client": (peer, 12345),
                "server": ("admin", 8080),
                "scheme": "https",
                "query_string": b"",
            }
        )

    def test_forwarding_header_is_ignored_by_default(self) -> None:
        request = self.request("10.20.0.5", "198.51.100.17")
        self.assertEqual(ProxyPolicy().client_ip(request), "10.20.0.5")

    def test_only_configured_proxy_network_is_trusted(self) -> None:
        policy = ProxyPolicy.from_cidrs(("10.20.0.0/24",))
        request = self.request("10.20.0.5", "198.51.100.17, 10.20.0.4")
        self.assertEqual(policy.client_ip(request), "198.51.100.17")
        invalid = self.request("10.20.0.5", "not-an-ip")
        self.assertEqual(policy.client_ip(invalid), "10.20.0.5")
        with self.assertRaises(ValueError):
            ProxyPolicy.from_cidrs(("10.20.0.7/24",))


class BootstrapAndHelpLifecycleTests(unittest.TestCase):
    def settings(self) -> Settings:
        return Settings(
            **{
                "DATABASE_URL": "postgresql+asyncpg://u:p@db/time_tl",
                "TIME_TL_ENV": "simulation",
                "SERVICE_ROLE": "admin-web",
                "ADMIN_BACKEND": "postgres",
                "ADMIN_COOKIE_SECURE": False,
            }
        )

    @staticmethod
    def csrf(page: str) -> str:
        import re

        match = re.search(r'name="csrf" value="([^"]+)"', page)
        assert match
        return match.group(1)

    def test_bootstrap_artifact_survives_login_until_password_change(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            artifact = Path(folder) / "bootstrap.txt"
            artifact.write_text("bootstrap-password-long", encoding="utf-8")
            client = TestClient(
                create_async_admin_app(
                    self.settings(),
                    FakeAsyncRepository(),
                    FakeMetrics(),
                    bootstrap_artifact_path=artifact,
                )
            )
            page = client.get("/login")
            response = client.post(
                "/login",
                data={
                    "csrf": self.csrf(page.text),
                    "username": "admin",
                    "password": "bootstrap-password-long",
                },
                follow_redirects=False,
            )
            self.assertEqual(response.status_code, 303)
            self.assertTrue(artifact.exists())
            change_page = client.get("/change-password")
            changed = client.post(
                "/change-password",
                data={
                    "csrf": self.csrf(change_page.text),
                    "password": "permanent-password-long",
                    "confirmation": "permanent-password-long",
                },
                follow_redirects=False,
            )
            self.assertEqual(changed.status_code, 303)
            self.assertFalse(artifact.exists())

    def test_expired_bootstrap_attempt_deletes_plaintext_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            artifact = Path(folder) / "bootstrap.txt"
            artifact.write_text("bootstrap-password-long", encoding="utf-8")
            repo = FakeAsyncRepository()
            repo.owner.bootstrap_expires_at = utcnow()
            client = TestClient(
                create_async_admin_app(
                    self.settings(),
                    repo,
                    FakeMetrics(),
                    bootstrap_artifact_path=artifact,
                )
            )
            page = client.get("/login")
            response = client.post(
                "/login",
                data={
                    "csrf": self.csrf(page.text),
                    "username": "admin",
                    "password": "bootstrap-password-long",
                },
            )
            self.assertEqual(response.status_code, 401)
            self.assertFalse(artifact.exists())

    def test_async_help_routes_are_authenticated_and_escape_content(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "SETUP_BOT.md").write_text("<token> setup", encoding="utf-8")
            (root / "ADMIN_PANEL.md").write_text("admin help", encoding="utf-8")
            repo = FakeAsyncRepository(must_change=False)
            client = TestClient(
                create_async_admin_app(self.settings(), repo, FakeMetrics(), docs_root=root)
            )
            self.assertEqual(client.get("/help/setup", follow_redirects=False).status_code, 303)
            page = client.get("/login")
            client.post(
                "/login",
                data={
                    "csrf": self.csrf(page.text),
                    "username": "admin",
                    "password": "bootstrap-password-long",
                },
            )
            response = client.get("/help/setup")
            self.assertEqual(response.status_code, 200)
            self.assertIn("&lt;token&gt; setup", response.text)
            self.assertEqual(client.get("/help/other").status_code, 404)


if __name__ == "__main__":
    unittest.main()

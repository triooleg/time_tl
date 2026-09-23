from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from pydantic import ValidationError

from time_tl.config import Settings

BASE = {"DATABASE_URL": "postgresql+asyncpg://u:p@db/time_tl"}


class AdminContractTests(unittest.TestCase):
    def test_local_sqlite_backend_rejected_in_production(self) -> None:
        values = BASE | {
            "TIME_TL_ENV": "production",
            "SERVICE_ROLE": "admin-web",
            "ADMIN_BACKEND": "local_sqlite",
            "ADMIN_COOKIE_SECURE": True,
            "ALERT_PROVIDER": "fake",
            "TELEGRAM_TRANSPORT": "disabled",
        }
        with self.assertRaises(ValidationError):
            Settings(**values)

    def test_compose_secrets_are_least_privilege(self) -> None:
        compose = Path("docker-compose.yml").read_text(encoding="utf-8")
        base = compose.split("services:", 1)[0]
        self.assertNotIn("ALERTS_IN_UA_TOKEN", base)
        self.assertNotIn("TELEGRAM_BOT_TOKEN", base)
        schedule = compose.split("  schedule-worker:", 1)[1].split("  sender-worker:", 1)[0]
        admin = compose.split("  admin-web:", 1)[1].split("  postgres:", 1)[0]
        self.assertNotIn("ALERTS_IN_UA_TOKEN", schedule + admin)
        self.assertNotIn("TELEGRAM_BOT_TOKEN", schedule + admin)

    def test_ui_contains_contract_and_no_remote_assets(self) -> None:
        from time_tl.admin.app import _JS, _dashboard_html

        page = _dashboard_html("csrf", "simulation", 15, 120)
        for copy in [
            "Оновити дані",
            "UNKNOWN",
            "Метрики недоступні",
            "read-only host exporter",
            "Возобновить отправку",
            "Отмена",
            "Майстер налаштування сервісу",
            "bootstrap-only",
            "SIMULATION",
            "schedule-worker",
            "sender-worker",
        ]:
            self.assertIn(copy, page)
        self.assertNotIn('type="password"', page)
        self.assertNotIn("https://", page)
        self.assertNotIn("docker.sock", page)
        self.assertNotIn("TELEGRAM_BOT_TOKEN=", page)
        self.assertNotIn("ALERTS_IN_UA_TOKEN=", page)
        self.assertNotIn("LESSON_LINK_FERNET_KEY=", page)
        self.assertNotIn("ADMIN_IP_HASH_KEY=", page)

        configured = _dashboard_html(
            "csrf",
            "simulation",
            15,
            120,
            owner_setup=SimpleNamespace(
                revision=1,
                activation_state="DRAFT",
                active_revision=None,
                environment="staging",
                alert_provider="alerts_in_ua",
                telegram_transport="live",
                poll_interval_seconds=15,
                stale_after_seconds=120,
                staging_allowlist=(1,),
                telegram_token_configured=True,
                alerts_in_ua_token_configured=True,
                ready_for_activation=True,
                validation_issues=(),
            ),
        )
        self.assertIn('name="telegram_token"', configured)
        self.assertIn('name="alerts_in_ua_token"', configured)
        self.assertNotRegex(configured, r'name="telegram_token"[^>]*\svalue=')
        self.assertNotRegex(configured, r'name="alerts_in_ua_token"[^>]*\svalue=')
        self.assertNotIn("docker.sock", configured)
        self.assertNotIn("https://", configured)
        for aggregate in (
            "aggregates.users",
            "aggregates.profiles",
            "aggregates.modes",
            "aggregates.outbox_pending",
            "data.workers",
        ):
            self.assertIn(aggregate, _JS)
        self.assertNotIn("alerts.in.ua", _JS)

    def test_provider_settings_are_validated_in_repository_contract(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            from time_tl.admin.repository import LocalAdminRepository

            repo = LocalAdminRepository(folder)
            repo.set_settings({"poll": 15, "stale": 30}, "admin", "test")
            self.assertEqual(repo.get_setting("poll"), 15)


if __name__ == "__main__":
    unittest.main()

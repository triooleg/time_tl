from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.fernet import Fernet
from pydantic import SecretStr, ValidationError

from time_tl.admin.settings import ActivationCandidate
from time_tl.config import Settings
from time_tl.runtime.contracts import RuntimeContractError
from time_tl.runtime.services import (
    PostgresAlertRuntimeSettingsReader,
    RuntimeConfiguredAlertWorker,
    resolve_owner_runtime_settings,
)


class Reader:
    def __init__(self, result: tuple[int, int] | Exception) -> None:
        self.result = result
        self.calls = 0

    async def read(self) -> tuple[int, int]:
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeEngine:
    async def dispose(self) -> None:
        return None


class FakeOwnerSettingsService:
    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs

    async def active_runtime_config(
        self, expected_environment: str, service_role: str
    ) -> ActivationCandidate:
        assert expected_environment == "staging"
        assert service_role == "alert-worker"
        return ActivationCandidate(
            revision=7,
            environment="staging",
            alert_provider="alerts_in_ua",
            telegram_transport="disabled",
            poll_interval_seconds=30,
            stale_after_seconds=180,
            staging_allowlist=(),
            telegram_token=None,
            alerts_in_ua_token=SecretStr("official-provider-token"),
        )


class FakeRuntimeSession:
    def __init__(self, rows: dict[str, object]) -> None:
        self.rows = rows

    async def __aenter__(self) -> FakeRuntimeSession:
        return self

    async def __aexit__(self, *args: object) -> None:
        del args

    async def get(self, model: object, key: str) -> object | None:
        del model
        return self.rows.get(key)


class RuntimeAlertSettingsTests(unittest.IsolatedAsyncioTestCase):
    def worker(self, reader: Reader) -> RuntimeConfiguredAlertWorker:
        return RuntimeConfiguredAlertWorker(
            provider=object(),
            repository=object(),
            lease=object(),
            poll_interval_seconds=15,
            requests_per_minute=5,
            monotonic=lambda: 0.0,
            jitter=lambda _low, _high: 0.0,
            runtime_settings=reader,
            stale_after_seconds=120,
        )

    async def test_valid_pair_is_applied_between_cycles(self) -> None:
        reader = Reader((30, 180))
        worker = self.worker(reader)
        await worker._refresh_runtime_settings()
        self.assertEqual(30.0, worker.poll_interval_seconds)
        self.assertEqual(180, worker.stale_after_seconds)
        self.assertEqual(1, reader.calls)

    async def test_unavailable_settings_keep_last_safe_values(self) -> None:
        reader = Reader(RuntimeError("database unavailable"))
        worker = self.worker(reader)
        await worker._refresh_runtime_settings()
        self.assertEqual(15.0, worker.poll_interval_seconds)
        self.assertEqual(120, worker.stale_after_seconds)

    def test_database_managed_alert_worker_requires_alerts_key(self) -> None:
        with self.assertRaises(ValidationError):
            Settings(
                **{
                    "DATABASE_URL": "postgresql+asyncpg://u:p@db/time_tl",
                    "TIME_TL_ENV": "staging",
                    "SERVICE_ROLE": "alert-worker",
                    "OWNER_CONFIG_SOURCE": "database",
                    "ALERT_PROVIDER": "fake",
                    "TELEGRAM_TRANSPORT": "disabled",
                }
            )

    def test_database_managed_schedule_worker_needs_no_owner_secret_key(self) -> None:
        settings = Settings(
            **{
                "DATABASE_URL": "postgresql+asyncpg://u:p@db/time_tl",
                "TIME_TL_ENV": "staging",
                "SERVICE_ROLE": "schedule-worker",
                "OWNER_CONFIG_SOURCE": "database",
                "ALERT_PROVIDER": "fake",
                "TELEGRAM_TRANSPORT": "disabled",
                "LESSON_LINK_FERNET_KEY": "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=",
            }
        )
        self.assertEqual(settings.service_role, "schedule-worker")

    async def test_database_loader_revalidates_alias_dump_end_to_end(self) -> None:
        settings = Settings(
            **{
                "DATABASE_URL": "postgresql+asyncpg://u:p@db/time_tl",
                "TIME_TL_ENV": "staging",
                "SERVICE_ROLE": "alert-worker",
                "OWNER_CONFIG_SOURCE": "database",
                "OWNER_ALERTS_FERNET_KEY": Fernet.generate_key().decode("ascii"),
                "ALERT_PROVIDER": "fake",
                "TELEGRAM_TRANSPORT": "disabled",
            }
        )
        with (
            patch("time_tl.infrastructure.db.engine.create_engine", return_value=FakeEngine()),
            patch("time_tl.infrastructure.db.engine.create_session_factory", return_value=object()),
            patch(
                "time_tl.admin.settings.OwnerSettingsService",
                FakeOwnerSettingsService,
            ),
        ):
            resolved = await resolve_owner_runtime_settings(settings)

        self.assertEqual(resolved.alert_provider.value, "alerts_in_ua")
        self.assertEqual(resolved.alert_poll_interval_seconds, 30)
        self.assertEqual(resolved.alert_stale_after_seconds, 180)
        self.assertEqual(resolved.owner_config_revision, 7)
        assert resolved.alerts_in_ua_token is not None
        self.assertEqual(resolved.alerts_in_ua_token.get_secret_value(), "official-provider-token")

    async def test_alert_runtime_reader_refuses_cross_revision_drift(self) -> None:
        rows = {
            "owner_configuration_state": SimpleNamespace(value={"active_revision": 8}),
            "owner_configuration_active": SimpleNamespace(
                value={
                    "revision": 8,
                    "poll_interval_seconds": 30,
                    "stale_after_seconds": 180,
                }
            ),
        }

        def factory() -> FakeRuntimeSession:
            return FakeRuntimeSession(rows)

        reader = PostgresAlertRuntimeSettingsReader(
            factory, default_poll=15, default_stale=120, expected_revision=7
        )
        with self.assertRaises(RuntimeContractError) as raised:
            await reader.read()
        self.assertIn("controlled restart required", str(raised.exception))

    async def test_environment_managed_reader_keeps_startup_defaults(self) -> None:
        def factory() -> FakeRuntimeSession:
            raise AssertionError("environment-managed runtime must not read owner rows")

        reader = PostgresAlertRuntimeSettingsReader(factory, default_poll=15, default_stale=120)
        self.assertEqual(await reader.read(), (15, 120))


if __name__ == "__main__":
    unittest.main()

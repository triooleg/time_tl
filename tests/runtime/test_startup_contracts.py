from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from pydantic import SecretStr

from time_tl.config import (
    AlertProvider,
    Environment,
    Settings,
    TelegramTransport,
)
from time_tl.domain.alerts import RegionScope, SourceFailureKind
from time_tl.infrastructure.alerts import (
    AlertPollState,
    HttpResponse,
    OblastAlertsInUaAdapter,
)
from time_tl.runtime.contracts import (
    RuntimeCapabilities,
    RuntimeContractError,
    validate_runtime_contract,
)
from time_tl.runtime.local_alerts import JsonAlertRepository
from time_tl.runtime.regions import (
    OBLAST_REGION_IDS,
    OBLAST_REGIONS,
    REGISTRY_IS_COMPLETE_HIERARCHY,
    REGISTRY_SUPPORTS_AGGREGATE_OBLAST_SCOPES,
)
from time_tl.runtime.services import (
    _provider,
    build_alert_worker,
    postgres_bot_dependencies,
    run_alert_worker,
    run_bot,
    run_inert_worker,
)


def settings_for(
    role: str,
    *,
    environment: Environment,
    telegram: TelegramTransport = TelegramTransport.DISABLED,
    alert_provider: AlertProvider = AlertProvider.FAKE,
    state_dir: str = ".time-tl-runtime",
) -> Settings:
    return Settings(
        TIME_TL_ENV=environment,
        SERVICE_ROLE=role,
        ALERT_PROVIDER=alert_provider,
        ALERTS_IN_UA_TOKEN=SecretStr("provider-secret"),
        TELEGRAM_TRANSPORT=telegram,
        TELEGRAM_BOT_TOKEN=SecretStr("telegram-secret"),
        LESSON_LINK_FERNET_KEY=SecretStr("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="),
        TEST_CHAT_ALLOWLIST="123" if environment is Environment.STAGING else "",
        DATABASE_URL=SecretStr("postgresql+asyncpg://user:pass@postgres/time_tl"),
        RUNTIME_STATE_DIR=state_dir,
        ALLOW_SCAFFOLD_SERVICES=environment in {Environment.LOCAL, Environment.SIMULATION},
        ADMIN_COOKIE_SECURE=True,
        _env_file=None,
    )


class RuntimeContractTests(unittest.IsolatedAsyncioTestCase):
    def test_sender_transport_timeout_is_capped_at_ten_seconds(self) -> None:
        with self.assertRaises(ValueError):
            Settings(
                DATABASE_URL=SecretStr("postgresql://user:pass@localhost/time_tl"),
                SENDER_TRANSPORT_TIMEOUT_SECONDS=10.1,
                _env_file=None,
            )

    def test_production_alert_worker_fails_before_provider_or_database_io(self) -> None:
        settings = settings_for(
            "alert-worker",
            environment=Environment.PRODUCTION,
            alert_provider=AlertProvider.ALERTS_IN_UA,
        )
        with self.assertRaisesRegex(RuntimeContractError, "production aggregate-oblast scope"):
            build_alert_worker(settings)

    def test_staging_contract_accepts_reviewed_aggregate_oblast_provider(self) -> None:
        settings = settings_for(
            "alert-worker",
            environment=Environment.STAGING,
            alert_provider=AlertProvider.ALERTS_IN_UA,
        )
        validate_runtime_contract(settings, "alert-worker")

    def test_staging_contract_rejects_unreviewed_aggregate_provider(self) -> None:
        settings = settings_for(
            "alert-worker",
            environment=Environment.STAGING,
            alert_provider=AlertProvider.ALERTS_IN_UA,
        )
        capabilities = RuntimeCapabilities(reviewed_aggregate_oblast_provider=False)
        with self.assertRaisesRegex(RuntimeContractError, "reviewed aggregate-oblast"):
            validate_runtime_contract(settings, "alert-worker", capabilities)

    async def test_staging_composes_postgres_worker_with_exact_oblast_registry(self) -> None:
        settings = settings_for(
            "alert-worker",
            environment=Environment.STAGING,
            alert_provider=AlertProvider.ALERTS_IN_UA,
        )
        worker = AsyncMock()
        with patch("time_tl.runtime.services._run_postgres_alert_worker", worker):
            await run_alert_worker(settings)
        worker.assert_awaited_once_with(settings, verified_region_ids=OBLAST_REGION_IDS)

    async def test_live_bot_fails_before_aiogram_polling(self) -> None:
        settings = settings_for(
            "bot",
            environment=Environment.PRODUCTION,
            telegram=TelegramTransport.LIVE,
        )
        with self.assertRaisesRegex(RuntimeContractError, "reviewed geolocation resolver"):
            await run_bot(settings)

    async def test_postgres_bot_dependencies_build_without_network_or_messages(self) -> None:
        settings = settings_for(
            "bot",
            environment=Environment.PRODUCTION,
            telegram=TelegramTransport.LIVE,
        )
        async with postgres_bot_dependencies(settings) as (
            repository,
            resolver,
            regions,
            import_sessions,
            region_confirmation,
            heartbeat_repository,
        ):
            self.assertEqual("AsyncPostgresAccountRepository", type(repository).__name__)
            self.assertIsNone(await resolver.resolve(50.45, 30.52))
            self.assertEqual(27, len(regions))
            self.assertEqual(
                "AsyncPostgresImportSessionAdapter",
                type(import_sessions(123)).__name__,
            )
            self.assertEqual(
                "PostgresRegionConfirmationService",
                type(region_confirmation).__name__,
            )
            self.assertEqual(
                "PostgresWorkerHeartbeatRepository",
                type(heartbeat_repository).__name__,
            )

    async def test_unimplemented_workers_cannot_be_scaffolds_in_production(self) -> None:
        for role in ("schedule-worker", "sender-worker"):
            telegram = (
                TelegramTransport.LIVE if role == "sender-worker" else TelegramTransport.DISABLED
            )
            settings = settings_for(role, environment=Environment.PRODUCTION, telegram=telegram)
            with self.subTest(role=role), self.assertRaises(RuntimeContractError):
                await run_inert_worker(settings, role)

    def test_oblast_registry_is_verified_but_explicitly_partial(self) -> None:
        self.assertEqual(27, len(OBLAST_REGIONS))
        self.assertEqual({"3", "16", "31"}.intersection(OBLAST_REGION_IDS), {"3", "16", "31"})
        self.assertFalse(REGISTRY_IS_COMPLETE_HIERARCHY)
        self.assertTrue(REGISTRY_SUPPORTS_AGGREGATE_OBLAST_SCOPES)
        self.assertEqual(OBLAST_REGION_IDS, OblastAlertsInUaAdapter.COVERED_REGION_IDS)

    def test_production_contract_keeps_public_scope_release_closed(self) -> None:
        settings = settings_for(
            "alert-worker",
            environment=Environment.PRODUCTION,
            alert_provider=AlertProvider.ALERTS_IN_UA,
        )
        with self.assertRaisesRegex(RuntimeContractError, "production aggregate-oblast scope"):
            validate_runtime_contract(settings, "alert-worker")

    async def test_official_provider_composition_has_no_network_and_restarts_unconditionally(
        self,
    ) -> None:
        class Transport:
            def __init__(self) -> None:
                self.requests = []

            async def send(self, request):
                self.requests.append(request)
                return HttpResponse(304, {}, b"")

        transport = Transport()
        settings = settings_for(
            "alert-worker",
            environment=Environment.STAGING,
            alert_provider=AlertProvider.ALERTS_IN_UA,
        )
        with patch("time_tl.runtime.services.HttpxTransport", return_value=transport):
            provider = _provider(settings, OBLAST_REGION_IDS)

        self.assertIsInstance(provider, OblastAlertsInUaAdapter)
        self.assertEqual([], transport.requests)
        result = await provider.fetch(fetch_revision=10, last_modified="persisted-marker")
        self.assertEqual(1, len(transport.requests))
        self.assertNotIn("If-Modified-Since", transport.requests[0].headers)
        self.assertIsNotNone(result.failure)
        assert result.failure is not None
        self.assertEqual(SourceFailureKind.INVALID_RESPONSE, result.failure.kind)


class LocalAlertStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_poll_revision_and_marker_survive_simulation_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            scopes = (("31", RegionScope.from_hierarchy("31")),)
            first = JsonAlertRepository(path, scopes)
            await first.set_poll_state(AlertPollState(fetch_revision=9, last_modified="marker"))
            second = JsonAlertRepository(path, scopes)
            restored = await second.poll_state()
            self.assertEqual(9, restored.fetch_revision)
            self.assertEqual("marker", restored.last_modified)
            self.assertTrue(path.exists())


if __name__ == "__main__":
    unittest.main()


class SecretAndAdminContractTests(unittest.TestCase):
    def test_live_bot_requires_stable_fernet_key(self) -> None:
        with self.assertRaisesRegex(ValueError, "LESSON_LINK_FERNET_KEY"):
            Settings(
                TIME_TL_ENV=Environment.PRODUCTION,
                SERVICE_ROLE="bot",
                ALERT_PROVIDER=AlertProvider.FAKE,
                TELEGRAM_TRANSPORT=TelegramTransport.LIVE,
                TELEGRAM_BOT_TOKEN=SecretStr("telegram-secret"),
                DATABASE_URL=SecretStr("postgresql+asyncpg://user:pass@postgres/time_tl"),
                _env_file=None,
            )

    def test_invalid_fernet_key_is_rejected_without_echoing_value(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be a Fernet base64 key"):
            Settings(
                TIME_TL_ENV=Environment.PRODUCTION,
                SERVICE_ROLE="bot",
                ALERT_PROVIDER=AlertProvider.FAKE,
                TELEGRAM_TRANSPORT=TelegramTransport.LIVE,
                TELEGRAM_BOT_TOKEN=SecretStr("telegram-secret"),
                LESSON_LINK_FERNET_KEY=SecretStr("not-valid"),
                DATABASE_URL=SecretStr("postgresql+asyncpg://user:pass@postgres/time_tl"),
                _env_file=None,
            )

    def test_production_admin_fails_on_async_application_boundary(self) -> None:
        settings = Settings(
            TIME_TL_ENV=Environment.PRODUCTION,
            SERVICE_ROLE="admin-web",
            ALERT_PROVIDER=AlertProvider.FAKE,
            TELEGRAM_TRANSPORT=TelegramTransport.DISABLED,
            DATABASE_URL=SecretStr("postgresql+asyncpg://user:pass@postgres/time_tl"),
            ADMIN_BACKEND="postgres",
            ADMIN_IP_HASH_KEY=SecretStr("a" * 32),
            HOST_METRICS_URL="http://node-exporter:9100/metrics",
            ADMIN_BOOTSTRAP_ARTIFACT_PATH="/run/secrets/admin-bootstrap",
            ADMIN_COOKIE_SECURE=True,
            _env_file=None,
        )
        validate_runtime_contract(settings, "admin-web")

from __future__ import annotations

import asyncio
import inspect
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
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
    FullTerritoryAlertsInUaAdapter,
    HttpResponse,
    OblastAlertsInUaAdapter,
)
from time_tl.runtime.contracts import (
    PRODUCTION_PUBLIC_RELEASE_APPROVED,
    PRODUCTION_RELEASE_MANIFEST,
    RuntimeCapabilities,
    RuntimeContractError,
    validate_runtime_contract,
)
from time_tl.runtime.local_alerts import JsonAlertRepository
from time_tl.runtime.region_registry_full import load_catalog
from time_tl.runtime.regions import (
    OBLAST_REGION_IDS,
    OBLAST_REGIONS,
    REGISTRY_IS_COMPLETE_HIERARCHY,
    REGISTRY_SUPPORTS_AGGREGATE_OBLAST_SCOPES,
)
from time_tl.runtime.services import (
    _assert_territory_runtime_ready,
    _build_inbound_allowlist_guard,
    _owner_config_revision_watch_loop,
    _provider,
    _run_postgres_alert_worker,
    _serve_sender_singleton,
    _serve_worker,
    build_alert_worker,
    postgres_bot_dependencies,
    run_alert_worker,
    run_bot,
    run_inert_worker,
    run_schedule_worker,
    run_sender_worker,
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
        ALERT_PROVIDER=(
            AlertProvider.ALERTS_IN_UA if environment is Environment.PRODUCTION else alert_provider
        ),
        ALERTS_IN_UA_TOKEN=SecretStr("provider-secret"),
        TELEGRAM_TRANSPORT=(
            TelegramTransport.LIVE if environment is Environment.PRODUCTION else telegram
        ),
        TELEGRAM_BOT_TOKEN=SecretStr("telegram-secret"),
        LESSON_LINK_FERNET_KEY=SecretStr("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="),
        TEST_CHAT_ALLOWLIST="123" if environment is Environment.STAGING else "",
        DATABASE_URL=SecretStr("postgresql+asyncpg://user:pass@postgres/time_tl"),
        RUNTIME_STATE_DIR=state_dir,
        ALLOW_SCAFFOLD_SERVICES=environment in {Environment.LOCAL, Environment.SIMULATION},
        OWNER_CONFIG_REVISION=(
            5 if environment in {Environment.STAGING, Environment.PRODUCTION} else None
        ),
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
        with self.assertRaisesRegex(RuntimeContractError, "production full-territory alert worker"):
            build_alert_worker(settings)

    def test_staging_contract_accepts_reviewed_aggregate_oblast_provider(self) -> None:
        settings = settings_for(
            "alert-worker",
            environment=Environment.STAGING,
            telegram=TelegramTransport.LIVE,
            alert_provider=AlertProvider.ALERTS_IN_UA,
        )
        validate_runtime_contract(settings, "alert-worker")

    def test_staging_contract_rejects_unreviewed_aggregate_provider(self) -> None:
        settings = settings_for(
            "alert-worker",
            environment=Environment.STAGING,
            telegram=TelegramTransport.LIVE,
            alert_provider=AlertProvider.ALERTS_IN_UA,
        )
        capabilities = RuntimeCapabilities(reviewed_full_territory_provider=False)
        with self.assertRaisesRegex(RuntimeContractError, "reviewed full-territory"):
            validate_runtime_contract(settings, "alert-worker", capabilities)

    async def test_staging_composes_postgres_worker_with_exact_full_registry(self) -> None:
        settings = settings_for(
            "alert-worker",
            environment=Environment.STAGING,
            telegram=TelegramTransport.LIVE,
            alert_provider=AlertProvider.ALERTS_IN_UA,
        )
        worker = AsyncMock()
        with patch("time_tl.runtime.services._run_postgres_alert_worker", worker):
            await run_alert_worker(settings)
        expected = frozenset(item.uid for item in load_catalog().records)
        worker.assert_awaited_once_with(settings, verified_region_ids=expected)

    async def test_territory_startup_requires_exact_official_provider_uids(self) -> None:
        readiness = AsyncMock(return_value="reviewed-v1")
        repository = SimpleNamespace(assert_runtime_ready=readiness)
        factory = object()
        expected = frozenset(item.uid for item in load_catalog().records)
        with patch(
            "time_tl.infrastructure.db.territories.PostgresTerritoryCatalogRepository",
            return_value=repository,
        ) as repository_type:
            active = await _assert_territory_runtime_ready(factory)

        self.assertEqual("reviewed-v1", active)
        repository_type.assert_called_once_with(
            factory,
            expected_region_ids=expected,
        )
        readiness.assert_awaited_once_with(
            provider="alerts.in.ua",
            expected_region_ids=expected,
        )

    async def test_live_bot_fails_before_aiogram_polling(self) -> None:
        settings = settings_for(
            "bot",
            environment=Environment.PRODUCTION,
            telegram=TelegramTransport.LIVE,
        )
        with self.assertRaisesRegex(RuntimeContractError, "production manual-territory bot"):
            await run_bot(settings)

    def test_staging_manual_region_bot_contract_is_released(self) -> None:
        settings = settings_for(
            "bot",
            environment=Environment.STAGING,
            telegram=TelegramTransport.LIVE,
            alert_provider=AlertProvider.ALERTS_IN_UA,
        )
        validate_runtime_contract(settings, "bot")
        with self.assertRaisesRegex(RuntimeContractError, "manual-territory"):
            validate_runtime_contract(
                settings,
                "bot",
                RuntimeCapabilities(staging_manual_region_bot=False),
            )

    def test_staging_roles_require_one_applied_safe_configuration(self) -> None:
        for role in ("bot", "alert-worker", "schedule-worker", "sender-worker"):
            base = settings_for(
                role,
                environment=Environment.STAGING,
                telegram=TelegramTransport.LIVE,
                alert_provider=AlertProvider.ALERTS_IN_UA,
            )
            forbidden = (
                ({"owner_config_revision": None}, "ACTIVE database revision"),
                ({"alert_provider": AlertProvider.FAKE}, "official alerts.in.ua"),
                ({"telegram_transport": TelegramTransport.DISABLED}, "live Telegram"),
                ({"test_chat_allowlist_raw": ""}, "allowlist"),
            )
            for update, message in forbidden:
                with (
                    self.subTest(role=role, update=update),
                    self.assertRaisesRegex(RuntimeContractError, message),
                ):
                    validate_runtime_contract(base.model_copy(update=update), role)

    def test_staging_worker_capabilities_can_be_closed_independently(self) -> None:
        for role, capabilities in (
            ("schedule-worker", RuntimeCapabilities(staging_schedule_worker=False)),
            ("sender-worker", RuntimeCapabilities(staging_sender_worker=False)),
        ):
            settings = settings_for(
                role,
                environment=Environment.STAGING,
                telegram=TelegramTransport.LIVE,
                alert_provider=AlertProvider.ALERTS_IN_UA,
            )
            with (
                self.subTest(role=role),
                self.assertRaisesRegex(RuntimeContractError, f"staging {role}"),
            ):
                validate_runtime_contract(settings, role, capabilities)

    def test_only_alert_worker_owns_provider_restart_fence(self) -> None:
        schedule_source = inspect.getsource(run_schedule_worker)
        sender_source = inspect.getsource(run_sender_worker)
        self.assertNotIn("_apply_alert_restart_fence", schedule_source)
        self.assertNotIn("_apply_alert_restart_fence", sender_source)

    def test_alert_worker_fences_before_health_serving_loop(self) -> None:
        source = inspect.getsource(_run_postgres_alert_worker)

        self.assertLess(
            source.index("await _assert_territory_runtime_ready"),
            source.index("await worker.acquire_and_prepare"),
        )
        self.assertLess(
            source.index("await worker.acquire_and_prepare"),
            source.index("await _serve_worker"),
        )

    async def test_health_does_not_start_until_worker_restart_fence_commits(self) -> None:
        settings = settings_for("alert-worker", environment=Environment.SIMULATION)
        entered = asyncio.Event()
        worker = AsyncMock()
        health = AsyncMock()

        async def blocking_prepare() -> bool:
            entered.set()
            await asyncio.Future()
            return True

        worker.acquire_and_prepare.side_effect = blocking_prepare
        with patch("time_tl.runtime.services.serve_health", health):
            task = asyncio.create_task(_serve_worker(worker, settings))
            await asyncio.wait_for(entered.wait(), timeout=1)
            health.assert_not_awaited()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_losing_replica_starts_neither_fence_health_nor_heartbeat(self) -> None:
        settings = settings_for("alert-worker", environment=Environment.SIMULATION)
        worker = AsyncMock()
        worker.acquire_and_prepare.return_value = False
        health = AsyncMock()
        heartbeat = AsyncMock()

        with patch("time_tl.runtime.services.serve_health", health):
            with self.assertRaisesRegex(RuntimeError, "singleton lease"):
                await _serve_worker(worker, settings, heartbeat)

        health.assert_not_awaited()
        heartbeat.beat.assert_not_awaited()
        worker.serve_prepared.assert_not_awaited()

    async def test_losing_sender_replica_never_starts_owned_tasks(self) -> None:
        lease = AsyncMock()
        lease.acquire.return_value = False
        serve = AsyncMock()
        with self.assertRaisesRegex(RuntimeError, "singleton lease"):
            await _serve_sender_singleton(lease, serve)
        serve.assert_not_awaited()

    async def test_owner_revision_mismatch_exits_for_restart(self) -> None:
        settings = settings_for(
            "bot",
            environment=Environment.STAGING,
            telegram=TelegramTransport.LIVE,
            alert_provider=AlertProvider.ALERTS_IN_UA,
        )
        session = AsyncMock()
        session.get.side_effect = (
            SimpleNamespace(value={"active_revision": 6}),
            SimpleNamespace(value={"revision": 6}),
        )

        class SessionContext:
            async def __aenter__(self):
                return session

            async def __aexit__(self, *_args):
                return None

        engine = AsyncMock()
        with (
            patch(
                "time_tl.infrastructure.db.engine.create_engine",
                return_value=engine,
            ),
            patch(
                "time_tl.infrastructure.db.engine.create_session_factory",
                return_value=lambda: SessionContext(),
            ),
            self.assertRaisesRegex(RuntimeContractError, "clean restart required"),
        ):
            await _owner_config_revision_watch_loop(settings, interval_seconds=0)
        engine.dispose.assert_awaited_once()

    async def test_postgres_bot_dependencies_build_without_network_or_messages(self) -> None:
        settings = settings_for(
            "bot",
            environment=Environment.PRODUCTION,
            telegram=TelegramTransport.LIVE,
        )
        with patch(
            "time_tl.runtime.services._assert_territory_runtime_ready",
            AsyncMock(return_value="reviewed-v1"),
        ):
            async with postgres_bot_dependencies(settings) as (
                repository,
                resolver,
                regions,
                import_sessions,
                region_confirmation,
                territory_catalog,
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
                    "PostgresTerritoryCatalogRepository",
                    type(territory_catalog).__name__,
                )
                self.assertEqual(
                    frozenset(item.uid for item in load_catalog().records),
                    territory_catalog._expected_region_ids,
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
        with self.assertRaisesRegex(RuntimeContractError, "production full-territory alert worker"):
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
            telegram=TelegramTransport.LIVE,
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

    async def test_full_registry_composes_full_iot_provider_without_network(self) -> None:
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
            telegram=TelegramTransport.LIVE,
            alert_provider=AlertProvider.ALERTS_IN_UA,
        )
        region_ids = frozenset(item.uid for item in load_catalog().records)

        with patch("time_tl.runtime.services.HttpxTransport", return_value=transport):
            provider = _provider(settings, region_ids)

        self.assertIsInstance(provider, FullTerritoryAlertsInUaAdapter)
        self.assertEqual([], transport.requests)
        result = await provider.fetch(fetch_revision=10, last_modified="persisted-marker")
        self.assertEqual(1, len(transport.requests))
        self.assertNotIn("If-Modified-Since", transport.requests[0].headers)
        self.assertIsNotNone(result.failure)
        assert result.failure is not None
        self.assertEqual(SourceFailureKind.INVALID_RESPONSE, result.failure.kind)

    def test_default_production_release_manifest_is_closed(self) -> None:
        self.assertFalse(PRODUCTION_PUBLIC_RELEASE_APPROVED)
        self.assertEqual(1, PRODUCTION_RELEASE_MANIFEST["replicas"])
        self.assertEqual(
            "manual-oblast-raion-hromada",
            PRODUCTION_RELEASE_MANIFEST["territory_selection"],
        )

    def test_inbound_allowlist_is_staging_only(self) -> None:
        staging = settings_for(
            "bot",
            environment=Environment.STAGING,
            telegram=TelegramTransport.LIVE,
            alert_provider=AlertProvider.ALERTS_IN_UA,
        )
        guard = _build_inbound_allowlist_guard(staging)
        self.assertIsNotNone(guard)
        assert guard is not None
        self.assertTrue(guard.allows(123))
        self.assertFalse(guard.allows(999))

        production = settings_for("bot", environment=Environment.PRODUCTION)
        self.assertIsNone(_build_inbound_allowlist_guard(production))

    def test_production_roles_require_each_named_release_capability(self) -> None:
        released = RuntimeCapabilities(
            production_full_territory_alert_worker=True,
            production_manual_territory_bot=True,
            production_schedule_worker=True,
            production_sender_worker=True,
        )
        roles = (
            ("alert-worker", "production_full_territory_alert_worker"),
            ("bot", "production_manual_territory_bot"),
            ("schedule-worker", "production_schedule_worker"),
            ("sender-worker", "production_sender_worker"),
        )
        for role, field in roles:
            settings = settings_for(role, environment=Environment.PRODUCTION)
            with self.subTest(role=role):
                validate_runtime_contract(settings, role, released)
                with self.assertRaises(RuntimeContractError):
                    validate_runtime_contract(settings, role, replace(released, **{field: False}))

    def test_production_rejects_stale_staging_allowlist(self) -> None:
        settings = settings_for("bot", environment=Environment.PRODUCTION).model_copy(
            update={"test_chat_allowlist_raw": "137302167"}
        )
        with self.assertRaisesRegex(RuntimeContractError, "empty STAGING allowlist"):
            validate_runtime_contract(
                settings,
                "bot",
                RuntimeCapabilities(production_manual_territory_bot=True),
            )


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

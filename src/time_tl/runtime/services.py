"""Runnable service composition for local/simulation and guarded live roles."""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

from time_tl.application.alert_worker import AlertWorker
from time_tl.bot.async_service import build_async_router
from time_tl.bot.service import Region
from time_tl.config import (
    AdminBackend,
    AlertProvider,
    Environment,
    Settings,
    TelegramTransport,
)
from time_tl.domain.alerts import RegionScope, SourceFailure, SourceFailureKind
from time_tl.entrypoints.health import serve_health
from time_tl.infrastructure.alerts import (
    FetchResult,
    InMemorySingletonLease,
    OblastAlertsInUaAdapter,
)

from .contracts import RuntimeContractError, validate_runtime_contract
from .httpx_transport import HttpxTransport
from .local_alerts import JsonAlertRepository
from .regions import OBLAST_REGION_IDS, OBLAST_REGIONS

LOGGER = logging.getLogger(__name__)
ALERT_ADVISORY_LOCK_KEY = 0x54494D45544C  # stable signed-int64 value: ASCII "TIMETL"


def build_sender_transport(settings: Settings, client_factory: Any) -> Any:
    """Compose the outbound boundary; staging is always target-allowlisted."""
    from time_tl.infrastructure.telegram.transport import (
        AiogramTelegramTransport,
        AllowlistedTelegramTransport,
    )

    transport = AiogramTelegramTransport(client_factory)
    if settings.environment is Environment.STAGING:
        from time_tl.bot.safety import TelegramAllowlistGuard

        return AllowlistedTelegramTransport(
            transport,
            TelegramAllowlistGuard.from_ids(settings.test_chat_allowlist),
        )
    return transport


class PostgresAlertRuntimeSettingsReader:
    """Read the admin-managed provider cadence between singleton poll cycles."""

    def __init__(self, factory: Any, *, default_poll: int, default_stale: int) -> None:
        self._factory = factory
        self._defaults = (default_poll, default_stale)

    async def read(self) -> tuple[int, int]:
        from time_tl.infrastructure.db.models import RuntimeSetting

        async with self._factory() as session:
            poll_row = await session.get(RuntimeSetting, "poll")
            stale_row = await session.get(RuntimeSetting, "stale")
        poll = int(poll_row.value) if poll_row is not None else self._defaults[0]
        stale = int(stale_row.value) if stale_row is not None else self._defaults[1]
        if not 15 <= poll <= 300 or not max(30, 2 * poll) <= stale <= 3600:
            raise RuntimeContractError("invalid persisted alert runtime settings")
        return poll, stale


class RuntimeConfiguredAlertWorker(AlertWorker):
    """Refresh safe runtime values immediately before every provider cycle."""

    def __init__(self, *args: Any, runtime_settings: Any, stale_after_seconds: int, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._runtime_settings = runtime_settings
        self.stale_after_seconds = stale_after_seconds

    async def _refresh_runtime_settings(self) -> None:
        try:
            poll, stale = await self._runtime_settings.read()
        except Exception as exc:
            LOGGER.warning(
                "Ignoring unavailable/invalid alert runtime settings: %s",
                type(exc).__name__,
            )
            return
        self.poll_interval_seconds = float(poll)
        self.stale_after_seconds = stale

    async def _poll_once_while_held(self) -> float:
        await self._refresh_runtime_settings()
        return await super()._poll_once_while_held()


class OfflineSimulationProvider:
    """No-network fake: absence of a source stays UNKNOWN/STALE, never clear."""

    async def fetch(self, *, fetch_revision: int, last_modified: str | None) -> FetchResult:
        del fetch_revision, last_modified
        return FetchResult(failure=SourceFailure(SourceFailureKind.HTTP_ERROR))


class RejectingCoordinateResolver:
    """Safe boundary until a reviewed coordinate-to-provider hierarchy exists."""

    async def resolve(self, latitude: float, longitude: float) -> Region | None:
        del latitude, longitude
        return None


def _provider(settings: Settings, covered_region_ids: frozenset[str]) -> Any:
    if settings.alert_provider is AlertProvider.ALERTS_IN_UA:
        assert settings.alerts_in_ua_token is not None
        if covered_region_ids != OblastAlertsInUaAdapter.COVERED_REGION_IDS:
            raise RuntimeContractError(
                "alerts.in.ua aggregate-oblast provider requires the reviewed 27-UID registry"
            )
        return OblastAlertsInUaAdapter(
            token=settings.alerts_in_ua_token.get_secret_value(),
            transport=HttpxTransport(),
        )
    return OfflineSimulationProvider()


def build_alert_worker(settings: Settings) -> AlertWorker:
    """Build the durable local/simulation worker without network by default."""
    validate_runtime_contract(settings, "alert-worker")
    if settings.environment not in {Environment.LOCAL, Environment.SIMULATION}:
        raise RuntimeError("use production PostgreSQL alert composition")
    scopes = tuple(
        (uid, RegionScope.from_hierarchy(uid)) for uid in sorted(OBLAST_REGION_IDS, key=int)
    )
    repository = JsonAlertRepository(Path(settings.runtime_state_dir) / "alerts-state.json", scopes)
    return AlertWorker(
        provider=_provider(settings, OBLAST_REGION_IDS),
        repository=repository,
        lease=InMemorySingletonLease(),
        poll_interval_seconds=float(settings.alert_poll_interval_seconds),
        requests_per_minute=float(settings.alert_rate_limit_per_minute),
        monotonic=time.monotonic,
    )


async def _heartbeat_loop(repository: Any, worker_name: str) -> None:
    """Publish sanitized process liveness without exposing host/container identity."""

    from datetime import UTC, datetime

    instance_id = f"{worker_name}-{secrets.token_urlsafe(9)}"
    while True:
        try:
            await repository.upsert(
                worker_name=worker_name,
                instance_id=instance_id,
                status="healthy",
                heartbeat_at=datetime.now(UTC),
                details={},
            )
        except Exception as exc:
            LOGGER.warning(
                "Worker heartbeat unavailable for %s: %s",
                worker_name,
                type(exc).__name__,
            )
        await asyncio.sleep(15)


async def _serve_worker(
    worker: AlertWorker, settings: Settings, heartbeat_repository: Any | None = None
) -> None:
    async with asyncio.TaskGroup() as tasks:
        tasks.create_task(serve_health(settings.health_host, settings.health_port, "alert-worker"))
        tasks.create_task(_worker_must_not_exit(worker))
        if heartbeat_repository is not None:
            tasks.create_task(_heartbeat_loop(heartbeat_repository, "alert-worker"))


async def _worker_must_not_exit(worker: AlertWorker) -> None:
    await worker.serve(asyncio.sleep)
    raise RuntimeError("alert-worker lost or failed to acquire singleton lease")


async def run_alert_worker(settings: Settings) -> None:
    validate_runtime_contract(settings, "alert-worker")
    if settings.environment in {Environment.LOCAL, Environment.SIMULATION}:
        await _serve_worker(build_alert_worker(settings), settings)
        return
    await _run_postgres_alert_worker(settings, verified_region_ids=OBLAST_REGION_IDS)


async def _run_postgres_alert_worker(
    settings: Settings, *, verified_region_ids: frozenset[str]
) -> None:
    """Compose durable polling for reviewed one-oblast user scopes."""
    if verified_region_ids != OblastAlertsInUaAdapter.COVERED_REGION_IDS:
        raise RuntimeContractError(
            "alert-worker requires the reviewed 27-UID aggregate-oblast registry"
        )
    from time_tl.infrastructure.db.alerts import PostgresAlertRepository
    from time_tl.infrastructure.db.engine import create_engine, create_session_factory
    from time_tl.infrastructure.db.lease import dedicated_advisory_lease
    from time_tl.infrastructure.db.workers import PostgresWorkerHeartbeatRepository

    database_url = settings.database_url.get_secret_value()
    engine = create_engine(database_url)
    factory = create_session_factory(engine)
    repository = PostgresAlertRepository(factory)
    heartbeat_repository = PostgresWorkerHeartbeatRepository(factory)
    provider = _provider(settings, verified_region_ids)
    runtime_settings = PostgresAlertRuntimeSettingsReader(
        factory,
        default_poll=settings.alert_poll_interval_seconds,
        default_stale=settings.alert_stale_after_seconds,
    )
    try:
        async with dedicated_advisory_lease(database_url, ALERT_ADVISORY_LOCK_KEY) as lease:
            worker = RuntimeConfiguredAlertWorker(
                provider=provider,
                repository=repository,
                lease=lease,
                poll_interval_seconds=float(settings.alert_poll_interval_seconds),
                requests_per_minute=float(settings.alert_rate_limit_per_minute),
                monotonic=time.monotonic,
                runtime_settings=runtime_settings,
                stale_after_seconds=settings.alert_stale_after_seconds,
            )
            await _serve_worker(worker, settings, heartbeat_repository)
    finally:
        await engine.dispose()


class PostgresRegionConfirmationService:
    """Atomically save a confirmed region and create/reset its AlertScope."""

    def __init__(self, factory: Any) -> None:
        self._factory = factory

    async def confirm_region(self, account: Any) -> None:
        from sqlalchemy import delete, select

        from time_tl.bot.models import NotificationMode
        from time_tl.infrastructure.db.models import AlertEpisode, AlertScope, AlertSnapshot, User

        if account.region_id is None:
            raise ValueError("confirmed region is required")
        async with self._factory() as session:
            async with session.begin():
                user = (
                    await session.execute(
                        select(User)
                        .where(User.telegram_user_id == account.user_id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if user is None:
                    raise LookupError(account.user_id)
                user.confirmed_region_id = account.region_id
                user.region_name = account.region_name
                user.alerts_enabled = account.mode in {
                    NotificationMode.LESSONS_AND_SIRENS,
                    NotificationMode.SIRENS,
                }
                user.lessons_enabled = account.mode in {
                    NotificationMode.LESSONS,
                    NotificationMode.LESSONS_AND_SIRENS,
                }
                scope = (
                    await session.execute(
                        select(AlertScope).where(AlertScope.user_id == user.id).with_for_update()
                    )
                ).scalar_one_or_none()
                changed = scope is not None and scope.selected_region_id != account.region_id
                if scope is None:
                    scope = AlertScope(
                        user_id=user.id,
                        selected_region_id=account.region_id,
                        applicable_region_ids=[account.region_id],
                    )
                    session.add(scope)
                    await session.flush()
                else:
                    scope.selected_region_id = account.region_id
                    scope.applicable_region_ids = [account.region_id]
                if changed:
                    await session.execute(
                        delete(AlertEpisode).where(AlertEpisode.scope_id == scope.id)
                    )
                    snapshot = await session.get(AlertSnapshot, scope.id)
                    if snapshot is not None:
                        snapshot.state = "UNKNOWN"
                        snapshot.last_fetch_revision = None
                        snapshot.last_full_active_episodes = None
                        snapshot.processed_update_ids = []
                        snapshot.pending_clear_candidate = False


@asynccontextmanager
async def postgres_bot_dependencies(
    settings: Settings,
) -> AsyncIterator[tuple[Any, Any, Any, Any, Any, Any]]:
    """Construct ready persistence plus a fail-safe resolver without connecting to Telegram."""
    from time_tl.infrastructure.db.accounts import AsyncPostgresAccountRepository
    from time_tl.infrastructure.db.codec import FernetLessonLinkCodec
    from time_tl.infrastructure.db.engine import create_engine, create_session_factory
    from time_tl.infrastructure.db.workers import PostgresWorkerHeartbeatRepository

    assert settings.lesson_link_fernet_key is not None
    engine = create_engine(settings.database_url.get_secret_value())
    factory = create_session_factory(engine)
    link_codec = FernetLessonLinkCodec(
        settings.lesson_link_fernet_key.get_secret_value().encode("ascii")
    )
    repository = AsyncPostgresAccountRepository(
        factory,
        link_codec,
    )
    region_confirmation = PostgresRegionConfirmationService(factory)
    from time_tl.infrastructure.db.import_sessions import AsyncPostgresImportSessionAdapter

    def import_sessions(user_id: int) -> AsyncPostgresImportSessionAdapter:
        return AsyncPostgresImportSessionAdapter(
            factory, telegram_user_id=user_id, payload_codec=link_codec
        )

    try:
        yield (
            repository,
            RejectingCoordinateResolver(),
            tuple(Region(item.uid, item.name_uk) for item in OBLAST_REGIONS),
            import_sessions,
            region_confirmation,
            PostgresWorkerHeartbeatRepository(factory),
        )
    finally:
        await engine.dispose()


async def run_bot(settings: Settings) -> None:
    validate_runtime_contract(settings, "bot")
    if settings.telegram_transport is TelegramTransport.DISABLED:
        if settings.environment not in {Environment.LOCAL, Environment.SIMULATION}:
            raise RuntimeError("disabled Telegram transport is not a live bot")
        LOGGER.warning("Telegram transport disabled; no messages will be received or sent")
        await serve_health(settings.health_host, settings.health_port, "bot")
        return
    from aiogram import Bot, Dispatcher

    assert settings.telegram_bot_token is not None
    async with postgres_bot_dependencies(settings) as (
        repository,
        resolver,
        regions,
        import_sessions,
        region_confirmation,
        heartbeat_repository,
    ):
        from time_tl.runtime.imports import (
            DurableAsyncTelegramApplication,
            DurableImportCoordinator,
            PostgresTransactionalProfileStore,
        )

        application = DurableAsyncTelegramApplication(
            regions=regions,
            resolver=resolver,
            repository=repository,
            region_confirmation=region_confirmation,
            durable_imports=DurableImportCoordinator(
                import_sessions,
                PostgresTransactionalProfileStore(repository),
            ),
        )
        bot = Bot(token=settings.telegram_bot_token.get_secret_value())
        dispatcher = Dispatcher()
        from time_tl.bot.safety import TelegramAllowlistGuard

        guard = TelegramAllowlistGuard.from_ids(settings.test_chat_allowlist)
        dispatcher.include_router(build_async_router(application, allowlist_guard=guard))
        try:
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(serve_health(settings.health_host, settings.health_port, "bot"))
                tasks.create_task(_polling_must_not_exit(dispatcher, bot))
                tasks.create_task(_heartbeat_loop(heartbeat_repository, "bot"))
        finally:
            await bot.session.close()


async def _polling_must_not_exit(dispatcher: Any, bot: Any) -> None:
    await dispatcher.start_polling(bot)
    raise RuntimeError("Telegram polling stopped unexpectedly")


def _postgres_admin_repository(settings: Settings) -> tuple[Any, Any]:
    from time_tl.infrastructure.db.admin import AsyncPostgresAdminRepository
    from time_tl.infrastructure.db.engine import create_engine, create_session_factory

    if settings.admin_ip_hash_key is None:
        raise RuntimeContractError("PostgreSQL admin requires stable ADMIN_IP_HASH_KEY")
    engine = create_engine(settings.database_url.get_secret_value())
    repository = AsyncPostgresAdminRepository(
        create_session_factory(engine),
        ip_hash_key=settings.admin_ip_hash_key.get_secret_value().encode("utf-8"),
    )
    return engine, repository


def run_admin_web_service(settings: Settings) -> None:
    validate_runtime_contract(settings, "admin-web")
    if settings.admin_backend is AdminBackend.LOCAL_SQLITE:
        from time_tl.admin.app import run_admin_web

        run_admin_web(settings)
        return
    if settings.admin_backend is AdminBackend.POSTGRES:
        import uvicorn

        from time_tl.admin.async_app import ProxyPolicy, create_async_admin_app
        from time_tl.admin.node_exporter import NodeExporterMetricsCollector

        if not settings.host_metrics_url:
            raise RuntimeContractError("production admin-web requires HOST_METRICS_URL")
        engine, repository = _postgres_admin_repository(settings)
        collector = NodeExporterMetricsCollector(settings.host_metrics_url)
        proxy_policy = ProxyPolicy.from_cidrs(settings.admin_trusted_proxy_cidrs)
        app = create_async_admin_app(
            settings,
            repository,
            collector,
            proxy_policy=proxy_policy,
            bootstrap_artifact_path=settings.admin_bootstrap_artifact_path,
        )

        @app.on_event("shutdown")
        async def dispose_engine() -> None:
            await engine.dispose()

        uvicorn.run(
            app,
            host=settings.admin_host,
            port=settings.admin_port,
            log_config=None,
            access_log=False,
            proxy_headers=False,
        )
        return
    raise RuntimeContractError("admin-web backend is disabled")


def run_admin_bootstrap(settings: Settings, output: str) -> None:
    if settings.admin_backend is AdminBackend.LOCAL_SQLITE:
        from time_tl.admin.bootstrap import bootstrap_admin

        bootstrap_admin(settings, output)
        return
    if settings.admin_backend is not AdminBackend.POSTGRES:
        raise RuntimeContractError("admin bootstrap backend is disabled")

    async def bootstrap() -> None:
        from time_tl.admin.async_bootstrap import bootstrap_async_admin

        engine, repository = _postgres_admin_repository(settings)
        try:
            await bootstrap_async_admin(repository, output)
        finally:
            await engine.dispose()

    asyncio.run(bootstrap())


async def run_schedule_worker(settings: Settings) -> None:
    """Prepared production loop; capability gate remains closed pending release approval."""
    validate_runtime_contract(settings, "schedule-worker")
    if settings.environment in {Environment.LOCAL, Environment.SIMULATION}:
        await run_inert_worker(settings, "schedule-worker")
        return
    from datetime import UTC, datetime

    from time_tl.application.schedule_worker import build_schedule_worker
    from time_tl.infrastructure.db.codec import FernetLessonLinkCodec
    from time_tl.infrastructure.db.engine import create_engine, create_session_factory
    from time_tl.infrastructure.db.workers import (
        PostgresScheduleWorkerRepository,
        PostgresWorkerHeartbeatRepository,
    )

    assert settings.lesson_link_fernet_key is not None
    engine = create_engine(settings.database_url.get_secret_value())
    factory = create_session_factory(engine)
    codec = FernetLessonLinkCodec(
        settings.lesson_link_fernet_key.get_secret_value().encode("ascii")
    )
    worker = build_schedule_worker(
        PostgresScheduleWorkerRepository(
            factory,
            link_codec=codec,
            stale_after_seconds=settings.alert_stale_after_seconds,
        )
    )
    heartbeat_repository = PostgresWorkerHeartbeatRepository(factory)

    async def loop() -> None:
        while True:
            await worker.run_once(datetime.now(UTC))
            await asyncio.sleep(settings.schedule_worker_interval_seconds)

    try:
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(
                serve_health(settings.health_host, settings.health_port, "schedule-worker")
            )
            tasks.create_task(loop())
            tasks.create_task(_heartbeat_loop(heartbeat_repository, "schedule-worker"))
    finally:
        await engine.dispose()


async def run_sender_worker(settings: Settings) -> None:
    """Prepared production delivery loop; gate prevents unapproved live sends."""
    validate_runtime_contract(settings, "sender-worker")
    if settings.environment in {Environment.LOCAL, Environment.SIMULATION}:
        await run_inert_worker(settings, "sender-worker")
        return
    from datetime import UTC, datetime

    from aiogram import Bot

    from time_tl.application.sender_worker import build_sender_worker
    from time_tl.infrastructure.db.codec import FernetLessonLinkCodec
    from time_tl.infrastructure.db.engine import create_engine, create_session_factory
    from time_tl.infrastructure.db.workers import (
        PostgresJoinLinkResolver,
        PostgresSenderRepository,
        PostgresWorkerHeartbeatRepository,
    )
    from time_tl.infrastructure.telegram.transport import TelegramClient

    assert settings.telegram_bot_token is not None
    assert settings.lesson_link_fernet_key is not None
    engine = create_engine(settings.database_url.get_secret_value())
    factory = create_session_factory(engine)
    codec = FernetLessonLinkCodec(
        settings.lesson_link_fernet_key.get_secret_value().encode("ascii")
    )
    bot = Bot(token=settings.telegram_bot_token.get_secret_value())
    worker = build_sender_worker(
        PostgresSenderRepository(
            factory,
            stale_after_seconds=settings.alert_stale_after_seconds,
        ),
        build_sender_transport(settings, lambda: cast(TelegramClient, bot)),
        join_links=PostgresJoinLinkResolver(factory, codec),
        messages_per_second=settings.sender_messages_per_second,
        transport_timeout_seconds=settings.sender_transport_timeout_seconds,
    )
    heartbeat_repository = PostgresWorkerHeartbeatRepository(factory)

    async def loop() -> None:
        while True:
            await worker.run_once(datetime.now(UTC), sleep=asyncio.sleep)
            await asyncio.sleep(settings.sender_worker_interval_seconds)

    try:
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(
                serve_health(settings.health_host, settings.health_port, "sender-worker")
            )
            tasks.create_task(loop())
            tasks.create_task(_heartbeat_loop(heartbeat_repository, "sender-worker"))
    finally:
        await bot.session.close()
        await engine.dispose()


async def run_inert_worker(settings: Settings, service: str) -> None:
    validate_runtime_contract(settings, service)
    if settings.environment not in {Environment.LOCAL, Environment.SIMULATION}:
        raise RuntimeError(f"{service} has no production adapter")
    if not settings.allow_scaffold_services:
        raise RuntimeError(f"{service} simulation scaffold is disabled")
    LOGGER.warning("Starting %s as simulation-only liveness scaffold", service)
    await serve_health(settings.health_host, settings.health_port, service)


def run_migrations(settings: Settings) -> None:
    """Explicit operator command; never called by application startup."""
    from alembic import command
    from alembic.config import Config

    config = Config("alembic.ini")
    database_url = settings.database_url.get_secret_value().replace("%", "%%")
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")

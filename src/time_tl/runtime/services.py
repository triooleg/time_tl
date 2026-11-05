"""Runnable service composition for local/simulation and guarded live roles."""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from time_tl.application.alert_worker import AlertWorker
from time_tl.bot.async_service import build_async_router
from time_tl.bot.service import Region
from time_tl.config import (
    AdminBackend,
    AlertProvider,
    Environment,
    OwnerConfigSource,
    Settings,
    TelegramTransport,
)
from time_tl.domain.alerts import RegionScope, SourceFailure, SourceFailureKind
from time_tl.domain.power_outages.models import PowerProvider
from time_tl.entrypoints.health import serve_health
from time_tl.infrastructure.alerts import (
    AlertsInUaAdapter,
    FetchResult,
    InMemorySingletonLease,
    OblastAlertsInUaAdapter,
)

from . import region_registry_full
from .contracts import (
    DEFAULT_CAPABILITIES,
    RuntimeCapabilities,
    RuntimeContractError,
    validate_runtime_contract,
)
from .httpx_transport import HttpxTransport
from .local_alerts import JsonAlertRepository
from .power_map_release import (
    MapFeatureAuthority,
    feature_authority_guard,
    verify_feature_scope,
)
from .region_registry_full import load_catalog
from .regions import OBLAST_REGION_IDS, OBLAST_REGIONS
from .weather_release import (
    WeatherFeatureAuthority,
    requested_capabilities,
    verify_weather_scope,
    weather_authority_guard,
)

LOGGER = logging.getLogger(__name__)
ALERT_ADVISORY_LOCK_KEY = 0x54494D45544C  # stable signed-int64 value: ASCII "TIMETL"
SENDER_ADVISORY_LOCK_KEY = 0x54494D455453  # distinct stable value: ASCII "TIMETS"
LIVE_ALERT_REQUESTS_PER_MINUTE_MAX = 4.0


def _admit_map_feature(
    role: str,
    settings: Settings,
    production_gate: Any,
) -> MapFeatureAuthority | None:
    """Admit new public capabilities before database owner resolution changes settings."""
    if settings.environment is not Environment.PRODUCTION or not (
        settings.power_outages_enabled and settings.power_alerts_energy_api_enabled
    ):
        return None
    if production_gate is None:
        raise RuntimeContractError("normal production gate is required for map features")
    return verify_feature_scope(role, settings, production_manifest=production_gate.manifest)


def _admit_weather_feature(
    role: str, settings: Settings, production_gate: Any = None
) -> WeatherFeatureAuthority | None:
    if settings.environment is not Environment.PRODUCTION or not any(
        requested_capabilities(role, settings).values()
    ):
        return None
    return verify_weather_scope(
        role, settings, production_manifest=production_gate.manifest if production_gate else None
    )


def _reviewed_map_capabilities(
    authority: MapFeatureAuthority | None, weather: WeatherFeatureAuthority | None = None
) -> RuntimeCapabilities:
    if authority is None and weather is None:
        return DEFAULT_CAPABILITIES
    # The environment cannot grant these review facts: authority admission has
    # already verified both signed releases and all installed source bytes.
    return replace(
        DEFAULT_CAPABILITIES,
        reviewed_power_outages=authority is not None,
        reviewed_power_address=authority.enabled("address_lookup") if authority else False,
        reviewed_weather=weather is not None,
    )


def _map_preferences_enabled(settings: Settings, authority: MapFeatureAuthority | None) -> bool:
    if not (
        settings.power_outages_enabled
        and settings.power_alerts_energy_api_enabled
        and settings.power_map_reminders_enabled
    ):
        return False
    if settings.environment is Environment.PRODUCTION:
        return authority is not None and authority.enabled("map_preferences")
    return (
        settings.environment in {Environment.LOCAL, Environment.SIMULATION}
        and settings.power_map_reminders_enabled
    )


def _live_alert_requests_per_minute(configured: int) -> float:
    """Cap the one global live poller below the documented provider soft limit."""

    return min(LIVE_ALERT_REQUESTS_PER_MINUTE_MAX, float(configured))


def _build_inbound_allowlist_guard(settings: Settings) -> Any | None:
    """Restrict inbound users only in STAGING; PRODUCTION is intentionally public."""

    if settings.environment is not Environment.STAGING:
        return None
    from time_tl.bot.safety import TelegramAllowlistGuard

    return TelegramAllowlistGuard.from_ids(settings.test_chat_allowlist)


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
    """Read cadence without crossing an activated configuration revision."""

    def __init__(
        self,
        factory: Any,
        *,
        default_poll: int,
        default_stale: int,
        expected_revision: int | None = None,
    ) -> None:
        self._factory = factory
        self._defaults = (default_poll, default_stale)
        self._expected_revision = expected_revision

    async def read(self) -> tuple[int, int]:
        # Environment-managed services are immutable after startup. Owner
        # activation must never partially change only cadence in that process.
        if self._expected_revision is None:
            return self._defaults

        from time_tl.infrastructure.db.models import RuntimeSetting

        async with self._factory() as session:
            state_row = await session.get(RuntimeSetting, "owner_configuration_state")
            active_row = await session.get(RuntimeSetting, "owner_configuration_active")
            if (
                state_row is None
                or active_row is None
                or int(state_row.value.get("active_revision", 0)) != self._expected_revision
                or int(active_row.value.get("revision", 0)) != self._expected_revision
            ):
                raise RuntimeContractError(
                    "active owner configuration changed; controlled restart required"
                )
            poll = int(active_row.value["poll_interval_seconds"])
            stale = int(active_row.value["stale_after_seconds"])
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
        if covered_region_ids == OblastAlertsInUaAdapter.COVERED_REGION_IDS:
            return OblastAlertsInUaAdapter(
                token=settings.alerts_in_ua_token.get_secret_value(),
                transport=HttpxTransport(),
            )
        catalog_records = load_catalog().records
        full_region_ids = frozenset(item.uid for item in catalog_records)
        if covered_region_ids != full_region_ids:
            raise RuntimeContractError(
                "alerts.in.ua full provider requires the byte-pinned UID registry"
            )
        return AlertsInUaAdapter(
            token=settings.alerts_in_ua_token.get_secret_value(),
            transport=HttpxTransport(
                max_response_bytes=AlertsInUaAdapter.MAX_RESPONSE_BYTES,
            ),
            region_types={item.uid: item.location_type for item in catalog_records},
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


async def resolve_owner_runtime_settings(
    settings: Settings, *, production_gate: Any | None = None
) -> Settings:
    """Load only an operator-activated DB version, otherwise preserve env settings.

    Any missing row, wrong key, target-environment mismatch or invalid active
    payload aborts service startup.  It never falls back to a draft or stale env
    token when database management was explicitly selected.
    """

    if settings.environment is Environment.PRODUCTION and production_gate is None:
        raise RuntimeContractError("production runtime requires trusted ACTIVE configuration")
    if (
        settings.owner_config_source is OwnerConfigSource.ENVIRONMENT
        and settings.environment is not Environment.PRODUCTION
    ):
        return settings
    if settings.service_role not in {
        "bot",
        "alert-worker",
        "schedule-worker",
        "sender-worker",
    }:
        return settings
    telegram_key = (
        settings.owner_telegram_fernet_key.get_secret_value()
        if settings.owner_telegram_fernet_key is not None
        else None
    )
    alerts_key = (
        settings.owner_alerts_fernet_key.get_secret_value()
        if settings.owner_alerts_fernet_key is not None
        else None
    )

    from time_tl.admin.settings import OwnerSettingsService

    if production_gate is not None:
        service = OwnerSettingsService(
            cast(Any, None),
            telegram_key,
            alerts_key,
            default_poll=settings.alert_poll_interval_seconds,
            default_stale=settings.alert_stale_after_seconds,
        )
        payload = production_gate.active_payload
        if str(payload.get("environment")) != Environment.PRODUCTION.value:
            raise RuntimeContractError("production ACTIVE configuration targets wrong environment")
        if service._issues(payload):
            raise RuntimeContractError("production ACTIVE configuration is invalid")
        candidate = service._candidate(
            payload,
            production_gate.manifest.config_revision,
            settings.service_role,
        )
    else:
        from time_tl.infrastructure.db.admin import AsyncPostgresAdminRepository
        from time_tl.infrastructure.db.engine import create_engine, create_session_factory

        engine = create_engine(settings.database_url.get_secret_value())
        repository = AsyncPostgresAdminRepository(
            create_session_factory(engine),
            ip_hash_key=(
                settings.admin_ip_hash_key.get_secret_value().encode("utf-8")
                if settings.admin_ip_hash_key is not None
                else b"runtime-reader-does-not-hash-client-addresses"
            ),
        )
        service = OwnerSettingsService(
            repository,
            telegram_key,
            alerts_key,
            default_poll=settings.alert_poll_interval_seconds,
            default_stale=settings.alert_stale_after_seconds,
        )
        try:
            candidate = await service.active_runtime_config(
                settings.environment.value, settings.service_role
            )
        finally:
            await engine.dispose()

    values = settings.model_dump(by_alias=True)
    values.update(
        {
            "ALERT_PROVIDER": candidate.alert_provider,
            "ALERTS_IN_UA_TOKEN": candidate.alerts_in_ua_token,
            "ALERT_POLL_INTERVAL_SECONDS": candidate.poll_interval_seconds,
            "ALERT_STALE_AFTER_SECONDS": candidate.stale_after_seconds,
            "TELEGRAM_TRANSPORT": candidate.telegram_transport,
            "TELEGRAM_BOT_TOKEN": candidate.telegram_token,
            "BOT_ONBOARDING_TEXT": candidate.onboarding_text,
            "TEST_CHAT_ALLOWLIST": ",".join(str(item) for item in candidate.staging_allowlist),
            # Re-run the strict environment-backed validators over the resolved values.
            "OWNER_CONFIG_SOURCE": OwnerConfigSource.ENVIRONMENT,
            "OWNER_CONFIG_REVISION": candidate.revision,
        }
    )
    resolved = Settings.model_validate(values)
    if production_gate is not None and (
        resolved.owner_config_revision != production_gate.manifest.config_revision
    ):
        raise RuntimeContractError("production resolved config revision mismatch")
    return resolved


async def _heartbeat_loop(
    repository: Any, worker_name: str, applied_revision: int | None = None
) -> None:
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
                details=(
                    {"applied_config_revision": applied_revision}
                    if applied_revision is not None
                    else {}
                ),
            )
        except Exception as exc:
            LOGGER.warning(
                "Worker heartbeat unavailable for %s: %s",
                worker_name,
                type(exc).__name__,
            )
        await asyncio.sleep(15)


async def _owner_config_revision_watch_loop(
    settings: Settings, *, interval_seconds: float = 5.0
) -> None:
    """Exit the process when the owner activates a different configuration."""

    if settings.owner_config_revision is None:
        raise RuntimeContractError("active owner configuration revision is required")
    from time_tl.infrastructure.db.engine import create_engine, create_session_factory
    from time_tl.infrastructure.db.models import RuntimeSetting

    engine = create_engine(settings.database_url.get_secret_value())
    factory = create_session_factory(engine)
    try:
        while True:
            async with factory() as session:
                state_row = await session.get(RuntimeSetting, "owner_configuration_state")
                active_row = await session.get(RuntimeSetting, "owner_configuration_active")
            active_revision = (
                int(state_row.value.get("active_revision", 0)) if state_row is not None else 0
            )
            payload_revision = (
                int(active_row.value.get("revision", 0)) if active_row is not None else 0
            )
            if (
                active_revision != settings.owner_config_revision
                or payload_revision != settings.owner_config_revision
            ):
                raise RuntimeContractError(
                    "active owner configuration changed; clean restart required"
                )
            await asyncio.sleep(interval_seconds)
    finally:
        await engine.dispose()


async def _serve_worker(
    worker: AlertWorker,
    settings: Settings,
    heartbeat_repository: Any | None = None,
    *,
    prepared: bool = False,
) -> None:
    if not prepared and not await worker.acquire_and_prepare():
        raise RuntimeError("alert-worker failed to acquire singleton lease")
    async with asyncio.TaskGroup() as tasks:
        tasks.create_task(serve_health(settings.health_host, settings.health_port, "alert-worker"))
        tasks.create_task(_worker_must_not_exit(worker))
        if settings.owner_config_revision is not None:
            tasks.create_task(_owner_config_revision_watch_loop(settings))
        if heartbeat_repository is not None:
            tasks.create_task(
                _heartbeat_loop(
                    heartbeat_repository,
                    "alert-worker",
                    settings.owner_config_revision,
                )
            )


async def _worker_must_not_exit(worker: AlertWorker) -> None:
    await worker.serve_prepared(asyncio.sleep)
    raise RuntimeError("alert-worker lost or failed to acquire singleton lease")


async def run_alert_worker(settings: Settings) -> None:
    from .production_release import validate_production_runtime_release

    production_gate = await validate_production_runtime_release(settings)
    if (
        settings.environment not in {Environment.LOCAL, Environment.SIMULATION}
        and not region_registry_full.ACTIVE_JSON_WIRE_RUNTIME
    ):
        raise RuntimeContractError("active.json full-territory alert runtime wiring is disabled")
    settings = await resolve_owner_runtime_settings(settings, production_gate=production_gate)
    validate_runtime_contract(settings, "alert-worker")
    if settings.environment in {Environment.LOCAL, Environment.SIMULATION}:
        await _serve_worker(build_alert_worker(settings), settings)
        return
    verified_region_ids = frozenset(item.uid for item in load_catalog().records)
    await _run_postgres_alert_worker(settings, verified_region_ids=verified_region_ids)


async def _run_postgres_alert_worker(
    settings: Settings, *, verified_region_ids: frozenset[str]
) -> None:
    """Compose durable polling for the reviewed full provider UID catalogue."""
    if not region_registry_full.ACTIVE_JSON_WIRE_RUNTIME:
        raise RuntimeContractError("active.json full-territory alert runtime wiring is disabled")
    if verified_region_ids != frozenset(item.uid for item in load_catalog().records):
        raise RuntimeContractError(
            "alert-worker requires the reviewed full alerts.in.ua UID registry"
        )
    from time_tl.infrastructure.db.alerts import PostgresAlertRepository
    from time_tl.infrastructure.db.engine import create_engine, create_session_factory
    from time_tl.infrastructure.db.lease import dedicated_advisory_lease
    from time_tl.infrastructure.db.workers import PostgresWorkerHeartbeatRepository

    database_url = settings.database_url.get_secret_value()
    engine = create_engine(database_url)
    factory = create_session_factory(engine)
    await _assert_territory_runtime_ready(factory)
    repository = PostgresAlertRepository(factory)
    heartbeat_repository = PostgresWorkerHeartbeatRepository(factory)
    provider = _provider(settings, verified_region_ids)
    runtime_settings = PostgresAlertRuntimeSettingsReader(
        factory,
        default_poll=settings.alert_poll_interval_seconds,
        default_stale=settings.alert_stale_after_seconds,
        expected_revision=settings.owner_config_revision,
    )
    try:
        async with dedicated_advisory_lease(database_url, ALERT_ADVISORY_LOCK_KEY) as lease:
            worker = RuntimeConfiguredAlertWorker(
                provider=provider,
                repository=repository,
                lease=lease,
                poll_interval_seconds=float(settings.alert_poll_interval_seconds),
                requests_per_minute=_live_alert_requests_per_minute(
                    settings.alert_rate_limit_per_minute
                ),
                monotonic=time.monotonic,
                runtime_settings=runtime_settings,
                stale_after_seconds=settings.alert_stale_after_seconds,
            )
            if not await worker.acquire_and_prepare():
                raise RuntimeError("alert-worker failed to acquire singleton lease")
            await _serve_worker(worker, settings, heartbeat_repository, prepared=True)
    finally:
        await engine.dispose()


class PostgresRegionConfirmationService:
    """Atomically save a confirmed region and create/reset its AlertScope."""

    def __init__(self, factory: Any, territory_catalog: Any | None = None) -> None:
        self._factory = factory
        self._territory_catalog = territory_catalog

    async def confirm_region(self, account: Any) -> None:
        from sqlalchemy import delete, select

        from time_tl.bot.models import NotificationMode
        from time_tl.infrastructure.db.models import AlertEpisode, AlertScope, AlertSnapshot, User

        if account.region_id is None:
            raise ValueError("confirmed region is required")
        async with self._factory() as session:
            async with session.begin():
                resolved = None
                if self._territory_catalog is not None:
                    resolved = await self._territory_catalog.resolve_scope_in_session(
                        session,
                        provider="alerts.in.ua",
                        region_id=account.region_id,
                    )
                    account.region_name = resolved.path[-1].name
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
                next_ids = (
                    list(resolved.applicable_region_ids)
                    if resolved is not None
                    else [account.region_id]
                )
                changed = scope is not None and (
                    scope.selected_region_id != account.region_id
                    or sorted(scope.applicable_region_ids) != sorted(next_ids)
                    or scope.territory_provider
                    != (resolved.provider if resolved is not None else None)
                    or scope.territory_catalog_version
                    != (resolved.catalog_version if resolved is not None else None)
                )
                if scope is None:
                    scope = AlertScope(
                        user_id=user.id,
                        selected_region_id=account.region_id,
                        applicable_region_ids=next_ids,
                        territory_provider=(resolved.provider if resolved is not None else None),
                        territory_catalog_version=(
                            resolved.catalog_version if resolved is not None else None
                        ),
                    )
                    session.add(scope)
                    await session.flush()
                else:
                    scope.selected_region_id = account.region_id
                    scope.applicable_region_ids = next_ids
                    scope.territory_provider = resolved.provider if resolved is not None else None
                    scope.territory_catalog_version = (
                        resolved.catalog_version if resolved is not None else None
                    )
                if changed:
                    await session.execute(
                        delete(AlertEpisode).where(AlertEpisode.scope_id == scope.id)
                    )
                    snapshot = await session.get(AlertSnapshot, scope.id)
                    if snapshot is not None:
                        snapshot.state = "UNKNOWN"
                        snapshot.last_fetch_revision = None
                        snapshot.transition_revision = None
                        snapshot.last_full_active_episodes = None
                        snapshot.processed_update_ids = []
                        snapshot.pending_clear_candidate = False


@asynccontextmanager
async def postgres_bot_dependencies(
    settings: Settings,
    *,
    power_authority: MapFeatureAuthority | None = None,
    power_feature_settings: Settings | None = None,
) -> AsyncIterator[tuple[Any, Any, Any, Any, Any, Any, Any, Any, Any, Any]]:
    """Construct ready persistence plus a fail-safe resolver without connecting to Telegram."""
    from time_tl.infrastructure.db.accounts import AsyncPostgresAccountRepository
    from time_tl.infrastructure.db.codec import FernetLessonLinkCodec
    from time_tl.infrastructure.db.engine import create_engine, create_session_factory
    from time_tl.infrastructure.db.workers import PostgresWorkerHeartbeatRepository

    if settings.environment is Environment.PRODUCTION and (
        settings.power_outages_enabled and settings.power_alerts_energy_api_enabled
    ):
        if (
            not isinstance(power_authority, MapFeatureAuthority)
            or power_authority.role != "bot"
            or power_feature_settings is None
            or not power_authority.enabled("bot_power_ui", power_feature_settings)
        ):
            raise RuntimeContractError("signed bot power authority is required")
        original = power_feature_settings
        if original.owner_config_revision is None:
            original = original.model_copy(
                update={
                    "owner_config_revision": power_authority.owner_revision,
                }
            )
        if verify_feature_scope("bot", original) != power_authority:
            raise RuntimeContractError("signed bot power authority changed")
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
    homework_repository: Any | None = None
    if settings.homework_enabled:
        from time_tl.infrastructure.db.homework import AsyncPostgresHomeworkRepository

        homework_repository = AsyncPostgresHomeworkRepository(factory)
    calendar_repository: Any | None = None
    if settings.calendars_enabled:
        from time_tl.infrastructure.db.calendars import AsyncPostgresCalendarRepository

        calendar_repository = AsyncPostgresCalendarRepository(factory)
    power_port: Any | None = None
    if settings.power_outages_enabled and settings.power_alerts_energy_api_enabled:
        from time_tl.runtime.power_map_bot import build_power_map_bot_port

        power_port = build_power_map_bot_port(
            factory,
            settings.owner_alerts_fernet_key,
            address_lookup_enabled=False,
            notifications_enabled=_map_preferences_enabled(settings, power_authority),
        )
    elif settings.power_outages_enabled:
        from time_tl.infrastructure.db.power import PostgresPowerRepository
        from time_tl.infrastructure.db.power_map import PostgresPowerMapRepository
        from time_tl.runtime.power import PostgresPowerBotPort
        from time_tl.runtime.power_map import PowerMapScheduleReader

        power_port = PostgresPowerBotPort(
            factory,
            PostgresPowerRepository(
                factory, stale_after_seconds=settings.power_stale_after_seconds
            ),
            map_reader=PowerMapScheduleReader(PostgresPowerMapRepository(factory))
            if settings.power_alerts_energy_api_enabled
            else None,
        )
        if settings.power_address_lookup_enabled and settings.owner_alerts_fernet_key is not None:
            from time_tl.infrastructure.db.energy_api_budget import PostgresEnergyApiBudget
            from time_tl.infrastructure.db.energy_api_credentials import (
                PostgresEnergyApiCredentials,
            )
            from time_tl.runtime.power_address import PostgresPowerAddressPort

            power_port.address_port = PostgresPowerAddressPort(
                power_port,
                PostgresEnergyApiCredentials(factory, settings.owner_alerts_fernet_key),
                budget=PostgresEnergyApiBudget(factory),
            )
    from time_tl.infrastructure.db.territories import PostgresTerritoryCatalogRepository

    await _assert_territory_runtime_ready(factory)
    official_region_ids = frozenset(record.uid for record in load_catalog().records)
    territory_catalog = PostgresTerritoryCatalogRepository(
        factory,
        expected_region_ids=official_region_ids,
    )
    region_confirmation = PostgresRegionConfirmationService(factory, territory_catalog)
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
            territory_catalog,
            PostgresWorkerHeartbeatRepository(factory),
            power_port,
            homework_repository,
            calendar_repository,
        )
    finally:
        await engine.dispose()


async def run_bot(settings: Settings) -> None:
    from .production_release import validate_production_runtime_release

    production_gate = await validate_production_runtime_release(settings)
    admission_settings = settings
    power_authority = _admit_map_feature("bot", admission_settings, production_gate)
    weather_authority = _admit_weather_feature("bot", admission_settings, production_gate)
    settings = await resolve_owner_runtime_settings(settings, production_gate=production_gate)
    validate_runtime_contract(
        settings, "bot", _reviewed_map_capabilities(power_authority, weather_authority)
    )
    if settings.telegram_transport is TelegramTransport.DISABLED:
        if settings.environment not in {Environment.LOCAL, Environment.SIMULATION}:
            raise RuntimeError("disabled Telegram transport is not a live bot")
        LOGGER.warning("Telegram transport disabled; no messages will be received or sent")
        await serve_health(settings.health_host, settings.health_port, "bot")
        return
    from aiogram import Bot, Dispatcher

    assert settings.telegram_bot_token is not None
    async with postgres_bot_dependencies(
        settings,
        power_authority=power_authority,
        power_feature_settings=admission_settings,
    ) as (
        repository,
        resolver,
        regions,
        import_sessions,
        region_confirmation,
        territory_catalog,
        heartbeat_repository,
        power_port,
        homework_repository,
        calendar_repository,
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
            territory_catalog=territory_catalog,
            durable_imports=DurableImportCoordinator(
                import_sessions,
                PostgresTransactionalProfileStore(repository),
            ),
            onboarding_text=settings.bot_onboarding_text,
            homework_enabled=settings.homework_enabled,
            homework_port=homework_repository,
            my_day_enabled=settings.my_day_enabled,
            calendars_enabled=settings.calendars_enabled,
            calendar_port=calendar_repository,
            calendar_reminder_port=(
                calendar_repository.make_calendar_reminder_repository()
                if settings.calendar_reminders_enabled
                else None
            ),
            power_outages_enabled=settings.power_outages_enabled,
            power_port=power_port,
            weather_enabled=settings.weather_enabled,
            weather_port=_weather_bot_port(
                settings, repository, weather_authority, admission_settings
            ),
            weather_notifications_available=settings.weather_notifications_enabled,
            weather_display_gust_ms=settings.weather_display_gust_ms,
            power_address_port=(
                power_port.address_port
                if settings.power_address_lookup_enabled and power_port is not None
                else None
            ),
            power_default_outage_lead=settings.power_notification_default_outage_lead,
            power_default_restore_lead=settings.power_notification_default_restore_lead,
            power_stale_after_seconds=settings.power_stale_after_seconds,
            power_supported_providers=(PowerProvider.DTEK, PowerProvider.CEK)
            if settings.power_alerts_energy_api_enabled
            else tuple(PowerProvider),
            power_notifications_available=(
                not settings.power_alerts_energy_api_enabled
                or _map_preferences_enabled(settings, power_authority)
            ),
            power_forecast_available=not settings.power_alerts_energy_api_enabled,
            personal_calendar_changes_enabled=settings.personal_calendar_changes_enabled,
            personal_calendar_port=(
                calendar_repository.make_personal_calendar_repository()
                if settings.personal_calendar_changes_enabled
                else None
            ),
        )
        bot = Bot(token=settings.telegram_bot_token.get_secret_value())
        from time_tl.bot.safety import (
            BoundedMemoryStorage,
            BoundedTelegramIngressGuard,
        )

        dispatcher = Dispatcher(
            storage=BoundedMemoryStorage(max_records=2_048, idle_ttl_seconds=3_600.0)
        )
        allowlist_guard = _build_inbound_allowlist_guard(settings)
        router = build_async_router(
            application,
            allowlist_guard=allowlist_guard,
            ingress_guard=BoundedTelegramIngressGuard(),
        )
        dispatcher.include_router(router)
        try:
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(serve_health(settings.health_host, settings.health_port, "bot"))
                tasks.create_task(_polling_must_not_exit(dispatcher, bot))
                tasks.create_task(_owner_config_revision_watch_loop(settings))
                tasks.create_task(
                    _heartbeat_loop(heartbeat_repository, "bot", settings.owner_config_revision)
                )
                if settings.calendars_enabled and calendar_repository is not None:
                    tasks.create_task(_calendar_invitation_retention_loop(calendar_repository))
        finally:
            await bot.session.close()


async def _polling_must_not_exit(dispatcher: Any, bot: Any) -> None:
    await dispatcher.start_polling(bot)
    raise RuntimeError("Telegram polling stopped unexpectedly")


async def _calendar_invitation_retention_loop(repository: Any) -> None:
    """Bounded expired-token maintenance, only in the explicitly enabled bot.

    No active invitation deletion or automatic retries of user mutations. Failed
    maintenance reports a fixed reason and cannot leak database/user contents.
    """
    from datetime import UTC, datetime

    while True:
        try:
            async with asyncio.timeout(10):
                await repository.cleanup_invitations(datetime.now(UTC), limit=200)
        except Exception:
            LOGGER.warning("Calendar invitation retention unavailable")
        await asyncio.sleep(3600)


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


def _power_admin_port(settings: Settings, engine: Any) -> Any | None:
    if not settings.power_outages_enabled:
        return None
    from time_tl.infrastructure.db.engine import create_session_factory
    from time_tl.infrastructure.db.power import PostgresPowerRepository
    from time_tl.runtime.power import PostgresPowerAdminPort

    return PostgresPowerAdminPort(
        PostgresPowerRepository(
            create_session_factory(engine), stale_after_seconds=settings.power_stale_after_seconds
        )
    )


def _power_source_admin_port(settings: Settings, engine: Any) -> Any | None:
    if not settings.power_outages_enabled:
        return None
    from time_tl.infrastructure.db.engine import create_session_factory
    from time_tl.infrastructure.db.power_sources import PostgresPowerSourceRepository
    from time_tl.runtime.power import PostgresPowerSourceAdminPort

    return PostgresPowerSourceAdminPort(
        PostgresPowerSourceRepository(create_session_factory(engine))
    )


def _energy_api_admin_port(settings: Settings, engine: Any) -> Any | None:
    # Credential setup is available before the power feature is enabled.
    # Missing owner key fails closed; never create or rotate it automatically.
    if settings.owner_alerts_fernet_key is None:
        return None
    from time_tl.infrastructure.db.energy_api_budget import PostgresEnergyApiBudget
    from time_tl.infrastructure.db.energy_api_credentials import PostgresEnergyApiCredentials
    from time_tl.infrastructure.db.engine import create_session_factory
    from time_tl.runtime.power import AlertsEnergyAccessChecker

    return PostgresEnergyApiCredentials(
        create_session_factory(engine),
        settings.owner_alerts_fernet_key,
        checker=AlertsEnergyAccessChecker(PostgresEnergyApiBudget(create_session_factory(engine))),
    )


def _power_map_admin_port(engine: Any) -> Any:
    from time_tl.infrastructure.db.energy_api_budget import PostgresEnergyApiBudget
    from time_tl.infrastructure.db.engine import create_session_factory
    from time_tl.infrastructure.db.power_map import PostgresPowerMapRepository
    from time_tl.runtime.power_map_admin import PowerMapAdminPort

    factory = create_session_factory(engine)
    return PowerMapAdminPort(PostgresPowerMapRepository(factory), PostgresEnergyApiBudget(factory))


def _weather_bot_port(
    settings: Settings,
    repository: Any,
    authority: WeatherFeatureAuthority | None = None,
    admission_settings: Settings | None = None,
) -> Any | None:
    if not settings.weather_enabled:
        return None
    from time_tl.runtime.weather_bot import build_weather_bot_port

    return build_weather_bot_port(
        repository._factory,
        notifications_enabled=settings.weather_notifications_enabled,
        authority_guard=weather_authority_guard(authority, admission_settings or settings)
        if authority
        else None,
    )


def _weather_admin_port(
    settings: Settings, engine: Any, authority: WeatherFeatureAuthority | None = None
) -> Any | None:
    if not settings.weather_admin_enabled:
        return None
    from time_tl.infrastructure.db.engine import create_session_factory
    from time_tl.runtime.weather_admin import WeatherAdminPort

    return WeatherAdminPort(
        create_session_factory(engine),
        authority_guard=weather_authority_guard(authority, settings) if authority else None,
    )


def run_admin_web_service(settings: Settings) -> None:
    production_gate = None
    if settings.environment is Environment.PRODUCTION:
        from .production_release import validate_production_runtime_release

        production_gate = asyncio.run(validate_production_runtime_release(settings))
    weather_authority = _admit_weather_feature("admin-web", settings, production_gate)
    validate_runtime_contract(
        settings, "admin-web", _reviewed_map_capabilities(None, weather_authority)
    )
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
        owner_settings = None
        if (
            settings.owner_telegram_fernet_key is not None
            and settings.owner_alerts_fernet_key is not None
        ):
            from time_tl.admin.settings import OwnerSettingsService

            owner_settings = OwnerSettingsService(
                repository,
                settings.owner_telegram_fernet_key.get_secret_value(),
                settings.owner_alerts_fernet_key.get_secret_value(),
                default_poll=settings.alert_poll_interval_seconds,
                default_stale=settings.alert_stale_after_seconds,
            )
        app = create_async_admin_app(
            settings,
            repository,
            collector,
            owner_settings=owner_settings,
            proxy_policy=proxy_policy,
            bootstrap_artifact_path=settings.admin_bootstrap_artifact_path,
            power_admin=_power_admin_port(settings, engine),
            power_source_admin=_power_source_admin_port(settings, engine),
            energy_api=_energy_api_admin_port(settings, engine),
            power_map=_power_map_admin_port(engine),
            weather_admin=_weather_admin_port(settings, engine, weather_authority),
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


async def activate_owner_settings(settings: Settings, revision: int, evidence_id: str) -> None:
    """Operator-only activation gate used after review and exact-version approval."""

    if settings.environment is Environment.PRODUCTION:
        raise RuntimeContractError(
            "production activation requires the exact production activation path"
        )
    if settings.admin_backend is not AdminBackend.POSTGRES:
        raise RuntimeContractError("owner settings activation requires PostgreSQL admin backend")
    if settings.owner_telegram_fernet_key is None or settings.owner_alerts_fernet_key is None:
        raise RuntimeContractError("owner settings activation requires both encryption keys")
    from time_tl.admin.settings import OwnerSettingsService, OwnerSettingsValidationError

    engine, repository = _postgres_admin_repository(settings)
    service = OwnerSettingsService(
        repository,
        settings.owner_telegram_fernet_key.get_secret_value(),
        settings.owner_alerts_fernet_key.get_secret_value(),
        default_poll=settings.alert_poll_interval_seconds,
        default_stale=settings.alert_stale_after_seconds,
    )
    try:
        candidate = await service.activation_candidate(revision)
        if candidate.environment != settings.environment.value:
            raise OwnerSettingsValidationError(
                "Pending configuration targets a different environment."
            )
        await service.mark_active(revision, "operator", evidence_id)
    finally:
        await engine.dispose()


async def activate_production_owner_settings(settings: Settings, revision: int) -> None:
    """Consume an exact preflight receipt without repeating external probes."""

    if settings.environment is not Environment.PRODUCTION:
        raise RuntimeContractError("production activation requires production environment")
    if settings.admin_backend is not AdminBackend.POSTGRES:
        raise RuntimeContractError("production activation requires PostgreSQL admin backend")
    if settings.owner_config_source is not OwnerConfigSource.DATABASE:
        raise RuntimeContractError("production activation requires database-managed owner config")
    if settings.test_chat_allowlist:
        raise RuntimeContractError("production activation requires empty STAGING allowlist")
    if settings.owner_telegram_fernet_key is None or settings.owner_alerts_fernet_key is None:
        raise RuntimeContractError("production activation requires both encryption keys")

    from datetime import UTC, datetime

    from time_tl.admin.settings import OwnerSettingsService

    from .production_release import load_verified_release_attestation

    manifest, manifest_fingerprint = load_verified_release_attestation(
        expected_config_revision=revision
    )

    engine, repository = _postgres_admin_repository(settings)
    service = OwnerSettingsService(
        repository,
        settings.owner_telegram_fernet_key.get_secret_value(),
        settings.owner_alerts_fernet_key.get_secret_value(),
        default_poll=settings.alert_poll_interval_seconds,
        default_stale=settings.alert_stale_after_seconds,
    )
    try:
        await service.mark_production_active(
            revision,
            manifest_fingerprint,
            manifest.migration_head,
            datetime.now(UTC),
        )
    finally:
        await engine.dispose()


async def _assert_territory_runtime_ready(factory: Any) -> str:
    """Shared production startup gate; no health/polling loop starts before it passes."""

    from time_tl.infrastructure.db.territories import PostgresTerritoryCatalogRepository

    official_region_ids = frozenset(record.uid for record in load_catalog().records)
    repository = PostgresTerritoryCatalogRepository(
        factory,
        expected_region_ids=official_region_ids,
    )
    return await repository.assert_runtime_ready(
        provider="alerts.in.ua",
        expected_region_ids=official_region_ids,
    )


async def run_schedule_worker(settings: Settings) -> None:
    """Prepared production loop; capability gate remains closed pending release approval."""
    from .production_release import validate_production_runtime_release

    production_gate = await validate_production_runtime_release(settings)
    settings = await resolve_owner_runtime_settings(settings, production_gate=production_gate)
    validate_runtime_contract(settings, "schedule-worker")
    if settings.environment in {Environment.LOCAL, Environment.SIMULATION}:
        await run_inert_worker(settings, "schedule-worker")
        return
    from datetime import UTC, datetime

    from time_tl.application.schedule_worker import build_schedule_worker
    from time_tl.infrastructure.db.calendar_reminders import PostgresCalendarReminderRepository
    from time_tl.infrastructure.db.codec import FernetLessonLinkCodec
    from time_tl.infrastructure.db.engine import create_engine, create_session_factory
    from time_tl.infrastructure.db.workers import (
        PostgresScheduleWorkerRepository,
        PostgresWorkerHeartbeatRepository,
    )

    assert settings.lesson_link_fernet_key is not None
    engine = create_engine(settings.database_url.get_secret_value())
    factory = create_session_factory(engine)
    await _assert_territory_runtime_ready(factory)
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
    calendar_reminders = (
        PostgresCalendarReminderRepository(factory) if settings.calendar_reminders_enabled else None
    )

    async def loop() -> None:
        while True:
            now = datetime.now(UTC)
            await worker.run_once(now)
            if calendar_reminders is not None:
                await calendar_reminders.plan(now)
            await asyncio.sleep(settings.schedule_worker_interval_seconds)

    try:
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(
                serve_health(settings.health_host, settings.health_port, "schedule-worker")
            )
            tasks.create_task(loop())
            tasks.create_task(_owner_config_revision_watch_loop(settings))
            tasks.create_task(
                _heartbeat_loop(
                    heartbeat_repository,
                    "schedule-worker",
                    settings.owner_config_revision,
                )
            )
    finally:
        await engine.dispose()


async def _lease_must_remain_held(lease: Any, *, interval_seconds: float = 5.0) -> None:
    while True:
        await asyncio.sleep(interval_seconds)
        if not await lease.renew():
            raise RuntimeError("sender-worker lost singleton lease")


async def _serve_sender_singleton(lease: Any, serve: Any) -> None:
    if not await lease.acquire():
        raise RuntimeError("sender-worker failed to acquire singleton lease")
    await serve()


async def run_sender_worker(settings: Settings) -> None:
    """Prepared production delivery loop; gate prevents unapproved live sends."""
    from .production_release import validate_production_runtime_release

    production_gate = await validate_production_runtime_release(settings)
    admission_settings = settings
    power_authority = _admit_map_feature("sender-worker", admission_settings, production_gate)
    weather_authority = _admit_weather_feature("sender-worker", admission_settings, production_gate)
    settings = await resolve_owner_runtime_settings(settings, production_gate=production_gate)
    validate_runtime_contract(
        settings,
        "sender-worker",
        _reviewed_map_capabilities(power_authority, weather_authority),
    )
    if settings.environment in {Environment.LOCAL, Environment.SIMULATION}:
        await run_inert_worker(settings, "sender-worker")
        return
    from datetime import UTC, datetime

    from aiogram import Bot

    from time_tl.application.sender_worker import build_sender_worker
    from time_tl.infrastructure.db.codec import FernetLessonLinkCodec
    from time_tl.infrastructure.db.engine import create_engine, create_session_factory
    from time_tl.infrastructure.db.lease import dedicated_advisory_lease
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
    await _assert_territory_runtime_ready(factory)
    codec = FernetLessonLinkCodec(
        settings.lesson_link_fernet_key.get_secret_value().encode("ascii")
    )
    bot = Bot(token=settings.telegram_bot_token.get_secret_value())
    sender_repository = PostgresSenderRepository(
        factory,
        stale_after_seconds=settings.alert_stale_after_seconds,
        expected_owner_config_revision=settings.owner_config_revision,
        power_enabled=settings.power_outages_enabled
        and not settings.power_alerts_energy_api_enabled,
        power_map_notifications_enabled=(
            settings.power_outages_enabled
            and settings.power_alerts_energy_api_enabled
            and settings.power_map_reminders_enabled
            and (
                settings.environment is not Environment.PRODUCTION
                or power_authority is not None
                and power_authority.enabled("map_dispatch")
            )
        ),
        power_map_authority_guard=(
            feature_authority_guard(power_authority, admission_settings)
            if power_authority is not None and power_authority.enabled("map_dispatch")
            else None
        ),
        power_stale_after_seconds=settings.power_stale_after_seconds,
        expected_staging_allowlist=(
            settings.test_chat_allowlist if settings.environment is Environment.STAGING else None
        ),
        weather_notifications_enabled=settings.weather_notifications_enabled,
        calendar_reminders_enabled=settings.calendar_reminders_enabled,
        weather_authority_guard=(
            weather_authority_guard(weather_authority, admission_settings)
            if weather_authority
            else None
        ),
    )
    worker = build_sender_worker(
        sender_repository,
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

    async def retention_loop() -> None:
        while True:
            try:
                await sender_repository.prune_terminal_outbox(datetime.now(UTC))
            except Exception as exc:
                LOGGER.warning(
                    "terminal outbox retention failed: %s",
                    type(exc).__name__,
                )
            await asyncio.sleep(300)

    async def serve_owned(lease: Any) -> None:
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(
                serve_health(settings.health_host, settings.health_port, "sender-worker")
            )
            tasks.create_task(loop())
            tasks.create_task(retention_loop())
            tasks.create_task(_lease_must_remain_held(lease))
            tasks.create_task(_owner_config_revision_watch_loop(settings))
            tasks.create_task(
                _heartbeat_loop(
                    heartbeat_repository,
                    "sender-worker",
                    settings.owner_config_revision,
                )
            )

    try:
        async with dedicated_advisory_lease(
            settings.database_url.get_secret_value(), SENDER_ADVISORY_LOCK_KEY
        ) as lease:
            await _serve_sender_singleton(lease, lambda: serve_owned(lease))
    finally:
        await bot.session.close()
        await engine.dispose()


async def run_power_worker(settings: Settings) -> None:
    """One isolated service; no alert poller, bot or sender is started here."""
    if not settings.power_outages_enabled:
        raise RuntimeContractError("power worker is disabled")
    validate_runtime_contract(settings, "power-worker")
    if settings.power_alerts_energy_api_enabled:
        raise RuntimeContractError("API map planning requires the signed map-planner entrypoint")
    from datetime import UTC, datetime
    from hashlib import sha256

    from time_tl.application.power_notification_worker import PowerNotificationWorker
    from time_tl.application.power_outage_worker import PowerOutageWorker
    from time_tl.domain.power_outages.models import PowerProvider
    from time_tl.infrastructure.db.engine import create_engine, create_session_factory
    from time_tl.infrastructure.db.lease import dedicated_advisory_lease
    from time_tl.infrastructure.db.power import PostgresPowerRepository
    from time_tl.infrastructure.db.power_sources import PostgresPowerSourceRepository
    from time_tl.infrastructure.db.workers import PostgresWorkerHeartbeatRepository
    from time_tl.infrastructure.power_outages.cek import CekPowerScheduleProvider
    from time_tl.infrastructure.power_outages.dtek_html_provider import DtekHtmlProvider

    database_url = settings.database_url.get_secret_value()
    engine = create_engine(database_url)
    factory = create_session_factory(engine)
    repository = PostgresPowerRepository(
        factory, stale_after_seconds=settings.power_stale_after_seconds
    )
    heartbeat_repository = PostgresWorkerHeartbeatRepository(factory)
    regions = {
        PowerProvider.DTEK: "dn",
        PowerProvider.DTEK_KYIV: "k",
        PowerProvider.DTEK_KYIV_REGION: "kr",
        PowerProvider.DTEK_ODESA: "o",
        PowerProvider.DTEK_DONETSK: "d",
    }

    async def poll(provider: Any) -> None:
        raw = b"PWR" + sha256(provider.provider_id.value.encode()).digest()[:5]
        async with dedicated_advisory_lease(database_url, int.from_bytes(raw, "big")) as lease:
            worker = PowerOutageWorker(
                provider,
                repository,
                lease,
                poll_interval_seconds=settings.power_poll_interval_seconds,
            )
            while True:
                await worker.run_once(datetime.now(UTC))
                await asyncio.sleep(1)

    async def plan() -> None:
        worker = PowerNotificationWorker(repository)
        while True:
            await worker.run_once(datetime.now(UTC))
            await asyncio.sleep(2)

    try:
        await repository.initialize()
        await PostgresPowerSourceRepository(
            factory, stale_after_seconds=settings.power_stale_after_seconds
        ).initialize()
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(
                serve_health(settings.health_host, settings.health_port, "power-worker")
            )
            tasks.create_task(plan())
            tasks.create_task(
                _heartbeat_loop(
                    heartbeat_repository, "power-worker", settings.owner_config_revision
                )
            )
            for region in regions.values():
                if settings.power_dtek_enabled:
                    tasks.create_task(poll(DtekHtmlProvider(region=region)))
            if settings.power_cek_enabled:
                tasks.create_task(poll(CekPowerScheduleProvider()))
    finally:
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

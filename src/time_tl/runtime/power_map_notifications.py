"""Cache-only reminder planning role; delivery remains in the fenced sender."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from time_tl.application.power_notification_worker import PowerNotificationWorker
from time_tl.entrypoints.health import serve_health
from time_tl.infrastructure.db.engine import create_engine, create_session_factory
from time_tl.infrastructure.db.lease import dedicated_advisory_lease
from time_tl.infrastructure.db.power_map_notifications import PostgresPowerMapNotificationRepository

from .contracts import RuntimeContractError
from .power_map_release import MapFeatureAuthority, validate_feature_database, verify_feature_scope

POWER_MAP_REMINDER_LOCK_KEY = 0x54494D455052


async def run_map_planner(settings: Any, *, authority: MapFeatureAuthority) -> None:
    from time_tl.infrastructure.db.workers import PostgresWorkerHeartbeatRepository

    if (
        not isinstance(authority, MapFeatureAuthority)
        or authority.role != "map-planner"
        or not authority.enabled("map_planner", settings)
        or verify_feature_scope("map-planner", settings) != authority
    ):
        raise RuntimeContractError("signed public map planner authority is required")
    engine = create_engine(settings.database_url.get_secret_value())
    try:
        factory = create_session_factory(engine)
        async with dedicated_advisory_lease(
            settings.database_url.get_secret_value(),
            POWER_MAP_REMINDER_LOCK_KEY,
        ) as lease:
            if not await lease.acquire():
                raise RuntimeError("power_map_reminder_planner_already_running")
            lease_guard = asyncio.Lock()

            async def renew() -> None:
                async with lease_guard:
                    if not await lease.renew():
                        raise RuntimeContractError("power map planner lease was lost")

            def current_authority() -> None:
                if verify_feature_scope("map-planner", settings) != authority:
                    raise RuntimeContractError("power map planner authority changed")

            async def commit_guard(session: Any, now: datetime) -> None:
                # The repository invokes this before user/cache locks and again
                # immediately before transaction exit. Owner locks span commit.
                if now.tzinfo is None:
                    raise RuntimeContractError("planner guard time is invalid")
                await renew()
                current_authority()
                await validate_feature_database(session, authority, lock=True)
                await renew()

            repository = PostgresPowerMapNotificationRepository(
                factory,
                stale_after_seconds=settings.power_stale_after_seconds,
                allowed_chat_ids=None,
                authority_guard=commit_guard,
            )
            worker = PowerNotificationWorker(repository)

            async def check() -> None:
                await renew()
                current_authority()
                async with factory() as session, session.begin():
                    await validate_feature_database(session, authority)

            await check()

            async def plan() -> None:
                while True:
                    await check()
                    await worker.run_once(datetime.now(UTC))
                    await asyncio.sleep(2)

            async def watch() -> None:
                while True:
                    await check()
                    await asyncio.sleep(2)

            async def heartbeat() -> None:
                heartbeat_repository = PostgresWorkerHeartbeatRepository(factory)
                while True:
                    await check()
                    await heartbeat_repository.upsert(
                        worker_name="map-planner",
                        instance_id="public-map-planner",
                        status="healthy",
                        heartbeat_at=datetime.now(UTC),
                        details={"applied_config_revision": authority.owner_revision},
                    )
                    await asyncio.sleep(15)

            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(plan())
                tasks.create_task(watch())
                tasks.create_task(
                    serve_health(settings.health_host, settings.health_port, "map-planner")
                )
                tasks.create_task(heartbeat())
    finally:
        await engine.dispose()

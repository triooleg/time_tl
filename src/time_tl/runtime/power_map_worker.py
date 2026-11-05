"""Isolated API collector role: DB and encrypted API credential only, no Telegram."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from datetime import UTC, datetime
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from time_tl.application.power_map_worker import PowerMapWorker
from time_tl.infrastructure.db.energy_api_budget import PostgresEnergyApiBudget
from time_tl.infrastructure.db.energy_api_credentials import PostgresEnergyApiCredentials
from time_tl.infrastructure.db.engine import create_engine, create_session_factory
from time_tl.infrastructure.db.lease import dedicated_advisory_lease
from time_tl.infrastructure.db.power_map import PostgresPowerMapRepository

from .weather_release import verify_weather_scope, weather_authority_guard

POWER_MAP_LOCK_KEY = 0x54494D45504D


class MapCollectorSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore", hide_input_in_errors=True)
    enabled: bool = Field(default=False, alias="POWER_MAP_COLLECTOR_ENABLED")
    dynamic_coverage: bool = Field(default=False, alias="DYNAMIC_COVERAGE_ENABLED")
    service_role: Literal["power-map-collector"] = Field(
        default="power-map-collector", alias="SERVICE_ROLE")
    environment: Literal["production", "staging", "local", "test"] = Field(
        default="production", alias="TIME_TL_ENV"
    )
    database_url: SecretStr = Field(alias="DATABASE_URL")
    seed: SecretStr = Field(alias="OWNER_ALERTS_FERNET_KEY")


async def run(settings: MapCollectorSettings, *, once: bool = False, health: bool = False) -> None:
    authority = None
    if settings.dynamic_coverage and settings.environment == "production":
        authority = verify_weather_scope("power-map-collector", settings)
        if not authority.enabled("dynamic_power_coverage"):
            raise RuntimeError("dynamic_coverage_production_release_not_authorized")
    if not settings.enabled:
        raise RuntimeError("power_map_collector_disabled")
    engine = create_engine(settings.database_url.get_secret_value())
    try:
        factory = create_session_factory(engine)
        repository = PostgresPowerMapRepository(factory, dynamic_coverage=settings.dynamic_coverage,
            authority_guard=weather_authority_guard(authority, settings) if authority else None)
        coverage = None
        if settings.dynamic_coverage:
            from time_tl.infrastructure.db.geographic_coverage import (
                PostgresActiveGeographicCoverage,
            )

            coverage = PostgresActiveGeographicCoverage(factory)
        if health:
            status = await repository.statistics(datetime.now(UTC))
            finished = status.get("last_finished_at")
            if (
                not isinstance(finished, str)
                or not 0
                <= (datetime.now(UTC) - datetime.fromisoformat(finished)).total_seconds()
                <= 900
            ):
                raise RuntimeError("power_map_collector_heartbeat_stale")
            print(json.dumps({"status": "PASS", "scope": "collector_heartbeat_only"}))
            return
        await repository.initialize()
        async with dedicated_advisory_lease(
            settings.database_url.get_secret_value(),
            POWER_MAP_LOCK_KEY,
        ) as lease:
            if not await lease.acquire():
                raise RuntimeError("power_map_collector_already_running")
            worker = PowerMapWorker(
                repository,
                PostgresEnergyApiCredentials(factory, settings.seed),
                PostgresEnergyApiBudget(factory),
                lease,
                coverage=coverage,
            )
            while True:
                committed = await worker.run_once()
                if once:
                    print(
                        json.dumps(
                            {
                                "cycle_committed": committed,
                                "map": await repository.statistics(datetime.now(UTC)),
                                "budget": await PostgresEnergyApiBudget(factory).statistics(
                                    datetime.now(UTC)
                                ),
                            }
                        )
                    )
                    return
                await asyncio.sleep(5)
    finally:
        await engine.dispose()


def main() -> None:
    # Never render settings or exception chains (driver errors can include credentials).
    logging.disable(logging.CRITICAL)
    try:
        args = sys.argv[1:]
        if args not in ([], ["--once"], ["--health"]):
            raise ValueError("invalid_collector_command")
        settings = MapCollectorSettings()  # type: ignore[call-arg]  # validated environment
        asyncio.run(run(settings, once=args == ["--once"], health=args == ["--health"]))
    except Exception:
        raise SystemExit("power_map_collector_failed; inspect local metadata") from None


if __name__ == "__main__":
    main()

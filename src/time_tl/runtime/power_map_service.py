"""Production-only DB planner entrypoint with no token, API key or decryption seed."""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import os
from collections.abc import Mapping
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from time_tl.infrastructure.db.engine import create_engine, create_session_factory

from .contracts import RuntimeContractError
from .power_map_release import MapFeatureAuthority, validate_feature_database, verify_feature_scope


class MapPlannerSettings(BaseSettings):
    """Only the DB credential is admitted. No project .env is opened."""

    model_config = SettingsConfigDict(
        env_file=None, extra="ignore", case_sensitive=False, hide_input_in_errors=True
    )
    environment: Literal["production"] = Field(default="production", alias="TIME_TL_ENV")
    service_role: Literal["map-planner"] = Field(default="map-planner", alias="SERVICE_ROLE")
    owner_config_source: Literal["database"] = Field(
        default="database", alias="OWNER_CONFIG_SOURCE"
    )
    owner_config_revision: int = Field(default=15, ge=15, le=15, alias="OWNER_CONFIG_REVISION")
    database_url: SecretStr = Field(alias="DATABASE_URL")
    power_outages_enabled: bool = Field(default=False, alias="POWER_OUTAGES_ENABLED")
    power_alerts_energy_api_enabled: bool = Field(
        default=False, alias="POWER_ALERTS_ENERGY_API_ENABLED"
    )
    power_map_reminders_enabled: bool = Field(default=False, alias="POWER_MAP_REMINDERS_ENABLED")
    power_address_lookup_enabled: bool = Field(default=False, alias="POWER_ADDRESS_LOOKUP_ENABLED")
    power_stale_after_seconds: int = Field(
        default=1800, ge=1, le=86400, alias="POWER_STALE_AFTER_SECONDS"
    )
    health_host: Literal["127.0.0.1"] = Field(default="127.0.0.1", alias="HEALTH_HOST")
    health_port: int = Field(default=8080, ge=1024, le=65535, alias="HEALTH_PORT")

    @property
    def test_chat_allowlist(self) -> tuple[int, ...]:
        return ()

    @field_validator("power_address_lookup_enabled")
    @classmethod
    def no_address_transport(cls, enabled: bool) -> bool:
        if enabled:
            raise ValueError("planner cannot enable an address transport")
        return False

    @field_validator("database_url")
    @classmethod
    def internal_postgres(cls, value: SecretStr) -> SecretStr:
        try:
            parsed = urlsplit(value.get_secret_value())
            if parsed.scheme != "postgresql+asyncpg" or not parsed.hostname or not parsed.path:
                raise ValueError
            try:
                address = ipaddress.ip_address(parsed.hostname)
            except ValueError:
                address = None
            if address is not None and not (address.is_private or address.is_loopback):
                raise ValueError
        except ValueError:
            raise ValueError("planner requires internal PostgreSQL") from None
        return value


def validate_planner_environment(environment: Mapping[str, str]) -> None:
    """Reject accidental secret inheritance instead of silently carrying it."""
    for key in environment:
        upper = key.upper()
        if upper.endswith(("_TOKEN", "_FERNET_KEY", "_API_KEY", "_PRIVATE_KEY")):
            raise RuntimeContractError("map planner must not receive transport or signing secrets")
    raw_allowlist = environment.get("TEST_CHAT_ALLOWLIST", "")
    if raw_allowlist.strip():
        raise RuntimeContractError("public map planner does not accept a staging allowlist")


async def validate_planner_startup(settings: MapPlannerSettings) -> MapFeatureAuthority:
    """Use the normal signed base release and equivalent public DB checks.

    The normal combined role helper selects the encrypted ACTIVE payload for
    bot/sender resolution. This DB-only role instead projects only its public
    policy fields, preserving the same production invariants.
    """
    authority = verify_feature_scope("map-planner", settings)
    if not authority.enabled("map_planner", settings):
        raise RuntimeContractError("signed public map planner capability is disabled")
    engine = create_engine(settings.database_url.get_secret_value())
    try:
        factory = create_session_factory(engine)
        async with factory() as session, session.begin():
            await validate_feature_database(session, authority)
    finally:
        await engine.dispose()
    return authority


async def run_public_map_service(*, check_only: bool = False) -> None:
    validate_planner_environment(os.environ)
    settings = MapPlannerSettings()  # type: ignore[call-arg]  # validated environment only
    authority = await validate_planner_startup(settings)
    if check_only:
        return
    from .power_map_notifications import run_map_planner

    await run_map_planner(settings, authority=authority)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        asyncio.run(run_public_map_service(check_only=args.check))
    except Exception:
        print(
            json.dumps(
                {
                    "status": "REJECTED",
                    "service": "map-planner",
                    "code": "MAP_PUBLIC_RUNTIME_NOT_AUTHORIZED",
                }
            )
        )
        raise SystemExit(1) from None
    if args.check:
        print(json.dumps({"status": "PASS", "scope": "public_planner_startup_only"}))


if __name__ == "__main__":
    main()

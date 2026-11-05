"""DB-only process liveness; never proof that a weather forecast is accurate."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import text

ROLES = frozenset({"weather-worker", "weather-planner"})


def _path(role: str, directory: Path) -> Path:
    if role not in ROLES:
        raise ValueError("invalid_weather_health_role")
    return directory / ("time-tl-" + role + "-health.json")


def write_weather_marker(
    role: str, instance: str, now: datetime, *, directory: Path = Path("/tmp")
) -> None:
    target = _path(role, directory)
    if str(UUID(instance)) != instance or now.tzinfo is None:
        raise ValueError("invalid_weather_health_marker")
    temporary = target.with_name(target.name + "." + uuid4().hex)
    descriptor = os.open(
        temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(
                {
                    "version": 1,
                    "role": role,
                    "instance_id": instance,
                    "heartbeat_at": now.isoformat(),
                },
                stream,
            )
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def read_weather_marker(role: str, *, directory: Path = Path("/tmp")) -> dict[str, str | int]:
    descriptor = os.open(_path(role, directory), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
        raw = stream.read(1025)
    if len(raw) > 1024:
        raise ValueError("invalid_weather_health_marker")

    def pairs(items: list[tuple[str, str | int]]) -> dict[str, str | int]:
        value: dict[str, str | int] = {}
        for key, item in items:
            if key in value:
                raise ValueError("invalid_weather_health_marker")
            value[key] = item
        return value

    marker = json.loads(raw, object_pairs_hook=pairs)
    if (
        type(marker) is not dict
        or set(marker) != {"version", "role", "instance_id", "heartbeat_at"}
        or type(marker["version"]) is not int
        or marker["version"] != 1
        or marker["role"] != role
        or type(marker["instance_id"]) is not str
        or str(UUID(marker["instance_id"])) != marker["instance_id"]
        or type(marker["heartbeat_at"]) is not str
    ):
        raise ValueError("invalid_weather_health_marker")
    return marker


def health_is_current(
    marker: dict[str, str | int],
    *,
    instance: str,
    heartbeat_at: datetime,
    status: str,
    now: datetime,
) -> bool:
    local = datetime.fromisoformat(str(marker["heartbeat_at"]))
    return (
        local.tzinfo is not None
        and heartbeat_at.tzinfo is not None
        and now.tzinfo is not None
        and marker["instance_id"] == instance
        and local == heartbeat_at
        and timedelta(seconds=-5) <= now - heartbeat_at < timedelta(seconds=90)
        and status in {"PASS", "WAITING", "DISABLED", "PARTIAL"}
    )


async def check(role: str) -> None:
    from time_tl.infrastructure.db.engine import create_engine, create_session_factory

    from .weather_notification_service import WeatherPlannerSettings
    from .weather_release import verify_weather_scope, weather_authority_guard
    from .weather_worker import WeatherWorkerSettings, validate_weather_environment

    validate_weather_environment(os.environ)
    settings = WeatherWorkerSettings() if role == "weather-worker" else WeatherPlannerSettings()  # type: ignore[call-arg]
    marker = read_weather_marker(role)
    authority = (
        verify_weather_scope(role, settings) if settings.environment == "production" else None
    )
    engine = create_engine(settings.database_url.get_secret_value(), pool_size=1, max_overflow=0)
    try:
        async with asyncio.timeout(5), create_session_factory(engine)() as session, session.begin():
            now = datetime.now(UTC)
            if authority is not None:
                await weather_authority_guard(authority, settings)(session, now)
            row = (
                await session.execute(
                    text(
                        "SELECT instance_id,heartbeat_at,status "
                        "FROM worker_heartbeats WHERE worker_name=:role"
                    ),
                    {"role": role},
                )
            ).one_or_none()
            if row is None or not health_is_current(
                marker, instance=row[0], heartbeat_at=row[1], status=row[2], now=now
            ):
                raise ValueError("weather_heartbeat_unavailable")
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=sorted(ROLES))
    role = parser.parse_args().role
    try:
        asyncio.run(check(role))
    except Exception:
        raise SystemExit(1) from None
    print("PASS process_liveness_only")


if __name__ == "__main__":
    main()

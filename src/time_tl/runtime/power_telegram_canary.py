"""Deadline-bound Telegram UI canary; no sender or alert worker is composed."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from time_tl.bot.async_service import build_async_router
from time_tl.bot.power_canary_guard import PowerCanaryGuard
from time_tl.bot.safety import BoundedMemoryStorage, BoundedTelegramIngressGuard
from time_tl.config import Settings
from time_tl.domain.power_outages.models import PowerProvider
from time_tl.entrypoints.health import serve_health
from time_tl.infrastructure.db.engine import create_engine, create_session_factory
from time_tl.infrastructure.db.lease import dedicated_advisory_lease
from time_tl.infrastructure.db.models import RuntimeSetting
from time_tl.runtime.power_map_bot import build_power_map_bot_port

_ACTION: ContextVar[str] = ContextVar("power_canary_action", default="other")
_PHASE = "initial"


class PollingConflict(BaseException):
    """Escape aiogram's ordinary retry loop on a second poller, without token logs."""


def canary_parameters() -> tuple[frozenset[int], datetime, str]:
    if os.environ.get("POWER_TELEGRAM_CANARY_ENABLED") != "true":
        raise ValueError("power_telegram_canary_disabled")
    raw = os.environ.get("POWER_TELEGRAM_CANARY_ALLOWED_ID", "")
    if not raw.isascii() or not raw.isdecimal() or not 1 <= len(raw) <= 19 or int(raw) <= 0:
        raise ValueError("invalid_power_canary_allowlist")
    expires = datetime.fromisoformat(os.environ.get("POWER_TELEGRAM_CANARY_EXPIRES_AT", ""))
    now = datetime.now(UTC)
    if expires.tzinfo is None or not 0 < (expires - now).total_seconds() <= 900:
        raise ValueError("invalid_power_canary_deadline")
    run_id = str(UUID(os.environ.get("POWER_TELEGRAM_CANARY_RUN_ID", "")))
    return frozenset({int(raw)}), expires, run_id


def verify_overlay() -> bool:
    from .production_release import _public_key

    path = Path("/run/time-tl-power-telegram-overlay.json")
    raw = path.read_bytes()
    _public_key().verify(
        base64.b64decode(
            Path("/run/time-tl-power-telegram-overlay.ed25519").read_bytes().strip(), validate=True
        ),
        raw,
    )
    record = json.loads(raw)
    if (
        record["kind"] != "time-tl-telegram-power-canary-overlay"
        or record["allowlist_count"] != 1
        or record["maximum_window_seconds"] != 900
        or record["messages_outside_allowlist"] is not False
        or type(record.get("map_reminder_preferences_enabled", False)) is not bool
    ):
        raise ValueError("invalid_power_canary_attestation")
    root = Path(__file__).resolve().parents[2]
    for relative, expected in record["source"]["files"].items():
        if not relative.startswith("overlay/time_tl/"):
            continue
        target = (root / relative.removeprefix("overlay/")).resolve()
        if (
            not target.is_relative_to(root)
            or hashlib.sha256(target.read_bytes()).hexdigest() != expected
        ):
            raise ValueError("power_canary_installed_source_mismatch")
    # Preference previews/saves only; this role composes no planner or sender.
    return record.get("map_reminder_preferences_enabled", False) is True


def action(update: Any) -> str:
    message = getattr(update, "message", None)
    text = getattr(message, "text", None)
    if (
        isinstance(text, str)
        and text.split(maxsplit=1)
        and text.split(maxsplit=1)[0].split("@", 1)[0] == "/start"
    ):
        return "start"
    callback = getattr(getattr(update, "callback_query", None), "data", None)
    if callback == "menu:power":
        return "power_menu"
    if isinstance(callback, str):
        if callback.startswith("pw:day:"):
            return "power_day"
        if callback.startswith("pw:save:"):
            return "power_save"
        if callback.startswith("pa:"):
            return "power_address"
        if callback in {"menu:myday", "menu:calendars", "menu:homework"}:
            return callback.removeprefix("menu:")
    return "other"


class CanaryObservations:
    def __init__(self, factory: Any, run_id: str, expires: datetime) -> None:
        self._factory, self.key, self.expires = factory, "power_telegram_canary:" + run_id, expires

    async def record(self, field: str) -> None:
        if field not in {
            "heartbeat",
            "completed_start",
            "completed_power_menu",
            "completed_power_day",
            "completed_power_save",
            "completed_power_address",
            "completed_myday",
            "completed_calendars",
            "completed_homework",
            "completed_other",
            "delivered_response",
        }:
            raise ValueError("invalid_power_canary_observation")
        now = datetime.now(UTC)
        async with self._factory() as session, session.begin():
            await session.execute(
                insert(RuntimeSetting)
                .values(
                    key=self.key,
                    value={
                        "version": 1,
                        "expires_at": self.expires.isoformat(),
                        "counters": {},
                    },
                    updated_by="power-telegram-canary",
                )
                .on_conflict_do_nothing(index_elements=[RuntimeSetting.key])
            )
            row = (
                await session.execute(
                    select(RuntimeSetting).where(RuntimeSetting.key == self.key).with_for_update()
                )
            ).scalar_one()
            value = dict(row.value)
            value["last_observed_at"] = now.isoformat()
            value["counters"] = {**value["counters"], field: value["counters"].get(field, 0) + 1}
            row.value = value

    async def __call__(self, handler: Any, update: Any, data: Any) -> Any:
        selected = action(update)
        token = _ACTION.set(selected)
        try:
            result = await handler(update, data)
            await self.record("completed_" + selected)
            return result
        finally:
            _ACTION.reset(token)


async def run(
    settings: Settings,
    allowed: frozenset[int],
    expires: datetime,
    run_id: str,
    *,
    reminder_preferences: bool = False,
) -> None:
    global _PHASE
    from aiogram import Bot, Dispatcher

    from .contracts import validate_runtime_contract
    from .imports import (
        DurableAsyncTelegramApplication,
        DurableImportCoordinator,
        PostgresTransactionalProfileStore,
    )
    from .production_release import validate_production_runtime_release
    from .services import (
        _owner_config_revision_watch_loop,
        postgres_bot_dependencies,
        resolve_owner_runtime_settings,
    )

    _PHASE = "owner_release"
    gate = await validate_production_runtime_release(settings)
    settings = await resolve_owner_runtime_settings(settings, production_gate=gate)
    validate_runtime_contract(settings, "bot")
    if settings.telegram_bot_token is None or settings.owner_alerts_fernet_key is None:
        raise ValueError("power_canary_required_credentials_missing")
    _PHASE = "database"
    engine = create_engine(settings.database_url.get_secret_value())
    guard = PowerCanaryGuard(allowed, expires_at=expires)
    factory = create_session_factory(engine)
    observations = CanaryObservations(factory, run_id, expires)
    token = settings.telegram_bot_token.get_secret_value()
    lease_key = int.from_bytes(hashlib.sha256(token.encode()).digest()[:8], "big") & (2**63 - 1)
    bot = Bot(token=token)

    async def outbound(make_request: Any, bot: Any, method: Any) -> Any:
        from aiogram.exceptions import TelegramConflictError

        try:
            result = await guard.request(make_request, bot, method)
        except TelegramConflictError:
            raise PollingConflict("power_canary_duplicate_poller") from None
        if method.__class__.__name__ not in {"GetMe", "GetUpdates", "DeleteWebhook"}:
            await observations.record("delivered_response")
        return result

    bot.session.middleware(outbound)
    try:
        _PHASE = "singleton_lease"
        async with dedicated_advisory_lease(
            settings.database_url.get_secret_value(), lease_key
        ) as lease:
            if not await lease.acquire():
                raise RuntimeError("power_canary_polling_lease_unavailable")
            async with postgres_bot_dependencies(settings) as parts:
                repository, resolver, regions, imports, confirmation, catalog, _ = parts[:7]
                homework, calendars = parts[-2:]
                power = build_power_map_bot_port(
                    factory,
                    settings.owner_alerts_fernet_key,
                    notifications_enabled=reminder_preferences,
                )
                application = DurableAsyncTelegramApplication(
                    regions=regions,
                    resolver=resolver,
                    repository=repository,
                    region_confirmation=confirmation,
                    territory_catalog=catalog,
                    durable_imports=DurableImportCoordinator(
                        imports, PostgresTransactionalProfileStore(repository)
                    ),
                    onboarding_text=(
                        "🧪 Коротка перевірка Time TL для вашого тестового акаунта. "
                        "Фонові сповіщення на час перевірки призупинено. "
                        "Відкрийте ⚡ Електроенергія. Ваші збережені дані залишаються в базі."
                    ),
                    homework_enabled=settings.homework_enabled,
                    homework_port=homework,
                    my_day_enabled=settings.my_day_enabled,
                    calendars_enabled=settings.calendars_enabled,
                    calendar_port=calendars,
                    power_outages_enabled=True,
                    power_port=power,
                    power_address_port=power.address_port,
                    power_supported_providers=(PowerProvider.DTEK, PowerProvider.CEK),
                    power_notifications_available=reminder_preferences,
                    power_forecast_available=False,
                )
                _PHASE = "router"
                dispatcher = Dispatcher(storage=BoundedMemoryStorage())
                dispatcher.update.outer_middleware(guard)
                dispatcher.update.outer_middleware(observations)
                dispatcher.include_router(
                    build_async_router(application, ingress_guard=BoundedTelegramIngressGuard())
                )

                async def heartbeat() -> None:
                    while True:
                        if not await lease.renew():
                            raise RuntimeError("power_canary_polling_lease_lost")
                        await observations.record("heartbeat")
                        await asyncio.sleep(5)

                remaining = (expires - datetime.now(UTC)).total_seconds()
                if remaining <= 0:
                    raise RuntimeError("power_canary_expired")
                _PHASE = "polling"
                async with asyncio.timeout(remaining), asyncio.TaskGroup() as tasks:
                    tasks.create_task(dispatcher.start_polling(bot, handle_signals=False))
                    tasks.create_task(heartbeat())
                    tasks.create_task(_owner_config_revision_watch_loop(settings))
                    tasks.create_task(
                        serve_health(
                            settings.health_host, settings.health_port, "power-telegram-canary"
                        )
                    )
    finally:
        await bot.session.close()
        await engine.dispose()


def main() -> None:
    global _PHASE
    logging.disable(logging.CRITICAL)
    try:
        _PHASE = "overlay"
        reminder_preferences = verify_overlay()
        _PHASE = "parameters"
        allowed, expires, run_id = canary_parameters()
        settings = Settings(_env_file=None)  # type: ignore[call-arg]  # environment validated
        asyncio.run(
            run(
                settings,
                allowed,
                expires,
                run_id,
                reminder_preferences=reminder_preferences,
            )
        )
    except BaseException:
        print(
            json.dumps({"status": "ENDED", "code": "POWER_TELEGRAM_CANARY_ENDED", "phase": _PHASE})
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()

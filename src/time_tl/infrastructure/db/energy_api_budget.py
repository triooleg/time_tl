"""Account-wide durable API admission. Reserve before I/O; never refund failures."""

from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from time_tl.domain.power_outages.models import aware

from .models import RuntimeSetting

SETTING_KEY = "power_api:account_budget"
KYIV = ZoneInfo("Europe/Kyiv")


class QuotaDeferred(RuntimeError):
    def __init__(self, retry_after_seconds: float, reason: str) -> None:
        if reason not in {"quota_spacing", "quota_blocked", "quota_daily_exhausted"}:
            raise ValueError("invalid_quota_reason")
        self.retry_after_seconds = max(0.001, retry_after_seconds)
        self.reason = reason
        super().__init__(reason)


def _stamp(raw: object) -> datetime | None:
    if raw is None:
        return None
    if type(raw) is not str or len(raw) > 40:
        raise ValueError("invalid_energy_budget_state")
    try:
        return aware(datetime.fromisoformat(raw))
    except ValueError:
        raise ValueError("invalid_energy_budget_state") from None


def _empty(now: datetime) -> dict[str, Any]:
    return {
        "version": 1,
        "day": aware(now).astimezone(KYIV).date().isoformat(),
        "used": 0,
        "next_request_at": None,
        "blocked_until": None,
        "revision": 1,
    }


def _validated(value: object, now: datetime) -> dict[str, Any]:
    if type(value) is not dict or set(value) != set(_empty(now)):
        raise ValueError("invalid_energy_budget_state")
    if (
        type(value["version"]) is not int
        or value["version"] != 1
        or type(value["used"]) is not int
        or not 0 <= value["used"] <= 10000
        or type(value["revision"]) is not int
        or value["revision"] < 1
        or type(value["day"]) is not str
    ):
        raise ValueError("invalid_energy_budget_state")
    try:
        day = date.fromisoformat(value["day"])
    except ValueError:
        raise ValueError("invalid_energy_budget_state") from None
    today = aware(now).astimezone(KYIV).date()
    if day.isoformat() != value["day"] or day > today:
        raise ValueError("invalid_energy_budget_state")
    _stamp(value["next_request_at"])
    _stamp(value["blocked_until"])
    result = dict(value)
    if day < today:
        result.update(day=today.isoformat(), used=0)
    return result


class PostgresEnergyApiBudget:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        *,
        daily_limit: int = 10000,
        spacing_seconds: float = 0.25,
    ) -> None:
        if (
            type(daily_limit) is not int
            or not 1 <= daily_limit <= 10000
            or type(spacing_seconds) not in {int, float}
            or not math.isfinite(spacing_seconds)
            or not 0.25 <= spacing_seconds <= 60
        ):
            raise ValueError("invalid_energy_budget_policy")
        self._factory, self.daily_limit, self.spacing_seconds = (
            factory,
            daily_limit,
            spacing_seconds,
        )

    @staticmethod
    async def _locked(session: AsyncSession, now: datetime) -> RuntimeSetting:
        await session.execute(
            insert(RuntimeSetting)
            .values(
                key=SETTING_KEY,
                value=_empty(now),
                updated_by="energy-api-budget",
            )
            .on_conflict_do_nothing(index_elements=[RuntimeSetting.key])
        )
        return (
            await session.execute(
                select(RuntimeSetting).where(RuntimeSetting.key == SETTING_KEY).with_for_update()
            )
        ).scalar_one()

    @staticmethod
    async def _admission_now(session: AsyncSession, requested: datetime) -> datetime:
        # clock_timestamp() is actual wall time after the row lock was acquired;
        # transaction_timestamp()/caller time can both predate contention waits.
        # Offline test subclasses may override this method, never production wiring.
        return aware((await session.execute(text("SELECT clock_timestamp()"))).scalar_one())

    async def reserve(self, now: datetime) -> float:
        stamp = aware(now)
        deferred: tuple[float, str] | None = None
        async with self._factory() as session, session.begin():
            row = await self._locked(session, stamp)
            stamp = await self._admission_now(session, stamp)
            value = _validated(row.value, stamp)
            blocked, next_slot = _stamp(value["blocked_until"]), _stamp(value["next_request_at"])
            if blocked is not None and blocked > stamp:
                deferred = ((blocked - stamp).total_seconds(), "quota_blocked")
            elif value["used"] >= self.daily_limit:
                tomorrow = stamp.astimezone(KYIV).date() + timedelta(days=1)
                boundary = aware(datetime.combine(tomorrow, time.min, KYIV))
                deferred = ((boundary - stamp).total_seconds(), "quota_daily_exhausted")
            elif next_slot is not None and next_slot > stamp:
                deferred = ((next_slot - stamp).total_seconds(), "quota_spacing")
            else:
                value.update(
                    used=value["used"] + 1,
                    revision=value["revision"] + 1,
                    next_request_at=(stamp + timedelta(seconds=self.spacing_seconds)).isoformat(),
                )
            row.value = value
        if deferred is not None:
            raise QuotaDeferred(*deferred)
        return 0.0

    async def defer(self, now: datetime, retry_seconds: float) -> None:
        stamp = aware(now)
        if (
            type(retry_seconds) not in {int, float}
            or not math.isfinite(retry_seconds)
            or not 0 <= retry_seconds <= 86400
        ):
            raise ValueError("invalid_energy_budget_delay")
        async with self._factory() as session, session.begin():
            row = await self._locked(session, stamp)
            stamp = await self._admission_now(session, stamp)
            value = _validated(row.value, stamp)
            until = stamp + timedelta(seconds=retry_seconds)
            current = _stamp(value["blocked_until"])
            if current is None or until > current:
                value.update(blocked_until=until.isoformat(), revision=value["revision"] + 1)
            row.value = value

    async def statistics(self, now: datetime) -> dict[str, object]:
        async with self._factory() as session:
            row = await session.get(RuntimeSetting, SETTING_KEY)
            stamp = await self._admission_now(session, aware(now))
            value = _validated(row.value, stamp) if row is not None else _empty(stamp)
        return {
            "day": value["day"],
            "used": value["used"],
            "limit": self.daily_limit,
            "remaining": max(0, self.daily_limit - value["used"]),
            "next_request_at": value["next_request_at"],
            "blocked_until": value["blocked_until"],
            "revision": value["revision"],
        }

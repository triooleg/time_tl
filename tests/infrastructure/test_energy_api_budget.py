"""Durable account admission: synthetic ports plus opt-in isolated PostgreSQL."""

import asyncio
import copy
import os
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import pytest
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from time_tl.infrastructure.db.energy_api_budget import (
    SETTING_KEY,
    PostgresEnergyApiBudget,
    QuotaDeferred,
    _empty,
    _validated,
)
from time_tl.infrastructure.db.models import RuntimeSetting

NOW = datetime(2026, 11, 4, 12, tzinfo=UTC)


class MemoryFactory:
    def __init__(self):
        self.rows, self.lock, self.credential_revision = {}, asyncio.Lock(), 1

    def __call__(self):
        return MemorySession(self)


class MemorySession:
    def __init__(self, store):
        self.store = store

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def begin(self):
        return Transaction(self.store)

    async def get(self, model, key, **kwargs):
        assert model is RuntimeSetting
        return self.store.rows.get(key)


class Transaction:
    def __init__(self, store):
        self.store = store

    async def __aenter__(self):
        await self.store.lock.acquire()
        self.before = {key: copy.deepcopy(row.value) for key, row in self.store.rows.items()}

    async def __aexit__(self, kind, *args):
        if kind is not None:
            for key in list(self.store.rows):
                if key not in self.before:
                    del self.store.rows[key]
                else:
                    self.store.rows[key].value = self.before[key]
        self.store.lock.release()


class MemoryBudget(PostgresEnergyApiBudget):
    @staticmethod
    async def _admission_now(session, requested):
        return requested

    @staticmethod
    async def _locked(session, now):
        if SETTING_KEY not in session.store.rows:
            session.store.rows[SETTING_KEY] = RuntimeSetting(key=SETTING_KEY, value=_empty(now))
        return session.store.rows[SETTING_KEY]


class RequestedTimeBudget(PostgresEnergyApiBudget):
    """Deterministic SQL-state tests only; production never uses this subclass."""

    @staticmethod
    async def _admission_now(session, requested):
        return requested


async def test_reserve_charges_once_before_io_restart_and_spacing():
    store = MemoryFactory()
    budget = MemoryBudget(store)
    assert await budget.reserve(NOW) == 0
    restarted = MemoryBudget(store)
    with pytest.raises(QuotaDeferred) as error:
        await restarted.reserve(NOW)
    assert error.value.retry_after_seconds == 0.25
    assert (await restarted.statistics(NOW))["used"] == 1
    assert await restarted.reserve(NOW + timedelta(seconds=0.25)) == 0
    assert (await restarted.statistics(NOW))["used"] == 2


async def test_parallel_callers_cannot_reserve_same_slot():
    store = MemoryFactory()
    results = await asyncio.gather(
        *[MemoryBudget(store).reserve(NOW) for _ in range(8)], return_exceptions=True
    )
    assert results.count(0.0) == 1
    assert sum(isinstance(result, QuotaDeferred) for result in results) == 7


async def test_daily_limit_resets_only_at_kyiv_midnight_and_preserves_429():
    store = MemoryFactory()
    before = datetime(2026, 11, 4, 21, 59, 59, tzinfo=UTC)
    budget = MemoryBudget(store, daily_limit=1)
    await budget.reserve(before)
    with pytest.raises(QuotaDeferred) as error:
        await budget.reserve(before + timedelta(seconds=0.5))
    assert error.value.reason == "quota_daily_exhausted"
    await budget.defer(before, 3600)
    with pytest.raises(QuotaDeferred) as error:
        await MemoryBudget(store, daily_limit=1).reserve(before + timedelta(seconds=1))
    assert error.value.reason == "quota_blocked"
    assert (await budget.statistics(before + timedelta(seconds=1)))["used"] == 0
    await budget.reserve(before + timedelta(hours=1))
    assert (await budget.statistics(before + timedelta(hours=1)))["used"] == 1


async def test_429_cannot_be_shortened_and_failed_requests_are_not_refunded():
    store = MemoryFactory()
    budget = MemoryBudget(store)
    await budget.reserve(NOW)
    await budget.defer(NOW, 3600)
    await budget.defer(NOW + timedelta(seconds=1), 1)
    with pytest.raises(QuotaDeferred) as error:
        await MemoryBudget(store).reserve(NOW + timedelta(minutes=30))
    assert error.value.retry_after_seconds == 1800
    assert (await budget.statistics(NOW))["used"] == 1


@pytest.mark.parametrize(
    "change",
    [
        {"used": True},
        {"used": 10001},
        {"version": False},
        {"day": "2099-01-01"},
        {"extra": "not-allowed"},
        {"blocked_until": "naive"},
    ],
)
def test_corrupt_state_fails_closed(change):
    value = {**_empty(NOW), **change}
    with pytest.raises(ValueError):
        _validated(value, NOW)


@pytest.mark.parametrize("delay", [True, -1, 86401, float("nan"), float("inf")])
async def test_invalid_defer_never_writes(delay):
    store = MemoryFactory()
    with pytest.raises(ValueError):
        await MemoryBudget(store).defer(NOW, delay)
    assert not store.rows


def isolated_url():
    url = os.environ.get("POWER_MAP_TEST_DATABASE_URL")
    if not url:
        pytest.skip("POWER_MAP_TEST_DATABASE_URL unset; no network connection")
    parsed = urlsplit(url)
    assert parsed.scheme == "postgresql+asyncpg" and parsed.hostname == "127.0.0.1"
    assert parsed.username == "powermapcheck" and parsed.password is None
    assert parsed.path == "/powermapcheck" and parsed.port and parsed.port >= 1024
    assert not parsed.query and not parsed.fragment
    return url


async def test_native_pg_budget_row_lock_restart_and_global_cooldown():
    engine = create_async_engine(isolated_url())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    owned = False
    try:
        async with factory() as session:
            assert (
                await session.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one() == "0009_calendar_foundation"
            assert await session.get(RuntimeSetting, SETTING_KEY) is None
            owned = True
        budget = RequestedTimeBudget(factory)
        results = await asyncio.gather(
            *[RequestedTimeBudget(factory).reserve(NOW) for _ in range(8)],
            return_exceptions=True,
        )
        assert results.count(0.0) == 1
        assert sum(isinstance(value, QuotaDeferred) for value in results) == 7
        await budget.defer(NOW, 3600)
        with pytest.raises(QuotaDeferred):
            await RequestedTimeBudget(factory).reserve(NOW + timedelta(minutes=30))
        assert (await budget.statistics(NOW))["used"] == 1
    finally:
        if owned:
            async with factory() as session, session.begin():
                await session.execute(
                    delete(RuntimeSetting).where(RuntimeSetting.key == SETTING_KEY)
                )
        await engine.dispose()


async def test_native_pg_actual_postlock_clock_fences_queued_callers_and_retry_after():
    engine = create_async_engine(isolated_url())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    owned, tasks = False, []
    try:
        async with factory() as session:
            assert (
                await session.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one() == "0009_calendar_foundation"
            assert await session.get(RuntimeSetting, SETTING_KEY) is None
        owned = True
        budget = PostgresEnergyApiBudget(factory, spacing_seconds=3)
        await budget.reserve(datetime.now(UTC))
        async with factory() as holder, holder.begin():
            row = await holder.get(RuntimeSetting, SETTING_KEY, with_for_update=True)
            row.value = {**row.value, "used": 0, "next_request_at": None}
            for seconds in (300, 200, 100):
                tasks.append(
                    asyncio.create_task(
                        budget.reserve(datetime.now(UTC) - timedelta(seconds=seconds))
                    )
                )
            await asyncio.sleep(0.1)
            assert not any(task.done() for task in tasks)
            released = datetime.now(UTC)
        results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=10)
        assert results.count(0.0) == 1
        assert sum(isinstance(result, QuotaDeferred) for result in results) == 2
        async with factory() as session:
            row = await session.get(RuntimeSetting, SETTING_KEY)
            assert datetime.fromisoformat(row.value["next_request_at"]) >= released + timedelta(
                seconds=3
            )
        async with factory() as holder, holder.begin():
            await holder.get(RuntimeSetting, SETTING_KEY, with_for_update=True)
            task = asyncio.create_task(budget.defer(datetime.now(UTC) - timedelta(hours=1), 5))
            tasks.append(task)
            await asyncio.sleep(0.1)
            released = datetime.now(UTC)
        await asyncio.wait_for(task, timeout=10)
        async with factory() as session:
            row = await session.get(RuntimeSetting, SETTING_KEY)
            assert datetime.fromisoformat(row.value["blocked_until"]) >= released + timedelta(
                seconds=5
            )
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if owned:
            async with factory() as session, session.begin():
                await session.execute(
                    delete(RuntimeSetting).where(RuntimeSetting.key == SETTING_KEY)
                )
        await engine.dispose()

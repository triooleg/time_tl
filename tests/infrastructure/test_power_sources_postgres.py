"""Opt-in source policy persistence in the already migrated disposable powercheck DB."""

import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.infrastructure.test_power_postgres import checked_url
from time_tl.domain.power_outages.models import PowerProvider, PowerSourceKind
from time_tl.infrastructure.db.models import AuditLog
from time_tl.infrastructure.db.power_sources import (
    PostgresPowerSourceRepository,
    PowerSourceConflict,
)


@pytest.mark.asyncio
async def test_source_cas_restart_refresh_and_closed_network_gate():
    engine = create_async_engine(checked_url())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 11, 4, 15, tzinfo=UTC)
    provider, kind = PowerProvider.CEK, PowerSourceKind.ALERTS_ENERGY_HTML
    try:
        repository = PostgresPowerSourceRepository(factory)
        await repository.initialize()
        states = await repository.source_states(now)
        assert len(states) == len(PowerProvider) * len(PowerSourceKind)
        old = next(
            row
            for row in states
            if row["provider_id"] == provider.value and row["source_kind"] == kind.value
        )
        results = await asyncio.gather(
            *(
                repository.set_source_enabled(
                    provider,
                    kind,
                    True,
                    expected_revision=old["revision"],
                    actor_id="local-source-test",
                    now=now,
                )
                for _ in range(2)
            ),
            return_exceptions=True,
        )
        assert sum(type(result) is int for result in results) == 1
        assert sum(isinstance(result, PowerSourceConflict) for result in results) == 1
        assert await repository.request_source_refresh(
            provider,
            kind,
            actor_id="local-source-test",
            now=now,
        )
        restarted = PostgresPowerSourceRepository(factory)
        assert not await restarted.request_source_refresh(
            provider,
            kind,
            actor_id="local-source-test",
            now=now,
        )
        assert await restarted.acquire_source_slot(provider, kind, now, 300) is None
        state = next(
            row
            for row in await restarted.source_states(now)
            if row["provider_id"] == provider.value and row["source_kind"] == kind.value
        )
        assert state["enabled"] is True
        assert state["collection_allowed"] is False
        assert state["match_percent"] is None and state["compared_count"] == 0
        async with factory() as session:
            audit = (
                await session.scalars(select(AuditLog).where(AuditLog.actor == "local-source-test"))
            ).all()
        assert len(audit) >= 3
        assert all("token" not in str(row.redacted_details).lower() for row in audit)
    finally:
        await engine.dispose()

"""Compare cached source observations without extra official requests or outbox."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from time_tl.domain.power_outages.models import (
    PowerProvider,
    PowerScheduleSnapshot,
    PowerSourceKind,
)
from time_tl.domain.power_outages.sources import SourceSelection, select_snapshot


class PowerSourceRepository(Protocol):
    async def source_states(self, now: datetime) -> tuple[dict[str, object], ...]: ...

    async def cached_snapshots(
        self, provider: PowerProvider,
    ) -> tuple[PowerScheduleSnapshot, ...]: ...

    async def record_observation(
        self, snapshot: PowerScheduleSnapshot, official_snapshot: PowerScheduleSnapshot | None,
        now: datetime,
    ) -> None: ...


class PowerSourceManager:
    def __init__(
        self, repository: PowerSourceRepository, *, stale_after_seconds: int = 1800,
    ) -> None:
        if type(stale_after_seconds) is not int or stale_after_seconds < 1:
            raise ValueError("invalid_power_source_stale_limit")
        self._repository, self._stale_after = repository, stale_after_seconds

    async def record_observation(
        self, snapshot: PowerScheduleSnapshot, official_snapshot: PowerScheduleSnapshot | None,
        now: datetime,
    ) -> None:
        await self._repository.record_observation(snapshot, official_snapshot, now)

    async def select_cached(self, provider: PowerProvider, now: datetime) -> SourceSelection:
        states = await self._repository.source_states(now)
        selected_states = [state for state in states if state["provider_id"] == provider.value]
        enabled = frozenset(
            PowerSourceKind(str(state["source_kind"])) for state in selected_states
            if state["enabled"] is True
        )
        priorities = {
            PowerSourceKind(str(state["source_kind"])): int(str(state["priority"]))
            for state in selected_states
        }
        return select_snapshot(
            provider, await self._repository.cached_snapshots(provider), now,
            enabled_sources=enabled, priorities=priorities if priorities else None,
            # All current G0 gates remain CLOSED; configuration cannot approve them.
            approved_sources=frozenset(), stale_after_seconds=self._stale_after,
        )

"""SYNTHETIC scripted provider for explicitly offline environments only."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from time_tl.domain.power_outages.models import (
    PowerDaySchedule,
    PowerInterval,
    PowerProvider,
    PowerScheduleSnapshot,
)

from .base import Clock, PowerFetchError, PowerFetchErrorCode, unavailable_snapshot, utc_now

ScriptItem = PowerScheduleSnapshot | PowerFetchError | asyncio.CancelledError


def _validated_copy(value: PowerScheduleSnapshot, provider: PowerProvider) -> PowerScheduleSnapshot:
    if type(value) is not PowerScheduleSnapshot or value.provider is not provider:
        raise ValueError("invalid_synthetic_power_snapshot")
    days = tuple(
        PowerDaySchedule(
            provider=day.provider,
            group_id=day.group_id,
            local_date=day.local_date,
            schedule_type=day.schedule_type,
            intervals=tuple(
                PowerInterval(item.group_id, item.starts_at, item.ends_at, item.state)
                for item in day.intervals
            ),
            mapping_version=day.mapping_version,
            publication_status=day.publication_status,
        )
        for day in value.days
    )
    return PowerScheduleSnapshot(
        provider=value.provider,
        fetched_at=value.fetched_at,
        source_updated_at=value.source_updated_at,
        days=days,
        status=value.status,
        error_code=value.error_code,
        schema_version=value.schema_version,
        source_kind=value.source_kind,
        source_url=value.source_url,
        locality=value.locality,
        source_checked_at=value.source_checked_at,
    )


class FakePowerScheduleProvider:
    fixture_kind = "SYNTHETIC"

    def __init__(
        self,
        provider_id: PowerProvider,
        script: Sequence[ScriptItem],
        *,
        environment: str,
        fixture_kind: str = "SYNTHETIC",
        clock: Clock = utc_now,
    ) -> None:
        if not isinstance(environment, str) or environment not in {"local", "test", "simulation"}:
            raise ValueError("synthetic_power_provider_forbidden_in_environment")
        if fixture_kind != self.fixture_kind or not isinstance(provider_id, PowerProvider):
            raise ValueError("invalid_synthetic_power_fixture")
        if type(script) not in {tuple, list} or not 1 <= len(script) <= 1024:
            raise ValueError("invalid_synthetic_power_script")
        checked: list[ScriptItem] = []
        for item in script:
            if type(item) is PowerScheduleSnapshot:
                checked.append(_validated_copy(item, provider_id))
            elif type(item) in {PowerFetchError, asyncio.CancelledError}:
                checked.append(item)
            else:
                raise ValueError("invalid_synthetic_power_script_item")
        self._provider_id = provider_id
        self._script = tuple(checked)
        self._index = 0
        self._clock = clock

    @property
    def provider_id(self) -> PowerProvider:
        return self._provider_id

    async def fetch_schedules(self) -> PowerScheduleSnapshot:
        await asyncio.sleep(0)
        if self._index >= len(self._script):
            return await unavailable_snapshot(
                self.provider_id, PowerFetchErrorCode.FAKE_SCRIPT_EXHAUSTED, self._clock
            )
        item = self._script[self._index]
        self._index += 1
        if isinstance(item, (PowerFetchError, asyncio.CancelledError)):
            raise item
        return _validated_copy(item, self.provider_id)

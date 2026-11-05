"""Official CEK identity; an empty ambiguous form is not a power schedule."""

from __future__ import annotations

from time_tl.domain.power_outages.models import PowerProvider, PowerScheduleSnapshot

from .base import (
    Clock,
    PowerFetchErrorCode,
    PowerSourceGate,
    unavailable_snapshot,
    utc_now,
)

CEK_SOURCE_GATE = PowerSourceGate(
    PowerProvider.CEK, PowerFetchErrorCode.SOURCE_DATE_CONTRACT_UNVERIFIED
)


class CekPowerScheduleProvider:
    """Public form discovery did not establish an accepted dated record contract."""

    source_gate = CEK_SOURCE_GATE

    def __init__(self, *, clock: Clock = utc_now) -> None:
        self._clock = clock

    @property
    def provider_id(self) -> PowerProvider:
        return self.source_gate.provider

    async def fetch_schedules(self) -> PowerScheduleSnapshot:
        return await unavailable_snapshot(self.provider_id, self.source_gate.reason, self._clock)

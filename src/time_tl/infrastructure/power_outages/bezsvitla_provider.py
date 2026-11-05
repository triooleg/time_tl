"""Independent unavailable BezSvitla source; no 403 bypass or guessed HTML schema."""

from __future__ import annotations

from dataclasses import replace

from time_tl.domain.power_outages.models import (
    PowerProvider,
    PowerScheduleSnapshot,
    PowerSourceKind,
)

from .base import Clock, PowerFetchErrorCode, unavailable_snapshot, utc_now


class BezSvitlaProvider:
    gate_status = "BLOCKED_ACCESS_AND_SEMANTICS"
    collection_allowed = False

    def __init__(self, provider: PowerProvider, *, clock: Clock = utc_now) -> None:
        if not isinstance(provider, PowerProvider):
            raise ValueError("invalid_bezsvitla_provider")
        self._provider, self._clock = provider, clock

    @property
    def provider_id(self) -> PowerProvider:
        return self._provider

    async def fetch_schedules(self) -> PowerScheduleSnapshot:
        value = await unavailable_snapshot(
            self.provider_id, PowerFetchErrorCode.SOURCE_POLICY_UNVERIFIED, self._clock
        )
        return replace(value, source_kind=PowerSourceKind.BEZSVITLA,
                       source_url="https://bezsvitla.com.ua/")

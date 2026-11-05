"""Policy-closed HTML candidate with an explicitly synthetic offline test seam."""

from __future__ import annotations

from dataclasses import replace
from enum import StrEnum
from typing import Literal, Protocol

from time_tl.domain.power_outages.models import (
    PowerProvider,
    PowerScheduleSnapshot,
    PowerSourceKind,
)
from time_tl.infrastructure.power_outages.alerts_energy_normalizer import (
    REGION_PATHS,
    normalize_alerts_energy_page,
)
from time_tl.infrastructure.power_outages.alerts_energy_parser import parse_alerts_energy_html
from time_tl.infrastructure.power_outages.base import (
    Clock,
    PowerFetchError,
    PowerFetchErrorCode,
    unavailable_snapshot,
    utc_now,
)


class AlertsEnergyMode(StrEnum):
    LIVE = "LIVE"
    SYNTHETIC = "SYNTHETIC"


class AlertsEnergySyntheticTransport(Protocol):
    @property
    def synthetic(self) -> Literal[True]: ...

    async def get_html(self, url: str) -> bytes: ...


class AlertsEnergyHtmlProvider:
    """LIVE gate cannot be enabled by configuration or a constructor boolean."""

    gate_status: Literal["BLOCKED_POLICY"] = "BLOCKED_POLICY"

    def __init__(
        self,
        provider: PowerProvider,
        *,
        mode: AlertsEnergyMode = AlertsEnergyMode.LIVE,
        synthetic_transport: AlertsEnergySyntheticTransport | None = None,
        clock: Clock = utc_now,
    ) -> None:
        if not isinstance(provider, PowerProvider) or not isinstance(mode, AlertsEnergyMode):
            raise ValueError("invalid_alerts_energy_provider")
        if mode is AlertsEnergyMode.SYNTHETIC and (
            synthetic_transport is None or synthetic_transport.synthetic is not True
        ):
            raise ValueError("alerts_energy_requires_synthetic_transport")
        self._provider = provider
        self._mode = mode
        self._transport = synthetic_transport
        self._clock = clock

    @property
    def provider_id(self) -> PowerProvider:
        return self._provider

    async def fetch_schedules(self) -> PowerScheduleSnapshot:
        if self._mode is AlertsEnergyMode.LIVE:
            result = await unavailable_snapshot(
                self.provider_id,
                PowerFetchErrorCode.SOURCE_POLICY_UNVERIFIED,
                self._clock,
            )
            return replace(
                result,
                source_kind=PowerSourceKind.ALERTS_ENERGY_HTML,
                source_url="https://alerts.energy/" + REGION_PATHS[self.provider_id],
            )
        transport = self._transport
        if transport is None:  # defensive; constructor guarantees the synthetic seam
            raise ValueError("alerts_energy_requires_synthetic_transport")
        region = REGION_PATHS[self.provider_id]
        try:
            payload = await transport.get_html("https://alerts.energy/" + region)
            page = parse_alerts_energy_html(payload, region_slug=region)
            return normalize_alerts_energy_page(
                page, provider=self.provider_id, fetched_at=self._clock()
            )
        except (ValueError, RecursionError):
            raise PowerFetchError(PowerFetchErrorCode.INVALID_RESPONSE) from None

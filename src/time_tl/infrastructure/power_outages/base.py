"""Provider boundary; source approval and schedule availability are separate."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from time_tl.domain.power_outages.models import (
    GROUPS,
    PowerProvider,
    PowerScheduleSnapshot,
    SourceStatus,
)

MAX_RETRY_AFTER_SECONDS = 86400
Clock = Callable[[], datetime]


class PowerFetchErrorCode(StrEnum):
    TIMEOUT = "TIMEOUT"
    CONNECTION_ERROR = "CONNECTION_ERROR"
    HTTP_ERROR = "HTTP_ERROR"
    RATE_LIMITED = "RATE_LIMITED"
    ACCESS_DENIED = "ACCESS_DENIED"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    SOURCE_POLICY_UNVERIFIED = "SOURCE_POLICY_UNVERIFIED"
    SOURCE_DATE_CONTRACT_UNVERIFIED = "SOURCE_DATE_CONTRACT_UNVERIFIED"
    FAKE_SCRIPT_EXHAUSTED = "FAKE_SCRIPT_EXHAUSTED"


class PowerFetchError(RuntimeError):
    """Closed diagnostic values only: no URL, response body or credentials."""

    def __init__(
        self,
        code: PowerFetchErrorCode,
        *,
        status_code: int | None = None,
        retry_after_seconds: float | None = None,
        permanent: bool = False,
    ) -> None:
        if not isinstance(code, PowerFetchErrorCode) or type(permanent) is not bool:
            raise ValueError("invalid_power_fetch_error")
        if status_code is not None and (
            type(status_code) is not int or not 400 <= status_code <= 599
        ):
            raise ValueError("invalid_power_http_status")
        if retry_after_seconds is not None and (
            type(retry_after_seconds) not in {int, float}
            or not math.isfinite(retry_after_seconds)
            or not 0 <= retry_after_seconds <= MAX_RETRY_AFTER_SECONDS
        ):
            raise ValueError("invalid_power_retry_after")
        if code is PowerFetchErrorCode.RATE_LIMITED and status_code not in {None, 429}:
            raise ValueError("inconsistent_power_rate_limit")
        if code is PowerFetchErrorCode.ACCESS_DENIED and status_code not in {None, 401, 403}:
            raise ValueError("inconsistent_power_access_denial")
        self.code = code
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds
        self.permanent = permanent or status_code in {401, 403} or code in {
            PowerFetchErrorCode.ACCESS_DENIED,
            PowerFetchErrorCode.SOURCE_POLICY_UNVERIFIED,
            PowerFetchErrorCode.SOURCE_DATE_CONTRACT_UNVERIFIED,
            PowerFetchErrorCode.FAKE_SCRIPT_EXHAUSTED,
        }
        super().__init__(code.value)


@dataclass(frozen=True)
class PowerSourceGate:
    provider: PowerProvider
    reason: PowerFetchErrorCode
    official_url: str = field(init=False)
    live_access_approved: bool = field(default=False, init=False)
    verified_endpoint: None = field(default=None, init=False)
    external_mapping_version: None = field(default=None, init=False)
    supported_internal_groups: tuple[str, ...] = field(default=GROUPS, init=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.provider, PowerProvider)
            or not isinstance(self.reason, PowerFetchErrorCode)
            or self.reason not in {
            PowerFetchErrorCode.SOURCE_POLICY_UNVERIFIED,
            PowerFetchErrorCode.SOURCE_DATE_CONTRACT_UNVERIFIED,
            }
        ):
            raise ValueError("invalid_power_source_gate")
        object.__setattr__(self, "official_url", self.provider.official_url)


class PowerScheduleProvider(Protocol):
    @property
    def provider_id(self) -> PowerProvider: ...

    async def fetch_schedules(self) -> PowerScheduleSnapshot: ...


def utc_now() -> datetime:
    return datetime.now(UTC)


async def unavailable_snapshot(
    provider: PowerProvider, reason: PowerFetchErrorCode, clock: Clock
) -> PowerScheduleSnapshot:
    # Cooperative cancellation even for this no-network implementation.
    await asyncio.sleep(0)
    observed = clock()
    if type(observed) is not datetime:
        raise ValueError("invalid_power_clock")
    return PowerScheduleSnapshot(
        provider=provider,
        fetched_at=observed,
        source_updated_at=None,
        days=(),
        status=SourceStatus.UNAVAILABLE,
        error_code=reason.value,
    )
